"""Self-mail (2026-10-20): send a briefing to the owner's OWN mailbox, and nowhere else.

The one exception to "no Flux module sends mail" (tests/test_followups.py allows exactly this file). The recipient is
read from the Gmail profile of the token's account at send time: there is no recipient argument, no config key and
nothing taken from the vault, so a briefing (written from untrusted captures) cannot steer a mail elsewhere. The From
is the same address. Gmail triage already skips mail from the owner's own addresses (triage.Triage.candidates), and
the Gmail module files only labelled or followed conversations, so a self-sent briefing is never filed back.
"""
import base64
import html
import re
from email.message import EmailMessage

from .google import GoogleToken

__all__ = ["send_to_self", "md_to_html"]

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text):
    """Escape, then turn [x](https://...) into a link and **x** into bold. Only http(s) links become <a>."""
    out = html.escape(text, quote=False)
    out = LINK.sub(lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', out)
    return BOLD.sub(r"<b>\1</b>", out)


def md_to_html(md):
    """A small Markdown subset for a mail body: front matter dropped, #-headings, - bullets (two-space nesting),
    paragraphs, bold and links. Enough for a briefing; anything else stays plain escaped text."""
    lines = md.splitlines()
    if lines and lines[0].strip() == "---":                       # front matter: not for the reader
        end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), 0)
        lines = lines[end + 1:]
    out, depth = [], 0
    for line in lines:
        m = re.match(r"^(\s*)[-*] (.*)$", line)
        want = (len(m.group(1)) // 2 + 1) if m else 0
        while depth < want:
            out.append("<ul>")
            depth += 1
        while depth > want:
            out.append("</ul>")
            depth -= 1
        if m:
            out.append(f"<li>{_inline(m.group(2))}</li>")
        elif (h := re.match(r"^(#{1,4}) (.*)$", line)):
            n = len(h.group(1))
            out.append(f"<h{n}>{_inline(h.group(2))}</h{n}>")
        elif line.strip():
            out.append(f"<p>{_inline(line.strip())}</p>")
    out += ["</ul>"] * depth
    return '<div style="font-family:sans-serif;font-size:15px;line-height:1.45">' + "\n".join(out) + "</div>"


def send_to_self(session, token_path, subject, markdown):
    """Send `markdown` (plain part) + its HTML rendering to the token account's own address. Returns the message id.
    Raises on any HTTP error; the caller retries on a later tick."""
    token = GoogleToken(session, token_path, "Gmail", "flux-gmail-auth")
    r = session.get(GMAIL + "/profile", headers=token.headers(), timeout=30)
    r.raise_for_status()
    me = r.json()["emailAddress"]
    msg = EmailMessage()
    msg["From"] = msg["To"] = me
    msg["Subject"] = subject
    msg.set_content(markdown)
    msg.add_alternative(md_to_html(markdown), subtype="html")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    r = session.post(GMAIL + "/messages/send", headers=token.headers(), json={"raw": raw}, timeout=60)
    r.raise_for_status()
    return r.json().get("id", "")
