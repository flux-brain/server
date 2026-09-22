"""Offline tests for the Drive module: query scope per drive type, idempotent upload, readable failures, and the
relay's attachment line with the module off (Discord copy linked) and on (Drive link). No network."""
import sys
from _load import relay as m, CFG  # noqa: E402  sets FLUX_HOME to a temp dir first
from flux_brain.lib import drive as d  # noqa: E402

class Resp:
    def __init__(self, code=200, js=None, headers=None): self.status_code, self._js, self.headers, self.ok = code, js or {}, headers or {}, code < 400
    def json(self): return self._js
    def raise_for_status(self):
        if not self.ok: raise RuntimeError(self.status_code)

class FakeSession:
    """Answers the token refresh, the name query and the two upload requests; records every call."""
    def __init__(self, existing=None): self.calls, self.existing = [], existing
    def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        if "oauth2" in url: return Resp(200, {"access_token": "at"})
        return Resp(200, {}, {"Location": "https://upload/session"})
    def get(self, url, **kw):
        self.calls.append(("get", url, kw)); return Resp(200, {"files": [{"id": "f1", "webViewLink": "https://drive/existing"}] if self.existing else []})
    def put(self, url, **kw):
        self.calls.append(("put", url, kw)); return Resp(200, {"id": "f2", "webViewLink": "https://drive/new"})

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name); fails += 0 if cond else 1

import json, os, pathlib
tok = pathlib.Path(CFG.home, "drive-token.json"); tok.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))

# 1 shared drive: corpora + driveId on the query; new file -> resumable upload -> new link
s = FakeSession(); dr = d.Drive(s, token_path=str(tok), folder="F", drive_id="D")
link = dr.upload("a.pdf", b"x", "application/pdf")
q = [c for c in s.calls if c[0] == "get"][0][2]["params"]
check("shared drive query scope", q["corpora"] == "drive" and q["driveId"] == "D" and q["supportsAllDrives"] == "true")
check("new file uploaded, link returned", link == "https://drive/new" and any(c[0] == "put" for c in s.calls))
# 2 My Drive: no corpora/driveId; existing name -> its link, no upload
s = FakeSession(existing=True); dr = d.Drive(s, token_path=str(tok), folder="F", drive_id="")
link = dr.upload("a.pdf", b"x", "application/pdf")
q = [c for c in s.calls if c[0] == "get"][0][2]["params"]
check("my drive query scope", "corpora" not in q and "driveId" not in q)
check("existing name reuses the link", link == "https://drive/existing" and not any(c[0] == "put" for c in s.calls))
# 3 token refreshed once per client
check("one token refresh", sum(1 for c in s.calls if c[0] == "post" and "oauth2" in c[1]) == 1)
# 4 readable failures: no folder, missing token file
try: d.Drive(s, token_path=str(tok), folder="", drive_id=""); check("empty folder refused", False)
except SystemExit as e: check("empty folder refused", "folder_id" in str(e))
try: d.Drive(s, token_path=str(tok) + ".missing", folder="F").headers(); check("missing token refused", False)
except SystemExit as e: check("missing token refused", "flux-drive-auth" in str(e))
# 5 relay attachment line: module off -> Discord copy linked, text file still written; on -> Drive link
class DL:
    content = b"%PDF"; ok = True
    def raise_for_status(self): pass
r = m.Relay.__new__(m.Relay); r.state = {}; puts = []
r.s = type("S", (), {"get": lambda self, url, **kw: DL()})(); r.put_file = lambda path, data, msg: puts.append(path)
m.extract_text = lambda data, mime, name: ("hello", "PDF text")
import datetime as dt
ts = dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc); msg = {"id": "1"}; att = {"url": "https://cdn/x.pdf", "size": 2048, "content_type": "application/pdf"}
CFG.mod_drive = False; r.drive = None
line = r.attachment_entry(msg, att, "x.pdf", "2026-09-22T0000Z", ts)
check("module off: Discord copy linked, marked as expiring", "(https://cdn/x.pdf)" in line and "expires" in line)
check("module off: text file still written", puts == ["raw/attachments/2026-09-22T0000Z-1-x.pdf.md"] and "(text)" in line)
CFG.mod_drive = True; r.drive = type("D", (), {"upload": lambda self, n, data, mime: "https://drive/up"})()
line = r.attachment_entry(msg, att, "x.pdf", "2026-09-22T0000Z", ts)
check("module on: Drive link", "(https://drive/up)" in line and "Google Drive" in line)
print(f"FAILS: {fails}"); sys.exit(1 if fails else 0)
