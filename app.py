#!/usr/bin/env python3
"""
premiarr — First-episode calendar notifier.

Monitors an XMLTV guide every hour and updates an ICS calendar
whenever a show airs its first episode (S01E01, Season 1 Episode 1).
"""

import os
import sys
import re
import logging
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

import httpx
import xmltodict
from icalendar import Calendar, Event, vDatetime
from fastapi import FastAPI, Response
import uvicorn

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VERSION: str = "vAPP_VERSION"
XMLTV_URL: str = os.environ.get("XMLTV_URL", "")
ICS_PATH: Path = Path(os.environ.get("ICS_PATH", "/data/premiere.ics"))
CHECK_INTERVAL: int = int(os.environ.get("CHECK_INTERVAL", "3600"))  # seconds
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("premiarr")

log.info("premiarr %s starting up", VERSION)

# ---------------------------------------------------------------------------
# HTTP client (connection-pooled, reused)
# ---------------------------------------------------------------------------

client = httpx.Client(timeout=30.0)

# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

# Channel whitelist: comma-separated numbers (e.g., "1,2,5,6,9")
# If empty, all channels are included.
_channels_env: str = os.environ.get("CHANNELS", "").strip()
ALLOWED_CHANNELS: set[str] = (
    {ch.strip() for ch in _channels_env.split(",") if ch.strip()}
    if _channels_env
    else None
)

if ALLOWED_CHANNELS:
    log.info("Channel whitelist enabled: %s", sorted(ALLOWED_CHANNELS))
else:
    log.info("No channel whitelist — all channels included")


def fetch_xmltv() -> dict | None:
    """Download and parse the XMLTV feed."""
    if not XMLTV_URL:
        log.error("XMLTV_URL is not set")
        return None

    try:
        resp = client.get(XMLTV_URL)
        resp.raise_for_status()
        return xmltodict.parse(resp.text)
    except httpx.HTTPError as exc:
        log.error("Failed to fetch XMLTV: %s", exc)
        return None


def _get_text(node: dict, key: str) -> list[str]:
    """Extract text values for a key from an XMLTV element."""
    val = node.get(key)
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v) for v in val]
    return [str(val)]


def _extract_episode_text(node: dict, key: str) -> str | None:
    """
    Extract text from a node that may be a plain string,
    a dict with #text/@text, or a list of mixed types.
    """
    val = node.get(key)
    if val is None:
        return None
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        # xmltodict parses <episode system="...">01</episode> as
        # {"#text": "01", "@system": "..."} or {"@system": "...", "text": "01"}
        for text_key in ("#text", "text", "@text"):
            if text_key in val and val[text_key]:
                return str(val[text_key])
        return None
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for text_key in ("#text", "text", "@text"):
                    if text_key in item and item[text_key]:
                        parts.append(str(item[text_key]))
                        break
        return " ".join(parts) if parts else None
    return None


def _is_first_episode(node: dict) -> bool:
    """
    Determine if this programme represents the first episode.

    Checks multiple XMLTV episode-numbering formats:
      <episode>01</episode>
      <episode-num system="xmltv_ns">1.0</episode-num>   (Season 1, Episode 0)
      <episode system="ovtk_epp">01</episode>
      <episodenumber>1</episodenumber>
    """
    # Try the main keys
    ep = _extract_episode_text(node, "episode") or \
         _extract_episode_text(node, "episode-num") or \
         _extract_episode_text(node, "episodenumber")

    # Fallback: some feeds use "season" + "episode" as separate numeric keys
    if ep is None:
        season = node.get("season")
        episode = node.get("episode")
        if season is not None and episode is not None:
            ep = f"{season} {episode}"

    if ep is None:
        return False

    # Check for the specific pattern: XXX.0. (e.g., 1.0. or 01.0.)
    # This signifies the first episode of a season in many XMLTV formats.
    if re.search(r'(\d+)\.0\.?$', ep):
        return True

    # Check for S01E0[... etc
    if isinstance(ep, str) and re.search(r's01\s*e01', ep, re.IGNORECASE):
        return True

    # Handle numeric formats
    if isinstance(ep, str):
        return _parse_episode_str(ep) is not None

    return False


def _parse_episode_str(s: str) -> tuple[int, int] | None:
    """
    Parse an episode string like '01', '001', 'S01E01', etc.
    Returns (season, episode) or None if not a first episode.
    """
    # Format: S01E01, s01e01
    match = re.search(r's(\d+)e(\d+)', s, re.IGNORECASE)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        if season == 1 and episode == 1:
            return (season, episode)
        return None

    # Format: just a number like '01' or '001'
    if re.match(r'^0+1$', s):
        return (0, 1)

    # Format: xmltv_ns style "0.0" means S01E01
    if re.match(r'^0+\.0+$', s):
        return (0, 1)

    return None


def extract_first_episodes(xmltv_data: dict) -> list[dict]:
    """
    Walk the parsed XMLTV structure and yield programmes that
    feature a first episode.

    Each yielded dict has: { 'channel', 'title', 'start', 'end', 'programme' }
    """
    programmes = []

    def _walk(node):
        if isinstance(node, dict):
            # Look for a programme element
            if "programme" in node:
                prog = node["programme"]
                if isinstance(prog, list):
                    for p in prog:
                        _check_programme(p)
                else:
                    _check_programme(prog)
            else:
                for value in node.values():
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    def _check_programme(prog):
        if not isinstance(prog, dict):
            return

        titles = _get_text(prog, "title")
        ep_value = _extract_episode_text(prog, "episode") or _extract_episode_text(prog, "episodenumber")

        log.debug("Checking programme: title=%s episode=%s", titles, ep_value)

        if not titles:
            log.debug("  → skipping: no title")
            return
        if not _is_first_episode(prog):
            log.debug("  → skipping: not a first episode")
            return

        channel = prog.get("@channel", "unknown")
        
        # Extract the channel number from the channel ID (e.g., "C6.54.tunarr.com" → "6")
        channel_match = re.match(r"C(\d+)", channel)
        if channel_match:
            channel_num = channel_match.group(1)
        else:
            channel_num = channel  # Use the raw value if no match

        # Check channel whitelist
        if ALLOWED_CHANNELS and channel_num not in ALLOWED_CHANNELS:
            log.debug("  → skipping: channel %s not in whitelist", channel_num)
            return

        log.debug("  ✓ FIRST EPISODE (channel %s)", channel_num)

        start_raw = prog.get("@start", "")
        end_raw = prog.get("@stop", "")
        start = _parse_time(start_raw)
        end = _parse_time(end_raw)

        if start is None or end is None:
            log.debug("  → skipping: could not parse time (%s → %s)", start, end)
            return

        title = "; ".join(titles) if isinstance(titles, list) else titles

        programmes.append({
            "channel": channel,
            "title": title,
            "start": start,
            "end": end,
            "programme": prog,
        })

    _walk(xmltv_data)
    return programmes


def _parse_time(raw: str) -> datetime | None:
    """Parse XMLTV timestamp like '20261022140000 +0300'."""
    if not raw:
        return None
    try:
        # 1. Clean whitespace
        raw = raw.strip()
        
        # 2. Separate the datetime part from the offset part
        # XMLTV format: YYYYMMDDHHmmss +ZZZZ
        # We split by the last space to isolate the offset
        parts = raw.rsplit(None, 1)
        dt_str = parts[0]
        
        from datetime import datetime, timezone, timedelta
        dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S")
        
        if len(parts) > 1:
            tz_offset_str = parts[1] # e.g., "+0000"
            tz_sign = 1 if tz_offset_str[0] == '+' else -1
            tz_hours = int(tz_offset_str[1:3])
            tz_mins = int(tz_offset_str[3:5])
            tz = timezone(timedelta(hours=tz_sign * tz_hours, minutes=tz_sign * tz_mins))
            return dt.replace(tzinfo=tz)
            
        return dt
    except (ValueError, IndexError, Exception) as e:
        log.warning("Could not parse time: '%s' - Error: %s", raw, e)
        return None


# ---------------------------------------------------------------------------
# ICS generation
# ---------------------------------------------------------------------------


def generate_ics(programmes: list[dict]) -> bytes:
    """Generate a calendar containing all premiere episodes."""
    cal = Calendar()
    cal.add("prodid", "-//premiarr//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")

    for prog in programmes:
        event = Event()
        event.add("summary", f"🎬 Premiere: {prog['title']}")
        event.add("dtstart", prog["start"])
        event.add("dtend", prog["end"])
        event.add("dtstamp", datetime.now(timezone.utc))
        event.add("uid", f"premiere-{hashlib.md5(str(prog['programme']).encode()).hexdigest()}@premiarr")
        event.add("category", "TV Premiere")
        event["description"] = (
            f"Channel: {prog['channel']}\n"
            f"This is the first episode of this series."
        )

        # 1-hour reminder alarm
        alarm = Event()
        alarm.add("summary", "Reminder")
        alarm.add("action", "DISPLAY")
        alarm.add("trigger", timedelta(hours=-1))
        event.add_component(alarm)

        cal.add_component(event)

    return cal.to_ical()


# ---------------------------------------------------------------------------
# State tracking — only write ICS when content changes
# ---------------------------------------------------------------------------

_last_hash: str | None = None


def write_ics_if_changed(programmes: list[dict]) -> bool:
    """Write the ICS file only when its content actually changed."""
    global _last_hash
    
    content = generate_ics(programmes)
    current_hash = hashlib.sha256(content).hexdigest()

    if current_hash == _last_hash:
        log.debug("ICS content unchanged — skipping write")
        return False

    ICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ICS_PATH.write_bytes(content)
    _last_hash = current_hash
    log.info("ICS file updated with %d premiere(s)", len(programmes))
    return True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    """Periodically fetch XMLTV and update the ICS file."""
    log.info("premiarr starting — polling %s every %ds", XMLTV_URL, CHECK_INTERVAL)
    log.info("ICS output: %s", ICS_PATH)

    while True:
        xmltv = fetch_xmltv()
        if xmltv:
            programmes = extract_first_episodes(xmltv)
            if programmes:
                log.info("Found %d premiere episode(s)", len(programmes))
                for p in programmes:
                    log.info(
                        "  • %s — %s (%s → %s)",
                        p["channel"],
                        p["title"],
                        p["start"],
                        p["end"],
                    )
                write_ics_if_changed(programmes)
            else:
                log.debug("No first episodes found in this cycle")
        else:
            log.warning("Skipping ICS update due to fetch error")

        # Sleep, allowing clean shutdown on SIGINT
        import time
        for _ in range(CHECK_INTERVAL):
            time.sleep(1)


# ---------------------------------------------------------------------------
# FastAPI server
# ---------------------------------------------------------------------------

app = FastAPI(title="premiarr", version="${VERSION}")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/calendar.ics")
def calendar_ics():
    """Serve the latest ICS calendar."""
    if not ICS_PATH.exists():
        return Response(
            content="No calendar available yet. Waiting for the next cycle.\n",
            media_type="text/plain",
            status_code=503,
        )
    content = ICS_PATH.read_bytes()
    return Response(
        content=content,
        media_type="text/calendar",
        headers={
            "Content-Disposition": f'attachment; filename="premiere.ics"',
            "Cache-Control": "no-cache",
        },
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import signal
    import threading
    import ssl

    def _handle_signal(signum, _frame):
        log.info("Received signal %d — shutting down", signum)
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Start the background poller in a daemon thread
    poller_thread = threading.Thread(target=run, daemon=True)
    poller_thread.start()
    log.info("Background poller started (updates every %ds)", CHECK_INTERVAL)

    # Start the HTTP server with optional SSL
    ssl_cert = os.environ.get("SSL_CERT", "")
    ssl_key = os.environ.get("SSL_KEY", "")

    if ssl_cert and ssl_key:
        log.info("Starting HTTPS server on 0.0.0.0:8000 (TLS enabled)")
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=ssl_cert, keyfile=ssl_key)
        uvicorn.run(app, host="0.0.0.0", port=8000, ssl_keyfile=ssl_key, ssl_certfile=ssl_cert)
    else:
        log.info("Starting HTTP server on 0.0.0.0:8000 (TLS disabled)")
        uvicorn.run(app, host="0.0.0.0", port=8000)
