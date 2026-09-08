"""Leak #2 LIVE fixture pin (ingest-integrity I2, kickoff §4.2) -- runs only on
the host that still holds the transcripts; skipped everywhere else.

The 2026-09-04 harvester run filed 18 captures into 08-Lexington-Services with
``phi=True`` (none Lexington; the one genuine LEX session was ``phi=False``), the
2026-09-03 run filed 5 the same way. With the fix, the prose screen must be
FALSE on every one of those transcripts -- so each files by its distilled entity
and none is quarantined -- while the strict screen (the old override) still
trips on them (that is the defect, reproduced). Prints session ids only, never
transcript text.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import phi_guard  # noqa: E402
from cora import session_capture as scap  # noqa: E402

LEDGER = _REPO_ROOT / "logs" / "session-captures.jsonl"
FIXTURE_DAYS = ("2026-09-03", "2026-09-04")


def _rows():
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if "08-Lexington" in str(r.get("note_path", "")) and r.get("phi") is True \
                and str(r.get("captured_at", ""))[:10] in FIXTURE_DAYS and r.get("surface") != "refile":
            out.append(r)
    return out


def _transcript(sid: str):
    if sid.startswith("cowork:"):
        name = sid.split(":", 1)[1]
        roots = scap._discover_cowork_roots()
        extra = Path(os.environ.get("APPDATA", "")) / "Claude" / "local-agent-mode-sessions"
        if extra.exists():
            roots.append(extra)
        for d in scap.iter_cowork_session_dirs(roots):
            if d.name == name:
                return scap.parse_cowork_session(d)
        return None
    for p in scap.iter_transcript_files(Path(os.path.expanduser("~")) / ".claude" / "projects"):
        if p.stem == sid:
            return scap.parse_transcript(p)
    return None


ROWS = _rows()
FOUND = {r["session_id"]: _transcript(r["session_id"]) for r in ROWS} if ROWS else {}
AVAILABLE = [sid for sid, s in FOUND.items() if s is not None]
# The Cora desktop is the ONLY host that can hold the fixture: it has the harvester
# ledger AND a Cowork session store. Elsewhere the pin is meaningless and skips.
_COWORK_STORE = Path(os.environ.get("APPDATA", "")) / "Claude" / "local-agent-mode-sessions"
ON_FIXTURE_HOST = LEDGER.exists() and _COWORK_STORE.exists()
_SKIP = pytest.mark.skipif(
    len(AVAILABLE) < 10,
    reason="live Leak #2 transcripts not present on this host (need >=10 of the 9/3 + 9/4 misfiled set)",
)


def test_live_fixture_window_still_open_on_this_host():
    """Lens F #9: the pin above is a SILENT skip when the transcripts age out. On the
    host that holds them this test FAILS BY NAME the day fewer than 10 remain -- the
    signal to retire the pin (or re-pin on a synthetic fixture), never a quiet 0/0.
    Retention: Code transcripts ~30 days (cleanupPeriodDays); Cowork unknown --
    the 9/3-9/4 set is expected to start thinning after 2026-10-03."""
    if not ON_FIXTURE_HOST:
        pytest.skip("not the Cora desktop (no harvester ledger + Cowork store)")
    assert len(ROWS) >= 10, "the ledger no longer lists the 9/3 + 9/4 misfiled rows -- retire this pin"
    assert len(AVAILABLE) >= 10, (
        f"only {len(AVAILABLE)} of {len(ROWS)} fixture transcripts remain on disk -- the live Leak #2 "
        "fixture window has closed; retire tests/test_leak2_live_fixtures.py or re-pin it")


@_SKIP
@pytest.mark.parametrize("sid", AVAILABLE)
def test_misfiled_9_3_and_9_4_transcripts_pass_the_prose_screen(sid):
    text = FOUND[sid].text
    assert phi_guard.is_phi_risk(text) is True          # the defect, reproduced: the strict screen trips
    assert phi_guard.is_prose_phi_risk(text) is False    # the fix: value-shaped screen passes
    assert scap.route_capture("F3E", text) == ("F3E", False, False)   # files by distill, never LEX


def test_fixture_population_is_the_locked_one():
    # 18 (9/4) + 5 (9/3) rows filed LEX phi=True; the genuine LEX session (phi=False) is not in the set
    assert 20 <= len(ROWS) <= 23, [r["session_id"][:24] for r in ROWS]
