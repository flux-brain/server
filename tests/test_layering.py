"""Import layering (2026-09-24): the Gmail and Tasks modules must not pull in the Discord relay, and the three share
one ops_alert. The "not imported" proof needs a fresh interpreter, hence the subprocess."""
import os
import subprocess
import sys

import flux_brain.gmail as gmail
import flux_brain.relay as relay
import flux_brain.tasks as tasks
from flux_brain.lib import common
from conftest import ROOT


def test_gmail_and_tasks_do_not_import_the_relay(tmp_path):
    (tmp_path / "flux.toml").write_text('[vault]\nrepo = "owner/vault"\n')
    proc = subprocess.run([sys.executable, "-c", "import sys, flux_brain.gmail, flux_brain.tasks; print('flux_brain.relay' in sys.modules)"],
                          cwd=ROOT, env={**os.environ, "FLUX_HOME": str(tmp_path)}, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "False"


def test_one_ops_alert_shared():
    assert gmail.ops_alert is common.ops_alert and tasks.ops_alert is common.ops_alert and relay.ops_alert is common.ops_alert


def test_ops_alert_never_raises(monkeypatch):
    monkeypatch.setattr(common.CFG, "ops_webhook", "")
    common.ops_alert("x")   # no webhook: silent no-op
    monkeypatch.setattr(common.CFG, "ops_webhook", "https://example.invalid/hook")
    monkeypatch.setattr(common.requests, "post", lambda url, **kw: (_ for _ in ()).throw(ConnectionError("down")))
    common.ops_alert("y")   # a webhook failure is swallowed
