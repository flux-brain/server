#!/usr/bin/env bash
# Runs every offline suite (no network; FLUX_HOME is a fresh temp dir, see _load.py). Non-zero exit on any failure.
# PYTHON=/path/to/venv/bin/python ./tests/run_all.sh   (the suites need the project's dependencies: cramjam, requests, ...)
# Script-style suites for now (assert + a final "N tests PASS" / "FAILS: 0" line); pytest wrapping is a v1.1 cleanup.
set -uo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"; rc=0
for t in test_secrets.py test_relay_posts.py test_relay_inbound.py test_iwork.py; do
  if out=$("$PY" "$t" 2>&1); then echo "PASS $t: $(echo "$out" | tail -n 1)"; else echo "FAIL $t"; echo "$out" | tail -n 5; rc=1; fi
done
exit $rc
