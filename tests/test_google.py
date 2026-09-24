"""flux_brain.lib.google, the one Google OAuth client the Drive, Gmail and Tasks modules share (2026-09-24)."""
import json
import sys

import pytest

import flux_brain.tasks as tk
from flux_brain.lib import drive as d
from flux_brain.lib import google as g
from fakes import Resp


class Session:
    def __init__(self, code=200):
        self.code, self.posts = code, []

    def post(self, url, **kw):
        self.posts.append((url, kw.get("data", {})))
        return Resp(self.code, {"access_token": f"at{len(self.posts)}"})


def test_one_exchange_per_process_cached_file_untouched(google_token):
    s = Session()
    t = g.GoogleToken(s, str(google_token), "Drive", "flux-drive-auth")
    h1, h2 = t.headers(), t.headers()
    assert len(s.posts) == 1 and h1 is h2 and h1["Authorization"] == "Bearer at1"
    assert s.posts[0][1]["grant_type"] == "refresh_token" and s.posts[0][1]["client_id"] == "c"
    assert s.posts[0][0] == g.TOKEN_URI
    assert json.loads(google_token.read_text())["refresh_token"] == "r"


def test_reset_exchanges_again(google_token):
    s = Session()
    t = g.GoogleToken(s, str(google_token), "Drive", "flux-drive-auth")
    t.headers()
    t.reset()
    assert t.headers()["Authorization"] == "Bearer at2" and len(s.posts) == 2


def test_failures_name_the_module_and_its_command(google_token):
    with pytest.raises(SystemExit, match="Gmail.*flux-gmail-auth"):
        g.GoogleToken(Session(), str(google_token) + ".missing", "Gmail", "flux-gmail-auth").headers()
    with pytest.raises(RuntimeError, match="Tasks.*HTTP 400.*flux-tasks-auth"):
        g.GoogleToken(Session(400), str(google_token), "Tasks", "flux-tasks-auth").headers()


def test_drive_and_tasks_clients_delegate(google_token):
    s = Session()
    dr = d.Drive(s, token_path=str(google_token), folder="F")
    dr.headers()
    dr.headers()
    assert isinstance(dr.token, g.GoogleToken) and len(s.posts) == 1
    api = tk.TasksApi(Session(), token_path=str(google_token))
    assert isinstance(api.token, g.GoogleToken) and api.headers()["Authorization"] == "Bearer at1"


def test_consent_command_usage_and_missing_extra(google_token, monkeypatch):
    with pytest.raises(SystemExit, match="flux-x-auth"):
        g.consent_main([], "flux-x-auth", ["scope"], str(google_token), "next")
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", None)   # the extra is not installed
    with pytest.raises(SystemExit, match=r"flux-brain\[google\]"):
        g.consent_main(["client.json"], "flux-x-auth", ["scope"], str(google_token), "next")
