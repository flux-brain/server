"""Offline tests for the Drive watch module (2026-09-28): first run records the feed position only, the cheap drops
(other drives, not owned, folders, trashed), the parent walk into sub-folders, settling, filing with project and text,
new files only, and a file that disappears while pending; starred folders (2026-10-10): the list read from Drive joins
the watched set, a listed folder keeps its project, and a file queued under a folder that lost its star is dropped.
No network: Drive and GitHub are fakes."""
from datetime import datetime, timedelta, timezone

import pytest

from flux_brain import drivewatch as dw
from flux_brain.config import CFG
from flux_brain.lib.captures import host_kind

MEET, SUB, OTHER, SHARED = "folderMeet00000000000001", "folderSub000000000000002", "folderOther0000000000003", "folderShared000000000004"
FOLDERS = {
    MEET: {"id": MEET, "name": "Meet", "parents": ["root"]},
    SUB: {"id": SUB, "name": "call 1", "parents": [MEET]},
    OTHER: {"id": OTHER, "name": "Other", "parents": ["root"]},
    "root": {"id": "root", "name": "My Drive"},
    SHARED: {"id": SHARED, "name": "Projects", "parents": ["driveX"], "driveId": "driveX"},
    "driveX": {"id": "driveX", "name": "Team", "driveId": "driveX"},
}


def iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FakeResp:
    def __init__(self, js, code=200):
        self._js, self.status_code = js, code

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            e = requests.HTTPError()
            e.response = self
            raise e


class FakeDrive:
    """Serves changes/startPageToken, one page of changes, folder lookups; metadata/export like lib.drive.Drive."""

    def __init__(self, changes, files):
        self.changes, self.files, self.lookups = changes, files, []
        self.starred, self.queries = [], []
        self.s = self
        self.token = type("T", (), {"reset": lambda self: None})()

    def headers(self):
        return {}

    def get(self, url, headers=None, timeout=None, params=None):
        if url.endswith("changes/startPageToken"):
            return FakeResp({"startPageToken": "t1"})
        if url.endswith("/changes"):
            return FakeResp({"changes": self.changes, "newStartPageToken": "t2"})
        if url.endswith("/files"):                      # the starred-folders list, served in pages of one
            self.queries.append(params)
            i = int(params.get("pageToken", 0))
            page = {"files": self.starred[i:i + 1]}
            if i + 1 < len(self.starred):
                page["nextPageToken"] = str(i + 1)
            return FakeResp(page)
        fid = url.rsplit("/", 1)[1]
        self.lookups.append(fid)
        return FakeResp(FOLDERS[fid])

    def metadata(self, fid):
        if fid not in self.files:
            return FakeResp({}, 404).raise_for_status()
        return dict(self.files[fid], folder="call 1")

    def export(self, fid, mime):
        return b"Anna: the fee is fixed"

    def download(self, fid):
        return b"%PDF"


def f(fid, parents, minutes_ago=30, **kw):
    return dict({"id": fid, "name": f"Notes {fid}", "mimeType": "application/vnd.google-apps.document",
                 "modifiedTime": iso(minutes_ago), "parents": parents, "ownedByMe": True,
                 "webViewLink": f"https://docs.google.com/document/d/{fid}/edit"}, **kw)


class FakeGH:
    def __init__(self):
        self.puts = []

    def put_file(self, path, data, message):
        self.puts.append((path, data.decode()))


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(CFG, "drive_watch_settle", 600)
    monkeypatch.setattr(CFG, "drive_watch_owned_only", True)
    monkeypatch.setattr(CFG, "drive_watch_text", True)
    monkeypatch.setattr(CFG, "drive_watch_max_per_run", 10)


def watcher(changes, files, st=None, folders=None):
    st = st if st is not None else {"page_token": "t1"}
    gh = FakeGH()
    w = dw.Watcher(FakeDrive(changes, files), gh, st,
                   folders=folders or [{"id": MEET, "project": "meetings"}, {"id": SHARED}])
    return w, gh, st


def test_first_run_records_the_position_only(cfg):
    w, gh, st = watcher([{"fileId": "a", "file": f("a", [SUB])}], {}, st={})
    assert w.scan() == 0 and st["page_token"] == "t1" and st["pending"] == {} and gh.puts == []


def test_scan_keeps_only_files_under_watched_folders(cfg):
    good = f("good0000000000000000001", [SUB])
    changes = [
        {"fileId": "x", "removed": True},
        {"fileId": good["id"], "file": good},
        {"fileId": "tr", "file": f("tr", [SUB], trashed=True)},
        {"fileId": "fo", "file": f("fo", [MEET], mimeType="application/vnd.google-apps.folder")},
        {"fileId": "ot", "file": f("ot", [OTHER])},
        {"fileId": "no", "file": f("no", [SUB], ownedByMe=False)},
        {"fileId": "dy", "file": f("dy", ["folderInDriveY"], driveId="driveY")},
        {"fileId": "sh", "file": f("sh", [SHARED], driveId="driveX", ownedByMe=False)},
    ]
    w, _gh, st = watcher(changes, {})
    assert w.scan() == 2
    assert set(st["pending"]) == {good["id"], "sh"} and st["page_token"] == "t2"
    assert "folderInDriveY" not in w.d.lookups   # another shared drive: dropped before any parent lookup


def test_settled_file_is_filed_with_project_and_text(cfg):
    fid = "doc00000000000000000001"
    w, gh, st = watcher([{"fileId": fid, "file": f(fid, [SUB])}], {fid: f(fid, [SUB], minutes_ago=30)})
    w.scan()
    assert w.settle_and_file() == 1
    paths = [p for p, _ in gh.puts]
    assert paths[0].startswith("raw/attachments/") and f"-drive-{fid}-Notes_{fid}.md" in paths[0]
    cap_path, cap = gh.puts[1]
    assert host_kind(cap_path) == ("drive", fid)
    assert "source: drive" in cap and "project: meetings" in cap and "watched Google Drive folder \"Meet\"" in cap
    assert f"text: [[{paths[0]}|" in cap and "Anna: the fee is fixed" in gh.puts[0][1]
    assert st["pending"] == {} and st["filed"] == [fid]


def test_unsettled_file_waits_and_filed_file_is_not_filed_again(cfg):
    fid = "doc00000000000000000002"
    files = {fid: f(fid, [SUB], minutes_ago=2)}
    w, gh, st = watcher([{"fileId": fid, "file": files[fid]}], files)
    w.scan()
    assert w.settle_and_file() == 0 and fid in st["pending"]
    files[fid] = f(fid, [SUB], minutes_ago=30)
    assert w.settle_and_file() == 1
    w2, gh2, _ = watcher([{"fileId": fid, "file": files[fid]}], files, st=st)   # edited later: in the feed again
    assert w2.scan() == 0 and gh2.puts == []


def test_file_gone_while_pending_is_dropped(cfg):
    fid = "doc00000000000000000003"
    w, gh, st = watcher([{"fileId": fid, "file": f(fid, [SUB])}], {})
    w.scan()
    assert w.settle_and_file() == 0 and st["pending"] == {} and gh.puts == []


def test_text_off_gives_details_only(cfg, monkeypatch):
    monkeypatch.setattr(CFG, "drive_watch_text", False)
    fid = "doc00000000000000000004"
    w, gh, _ = watcher([{"fileId": fid, "file": f(fid, [SUB])}], {fid: f(fid, [SUB])})
    w.scan()
    w.settle_and_file()
    assert len(gh.puts) == 1 and "text:" not in gh.puts[0][1] and "## Links" in gh.puts[0][1]


# ---------- folder suggestions ----------

@pytest.fixture
def sug(cfg, monkeypatch):
    import shutil
    shutil.rmtree(CFG.state_dir / "tracked", ignore_errors=True)
    shutil.rmtree(CFG.state_dir / "actions", ignore_errors=True)
    monkeypatch.setattr(CFG, "drive_watch_suggest", True)
    monkeypatch.setattr(CFG, "drive_watch_suggest_every", 7)
    monkeypatch.setattr(CFG, "drive_watch_suggest_min", 3)
    monkeypatch.setattr(CFG, "drive_watch_suggest_max", 3)
    monkeypatch.setattr(CFG, "drive_watch_never", [])


def mine(fid, parent, **kw):
    return {"fileId": fid, "file": f(fid, [parent], lastModifyingUser={"me": True}, **kw)}


def test_owner_activity_is_counted_anywhere_and_suggested_weekly(sug):
    changes = [mine(f"o{i}", OTHER) for i in range(4)] + [mine("s1", SUB)]
    changes += [{"fileId": "x1", "file": f("x1", [OTHER], lastModifyingUser={"me": False})}]
    w, _gh, st = watcher(changes, {}, st={"page_token": "t1", "suggested_at": 1.0})
    w.scan()
    assert len(st["activity"][OTHER]) == 4 and st["activity"][SUB] == ["s1"]
    got = w.suggestions(now=1.0 + 8 * 86400)
    assert got == [(OTHER, "My Drive > Other", 4)]       # SUB is under a watched folder; x1 was not the owner's
    assert st["activity"] == {} and OTHER in st["suggested"]
    assert w.suggestions(now=1.0 + 9 * 86400) == []       # not due again for a week


def test_first_period_only_starts_counting(sug):
    w, _gh, st = watcher([], {}, st={"page_token": "t1"})
    assert w.suggestions(now=100.0) == [] and st["suggested_at"] == 100.0


def test_never_list_blocks_a_folder_and_its_children(sug, monkeypatch):
    monkeypatch.setattr(CFG, "drive_watch_never", ["root"])
    w, _gh, st = watcher([mine(f"o{i}", OTHER) for i in range(4)], {}, st={"page_token": "t1", "suggested_at": 1.0})
    w.scan()
    assert w.suggestions(now=1.0 + 8 * 86400) == []


def test_post_and_tap_adds_the_folder(sug):
    from flux_brain.lib import buttons

    class Bot:
        def __init__(self):
            self.posts = []

        def channel_id(self, names, cache):
            return "c1"

        def actions_channel(self, cache):
            return "c1"

        def post(self, ch, text):
            self.posts.append(text)
            return "p1"

        def react(self, ch, mid, e):
            pass
    w, _gh, st = watcher([], {}, st={"page_token": "t1"})
    bot = Bot()
    w.post_suggestions([(OTHER, "My Drive > Other", 4)], bot)
    assert "**My Drive > Other**" in bot.posts[0]
    (_, entry), = buttons.tracked()
    buttons.emit(entry["module"], "p1", "✅", entry["actions"]["✅"])
    assert w.act() == 1 and st["extra_folders"] == [{"id": OTHER}] and OTHER in w.folders
    w2, _gh2, _ = watcher([], {}, st=st)
    assert OTHER in w2.folders                              # persists in state, flux.toml untouched


# ---------- starred folders ----------
def test_starred_folders_join_the_watched_set(cfg):
    under_star, under_meet = "doc00000000000000000010", "doc00000000000000000011"
    changes = [{"fileId": under_star, "file": f(under_star, [OTHER])}, {"fileId": under_meet, "file": f(under_meet, [SUB])}]
    w, _gh, st = watcher(changes, {})
    w.d.starred = [{"id": OTHER, "name": "Other", "parents": ["root"]}, {"id": MEET, "name": "Meet", "parents": ["root"]}]
    assert w.add_starred() == 1                                     # Meet was listed already
    assert w.folders[OTHER] == {"id": OTHER, "starred": True} and w.folders[MEET]["project"] == "meetings"
    assert len(w.d.queries) == 2 and "starred = true" in w.d.queries[0]["q"] and w.d.queries[1]["pageToken"] == "1"
    assert OTHER not in w.d.lookups                                 # name and parent come with the list
    assert w.scan() == 2
    assert st["pending"][under_star]["folder"] == OTHER and st["pending"][under_meet]["folder"] == MEET
    assert "extra_folders" not in st                                # the star is the switch: nothing kept in the state


def test_starred_only_needs_no_listed_folder(cfg):
    fid = "doc00000000000000000012"
    st = {"page_token": "t1"}
    w = dw.Watcher(FakeDrive([{"fileId": fid, "file": f(fid, [SUB])}], {fid: f(fid, [SUB])}), FakeGH(), st, folders=[])
    assert w.scan() == 0 and st["pending"] == {}                    # nothing starred yet: nothing is watched
    w.d.starred = [{"id": MEET, "name": "Meet", "parents": ["root"]}]
    w.st["page_token"] = "t1"
    assert w.add_starred() == 1 and w.scan() == 1                   # a sub-folder of the starred one
    assert w.settle_and_file() == 1
    cap = w.gh.puts[-1][1]
    assert "watched Google Drive folder \"Meet\"" in cap and "project:" not in cap


def test_file_queued_under_a_folder_that_lost_its_star_is_dropped(cfg):
    fid = "doc00000000000000000013"
    files = {fid: f(fid, [OTHER], minutes_ago=30)}
    w, _gh, st = watcher([{"fileId": fid, "file": files[fid]}], files)
    w.d.starred = [{"id": OTHER, "name": "Other", "parents": ["root"]}]
    w.add_starred()
    assert w.scan() == 1 and fid in st["pending"]
    w2, gh2, _ = watcher([], files, st=st)                          # next run: the star is gone
    assert w2.add_starred() == 0
    assert w2.settle_and_file() == 0 and st["pending"] == {} and gh2.puts == [] and fid not in st["filed"]


def test_starred_is_off_by_default_and_read_from_flux_toml(flux_home):
    flux_home('[vault]\nrepo = "owner/vault"\n')
    assert CFG.drive_watch_starred is False
    flux_home('[vault]\nrepo = "owner/vault"\n[drive_watch]\nstarred = true\n')
    assert CFG.drive_watch_starred is True and CFG.drive_watch_folders == []
