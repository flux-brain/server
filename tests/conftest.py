"""Test bootstrap: FLUX_HOME points at a fresh temporary directory (minimal flux.toml, placeholder secrets, empty
state/) BEFORE any flux_brain module is imported, so no test can touch a live state file. pytest imports this
conftest before it collects the test modules, which is what makes the guard hold.

Why a hard rule: on the live system a test suite once wrote the production state file because the module default IS
production (2026-09-20). Here the default home is a temp dir that did not exist a second ago.

Tests that need a different home (a non-default branch, or proof that a module is NOT imported) run a subprocess
with their own FLUX_HOME; see test_config.py and test_layering.py.
"""
import os
import pathlib
import tempfile

import pytest

HOME = tempfile.mkdtemp(prefix="flux-test-")
os.environ["FLUX_HOME"] = HOME
pathlib.Path(HOME, "flux.toml").write_text(
    '[owner]\nname = "Test Owner"\ndiscord_user_id = "100000000000000001"\n'
    '[vault]\nrepo = "owner/vault"\n[discord]\nchannel = "flux"\nlog_channel = "flux-log"\n'
    '[routine]\nfire_url = "https://api.anthropic.com/v1/claude_code/routines/trig_test/fire"\n'
)
pathlib.Path(HOME, "flux.env").write_text("DISCORD_BOT_TOKEN=t\nDISCORD_GUILD_ID=g\nROUTINE_FIRE_TOKEN=x\n")
pathlib.Path(HOME, "state").mkdir()
ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _flux_home_is_temporary():
    """Every test runs against the temp home; a test that changed FLUX_HOME would fail the ones after it."""
    assert os.environ["FLUX_HOME"] == HOME
    yield


@pytest.fixture
def google_token(tmp_path):
    """A refresh-token file of the shape the consent commands write (fake ids)."""
    import json
    tok = tmp_path / "token.json"
    tok.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
    return tok
