"""Configuration and the capture-kinds table (2026-09-24). Since the configuration is no longer bound at import,
the branch check loads a second home in-process (`flux_home` fixture); only the missing-file warning, which is
printed when a process first loads its configuration, runs in a subprocess."""
import os
import subprocess
import sys

import flux_brain.relay as relay
from flux_brain import config
from flux_brain.config import CFG
from flux_brain.lib import captures, github
from conftest import ROOT

TASKS = "inbox/2026-09-22T163859Z-tasks-garden.md"
WA = "inbox/2026-09-22T180000Z-whatsapp-acct-abc123.md"
KEEPN = "inbox/2026-09-17T122915Z-keep-note-abc123.md"


def run_with_home(home, code):
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "FLUX_HOME": str(home)}, capture_output=True, text=True)


def test_vault_branch_reaches_every_client(flux_home):
    flux_home('[vault]\nrepo = "owner/vault"\nbranch = "trunk"\n', "GITHUB_TOKEN=t\n")
    assert CFG.vault_branch == "trunk" and github.GitHub(retry_writes=True).branch == "trunk"
    r = relay.Relay.__new__(relay.Relay)
    r.state, r.log_channel = {}, None
    posted = []
    r.blob_text, r.post = (lambda sha: "\n".join(["x" * 1800] * 6)), (lambda ch, content, **kw: posted.append(content))
    r.outbound("C", [{"type": "blob", "path": "notify/2026-09-24T1200-x.md", "sha": "s"}])
    assert "/blob/trunk/" in posted[-1]   # the tail cut of a long file links the page on the loaded branch


def test_load_switches_the_whole_package_and_the_default_home_comes_back(flux_home):
    cfg = flux_home('[vault]\nrepo = "someone/other"\n[tasks]\nprefix = "» "\n')
    assert CFG.vault_repo == "someone/other" and CFG.tasks_prefix == "» " and cfg is config.current()
    assert str(CFG.state_file("x.json")).startswith(str(cfg.home))
    config.load(os.environ["FLUX_HOME"])
    assert CFG.vault_repo == "owner/vault" and CFG.tasks_prefix == "📁 "


def test_missing_flux_toml_warns_present_one_does_not(tmp_path):
    load = "from flux_brain.config import CFG; CFG.home"   # importing alone reads nothing; the first access loads
    proc = run_with_home(tmp_path, load)
    assert proc.returncode == 0 and "not found" in proc.stderr and str(tmp_path) in proc.stderr
    assert "not found" not in run_with_home(tmp_path, "import flux_brain.config, flux_brain.relay").stderr   # import only: silent
    (tmp_path / "flux.toml").write_text('[vault]\nrepo = "owner/vault"\n')
    assert "not found" not in run_with_home(tmp_path, load).stderr


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
