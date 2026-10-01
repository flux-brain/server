"""The run guard's commit audit (2026-09-29): routine commits pushed while no run marker is in place.

The run protocol (vault CLAUDE.md, step 7) makes the commit that removes `.run/active` a run's LAST. A routine that
pushes a second thought after it works without the marker, so the relay may already have started the next run on top
of it: that is how runs overlap and how an already-posted notice got rewritten unseen. Prompt rules alone did not hold
under a burst of captures (5 of 45 runs on 2026-09-29), so the relay now checks every new commit itself:

  - a commit by the routines' git author (`[routine] author`) that neither adds nor removes the marker, while the
    marker is absent, is LATE;
  - a late commit that arrives in the SAME push as the commit removing the marker (one tip move) is harmless: GitHub
    never showed the repo without the marker, so no start could slip in and the notices went out in their final
    version. It is only logged and counted (`late_same_push`): no post, no hold (refinement 2026-10-01; all five late
    commits of the first two days were of this kind, so posting them only taught the owner to ignore the warning);
  - a late commit pushed when GitHub ALREADY showed the marker gone (no removal earlier in the same tip move) is the
    real overlap risk: logged, counted (`late_commits`), reported in ONE log-channel post per tip, and it restarts
    the post-run quiet window (`quiet_until`), so no start goes out while a run is still pushing.

Marker presence is followed commit by commit from the last audited tip: only the routines touch `.run/active`, so
other commits (relay captures, Drive watch, memory mirror) are not fetched. Never raises: a failed audit only skips
the check; a vanished base (a force push) restarts it from the current tip.
"""
import time

import requests

from ..config import CFG
from ..lib.common import log
from . import state
from .fire import RUN_MARKER


class AuditMixin:
    def audit_commits(self, tree, channel):
        st = self.state
        try:
            tip = self.ghc.tip
            if not tip:
                return
            has = any(e["type"] == "blob" and e["path"] == RUN_MARKER for e in tree)
            a = st.get("audit")
            if not a:
                st["audit"] = {"tip": tip, "marker": has}   # first run: audit from here on, never the history
                state.save_state(st)
                return
            if a["tip"] == tip:
                return
            try:
                commits = self.ghc.compare(a["tip"], tip)
            except requests.HTTPError as exc:
                if getattr(exc.response, "status_code", None) != 404:
                    raise
                log("commit audit: last audited tip is gone (force push?), restarting from the current tip")
                commits = []
            present, late, same_push, removed_here = a["marker"], [], [], False
            for c in commits:
                if (c.get("commit", {}).get("author") or {}).get("name") != CFG.routine_author:
                    continue
                touch = [f for f in self.ghc.commit_files(c["sha"]) if f.get("filename") == RUN_MARKER]
                if touch:
                    present = touch[-1].get("status") != "removed"
                    removed_here = removed_here or not present
                elif not present:
                    # removed in this same tip move: pushed together with the run's final commit, harmless
                    (same_push if removed_here else late).append(c)
            if same_push:
                st["late_same_push"] = st.get("late_same_push", 0) + len(same_push)
                for c in same_push:
                    log(f"late routine commit pushed with the run's final commit (harmless, not posted): "
                        f"`{c['sha'][:7]}` {c['commit']['message'].splitlines()[0][:120]}")
            if late:
                self.report_late(late, tip, channel)
            st["audit"] = {"tip": tip, "marker": has}
            state.save_state(st)
        except Exception as exc:  # noqa: BLE001 - the audit must never stop filing or posting
            log(f"commit audit failed ({exc.__class__.__name__}), will retry next tick")

    def report_late(self, late, tip, channel):
        st = self.state
        st["late_commits"] = st.get("late_commits", 0) + len(late)
        st["quiet_until"] = max(st.get("quiet_until", 0), time.time() + CFG.post_run_quiet)
        lines = [f"`{c['sha'][:7]}` {c['commit']['message'].splitlines()[0][:120]}" for c in late]
        for ln in lines:
            log(f"late routine commit (no run marker): {ln}")
        state.save_state(st)   # counted and quiet window set before the post: a failed post never re-counts
        text = (f"⚠️ {len(late)} routine commit(s) pushed after the run marker was removed (run protocol step 7); "
                f"next start held {CFG.post_run_quiet}s. Total so far: {st['late_commits']}.\n" + "\n".join(lines))
        try:
            self.post(self.log_target(channel), text[:1900], key=f"late@{tip[:10]}")
        except Exception as exc:  # noqa: BLE001 - the log line above is the record; the post is a courtesy
            log(f"late-commit post failed ({exc.__class__.__name__})")
