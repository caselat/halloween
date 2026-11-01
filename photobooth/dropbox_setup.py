"""One-time Dropbox connection. Run with setup_dropbox.bat (Windows) or setup_dropbox.command (Mac)."""

import json
import webbrowser
from pathlib import Path

from export import Dropbox, DropboxError, remote_root

ROOT = Path(__file__).resolve().parent
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

print("""
Connect the photobooth to Dropbox
=================================
1. Go to https://www.dropbox.com/developers/apps and click "Create app".
2. Choose "Scoped access", then "App folder", and name it (e.g. "Photobooth").
   The booth can only use the folder Dropbox/Apps/<that name>. Nothing else in
   your Dropbox is reachable.
3. On the app's Permissions tab, tick:
     files.content.write   files.content.read   sharing.write   sharing.read
   and click Submit at the bottom. (Do this before step 5: permissions are
   fixed when you approve the app.)
4. On the Settings tab, copy the App key and App secret.
""")
key = input("App key: ").strip()
secret = input("App secret: ").strip()

url = ("https://www.dropbox.com/oauth2/authorize?client_id=" + key
       + "&response_type=code&token_access_type=offline")
print("\nA browser page will open. Click Continue, then Allow, and copy the code it shows.")
print("If it doesn't open, go to:\n" + url + "\n")
webbrowser.open(url)
code = input("Code: ").strip()

tokens = Dropbox.exchange_code(key, secret, code)
creds = {"app_key": key, "app_secret": secret, "refresh_token": tokens["refresh_token"]}

# Prove uploading and sharing both work, then clean up.
dbx = Dropbox(creds)
test = remote_root(cfg) + "/_booth_connection_test"
try:
    dbx.create_folder(test)
    dbx.share_link(test)
    dbx.rpc("files/delete_v2", {"path": test})
except DropboxError as e:
    print(f"\nDropbox said: {e}")
    if "scope" in str(e).lower():
        print("A permission is missing. Tick all four on the Permissions tab, click Submit,")
        print("then run this setup again.")
    input("\nPress Enter to close.")
    raise SystemExit(1)

(ROOT / "dropbox.json").write_text(json.dumps(creds, indent=2))
print("\nConnected.")
print("Sessions upload to Dropbox/Apps/<your app name>" + (remote_root(cfg) or ""))
print("To keep events apart, set \"dropbox_folder\" in config.json, e.g. \"Smith Wedding\".")
print("Saved to dropbox.json. Keep that file private: it can write to that Dropbox folder.")
input("\nPress Enter to close.")
