"""Google Drive uploads for the relays (flux_brain.lib, 2026-09-18). Attachments and .eml originals go to the shared drive
"Vault" > folder "Claude", not into git, so the repo and the phone copy stay light.

The drive MCP's OAuth token file is READ only: an access token is refreshed in memory and the file is never written
back, so the MCP's own refresh cycle is never disturbed. Uploads are idempotent on file name (a re-run after a crash
finds the existing file and returns its link instead of creating a duplicate).
"""
import json
from ..config import CFG

__all__ = ["DRIVE_FOLDER", "DRIVE_DRIVE_ID", "DRIVE_TOKEN", "Drive"]

DRIVE_FOLDER = CFG.drive_folder_id   # flux.toml [drive] folder_id: where attachment originals go
DRIVE_DRIVE_ID = CFG.drive_id        # flux.toml [drive] drive_id (shared drive) or "" for My Drive
DRIVE_TOKEN = CFG.drive_token_file  # OAuth token written by the one-time consent run (INSTALL.md)


class Drive:
    def __init__(self, session, token_path=DRIVE_TOKEN, folder=DRIVE_FOLDER, drive_id=DRIVE_DRIVE_ID):
        self.s, self.token_path, self.folder, self.drive_id = session, token_path, folder, drive_id
        self._auth = None

    def headers(self):
        if not self._auth:
            with open(self.token_path) as f:
                t = json.load(f)
            r = self.s.post(t.get("token_uri", "https://oauth2.googleapis.com/token"), timeout=30, data={
                "client_id": t["client_id"], "client_secret": t["client_secret"],
                "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
            if not r.ok:  # e.g. invalid_grant: the MCP token needs its re-bootstrap (google-sheets-mcp memory)
                raise RuntimeError(f"Drive token refresh failed: HTTP {r.status_code}")
            self._auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        return self._auth

    def upload(self, name, data: bytes, mime):
        """Upload into the folder; return the file's webViewLink. Existing name -> its link, no duplicate."""
        h = self.headers()
        common = {"supportsAllDrives": "true"}  # without it Drive answers 404 (drive-python-supports-all-drives)
        q = f"name = '{name}' and '{self.folder}' in parents and trashed = false"
        r = self.s.get("https://www.googleapis.com/drive/v3/files", headers=h, timeout=30, params={
            **common, "q": q, "corpora": "drive", "driveId": self.drive_id,
            "includeItemsFromAllDrives": "true", "fields": "files(id,webViewLink)"})
        r.raise_for_status()
        if r.json()["files"]:
            return r.json()["files"][0]["webViewLink"]
        # Resumable upload: any size up to the relays' 20 MB cap in two requests.
        init = self.s.post("https://www.googleapis.com/upload/drive/v3/files", headers={
            **h, "X-Upload-Content-Type": mime}, timeout=30,
            params={**common, "uploadType": "resumable", "fields": "id,webViewLink"},
            json={"name": name, "parents": [self.folder]})
        init.raise_for_status()
        up = self.s.put(init.headers["Location"], headers={"Content-Type": mime}, data=data, timeout=300)
        up.raise_for_status()
        return up.json()["webViewLink"]
