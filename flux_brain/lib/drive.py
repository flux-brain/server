"""Google Drive uploads (the Drive module). Attachment originals go to a Drive folder, not into git, so the
repository and the phone copy stay light; the note links the file and its extracted text lives in the repository.

Token: `$FLUX_HOME/drive-token.json`, written once by `flux-drive-auth` (flux_brain.lib.google, shared with the Gmail
and Tasks modules since 2026-09-24). Uploads are idempotent on file name (a re-run after a crash finds the existing file and returns
its link instead of creating a duplicate).

Works with a shared drive (`[drive] drive_id` set) and with a folder in "My Drive" (`drive_id` empty).
"""

from ..config import CFG
from .google import GoogleToken, consent_main

__all__ = ["Drive", "auth_main"]

SCOPE = "https://www.googleapis.com/auth/drive.file"   # only files this app created or was handed; never the whole Drive


class Drive:
    def __init__(self, session, token_path=None, folder=None, drive_id=None):
        # None = the flux.toml [drive] values (token_file, folder_id, drive_id), read here and not at import; "" is a
        # real value for drive_id (My Drive), so the test is `is None`.
        self.s = session
        self.token_path = CFG.drive_token_file if token_path is None else token_path
        self.folder = CFG.drive_folder_id if folder is None else folder
        self.drive_id = CFG.drive_id if drive_id is None else drive_id
        token_path, folder, drive_id = self.token_path, self.folder, self.drive_id
        self.token = GoogleToken(session, token_path, "Drive", "flux-drive-auth")
        if not folder:
            raise SystemExit("flux: Drive module is on but [drive] folder_id is empty in flux.toml")

    def headers(self):
        return self.token.headers()   # exchanged once per process, in memory (lib.google)

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
    """`flux-drive-auth <client_secret.json>`: one-time consent (scope drive.file only: files this app creates) that
    writes $FLUX_HOME/drive-token.json. Enable the Drive API on the Cloud project first; the rest is in lib.google."""
    return consent_main(argv, "flux-drive-auth", [SCOPE], CFG.drive_token_file,
                        "set [modules] drive = true and [drive] folder_id in flux.toml.")
