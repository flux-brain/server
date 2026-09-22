# Installing the Flux server side (v1 outline)

What runs on your always-on machine: the Discord relay (every 15 s), and the optional modules you turn
on. Everything talks to your vault repository through the GitHub API; nothing is cloned on the server.

## 1. Discord

1. Create an application and a bot at discord.com/developers. Under Bot, turn on **Message Content
   Intent** (the relay reads your messages). Copy the token into `flux.env`.
2. Invite the bot to your server with permissions **View Channels, Send Messages, Read Message
   History, Add Reactions** (integer 68672). It needs nothing else.
3. Create two text channels, both private: `flux` and `flux-log`. On each, add the bot's role with
   View Channel + Send Messages (the bot cannot grant itself access; a missing grant shows as
   `Missing Access` in the relay log). Mute `flux-log`.
4. Find your user id (Discord settings, Advanced, Developer Mode, then copy id on your profile) and
   the server id; they go in `flux.toml` and `flux.env`.

## 2. Vault repository

Made from the vault template (companion repository). Create a fine-grained GitHub token limited to
that repository with Contents read and write; `flux.env: GITHUB_TOKEN`.

## 3. Routines

Create `vault-inbox` and `vault-review` as described in the template README. Generate the API trigger
token of `vault-inbox`; `flux.env: ROUTINE_FIRE_TOKEN`, and put the trigger URL in `flux.toml`.

## 4. Install

```
sudo useradd -r -m -d /var/lib/flux flux
sudo -u flux python3 -m venv /var/lib/flux/venv
sudo -u flux /var/lib/flux/venv/bin/pip install -e .
sudo cp flux.example.toml /var/lib/flux/flux.toml   # then edit
sudo cp flux.env.example  /var/lib/flux/flux.env    # then edit; chmod 600
sudo cp systemd/*.service /etc/systemd/system/ && sudo systemctl enable --now flux-relay
```

`pip install` puts three commands in the venv: `flux-relay` (one tick), `flux-relay-loop` (what the unit
runs) and `flux-ask`. Before enabling the unit, run one tick by hand as the `flux` user:

```
sudo -u flux FLUX_HOME=/var/lib/flux /var/lib/flux/venv/bin/flux-relay
```

A readable one-line error means a missing setting (for example `no GitHub token: set GITHUB_TOKEN`);
a `401` from Discord means the bot token; silence means the tick worked. The relay logs to
`$FLUX_HOME/logs/relay.log`. First lines to expect: `#flux found`, then `#flux-log found`; post a message
in `#flux` and watch it react within 15 seconds.

## 5. Optional modules

- **Drive attachments**: a Google Cloud project with the Drive API, an OAuth client, a one-time
  consent run that writes `drive-token.json`; a shared drive or folder id in `flux.toml`.
- **Memory mirror**: only meaningful if you use Claude Code with a file-based memory store; documented
  separately (v2).
- **Google Keep**: unofficial API (`gkeepapi`) with a full-account master token. Experimental; read
  the warning in the module doc before turning it on.
- **Gmail feed**: Gmail API with a read + labels scope.

## 6. Tests

`pytest` runs the offline suites (no network; every state path points at a temporary directory).
Run them before installing any edit: the relay picks up file changes within 15 seconds.
