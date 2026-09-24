"""Offline tests for flux_brain.lib.google, the one Google OAuth client the Drive, Gmail and Tasks modules share
(2026-09-24, architecture review: three copies of the refresh exchange and of the consent command before)."""
import json
import pathlib
import sys

from _load import CFG  # noqa: E402  sets FLUX_HOME to a temp dir first
from flux_brain.lib import google as g  # noqa: E402
from flux_brain.lib import drive as d  # noqa: E402
import flux_brain.tasks as tk  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += 0 if ok else 1


class Resp:
    def __init__(self, code, js=None): self.status_code, self._js = code, js or {}
    ok = property(lambda s: s.status_code < 400)
    def json(self): return self._js


class Session:
    def __init__(self, code=200): self.code, self.posts = code, []
    def post(self, url, **kw): self.posts.append((url, kw.get("data", {}))); return Resp(self.code, {"access_token": f"at{len(self.posts)}"})


tok = pathlib.Path(CFG.home, "t-token.json")
tok.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))

# 1 one exchange per process, the header carries the access token, the file is never written
s = Session(); t = g.GoogleToken(s, str(tok), "Drive", "flux-drive-auth")
h1 = t.headers(); h2 = t.headers()
check("one refresh exchange, cached", len(s.posts) == 1 and h1 is h2 and h1["Authorization"] == "Bearer at1")
check("exchange is a refresh_token grant with the file's ids", s.posts[0][1]["grant_type"] == "refresh_token" and s.posts[0][1]["client_id"] == "c")
check("default token endpoint", s.posts[0][0] == g.TOKEN_URI)
check("token file untouched", json.loads(tok.read_text())["refresh_token"] == "r")
# 2 reset() exchanges again (a 401 from the API)
t.reset(); h3 = t.headers()
check("reset exchanges a fresh token", len(s.posts) == 2 and h3["Authorization"] == "Bearer at2")
# 3 readable failures name the module and its consent command
try: g.GoogleToken(s, str(tok) + ".missing", "Gmail", "flux-gmail-auth").headers(); check("missing token refused", False)
except SystemExit as e: check("missing token refused, names the command", "Gmail" in str(e) and "flux-gmail-auth" in str(e))
try: g.GoogleToken(Session(400), str(tok), "Tasks", "flux-tasks-auth").headers(); check("refresh failure raised", False)
except RuntimeError as e: check("refresh failure names the command", "Tasks" in str(e) and "HTTP 400" in str(e) and "flux-tasks-auth" in str(e))
# 4 the modules use it: Drive and Tasks clients hold a GoogleToken; the Drive error message is unchanged for operators
s = Session(); dr = d.Drive(s, token_path=str(tok), folder="F"); dr.headers(); dr.headers()
check("Drive delegates, one exchange", isinstance(dr.token, g.GoogleToken) and len(s.posts) == 1)
api = tk.TasksApi(Session(), token_path=str(tok))
check("Tasks delegates", isinstance(api.token, g.GoogleToken) and api.headers()["Authorization"] == "Bearer at1")
# 5 the consent command: usage error without a file, a readable message without the extra
try: g.consent_main([], "flux-x-auth", ["scope"], str(tok), "next"); check("usage", False)
except SystemExit as e: check("consent: usage error names the command", "flux-x-auth" in str(e))
sys.modules["google_auth_oauthlib.flow"] = None   # simulate the extra not installed
try: g.consent_main(["client.json"], "flux-x-auth", ["scope"], str(tok), "next"); check("extra", False)
except SystemExit as e: check("consent: missing extra names pip install 'flux-brain[google]'", "flux-brain[google]" in str(e))
del sys.modules["google_auth_oauthlib.flow"]
print(f"FAILS: {fails}")
sys.exit(1 if fails else 0)
