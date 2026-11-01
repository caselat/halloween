"""Direct Canon camera control on Windows through Canon's EDSDK.dll.

Uses the EDSDK.dll that digiCamControl installs (32-bit, so this needs 32-bit
Python). Function signatures match EDSDK 13.x, where sizes are 64-bit, taken
from digiCamControl's own bindings.

Everything that touches the SDK runs on one dedicated thread, which also pumps
Windows messages so the SDK can deliver "photo ready" events.
"""

import ctypes
import os
import queue
import struct
import sys
import threading
import time
from pathlib import Path

from camera import BaseCamera

# ---- EDSDK constants ----
ERR_OK = 0x0
ERR_DEVICE_BUSY = 0x81
ERR_OBJECT_NOTREADY = 0xA102

PROP_SAVE_TO = 0x0B
SAVE_TO_HOST = 2
PROP_EVF_OUTPUT_DEVICE = 0x500
PROP_EVF_MODE = 0x501
EVF_OUTPUT_TFT = 1
EVF_OUTPUT_PC = 2

CMD_TAKE_PICTURE = 0x0
CMD_PRESS_SHUTTER = 0x4
SHUTTER_OFF = 0x0
SHUTTER_COMPLETELY_NON_AF = 0x10003

OBJECT_EVENT_ALL = 0x200
OBJECT_EVENT_DIRITEM_REQUEST_TRANSFER = 0x208
STATE_EVENT_ALL = 0x300
STATE_EVENT_SHUTDOWN = 0x301

c_u32, c_i32, c_u64, c_vp = ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint64, ctypes.c_void_p


class EdsCapacity(ctypes.Structure):
    _fields_ = [("numberOfFreeClusters", c_i32), ("bytesPerSector", c_i32), ("reset", c_i32)]


class EdsDirectoryItemInfo(ctypes.Structure):
    _fields_ = [("size", c_u64), ("isFolder", c_i32), ("groupID", c_u32), ("option", c_u32),
                ("szFileName", ctypes.c_char * 256), ("format", c_u32), ("dateTime", c_u32)]


if sys.platform == "win32":
    _FUNCTYPE = ctypes.WINFUNCTYPE  # EDSDK uses stdcall on Windows
else:
    _FUNCTYPE = ctypes.CFUNCTYPE    # only used by the test harness

ObjectHandler = _FUNCTYPE(c_u32, c_u32, c_vp, c_vp)
StateHandler = _FUNCTYPE(c_u32, c_u32, c_u32, c_vp)


class CanonError(RuntimeError):
    pass


class NoCamera(RuntimeError):
    pass


def find_edsdk(cfg):
    cands = [cfg.get("edsdk_path")]
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if base:
            cands.append(Path(base) / "digiCamControl" / "EDSDK.dll")
    cands.append(Path(__file__).resolve().parent / "edsdk" / "EDSDK.dll")
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    return None


def python_is_32bit():
    return struct.calcsize("P") == 4


def load_edsdk(dll_path):
    """Load the DLL and declare the functions we use."""
    os.add_dll_directory(str(Path(dll_path).parent))  # EDSDK.dll loads EdsImage.dll beside it
    dll = ctypes.WinDLL(str(dll_path))
    P = ctypes.POINTER
    sigs = {
        "EdsInitializeSDK": [],
        "EdsTerminateSDK": [],
        "EdsGetCameraList": [P(c_vp)],
        "EdsGetChildCount": [c_vp, P(c_i32)],
        "EdsGetChildAtIndex": [c_vp, c_i32, P(c_vp)],
        "EdsRelease": [c_vp],
        "EdsOpenSession": [c_vp],
        "EdsCloseSession": [c_vp],
        "EdsGetPropertyData": [c_vp, c_u32, c_i32, c_i32, c_vp],
        "EdsSetPropertyData": [c_vp, c_u32, c_i32, c_i32, c_vp],
        "EdsSendCommand": [c_vp, c_u32, c_i32],
        "EdsSetCapacity": [c_vp, EdsCapacity],
        "EdsGetDirectoryItemInfo": [c_vp, P(EdsDirectoryItemInfo)],
        "EdsDownload": [c_vp, c_u64, c_vp],
        "EdsDownloadComplete": [c_vp],
        "EdsDownloadCancel": [c_vp],
        "EdsCreateMemoryStream": [c_u64, P(c_vp)],
        "EdsGetPointer": [c_vp, P(c_vp)],
        "EdsGetLength": [c_vp, P(c_u64)],
        "EdsCreateEvfImageRef": [c_vp, P(c_vp)],
        "EdsDownloadEvfImage": [c_vp, c_vp],
        "EdsSetObjectEventHandler": [c_vp, c_u32, ObjectHandler, c_vp],
        "EdsSetCameraStateEventHandler": [c_vp, c_u32, StateHandler, c_vp],
    }
    for name, args in sigs.items():
        f = getattr(dll, name)
        f.restype = c_u32
        f.argtypes = args
    try:
        dll.EdsGetEvent.restype = c_u32
        dll.EdsGetEvent.argtypes = []
    except AttributeError:
        pass
    return dll


class CanonSdkCamera(BaseCamera):
    name = "canon"

    def __init__(self, cfg, sdk=None):
        super().__init__()
        self._sdk = sdk
        self._dll_path = None
        if sdk is None:
            self._dll_path = find_edsdk(cfg)
            if not self._dll_path:
                raise RuntimeError("Canon EDSDK.dll not found. Install digiCamControl.")
            if not python_is_32bit():
                raise RuntimeError("The Canon SDK is 32-bit. Run the booth with start.bat, "
                                   "which uses 32-bit Python.")
        self._jobs = queue.Queue()
        self._cam = None
        self._lost = False
        self._dl = None
        self._obj_cb = ObjectHandler(self._on_object)
        self._state_cb = StateHandler(self._on_state)
        threading.Thread(target=self._thread, daemon=True, name="canon").start()

    # ---------- public ----------
    def capture(self, dest_stem: Path) -> Path:
        with self._capture_lock:
            job = {"stem": Path(dest_stem), "done": threading.Event(), "path": None, "error": None}
            self._jobs.put(job)
            if not job["done"].wait(45):
                raise RuntimeError("The camera didn't respond.")
            if job["error"]:
                raise RuntimeError(job["error"])
            return job["path"]

    # ---------- helpers ----------
    def _check(self, err, what):
        if err != ERR_OK:
            raise CanonError(f"{what} failed (Canon error 0x{err:X})")

    def _set_int(self, prop, value):
        v = c_u32(value)
        return self._sdk.EdsSetPropertyData(self._cam, prop, 0, 4, ctypes.addressof(v))

    def _get_int(self, prop):
        v = c_u32(0)
        err = self._sdk.EdsGetPropertyData(self._cam, prop, 0, 4, ctypes.addressof(v))
        return v.value if err == ERR_OK else None

    def _pump(self):
        if sys.platform == "win32":
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            msg = wintypes.MSG()
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        get_event = getattr(self._sdk, "EdsGetEvent", None)
        if get_event is not None:
            try:
                get_event()
            except Exception:
                pass

    def _fail_queued(self, message):
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            job["error"] = message
            job["done"].set()

    # ---------- camera thread ----------
    def _thread(self):
        if sys.platform == "win32":
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # apartment threaded, as the SDK expects
        try:
            if self._sdk is None:
                self._sdk = load_edsdk(self._dll_path)
            self._check(self._sdk.EdsInitializeSDK(), "Starting the Canon SDK")
        except Exception as e:
            self.last_error = f"Couldn't load Canon's camera software: {e}"
            while self._running:  # keep failing captures quickly instead of hanging
                self._fail_queued(self.last_error)
                time.sleep(0.5)
            return

        evf_misses, evf_errors, last_restart = 0, 0, time.time()
        last_frame = 0.0
        while self._running:
            try:
                if self._cam is None:
                    self._connect()
                    evf_misses, evf_errors, last_restart = 0, 0, time.time()
                self._pump()
                if self._lost:
                    raise NoCamera("Camera disconnected. Check it's on and plugged in.")
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    job = None
                if job:
                    self._do_capture(job)
                    evf_misses, last_restart = 0, time.time()
                    continue

                wait = 1 / 30 - (time.time() - last_frame)  # cap live view at 30 fps
                if wait > 0:
                    time.sleep(wait)
                frame, err = self._grab_evf()
                if frame:
                    self._publish(frame)
                    last_frame = time.time()
                    evf_misses = evf_errors = 0
                    self.last_error = None
                    continue
                evf_misses += 1
                if err not in (ERR_OK, ERR_OBJECT_NOTREADY, ERR_DEVICE_BUSY):
                    evf_errors += 1
                    if evf_errors > 40:
                        raise CanonError(f"Live view keeps failing (Canon error 0x{err:X})")
                # no frames for 3 seconds: ask the camera to send live view to the PC again
                if evf_misses > 60 and time.time() - last_restart > 3:
                    self._start_liveview()
                    last_restart, evf_misses = time.time(), 0
                    self.last_error = "Waiting for live view..."
                time.sleep(0.03)
            except NoCamera as e:
                self.last_error = str(e)
                self._disconnect()
                self._fail_queued(self.last_error)
                time.sleep(2)
            except Exception as e:
                self.last_error = str(e)
                self._disconnect()
                self._fail_queued(f"Camera problem: {e}")
                time.sleep(2)
        self._disconnect()

    def _connect(self):
        sdk = self._sdk
        lst = c_vp()
        self._check(sdk.EdsGetCameraList(ctypes.byref(lst)), "Listing cameras")
        try:
            n = c_i32(0)
            self._check(sdk.EdsGetChildCount(lst, ctypes.byref(n)), "Counting cameras")
            if n.value < 1:
                raise NoCamera("No Canon camera found. Turn it on, and close digiCamControl, "
                               "EOS Utility and Lumabooth.")
            cam = c_vp()
            self._check(sdk.EdsGetChildAtIndex(lst, 0, ctypes.byref(cam)), "Opening camera")
        finally:
            sdk.EdsRelease(lst)

        self._lost = False
        sdk.EdsSetObjectEventHandler(cam, OBJECT_EVENT_ALL, self._obj_cb, None)
        sdk.EdsSetCameraStateEventHandler(cam, STATE_EVENT_ALL, self._state_cb, None)
        err = sdk.EdsOpenSession(cam)
        if err != ERR_OK:
            sdk.EdsRelease(cam)
            raise NoCamera(f"Couldn't connect to the camera (Canon error 0x{err:X}). "
                           "Is another app using it?")
        self._cam = cam
        # photos go to this computer, not the SD card
        self._check(self._set_int(PROP_SAVE_TO, SAVE_TO_HOST), "Setting save location")
        sdk.EdsSetCapacity(cam, EdsCapacity(0x7FFFFFFF, 0x1000, 1))
        self._start_liveview()
        self.last_error = "Waiting for live view..."

    def _start_liveview(self):
        self._set_int(PROP_EVF_MODE, 1)
        dev = self._get_int(PROP_EVF_OUTPUT_DEVICE) or 0
        self._set_int(PROP_EVF_OUTPUT_DEVICE, dev | EVF_OUTPUT_PC)

    def _disconnect(self):
        cam, self._cam = self._cam, None
        if cam:
            try:
                v = c_u32(EVF_OUTPUT_TFT)
                self._sdk.EdsSetPropertyData(cam, PROP_EVF_OUTPUT_DEVICE, 0, 4, ctypes.addressof(v))
                self._sdk.EdsCloseSession(cam)
                self._sdk.EdsRelease(cam)
            except Exception:
                pass

    def _read_stream(self, stream):
        ptr, ln = c_vp(), c_u64(0)
        self._sdk.EdsGetPointer(stream, ctypes.byref(ptr))
        self._sdk.EdsGetLength(stream, ctypes.byref(ln))
        if not ptr.value or not ln.value:
            return None
        return ctypes.string_at(ptr.value, ln.value)

    def _grab_evf(self):
        sdk = self._sdk
        stream, evf = c_vp(), c_vp()
        self._check(sdk.EdsCreateMemoryStream(0, ctypes.byref(stream)), "Live view buffer")
        try:
            self._check(sdk.EdsCreateEvfImageRef(stream, ctypes.byref(evf)), "Live view image")
            err = sdk.EdsDownloadEvfImage(self._cam, evf)
            if err != ERR_OK:
                return None, err
            data = self._read_stream(stream)
            return (data if data and data[:2] == b"\xff\xd8" else None), ERR_OK
        finally:
            if evf.value:
                sdk.EdsRelease(evf)
            sdk.EdsRelease(stream)

    def _do_capture(self, job):
        sdk, cam = self._sdk, self._cam
        self._dl = {"stem": job["stem"], "path": None, "error": None, "raw_at": None}
        try:
            # same sequence digiCamControl uses: release, full press without autofocus, release
            err = ERR_DEVICE_BUSY
            for _ in range(10):
                sdk.EdsSendCommand(cam, CMD_PRESS_SHUTTER, SHUTTER_OFF)
                err = sdk.EdsSendCommand(cam, CMD_PRESS_SHUTTER, SHUTTER_COMPLETELY_NON_AF)
                sdk.EdsSendCommand(cam, CMD_PRESS_SHUTTER, SHUTTER_OFF)
                if err != ERR_DEVICE_BUSY:
                    break
                self._pump()
                time.sleep(0.2)
            if err != ERR_OK:
                err = sdk.EdsSendCommand(cam, CMD_TAKE_PICTURE, 0)
            if err == 0x8D01:
                raise CanonError("Camera couldn't focus. Switch the lens to MF.")
            self._check(err, "Taking the photo")

            deadline = time.time() + 25
            while time.time() < deadline:
                self._pump()
                dl = self._dl
                if dl["path"] or dl["error"]:
                    break
                if dl["raw_at"] and time.time() - dl["raw_at"] > 4:
                    dl["error"] = "Camera is shooting RAW only. Set image quality to JPEG."
                    break
                time.sleep(0.01)
            if self._dl["path"]:
                job["path"] = self._dl["path"]
            else:
                job["error"] = self._dl["error"] or "The photo never arrived from the camera."
        except Exception as e:
            job["error"] = str(e)
        finally:
            self._dl = None
            job["done"].set()

    # ---------- SDK callbacks (run on the camera thread while pumping) ----------
    def _on_object(self, event, ref, ctx):
        try:
            if event == OBJECT_EVENT_DIRITEM_REQUEST_TRANSFER and ref:
                self._download(ref)
        except Exception as e:
            if self._dl is not None:
                self._dl["error"] = str(e)
        finally:
            if ref:
                self._sdk.EdsRelease(ref)
        return 0

    def _on_state(self, event, param, ctx):
        if event == STATE_EVENT_SHUTDOWN:
            self._lost = True
        return 0

    def _download(self, ref):
        sdk, dl = self._sdk, self._dl
        info = EdsDirectoryItemInfo()
        self._check(sdk.EdsGetDirectoryItemInfo(ref, ctypes.byref(info)), "Reading photo info")
        name = info.szFileName.decode("ascii", "ignore")
        if dl is None or not name.lower().endswith((".jpg", ".jpeg")):
            sdk.EdsDownloadCancel(ref)
            if dl is not None and dl["raw_at"] is None:
                dl["raw_at"] = time.time()
            return
        stream = c_vp()
        self._check(sdk.EdsCreateMemoryStream(info.size, ctypes.byref(stream)), "Photo buffer")
        try:
            self._check(sdk.EdsDownload(ref, info.size, stream), "Downloading photo")
            self._check(sdk.EdsDownloadComplete(ref), "Finishing download")
            data = self._read_stream(stream)
        finally:
            sdk.EdsRelease(stream)
        if not data:
            raise CanonError("Downloaded photo was empty")
        out = dl["stem"].with_suffix(".jpg")
        out.write_bytes(data)
        dl["path"] = out
