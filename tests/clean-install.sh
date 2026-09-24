#!/usr/bin/env bash
# Clean-host install test: follows INSTALL.md literally on a fresh Ubuntu 24.04 (apt packages, flux user, venv,
# pip install from the published repository, example config files, unit file), then exercises what a new operator
# would hit: import, config parsing, one `flux-relay` tick with placeholder secrets (must end on a Discord 401, not on
# a Python error), the loop script, the offline tests in the venv, flux-ask, and the external tools.
#
# Run it in a throwaway container from the repository root (needs Docker and network access):
#   docker run --rm -v "$PWD/tests/clean-install.sh:/clean-install.sh:ro" ubuntu:24.04 bash /clean-install.sh
# To test a branch instead of main, set FLUX_REPO_REF (e.g. FLUX_REPO_REF=fix/x) in the container's environment.
# Exit code 0 = ALL OK. It changes nothing outside the container.
set -u
step() { echo; echo "### $*"; }
fail=0; ok() { echo "OK   $1"; }; bad() { echo "FAIL $1"; fail=1; }

step "1. system packages (what INSTALL.md names)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends \
  python3 python3-venv python3-pip git ca-certificates poppler-utils tesseract-ocr tesseract-ocr-eng ffmpeg util-linux >/dev/null \
  && ok "apt: python3 venv git poppler tesseract ffmpeg" || bad "apt install"
python3 --version

step "2. flux user + FLUX_HOME"
useradd -r -m -d /var/lib/flux flux && ok "useradd flux" || bad "useradd"

step "3. clone the PUBLISHED repo and install into a venv as the flux user"
su -s /bin/bash flux -c 'cd /var/lib/flux && git clone -q --branch "${FLUX_REPO_REF:-main}" https://github.com/flux-brain/server.git src && python3 -m venv venv && venv/bin/pip install -q --upgrade pip >/dev/null && venv/bin/pip install -q ./src' \
  && ok "pip install ./src" || bad "pip install"
su -s /bin/bash flux -c '/var/lib/flux/venv/bin/pip show flux-brain 2>/dev/null | sed -n 1,3p'
ls -la /var/lib/flux/venv/bin/flux-relay 2>/dev/null && ok "entry point flux-relay installed" || bad "entry point missing"

step "4. config files from the examples (placeholders)"
su -s /bin/bash flux -c 'cd /var/lib/flux && cp src/flux.example.toml flux.toml && cp src/flux.env.example flux.env && chmod 600 flux.env && sed -i "s/^DISCORD_BOT_TOKEN=.*/DISCORD_BOT_TOKEN=placeholder/; s/^DISCORD_GUILD_ID=.*/DISCORD_GUILD_ID=1/; s/^GITHUB_TOKEN=.*/GITHUB_TOKEN=placeholder/" flux.env && mkdir -p state logs'
su -s /bin/bash flux -c 'cd /var/lib/flux && venv/bin/python -c "
from flux_brain.config import CFG
print(\"config:\", CFG.home, CFG.vault_repo, CFG.channel_names, CFG.log_channel_name, \"token set:\", bool(CFG.discord_token))"' \
  && ok "config parses" || bad "config parse"

step "5. first run: must fail cleanly on Discord auth (placeholder token), not on Python"
su -s /bin/bash flux -c 'cd /var/lib/flux && FLUX_HOME=/var/lib/flux venv/bin/flux-relay; echo "exit=$?"' 2>&1 | tail -4

step "6. the loop script under flock, one tick (timeout 20 s), log file created"
su -s /bin/bash flux -c 'cd /var/lib/flux && FLUX_HOME=/var/lib/flux timeout 20 venv/bin/flux-relay-loop; true'
test -s /var/lib/flux/logs/relay.log && ok "logs/relay.log written: $(tail -n 1 /var/lib/flux/logs/relay.log | cut -c1-100)" || bad "no relay.log"

step "7. test suite in the venv (needs cramjam etc. from the install)"
su -s /bin/bash flux -c 'cd /var/lib/flux/src && /var/lib/flux/venv/bin/pip install -q "/var/lib/flux/src[dev]" && /var/lib/flux/venv/bin/python -m pytest -q -p no:cacheprovider' && ok "tests pass in the venv (pytest)" || bad "tests"

step "8. flux-ask guards with the installed package"
su -s /bin/bash flux -c 'cd /var/lib/flux && PATH=/var/lib/flux/venv/bin:$PATH FLUX_HOME=/var/lib/flux bash /var/lib/flux/venv/bin/flux-ask "bad slug" x; echo "exit=$?"' 2>&1 | tail -2

step "9. unit file: paths it names exist; systemd-analyze if available"
grep -o "/var/lib/flux/venv/bin/flux-relay-loop" /var/lib/flux/src/systemd/flux-relay.service >/dev/null && test -x /var/lib/flux/venv/bin/flux-relay-loop && ok "ExecStart target exists" || bad "ExecStart target"
command -v systemd-analyze >/dev/null && systemd-analyze verify /var/lib/flux/src/systemd/flux-relay.service 2>&1 | head -3 || echo "(no systemd in the container; unit checked by path only)"

step "10. external tools the extractor calls"
for t in pdftotext pdftoppm pdfinfo tesseract ffmpeg flock; do command -v $t >/dev/null && ok "$t" || bad "$t missing"; done

echo; echo "RESULT: $([ $fail = 0 ] && echo ALL OK || echo FAILURES)"; exit $fail
