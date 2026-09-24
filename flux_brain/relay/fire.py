"""Starting the vault-inbox routine: the API trigger, the run marker, the start-message manifest and its page hints."""
import re
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
        try:
            r = requests.post(CFG.fire_url, headers=headers, json=body, timeout=30)
        except requests.RequestException as exc:
            st["fire_not_before"] = now + CFG.fire_min_interval
            log(f"fire: network error ({exc.__class__.__name__}), will retry in {CFG.fire_min_interval}s")
            state.save_state(st)
            return
        if r.ok:
            st.update(fire_pending=False, fire_ceiling_until=now + CFG.fire_min_interval, fire_alerted=False,
                      fired_inbox=[p for p in (waiting or []) if p not in settling])
            st.pop("reconcile_first", None)
            st.pop("reconcile_hold_until", None)
            # Persist the start BEFORE anything slow: if the process dies after this point, the next tick must
            # not see fire_pending=True and start a duplicate run (code review of b08ec02, 2026-09-16).
            state.save_state(st)
            url = r.json().get('claude_code_session_url')
            log(f"fired vault-inbox: {url or '?'}")
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
                    started = None
                    try:
                        mt = re.search(r"started:\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z", self.blob_text(ent["sha"]))
                        if mt:
                            started = datetime.strptime(mt.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
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
