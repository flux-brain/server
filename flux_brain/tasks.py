"""flux_brain.tasks: Google Tasks view of the vault's project pages (the Tasks module).

One Google Tasks list per ACTIVE project page (`wiki/projects/<slug>.md`), titled `<prefix><page title>`, holding
one task per line of the page's `## Next actions` (completed = ticked). The owner ticks, adds, rewords or deletes
tasks on the phone or in Gmail's side panel; once a list has been left alone for `settle_s`, the edits are filed as
one capture `inbox/<stamp>-tasks-<slug>.md` (`source: tasks`, `kind: project-checklist`) and the vault routine
applies them to the page (template `ops/tasks.md`). Page changes flow the other way at the next tick.

Why Tasks and not Keep: Tasks has an official REST API with a scope of its own (`tasks`), so the token is an OAuth
grant limited to task lists rather than a full-account credential, and the API does not change under the client.

Identity: every action line ends with an Obsidian block id (`^a1b2`, see the vault CLAUDE.md). The id is written
into the task's `notes` field, so the mapping task <-> action survives a reword on either side AND a lost state file
(the map is rebuilt from the notes). A task without an id in its notes is one the owner added.

Quota: the Tasks API allows 50,000 calls a day per Cloud project. Each tick lists the task lists once and the tasks
of each active project once, so with P active projects and a tick of T seconds the daily use is about
(1 + P) * 86400 / T; the default T = 60 keeps 25 projects near 37,000. Paused and done projects are renamed once
and not polled. Raise `tick_s` if the log reports 403 quota errors.

Token: `$FLUX_HOME/tasks-token.json`, written once by `flux-tasks-auth`, read only (access token refreshed in
memory). State: `$FLUX_HOME/state/tasks-state.json`. Captures, not direct page edits: the routine is the only
writer of pages, so a tick that files an edit never races a run.
"""
import json
import os
import pathlib
import random
import re
import sys
import time
from datetime import datetime, timezone

import requests

from .config import CFG
from .lib.common import log
from .lib.github import GitHub, session
from .lib.secrets import SECRET_PATTERNS
from .lib.state import load_json, save_json

TASKS = "https://tasks.googleapis.com/tasks/v1"
SCOPE = "https://www.googleapis.com/auth/tasks"
TOKEN = CFG.tasks_token_file
STATE_FILE = str(CFG.state_dir / "tasks-state.json")
PREFIX = CFG.tasks_prefix            # list title prefix, "📁 " by default
TICK = CFG.tasks_tick                # seconds between ticks
SETTLE = CFG.tasks_settle            # a list must be unchanged this long before its edits are filed
PENDING_TTL = 2 * 3600               # a filed edit the page never reflected expires after this
PAGES = re.compile(r"^wiki/projects/([a-z0-9-]+)\.md$")
MAX_TITLE = 1000                     # Tasks caps a title at 1024 bytes
ACTION_ID = re.compile(r"[ \t]\^([a-z0-9]{4})(?=[ \t]|$)", re.M)   # block id ending an action line (re.M matters)
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
FAIL_ALERT_AFTER = 10
ONCE = os.environ.get("TASKS_ONCE") == "1"   # one tick, then exit (tests, hand runs)


# ---------- pure helpers (the vault side; shared shape with the Keep module) ----------
def split_action_id(text):
    """(action text without its `^id`, the id or None)."""
    m = ACTION_ID.search(text)
    if not m:
        return text.strip(), None
    return (text[:m.start()] + text[m.end():]).strip(), m.group(1)


def mint_action_id(taken):
    while True:
        new = "".join(random.choice(ID_ALPHABET) for _ in range(4))
        if new not in taken:
            return new


def norm(text):
    return " ".join((text or "").split()).casefold()


def clean(text):
    """Redact secret-shaped values; returns (text, redacted?)."""
    out = SECRET_PATTERNS.sub("[REDACTED]", text or "")
    return out, out != (text or "")


def parse_page(text):
    """{title, status, updated, actions: [[text, checked, id-or-None], ...]} from a project page."""
    fm = dict(re.findall(r"^(status|updated):\s*(\S+)", text.split("\n---", 1)[0], re.M))
    m = re.search(r"^# (.+)$", text, re.M)
    sec = re.search(r"^## Next actions[ \t]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    actions, open_action = [], False
    for raw in (sec.group(1) if sec else "").splitlines():
        box = re.match(r"^\s*[-*] \[([ xX])\] (.+)$", raw)
        if box:
            actions.append([box.group(2).strip(), box.group(1).lower() == "x"])
            open_action = True
        elif open_action and raw.strip() and raw[:1] in " \t":
            actions[-1][0] += " " + raw.strip()   # hard-wrapped continuation of the checkbox line
        else:
            open_action = False
    for a in actions:
        t, aid = split_action_id(a[0])
        a[0] = clean(t)[0][:MAX_TITLE]
        a.append(aid)
    return {"title": m.group(1).strip() if m else None, "status": fm.get("status", "?"),
            "updated": fm.get("updated", "?"), "actions": actions}


def diff_tasks(old, cur):
    """The owner's edits since the last render/filing: [{kind, tid, text, old?}] from two snapshots {tid: {t, c}}."""
    events = []
    for tid, c in cur.items():
        o = old.get(tid)
        if o is None:
            if norm(c["t"]):
                events.append({"kind": "done-new" if c["c"] else "new", "tid": tid, "text": c["t"]})
            continue
        if not norm(c["t"]):
            events.append({"kind": "removed", "tid": tid, "text": o["t"]})
            continue
        if norm(c["t"]) != norm(o["t"]):
            events.append({"kind": "reworded", "tid": tid, "text": c["t"], "old": o["t"]})
        if c["c"] != o["c"]:
            events.append({"kind": "done" if c["c"] else "reopened", "tid": tid, "text": c["t"]})
    for tid, o in old.items():
        if tid not in cur and norm(o["t"]):
            events.append({"kind": "removed", "tid": tid, "text": o["t"]})
    return events


def resolve_pending(pending, page, inbox, t):
    """Drop pending edits the page now reflects, or whose capture was processed long enough ago."""
    open_t = {norm(a) for a, c, *_ in page["actions"] if not c}
    all_t = {norm(a) for a, _, *_ in page["actions"]}
    keep = {}
    for tid, v in pending.items():
        n = norm(v["text"])
        resolved = {
            "done": n not in open_t, "done-new": n not in open_t, "reopened": n in open_t,
            "new": n in all_t, "removed": n not in all_t,
            "reworded": n in all_t or norm(v.get("old")) not in all_t,
        }.get(v["kind"], True)
        if v["capture"] in inbox:
            v["left_inbox"] = None
        elif not v.get("left_inbox"):
            v["left_inbox"] = t
        expired = v.get("left_inbox") and t - v["left_inbox"] > PENDING_TTL
        if not (resolved or expired):
            keep[tid] = v
    return keep


def stamp(t=None):
    return datetime.fromtimestamp(t or time.time(), timezone.utc)


def checklist_capture(slug, page, list_id, events, owner):
    words = {"done": "done", "done-new": "new action, already done", "reopened": "reopened",
             "new": "new action", "removed": "removed", "reworded": "reworded"}
    lines, redacted = [], False
    for e in events:
        text, r = clean(e["text"])
        redacted |= r
        tag = f" ^{e['action_id']}" if e.get("action_id") else ""   # edit the page line carrying this id
        if e["kind"] == "reworded":
            old, r2 = clean(e["old"])
            redacted |= r2
            lines.append(f"- reworded{tag}: {old} → {text}")
        else:
            lines.append(f"- {words[e['kind']]}{tag}: {text}")
    body = [
        "---", "source: tasks", "kind: project-checklist", f"project: {slug}",
        f"page: wiki/projects/{slug}.md", f"tasks_list: {list_id}",
        f"captured: {stamp():%Y-%m-%dT%H:%M:%SZ}", "---", "",
        f"{owner} edited the Google Tasks list of project [[{slug}]] ({page['title']}):", "", *lines,
    ]
    if redacted:
        body += ["", "⚠ Secret-shaped text was redacted by the Tasks module."]
    return "\n".join(body) + "\n"


# ---------- Google Tasks ----------
class TasksApi:
    """Thin client. The token file is read once; the access token is refreshed in memory per process."""

    def __init__(self, s, token_path=TOKEN):
        self.s, self.token_path, self._h = s, token_path, None

    def headers(self):
        if not self._h:
            try:
                with open(self.token_path) as f:
                    t = json.load(f)
            except FileNotFoundError:
                raise SystemExit(f"flux: Tasks module is on but {self.token_path} is missing: run flux-tasks-auth (INSTALL.md)")
            r = self.s.post(t.get("token_uri", "https://oauth2.googleapis.com/token"), timeout=30, data={
                "client_id": t["client_id"], "client_secret": t["client_secret"],
                "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
            if not r.ok:
                raise RuntimeError(f"Tasks token refresh failed: HTTP {r.status_code} (run flux-tasks-auth again if it persists)")
            self._h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        return self._h

    def call(self, method, path, **kw):
        """One API call. 401 -> refresh the access token once. 403/429 with a quota or rate reason (the first tick
        of a vault with many projects creates lists and tasks faster than the per-minute quota; seen live
        2026-09-22) -> wait and retry, 5 s, 10 s, 20 s, 40 s, then give up and let the tick fail."""
        for attempt in range(5):
            r = self.s.request(method, TASKS + path, headers=self.headers(), timeout=30, **kw)
            if r.status_code == 401 and attempt == 0:
                self._h = None
                continue
            if r.status_code in (403, 429) and attempt < 4 and ("uota" in r.text or "ate" in r.text or r.status_code == 429):
                wait = 5 * 2 ** attempt
                log(f"tasks: HTTP {r.status_code} on {method} {path.split('?')[0]}, waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json() if r.content else {}
        r.raise_for_status()
        return r.json() if r.content else {}

    def lists(self):
        out, token = [], None
        while True:
            page = self.call("GET", "/users/@me/lists", params={"maxResults": 100, **({"pageToken": token} if token else {})})
            out += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                return out

    def create_list(self, title):
        return self.call("POST", "/users/@me/lists", json={"title": title})

    def rename_list(self, list_id, title):
        return self.call("PATCH", f"/users/@me/lists/{list_id}", json={"title": title})

    def tasks(self, list_id):
        out, token = [], None
        while True:
            page = self.call("GET", f"/lists/{list_id}/tasks", params={
                "maxResults": 100, "showCompleted": "true", "showHidden": "true", **({"pageToken": token} if token else {})})
            out += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                return [t for t in out if not t.get("deleted")]

    def create_task(self, list_id, title, notes, done, previous=None):
        """Create a task. Google inserts at the TOP unless `previous` names the task to insert after, so the render
        passes the previous task in page order (seen live 2026-09-22: without it every first render reordered
        the whole list with one move call per task)."""
        return self.call("POST", f"/lists/{list_id}/tasks", params={"previous": previous} if previous else {}, json={
            "title": title, "notes": notes, "status": "completed" if done else "needsAction"})

    def patch_task(self, list_id, tid, **fields):
        return self.call("PATCH", f"/lists/{list_id}/tasks/{tid}", json=fields)

    def delete_task(self, list_id, tid):
        self.call("DELETE", f"/lists/{list_id}/tasks/{tid}")

    def move_task(self, list_id, tid, previous=None):
        return self.call("POST", f"/lists/{list_id}/tasks/{tid}/move", params={"previous": previous} if previous else {})


def snapshot(tasks):
    """{tid: {t, c, aid}} in list order (the API returns tasks by position)."""
    out = {}
    for t in tasks:
        aid = split_action_id(" " + (t.get("notes") or "").strip())[1]   # notes hold "^a1b2"
        out[t["id"]] = {"t": (t.get("title") or "").strip(), "c": t.get("status") == "completed", "aid": aid}
    return out


# ---------- the sync ----------
class Sync:
    def __init__(self, state):
        self.st = state
        self.st.setdefault("projects", {})
        self.s = session()
        self.gh = GitHub()
        self.api = TasksApi(self.s)
        self._lists = None

    def list_by_title(self):
        if self._lists is None:
            self._lists = {l["title"]: l["id"] for l in self.api.lists()}
        return self._lists

    def find_or_create_list(self, p, title):
        want = PREFIX + title
        if p.get("list_id"):
            return p["list_id"], False
        lid = self.list_by_title().get(want)
        if lid:
            return lid, False
        lid = self.api.create_list(want)["id"]
        self.list_by_title()[want] = lid
        return lid, True

    def render(self, slug, p, page, cur):
        """Make the list match the page: title, one task per action (matched by id from notes, else by text),
        status, order; delete tasks the page no longer has unless they are the owner's pending additions."""
        lid = p["list_id"]
        by_aid = {v["aid"]: tid for tid, v in cur.items() if v["aid"]}
        by_text = {norm(v["t"]): tid for tid, v in cur.items() if not v["aid"]}
        pending_new = {norm(v["text"]) for v in p.get("pending", {}).values() if v["kind"] in ("new", "done-new")}
        used, order, prev = set(), [], None
        for text, done, aid in page["actions"]:
            tid = by_aid.get(aid) if aid else None
            if not tid and norm(text) in by_text:
                tid = by_text.pop(norm(text))
            if tid:
                fields = {}
                if cur[tid]["t"] != text:
                    fields["title"] = text
                if cur[tid]["c"] != done:
                    fields["status"] = "completed" if done else "needsAction"
                    if not done:
                        fields["completed"] = None
                if aid and cur[tid]["aid"] != aid:
                    fields["notes"] = f"^{aid}"
                if fields:
                    self.api.patch_task(lid, tid, **fields)
            else:
                tid = self.api.create_task(lid, text, f"^{aid}" if aid else "", done, previous=prev)["id"]
            used.add(tid)
            order.append(tid)
            prev = tid
        for tid, v in cur.items():
            if tid not in used and norm(v["t"]) not in pending_new:
                self.api.delete_task(lid, tid)   # gone from the page (the page is the source of truth)
        # Reorder only when the EXISTING tasks are out of page order (new ones were inserted in place above).
        if [t for t in cur if t in used] != [t for t in order if t in cur]:
            prev = None
            for tid in order:
                self.api.move_task(lid, tid, previous=prev)
                prev = tid
        p["snapshot"] = snapshot(self.api.tasks(lid))

    def project(self, slug, entry, inbox):
        p = self.st["projects"].setdefault(slug, {})
        changed = entry["sha"] != p.get("page_sha") or "page" not in p
        if changed:
            page = parse_page(self.gh.blob_text(entry["sha"]))
            page["title"] = page["title"] or slug
        else:
            page = p["page"]
        if page["status"] != "active":
            # paused / done: rename once, stop polling (quota), keep the tasks as they are
            if p.get("list_id") and p.get("marked") != page["status"]:
                self.api.rename_list(p["list_id"], f"{PREFIX}{page['title']} ({page['status']})")
                p["marked"] = page["status"]
            p.update(page=page, page_sha=entry["sha"])
            return
        lid, created = self.find_or_create_list(p, page["title"])
        p["list_id"] = lid
        if p.get("marked"):
            self.api.rename_list(lid, PREFIX + page["title"])
            p.pop("marked")
        cur = snapshot(self.api.tasks(lid))
        filed = False
        if not created and "snapshot" in p:
            events = diff_tasks(p["snapshot"], cur)
            for e in events:
                e["action_id"] = cur.get(e["tid"], {}).get("aid") or p["snapshot"].get(e["tid"], {}).get("aid")
            if events:
                h = json.dumps(cur, sort_keys=True)
                u = p.get("unsettled")
                if not u or u["hash"] != h:
                    p["unsettled"] = {"hash": h, "since": time.time()}
                    return   # the owner may still be editing
                if time.time() - u["since"] < SETTLE:
                    return
                path = f"inbox/{stamp():%Y-%m-%dT%H%M%SZ}-tasks-{slug}.md"
                self.gh.put_file(path, checklist_capture(slug, page, lid, events, CFG.owner_name).encode(),
                                 f"tasks: {len(events)} list edit(s) on {slug}")
                pending = p.setdefault("pending", {})
                for e in events:
                    pending[e["tid"]] = {"kind": e["kind"], "text": e["text"], "old": e.get("old"),
                                         "capture": path, "filed": time.time()}
                p["snapshot"] = cur
                p.pop("unsettled", None)
                save_json(STATE_FILE, self.st)
                log(f"{slug}: filed {len(events)} Tasks edit(s) as {path}")
                filed = True
            else:
                p.pop("unsettled", None)
        before = len(p.get("pending", {}))
        p["pending"] = resolve_pending(p.get("pending", {}), page, inbox, time.time())
        cleared = len(p["pending"]) != before
        if created or changed or cleared or filed or "snapshot" not in p:
            self.render(slug, p, page, cur)
            log(f"{slug}: rendered list {lid} ({len(page['actions'])} actions, {len(p['pending'])} pending)")
        p.update(page=page, page_sha=entry["sha"], title=page["title"])

    def tick(self):
        tree = self.gh.tree()
        pages = {m.group(1): e for e in tree if e["type"] == "blob" for m in [PAGES.match(e["path"])] if m}
        inbox = {e["path"] for e in tree if e["path"].startswith("inbox/")}
        for slug, entry in sorted(pages.items()):
            self.project(slug, entry, inbox)
            save_json(STATE_FILE, self.st)   # per project: a failure later in the tick keeps what was rendered
        for slug in [s for s in self.st["projects"] if s not in pages]:
            p = self.st["projects"][slug]
            if p.get("list_id") and not p.get("gone"):
                self.api.rename_list(p["list_id"], f"{PREFIX}{p.get('title', slug)} (page removed)")
                p["gone"] = True
        save_json(STATE_FILE, self.st)


def ops_alert(text):
    if CFG.ops_webhook:
        try:
            requests.post(CFG.ops_webhook, json={"content": text}, timeout=15)
        except Exception as exc:  # noqa: BLE001
            log(f"ops alert not sent ({exc.__class__.__name__})")


def main():
    if not CFG.mod_tasks:
        raise SystemExit("flux: the Tasks module is off ([modules] tasks = false in flux.toml); nothing to do")
    st = load_json(STATE_FILE, {})
    sync = Sync(st)
    failures = 0
    while True:
        try:
            sync.tick()
            failures = 0
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log(f"ERROR ({failures} in a row): {exc.__class__.__name__}: {str(exc)[:200]}")
            if failures == FAIL_ALERT_AFTER:
                ops_alert(f"❌ Flux Tasks module failing {FAIL_ALERT_AFTER} ticks in a row: {exc.__class__.__name__}")
            sync._lists = None
        if ONCE:
            return 0
        time.sleep(TICK)


def auth_main(argv=None):
    """`flux-tasks-auth <client_secret.json>`: one-time consent (scope tasks only) that writes tasks-token.json.
    Same Desktop OAuth client as flux-drive-auth; enable the Tasks API on the project first."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit("usage: flux-tasks-auth <client_secret.json>")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit("flux-tasks-auth needs the drive extra: pip install 'flux-brain[drive]'")
    creds = InstalledAppFlow.from_client_secrets_file(argv[0], scopes=[SCOPE]).run_local_server(port=0, open_browser=True)
    out = pathlib.Path(TOKEN)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"client_id": creds.client_id, "client_secret": creds.client_secret,
                               "refresh_token": creds.refresh_token, "token_uri": creds.token_uri,
                               "scopes": list(creds.scopes or [SCOPE])}, indent=2))
    out.chmod(0o600)
    print(f"wrote {out} (mode 600). Copy it to the server's FLUX_HOME if this is not the server, then set "
          f"[modules] tasks = true in flux.toml and enable flux-tasks.service.")
