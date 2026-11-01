"""End of party: one page with everyone's photos, a GIF of the whole night, and a copy of
it all in Dropbox so the page outlives the booth.

build() makes party/<token>/ on the PC: a self-contained little website (index.html plus
the images). The booth serves it at /p/<token>/ (reachable through the tunnel while the
booth runs), and the same folder is uploaded to Dropbox under _party/<date-time>/ with a
shared folder link, so guests can still grab everything after the PC is off. The folder
can also be dropped onto any static host (GitHub Pages and the like) as-is.
"""

import json
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from PIL import Image, ImageOps

STATE = {"token": None, "stage": "idle", "message": "", "url": None, "dropbox": None,
         "sessions": 0, "done": 0, "total": 0, "error": None}
_lock = threading.Lock()


def _set(**kw):
    with _lock:
        STATE.update(kw)


def status():
    with _lock:
        return dict(STATE)


def _when(meta):
    return time.strftime("%b %d, %I:%M %p", time.localtime(meta.get("created", 0))).replace(" 0", " ")


def _compilation(frames, out, fps, width):
    """Every photo of the night, in order, as one GIF (smaller than the session GIFs so
    phones can load it)."""
    ims = []
    for p in frames:
        im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
        im.thumbnail((width, width))
        ims.append(im)
    if not ims:
        return False
    ims[0].save(out, format="GIF", save_all=True, append_images=ims[1:],
                duration=int(1000 / fps), loop=0, optimize=True)
    return True


def _video(frames, out, seconds, width):
    """Every photo of the night as one MP4 that runs `seconds` long. Needs the ffmpeg that
    imageio-ffmpeg installs; returns False (so the GIF is used) if it's missing."""
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return False
    if not frames:
        return False
    per = min(1.0, max(0.12, seconds / len(frames)))   # seconds per photo
    with tempfile.TemporaryDirectory() as tmp:
        size = None
        for n, p in enumerate(frames):
            im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
            if size is None:
                w, h = im.size
                size = (width, max(2, round(width * h / w)) // 2 * 2)
            im = ImageOps.pad(im, size, Image.LANCZOS, color="black")   # all frames the same size
            im.save(Path(tmp) / f"f{n:05d}.jpg", quality=90)
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-framerate", f"{1 / per:.4f}", "-i", str(Path(tmp) / "f%05d.jpg"),
               "-r", "30", "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(out)]
        try:
            subprocess.run(cmd, check=True, timeout=600)
        except Exception as e:
            print(f"[party] video failed, using a GIF instead: {e}")
            return False
    return out.exists()


def build(root: Path, sessions: Path, cfg, read_meta, exporter, public_base, title="Party photos"):
    """Make the page now (fast), then upload it to Dropbox in the background.
    Returns the new token; poll status() for the rest."""
    token = uuid.uuid4().hex[:10]
    out = root / "party" / token
    out.mkdir(parents=True, exist_ok=True)
    _set(token=token, stage="building", message="Collecting photos…", url=None, dropbox=None,
         sessions=0, done=0, total=0, error=None)

    items, frames = [], []
    for d in sorted((p for p in sessions.iterdir() if p.is_dir()), key=lambda p: p.name):
        try:
            meta = read_meta(d)
        except (OSError, ValueError):
            continue
        if not (d / "print.jpg").exists():
            continue
        sd = out / d.name
        sd.mkdir(exist_ok=True)
        item = {"id": d.name, "name": _when(meta), "gif": None, "print": None, "shots": [],
                "dropbox": exporter.share(d.name)[1] or ""}
        if (d / "animation.gif").exists():
            shutil.copy2(d / "animation.gif", sd / "animation.gif")
            item["gif"] = f"{d.name}/animation.gif"
        src = d / "print_share.jpg" if (d / "print_share.jpg").exists() else d / "print.jpg"
        shutil.copy2(src, sd / "4up.jpg")
        item["print"] = f"{d.name}/4up.jpg"
        for i in range(meta.get("shots", 4)):
            p = d / f"shot{i}_preview.jpg"
            if not p.exists():
                p = d / f"shot{i}.jpg"
            if not p.exists():
                continue
            shutil.copy2(p, sd / f"photo{i + 1}.jpg")
            item["shots"].append(f"{d.name}/photo{i + 1}.jpg")
            frames.append(sd / f"photo{i + 1}.jpg")
        items.append(item)

    _set(sessions=len(items), message=f"Making the video of the night ({len(frames)} photos)…")
    video = _video(frames, out / "party.mp4", cfg.get("party_video_seconds", 30), cfg.get("party_video_width", 720))
    has_gif = False if video else _compilation(frames, out / "party.gif", cfg.get("gif_fps", 4), cfg.get("party_gif_width", 480))

    data = {"title": title, "video": "party.mp4" if video else None, "gif": "party.gif" if has_gif else None,
            "sessions": list(reversed(items)), "dropbox": None, "count": len(frames)}
    template = (root / "static" / "party.html").read_text(encoding="utf-8")

    def write_index():
        (out / "index.html").write_text(template.replace("/*DATA*/", json.dumps(data)), encoding="utf-8")

    write_index()
    url = f"{public_base}/p/{token}/" if public_base else None
    _set(url=url, stage="uploading" if exporter.dbx else "done",
         message="Page ready." + (" Copying to Dropbox…" if exporter.dbx else " (Dropbox is off, so it only lives on the booth.)"))

    if not exporter.dbx:
        return token

    def upload():
        try:
            remote = f"{exporter.remote_root}/_party/{time.strftime('%Y-%m-%d_%H-%M')}"
            files = [p for p in out.rglob("*") if p.is_file() and p.name != "index.html"]
            _set(total=len(files) + 1, done=0)
            exporter.dbx.create_folder(remote)
            link = exporter.dbx.share_link(remote)
            data["dropbox"] = link
            write_index()
            for n, p in enumerate(files, 1):
                exporter.dbx.upload(p, f"{remote}/{p.relative_to(out).as_posix()}")
                _set(done=n, message=f"Copying to Dropbox… {n} of {len(files) + 1}")
            exporter.dbx.upload(out / "index.html", f"{remote}/index.html")
            _set(done=len(files) + 1, dropbox=link, stage="done", message="Done. Everything is on Dropbox too.")
        except Exception as e:
            _set(stage="done", error=str(e), message=f"Page is up on the booth, but the Dropbox copy failed: {e}")

    threading.Thread(target=upload, daemon=True).start()
    return token
