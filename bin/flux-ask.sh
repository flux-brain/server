#!/usr/bin/env bash
# flux-ask: ask the owner a short question on the Discord capture channel from a Claude Code session, WITHOUT
# touching Discord.
#
# The relay posts any `notify/*question*.md` file that appears in the vault repository to the capture channel as
# `@owner ❓ <text>` within about 15 s. So a question only has to be written into the repository, which `gh api`
# can do with the owner's normal GitHub login. No bot token, no webhook, nothing secret leaves the session.
#
# Usage:  flux-ask <slug> "<question>"
#   slug      : short kebab-case topic, e.g. oddo-proposal (becomes part of the file name)
#   question  : the text the owner reads on their phone; keep it to a few lines
#
# What happens next: the owner answers in the channel with the "reply" gesture; the relay files the reply in inbox/
# with `in_reply_to:` = the first 300 chars of the question, and starts the vault run. A later session finds the
# answer with:  gh api "search/code?q=in_reply_to+<a word of the question>+repo:<owner/vault>"
#
# Guards: refuses a text that matches the shared SECRET_PATTERNS (flux_brain/lib/secrets.py, same definition as
# the relay), refuses a slug that already has a question file today (no duplicates), refuses more than 1500 chars
# (one Discord post; longer text belongs in a wiki page).
#
# Configuration: the repository comes from $FLUX_HOME/flux.toml ([vault] repo) or the env var FLUX_VAULT_REPO.
set -euo pipefail

REPO="${FLUX_VAULT_REPO:-$(python3 -c "import tomllib,os,pathlib
p=pathlib.Path(os.environ.get('FLUX_HOME','/var/lib/flux'))/'flux.toml'
print(tomllib.loads(p.read_text())['vault']['repo'] if p.exists() else '')")}"
[[ -n "$REPO" ]] || { echo "flux-ask: no repository: set [vault] repo in flux.toml or FLUX_VAULT_REPO" >&2; exit 2; }
NOTIFY_DIR="notify"
MAX_CHARS=1500

slug="${1:-}"
question="${2:-}"
if [[ -z "$slug" || -z "$question" ]]; then
  echo "usage: $0 <slug> \"<question>\"" >&2
  exit 2
fi
if ! [[ "$slug" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "slug must be kebab-case ([a-z0-9-]): $slug" >&2
  exit 2
fi
if (( ${#question} > MAX_CHARS )); then
  echo "question is ${#question} chars, max $MAX_CHARS: put the long form in a wiki page and ask the short question" >&2
  exit 2
fi
# Secret guard: same regex as the relay, so the two never drift apart.
if ! printf '%s' "$question" | python3 -c '
import sys
from flux_brain.lib.secrets import SECRET_PATTERNS  # the package is installed in the same venv as flux-relay
sys.exit(1 if SECRET_PATTERNS.search(sys.stdin.read()) else 0)'; then
  echo "refused: the text looks like it contains a secret (key, token or webhook)" >&2
  exit 3
fi

today="$(date -u +%Y-%m-%d)"
stamp="$(date -u +%Y-%m-%dT%H%M)"
path="${NOTIFY_DIR}/${stamp}-question-${slug}.md"

# Duplicate guard: one question per slug per day. The listing is one GitHub call; an empty or
# missing notify/ dir is not an error.
if gh api "repos/${REPO}/contents/${NOTIFY_DIR}" --jq '.[].name' 2>/dev/null \
   | /usr/bin/grep -q "^${today}T[0-9]\{4\}-question-${slug}\.md$"; then
  echo "refused: a question with slug '${slug}' already exists today in ${NOTIFY_DIR}/ (answer it or pick another slug)" >&2
  exit 4
fi

# Body: the question as plain text (the relay strips nothing but callout syntax, so no markdown
# tricks), followed by one line telling the vault run where it came from, so a reply is filed
# with the right context. The relay posts the whole body, so the trailer stays short.
body="$(printf '%s\n\n(Question posée depuis une session Claude Code sur le serveur ; réponds ici avec le geste « répondre ».)\n' "$question")"

gh api -X PUT "repos/${REPO}/contents/${path}" \
  -f message="notify: ${stamp}-question-${slug} (vault-ask)" \
  -f content="$(printf '%s' "$body" | base64 -w0)" \
  --jq '"posted " + .content.path + " @ " + .commit.sha[0:8] + " (Discord #vault within ~15 s)"'
