"""The party page: everyone's photos on one page, kept up to date through the night.

party/live/ on the PC is a self-contained little website (index.html plus the images).
Every finished set is added to it automatically; "Build party page" in the admin panel
rebuilds it from scratch and makes the video of the night. The booth serves the folder
at /p/live/ (reachable through the tunnel while the booth runs), and it is published to
a folder of a GitHub repo (party_repo in config.json, served by GitHub Pages) or, with no
repo set, copied to Dropbox under _party/, so the page outlives the booth.
"""

import json
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

FOLDER = "live"
STATE = {"stage": "idle", "message": "", "url": None, "dropbox": None, "sessions": 0,
         "done": 0, "total": 0, "error": None, "updated": None}
_lock = threading.Lock()
_publish_lock = threading.Lock()
_dirty = {"pending": False}
_ctx = {}


def _set(**kw):
    with _lock:
        STATE.update(kw)


def status():
    with _lock:
        return dict(STATE)


def setup(root: Path, sessions: Path, cfg, read_meta, exporter, public_base):
    """Called once by the server; remembers where things are."""
    _ctx.update(root=root, sessions=sessions, cfg=cfg, read_meta=read_meta, exporter=exporter,
                public_base=public_base)
    out = root / "party" / FOLDER
    if (out / "index.html").exists():
        _set(stage="done", url=_page_url(), sessions=len(_load_sets(out)), message="Page from last time is still up.")


# ---------- the page folder ----------
def _out():
    return _ctx["root"] / "party" / FOLDER


def _load_sets(out):
    f = out / "sets.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _copy_set(d, meta, out):
    """Copy one session's GIF, 4-up and singles into the page folder; returns its entry."""
    sd = out / d.name
    sd.mkdir(parents=True, exist_ok=True)
    item = {"id": d.name, "created": meta.get("created", 0), "gif": None, "print": None, "shots": []}
    if (d / "animation.gif").exists():
        shutil.copy2(d / "animation.gif", sd / "animation.gif")
        item["gif"] = f"{d.name}/animation.gif"
    src = d / "print_share.jpg" if (d / "print_share.jpg").exists() else d / "print.jpg"
    if src.exists():
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
    return item


def _write_page(out, sets, extra=None):
    cfg = _ctx["cfg"]
    sets = sorted(sets, key=lambda s: s["id"])
    (out / "sets.json").write_text(json.dumps(sets), encoding="utf-8")
    data = {"title": cfg.get("party_title") or "Party photos",
            "video": "party.mp4" if (out / "party.mp4").exists() else None,
            "gif": "party.gif" if (out / "party.gif").exists() and not (out / "party.mp4").exists() else None,
            "sessions": list(reversed(sets)), "count": sum(len(s["shots"]) for s in sets)}
    data.update(extra or {})
    template = (_ctx["root"] / "static" / "party.html").read_text(encoding="utf-8")
    (out / "index.html").write_text(template.replace("/*DATA*/", json.dumps(data)), encoding="utf-8")
    _set(sessions=len(sets), updated=time.time())


# ---------- the video of the night ----------
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


def _compilation(frames, out, fps, width):
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


# ---------- entry points ----------
def add_session(d, meta):
    """A set just finished: put it on the page and publish. Runs in the background."""
    cfg = _ctx["cfg"]
    if not cfg.get("party_auto", True) or not _ctx:
        return

    def work():
        try:
            out = _out()
            out.mkdir(parents=True, exist_ok=True)
            sets = [s for s in _load_sets(out) if s["id"] != d.name]
            sets.append(_copy_set(d, meta, out))
            _write_page(out, sets)
            _set(url=_page_url(), stage=STATE["stage"] if STATE["stage"] == "uploading" else "done")
            _publish()
        except Exception as e:
            print(f"[party] couldn't add {d.name}: {e}")

    threading.Thread(target=work, daemon=True).start()


def build():
    """Admin: rebuild the page from every set in the gallery, make the video of the night,
    then publish. Returns at once; poll status()."""
    cfg, sessions, read_meta = _ctx["cfg"], _ctx["sessions"], _ctx["read_meta"]
    out = _out()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    _set(stage="building", message="Collecting photos…", url=None, dropbox=None, sessions=0, done=0, total=0, error=None)

    sets, frames = [], []
    for d in sorted((p for p in sessions.iterdir() if p.is_dir()), key=lambda p: p.name):
        try:
            meta = read_meta(d)
        except (OSError, ValueError):
            continue
        if not (d / "print.jpg").exists():
            continue
        item = _copy_set(d, meta, out)
        frames += [out / s for s in item["shots"]]
        sets.append(item)

    _set(sessions=len(sets), message=f"Making the video of the night ({len(frames)} photos)…")
    if not _video(frames, out / "party.mp4", cfg.get("party_video_seconds", 30), cfg.get("party_video_width", 720)):
        _compilation(frames, out / "party.gif", cfg.get("gif_fps", 4), cfg.get("party_gif_width", 480))
    _write_page(out, sets)
    _set(url=_page_url(), stage="done", message="Page ready on the booth.")
    threading.Thread(target=_publish, daemon=True).start()


# ---------- publishing ----------
def _page_url():
    cfg = _ctx["cfg"]
    repo = cfg.get("party_repo")
    if repo:
        url = cfg.get("party_url")
        if not url:
            try:
                origin = _git(Path(repo).expanduser(), "remote", "get-url", "origin")
                path = origin.replace("git@github.com:", "").replace("https://github.com/", "").removesuffix(".git").rstrip("/")
                user, name = path.split("/", 1)
                url = f"https://{user}.github.io/{name}/{(cfg.get('party_repo_folder') or 'party').strip('/')}"
            except Exception:
                url = None
        if url:
            return url.rstrip("/") + "/"
    base = _ctx["public_base"]()
    return f"{base}/p/{FOLDER}/" if base else None


def _publish():
    """Push the page out. Calls while one is running are folded into one more run after it."""
    _dirty["pending"] = True
    if not _publish_lock.acquire(blocking=False):
        return
    try:
        while _dirty["pending"]:
            _dirty["pending"] = False
            cfg, exporter = _ctx["cfg"], _ctx["exporter"]
            if cfg.get("party_repo"):
                _publish_github(_out(), Path(cfg["party_repo"]).expanduser(), cfg)
            elif exporter.dbx:
                _publish_dropbox(_out(), exporter)
    finally:
        _publish_lock.release()


def _publish_dropbox(out, exporter):
    _set(stage="uploading", message="Copying to Dropbox…")
    try:
        remote = f"{exporter.remote_root}/_party"
        files = [p for p in out.rglob("*") if p.is_file() and p.name != "index.html"]
        _set(total=len(files) + 1, done=0)
        exporter.dbx.create_folder(remote)
        link = exporter.dbx.share_link(remote)
        _write_page(out, _load_sets(out), {"dropbox": link})
        for n, p in enumerate(files, 1):
            exporter.dbx.upload(p, f"{remote}/{p.relative_to(out).as_posix()}")
            _set(done=n, message=f"Copying to Dropbox… {n} of {len(files) + 1}")
        exporter.dbx.upload(out / "index.html", f"{remote}/index.html")
        _set(done=len(files) + 1, dropbox=link, stage="done", error=None, message="Page is on Dropbox too.")
    except Exception as e:
        _set(stage="done", error=str(e), message=f"Page is up on the booth, but the Dropbox copy failed: {e}")


def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=600)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or f"git {args[0]} failed")
    return r.stdout.strip()


def _publish_github(out, repo, cfg):
    """Copy the page into a folder of a GitHub repo on this PC and push it, so GitHub
    Pages serves it at https://USER.github.io/REPO/FOLDER/ (party_url overrides that)."""
    folder = (cfg.get("party_repo_folder") or "party").strip("/")
    _set(stage="uploading", message="Publishing to GitHub…")
    try:
        if not (repo / ".git").exists():
            raise RuntimeError(f"{repo} is not a git checkout")
        try:
            _git(repo, "pull", "--ff-only", "-q")
        except Exception as e:
            print(f"[party] git pull: {e}")
        dest = repo / folder
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(out, dest)
        _git(repo, "add", "-A", folder)
        if _git(repo, "status", "--porcelain", folder):
            _git(repo, "-c", "user.name=Photobooth", "-c", "user.email=photobooth@localhost",
                 "commit", "-q", "-m", f"Party page {time.strftime('%Y-%m-%d %H:%M')}")
            _git(repo, "push", "-q")
        _set(stage="done", error=None, url=_page_url(),
             message=f"Published to GitHub {time.strftime('%I:%M %p').lstrip('0')}. The page updates a minute or two after each push.")
    except Exception as e:
        _set(stage="done", error=str(e), message=f"Page is up on the booth, but publishing to GitHub failed: {e}")
