"""Watcher: notes written in Obsidian (settle first) or by the server's own programs (at once) make a routine start
pending; new memory-proposals/ files start the memory applier."""
import re
import subprocess
import time

from ..config import CFG
from ..lib.common import log
from ..lib.captures import RELAY_NOTE, HOST_NOTE
from . import state

# Memory reconcile notes coalesce (review round 2 N4): a start whose only captures are reconcile notes waits
# RECONCILE_COALESCE after the latest reconcile change, at most RECONCILE_MAX after the first, so a Claude Code session's
# stream of memory edits is one run; the owner's own captures never wait for this and carry the reconcile notes along.
RECONCILE_NOTE = re.compile(r"^inbox/[^/]*Z-memory-reconcile-[a-z0-9-]+\.md$")
RECONCILE_COALESCE = 300
RECONCILE_MAX = 900
# New memory-proposals/ files start the memory applier at once (round 2) instead of waiting for its */5 cron. Same
# command and lock as the cron line, so the two never run together; the cron keeps the Kuma ping.
# memory module (v2): the applier runs detached under the same lock as its cron line, so the two never overlap


def reconcile_cmd():
    return ["flock", "-n", str(CFG.lock_dir / "flux-memory-reconcile.lock"), "flux-memory-reconcile", "apply"]


def reconcile_log():
    return str(CFG.log_dir / "memory-reconcile.log")

# Notes written in Obsidian (phone via GitSync, or laptop) also start the routine (2026-09-15, the owner: "add the
# obsidian notes trigger"). GitSync pushes while a note is still being typed (and Obsidian creates an EMPTY file on
# New note), so a note must stay unchanged for CFG.phone_settle seconds before it counts. Notes the relay writes itself
# (RELAY_NOTE) are excluded: they already started a run through `filed`. Notes other programs on this server write
# COMPLETE in one commit (HOST_NOTE: Keep, Gmail, memory reconcile, Tasks, WhatsApp) start the routine on the tick
# they appear (2026-09-17, responsiveness review P1). Both patterns and the wording per kind: flux_brain.lib.captures.


class WatchMixin:
    def watch_obsidian_notes(self, tree, now=None, channel=None):
        """Mark a routine start as pending once a new or edited inbox note from Obsidian has settled.
        Notes written by the server's own programs (HOST_NOTE) make a start pending on the same tick (2026-09-17, review
        P1). Since round 2 they no longer wait for a typed note that is still settling: the start lists that note as
        "still being edited: leave it" instead (N1), and reconcile notes get a coalescing timer (N4).
        A NEW typed note gets one "received" post in the log channel when `channel` is given (review P9c).
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
            state.save_state(st)
            return
        changed = [p for p, sha in cur.items() if st["obsidian_notes"].get(p) != sha]
        dirty = bool(changed)
        host = [p for p in changed if HOST_NOTE.match(p)]
        typed = [p for p in changed if not HOST_NOTE.match(p)]
        if typed:
            # Every further push restarts the settle timer, so a note still being typed keeps waiting
            st["obsidian_pending"] = sorted(set(st.get("obsidian_pending", [])) | set(typed))
            st["obsidian_fire_at"] = now + CFG.phone_settle
            log(f"obsidian note(s) changed, start in {CFG.phone_settle}s if unchanged: {', '.join(typed)}")
            for path in typed:
                if path not in st["obsidian_notes"] and channel:
                    # First sight of a typed note (empty New-note files are excluded above, so this is the first push
                    # with text). One post per note version key, never retried: a failure only loses the courtesy.
                    try:
                        name = path.split("/", 1)[1]
                        # log channel since 2026-09-22: a courtesy notice, nothing for the owner to do
                        self.post(self.log_target(channel), f"📥 Obsidian note \"{name}\" received; Claude starts on it about "
                                  f"{CFG.phone_settle} seconds after your last sync.", key=f"recv-{path}@{cur[path][:10]}")
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
            state.save_state(st)

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
                state.save_state(st)
                return
            seen = set(st["proposals_seen"])
            new = [p for p in props if p not in seen]
            if new:
                with open(reconcile_log(), "ab") as out:
                    subprocess.Popen(reconcile_cmd(), stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     start_new_session=True, close_fds=True)
                log(f"memory proposal(s) seen, applier started: {', '.join(new)}")
            if new or len(props) != len(seen):
                st["proposals_seen"] = props[-1000:]
                state.save_state(st)
        except Exception as exc:  # noqa: BLE001
            log(f"applier start failed ({exc.__class__.__name__})")
