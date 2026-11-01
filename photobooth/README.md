# Photobooth

A small photobooth: Canon over USB, your strobe, DNP printer, with a tablet as the touch screen. Two print options on 4x6: a 2x2 grid of four photos, or one full-bleed photo. Four-photo sessions also make an animated GIF. Guests can retake any shot, choose copies, reprint, and scan a QR code to download their photos from Dropbox.

The PC runs everything (camera, printer, files, uploads). The screen is a web page, shown on the PC itself and/or on a tablet on the same network. Printing works without internet; the QR code and uploads need it.

## Windows setup (once)

1. **digiCamControl** (free) must be installed. The booth uses the Canon camera file (EDSDK.dll) that digiCamControl installs, and talks to the camera directly. digiCamControl itself doesn't need to be running; the launcher closes it if it is.
2. **Python** is handled by start.bat. Canon's file is 32-bit, so on first run start.bat installs 32-bit Python 3.11 with winget. This sits alongside any other Python you have.
3. **Printer.** Install the DNP Windows driver for your model. In Windows **Printers & scanners > (your DNP) > Printing preferences**, set the paper size to **4x6 (PC size)**. The booth uses whatever the driver default is.
4. **Printer name.** In PowerShell:
   ```
   Get-Printer | Select-Object Name
   ```
   Copy the DNP's exact name into `config.json` as `"printer"`. Until you do, the booth saves prints without printing, and the corner of the start screen lists the printers it found.

5. **Dropbox.** Copy `dropbox.json` from the Mac's photobooth folder into this one (it's the Dropbox connection), or run `setup_dropbox.bat` to connect fresh.

Close Lumabooth before running the booth. Only one app can hold the camera at a time.

## Run it (Windows)

Double-click **start.bat**. The first run takes a few minutes to set up. It closes digiCamControl and EOS Utility if they're open, connects to the camera, waits for live view, then opens the booth full screen in Edge.

- **Alt+F4** closes the booth screen and stops the booth. Closing the black **Photobooth** window does the same.
- start.bat shuts down any booth still running from last time before it starts.
- If Windows SmartScreen warns about start.bat, click **More info > Run anyway**.

To try it with no camera attached, set `"camera": "mock"` in `config.json`.

## Tablet as the screen

The black Photobooth window prints the address to open, like `http://192.168.1.20:5050`.

- **First time on Windows:** when Windows asks about Python and the firewall, allow it on **Private networks**.
- **iPad:** open the address in Safari, tap Share > **Add to Home Screen**, and launch it from there so it runs full screen. Turn on **Guided Access** (Settings > Accessibility) to lock the iPad to the booth.
- **Fire tablet:** open the address in Silk, or use a kiosk browser app to lock it down.
- Set the tablet to never auto-lock and keep it plugged in.
- Set `"open_on_this_computer": false` in config.json if you don't want the booth screen on the PC too.
- The PC's address can change after a router restart. Give the PC a reserved address in your router settings so the tablet bookmark keeps working.

## Guest pages: what the QR code opens

With a tunnel running, the QR code opens a small page served by the booth itself: the GIF, the 4-up and the photos, with a **Save to Photos** button that puts all of them in the phone's camera roll in one tap. It loads in a second, with no sign-in. Dropbox still gets every session in the background and the page links to it as a fallback.

The tunnel is Cloudflare's free quick tunnel. One-time setup on the PC, in PowerShell:
```
winget install Cloudflare.cloudflared
```
start.bat then starts it automatically and prints `Guest pages (QR codes) at https://....trycloudflare.com`. If it isn't installed, or the internet is down, QR codes open the Dropbox folder as before. The address changes each time the booth starts, so QR codes from a previous run stop working after a restart; Dropbox still has those photos. Set `"tunnel": false` in config.json to turn it off, or `"public_url"` to use your own fixed tunnel address.

## Dropbox and the QR code

As soon as a session's GIF and print are made, the booth creates a Dropbox folder for it plus a share link, and the results screen shows a QR code for that link (it also shows on the done screen). The files upload in the background. If a guest retakes a photo, the updated set goes to a new folder (`<session>-retake1`, `-retake2`...) with its own QR code; earlier folders are never changed, so a code someone already scanned still works. **Start over** deletes the session and all its retake folders from Dropbox; if a guest just walks away and the screen times out, the photos stay.

If the internet is down there's no QR code on the results screen; the booth tries again when the guest hits Print, and the files upload once it's back, even after a restart. The status line in the corner of the start screen shows "Dropbox on" and how many sessions are still uploading.

**Connect Dropbox (once):** run start.bat once first, then double-click **setup_dropbox.bat** (Mac: setup_dropbox.command) and follow the steps. Choose **App folder** access when creating the Dropbox app: the booth then only has access to **Dropbox/Apps/(app name)**, which Dropbox creates for you, and can't see anything else in your Dropbox. The setup saves `dropbox.json` in the booth folder. Keep that file private.

Sessions land in Dropbox/Apps/(app name)/(session). To keep events apart, set `"dropbox_folder"` to a subfolder name, like `"Smith Wedding"`. Your Dropbox desktop app syncs everything down to your computer as usual.

A four-photo session folder holds the four full-resolution originals, the 4-up print and the GIF, named by when the session was taken: `2026-10-31_21-04-12_photo1.jpg` to `_photo4.jpg`, `_4up.jpg`, `_animation.gif`. A one-photo session holds just `..._photo.jpg`, the full-resolution original.

**Without Dropbox:** set `"save_folder"` to any folder (for example `"C:\\Users\\you\\Pictures\\Booth"`) and every finished session is copied there, with the same files as above. No QR code in that mode.

## Camera settings (Canon Rebel)

- **Wi-Fi/NFC: Disable.** USB doesn't work while it's on.
- **Image quality: JPEG** (Large/Fine). RAW+JPEG works but is slower; RAW alone won't.
- **Mode: M**, shutter 1/125 to 1/200 (your strobe's sync speed), ISO 100 to 200, aperture f/5.6 to f/8 for forgiving focus.
- **Exposure simulation: off** (live view menu). Otherwise the preview goes dark when exposure is set for the strobe.
- **Auto power off: disable.**
- Focus once on where people stand and switch the lens to **MF**. Fastest and most reliable for a booth.
- Use AC power (dummy battery) for events.

## Controls

| Key | Action |
|---|---|
| 4 / 1 | Start a four photo or one photo session |
| Space / Enter | Main action on each screen (start, print, done) |
| R | Retake (on the results screen) |
| Esc | Back to start |
| F | Full screen (if not already) |

Touch or mouse works for everything.

## Files

Every session gets a folder in `sessions\` with the full-res originals (`shot0.jpg`...), `animation.gif`, and `print.jpg`, the 1844x1240, 300 dpi file sent to the printer. `export-1`, `export-2`... hold each version exactly as it was sent out. Sessions ended with Start over stay here but are removed from Dropbox and the save folder.

## config.json

| Key | What it does |
|---|---|
| `camera` | `auto` (direct Canon on Windows, gphoto2 on Mac), `canon`, `digicamcontrol` (slow fallback), or `mock` |
| `edsdk_path` | Path to EDSDK.dll, if not in the digiCamControl folder |
| `printer` | Exact printer name. Empty = save only |
| `print_width` / `print_height` | Print size in pixels. 1844x1240 is DNP native 4x6 at 300 dpi |
| `spacing` / `background` | 4-up: border between photos in print pixels, and its color |
| `print_bleed` | Extra outer border the printer trims off (default 28). If the outer border prints wider than the inner gaps, lower it; narrower, raise it |
| `corner_radius` | Rounded corners on each 4-up photo |
| `theme` | Screen look, from `static/themes/<name>.css`. Empty = plain. Screen only; prints have no overlays |
| `start_title` / `start_subtitle` | The words on the start screen |
| `countdown` | Countdown before the first photo (and before a retake) |
| `countdown_next` | Countdown before photos 2 to 4 |
| `shot_preview_seconds` | How long each photo shows after it's taken |
| `gif_fps` / `gif_width` | GIF speed (frames per second) and size in pixels |
| `dropbox_folder` | Subfolder inside Dropbox/Apps/(app name) for this event. Empty = the app folder itself |
| `save_folder` | Local folder every finished session is copied to. Empty = off |
| `open_on_this_computer` | Also open the booth screen on the PC |
| `host` / `port` | Network address the booth listens on. `0.0.0.0` lets the tablet connect |
| `idle_timeout` | Seconds before the print/done screens reset to start |
| `max_copies` | Upper limit on the copies picker |
| `mirror_preview` | Mirror the live view (prints are never mirrored) |
| `rotate` | 0 or 180, for a camera mounted upside down (also in the admin panel) |
| `sounds` | Countdown beeps and shutter tone |
| `admin_pin` | PIN for the admin panel: tap the corner status text 5 times on the start screen |
| `liveview_fps` | Frames per second sent to each screen (default 15). Lower it if the tablet stutters |
| `digicam_url` | digiCamControl webserver address, if you changed its port |
| `print_command` | Mac only: print command template |

## Troubleshooting (Windows)

- **"No Canon camera found"** in the corner: turn the camera on, check Wi-Fi is disabled on it, close Lumabooth, and replug USB. The booth reconnects on its own.
- **"Canon SDK not usable here"** in the black window: the booth was started without start.bat, or digiCamControl isn't installed. It falls back to the slower digiCamControl webserver mode.
- **"Couldn't load Canon's camera software"**: send Claude the exact message.
- **Print is cropped, shrunk, or rotated wrong:** check the DNP driver's default paper size is 4x6. Send me a photo of the print and I'll adjust.
- **"Camera is shooting RAW only":** set image quality to JPEG on the camera.

## Mac (design and testing)

Double-click `start.command`. With `"camera": "auto"`, the Mac uses a Canon over USB if gphoto2 is installed and sees one, otherwise the built-in webcam. The first launch asks for camera access for Terminal; allow it, then quit and relaunch.

- `"webcam_index": 1` picks a different camera (an iPhone through Continuity Camera sometimes takes slot 0).
- Webcam photos go through the same layouts, retakes and print screens as the Canon, so what you design here looks the same on the PC.
- To print from the Mac, put the name from `lpstat -p` in `"printer"`; leave it as is to just save.
