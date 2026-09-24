"""Logging, the ops alert and the secret definition shared by every Flux program."""
from datetime import datetime, timezone

import requests

from ..config import CFG
from .secrets import SECRET_PATTERNS  # noqa: E402  the one definition

__all__ = ["log", "ops_alert", "SECRET_PATTERNS", "redact"]


def log(msg):
    """One UTC-stamped line on stdout, flushed (every script's log is a cron/loop redirect of stdout)."""
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def ops_alert(text):
    """Failure alert to the optional ops webhook (flux.env OPS_WEBHOOK_URL). Best effort, never raises; without a
    webhook the log line is the alert. Here since 2026-09-24 (architecture review): the relay owned it, the Tasks
    module carried a copy and the Gmail module imported the whole relay, with its module-level setup, just for this."""
    if not CFG.ops_webhook:
        return
    try:
        requests.post(CFG.ops_webhook, json={"content": text}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        log(f"ops alert not sent ({exc.__class__.__name__})")


def redact(text):
    """(text with secret-shaped strings replaced, number replaced)."""
    return SECRET_PATTERNS.subn("[REDACTED]", text or "")
