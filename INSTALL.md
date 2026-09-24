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

`pip install` puts these commands in the venv: `flux-relay` (one tick), `flux-relay-loop` (what the unit
runs), `flux-ask`, and the module commands `flux-gmail`, `flux-tasks`, `flux-drive-auth`, `flux-gmail-auth`, `flux-tasks-auth`. Before enabling the unit, run one tick by hand as the `flux` user:

```
sudo -u flux FLUX_HOME=/var/lib/flux /var/lib/flux/venv/bin/flux-relay
```

A readable one-line error means a missing setting (for example `no GitHub token: set GITHUB_TOKEN`);
a `401` from Discord means the bot token; silence means the tick worked. The relay logs to
`$FLUX_HOME/logs/relay.log`. First lines to expect: `#flux found`, then `#flux-log found`; post a message
in `#flux` and watch it react within 15 seconds.

## 5. Optional modules

- **Drive attachments** (recommended: without it the note links Discord's copy of a photo or PDF, which
  Discord expires after some weeks; the extracted text is kept either way).
  1. Google Cloud Console: create a project (or reuse one), enable the **Google Drive API**, then
     APIs & Services > Credentials > Create credentials > **OAuth client ID** > type **Desktop app**;
     download the client secret JSON. If the consent screen is in "Testing", add your own Google
     account as a test user.
  2. On a machine with a browser: `pip install 'flux-brain[google]'` (the extra the three consent commands
     need; `[drive]` is its old name and still works), then
     `flux-drive-auth client_secret.json`. It opens the consent page (scope `drive.file` only: files
     this app creates) and writes `drive-token.json` (mode 600) into `FLUX_HOME`. Copy that file to
     the server's `/var/lib/flux/` if you ran it elsewhere, owner `flux`, mode 600.
  3. Create the destination folder in Drive (My Drive or a shared drive) and put its id (the last part
     of its URL) in `flux.toml` `[drive] folder_id`; for a shared drive also `drive_id` (the id in the
     shared drive's URL), else leave `drive_id` empty. Set `[modules] drive = true`.
  4. Restart the unit. Post a photo in `#flux`: the capture's `## Attachments` line links the Drive
     file. A missing token or folder id fails the tick with a one-line message naming the fix.
- **Memory mirror**: only meaningful if you use Claude Code with a file-based memory store; documented
  separately (v2).
- **Google Tasks** (the checklist view of your projects): one Tasks list per active project page,
  one task per action; ticks, additions, rewords and deletions on the phone come back as captures
  once the list has been left alone for 45 seconds, and page changes flow to the list within a minute.
  1. Enable the **Google Tasks API** on the Cloud project; reuse the Desktop OAuth client.
  2. On a machine with a browser: `flux-tasks-auth client_secret.json` (scope `tasks` only). Copy
     `tasks-token.json` to the server if needed, owner `flux`, mode 600.
  3. `flux.toml`: `[modules] tasks = true`; optionally `[tasks] prefix`, `tick_s`, `settle_s`.
  4. `sudo cp systemd/flux-tasks.service /etc/systemd/system/ && sudo systemctl enable --now flux-tasks`
     (long-running; log in `$FLUX_HOME/logs/tasks.log`). One tick by hand first:
     `sudo -u flux FLUX_HOME=/var/lib/flux TASKS_ONCE=1 /var/lib/flux/venv/bin/flux-tasks`.
  **Emails into a project:** drag an email from Gmail into a project's list (Gmail's Tasks side panel does
  this and keeps a link to the email). The task stays in the list as an action, and the whole conversation is
  filed under that project with its attachments, through the Gmail module (which must be on). The email must
  be in the same Google account as the Tasks token, and dragged into a `📁` project list, not "My Tasks".
  Each action's `^id` is written into the task's notes (a small grey line under the title); that is what
  keeps a task attached to its action through rewords and what rebuilds the mapping if the state file is
  lost. Quota: about (1 + active projects) API calls per tick; the default 60 s tick keeps 25 projects
  under the 50,000 calls a day. Paused and done projects are renamed once and not polled.
- **Google Keep**: not shipped. Keep has no public API for personal accounts; the only route is an
  unofficial library with a full-account master token, which is why this project uses Tasks instead.
- **Gmail feed**: label a conversation in Gmail and it is filed as one capture (messages oldest first,
  attachments through the same converters as Discord ones), then relabelled `<label>/Filed`.
  1. Enable the **Gmail API** on the same Cloud project as Drive; reuse the Desktop OAuth client.
  2. On a machine with a browser: `flux-gmail-auth client_secret.json` (scope `gmail.modify`: read and
     label changes, it cannot send). It writes `gmail-token.json` (mode 600) into `FLUX_HOME`; copy it to
     the server if needed, owner `flux`, mode 600.
  3. In Gmail, create the label named in `flux.toml` `[gmail] label` (default `📁 Flux`); the `/Filed`
     sub-label is created by the module. Set `[modules] gmail = true`.
  4. `sudo cp systemd/flux-gmail.* /etc/systemd/system/ && sudo systemctl enable --now flux-gmail.timer`
     (one pass a minute; log in `$FLUX_HOME/logs/gmail.log`). Test one pass by hand first:
     `sudo -u flux FLUX_HOME=/var/lib/flux /var/lib/flux/venv/bin/flux-gmail`.
  Without the Drive module the original email and its attachments stay in Gmail (the capture links the
  conversation); with it they are also stored in the Drive folder.

## 6. Tests

`pytest` runs the offline suites (no network; every state path points at a temporary directory).
Run them before installing any edit: the relay picks up file changes within 15 seconds.
