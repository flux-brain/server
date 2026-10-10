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
   Optional, at any time: one channel each for the digests, for the posts that wait for a tap and for
   the voicemail notices (`[discord] digest_channel`, `actions_channel`, `voice_channel` in
   `flux.toml`). Same grant plus Read Message History; until a channel exists its posts stay in `flux`.
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
runs), `flux-ask`, and the module commands `flux-gmail`, `flux-tasks`, `flux-calendar`, `flux-drive-auth`, `flux-gmail-auth`, `flux-tasks-auth`, `flux-calendar-auth`. Before enabling the unit, run one tick by hand as the `flux` user:

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
  5. Optional, **Drive links**: set `[drive] links = "details"` and a Google Docs, Sheets, Slides or Drive
     link in a message gets a `## Links` line in the capture (name, type, folder, last edit); add the word
     `+text` to the message to copy the file's text into `raw/attachments/` too. With `links = "text"` the
     text is copied by default and the word `-text` asks for the details only. The file itself stays in
     Drive. This needs a token that can READ the linked files (scope `drive.readonly` or `drive`): the
     `drive.file` token of step 2 sees only files this app created, so every lookup would answer 404 and
     the line would say "not accessible". Point `[drive] token_file` at such a token, or leave `links` off.
- **Web links** (the text of a web page linked in a message; needs no account): set `[capture] web_links = "text"`
  and restart the unit. Post a link in `#flux`: the capture gets a `## Links` line with the page's title and a link to
  its text in `raw/attachments/`, fetched once when the message is filed (the page may change later; the copy does
  not). The routine has no network access, so without this a bare link is filed by its address alone. The word
  `-text` in a message keeps its links as plain URLs. The request leaves from YOUR server, so the relay asks only
  for public http(s) addresses on the default ports, checks every redirect the same way, reads at most 10 MB, and
  never retries: a page that refuses automated readers, or an address on your own network, gives a line reading
  `not fetched (...)` and the note is filed anyway. Keep the capture channel private: whoever can post in it can
  make your server fetch a page. A linked PDF is converted like an attached one.
- **Drive watch** (new files in a few chosen Drive folders become captures, e.g. the folder where Google Meet saves
  Gemini's meeting notes): needs the Drive token of step 5 above, one that can READ those folders.
  1. List the folders in `flux.toml` `[drive_watch] folders` (sub-folders are included; add `project = "<page slug>"`
     to route a folder to a project page), set `[modules] drive_watch = true`.
  2. Optional dry pass: copy `flux.toml` into a scratch directory, link `flux.env` next to it, and run
     `FLUX_HOME=<scratch> DRIVE_WATCH_DRY=1 flux-drive-watch` twice (the first run only records where the change
     feed starts); the captures print instead of being written.
  3. Enable `systemd/flux-drive-watch.timer` (every 5 minutes). New files are filed once they have gone
     `settle_s` without an edit; edits to a file that was filed already are not filed again.
  4. Optional, `suggest = true`: once a week the module posts the folders you changed most outside the watched ones
     (at least `suggest_min_files` files), each with a ✅ button; a tap adds it to the watched list (kept in the
     state file). Put in `never` the folders that change on their own under your account (sync mirrors, backups,
     the Drive module's attachment folder) and any folder you never want filed.
  5. Optional, `starred = true`: the folders you have starred in Drive are watched as well, sub-folders included.
     The list is read from Drive at every run, so starring a folder (on a phone, say) starts the filing and removing
     the star stops it; `folders` may then stay empty. A starred folder has no `project`, so the routine routes its
     files by content. A folder in `never` is left out even inside a starred or listed folder, sub-folders included:
     put there what must not reach the vault.
- **Follow-ups** (sent emails still waiting for an answer, listed in `followups/waiting.md` for the daily digest):
  needs the Gmail token. Set `[modules] followups = true`, enable `systemd/flux-followups.timer` (hourly). It reads
  only your own sent threads and their replies; newsletters and no-reply addresses are skipped. Label a thread with
  `[followups] dismiss_label` in Gmail to drop it. `FOLLOWUPS_DRY=1 flux-followups` prints the list instead.
- **Gmail triage** (new emails that probably matter, posted in `#flux` with two buttons): needs the Gmail token and
  the bot. A message is posted when a person (not a newsletter or a no-reply address) sent it and it answers a
  conversation you wrote in, or comes from someone you wrote to (`correspondents_days`), or names a `keywords` entry.
  Tap ✅ to file it (the Gmail module picks it up as if you had labelled it) or ✍️ to also get a reply draft in
  `#flux` (nothing is ever sent). The relay reads the taps (once a minute, yours only: `[owner] discord_user_id`),
  marks a tap ⏳ and acts after `[discord] button_grace_s` (10 minutes): remove the reaction before then to cancel.
  Add a second timer or cron line running `flux-triage --act-only` every minute so a tap acts right after its
  grace period instead of at the next hourly run.
  Set `[modules] triage = true`, enable `systemd/flux-triage.timer` (hourly); the first run only records the inbox.
  `TRIAGE_DRY=1 flux-triage` prints what it would post.
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
- **Google Calendar** (the day's events, read-only): one file per day, `calendar/YYYY-MM-DD.md`, for today
  and `lookahead_days` ahead, rewritten only when the calendar changed; a day that has ended is never written
  again. Not a capture: it never starts a run; the digest and the weekly review read it.
  1. Enable the **Google Calendar API** on the Cloud project; reuse the Desktop OAuth client.
  2. On a machine with a browser: `flux-calendar-auth client_secret.json` (two read-only scopes,
     `calendar.events.readonly` and `calendar.calendarlist.readonly`: the module cannot change a calendar).
     Copy `calendar-token.json` to the server if needed, owner `flux`, mode 600.
  3. `flux.toml`: `[modules] calendar = true`; `[calendar] calendars` = `"selected"` (the calendars ticked in
     Google Calendar, the default), `"all"`, or a list of ids; `exclude`, `lookahead_days`, `timezone`.
  4. Privacy: `details = true` also writes attendees and descriptions. Both are written by other people and
     land in your private vault repository and its history; the vault template treats `calendar/` as data,
     never instructions. Private and confidential events, and events you declined, are never written.
  5. One pass by hand, nothing written to GitHub: `sudo -u flux FLUX_HOME=/var/lib/flux CALENDAR_DRY=1
     CALENDAR_DRY_DIR=/tmp /var/lib/flux/venv/bin/flux-calendar`, read the files, then
     `sudo cp systemd/flux-calendar.* /etc/systemd/system/ && sudo systemctl enable --now flux-calendar.timer`
     (every 30 minutes; log in `$FLUX_HOME/logs/calendar.log`).
- **Google Keep**: not shipped. Keep has no public API for personal accounts; the only route is an
  unofficial library with a full-account master token, which is why this project uses Tasks instead.
- **Gmail feed**: label a conversation in Gmail and it is filed as one capture (messages oldest first,
  attachments through the same converters as Discord ones), then relabelled `<label>/Filed`. A filed conversation
  stays followed: a reply that arrives in it later is filed by itself as a `kind: followup` capture, with no new
  label or tap (`[gmail] follow_threads`, on by default; `follow_days` = how far back a reply is looked for).
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

`pip install -e ".[dev]"`, then `pytest`: offline tests (no network; `tests/conftest.py` points every state path
at a temporary directory). Run them before installing any edit.
