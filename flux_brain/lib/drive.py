"""Google Drive uploads (the Drive module). Attachment originals go to a Drive folder, not into git, so the
repository and the phone copy stay light; the note links the file and its extracted text lives in the repository.

Token: `$FLUX_HOME/drive-token.json`, written once by `flux-drive-auth` (see below) from a Google Cloud OAuth
"Desktop app" client. The file is READ only: an access token is refreshed in memory for each run and the file is
never written back. Uploads are idempotent on file name (a re-run after a crash finds the existing file and returns
its link instead of creating a duplicate).

Works with a shared drive (`[drive] drive_id` set) and with a folder in "My Drive" (`drive_id` empty).
"""
import json
import pathlib
import sys

from ..config import CFG

__all__ = ["DRIVE_FOLDER", "DRIVE_DRIVE_ID", "DRIVE_TOKEN", "Drive", "auth_main"]

DRIVE_FOLDER = CFG.drive_folder_id   # flux.toml [drive] folder_id: where attachment originals go
DRIVE_DRIVE_ID = CFG.drive_id        # flux.toml [drive] drive_id (shared drive) or "" for My Drive
DRIVE_TOKEN = CFG.drive_token_file   # OAuth token written by flux-drive-auth (INSTALL.md)
SCOPE = "https://www.googleapis.com/auth/drive.file"   # only files this app created or was handed; never the whole Drive


class Drive:
    def __init__(self, session, token_path=DRIVE_TOKEN, folder=DRIVE_FOLDER, drive_id=DRIVE_DRIVE_ID):
        self.s, self.token_path, self.folder, self.drive_id = session, token_path, folder, drive_id
        self._auth = None
        if not folder:
            raise SystemExit("flux: Drive module is on but [drive] folder_id is empty in flux.toml")

    def headers(self):
        if not self._auth:
            try:
                with open(self.token_path) as f:
                    t = json.load(f)
            except FileNotFoundError:
                raise SystemExit(f"flux: Drive module is on but {self.token_path} is missing: run flux-drive-auth (INSTALL.md)")
            r = self.s.post(t.get("token_uri", "https://oauth2.googleapis.com/token"), timeout=30, data={
                "client_id": t["client_id"], "client_secret": t["client_secret"],
                "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
            if not r.ok:  # e.g. invalid_grant after the consent was revoked: run flux-drive-auth again
                raise RuntimeError(f"Drive token refresh failed: HTTP {r.status_code} (run flux-drive-auth again if it persists)")
            self._auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        return self._auth

    def _corpus(self):
        """Query scope: a shared drive needs corpora + driveId; My Drive needs neither."""
        if self.drive_id:
            return {"corpora": "drive", "driveId": self.drive_id, "includeItemsFromAllDrives": "true"}
        return {}

    def upload(self, name, data: bytes, mime):
        """Upload into the folder; return the file's webViewLink. Existing name -> its link, no duplicate."""
        h = self.headers()
        common = {"supportsAllDrives": "true"}  # harmless on My Drive; required on a shared drive (else 404)
        q = f"name = '{name}' and '{self.folder}' in parents and trashed = false"
        r = self.s.get("https://www.googleapis.com/drive/v3/files", headers=h, timeout=30, params={
            **common, **self._corpus(), "q": q, "fields": "files(id,webViewLink)"})
        r.raise_for_status()
        if r.json()["files"]:
            return r.json()["files"][0]["webViewLink"]
        # Resumable upload: any size up to the attachment cap in two requests.
        init = self.s.post("https://www.googleapis.com/upload/drive/v3/files", headers={
            **h, "X-Upload-Content-Type": mime}, timeout=30,
            params={**common, "uploadType": "resumable", "fields": "id,webViewLink"},
            json={"name": name, "parents": [self.folder]})
        init.raise_for_status()
        up = self.s.put(init.headers["Location"], headers={"Content-Type": mime}, data=data, timeout=300)
        up.raise_for_status()
        return up.json()["webViewLink"]


def auth_main(argv=None):
    """`flux-drive-auth <client_secret.json>`: one-time consent that writes $FLUX_HOME/drive-token.json.

    Needs a browser, so run it on a laptop (`pip install 'flux-brain[drive]'`, then copy the token file to the
    server's FLUX_HOME, mode 600) or on the server with SSH port forwarding of the port it prints. The client
    secret comes from Google Cloud Console: APIs & Services > Credentials > OAuth client ID > Desktop app, with
    the Drive API enabled on the project. The token grants the drive.file scope only."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit("usage: flux-drive-auth <client_secret.json>")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit("flux-drive-auth needs the drive extra: pip install 'flux-brain[drive]'")
    flow = InstalledAppFlow.from_client_secrets_file(argv[0], scopes=[SCOPE])
    creds = flow.run_local_server(port=0, open_browser=True)
    out = pathlib.Path(DRIVE_TOKEN)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "client_id": creds.client_id, "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token, "token_uri": creds.token_uri, "scopes": list(creds.scopes or [SCOPE]),
    }, indent=2))
    out.chmod(0o600)
    print(f"wrote {out} (mode 600). Copy it to the server's FLUX_HOME if this is not the server, then set "
          f"[modules] drive = true and [drive] folder_id in flux.toml.")
