"""Camera backends.

Each backend keeps a background live view running and exposes:
  wait_frame(last_id, timeout) -> (frame_id, jpeg_bytes) or None
  capture(dest_stem: Path) -> Path to a full-res JPEG
  status() -> dict

Backends:
  canon           Windows, direct through Canon's EDSDK (canon_sdk.py), smooth live view
  digicamcontrol  Windows, through digiCamControl's webserver (slow live view, fallback)
  gphoto2         Mac and Linux, Canon over USB (brew install gphoto2)
  webcam          Any webcam via OpenCV, for testing the look on a laptop
  mock            Generated frames, for testing with no hardware
"""

import http.client
import io
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse


class BaseCamera:
    name = "base"

    def __init__(self):
        self._cond = threading.Condition()
        self._frame = None
        self._frame_id = 0
        self._capture_lock = threading.Lock()
        self._paused = threading.Event()
        self._running = True
        self.last_error = None

    def _publish(self, jpeg: bytes):
        with self._cond:
            self._frame = jpeg
            self._frame_id += 1
            self._cond.notify_all()

    def wait_frame(self, last_id: int, timeout: float = 2.0):
        with self._cond:
            if self._frame_id == last_id:
                self._cond.wait(timeout)
            if self._frame is None or self._frame_id == last_id:
                return None
            return self._frame_id, self._frame

    def status(self):
        return {"backend": self.name, "error": self.last_error,
                "live": self._frame is not None and not self._paused.is_set()}

    def stop(self):
        self._running = False


class MockCamera(BaseCamera):
    """Animated test pattern so the whole flow works without a camera."""
    name = "mock"

    def __init__(self):
        super().__init__()
        from PIL import Image, ImageDraw
        self._Image, self._Draw = Image, ImageDraw
        threading.Thread(target=self._loop, daemon=True).start()

    def _render(self, w, h, t, label):
        img = self._Image.new("RGB", (w, h))
        d = self._Draw.Draw(img)
        # slow moving diagonal bands so motion is obvious
        band = max(8, w // 12)
        off = int(t * band * 0.8) % (band * 2)
        for x in range(-h - band * 2, w + band * 2, band * 2):
            d.polygon([(x + off, 0), (x + off + band, 0),
                       (x + off + band - h, h), (x + off - h, h)], fill=(52, 46, 40))
        d.rectangle([0, 0, w, h], outline=(240, 200, 150), width=max(2, w // 200))
        cx, cy, r = w // 2, h // 2, h // 5
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(240, 200, 150), width=max(2, w // 300))
        d.text((w * 0.04, h * 0.06), label, fill=(240, 200, 150))
        return img

    def _loop(self):
        start = time.time()
        while self._running:
            if not self._paused.is_set():
                img = self._render(960, 640, time.time() - start,
                                   "MOCK CAMERA  " + time.strftime("%H:%M:%S"))
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=70)
                self._publish(buf.getvalue())
            time.sleep(1 / 15)

    def capture(self, dest_stem: Path) -> Path:
        with self._capture_lock:
            self._paused.set()
            try:
                time.sleep(0.4)  # mimic shutter latency
                out = dest_stem.with_suffix(".jpg")
                self._render(3000, 2000, time.time(),
                             "MOCK CAPTURE  " + time.strftime("%H:%M:%S")).save(out, "JPEG", quality=92)
                return out
            finally:
                self._paused.clear()


class WebcamCamera(BaseCamera):
    """Any webcam through OpenCV. For testing the booth's look on a laptop.

    Feeds live view and captures through the same path as the real cameras,
    so everything downstream behaves identically. Captures are the full webcam
    frame (no strobe, of course).
    """
    name = "webcam"

    def __init__(self, cfg):
        super().__init__()
        import cv2
        self.cv2 = cv2
        src = cfg.get("webcam_index", 0)
        if sys.platform == "darwin":
            api = cv2.CAP_AVFOUNDATION
        elif sys.platform == "win32":
            api = cv2.CAP_DSHOW
        else:
            api = cv2.CAP_ANY
        # Opened here, on the main thread: macOS only shows its camera permission prompt from it.
        self.cap = cv2.VideoCapture(src, api) if isinstance(src, int) else cv2.VideoCapture(src)
        w, h = cfg.get("webcam_size", [1920, 1080])
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        self._latest, self._latest_at = None, 0.0
        self._latest_lock = threading.Lock()
        if not self.cap.isOpened():
            self.last_error = self._no_access_message()
        threading.Thread(target=self._loop, daemon=True).start()

    @staticmethod
    def _no_access_message():
        if sys.platform == "darwin":
            return ("Webcam not available. Allow Terminal in System Settings > Privacy & Security "
                    "> Camera, then relaunch.")
        return "Webcam not available."

    def _loop(self):
        cv2, fails = self.cv2, 0
        while self._running:
            if not self.cap.isOpened():
                time.sleep(1)
                continue
            ok, frame = self.cap.read()
            if not ok or frame is None:
                fails += 1
                if fails > 30:
                    self.last_error = self._no_access_message()
                time.sleep(0.05)
                continue
            fails = 0
            with self._latest_lock:
                self._latest, self._latest_at = frame, time.time()
            fh, fw = frame.shape[:2]
            small = cv2.resize(frame, (960, int(960 * fh / fw))) if fw > 960 else frame
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                self._publish(buf.tobytes())
                self.last_error = None

    def capture(self, dest_stem: Path) -> Path:
        with self._capture_lock:
            time.sleep(0.15)  # roughly where a real shutter would fire after the countdown
            with self._latest_lock:
                frame = None if self._latest is None else self._latest.copy()
                age = time.time() - self._latest_at
            if frame is None:
                raise RuntimeError(self.last_error or "The webcam has no picture yet.")
            if age > 2:
                raise RuntimeError("The webcam stopped sending pictures.")
            out = dest_stem.with_suffix(".jpg")
            self.cv2.imwrite(str(out), frame, [self.cv2.IMWRITE_JPEG_QUALITY, 95])
            return out

    def stop(self):
        super().stop()
        try:
            self.cap.release()
        except Exception:
            pass


def _gphoto2_sees_camera():
    try:
        r = subprocess.run(["gphoto2", "--auto-detect"], capture_output=True, text=True, timeout=8)
        rows = [line for line in r.stdout.splitlines()[2:] if line.strip()]
        return bool(rows)
    except Exception:
        return False


def _opencv_available():
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


class GPhoto2Camera(BaseCamera):
    """Canon (and most DSLRs) via the gphoto2 command line tool.

    Live view runs `gphoto2 --capture-movie --stdout` and splits the MJPEG
    stream into frames. To shoot, live view is stopped, a full capture runs,
    and live view restarts. The camera fires the strobe normally.
    """
    name = "gphoto2"

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.bin = shutil.which("gphoto2")
        if not self.bin:
            raise RuntimeError("gphoto2 not found. On a Mac: brew install gphoto2")
        self._proc = None
        self._configured = False
        threading.Thread(target=self._loop, daemon=True).start()

    # macOS starts its own PTP daemon whenever a camera appears and it holds the USB device.
    def _free_usb(self):
        if sys.platform == "darwin":
            subprocess.run(["pkill", "-f", "ptpcamera"], capture_output=True)
            subprocess.run(["pkill", "-f", "PTPCamera"], capture_output=True)

    def _run(self, args, timeout=20):
        self._free_usb()
        return subprocess.run([self.bin] + args, capture_output=True, text=True, timeout=timeout)

    def _configure(self):
        # Save straight to the computer (camera RAM), not the SD card.
        for setting in self.cfg.get("gphoto2_settings", ["capturetarget=0"]):
            r = self._run(["--set-config", setting])
            if r.returncode != 0:
                print(f"[camera] could not set {setting}: {r.stderr.strip()[:200]}")
        self._configured = True

    def _loop(self):
        while self._running:
            if self._paused.is_set():
                time.sleep(0.05)
                continue
            try:
                if not self._configured:
                    self._configure()
                self._free_usb()
                self._proc = subprocess.Popen([self.bin, "--capture-movie", "--stdout"],
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                buf = b""
                while self._running and not self._paused.is_set():
                    chunk = self._proc.stdout.read1(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        start = buf.find(b"\xff\xd8")
                        end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                        if start < 0 or end < 0:
                            break
                        self._publish(buf[start:end + 2])
                        buf = buf[end + 2:]
                    if len(buf) > 4_000_000:  # never let a bad stream grow forever
                        buf = b""
                    self.last_error = None
                self._stop_proc()
                if not self._paused.is_set() and self._running:
                    self.last_error = "Live view stopped. Is the camera on and connected?"
                    time.sleep(1.5)
            except Exception as e:  # keep retrying, the booth must not die
                self.last_error = str(e)
                time.sleep(1.5)

    def _stop_proc(self):
        p, self._proc = self._proc, None
        if p and p.poll() is None:
            p.send_signal(signal.SIGINT)  # lets gphoto2 end movie mode cleanly
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def capture(self, dest_stem: Path) -> Path:
        with self._capture_lock:
            self._paused.set()
            try:
                self._stop_proc()
                time.sleep(0.3)
                pattern = str(dest_stem) + ".%C"
                last = ""
                for attempt in range(2):
                    r = self._run(["--capture-image-and-download", "--force-overwrite",
                                   "--filename", pattern], timeout=30)
                    jpg = next((p for p in (dest_stem.with_suffix(".jpg"), dest_stem.with_suffix(".JPG"))
                                if p.exists()), None)
                    if r.returncode == 0 and jpg:
                        return jpg
                    last = (r.stderr or r.stdout).strip()[-300:]
                    time.sleep(0.8)
                if dest_stem.with_suffix(".cr2").exists() or dest_stem.with_suffix(".cr3").exists():
                    last = "Camera is shooting RAW only. Set image quality to JPEG."
                raise RuntimeError(last or "Capture failed")
            finally:
                self._paused.clear()


class DigiCamCamera(BaseCamera):
    """Windows: drives the camera through digiCamControl's built-in webserver.

    digiCamControl (free) talks to the Canon with Canon's own SDK. It must be
    running with Settings > Webserver enabled (default port 5513).
      live view frame:  GET /liveview.jpg
      start live view:  GET /?CMD=LiveViewWnd_Show
      capture to file:  GET /?slc=capture&param1=<full path>&param2=
      last file:        GET /?slc=get&param1=lastcaptured&param2=   ("-" while busy)
    """
    name = "digicamcontrol"

    def __init__(self, cfg):
        super().__init__()
        # "localhost" on Windows tries IPv6 first and stalls ~2s per request when
        # digiCamControl only listens on IPv4, so always use the IPv4 address.
        self.base = cfg.get("digicam_url", "http://127.0.0.1:5513").rstrip("/") \
            .replace("//localhost", "//127.0.0.1")
        u = urlparse(self.base)
        self._host, self._port = u.hostname, u.port or 80
        self._lv_conn = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _liveview_frame(self) -> bytes:
        """Fetch one live view frame over a reused connection."""
        if self._lv_conn is None:
            self._lv_conn = http.client.HTTPConnection(self._host, self._port, timeout=3)
        try:
            self._lv_conn.request("GET", "/liveview.jpg")
            r = self._lv_conn.getresponse()
            data = r.read()
            if r.status != 200:
                raise ValueError(f"live view HTTP {r.status}")
            return data
        except Exception:
            self._lv_conn.close()
            self._lv_conn = None
            raise

    def _get(self, query, timeout=5.0) -> bytes:
        with urllib.request.urlopen(self.base + query, timeout=timeout) as r:
            return r.read()

    def _slc(self, cmd, p1="", p2="", timeout=30.0) -> str:
        q = "/?slc={}&param1={}&param2={}".format(cmd, quote(p1, safe=""), quote(p2, safe=""))
        return self._get(q, timeout).decode("utf-8", "replace").strip().strip('"')

    def _loop(self):
        fails, last_kick = 0, 0.0
        while self._running:
            if self._paused.is_set():
                time.sleep(0.05)
                continue
            try:
                data = self._liveview_frame()
                if not data.startswith(b"\xff\xd8"):
                    raise ValueError("no live view frame")
                self._publish(data)
                fails, self.last_error = 0, None
            except ConnectionRefusedError:
                self.last_error = ("Can't reach digiCamControl. Is it open, with "
                                   "Settings > Webserver turned on?")
                time.sleep(1.5)
                continue
            except Exception:
                fails += 1
            # live view isn't running (startup, or the camera dropped out of it): ask for it
            if fails >= 15 and time.time() - last_kick > 8:
                last_kick = time.time()
                self.last_error = "Starting live view. Is the camera on and connected?"
                try:
                    self._get("/?CMD=LiveViewWnd_Show", timeout=5)
                except Exception:
                    pass
            time.sleep(1 / 20)

    @staticmethod
    def _settled(p: Path) -> bool:
        try:
            a = p.stat().st_size
            time.sleep(0.2)
            return a > 0 and a == p.stat().st_size
        except OSError:
            return False

    def capture(self, dest_stem: Path) -> Path:
        with self._capture_lock:
            self._paused.set()
            try:
                target = dest_stem.with_suffix(".jpg")
                folder = dest_stem.parent
                started = time.time()
                try:
                    before = self._slc("get", "lastcaptured", timeout=5)
                except Exception:
                    before = ""
                reply = self._slc("capture", str(target))
                if "error" in reply.lower():
                    raise RuntimeError(f"digiCamControl: {reply[:200]}")

                deadline = started + 25
                while time.time() < deadline:
                    if target.exists() and self._settled(target):
                        return target
                    try:
                        last = self._slc("get", "lastcaptured", timeout=5)
                    except Exception:
                        last = "-"
                    if last and last not in ("-", before):
                        for cand in (Path(last), folder / Path(last).name):
                            if cand.exists():
                                if cand.suffix.lower() not in (".jpg", ".jpeg"):
                                    raise RuntimeError("Camera is shooting RAW only. Set image quality to JPEG.")
                                if self._settled(cand):
                                    return cand
                    # catch files saved under a slightly different name in the session folder
                    for cand in list(folder.glob("*.jpg")) + list(folder.glob("*.JPG")):
                        if "_preview" not in cand.name and cand.stat().st_mtime >= started - 1 \
                                and cand != target and self._settled(cand):
                            return cand
                    time.sleep(0.25)
                raise RuntimeError("The camera didn't send a photo back. Check that it's on, "
                                   "in focus, and that digiCamControl shows it connected.")
            finally:
                self._paused.clear()


def make_camera(cfg):
    choice = cfg.get("camera", "auto")
    if choice == "mock":
        return MockCamera()
    if choice == "auto" and sys.platform == "win32":
        from canon_sdk import find_edsdk, python_is_32bit
        if find_edsdk(cfg) and python_is_32bit():
            choice = "canon"
        else:
            print("[camera] Canon SDK not usable here (needs digiCamControl installed and 32-bit "
                  "Python). Falling back to digiCamControl's webserver.")
            choice = "digicamcontrol"
    if choice == "canon":
        from canon_sdk import CanonSdkCamera
        return CanonSdkCamera(cfg)
    if choice == "digicamcontrol":
        return DigiCamCamera(cfg)
    if choice == "auto":
        # Mac: a Canon over USB if one is connected, otherwise the built-in webcam.
        if shutil.which("gphoto2") and _gphoto2_sees_camera():
            choice = "gphoto2"
        elif _opencv_available():
            print("[camera] no USB camera found, using the webcam")
            choice = "webcam"
        else:
            print("[camera] no USB camera or webcam support, using the test pattern")
            return MockCamera()
    if choice == "webcam":
        return WebcamCamera(cfg)
    if choice == "gphoto2":
        return GPhoto2Camera(cfg)
    raise ValueError(f"Unknown camera backend: {choice}")
