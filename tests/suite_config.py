"""Offline checks for the configuration and the capture-kinds table (2026-09-24, architecture review).

Own FLUX_HOME (not _load.py's): this suite needs a flux.toml with a non-default branch to prove the setting reaches
the GitHub client, which used to hard-code main. Covers: [vault] branch honoured everywhere, one table of server-
written capture kinds (settle rule, start message wording, page-hint eligibility agree), the warning when flux.toml
is missing, and the removal of settings nothing read (Keep flag, Kuma URLs).
"""
import os
import pathlib
import subprocess
import sys
import tempfile

HOME = tempfile.mkdtemp(prefix="flux-test-")
os.environ["FLUX_HOME"] = HOME
pathlib.Path(HOME, "flux.toml").write_text('[vault]\nrepo = "owner/vault"\nbranch = "trunk"\n')
pathlib.Path(HOME, "flux.env").write_text("GITHUB_TOKEN=t\n")
pathlib.Path(HOME, "state").mkdir()
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flux_brain.config import CFG  # noqa: E402
from flux_brain.lib import captures, github  # noqa: E402
import flux_brain.relay as relay  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    print(("ok   " if ok else "FAIL ") + name)
    fails += 0 if ok else 1


# 1 [vault] branch reaches every GitHub client and the relay's links
check("config reads the branch", CFG.vault_branch == "trunk")
check("GitHub client default branch is the configured one", github.BRANCH == "trunk" and github.GitHub(retry_writes=True).branch == "trunk")
check("relay links use the configured branch", relay.BRANCH == "trunk")

# 2 one table of capture kinds: the settle rule, the wording and the page-hint eligibility agree
TASKS = "inbox/2026-09-22T163859Z-tasks-garden.md"
WA = "inbox/2026-09-22T180000Z-whatsapp-acct-abc123.md"
KEEPN = "inbox/2026-09-17T122915Z-keep-note-abc123.md"
check("host notes: every registered kind is server-written", all(captures.HOST_NOTE.match(p) for p in (TASKS, WA, KEEPN)))
check("keep-note is not read as keep", captures.host_kind(KEEPN) == ("keep-note", "abc123"))
check("tasks capture is described as a checklist edit", relay.describe_inbox(TASKS) == "`" + TASKS + "` (Tasks checklist edit, project garden)")
check("whatsapp capture is described", "WhatsApp" in relay.describe_inbox(WA))
check("typed and relay notes are not host kinds", captures.host_kind("inbox/Test nite.md") is None
      and captures.host_kind("inbox/2026-09-17T1501Z-1550159687578550363.md") is None)
check("page hints: checklist and reconcile kinds carry a page, gmail and whatsapp do not",
      [captures.carries_page(k) for k in ("keep", "tasks", "memory-reconcile", "gmail", "whatsapp", "keep-note")]
      == [True, True, True, False, False, False])
# page_hints opens a Tasks capture (it used to skip everything but keep and memory-reconcile)
r = relay.Relay.__new__(relay.Relay)
blobs = {"t": "---\nsource: tasks\nproject: garden\npage: wiki/projects/garden.md\n---\n",
         "p": "---\nmemory: [garden-INDEX.md, garden-plan.md]\n---\n# Garden\n"}
r.blob_text = lambda sha: blobs[sha]
tree = [{"type": "blob", "path": TASKS, "sha": "t"}, {"type": "blob", "path": "wiki/projects/garden.md", "sha": "p"}]
check("page hint for a Tasks capture", r.page_hints([TASKS], tree) == {TASKS: "page wiki/projects/garden.md, memory: garden-INDEX.md, garden-plan.md"})

# 3 a missing flux.toml warns on stderr (a wrong FLUX_HOME on a host used to run silently against owner/vault)
empty = tempfile.mkdtemp(prefix="flux-test-empty-")
proc = subprocess.run([sys.executable, "-c", "import flux_brain.config"], cwd=ROOT, env={**os.environ, "FLUX_HOME": empty},
                      capture_output=True, text=True)
check("missing flux.toml warns", proc.returncode == 0 and "not found" in proc.stderr and empty in proc.stderr)
check("present flux.toml does not warn", "not found" not in subprocess.run(
    [sys.executable, "-c", "import flux_brain.config"], cwd=ROOT, env=os.environ, capture_output=True, text=True).stderr)

# 4 settings nothing read are gone
check("no dead config attributes", not hasattr(CFG, "mod_keep") and not hasattr(CFG, "kuma"))

print(f"FAILS: {fails}")
sys.exit(1 if fails else 0)
