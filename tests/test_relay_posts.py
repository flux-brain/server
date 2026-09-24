"""Offline tests for the relay's Discord posting and routine starts (2026-09-16, pytest form 2026-09-24).

Covers: one-shot post() with nonce + enforce_nonce keys, a single retry only after a short 429, outbound resume by
part tied to the file's blob sha, cleanup of stale resume records, question @mentions, the run-link post in
maybe_fire (start saved before the post, failure swallowed), server-written inbox notes starting at once, typed notes
settling with one "received" post, the run marker, the start-message manifest, the 👀 reaction before attachment
work, the reconcile coalescing timer, the memory applier trigger and the two-channel routing (2026-09-22).
No network: requests.post, time.sleep and save_state are replaced per test, so the live state file is never touched.
"""
import datetime as dt
import time

import pytest

import flux_brain.relay as m
from fakes import Resp

OWNER = "100000000000000001"   # discord_user_id in the test flux.toml (conftest)


class Harness:
    """What the module-level fakes of the old script suite provided, per test."""

    def __init__(self, monkeypatch):
        self.calls, self.sleeps, self.saves, self.queue = [], [], [], []

        def fake_post(url, **kw):
            if "claude_code/routines" in url:
                self.calls.append(("fire", kw.get("json")))
                return Resp(200, {"claude_code_session_url": "https://claude.ai/code/cse_X"})
            self.calls.append(("discord", kw["json"], url))   # url: channel routing tests
            return self.queue.pop(0) if self.queue else Resp(200)
        monkeypatch.setattr(m.requests, "post", fake_post)
        monkeypatch.setattr(m.time, "sleep", lambda s: self.sleeps.append(s))
        monkeypatch.setattr(m, "save_state", lambda st: self.saves.append(len(self.calls)))

    def relay(self, state=None):
        r = m.Relay.__new__(m.Relay)
        r.state = state if state is not None else {}
        r.dh = {"Authorization": "Bot t"}
        r.s = None   # any use of the retrying session would crash the test
        return r

    def fired(self):
        return any(c[0] == "fire" for c in self.calls)

    def fire_text(self):
        return [c[1]["text"] for c in self.calls if c[0] == "fire"][0]


@pytest.fixture
def h(monkeypatch):
    return Harness(monkeypatch)


class FixedDT(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return dt.datetime(2026, 9, 16, 14, 30, tzinfo=dt.timezone.utc)


def blob(path, sha="a" * 40, size=10):
    return {"type": "blob", "path": path, "sha": sha, "size": size}


BODY3 = "\n".join(["x" * 1800] * 3)
LONG = "notify/2026-09-16T2000-long.md"
TREE = [{"type": "blob", "path": LONG, "sha": "s1"}]
KEEP = "inbox/2026-09-17T122915Z-keep-vault-setup.md"
GMAIL = "inbox/2026-09-17T0633Z-gmail-19fb26394223d95e.md"
RECON = "inbox/2026-09-17T1318Z-memory-reconcile-vault-setup.md"
TYPED = "inbox/Test nite 1656.md"
DISC = "inbox/2026-09-17T1501Z-1550159687578550363.md"
MARK = {"type": "blob", "path": ".run/active", "sha": "m1", "size": 30}


def marker(age_s):
    return dt.datetime.fromtimestamp(time.time() - age_s, dt.timezone.utc).strftime("started: %Y-%m-%dT%H:%M:%SZ")


# ---------- post() ----------
def test_nonce_stable_per_key(h):
    r = h.relay()
    r.post("C", "hi", key="notify/a.md#0")
    r.post("C", "hi", key="notify/a.md#0")
    b1, b2 = h.calls[0][1], h.calls[1][1]
    assert b1["enforce_nonce"] is True and len(b1["nonce"]) == 25 and b1["nonce"] == b2["nonce"]


def test_no_key_no_nonce(h):
    h.relay().post("C", "hi")
    assert "nonce" not in h.calls[0][1]


def test_502_sent_once_and_raises(h):
    h.queue[:] = [Resp(502)]
    with pytest.raises(m.requests.HTTPError):
        h.relay().post("C", "hi", key="k")
    assert len(h.calls) == 1


def test_short_429_sleeps_then_one_retry(h):
    h.queue[:] = [Resp(429, {"retry_after": 1.5}), Resp(200)]
    h.relay().post("C", "hi", key="k")
    assert len(h.calls) == 2 and h.sleeps == [1.5]


def test_long_429_raises_without_sleep(h):
    h.queue[:] = [Resp(429, {"retry_after": 60})]
    with pytest.raises(m.requests.HTTPError):
        h.relay().post("C", "hi", key="k")
    assert len(h.calls) == 1 and h.sleeps == []


# ---------- outbound ----------
def test_outbound_resumes_at_failed_part_with_same_nonces(h):
    st = {}
    r = h.relay(st)
    r.blob_text = lambda sha: BODY3
    h.queue[:] = [Resp(200), Resp(502)]
    with pytest.raises(m.requests.HTTPError):
        r.outbound("C", TREE)
    first_nonces = [c[1]["nonce"] for c in h.calls]
    assert st["posting"] == {LONG: {"sha": "s1", "done": 1}} and "posted" not in st
    h.calls.clear()
    r.outbound("C", TREE)
    second = [c[1]["nonce"] for c in h.calls]
    assert len(second) == 2 and second[0] == first_nonces[1]
    assert st["posting"] == {} and st["posted"] == [LONG]


def test_outbound_already_posted_and_stale_counter_dropped(h):
    st = {"posted": [LONG], "posting": {"notify/gone.md": 2}}
    r = h.relay(st)
    r.blob_text = lambda sha: BODY3
    r.outbound("C", TREE)
    assert h.calls == [] and st["posting"] == {}


def test_run_link_posted_after_the_start_is_saved(h, monkeypatch):
    monkeypatch.setattr(m, "datetime", FixedDT)
    r = h.relay()
    r.maybe_fire(1, "C")
    disc = [i for i, c in enumerate(h.calls) if c[0] == "discord"]
    assert len(disc) == 1 and h.calls[disc[0]][1]["enforce_nonce"] and h.saves and h.saves[0] <= disc[0]


def test_run_link_failure_swallowed_start_kept(h):
    h.queue[:] = [Resp(502)]
    r = h.relay()
    r.maybe_fire(1, "C")
    assert r.state["fire_pending"] is False


def test_file_changed_after_partial_post_restarts_from_part_0(h):
    st = {}
    r = h.relay(st)
    r.blob_text = lambda sha: BODY3
    h.queue[:] = [Resp(200), Resp(502)]
    with pytest.raises(m.requests.HTTPError):
        r.outbound("C", TREE)
    old_nonces = [c[1]["nonce"] for c in h.calls]
    h.calls.clear()
    r.blob_text = lambda sha: "short new text"
    r.outbound("C", [{"type": "blob", "path": LONG, "sha": "s2"}])
    assert len(h.calls) == 1 and "short new text" in h.calls[0][1]["content"] and h.calls[0][1]["nonce"] not in old_nonces
    assert st["posting"] == {} and st["posted"] == [LONG]


def test_legacy_int_counter_restarts_from_part_0(h):
    st = {"posting": {LONG: 2}}
    r = h.relay(st)
    r.blob_text = lambda sha: BODY3
    r.outbound("C", TREE)
    assert len(h.calls) == 3 and st["posted"] == [LONG]


def test_question_callout_mentions_owner_once_without_header(h):
    r = h.relay({})
    r.blob_text = lambda sha: "> [!question] Move Photobooth520 to Nexa?\n> Or keep both trackers?"
    r.outbound("C", [{"type": "blob", "path": "notify/2026-09-17T0950-photobooth520-nexa-vs-pe.md", "sha": "q1"}])
    c = h.calls[0][1]
    assert c["content"].startswith(f"<@{OWNER}> ❓ Move Photobooth520") and "[!question]" not in c["content"]
    assert "**notify/" not in c["content"] and "\n> " not in c["content"]
    assert c["allowed_mentions"] == {"parse": [], "users": [OWNER]}


def test_normal_notify_keeps_header_pings_nobody(h):
    r = h.relay({})
    r.blob_text = lambda sha: "Filed 2 capture(s)"
    r.outbound("C", [{"type": "blob", "path": "notify/2026-09-17T0950-filed.md", "sha": "f1"}])
    assert h.calls[0][1]["allowed_mentions"] == {"parse": []} and "**notify/2026-09-17T0950-filed.md**" in h.calls[0][1]["content"]


def test_question_by_file_name_pings_briefing_never(h):
    r = h.relay({})
    r.blob_text = lambda sha: "Is this private or FEFW?"
    r.outbound("C", [{"type": "blob", "path": "notify/2026-09-17T0639-art-storage-fefw-question.md", "sha": "a1"},
                     {"type": "blob", "path": "briefings/daily/2026-09-17.md", "sha": "b1"}])
    qpost = [c[1] for c in h.calls if "notify/" not in c[1]["content"] and "briefings/" not in c[1]["content"]]
    bpost = [c[1] for c in h.calls if "briefings/daily" in c[1]["content"]]
    assert len(qpost) == 1 and qpost[0]["allowed_mentions"].get("users") == [OWNER]
    assert len(bpost) == 1 and "users" not in bpost[0]["allowed_mentions"]   # outbound posts in path order


def test_long_question_only_first_part_mentions(h):
    r = h.relay({})
    r.blob_text = lambda sha: "> [!question] " + "\n".join(["y" * 1800] * 2)
    r.outbound("C", [{"type": "blob", "path": "notify/2026-09-17T1000-long-question.md", "sha": "l1"}])
    assert len(h.calls) == 2 and "users" in h.calls[0][1]["allowed_mentions"] and "users" not in h.calls[1][1]["allowed_mentions"]


# ---------- responsiveness (2026-09-17): server notes, typed notes, the run marker, the manifest ----------
@pytest.mark.parametrize("path", [KEEP, GMAIL, RECON, "inbox/2026-09-17T122915Z-keep-note-abc123.md",
                                  "inbox/2026-09-22T163859Z-tasks-sudesca.md", "inbox/2026-09-22T180000Z-whatsapp-acct-abc.md"])
def test_host_note_matches_server_written_captures(path):
    assert m.HOST_NOTE.match(path)


@pytest.mark.parametrize("path", [TYPED, DISC, "inbox/keep-shopping.md"])
def test_host_note_rejects_typed_and_relay_notes(path):
    assert not m.HOST_NOTE.match(path)


def test_server_note_starts_at_once(h):
    st = {"obsidian_notes": {}}
    h.relay(st).watch_obsidian_notes([blob(KEEP)], now=1000, channel="C")
    assert st.get("fire_pending") is True and "obsidian_fire_at" not in st and h.calls == []


def test_typed_note_settles_and_gets_one_received_post(h):
    st = {"obsidian_notes": {}}
    r = h.relay(st)
    r.watch_obsidian_notes([blob(TYPED, "b" * 40)], now=1000, channel="C")
    assert not st.get("fire_pending") and st["obsidian_fire_at"] == 1000 + m.CFG.phone_settle
    assert len(h.calls) == 1 and "received" in h.calls[0][1]["content"] and h.calls[0][1]["enforce_nonce"]
    r.watch_obsidian_notes([blob(TYPED, "c" * 40)], now=1050, channel="C")
    assert len(h.calls) == 1
    r.watch_obsidian_notes([blob(TYPED, "c" * 40)], now=1050 + m.CFG.phone_settle, channel="C")
    assert st.get("fire_pending") is True


def test_server_note_does_not_wait_for_typed_note_reconcile_timer(h):
    st = {"obsidian_notes": {}}
    r = h.relay(st)
    r.watch_obsidian_notes([blob(TYPED, "b" * 40), blob(RECON)], now=1000)
    assert st.get("fire_pending") is True and set(st["obsidian_pending"]) == {TYPED}
    assert st["reconcile_first"] == 1000 and st["reconcile_hold_until"] == 1000 + m.RECONCILE_COALESCE
    r.watch_obsidian_notes([blob(TYPED, "b" * 40), blob(RECON, "d" * 40)], now=1000 + 800)
    assert st["reconcile_hold_until"] == 1000 + m.RECONCILE_MAX   # capped at 15 min after the first


@pytest.mark.parametrize("hh,mm", [(14, 58), (15, 2)])
def test_no_clock_windows_any_more(h, monkeypatch, hh, mm):
    class DTW(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 9, 17, hh, mm, 30, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(m, "datetime", DTW)
    st = {"fire_pending": True}
    h.relay(st).maybe_fire(0, "C", [blob(TYPED)])
    assert h.fired()


def test_empty_inbox_drops_the_pending_start(h):
    st = {"fire_pending": True}
    h.relay(st).maybe_fire(0, "C", [blob("inbox/.gitkeep", size=0), blob("wiki/x.md")])
    assert st["fire_pending"] is False and not h.fired()


def test_start_message_lists_the_inbox_with_kinds(h):
    st = {"fire_pending": True}
    h.relay(st).maybe_fire(0, "C", [blob(KEEP), blob(TYPED), blob(DISC)])
    text = h.fire_text()
    assert "Inbox now holds:" in text and "(Keep checklist edit, project vault-setup)" in text and "(Obsidian note)" in text and "(Discord)" in text


def test_filed_this_tick_always_starts(h):
    h.relay({}).maybe_fire(1, "C", [])
    assert h.fired()


def test_seen_reaction_precedes_download_and_never_blocks(h):
    seen_calls = []
    r = h.relay({})
    r.discord = lambda method, path, **kw: seen_calls.append((method, path)) or Resp(200)

    class Boom(Exception):
        pass

    class S:
        def get(self, *a, **k):
            seen_calls.append(("download",))
            raise Boom()
    r.s = S()
    msg = {"id": "1", "content": "doc", "timestamp": "2026-09-17T15:00:00+00:00", "attachments": [{"filename": "a.pdf", "url": "u", "size": 5}]}
    with pytest.raises(Boom):
        r.file_message("C", msg)
    assert seen_calls[0][0] == "PUT" and "%F0%9F%91%80" in seen_calls[0][1] and seen_calls[1] == ("download",)
    r.discord = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    r.seen("C", "1")
    r.unseen("C", "1")


def test_typed_note_alone_waits_with_another_it_goes_and_is_marked_leave_it(h):
    st = {"fire_pending": True, "obsidian_fire_at": 10**12, "obsidian_pending": [TYPED]}
    r = h.relay(st)
    r.maybe_fire(0, "C", [blob(TYPED)])
    assert st["fire_pending"] is True and not h.fired()
    r.maybe_fire(0, "C", [blob(TYPED), blob(DISC)])
    assert "`inbox/Test nite 1656.md` (Obsidian note, still being edited: leave it)" in h.fire_text() and st["fired_inbox"] == [DISC]


def test_run_marker_blocks_while_fresh_then_start_goes_ahead(h):
    st = {"fire_pending": True, "fire_ceiling_until": time.time() + 100}
    r = h.relay(st)
    r.blob_text = lambda sha: marker(60)
    r.maybe_fire(0, "C", [blob(DISC), MARK])
    assert st.get("run_active") is True and not h.fired()
    r.maybe_fire(0, "C", [blob(DISC)])
    assert "run_active" not in st and h.fired()


def test_stale_marker_ignored(h):
    st = {"fire_pending": True}
    r = h.relay(st)
    r.blob_text = lambda sha: marker(700)
    r.maybe_fire(0, "C", [blob(DISC), MARK])
    assert not st.get("run_active") and h.fired()


def test_second_start_waits_for_the_ceiling(h):
    st = {"fire_pending": True}
    r = h.relay(st)
    r.maybe_fire(0, "C", [blob(DISC)])
    assert st["fire_ceiling_until"] > time.time() + m.CFG.fire_min_interval - 5 and m.CFG.fire_min_interval == 180
    h.calls.clear()
    st["fire_pending"] = True
    r.maybe_fire(0, "C", [blob(DISC)])
    assert not h.fired()
    st["fire_ceiling_until"] = time.time() - 1
    r.maybe_fire(0, "C", [blob(DISC)])
    assert h.fired()


def test_capture_left_behind_by_a_finished_run_rearms_once(h):
    st = {"fired_inbox": [DISC]}
    r = h.relay(st)
    r.blob_text = lambda sha: marker(60)
    r.track_run_marker([blob(DISC), MARK], time.time())
    r.track_run_marker([blob(DISC)], time.time())
    assert st.get("fire_pending") is True and st["rearmed"] == [DISC]
    st["fire_pending"] = False
    r.track_run_marker([blob(DISC), dict(MARK, sha="m2")], time.time())
    r.track_run_marker([blob(DISC)], time.time())
    assert st["fire_pending"] is False   # the same capture never loops


def test_reconcile_only_start_waits_owner_capture_carries_it(h):
    st = {"fire_pending": True, "reconcile_hold_until": time.time() + 100}
    r = h.relay(st)
    r.maybe_fire(0, "C", [blob(RECON)])
    assert not h.fired()
    r.maybe_fire(0, "C", [blob(RECON), blob(KEEP)])
    assert h.fired() and "reconcile_hold_until" not in st


def test_memory_proposal_starts_the_applier_once_detached(h, monkeypatch, tmp_path):
    popens = []
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: popens.append((cmd, kw)))
    monkeypatch.setattr(m.CFG, "log_dir", tmp_path)   # the applier's log file goes under the test home
    st = {}
    r = h.relay(st)
    P1 = {"type": "blob", "path": "memory-proposals/2026-09-17T1231-vault-setup.md", "sha": "p", "size": 9}
    monkeypatch.setattr(m.CFG, "mod_memory", False)
    r.trigger_apply([P1])
    assert popens == [] and "proposals_seen" not in st   # module off: no-op
    monkeypatch.setattr(m.CFG, "mod_memory", True)
    r.trigger_apply([P1])
    assert popens == [] and st["proposals_seen"] == [P1["path"]]   # first sight only initialises
    P2 = dict(P1, path="memory-proposals/2026-09-17T1700-vault-setup.md")
    r.trigger_apply([P1, P2])
    r.trigger_apply([P1, P2])
    assert len(popens) == 1 and popens[0][0][:3] == m.reconcile_cmd()[:3]
    assert m.reconcile_cmd()[2] == str(m.CFG.lock_dir / "flux-memory-reconcile.lock") and popens[0][1]["start_new_session"]


def test_manifest_hints_name_page_and_memory_files(h):
    PAGE = {"type": "blob", "path": "wiki/projects/vault-setup.md", "sha": "pg", "size": 9}
    blobs = {"k": "---\nsource: keep\nproject: vault-setup\npage: wiki/projects/vault-setup.md\n---\n", "r": "---\npage: vault-setup\n---\n",
             "pg": "---\nmemory: [second-brain-INDEX.md, vault-state.md]\n---\n"}
    st = {"fire_pending": True}
    r = h.relay(st)
    r.blob_text = lambda sha: blobs[sha]
    r.maybe_fire(0, "C", [dict(blob(KEEP), sha="k"), dict(blob(RECON), sha="r"), PAGE])
    text = h.fire_text()
    assert "(Keep checklist edit, project vault-setup; page wiki/projects/vault-setup.md, memory: second-brain-INDEX.md, vault-state.md)" in text
    assert "(memory reconcile, project vault-setup; page wiki/projects/vault-setup.md, memory:" in text


def test_describe_inbox_cleans_the_path():
    assert m.describe_inbox("inbox/a`b\x07c.md") == "`inbox/abc.md` (Obsidian note)"


# ---------- two channels (2026-09-22) ----------
def chan(c):
    return c[2].split("/channels/")[1].split("/")[0]


FILED = {"type": "blob", "path": "notify/2026-09-22T0524-filed.md", "sha": "f2"}
QUEST = {"type": "blob", "path": "notify/2026-09-22T0519-question-test.md", "sha": "q2"}
DRAFT = {"type": "blob", "path": "notify/2026-09-22T0527-taches-urgentes.md", "sha": "d2"}
DAILY = {"type": "blob", "path": "briefings/daily/2026-09-22.md", "sha": "b2"}


def test_only_the_run_summary_leaves_the_conversation_channel(h):
    assert m.is_run_summary(FILED["path"]) and not any(m.is_run_summary(p["path"]) for p in (QUEST, DRAFT, DAILY))
    r = h.relay({})
    r.log_channel = "L"
    r.blob_text = lambda sha: "body"
    r.outbound("C", [DAILY, FILED, QUEST, DRAFT])
    assert [chan(c) for c in h.calls] == ["C", "C", "L", "C"]   # path order: daily, 0519-question, 0524-filed, 0527


def test_no_log_channel_everything_stays(h):
    r = h.relay({})
    r.blob_text = lambda sha: "body"
    r.outbound("C", [FILED, QUEST])
    assert [chan(c) for c in h.calls] == ["C", "C"] and r.log_target("C") == "C"


def test_received_post_and_run_link_go_to_the_log_channel(h):
    st = {"obsidian_notes": {}}
    r = h.relay(st)
    r.log_channel = "L"
    r.watch_obsidian_notes([blob(TYPED, "d" * 40)], now=1000, channel="C")
    assert len(h.calls) == 1 and "received" in h.calls[0][1]["content"] and chan(h.calls[0]) == "L"
    h.calls.clear()
    r = h.relay()
    r.log_channel = "L"
    r.maybe_fire(1, "C")
    disc = [c for c in h.calls if c[0] == "discord"]
    assert len(disc) == 1 and "watch it work" in disc[0][1]["content"] and chan(disc[0]) == "L"


def test_find_log_channel_cache_miss_and_names(h):
    assert h.relay({"log_channel_id": "L9"}).find_log_channel() == "L9"

    class G:
        def __init__(self, names):
            self.names = names

        def request(self, method, url, **kw):
            return Resp(200, [{"type": 0, "name": nm, "id": f"id-{nm}"} for nm in self.names])
    r = h.relay({})
    r.s, r.guild = G(["general", "flux", "flux-log"]), "g"
    assert r.find_channel() == "id-flux" and r.find_log_channel() == "id-flux-log" and "log_channel_missing" not in r.state
    r = h.relay({})
    r.s, r.guild = G(["general", "flux"]), "g"   # config names only (no legacy alias in the package)
    assert r.find_channel() == "id-flux" and r.find_log_channel() is None and r.state["log_channel_missing"] is True
