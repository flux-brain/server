# Flux server

The server half of **Flux**, a personal AI assistant built on a second-brain structure. This package runs on a
small always-on machine and connects a Discord channel to the vault repository that Claude Code routines
maintain (see the companion `vault-template` repository for the vault side and the routines).

What it does, every 15 seconds:

- **Inbound:** new messages in your capture channel (`#flux`) become `inbox/` files in the vault, with a
  reaction to confirm; attachments (photos, PDFs, Office files, emails, voice memos) are converted to text
  for Claude and, with the Drive module, stored in your cloud folder.
- **Start the run:** as soon as a capture is filed, the relay starts the `vault-inbox` routine through its
  API trigger and posts the run link, so you can watch Claude work.
- **Outbound:** answers, drafts, questions (with an @mention) and digests are posted to `#flux`; run
  summaries and other background notices go to the muted `#flux-log`.
- **Google Tasks (module):** every active project page is a Tasks list you can tick and edit on your phone;
  edits come back as captures, page changes go out to the list. Drag an email into a project's list and it is
  filed under that project with its attachments.
- **Google Calendar (module):** one file per day, `calendar/YYYY-MM-DD.md`, for today and the week ahead,
  read-only; the daily digest opens with the day's events and what the vault knows about the people and
  projects involved. A day that has ended is kept as it stood.
- **`flux-ask`:** lets a Claude Code session ask you a question in `#flux` without touching Discord.

## Install

See [INSTALL.md](INSTALL.md): Discord bot and channels, vault repository token, routines, then
`pip install .` in a virtualenv, `flux.toml` + `flux.env`, and the systemd unit. Python 3.11 or newer;
system packages `poppler-utils`, `tesseract-ocr` (and language packs), `ffmpeg` for audio.

## Configuration

`$FLUX_HOME/flux.toml` (settings, see `flux.example.toml`) and `$FLUX_HOME/flux.env` (secrets, mode 600,
see `flux.env.example`). `FLUX_HOME` defaults to `/var/lib/flux`. Every instance-specific value lives in
those two files; the code contains none.

## Modules

| Module | State in v1 | Needs |
|---|---|---|
| Discord relay | core | a bot, two channels |
| Attachment text extraction | core | poppler, tesseract, optional faster-whisper |
| Drive attachments | shipped (`flux-drive-auth`) | Google Cloud OAuth client, a folder |
| Gmail feed | shipped (`flux-gmail`, `flux-gmail-auth`) | Gmail API, scope gmail.modify |
| Google Tasks checklists | shipped (`flux-tasks`, `flux-tasks-auth`) | Tasks API, scope tasks |
| Google Calendar day files | shipped (`flux-calendar`, `flux-calendar-auth`) | Calendar API, two read-only scopes (events, calendar list) |
| Google Keep checklists | not shipped | no public API; replaced by Tasks |
| Memory mirror + reconcile | not shipped | Claude Code file-based memory store |

## Tests

`pytest` (after `pip install -e ".[dev]"`). Offline: `tests/conftest.py` points `FLUX_HOME` at a fresh temporary
directory before the package is imported, so no test can touch a live state file. Run them before installing any
edit.

`tests/clean-install.sh` is the install test: it follows INSTALL.md on a fresh Ubuntu 24.04 container against the
published repository and ends on the expected Discord 401 for placeholder secrets. Run it after any change to
`pyproject.toml`, `bin/`, `systemd/` or INSTALL.md (command in the script header).

## Privacy by design

Flux never reads your private conversations with other people (messaging apps). The relay only reads the
channels you created for it, and the routines only see the vault repository.

## Licence

Apache License 2.0; see `LICENSE` and `NOTICE`.
