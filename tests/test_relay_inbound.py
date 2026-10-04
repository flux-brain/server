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


# ---------- transcript echo (a voice note is answered with what was heard) ----------
V = {"id": "102", "author": {"bot": False}, "type": 0, "content": "", "timestamp": "2026-09-17T10:02:00.000000+00:00",
     "attachments": [{"filename": "voice-message.ogg", "url": "https://cdn/v.ogg?ex=1", "size": 9000, "content_type": "audio/ogg"}]}
METHOD = "Whisper small transcript, language en (99%), 7 s"


def heard(monkeypatch, text, method=METHOD):
    monkeypatch.setattr(m.inbound, "extract_text", lambda data, mime, name: (text, method))


def test_voice_note_is_answered_with_its_transcript(make, monkeypatch):
    heard(monkeypatch, "[00:00] Call the notary on Monday.\n[00:04] Budget is 1,200.")
    r = make({"last_message_id": "101"}, [V], set())
    assert r.inbound("C") == 1 and len(r.posts) == 1
    content, kw = r.posts[0]
    assert kw == {"reply_to": "102", "key": "heard-102-0", "suppress_embeds": True}
    assert "> Call the notary on Monday.\n> Budget is 1,200." in content and "[00:0" not in content   # stamps dropped
    assert METHOD in content and "Reply to correct" in content
    assert content.startswith("🎙️ Heard (inbox/2026-09-17T1002Z-102.md, ")   # in_reply_to keeps the start: the note's path
    assert [p[0] for p in r.puts][-1] == "inbox/2026-09-17T1002Z-102.md"   # the capture itself is unchanged, filed first


def test_echo_redacts_a_spoken_or_embedded_secret_and_cuts_a_long_transcript(make, monkeypatch):
    heard(monkeypatch, "[00:00] the key is ghp_" + "a" * 36 + "\n" + "word " * 400)
    r = make({"last_message_id": "101"}, [V], set())
    r.inbound("C")
    content = r.posts[0][0]
    assert "ghp_" not in content and "[REDACTED]" in content
    assert "cut here" in content and len(content) < 2000


def test_no_echo_for_a_document_a_failed_transcription_or_when_switched_off(make, monkeypatch):
    r = make({"last_message_id": "99"}, [A], set())          # a PDF: text extracted, nothing heard
    r.inbound("C")
    assert not r.posts
    heard(monkeypatch, "", "extraction failed: RuntimeError")
    r = make({"last_message_id": "101"}, [V], set())
    assert r.inbound("C") == 1 and not r.posts
    heard(monkeypatch, "[00:00] hello")
    monkeypatch.setattr(m.CFG, "echo_transcripts", False)
    r = make({"last_message_id": "101"}, [V], set())
    assert r.inbound("C") == 1 and not r.posts


def test_silent_audio_says_so_and_a_failed_echo_never_blocks_the_filing(make, monkeypatch):
    heard(monkeypatch, "")
    r = make({"last_message_id": "101"}, [V], set())
    r.inbound("C")
    assert "no speech recognised" in r.posts[0][0]
    heard(monkeypatch, "[00:00] hello")
    st = {"last_message_id": "101"}
    r = make(st, [V], set())

    def boom(channel, content, **kw):
        raise requests.HTTPError("500")
    r.post = boom
    assert r.inbound("C") == 1 and st["last_message_id"] == "102" and st["message_failures"] == {}


# ---------- translation buttons under the echo ([capture] translate_to) ----------
def with_buttons(make, monkeypatch, text, method, langs):
    """A relay whose echo post returns an id and whose Discord calls are recorded; clean button state."""
    import shutil
    from flux_brain.lib import buttons
    for d in ("tracked", "actions"):
        shutil.rmtree(m.CFG.state_dir / d, ignore_errors=True)
    heard(monkeypatch, text, method)
    monkeypatch.setattr(m.CFG, "translate_to", langs)
    r = make({"last_message_id": "101"}, [V], set())
    inner, r.calls = r.discord, []

    def discord(method, path, **kw):
        r.calls.append((method, path))
        return inner(method, path, **kw)
    r.discord = discord
    r.post = lambda channel, content, **kw: (r.posts.append((content, kw)), "900")[1]
    return r, buttons


def test_echo_offers_the_other_language_when_the_note_language_is_sure(make, monkeypatch):
    r, buttons = with_buttons(make, monkeypatch, "[00:00] Bonjour à tous.", "Whisper medium transcript, language fr (97%), 3 s", ["en", "fr"])
    r.inbound("C")
    assert "Tap 🇬🇧 for a translation." in r.posts[0][0] and "🇫🇷" not in r.posts[0][0]
    puts = [p for mth, p in r.calls if mth == "PUT" and "/messages/900/reactions/" in p]
    assert len(puts) == 1                                         # the bot's own 🇬🇧 is the button
    (_, entry), = buttons.tracked()
    assert entry["module"] == "translate" and entry["message"] == "900" and list(entry["actions"]) == ["🇬🇧"]
    assert entry["actions"]["🇬🇧"] == {"code": "en", "language": "English", "note": "inbox/2026-09-17T1002Z-102.md",
                                        "transcript": "raw/attachments/2026-09-17T1002Z-102-voice-message.ogg.md", "voice_message": "102"}


def test_an_unsure_language_keeps_every_flag_and_no_setting_means_no_buttons(make, monkeypatch):
    r, buttons = with_buttons(make, monkeypatch, "[00:00] Hello.", "Whisper medium transcript, language fr (65%), 3 s", ["en", "fr", "xx"])
    r.inbound("C")
    assert "Tap 🇬🇧 or 🇫🇷 for a translation." in r.posts[0][0]         # "xx" is not a known language: ignored
    assert list(buttons.tracked()[0][1]["actions"]) == ["🇬🇧", "🇫🇷"]
    r, buttons = with_buttons(make, monkeypatch, "[00:00] Hello.", METHOD, [])
    r.inbound("C")
    assert "translation" not in r.posts[0][0] and buttons.tracked() == []
    r, buttons = with_buttons(make, monkeypatch, "", METHOD, ["en", "fr"])     # silence: nothing to translate
    r.inbound("C")
    assert buttons.tracked() == []


def test_a_tap_becomes_a_translate_capture_that_starts_a_run(make, monkeypatch):
    from flux_brain.lib import captures
    r, buttons = with_buttons(make, monkeypatch, "[00:00] Bonjour.", "Whisper medium transcript, language fr (97%), 3 s", ["en"])
    r.inbound("C")
    (_, entry), = buttons.tracked()
    buttons.emit("translate", "900", "🇬🇧", entry["actions"]["🇬🇧"])          # what the reaction check does after the grace period
    r.puts.clear()
    assert r.translation_requests() == 1 and buttons.actions("translate") == []
    (path, note), = r.puts
    assert captures.host_kind(path) == ("translate", "102-en")                 # a server note: the run starts on this tick
    assert "source: translate" in note and "language: English" in note
    assert "voice_note: inbox/2026-09-17T1002Z-102.md" in note and "transcript: raw/attachments/" in note and "Bonjour" not in note

    def broken(path, data, msg):
        raise requests.HTTPError("500")
    buttons.emit("translate", "900", "🇬🇧", entry["actions"]["🇬🇧"])
    r.put_file = broken
    assert r.translation_requests() == 0 and len(buttons.actions("translate")) == 1   # kept for the next tick
