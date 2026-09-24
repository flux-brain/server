"""flux_brain.relay (a package since 2026-09-24): Discord capture channel <-> the vault repository on GitHub, run every 15 s by the loop service.

Why this exists: the vault routines run in Anthropic's cloud, whose default network allowlist
does not reach discord.com, and we refuse to put a bot token in cloud config. So this host does
all Discord I/O and talks to the repo only through the GitHub REST API (no local clone, hence no
rebase conflicts with the routines or the phone).

  inbound : new human messages in the conversation channel -> inbox/<ts>-<msgid>.md, then a ✅ reaction (or a short
            reply if reactions are denied). With the Drive module on, attachment originals go to a
            Drive folder, NOT into git, so the repo and the phone copy stay light, and the note links
            the Drive file; without it the note links Discord's copy. The extracted text always lands
            in raw/attachments/.
  outbound: new files under briefings/daily, briefings/weekly, notify/ -> posted to the conversation channel once.
  run link: each time the relay starts the vault-inbox routine it posts the run's claude.ai link to
            the log channel, so the owner can watch Claude work step by step (2026-09-16, the owner).

Secrets come from $FLUX_HOME/flux.env (Discord bot token, guild id, routine trigger token, GitHub token; see
flux_brain.config). Nothing secret is written to disk or logs. State (channel ids, last message id, posted paths,
failure count): $FLUX_HOME/state/state.json. Shared code (GitHub/Drive clients, state files, extraction, log):
flux_brain.lib.

Layout (2026-09-24, architecture review; one 800-line module before): `Relay` in this file composes one mixin per
concern, each in its own module with the constants and comments that belong to it: discord (session, channels,
post), inbound (filing), watch (Obsidian notes, server notes, memory proposals), fire (the routine start, the run
marker, the manifest), outbound (posting files). state.py documents every key of state.json. Method names and the
state file are unchanged, so the tests and the live state carry over.
"""
from ..config import CFG
from ..lib.common import log, ops_alert
from ..lib.captures import RELAY_NOTE, HOST_NOTE  # noqa: F401  re-exported: the tests and the docs refer to them here
from ..lib.github import GitHub, session
from ..lib.drive import Drive
from . import state, discord, inbound, watch, fire, outbound  # noqa: F401  submodules, reachable as relay.<name>
from .state import state_file, load_state, save_state, prune_posted  # noqa: F401
from .discord import DiscordMixin, DISCORD  # noqa: F401
from .inbound import InboundMixin, MESSAGE_GIVE_UP  # noqa: F401
from .watch import WatchMixin, RECONCILE_NOTE, RECONCILE_COALESCE, RECONCILE_MAX, reconcile_cmd, reconcile_log  # noqa: F401
from .fire import FireMixin, RUN_MARKER, MANIFEST_MAX, describe_inbox  # noqa: F401
from .outbound import OutboundMixin, OUTBOUND_DIRS, MAX_POST_CHUNKS, is_question, question_text, is_run_summary, chunk_lines  # noqa: F401

__all__ = ["Relay", "main", "CFG", "FAIL_ALERT_AFTER", "RELAY_NOTE", "HOST_NOTE", "state", "discord", "inbound", "watch", "fire",
           "outbound", "state_file", "load_state", "save_state", "prune_posted", "DISCORD", "MESSAGE_GIVE_UP", "RECONCILE_NOTE",
           "RECONCILE_COALESCE", "RECONCILE_MAX", "reconcile_cmd", "reconcile_log", "RUN_MARKER", "MANIFEST_MAX", "describe_inbox",
           "OUTBOUND_DIRS", "MAX_POST_CHUNKS", "is_question", "question_text", "is_run_summary", "chunk_lines"]

FAIL_ALERT_AFTER = 20              # consecutive failed runs before alerting: since 2026-09-15 the relay runs
                                   # every 15 s (flux-relay-loop), so 20 runs ~ 5 minutes
                                   # (was 3 at the old 5-minute cadence; 3 x 15 s would page on a GitHub blip)


class Relay(DiscordMixin, InboundMixin, WatchMixin, FireMixin, OutboundMixin):
    def __init__(self, state):
        self.state = state
        self.s = session(retry_writes=True)
        CFG.require("discord_token", "discord_guild")
        self.guild = CFG.discord_guild
        self.dh = {"Authorization": f"Bot {CFG.discord_token}", "User-Agent": "flux-relay (flux-brain, 1.0)"}
        # tree-cache.json: the branch tip is checked with a conditional GET each tick and the recursive tree is
        # re-fetched only when it moved (item 7); this process is per-run, so the cache lives on disk.
        self.ghc = GitHub(retry_writes=True, cache_file=CFG.state_file("tree-cache.json"))
        self.drive = Drive(self.s) if CFG.mod_drive else None  # Drive module: attachments to cloud storage

    # ---------- GitHub + Drive (lib since 2026-09-18; method names kept for the tests and the callers) ----------
    def put_file(self, path, data: bytes, message):
        self.ghc.put_file(path, data, message)  # an existing file is kept (re-run after a crash)

    def tree(self):
        return self.ghc.tree()  # conditional: re-fetched only when the branch tip moved (item 7)

    def blob_text(self, sha):
        return self.ghc.blob_text(sha)

    def drive_upload(self, name, data: bytes, mime):
        return self.drive.upload(name, data, mime)


def main():
    st = load_state()
    try:
        relay = Relay(st)
        channel = relay.find_channel()
        if not channel:
            log(f"#{' / #'.join(CFG.channel_names)} not visible to the bot yet; nothing to do")
            save_state(st)
            return 0
        relay.log_channel = relay.find_log_channel()  # None until the log channel exists: then log_target() = channel
        filed = relay.inbound(channel)
        tree = relay.tree()  # after inbound, so this tick's Discord notes are in it (and excluded by RELAY_NOTE)
        relay.watch_obsidian_notes(tree, channel=channel)  # may set fire_pending (typed notes after settling, server notes now)
        relay.trigger_apply(tree)  # new memory proposals start the applier now instead of at its next cron tick
        relay.maybe_fire(filed, channel, tree)  # never raises: a failed start leaves captures for the hourly run
        relay.outbound(channel, tree)
        st["failures"] = 0
        save_state(st)
        return 0
    except Exception as exc:  # noqa: BLE001 - one alert path for every failure mode
        st["failures"] = st.get("failures", 0) + 1
        save_state(st)
        log(f"ERROR ({st['failures']} in a row): {exc}")
        if st["failures"] == FAIL_ALERT_AFTER:  # alert once per outage, not every 5 minutes
            ops_alert(f"❌ Flux relay failing {FAIL_ALERT_AFTER} runs in a row: {exc}"[:1800])
        return 1
