"""Reaction buttons, relay side (2026-09-28): once a minute, check the posts host modules registered
(flux_brain.lib.buttons.track) for the OWNER's reaction, and hand the matching action to its module.

Grace period (`[discord] button_grace_s`, 600 s by default): a tap is not acted on at once. The relay marks the post
⏳ and waits; if the owner removes the reaction within the grace period, the tap is cancelled (⏳ removed). Once it has
held for the whole period, the action goes to the module and ⏳ becomes 👌. A mis-tap (seen live on the first day:
✍️ on the post below the intended one) costs nothing if it is taken back."""
import time
import urllib.parse

from ..config import CFG
from ..lib import buttons
from ..lib.common import log

REACTION_EVERY = 60          # seconds between scans for NEW taps (the loop ticks every 15 s). A tap waiting out its
                             # grace period is re-checked on every call instead, so a short grace (e.g. 30 s) is
                             # honoured to within one loop tick.
# A scan reads the reaction COUNTS of every tracked post from the channel's message list (one call per 100 messages,
# SCAN_PAGES at most), and asks who reacted only where a count shows someone besides the bot. It used to ask Discord
# once per post and emoji: with 23 posts and 40 buttons that was 40 calls, every fifth waiting about 4 s on the
# reactions rate limit, so a scan took 39 s and the relay, which does one thing at a time, was busy two thirds of
# every minute (measured 2026-10-04).
SCAN_PAGES = 5
WAIT, DONE = "⏳", "👌"


class ReactionsMixin:
    def _reactors(self, t, emoji):
        """Ids of the users who reacted `emoji` on tracked post `t`, or None when the post is gone."""
        r = self.discord("GET", f"/channels/{t['channel']}/messages/{t['message']}/reactions/"
                                f"{urllib.parse.quote(emoji)}", params={"limit": 100})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return {u.get("id") for u in r.json()}

    @staticmethod
    def _others(msg):
        """{emoji: reactions by anyone but the bot} of one Discord message object. Emoji are compared without the
        variation selector, which Discord may drop from a name (✍️ comes back as either form)."""
        return {(x.get("emoji") or {}).get("name", "").replace("\ufe0f", ""): x.get("count", 0) - (1 if x.get("me") else 0)
                for x in msg.get("reactions") or []}

    def _reaction_counts(self, entries):
        """{message id: {emoji: others}} for the tracked posts not waiting out a grace period; None for a post that is
        gone. One list call per channel and 100 messages, newest first, until every tracked post of that channel
        is covered (SCAN_PAGES at most); a post still not found is fetched by itself. Raises on a Discord error
        (check_reactions retries next minute)."""
        out, by_channel = {}, {}
        for _, t in entries:
            if not t.get("pending"):
                by_channel.setdefault(t["channel"], set()).add(t["message"])
        for ch, wanted in by_channel.items():
            before = None
            for _ in range(SCAN_PAGES):
                r = self.discord("GET", f"/channels/{ch}/messages", params={"limit": 100, **({"before": before} if before else {})})
                r.raise_for_status()
                page = r.json()
                for msg in page:
                    if msg.get("id") in wanted:
                        out[msg["id"]] = self._others(msg)
                left = wanted - set(out)
                # stop when all are found, the channel is exhausted, or ids cannot be ordered (not snowflakes)
                if not left or len(page) < 100 or not all(str(i).isdigit() for i in left | {page[-1]["id"]}):
                    break
                before = page[-1]["id"]
                if min(int(i) for i in left) > int(before):   # older pages cannot hold them: they were deleted
                    break
            for mid in wanted - set(out):                      # older than the pages read, or deleted
                r = self.discord("GET", f"/channels/{ch}/messages/{mid}")
                if r.status_code == 404:
                    out[mid] = None
                else:
                    r.raise_for_status()
                    out[mid] = self._others(r.json())
        return out

    def _mark(self, t, emoji, on):
        self.discord("PUT" if on else "DELETE",
                     f"/channels/{t['channel']}/messages/{t['message']}/reactions/{urllib.parse.quote(emoji)}/@me")

    def check_reactions(self, now=None):
        """Returns the number of actions emitted. Never raises: a Discord blip only delays a button by a minute."""
        now = now or time.time()
        scan = now - self.state.get("reactions_checked", 0) >= REACTION_EVERY
        entries = buttons.tracked()
        if not scan and not any(t.get("pending") for _, t in entries):
            return 0
        if scan:
            self.state["reactions_checked"] = now
        n = 0
        try:
            counts = self._reaction_counts(entries) if scan else {}
            for path, t in entries:
                if t.get("expires", 0) < now and not t.get("pending"):
                    buttons.untrack(path)
                    continue
                pend = t.get("pending")
                if pend:                                   # a tap waiting out its grace period
                    who = self._reactors(t, pend["emoji"])
                    if who is None:
                        buttons.untrack(path)
                    elif CFG.owner_discord_id not in who:  # taken back: cancelled
                        t.pop("pending")
                        buttons.save(path, t)
                        self._mark(t, WAIT, False)
                        log(f"button {pend['emoji']} on {t['message']} cancelled (reaction removed)")
                    elif now - pend["at"] >= CFG.button_grace:
                        buttons.emit(t["module"], t["message"], pend["emoji"], t["actions"][pend["emoji"]])
                        buttons.untrack(path)
                        self._mark(t, WAIT, False)
                        self._mark(t, DONE, True)
                        log(f"button {pend['emoji']} on {t['message']} -> {t['module']}")
                        n += 1
                    continue
                if not scan:
                    continue
                others = counts.get(t["message"])
                if others is None:                         # the post was deleted: nothing to act on
                    buttons.untrack(path)
                    continue
                for emoji in t.get("actions", {}):
                    if others.get(emoji.replace("\ufe0f", ""), 0) <= 0:
                        continue                           # nobody but the bot on this button: no call needed
                    who = self._reactors(t, emoji)         # someone reacted: only the owner's tap counts
                    if who is None:                        # the post was deleted: nothing to act on
                        buttons.untrack(path)
                        break
                    if CFG.owner_discord_id in who:
                        t["pending"] = {"emoji": emoji, "at": now}
                        buttons.save(path, t)
                        self._mark(t, WAIT, True)
                        log(f"button {emoji} on {t['message']} tapped, acting in {CFG.button_grace} s unless taken back")
                        break
        except Exception as exc:  # noqa: BLE001
            log(f"reaction check failed ({exc.__class__.__name__}); retried next minute")
        return n
