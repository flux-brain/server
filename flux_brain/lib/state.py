"""Atomic JSON state files (flux_brain.lib, 2026-09-18). Four scripts had their own copy; the Keep sync's wrote 0600 (its
state names the owner's Keep notes), the others the default mode. One definition, mode chosen by the caller."""
import json
import os

__all__ = ["load_json", "save_json"]


def load_json(path, default):
    """The parsed file, or `default` (a fresh dict is the usual choice) when it is missing or unreadable JSON."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj, mode=None, **dump_kw):
    """Write via a temp file + os.replace so a crash never leaves a half-written state file. `mode` (e.g. 0o600)
    applies to the temp file before the rename; dump_kw go to json.dump (indent defaults to 1)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    dump_kw.setdefault("indent", 1)
    tmp = path + ".tmp"
    if mode is not None:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        f = os.fdopen(fd, "w", encoding="utf-8")
    else:
        f = open(tmp, "w", encoding="utf-8")
    with f:
        json.dump(obj, f, **dump_kw)
    os.replace(tmp, path)
