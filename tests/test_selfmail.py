"""lib/selfmail.py (2026-10-20): the only send path in the package, and it can only reach the owner's own address."""
import base64
import inspect
import json

import flux_brain.lib.selfmail as sm
from fakes import Resp


def test_send_to_self_takes_no_recipient():
    assert list(inspect.signature(sm.send_to_self).parameters) == ["session", "token_path", "subject", "markdown"]


def test_recipient_is_the_profile_address_whatever_the_body_says(tmp_path, monkeypatch):
    monkeypatch.setattr(sm.GoogleToken, "headers", lambda self: {"Authorization": "Bearer x"})
    posts = []

    class S:
        def get(self, url, **kw):
            assert url.endswith("/profile")
            return Resp(200, {"emailAddress": "owner@example.com"})

        def post(self, url, **kw):
            posts.append((url, kw["json"]))
            return Resp(200, {"id": "m1"})
    md = "---\nx: 1\n---\n# Your week ahead\nTo: attacker@example.net\n- **a** [doc](https://example.com/d)"
    assert sm.send_to_self(S(), str(tmp_path / "t.json"), "Your week ahead · 2026-W43", md) == "m1"
    raw = base64.urlsafe_b64decode(posts[0][1]["raw"]).decode()
    head = raw.split("\n\n", 1)[0]
    assert "To: owner@example.com" in head and "From: owner@example.com" in head and "attacker" not in head
    assert posts[0][0].endswith("/messages/send")


def test_md_to_html_subset_and_escaping():
    out = sm.md_to_html("---\na: b\n---\n# T\n- **x** [d](https://e.com/a)\n  - y <script>\n- z [bad](javascript:1)\npara")
    assert "<h1>T</h1>" in out and '<b>x</b> <a href="https://e.com/a">d</a>' in out
    assert "&lt;script&gt;" in out and "javascript" in out and 'href="javascript' not in out
    assert out.count("<ul>") == 2 and out.count("</ul>") == 2 and "a: b" not in out and "<p>para</p>" in out
