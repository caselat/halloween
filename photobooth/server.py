"""Photobooth server. Run: python3 server.py, then open the printed address on the PC or tablet."""

import io
from html import escape
import json
import os
import shutil
import socket
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from PIL import Image, ImageOps

from camera import make_camera
from export import Exporter
from printing import LAYOUTS, compose, crop_to_cell, list_printers, print_image, trim_bleed

VERSION = "2026-10-25.44"  # bump with every update; shown in Terminal and the screen corner
ROOT = Path(__file__).resolve().parent
SESSIONS = ROOT / "sessions"
SESSIONS.mkdir(exist_ok=True)

DEFAULTS = {
    "camera": "auto",
    "digicam_url": "http://127.0.0.1:5513",
    "gphoto2_settings": ["capturetarget=0"],
    "printer": "",
    "print_command": ["lp", "-d", "{printer}", "-n", "{copies}", "-o", "fit-to-page", "{file}"],
    "print_width": 1844,
    "print_height": 1240,
    "spacing": 24,         # 4-up: border between photos (and, after trimming, around the edge)
    "print_bleed": 28,     # extra outer border the printer trims off a 4x6; tune from a test print
    "corner_radius": 8,    # rounded corners on each 4-up photo, in print pixels
    "layouts": ["grid"],   # start screen options: "grid" (four photos) and/or "single"
    "background": "#000000",
    "theme": "halloween",
    "start_title": "Oh fuck, eek",
    "start_subtitle": "Get your boos together",
    "making_text": "Brewing photos…",
    "done_title": "Done",
    "countdown": 3,
    "countdown_next": 2,
    "shot_preview_seconds": 1,
    "idle_timeout": 45,
    "max_copies": 6,
    "mirror_preview": True,
    "rotate": 0,           # 0, 90, 180 or 270: how the camera is mounted (admin panel sets this);
                           # 90/270 = camera on its side, everything comes out portrait
    "admin_pin": "1321",   # tap the corner status text 5 times, enter this, to open the admin panel
    "sounds": True,        # countdown beeps and shutter tone on the screen
    "party_title": "Party photos",   # headline of the end-of-party page
    "party_video_seconds": 30,       # the video of the night runs this long
    "party_auto": True,              # every finished set goes onto the party page right away
    "party_repo": "",                # a git checkout of your GitHub repo on this PC: the page is pushed there
    "party_repo_folder": "party",    # folder inside that repo (served at https://USER.github.io/REPO/FOLDER/)
    "party_url": "",                 # where that folder is served, if it isn't the github.io address
    "public_url": "",      # a fixed public address for guest pages, if you run your own tunnel; else the
                           # launcher's Cloudflare quick tunnel (tunnel.json) is used when it's running
    "liveview_fps": 30,    # frames per second sent to each screen
    "liveview_width": 0,   # 0 = send the camera's frames as they are; e.g. 800 shrinks them for a tablet
    "liveview_quality": 60,
    "gif_fps": 4,
    "gif_width": 900,
    "save_folder": "",
    "dropbox_folder": "",
    "host": "0.0.0.0",
    "port": 5050,
    "open_on_this_computer": True,
}

UI_KEYS = ("countdown", "countdown_next", "shot_preview_seconds", "idle_timeout",
           "max_copies", "mirror_preview", "rotate", "making_text", "done_title", "liveview_fps", "sounds")


def load_config():
    cfg = dict(DEFAULTS)
    path = ROOT / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))  # Windows would otherwise mangle "…"
    return cfg


CFG = load_config()
camera = make_camera(CFG)
exporter = Exporter(CFG, ROOT, SESSIONS)
app = Flask(__name__, static_folder=str(ROOT / "static"), static_url_path="/static")
print_lock = threading.Lock()


def session_dir(sid):
    if not sid.replace("-", "").isalnum():
        abort(400)
    d = SESSIONS / sid
    if not d.is_dir():
        abort(404)
    return d


def lan_address():
    """This computer's address on the local network, for the tablet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def all_addresses():
    """Every address this computer can be reached at (Wi-Fi, Ethernet, a hotspot it hosts...)."""
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in found:
                found.append(ip)
    except socket.gaierror:
        pass
    main = lan_address()
    if main not in found and not main.startswith("127."):
        found.insert(0, main)
    return found


def make_gif(shots, out, fps, width):
    frames = []
    for p in shots:
        im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
        im.thumbnail((width, width))
        frames.append(im)
    tmp = out.with_suffix(".tmp")
    frames[0].save(tmp, format="GIF", save_all=True, append_images=frames[1:],
                   duration=int(1000 / fps), loop=0, optimize=True)
    os.replace(tmp, out)  # never let the uploader see a half-written file


def read_meta(d):
    return json.loads((d / "session.json").read_text(encoding="utf-8"))


def write_meta(d, meta):
    # write then swap, so the uploader thread never reads a half-written file
    tmp = d / "session.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp, d / "session.json")


@app.get("/")
def index():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    theme = CFG.get("theme") or ""
    if theme and (ROOT / "static" / "themes" / f"{theme}.css").exists():
        html = html.replace("<!--THEME-->",
                            f'<link rel="stylesheet" href="/static/themes/{theme}.css?v={VERSION}">')
    html = html.replace('<body data-state="idle">',
                        f'<body data-state="idle" data-layouts="{escape(" ".join(CFG["layouts"]))}">')
    html = html.replace("<!--START_TITLE-->", escape(CFG["start_title"]))
    html = html.replace("<!--START_SUBTITLE-->", escape(CFG["start_subtitle"]))
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})


_enc_lock = threading.Lock()
_enc = {"id": 0, "jpeg": b""}


def encoded_frame(frame_id, jpeg):
    """Live view frame shrunk and recompressed for the screens. Done once per frame,
    shared by every screen that asks for it."""
    w, q = int(CFG.get("liveview_width", 0)), int(CFG.get("liveview_quality", 60))
    if not w:
        return jpeg
    with _enc_lock:
        if _enc["id"] == frame_id:
            return _enc["jpeg"]
        try:
            im = Image.open(io.BytesIO(jpeg))
            im.draft("RGB", (w, w))   # fast JPEG downscale while decoding
            im = im.convert("RGB")
            if im.width > w:
                im = im.resize((w, int(im.height * w / im.width)), Image.BILINEAR)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q)
            out = buf.getvalue()
        except Exception:
            out = jpeg
        _enc["id"], _enc["jpeg"] = frame_id, out
        return out


@app.get("/frame")
def frame():
    """The newest live view frame, newer than ?since=<id>. Waits up to 2s for one.
    Screens call this in a loop, so a slow tablet just gets fewer frames."""
    since = int(request.args.get("since", 0))
    got = camera.wait_frame(since, timeout=2)
    if got is None:
        return Response(status=204, headers={"Cache-Control": "no-store"})
    frame_id, jpeg = got
    return Response(encoded_frame(frame_id, jpeg), mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store", "X-Frame-Id": str(frame_id),
                             "Access-Control-Expose-Headers": "X-Frame-Id"})


@app.get("/liveview")
def liveview():
    def stream():
        last, last_sent = 0, 0.0
        min_gap = 1.0 / max(1, float(CFG.get("liveview_fps", 15)))
        while True:
            got = camera.wait_frame(last, timeout=2)
            if got is None:
                continue
            last, jpeg = got
            now = time.time()
            if now - last_sent < min_gap:   # skip frames to stay under the cap
                continue
            last_sent = now
            jpeg = encoded_frame(last, jpeg)
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
    return Response(stream(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def status():
    return jsonify(version=VERSION, camera=camera.status(), printer=CFG["printer"] or None, latest=latest_print(),
                   public_url=public_url() or None,
                   printers=list_printers(), export=exporter.status(),
                   address=f"http://{lan_address()}:{CFG['port']}",
                   addresses=[f"http://{ip}:{CFG['port']}" for ip in all_addresses()],
                   ui={k: CFG[k] for k in UI_KEYS})


@app.post("/api/session")
def new_session():
    layout = (request.get_json(silent=True) or {}).get("layout", "grid")
    if layout not in LAYOUTS or layout not in CFG["layouts"]:
        abort(400)
    sid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    d = SESSIONS / sid
    d.mkdir()
    write_meta(d, {"layout": layout, "shots": LAYOUTS[layout], "created": time.time(), "prints": 0,
                   "token": uuid.uuid4().hex[:12],
                   "discarded": False})
    return jsonify(id=sid, layout=layout, shots=LAYOUTS[layout])


@app.post("/api/session/<sid>/shot/<int:idx>")
def shoot(sid, idx):
    d = session_dir(sid)
    meta = read_meta(d)
    if not 0 <= idx < meta["shots"]:
        abort(400)
    for old in d.glob(f"shot{idx}.*"):  # retake replaces the old frame
        old.unlink()
    try:
        path = camera.capture(d / f"shot{idx}")
    except Exception as e:
        return jsonify(ok=False, message=str(e)), 500
    if path != d / f"shot{idx}.jpg":
        path = Path(shutil.move(str(path), str(d / f"shot{idx}.jpg")))
    # screen-size preview so the browser is not loading a 24MP file
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    if int(CFG.get("rotate", 0)) % 360:
        # camera is mounted rotated: fix the photo itself so prints, GIF and uploads are upright
        img = img.rotate(-int(CFG["rotate"]), expand=True)
    # every photo gets the 4-up cell's framing, so singles, GIF and 4-up all match
    cropped = crop_to_cell(img, CFG)
    if cropped is not img or int(CFG.get("rotate", 0)) % 360:
        img = cropped
        img.save(d / "shot_fix.tmp", format="JPEG", quality=95)
        os.replace(d / "shot_fix.tmp", path)
    img.thumbnail((1600, 1600))
    img.save(d / f"shot{idx}_preview.jpg", quality=85)
    return jsonify(ok=True, preview=f"/sessions/{sid}/shot{idx}_preview.jpg?t={time.time():.0f}")


@app.post("/api/session/<sid>/compose")
def make_print(sid):
    d = session_dir(sid)
    meta = read_meta(d)
    shots = [d / f"shot{i}.jpg" for i in range(meta["shots"])]
    missing = [p.name for p in shots if not p.exists()]
    if missing:
        return jsonify(ok=False, message=f"Missing {', '.join(missing)}"), 400
    out = compose(shots, meta["layout"], CFG)
    out.save(d / "print.tmp", format="JPEG", quality=95, dpi=(300, 300))
    os.replace(d / "print.tmp", d / "print.jpg")
    share = trim_bleed(out, CFG)   # as it looks once the printer has trimmed it
    share.save(d / "print_share.tmp", format="JPEG", quality=92)
    os.replace(d / "print_share.tmp", d / "print_share.jpg")
    share.thumbnail((1400, 1400))
    share.save(d / "print_preview.jpg", quality=85)
    (d / "print_thumb.jpg").unlink(missing_ok=True)
    t = f"{time.time():.3f}"
    gif = None
    if len(shots) > 1:
        make_gif(shots, d / "animation.gif", CFG["gif_fps"], CFG["gif_width"])
        gif = f"/sessions/{sid}/animation.gif?t={t}"
    exporter.sync(sid, meta)  # share link + uploads start now, so the QR shows on this screen
    party.add_session(d, meta)  # and the party page gets this set
    return jsonify(ok=True, preview=f"/sessions/{sid}/print_preview.jpg?t={t}", gif=gif,
                   shots=[f"/sessions/{sid}/shot{i}_preview.jpg?t={t}" for i in range(len(shots))])


def _thumb(d):
    """Small square-ish thumbnail of the print for the gallery grid (made once)."""
    t = d / "print_thumb.jpg"
    src = d / "print_share.jpg" if (d / "print_share.jpg").exists() else d / "print.jpg"
    if not t.exists() and src.exists():
        im = Image.open(src).convert("RGB")
        im.thumbnail((480, 480))
        im.save(d / "print_thumb.tmp", format="JPEG", quality=82)
        os.replace(d / "print_thumb.tmp", t)
    return t.exists()


def latest_print():
    """Thumbnail URL of the newest finished session, for the gallery tile."""
    for d in sorted((p for p in SESSIONS.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        try:
            meta = read_meta(d)
        except (OSError, ValueError):
            continue
        if (d / "print.jpg").exists() and _thumb(d):
            return f"/sessions/{d.name}/print_thumb.jpg"
    return None


@app.get("/api/gallery")
def gallery():
    """Finished sessions, newest first, for the gallery screen."""
    offset = max(0, int(request.args.get("offset", 0)))
    limit = max(1, min(int(request.args.get("limit", 24)), 100))
    items = []
    for d in sorted((p for p in SESSIONS.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
        try:
            meta = read_meta(d)
        except (OSError, ValueError):
            continue
        if not (d / "print.jpg").exists():
            continue
        items.append((d, meta))
    page = items[offset:offset + limit]
    out = []
    for d, meta in page:
        if not _thumb(d):
            continue
        out.append({"id": d.name, "shots": meta["shots"],
                    "thumb": f"/sessions/{d.name}/print_thumb.jpg",
                    "preview": f"/sessions/{d.name}/print_preview.jpg",
                    "gif": f"/sessions/{d.name}/animation.gif" if (d / "animation.gif").exists() else None})
    return jsonify(items=out, more=offset + limit < len(items))


# ---------- guest pages: what the QR code opens on a phone ----------
_tunnel = {"mtime": None, "url": ""}


def public_url():
    """Where guests' phones can reach this booth: a fixed public_url from config, or the
    address of the Cloudflare quick tunnel the launcher started (written to tunnel.json)."""
    if CFG.get("public_url"):
        return CFG["public_url"].rstrip("/")
    f = ROOT / "tunnel.json"
    try:
        m = f.stat().st_mtime
    except OSError:
        return ""
    if _tunnel["mtime"] != m:
        try:
            _tunnel["url"] = json.loads(f.read_text(encoding="utf-8")).get("url", "").rstrip("/")
        except (OSError, ValueError):
            _tunnel["url"] = ""
        _tunnel["mtime"] = m
    return _tunnel["url"]


def guest_url(meta, sid):
    base = public_url()
    if base and meta.get("token"):
        return f"{base}/g/{sid}/{meta['token']}"
    return None


def is_public_request():
    """True when the request came through the tunnel rather than from the booth's own network."""
    host = request.host.split(":")[0]
    return not (host == "localhost" or host.replace(".", "").isdigit())


@app.before_request
def guard_public():
    # Through the tunnel, only guest pages are reachable; the booth itself stays private.
    if is_public_request() and not (request.path.startswith("/g/") or request.path.startswith("/p/")):
        abort(404)


GUEST_FILES = {"print.jpg", "print_share.jpg", "print_preview.jpg", "animation.gif"} | {f"shot{i}.jpg" for i in range(4)} \
              | {f"shot{i}_preview.jpg" for i in range(4)}


def guest_session(sid, token):
    d = session_dir(sid)
    meta = read_meta(d)
    if not meta.get("token") or meta["token"] != token:
        abort(404)
    return d, meta


@app.get("/g/<sid>/<token>")
def guest_page(sid, token):
    d, meta = guest_session(sid, token)
    base = f"/g/{sid}/{token}/f"
    photos = [i for i in range(meta["shots"]) if (d / f"shot{i}.jpg").exists()]
    dropbox = exporter.share(sid)[1] or ""
    html = (ROOT / "static" / "guest.html").read_text(encoding="utf-8")
    data = {"base": base, "photos": photos, "gif": (d / "animation.gif").exists(),
            "print": "print_share.jpg" if (d / "print_share.jpg").exists() else ("print.jpg" if (d / "print.jpg").exists() else None),
            "dropbox": dropbox, "name": time.strftime(
                "%b %d, %I:%M %p", time.localtime(meta.get("created", 0))).replace(" 0", " ")}
    html = html.replace("/*DATA*/", json.dumps(data))
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.get("/g/<sid>/<token>/f/<name>")
def guest_file(sid, token, name):
    d, _ = guest_session(sid, token)
    if name not in GUEST_FILES:
        abort(404)
    return send_from_directory(d, name, max_age=3600, as_attachment=request.args.get("dl") == "1")


@app.get("/api/session/<sid>/share")
def share_state(sid):
    """The client polls this until there's a link for the QR code: the booth's own guest
    page when guests' phones can reach us, otherwise the Dropbox folder."""
    d = session_dir(sid)
    meta = read_meta(d)
    if guest_url(meta, sid):
        return jsonify(state="ready", qr=f"/api/session/{sid}/qr.svg", error=None)
    state, url, error = exporter.share(sid)
    if state == "failed" and exporter.retry_link(sid):
        state = "pending"
    return jsonify(state=state, qr=f"/api/session/{sid}/qr.svg" if url else None, error=error)


@app.post("/api/session/<sid>/discard")
def discard(sid):
    """Start over: nothing is deleted. The set stays in the gallery, in Dropbox and on
    its guest page; the screen just goes back to the start."""
    session_dir(sid)
    return jsonify(ok=True)


@app.post("/api/session/<sid>/print")
def do_print(sid):
    d = session_dir(sid)
    if not (d / "print.jpg").exists():
        return jsonify(ok=False, message="Nothing to print yet"), 400
    copies = (request.get_json(silent=True) or {}).get("copies", 1)
    meta = read_meta(d)
    if exporter.share(sid)[0] == "failed":  # no link yet (was offline): try again
        exporter.retry_link(sid)
    with print_lock:
        result = print_image(d / "print.jpg", copies, CFG)
    if result.get("ok"):
        meta["prints"] += result.get("copies", 0)
        write_meta(d, meta)
    return jsonify(result), (200 if result.get("ok") else 500)


@app.get("/api/session/<sid>/qr.svg")
def qr(sid):
    d = session_dir(sid)
    url = guest_url(read_meta(d), sid) or exporter.share(sid)[1]
    if not url:
        abort(404)
    try:
        import segno
    except ImportError:
        return Response("QR library missing: quit the booth and run start.command / start.bat again",
                        status=500, mimetype="text/plain")
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=10, border=2, dark="#000", light="#fff")
    return Response(buf.getvalue(), mimetype="image/svg+xml")


# ---------- end of party page ----------
import party
party.setup(ROOT, SESSIONS, CFG, read_meta, exporter, lambda: public_url() or f"http://{lan_address()}:{CFG['port']}")



@app.get("/p/<token>/")
@app.get("/p/<token>/<path:name>")
def party_page(token, name="index.html"):
    d = ROOT / "party" / token
    if not token.isalnum() or not d.is_dir():
        abort(404)
    return send_from_directory(d, name, max_age=0 if name in ("index.html", "sets.json") else 3600)


@app.post("/api/admin/party")
def admin_party():
    """Rebuild the page from every set in the gallery, make the video of the night, publish."""
    if not _admin_ok():
        return jsonify(ok=False, message="Wrong PIN"), 403
    if party.status()["stage"] in ("building", "uploading"):
        return jsonify(ok=False, message="Still working on the last one"), 409
    try:
        party.build()
    except Exception as e:
        return jsonify(ok=False, message=f"{type(e).__name__}: {e}"[:300]), 500
    return jsonify(ok=True, **party.status())


@app.get("/api/admin/party")
def admin_party_status():
    return jsonify(ok=True, **party.status())


@app.get("/api/party/qr.svg")
def party_qr():
    url = party.status()["url"]
    if not url:
        abort(404)
    import segno
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=10, border=2, dark="#000", light="#fff")
    return Response(buf.getvalue(), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


# ---------- admin panel (PIN protected) ----------
def _admin_ok():
    body = request.get_json(silent=True) or {}
    return str(body.get("pin", "")) == str(CFG.get("admin_pin", ""))


def save_settings(changes):
    """Apply settings now and write them into config.json (keeping everything else in it)."""
    CFG.update(changes)
    path = ROOT / "config.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    current.update(changes)
    tmp = ROOT / "config.json.tmp"
    tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


@app.post("/api/admin/login")
def admin_login():
    if not _admin_ok():
        return jsonify(ok=False, message="Wrong PIN"), 403
    return jsonify(ok=True, settings={k: CFG[k] for k in ("rotate", "mirror_preview", "countdown", "countdown_next",
                                                           "liveview_fps", "spacing", "print_bleed", "sounds")},
                   public_url=public_url() or None,
                   addresses=[f"http://{ip}:{CFG['port']}" for ip in all_addresses()])


@app.post("/api/admin/settings")
def admin_settings():
    if not _admin_ok():
        return jsonify(ok=False, message="Wrong PIN"), 403
    body = request.get_json(silent=True) or {}
    allowed = {"rotate": int, "mirror_preview": bool, "countdown": int, "countdown_next": int,
               "liveview_fps": int, "spacing": int, "print_bleed": int, "sounds": bool}
    changes = {}
    for k, cast in allowed.items():
        if k in body:
            try:
                changes[k] = cast(body[k])
            except (TypeError, ValueError):
                return jsonify(ok=False, message=f"Bad value for {k}"), 400
    if "rotate" in changes and changes["rotate"] not in (0, 90, 180, 270):
        return jsonify(ok=False, message="Rotation must be 0, 90, 180 or 270"), 400
    save_settings(changes)
    return jsonify(ok=True, settings={k: CFG[k] for k in allowed})


@app.post("/api/admin/clear")
def admin_clear():
    """Start of an event: move every session out of the gallery into archive/<date-time>/.
    Nothing is deleted, and Dropbox is left alone. Sessions still uploading stay put."""
    if not _admin_ok():
        return jsonify(ok=False, message="Wrong PIN"), 403
    uploading = {v.parent.name for v in exporter._pending}
    dest = ROOT / "archive" / time.strftime("%Y-%m-%d_%H-%M-%S")
    moved = skipped = 0
    for d in list(SESSIONS.iterdir()):
        if not d.is_dir():
            continue
        if d.name in uploading:
            skipped += 1
            continue
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(dest / d.name))
        moved += 1
    return jsonify(ok=True, moved=moved, skipped=skipped,
                   message=f"Moved {moved} session{'s' if moved != 1 else ''} to archive"
                           + (f", {skipped} still uploading, try again in a minute" if skipped else "") + ".")


@app.post("/api/admin/test-print")
def admin_test_print():
    if not _admin_ok():
        return jsonify(ok=False, message="Wrong PIN"), 403
    latest = latest_print()
    if not latest:
        return jsonify(ok=False, message="No print to test with yet"), 400
    sid = latest.split("/")[2]
    with print_lock:
        result = print_image(SESSIONS / sid / "print.jpg", 1, CFG)
    return jsonify(result), (200 if result.get("ok") else 500)


@app.get("/sessions/<sid>/<path:name>")
def session_file(sid, name):
    return send_from_directory(session_dir(sid), name)


if __name__ == "__main__":
    try:
        import segno  # noqa: F401
    except ImportError:
        print("WARNING: the QR code library isn't installed. Relaunch with start.command / start.bat.")
    print(f"Photobooth version {VERSION}")
    print(f"Photobooth running. Camera: {camera.name}. Printer: {CFG['printer'] or '(none, save only)'}. "
          f"Dropbox: {exporter.status()['dropbox']}.")
    print(f"  On this computer: http://127.0.0.1:{CFG['port']}")
    for ip in all_addresses():
        print(f"  On the tablet:    http://{ip}:{CFG['port']}")
    print("  (a hotspot hosted by this PC is usually the 192.168.137.x one)")
    app.run(host=CFG["host"], port=CFG["port"], threaded=True, debug=False)
