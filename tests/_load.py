"""Test bootstrap: point FLUX_HOME at a fresh temporary directory (with a minimal flux.toml and no secrets) BEFORE
flux_brain is imported, so no test can touch a live state file, then import the package modules.

Why a hard rule: on the live system a test suite once wrote the production state file because the module default IS
production (2026-09-20). Here the default home is a temp dir that did not exist a second ago."""
import os
import pathlib
import sys
import tempfile

HOME = tempfile.mkdtemp(prefix="flux-test-")
os.environ["FLUX_HOME"] = HOME
pathlib.Path(HOME, "flux.toml").write_text(
    '[owner]\nname = "Test Owner"\ndiscord_user_id = "100000000000000001"\n'
    '[vault]\nrepo = "owner/vault"\n[discord]\nchannel = "flux"\nlog_channel = "flux-log"\n'
    '[routine]\nfire_url = "https://api.anthropic.com/v1/claude_code/routines/trig_test/fire"\n'
)
pathlib.Path(HOME, "flux.env").write_text("DISCORD_BOT_TOKEN=t\nDISCORD_GUILD_ID=g\nROUTINE_FIRE_TOKEN=x\n")
pathlib.Path(HOME, "state").mkdir()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import flux_brain.relay as relay  # noqa: E402
from flux_brain.lib import extract, secrets  # noqa: E402
from flux_brain.config import CFG  # noqa: E402
