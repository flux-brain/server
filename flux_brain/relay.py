"""flux_brain.relay: Discord capture channel <-> the vault repository on GitHub, run every 15 s by the loop service.

Why this exists: the vault routines run in Anthropic's cloud, whose default network allowlist
does not reach discord.com, and we refuse to put a bot token in cloud config. So this host does
all Discord I/O and talks to the repo only through the GitHub REST API (no local clone, hence no
rebase conflicts with the routines or the phone).

  inbound : new human messages in #vault -> inbox/<ts>-<msgid>.md, then a ✅ reaction (or a short
            reply if reactions are denied). Attachments go to Google Drive (shared drive "Vault",
            folder "Claude"), NOT into git, so the repo and the phone copy stay light; the note
            links to the Drive file. (Changed 2026-09-15 at the owner's request; was raw/attachments/.)
  outbound: new files under briefings/daily, briefings/weekly, notify/ -> posted to #vault once.
  run link: each time the relay starts the vault-inbox routine it posts the run's claude.ai link to
            #vault, so the owner can watch Claude work step by step (2026-09-16, the owner).

Secrets come from $FLUX_HOME/flux.env (Discord bot token, guild id, routine trigger token, GitHub token; see
flux_brain.config). Nothing secret is written to disk or logs. State (channel ids, last message id, posted paths,
failure count): $FLUX_HOME/state/state.json. Shared code (GitHub/Drive clients, state files, extraction, log):
flux_brain.lib.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone

import requests

# Shared code since 2026-09-18 (code review item 6): one GitHub client (retries PUTs here: put_file keeps an existing
# file, so a replayed write is idempotent), one Drive uploader, one state-file writer, the extraction functions
# (re-exported below so the offline tests can still stub `extract_text` on THIS module), one log().
from .config import CFG  # noqa: E402
from .lib.common import log  # noqa: E402
from .lib.secrets import SECRET_PATTERNS  # noqa: E402
from .lib.state import load_json, save_json  # noqa: E402
from .lib.github import GitHub, session  # noqa: E402
from .lib.drive import Drive  # noqa: E402
from .lib.extract import *  # noqa: E402,F401,F403  everything in extract.__all__ (extract_text, MAX_ATTACHMENT, ...)
from .lib.extract import _iwa_blocks, _run  # noqa: E402,F401  private helpers the iWork test exercises

REPO = CFG.vault_repo
BRANCH = CFG.vault_branch
# Two channels since 2026-09-22 (the owner: "separate human questions and background info"; a run summary posted right
# after his reply to a question was noise, and a summary landing after a question made him reply to the wrong
# message). The CONVERSATION channel (`CHANNEL_NAMES`, first name found wins; the id is cached in state so a rename
# in Discord changes nothing) carries his captures, ❓ questions, answers/drafts and the daily/weekly digests. The
# LOG channel (`LOG_CHANNEL_NAME`, meant to be muted) carries what needs no human: `-filed.md` run summaries, the
# 🤖 run links and the 📥 "note received" posts. Until the log channel exists and the bot role can see it, every
# post goes to the conversation channel as before (see find_log_channel).
CHANNEL_NAMES = CFG.channel_names
LOG_CHANNEL_NAME = CFG.log_channel_name
STATE_DIR = str(CFG.state_dir)
STATE_FILE = os.path.join(STATE_DIR, "state.json")
OUTBOUND_DIRS = ("briefings/daily/", "briefings/weekly/", "notify/")
# Questions for the owner @mention him (the owner, 2026-09-17: "add the @mention for questions"). The routine's Photobooth520
# question of 09:53 sat unseen among summaries and run links, because bot posts never notify. Only a notify/ file
# that IS a question pings; summaries, digests and run links stay silent so the ping keeps its meaning.
OWNER_DISCORD_ID = CFG.owner_discord_id


def ops_alert(text):
    """Failure alert to the optional ops webhook (flux.env OPS_WEBHOOK_URL). Best effort, never raises; without a
    webhook the log line is the alert."""
    if not CFG.ops_webhook:
        return
    try:
        requests.post(CFG.ops_webhook, json={"content": text}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        log(f"ops alert not sent ({exc.__class__.__name__})")


def is_question(path, body):
    """A notify/ file whose name says question, or whose text carries an Obsidian question callout."""
    return path.startswith("notify/") and ("question" in path.rsplit("/", 1)[-1] or "[!question]" in body)


def question_text(body):
    """Discord-readable question: drop the callout marker and the '> ' quoting Obsidian needs, keep the words."""
    lines = [ln[2:] if ln.startswith("> ") else (ln[1:] if ln.startswith(">") else ln) for ln in body.split("\n")]
    return "\n".join(lines).replace("[!question]", "").strip()


def is_run_summary(path):
    """The per-run `notify/<stamp>-filed.md` summary the routine writes (vault CLAUDE.md, run protocol): background
    information for the log channel. Everything else in notify/ was written for the owner to read or copy."""
    return path.startswith("notify/") and path.endswith("-filed.md")
MESSAGE_GIVE_UP = 3                # (2026-09-17 code review fix 2) strict filing attempts for ONE message before its
                                   # failing attachments are listed as not fetched and the queue moves on (file_guarded)
FAIL_ALERT_AFTER = 20              # consecutive failed runs before alerting: since 2026-09-15 the relay runs
                                   # every 15 s (vault-discord-relay-loop service), so 20 runs ~ 5 minutes
                                   # (was 3 at the old 5-minute cadence; 3 x 15 s would page on a GitHub blip)
# Start the vault-inbox routine as soon as a capture is filed (2026-09-15, the owner: "relay immediately").
# Per-routine API trigger token generated at claude.ai/code/routines, kept only in this mode-600 file.
FIRE_URL = CFG.fire_url
FIRE_MIN_INTERVAL = CFG.fire_min_interval            # after a start, wait this long for its run marker before another start (was a fixed
                                   # 300 s spacing until 2026-09-17 round 2; 20 of 21 runs that day pushed within 170 s)
# Run marker (2026-09-17, review round 2 P2a): every routine run pushes `.run/active` ("started: <UTC>") when it starts
# and removes it in its last commit (vault CLAUDE.md, Run protocol). While a fresh marker exists no start is sent, for
# fired AND scheduled runs, which replaced the clock-based quiet windows around the hour. A marker older than
# MARKER_FRESH is a crashed run's leftover and is ignored (runs overwrite it too).
RUN_MARKER = ".run/active"
MARKER_FRESH = CFG.marker_fresh
# Memory reconcile notes coalesce (review round 2 N4): a start whose only captures are reconcile notes waits
# RECONCILE_COALESCE after the latest reconcile change, at most RECONCILE_MAX after the first, so a Claude Code session's
# stream of memory edits is one run; the owner's own captures never wait for this and carry the reconcile notes along.
RECONCILE_NOTE = re.compile(r"^inbox/[^/]*Z-memory-reconcile-[a-z0-9-]+\.md$")
RECONCILE_COALESCE = 300
RECONCILE_MAX = 900
# New memory-proposals/ files start the memory applier at once (round 2) instead of waiting for its */5 cron. Same
# command and lock as the cron line, so the two never run together; the cron keeps the Kuma ping.
# memory module (v2): the applier runs detached under the same lock as its cron line, so the two never overlap
RECONCILE_CMD = ["flock", "-n", str(CFG.lock_dir / "flux-memory-reconcile.lock"), "flux-memory-reconcile", "apply"]
RECONCILE_LOG = str(CFG.log_dir / "memory-reconcile.log")
# Notes written in Obsidian (phone via GitSync, or laptop) also start the routine (2026-09-15, the owner: "add the
# obsidian notes trigger"). GitSync pushes while a note is still being typed (and Obsidian creates an EMPTY file on
# New note), so a note must stay unchanged for PHONE_SETTLE seconds before it counts. Notes the relay writes itself
# (inbox/<stamp>-<discord snowflake>.md) are excluded: they already started a run through `filed`.
PHONE_SETTLE = CFG.phone_settle
RELAY_NOTE = re.compile(r"^inbox/\d{4}-\d{2}-\d{2}T\d{4}Z-\d{17,20}\.md$")
# Notes other programs on this server write COMPLETE in one commit (2026-09-17, responsiveness review P1): the Keep sync
# (<stamp>-keep-<slug>.md, <stamp>-keep-note-<id>.md, already settled 90 s in Keep), the Gmail relay (<stamp>-gmail-<id>.md)
# and the memory reconcile (<stamp>-memory-reconcile-<slug>.md). Nobody is still typing them, so they start the routine
# on the tick they appear instead of waiting PHONE_SETTLE. Stamps: Keep %H%M%S, Gmail and reconcile %H%M.
HOST_NOTE = re.compile(r"^inbox/\d{4}-\d{2}-\d{2}T\d{4}(\d{2})?Z-(keep|gmail|memory-reconcile)-[^/]+\.md$")
MANIFEST_MAX = 20  # inbox paths listed in the start message (P8)
DISCORD = "https://discord.com/api/v10"
GITHUB = "https://api.github.com"
# Attachments go to Drive (shared drive "Vault" > "Claude"): vaultlib.drive.

# A capture matching these is NOT filed: the repo is synced to a phone and a laptop, so a pasted key
# would spread. Shared with memory-split.py (2026-09-15) so the two can never drift apart.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))





MAX_POST_CHUNKS = 5  # outbound files longer than ~9500 chars are cut after this many Discord posts


def chunk_lines(text, size):
    """Split text into pieces <= size, breaking between lines (hard-splitting only overlong lines)."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:size])
            line = line[size:]
        if cur and len(cur) + 1 + len(line) > size:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def load_state():
    return load_json(STATE_FILE, {"channel_id": None, "last_message_id": None, "posted": [], "failures": 0})


def save_state(state):
    save_json(STATE_FILE, state)  # atomic (vaultlib.state), so a crash never leaves half a state file


def prune_posted(posted, tree_paths, cap=2000):
    """The `posted` list to keep (code review item 8). Paths gone from the repo are dropped (they cannot be posted again),
    then under `cap` the newest per top-level dir are kept: the old `sorted(posted)[-2000:]` was lexicographic, so under
    pressure every briefings/ entry was evicted before any notify/ one and old briefings would have been re-posted."""
    keep = sorted(p for p in posted if p in tree_paths)
    if len(keep) <= cap:
        return keep
    groups = {}
    for p in keep:
        groups.setdefault(p.split("/", 1)[0], []).append(p)
    share = max(1, cap // len(groups))
    return sorted(p for g in groups.values() for p in g[-share:])


def describe_inbox(path, hint=None, leave=False):
    """One manifest entry for the start message (review P8): the path, plus what kind of capture its name says it is.
    Round 2: the path is wrapped in backticks with control characters and backticks removed (Obsidian note titles come
    from the phone), `hint` adds the page and its memory files (N7b), `leave` marks a note the owner is still typing (N1)."""
    shown = "`" + re.sub(r"[\x00-\x1f`]", "", path) + "`"
    if leave:
        return f"{shown} (Obsidian note, still being edited: leave it)"
    mt = re.match(r"^inbox/[^/]*Z-(keep-note|keep|gmail|memory-reconcile)-([^/]+)\.md$", path)
    if RELAY_NOTE.match(path):
        return f"{shown} (Discord)"
    if not mt:
        return f"{shown} (Obsidian note)"
    kind, rest = mt.groups()
    text = {"keep-note": "Keep note", "keep": f"Keep checklist edit, project {rest}", "gmail": "Gmail",
            "memory-reconcile": f"memory reconcile, project {rest}"}[kind]
    return f"{shown} ({text}{'; ' + hint if hint else ''})"


class Relay:
    def __init__(self, state):
        self.state = state
        self.s = session(retry_writes=True)
        CFG.require("discord_token", "discord_guild")
        self.guild = CFG.discord_guild
        self.dh = {"Authorization": f"Bot {CFG.discord_token}", "User-Agent": "flux-relay (flux-brain, 1.0)"}
        # tree-cache.json: the branch tip is checked with a conditional GET each tick and the recursive tree is
        # re-fetched only when it moved (item 7); this process is per-run, so the cache lives on disk.
        self.ghc = GitHub(retry_writes=True, cache_file=os.path.join(STATE_DIR, "tree-cache.json"))
        self.drive = Drive(self.s)

    # ---------- Discord ----------
    def discord(self, method, path, **kw):
        r = self.s.request(method, DISCORD + path, headers=self.dh, timeout=30, **kw)
        return r

    def find_channel(self):
        if self.state.get("channel_id"):
            return self.state["channel_id"]
        r = self.discord("GET", f"/guilds/{self.guild}/channels")
        r.raise_for_status()
        for c in r.json():
            if c["type"] == 0 and c["name"] in CHANNEL_NAMES:
                self.state["channel_id"] = c["id"]
                return c["id"]
        return None  # channel not created yet, or bot role not granted on it

    def find_log_channel(self):
        """Id of the log channel, or None while it does not exist (or the bot role cannot see it): callers then
        fall back to the conversation channel, so the split is opt-in by creating the channel. The id is cached in
        state once found; the miss is logged once (`log_channel_missing`), not every 15 s tick."""
        st = self.state
        if st.get("log_channel_id"):
            return st["log_channel_id"]
        r = self.discord("GET", f"/guilds/{self.guild}/channels")
        r.raise_for_status()
        for c in r.json():
            if c["type"] == 0 and c["name"] == LOG_CHANNEL_NAME:
                st["log_channel_id"] = c["id"]
                st.pop("log_channel_missing", None)
                log(f"log channel #{LOG_CHANNEL_NAME} found ({c['id']}): summaries, run links and received posts go there now")
                return c["id"]
        if not st.get("log_channel_missing"):
            st["log_channel_missing"] = True
            log(f"#{LOG_CHANNEL_NAME} not visible to the bot; everything posts to the conversation channel until it is")
        return None

    def log_target(self, channel):
        """Channel for a post that needs no human: the log channel when main() found one, else `channel`."""
        return getattr(self, "log_channel", None) or channel

    def post(self, channel, content, reply_to=None, key=None, mention_user=None):
        """Create one message. Not through self.s (2026-09-16, code review of the run-link post): that session
        retries POSTs on 5xx, and a 5xx can come back AFTER Discord created the message, so every retry could
        duplicate it. Here a POST is sent once, except after a 429 (Discord did NOT create the message, so a
        second try is safe). `key` names the message stably (e.g. "notify/x.md#0"): it becomes a Discord nonce
        with enforce_nonce, so if a later tick sends the same message again after a lost reply, Discord returns
        the existing message instead of creating a second one (uniqueness window: a few minutes)."""
        body = {"content": content[:2000], "allowed_mentions": {"parse": []}}
        if mention_user:  # ping exactly this user and nobody else (never @everyone or roles)
            body["allowed_mentions"]["users"] = [mention_user]
        if reply_to:
            body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        if key:
            body["nonce"] = hashlib.sha1(key.encode()).hexdigest()[:25]  # Discord caps a nonce at 25 chars
            body["enforce_nonce"] = True
        for attempt in (1, 2):
            r = requests.post(f"{DISCORD}/channels/{channel}/messages", headers=self.dh, json=body, timeout=30)
            if r.status_code == 429 and attempt == 1:
                try:
                    wait = float(r.json().get("retry_after", 5))
                except ValueError:
                    wait = 5.0
                if wait <= 10:  # short per-route limit (5 posts / 5 s): wait it out; longer ones fail the tick
                    time.sleep(wait)
                    continue
            break
        r.raise_for_status()

    def seen(self, channel, message_id):
        """React 👀 as soon as a message with attachments is picked up (2026-09-17, review P9a): download, Drive upload
        and conversion take 30-50 s before the ✅, so this is the first sign the relay has it. Best effort only: a
        failure here must never stop the filing (a raise would retry the whole message next tick), and no reply is
        posted on 403 because ack() already falls back to a reply for the ✅."""
        try:
            emoji = urllib.parse.quote("👀")
            self.discord("PUT", f"/channels/{channel}/messages/{message_id}/reactions/{emoji}/@me")
        except Exception as exc:  # noqa: BLE001
            log(f"seen reaction not added ({exc.__class__.__name__})")

    def unseen(self, channel, message_id):
        """Remove the 👀 once ✅ is on (best effort, same reasons as seen())."""
        try:
            emoji = urllib.parse.quote("👀")
            self.discord("DELETE", f"/channels/{channel}/messages/{message_id}/reactions/{emoji}/@me")
        except Exception as exc:  # noqa: BLE001
            log(f"seen reaction not removed ({exc.__class__.__name__})")

    def ack(self, channel, message_id, fallback):
        emoji = urllib.parse.quote("✅")
        r = self.discord("PUT", f"/channels/{channel}/messages/{message_id}/reactions/{emoji}/@me")
        if r.status_code == 403:  # Add Reactions not granted on the private channel: reply instead
            self.post(channel, fallback, reply_to=message_id, key=f"ack-{message_id}")
        elif not r.ok:
            r.raise_for_status()

    # ---------- GitHub + Drive (vaultlib since 2026-09-18; method names kept for the tests and the callers) ----------
    def put_file(self, path, data: bytes, message):
        self.ghc.put_file(path, data, message)  # an existing file is kept (re-run after a crash)

    def tree(self):
        return self.ghc.tree()  # conditional: re-fetched only when the branch tip moved (item 7)

    def blob_text(self, sha):
        return self.ghc.blob_text(sha)

    def drive_upload(self, name, data: bytes, mime):
        return self.drive.upload(name, data, mime)

    # ---------- inbound ----------
    def inbound(self, channel):
        if not self.state.get("last_message_id"):
            # First sight of the channel: start from "now" so old history is not ingested.
            r = self.discord("GET", f"/channels/{channel}/messages", params={"limit": 1})
            r.raise_for_status()
            msgs = r.json()
            self.state["last_message_id"] = msgs[0]["id"] if msgs else "0"
            log(f"inbound initialised at message {self.state['last_message_id']}")
            return 0
        filed = 0
        while True:
            r = self.discord("GET", f"/channels/{channel}/messages",
                             params={"after": self.state["last_message_id"], "limit": 100})
            if r.status_code == 403:
                raise RuntimeError(f"bot cannot read #{CHANNEL_NAMES[0]}: grant the bot's role View Channel + Send Messages on it (INSTALL.md)")
            r.raise_for_status()
            msgs = sorted(r.json(), key=lambda m: int(m["id"]))  # API returns newest first
            if not msgs:
                return filed
            for m in msgs:
                if not m["author"].get("bot") and m.get("type") in (0, 19):  # default + reply only
                    filed += self.file_guarded(channel, m)  # per-message failure count, never blocks the queue for good (fix 2)
                self.state["last_message_id"] = m["id"]
                save_state(self.state)  # advance per message so a crash never files twice

    def file_guarded(self, channel, m):
        """file_message with a per-message failure count (2026-09-17 code review fix 2: head-of-line blocking).
        inbound() advances last_message_id only after a message is filed, so a message whose filing kept raising (an
        expired attachment URL after a long outage, Drive refusing a file, a GitHub 4xx on the text file) was retried
        every tick FOREVER, one alert at FAIL_ALERT_AFTER and then silence, and nothing newer was ever filed. Now the
        first MESSAGE_GIVE_UP-1 attempts stay strict (a transient blip must not file a note without its attachment); from
        attempt MESSAGE_GIVE_UP on the message is filed leniently: each attachment that still fails is listed in the note
        as not fetched, the owner gets a reply saying so, and the queue moves on. State: `message_failures` {id: attempts},
        cleared once the message is filed (only the head of the queue can ever hold an entry)."""
        fails = self.state.setdefault("message_failures", {})
        n = fails.get(m["id"], 0)
        try:
            filed = self.file_message(channel, m, lenient=n + 1 >= MESSAGE_GIVE_UP)
        except Exception as exc:
            fails[m["id"]] = n + 1
            save_state(self.state)
            log(f"message {m['id']} not filed, attempt {n + 1}/{MESSAGE_GIVE_UP}: {exc.__class__.__name__}")
            raise
        fails.pop(m["id"], None)
        return filed

    def attachment_entry(self, m, a, name, stamp, ts):
        """One `## Attachments` line for the note: download, Drive upload, text extraction, text file in GitHub.
        Raises when the download, the upload or the text-file put fails (file_guarded decides what that means);
        an extraction failure is recorded in the line, never raised (the capture still lands)."""
        dl = self.s.get(a["url"], timeout=120)
        dl.raise_for_status()
        # Drive instead of git (2026-09-15): keeps binary history out of the repo.
        drive_name = f"{stamp}-{m['id']}-{name}"
        link = self.drive_upload(drive_name, dl.content, a.get("content_type") or "application/octet-stream")
        kb = max(1, a.get("size", 0) // 1024)
        mime = a.get("content_type") or "application/octet-stream"
        entry = f"- [{name}]({link}) ({mime}, {kb} KB, original in Google Drive)"
        # att_text, NOT text: `text` holds the Discord message itself (bug caught in testing
        # 2026-09-15, the note body was being replaced by the attachment's text).
        try:
            att_text, method = extract_text(dl.content, a.get("content_type") or "", name)
        except Exception as exc:  # noqa: BLE001 - encrypted PDF, timeout, corrupt file...
            att_text, method = "", f"extraction failed: {exc.__class__.__name__}"
        if method:
            # 2026-09-15: the text lives in its own file in GitHub (one per attachment) instead
            # of inline in the note, so notes stay short and texts are reusable.
            text_path = f"raw/attachments/{drive_name}.md"
            self.put_file(text_path, attachment_text_file(
                name, link, mime, kb, method, att_text, m["id"], f"{ts:%Y-%m-%dT%H:%MZ}").encode(),
                f"inbox: text of attachment {name}")
            entry += f", text: [[{text_path}|{name} (text)]]"
        else:
            entry += ", no text (type not converted)"
        return entry

    def file_message(self, channel, m, lenient=False):
        """File one Discord message as an inbox capture. `lenient` (fix 2, set by file_guarded on the last attempt):
        an attachment that fails is listed as not fetched instead of failing the message."""
        text = m.get("content", "")
        if SECRET_PATTERNS.search(text):
            self.post(channel, "⛔ Not filed: this looks like it contains a secret (key, token or webhook). "
                               "Delete the message and post it again without the secret.", reply_to=m["id"],
                      key=f"refused-{m['id']}")
            log(f"refused message {m['id']} (secret pattern)")
            return 0
        ts = datetime.fromisoformat(m["timestamp"]).astimezone(timezone.utc)
        stamp = ts.strftime("%Y-%m-%dT%H%MZ")
        lines, not_fetched = [], []
        if m.get("attachments"):
            self.seen(channel, m["id"])  # P9a: immediate signal before the slow part
        for a in m.get("attachments", []):
            name = re.sub(r"[^A-Za-z0-9._-]", "_", a["filename"])
            if a.get("size", 0) > MAX_ATTACHMENT:
                lines.append(f"- attachment `{name}` skipped (over 20 MB)")
                continue
            try:
                lines.append(self.attachment_entry(m, a, name, stamp, ts))
            except Exception as exc:  # noqa: BLE001 - download, Drive or GitHub failure
                if not lenient:
                    raise  # strict attempt: retried next tick, the note must not land without its attachment
                not_fetched.append(f"{name} ({exc.__class__.__name__})")
                lines.append(f"- attachment `{name}` NOT fetched or stored after {MESSAGE_GIVE_UP} attempts "
                             f"({exc.__class__.__name__}); it is still on the Discord message, ask the owner to re-post it if it matters")
        refs = ""
        if m.get("referenced_message"):  # a reply, e.g. answering a question Claude posted
            refs = "\nin_reply_to: |\n  " + m["referenced_message"].get("content", "")[:300].replace("\n", "\n  ")
        note = (f"---\nsource: discord\nmessage_id: \"{m['id']}\"\ncaptured: {ts:%Y-%m-%dT%H:%MZ}{refs}\n---\n\n"
                f"{text}\n" + ("\n## Attachments\n" + "\n".join(lines) + "\n" if lines else ""))
        self.put_file(f"inbox/{stamp}-{m['id']}.md", note.encode(), "inbox: 1 capture from Discord")
        self.ack(channel, m["id"], "📥 Filed to inbox.")
        if m.get("attachments"):
            self.unseen(channel, m["id"])
        if not_fetched:
            # Best effort, like the 👀 reaction: the capture is filed either way, and this reply is the only place
            # the owner learns that a file did not make it (the note is read by the routine, not by him).
            try:
                self.post(channel, f"⚠️ Filed, but {len(not_fetched)} attachment(s) could not be processed after "
                                   f"{MESSAGE_GIVE_UP} attempts: {', '.join(not_fetched)}. Re-post the file if it matters.",
                          reply_to=m["id"], key=f"degraded-{m['id']}")
            except Exception as exc:  # noqa: BLE001
                log(f"degraded-filing reply not posted ({exc.__class__.__name__})")
        log(f"filed message {m['id']} ({len(lines)} attachment(s)"
            + (f", {len(not_fetched)} not fetched" if not_fetched else "") + ")")
        return 1

    # ---------- notes written in Obsidian ----------
    def watch_obsidian_notes(self, tree, now=None, channel=None):
        """Mark a routine start as pending once a new or edited inbox note from Obsidian has settled.
        Notes written by the server's own programs (HOST_NOTE) make a start pending on the same tick (2026-09-17, review
        P1). Since round 2 they no longer wait for a typed note that is still settling: the start lists that note as
        "still being edited: leave it" instead (N1), and reconcile notes get a coalescing timer (N4).
        A NEW typed note gets one "received" post in #vault when `channel` is given (review P9c).
        Uses the tree outbound() needs anyway (no extra GitHub call). State: `obsidian_notes` {path: sha} as of
        the last tick, `obsidian_pending` paths waiting to settle, `obsidian_fire_at` when they count."""
        st = self.state
        now = time.time() if now is None else now  # parameter only so the offline test can move the clock
        # size > 0: Obsidian's empty New-note file is not a capture yet; when text arrives its sha changes
        cur = {e["path"]: e["sha"] for e in tree
               if e["type"] == "blob" and e["path"].startswith("inbox/") and e["path"].endswith(".md")
               and not RELAY_NOTE.match(e["path"]) and e.get("size", 1) > 0}
        if "obsidian_notes" not in st:
            # First run with this feature: take what is already there as seen, never fire for old notes
            st["obsidian_notes"] = cur
            log(f"obsidian watch initialised with {len(cur)} inbox note(s)")
            save_state(st)
            return
        changed = [p for p, sha in cur.items() if st["obsidian_notes"].get(p) != sha]
        dirty = bool(changed)
        host = [p for p in changed if HOST_NOTE.match(p)]
        typed = [p for p in changed if not HOST_NOTE.match(p)]
        if typed:
            # Every further push restarts the settle timer, so a note still being typed keeps waiting
            st["obsidian_pending"] = sorted(set(st.get("obsidian_pending", [])) | set(typed))
            st["obsidian_fire_at"] = now + PHONE_SETTLE
            log(f"obsidian note(s) changed, start in {PHONE_SETTLE}s if unchanged: {', '.join(typed)}")
            for path in typed:
                if path not in st["obsidian_notes"] and channel:
                    # First sight of a typed note (empty New-note files are excluded above, so this is the first push
                    # with text). One post per note version key, never retried: a failure only loses the courtesy.
                    try:
                        name = path.split("/", 1)[1]
                        # log channel since 2026-09-22: a courtesy notice, nothing for the owner to do
                        self.post(self.log_target(channel), f"📥 Obsidian note \"{name}\" received; Claude starts on it about "
                                  f"{PHONE_SETTLE} seconds after your last sync.", key=f"recv-{path}@{cur[path][:10]}")
                    except Exception as exc:  # noqa: BLE001
                        log(f"received post failed ({exc.__class__.__name__})")
        if host:
            st["fire_pending"] = True
            if any(RECONCILE_NOTE.match(p) for p in host):
                first = st.get("reconcile_first") or now
                st["reconcile_first"] = first
                st["reconcile_hold_until"] = min(now + RECONCILE_COALESCE, first + RECONCILE_MAX)
            log(f"server note(s) changed, start pending: {', '.join(host)}")
        if st.get("obsidian_fire_at") and now >= st["obsidian_fire_at"]:
            # Only if a pending note is still in inbox/: an hourly run may have filed it meanwhile
            if any(p in cur for p in st.get("obsidian_pending", [])):
                st["fire_pending"] = True
                log("obsidian note(s) settled, starting vault-inbox")
            st.pop("obsidian_fire_at")
            st.pop("obsidian_pending", None)
            dirty = True
        if st["obsidian_notes"] != cur:
            st["obsidian_notes"] = cur
            dirty = True
        if dirty:
            save_state(st)

    # ---------- start the Claude routine right away ----------
    def maybe_fire(self, filed, channel=None, tree=None):
        """Start vault-inbox once captures are filed, instead of waiting for its hourly schedule.
        Deliberately a plain one-shot requests.post, NOT self.s: that session retries POSTs, and the
        fire endpoint has no idempotency key, so a retry would start duplicate runs. Every failure
        leaves the captures for the hourly run, so nothing here may raise.
        `tree` (this tick's repo tree, fetched after inbound) lets it skip a start when inbox/ is already empty and
        list what is waiting in the start message (2026-09-17, review P7/P8); without it the old behaviour holds.
        Round 2 (2026-09-17): no start while a run marker is fresh (P2a, which replaced the quiet windows around the
        hour), a 180 s ceiling after our own start until its marker shows, notes the owner is still typing are listed as
        "leave it" instead of holding every start (N1), and reconcile-only starts coalesce (N4)."""
        st = self.state
        if filed:
            st["fire_pending"] = True
        now = time.time()
        if tree is not None:
            self.track_run_marker(tree, now)  # may re-arm a start for captures a finished run left behind
        if not st.get("fire_pending") or not (FIRE_URL and CFG.fire_token):
            return
        if now < st.get("fire_not_before", 0):
            return  # Retry-After or an error backoff still running; fire on a later tick
        if st.get("run_active"):
            return  # a run (fired or scheduled) is working: start the next one when its marker is gone
        if now < st.get("fire_ceiling_until", 0):
            return  # our last start has not shown its marker yet: do not start a second run on top of it

        waiting, settling = None, set()
        if tree is not None:
            waiting = sorted(e["path"] for e in tree if e["type"] == "blob" and e["path"].startswith("inbox/")
                             and e["path"].endswith(".md") and e.get("size", 1) > 0)
            if not waiting and not filed:
                # Everything already filed (by the scheduled run, or the previous fired one): a start would be an
                # empty run. `not filed`: a capture written this tick is in the tree anyway, but never risk dropping it.
                st["fire_pending"] = False
                log("fire: skipped, nothing left in inbox/")
                save_state(st)
                return
            if st.get("obsidian_fire_at"):
                settling = set(st.get("obsidian_pending", [])) & set(waiting)
            others = [p for p in waiting if p not in settling]
            if not others and not filed:
                return  # only notes the owner is still typing: their settle makes the start pending again when it ends
            if (not filed and others and all(RECONCILE_NOTE.match(p) for p in others)
                    and now < st.get("reconcile_hold_until", 0)):
                return  # memory reconcile notes only: let a burst of memory edits become one run
        elif st.get("obsidian_fire_at"):
            return  # no tree to tell notes apart: keep the conservative hold

        headers = {"Authorization": f"Bearer {CFG.fire_token}", "anthropic-version": "2023-06-01",
                   "anthropic-beta": "experimental-cc-routine-2026-04-01", "Content-Type": "application/json"}
        body = {"text": "The Discord relay just filed new capture(s) in inbox/. Run the normal inbox run."}
        if waiting:
            # P8: paths only (never contents), so the run does not spend its first tool calls discovering them
            hints = self.page_hints(waiting[:MANIFEST_MAX], tree)
            body["text"] += " Inbox now holds: " + "; ".join(
                describe_inbox(p, hints.get(p), p in settling) for p in waiting[:MANIFEST_MAX])
            if len(waiting) > MANIFEST_MAX:
                body["text"] += f"; and {len(waiting) - MANIFEST_MAX} more"
            body["text"] += "."
        try:
            r = requests.post(FIRE_URL, headers=headers, json=body, timeout=30)
        except requests.RequestException as exc:
            st["fire_not_before"] = now + FIRE_MIN_INTERVAL
            log(f"fire: network error ({exc.__class__.__name__}), will retry in {FIRE_MIN_INTERVAL}s")
            save_state(st)
            return
        if r.ok:
            st.update(fire_pending=False, fire_ceiling_until=now + FIRE_MIN_INTERVAL, fire_alerted=False,
                      fired_inbox=[p for p in (waiting or []) if p not in settling])
            st.pop("reconcile_first", None)
            st.pop("reconcile_hold_until", None)
            # Persist the start BEFORE anything slow: if the process dies after this point, the next tick must
            # not see fire_pending=True and start a duplicate run (code review of b08ec02, 2026-09-16).
            save_state(st)
            url = r.json().get('claude_code_session_url')
            log(f"fired vault-inbox: {url or '?'}")
            # Post the run link to #vault (2026-09-16, the owner: "see Claude step by step work in Discord"): the
            # cloud run cannot reach Discord itself, so the live step-by-step view is the claude.ai page this
            # links to. <...> suppresses Discord's link preview. self.post sends it once (no 5xx retries, see its
            # docstring). A failed post never undoes the start and is not retried; the log line keeps the link.
            if url and channel:
                try:  # log channel since 2026-09-22: the link is background information, not a question
                    target = self.log_target(channel)
                    self.post(target, f"🤖 Claude started on your capture(s), watch it work: <{url}>", key=f"run-{url}")
                    log(f"run link posted -> {'log' if target != channel else 'conversation'} channel")
                except Exception as exc:  # noqa: BLE001 - never raise out of maybe_fire
                    log(f"fire: run link not posted to Discord ({exc.__class__.__name__})")
        elif r.status_code == 429:  # daily run cap or usage limit: wait as told, the hourly run still exists
            try:
                wait = int(r.headers.get("Retry-After", "3600"))
            except ValueError:
                wait = 3600
            st["fire_not_before"] = now + max(wait, FIRE_MIN_INTERVAL)
            log(f"fire: 429 rate limited, next attempt in {wait}s")
        elif r.status_code >= 500:
            st["fire_not_before"] = now + FIRE_MIN_INTERVAL
            log(f"fire: HTTP {r.status_code}, will retry in {FIRE_MIN_INTERVAL}s")
        else:
            # 400 (routine paused / beta header changed), 401 (token revoked or regenerated), 403, 404:
            # does not heal by itself. Drop the pending start, alert ONCE, try again in an hour.
            st.update(fire_pending=False, fire_not_before=now + 3600)
            log(f"fire: HTTP {r.status_code} {r.text[:200]}")
            if not st.get("fire_alerted"):
                ops_alert(f"⚠️ Flux relay could not start the vault-inbox routine (HTTP {r.status_code}). "
                          "Captures still get filed by the hourly run. If the API token was revoked or "
                          "regenerated, put the new one in flux.env (ROUTINE_FIRE_TOKEN).")
                st["fire_alerted"] = True
        save_state(st)

    def track_run_marker(self, tree, now):
        """Follow the routine's `.run/active` marker (round 2 P2a). Sets state `run_active` while a fresh marker exists;
        when it goes away, clears the start ceiling and re-arms a start ONCE per capture that our last start listed and
        that is still in inbox/ (the run may have ended at once because another run was working). Never raises."""
        st = self.state
        try:
            ent = next((e for e in tree if e["type"] == "blob" and e["path"] == RUN_MARKER), None)
            active = False
            if ent:
                mk = st.get("marker") or {}
                if mk.get("sha") != ent["sha"]:
                    started = None
                    try:
                        mt = re.search(r"started:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z", self.blob_text(ent["sha"]))
                        if mt:
                            started = datetime.strptime(mt.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                    except Exception:  # noqa: BLE001 - unreadable marker: age it from first sight
                        started = None
                    mk = st["marker"] = {"sha": ent["sha"], "started": started or now}
                active = now - mk["started"] < MARKER_FRESH
            else:
                st.pop("marker", None)
            if active and not st.get("run_active"):
                st["run_active"] = True
                log(f"run marker seen (started {datetime.fromtimestamp(st['marker']['started'], timezone.utc):%H:%M:%S}Z)")
                save_state(st)
            elif not active and st.get("run_active"):
                st.pop("run_active")
                st.pop("fire_ceiling_until", None)
                inbox = {e["path"] for e in tree if e["type"] == "blob" and e["path"].startswith("inbox/")}
                done = set(st.get("rearmed", []))
                left = [p for p in st.get("fired_inbox", []) if p in inbox and p not in done]
                if left:
                    st["fire_pending"] = True
                    st["rearmed"] = (st.get("rearmed", []) + left)[-200:]
                    log(f"run finished, capture(s) still waiting, start pending: {', '.join(left)}")
                else:
                    log("run finished (marker removed)")
                save_state(st)
        except Exception as exc:  # noqa: BLE001 - a marker problem must never stop filing or posting
            log(f"run marker check failed ({exc.__class__.__name__})")

    def page_hints(self, paths, tree):
        """{inbox path: "page wiki/projects/<slug>.md, memory: a.md, b.md"} for Keep checklist and memory reconcile
        notes (round 2 N7b), so the run opens the right memory files at once. Best effort: any failure means no hint."""
        out = {}
        try:
            shas = {e["path"]: e["sha"] for e in tree if e["type"] == "blob"}
            for path in paths:
                if not re.search(r"Z-(keep|memory-reconcile)-[a-z0-9-]+\.md$", path) or "-keep-note-" in path:
                    continue
                note = self.blob_text(shas[path])
                mt = re.search(r"^(?:project|page):\s*(?:wiki/projects/)?([a-z0-9-]+?)(?:\.md)?\s*$", note, re.M)
                page = f"wiki/projects/{mt.group(1)}.md" if mt else None
                if not page or page not in shas:
                    continue
                mm = re.search(r"^memory:\s*\[(.*)\]\s*$", self.blob_text(shas[page]), re.M)
                files = ", ".join(f.strip().strip("'\"") for f in mm.group(1).split(",") if f.strip()) if mm else ""
                out[path] = f"page {page}" + (f", memory: {files}" if files else "")
        except Exception as exc:  # noqa: BLE001
            log(f"page hints skipped ({exc.__class__.__name__})")
        return out

    def trigger_apply(self, tree):
        """Start the memory applier when a new memory-proposals/ file appears (round 2), detached and under the cron's
        own lock, so this 15 s tick never waits for it. Never raises."""
        st = self.state
        if not CFG.mod_memory:
            return  # memory module off (v1 default): proposals are never written, nothing to apply
        try:
            props = sorted(e["path"] for e in tree if e["type"] == "blob"
                           and e["path"].startswith("memory-proposals/") and e["path"].endswith(".md"))
            if "proposals_seen" not in st:
                st["proposals_seen"] = props  # first run with this feature: existing proposals are the cron's
                save_state(st)
                return
            seen = set(st["proposals_seen"])
            new = [p for p in props if p not in seen]
            if new:
                with open(RECONCILE_LOG, "ab") as out:
                    subprocess.Popen(RECONCILE_CMD, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     start_new_session=True, close_fds=True)
                log(f"memory proposal(s) seen, applier started: {', '.join(new)}")
            if new or len(props) != len(seen):
                st["proposals_seen"] = props[-1000:]
                save_state(st)
        except Exception as exc:  # noqa: BLE001
            log(f"applier start failed ({exc.__class__.__name__})")

    # ---------- outbound ----------
    def outbound(self, channel, tree=None):
        # tree passed in by main() since 2026-09-15 (shared with watch_obsidian_notes, one GitHub call per tick)
        tree = tree if tree is not None else self.tree()
        posted = set(self.state.get("posted", []))
        # Drop resume counters for files that are gone or already fully posted (deleted mid-post, say)
        pending_paths = {e["path"] for e in tree if e["path"] not in posted}
        for path in [p for p in self.state.get("posting", {}) if p not in pending_paths]:
            self.state["posting"].pop(path)
        new = sorted((e for e in tree if e["type"] == "blob"
                      and e["path"].startswith(OUTBOUND_DIRS) and e["path"].endswith(".md")
                      and e["path"] not in posted), key=lambda e: e["path"])
        for e in new:
            body = self.blob_text(e["sha"]).strip()
            link = f"https://github.com/{REPO}/blob/{BRANCH}/{urllib.parse.quote(e['path'])}"
            head = "📰" if e["path"].startswith("briefings/") else "💬"
            # Discord caps a message at 2000 chars. Split on line boundaries into several posts
            # (2026-09-15: full drafts must arrive whole, the owner copies them from Discord); only past
            # MAX_POST_CHUNKS is the tail cut, with a link to the page.
            question = is_question(e["path"], body)
            if question:  # no file-name header: the mention and the question itself are what the owner sees
                parts = chunk_lines(f"<@{OWNER_DISCORD_ID}> ❓ {question_text(body)}", 1900)
            else:
                parts = chunk_lines(f"{head} **{e['path']}**\n{body}", 1900)
            # Routing (2026-09-22): run summaries are background -> log channel; questions, answers, drafts and the
            # digests (the owner's choice: "digest to vault channel") stay in the conversation channel.
            target = self.log_target(channel) if is_run_summary(e["path"]) else channel
            if len(parts) > MAX_POST_CHUNKS:
                parts = parts[:MAX_POST_CHUNKS]
                parts[-1] = parts[-1][:1800] + f"\n… (continued: <{link}>)"
            # Resume where a failed tick stopped (2026-09-16): a post error now fails the tick instead of being
            # retried inside the session, so remember how many parts of this file are already in #vault. Each part
            # also carries a stable key, so a part whose reply was lost is not duplicated when it is sent again.
            # The counter is tied to the file VERSION (blob sha; code review of 74c4123): if the file changed
            # after a partial post, start again from part 0 so the new version arrives whole and in order (the old
            # version's partial parts stay in #vault above it; they are not deleted), and the sha in the key keeps
            # Discord's nonce check from handing back a part of the old version.
            progress = self.state.setdefault("posting", {})
            rec = progress.get(e["path"])
            if not isinstance(rec, dict) or rec.get("sha") != e["sha"]:
                if rec is not None:
                    log(f"{e['path']} changed after a partial post: posting it again from the start")
                rec = progress[e["path"]] = {"sha": e["sha"], "done": 0}
            for i, part in enumerate(parts):
                if i < rec["done"]:
                    continue
                self.post(target, part, key=f"{e['path']}@{e['sha'][:10]}#{i}",
                          mention_user=OWNER_DISCORD_ID if question and i == 0 else None)
                rec["done"] = i + 1
                save_state(self.state)
            progress.pop(e["path"], None)
            posted.add(e["path"])
            self.state["posted"] = prune_posted(posted, {t["path"] for t in tree})
            save_state(self.state)
            # name the channel (2026-09-22): the only proof of the two-channel routing outside Discord itself
            log(f"posted {e['path']} -> {'log' if target != channel else 'conversation'} channel")


def main():
    state = load_state()
    try:
        relay = Relay(state)
        channel = relay.find_channel()
        if not channel:
            log(f"#{' / #'.join(CHANNEL_NAMES)} not visible to the bot yet; nothing to do")
            save_state(state)
            return 0
        relay.log_channel = relay.find_log_channel()  # None until #<LOG_CHANNEL_NAME> exists: then log_target() = channel
        filed = relay.inbound(channel)
        tree = relay.tree()  # after inbound, so this tick's Discord notes are in it (and excluded by RELAY_NOTE)
        relay.watch_obsidian_notes(tree, channel=channel)  # may set fire_pending (typed notes after settling, server notes now)
        relay.trigger_apply(tree)  # new memory proposals start the applier now instead of at its next cron tick
        relay.maybe_fire(filed, channel, tree)  # never raises: a failed start leaves captures for the hourly run
        relay.outbound(channel, tree)
        state["failures"] = 0
        save_state(state)
        return 0
    except Exception as exc:  # noqa: BLE001 - one alert path for every failure mode
        state["failures"] = state.get("failures", 0) + 1
        save_state(state)
        log(f"ERROR ({state['failures']} in a row): {exc}")
        if state["failures"] == FAIL_ALERT_AFTER:  # alert once per outage, not every 5 minutes
            ops_alert(f"❌ Flux relay failing {FAIL_ALERT_AFTER} runs in a row: {exc}"[:1800])
        return 1


if __name__ == "__main__":
    sys.exit(main())
