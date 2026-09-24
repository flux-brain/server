"""Offline tests for the Gmail module (2026-09-17, the owner: "when an email is labeled, make sure its attachments are
processed"). No Gmail, Drive or GitHub: pure functions, the retry wrapper with a faked converter, and run() over an
in-memory API."""
import email
import types
from email import policy
from email.message import EmailMessage

import pytest

import flux_brain.gmail as gr


# ---------- real_attachments ----------
def test_real_files_kept_signature_logos_skipped():
    m = EmailMessage()
    m["Subject"] = "Invoice"
    m.set_content("See attached.")
    m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="CROZIER-261540.pdf")
    m.add_attachment(b"x" * 500, maintype="image", subtype="png", filename="logo.png", disposition="inline", cid="logo")
    m.add_attachment(b"y" * (30 * 1024), maintype="image", subtype="png", filename="scan.png", disposition="inline", cid="scan")
    names = [n for n, _, _ in gr.real_attachments(email.message_from_bytes(m.as_bytes(), policy=policy.default))]
    assert "CROZIER-261540.pdf" in names and "logo.png" not in names and "scan.png" in names


def test_attachment_inside_multipart_alternative_is_filed():
    alt = EmailMessage()
    alt["Subject"] = "BIOWELS"
    alt.set_content("Bonsoir")
    alt.add_alternative("<p>Bonsoir</p>", subtype="html")
    alt.get_payload()[1].add_related(b"%PDF-1.4 " + b"z" * 40000, maintype="application", subtype="pdf",
                                     filename="BOOK DE PRESSE.pdf", disposition="inline")
    parsed = email.message_from_bytes(alt.as_bytes(), policy=policy.default)
    assert not [p for p in parsed.iter_attachments() if p.get_filename()]   # standard iteration misses it (the bug)
    found = gr.real_attachments(parsed)
    assert [n for n, _, _ in found] == ["BOOK_DE_PRESSE.pdf"]
    assert len(found[0][2]) > 39000 and found[0][1] == "application/pdf"
    assert all(mt not in ("text/html", "text/plain") for _, mt, _ in found)


def test_forwarded_email_counted_once():
    fw = EmailMessage()
    fw["Subject"] = "Fwd"
    fw.set_content("see below")
    inner = EmailMessage()
    inner["Subject"] = "inner"
    inner.set_content("inner body")
    inner.add_attachment(b"%PDF-1.4 inner", maintype="application", subtype="pdf", filename="inner.pdf")
    fw.add_attachment(inner, filename="forwarded.eml")
    parsed = email.message_from_bytes(fw.as_bytes(), policy=policy.default)
    assert [mt for _, mt, _ in gr.real_attachments(parsed)] == ["message/rfc822"]


# ---------- extract_with_retry ----------
def test_retry_recovers_a_transient_converter_failure(monkeypatch):
    calls = []

    def flaky(blob, mime, name):
        calls.append(name)
        if len(calls) < 2:
            raise TimeoutError("pdftoppm")
        return "invoice text", "PDF text layer"
    monkeypatch.setattr(gr, "extract_text", flaky)
    monkeypatch.setattr(gr.time, "sleep", lambda s: None)
    assert gr.extract_with_retry(b"x", "application/pdf", "a.pdf") == ("invoice text", "PDF text layer")


def test_two_failures_give_empty_text_and_a_reason(monkeypatch):
    def always(blob, mime, name):
        raise RuntimeError("encrypted")
    monkeypatch.setattr(gr, "extract_text", always)
    monkeypatch.setattr(gr.time, "sleep", lambda s: None)
    assert gr.extract_with_retry(b"x", "application/pdf", "b.pdf") == ("", "extraction failed: RuntimeError")


def test_unconverted_type_gives_no_method(monkeypatch):
    monkeypatch.setattr(gr, "extract_text", lambda blob, mime, name: ("", None))
    assert gr.extract_with_retry(b"x", "video/mp4", "c.mp4") == ("", None)


# ---------- missing_text_note ----------
def test_missing_text_note():
    assert gr.missing_text_note([]) == []
    note = "\n".join(gr.missing_text_note([("b.pdf", "extraction failed: RuntimeError"), ("c.mp4", "video/mp4 is not converted")]))
    assert "`b.pdf` (extraction failed: RuntimeError)" in note and "`c.mp4` (video/mp4 is not converted)" in note
    assert "ask the owner in notify/" in note and note.startswith("\n#### ⚠ Attachments without text")


# ---------- run(): stale relabel (fix 4) and per-thread give-up (fix 3) ----------
class RunHarness:
    def __init__(self, monkeypatch):
        self.calls_api, self.puts, self.notified = [], [], []
        monkeypatch.setattr(gr, "save_state", lambda st: None)
        monkeypatch.setattr(gr, "DRY", False)
        monkeypatch.setattr(gr, "ops_alert", lambda text: self.notified.append(text))

    def make(self, filed, pages, file_thread):
        r = gr.GmailRelay.__new__(gr.GmailRelay)
        r.st = {"filed": dict(filed), "failures": 0}
        r.labels = {gr.CFG.gmail_label: "L1", gr.CFG.gmail_filed_label: "L2"}
        r.label_ids = lambda: r.labels

        def api(method, path, **kw):
            self.calls_api.append((method, path, kw.get("json")))
            return {"messages": pages} if path == "/messages" else {}
        r.api = api
        r.file_thread = file_thread
        r.gh = types.SimpleNamespace(put_file=lambda path, data, msg: self.puts.append((path, data.decode())))
        return r

    def mods(self):
        return [c[2] for c in self.calls_api if c[1] == "/messages/batchModify"]


@pytest.fixture
def rh(monkeypatch):
    return RunHarness(monkeypatch)


def test_stale_filed_message_relabelled_first_new_one_filed(rh):
    r = rh.make({"old1": 1}, [{"id": "old1", "threadId": "T0"}, {"id": "new1", "threadId": "T1"}], lambda t, ids: (list(ids), "inbox/x.md"))
    n = r.run()
    mods = rh.mods()
    assert mods and mods[0]["ids"] == ["old1"] and mods[0]["removeLabelIds"] == ["L1"] and mods[0]["addLabelIds"] == ["L2"]
    assert n == 1 and ["new1"] in [x["ids"] for x in mods[1:]] and "new1" in r.st["filed"]
    assert all(x["ids"] != ["old1"] for x in mods[1:])   # the stale message is not refiled


def test_bad_thread_skipped_then_stubbed_after_three_attempts(rh):
    def bad_then_good(t, ids):
        if t == "TA":
            raise RuntimeError("Drive 403")
        return list(ids), "inbox/x.md"
    r = rh.make({}, [{"id": "a1", "threadId": "TA"}, {"id": "b1", "threadId": "TB"}], bad_then_good)
    assert r.run() == 1 and r.st["thread_failures"] == {"TA": 1} and "b1" in r.st["filed"]
    r.run()
    assert r.st["thread_failures"] == {"TA": 2} and not rh.puts and not rh.notified
    r.run()
    assert rh.puts and rh.puts[0][0].endswith("-gmail-a1.md") and "kind: unprocessed" in rh.puts[0][1]
    assert "a1" not in r.st["filed"] and r.st["thread_failures"] == {}   # NOT marked filed: a re-label retries it
    assert any(mm and mm.get("ids") == ["a1"] for mm in rh.mods())
    assert "RuntimeError: Drive 403" in rh.puts[0][1] and "#all/TA" in rh.puts[0][1]
    assert len(rh.notified) == 1 and "gave up" in rh.notified[0]


def test_tasks_hand_off_files_the_thread_under_the_project(monkeypatch):
    monkeypatch.setattr(gr, "save_state", lambda st: None)

    class FakeRelay(gr.GmailRelay):
        def __init__(self):
            self.st, self.calls, self.filed_args = {"filed": {}}, [], None

        def api(self, method, path, **kw):
            self.calls.append(path)
            if path.startswith("/messages/"):
                return {"id": path.split("/")[2], "threadId": "T9"}
            if path.startswith("/threads/"):
                return {"messages": [{"id": "m1"}, {"id": "m2"}]}
            raise AssertionError(path)

        def file_thread(self, thread_id, msg_ids, project=None, via=None):
            self.filed_args = (thread_id, msg_ids, project, via)
            return msg_ids, "inbox/x-gmail-m2.md"
    fr = FakeRelay()
    path = fr.file_message_id("abc123", project="project-x")
    assert path == "inbox/x-gmail-m2.md" and fr.filed_args == ("T9", ["m1", "m2"], "project-x", "tasks") and set(fr.st["filed"]) == {"m1", "m2"}
