import base64, os, subprocess, tempfile, hmac
import requests
from urllib.parse import urlparse
from flask import Flask, request, send_file, jsonify

app = Flask(__name__)
TOKEN = os.environ.get("MERGE_TOKEN", "")
MAX_BYTES = 60 * 1024 * 1024  # refuse videos bigger than 60 MB
# Only download videos from these hosts (comma-separated env var to change)
ALLOWED_HOSTS = set(h.strip() for h in os.environ.get("ALLOWED_VIDEO_HOSTS", "tempfile.aiquickdraw.com").split(",") if h.strip())


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


@app.get("/health")
def health():
    return jsonify(ok=True)


@app.post("/merge")
def merge():
    # Shared-secret check (set MERGE_TOKEN in Coolify, send it as X-Token)
    if not TOKEN or not hmac.compare_digest(request.headers.get("X-Token", ""), TOKEN):
        return jsonify(error="unauthorized"), 401

    body = request.get_json(silent=True) or {}
    video_url = body.get("video_url", "")
    audio_b64 = body.get("audio_b64", "")
    if not video_url.startswith("https://") or not audio_b64:
        return jsonify(error="video_url (https) and audio_b64 are required"), 400
    if urlparse(video_url).hostname not in ALLOWED_HOSTS:
        return jsonify(error="video host not allowed"), 400

    with tempfile.TemporaryDirectory() as tmp:
        vpath, apath, opath = (os.path.join(tmp, n) for n in ("v.mp4", "a.wav", "out.mp4"))
        try:
            r = requests.get(video_url, timeout=60, stream=True)
            r.raise_for_status()
            if urlparse(r.url).hostname not in ALLOWED_HOSTS:
                return jsonify(error="redirected to a host that is not allowed"), 400
            size = 0
            with open(vpath, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        return jsonify(error="video too large"), 413
                    f.write(chunk)
            with open(apath, "wb") as f:
                f.write(base64.b64decode(audio_b64))
            vdur, adur = duration(vpath), duration(apath)
        except Exception as e:
            return jsonify(error=f"input problem: {e}"), 400

        # If the narration is longer than the video, speed it up a little (max 1.25x)
        # so it is not cut mid-word. If shorter, pad with silence to the video length.
        filters = []
        if adur > vdur:
            filters.append(f"atempo={min(adur / vdur, 1.25):.4f}")
        filters.append("apad")
        cmd = ["ffmpeg", "-y", "-i", vpath, "-i", apath,
               "-map", "0:v:0", "-map", "1:a:0",
               # Re-encode to the most compatible format for Telegram apps:
               # H.264 High / yuv420p video, AAC-LC 44.1 kHz stereo audio.
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
               "-af", ",".join(filters), "-t", f"{vdur:.3f}",
               "-movflags", "+faststart", opath]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            return jsonify(error="ffmpeg failed", detail=p.stderr[-500:]), 500

        # Read into memory so the temp dir can be removed before responding
        with open(opath, "rb") as f:
            data = f.read()

    from io import BytesIO
    resp = send_file(BytesIO(data), mimetype="video/mp4", download_name="merged.mp4")
    resp.headers["X-Video-Seconds"] = f"{vdur:.2f}"
    resp.headers["X-Audio-Seconds"] = f"{adur:.2f}"
    return resp
