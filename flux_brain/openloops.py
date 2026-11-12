"""flux_brain.openloops: an open-items Google Sheet the owner reads and types instructions into (2026-11-12).

One row per open item of every project. The owner sees everything that is open in one place, sorts and filters it,
and can type an instruction on a row; Flux acts on it in the vault and writes its answer back into the row.
Each run (systemd timer, every 5 minutes by default, under flock):

  1. Guard: the Sheet's Drive permissions are read; any writer other than `[openloops] writers` (or no writers
     configured at all) = the run is skipped and an ops alert sent. The Instruction column is the one place where
     typed text is acted on as the owner's words, so only the owner may be able to edit the file. Sheets has no
     per-cell author, hence the file-level check.
  2. Pick-up: a row whose Instruction is non-empty and whose Instruction status is blank or `new`, or whose
     Instruction text changed since it was last picked up, becomes ONE vault capture `inbox/<stamp>-sheet-<ID>.md`
     (the row, the instruction and how to answer), then the row is set `queued`, then a short notice is posted in
     the actions channel. Each step is recorded in state, so a failed step is retried without repeating the others.
  3. Answers: the routine (no Sheet access) answers by committing `openloops/replies/<ID>.md` to the vault
     (frontmatter `status: done | waiting for you | question`, optional `item_status:`, `notify:`, `moved:`,
     `duplicate_of:`; body = one line). This module writes Instruction status, Reply, Updated (and Status when given)
     into the row, then deletes the reply file. A reply naming the `notify:` file it posted gets a link to that
     Discord message once the relay has posted it (held up to 15 minutes).
     A reply may say `moved: ^<id>` (the item became that action on another page): once a row holds that action,
     the old row gets Status done and `· moved to <new ID>` in its Reply.
  4. Status edits: a Status changed in the Sheet by anyone but this module is sent to Flux, all changes of one run in
     ONE capture `inbox/<stamp>-sheet-status-<stamp>.md`; the routine reflects them on the pages. The first run only
     records every Status (no backfill). Status `duplicate` = the row repeats another (`Duplicate of` names it, or
     Flux finds it and answers `duplicate_of:`).
  5. New page actions: a new open `- [ ] ... ^id` action on a vault project page becomes a row (`<PROJECT>-nn`,
     project = `[openloops] projects` entry for the page, else (when the vault holds a memory mirror) the index
     cluster of the page's first `memory:` file, else the page name; Holder `vault:<page>#^id`); when that
     action is ticked, the row's Status becomes `done`. The first run only records the actions already there.

Rows are found by their ID (column A) on every run, never by a saved row number: the owner sorts and filters, and
moves columns (cells are always written by header name).

Config (`flux.toml`):
    [modules] openloops = true
    [openloops]
    sheet_id = "..."                 # printed by `flux-openloops setup`
    writers = ["you@example.com"]    # REQUIRED: the only accounts allowed to edit the Sheet
    tab = "Inbox"
    title = "Flux open items"
    row_owner = ""                   # Owner cell of the rows this module adds
    projects = { kitchen-renovation = "home" }   # optional: vault page name -> project name
    token_file = "drive-token.json"  # default: the Drive token ([drive] token_file)

Token: a Google token that can read and write the Sheet and list its permissions. The Drive token (`drive.file`,
written by `flux-drive-auth`) is enough when `flux-openloops setup` created the Sheet: drive.file covers files this
app created. Commands: `flux-openloops` (one pass), `flux-openloops --dry` (reads and prints only),
`flux-openloops setup` (creates the Sheet). Switch: `touch $FLUX_HOME/openloops.disabled` stops it.
Exit 0 = run done (or disabled / guarded); 1 = something failed.
"""
import argparse
import datetime
import hashlib
import re
import sys

from .config import CFG
from .lib.common import log, ops_alert, redact

STATE_NAME = "openloops-state.json"   # {"rows": {ID: {"hash", "capture", "queued", "notice", "at"}}, "status", ...}
REPLY_DIR = "openloops/replies"
COLS = ["ID", "Project", "Item", "Owner", "Status", "Next check", "How to verify", "Holder",
        "Instruction", "Instruction status", "Reply", "Updated", "Duplicate of"]
ITEM_STATUSES = {"open", "waiting", "blocked", "watch", "check", "done", "duplicate"}      # the Status dropdown
CLOSED_STATUSES = {"done", "duplicate"}                                       # rows no longer open
ANSWER_STATUSES = {"done", "waiting for you", "question"}                    # Instruction status values a reply may set
WRITER_ROLES = {"writer", "fileOrganizer", "organizer", "owner"}
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"


def col_letter(i):
    """0 -> A ... 11 -> L (the Sheet has 12 columns, one letter is enough)."""
    return chr(ord("A") + i)


def ihash(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


# ---------- pure logic (tested without network) ----------

def parse_rows(values):
    """Sheet values (header first) -> {ID: {"row": 1-based sheet row, col name: value}}; header names located by
    text so a moved column does not shift meanings."""
    if not values:
        return {}, {}                                     # same (rows, column index) shape as below
    head = [h.strip() for h in values[0]]
    idx = {c: head.index(c) for c in COLS if c in head}
    missing = [c for c in ("ID", "Instruction", "Instruction status", "Reply", "Updated") if c not in idx]
    if missing:
        raise RuntimeError(f"header missing {missing}")
    rows = {}
    for n, v in enumerate(values[1:], start=2):
        get = lambda c: (v[idx[c]] if c in idx and idx[c] < len(v) else "").strip()  # noqa: E731
        rid = get("ID")
        if rid:
            rows[rid] = {"row": n, **{c: get(c) for c in idx}}
    return rows, idx


def pickups(rows, state):
    """IDs whose instruction must be picked up now: text present and (status blank/new, or text changed since the
    last pick-up). An in-flight pick-up (capture written, row not yet queued) is resumed, not restarted."""
    out = []
    for rid, r in rows.items():
        text = r.get("Instruction", "")
        if not text:
            continue
        st = state.get(rid, {})
        h = ihash(text)
        status = r.get("Instruction status", "").lower()
        if st.get("hash") == h and st.get("capture") and not (st.get("queued") and st.get("notice")):
            out.append(rid)                       # same instruction, a step still missing: resume
        elif status in ("", "new"):
            # the Sheet no longer says queued/done: a first instruction, or the owner reset the status to run it again
            if st.get("queued"):                  # a finished pick-up: forget its steps so all three run again
                keep = st.get("hash")
                st.clear()
                st["hash"] = keep
            out.append(rid)
        elif st.get("hash") and st.get("hash") != h:
            out.append(rid)                       # the owner rewrote the instruction without resetting the status
    return out


def parse_reply(text):
    """openloops/replies/<ID>.md -> (status, item_status, reply line, notify path, moved action id, duplicate_of) or None.
    `duplicate_of: <ROW-ID>` names the row this one repeats ("" when absent or not a row id).
    `moved: ^<id>` names the vault action this row's item became; "" when absent, None when the
    value is not an action id (the reply itself still counts)."""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text or "", re.S)
    if not m:
        return None
    # a trailing `# comment` on a frontmatter line (the example in ops/sheet.md has them) is dropped
    fm = {k: re.sub(r"\s+#.*$", "", v) for k, v in re.findall(r"^(\w+):\s*(.*?)\s*$", m.group(1), re.M)}
    status = fm.get("status", "").strip().strip('"').lower()
    item = fm.get("item_status", "").strip().strip('"').lower()
    notify = fm.get("notify", "").strip().strip('"')               # the notify/ file this answer posted
    body = " ".join(m.group(2).split())[:500]
    if status not in ANSWER_STATUSES or (item and item not in ITEM_STATUSES):
        return None
    if notify and not re.fullmatch(r"notify/[^/\s]+\.md", notify):
        notify = ""                                                 # only a notify/ file can be linked
    # same id shape as ACTION (four lowercase letters/digits), with or without the leading ^
    moved = fm.get("moved", "").strip().strip('"').lstrip("^")
    if moved and not re.fullmatch(r"[a-z0-9]{4}", moved):
        moved = None
    # a row ID like HOME-21 (letters, digits, dashes, ending -number)
    dup = fm.get("duplicate_of", "").strip().strip('"').upper()
    if dup and not re.fullmatch(r"[A-Z0-9-]+-\d+", dup):
        dup = ""
    return status, item, body, notify, moved, dup


def to_cells(rec, idx):
    """a new row {column name: value} -> the list of cells in the Sheet's CURRENT column order (the owner moves
    columns; a fixed order wrote values under the wrong headers)."""
    out = [""] * (max(idx.values()) + 1)
    for c, v in rec.items():
        if c in idx:
            out[idx[c]] = v
    return out


def dup_note(c):
    """what a status capture tells Flux about a row set to `duplicate`."""
    if c["to"].lower() != "duplicate":
        return ""
    if c.get("dup"):
        return (f" · DUPLICATE of {c['dup']}: close this item on its page (tick it, note `duplicate of {c['dup']}`),"
                f" keep {c['dup']}, copy over any fact only this one has")
    return (" · DUPLICATE, partner not given: find the row it repeats (same page or holder, same subject) and answer"
            f" in {REPLY_DIR}/{c['id']}.md with `status: done` + `duplicate_of: <ID>`; none or several → `status: question`")


def capture_body(rid, r, now):
    """The vault capture for one instruction: the row as context, then what the routine must do and how to answer."""
    ctx = "\n".join(f"- {c}: {r.get(c, '')}" for c in COLS if c not in ("Instruction", "Instruction status", "Reply") and r.get(c))
    return (
        "---\nsource: sheet\n"
        f"item_id: {rid}\n"
        f"project: {r.get('Project', '')}\n"
        f"captured: {now:%Y-%m-%dT%H:%MZ}\n---\n\n"
        f"{CFG.owner_name} typed an INSTRUCTION on row {rid} of the open-items Sheet (`{CFG.openloops_title}`, tab {CFG.openloops_tab}).\n\n"
        f"## Instruction\n\n{r.get('Instruction', '').strip()}\n\n"
        f"## The row\n\n{ctx}\n\n"
        "## How to handle it (vault `ops/sheet.md`)\n\n"
        "- A note, fact or status change: update the project page (and, with the memory module on, the usual memory proposal).\n"
        "- Work that needs the host or the outside world (send, pay, delete, sign, anything on the server): do NOT do it;\n"
        "  write a `notify/` card in #flux-actions saying what a Claude session should do, and answer `waiting for you`.\n"
        f"- Unclear: ask {CFG.owner_name} with a `notify/*question*` file and answer `question`.\n"
        f"- ALWAYS answer by committing `{REPLY_DIR}/{rid}.md`:\n"
        "  ```\n  ---\n  status: done            # or: waiting for you | question\n"
        "  item_status: done       # optional, only to change the row's Status (open, waiting, blocked, watch, check, done, duplicate)\n"
        "  moved: ^abcd            # optional: the instruction MOVED this item to an action on another page; give that\n"
        "                          # action's id and the host job closes this row once the new row exists\n"
        "  ---\n  One line for the Reply column.\n  ```\n"
    )


# ---------- vault pages ----------

ACTION = re.compile(r"^\s*- \[( |x|X)\] (.*?)\s*\^([a-z0-9]{4})\s*$")
DUE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")


def page_actions(text):
    """{^id: (ticked, text without the id, due date or "")} of one vault page."""
    out = {}
    for line in (text or "").splitlines():
        m = ACTION.match(line)
        if m:
            due = DUE.search(m.group(2))
            out[m.group(3)] = (m.group(1) != " ", m.group(2).strip(), due.group(1) if due else "")
    return out


MEMORY_INDEX = re.compile(r"memory/([a-z0-9][a-z0-9-]*)-INDEX\.md")


def memory_clusters(index_texts):
    """{memory file slug: cluster} from the vault's memory mirror: `memory/<cluster>-INDEX.md` lists its members as
    `](file.md)` links. The first cluster that lists a file wins (a file sits in one index)."""
    out = {}
    for cluster, text in sorted(index_texts.items()):
        for f in re.findall(r"\]\(([a-z0-9][a-z0-9-]*)\.md\)", text or ""):
            if not f.endswith("-INDEX"):
                out.setdefault(f, cluster)
    return out


def page_project(page, projects, text="", cluster_of=None):
    """Project of a vault page, first match wins: `[openloops] projects` (page name -> project); with a memory mirror in
    the vault, the cluster of the first file in the page's `memory:` list that an index lists; else the page name."""
    if page in projects:
        return projects[page]
    m = re.search(r"^memory:\s*\[(.*?)\]\s*$", text or "", re.M)
    for f in (m.group(1).split(",") if m and cluster_of else []):
        c = cluster_of.get(f.strip().strip("'\"").removesuffix(".md"))
        if c:
            return c
    return page


def next_id(project, rows, taken):
    """`<PROJECT>-nn`, one above the highest number used for that prefix (rows kept forever, so never reused)."""
    pre = project.upper() + "-"
    nums = [int(k[len(pre):]) for k in list(rows) + list(taken) if k.startswith(pre) and k[len(pre):].isdigit()]
    return f"{pre}{(max(nums) if nums else 0) + 1:02d}"


def run(sheet, gh, bot, state, today, now, dry=False, allowed=(),
        relay=None, guild="", projects=None):
    """One pass. Returns (exit code, list of log lines). Pure apart from the injected clients.
    relay = the relay's state dict (posted / posted_msg); projects = {page name: project}."""
    relay, projects = relay or {}, projects or {}
    if not allowed:
        return 0, ["GUARD: no [openloops] writers configured: run skipped, nothing picked up"]
    lines, rc = [], 0
    bad = sheet.foreign_writers(list(allowed))
    if bad:
        return 0, [f"GUARD: other writers on the Sheet {bad}: run skipped, nothing picked up"]
    rows, idx = parse_rows(sheet.values())
    cell = lambda rid, c: f"{col_letter(idx[c])}{rows[rid]['row']}"  # noqa: E731
    # 2: pick-ups
    for rid in pickups(rows, state.setdefault("rows", {})):
        r = rows[rid]
        st = state["rows"].setdefault(rid, {})
        h = ihash(r["Instruction"])
        if st.get("hash") != h:                                   # a new or rewritten instruction starts clean
            st.clear()
            st["hash"] = h
        if dry:
            lines.append(f"DRY pick-up {rid}: {r['Instruction'][:80]!r}")
            continue
        try:
            if not st.get("capture"):
                path = f"inbox/{now:%Y-%m-%dT%H%M%SZ}-sheet-{rid}.md"
                gh.put_file(path, redact(capture_body(rid, r, now))[0].encode(), f"inbox: 1 instruction from the open-items Sheet ({rid})")
                st["capture"] = path
            if not st.get("queued"):
                sheet.write([(cell(rid, "Instruction status"), "queued"), (cell(rid, "Updated"), today)])
                st["queued"] = True
            if not st.get("notice") and bot is not None:
                st["notice"] = bot(f"📝 **Open items** · {rid} ({r.get('Project', '')}) · instruction queued for Flux\n"
                                   f"> {r['Instruction'][:300]}")
            st["at"] = f"{now:%Y-%m-%dT%H:%MZ}"
            lines.append(f"PICKED {rid} -> {st['capture']}")
        except Exception as exc:                                  # noqa: BLE001
            lines.append(f"pick-up {rid} FAILED ({exc.__class__.__name__}: {str(exc)[:120]})")
            rc = 1
    # 3: answers from the routine
    for e in gh.list_dir(REPLY_DIR):
        rid = e["name"][:-3] if e["name"].endswith(".md") else None
        if not rid:
            continue
        if rid not in rows:
            lines.append(f"reply {rid}: no such row in the Sheet, file left for a human")
            continue
        text, sha = gh.get(e["path"])
        parsed = parse_reply(text)
        if not parsed:
            lines.append(f"reply {rid}: malformed, file left for a human")
            rc = 1
            continue
        status, item, body, notify, moved, dup = parsed
        if moved is None:                                         # bad id, the answer still goes in
            lines.append(f"reply {rid}: `moved:` is not an action id, ignored")
        # link the Discord message of the notice this answer posted, once the relay has posted it
        if notify:
            msg = relay.get("posted_msg", {}).get(notify)
            if msg and guild:
                body = f"{body} https://discord.com/channels/{guild}/{msg}"
            elif notify not in relay.get("posted", []) and "posted_msg" in relay:
                first = state.setdefault("waiting_notice", {}).setdefault(rid, f"{now:%Y-%m-%dT%H:%MZ}")
                age = (now - datetime.datetime.strptime(first, "%Y-%m-%dT%H:%MZ").replace(tzinfo=datetime.timezone.utc))
                if age < datetime.timedelta(minutes=15):
                    lines.append(f"reply {rid}: waiting for the relay to post {notify}")
                    continue
                body = f"{body} (see {notify})"
            else:
                body = f"{body} (see {notify})"             # relay without message ids, or posted before they were kept
            state.get("waiting_notice", {}).pop(rid, None)
        cells = [(cell(rid, "Instruction status"), status), (cell(rid, "Reply"), body), (cell(rid, "Updated"), today)]
        if dup and "Duplicate of" in idx and dup != rid:          # Flux named the partner row
            cells.append((cell(rid, "Duplicate of"), dup))
        if item and "Status" in idx:
            cells.append((cell(rid, "Status"), item))
            if not dry:
                state.setdefault("status", {})[rid] = item  # our own write: must not be sent back to Flux as a status edit
                rows[rid]["Status"] = item                  # and the snapshot read at the start of the run agrees
        if dry:
            lines.append(f"DRY reply {rid}: {status} / {item or '-'} / {body[:80]!r}")
            continue
        try:
            sheet.write(cells)
            gh.delete(e["path"], sha, f"openloops: reply {rid} written to the Sheet")
            if moved:                                             # close this row once the new one exists
                state.setdefault("moves", {})[rid] = {"id": moved, "reply": body, "at": f"{now:%Y-%m-%dT%H:%MZ}"}
            lines.append(f"REPLY {rid}: {status}" + (f", Status -> {item}" if item else ""))
        except Exception as exc:                                  # noqa: BLE001
            lines.append(f"reply {rid} FAILED ({exc.__class__.__name__}: {str(exc)[:120]})")
            rc = 1
    # 4: Status edits made in the Sheet by anyone but this job -> one capture for the routine
    seen = state.get("status")
    if "Status" in idx:
        current = {rid: r.get("Status", "") for rid, r in rows.items()}
        if seen is None:                                       # first run: record, no backfill
            if not dry:
                state["status"] = current
        else:
            changes = state.setdefault("status_pending", [])
            for rid, val in current.items():
                if rid in seen and seen[rid] != val and not any(c["id"] == rid and c["to"] == val for c in changes):
                    r = rows[rid]
                    changes.append({"id": rid, "project": r.get("Project", ""), "from": seen[rid], "to": val,
                                    "item": r.get("Item", "")[:200], "holder": r.get("Holder", ""),
                                    "dup": r.get("Duplicate of", "")})       # partner, may be blank
            if changes and not dry:
                try:
                    stamp = f"{now:%Y-%m-%dT%H%M%SZ}"
                    body = ("---\nsource: sheet\nkind: status\n" f"captured: {now:%Y-%m-%dT%H:%MZ}\n---\n\n"
                            f"Status changes made in the open-items Sheet (`{CFG.openloops_title}`), not by "
                            "Flux. Reflect each on its project page (ops/sheet.md, \"Status edits\"); no reply file.\n\n"
                            + "".join(f"- {c['id']} ({c['project']}): {c['from'] or '(blank)'} -> {c['to'] or '(blank)'}"
                                      f" · {c['item']}" + (f" · holder {c['holder']}" if c['holder'] else "")
                                      + dup_note(c) + "\n"
                                      for c in changes))
                    gh.put_file(f"inbox/{stamp}-sheet-status-{stamp}.md", redact(body)[0].encode(),
                                f"inbox: {len(changes)} status edit(s) from the open-items Sheet")
                    lines.append(f"STATUS {len(changes)} change(s) sent: " + ", ".join(c["id"] for c in changes))
                    changes.clear()
                except Exception as exc:                       # noqa: BLE001
                    lines.append(f"status capture FAILED ({exc.__class__.__name__}: {str(exc)[:120]}), kept for next run")
                    rc = 1
            elif changes:
                lines.append(f"DRY status changes: {[(c['id'], c['to']) for c in changes]}")
                changes.clear()
            if not dry:
                state["status"] = {**{k: v for k, v in seen.items() if k in current}, **current}
    # 5: new open actions on vault project pages -> rows; a ticked action closes its row
    try:
        tree = [e for e in gh.tree() if e.get("type", "blob") == "blob"]
        pages = [e for e in tree if re.fullmatch(r"wiki/projects/[^/]+\.md", e.get("path", ""))]
        indexes = {m.group(1): e["sha"] for e in tree if (m := MEMORY_INDEX.fullmatch(e.get("path", "")))}
    except Exception as exc:                                   # noqa: BLE001
        pages = None
        lines.append(f"page scan FAILED ({exc.__class__.__name__}: {str(exc)[:120]})")
        rc = 1
    if pages is not None:
        cluster_of = None                                      # memory mirror, read on first need
        known = state.get("actions")                           # {page path: {"sha", "ids": {id: ticked}}}
        first = known is None
        known = {} if first else known
        holders = {r.get("Holder", ""): rid for rid, r in rows.items() if r.get("Holder", "").startswith("vault:")}
        new_rows, closes, taken = [], [], []
        for e in pages:
            rec = known.get(e["path"])
            if rec and rec.get("sha") == e["sha"]:
                continue                                       # page unchanged since last run
            text = gh.blob_text(e["sha"])
            acts = page_actions(text)
            old = (rec or {}).get("ids", {})
            page = e["path"].rsplit("/", 1)[1][:-3]
            for aid, (ticked, atext, due) in acts.items():
                hold = f"vault:{e['path']}#^{aid}"
                if not first and aid not in old and not ticked and hold not in holders:
                    if cluster_of is None:                     # read the memory mirror once, only when a row is due
                        cluster_of = memory_clusters({c: gh.blob_text(s) for c, s in indexes.items()})
                    proj = page_project(page, projects, text, cluster_of)
                    nid = next_id(proj, rows, taken)
                    taken.append(nid)
                    new_rows.append({"ID": nid, "Project": proj, "Item": atext[:300], "Owner": CFG.openloops_row_owner, "Status": "open",
                                     "Next check": due, "Holder": hold, "Updated": today})
                elif ticked and not old.get(aid) and hold in holders:
                    rid = holders[hold]
                    if rows[rid].get("Status") not in CLOSED_STATUSES:   # keep `duplicate` as set
                        closes.append(rid)
            known[e["path"]] = {"sha": e["sha"], "ids": {a: t for a, (t, _x, _d) in acts.items()}}
        if dry:
            lines.append(f"DRY page actions: {'baseline' if first else ''} {len(new_rows)} new row(s), close {closes}")
        else:
            try:
                sheet.append([to_cells(nr, idx) for nr in new_rows])  # by header, not fixed order
                if closes and "Status" in idx:
                    sheet.write([(cell(rid, "Status"), "done") for rid in closes] + [(cell(rid, "Updated"), today) for rid in closes])
                    for rid in closes:
                        state.setdefault("status", {})[rid] = "done"   # our own write: not sent back to Flux
                for nr in new_rows:
                    state.setdefault("status", {})[nr["ID"]] = "open"
                live = {e["path"] for e in pages}
                state["actions"] = {p: v for p, v in known.items() if p in live}
                if first:
                    lines.append(f"page actions: baseline recorded ({sum(len(v['ids']) for v in known.values())} actions, no rows added)")
                if new_rows or closes:
                    lines.append(f"ROWS added {[r['ID'] for r in new_rows]}, closed {closes}")
            except Exception as exc:                           # noqa: BLE001
                lines.append(f"page rows FAILED ({exc.__class__.__name__}: {str(exc)[:120]}), retried next run")
                rc = 1
    # 6: a reply said the item MOVED to action ^id -> once a row holds that action, close the old row
    moves = state.get("moves", {})
    if moves:
        added = {nr["ID"]: {"Holder": nr["Holder"], "Status": nr["Status"]} for nr in (new_rows if pages is not None else [])}
        everyone = {**{k: v for k, v in rows.items()}, **added}
        for old, mv in list(moves.items()):
            tail = f"#^{mv['id']}"
            nid = next((k for k, v in everyone.items() if k != old and v.get("Holder", "").endswith(tail)), None)
            if old not in rows:
                lines.append(f"move {old}: row gone, dropped")
                moves.pop(old)
            elif nid:
                # current Reply cell (the owner may have edited it); fall back to what this run just wrote
                cur = rows[old].get("Reply", "") or mv.get("reply", "")
                if f"moved to {nid}" in cur:
                    moves.pop(old)
                    continue
                text = f"{cur} · moved to {nid}".lstrip(" ·")
                cells = [(cell(old, "Reply"), text), (cell(old, "Updated"), today)]
                if rows[old].get("Status") not in CLOSED_STATUSES and "Status" in idx:
                    cells.append((cell(old, "Status"), "done"))
                if dry:
                    lines.append(f"DRY move {old} -> {nid}")
                    continue
                try:
                    sheet.write(cells)
                    state.setdefault("status", {})[old] = "done"  # our own write: not echoed as a status edit
                    moves.pop(old)
                    lines.append(f"MOVED {old} -> {nid}: old row closed")
                except Exception as exc:                          # noqa: BLE001
                    lines.append(f"move {old} FAILED ({exc.__class__.__name__}: {str(exc)[:120]})")
                    rc = 1
            else:
                first_at = datetime.datetime.strptime(mv["at"], "%Y-%m-%dT%H:%MZ").replace(tzinfo=datetime.timezone.utc)
                if now - first_at > datetime.timedelta(hours=1):
                    lines.append(f"move {old} -> ^{mv['id']}: no row appeared within 1 h, left for a human")
                    if not dry:
                        moves.pop(old)
    # one summary line per run, so the log shows the Sheet was read even when nothing happened
    lines.append(f"run: {len(rows)} rows, {sum(1 for r in rows.values() if r.get('Instruction'))} with an instruction")
    return rc, lines



# ---------- I/O ----------

class Sheet:
    """The few Sheets/Drive calls this module needs, with the `[openloops] token_file` token."""

    def __init__(self, session, sheet_id=None, tab=None, token_path=None):
        from .lib.google import GoogleToken
        self.s = session
        self.token = GoogleToken(session, token_path or CFG.openloops_token_file, "open-items Sheet", "flux-drive-auth")
        self.id = CFG.openloops_sheet_id if sheet_id is None else sheet_id
        self.tab = tab or CFG.openloops_tab

    def _call(self, method, url, **kw):
        r = self.s.request(method, url, headers=self.token.headers(), timeout=60, **kw)
        if r.status_code == 401:                                  # access token expired mid-run: one fresh exchange
            self.token.reset()
            r = self.s.request(method, url, headers=self.token.headers(), timeout=60, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    def values(self):
        return self._call("GET", f"{SHEETS}/{self.id}/values/{self.tab}!A1:M").get("values", [])

    def write(self, cells):
        """cells = [(A1 cell, value)], USER_ENTERED so dates land as real dates."""
        if not cells:
            return
        self._call("POST", f"{SHEETS}/{self.id}/values:batchUpdate", json={
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": f"{self.tab}!{a}", "values": [[v]]} for a, v in cells]})

    def append(self, rows):
        """Add rows (cell lists in the header order, see to_cells) under the last row, USER_ENTERED."""
        if not rows:
            return
        self._call("POST", f"{SHEETS}/{self.id}/values/{self.tab}!A:M:append"
                           "?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS", json={"values": rows})

    def foreign_writers(self, allowed):
        """Writers of the file other than `allowed` (emails), incl. shared-drive members; anyone/domain = foreign."""
        perms = self._call("GET", f"https://www.googleapis.com/drive/v3/files/{self.id}/permissions",
                           params={"supportsAllDrives": "true",
                                   "fields": "permissions(type,role,emailAddress,domain)"}).get("permissions", [])
        ok = {a.lower() for a in allowed}
        bad = []
        for p in perms:
            if p.get("role") not in WRITER_ROLES:
                continue
            who = (p.get("emailAddress") or p.get("domain") or p.get("type", "")).lower()
            if p.get("type") in ("anyone", "domain") or who not in ok:
                bad.append(f"{p.get('type')}:{who}:{p.get('role')}")
        return bad

    def create(self, title, tab):
        """`setup`: a new spreadsheet with the header row, a frozen header, and the Status dropdown. Returns its id."""
        status_col = COLS.index("Status")
        body = {"properties": {"title": title},
                "sheets": [{"properties": {"title": tab, "gridProperties": {"frozenRowCount": 1}},
                            "data": [{"rowData": [{"values": [{"userEnteredValue": {"stringValue": c},
                                                               "userEnteredFormat": {"textFormat": {"bold": True}}}
                                                              for c in COLS]}]}]}]}
        sheet = self._call("POST", SHEETS, json=body)
        sid, gid = sheet["spreadsheetId"], sheet["sheets"][0]["properties"]["sheetId"]
        rule = {"condition": {"type": "ONE_OF_LIST",
                              "values": [{"userEnteredValue": v} for v in
                                         ["open", "waiting", "blocked", "watch", "check", "done", "duplicate"]]},
                "showCustomUi": True, "strict": False}
        self._call("POST", f"{SHEETS}/{sid}:batchUpdate", json={"requests": [{"setDataValidation": {
            "range": {"sheetId": gid, "startRowIndex": 1, "startColumnIndex": status_col,
                      "endColumnIndex": status_col + 1}, "rule": rule}}]})
        return sid


def state_file():
    return CFG.state_file(STATE_NAME)


def setup_main():
    """`flux-openloops setup`: create the Sheet with the Drive token (drive.file covers files this app created)."""
    from .lib.github import session
    sid = Sheet(session(), sheet_id="").create(CFG.openloops_title, CFG.openloops_tab)
    print(f"Created the open-items Sheet: https://docs.google.com/spreadsheets/d/{sid}\n"
          f"Put this in flux.toml:\n\n[openloops]\nsheet_id = \"{sid}\"\nwriters = [\"<your Google account email>\"]\n\n"
          "Then share nothing: anyone else with edit access stops the module (the guard).")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["setup"]:
        return setup_main()
    ap = argparse.ArgumentParser(prog="flux-openloops")
    ap.add_argument("--dry", action="store_true", help="read and print only")
    a = ap.parse_args(argv)
    if not CFG.mod_openloops:
        raise SystemExit("flux: the open-items Sheet module is off ([modules] openloops = false in flux.toml); nothing to do")
    if (CFG.home / "openloops.disabled").exists():
        log("openloops: disabled (openloops.disabled present)")
        return 0
    if not CFG.openloops_sheet_id:
        raise SystemExit("flux: [openloops] sheet_id missing in flux.toml (run `flux-openloops setup`)")
    from .lib import buttons
    from .lib.github import GitHub, session
    from .lib.state import load_json, save_json
    state = load_json(state_file(), {})
    poster = None
    if not a.dry:
        b = buttons.Bot()
        poster = lambda text: b.post(b.actions_channel(state), text)  # noqa: E731
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        rc, lines = run(Sheet(session()), GitHub(), poster, state, now.strftime("%Y-%m-%d"), now, dry=a.dry,
                        allowed=CFG.openloops_writers, relay=load_json(CFG.state_file("state.json"), {}),  # read only
                        guild=str(CFG.discord_guild or ""), projects=CFG.openloops_projects)
    except Exception as exc:                                      # noqa: BLE001
        log(f"openloops: run FAILED ({exc.__class__.__name__}: {str(exc)[:160]})")
        return 1
    for x in lines:
        log(f"openloops: {x}")
        if x.startswith("GUARD"):
            ops_alert(f"flux-openloops: {x}")
    if not a.dry:
        save_json(state_file(), state)
    return rc


if __name__ == "__main__":
    sys.exit(main())
