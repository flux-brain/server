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

REACTION_EVERY = 60          # seconds between scans for NEW taps (one GET per tracked post and emoji; the loop ticks
                             # every 15 s). A tap waiting out its grace period is re-checked on every call instead, so
                             # a short grace (e.g. 30 s) is honoured to within one loop tick.
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
                for emoji in t.get("actions", {}):
                    who = self._reactors(t, emoji)
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
