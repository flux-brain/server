"""Flux configuration: `flux.toml` (settings, committed nowhere but the server) + `flux.env` (secrets, mode 600).

Every instance-specific literal that used to sit in the scripts is resolved here once, at import time, into `CFG`.
Location: `$FLUX_HOME` (default `/var/lib/flux`); tests point `FLUX_HOME` at a temporary directory that holds a
minimal `flux.toml`, so no test ever touches a live state file.
"""
import os
import pathlib
import sys
import tomllib

DEFAULT_HOME = "/var/lib/flux"


def _read_env(path):
    """KEY=value lines, `#` comments, optional quotes. Missing file = no secrets (the caller decides if that is fatal)."""
    out = {}
    p = pathlib.Path(path)
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in ("'", '"') and v.count(v[0]) >= 2:      # quoted: up to the closing quote, comment after it ignored
            v = v[1:v.index(v[0], 1)]
        else:                                                # bare: a trailing `# comment` is dropped
            v = v.split("#", 1)[0].strip()
        out[k.strip()] = v
    return out


class Config:
    def __init__(self, home=None):
        self.home = pathlib.Path(home or os.environ.get("FLUX_HOME") or DEFAULT_HOME)
        toml_path = self.home / "flux.toml"
        if toml_path.exists():
            t = tomllib.loads(toml_path.read_text())
        else:
            # Defaults keep the tests and a first `flux-relay` tick working, but on a host they mean a wrong FLUX_HOME:
            # say so once per process instead of quietly talking to the placeholder repository (2026-09-24).
            t = {}
            print(f"flux: {toml_path} not found, running with defaults (is FLUX_HOME right?)", file=sys.stderr, flush=True)
        env = _read_env(self.home / "flux.env")
        g = lambda sect, key, default=None: t.get(sect, {}).get(key, default)  # noqa: E731
        # owner
        self.owner_name = g("owner", "name", "Owner")
        self.owner_discord_id = str(g("owner", "discord_user_id", ""))
        self.git_email = g("owner", "git_email", "flux@localhost")  # committer email on host-side writes
        # vault
        self.vault_repo = g("vault", "repo", "owner/vault")
        self.vault_branch = g("vault", "branch", "main")
        # discord
        self.channel_names = tuple(g("discord", "channel_names", None) or [g("discord", "channel", "flux")])
        self.log_channel_name = g("discord", "log_channel", "flux-log")
        self.discord_token = env.get("DISCORD_BOT_TOKEN", "")
        self.discord_guild = env.get("DISCORD_GUILD_ID", "")
        # routine
        self.fire_url = g("routine", "fire_url", "")
        self.fire_token = env.get("ROUTINE_FIRE_TOKEN", "")
        self.fire_min_interval = int(g("routine", "min_interval_s", 180))
        self.marker_fresh = int(g("routine", "marker_fresh_s", 600))
        # capture
        self.phone_settle = int(g("capture", "settle_s", 60))
        self.max_attachment_mb = int(g("capture", "max_attachment_mb", 20))
        self.audio_minutes = int(g("capture", "audio_minutes", 30))
        # paths
        self.state_dir = self.home / "state"
        self.log_dir = self.home / "logs"
        self.lock_dir = pathlib.Path(g("paths", "lock_dir", "/run/lock"))
        self.whisper_dir = str(self.home / "whisper")
        # modules
        self.mod_drive = bool(g("modules", "drive", False))
        self.mod_memory = bool(g("modules", "memory", False))
        self.mod_gmail = bool(g("modules", "gmail", False))
        self.mod_tasks = bool(g("modules", "tasks", False))
        self.drive_id = g("drive", "drive_id", "")
        self.drive_folder_id = g("drive", "folder_id", "")
        self.drive_token_file = str(self.home / g("drive", "token_file", "drive-token.json"))
        self.gmail_label = g("gmail", "label", "📁 Flux")
        self.gmail_filed_label = g("gmail", "filed_label", "📁 Flux/Filed")
        self.gmail_token_file = str(self.home / g("gmail", "token_file", "gmail-token.json"))
        self.tasks_token_file = str(self.home / g("tasks", "token_file", "tasks-token.json"))
        self.tasks_prefix = g("tasks", "prefix", "📁 ")
        self.tasks_tick = int(g("tasks", "tick_s", 60))
        self.tasks_settle = int(g("tasks", "settle_s", 45))
        # secrets
        self.github_token = env.get("GITHUB_TOKEN", "")
        self.ops_webhook = env.get("OPS_WEBHOOK_URL", "")

    def require(self, *names):
        """Fail early with a readable message when a needed secret is missing (called by the entry points, not at import)."""
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise SystemExit(f"flux: missing in {self.home}/flux.env or flux.toml: {', '.join(missing)}")


CFG = Config()
