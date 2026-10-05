"""Reaction buttons on Discord posts (2026-09-28): a host program posts a message, the owner taps a reaction, the
program acts on its next run.

Why files and not a queue: every Flux program is a short cron or loop process under its own lock, and they already
share one place, `$FLUX_HOME/state`. A program that posts a message with buttons registers it with `track()`:
`state/tracked/<message id>.json` = which module, which channel, which reaction means what, and until when. The relay
(flux_brain.relay.reactions) checks the tracked posts about once a minute; when the OWNER (flux.toml
`[owner] discord_user_id`, never a reaction count) has added one of the reactions and kept it for the grace period
(`[discord] button_grace_s`, ⏳ on the post meanwhile; removing the reaction cancels), it writes
`state/actions/<module>/<message id>.json` with the payload and forgets the post. The module reads its actions on its
next run and deletes each file after acting, so a crash never acts twice.

Security: only host programs can create buttons. The vault routine writes `notify/` files, which the relay posts as
plain messages; nothing it writes can register a tracked post or an action, so an email or a document that talks the
routine into writing something can never reach a button's action.
"""
import os
import time
import urllib.parse

import requests

from ..config import CFG
from .state import load_json, save_json

__all__ = ["track", "tracked", "save", "untrack", "emit", "actions", "done", "Bot", "DISCORD"]

DISCORD = "https://discord.com/api/v10"


def _dir(*parts):
    return os.path.join(str(CFG.state_dir), *parts)


def track(message_id, channel_id, module, actions, ttl_s):
    """Register a posted message: `actions` = {emoji: payload (JSON-able)}; forgotten after `ttl_s` seconds."""
    save_json(_dir("tracked", f"{message_id}.json"), {"message": str(message_id), "channel": str(channel_id),
                                                      "module": module, "actions": actions,
                                                      "expires": time.time() + ttl_s})


def tracked():
    """[(path, entry)] of every tracked post."""
    d = _dir("tracked")
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            p = os.path.join(d, name)
            e = load_json(p, None)
            if e:
                out.append((p, e))
    return out


def save(path, entry):
    """Rewrite a tracked entry (the relay records a tap waiting out its grace period in it)."""
    save_json(path, entry)


def untrack(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def emit(module, message_id, emoji, payload):
    save_json(_dir("actions", module, f"{message_id}.json"), {"emoji": emoji, "payload": payload, "at": time.time()})


def actions(module):
    """[(path, action)] waiting for `module`, oldest first."""
    d = _dir("actions", module)
    if not os.path.isdir(d):
        return []
    out = [(os.path.join(d, n), load_json(os.path.join(d, n), None)) for n in os.listdir(d) if n.endswith(".json")]
    return sorted([(p, a) for p, a in out if a], key=lambda pa: pa[1].get("at", 0))


def done(path):
    untrack(path)


class Bot:
    """The few Discord calls a host module needs (post, react), with the relay's bot token (REST only)."""

    def __init__(self, session=None):
        self.s = session or requests.Session()
        self.h = {"Authorization": f"Bot {CFG.discord_token}", "User-Agent": "flux-brain module (1.0)"}

    def call(self, method, path, **kw):
        for _ in range(3):
            r = self.s.request(method, DISCORD + path, headers=self.h, timeout=30, **kw)
            if r.status_code == 429:                  # rate limited: wait as told, then retry
                time.sleep(min(5.0, float((r.json() or {}).get("retry_after", 1))))
                continue
            r.raise_for_status()
            return r.json() if r.content else {}
        r.raise_for_status()

    def channel_id(self, names, cache):
        """Id of the first text channel named in `names`, cached in the `cache` dict under "channel_id"."""
        if cache.get("channel_id"):
            return cache["channel_id"]
        for c in self.call("GET", f"/guilds/{CFG.discord_guild}/channels"):
            if c["type"] == 0 and c["name"] in names:
                cache["channel_id"] = c["id"]
                return c["id"]
        raise RuntimeError(f"channel #{names[0]} not visible to the bot")

    def actions_channel(self, cache):
        """Channel for a module's post with buttons: `[discord] actions_channel` when flux.toml names one and the bot
        sees it (cached in `cache` with its name, so a new name is looked up again), else the conversation channel.
        Looked up again on each post while it is missing, so the posts move there once the channel exists."""
        name = CFG.actions_channel_name
        if name:
            rec = cache.get("actions_channel") or {}
            if rec.get("name") == name and rec.get("id"):
                return rec["id"]
            for c in self.call("GET", f"/guilds/{CFG.discord_guild}/channels"):
                if c["type"] == 0 and c["name"] == name:
                    cache["actions_channel"] = {"name": name, "id": c["id"]}
                    return c["id"]
        return self.channel_id(CFG.channel_names, cache)

    def post(self, channel, content):
        # flags=4 (SUPPRESS_EMBEDS, 2026-09-30): module posts quote outside content (email snippets, Drive names),
        # so Discord must not unfurl their links: its preview bot would fetch whatever URL a sender pasted.
        return self.call("POST", f"/channels/{channel}/messages", json={
            "content": content[:1990], "allowed_mentions": {"parse": []}, "flags": 4})["id"]

    def react(self, channel, message, emoji):
        self.call("PUT", f"/channels/{channel}/messages/{message}/reactions/{urllib.parse.quote(emoji)}/@me")
