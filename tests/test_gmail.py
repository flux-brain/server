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


# ---------- where the originals go ([gmail] originals_to_drive, 2026-09-26, the owner: "for gmail, keep attachments
# with original email") ----------

def _relay_with_one_email(monkeypatch, mod_drive, to_drive):
    """A GmailRelay built through its real __init__ with Drive, GitHub, the Gmail token and the converter faked, fed one
    email with one PDF attachment. Returns (relay, drive uploads, GitHub puts)."""
    uploads, puts = [], []

    class FakeDrive:
        def __init__(self, s):
            pass

        def upload(self, name, data, mime):
            uploads.append(name)
            return f"https://drive.example/{name}"

    monkeypatch.setattr(gr, "DRY", False)
    monkeypatch.setattr(gr, "session", lambda: None)
    monkeypatch.setattr(gr, "GitHub", lambda: types.SimpleNamespace(put_file=lambda p, d, m: puts.append((p, d.decode()))))
    monkeypatch.setattr(gr, "Drive", FakeDrive)
    monkeypatch.setattr(gr, "GoogleToken", lambda *a: types.SimpleNamespace(headers=lambda: {}))
    monkeypatch.setattr(gr, "extract_with_retry", lambda blob, mime, name: ("invoice text", "text layer"))
    monkeypatch.setattr(gr.CFG, "mod_drive", mod_drive)
    monkeypatch.setattr(gr.CFG, "gmail_originals_to_drive", to_drive)
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = "Invoice", "a@example.com", "b@example.com"
    m.set_content("see attached")
    m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="invoice.pdf")
    raw = gr.base64.urlsafe_b64encode(m.as_bytes()).decode()
    r = gr.GmailRelay({"filed": {}})
    r.api = lambda method, path, **kw: {"internalDate": "1790000000000", "raw": raw}
    return r, uploads, puts


def test_originals_stay_in_gmail_when_originals_to_drive_is_false(monkeypatch):
    r, uploads, puts = _relay_with_one_email(monkeypatch, mod_drive=True, to_drive=False)
    ids, path = r.file_thread("T1", ["m1"])
    assert uploads == [] and r.drive is None                      # nothing copied to Drive
    capture = dict(puts)[path]
    text_file = next(d for p, d in puts if p.startswith("raw/attachments/"))
    assert "#all/m1" in capture and "Google Drive" not in capture   # the message itself, in Gmail
    assert "Original email, with its attachments, in Gmail" in capture
    assert "original in Gmail, attached to the email" in capture
    assert "invoice text" in text_file and "#all/m1" in text_file   # text still extracted, linked to the email


def test_originals_go_to_drive_by_default(monkeypatch):
    r, uploads, puts = _relay_with_one_email(monkeypatch, mod_drive=True, to_drive=True)
    ids, path = r.file_thread("T1", ["m1"])
    assert len(uploads) == 2 and uploads[0].endswith("-gmail-m1.eml") and uploads[1].endswith("-invoice.pdf")
    assert "Original email in Google Drive" in dict(puts)[path]


# ---------- the follow pass: a filed conversation stays followed ([gmail] follow_threads) ----------
class FollowHarness:
    """run() over an in-memory mailbox: `labelled` = messages carrying the label, `incoming` = what the recent-incoming
    search returns, `threads` = {thread id: [{"id", "labelIds"}]}, `filed_label_threads` = conversations with Filed."""

    def __init__(self, monkeypatch, follow=True):
        self.calls, self.filed_calls = [], []
        monkeypatch.setattr(gr, "save_state", lambda st: None)
        monkeypatch.setattr(gr, "DRY", False)
        monkeypatch.setattr(gr, "ops_alert", lambda text: None)
        monkeypatch.setattr(gr.CFG, "gmail_follow_threads", follow)
        monkeypatch.setattr(gr.CFG, "gmail_follow_days", 3)

    def make(self, st, labelled=(), incoming=(), threads=None, filed_label_threads=()):
        r = gr.GmailRelay.__new__(gr.GmailRelay)
        r.st = {"filed": {}, "failures": 0, **st}
        r.labels = {gr.CFG.gmail_label: "L1", gr.CFG.gmail_filed_label: "L2"}
        r.label_ids = lambda: r.labels

        def api(method, path, **kw):
            params = kw.get("params") or {}
            self.calls.append((method, path, params, kw.get("json")))
            if path == "/messages":
                return {"messages": list(incoming if "q" in params else labelled)}
            if path == "/threads":
                return {"threads": [{"id": t} for t in filed_label_threads]}
            if path.startswith("/threads/"):
                return {"messages": (threads or {})[path.split("/")[2]]}
            return {}
        r.api = api

        def file_thread(thread_id, ids, followup=False):
            self.filed_calls.append((thread_id, list(ids), followup))
            return list(ids), "inbox/x.md"
        r.file_thread = file_thread
        return r

    def thread_reads(self):
        return [c[1] for c in self.calls if c[1].startswith("/threads/")]

    def relabelled(self):
        return [c[3]["ids"] for c in self.calls if c[1] == "/messages/batchModify"]


def test_new_message_in_a_filed_conversation_is_filed_as_a_followup(monkeypatch):
    fh = FollowHarness(monkeypatch)
    r = fh.make({"filed": {"f1": 1}, "filed_threads": {"T1": 1}, "filed_threads_refreshed": gr.time.time()},
                incoming=[{"id": "n1", "threadId": "T1"}],
                threads={"T1": [{"id": "f1", "labelIds": ["L2"]}, {"id": "s1", "labelIds": ["SENT"]},
                                {"id": "d1", "labelIds": ["DRAFT"]}, {"id": "n1", "labelIds": ["INBOX"]}]})
    assert r.run() == 1
    # only the messages not filed yet, the owner's own reply included for context, the draft left out
    assert fh.filed_calls == [("T1", ["s1", "n1"], True)]
    assert fh.relabelled() == [["s1", "n1"]] and {"s1", "n1"} <= set(r.st["filed"])
    assert fh.calls[-1][3]["addLabelIds"] == ["L2"]   # now Filed, so the next run finds nothing


def test_nothing_new_in_a_filed_conversation_files_nothing(monkeypatch):
    fh = FollowHarness(monkeypatch)
    r = fh.make({"filed_threads": {"T1": 1}, "filed_threads_refreshed": gr.time.time()},
                incoming=[{"id": "f2", "threadId": "T1"}],   # recent, but it already carries Filed (state file lost)
                threads={"T1": [{"id": "f1", "labelIds": ["L2"]}, {"id": "f2", "labelIds": ["L2", "INBOX"]}]})
    assert r.run() == 0 and fh.filed_calls == [] and fh.relabelled() == []
    assert "f2" in r.st["filed"]                 # remembered, so the conversation is not read again next minute
    r.run()
    assert fh.thread_reads() == ["/threads/T1"]


def test_incoming_message_of_a_conversation_never_filed_is_ignored(monkeypatch):
    fh = FollowHarness(monkeypatch)
    r = fh.make({"filed_threads": {"T1": 1}, "filed_threads_refreshed": gr.time.time()},
                incoming=[{"id": "x1", "threadId": "T9"}])
    assert r.run() == 0 and fh.filed_calls == [] and fh.thread_reads() == []


def test_relabelled_conversation_goes_through_the_label_pass_only(monkeypatch):
    fh = FollowHarness(monkeypatch)
    r = fh.make({"filed_threads": {"T1": 1}, "filed_threads_refreshed": gr.time.time()},
                labelled=[{"id": "n1", "threadId": "T1"}], incoming=[{"id": "n1", "threadId": "T1"}],
                threads={"T1": [{"id": "f1", "labelIds": ["L2"]}, {"id": "n1", "labelIds": ["L1", "INBOX"]}]})
    assert r.run() == 1 and fh.filed_calls == [("T1", ["n1"], False)] and fh.thread_reads() == []


def test_message_with_the_label_in_another_run_is_left_to_the_label_pass(monkeypatch):
    fh = FollowHarness(monkeypatch)
    r = fh.make({"filed_threads": {"T1": 1}, "filed_threads_refreshed": gr.time.time()},
                incoming=[{"id": "n1", "threadId": "T1"}, {"id": "n2", "threadId": "T1"}],
                threads={"T1": [{"id": "n1", "labelIds": ["L1"]}, {"id": "n2", "labelIds": ["INBOX"]}]})
    r.run()
    assert fh.filed_calls == [("T1", ["n2"], True)]


def test_conversation_filed_by_the_tasks_hand_off_is_not_filed_again(monkeypatch):
    fh = FollowHarness(monkeypatch)
    r = fh.make({"filed": {"m1": 1, "m2": 1}, "filed_threads": {"T1": 1}, "filed_threads_refreshed": gr.time.time()},
                incoming=[{"id": "n1", "threadId": "T1"}],   # m1, m2 were filed without the Filed label
                threads={"T1": [{"id": "m1", "labelIds": ["INBOX"]}, {"id": "m2", "labelIds": ["SENT"]},
                                {"id": "n1", "labelIds": ["INBOX"]}]})
    r.run()
    assert fh.filed_calls == [("T1", ["n1"], True)]


def test_first_run_learns_the_filed_conversations_from_the_label_then_daily(monkeypatch):
    fh = FollowHarness(monkeypatch)
    r = fh.make({}, incoming=[{"id": "n1", "threadId": "T7"}], filed_label_threads=["T7", "T8"],
                threads={"T7": [{"id": "f1", "labelIds": ["L2"]}, {"id": "n1", "labelIds": ["INBOX"]}]})
    r.run()
    assert set(r.st["filed_threads"]) == {"T7", "T8"} and fh.filed_calls == [("T7", ["n1"], True)]
    r.run()
    assert [c[1] for c in fh.calls].count("/threads") == 1   # the label listing is not repeated within a day


def test_label_pass_first_and_a_shared_budget(monkeypatch):
    fh = FollowHarness(monkeypatch)
    monkeypatch.setattr(gr, "MAX_PER_RUN", 1)
    r = fh.make({"filed_threads": {"T1": 1}, "filed_threads_refreshed": gr.time.time()},
                labelled=[{"id": "a1", "threadId": "TA"}], incoming=[{"id": "n1", "threadId": "T1"}],
                threads={"T1": [{"id": "n1", "labelIds": ["INBOX"]}]})
    r.run()
    assert fh.filed_calls == [("TA", ["a1"], False)] and fh.thread_reads() == []
    assert "TA" in r.st["filed_threads"]          # a conversation filed by label is followed from now on


def test_follow_threads_off_changes_nothing(monkeypatch):
    fh = FollowHarness(monkeypatch, follow=False)
    r = fh.make({"filed_threads": {"T1": 1}}, incoming=[{"id": "n1", "threadId": "T1"}],
                threads={"T1": [{"id": "n1", "labelIds": ["INBOX"]}]})
    assert r.run() == 0 and fh.filed_calls == []
    assert all("q" not in c[2] and c[1] != "/threads" for c in fh.calls)


def test_followup_capture_says_so(monkeypatch):
    r, uploads, puts = _relay_with_one_email(monkeypatch, mod_drive=False, to_drive=False)
    ids, path = r.file_thread("T1", ["m1"], followup=True)
    capture = dict(puts)[path]
    assert "kind: followup" in capture and 'thread_id: "T1"' in capture
    assert "New message in a conversation already filed" in capture and "labelled" not in capture
    plain = dict(_relay_with_one_email(monkeypatch, mod_drive=False, to_drive=False)[2])
    assert plain == {}   # nothing written until file_thread runs
    r2, _, puts2 = _relay_with_one_email(monkeypatch, mod_drive=False, to_drive=False)
    _, path2 = r2.file_thread("T1", ["m1"])
    assert "kind: followup" not in dict(puts2)[path2]
