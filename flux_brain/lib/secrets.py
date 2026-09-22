"""One definition of "looks like a secret", shared by the relay, the modules and the tests, so they never drift apart.
Structured token shapes only (webhook URLs, private keys, cloud API keys, GitHub/Slack/Stripe/GitLab/npm tokens, OAuth
tokens, JWTs); a generic `password=` heuristic would cost false positives here."""
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
)
