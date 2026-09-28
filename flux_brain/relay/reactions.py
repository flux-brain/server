"""Reaction buttons, relay side (2026-09-28): once a minute, check the posts host modules registered
(flux_brain.lib.buttons.track) for the OWNER's reaction, and hand the matching action to its module."""
import time
import urllib.parse

from ..config import CFG
from ..lib import buttons
from ..lib.common import log

REACTION_EVERY = 60          # seconds between checks (the loop ticks every 15 s; one GET per tracked post and emoji)


class ReactionsMixin:
    def check_reactions(self, now=None):
        """Returns the number of actions emitted. Never raises: a Discord blip only delays a button by a minute."""
        now = now or time.time()
        if now - self.state.get("reactions_checked", 0) < REACTION_EVERY:
            return 0
        self.state["reactions_checked"] = now
        n = 0
        try:
            for path, t in buttons.tracked():
                if t.get("expires", 0) < now:
                    buttons.untrack(path)
                    continue
                for emoji, payload in t.get("actions", {}).items():
                    r = self.discord("GET", f"/channels/{t['channel']}/messages/{t['message']}/reactions/"
                                            f"{urllib.parse.quote(emoji)}", params={"limit": 100})
                    if r.status_code == 404:               # the post was deleted: nothing to act on
                        buttons.untrack(path)
                        break
                    r.raise_for_status()
                    if any(u.get("id") == CFG.owner_discord_id for u in r.json()):
                        buttons.emit(t["module"], t["message"], emoji, payload)
                        buttons.untrack(path)
                        self.discord("PUT", f"/channels/{t['channel']}/messages/{t['message']}/reactions/"
                                            f"{urllib.parse.quote('👌')}/@me")
                        log(f"button {emoji} on {t['message']} -> {t['module']}")
                        n += 1
                        break
        except Exception as exc:  # noqa: BLE001
            log(f"reaction check failed ({exc.__class__.__name__}); retried next minute")
        return n
