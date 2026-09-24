"""Offline tests for inbound filing (2026-09-17, code review fix 2): a message whose attachment keeps failing is
retried strictly MESSAGE_GIVE_UP-1 times, then filed with the attachment listed as not fetched, so the queue never
blocks for good. No network: Discord, the download session, Drive and GitHub are fakes."""
import pytest
import requests

import flux_brain.relay as m
from fakes import Resp


class FakeSession:
    def __init__(self, fail_urls):
        self.fail, self.gets = set(fail_urls), []

    def get(self, url, **kw):
        self.gets.append(url)
        return Resp(404) if url in self.fail else Resp(200, content=b"%PDF-1.4 fake")


A = {"id": "100", "author": {"bot": False}, "type": 0, "content": "invoice attached",
     "timestamp": "2026-09-17T10:00:00.000000+00:00",
     "attachments": [{"filename": "inv.pdf", "url": "https://cdn/inv.pdf?ex=1", "size": 1000, "content_type": "application/pdf"}]}
B = {"id": "101", "author": {"bot": False}, "type": 0, "content": "plain note",
     "timestamp": "2026-09-17T10:01:00.000000+00:00", "attachments": []}


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setattr(m.state, "save_state", lambda st: None)
    monkeypatch.setattr(m.inbound, "extract_text", lambda data, mime, name: ("some text", "PDF text layer"))
    monkeypatch.setattr(m.CFG, "mod_drive", True)   # these cases exercise the Drive path; test_drive covers the module-off line

    def relay(state, msgs, fail_urls):
        r = m.Relay.__new__(m.Relay)
        r.state, r.dh, r.s = state, {}, FakeSession(fail_urls)

        def discord(method, path, **kw):
            if method == "GET" and path.endswith("/messages"):
                after = int(kw.get("params", {}).get("after", 0))
                return Resp(200, [x for x in msgs if int(x["id"]) > after])
            return Resp(200)   # reactions
        r.discord = discord
        r.puts, r.posts = [], []
        r.put_file = lambda path, data, msg: r.puts.append((path, data.decode()))
        r.drive_upload = lambda name, data, mime: "https://drive/x"
        r.post = lambda channel, content, **kw: r.posts.append((content, kw))
        return r
    return relay


def test_poisoned_attachment_two_strict_failures_then_lenient_filing(make):
    st = {"last_message_id": "99"}
    r = make(st, [A, B], {A["attachments"][0]["url"]})
    for attempt in (1, 2):
        with pytest.raises(requests.HTTPError):
            r.inbound("C")
        assert st["last_message_id"] == "99" and not r.puts, f"attempt {attempt}: queue not advanced, nothing filed"
        assert st["message_failures"] == {"100": attempt}
    n = r.inbound("C")
    assert n == 2 and [p[0] for p in r.puts] == ["inbox/2026-09-17T1000Z-100.md", "inbox/2026-09-17T1001Z-101.md"]
    assert "NOT fetched or stored after 3 attempts (HTTPError)" in r.puts[0][1] and "invoice attached" in r.puts[0][1]
    assert st["last_message_id"] == "101" and st["message_failures"] == {}
    assert len(r.posts) == 1 and r.posts[0][1].get("reply_to") == "100" and "inv.pdf (HTTPError)" in r.posts[0][0] and r.posts[0][1].get("key") == "degraded-100"


def test_transient_failure_second_attempt_files_normally(make):
    st = {"last_message_id": "99"}
    r = make(st, [A], {A["attachments"][0]["url"]})
    with pytest.raises(requests.HTTPError):
        r.inbound("C")
    r.s.fail.clear()   # the CDN answers now
    n = r.inbound("C")
    note = next(p for p in r.puts if p[0].startswith("inbox/"))   # the text file is put before the note
    assert n == 1 and "(https://drive/x)" in note[1] and "NOT fetched" not in note[1]
    assert any(p[0].startswith("raw/attachments/") for p in r.puts) and st["message_failures"] == {} and not r.posts


def test_message_without_attachments_never_touches_the_counter(make):
    st = {"last_message_id": "100"}
    r = make(st, [B], set())
    assert r.inbound("C") == 1 and st["message_failures"] == {} and st["last_message_id"] == "101"
