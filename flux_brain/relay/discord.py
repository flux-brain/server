"""Discord side of the relay: the bot session, the two channels and the one-shot post with a nonce."""
import hashlib
import time
import urllib.parse

import requests

from ..config import CFG
from ..lib.common import log

DISCORD = "https://discord.com/api/v10"
# Two channels since 2026-09-22 (the owner: "separate human questions and background info"; a run summary posted right
# after his reply to a question was noise, and a summary landing after a question made him reply to the wrong
# message). The CONVERSATION channel (`CFG.channel_names`, first name found wins; the id is cached in state so a rename
# in Discord changes nothing) carries his captures, ❓ questions, answers/drafts and the daily/weekly digests. The
# LOG channel (`CFG.log_channel_name`, meant to be muted) carries what needs no human: `-filed.md` run summaries, the
# 🤖 run links and the 📥 "note received" posts. Until the log channel exists and the bot role can see it, every
# post goes to the conversation channel as before (see find_log_channel).


class DiscordMixin:
    def discord(self, method, path, **kw):
        r = self.s.request(method, DISCORD + path, headers=self.dh, timeout=30, **kw)
        return r

    def find_channel(self):
        if self.state.get("channel_id"):
            return self.state["channel_id"]
        r = self.discord("GET", f"/guilds/{self.guild}/channels")
        r.raise_for_status()
        for c in r.json():
            if c["type"] == 0 and c["name"] in CFG.channel_names:
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
            if c["type"] == 0 and c["name"] == CFG.log_channel_name:
                st["log_channel_id"] = c["id"]
                st.pop("log_channel_missing", None)
                log(f"log channel #{CFG.log_channel_name} found ({c['id']}): summaries, run links and received posts go there now")
                return c["id"]
        if not st.get("log_channel_missing"):
            st["log_channel_missing"] = True
            log(f"#{CFG.log_channel_name} not visible to the bot; everything posts to the conversation channel until it is")
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
