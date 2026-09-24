"""Offline tests for the Tasks module: page parsing with action ids, the render (create / patch / delete / order),
the owner's edits becoming one capture after the settle window, pending edits resolving when the page catches up,
the id-in-notes mapping surviving a lost state file, paused projects renamed and not polled, the email hand-off.
No network: the Tasks API and the GitHub client are in-memory fakes."""

import flux_brain.tasks as tk
from flux_brain.config import CFG


class FakeTasks:
    """Google Tasks in a dict: lists {id: {title, tasks: [task dicts in position order]}}. Records calls."""

    def __init__(self):
        self.store, self.calls, self.n = {}, [], 0

    def _id(self, p):
        self.n += 1
        return f"{p}{self.n}"

    def lists(self):
        return [{"id": i, "title": l["title"]} for i, l in self.store.items()]

    def create_list(self, title):
        i = self._id("L")
        self.store[i] = {"title": title, "tasks": []}
        self.calls.append(("create_list", title))
        return {"id": i}

    def rename_list(self, lid, title):
        self.store[lid]["title"] = title
        self.calls.append(("rename_list", title))
        return {}

    def tasks(self, lid):
        return [dict(t) for t in self.store[lid]["tasks"]]

    def create_task(self, lid, title, notes, done, previous=None):
        t = {"id": self._id("T"), "title": title, "notes": notes, "status": "completed" if done else "needsAction"}
        ts = self.store[lid]["tasks"]
        idx = ([x["id"] for x in ts].index(previous) + 1) if previous else 0   # Google: top unless previous
        ts.insert(idx, t)
        self.calls.append(("create_task", title, previous))
        return t

    def patch_task(self, lid, tid, **f):
        t = next(x for x in self.store[lid]["tasks"] if x["id"] == tid)
        t.update({k: v for k, v in f.items() if v is not None})
        self.calls.append(("patch_task", tid, f))
        return t

    def delete_task(self, lid, tid):
        self.store[lid]["tasks"] = [x for x in self.store[lid]["tasks"] if x["id"] != tid]
        self.calls.append(("delete_task", tid))

    def move_task(self, lid, tid, previous=None):
        ts = self.store[lid]["tasks"]
        t = next(x for x in ts if x["id"] == tid)
        ts.remove(t)
        idx = ([x["id"] for x in ts].index(previous) + 1) if previous else 0
        ts.insert(idx, t)
        self.calls.append(("move_task", tid, previous))

    # what the owner does on the phone
    def tick(self, lid, tid, done=True):
        next(x for x in self.store[lid]["tasks"] if x["id"] == tid)["status"] = "completed" if done else "needsAction"

    def add(self, lid, title):
        t = {"id": self._id("T"), "title": title, "notes": "", "status": "needsAction"}
        self.store[lid]["tasks"].append(t)
        return t["id"]

    def reword(self, lid, tid, title):
        next(x for x in self.store[lid]["tasks"] if x["id"] == tid)["title"] = title

    def by_notes(self, lid, notes):
        return next(t["id"] for t in self.store[lid]["tasks"] if t["notes"] == notes)


class FakeGitHub:
    def __init__(self, pages):
        self.pages, self.puts, self.inbox = dict(pages), [], set()

    def tree(self):
        return ([{"type": "blob", "path": f"wiki/projects/{s}.md", "sha": f"sha-{hash(t) & 0xffff:x}"} for s, t in self.pages.items()]
                + [{"type": "blob", "path": p, "sha": "i"} for p in self.inbox])

    def blob_text(self, sha):
        return next(t for t in self.pages.values() if f"sha-{hash(t) & 0xffff:x}" == sha)

    def put_file(self, path, data, message):
        self.puts.append((path, data.decode()))
        self.inbox.add(path)


PAGE = """---
type: project
status: active
updated: 2026-09-22
---

# Project X

## Next actions

- [ ] Send the recap to the partner ^a1b2
- [x] Book the call ^c3d4
- [ ] Read the term sheet, then
  reply to the lawyer ^e5f6

## Log
"""
PAGE2 = PAGE.replace("- [ ] Send the recap to the partner ^a1b2", "- [x] Send the recap to the partner ^a1b2").replace("## Log", "- [ ] Call the bank ^g7h8\n\n## Log")
PAGE3 = PAGE2.replace("Book the call ^c3d4", "Book the call with the partner ^c3d4").replace("- [ ] Read the term sheet, then\n  reply to the lawyer ^e5f6\n", "")


def make(pages):
    s = tk.Sync.__new__(tk.Sync)
    s.st, s.gh, s.api, s._lists = {"projects": {}}, FakeGitHub(pages), FakeTasks(), None
    return s


def settle(s, slug="project-x"):
    """The owner's edit seen on one tick, then the settle window elapsed and the next tick files it."""
    s.tick()
    s.st["projects"][slug]["unsettled"]["since"] -= tk.CFG.tasks_settle + 1
    s.tick()


def synced():
    """A Sync after the first render of PAGE (the state every later scenario starts from)."""
    s = make({"project-x": PAGE})
    s.tick()
    return s, s.st["projects"]["project-x"]["list_id"]


def after_owner_edit():
    """... plus the owner ticked a1b2 and added "Call the bank", filed as one capture."""
    s, lid = synced()
    s.api.tick(lid, s.api.by_notes(lid, "^a1b2"))
    s.api.add(lid, "Call the bank")
    settle(s)
    return s, lid


def after_catch_up():
    """... plus the page reflecting both edits (the routine gave the new action id g7h8)."""
    s, lid = after_owner_edit()
    s.gh.pages["project-x"] = PAGE2
    s.gh.inbox.clear()
    s.tick()
    return s, lid


def after_page_reword():
    """... plus a reword and a removal on the page (PAGE3)."""
    s, lid = after_catch_up()
    s.gh.pages["project-x"] = PAGE3
    s.tick()
    return s, lid


def test_parse_actions_ids_and_wrapped_line():
    p = tk.parse_page(PAGE)
    assert [a[0] for a in p["actions"]] == ["Send the recap to the partner", "Book the call", "Read the term sheet, then reply to the lawyer"]
    assert [a[2] for a in p["actions"]] == ["a1b2", "c3d4", "e5f6"] and p["actions"][1][1] is True and p["status"] == "active"


def test_first_render_creates_list_and_tasks_in_page_order():
    s, lid = synced()
    L = s.api.store[lid]
    assert L["title"] == "📁 Project X" and [t["notes"] for t in L["tasks"]] == ["^a1b2", "^c3d4", "^e5f6"]
    assert L["tasks"][1]["status"] == "completed" and len(s.st["projects"]["project-x"]["snapshot"]) == 3
    assert not any(c[0] == "move_task" for c in s.api.calls)   # inserted in place, no reorder


def test_idle_tick_writes_nothing():
    s, _ = synced()
    n = len(s.api.calls)
    s.tick()
    assert len(s.api.calls) == n


def test_owner_edits_open_the_settle_window_then_one_capture():
    s, lid = synced()
    s.api.tick(lid, s.api.by_notes(lid, "^a1b2"))
    s.api.add(lid, "Call the bank")
    s.tick()
    assert s.gh.puts == [] and "unsettled" in s.st["projects"]["project-x"]
    s.st["projects"]["project-x"]["unsettled"]["since"] -= tk.CFG.tasks_settle + 1
    s.tick()
    assert len(s.gh.puts) == 1 and s.gh.puts[0][0].startswith("inbox/") and "-tasks-project-x.md" in s.gh.puts[0][0]
    body = s.gh.puts[0][1]
    assert "source: tasks" in body and "kind: project-checklist" in body
    assert "- done ^a1b2: Send the recap to the partner" in body and "- new action: Call the bank" in body and CFG.owner_name in body
    assert any(t["title"] == "Call the bank" for t in s.api.store[lid]["tasks"])   # kept in the list while pending


def test_page_catching_up_resolves_pending_and_adopts_the_owner_task():
    s, lid = after_catch_up()
    P, L = s.st["projects"]["project-x"], s.api.store[lid]
    assert P["pending"] == {}
    assert [t["notes"] for t in L["tasks"]].count("^g7h8") == 1 and len(L["tasks"]) == 4


def test_page_reword_patches_by_id_and_removal_deletes():
    s, lid = after_page_reword()
    L = s.api.store[lid]
    assert any(t["title"] == "Book the call with the partner" and t["notes"] == "^c3d4" for t in L["tasks"])
    assert not any(t["notes"] == "^e5f6" for t in L["tasks"]) and len(L["tasks"]) == 3


def test_lost_state_rebuilds_the_mapping_from_the_notes():
    s, lid = after_page_reword()
    s2 = tk.Sync.__new__(tk.Sync)
    s2.st, s2.gh, s2.api, s2._lists = {"projects": {}}, s.gh, s.api, None
    before = len(s2.api.calls)
    s2.tick()
    creates = [c for c in s2.api.calls[before:] if c[0] in ("create_list", "create_task")]
    assert creates == [] and s2.gh.puts == s.gh.puts and s2.st["projects"]["project-x"]["list_id"] == lid


def test_owner_reword_and_reopen_carry_the_action_id():
    s, lid = after_page_reword()
    tid_b = s.api.by_notes(lid, "^c3d4")
    s.api.reword(lid, tid_b, "Book the call with the partner next week")
    s.api.tick(lid, tid_b, done=False)
    settle(s)
    body = s.gh.puts[-1][1]
    assert "- reworded ^c3d4: Book the call with the partner → Book the call with the partner next week" in body and "- reopened ^c3d4:" in body


def test_paused_project_renamed_once_not_polled_then_restored():
    s, lid = after_page_reword()
    s.gh.pages["project-x"] = PAGE3.replace("status: active", "status: paused")
    s.tick()
    assert s.api.store[lid]["title"] == "📁 Project X (paused)"
    n = len(s.api.calls)
    s.tick()
    assert len(s.api.calls) == n
    s.gh.pages["project-x"] = PAGE3
    s.tick()
    assert s.api.store[lid]["title"] == "📁 Project X"


def test_secret_in_a_task_title_is_redacted():
    s, lid = after_page_reword()
    s.api.add(lid, "token " + "AKIA" + "ABCDEFGHIJKLMNOP" + " for the deploy")   # built so no scanner sees a literal
    settle(s)
    assert "[REDACTED]" in s.gh.puts[-1][1] and "AKIA" not in s.gh.puts[-1][1]


def test_quota_403_retried_with_backoff(monkeypatch, google_token):
    class R:
        def __init__(self, code, text="", js=None):
            self.status_code, self.text, self._js, self.content, self.ok = code, text, js or {}, b"1", code < 400

        def json(self):
            return self._js

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(self.status_code)

    class QuotaSession:
        def __init__(self):
            self.n = 0

        def post(self, url, **kw):
            return R(200, js={"access_token": "at"})

        def request(self, method, url, **kw):
            self.n += 1
            return R(403, text='{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}') if self.n <= 2 else R(200, js={"items": []})
    waits = []
    monkeypatch.setattr(tk.time, "sleep", lambda n: waits.append(n))
    api = tk.TasksApi(QuotaSession(), token_path=str(google_token))
    assert api.tasks("L") == [] and waits == [5, 10]


class FakeGmail:
    def __init__(self):
        self.calls = []

    def file_message_id(self, mid, project, via="tasks"):
        self.calls.append((mid, project, via))
        return f"inbox/2026-09-22T1600Z-gmail-{mid}.md"


def add_email_task(s, lid, title, mid):
    tid = s.api.add(lid, title)
    next(x for x in s.api.store[lid]["tasks"] if x["id"] == tid)["links"] = [{"type": "email", "description": title, "link": f"https://mail.google.com/mail/#all/{mid}"}]
    return tid


def test_email_link_parsing():
    assert tk.email_id("https://mail.google.com/mail/u/0/#inbox/18f3a2b4c5d6e7f8") == "18f3a2b4c5d6e7f8" and tk.email_id("https://example.com/x") is None


def test_email_dragged_into_the_list_is_handed_to_gmail_once(monkeypatch):
    monkeypatch.setattr(CFG, "mod_gmail", True)
    s = make({"project-x": PAGE3})
    s.tick()
    lid = s.st["projects"]["project-x"]["list_id"]
    g = s._gmail = FakeGmail()
    tid_e = add_email_task(s, lid, "Re: invoice 4471", "18f3a2b4c5d6e7f8")
    settle(s)
    body = s.gh.puts[-1][1]
    assert g.calls == [("18f3a2b4c5d6e7f8", "project-x", "tasks")]
    assert "- new action: Re: invoice 4471 (email filed as inbox/2026-09-22T1600Z-gmail-18f3a2b4c5d6e7f8.md)" in body
    assert any(x["id"] == tid_e for x in s.api.store[lid]["tasks"])   # the task stays in the list
    assert s.st["projects"]["project-x"]["emailed"] == {tid_e: "inbox/2026-09-22T1600Z-gmail-18f3a2b4c5d6e7f8.md"}


def test_gmail_module_off_plain_action_no_hand_off(monkeypatch):
    monkeypatch.setattr(CFG, "mod_gmail", False)
    s = make({"project-x": PAGE3})
    s.tick()
    lid = s.st["projects"]["project-x"]["list_id"]
    g = s._gmail = FakeGmail()
    add_email_task(s, lid, "Fwd: contract", "18f3a2b4c5d6e7f9")
    settle(s)
    assert "- new action: Fwd: contract\n" in s.gh.puts[-1][1] and g.calls == []
