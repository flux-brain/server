"""Offline tests for vault-discord-relay.py Discord posting (2026-09-16).

Covers: one-shot post() with nonce + enforce_nonce keys, a single retry only after a short 429, outbound
resume by part tied to the file's blob sha, cleanup of stale resume records, question @mentions (2026-09-17), and the run-link post in
maybe_fire (start saved before the post, failure swallowed). Since 2026-09-17 (responsiveness quick wins): server-written
inbox notes start at once, typed notes settle and get one "received" post, the :58 deferral keeps the start pending,
an empty inbox skips the start, the start message lists the inbox, and the 👀 reaction precedes attachment work. No network: requests.post, time.sleep and
save_state are replaced by fakes, so the live state file and #vault are never touched.

Run before installing an edited relay (the loop service picks up flux_brain/ changes within 15 s).
Canonical copy: tests/ .
  python tests/test_vault_relay_posts.py [relay.py]
The relay path defaults to flux_brain/relay.py; pass a copy to test it before swapping it in.
"""
import datetime as dt, tempfile, os, sys
from _load import relay as m  # noqa: E402  sets FLUX_HOME to a temp dir first
class Resp:
    def __init__(self, code, js=None): self.status_code, self._js, self.headers, self.text = code, js or {}, {}, ""
    ok = property(lambda s: s.status_code < 400)
    def json(self): return self._js
    def raise_for_status(self):
        if not self.ok: raise m.requests.HTTPError(str(self.status_code))
calls, sleeps, saves = [], [], []
queue = []
def fake_post(url, **kw):
    if "claude_code/routines" in url:
        calls.append(("fire", kw.get("json"))); return Resp(200, {"claude_code_session_url": "https://claude.ai/code/cse_X"})
    calls.append(("discord", kw["json"], url)); return queue.pop(0) if queue else Resp(200)  # url: channel routing tests (32-35)
m.requests.post = fake_post
m.time.sleep = lambda s: sleeps.append(s)
m.save_state = lambda st: saves.append(len(calls))
def relay(state=None):
    r = m.Relay.__new__(m.Relay); r.state = state if state is not None else {}; r.dh = {"Authorization": "Bot t"}
    r.s = None  # any use of the retrying session would crash the test
    return r
def reset(): calls.clear(); sleeps.clear(); saves.clear(); queue.clear()
n = 0
# 1 nonce + enforce_nonce, 25 chars, stable per key
reset(); r = relay(); r.post("C", "hi", key="notify/a.md#0"); r.post("C", "hi", key="notify/a.md#0")
b1, b2 = calls[0][1], calls[1][1]
assert b1["enforce_nonce"] is True and len(b1["nonce"]) == 25 and b1["nonce"] == b2["nonce"]; n += 1
# 2 no key -> no nonce
reset(); relay().post("C", "hi"); assert "nonce" not in calls[0][1]; n += 1
# 3 a 502 is sent once and raises
reset(); queue[:] = [Resp(502)]
try: relay().post("C", "hi", key="k"); raise SystemExit("no raise")
except m.requests.HTTPError: pass
assert len(calls) == 1; n += 1
# 4 short 429 -> sleep then one retry
reset(); queue[:] = [Resp(429, {"retry_after": 1.5}), Resp(200)]; relay().post("C", "hi", key="k")
assert len(calls) == 2 and sleeps == [1.5]; n += 1
# 5 long 429 -> no sleep, raises after one call
reset(); queue[:] = [Resp(429, {"retry_after": 60})]
try: relay().post("C", "hi", key="k"); raise SystemExit("no raise")
except m.requests.HTTPError: pass
assert len(calls) == 1 and sleeps == []; n += 1
# 6 outbound: 3-part file, part #1 fails, next tick resumes at #1 only, same nonces
body = "\n".join(["x" * 1800] * 3)
tree = [{"type": "blob", "path": "notify/2026-09-16T2000-long.md", "sha": "s1"}]
st = {}
reset(); r = relay(st); r.blob_text = lambda sha: body
queue[:] = [Resp(200), Resp(502)]
try: r.outbound("C", tree); raise SystemExit("no raise")
except m.requests.HTTPError: pass
first_nonces = [c[1]["nonce"] for c in calls]
assert st["posting"] == {"notify/2026-09-16T2000-long.md": {"sha": "s1", "done": 1}} and "posted" not in st; n += 1
calls.clear(); r.outbound("C", tree)
second = [c[1]["nonce"] for c in calls]
assert len(second) == 2 and second[0] == first_nonces[1]
assert st["posting"] == {} and st["posted"] == ["notify/2026-09-16T2000-long.md"]; n += 1
# 7 already posted -> nothing sent; stale resume counter for a deleted file is dropped
reset(); st2 = {"posted": ["notify/2026-09-16T2000-long.md"], "posting": {"notify/gone.md": 2}}
r = relay(st2); r.blob_text = lambda sha: body; r.outbound("C", tree)
assert calls == [] and st2["posting"] == {}; n += 1
# 8 run link goes through post with key run-<url>, after the start is saved
reset()  # the trigger token comes from the test flux.env (ROUTINE_FIRE_TOKEN=x), no token file any more
class FixedDT(dt.datetime):
    @classmethod
    def now(cls, tz=None): return dt.datetime(2026, 9, 16, 14, 30, tzinfo=dt.timezone.utc)
m.datetime = FixedDT
r = relay(); r.maybe_fire(1, "C")
disc = [i for i, c in enumerate(calls) if c[0] == "discord"]
assert len(disc) == 1 and calls[disc[0]][1]["enforce_nonce"] and saves and saves[0] <= disc[0]; n += 1
# 9 run link failure swallowed, start kept
reset(); queue[:] = [Resp(502)]; r = relay(); r.maybe_fire(1, "C"); assert r.state["fire_pending"] is False; n += 1
# 10 file changed after a partial post: restart from part 0 with new keys; a shorter new version posts in full
P = "notify/2026-09-16T2000-long.md"
reset(); st3 = {}; r = relay(st3); r.blob_text = lambda sha: body
queue[:] = [Resp(200), Resp(502)]
try: r.outbound("C", tree); raise SystemExit("no raise")
except m.requests.HTTPError: pass
old_nonces = [c[1]["nonce"] for c in calls]
calls.clear(); r.blob_text = lambda sha: "short new text"
r.outbound("C", [{"type": "blob", "path": P, "sha": "s2"}])
assert len(calls) == 1 and "short new text" in calls[0][1]["content"] and calls[0][1]["nonce"] not in old_nonces
assert st3["posting"] == {} and st3["posted"] == [P]; n += 1
# 11 a legacy int counter (pre-fix state) is treated as a changed version: restart from part 0
reset(); st4 = {"posting": {P: 2}}; r = relay(st4); r.blob_text = lambda sha: body
r.outbound("C", tree); assert len(calls) == 3 and st4["posted"] == [P]; n += 1
# 12 a question file (callout in body) mentions the owner once, without the file-name header or callout syntax
Q = "notify/2026-09-17T0950-photobooth520-nexa-vs-pe.md"
reset(); r = relay({}); r.blob_text = lambda sha: "> [!question] Move Photobooth520 to Nexa?\n> Or keep both trackers?"
r.outbound("C", [{"type": "blob", "path": Q, "sha": "q1"}])
c = calls[0][1]
assert c["content"].startswith("<@100000000000000001> ❓ Move Photobooth520") and "[!question]" not in c["content"]
assert "**notify/" not in c["content"] and "\n> " not in c["content"]
assert c["allowed_mentions"] == {"parse": [], "users": ["100000000000000001"]}; n += 1
# 13 a normal notify file keeps its header and pings nobody
reset(); r = relay({}); r.blob_text = lambda sha: "Filed 2 capture(s)"
r.outbound("C", [{"type": "blob", "path": "notify/2026-09-17T0950-filed.md", "sha": "f1"}])
assert calls[0][1]["allowed_mentions"] == {"parse": []} and "**notify/2026-09-17T0950-filed.md**" in calls[0][1]["content"]; n += 1
# 14 a question named by its file (plain text body) also pings; a briefing never does
reset(); r = relay({}); r.blob_text = lambda sha: "Is this private or FEFW?"
r.outbound("C", [{"type": "blob", "path": "notify/2026-09-17T0639-art-storage-fefw-question.md", "sha": "a1"},
                 {"type": "blob", "path": "briefings/daily/2026-09-17.md", "sha": "b1"}])
qpost = [c[1] for c in calls if "notify/" not in c[1]["content"] and "briefings/" not in c[1]["content"]]
bpost = [c[1] for c in calls if "briefings/daily" in c[1]["content"]]
assert len(qpost) == 1 and qpost[0]["allowed_mentions"].get("users") == ["100000000000000001"]
assert len(bpost) == 1 and "users" not in bpost[0]["allowed_mentions"]; n += 1  # outbound posts in path order
# 15 a long question splits: only the first part carries the mention
reset(); r = relay({}); r.blob_text = lambda sha: "> [!question] " + "\n".join(["y" * 1800] * 2)
r.outbound("C", [{"type": "blob", "path": "notify/2026-09-17T1000-long-question.md", "sha": "l1"}])
assert len(calls) == 2 and "users" in calls[0][1]["allowed_mentions"] and "users" not in calls[1][1]["allowed_mentions"]; n += 1

# ---------- responsiveness quick wins (2026-09-17) ----------
def blob(path, sha="a" * 40, size=10): return {"type": "blob", "path": path, "sha": sha, "size": size}
KEEP = "inbox/2026-09-17T122915Z-keep-vault-setup.md"
GMAIL = "inbox/2026-09-17T0633Z-gmail-19fb26394223d95e.md"
RECON = "inbox/2026-09-17T1318Z-memory-reconcile-vault-setup.md"
TYPED = "inbox/Test nite 1656.md"
DISC = "inbox/2026-09-17T1501Z-1550159687578550363.md"
# 17 server-written notes (Keep, Gmail, reconcile) start at once: no settle timer, no received post
for hp in (KEEP, GMAIL, RECON, "inbox/2026-09-17T122915Z-keep-note-abc123.md", "inbox/2026-09-22T163859Z-tasks-sudesca.md"):
    assert m.HOST_NOTE.match(hp), hp
assert not m.HOST_NOTE.match(TYPED) and not m.HOST_NOTE.match(DISC) and not m.HOST_NOTE.match("inbox/keep-shopping.md")
reset(); st = {"obsidian_notes": {}}; r = relay(st); r.watch_obsidian_notes([blob(KEEP)], now=1000, channel="C")
assert st.get("fire_pending") is True and "obsidian_fire_at" not in st and calls == []; n += 1
# 18 a typed note settles 120 s and gets ONE received post (not again when edited)
reset(); st = {"obsidian_notes": {}}; r = relay(st); r.watch_obsidian_notes([blob(TYPED, "b" * 40)], now=1000, channel="C")
assert not st.get("fire_pending") and st["obsidian_fire_at"] == 1000 + m.PHONE_SETTLE
assert len(calls) == 1 and "received" in calls[0][1]["content"] and calls[0][1]["enforce_nonce"]
r.watch_obsidian_notes([blob(TYPED, "c" * 40)], now=1050, channel="C"); assert len(calls) == 1
r.watch_obsidian_notes([blob(TYPED, "c" * 40)], now=1050 + m.PHONE_SETTLE, channel="C"); assert st.get("fire_pending") is True; n += 1
# 19 round 2: a server note no longer waits for a settling typed note; a reconcile note starts the coalescing timer
reset(); st = {"obsidian_notes": {}}; r = relay(st)
r.watch_obsidian_notes([blob(TYPED, "b" * 40), blob(RECON)], now=1000)
assert st.get("fire_pending") is True and set(st["obsidian_pending"]) == {TYPED}
assert st["reconcile_first"] == 1000 and st["reconcile_hold_until"] == 1000 + m.RECONCILE_COALESCE
r.watch_obsidian_notes([blob(TYPED, "b" * 40), blob(RECON, "d" * 40)], now=1000 + 800)
assert st["reconcile_hold_until"] == 1000 + m.RECONCILE_MAX; n += 1  # capped at 15 min after the first
# 20 round 2: no clock windows any more (the run marker replaced them): a start at :58 or :02 goes ahead
for hh, mm in ((14, 58), (15, 2)):
    class DTW(dt.datetime):
        @classmethod
        def now(cls, tz=None, _h=hh, _m=mm): return dt.datetime(2026, 9, 17, _h, _m, 30, tzinfo=dt.timezone.utc)
    m.datetime = DTW
    reset(); st = {"fire_pending": True}; r = relay(st); r.maybe_fire(0, "C", [blob(TYPED)])
    assert any(c[0] == "fire" for c in calls), (hh, mm)
m.datetime = FixedDT; n += 1
# 21 empty inbox at start time: pending dropped, no start
reset(); st = {"fire_pending": True}; r = relay(st); r.maybe_fire(0, "C", [blob("inbox/.gitkeep", size=0), blob("wiki/x.md")])
assert st["fire_pending"] is False and not any(c[0] == "fire" for c in calls); n += 1
# 22 non-empty inbox: start message lists the waiting captures with their kind; filed this tick always starts
reset(); st = {"fire_pending": True}; r = relay(st); r.maybe_fire(0, "C", [blob(KEEP), blob(TYPED), blob(DISC)])
text = [c[1]["text"] for c in calls if c[0] == "fire"][0]
assert "Inbox now holds:" in text and "(Keep checklist edit, project vault-setup)" in text and "(Obsidian note)" in text and "(Discord)" in text; n += 1
reset(); st = {}; r = relay(st); r.maybe_fire(1, "C", []); assert any(c[0] == "fire" for c in calls); n += 1
# 23 👀 goes on before any attachment download, and a failed reaction never blocks filing
reset(); seen_calls = []
r = relay({}); r.discord = lambda method, path, **kw: seen_calls.append((method, path)) or Resp(200)
class Boom(Exception): pass
class S:
    def get(self, *a, **k): seen_calls.append(("download",)); raise Boom()
r.s = S()
msg = {"id": "1", "content": "doc", "timestamp": "2026-09-17T15:00:00+00:00", "attachments": [{"filename": "a.pdf", "url": "u", "size": 5}]}
try: r.file_message("C", msg); raise SystemExit("no raise")
except Boom: pass
assert seen_calls[0][0] == "PUT" and "%F0%9F%91%80" in seen_calls[0][1] and seen_calls[1] == ("download",)
r.discord = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")); r.seen("C", "1"); r.unseen("C", "1"); n += 1
# 24 a start whose only capture is a note the owner is still typing waits (live 16:05); with another capture it goes ahead
#    and the start message tells the run to leave the typed note alone (round 2 N1)
import time as _t
reset(); st = {"fire_pending": True, "obsidian_fire_at": 10**12, "obsidian_pending": [TYPED]}; r = relay(st)
r.maybe_fire(0, "C", [blob(TYPED)]); assert st["fire_pending"] is True and not any(c[0] == "fire" for c in calls)
r.maybe_fire(0, "C", [blob(TYPED), blob(DISC)])
text = [c[1]["text"] for c in calls if c[0] == "fire"][0]
assert "`inbox/Test nite 1656.md` (Obsidian note, still being edited: leave it)" in text and st["fired_inbox"] == [DISC]; n += 1
# 25 run marker: no start while fresh; when it goes away the start goes ahead at once (ceiling cleared)
MARK = {"type": "blob", "path": ".run/active", "sha": "m1", "size": 30}
fresh = dt.datetime.fromtimestamp(_t.time() - 60, dt.timezone.utc).strftime("started: %Y-%m-%dT%H:%M:%SZ")
reset(); st = {"fire_pending": True, "fire_ceiling_until": _t.time() + 100}; r = relay(st); r.blob_text = lambda sha: fresh
r.maybe_fire(0, "C", [blob(DISC), MARK]); assert st.get("run_active") is True and not any(c[0] == "fire" for c in calls)
r.maybe_fire(0, "C", [blob(DISC)]); assert "run_active" not in st and any(c[0] == "fire" for c in calls); n += 1
# 26 a stale marker (10+ min) is ignored
stale = dt.datetime.fromtimestamp(_t.time() - 700, dt.timezone.utc).strftime("started: %Y-%m-%dT%H:%M:%SZ")
reset(); st = {"fire_pending": True}; r = relay(st); r.blob_text = lambda sha: stale
r.maybe_fire(0, "C", [blob(DISC), MARK]); assert not st.get("run_active") and any(c[0] == "fire" for c in calls); n += 1
# 27 after our own start, a second start waits for the 180 s ceiling when no marker shows
reset(); st = {"fire_pending": True}; r = relay(st); r.maybe_fire(0, "C", [blob(DISC)])
assert st["fire_ceiling_until"] > _t.time() + m.FIRE_MIN_INTERVAL - 5 and m.FIRE_MIN_INTERVAL == 180
calls.clear(); st["fire_pending"] = True; r.maybe_fire(0, "C", [blob(DISC)]); assert not any(c[0] == "fire" for c in calls)
st["fire_ceiling_until"] = _t.time() - 1; r.maybe_fire(0, "C", [blob(DISC)]); assert any(c[0] == "fire" for c in calls); n += 1
# 28 a capture our start listed and a finished run left behind re-arms ONCE (the run may have yielded to another run)
reset(); st = {"fired_inbox": [DISC]}; r = relay(st); r.blob_text = lambda sha: fresh
r.track_run_marker([blob(DISC), MARK], _t.time()); r.track_run_marker([blob(DISC)], _t.time())
assert st.get("fire_pending") is True and st["rearmed"] == [DISC]
st["fire_pending"] = False; r.track_run_marker([blob(DISC), dict(MARK, sha="m2")], _t.time()); r.track_run_marker([blob(DISC)], _t.time())
assert st["fire_pending"] is False; n += 1  # the same capture never loops
# 29 reconcile-only start waits for the coalescing timer; an own capture of the owner's goes at once and carries it
reset(); st = {"fire_pending": True, "reconcile_hold_until": _t.time() + 100}; r = relay(st)
r.maybe_fire(0, "C", [blob(RECON)]); assert not any(c[0] == "fire" for c in calls)
r.maybe_fire(0, "C", [blob(RECON), blob(KEEP)]); assert any(c[0] == "fire" for c in calls) and "reconcile_hold_until" not in st; n += 1
# 30 a new memory proposal starts the applier once, detached; the first sight only initialises
popens = []
m.subprocess.Popen = lambda cmd, **kw: popens.append((cmd, kw))
m.RECONCILE_LOG = tempfile.NamedTemporaryFile(delete=False).name
reset(); st = {}; r = relay(st); P1 = {"type": "blob", "path": "memory-proposals/2026-09-17T1231-vault-setup.md", "sha": "p", "size": 9}
m.CFG.mod_memory = False; r.trigger_apply([P1]); assert popens == [] and "proposals_seen" not in st  # module off: no-op
m.CFG.mod_memory = True   # memory module on for the rest of this test
r.trigger_apply([P1]); assert popens == [] and st["proposals_seen"] == [P1["path"]]
P2 = dict(P1, path="memory-proposals/2026-09-17T1700-vault-setup.md"); r.trigger_apply([P1, P2]); r.trigger_apply([P1, P2])
assert len(popens) == 1 and popens[0][0][:3] == m.RECONCILE_CMD[:3] and m.RECONCILE_CMD[2] == str(m.CFG.lock_dir / "flux-memory-reconcile.lock") and popens[0][1]["start_new_session"]; n += 1
# 31 manifest hints: Keep and reconcile notes name the page and its memory files; paths are backticked and cleaned
PAGE = {"type": "blob", "path": "wiki/projects/vault-setup.md", "sha": "pg", "size": 9}
blobs = {"k": "---\nsource: keep\nproject: vault-setup\npage: wiki/projects/vault-setup.md\n---\n", "r": "---\npage: vault-setup\n---\n",
         "pg": "---\nmemory: [second-brain-INDEX.md, vault-state.md]\n---\n"}
reset(); st = {"fire_pending": True}; r = relay(st); r.blob_text = lambda sha: blobs[sha]
r.maybe_fire(0, "C", [dict(blob(KEEP), sha="k"), dict(blob(RECON), sha="r"), PAGE])
text = [c[1]["text"] for c in calls if c[0] == "fire"][0]
assert "(Keep checklist edit, project vault-setup; page wiki/projects/vault-setup.md, memory: second-brain-INDEX.md, vault-state.md)" in text
assert "(memory reconcile, project vault-setup; page wiki/projects/vault-setup.md, memory:" in text
assert m.describe_inbox("inbox/a`b\x07c.md") == "`inbox/abc.md` (Obsidian note)"; n += 1
# ---------- two channels (2026-09-22): background posts go to the log channel, human-facing ones stay ----------
def chan(c): return c[2].split("/channels/")[1].split("/")[0]
FILED = {"type": "blob", "path": "notify/2026-09-22T0524-filed.md", "sha": "f2"}
QUEST = {"type": "blob", "path": "notify/2026-09-22T0519-question-test.md", "sha": "q2"}
DRAFT = {"type": "blob", "path": "notify/2026-09-22T0527-taches-urgentes.md", "sha": "d2"}
DAILY = {"type": "blob", "path": "briefings/daily/2026-09-22.md", "sha": "b2"}
# 32 with a log channel: only the run summary leaves the conversation channel; digests and drafts stay (the owner's choice)
assert m.is_run_summary(FILED["path"]) and not any(m.is_run_summary(p["path"]) for p in (QUEST, DRAFT, DAILY))
reset(); r = relay({}); r.log_channel = "L"; r.blob_text = lambda sha: "body"
r.outbound("C", [DAILY, FILED, QUEST, DRAFT])
routed = {c[1]["content"].split("\n")[0][:40]: chan(c) for c in calls}
assert [chan(c) for c in calls] == ["C", "C", "L", "C"], routed; n += 1  # path order: daily, 0519-question, 0524-filed, 0527
# 33 no log channel yet (attribute absent, as before the split): everything still goes to the conversation channel
reset(); r = relay({}); r.blob_text = lambda sha: "body"; r.outbound("C", [FILED, QUEST])
assert [chan(c) for c in calls] == ["C", "C"] and r.log_target("C") == "C"; n += 1
# 34 the "note received" courtesy post and the run link go to the log channel when there is one
reset(); st = {"obsidian_notes": {}}; r = relay(st); r.log_channel = "L"
r.watch_obsidian_notes([blob(TYPED, "d" * 40)], now=1000, channel="C")
assert len(calls) == 1 and "received" in calls[0][1]["content"] and chan(calls[0]) == "L"; n += 1
reset(); r = relay(); r.log_channel = "L"; r.maybe_fire(1, "C")
disc = [c for c in calls if c[0] == "discord"]
assert len(disc) == 1 and "watch it work" in disc[0][1]["content"] and chan(disc[0]) == "L"; n += 1
# 35 find_log_channel: cached id wins; a miss is logged once and leaves the fallback; either conversation name is accepted
reset(); r = relay({"log_channel_id": "L9"}); assert r.find_log_channel() == "L9"
class G:
    def __init__(self, names): self.names = names
    def request(self, method, url, **kw): return Resp(200, [{"type": 0, "name": nm, "id": f"id-{nm}"} for nm in self.names])
r = relay({}); r.s = G(["general", "flux", "flux-log"]); r.guild = "g"
assert r.find_channel() == "id-flux" and r.find_log_channel() == "id-flux-log" and "log_channel_missing" not in r.state
r = relay({}); r.s = G(["general", "flux"]); r.guild = "g"   # config names only (no legacy alias in the package)
assert r.find_channel() == "id-flux" and r.find_log_channel() is None and r.state["log_channel_missing"] is True; n += 1
print(f"{n} tests PASS")
