"""A small read-mostly Gmail client for the modules that look at the mailbox without filing it (2026-09-28): the
follow-up tracker, the triage list and the meeting prep. The Gmail module keeps its own client (it moves labels).

What it can do: read messages and threads (metadata only), list labels, create a label, add or remove a label on a
thread. What it cannot do, by construction: send, or write a draft. The token may carry gmail.send (an
MCP's token reused on a host), so there is no send path here at all and a test fails if one appears.
"""
import re
from email.utils import getaddresses, parseaddr

from .google import GoogleToken

__all__ = ["GmailApi", "header", "addresses", "is_bulk", "gmail_link", "NOREPLY"]

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
NOREPLY = re.compile(r"(?i)^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster|notifications?|alerts?)@")


class GmailApi:
    def __init__(self, session, token_path, command="flux-gmail-auth"):
        self.s = session
        self.token = GoogleToken(session, token_path, "Gmail", command)
        self._owner = None

    def call(self, method, path, **kw):
        r = self.s.request(method, GMAIL + path, headers=self.token.headers(), timeout=60, **kw)
        if r.status_code == 401:
            self.token.reset()
            r = self.s.request(method, GMAIL + path, headers=self.token.headers(), timeout=60, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    def search(self, q, limit=500):
        """Message stubs ({id, threadId}) matching a Gmail search, newest first, at most `limit`."""
        out, token = [], None
        while len(out) < limit:
            params = {"q": q, "maxResults": min(500, limit - len(out))}
            if token:
                params["pageToken"] = token
            page = self.call("GET", "/messages", params=params)
            out += page.get("messages", [])
            token = page.get("nextPageToken")
            if not token:
                break
        return out

    def thread(self, thread_id, headers=("From", "To", "Cc", "Subject", "Date", "List-Unsubscribe", "Precedence",
                                         "Auto-Submitted")):
        """The thread with metadata (the named headers, labelIds, internalDate, snippet) of every message."""
        return self.call("GET", f"/threads/{thread_id}", params=[("format", "metadata")]
                         + [("metadataHeaders", h) for h in headers])

    def labels(self):
        return {l["name"]: l["id"] for l in self.call("GET", "/labels").get("labels", [])}

    def ensure_label(self, name):
        """The id of label `name`, created (shown in the label list) when missing."""
        have = self.labels()
        if name in have:
            return have[name]
        return self.call("POST", "/labels", json={"name": name, "labelListVisibility": "labelShow",
                                                  "messageListVisibility": "show"})["id"]

    def label_thread(self, thread_id, add=(), remove=()):
        return self.call("POST", f"/threads/{thread_id}/modify",
                         json={"addLabelIds": list(add), "removeLabelIds": list(remove)})

    def owner_addresses(self):
        """The owner's own addresses (the profile address and every send-as alias), lower case."""
        if self._owner is None:
            own = {self.call("GET", "/profile")["emailAddress"].lower()}
            try:
                own |= {a["sendAsEmail"].lower() for a in self.call("GET", "/settings/sendAs").get("sendAs", [])}
            except Exception:  # noqa: BLE001 - aliases are a refinement; the profile address is enough to work
                pass
            self._owner = own
        return self._owner


def header(msg, name):
    """The value of header `name` on a metadata message, "" when absent."""
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def addresses(msg, *names):
    """[(display name, address lower case)] from the named headers of a message."""
    vals = [header(msg, n) for n in names]
    return [(n.strip(), a.lower()) for n, a in getaddresses([v for v in vals if v]) if a]


def is_bulk(msg):
    """A newsletter, a notification or an automatic reply: never a person waiting for (or owed) an answer."""
    sender = parseaddr(header(msg, "From"))[1]
    return bool(header(msg, "List-Unsubscribe") or header(msg, "Precedence").lower() in ("bulk", "list", "junk")
                or header(msg, "Auto-Submitted").lower() not in ("", "no") or NOREPLY.match(sender or ""))


def gmail_link(thread_id):
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}"
