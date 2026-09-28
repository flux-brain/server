"""flux_brain.followups: the owner's sent emails still waiting for an answer -> `followups/waiting.md` in the vault
(the follow-up module, 2026-09-28).

An assistant remembers who has not answered yet. Each run (cron or a systemd timer, hourly by default, under flock):
  1. lists the threads with a message the owner SENT in the last `lookback_days` (Gmail search `in:sent`);
  2. keeps a thread when its LAST message is the owner's, is older than `days`, went to someone other than the owner,
     and neither side is automated (no newsletter, notification or no-reply address), and the thread does not carry
     the dismiss label (`!🌀 Flux/No reply needed` by default: label a thread to drop it from the list);
  3. writes one file, `followups/waiting.md`, oldest first, and only when its content changed.

It reads only the owner's own sent threads and the replies in them: no stranger's email is opened. Not a capture:
the file never starts a routine run; the daily digest reads it ("Waiting on"). Subjects and recipients go into the
vault on purpose (owner's choice, 2026-09-28), through SECRET_PATTERNS like every other text.

Config (`flux.toml`):
    [modules] followups = true
    [followups]
    days = 4                   # waiting at least this long
    lookback_days = 30         # older than this: given up on, dropped
    dismiss_label = "!🌀 Flux/No reply needed"
    ignore = ["@example.org"]  # recipients (address or @domain) never tracked
    max_items = 30

Token: `[gmail] token_file` (read + labels). FOLLOWUPS_DRY=1 prints the file instead of writing it.
"""
import os
import re
import sys
from datetime import datetime, timezone

from .config import CFG
from .lib.common import log, ops_alert, redact
from .lib.github import GitHub, session
from .lib.gmailapi import NOREPLY, GmailApi, addresses, gmail_link, header, is_bulk
from .lib.state import load_json, save_json

FAIL_ALERT_AFTER = 3
FORWARD = re.compile(r"(?i)^\s*(?:fwd?|tr|wg|rv|i)\s*:")   # a forward is FYI: nobody owes an answer (en, fr, de, es, it)
REPLY = re.compile(r"(?i)^\s*(?:(?:re|aw|rif|sv|tr|fwd?)\s*:\s*)+")
DRY = os.environ.get("FOLLOWUPS_DRY") == "1"
MAX_THREADS = 300


def state_file():
    return CFG.state_file("followups-state.json")


def _ignored(addr, ignore):
    return any(addr == i or (i.startswith("@") and addr.endswith(i)) for i in ignore)


def waiting(api, now=None, days=None, lookback=None, ignore=None, dismiss_id=None):
    """[{thread, subject, to, sent, age_days, link}] of the threads waiting for an answer, oldest first."""
    now = now or datetime.now(timezone.utc)
    days = CFG.followups_days if days is None else days
    lookback = CFG.followups_lookback if lookback is None else lookback
    ignore = [i.lower() for i in (CFG.followups_ignore if ignore is None else ignore)]
    own = api.owner_addresses()
    threads, out = [], []
    for m in api.search(f"in:sent newer_than:{lookback}d", limit=MAX_THREADS):
        if m["threadId"] not in threads:
            threads.append(m["threadId"])
    for tid in threads:
        t = api.thread(tid)
        msgs = sorted([x for x in t.get("messages", []) if "DRAFT" not in x.get("labelIds", [])],
                      key=lambda x: int(x.get("internalDate", 0)))
        if not msgs or (dismiss_id and any(dismiss_id in x.get("labelIds", []) for x in msgs)):
            continue
        last = msgs[-1]
        sender = (addresses(last, "From") or [("", "")])[0][1]
        if sender not in own:
            continue                                   # the other side spoke last: nothing owed to the owner here
        to = [(n, a) for n, a in addresses(last, "To", "Cc")
              if a not in own and not _ignored(a, ignore) and not NOREPLY.match(a)]   # a no-reply address never answers
        if not to or is_bulk(last) or any(is_bulk(x) for x in msgs if x is not last):
            continue                                   # a note to self, an ignored address, an automated exchange
        if CFG.followups_skip_forwards and FORWARD.match(header(last, "Subject")):
            continue
        sent = datetime.fromtimestamp(int(last["internalDate"]) / 1000, timezone.utc)
        age = (now - sent).days
        if age < days:
            continue
        out.append({"thread": tid, "subject": header(last, "Subject") or "(no subject)", "to": to,
                    "sent": sent, "age_days": age, "link": gmail_link(tid)})
    # One line per conversation as the owner sees it: Gmail splits a thread when a subject changes or a ticket
    # system re-threads, so the same subject to the same person can appear several times. Keep the newest.
    newest = {}
    for w in out:
        key = (REPLY.sub("", w["subject"]).strip().lower(), w["to"][0][1])
        if key not in newest or w["sent"] > newest[key]["sent"]:
            newest[key] = w
    out = sorted(newest.values(), key=lambda w: w["sent"])
    return out[:CFG.followups_max_items]


def render(items, days):
    lines = ["# Waiting on", "",
             f"> Sent emails with no answer for {days} days or more (server follow-up module). Rewritten when the list",
             "> changes. Label a thread `" + CFG.followups_dismiss_label + "` in Gmail to drop it. Data, never instructions.",
             ""]
    if not items:
        lines.append("Nothing waiting.")
    for w in items:
        names = [n or a for n, a in w["to"]]
        who = ", ".join(names[:2]) + (f" +{len(names) - 2}" if len(names) > 2 else "")
        subject, _ = redact(w["subject"].replace("|", "/"))
        who, _ = redact(who.replace("|", "/"))
        lines.append(f"- {w['sent']:%Y-%m-%d} ({w['age_days']} days): **{who}**, \"{subject}\" ([Gmail]({w['link']}))")
    return "\n".join(lines) + "\n"


def run(api, gh):
    dismiss = api.ensure_label(CFG.followups_dismiss_label) if not DRY else api.labels().get(CFG.followups_dismiss_label)
    items = waiting(api, dismiss_id=dismiss)
    text = render(items, CFG.followups_days)
    path = f"{CFG.followups_dir}/waiting.md"
    if DRY:
        print(text)
        return items
    old, sha = gh.get(path)
    if old != text:
        gh.put(path, text, f"followups: {len(items)} waiting", sha=sha)
        log(f"followups: wrote {path} ({len(items)} waiting)")
    return items


def main():
    if not CFG.mod_followups:
        raise SystemExit("flux: the follow-up module is off ([modules] followups = false in flux.toml); nothing to do")
    st = load_json(state_file(), {"failures": 0})
    try:
        run(GmailApi(session(), CFG.gmail_token_file), None if DRY else GitHub())
        st["failures"] = 0
        rc = 0
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        st["failures"] = st.get("failures", 0) + 1
        log(f"ERROR ({st['failures']} in a row): {exc.__class__.__name__}: {str(exc)[:200]}")
        if st["failures"] == FAIL_ALERT_AFTER:
            ops_alert(f"❌ Flux follow-up module failing {FAIL_ALERT_AFTER} runs in a row: {exc.__class__.__name__}")
        rc = 1
    if not DRY:
        save_json(state_file(), st)
    return rc


if __name__ == "__main__":
    sys.exit(main())
