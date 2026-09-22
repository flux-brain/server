"""Offline tests for the Gmail module's attachment handling (2026-09-17, the owner: "when an email is labeled, make
sure its attachments are processed"). No Gmail, Drive or GitHub: the module is imported and only pure functions plus
the retry wrapper (with a faked converter) are exercised.

  python tests/test_gmail.py
Exit 1 on any FAIL.
"""
import email
import sys
from email import policy
from email.message import EmailMessage

import _load  # noqa: F401,E402  sets FLUX_HOME to a temp dir first
import flux_brain.gmail as gr  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    fails += 0 if cond else 1


# ---------- real_attachments: real files kept, signature logos skipped ----------
m = EmailMessage()
m["Subject"] = "Invoice"
m.set_content("See attached.")
m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="CROZIER-261540.pdf")
m.add_attachment(b"x" * 500, maintype="image", subtype="png", filename="logo.png", disposition="inline", cid="logo")
m.add_attachment(b"y" * (30 * 1024), maintype="image", subtype="png", filename="scan.png", disposition="inline", cid="scan")
parsed = email.message_from_bytes(m.as_bytes(), policy=policy.default)
names = [n for n, _, _ in gr.real_attachments(parsed)]
check("real attachment kept", "CROZIER-261540.pdf" in names)
check("small inline logo skipped", "logo.png" not in names)
check("large inline image kept", "scan.png" in names)

# an attachment INSIDE multipart/alternative (Apple Mail): missed until 2026-09-17, see real_attachments
alt = EmailMessage()
alt["Subject"] = "BIOWELS"
alt.set_content("Bonsoir")
alt.add_alternative("<p>Bonsoir</p>", subtype="html")
alt.get_payload()[1].add_related(b"%PDF-1.4 " + b"z" * 40000, maintype="application", subtype="pdf",
                                 filename="BOOK DE PRESSE.pdf", disposition="inline")
alt_parsed = email.message_from_bytes(alt.as_bytes(), policy=policy.default)
check("standard iteration misses it (the bug)", not [p for p in alt_parsed.iter_attachments() if p.get_filename()])
found = gr.real_attachments(alt_parsed)
check("attachment inside the body part is filed", [n for n, _, _ in found] == ["BOOK_DE_PRESSE.pdf"])
check("its bytes are complete", len(found[0][2]) > 39000 and found[0][1] == "application/pdf")
check("body parts are not filed as attachments", all(m != "text/html" and m != "text/plain" for _, m, _ in found))

# a forwarded email attached whole: one entry, its own parts are not separate attachments
fw = EmailMessage()
fw["Subject"] = "Fwd"
fw.set_content("see below")
inner = EmailMessage()
inner["Subject"] = "inner"
inner.set_content("inner body")
inner.add_attachment(b"%PDF-1.4 inner", maintype="application", subtype="pdf", filename="inner.pdf")
fw.add_attachment(inner, filename="forwarded.eml")
fw_parsed = email.message_from_bytes(fw.as_bytes(), policy=policy.default)
check("forwarded email counted once", [m for _, m, _ in gr.real_attachments(fw_parsed)] == ["message/rfc822"])

# ---------- extract_with_retry: one retry, then a reason ----------
calls = []


def flaky(blob, mime, name):
    calls.append(name)
    if len(calls) < 2:
        raise TimeoutError("pdftoppm")
    return "invoice text", "PDF text layer"


gr.extract_text = flaky
gr.time.sleep = lambda s: None
check("retry recovers a transient converter failure", gr.extract_with_retry(b"x", "application/pdf", "a.pdf") == ("invoice text", "PDF text layer"))


def always(blob, mime, name):
    raise RuntimeError("encrypted")


gr.extract_text = always
text, why = gr.extract_with_retry(b"x", "application/pdf", "b.pdf")
check("two failures give an empty text and a reason", text == "" and why == "extraction failed: RuntimeError")

gr.extract_text = lambda blob, mime, name: ("", None)
check("type that is not converted gives no method", gr.extract_with_retry(b"x", "video/mp4", "c.mp4") == ("", None))

# ---------- missing_text_note ----------
check("no note when every attachment has text", gr.missing_text_note([]) == [])
note = "\n".join(gr.missing_text_note([("b.pdf", "extraction failed: RuntimeError"), ("c.mp4", "video/mp4 is not converted")]))
check("note names each file and its reason", "`b.pdf` (extraction failed: RuntimeError)" in note and "`c.mp4` (video/mp4 is not converted)" in note)
check("note tells the routine to ask the owner", "ask the owner in notify/" in note and note.startswith("\n#### ⚠ Attachments without text"))


# ---------- run(): stale relabel (2026-09-17 code review fix 4) and per-thread give-up (fix 3) ----------
import types
calls_api, puts, notified = [], [], []
gr.save_state = lambda st: None
gr.DRY = False
gr.ops_alert = lambda text: notified.append(text)  # the give-up ops alert (optional webhook in the package)


def make(filed, pages, file_thread):
    r = gr.GmailRelay.__new__(gr.GmailRelay)
    r.st = {"filed": dict(filed), "failures": 0}
    r.labels = {gr.LABEL: "L1", gr.FILED: "L2"}
    r.label_ids = lambda: r.labels

    def api(method, path, **kw):
        calls_api.append((method, path, kw.get("json")))
        return {"messages": pages} if path == "/messages" else {}
    r.api = api
    r.file_thread = file_thread
    r.gh = types.SimpleNamespace(put_file=lambda path, data, msg: puts.append((path, data.decode())))
    return r


calls_api.clear()
r = make({"old1": 1}, [{"id": "old1", "threadId": "T0"}, {"id": "new1", "threadId": "T1"}], lambda t, ids: list(ids))
n = r.run()
mods = [c[2] for c in calls_api if c[1] == "/messages/batchModify"]
check("stale filed message is relabelled to Filed first", mods and mods[0]["ids"] == ["old1"]
      and mods[0]["removeLabelIds"] == ["L1"] and mods[0]["addLabelIds"] == ["L2"])
check("new message filed and relabelled", n == 1 and ["new1"] in [x["ids"] for x in mods[1:]] and "new1" in r.st["filed"])
check("stale message is not refiled", all(x["ids"] != ["old1"] for x in mods[1:]))


def bad_then_good(t, ids):
    if t == "TA":
        raise RuntimeError("Drive 403")
    return list(ids)


pages = [{"id": "a1", "threadId": "TA"}, {"id": "b1", "threadId": "TB"}]
r = make({}, pages, bad_then_good)
check("run 1: bad thread skipped, the one behind it filed", r.run() == 1 and r.st["thread_failures"] == {"TA": 1} and "b1" in r.st["filed"])
r.run()
check("run 2: counted again, no stub yet", r.st["thread_failures"] == {"TA": 2} and not puts and not notified)
r.run()
check("run 3: stub filed, thread relabelled, counter cleared, NOT marked filed (a re-label retries it)",
      puts and puts[0][0].endswith("-gmail-a1.md") and "kind: unprocessed" in puts[0][1]
      and "a1" not in r.st["filed"] and r.st["thread_failures"] == {}
      and any(c[2] and c[2].get("ids") == ["a1"] for c in calls_api if c[1] == "/messages/batchModify"))
check("stub names the error and the Gmail link", "RuntimeError: Drive 403" in puts[0][1] and "#all/TA" in puts[0][1])
check("give-up alerts Discord once", len(notified) == 1 and "gave up" in notified[0])

print("FAILS:", fails)
sys.exit(1 if fails else 0)
