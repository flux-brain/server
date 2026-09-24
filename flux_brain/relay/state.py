"""The relay's state file: `$FLUX_HOME/state/state.json`, one JSON object, written atomically (lib.state) after every
step that must not repeat after a crash. The keys, in one place (2026-09-24, architecture review; before this the
twenty keys were scattered through an 800-line module):

  channel_id, log_channel_id   Discord channel ids once found (a rename in Discord changes nothing)
  log_channel_missing          True once the "no log channel" line was logged, so it is not logged every tick
  last_message_id              the newest Discord message filed; inbound reads `after` it (absent = not initialised)
  message_failures             {message id: attempts} for the head of the queue only (inbound, fix 2)
  failures                     consecutive failed ticks (main: one ops alert at FAIL_ALERT_AFTER)
  posted                       outbound paths already posted (pruned to `cap` per top-level dir)
  posting                      {path: {sha, done}} resume record of a multi-part post that failed mid-way
  obsidian_notes               {inbox path: blob sha} as of the last tick (absent until the watcher's first run)
  obsidian_pending             typed notes waiting to settle; obsidian_fire_at: when they count
  reconcile_first, reconcile_hold_until   the coalescing window for memory-reconcile-only starts
  proposals_seen               memory-proposals/ paths already handed to the applier (absent until first run)
  fire_pending                 a routine start is owed; fire_not_before: backoff after an error or a 429;
  fire_ceiling_until           no second start until the last one shows its marker; fire_alerted: alerted once
  fired_inbox                  the inbox paths the last start listed; rearmed: those re-armed once after a run
  run_active, marker           a fresh `.run/active` marker is being followed ({sha, started})

Keys marked "absent until" are sentinels: their absence is what makes a feature initialise instead of firing for
history, so load_state() must not fill them in.
"""
from ..config import CFG
from ..lib.state import load_json, save_json

__all__ = ["state_file", "load_state", "save_state", "prune_posted"]


def state_file():
    return CFG.state_file("state.json")


def load_state():
    return load_json(state_file(), {"channel_id": None, "last_message_id": None, "posted": [], "failures": 0})


def save_state(state):
    save_json(state_file(), state)  # atomic (lib.state), so a crash never leaves half a state file


def prune_posted(posted, tree_paths, cap=2000):
    """The `posted` list to keep (code review item 8). Paths gone from the repo are dropped (they cannot be posted again),
    then under `cap` the newest per top-level dir are kept: the old `sorted(posted)[-2000:]` was lexicographic, so under
    pressure every briefings/ entry was evicted before any notify/ one and old briefings would have been re-posted."""
    keep = sorted(p for p in posted if p in tree_paths)
    if len(keep) <= cap:
        return keep
    groups = {}
    for p in keep:
        groups.setdefault(p.split("/", 1)[0], []).append(p)
    share = max(1, cap // len(groups))
    return sorted(p for g in groups.values() for p in g[-share:])
