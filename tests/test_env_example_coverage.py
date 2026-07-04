"""Guardrail: every env var read under src/ must be documented in .env.example.

Durable fix for audit W9-01 (Slice A, 2026-07-03). .env.example had drifted
30+ vars stale, and — critically — DRIVE_EXTRACTOR_PROPOSALS_ENABLED (code
default "1" = ENABLED) was undocumented, so a rebuild-from-.env.example would
silently RE-ENABLE the ratified D-066 drive-extractor pause. A one-time regen
fixes today; this test stops the drift from ever recurring.

Extraction mirrors how env vars are actually read in this codebase:
  1. direct call/subscript: os.environ.get("X") / os.getenv("X") / os.environ["X"]
  2. constant indirection:  _FOO_ENV = "X"   (then os.environ.get(_FOO_ENV, ...))
  3. config.py wrapper:      get("X", ...)    (the local _load() helper, config.py only)

Keys read only via a runtime-built tuple/loop (e.g. the Fireflies fallback var
names) are NOT extracted — that is the safe direction (the test never demands a
key that isn't provably read). Scope is src/ only, per the W9-01 fix spec.

A documented key may appear ACTIVE (`KEY=...`) or COMMENTED (`# KEY=...`) in
.env.example — a commented, explained entry is documentation.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
_CONFIG_PY = _SRC / "cora" / "config.py"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

_KEY = r"([A-Z][A-Z0-9_]{2,})"

# 1. direct env access with a string-literal key
_RE_ENV_CALL = re.compile(r"""(?:os\.)?(?:environ\.get|getenv)\s*\(\s*["']""" + _KEY + r"""["']""")
_RE_ENV_SUB = re.compile(r"""(?:os\.)?environ\s*\[\s*["']""" + _KEY + r"""["']""")
# 2. constant indirection: an identifier ending in _ENV assigned an env-key literal
_RE_ENV_CONST = re.compile(r"""\b\w*_ENV\b\s*=\s*["']""" + _KEY + r"""["']""")
# 3. config.py local get("X", ...) wrapper (guarded so dict.get(name) — no literal — never matches)
_RE_CONFIG_GET = re.compile(r"""(?<![.\w])get\s*\(\s*["']""" + _KEY + r"""["']""")

# LHS of an env line in .env.example, whether active or commented out.
_RE_EXAMPLE_KEY = re.compile(r"""^[ \t]*#?[ \t]*""" + _KEY + r"""[ \t]*=""")


def _src_env_keys() -> dict[str, str]:
    """Map each env key read in src/ -> the first file:line that reads it.

    Scans the FULL file text (not line-by-line) so a read split across lines
    — e.g. config.py's `get(\n    "QBO_REDIRECT_URI", ...)` or a
    `Path(\n  os.environ.get("X"))` wrap — is still caught. The regexes use
    `\\s*` between the call and the key literal, which spans newlines only when
    matched against joined text. (A per-line scan silently under-covered the two
    multi-line config.py reads — flagged by the Slice A D-051 review.)
    """
    found: dict[str, str] = {}

    def _line_of(text: str, offset: int) -> int:
        return text.count("\n", 0, offset) + 1

    for path in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(_REPO_ROOT).as_posix()
        regexes = [_RE_ENV_CALL, _RE_ENV_SUB, _RE_ENV_CONST]
        if path == _CONFIG_PY:
            regexes.append(_RE_CONFIG_GET)
        for rx in regexes:
            for m in rx.finditer(text):
                found.setdefault(m.group(1), f"{rel}:{_line_of(text, m.start())}")
    return found


def _documented_keys() -> set[str]:
    keys: set[str] = set()
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _RE_EXAMPLE_KEY.match(line)
        if m:
            keys.add(m.group(1))
    return keys


def test_every_src_env_var_is_documented_in_env_example() -> None:
    src_keys = _src_env_keys()
    documented = _documented_keys()
    missing = {k: loc for k, loc in src_keys.items() if k not in documented}
    assert not missing, (
        "These env vars are read under src/ but are NOT documented in .env.example "
        "(add them — W9-01 drift guard):\n"
        + "\n".join(f"  {k}  (read at {loc})" for k, loc in sorted(missing.items()))
    )


def test_reserved_drive_extractor_pause_is_pinned_in_env_example() -> None:
    """The W9-01 silent-reversal fix: the example MUST carry the ratified paused
    value (=0), ACTIVE (not commented), because the code default is ENABLED ("1").
    A future edit that drops or comments this line would re-open the DR hazard.
    """
    text = _ENV_EXAMPLE.read_text(encoding="utf-8", errors="replace")
    assert re.search(r"^DRIVE_EXTRACTOR_PROPOSALS_ENABLED=0\b", text, re.MULTILINE), (
        "DRIVE_EXTRACTOR_PROPOSALS_ENABLED must be present and ACTIVE as =0 in "
        ".env.example (code default is '1'=ENABLED; live ratified value is 0=PAUSED, "
        "D-066). Without it, a rebuild-from-example silently re-enables the pause."
    )


def test_dead_linkedin_spy_channel_removed() -> None:
    """W9-04: LINKEDIN_SPY_CHANNEL is a dead key (LinkedIn Spy migrated to Make.com;
    read nowhere in src/). It must not reappear as an active key in .env.example.
    """
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # a commented mention (e.g. the removal note) is fine
        assert not stripped.startswith("LINKEDIN_SPY_CHANNEL="), (
            "LINKEDIN_SPY_CHANNEL is dead (W9-04) — do not reintroduce it as an active key."
        )
