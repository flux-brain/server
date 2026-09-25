"""Flux configuration: `flux.toml` (settings, committed nowhere but the server) + `flux.env` (secrets, mode 600).

Every instance-specific literal that used to sit in the scripts is resolved here into `CFG`. Since 2026-09-24 nothing
is read at import time: `CFG` is a proxy to the current `Config`, built from `$FLUX_HOME` (default `/var/lib/flux`) on
first access, and `load(home)` replaces it, so a test can point the whole package at a temporary home without
re-importing anything, and no module binds a setting into a constant that a later `load()` could not reach.
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
    """One parsed home. Modules read `CFG.<name>` when they need a value, never at import."""

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
        # derived values the modules used to compute at import
        self.max_attachment_bytes = self.max_attachment_mb * 1024 * 1024
        self.max_audio_seconds = self.audio_minutes * 60
        # modules
        self.mod_drive = bool(g("modules", "drive", False))
        self.mod_memory = bool(g("modules", "memory", False))
        self.mod_gmail = bool(g("modules", "gmail", False))
        self.mod_tasks = bool(g("modules", "tasks", False))
        self.mod_calendar = bool(g("modules", "calendar", False))
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
        # Calendar module (2026-09-25): which calendars ("all", "selected" or a list of ids), the window, the time zone
        # (empty = the primary calendar's), whether attendees + descriptions are written, the vault folder.
        self.calendar_token_file = str(self.home / g("calendar", "token_file", "calendar-token.json"))
        self.calendar_calendars = g("calendar", "calendars", "selected")
        self.calendar_exclude = list(g("calendar", "exclude", []))
        self.calendar_lookahead = int(g("calendar", "lookahead_days", 7))
        self.calendar_timezone = g("calendar", "timezone", "")
        self.calendar_details = bool(g("calendar", "details", False))
        self.calendar_max_desc = int(g("calendar", "max_description", 1000))
        self.calendar_dir = g("calendar", "dir", "calendar").strip("/")
        # secrets
        self.github_token = env.get("GITHUB_TOKEN", "")
        self.ops_webhook = env.get("OPS_WEBHOOK_URL", "")

    def require(self, *names):
        """Fail early with a readable message when a needed secret is missing (called by the entry points, not at import)."""
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            raise SystemExit(f"flux: missing in {self.home}/flux.env or flux.toml: {', '.join(missing)}")

    def state_file(self, name):
        """Path of a state file under $FLUX_HOME/state (each program has one)."""
        return str(self.state_dir / name)


_current = None


def load(home=None):
    """Build the configuration from `home` (default $FLUX_HOME) and make it the one `CFG` reads. Tests call it with a
    temporary directory; the entry points never need to, the first `CFG` access loads it."""
    global _current
    _current = Config(home)
    return _current


def current():
    return _current if _current is not None else load()


class _Proxy:
    """`CFG`: attribute reads and writes go to the current Config, so `CFG.vault_repo` is always the loaded value and a
    test's `monkeypatch.setattr(CFG, "mod_drive", True)` lands on it too."""
    __slots__ = ()

    def __getattr__(self, name):
        return getattr(current(), name)

    def __setattr__(self, name, value):
        setattr(current(), name, value)

    def __repr__(self):
        return f"<CFG {current().home}>"


CFG = _Proxy()
