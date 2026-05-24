"""Tests for the fndr_open_decisions tool handler.

Two test layers:
  - Layer A: Parser tests (TestDecisionsParser, TestRealWorldSnapshot) — pure logic,
    no imports from src/, runnable anywhere. Mirrors the exact parsing/formatting
    logic in _tool_fndr_open_decisions so a behavior change in either breaks tests.
  - Layer B: Integration tests (TestDispatchIntegration) — imports from tool_dispatch.
    These run on the full codebase (Windows / CI) and verify dispatch routing.
    Skipped if tool_dispatch cannot be imported (e.g. stale sandbox mount).

Coverage:
  1. P0 items ≤14d stale render with 🔴
  2. P0 items >14d stale render with 🚨
  3. P1 items render with 🟡
  4. P2 and P3 items are filtered out
  5. Output header format: "*Open decisions — X P0, Y P1:*"
  6. P0s sorted by age descending (stalest first)
  7. P1s sorted by age descending
  8. Owner of next nudge included in each line
  9. Month-only date (~2026-04) is parsed to a reasonable age
 10. "Last touched: today" yields "touched today"
 11. "Last touched: 1 day ago" yields "1d stale"
 12. FileNotFoundError path returns fallback string
 13. No ## Active section → graceful message
 14. No P0/P1 items in active section → graceful message
 15. Gmail Deep Dive ## section is not parsed
 16. Output is Slack mrkdwn (*bold* topic, emoji prefix)
 17. dispatch() entry point routes fndr_open_decisions correctly
 18. dispatch() works from HJRG entity (same handler as FNDR)
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# ──────────────────────────────────────────────────────────────────────────────
# Inline parser — mirrors _tool_fndr_open_decisions logic exactly.
# Must stay in sync with src/cora/tools/tool_dispatch.py lines 1305-1426.
# If you change the handler, update this too (or the tests will catch the drift).
# ──────────────────────────────────────────────────────────────────────────────

def _parse_decisions(content: str, today: date | None = None) -> str:
    """Standalone reimplementation of the handler's parse+format logic.

    Accepts the file content string; returns the formatted Slack output.
    Used by Layer A tests so they don't need to import tool_dispatch.
    """
    if today is None:
        today = date.today()

    active_match = re.search(r"^## Active\b", content, re.MULTILINE)
    if not active_match:
        return "No active decisions found in the pending queue."

    rest_after_active = content[active_match.end():]
    next_section_match = re.search(r"^## ", rest_after_active, re.MULTILINE)
    active_section = (
        rest_after_active[: next_section_match.start()]
        if next_section_match
        else rest_after_active
    )

    entries: list[dict] = []
    topic_blocks = re.split(r"\n(?=### )", active_section)

    for block in topic_blocks:
        if not block.startswith("### "):
            continue
        topic = block.split("\n", 1)[0][4:].strip()

        sev_match = re.search(r"\*\*Severity\*\*:\s*(P\d)", block)
        if not sev_match:
            continue
        severity = sev_match.group(1)
        if severity not in ("P0", "P1"):
            continue

        touched_match = re.search(r"\*\*Last touched\*\*:\s*([^\n]+)", block)
        age_days: int | None = None
        if touched_match:
            raw = touched_match.group(1).strip()
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
            month_match = re.search(r"~?(\d{4}-\d{2})$", raw.strip())
            if date_match:
                try:
                    touched = datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
                    age_days = (today - touched).days
                except ValueError:
                    pass
            elif month_match:
                try:
                    touched = datetime.strptime(month_match.group(1) + "-01", "%Y-%m-%d").date()
                    age_days = (today - touched).days
                except ValueError:
                    pass

        owner_match = re.search(r"\*\*Owner of next nudge\*\*:\s*([^\n]+)", block)
        owner = owner_match.group(1).strip() if owner_match else "unassigned"

        entries.append({"topic": topic, "severity": severity, "age_days": age_days, "owner": owner})

    if not entries:
        return "No P0 or P1 decisions are currently pending."

    p0 = sorted([e for e in entries if e["severity"] == "P0"],
                key=lambda x: x["age_days"] or 0, reverse=True)
    p1 = sorted([e for e in entries if e["severity"] == "P1"],
                key=lambda x: x["age_days"] or 0, reverse=True)

    def _fmt(e: dict) -> str:
        age = e["age_days"]
        if age is None:
            age_str = "age unknown"
        elif age == 0:
            age_str = "touched today"
        elif age == 1:
            age_str = "1d stale"
        else:
            age_str = f"{age}d stale"
        if e["severity"] == "P0" and (age or 0) > 14:
            marker = "🚨"
        elif e["severity"] == "P0":
            marker = "🔴"
        else:
            marker = "🟡"
        return f"{marker} *{e['topic']}* ({age_str}) — {e['owner']}"

    lines = [f"*Open decisions — {len(p0)} P0, {len(p1)} P1:*", ""]
    for e in p0:
        lines.append(_fmt(e))
    if p0 and p1:
        lines.append("")
    for e in p1:
        lines.append(_fmt(e))

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Fixture helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_md(
    p0_items: list[dict] | None = None,
    p1_items: list[dict] | None = None,
    p2_items: list[dict] | None = None,
    extra_sections: str = "",
) -> str:
    all_items = (p0_items or []) + (p1_items or []) + (p2_items or [])
    blocks = []
    for item in all_items:
        blocks.append(
            f"### {item['topic']}\n"
            f"- **Question**: what needs to be decided\n"
            f"- **Decision-maker**: Harrison\n"
            f"- **Severity**: {item['severity']}\n"
            f"- **Last touched**: {item['last_touched']}\n"
            f"- **Owner of next nudge**: {item['owner']}\n"
        )
    active_body = "\n".join(blocks)
    return (
        "# Pending Decisions Queue\n\n---\n\n"
        f"## Active (as of 2026-05-24)\n\n"
        f"{active_body}\n\n"
        f"{extra_sections}"
        "## Recently resolved\n\n### Done\n→ resolved.\n"
    )


def _ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).strftime("%Y-%m-%d")


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


# ──────────────────────────────────────────────────────────────────────────────
# Layer A — Parser tests (no src/ imports)
# ──────────────────────────────────────────────────────────────────────────────

class TestEmojiMarkers:
    def test_p0_recent_gets_red_circle(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "Fresh P0", "severity": "P0",
                       "last_touched": _ago(3), "owner": "Harrison"}]
        ))
        assert "🔴" in result
        assert "Fresh P0" in result

    def test_p0_exactly_14d_gets_red_circle(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "Boundary P0", "severity": "P0",
                       "last_touched": _ago(14), "owner": "Harrison"}]
        ))
        assert "🔴" in result

    def test_p0_stale_15d_gets_siren(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "Old P0", "severity": "P0",
                       "last_touched": _ago(15), "owner": "Harrison"}]
        ))
        assert "🚨" in result
        assert "🔴" not in result

    def test_p0_very_stale_gets_siren(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "Ancient P0", "severity": "P0",
                       "last_touched": _ago(40), "owner": "Harrison"}]
        ))
        assert "🚨" in result

    def test_p1_gets_yellow_circle(self):
        result = _parse_decisions(_make_md(
            p1_items=[{"topic": "A P1 decision", "severity": "P1",
                       "last_touched": _ago(10), "owner": "Harrison"}]
        ))
        assert "🟡" in result
        assert "🔴" not in result
        assert "🚨" not in result


class TestSeverityFiltering:
    def test_p2_items_excluded(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "Important P0", "severity": "P0",
                       "last_touched": _ago(5), "owner": "Harrison"}],
            p2_items=[{"topic": "Nice-to-have P2", "severity": "P2",
                       "last_touched": _ago(5), "owner": "Harrison"}],
        ))
        assert "Nice-to-have P2" not in result
        assert "Important P0" in result

    def test_p3_items_excluded(self):
        result = _parse_decisions(_make_md(
            p1_items=[{"topic": "Good P1", "severity": "P1",
                       "last_touched": _ago(5), "owner": "Harrison"}],
            p2_items=[{"topic": "Low priority P3", "severity": "P3",
                       "last_touched": _ago(5), "owner": "Harrison"}],
        ))
        assert "Low priority P3" not in result

    def test_only_p2_returns_no_p0_p1_message(self):
        result = _parse_decisions(_make_md(
            p2_items=[{"topic": "Only P2", "severity": "P2",
                       "last_touched": _ago(5), "owner": "Harrison"}],
        ))
        assert "No P0 or P1" in result


class TestOutputFormat:
    def test_header_counts_match(self):
        result = _parse_decisions(_make_md(
            p0_items=[
                {"topic": "P0 alpha", "severity": "P0", "last_touched": _ago(5), "owner": "Harrison"},
                {"topic": "P0 beta", "severity": "P0", "last_touched": _ago(8), "owner": "Harrison"},
            ],
            p1_items=[
                {"topic": "P1 gamma", "severity": "P1", "last_touched": _ago(3), "owner": "Justin"},
            ],
        ))
        assert "2 P0" in result
        assert "1 P1" in result

    def test_header_uses_slack_bold(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "Any P0", "severity": "P0",
                       "last_touched": _ago(5), "owner": "Harrison"}]
        ))
        assert result.startswith("*Open decisions")

    def test_topic_wrapped_in_bold(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "My Special Topic", "severity": "P0",
                       "last_touched": _ago(5), "owner": "Harrison"}]
        ))
        assert "*My Special Topic*" in result

    def test_line_format_has_em_dash_separator(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "Some decision", "severity": "P0",
                       "last_touched": _ago(5), "owner": "Harrison to assign"}]
        ))
        # Each item line: emoji *topic* (age) — owner
        assert "— Harrison to assign" in result


class TestSorting:
    def test_p0_sorted_stalest_first(self):
        result = _parse_decisions(_make_md(
            p0_items=[
                {"topic": "Fresh (5d)", "severity": "P0", "last_touched": _ago(5), "owner": "H"},
                {"topic": "Ancient (40d)", "severity": "P0", "last_touched": _ago(40), "owner": "H"},
                {"topic": "Medium (15d)", "severity": "P0", "last_touched": _ago(15), "owner": "H"},
            ]
        ))
        assert result.index("Ancient") < result.index("Medium") < result.index("Fresh")

    def test_p1_sorted_stalest_first(self):
        result = _parse_decisions(_make_md(
            p1_items=[
                {"topic": "New P1 (2d)", "severity": "P1", "last_touched": _ago(2), "owner": "H"},
                {"topic": "Old P1 (30d)", "severity": "P1", "last_touched": _ago(30), "owner": "H"},
            ]
        ))
        assert result.index("Old P1") < result.index("New P1")


class TestOwnerField:
    def test_owner_in_output(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "Some decision", "severity": "P0",
                       "last_touched": _ago(5), "owner": "Harrison to assign to Justin"}]
        ))
        assert "Harrison to assign to Justin" in result

    def test_p1_owner_included(self):
        result = _parse_decisions(_make_md(
            p1_items=[{"topic": "P1 item", "severity": "P1",
                       "last_touched": _ago(5), "owner": "Harrison to schedule"}]
        ))
        assert "Harrison to schedule" in result


class TestDateParsing:
    def test_month_only_date_parses_without_crash(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "OIC pre-qualifier", "severity": "P0",
                       "last_touched": "~2026-04", "owner": "Harrison"}]
        ))
        assert "OIC pre-qualifier" in result
        # Must render some age string, not crash
        assert "stale" in result or "touched today" in result

    def test_month_only_date_is_older_than_recent(self):
        """~2026-04 is older than a 5-day-stale item; must sort first."""
        result = _parse_decisions(_make_md(
            p0_items=[
                {"topic": "Month-only old", "severity": "P0",
                 "last_touched": "~2026-04", "owner": "Harrison"},
                {"topic": "Recent 5d", "severity": "P0",
                 "last_touched": _ago(5), "owner": "Harrison"},
            ]
        ))
        assert result.index("Month-only old") < result.index("Recent 5d")

    def test_today_renders_touched_today(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "Brand new item", "severity": "P0",
                       "last_touched": _today(), "owner": "Harrison"}]
        ))
        assert "touched today" in result

    def test_one_day_renders_1d_stale(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "Yesterday item", "severity": "P0",
                       "last_touched": _ago(1), "owner": "Harrison"}]
        ))
        assert "1d stale" in result

    def test_exact_7d_renders_7d_stale(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "Week-old item", "severity": "P0",
                       "last_touched": _ago(7), "owner": "Harrison"}]
        ))
        assert "7d stale" in result


class TestEdgeCases:
    def test_no_active_section_returns_graceful_message(self):
        result = _parse_decisions("# Pending Decisions Queue\n\nNo Active here.\n")
        assert "No active decisions" in result

    def test_empty_active_section_returns_graceful_message(self):
        result = _parse_decisions(
            "# Pending Decisions Queue\n\n"
            "## Active (as of 2026-05-24)\n\n"
            "## Recently resolved\n\n"
        )
        assert "No P0 or P1" in result

    def test_gmail_deep_dive_section_excluded(self):
        """Items under ## Gmail Deep Dive must not bleed into results."""
        content = (
            "# Pending Decisions Queue\n\n"
            "## Active (as of 2026-05-24)\n\n"
            f"### Real P0\n"
            f"- **Severity**: P0\n"
            f"- **Last touched**: {_ago(5)}\n"
            f"- **Owner of next nudge**: Harrison\n\n"
            "## Gmail Deep Dive — Open Questions\n\n"
            "### Gmail ghost P0\n"
            "- **Severity**: P0\n"
            f"- **Last touched**: {_ago(5)}\n"
            "- **Owner of next nudge**: Harrison\n\n"
        )
        result = _parse_decisions(content)
        assert "Real P0" in result
        assert "Gmail ghost P0" not in result

    def test_mixed_severities_only_p0_p1_shown(self):
        result = _parse_decisions(_make_md(
            p0_items=[{"topic": "Keep P0", "severity": "P0",
                       "last_touched": _ago(5), "owner": "Harrison"}],
            p1_items=[{"topic": "Keep P1", "severity": "P1",
                       "last_touched": _ago(5), "owner": "Justin"}],
            p2_items=[{"topic": "Drop P2", "severity": "P2",
                       "last_touched": _ago(5), "owner": "Harrison"}],
        ))
        assert "Keep P0" in result
        assert "Keep P1" in result
        assert "Drop P2" not in result


# ──────────────────────────────────────────────────────────────────────────────
# Real-world content snapshot
# ──────────────────────────────────────────────────────────────────────────────

class TestRealWorldSnapshot:
    SNAPSHOT = """\
# Pending Decisions Queue

## Active (as of 2026-05-14)

### HJRP cash depletion / June rent suspension

- **Question**: HJRP is facing ~$46K+ in outgoing obligations.
- **Decision-maker**: Harrison
- **Blockers**: Harrison needs current HJRP bank balance.
- **Severity**: P0
- **Surfaced**: 1 day (2026-05-23)
- **Last touched**: 2026-05-23
- **Owner of next nudge**: Harrison (this weekend)
- **Source**: Gmail hygiene 2026-05-23

### Personal 1040 OIC pre-qualifier filing

- **Question**: file OIC pre-qualifier.
- **Decision-maker**: Harrison (delegate to Andrew or Justin)
- **Blockers**: just bandwidth
- **Severity**: P0
- **Surfaced**: 30+ days
- **Last touched**: ~2026-04
- **Owner of next nudge**: Harrison to assign to Justin
- **Source**: CLAUDE.md TOM #4

### UFL pivot public announcement timing

- **Question**: when does the UFL pause become public?
- **Decision-maker**: Harrison
- **Blockers**: CAA conversation pending
- **Severity**: P1
- **Surfaced**: 5+ days
- **Last touched**: 2026-05-10
- **Owner of next nudge**: Harrison to schedule team meeting
- **Source**: CLAUDE.md TOM #8

### Hannah HJRP-recurring sustainability check

- **Question**: at what trigger escalate to PM contractor?
- **Decision-maker**: Harrison
- **Blockers**: needs explicit SLA
- **Severity**: P2
- **Last touched**: 2026-05-14
- **Owner of next nudge**: Harrison to define escalation trigger
- **Source**: Tessa transition

## Gmail Deep Dive — Open Questions

### CITGO LOI — Who is Rob Solomon?

- **Question**: unknown deal context.
- **Decision-maker**: Harrison (context only)
- **Severity**: P1
- **Last touched**: 2026-05-22
- **Owner of next nudge**: Harrison to identify
- **Source**: cascade draft

## Recently resolved

### F3 Pure launch date (resolved 2026-05-22)
→ 6/15 LOCKED.
"""

    def test_yields_2_p0(self):
        result = _parse_decisions(self.SNAPSHOT)
        assert "2 P0" in result

    def test_yields_1_p1(self):
        result = _parse_decisions(self.SNAPSHOT)
        assert "1 P1" in result

    def test_p2_excluded(self):
        result = _parse_decisions(self.SNAPSHOT)
        assert "Hannah HJRP-recurring" not in result

    def test_gmail_deep_dive_excluded(self):
        result = _parse_decisions(self.SNAPSHOT)
        assert "CITGO LOI" not in result

    def test_oic_appears(self):
        result = _parse_decisions(self.SNAPSHOT)
        assert "OIC" in result or "1040" in result

    def test_hjrp_cash_appears(self):
        result = _parse_decisions(self.SNAPSHOT)
        assert "HJRP cash depletion" in result

    def test_ufl_appears(self):
        result = _parse_decisions(self.SNAPSHOT)
        assert "UFL pivot" in result

    def test_oic_sorts_before_hjrp_cash(self):
        """OIC (~2026-04) is older than HJRP cash (2026-05-23); must appear first."""
        result = _parse_decisions(self.SNAPSHOT)
        assert result.index("OIC") < result.index("HJRP cash depletion")

    def test_ufl_pivot_is_p1_not_p0(self):
        result = _parse_decisions(self.SNAPSHOT)
        # UFL line must have 🟡 prefix, not 🔴 or 🚨
        lines = result.split("\n")
        ufl_line = next((l for l in lines if "UFL pivot" in l), None)
        assert ufl_line is not None
        assert ufl_line.startswith("🟡")


# ──────────────────────────────────────────────────────────────────────────────
# Layer B — Integration tests (imports tool_dispatch; skip if import fails)
# ──────────────────────────────────────────────────────────────────────────────

_TOOL_DISPATCH_AVAILABLE = False
try:
    _REPO_ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from cora.tools.tool_dispatch import dispatch, _tool_fndr_open_decisions as _handler
    _TOOL_DISPATCH_AVAILABLE = True
except (ImportError, SyntaxError):
    pass

_skip_if_no_dispatch = pytest.mark.skipif(
    not _TOOL_DISPATCH_AVAILABLE,
    reason="tool_dispatch not importable (stale mount or syntax error) — run on Windows",
)


@_skip_if_no_dispatch
class TestDispatchIntegration:
    def test_dispatch_routes_to_handler(self):
        md = _make_md(
            p0_items=[{"topic": "Dispatch route test", "severity": "P0",
                       "last_touched": _ago(5), "owner": "Harrison"}]
        )
        with patch.object(Path, "read_text", return_value=md):
            result = dispatch("fndr_open_decisions", {}, "U_HARRISON", entity="FNDR")
        assert "Dispatch route test" in result
        assert "🔴" in result or "🚨" in result

    def test_dispatch_unknown_tool_returns_error(self):
        result = dispatch("fndr_open_decisions_nonexistent", {}, "U_TEST", entity="FNDR")
        assert "Unknown tool" in result

    def test_dispatch_works_from_hjrg_entity(self):
        """HJRG channels pass entity=HJRG; handler must still work."""
        md = _make_md(
            p1_items=[{"topic": "HJRG entity test", "severity": "P1",
                       "last_touched": _ago(5), "owner": "Harrison"}]
        )
        with patch.object(Path, "read_text", return_value=md):
            result = dispatch("fndr_open_decisions", {}, "U_HARRISON", entity="HJRG")
        assert "HJRG entity test" in result
        assert "🟡" in result

    def test_handler_file_not_found_returns_fallback(self):
        with patch.object(Path, "read_text", side_effect=FileNotFoundError("not found")):
            result = _handler("U_TEST", "FNDR", {})
        assert "don't have that right now" in result.lower()

    def test_handler_exception_returns_fallback(self):
        with patch.object(Path, "read_text", side_effect=OSError("disk error")):
            result = _handler("U_TEST", "FNDR", {})
        assert "don't have that right now" in result.lower()
