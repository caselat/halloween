"""Sending finished sessions out: a local folder copy and/or Dropbox with a share link.

Every time a session's results are made (and again after each retake) sync()
freezes that set of files as a new version (sessions/<id>/export-<n>/), then:
  - copies it to save_folder, if set
  - creates a new Dropbox folder for it and a share link in the background, so a
    QR code can show on the results screen within a second or two
  - queues its files for upload.
A retake never touches an earlier version's folder, so a QR code someone already
scanned keeps showing what it showed. Unfinished uploads resume after a restart
or when the internet returns. discard() (Start over) removes every version.
"""

import json
import os
import queue
import shutil
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PENDING = "uploads.json"  # per version: which files are uploaded, and whether it's complete
SHARE = "share.json"      # per version: its Dropbox share link


def write_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def export_names(meta):
    """(file in session folder, name it gets when exported), in upload order.

    Names start with when the session was taken, so they sort in order and stay
    unique once downloaded: 2026-10-25_22-14-03_photo1.jpg
    One photo: just the full-resolution original (the print is a crop of it).
    Four photos: the GIF, the 4-up print, and the four originals.
    """
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(meta.get("created", time.time())))
    if meta["shots"] == 1:
        return [("shot0.jpg", f"{stamp}_photo.jpg")]
    names = [("animation.gif", f"{stamp}_animation.gif"), ("print.jpg", f"{stamp}_4up.jpg")]
    names += [(f"shot{i}.jpg", f"{stamp}_photo{i + 1}.jpg") for i in range(meta["shots"])]
    return names


def remote_root(cfg):
    """Folder inside the app's Dropbox folder that sessions go in. "" = the app folder itself."""
    sub = (cfg.get("dropbox_folder") or "").strip().strip("/")
    return "/" + sub if sub else ""


class DropboxError(RuntimeError):
    pass


class Dropbox:
    API = os.environ.get("PHOTOBOOTH_DROPBOX_API", "https://api.dropboxapi.com")
    CONTENT = os.environ.get("PHOTOBOOTH_DROPBOX_CONTENT", "https://content.dropboxapi.com")

    def __init__(self, creds):
        self.app_key = creds["app_key"]
        self.app_secret = creds["app_secret"]
        self.refresh_token = creds["refresh_token"]
        self._token, self._expires = None, 0.0
        self._lock = threading.Lock()

    # --- auth ---
    @classmethod
    def exchange_code(cls, app_key, app_secret, code):
        return cls._post_form(cls.API + "/oauth2/token", {
            "code": code, "grant_type": "authorization_code",
            "client_id": app_key, "client_secret": app_secret})

    @staticmethod
    def _post_form(url, fields):
        data = urllib.parse.urlencode(fields).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise DropboxError(f"Dropbox sign-in failed: {e.read().decode(errors='replace')[:200]}")

    def _access_token(self, force=False):
        with self._lock:
            if force or not self._token or time.time() > self._expires - 60:
                j = self._post_form(self.API + "/oauth2/token", {
                    "grant_type": "refresh_token", "refresh_token": self.refresh_token,
                    "client_id": self.app_key, "client_secret": self.app_secret})
                self._token = j["access_token"]
                self._expires = time.time() + int(j.get("expires_in", 14400))
            return self._token

    # --- calls ---
    def _call(self, url, headers, data, timeout):
        refreshed, busy = False, 0
        while True:
            h = dict(headers, Authorization="Bearer " + self._access_token(force=refreshed))
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h),
                                            timeout=timeout) as r:
                    body = r.read()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                if e.code == 401 and not refreshed:
                    refreshed = True  # token expired early: refresh and retry once
                    continue
                if (e.code == 429 or e.code >= 500) and busy < 6:
                    # Dropbox is busy (often two writes to the same folder at once):
                    # wait as long as it asks, then retry
                    busy += 1
                    try:
                        wait = float(e.headers.get("Retry-After") or 0)
                    except ValueError:
                        wait = 0
                    time.sleep(min(max(wait, 0.5 * busy), 10))
                    continue
                try:
                    summary = json.loads(body).get("error_summary", "")
                except ValueError:
                    summary = body.strip()[:200]
                call = url.split("/2/")[-1]
                raise DropboxError(f"{call} HTTP {e.code}: {summary or e.reason or 'no details'}") from None
            except urllib.error.URLError as e:
                raise DropboxError(f"can't reach Dropbox ({e.reason})") from None

    def rpc(self, endpoint, body):
        return self._call(self.API + "/2/" + endpoint, {"Content-Type": "application/json"},
                          json.dumps(body).encode(), timeout=20)

    def upload(self, local: Path, remote: str):
        arg = json.dumps({"path": remote, "mode": "overwrite", "mute": True})  # ASCII-escaped
        self._call(self.CONTENT + "/2/files/upload",
                   {"Content-Type": "application/octet-stream", "Dropbox-API-Arg": arg},
                   Path(local).read_bytes(), timeout=300)

    def create_folder(self, path):
        try:
            self.rpc("files/create_folder_v2", {"path": path, "autorename": False})
        except DropboxError as e:
            if "conflict" not in str(e):
                raise

    def share_link(self, path):
        try:
            return self.rpc("sharing/create_shared_link_with_settings", {"path": path})["url"]
        except DropboxError as e:
            if "shared_link_already_exists" not in str(e):
                raise
        links = self.rpc("sharing/list_shared_links", {"path": path, "direct_only": True})["links"]
        if not links:
            raise DropboxError("Couldn't get a share link")
        return links[0]["url"]


class Exporter:
    def __init__(self, cfg, root: Path, sessions: Path):
        self.cfg, self.sessions = cfg, sessions
        self.save_folder = Path(cfg["save_folder"]).expanduser() if cfg.get("save_folder") else None
        self.remote_root = remote_root(cfg)
        self.dbx, self.dropbox_error = None, None
        self._creds = root / "dropbox.json"
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._pending = set()     # version folders with uploads queued or running
        self._linking = set()     # version folders whose share link is being created
        self._discarded = set()   # session ids
        self._link_tried = {}     # version folder -> last attempt time
        self._link_error = {}     # version folder -> why the last attempt failed
        self.last_upload_error = None
        self._started = False
        self._ensure_dropbox()

    def _ensure_dropbox(self):
        """Connect if dropbox.json exists. Called again on each status check and
        sync, so running the Dropbox setup doesn't need a booth restart."""
        if self.dbx or not self._creds.exists():
            return self.dbx
        try:
            self.dbx = Dropbox(json.loads(self._creds.read_text(encoding="utf-8")))
            self.dropbox_error = None
        except Exception as e:
            self.dropbox_error = f"dropbox.json unreadable: {e}"
            return None
        if not self._started:
            self._started = True
            threading.Thread(target=self._worker, daemon=True, name="uploader").start()
            with self._lock:  # resume after a restart
                for marker in sorted(self.sessions.glob(f"*/export-*/{PENDING}")):
                    try:
                        if not json.loads(marker.read_text(encoding="utf-8")).get("complete"):
                            self._enqueue_locked(marker.parent)
                    except ValueError:
                        pass
        return self.dbx

    def status(self):
        if not self._ensure_dropbox():
            return {"dropbox": "error" if self.dropbox_error else "off",
                    "error": self.dropbox_error, "uploading": 0}
        return {"dropbox": "on", "error": self.last_upload_error, "uploading": len(self._pending)}

    # ---------- versions ----------
    def _versions(self, sid):
        d = self.sessions / sid
        found = []
        for p in d.glob("export-*"):
            try:
                found.append((int(p.name.split("-")[1]), p))
            except (IndexError, ValueError):
                pass
        return [p for _, p in sorted(found)]

    @staticmethod
    def _name(sid, n):
        return sid if n == 1 else f"{sid}-retake{n - 1}"

    def _snapshot(self, sid, meta):
        """Freeze the current files as a new version, named as they'll be exported."""
        d = self.sessions / sid
        n = len(self._versions(sid)) + 1
        v = d / f"export-{n}"
        v.mkdir()
        for src, dst in export_names(meta):
            if (d / src).exists():
                try:
                    os.link(d / src, v / dst)  # instant; a retake replaces the original, not this
                except OSError:
                    shutil.copy2(d / src, v / dst)
        write_json(v / "version.json", {"name": self._name(sid, n),
                                         "files": [dst for _, dst in export_names(meta)
                                                   if (v / dst).exists()]})
        return v

    # ---------- called by the server ----------
    def sync(self, sid, meta):
        """Session results were made or remade: export them as a new version."""
        with self._lock:
            self._discarded.discard(sid)
        v = self._snapshot(sid, meta)
        info = json.loads((v / "version.json").read_text(encoding="utf-8"))

        if self.save_folder:
            try:
                out = self.save_folder / info["name"]
                out.mkdir(parents=True, exist_ok=True)
                for f in info["files"]:
                    shutil.copy2(v / f, out / f)
            except Exception as e:
                print(f"[export] copy to {self.save_folder} failed: {e}")

        if not self._ensure_dropbox():
            return
        with self._lock:
            write_json(v / PENDING, {"remote": f"{self.remote_root}/{info['name']}",
                                     "done": [], "complete": False})
            self._enqueue_locked(v)
            self._linking.add(v)
        threading.Thread(target=self._make_link, args=(v,), daemon=True).start()

    def retry_link(self, sid, min_interval=4.0):
        """No link for the latest version yet (offline, or Dropbox refused): try again,
        at most every few seconds. Returns True if an attempt is running."""
        vs = self._versions(sid)
        if not vs or not self.dbx or (vs[-1] / SHARE).exists():
            return False
        v = vs[-1]
        if v in self._linking:
            return True
        if time.time() - self._link_tried.get(v, 0) < min_interval:
            return False
        self._linking.add(v)
        threading.Thread(target=self._make_link, args=(v,), daemon=True).start()
        return True

    def share(self, sid):
        """For the latest version: (state, url, error). state is ready | pending | failed | off."""
        if not self.dbx:
            return "off", None, None
        vs = self._versions(sid)
        if not vs:
            return "failed", None, "no results yet"
        v = vs[-1]
        if (v / SHARE).exists():
            return "ready", json.loads((v / SHARE).read_text(encoding="utf-8"))["url"], None
        return ("pending" if v in self._linking else "failed"), None, self._link_error.get(v)

    def discard(self, sid):
        """Start over: remove every version from the save folder and Dropbox."""
        with self._lock:
            self._discarded.add(sid)
            versions = self._versions(sid)
            for v in versions:
                for name in (PENDING, SHARE):
                    (v / name).unlink(missing_ok=True)
        for v in versions:
            name = json.loads((v / "version.json").read_text(encoding="utf-8"))["name"]
            if self.save_folder:
                shutil.rmtree(self.save_folder / name, ignore_errors=True)
            if self.dbx:
                self._q.put(("delete", f"{self.remote_root}/{name}"))  # after uploads stop

    # ---------- background ----------
    def _make_link(self, v: Path):
        self._link_tried[v] = time.time()
        sid = v.parent.name
        name = json.loads((v / "version.json").read_text(encoding="utf-8"))["name"]
        remote = f"{self.remote_root}/{name}"
        try:
            self.dbx.create_folder(remote)
            url = self.dbx.share_link(remote)
            with self._lock:
                if sid not in self._discarded:
                    write_json(v / SHARE, {"url": url})
            self._link_error.pop(v, None)
            if (self.last_upload_error or "").startswith("Dropbox share link"):
                self.last_upload_error = None
        except Exception as e:  # offline or refused: the screen keeps asking, so we retry
            self._link_error[v] = str(e)
            self.last_upload_error = f"Dropbox share link: {e}"
            print(f"[export] couldn't create share link for {v.parent.name}/{v.name}: {e}")
        finally:
            self._linking.discard(v)

    def _enqueue_locked(self, v: Path):
        if v not in self._pending:
            self._pending.add(v)
            self._q.put(("upload", v))

    def _worker(self):
        while True:
            kind, target = self._q.get()
            try:
                if kind == "delete":
                    self._delete(target)
                else:
                    self._upload(target)
            except Exception as e:
                why = str(e) if isinstance(e, DropboxError) else f"{type(e).__name__}: {e}"
                self.last_upload_error = f"Dropbox {kind} waiting to retry: {why}"
                print(f"[export] {kind} of {target} failed, retrying in 20s: {why}")
                if not isinstance(e, DropboxError):
                    traceback.print_exc()
                threading.Timer(20, self._q.put, args=((kind, target),)).start()

    def _delete(self, remote):
        try:
            self.dbx.rpc("files/delete_v2", {"path": remote})
        except DropboxError as e:
            if "not_found" not in str(e):
                raise

    def _upload(self, v: Path):
        sid, marker = v.parent.name, v / PENDING
        with self._lock:
            if sid in self._discarded or not marker.exists():
                self._pending.discard(v)
                return
            state = json.loads(marker.read_text(encoding="utf-8"))
        files = json.loads((v / "version.json").read_text(encoding="utf-8"))["files"]
        # Let the share link go first: two writes to the same new Dropbox folder at once
        # get "429 Too Many Requests", and the link is what the guest is waiting on.
        deadline = time.time() + 20
        while v in self._linking and time.time() < deadline:
            time.sleep(0.2)
        for f in files:
            if f in state["done"]:
                continue
            if sid in self._discarded:
                break
            self.dbx.upload(v / f, f"{state['remote']}/{f}")
            with self._lock:
                if not marker.exists():
                    break
                state["done"].append(f)
                write_json(marker, state)
        with self._lock:
            if marker.exists() and sid not in self._discarded and len(state["done"]) >= len(files):
                state["complete"] = True
                write_json(marker, state)
                self.last_upload_error = None
            self._pending.discard(v)
        if not (v / SHARE).exists() and v not in self._linking and sid not in self._discarded:
            self._linking.add(v)  # link failed earlier: one more try now the files are there
            self._make_link(v)
