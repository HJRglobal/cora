"""The uv resolver cutoff in pyproject.toml and the one recorded in uv.lock stay equal.

uv.lock records `[options] exclude-newer` when a lock is built with a cutoff. If
pyproject.toml does not carry the SAME instant under `[tool.uv]`, a plain `uv sync`
reads the recorded value as a removed setting ("Resolving despite existing lockfile
due to removal of global exclude newer"), re-resolves, rewrites uv.lock and silently
upgrades the venv (2026-09-24: mcp 2.0.0 -> 2.2.0 et al.). Code #14 r4 pinned it.

A date-only value is read by uv as the END of the LOCAL day, so the same text means a
different instant on a different host; both values must be a full instant in Z.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _pyproject_pin() -> object:
    data = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return (data.get("tool") or {}).get("uv", {}).get("exclude-newer")


def _lock_cutoff() -> object:
    data = tomllib.loads((_REPO / "uv.lock").read_text(encoding="utf-8"))
    return (data.get("options") or {}).get("exclude-newer")


def test_pyproject_pins_the_cutoff_the_lock_records():
    pin, recorded = _pyproject_pin(), _lock_cutoff()
    assert recorded is not None, "uv.lock records no exclude-newer; drop the [tool.uv] pin in the same commit"
    assert pin == recorded, (
        f"pyproject [tool.uv] exclude-newer={pin!r} but uv.lock [options] exclude-newer={recorded!r}: "
        "edit the pyproject value, run plain `uv lock` (never --exclude-newer on the command line), "
        "commit both files together")


def test_the_cutoff_is_a_full_utc_instant():
    for label, value in (("pyproject.toml", _pyproject_pin()), ("uv.lock", _lock_cutoff())):
        assert isinstance(value, str) and _INSTANT.match(value), (
            f"{label} exclude-newer={value!r} must be a full instant like 2026-07-30T00:00:00Z "
            "(a date-only value means end-of-local-day and differs per host timezone)")
