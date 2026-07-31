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
    """The 'Drive is authoritative' banner must be present on line 1.

    Checked on the first NON-EMPTY line (not substring-anywhere) so a banner that
    drifted off the top -- the position the README promises and context injection
    surfaces first -- is caught, not silently tolerated lower in the file.
    """
    offenders = []
    for name in _SEED_FILES:
        p = _KA_DIR / name
        if not p.is_file():
            continue  # covered by the existence test
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        first = next((ln for ln in lines if ln.strip()), "")
        if _BANNER_ANCHOR not in first:
            offenders.append(name)
    assert not offenders, (
        "known-answers seed(s) missing the line-1 authoritative-store banner: "
        + ", ".join(sorted(offenders))
        + " -- restore the line-1 banner (see design/known-answers/README.md)."
    )


def test_every_seed_still_parses_as_fallback():
    """Each seed must remain a usable fallback: the append-target section exists.

    The marker is checked as a STANDALONE line, mirroring the write path exactly
    (gap_autofill._append_to_section locates the section with un-stripped
    `line == section_header`). A substring check would false-pass on an inline
    mention while `_append_to_section` failed to find the header and silently
    appended at EOF.
    """
    offenders = []
    for name in _SEED_FILES:
        p = _KA_DIR / name
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if not any(line == _PARSE_MARKER for line in text.splitlines()):
            offenders.append(name)
    assert not offenders, (
        f"known-answers seed(s) missing a standalone '{_PARSE_MARKER}' section line: "
        + ", ".join(sorted(offenders))
    )


def _drive_ka_dir() -> Path | None:
    """Resolve the live Drive known-answers dir if mounted, else None.

    Parses the repo .env FIRST, os.environ as the fallback (cq-d9432f552a33):
    the conftest autouse fixture now redirects KNOWN_ANSWERS_DIR to a tmp dir
    for EVERY test (write-isolation), so the env var at runtime points at an
    empty tmp path -- only the .env file names the real Drive store. Returns
    None on anything unusual so the gated test skips rather than errors.
    """
    try:
        val = ""
        env_path = _REPO / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("KNOWN_ANSWERS_DIR=") and "=" in line:
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        if not val:
            val = os.environ.get("KNOWN_ANSWERS_DIR") or ""
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


def test_unpinned_known_answer_write_lands_in_tmp():
    """cq-d9432f552a33 regression: a test that drives the known-answers WRITER
    without setting its own KNOWN_ANSWERS_DIR must land in the conftest autouse
    tmp redirect -- NEVER the live Drive store. (The 2026-07-25 leak mode: a
    full knowledge-review drain with CORA_AUTOWRITE_LIVE=all and no redirect
    auto-wrote the U-TOMMY fixture into the PRODUCTION _brain/f3e.md.)"""
    from cora import gap_autofill as ga

    target = Path(os.environ["KNOWN_ANSWERS_DIR"])
    assert "_brain" not in str(target)            # the redirect is in force
    ok, msg = ga.apply_known_answer({
        "entity": "F3E",
        "question": "where is the dashboard",
        "answer": "REGRESSION-PROBE-ANSWER (must land in tmp only)",
    })
    assert ok, msg
    written = target / "f3e.md"
    assert written.exists()
    assert "REGRESSION-PROBE-ANSWER" in written.read_text(encoding="utf-8")


@pytest.mark.skipif(
    _drive_ka_dir() is None,
    reason="Drive known-answers store not mounted (CI / no KNOWN_ANSWERS_DIR)",
)
def test_live_store_carries_no_fixture_markers():
    """Host-only: the PRODUCTION known-answers store must never contain the
    test-fixture text (leaked 2026-07-25, purged 2026-07-31 -- the daily
    flywheel monitor re-flagged it every run until then). The exact fixture
    phrase is asserted; the legitimate fndr.md 'Polar Analytics' entry
    (2026-05-18) deliberately does not match."""
    drive = _drive_ka_dir()
    assert drive is not None
    for p in sorted(drive.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            pytest.skip(f"could not read {p.name}: {exc}")
        assert "ops dashboard lives in Polar" not in text, (
            f"test-fixture marker present in LIVE {p.name} -- a suite run wrote "
            "to the production known-answers store (see cq-d9432f552a33)")
        assert "U-TOMMY" not in text, f"fixture user id in LIVE {p.name}"
