"""Drift-flag guard for the repo known-answers DR-seed copies (Slice 07 hygiene).

The AUTHORITATIVE known-answers store is on Drive (`_brain/known-answers`,
env `KNOWN_ANSWERS_DIR`); the repo `design/known-answers/*.md` files are a DR
seed / offline fallback only and are intentionally allowed to lag Drive in
CONTENT (see design/known-answers/README.md). Content equality is therefore a
non-goal and is NOT asserted.

What IS asserted:
  * CI-safe structural invariants (run everywhere): every entity in the D-059
    map has a repo seed file that (a) exists, (b) carries the authoritative-store
    banner so the "Drive is authoritative" notice can't be silently dropped, and
    (c) still parses as a usable fallback (has a `## Known facts` section, the
    append target the write path needs).
  * Host-only DR-seed completeness (skips in CI): when the Drive store is
    mounted, every live Drive `*.md` that maps to a D-059 filename must have a
    repo seed counterpart -- so a new live entity can never lack a DR seed.

Styled after tests/test_clover_retired.py (enumerate + invariant + offenders).
Does NOT touch the read/write path (sound D-059 single-map) and never writes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import sys

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from cora.known_answers_map import ENTITY_FILES  # noqa: E402

_KA_DIR = _REPO / "design" / "known-answers"
_BANNER_ANCHOR = "AUTHORITATIVE STORE: Drive _brain/known-answers"
_PARSE_MARKER = "## Known facts"

# Distinct seed filenames the D-059 map points at (LEX-* all collapse to lex.md).
_SEED_FILES = sorted(set(ENTITY_FILES.values()))


def test_map_points_only_at_md_files():
    """Every ENTITY_FILES value is a .md filename (no path segments)."""
    bad = [f for f in _SEED_FILES if not f.endswith(".md") or "/" in f or "\\" in f]
    assert not bad, f"ENTITY_FILES has non-.md / path-bearing values: {bad}"


def test_every_entity_has_a_repo_seed_file():
    """DR-seed completeness: a fallback file exists for every mapped entity."""
    missing = [f for f in _SEED_FILES if not (_KA_DIR / f).is_file()]
    assert not missing, (
        "known-answers DR seed missing for: " + ", ".join(missing)
        + " -- add the seed file or reconcile known_answers_map.ENTITY_FILES."
    )


def test_every_seed_carries_authoritative_banner():
    """The 'Drive is authoritative' banner must not be silently removed."""
    offenders = []
    for name in _SEED_FILES:
        p = _KA_DIR / name
        if not p.is_file():
            continue  # covered by the existence test
        if _BANNER_ANCHOR not in p.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(name)
    assert not offenders, (
        "known-answers seed(s) missing the authoritative-store banner: "
        + ", ".join(sorted(offenders))
        + " -- restore the line-1 banner (see design/known-answers/README.md)."
    )


def test_every_seed_still_parses_as_fallback():
    """Each seed must remain a usable fallback: it has the append-target section."""
    offenders = []
    for name in _SEED_FILES:
        p = _KA_DIR / name
        if not p.is_file():
            continue
        if _PARSE_MARKER not in p.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(name)
    assert not offenders, (
        f"known-answers seed(s) missing a '{_PARSE_MARKER}' section: "
        + ", ".join(sorted(offenders))
    )


def _drive_ka_dir() -> Path | None:
    """Resolve the live Drive known-answers dir if mounted, else None.

    Checks the env var first (as the bot does), then falls back to parsing the
    repo .env so the host-only check runs under a plain `pytest` invocation
    (conftest does not load the real .env). Returns None on anything unusual so
    the gated test skips rather than errors.
    """
    try:
        val = os.environ.get("KNOWN_ANSWERS_DIR")
        if not val:
            env_path = _REPO / ".env"
            if env_path.is_file():
                for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line.startswith("KNOWN_ANSWERS_DIR=") and "=" in line:
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        if not val:
            return None
        d = Path(val)
        return d if d.is_dir() else None
    except Exception:
        return None


@pytest.mark.skipif(
    _drive_ka_dir() is None,
    reason="Drive known-answers store not mounted (CI / no KNOWN_ANSWERS_DIR)",
)
def test_repo_seed_covers_live_drive_store():
    """Host-only: every live Drive entity file must have a repo DR-seed.

    This catches a live store that has drifted AHEAD in COVERAGE (a new entity
    file added on Drive) without a matching repo seed. Content staleness is
    expected and deliberately NOT checked.
    """
    drive = _drive_ka_dir()
    assert drive is not None  # guarded by skipif
    seed_names = set(_SEED_FILES)
    try:
        drive_md = {p.name for p in drive.glob("*.md")}
    except OSError as exc:  # transient mount glitch -> don't hard-fail
        pytest.skip(f"could not list Drive known-answers dir: {exc}")
    live_mapped = drive_md & seed_names
    missing_seed = [n for n in sorted(live_mapped) if not (_KA_DIR / n).is_file()]
    assert not missing_seed, (
        "live Drive known-answers files lack a repo DR-seed: "
        + ", ".join(missing_seed)
    )
