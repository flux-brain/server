"""Offline check of the import layering (2026-09-24, architecture review).

The Gmail module used to import the whole Discord relay, with its module-level setup, only to reach the ops alert,
and the Tasks module carried a copy of it. Own FLUX_HOME (not _load.py's, which imports the relay itself).
"""
import os
import pathlib
import sys
import tempfile

HOME = tempfile.mkdtemp(prefix="flux-test-")
os.environ["FLUX_HOME"] = HOME
pathlib.Path(HOME, "flux.toml").write_text('[vault]\nrepo = "owner/vault"\n')
pathlib.Path(HOME, "state").mkdir()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import flux_brain.gmail as gmail  # noqa: E402
import flux_brain.tasks as tasks  # noqa: E402
from flux_brain.lib import common  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += 0 if ok else 1


check("the Gmail and Tasks modules do not import the relay", "flux_brain.relay" not in sys.modules)
check("one ops_alert, shared", gmail.ops_alert is common.ops_alert and tasks.ops_alert is common.ops_alert)
import flux_brain.relay as relay  # noqa: E402
check("the relay uses the same one", relay.ops_alert is common.ops_alert)
# without a webhook the alert is a no-op that never raises (the log line is the alert)
common.CFG.ops_webhook = ""
common.ops_alert("x")
check("no webhook: silent no-op", True)
# with a webhook, a network failure is swallowed
sent = []
common.requests.post = lambda url, **kw: (_ for _ in ()).throw(ConnectionError("down"))
common.CFG.ops_webhook = "https://example.invalid/hook"
common.ops_alert("y")
check("webhook failure never raises", True)
print(f"FAILS: {fails}")
sys.exit(1 if fails else 0)
