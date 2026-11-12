"""Offline tests for flux_brain.openloops (the open-items Sheet module). No network: the Sheet, GitHub and Discord
are faked. Ported from the private host job's suite (2026-11-12); one scenario test, because the steps share state
the way consecutive runs do."""
import datetime as dt

import flux_brain.openloops as ol

HEAD = ol.COLS
WRITERS = ("owner@example.com",)


def run(*a, **k):
    k.setdefault("allowed", WRITERS)
    return ol.run(*a, **k)


NOW = dt.datetime(2026, 11, 9, 10, 0, tzinfo=dt.timezone.utc)
HEAD = ol.COLS


def row(rid, instr="", istatus="", status="open", project="alpha"):
    return [rid, project, "an item", "Owner", status, "", "", "alpha-notes", instr, istatus, "", "2026-11-09"]


class FakeSheet:
    def __init__(self, values, bad=()):
        self.v, self.bad, self.writes = values, list(bad), []

    def foreign_writers(self, allowed):
        return self.bad

    def values(self):
        return self.v

    def write(self, cells):
        self.writes.extend(cells)

    def append(self, rows):
        self.appended = getattr(self, "appended", []) + list(rows)


class FakeGH:
    def __init__(self, replies=None):
        self.files, self.deleted, self.replies = {}, [], replies or {}

    def put_file(self, path, data, msg):
        self.files[path] = data.decode()
        return True

    def list_dir(self, path):
        return [{"name": f"{k}.md", "path": f"{path}/{k}.md"} for k in self.replies]

    def get(self, path):
        return self.replies[path.rsplit("/", 1)[1][:-3]], "sha1"

    def delete(self, path, sha, msg):
        self.deleted.append(path)

    pages = {}                                                 # {path: text}; sha = hash of text

    def tree(self):
        return [{"path": p, "sha": str(hash(t)), "type": "blob"} for p, t in self.pages.items()]

    def blob_text(self, sha):
        return next(t for t in self.pages.values() if str(hash(t)) == sha)


posts = []
bot = lambda t: posts.append(t) or "msg1"  # noqa: E731
FAILED = []


def check(name, cond):
    if not cond:
        FAILED.append(name)




def test_no_writers_refuses_to_run():
    sh = FakeSheet([HEAD, row("ALPHA-01", "do x")])
    gh = FakeGH()
    rc, lines = ol.run(sh, gh, None, {}, "2026-11-09", NOW, allowed=())
    assert rc == 0 and not gh.files and lines[0].startswith("GUARD")


def test_capture_is_redacted():
    sh = FakeSheet([HEAD, row("ALPHA-01", "token ghp_" + "a" * 36)])
    gh = FakeGH()
    run(sh, gh, None, {}, "2026-11-09", NOW)
    assert "ghp_" not in list(gh.files.values())[0] and "[REDACTED]" in list(gh.files.values())[0]


def test_scenarios():
    FAILED.clear()
    # blank status + instruction -> picked, capture written, queued, notice posted
    state = {}
    sh = FakeSheet([HEAD, row("ALPHA-01", "close this, it is done"), row("ALPHA-02")])
    gh = FakeGH()
    rc, lines = run(sh, gh, bot, state, "2026-11-09", NOW)
    check("rc 0", rc == 0)
    check("one capture", len(gh.files) == 1 and list(gh.files)[0].endswith("-sheet-ALPHA-01.md"))
    check("capture names the reply path", "openloops/replies/ALPHA-01.md" in list(gh.files.values())[0])
    check("queued written at row 2 col J", ("J2", "queued") in sh.writes)
    check("notice posted", len(posts) == 1)
    # second run, nothing changed -> nothing new
    sh.writes.clear(); gh.files.clear(); posts.clear()
    sh.v[1][9] = "queued"
    rc, lines = run(sh, gh, bot, state, "2026-11-09", NOW)
    check("no repeat", not gh.files and not sh.writes and not posts)
    # the owner rewrites the instruction while status says done -> picked again
    sh.v[1][8], sh.v[1][9] = "actually reopen it", "done"
    rc, lines = run(sh, gh, bot, state, "2026-11-09", NOW)
    check("rewrite picked", len(gh.files) == 1)
    # status `new` typed by hand with the same text -> picked (explicit re-run)
    gh.files.clear(); sh.writes.clear(); sh.v[1][9] = "new"
    state["rows"]["ALPHA-01"]["queued"] = True
    rc, lines = run(sh, gh, bot, state, "2026-11-09", NOW)
    check("status reset to new re-runs the same instruction", len(gh.files) == 1 and ("J2", "queued") in sh.writes)
    # rows sorted differently: still found by ID
    state = {}
    sh = FakeSheet([HEAD, row("ALPHA-02"), row("ALPHA-01", "do x")])
    gh = FakeGH()
    run(sh, gh, bot, state, "2026-11-09", NOW)
    check("located by ID after reorder", ("J3", "queued") in sh.writes)
    # guard: a foreign writer -> nothing happens
    sh = FakeSheet([HEAD, row("ALPHA-01", "do x")], bad=["user:someone@x.com:writer"])
    gh = FakeGH()
    rc, lines = run(sh, gh, bot, {}, "2026-11-09", NOW)
    check("guard skips", rc == 0 and not gh.files and lines[0].startswith("GUARD"))
    # failed capture -> rc 1, nothing queued, retried next time
    class BadGH(FakeGH):
        def put_file(self, *a):
            raise RuntimeError("boom")
    state = {}
    sh = FakeSheet([HEAD, row("ALPHA-01", "do x")])
    rc, lines = run(sh, BadGH(), bot, state, "2026-11-09", NOW)
    check("capture failure rc 1, not queued", rc == 1 and not sh.writes)
    rc, lines = run(sh, FakeGH(), bot, state, "2026-11-09", NOW)
    check("retry succeeds", rc == 0 and ("J2", "queued") in sh.writes)
    # replies
    good = "---\nstatus: done\nitem_status: done\n---\nClosed: confirmation found.\n"
    gh = FakeGH({"ALPHA-01": good, "ALPHA-09": good, "ALPHA-02": "no frontmatter"})
    sh = FakeSheet([HEAD, row("ALPHA-01", "x", "queued"), row("ALPHA-02", "y", "queued")])
    rc, lines = run(sh, gh, bot, {"rows": {"ALPHA-01": {"hash": ol.ihash("x"), "capture": "c", "queued": True, "notice": "m"},
                                             "ALPHA-02": {"hash": ol.ihash("y"), "capture": "c", "queued": True, "notice": "m"}}},
                       "2026-11-09", NOW)
    check("reply written", ("J2", "done") in sh.writes and ("E2", "done") in sh.writes and ("K2", "Closed: confirmation found.") in sh.writes)
    check("reply file deleted", gh.deleted == ["openloops/replies/ALPHA-01.md"])
    check("malformed reply -> rc 1, kept", rc == 1 and "openloops/replies/ALPHA-02.md" not in gh.deleted)
    check("unknown row reply kept", any("ALPHA-09" in x and "no such row" in x for x in lines))
    check("bad item_status rejected", ol.parse_reply("---\nstatus: done\nitem_status: closed\n---\nx") is None)
    # dry run writes nothing
    sh = FakeSheet([HEAD, row("ALPHA-01", "do x")]); gh = FakeGH({"ALPHA-01": good}); st = {}
    run(sh, gh, None, st, "2026-11-09", NOW, dry=True)
    check("dry writes nothing", not sh.writes and not gh.files and not gh.deleted)

    # ---- reply link ----
    rep_link = "---\nstatus: waiting for you   # a comment\nnotify: notify/2026-11-09T1000-x.md\n---\nCard posted.\n"
    gh = FakeGH({"ALPHA-01": rep_link})
    sh = FakeSheet([HEAD, row("ALPHA-01", "x", "queued")])
    st = {"rows": {}, "status": {"ALPHA-01": "open"}}
    relay = {"posted": [], "posted_msg": {}}
    rc, lines = run(sh, gh, bot, st, "2026-11-09", NOW, relay=relay, guild="G")
    check("reply waits for the notice", not sh.writes and any("waiting for the relay" in x for x in lines))
    relay = {"posted": ["notify/2026-11-09T1000-x.md"], "posted_msg": {"notify/2026-11-09T1000-x.md": "C9/M9"}}
    rc, lines = run(sh, gh, bot, st, "2026-11-09", NOW, relay=relay, guild="G")
    check("reply carries the discord link", ("K2", "Card posted. https://discord.com/channels/G/C9/M9") in sh.writes)
    check("comment in frontmatter tolerated", ("J2", "waiting for you") in sh.writes)
    gh = FakeGH({"ALPHA-01": rep_link}); sh = FakeSheet([HEAD, row("ALPHA-01", "x", "queued")])
    rc, lines = run(sh, gh, bot, {"rows": {}}, "2026-11-09", NOW, relay={"posted": []}, guild="G")
    check("old relay: file named, no wait", ("K2", "Card posted. (see notify/2026-11-09T1000-x.md)") in sh.writes)
    late = NOW + dt.timedelta(minutes=20)
    gh = FakeGH({"ALPHA-01": rep_link}); sh = FakeSheet([HEAD, row("ALPHA-01", "x", "queued")])
    st = {"rows": {}, "waiting_notice": {"ALPHA-01": "2026-11-09T10:00Z"}}
    run(sh, gh, bot, st, "2026-11-09", late, relay={"posted": [], "posted_msg": {}}, guild="G")
    check("gives up waiting after 15 min", ("K2", "Card posted. (see notify/2026-11-09T1000-x.md)") in sh.writes)

    # ---- status edits ----
    gh = FakeGH(); sh = FakeSheet([HEAD, row("ALPHA-01"), row("ALPHA-02")]); st = {}
    run(sh, gh, bot, st, "2026-11-09", NOW)
    check("first run records statuses only", not gh.files and st["status"] == {"ALPHA-01": "open", "ALPHA-02": "open"})
    sh.v[1][4] = "done"
    rc, lines = run(sh, gh, bot, st, "2026-11-09", NOW)
    cap = [k for k in gh.files if "-sheet-status-" in k]
    check("status edit -> one capture", len(cap) == 1 and "ALPHA-01 (alpha): open -> done" in gh.files[cap[0]])
    gh.files.clear()
    run(sh, gh, bot, st, "2026-11-09", NOW)
    check("not sent twice", not gh.files)
    # a reply that sets item_status is our own write: not echoed back
    good2 = "---\nstatus: done\nitem_status: done\n---\nok\n"
    gh = FakeGH({"ALPHA-02": good2}); sh = FakeSheet([HEAD, row("ALPHA-01", status="done"), row("ALPHA-02", "y", "queued")])
    run(sh, gh, bot, st, "2026-11-09", NOW)
    sh.v[2][4] = "done"
    run(sh, gh, bot, st, "2026-11-09", NOW)
    check("own status write not echoed", not [k for k in gh.files if "-sheet-status-" in k])

    # ---- page actions ----
    page1 = "---\nmemory: [alpha-notes.md]\n---\n- [ ] old thing ^aaaa\n"
    gh = FakeGH(); gh.pages = {"wiki/projects/house.md": page1}
    sh = FakeSheet([HEAD, row("ALPHA-01"), row("ALPHA-07")]); st = {}
    rc, lines = run(sh, gh, bot, st, "2026-11-09", NOW, projects={"house": "alpha"})
    check("baseline adds no rows", not getattr(sh, "appended", []) and any("baseline" in x for x in lines))
    gh.pages = {"wiki/projects/house.md": page1 + "- [ ] call the plumber 📅 2026-11-20 ^bbbb\n"}
    rc, lines = run(sh, gh, bot, st, "2026-11-09", NOW, projects={"house": "alpha"})
    ap = getattr(sh, "appended", [])
    check("new action -> row ALPHA-08", len(ap) == 1 and ap[0][0] == "ALPHA-08" and ap[0][5] == "2026-11-20"
          and ap[0][7] == "vault:wiki/projects/house.md#^bbbb")
    check("unknown cluster -> page name", ol.page_project("house", {}) == "house")
    # the row now exists; the action is ticked -> row done
    sh = FakeSheet([HEAD, row("ALPHA-01"), row("ALPHA-07"), ["ALPHA-08", "alpha", "call the plumber", "Owner", "open", "", "", "vault:wiki/projects/house.md#^bbbb", "", "", "", ""]])
    gh.pages = {"wiki/projects/house.md": page1 + "- [x] call the plumber 📅 2026-11-20 ^bbbb\n"}
    rc, lines = run(sh, gh, bot, st, "2026-11-09", NOW, projects={"house": "alpha"})
    check("ticked action closes its row", ("E4", "done") in sh.writes)
    sh.v[3][4] = "done"; gh.files.clear()
    run(sh, gh, bot, st, "2026-11-09", NOW, projects={"house": "alpha"})
    check("close not echoed as a status edit", not [k for k in gh.files if "-sheet-status-" in k])

    # ---- a reply says the item moved to another page's action ----
    mv = "---\nstatus: done\nmoved: ^mlt1\n---\nMoved ^mlt1 to gamma.\n"
    check("moved parsed without ^", ol.parse_reply("---\nstatus: done\nmoved: mlt1\n---\nx")[4] == "mlt1")
    check("bad moved -> None, reply kept", ol.parse_reply("---\nstatus: done\nmoved: ^TOO-LONG\n---\nx")[4] is None)
    # same run: reply + the new action appears on another page
    reloc = "---\nmemory: [gamma-notes.md]\n---\n- [ ] notify the agent ^mlt1\n"
    gh = FakeGH({"BETA-29": mv}); gh.pages = {"wiki/projects/gamma-page.md": "---\nmemory: [gamma-notes.md]\n---\n"}
    sh = FakeSheet([HEAD, row("BETA-29", "move it", "queued", status="blocked", project="beta"), row("GAMMA-11", project="gamma")])
    st = {}
    run(sh, gh, bot, st, "2026-11-09", NOW, projects={"gamma-page": "gamma"})          # baseline of pages
    gh.replies = {"BETA-29": mv}; gh.pages = {"wiki/projects/gamma-page.md": reloc}; sh.writes = []
    rc, lines = run(sh, gh, bot, st, "2026-11-09", NOW, projects={"gamma-page": "gamma"})
    check("same run: new row added", [r[0] for r in getattr(sh, "appended", [])] == ["GAMMA-12"])
    check("same run: old row closed", ("E2", "done") in sh.writes and ("K2", "Moved ^mlt1 to gamma. · moved to GAMMA-12") in sh.writes)
    check("same run: move cleared, logged", not st.get("moves") and any("MOVED BETA-29 -> GAMMA-12" in x for x in lines))
    sh.v[1][4] = "done"; gh.files.clear(); gh.replies = {}
    run(sh, gh, bot, st, "2026-11-09", NOW, projects={"gamma-page": "gamma"})
    check("move close not echoed as a status edit", not [k for k in gh.files if "-sheet-status-" in k])
    # next run: the row already exists (added earlier), old row already done by hand -> suffix only
    gh = FakeGH({"BETA-29": mv}); gh.pages = {}
    sh = FakeSheet([HEAD, row("BETA-29", "move it", "queued", status="done", project="beta"),
                    ["GAMMA-12", "gamma", "notify", "Owner", "open", "", "", "vault:wiki/projects/gamma-page.md#^mlt1", "", "", "", ""]])
    st = {"status": {"BETA-29": "done", "GAMMA-12": "open"}, "actions": {}}
    rc, lines = run(sh, gh, bot, st, "2026-11-09", NOW)
    check("existing row: suffix written", any(c[0] == "K2" and c[1].endswith("moved to GAMMA-12") for c in sh.writes))
    check("already done: no Status write", ("E2", "done") not in sh.writes)
    # no row ever appears -> pending, then one warning after 1 h and dropped
    gh = FakeGH({"BETA-29": mv}); gh.pages = {}
    sh = FakeSheet([HEAD, row("BETA-29", "move it", "queued", project="beta")]); st = {"actions": {}}
    run(sh, gh, bot, st, "2026-11-09", NOW)
    check("pending move kept", st.get("moves", {}).get("BETA-29", {}).get("id") == "mlt1")
    gh.replies = {}
    rc, lines = run(sh, gh, bot, st, "2026-11-09", NOW + dt.timedelta(hours=2))
    check("stale move warned and dropped", any("left for a human" in x for x in lines) and not st["moves"] and rc == 0)
    # dry run resolves nothing
    gh = FakeGH(); gh.pages = {}
    sh = FakeSheet([HEAD, row("BETA-29", project="beta"),
                    ["GAMMA-12", "gamma", "n", "Owner", "open", "", "", "vault:wiki/projects/gamma-page.md#^mlt1", "", "", "", ""]])
    st = {"actions": {}, "moves": {"BETA-29": {"id": "mlt1", "reply": "r", "at": "2026-11-09T10:00Z"}}}
    rc, lines = run(sh, gh, bot, st, "2026-11-09", NOW, dry=True)
    check("dry move writes nothing", not sh.writes and "BETA-29" in st["moves"] and any("DRY move" in x for x in lines))

    # ---- Status `duplicate` + new rows written by header name ----
    # a reordered header (the owner moved columns): the new row's cells follow the header, not the old fixed order
    H2 = ["ID", "Project", "Item", "Status", "Instruction", "Instruction status", "Reply", "Owner", "Next check",
          "How to verify", "Holder", "Updated", "Duplicate of"]
    gh = FakeGH(); gh.pages = {"wiki/projects/house.md": page1}
    sh = FakeSheet([H2, ["ALPHA-01", "alpha", "x", "open"]]); st = {}
    run(sh, gh, bot, st, "2026-11-09", NOW, projects={"house": "alpha"})
    gh.pages = {"wiki/projects/house.md": page1 + "- [ ] call the plumber 📅 2026-11-20 ^bbbb\n"}
    run(sh, gh, bot, st, "2026-11-09", NOW, projects={"house": "alpha"})
    ap = getattr(sh, "appended", [])
    check("reordered header: row by name", len(ap) == 1 and dict(zip(H2, ap[0])) == {
        "ID": "ALPHA-02", "Project": "alpha", "Item": "call the plumber 📅 2026-11-20", "Status": "open", "Instruction": "", "Instruction status": "",
        "Reply": "", "Owner": "", "Next check": "2026-11-20", "How to verify": "",
        "Holder": "vault:wiki/projects/house.md#^bbbb", "Updated": "2026-11-09", "Duplicate of": ""})
    # status -> duplicate with a partner: the capture names it
    def drow(rid, status="open", dup=""):
        return row(rid, status=status) + [dup]
    gh = FakeGH(); sh = FakeSheet([HEAD, drow("ALPHA-21", dup="DELTA-11"), drow("ALPHA-22")]); st = {}
    run(sh, gh, bot, st, "2026-11-09", NOW)
    sh.v[1][4] = "duplicate"; sh.v[2][4] = "duplicate"
    run(sh, gh, bot, st, "2026-11-09", NOW)
    cap = "".join(v for k, v in gh.files.items() if "-sheet-status-" in k)
    check("duplicate with partner: named in capture", "ALPHA-21 (alpha): open -> duplicate" in cap and "DUPLICATE of DELTA-11" in cap)
    check("duplicate without partner: Flux asked to find it", "partner not given" in cap and "openloops/replies/ALPHA-22.md" in cap)
    # Flux's reply names the partner -> written to Duplicate of
    rp = "---\nstatus: done\nduplicate_of: delta-11\n---\nDuplicate of DELTA-11.\n"
    check("duplicate_of parsed + uppercased", ol.parse_reply(rp)[5] == "DELTA-11")
    check("bad duplicate_of dropped", ol.parse_reply("---\nstatus: done\nduplicate_of: nope\n---\nx")[5] == "")
    check("duplicate is a valid item_status", ol.parse_reply("---\nstatus: done\nitem_status: duplicate\n---\nx")[1] == "duplicate")
    gh = FakeGH({"ALPHA-22": rp}); sh = FakeSheet([HEAD, drow("ALPHA-22", status="duplicate")]); st = {"status": {"ALPHA-22": "duplicate"}}
    run(sh, gh, bot, st, "2026-11-09", NOW)
    check("partner written to Duplicate of", ("M2", "DELTA-11") in sh.writes and ("K2", "Duplicate of DELTA-11.") in sh.writes)
    # Flux then ticks the action on the page: the row stays `duplicate`, not `done`
    gh = FakeGH(); gh.pages = {"wiki/projects/house.md": "---\n---\n- [ ] router ^tpo1\n"}
    dr = drow("ALPHA-21", dup="DELTA-11"); dr[7] = "vault:wiki/projects/house.md#^tpo1"
    sh = FakeSheet([HEAD, dr]); st = {}
    run(sh, gh, bot, st, "2026-11-09", NOW)
    sh.v[1][4] = "duplicate"; st["status"]["ALPHA-21"] = "duplicate"
    gh.pages = {"wiki/projects/house.md": "---\n---\n- [x] router ^tpo1\n"}; sh.writes = []
    run(sh, gh, bot, st, "2026-11-09", NOW)
    check("ticked action keeps duplicate", ("E2", "done") not in sh.writes)
    assert not FAILED, FAILED



def test_config_defaults_fail_closed(flux_home):
    """No writers by default (the module refuses to run), the Drive token by default, projects map read as given."""
    c = flux_home('[vault]\nrepo = "owner/vault"\n[drive]\ntoken_file = "d.json"\n'
                  '[openloops]\nsheet_id = "S"\nprojects = { house = "alpha" }\n')
    assert c.openloops_writers == [] and c.openloops_tab == "Inbox" and not c.mod_openloops
    assert c.openloops_token_file.endswith("/d.json") and c.openloops_projects == {"house": "alpha"}


def test_project_from_memory_mirror():
    """With memory/<cluster>-INDEX.md in the vault, a new page action takes the cluster of the page's memory file."""
    page = "---\nmemory: [alpha-notes.md]\n---\n- [ ] old ^aaaa\n"
    gh = FakeGH()
    gh.pages = {"wiki/projects/house.md": page,
                "memory/alpha-INDEX.md": "# Alpha\n- [Notes](alpha-notes.md) — x\n- [Other](beta-INDEX.md)\n"}
    sh = FakeSheet([HEAD, row("ALPHA-03")])
    st = {}
    run(sh, gh, None, st, "2026-11-09", NOW)                     # baseline
    gh.pages["wiki/projects/house.md"] = page + "- [ ] new thing ^bbbb\n"
    run(sh, gh, None, st, "2026-11-09", NOW)
    ap = getattr(sh, "appended", [])
    assert len(ap) == 1 and ap[0][0] == "ALPHA-04" and ap[0][1] == "alpha"


def test_project_lookup_order():
    cl = ol.memory_clusters({"alpha": "- [n](alpha-notes.md)", "beta": "- [n](alpha-notes.md)\n- [m](beta-m.md)"})
    assert cl == {"alpha-notes": "alpha", "beta-m": "beta"}
    text = "---\nmemory: [unknown.md, beta-m.md]\n---"
    assert ol.page_project("house", {"house": "home"}, text, cl) == "home"     # the flux.toml map wins
    assert ol.page_project("house", {}, text, cl) == "beta"                     # first file an index lists
    assert ol.page_project("house", {}, "---\nmemory: []\n---", cl) == "house"  # else the page name
