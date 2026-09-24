"""Logging and the secret definition shared by every Flux script."""
from datetime import datetime, timezone

from .secrets import SECRET_PATTERNS  # noqa: E402  the one definition

__all__ = ["log", "SECRET_PATTERNS", "redact"]


def log(msg):
    """One UTC-stamped line on stdout, flushed (every script's log is a cron/loop redirect of stdout)."""
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def redact(text):
    """(text with secret-shaped strings replaced, number replaced)."""
    return SECRET_PATTERNS.subn("[REDACTED]", text or "")
