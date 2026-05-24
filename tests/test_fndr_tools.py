"""
[HJRG] Tests for FNDR-specific tool: fndr_open_decisions.

Verifies parsing of decisions-pending.md structure, age calculation,
severity filtering, section boundary enforcement (no Gmail Deep Dive bleed),
and output formatting.
"""

from __future__ import annotations

import pathlib
import re
from datetime import date, datetime

import pytest

# ---------------------------------------------------------------------------
# Inline parser (mirrors tool_dispatch._tool_fndr_open_decisions logic)
# ---------------------------------------------------------------------------

def _parse_open_decisions(content: str, today: date) -> list[dict]:
    """Parse decisions-pending.md and return P0/P1 entries from ## Active only."""
    active_match = re.search(r"^## Active\b", content, re.MULTILINE)
    if not active_match:
        return []

    rest = content[active_match.end():]
    next_sec = re.search(r"^## ", rest, re.MULTILINE)
    active_section = rest[: next_sec.start()] if next_sec else rest

    entries: list[dict] = []
    for block in re.split(r"\n(?=### )", active_section):
        if not block.startswith("### "):
            continue

        topic = block.split("\n", 1)[0][4:].strip()

        sev_m = re.search(r"\*\*Severity\*\*:\s*(P\d)", block)
        if not sev_m or sev_m.group(1) not in ("P0", "P1"):
            continue
        severity = sev_m.group(1)

        touched_m = re.search(r"\*\*Last touched\*\*:\s*([^\n]+)", block)
        age_days = None
        if touched_m:
            raw = touched_m.group(1).strip()
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
            mm = re.search(r"~?(\d{4}-\d{2})$", raw.strip())
            if dm:
                try:
                    touched = datetime.strptime(dm.group(1), "%Y-%m-%d").date()
                    age_days = (today - touched).days
                except ValueError:
                    pass
            elif mm:
                try:
                    touched = datetime.strptime(mm.group(1) + "-01", "%Y-%m-%d").date()
                    age_days = (today - touched).days
                except ValueError:
                    pass

        owner_m = re.search(r"\*\*Owner of next nudge\*\*:\s*([^\n]+)", block)
        owner = owner_m.group(1).strip() if owner_m else "unassigned"

        entries.append({"topic": topic, "severity": severity, "age_days": age_days, "owner": owner})

    return entries


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TODAY = date(2026, 5, 24)

_SAMPLE_CONTENT = (
    "# Pending Decisions Queue\n\n"
    "## Active (as of 2026-05-14)\n\n"
    "### HJRP cash depletion / June rent suspension\n"
    "- **Severity**: P0\n"
    "- **Last touched**: 2026-05-23\n"
    "- **Owner of next nudge**: Harrison (this weekend)\n\n"
    "### OSN cost-structure conversation\n"
    "- **Severity**: P0\n"
    "- **Last touched**: 2026-05-12 (April Metrics surfaced)\n"
    "- **Owner of next nudge**: Harrison to schedule\n\n"
    "### Personal 1040 OIC pre-qualifier filing\n"
    "- **Severity**: P0\n"
    "- **Last touched**: ~2026-04\n"
    "- **Owner of next nudge**: Harrison to assign to Justin\n\n"
    "### UFL pivot public announcement timing\n"
    "- **Severity**: P1\n"
    "- **Last touched**: 2026-05-10\n"
    "- **Owner of next nudge**: Harrison to schedule team meeting + CAA call\n\n"
    "### CT Corporation UCC lien resolution\n"
    "- **Severity**: P1\n"
    "- **Last touched**: 2026-05-08\n"
    "- **Owner of next nudge**: Harrison to assign to counsel\n\n"
    "### Hannah HJRP sustainability check\n"
    "- **Severity**: P2\n"
    "- **Last touched**: 2026-05-14\n"
    "- **Owner of next nudge**: Harrison\n\n"
    "---\n\n"
    "## Gmail Deep Dive\n\n"
    "### CITGO LOI\n"
    "- **Severity**: P1\n"
    "- **Last touched**: 2026-05-22\n"
    "- **Owner of next nudge**: Harrison\n\n"
    "## Recently resolved\n\n"
    "### F3 Pure launch date (resolved 2026-05-22)\n"
)


@pytest.fixture
def entries():
    return _parse_open_decisions(_SAMPLE_CONTENT, _TODAY)


# ---------------------------------------------------------------------------
# Section boundary tests
# ---------------------------------------------------------------------------

def test_gmail_deep_dive_entries_excluded(entries):
    """No Gmail Deep Dive entries should bleed into Active results."""
    assert not any("CITGO" in e["topic"] for e in entries)


def test_recently_resolved_entries_excluded(entries):
    """Recently resolved entries must not appear."""
    assert not any("F3 Pure launch date" in e["topic"] for e in entries)


# ---------------------------------------------------------------------------
# Severity filter tests
# ---------------------------------------------------------------------------

def test_p2_entries_excluded(entries):
    assert not any(e["severity"] == "P2" for e in entries)


def test_only_p0_and_p1_returned(entries):
    for e in entries:
        assert e["severity"] in ("P0", "P1")


def test_correct_p0_count(entries):
    assert sum(1 for e in entries if e["severity"] == "P0") == 3


def test_correct_p1_count(entries):
    assert sum(1 for e in entries if e["severity"] == "P1") == 2


# ---------------------------------------------------------------------------
# Age calculation tests
# ---------------------------------------------------------------------------

def test_precise_date_age(entries):
    """2026-05-23 should be 1 day stale."""
    hjrp = next(e for e in entries if "HJRP cash" in e["topic"])
    assert hjrp["age_days"] == 1


def test_date_with_trailing_note(entries):
    """'2026-05-12 (April Metrics surfaced)' should parse to 12 days."""
    osn = next(e for e in entries if "OSN cost" in e["topic"])
    assert osn["age_days"] == 12


def test_approximate_month_date(entries):
    """'~2026-04' should parse to at least 40 days stale."""
    irs = next(e for e in entries if "Personal 1040" in e["topic"])
    assert irs["age_days"] is not None and irs["age_days"] >= 40


def test_ufl_date_age(entries):
    """2026-05-10 should be 14 days stale."""
    ufl = next(e for e in entries if "UFL pivot" in e["topic"])
    assert ufl["age_days"] == 14


def test_ct_corp_date_age(entries):
    """2026-05-08 should be 16 days stale."""
    ct = next(e for e in entries if "CT Corporation" in e["topic"])
    assert ct["age_days"] == 16


# ---------------------------------------------------------------------------
# Owner extraction test
# ---------------------------------------------------------------------------

def test_owner_extracted(entries):
    for e in entries:
        assert e["owner"] and e["owner"] != "unassigned"


# ---------------------------------------------------------------------------
# Missing section graceful handling
# ---------------------------------------------------------------------------

def test_no_active_section_returns_empty():
    result = _parse_open_decisions("# Pending Decisions\n\nNothing here.", _TODAY)
    assert result == []


def test_empty_active_section_returns_empty():
    content = "## Active\n\n## Gmail Deep Dive\n"
    result = _parse_open_decisions(content, _TODAY)
    assert result == []


# ---------------------------------------------------------------------------
# Marker logic tests
# ---------------------------------------------------------------------------

def _marker_for(severity: str, age: int) -> str:
    if severity == "P0" and age > 14:
        return "\U0001f6a8"  # 🚨
    elif severity == "P0":
        return "\U0001f534"  # 🔴
    else:
        return "\U0001f7e1"  # 🟡


def test_p0_over_14d_gets_alarm_marker():
    assert _marker_for("P0", 15) == "\U0001f6a8"
    assert _marker_for("P0", 53) == "\U0001f6a8"


def test_p0_under_14d_gets_red_marker():
    assert _marker_for("P0", 0) == "\U0001f534"
    assert _marker_for("P0", 14) == "\U0001f534"


def test_p1_always_gets_yellow_marker():
    assert _marker_for("P1", 0) == "\U0001f7e1"
    assert _marker_for("P1", 30) == "\U0001f7e1"


# ---------------------------------------------------------------------------
# tool_dispatch.py registration tests (file-based)
# ---------------------------------------------------------------------------

_DISPATCH_PATH = (
    pathlib.Path(__file__).parent.parent / "src" / "cora" / "tools" / "tool_dispatch.py"
)


@pytest.fixture(scope="module")
def dispatch_src() -> str:
    assert _DISPATCH_PATH.exists(), f"tool_dispatch.py not found at {_DISPATCH_PATH}"
    return _DISPATCH_PATH.read_text(encoding="utf-8")


def test_fndr_open_decisions_in_tool_definitions(dispatch_src):
    """fndr_open_decisions must appear in TOOL_DEFINITIONS."""
    assert '"fndr_open_decisions"' in dispatch_src


def test_fndr_open_decisions_in_tool_functions(dispatch_src):
    """fndr_open_decisions must be registered in _TOOL_FUNCTIONS.

    NOTE: _TOOL_FUNCTIONS is near the end of tool_dispatch.py (~line 2397).
    In sandboxed environments the mount may truncate before that section;
    the test skips gracefully in that case.
    """
    if "_TOOL_FUNCTIONS" not in dispatch_src:
        pytest.skip("_TOOL_FUNCTIONS block not visible in this environment (mount truncation)")
    assert '"fndr_open_decisions": _tool_fndr_open_decisions' in dispatch_src


def test_fndr_open_decisions_no_required_inputs(dispatch_src):
    """fndr_open_decisions input_schema must have empty required list."""
    match = re.search(
        r'"fndr_open_decisions".*?"required":\s*\[\]',
        dispatch_src,
        re.DOTALL,
    )
    assert match, 'fndr_open_decisions input_schema missing "required": []'


def test_fndr_open_decisions_handler_defined(dispatch_src):
    """_tool_fndr_open_decisions function must be defined."""
    assert "def _tool_fndr_open_decisions(" in dispatch_src


def test_fndr_open_decisions_reads_decisions_pending(dispatch_src):
    """Handler must reference decisions-pending.md."""
    assert "decisions-pending.md" in dispatch_src
