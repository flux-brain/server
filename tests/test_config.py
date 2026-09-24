"""Configuration and the capture-kinds table (2026-09-24). The branch check and the missing-file warning need a
FLUX_HOME of their own, so they run in a subprocess; the rest runs in-process."""
import os
import subprocess
import sys


import flux_brain.relay as relay
from flux_brain.config import CFG
from flux_brain.lib import captures
from conftest import ROOT

TASKS = "inbox/2026-09-22T163859Z-tasks-garden.md"
WA = "inbox/2026-09-22T180000Z-whatsapp-acct-abc123.md"
KEEPN = "inbox/2026-09-17T122915Z-keep-note-abc123.md"


def run_with_home(home, code):
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "FLUX_HOME": str(home)}, capture_output=True, text=True)


def test_vault_branch_reaches_every_client(tmp_path):
    (tmp_path / "flux.toml").write_text('[vault]\nrepo = "owner/vault"\nbranch = "trunk"\n')
    (tmp_path / "flux.env").write_text("GITHUB_TOKEN=t\n")
    proc = run_with_home(tmp_path, "from flux_brain.lib import github as g; import flux_brain.relay as r\n"
                                   "print(g.BRANCH, g.GitHub(retry_writes=True).branch, r.BRANCH)")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["trunk", "trunk", "trunk"]


def test_missing_flux_toml_warns_present_one_does_not(tmp_path):
    proc = run_with_home(tmp_path, "import flux_brain.config")
    assert proc.returncode == 0 and "not found" in proc.stderr and str(tmp_path) in proc.stderr
    (tmp_path / "flux.toml").write_text('[vault]\nrepo = "owner/vault"\n')
    assert "not found" not in run_with_home(tmp_path, "import flux_brain.config").stderr


def test_every_registered_kind_is_server_written():
    assert all(captures.HOST_NOTE.match(p) for p in (TASKS, WA, KEEPN))
    assert captures.host_kind(KEEPN) == ("keep-note", "abc123")   # keep-note is not read as keep
    assert captures.host_kind("inbox/Test nite.md") is None
    assert captures.host_kind("inbox/2026-09-17T1501Z-1550159687578550363.md") is None


def test_descriptions():
    assert relay.describe_inbox(TASKS) == "`" + TASKS + "` (Tasks checklist edit, project garden)"
    assert "WhatsApp" in relay.describe_inbox(WA)


def test_page_hint_eligibility():
    assert [captures.carries_page(k) for k in ("keep", "tasks", "memory-reconcile", "gmail", "whatsapp", "keep-note")] == [True, True, True, False, False, False]


def test_page_hint_for_a_tasks_capture():
    r = relay.Relay.__new__(relay.Relay)
    blobs = {"t": "---\nsource: tasks\nproject: garden\npage: wiki/projects/garden.md\n---\n",
             "p": "---\nmemory: [garden-INDEX.md, garden-plan.md]\n---\n# Garden\n"}
    r.blob_text = lambda sha: blobs[sha]
    tree = [{"type": "blob", "path": TASKS, "sha": "t"}, {"type": "blob", "path": "wiki/projects/garden.md", "sha": "p"}]
    assert r.page_hints([TASKS], tree) == {TASKS: "page wiki/projects/garden.md, memory: garden-INDEX.md, garden-plan.md"}


def test_no_dead_config_attributes():
    assert not hasattr(CFG, "mod_keep") and not hasattr(CFG, "kuma")
