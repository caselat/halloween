"""Windows launcher: opens digiCamControl, starts the booth, then opens Edge full screen.
Run through start.bat."""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8")) if (ROOT / "config.json").exists() else {}
PORT = cfg.get("port", 5050)
URL = f"http://127.0.0.1:{PORT}"  # not "localhost": avoids a slow IPv6 attempt on Windows


def is_running(exe):
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}"],
                         capture_output=True, text=True).stdout
    return exe.lower() in out.lower()


def find(*candidates):
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


def status():
    try:
        with urllib.request.urlopen(URL + "/api/status", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
pf = os.environ.get("ProgramFiles", r"C:\Program Files")

sys.path.insert(0, str(ROOT))
from canon_sdk import find_edsdk, python_is_32bit  # noqa: E402

backend = cfg.get("camera", "auto")
if backend == "auto":
    backend = "canon" if (find_edsdk(cfg) and python_is_32bit()) else "digicamcontrol"

if backend == "canon":
    # The booth talks to the camera directly; any app holding it would block us.
    for exe in ("CameraControl.exe", "EOS Utility 3.exe", "EOS Utility.exe"):
        if is_running(exe):
            print(f"Closing {exe} so the booth can use the camera...")
            subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True)
            time.sleep(2)
elif backend == "digicamcontrol" and not is_running("CameraControl.exe"):
    dcc = find(Path(pf86) / "digiCamControl" / "CameraControl.exe",
               Path(pf) / "digiCamControl" / "CameraControl.exe")
    if dcc:
        print("Opening digiCamControl...")
        subprocess.Popen([dcc], cwd=str(Path(dcc).parent))
        time.sleep(6)
    else:
        print("digiCamControl isn't installed. Get it from digicamcontrol.com (see README).")

# Stop any booth that's still running from last time, so this one starts fresh.
out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True).stdout
for line in out.splitlines():
    parts = line.split()
    if len(parts) >= 5 and parts[1].endswith(f":{PORT}") and parts[3] == "LISTENING":
        subprocess.run(["taskkill", "/PID", parts[4], "/F"], capture_output=True)
        time.sleep(1)

# Cloudflare quick tunnel: gives guests' phones a public address for the photo pages.
# Free, no account. Installed with: winget install Cloudflare.cloudflared
tunnel_file = ROOT / "tunnel.json"
tunnel_file.unlink(missing_ok=True)
tunnel = None
cloudflared = shutil.which("cloudflared") or find(
    Path(pf) / "cloudflared" / "cloudflared.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "cloudflared.exe")
if cloudflared and cfg.get("tunnel", True):
    import re, threading
    tunnel = subprocess.Popen([cloudflared, "tunnel", "--url", URL, "--no-autoupdate"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def watch_tunnel():
        for line in tunnel.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m:
                tunnel_file.write_text(json.dumps({"url": m.group(0)}), encoding="utf-8")
                print(f"Guest pages (QR codes) at {m.group(0)}")
    threading.Thread(target=watch_tunnel, daemon=True).start()
else:
    print("No cloudflared: QR codes will open Dropbox. To get fast guest pages, run in PowerShell:")
    print("    winget install Cloudflare.cloudflared")

print("Starting booth server...")
server = subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=str(ROOT))

# Wait for live view so digiCamControl's own live view window opens before the booth
# goes full screen on top of it.
print("Waiting for the camera", end="", flush=True)
deadline = time.time() + 45
while time.time() < deadline and server.poll() is None:
    s = status()
    if s and s["camera"].get("live"):
        break
    print(".", end="", flush=True)
    time.sleep(1)
print()
s = status()
if s and not s["camera"].get("live"):
    print("Camera not ready yet:", s["camera"].get("error") or "no live view")
    print("Opening the booth anyway. The corner of the start screen shows camera status.")

s = status()
if s:
    print("\nOn the tablet, open one of these (the one on the network the tablet is on):")
    for a in s.get("addresses", [s["address"]]):
        print("    " + a)
    print("(first time on Windows: allow Python through the firewall on Private networks)\n")

if cfg.get("open_on_this_computer", True):
    browser = find(shutil.which("msedge"),
                   Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                   Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    if browser:
        # Its own Edge window; when it closes (Alt+F4), the booth stops too.
        edge = subprocess.Popen([browser, "--kiosk", URL, "--edge-kiosk-type=fullscreen", "--no-first-run",
                                 f"--user-data-dir={ROOT / '.edge-booth'}"])
        print("Booth is full screen on this PC. Alt+F4 closes it and stops the booth.")
        try:
            while edge.poll() is None and server.poll() is None:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        print("Booth window closed. Stopping the booth.")
        server.terminate()
        if tunnel: tunnel.terminate()
        tunnel_file.unlink(missing_ok=True)
        raise SystemExit
    os.startfile(URL)
print("Close this window to stop the booth.")

try:
    server.wait()
except KeyboardInterrupt:
    server.terminate()
if tunnel: tunnel.terminate()
tunnel_file.unlink(missing_ok=True)
