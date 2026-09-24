"""Offline tests for the Drive module: query scope per drive type, idempotent upload, readable failures, and the
relay's attachment line with the module off (Discord copy linked) and on (Drive link). No network."""
import datetime as dt

import pytest

import flux_brain.relay as m
from flux_brain.config import CFG
from flux_brain.lib import drive as d
from fakes import Resp


class FakeSession:
    """Answers the token refresh, the name query and the two upload requests; records every call."""

    def __init__(self, existing=None):
        self.calls, self.existing = [], existing

    def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        if "oauth2" in url:
            return Resp(200, {"access_token": "at"})
        return Resp(200, {}, {"Location": "https://upload/session"})

    def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return Resp(200, {"files": [{"id": "f1", "webViewLink": "https://drive/existing"}] if self.existing else []})

    def put(self, url, **kw):
        self.calls.append(("put", url, kw))
        return Resp(200, {"id": "f2", "webViewLink": "https://drive/new"})


def test_shared_drive_query_scope_and_resumable_upload(google_token):
    s = FakeSession()
    link = d.Drive(s, token_path=str(google_token), folder="F", drive_id="D").upload("a.pdf", b"x", "application/pdf")
    q = [c for c in s.calls if c[0] == "get"][0][2]["params"]
    assert q["corpora"] == "drive" and q["driveId"] == "D" and q["supportsAllDrives"] == "true"
    assert link == "https://drive/new" and any(c[0] == "put" for c in s.calls)


def test_my_drive_scope_and_existing_name_reuses_link(google_token):
    s = FakeSession(existing=True)
    link = d.Drive(s, token_path=str(google_token), folder="F", drive_id="").upload("a.pdf", b"x", "application/pdf")
    q = [c for c in s.calls if c[0] == "get"][0][2]["params"]
    assert "corpora" not in q and "driveId" not in q
    assert link == "https://drive/existing" and not any(c[0] == "put" for c in s.calls)
    assert sum(1 for c in s.calls if c[0] == "post" and "oauth2" in c[1]) == 1   # one token refresh per client


def test_readable_failures(google_token):
    s = FakeSession()
    with pytest.raises(SystemExit, match="folder_id"):
        d.Drive(s, token_path=str(google_token), folder="", drive_id="")
    with pytest.raises(SystemExit, match="flux-drive-auth"):
        d.Drive(s, token_path=str(google_token) + ".missing", folder="F").headers()


def test_relay_attachment_line_module_off_and_on(monkeypatch):
    class DL:
        content = b"%PDF"
        ok = True

        def raise_for_status(self):
            pass
    r = m.Relay.__new__(m.Relay)
    r.state, puts = {}, []
    r.s = type("S", (), {"get": lambda self, url, **kw: DL()})()
    r.put_file = lambda path, data, msg: puts.append(path)
    monkeypatch.setattr(m.inbound, "extract_text", lambda data, mime, name: ("hello", "PDF text"))
    ts = dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc)
    msg, att = {"id": "1"}, {"url": "https://cdn/x.pdf", "size": 2048, "content_type": "application/pdf"}
    monkeypatch.setattr(CFG, "mod_drive", False)
    r.drive = None
    line = r.attachment_entry(msg, att, "x.pdf", "2026-09-22T0000Z", ts)
    assert "(https://cdn/x.pdf)" in line and "expires" in line   # module off: Discord copy linked, marked as expiring
    assert puts == ["raw/attachments/2026-09-22T0000Z-1-x.pdf.md"] and "(text)" in line   # text file still written
    monkeypatch.setattr(CFG, "mod_drive", True)
    r.drive = type("D", (), {"upload": lambda self, n, data, mime: "https://drive/up"})()
    line = r.attachment_entry(msg, att, "x.pdf", "2026-09-22T0000Z", ts)
    assert "(https://drive/up)" in line and "Google Drive" in line
