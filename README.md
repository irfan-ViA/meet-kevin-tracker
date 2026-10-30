# Meet Kevin Tracker

Tracks the stocks Meet Kevin talks about on YouTube, his stance on each, recent prices, and alerts.

- **Tracker page:** https://irfan-via.github.io/meet-kevin-tracker/
- **Android app:** download `KevinTracker.apk` from the [latest release](../../releases/latest).

## How it works
- `.github/workflows/update.yml` runs every 2 hours. It checks Kevin's channel feed for new videos, reads their captions, asks Claude which stocks he gives a view on (`pipeline/update.py`), refreshes prices from Yahoo Finance, saves `docs/data/*.json` and publishes the page.
- The Android app shows the page and checks `docs/data/status.json` every hour, notifying you about new videos, new calls and big moves (5%+ in a day, or 10%+ since his first mention).

## Secrets (Settings → Secrets and variables → Actions)
- `ANTHROPIC_API_KEY` (required): reads the transcripts.
- `GEMINI_API_KEY` (recommended): Google fetches each YouTube video itself and transcribes it, which avoids YouTube blocking GitHub. Method from bradautomates/claude-video.
- `WEBSHARE_PROXY_USERNAME` and `WEBSHARE_PROXY_PASSWORD`: a Webshare residential proxy, needed because YouTube blocks caption downloads from GitHub servers.
- `TRANSCRIPT_PROXY_URL` (optional): any other proxy, as `http://user:pass@host:port`.

Stances are Claude's reading of the transcripts. Information only, not financial advice.
