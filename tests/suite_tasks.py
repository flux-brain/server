"""Offline tests for the Tasks module: page parsing with action ids, the render (create / patch / delete / order),
the owner's edits becoming one capture after the settle window, pending edits resolving when the page catches up,
the id-in-notes mapping surviving a lost state file, paused projects renamed and not polled. No network: the Tasks
API and the GitHub client are in-memory fakes.
  python tests/suite_tasks.py
"""
import sys
import time

from _load import CFG  # noqa: E402  sets FLUX_HOME to a temp dir first
import flux_brain.tasks as tk  # noqa: E402

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name); fails += 0 if cond else 1


class FakeTasks:
    """Google Tasks in a dict: lists {id: {title, tasks: [task dicts in position order]}}. Records calls."""
    def __init__(self):
        self.store, self.calls, self.n = {}, [], 0
    def _id(self, p):
        self.n += 1; return f"{p}{self.n}"
    def lists(self): return [{"id": i, "title": l["title"]} for i, l in self.store.items()]
    def create_list(self, title):
        i = self._id("L"); self.store[i] = {"title": title, "tasks": []}; self.calls.append(("create_list", title)); return {"id": i}
    def rename_list(self, lid, title):
        self.store[lid]["title"] = title; self.calls.append(("rename_list", title)); return {}
    def tasks(self, lid): return [dict(t) for t in self.store[lid]["tasks"]]
    def create_task(self, lid, title, notes, done, previous=None):
        t = {"id": self._id("T"), "title": title, "notes": notes, "status": "completed" if done else "needsAction"}
        ts = self.store[lid]["tasks"]; idx = ([x["id"] for x in ts].index(previous) + 1) if previous else 0   # Google: top unless previous
        ts.insert(idx, t); self.calls.append(("create_task", title, previous)); return t
    def patch_task(self, lid, tid, **f):
        t = next(x for x in self.store[lid]["tasks"] if x["id"] == tid); t.update({k: v for k, v in f.items() if v is not None})
        self.calls.append(("patch_task", tid, f)); return t
    def delete_task(self, lid, tid):
        self.store[lid]["tasks"] = [x for x in self.store[lid]["tasks"] if x["id"] != tid]; self.calls.append(("delete_task", tid))
    def move_task(self, lid, tid, previous=None):
        ts = self.store[lid]["tasks"]; t = next(x for x in ts if x["id"] == tid); ts.remove(t)
        idx = ([x["id"] for x in ts].index(previous) + 1) if previous else 0; ts.insert(idx, t); self.calls.append(("move_task", tid, previous))
    # what the owner does on the phone
    def tick(self, lid, tid, done=True):
        next(x for x in self.store[lid]["tasks"] if x["id"] == tid)["status"] = "completed" if done else "needsAction"
    def add(self, lid, title):
        t = {"id": self._id("T"), "title": title, "notes": "", "status": "needsAction"}; self.store[lid]["tasks"].append(t); return t["id"]
    def reword(self, lid, tid, title): next(x for x in self.store[lid]["tasks"] if x["id"] == tid)["title"] = title


class FakeGitHub:
    def __init__(self, pages): self.pages, self.puts, self.inbox = dict(pages), [], set()
    def tree(self):
        return ([{"type": "blob", "path": f"wiki/projects/{s}.md", "sha": f"sha-{hash(t) & 0xffff:x}"} for s, t in self.pages.items()]
                + [{"type": "blob", "path": p, "sha": "i"} for p in self.inbox])
    def blob_text(self, sha):
        return next(t for t in self.pages.values() if f"sha-{hash(t) & 0xffff:x}" == sha)
    def put_file(self, path, data, message): self.puts.append((path, data.decode())); self.inbox.add(path)


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

def make(pages):
    s = tk.Sync.__new__(tk.Sync); s.st = {"projects": {}}; s.gh = FakeGitHub(pages); s.api = FakeTasks(); s._lists = None
    return s

# 1 parse: three actions, wrapped line joined, ids stripped from the text
p = tk.parse_page(PAGE)
check("parse: actions, ids, wrap", [a[0] for a in p["actions"]] == ["Send the recap to the partner", "Book the call", "Read the term sheet, then reply to the lawyer"]
      and [a[2] for a in p["actions"]] == ["a1b2", "c3d4", "e5f6"] and p["actions"][1][1] is True and p["status"] == "active")

# 2 first tick: list created with the prefix, three tasks with ^id in notes, done state right, snapshot stored
s = make({"project-x": PAGE}); s.tick()
lid = s.st["projects"]["project-x"]["list_id"]; L = s.api.store[lid]
check("render: list + tasks", L["title"] == "📁 Project X" and [t["notes"] for t in L["tasks"]] == ["^a1b2", "^c3d4", "^e5f6"]
      and L["tasks"][1]["status"] == "completed" and len(s.st["projects"]["project-x"]["snapshot"]) == 3)
check("first render: tasks in page order with no move calls", [t["notes"] for t in L["tasks"]] == ["^a1b2", "^c3d4", "^e5f6"] and not any(c[0] == "move_task" for c in s.api.calls))
n_calls = len(s.api.calls)
# 3 second tick with nothing changed: no API writes
s.tick(); check("idle tick writes nothing", len(s.api.calls) == n_calls)

# 4 the owner ticks a task and adds one: first tick opens the settle window (no capture), after SETTLE one capture
tid_a = L["tasks"][0]["id"]; s.api.tick(lid, tid_a); s.api.add(lid, "Call the bank")
s.tick(); check("edit seen: settle window opened, no capture yet", s.gh.puts == [] and "unsettled" in s.st["projects"]["project-x"])
s.st["projects"]["project-x"]["unsettled"]["since"] -= tk.SETTLE + 1
s.tick()
check("capture filed after settle", len(s.gh.puts) == 1 and s.gh.puts[0][0].startswith("inbox/") and "-tasks-project-x.md" in s.gh.puts[0][0])
body = s.gh.puts[0][1]
check("capture body: source, id-tagged done, new action", "source: tasks" in body and "kind: project-checklist" in body
      and "- done ^a1b2: Send the recap to the partner" in body and "- new action: Call the bank" in body and CFG.owner_name in body)
check("owner's new task kept in the list while pending", any(t["title"] == "Call the bank" for t in s.api.store[lid]["tasks"]))

# 5 the page catches up (routine ticked a1b2 and added the new action with a fresh id): pending resolves, list rendered
PAGE2 = PAGE.replace("- [ ] Send the recap to the partner ^a1b2", "- [x] Send the recap to the partner ^a1b2").replace("## Log", "- [ ] Call the bank ^g7h8\n\n## Log")
s.gh.pages["project-x"] = PAGE2; s.gh.inbox.clear(); s.tick()
P = s.st["projects"]["project-x"]; L = s.api.store[lid]
check("pending resolved once the page reflects it", P["pending"] == {})
check("owner's task adopted: id written into its notes, no duplicate", [t["notes"] for t in L["tasks"]].count("^g7h8") == 1 and len(L["tasks"]) == 4)

# 6 reword on the page keeps the task (matched by id), title patched; a removed action deletes its task
PAGE3 = PAGE2.replace("Book the call ^c3d4", "Book the call with the partner ^c3d4").replace("- [ ] Read the term sheet, then\n  reply to the lawyer ^e5f6\n", "")
s.gh.pages["project-x"] = PAGE3; s.tick(); L = s.api.store[lid]
check("page reword patches the task by id", any(t["title"] == "Book the call with the partner" and t["notes"] == "^c3d4" for t in L["tasks"]))
check("page removal deletes the task", not any(t["notes"] == "^e5f6" for t in L["tasks"]) and len(L["tasks"]) == 3)

# 7 lost state: a fresh Sync over the same API rebuilds the mapping from the notes, files nothing, creates nothing
s2 = tk.Sync.__new__(tk.Sync); s2.st = {"projects": {}}; s2.gh = s.gh; s2.api = s.api; s2._lists = None
before = len(s2.api.calls); s2.tick()
creates = [c for c in s2.api.calls[before:] if c[0] in ("create_list", "create_task")]
check("state loss: no duplicate list or task, nothing filed", creates == [] and s2.gh.puts == s.gh.puts and s2.st["projects"]["project-x"]["list_id"] == lid)

# 8 owner reword + reopen on the phone become reworded + reopened events with the action id
tid_b = next(t["id"] for t in L["tasks"] if t["notes"] == "^c3d4")
s.api.reword(lid, tid_b, "Book the call with the partner next week"); s.api.tick(lid, tid_b, done=False)
s.tick(); s.st["projects"]["project-x"]["unsettled"]["since"] -= tk.SETTLE + 1; s.tick()
body = s.gh.puts[-1][1]
check("reworded + reopened captured with the id", "- reworded ^c3d4: Book the call with the partner → Book the call with the partner next week" in body and "- reopened ^c3d4:" in body)

# 9 a paused project: list renamed once, then not polled; back to active: renamed back
PAUSED = PAGE3.replace("status: active", "status: paused"); s.gh.pages["project-x"] = PAUSED; s.tick()
check("paused: renamed with the status", s.api.store[lid]["title"] == "📁 Project X (paused)")
n = len(s.api.calls); s.tick(); check("paused: not polled", len(s.api.calls) == n)
s.gh.pages["project-x"] = PAGE3; s.tick(); check("active again: title restored", s.api.store[lid]["title"] == "📁 Project X")

# 10 a secret in a task title is redacted in the capture
s.api.add(lid, "token " + "AKIA" + "ABCDEFGHIJKLMNOP" + " for the deploy")   # a secret-shaped string, built so no scanner sees a literal
s.tick(); s.st["projects"]["project-x"]["unsettled"]["since"] -= tk.SETTLE + 1; s.tick()
check("secret redacted in the capture", "[REDACTED]" in s.gh.puts[-1][1] and "AKIA" not in s.gh.puts[-1][1])

# 11 a 403 with a quota reason is retried with backoff (sleep stubbed), then succeeds
class R:
    def __init__(self, code, text="", js=None): self.status_code, self.text, self._js, self.content, self.ok = code, text, js or {}, b"1", code < 400
    def json(self): return self._js
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)
class QuotaSession:
    def __init__(self): self.n = 0
    def post(self, url, **kw): return R(200, js={"access_token": "at"})
    def request(self, method, url, **kw):
        self.n += 1
        return R(403, text='{"error": {"errors": [{"reason": "rateLimitExceeded"}]}}') if self.n <= 2 else R(200, js={"items": []})
waits = []; tk.time.sleep = lambda n: waits.append(n)
import json as _j, pathlib as _p; _tok = _p.Path(CFG.home, "drive-token.json"); _tok.write_text(_j.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
api = tk.TasksApi(QuotaSession(), token_path=str(_tok))
check("quota 403 retried with backoff", api.tasks("L") == [] and waits == [5, 10])

# 12 an email dragged into the list: handed to the Gmail module once, capture line names the filed email, task stays
class FakeGmail:
    def __init__(self): self.calls = []
    def file_message_id(self, mid, project, via="tasks"):
        self.calls.append((mid, project, via)); return f"inbox/2026-09-22T1600Z-gmail-{mid}.md"
s = make({"project-x": PAGE3}); s.tick()
lid = s.st["projects"]["project-x"]["list_id"]; g = FakeGmail(); s._gmail = g; CFG.mod_gmail = True
tid_e = s.api.add(lid, "Re: invoice 4471")
next(x for x in s.api.store[lid]["tasks"] if x["id"] == tid_e)["links"] = [{"type": "email", "description": "Re: invoice 4471", "link": "https://mail.google.com/mail/#all/18f3a2b4c5d6e7f8"}]
s.tick(); s.st["projects"]["project-x"]["unsettled"]["since"] -= tk.SETTLE + 1; s.tick()
body = s.gh.puts[-1][1]
check("email link parsed", tk.email_id("https://mail.google.com/mail/u/0/#inbox/18f3a2b4c5d6e7f8") == "18f3a2b4c5d6e7f8" and tk.email_id("https://example.com/x") is None)
check("email handed to the Gmail module with the project", g.calls == [("18f3a2b4c5d6e7f8", "project-x", "tasks")])
check("checklist line names the filed email", "- new action: Re: invoice 4471 (email filed as inbox/2026-09-22T1600Z-gmail-18f3a2b4c5d6e7f8.md)" in body)
check("task kept in the list", any(x["id"] == tid_e for x in s.api.store[lid]["tasks"]))
check("hand-off recorded once", s.st["projects"]["project-x"]["emailed"] == {tid_e: "inbox/2026-09-22T1600Z-gmail-18f3a2b4c5d6e7f8.md"})
# 13 gmail module off: plain action, no hand-off
CFG.mod_gmail = False; tid_f = s.api.add(lid, "Fwd: contract")
next(x for x in s.api.store[lid]["tasks"] if x["id"] == tid_f)["links"] = [{"type": "email", "link": "https://mail.google.com/mail/#all/18f3a2b4c5d6e7f9"}]
s.tick(); s.st["projects"]["project-x"]["unsettled"]["since"] -= tk.SETTLE + 1; s.tick()
check("gmail off: plain new action, no hand-off", "- new action: Fwd: contract\n" in s.gh.puts[-1][1] and len(g.calls) == 1)
CFG.mod_gmail = True

print(f"FAILS: {fails}"); sys.exit(1 if fails else 0)
