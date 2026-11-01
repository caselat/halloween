# Party GIF Cycler

Five keywords. One Start button. Endless GIFs. Three loops each. Fullscreen.

One file, no build step, no dependencies. Open it in Chrome and go.

## Run it (macOS)

Option A, no Terminal:

1. Double-click `index.html`, or drag it onto Chrome.

Option B, local server (only needed if Chrome ever complains about `file://`):

```bash
cd party_gif
python3 -m http.server 8080
```

Then open http://localhost:8080/index.html

## GIPHY API key

1. Go to https://developers.giphy.com/dashboard/ and sign in (free account).
2. Create App, choose **API** (not SDK), give it any name.
3. Copy the key and paste it into the app. Leave "Remember key on this computer" on and you won't have to paste it again.

The key lives in Chrome's local storage for this file only. Fine for a laptop you control; don't host this file publicly with a key baked in.

## Test it

1. Enter Halloween, Skulls, Skeletons, Horror, Ghosts. Paste the key. Press START.
2. A GIF appears on black within a few seconds. Move the mouse: the bottom overlay shows `loop 1/3`, then `2/3`, then `3/3`, then the next GIF crossfades in.
3. Press Space to pause mid-loop, Space again: it resumes from the same spot with the same loop count.
4. Right arrow skips, left arrow goes back. Both restart that GIF at loop 1.
5. Press F for fullscreen. The cursor and overlay vanish after a couple of seconds. Esc exits.
6. Leave it running. New GIFs keep arriving from GIPHY in the background; if GIPHY goes away, it replays what it already has and retries every 30 seconds.
7. Click Settings in the overlay, change a keyword, press START. New stream, no reload.

Try "Start fullscreen" on the setup screen when the laptop is already on the TV.

## How the three loops work

GIPHY serves MP4 versions of nearly every GIF. The app plays those in a muted `<video>` with no `loop` attribute, counts `ended` events, restarts twice, then advances. If a GIF has no MP4, the app reads the frame delays out of the GIF file itself and runs a timer for three loops. That path is close but not frame-exact.
