"""flux_brain.triage: new emails that probably matter, posted to the capture channel with two buttons (the triage
module, 2026-09-28).

Filing every email would bury the vault; filing none means the owner labels each one by hand. Each run (hourly by
default, under flock):
  1. acts on the buttons the owner tapped since the last run (flux_brain.lib.buttons):
     ✅ = label the conversation `[gmail] label`, so the Gmail module files it within a minute, as if labelled by hand;
     ✍️ = the same, plus a capture `inbox/<stamp>-triage-<thread id>.md` asking the routine for a reply DRAFT (it lands
     in `notify/` like every requested draft; nothing is ever sent or saved in Gmail);
  2. looks at the inbox of the last two days and keeps a message when a PERSON sent it (no newsletter, notification or
     no-reply address) and at least one reason holds, decided by fixed rules, never by a model:
       - it answers a conversation the owner wrote in;
       - its sender is someone the owner wrote to in the last `correspondents_days` (cached, refreshed daily);
       - its subject or snippet names one of `keywords` (e.g. the active projects);
  3. posts each one (at most `max_posts` a run, only between `hours_utc`) with the sender, the subject, a short
     redacted snippet and the reason, and adds the two reactions itself so a tap is all it takes.

Already labelled or filed conversations, the owner's own messages and anything seen before are skipped. The first
run only records what is in the inbox (no flood). Token: `[gmail] token_file` (read + labels); the module has no send
path (lib.gmailapi). TRIAGE_DRY=1 prints the posts instead and acts on nothing.
"""
import html
import os
import re
import sys
import time
from datetime import datetime, timezone

from .config import CFG
from .lib import buttons
from .lib.common import log, ops_alert, redact
from .lib.github import GitHub, session
from .lib.gmailapi import GmailApi, addresses, gmail_link, header, is_bulk
from .lib.state import load_json, save_json

FAIL_ALERT_AFTER = 3
FILE, DRAFT = "✅", "✍️"
MAX_SEEN = 3000
DRY = os.environ.get("TRIAGE_DRY") == "1"


def state_file():
    return CFG.state_file("triage-state.json")


class Triage:
    def __init__(self, api, gh, bot, st):
        self.api, self.gh, self.bot, self.st = api, gh, bot, st
        st.setdefault("seen", [])

    # ---------- 1. buttons ----------
    def act(self):
        n = 0
        for path, a in buttons.actions("triage"):
            tid = a["payload"]["thread"]
            if not DRY:
                self.api.label_thread(tid, add=[self.api.ensure_label(CFG.gmail_label)])
                if a["emoji"] == DRAFT:
                    self.draft_request(a["payload"])
                buttons.done(path)
            log(f"triage: {a['emoji']} on thread {tid}")
            n += 1
        return n

    def draft_request(self, p):
        now = datetime.now(timezone.utc)
        note = (f"---\nsource: triage\nwant: reply-draft\nthread_id: \"{p['thread']}\"\ngmail_link: {gmail_link(p['thread'])}\n"
                f"captured: {now:%Y-%m-%dT%H:%MZ}\n---\n\n"
                f"{CFG.owner_name} tapped ✍️ on the triage post for the email from {p.get('from', '?')}, "
                f"\"{p.get('subject', '')}\": write a reply draft. The conversation itself arrives as a `source: gmail` "
                f"capture with the same thread_id (labelled at the same time).\n")
        self.gh.put_file(f"inbox/{now:%Y-%m-%dT%H%M%SZ}-triage-{p['thread']}.md", note.encode(),
                         "inbox: triage reply-draft request")

    # ---------- 2. candidates ----------
    def correspondents(self):
        """Addresses the owner wrote to in the last correspondents_days, refreshed once a day (cached in state)."""
        c = self.st.get("correspondents") or {}
        if time.time() - c.get("at", 0) > 86400:
            addrs = set()
            for m in self.api.search(f"in:sent newer_than:{CFG.triage_correspondents_days}d", limit=500):
                addrs |= {a for _, a in addresses(self.api.message(m["id"], headers=("To", "Cc")), "To", "Cc")}
            c = {"at": time.time(), "addrs": sorted(addrs - self.api.owner_addresses())}
            self.st["correspondents"] = c
        return set(c["addrs"])

    def reason(self, msg, sender, known, words):
        tid = msg["threadId"]
        own = self.api.owner_addresses()
        if any((addresses(x, "From") or [("", "")])[0][1] in own for x in self.api.thread(tid).get("messages", [])):
            return "answers a conversation you wrote in"
        if sender in known:
            return "someone you have written to"
        text = f"{header(msg, 'Subject')} {msg.get('snippet', '')}"
        for w in words:
            if re.search(rf"(?i)(?<!\w){re.escape(w)}(?!\w)", text):
                return f"mentions {w}"
        return None

    def candidates(self):
        flux = {i for n, i in self.api.labels().items() if n in (CFG.gmail_label, CFG.gmail_filed_label)}
        own = self.api.owner_addresses()
        seen = set(self.st["seen"])
        stubs = [m for m in self.api.search("in:inbox newer_than:2d", limit=100) if m["id"] not in seen]
        if not self.st.get("initialised") and not DRY:                # a dry pass shows the current inbox instead
            self.st["initialised"] = True
            self.remember([m["id"] for m in stubs])
            log(f"triage initialised: {len(stubs)} inbox message(s) recorded, posts start with the next new one")
            return []
        known, words, out = self.correspondents(), CFG.triage_keywords, []
        for stub in reversed(stubs):                                   # oldest first
            msg = self.api.message(stub["id"])
            sender = (addresses(msg, "From") or [("", "")])[0]
            skip = (sender[1] in own or is_bulk(msg) or flux & set(msg.get("labelIds", []))
                    or "SPAM" in msg.get("labelIds", []))
            why = None if skip else self.reason(msg, sender[1], known, words)
            if why:
                out.append((msg, sender, why))
            else:
                self.remember([stub["id"]])                            # decided: never looked at again
        return out

    def remember(self, ids):
        self.st["seen"] = (self.st["seen"] + list(ids))[-MAX_SEEN:]

    # ---------- 3. posts ----------
    def post(self, items):
        lo, hi = CFG.triage_hours_utc
        if not DRY and not lo <= datetime.now(timezone.utc).hour < hi:
            return 0                                                   # quiet hours: they wait, still unseen
        n = 0
        for msg, (name, addr), why in items[:CFG.triage_max_posts]:
            subject, _ = redact(header(msg, "Subject") or "(no subject)")
            snippet, _ = redact(html.unescape(msg.get("snippet") or "")[:200])
            who = name or addr
            text = (f"📧 **{who}** · {subject}\n> {snippet}\n_{why}_ · {FILE} file it · {DRAFT} file it + reply draft")
            if DRY:
                print(text, "\n")
            else:
                ch = self.bot.channel_id(CFG.channel_names, self.st)
                mid = self.bot.post(ch, text)
                for e in (FILE, DRAFT):
                    self.bot.react(ch, mid, e)
                payload = {"thread": msg["threadId"], "from": who, "subject": subject}
                buttons.track(mid, ch, "triage", {FILE: payload, DRAFT: payload}, CFG.triage_ttl_days * 86400)
                self.remember([msg["id"]])
            n += 1
        return n


def main():
    if not CFG.mod_triage:
        raise SystemExit("flux: the triage module is off ([modules] triage = false in flux.toml); nothing to do")
    st = load_json(state_file(), {"failures": 0})
    try:
        s = session()
        t = Triage(GmailApi(s, CFG.gmail_token_file), None if DRY else GitHub(), None if DRY else buttons.Bot(), st)
        acted = t.act()
        posted = t.post(t.candidates())
        if acted or posted:
            log(f"triage: {acted} button(s) acted on, {posted} post(s)")
        st["failures"] = 0
        rc = 0
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        st["failures"] = st.get("failures", 0) + 1
        log(f"ERROR ({st['failures']} in a row): {exc.__class__.__name__}: {str(exc)[:200]}")
        if st["failures"] == FAIL_ALERT_AFTER:
            ops_alert(f"❌ Flux triage module failing {FAIL_ALERT_AFTER} runs in a row: {exc.__class__.__name__}")
        rc = 1
    save_json(state_file(), st)   # DRY too: run a dry pass with a scratch FLUX_HOME
    return rc


if __name__ == "__main__":
    sys.exit(main())
