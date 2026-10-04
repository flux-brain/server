"""Offline tests for reaction buttons (lib.buttons + the relay's check) and the Gmail triage module (2026-09-28):
only the OWNER's reaction acts, expiry, a deleted post; triage reasons, skips, first run, quiet hours, ✅ and ✍️.
No network: Gmail, Discord and GitHub are fakes; state lives in the temporary FLUX_HOME of conftest."""
import shutil
import time
import urllib.parse

import pytest

import flux_brain.relay as m
from flux_brain import triage as tr
from flux_brain.config import CFG
from flux_brain.lib import buttons
from fakes import Resp

OWNER = "100000000000000001"
OWN = "owner@example.com"


@pytest.fixture(autouse=True)
def clean_state():
    for d in ("tracked", "actions"):
        shutil.rmtree(CFG.state_dir / d, ignore_errors=True)
    yield


# ---------- buttons + relay ----------

def relay_with(reactors):
    r = m.Relay.__new__(m.Relay)
    r.state, r.calls = {}, []

    def message(t):
        """The Discord message object of a tracked post, its reaction counts built from `reactors` (bot + the rest);
        None when `reactors` says the post is gone."""
        out = []
        for emoji in t["actions"]:
            who = reactors(f"/channels/{t['channel']}/messages/{t['message']}/reactions/{urllib.parse.quote(emoji)}")
            if who.status_code == 404:
                return None
            out.append({"emoji": {"id": None, "name": emoji.replace("\ufe0f", "")}, "count": len(who.json()) + 1, "me": True})
        return {"id": t["message"], "reactions": out}

    def discord(method, path, **kw):
        r.calls.append((method, path))
        if method != "GET":
            return Resp(204)
        if path.endswith("/messages"):                    # the channel's message list: one call covers every post
            ch = path.split("/")[2]
            return Resp(200, [x for x in (message(t) for _, t in buttons.tracked() if t["channel"] == ch) if x])
        if "/reactions/" not in path:                     # one message by id (a post the list did not hold)
            t = next(t for _, t in buttons.tracked() if t["message"] == path.rsplit("/", 1)[1])
            x = message(t)
            return Resp(200, x) if x else Resp(404)
        return reactors(path)
    r.discord = discord
    return r


def test_owner_tap_waits_the_grace_period_then_emits(monkeypatch):
    monkeypatch.setattr(CFG, "button_grace", 600)
    buttons.track("m1", "c1", "triage", {"✅": {"thread": "t1"}, "✍️": {"thread": "t1"}}, 3600)
    r = relay_with(lambda path: Resp(200, [{"id": OWNER}] if "%E2%9C%85" in path else []))
    t0 = time.time()
    assert r.check_reactions(now=t0) == 0 and buttons.actions("triage") == []
    (_, entry), = buttons.tracked()
    assert entry["pending"]["emoji"] == "✅" and ("PUT", "/channels/c1/messages/m1/reactions/%E2%8F%B3/@me") in r.calls
    assert r.check_reactions(now=t0 + 300) == 0                       # still inside the grace period
    assert r.check_reactions(now=t0 + 660) == 1
    acts = buttons.actions("triage")
    assert len(acts) == 1 and acts[0][1]["emoji"] == "✅" and acts[0][1]["payload"] == {"thread": "t1"}
    assert buttons.tracked() == [] and ("PUT", "/channels/c1/messages/m1/reactions/%F0%9F%91%8C/@me") in r.calls


def test_removing_the_reaction_in_time_cancels(monkeypatch):
    monkeypatch.setattr(CFG, "button_grace", 600)
    buttons.track("m5", "c1", "triage", {"✍️": {"thread": "t5"}}, 3600)
    reactors = [[{"id": OWNER}]]
    r = relay_with(lambda path: Resp(200, reactors[0]))
    t0 = time.time()
    r.check_reactions(now=t0)
    reactors[0] = []                                                  # the owner takes the tap back
    r.check_reactions(now=t0 + 120)
    (_, entry), = buttons.tracked()
    assert "pending" not in entry and buttons.actions("triage") == []
    assert ("DELETE", "/channels/c1/messages/m5/reactions/%E2%8F%B3/@me") in r.calls
    assert r.check_reactions(now=t0 + 900) == 0 and buttons.actions("triage") == []


def test_someone_elses_reaction_does_nothing_and_checks_are_throttled():
    buttons.track("m2", "c1", "triage", {"✅": {"thread": "t2"}}, 3600)
    r = relay_with(lambda path: Resp(200, [{"id": "999"}]))
    now = time.time()
    assert r.check_reactions(now=now) == 0 and buttons.actions("triage") == [] and len(buttons.tracked()) == 1
    n = len(r.calls)
    r.check_reactions(now=now + 10)
    assert len(r.calls) == n                      # within REACTION_EVERY: no Discord call at all


def test_a_scan_is_one_list_call_and_asks_who_reacted_only_where_someone_did():
    for i in range(12):
        buttons.track(f"p{i}", "c1", "triage", {"✅": {"thread": f"t{i}"}, "✍️": {"thread": f"t{i}"}}, 3600)
    r = relay_with(lambda path: Resp(200, [{"id": OWNER}] if "/p7/" in path and "%E2%9C%8D" in path else []))
    assert r.check_reactions(now=time.time()) == 0
    gets = [p for mth, p in r.calls if mth == "GET"]
    assert gets == ["/channels/c1/messages", "/channels/c1/messages/p7/reactions/%E2%9C%8D%EF%B8%8F"]   # 2 calls, not 24
    pend = [t for _, t in buttons.tracked() if t.get("pending")]
    assert len(pend) == 1 and pend[0]["message"] == "p7" and pend[0]["pending"]["emoji"] == "✍️"   # found without its variation selector


def test_expired_or_deleted_posts_are_forgotten():
    buttons.track("m3", "c1", "triage", {"✅": {}}, -1)
    buttons.track("m4", "c1", "triage", {"✅": {}}, 3600)
    r = relay_with(lambda path: Resp(404))
    r.check_reactions(now=time.time())
    assert buttons.tracked() == [] and buttons.actions("triage") == []


# ---------- triage ----------

def msg(mid, tid, frm, subject="Hello", snippet="Can we talk", labels=(), **hdr):
    headers = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]
    headers += [{"name": k.replace("_", "-"), "value": v} for k, v in hdr.items()]
    return {"id": mid, "threadId": tid, "labelIds": list(labels), "snippet": snippet, "payload": {"headers": headers}}


class FakeApi:
    def __init__(self, inbox, threads=None):
        self.inbox, self.threads, self.labelled, self.created = inbox, threads or {}, [], []

    def owner_addresses(self):
        return {OWN}

    def search(self, q, limit=500):
        if q.startswith("in:sent"):
            return [{"id": "s1", "threadId": "st1"}]
        return [{"id": x["id"], "threadId": x["threadId"]} for x in reversed(self.inbox)]   # newest first

    def message(self, mid, headers=None):
        if mid == "s1":
            return {"payload": {"headers": [{"name": "To", "value": "Known <known@firm.example>"}]}}
        return next(x for x in self.inbox if x["id"] == mid)

    def thread(self, tid):
        return {"messages": self.threads.get(tid, [])}

    def labels(self):
        return {CFG.gmail_label: "LFLUX", CFG.gmail_filed_label: "LFILED"}

    def ensure_label(self, name):
        return "LFLUX"

    def label_thread(self, tid, add=(), remove=()):
        self.labelled.append((tid, list(add)))


class FakeBot:
    def __init__(self):
        self.posts, self.reacts = [], []

    def channel_id(self, names, cache):
        return "c1"

    def post(self, ch, text):
        self.posts.append(text)
        return f"p{len(self.posts)}"

    def react(self, ch, mid, e):
        self.reacts.append((mid, e))


class FakeGH:
    def __init__(self):
        self.puts = []

    def put_file(self, path, data, message):
        self.puts.append((path, data.decode()))


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(tr, "DRY", False)
    monkeypatch.setattr(CFG, "triage_keywords", ["Harbour"])
    monkeypatch.setattr(CFG, "triage_hours_utc", (0, 24))
    monkeypatch.setattr(CFG, "triage_max_posts", 8)
    monkeypatch.setattr(CFG, "triage_correspondents_days", 180)
    monkeypatch.setattr(CFG, "triage_ttl_days", 3)


def test_first_run_records_the_inbox_without_posting(cfg):
    t = tr.Triage(FakeApi([msg("a", "ta", "Anna <anna@x.example>")]), FakeGH(), FakeBot(), {})
    assert t.candidates() == [] and t.st["seen"] == ["a"]


def test_reasons_and_skips(cfg):
    inbox = [
        msg("r", "tr", "Rita <rita@x.example>"),                              # answers the owner's thread
        msg("k", "tk", "Known <known@firm.example>"),                         # someone the owner wrote to
        msg("w", "tw", "Walt <walt@y.example>", subject="Harbour lease"),       # keyword
        msg("n", "tn", "Nobody <nobody@z.example>"),                          # no reason
        msg("b", "tb", "News <news@list.example>", List_Unsubscribe="<x>", subject="Harbour digest"),   # bulk
        msg("f", "tf", "Rita <rita@x.example>", labels=("LFILED",)),          # already filed
        msg("o", "to", f"Me <{OWN}>"),                                        # the owner's own
    ]
    threads = {"tr": [msg("r0", "tr", f"Me <{OWN}>"), inbox[0]]}
    t = tr.Triage(FakeApi(inbox, threads), FakeGH(), FakeBot(), {"initialised": True})
    got = [(x[0]["id"], x[2]) for x in t.candidates()]
    assert got == [("r", "answers a conversation you wrote in"), ("k", "someone you have written to"), ("w", "mentions Harbour")]
    assert set(t.st["seen"]) == {"n", "b", "f", "o"}                          # decided and never looked at again


def test_post_adds_both_buttons_and_tracks(cfg):
    bot = FakeBot()
    t = tr.Triage(FakeApi([]), FakeGH(), bot, {"initialised": True})
    n = t.post([(msg("k", "tk", "Known <known@firm.example>", subject="Lease"), ("Known", "known@firm.example"),
                 "someone you have written to")])
    assert n == 1 and "**Known** · Lease" in bot.posts[0] and [e for _, e in bot.reacts] == ["✅", "✍️"]
    (_, entry), = buttons.tracked()
    assert entry["module"] == "triage" and entry["actions"]["✅"]["thread"] == "tk" and "k" in t.st["seen"]


def test_quiet_hours_keep_items_unseen(cfg, monkeypatch):
    monkeypatch.setattr(CFG, "triage_hours_utc", (0, 0))
    bot = FakeBot()
    t = tr.Triage(FakeApi([]), FakeGH(), bot, {"initialised": True})
    assert t.post([(msg("k", "tk", "K <k@x>"), ("K", "k@x"), "why")]) == 0 and bot.posts == [] and t.st["seen"] == []


def test_buttons_label_the_thread_and_draft_writes_a_capture(cfg):
    buttons.emit("triage", "p1", "✅", {"thread": "t1", "from": "Anna", "subject": "Lease"})
    buttons.emit("triage", "p2", "✍️", {"thread": "t2", "from": "Walt", "subject": "Harbour lease"})
    api, gh = FakeApi([]), FakeGH()
    assert tr.Triage(api, gh, FakeBot(), {}).act() == 2
    assert api.labelled == [("t1", ["LFLUX"]), ("t2", ["LFLUX"])]
    (path, note), = gh.puts
    assert path.startswith("inbox/") and path.endswith("-triage-t2.md")
    assert "source: triage" in note and "want: reply-draft" in note and 'thread_id: "t2"' in note
    assert buttons.actions("triage") == []                                    # done: never acted on twice


def test_pending_tap_is_rechecked_between_scans(monkeypatch):
    monkeypatch.setattr(CFG, "button_grace", 30)
    buttons.track("m6", "c1", "triage", {"✅": {"thread": "t6"}}, 3600)
    r = relay_with(lambda path: Resp(200, [{"id": OWNER}]))
    t0 = time.time()
    r.check_reactions(now=t0)                                         # scan: tap seen, ⏳
    assert r.check_reactions(now=t0 + 15) == 0                        # re-checked, grace not over
    assert r.check_reactions(now=t0 + 31) == 1                        # 31 s < REACTION_EVERY: acted anyway
    buttons.track("m7", "c1", "triage", {"✅": {"thread": "t7"}}, 3600)
    n = len(r.calls)
    r.check_reactions(now=t0 + 45)                                    # nothing pending, no scan due: no call
    assert len(r.calls) == n



def test_module_posts_suppress_link_previews():
    # Discord's preview bot must never fetch a link quoted from an email (2026-09-30: a pasted personal link was fetched)
    from flux_brain.lib import buttons
    sent = {}
    bot = object.__new__(buttons.Bot)
    bot.call = lambda method, path, **kw: sent.update(kw) or {"id": "m1"}
    assert bot.post("c1", "hello https://example.com") == "m1"
    assert sent["json"]["flags"] & 4
