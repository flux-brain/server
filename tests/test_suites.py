"""pytest entry point: runs each offline suite as its own subprocess and fails the test if the suite fails.

The suites are script-style (module-level asserts and check() calls, a final "N tests PASS" / "FAILS: 0" line) and
each one sets FLUX_HOME to a fresh temporary directory before importing the package (see _load.py), so running them
in separate processes keeps that isolation and lets pytest report them one by one. They are named suite_*.py on
purpose: pytest must not import them as test modules (their asserts run at import time and they call sys.exit).

    pytest                       # all suites, from the repository root or tests/
    pytest -k drive              # one suite
    PYTHON=... tests/run_all.sh  # the same suites without pytest
"""
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
SUITES = sorted(p.name for p in HERE.glob("suite_*.py"))


@pytest.mark.parametrize("suite", SUITES)
def test_suite(suite):
    proc = subprocess.run([sys.executable, str(HERE / suite)], cwd=HERE, capture_output=True, text=True, timeout=600)
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-15:])
    assert proc.returncode == 0, f"{suite} failed (exit {proc.returncode}):\n{tail}"
    last = (proc.stdout.strip().splitlines() or [""])[-1]
    assert "PASS" in last or "FAILS: 0" in last, f"{suite}: unexpected last line {last!r}"
