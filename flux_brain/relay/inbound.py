"""Inbound: new human messages in the conversation channel become inbox/ captures (attachments through Drive and the
text extractors), with a per-message failure count so one bad message never blocks the queue for good."""
import re
from datetime import datetime, timezone

from ..config import CFG
from ..lib.common import log
from ..lib.secrets import SECRET_PATTERNS
from ..lib.extract import extract_text, attachment_text_file   # (tests stub extract_text on this module)
from . import state

MESSAGE_GIVE_UP = 3                # (2026-09-17 code review fix 2) strict filing attempts for ONE message before its
                                   # failing attachments are listed as not fetched and the queue moves on (file_guarded)
# Attachments go to cloud storage when the Drive module is on: flux_brain.lib.drive.
# A capture matching SECRET_PATTERNS is NOT filed: the repo is synced to a phone and a laptop, so a pasted key
# would spread (the one definition: flux_brain.lib.secrets).


class InboundMixin:
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
                raise RuntimeError(f"bot cannot read #{CFG.channel_names[0]}: grant the bot's role View Channel + Send Messages on it (INSTALL.md)")
            r.raise_for_status()
            msgs = sorted(r.json(), key=lambda m: int(m["id"]))  # API returns newest first
            if not msgs:
                return filed
            for m in msgs:
                if not m["author"].get("bot") and m.get("type") in (0, 19):  # default + reply only
                    filed += self.file_guarded(channel, m)  # per-message failure count, never blocks the queue for good (fix 2)
                self.state["last_message_id"] = m["id"]
                state.save_state(self.state)  # advance per message so a crash never files twice

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
            state.save_state(self.state)
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
        # The original goes to cloud storage instead of git (keeps binary history out of the repo) when the Drive
        # module is on; without it the note links the Discord copy, which Discord expires after some weeks, so the
        # text file below is the lasting part.
        drive_name = f"{stamp}-{m['id']}-{name}"
        kb = max(1, a.get("size", 0) // 1024)
        mime = a.get("content_type") or "application/octet-stream"
        if CFG.mod_drive:
            link = self.drive_upload(drive_name, dl.content, mime)
            entry = f"- [{name}]({link}) ({mime}, {kb} KB, original in Google Drive)"
        else:
            link = a["url"]
            entry = f"- [{name}]({link}) ({mime}, {kb} KB, Discord copy, expires; text below is kept)"
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
            if a.get("size", 0) > CFG.max_attachment_bytes:
                lines.append(f"- attachment `{name}` skipped (over {CFG.max_attachment_mb} MB)")
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
