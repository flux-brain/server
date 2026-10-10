"""flux_brain.drivewatch: new files in chosen Google Drive folders -> inbox captures (the Drive watch module, 2026-09-28).

The owner's own documents (meeting notes Gemini writes after a call, a scan dropped into a project folder) used to reach
the vault only when he posted them. This module watches a short list of folders and files each NEW file there, with
its details and its text, like a Drive link posted in the capture channel (lib.linked writes both).

Each run (cron or a systemd timer, every 5 minutes by default, under flock):
  1. reads Drive's change feed from the page token saved last time (the first run only saves a token: no backfill);
  2. drops, cheaply and in this order: removals, trashed files, folders, files on a shared drive none of the watched
     folders lives on, My Drive files the owner does not own (`owned_only`), and only then walks the parents (cached)
     to find a watched ancestor. The feed also carries every other change in the account (sync mirrors, uploads,
     documents being edited), which is why the order matters;
  3. queues a match in `pending` and files it once it has gone `settle_s` without an edit (Gemini keeps writing for a
     few minutes after a call ends), at most `max_per_run` per run;
  4. files it: the text to `raw/attachments/<stamp>-drive-<id>-<name>.md` (unless `-text`-like: see `text`), then the
     capture `inbox/<stamp>-drive-<id>.md` (`source: drive`, `project:` when the folder names one), which starts a run.

New files only: an edited file that was filed already is not filed again (post its link to refresh it). A file that
was filed is remembered by id; the list keeps the last MAX_FILED ids.

Folder suggestions (2026-09-28, `suggest = true`): the list improves with time. While reading the feed, the module
counts the files the OWNER changed per folder (the feed says who: `lastModifyingUser.me`), whatever the folder. Every
`suggest_every_days`, the busiest folders outside the watched ones (at least `suggest_min_files` files, at most
`suggest_max`, never one in `never`) are posted in the capture channel with a ✅ button (flux_brain.lib.buttons). A
tap adds the folder to `extra_folders` in the state file, watched from the next run on; flux.toml is never edited.

Starred folders (2026-10-10, `starred = true`): the folders the owner has STARRED in Drive are watched too, with their
sub-folders. The list is read from Drive at the start of every run, so a star set on a phone starts the filing and
removing it stops it, with no file to edit on the server; `folders` may then stay empty. A file queued under a folder
that is no longer watched (the star was removed, a folder left the list) is dropped, not filed.

`never` (folder ids) also limits the FILING since 2026-10-10: a file below a `never` folder is not filed even when a
folder above it is watched. Before, the list only kept folders out of the suggestions.

Config (`flux.toml`):
    [modules] drive_watch = true
    [drive_watch]
    folders = [{ id = "<folder id>", project = "<page slug>" }, { id = "<folder id>" }]   # sub-folders included
    settle_s = 600
    owned_only = true        # My Drive files must be the owner's; shared drives rely on the folder list
    text = true              # copy the text (false = details only)
    max_per_run = 10
    starred = false          # true: also watch the folders starred in Drive (list read at every run)

Token: `[drive] token_file`, which must be able to READ those folders (drive.readonly or drive), not the drive.file
consent of flux-drive-auth. DRIVE_WATCH_DRY=1: nothing is written to GitHub, the captures are printed instead; the
state IS saved, so run a dry pass with a scratch FLUX_HOME (a copy of flux.toml, the live flux.env), never the live one.
"""
import os
import sys
from datetime import datetime, timezone

from .config import CFG
from .lib import buttons
from .lib.common import log, ops_alert
from .lib.drive import Drive
from .lib.extract import extract_text
from .lib.github import GitHub, session
from .lib.linked import details_line, text_file
from .lib.state import load_json, save_json

API = "https://www.googleapis.com/drive/v3"
FAIL_ALERT_AFTER = 3
MAX_FILED = 2000
MAX_DEPTH = 20                      # parent hops before giving up (a Drive tree is never this deep in practice)
FOLDER = "application/vnd.google-apps.folder"
CHANGE_FIELDS = ("nextPageToken,newStartPageToken,changes(fileId,removed,file(id,name,mimeType,modifiedTime,parents,"
                 "driveId,trashed,ownedByMe,lastModifyingUser(me)))")
STARRED_Q = f"starred = true and mimeType = '{FOLDER}' and trashed = false"
MAX_STARRED_PAGES = 5               # 500 starred folders: far past any real list, and a bound on one run's requests
ADD = "✅"
DRY = os.environ.get("DRIVE_WATCH_DRY") == "1"


def state_file():
    return CFG.state_file("drive-watch-state.json")


class Watcher:
    def __init__(self, drive, gh, st, folders=None):
        self.d, self.gh, self.st = drive, gh, st
        base = CFG.drive_watch_folders if folders is None else folders
        self.folders = {f["id"]: f for f in base + st.get("extra_folders", [])}
        st.setdefault("filed", [])
        st.setdefault("activity", {})       # folder id -> [file ids the owner changed there] since the last suggestion
        st.setdefault("pending", {})
        st.setdefault("parents", {})        # folder id -> [name, parent id or "", drive id or ""]

    # ---------- Drive ----------
    def get(self, path, params):
        r = self.d.s.get(f"{API}/{path}", headers=self.d.headers(), timeout=30, params=params)
        if r.status_code == 401:
            self.d.token.reset()
            r = self.d.s.get(f"{API}/{path}", headers=self.d.headers(), timeout=30, params=params)
        r.raise_for_status()
        return r.json()

    def folder_info(self, fid):
        """[name, parent, drive id] of a folder, cached in state (folders rarely move; a stale entry only delays a
        match until the cache is cleared)."""
        if fid not in self.st["parents"]:
            f = self.get(f"files/{fid}", {"supportsAllDrives": "true", "fields": "id,name,parents,driveId"})
            self.st["parents"][fid] = [f.get("name", ""), (f.get("parents") or [""])[0], f.get("driveId", "")]
        return self.st["parents"][fid]

    def add_starred(self):
        """Add the folders the owner starred in Drive to the watched set, for this run only (nothing is kept in the
        state: the star is the switch). A folder already listed in flux.toml keeps its entry, hence its project.
        Returns the number added."""
        n, token = 0, None
        for _ in range(MAX_STARRED_PAGES):
            params = {"q": STARRED_Q, "corpora": "allDrives", "supportsAllDrives": "true",
                      "includeItemsFromAllDrives": "true", "pageSize": 100,
                      "fields": "nextPageToken,files(id,name,parents,driveId)"}
            if token:
                params["pageToken"] = token
            page = self.get("files", params)
            for f in page.get("files", []):
                self.st["parents"][f["id"]] = [f.get("name", ""), (f.get("parents") or [""])[0], f.get("driveId", "")]
                if f["id"] not in self.folders:
                    self.folders[f["id"]] = {"id": f["id"], "starred": True}
                    n += 1
            token = page.get("nextPageToken")
            if not token:
                break
        return n

    def watched_drive_ids(self):
        """The drive ids the watched folders live on ("" = My Drive), so most changes are dropped without a lookup."""
        return {self.folder_info(fid)[2] for fid in self.folders}

    def watched_ancestor(self, parents):
        """The watched folder entry above a file whose parents are `parents`, or None. A folder in `never` met on the
        way up hides what is below it, even inside a watched folder (2026-10-10: a starred folder is watched whole,
        and one sub-folder of it may hold what must not be filed)."""
        cur, hops = (parents or [""])[0], 0
        while cur and hops < MAX_DEPTH:
            if cur in CFG.drive_watch_never:
                return None
            if cur in self.folders:
                return self.folders[cur]
            cur = self.folder_info(cur)[1]
            hops += 1
        return None

    # ---------- one pass ----------
    def scan(self):
        """Read the change feed and queue matches in `pending`. Returns the number queued."""
        if not self.st.get("page_token"):
            self.st["page_token"] = self.get("changes/startPageToken", {"supportsAllDrives": "true"})["startPageToken"]
            log("drive watch initialised: changes from now on, no backfill")
            return 0
        drives = self.watched_drive_ids()
        filed = set(self.st["filed"])
        queued, token = 0, self.st["page_token"]
        while token:
            page = self.get("changes", {"pageToken": token, "pageSize": 1000, "supportsAllDrives": "true",
                                        "includeItemsFromAllDrives": "true", "spaces": "drive", "fields": CHANGE_FIELDS})
            for ch in page.get("changes", []):
                f = ch.get("file") or {}
                if ch.get("removed") or not f or f.get("trashed") or f.get("mimeType") == FOLDER:
                    continue
                if CFG.drive_watch_suggest and (f.get("lastModifyingUser") or {}).get("me") and f.get("parents"):
                    seen = self.st["activity"].setdefault(f["parents"][0], [])
                    if f["id"] not in seen and len(seen) < 50 and len(self.st["activity"]) < 500:
                        seen.append(f["id"])
                if f.get("driveId", "") not in drives:
                    continue
                if not f.get("driveId") and CFG.drive_watch_owned_only and not f.get("ownedByMe"):
                    continue
                if f["id"] in filed or f["id"] in self.st["pending"]:
                    continue
                folder = self.watched_ancestor(f.get("parents"))
                if folder:
                    self.st["pending"][f["id"]] = {"folder": folder["id"], "seen": _now()}
                    queued += 1
            if page.get("newStartPageToken"):
                self.st["page_token"] = page["newStartPageToken"]
                break
            token = page.get("nextPageToken")
            self.st["page_token"] = token or self.st["page_token"]
        return queued

    def settle_and_file(self):
        """File the pending files that have gone settle_s without an edit. Returns the number filed."""
        n = 0
        for fid in list(self.st["pending"]):
            if n >= CFG.drive_watch_max_per_run:
                break
            if self.st["pending"][fid].get("folder") not in self.folders:   # star removed, folder taken off the list
                self.st["pending"].pop(fid)
                log(f"drive watch: dropped {fid}, its folder is no longer watched")
                continue
            try:
                meta = self.d.metadata(fid)
            except Exception as exc:  # noqa: BLE001
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code in (403, 404):            # gone or no longer readable: forget it
                    self.st["pending"].pop(fid)
                    continue
                raise
            if meta.get("trashed"):
                self.st["pending"].pop(fid)
                continue
            age = _now() - _ts(meta.get("modifiedTime"))
            if age < CFG.drive_watch_settle:
                continue
            self.file(meta, self.folders.get(self.st["pending"][fid]["folder"], {}))
            self.st["pending"].pop(fid)
            self.st["filed"] = (self.st["filed"] + [fid])[-MAX_FILED:]
            n += 1
        return n

    def file(self, meta, folder):
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
        fid = meta["id"]
        line = details_line(meta, meta.get("webViewLink") or "")
        if CFG.drive_watch_text:
            try:
                path, body, _kb, suffix = text_file(
                    meta, self.file_bytes, extract_text, CFG.max_attachment_bytes,
                    f"raw/attachments/{stamp}-drive-{fid}", fid, f"{now:%Y-%m-%dT%H:%MZ}",
                    "Text of a file in a watched Google Drive folder; the original stays in Drive and may have changed since.")
            except Exception as exc:  # noqa: BLE001 - the capture still lands, with the reason
                path, body, suffix = None, None, f", text NOT fetched ({exc.__class__.__name__})"
            if path:
                self.put(path, body, f"inbox: text of Drive file {meta.get('name') or fid}")
            line += suffix
        watched = self.folder_info(folder["id"])[0] if folder.get("id") else ""
        fm = ["source: drive", f"file_id: \"{fid}\"", f"captured: {now:%Y-%m-%dT%H:%MZ}"]
        if folder.get("project"):
            fm.append(f"project: {folder['project']}")
        note = ("---\n" + "\n".join(fm) + "\n---\n\n"
                f"New file in the watched Google Drive folder \"{watched}\" (Drive watch module).\n\n## Links\n{line}\n")
        self.put(f"inbox/{stamp}-drive-{fid}.md", note, "inbox: 1 capture from Drive watch")
        log(f"filed Drive file {fid} ({meta.get('mimeType', '')}) from folder {folder.get('id', '?')}")

    # ---------- folder suggestions ----------
    def path(self, fid):
        """"Drive > a > b" for a folder id (names from the cache; the drive's own name when it is a shared drive)."""
        names, cur, hops = [], fid, 0
        while cur and hops < MAX_DEPTH:
            name, parent, _ = self.folder_info(cur)
            names.append(name)
            cur, hops = parent, hops + 1
        return " > ".join(reversed(names))

    def suggestions(self, now=None):
        """[(folder id, path, n files)] due for a suggestion, and resets the counts; [] when not due yet."""
        now = now or _now()
        if not CFG.drive_watch_suggest:
            return []
        if not self.st.get("suggested_at"):
            self.st["suggested_at"] = now                  # first run: start counting, suggest after one period
            return []
        if now - self.st["suggested_at"] < CFG.drive_watch_suggest_every * 86400:
            return []
        asked = set(self.st.get("suggested", []))
        out = []
        for fid, files in sorted(self.st["activity"].items(), key=lambda kv: -len(kv[1])):
            if len(files) < CFG.drive_watch_suggest_min or fid in asked or fid in CFG.drive_watch_never:
                continue
            try:
                if self.watched_ancestor([fid]) or any(a in CFG.drive_watch_never for a in self.ancestors(fid)):
                    continue
                out.append((fid, self.path(fid), len(files)))
            except Exception:  # noqa: BLE001 - a folder we cannot read is not a suggestion
                continue
            if len(out) >= CFG.drive_watch_suggest_max:
                break
        self.st["activity"], self.st["suggested_at"] = {}, now
        self.st["suggested"] = (self.st.get("suggested", []) + [f for f, _, _ in out])[-500:]
        return out

    def ancestors(self, fid):
        out, cur, hops = [], self.folder_info(fid)[1], 0
        while cur and hops < MAX_DEPTH:
            out.append(cur)
            cur, hops = self.folder_info(cur)[1], hops + 1
        return out

    def post_suggestions(self, items, bot):
        for fid, path, n in items:
            text = (f"📁 You changed {n} files in **{path}** since the last check. Watch this folder? "
                    f"Tap {ADD}: new files there will be filed like the Meet notes.")
            if DRY:
                print(text)
                continue
            ch = bot.actions_channel(self.st)   # its own channel when flux.toml names one
            mid = bot.post(ch, text)
            bot.react(ch, mid, ADD)
            buttons.track(mid, ch, "drive-watch", {ADD: {"folder": fid, "path": path}}, 7 * 86400)

    def act(self):
        """Add the folders the owner tapped ✅ on to the watched list (state `extra_folders`)."""
        n = 0
        for path, a in buttons.actions("drive-watch"):
            fid = a["payload"]["folder"]
            if fid not in self.folders:
                self.st.setdefault("extra_folders", []).append({"id": fid})
                self.folders[fid] = {"id": fid}
                log(f"drive watch: now watching {a['payload'].get('path', fid)}")
            if not DRY:
                buttons.done(path)
            n += 1
        return n

    def file_bytes(self, fid, export_mime=None):
        return self.d.export(fid, export_mime) if export_mime else self.d.download(fid)

    def put(self, path, text, message):
        if DRY:
            print(f"--- DRY {path} ---\n{text[:1500]}")
        else:
            self.gh.put_file(path, text.encode(), message)


def _now():
    return datetime.now(timezone.utc).timestamp()


def _ts(iso):
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def main():
    if not CFG.mod_drive_watch:
        raise SystemExit("flux: the Drive watch module is off ([modules] drive_watch = false in flux.toml); nothing to do")
    if not CFG.drive_watch_folders and not CFG.drive_watch_starred:
        raise SystemExit("flux: [drive_watch] folders is empty and starred is off in flux.toml; nothing to watch")
    st = load_json(state_file(), {"failures": 0})
    try:
        s = session()
        w = Watcher(Drive(s, folder=CFG.drive_folder_id or "unused"), None if DRY else GitHub(), st)
        if CFG.drive_watch_starred:
            w.add_starred()
        w.act()
        q = w.scan()
        n = w.settle_and_file()
        sug = w.suggestions()
        if sug:
            w.post_suggestions(sug, None if DRY else buttons.Bot())
        if q or n:
            log(f"drive watch: {q} queued, {n} filed, {len(st['pending'])} pending")
        st["failures"] = 0
        rc = 0
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        st["failures"] = st.get("failures", 0) + 1
        log(f"ERROR ({st['failures']} in a row): {exc.__class__.__name__}: {str(exc)[:200]}")
        if st["failures"] == FAIL_ALERT_AFTER:
            ops_alert(f"❌ Flux Drive watch module failing {FAIL_ALERT_AFTER} runs in a row: {exc.__class__.__name__}")
        rc = 1
    save_json(state_file(), st)   # DRY too: run a dry pass with a scratch FLUX_HOME, never the live one
    return rc


if __name__ == "__main__":
    sys.exit(main())
