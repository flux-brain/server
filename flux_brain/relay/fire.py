"""Starting the vault-inbox routine: the API trigger, the run marker, the start-message manifest and its page hints."""
import re
import secrets
import time
from datetime import datetime, timezone

import requests

from ..config import CFG
from ..lib.common import log, ops_alert
from ..lib.captures import RELAY_NOTE, host_kind, describe as describe_kind, carries_page
from .watch import RECONCILE_NOTE
from . import state

# Start the vault-inbox routine as soon as a capture is filed (2026-09-15, the owner: "relay immediately").
# Per-routine API trigger token generated at claude.ai/code/routines, kept only in this mode-600 file.
# CFG.fire_url / CFG.fire_min_interval: after a start, wait this long for its run marker before another start (was a fixed
# 300 s spacing until 2026-09-17 round 2; 20 of 21 runs that day pushed within 170 s).
# Run marker (2026-09-17, review round 2 P2a): every routine run pushes `.run/active` ("started: <UTC>") when it starts
# and removes it in its last commit (vault CLAUDE.md, Run protocol). While a fresh marker exists no start is sent, for
# fired AND scheduled runs, which replaced the clock-based quiet windows around the hour. A marker older than
# CFG.marker_fresh is a crashed run's leftover and is ignored (runs overwrite it too).
RUN_MARKER = ".run/active"
# The relay writes the marker itself (2026-10-05, `[routine] relay_marker`). Until then every run pushed its own, and
# that push was the weak point: a run that read its captures first left the relay blind (it started a second run after
# the ceiling), and a marker push rejected because the branch had moved cost up to two minutes, once a whole run parked
# on a permission prompt. Now the marker is on the branch BEFORE the start is sent, so the run's clone already holds
# it; the start message carries its run id, which is how the run tells its own marker from another run's. Scheduled
# runs (nobody starts them here) still write their own, and a run still removes the marker in its last commit.
MARKER_START_MSG = "run: start"          # the same message a run uses; the audit tells the two apart by author
MARKER_UNDO_MSG = "run: start undone"    # the start failed after the marker was written: the relay took it back
MANIFEST_MAX = 20  # inbox paths listed in the start message (P8)


def describe_inbox(path, hint=None, leave=False):
    """One manifest entry for the start message (review P8): the path, plus what kind of capture its name says it is.
    Round 2: the path is wrapped in backticks with control characters and backticks removed (Obsidian note titles come
    from the phone), `hint` adds the page and its memory files (N7b), `leave` marks a note the owner is still typing (N1)."""
    shown = "`" + re.sub(r"[\x00-\x1f`]", "", path) + "`"
    if leave:
        return f"{shown} (Obsidian note, still being edited: leave it)"
    if RELAY_NOTE.match(path):
        return f"{shown} (Discord)"
    hk = host_kind(path)   # lib.captures: the same table the settle rule and page_hints use (2026-09-24)
    if not hk:
        return f"{shown} (Obsidian note)"
    return f"{shown} ({describe_kind(*hk)}{'; ' + hint if hint else ''})"


class FireMixin:
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
        if not st.get("fire_pending") or not (CFG.fire_url and CFG.fire_token):
            return
        if now < st.get("fire_not_before", 0):
            return  # Retry-After or an error backoff still running; fire on a later tick
        if st.get("run_active"):
            return  # a run (fired or scheduled) is working: start the next one when its marker is gone
        if now < st.get("fire_ceiling_until", 0):
            return  # our last start has not shown its marker yet: do not start a second run on top of it
        if now < st.get("quiet_until", 0):
            return  # run guard (2026-09-29): a run just ended or pushed after its marker; let its last push land

        waiting, settling = None, set()
        if tree is not None:
            waiting = sorted(e["path"] for e in tree if e["type"] == "blob" and e["path"].startswith("inbox/")
                             and e["path"].endswith(".md") and e.get("size", 1) > 0)
            if not waiting and not filed:
                # Everything already filed (by the scheduled run, or the previous fired one): a start would be an
                # empty run. `not filed`: a capture written this tick is in the tree anyway, but never risk dropping it.
                st["fire_pending"] = False
                log("fire: skipped, nothing left in inbox/")
                state.save_state(st)
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

        run_id = None
        if CFG.relay_marker:
            run_id = self.place_marker(now)
            if not run_id:
                return  # another run's marker is there after all, or GitHub refused: a later tick starts the run

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
        if run_id:
            # read by the vault-inbox prompt (step 4) and the vault's CLAUDE.md (Run protocol step 4)
            body["text"] += (f" Run marker: the relay already wrote `{RUN_MARKER}` for this run (run id `{run_id}`). "
                             "Do not write or push a run marker.")
        try:
            r = requests.post(CFG.fire_url, headers=headers, json=body, timeout=30)
        except requests.RequestException as exc:
            self.take_back_marker(run_id)
            st["fire_not_before"] = now + CFG.fire_min_interval
            log(f"fire: network error ({exc.__class__.__name__}), will retry in {CFG.fire_min_interval}s")
            state.save_state(st)
            return
        if not r.ok:
            self.take_back_marker(run_id)  # no run was started: its marker must not hold the next start or a scheduled run
        if r.ok:
            st.update(fire_pending=False, fire_ceiling_until=now + CFG.fire_min_interval, fire_alerted=False,
                      fired_inbox=[p for p in (waiting or []) if p not in settling])
            st.pop("reconcile_first", None)
            st.pop("reconcile_hold_until", None)
            # Persist the start BEFORE anything slow: if the process dies after this point, the next tick must
            # not see fire_pending=True and start a duplicate run (code review of b08ec02, 2026-09-16).
            state.save_state(st)
            url = r.json().get('claude_code_session_url')
            log(f"fired vault-inbox: {url or '?'}" + (f" (run marker written, run {run_id})" if run_id else ""))
            # Post the run link to Discord (2026-09-16, the owner: "see Claude step by step work in Discord"): the
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
            st["fire_not_before"] = now + max(wait, CFG.fire_min_interval)
            log(f"fire: 429 rate limited, next attempt in {wait}s")
        elif r.status_code >= 500:
            st["fire_not_before"] = now + CFG.fire_min_interval
            log(f"fire: HTTP {r.status_code}, will retry in {CFG.fire_min_interval}s")
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
        state.save_state(st)

    def place_marker(self, now):
        """Write `.run/active` for the run about to be started and return its run id; None when no start should be
        sent now. Read fresh, not from this tick's tree: a scheduled run may have pushed its marker since.
        A stale marker (a crashed run's leftover) is overwritten, as a run would do. Never raises."""
        st = self.state
        run_id = secrets.token_hex(4)
        text = f"started: {datetime.fromtimestamp(now, timezone.utc):%Y-%m-%dT%H:%M:%SZ}\nrun: {run_id}\nby: relay\n"
        try:
            cur, sha = self.ghc.get(RUN_MARKER)
            if cur is not None:
                if now - self.marker_started(cur, now) < CFG.marker_fresh:
                    return None  # a run began since the tree was read: the next tick sees its marker
                self.ghc.put(RUN_MARKER, text, MARKER_START_MSG, sha=sha)
            elif not self.ghc.put_file(RUN_MARKER, text.encode(), MARKER_START_MSG):
                # there after all: a run's own marker won the race, or our write was retried after a lost answer
                cur, _ = self.ghc.get(RUN_MARKER)
                if not cur or f"run: {run_id}" not in cur:
                    return None
            return run_id
        except Exception as exc:  # noqa: BLE001 - a marker problem must never stop filing or posting
            log(f"run marker not written ({exc.__class__.__name__}), start postponed")
            self.take_back_marker(run_id)  # the write may have landed although its answer was lost
            st["fire_not_before"] = now + 60
            state.save_state(st)
            return None

    def take_back_marker(self, run_id):
        """Remove the marker the relay wrote for a start that did not happen. Only its own (the run id must match): a
        marker left behind would hold every start, and end every scheduled run, until it went stale. Never raises."""
        if not run_id:
            return
        try:
            cur, sha = self.ghc.get(RUN_MARKER)
            if cur and f"run: {run_id}" in cur:
                self.ghc.delete(RUN_MARKER, sha, MARKER_UNDO_MSG)
                log(f"run marker taken back (run {run_id}): the start did not go through")
        except Exception as exc:  # noqa: BLE001
            log(f"run marker NOT taken back (run {run_id}, {exc.__class__.__name__}): starts held until it goes stale")

    @staticmethod
    def marker_started(text, default):
        """The `started:` time of a marker as a timestamp, `default` when it cannot be read."""
        mt = re.search(r"started:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z", text or "")
        if not mt:
            return default
        return datetime.strptime(mt.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()

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
                    try:
                        started = self.marker_started(self.blob_text(ent["sha"]), None)
                    except Exception:  # noqa: BLE001 - unreadable marker: age it from first sight
                        started = None
                    mk = st["marker"] = {"sha": ent["sha"], "started": started or now}
                active = now - mk["started"] < CFG.marker_fresh
            else:
                st.pop("marker", None)
            if active and not st.get("run_active"):
                st["run_active"] = True
                log(f"run marker seen (started {datetime.fromtimestamp(st['marker']['started'], timezone.utc):%H:%M:%S}Z)")
                state.save_state(st)
            elif not active and st.get("run_active"):
                st.pop("run_active")
                st.pop("fire_ceiling_until", None)
                # run guard (2026-09-29): a run's second thoughts come in the minutes after its marker goes (5 of
                # 45 runs on 2026-09-29); starting at once put the next run on top of them
                st["quiet_until"] = now + CFG.post_run_quiet
                inbox = {e["path"] for e in tree if e["type"] == "blob" and e["path"].startswith("inbox/")}
                done = set(st.get("rearmed", []))
                left = [p for p in st.get("fired_inbox", []) if p in inbox and p not in done]
                if left:
                    st["fire_pending"] = True
                    st["rearmed"] = (st.get("rearmed", []) + left)[-200:]
                    log(f"run finished, capture(s) still waiting, start pending: {', '.join(left)}")
                else:
                    log("run finished (marker removed)")
                state.save_state(st)
        except Exception as exc:  # noqa: BLE001 - a marker problem must never stop filing or posting
            log(f"run marker check failed ({exc.__class__.__name__})")

    def page_hints(self, paths, tree):
        """{inbox path: "page wiki/projects/<slug>.md, memory: a.md, b.md"} for the captures that name a project page
        (lib.captures: Keep and Tasks checklist edits, memory reconcile notes; round 2 N7b), so the run opens the right
        memory files at once. Best effort: any failure means no hint."""
        out = {}
        try:
            shas = {e["path"]: e["sha"] for e in tree if e["type"] == "blob"}
            for path in paths:
                hk = host_kind(path)
                if not hk or not carries_page(hk[0]):
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
