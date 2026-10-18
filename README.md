# AI YouTube Viral Clip Creator

## What it does
- n8n form accepts a YouTube URL, clip count, format and maximum duration.
- Companion API downloads the video and English captions, asks Gemini for clip candidates, validates timestamps, and renders MP4 clips with FFmpeg.
- Output clips and `manifest.json` are saved under `output/<job-id>/`.

## Important limitation
This baseline requires English captions to exist. It does not use speech-to-text when captions are missing. It does not upload to YouTube automatically; add the YouTube upload node after review and configure OAuth first. This avoids publishing unreviewed clips automatically.

## Setup (Docker Desktop on Windows)
1. Extract this folder.
2. Copy `.env.example` to `.env` and set `GEMINI_API_KEY` and a long random `CLIPPER_API_TOKEN`.
3. In PowerShell, run `docker compose up --build -d`.
4. Check `http://localhost:8787/health`.
5. Import `ai_youtube_clipper_workflow.json` into n8n.
6. Ensure n8n's container can reach `host.docker.internal:8787`. If it cannot, put both services on the same Docker network and change the workflow URL to `http://clipper-api:8787/clip`.
7. Open the Form Trigger's test URL and submit a video URL. Workflow remains inactive until you activate it.

## Requirements / caveats
- Self-hosted n8n. The workflow uses a Form Trigger, Code, and HTTP Request nodes.
- This service uses yt-dlp and FFmpeg in its own container, not inside n8n. That avoids relying on the problematic Alpine/PyInstaller node setup.
- Respect copyright and YouTube terms; only download/process content you own or have permission to use.
- The API requires a bearer token, but the published port is reachable on the host network. Keep it behind your firewall; do not expose it publicly without HTTPS, rate limiting, and stronger access controls. After importing, open the HTTP Request node and set its Authorization header value to `Bearer YOUR_CLIPPER_API_TOKEN` (the same value configured in `.env`).
- No Gmail credential, Google OAuth, or YouTube upload credential is embedded.
