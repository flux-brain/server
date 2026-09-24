"""Google OAuth for the modules that talk to a Google API (Drive, Gmail, Tasks), in one place (2026-09-24,
architecture review: the refresh-token exchange and the one-time consent command were written three times, differing
only in the scope, the token file and the sentence that tells the operator what to do next).

Token file: `$FLUX_HOME/<module>-token.json`, written once by the module's `flux-<module>-auth` command from a Google
Cloud OAuth "Desktop app" client. The file is READ only: an access token is exchanged in memory when a process first
needs it and the file is never written back, so a revoked consent shows as a refresh failure that names the command
to run again.
"""
import json
import pathlib
import sys

__all__ = ["GoogleToken", "consent_main"]

TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleToken:
    """The Authorization header for one module, refreshed in memory once per process (and again after `reset()`,
    which a client calls on a 401)."""

    def __init__(self, session, token_path, module, command):
        self.s, self.token_path, self.module, self.command = session, token_path, module, command
        self._h = None

    def headers(self):
        if not self._h:
            try:
                with open(self.token_path) as f:
                    t = json.load(f)
            except FileNotFoundError:
                raise SystemExit(f"flux: {self.module} module is on but {self.token_path} is missing: run {self.command} (INSTALL.md)")
            r = self.s.post(t.get("token_uri", TOKEN_URI), timeout=30, data={
                "client_id": t["client_id"], "client_secret": t["client_secret"],
                "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
            if not r.ok:  # e.g. invalid_grant after the consent was revoked: run the consent command again
                raise RuntimeError(f"{self.module} token refresh failed: HTTP {r.status_code} (run {self.command} again if it persists)")
            self._h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        return self._h

    def reset(self):
        """Forget the access token (a 401 from the API): the next headers() call exchanges a fresh one."""
        self._h = None


def consent_main(argv, command, scopes, token_path, next_step):
    """The body of every `flux-<module>-auth <client_secret.json>` command: a one-time browser consent for `scopes`
    that writes `token_path` (mode 600) and prints `next_step`.

    Needs a browser, so run it on a laptop (`pip install 'flux-brain[google]'`, then copy the token file to the
    server's FLUX_HOME, owner flux, mode 600) or on the server with SSH port forwarding of the port it prints. The
    client secret comes from Google Cloud Console: APIs & Services > Credentials > OAuth client ID > Desktop app, with
    the module's API enabled on the project. The token grants exactly `scopes`, nothing wider."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit(f"usage: {command} <client_secret.json>")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit(f"{command} needs the google extra: pip install 'flux-brain[google]'")
    creds = InstalledAppFlow.from_client_secrets_file(argv[0], scopes=scopes).run_local_server(port=0, open_browser=True)
    out = pathlib.Path(token_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "client_id": creds.client_id, "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token, "token_uri": creds.token_uri, "scopes": list(creds.scopes or scopes),
    }, indent=2))
    out.chmod(0o600)
    print(f"wrote {out} (mode 600). Copy it to the server's FLUX_HOME if this is not the server, then {next_step}")
