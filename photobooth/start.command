#!/bin/bash
# Double-click on a Mac to launch the booth. Quitting the booth window (Cmd+Q) stops everything.
cd "$(dirname "$0")"

# Stop any booth that's still running, so the new one always starts fresh.
lsof -ti:5050 | xargs kill 2>/dev/null; sleep 1

[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -q -r requirements.txt

python3 server.py &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT   # whatever happens, the server stops with this window
sleep 2

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ -x "$CHROME" ]; then
  # Runs as its own Chrome instance and waits here until you Cmd+Q it.
  "$CHROME" --kiosk --app=http://127.0.0.1:5050 --user-data-dir="$PWD/.chrome-booth" --no-first-run >/dev/null 2>&1
  echo "Booth window closed. Stopping the booth."
else
  open http://127.0.0.1:5050
  echo "Press Ctrl+C here to stop the booth."
  wait $SERVER
fi
