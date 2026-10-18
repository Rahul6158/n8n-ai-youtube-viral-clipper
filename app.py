import os, re, json, uuid, shutil, subprocess, time
from pathlib import Path
from urllib.parse import urlparse
import requests
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field

APP_DIR = Path(os.getenv("CLIPPER_OUTPUT_DIR", "/data/clips")).resolve()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
CLIPPER_API_TOKEN = os.getenv("CLIPPER_API_TOKEN", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_VIDEO_BYTES = int(os.getenv("MAX_VIDEO_BYTES", str(2 * 1024 * 1024 * 1024)))
APP_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="AI YouTube Clipper API", version="1.0.0")

class ClipRequest(BaseModel):
    video_url: str
    clip_count: int = Field(default=3, ge=1, le=5)
    max_duration: int = Field(default=45, ge=15, le=90)
    aspect_ratio: str = Field(default="portrait", pattern="^(portrait|landscape)$")

def run(cmd, timeout=3600):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout or "Command failed")[-3000:])
    return p.stdout

def safe_name(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("._")
    return (s or "video")[:80]

def parse_vtt(path):
    # Parse WebVTT cues; timestamps can be HH:MM:SS.mmm or MM:SS.mmm.
    text = path.read_text(encoding="utf-8", errors="replace")
    cues, current = [], None
    for line in text.splitlines():
        line = line.strip()
        m = re.search(r"(?:(\d{2,}):)?(\d{2}):(\d{2})[.,](\d{3})\s+-->\s+(?:(\d{2,}):)?(\d{2}):(\d{2})[.,](\d{3})", line)
        if m:
            g = m.groups()
            start = (int(g[0] or 0)*3600 + int(g[1])*60 + int(g[2]) + int(g[3])/1000)
            end = (int(g[4] or 0)*3600 + int(g[5])*60 + int(g[6]) + int(g[7])/1000)
            current = {"start": start, "end": end, "text": ""}
            cues.append(current)
        elif current and line and not line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:")) and not re.fullmatch(r"\d+", line):
            cleaned = re.sub(r"<[^>]+>", "", line)
            cleaned = re.sub(r"&amp;", "&", cleaned)
            if cleaned and (not current["text"] or cleaned != current["text"]):
                current["text"] = (current["text"] + " " + cleaned).strip()
    return [c for c in cues if c["text"] and c["end"] > c["start"]]

def fmt(sec):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}.{ms:03}"

def analyze(title, cues, count, max_duration, duration):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set in the API service environment.")
    transcript = "\n".join(f"[{fmt(c['start'])} --> {fmt(c['end'])}] {c['text']}" for c in cues)
    # Keep prompt bounded; sample long transcripts uniformly to avoid huge requests.
    max_chars = 90000
    if len(transcript) > max_chars:
        step = max(1, len(transcript) // max_chars)
        transcript = "\n".join(f"[{fmt(c['start'])} --> {fmt(c['end'])}] {c['text']}" for c in cues[::step])
    prompt = f"""You are a short-form video editor. From the transcript, choose up to {count} distinct, compelling clips for social short videos.
Return ONLY JSON as an array. Each object must have start_seconds (number), end_seconds (number), hook (string), score (number 0 to 100), caption (string).
Rules: use timestamps from the transcript; end > start; each clip must be 15 to {max_duration} seconds; end cannot exceed video duration {duration:.3f}; prioritize a strong opening, a complete thought, surprise/usefulness/emotion, and minimal context needed. Avoid overlapping clips. Do not invent dialogue.
Video title: {title}
Transcript:
{transcript}"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    response = requests.post(url, params={"key": GEMINI_API_KEY}, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }, timeout=180)
    if not response.ok:
        raise RuntimeError(f"Gemini API HTTP {response.status_code}: {response.text[:1500]}")
    data = response.json()
    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        clips = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"Gemini response was not a JSON clip array: {str(e)}")
    if not isinstance(clips, list):
        raise RuntimeError("Gemini did not return a JSON array.")
    good = []
    for c in clips:
        try:
            start, end = float(c["start_seconds"]), float(c["end_seconds"])
            if start < 0 or end <= start or end - start > max_duration or end > duration:
                continue
            good.append({"start_seconds": start, "end_seconds": end,
                         "hook": str(c.get("hook", "Clip"))[:180],
                         "caption": str(c.get("caption", ""))[:220],
                         "score": max(0, min(100, float(c.get("score", 0))))})
        except (KeyError, TypeError, ValueError):
            continue
    good.sort(key=lambda x: x["score"], reverse=True)
    selected = []
    for c in good:
        if all(c["end_seconds"] <= s["start_seconds"] or c["start_seconds"] >= s["end_seconds"] for s in selected):
            selected.append(c)
        if len(selected) >= count:
            break
    if not selected:
        raise RuntimeError("No valid clip candidates were returned by Gemini.")
    return selected

@app.get("/health")
def health():
    return {"ok": True, "ffmpeg": shutil.which("ffmpeg") is not None, "yt_dlp": shutil.which("yt-dlp") is not None, "gemini_key_configured": bool(GEMINI_API_KEY)}

@app.post("/clip")
def clip(req: ClipRequest, authorization: str = Header(default="")):
    if not CLIPPER_API_TOKEN or authorization != f"Bearer {CLIPPER_API_TOKEN}":
        raise HTTPException(401, "Missing or invalid API token.")
    parsed = urlparse(req.video_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com") or host == "youtube-nocookie.com"):
        raise HTTPException(400, "Only valid YouTube URLs are accepted.")
    job_id = uuid.uuid4().hex[:12]
    job_dir = APP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    try:
        meta = json.loads(run(["yt-dlp", "--no-warnings", "--skip-download", "--dump-single-json", "--no-playlist", req.video_url], timeout=180))
        title = str(meta.get("title") or "youtube_video")
        duration = float(meta.get("duration") or 0)
        if duration <= 0:
            raise RuntimeError("Could not determine video duration.")
        # Download video into a predictable path. Avoid shell interpolation entirely.
        video_path = job_dir / "source.mp4"
        run(["yt-dlp", "--no-warnings", "--no-playlist", "-f", "bv*+ba/b", "--merge-output-format", "mp4",
             "-o", str(video_path), req.video_url], timeout=3600)
        if not video_path.exists():
            candidates = list(job_dir.glob("source.*"))
            if candidates: video_path = candidates[0]
            else: raise RuntimeError("yt-dlp completed but no source video file was found.")
        if video_path.stat().st_size > MAX_VIDEO_BYTES:
            raise RuntimeError("Video exceeds MAX_VIDEO_BYTES limit.")
        # Request automatic captions first, then manual captions; no transcript extraction by guessing log output.
        run(["yt-dlp", "--no-warnings", "--no-playlist", "--write-auto-subs", "--write-subs",
             "--sub-langs", "en.*,en", "--sub-format", "vtt", "--skip-download",
             "-o", str(job_dir / "subs.%(ext)s"), req.video_url], timeout=300)
        vtts = sorted(job_dir.glob("subs*.vtt"))
        if not vtts:
            raise RuntimeError("No English VTT captions were available for this video. Add a transcription provider if you need videos without captions.")
        cues = parse_vtt(vtts[0])
        if len(cues) < 3:
            raise RuntimeError("Transcript was too short or could not be parsed.")
        clips = analyze(title, cues, req.clip_count, req.max_duration, duration)
        output = []
        for i, c in enumerate(clips, 1):
            start, end = c["start_seconds"], c["end_seconds"]
            dest = job_dir / f"clip_{i:02d}.mp4"
            if req.aspect_ratio == "portrait":
                vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
            else:
                vf = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"
            # Burn readable captions using drawtext only for the short AI hook as a title overlay;
            # subtitles themselves require a local font and are intentionally not fabricated from AI.
            hook = re.sub(r"[^A-Za-z0-9 ,.!?'-]", "", c["hook"]).replace(":", "")
            hook = hook[:100].replace("\\", "").replace("'", "")
            vf += f",drawtext=text='{hook}':fontcolor=white:fontsize=48:box=1:boxcolor=black@0.55:boxborderw=18:x=(w-text_w)/2:y=h*0.12"
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(video_path),
                 "-t", f"{end-start:.3f}", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "23", "-c:a", "aac", "-movflags", "+faststart", str(dest)], timeout=1800)
            output.append({"file": str(dest), "filename": dest.name, "start_seconds": start,
                           "end_seconds": end, "duration_seconds": round(end-start, 3),
                           "hook": c["hook"], "caption": c["caption"], "score": c["score"]})
        manifest = {"job_id": job_id, "source_url": req.video_url, "title": title,
                    "output_directory": str(job_dir), "clips": output}
        (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest
    except HTTPException:
        raise
    except Exception as e:
        # Keep job folder and logs/artifacts for debugging; do not delete source files automatically.
        raise HTTPException(500, str(e))
