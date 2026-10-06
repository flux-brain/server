"""One definition of "looks like a secret", shared by the relay, the modules and the tests, so they never drift apart.
Structured token shapes only (webhook URLs, private keys, cloud API keys, GitHub/Slack/Stripe/GitLab/npm tokens, OAuth
tokens, JWTs, capability URLs); a generic `password=` heuristic would cost false positives here."""
import re

SECRET_PATTERNS = re.compile(
    r"discord(app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{40,}"
    r"|cfut_[A-Za-z0-9_-]{20,}"
    r"|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY"
    r"|AIzaSy[A-Za-z0-9_-]{30,}|AQ\.Ab8[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|sk-(ant|or|proj)-[A-Za-z0-9_-]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
    r"|ya29\.[A-Za-z0-9_-]{30,}|1//0[A-Za-z0-9_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|aas_et/[A-Za-z0-9_+/=-]{20,}"
    r"|[sr]k_(live|test)_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{20,}|npm_[A-Za-z0-9]{36}"
    # Capability URLs (2026-09-30): a link whose path carries a 32-hex secret (a 128-bit token, e.g. a personal
    # config or download link) IS the credential. Exactly 32 hex, so git commit links (40) and digests (64) pass.
    r"|https?://[^\s/]+/(?:[^\s/]+/)*(?<![0-9A-Fa-f])[0-9A-Fa-f]{32}(?![0-9A-Fa-f])(?:/[^\s<>\"')\]]*)?"
)

# Sign-in links in filed email (2026-10-06). A "view and sign" or "log in" email carries its credential as a query
# parameter of an ordinary link (`...?login_request_token=<uuid>&doc_id=1`), which no structured pattern above matches,
# so the token went into the repository with the email. Only the VALUE is replaced, and only for a parameter whose
# name ENDS with a credential word and whose value is at least 12 characters: the link stays readable, short
# values (a language code, a page number, a postcode) and names like `keywords=` are left alone. Kept apart from
# SECRET_PATTERNS on purpose: the relay REFUSES a capture that matches SECRET_PATTERNS, and a link pasted in the
# capture channel must not be refused for carrying a `token=`. The Gmail module applies it to what it files.
LINK_TOKENS = re.compile(
    r"(?i)(?<=[?&;])((?:[\w.-]*[_.-])?(?:token|secret|passwd|password|otp|magic|ticket|signature|sig|auth|session"
    r"|nonce|apikey|key|code)=)[^&#\s<>\"')\]]{12,}")


def redact_link_tokens(text):
    """(text with the value of every credential-named link parameter replaced, number replaced)."""
    return LINK_TOKENS.subn(r"\1[REDACTED]", text or "")
