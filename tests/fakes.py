"""Fakes shared by the test modules (not collected: no test_ prefix)."""
import requests


class Resp:
    """A requests.Response stand-in: status, JSON body, headers, text, raw content."""

    def __init__(self, code=200, js=None, headers=None, content=b"", text=""):
        self.status_code, self._js, self.headers, self.content, self.text = code, js if js is not None else {}, headers or {}, content, text

    ok = property(lambda s: s.status_code < 400)

    def json(self):
        return self._js

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(str(self.status_code))
