# premiarr

First-episode calendar notifier for TV shows.

Monitors an XMLTV programming guide every hour and updates an ICS calendar whenever a show airs its first episode (S01E01, Season 1 Episode 1, etc.).
By default it checks all seasons, but can be limited to Season 1.

## Features

- Polls any XMLTV guide on a configurable interval (default: hourly)
- Detects first episodes across multiple numbering formats (`01`, `001`, `S01E01`, `s01e01`, xmltv_ns `0.0`, etc.)
- Detects first episodes across seasons by default (can be changed with `SEASON_ONE_ONLY` flag)
- Writes a single `.ics` file with premiere events
- Serves the calendar via HTTP (`/calendar.ics`) for easy subscription
- Only writes the ICS file when content actually changes (avoids unnecessary syncs)
- Runs as a background daemon by default; use `--serve` flag for HTTP-only mode

## Configuration

| Environment Variable   | Default                 | Required?    | Description                                |
|------------------------|-------------------------|--------------|--------------------------------------------|
| `XMLTV_URL`            | None                    | Yes          | URL of the XMLTV programming guide         |
| `CHECK_INTERVAL`       | `3600`                  | No           | Seconds between XMLTV polls                |
| `LOG_LEVEL`            | `INFO`                  | No           | Python logging level                       |
| `ICS_PATH`             | `/data/premiere.ics`    | No           | Output path for the ICS calendar file      |
| `SEASON_ONE_ONLY`      | false                   | No           | Only grab Season 1 premieres               |
| `SSL_CERT`             | None                    | No           | SSL cert PEM                               |
| `SSL_KEY`              | None                    | No           | SSL cert key                               |

## Quick Start

### Docker Compose

1. Copy and edit `.env`:

```bash
cp .env.example .env
# Edit .env and set your XMLTV_URL
```

2. Start:

```bash
docker compose up -d
```

3. Subscribe to the calendar from your client:

```
http://localhost:8001/calendar.ics
```

### Local Run

```bash
export XMLTV_URL="https://example.com/guide.xml"
python app.py          # Background update mode (default)
# or
python app.py --serve  # HTTP server only (ICS updated elsewhere)
```

## Docker

### Build

```bash
docker build -t flashdim/premiarr:latest .
```

### Run

```bash
docker run -d \
  --name premiarr \
  -e XMLTV_URL="https://example.com/guide.xml" \
  -v $(pwd)/data:/data \
  -p 8001:8000 \
  flashdim/premiarr:latest
```

## Integration

### Sonarr / Tautulli / Jellyfin

Use `http://<premiarr-host>:8001/calendar.ics` as an external calendar source. The file is served as `text/calendar` with a `Content-Disposition: attachment` header for easy subscription.

### Example: Subscribe in Thunderbird / Evolution / Apple Calendar

1. Open your calendar application.
2. Add a **Network** / **From the Web** / **URL** calendar.
3. Enter: `http://<premiarr-host>:8001/calendar.ics`
4. Calendar will auto-refresh on next sync (ICS is served without cache headers).

## File Structure

```
premiarr/
├── app.py                # Main application
├── requirements.txt      # Python dependencies
├── Dockerfile            # Docker image build
├── docker-compose.yml    # Docker Compose service
├── README.md             # This file
├── .gitignore
└── data/                 # Mounted volume (ICS output)
    └── premiere.ics      # Generated calendar
```

## License

MIT
