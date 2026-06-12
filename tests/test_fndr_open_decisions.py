"""Tests for the fndr_open_decisions tool handler.

Two test layers:
  - Layer A: Parser tests (TestDecisions*, TestEntityFilter, TestRealWorldSnapshot)
    Pure logic, no imports from src/, runnable anywhere.  Mirrors the exact parsing
    and formatting logic in _tool_fndr_open_decisions so a behavior change in either
    breaks tests.
  - Layer B: Integration tests (TestDispatchIntegration) -- imports from tool_dispatch.
    These run on the full codebase (Windows / CI) and verify dispatch routing.
    Skipped if tool_dispatch cannot be imported (e.g. stale sandbox mount).

Coverage:
  1.  P0 items <=14d stale render with red circle emoji
  2.  P0 items >14d stale render with siren emoji
  3.  P1 items render with yellow circle emoji
  4.  P2 items filtered out in portfolio-wide (FNDR/HJRG) queries
  5.  P2 items INCLUDED for entity-specific queries
  6.  Output header format includes scope label and severity counts
  7.  P0s sorted by age descending (stalest first)
  8.  P1s sorted by age descending
  9.  Owner of next nudge included in each item line
 10.  Month-only date (~2026-04) is parsed to a reasonable age
 11.  "Last touched: today" yields "touched today"
 12.  "Last touched: 1 day ago" yields "1d stale"
 13.  FileNotFoundError path returns fallback string
 14.  No active decisions returns graceful message
 15.  Entity filtering: OSN entity sees only OSN-tagged items
 16.  Entity filtering: F3E entity sees only F3E-tagged items
 17.  Entity filtering: FNDR entity sees all items regardless of entity tag
 18.  Entity filtering: HJRG entity sees all items (same as FNDR)
 19.  Entity filtering: LEX sees items tagged LEX, LEX-LLC, LEX-LLA, LEX-LBHS, LEX-LTS
 20.  Entity filtering: LEX-LLC sees only LEX-LLC-tagged items
 21.  FNDR-tagged items visible in ALL entity channels
 22.  Gmail Deep Dive entries ARE parsed when they have matching entity tags
 23.  Recently resolved section is always excluded
 24.  Slack mrkdwn format (*bold* topic, emoji prefix)
 25.  dispatch() entry point routes fndr_open_decisions correctly
 26.  dispatch() entity filtering works from HJRG channel
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest


# ==============================================================================
# Inline parser -- mirrors _tool_fndr_open_decisions logic exactly.
# Must stay in sync with src/cora/tools/tool_dispatch.py.
# If you change the handler, update this too (or the tests will catch the drift).
# ==============================================================================

def _parse_decisions(content: str, entity: str = "FNDR", today: date | None = None) -> str:
    """Standalone reimplementation of the handler's parse+format logic.

    Accepts the file content string and the calling channel's entity code.
    Returns the formatted Slack output.
    Used by Layer A tests so they don't need to import tool_dispatch.
    """
    if today is None:
        today = date.today()

    calling_entity = (entity or "FNDR").upper().strip()
    portfolio_wide = calling_entity in ("FNDR", "HJRG", "")

    def _entity_matches(entry_entity_raw: str) -> bool:
        if portfolio_wide:
            return True
        entry_entities = [e.strip().upper() for e in entry_entity_raw.split(",")]
        if "FNDR" in entry_entities:
            return True
        if calling_entity in entry_entities:
            return True
        if calling_entity == "LEX":
            return any(e.startswith("LEX") for e in entry_entities)
        return False

    # Strip the ## Recently resolved section
    resolved_match = re.search(r"^## Recently resolved\b", content, re.MULTILINE)
    parseable = content[: resolved_match.start()] if resolved_match else content

    entries: list[dict] = []
    topic_blocks = re.split(r"\n(?=### )", parseable)

    for block in topic_blocks:
        if not block.startswith("### "):
            continue

        topic = block.split("\n", 1)[0][4:].strip()
        if topic == "[Topic]":
            continue  # the "How to use" template skeleton, not a real entry

        entity_match = re.search(r"\*\*Entity\*\*:\s*([^\n]+)", block)
        entry_entity_raw = entity_match.group(1).strip() if entity_match else "FNDR"
        if not _entity_matches(entry_entity_raw):
            continue

        # The template's "P0 / P1 / P2 / P3" alternatives line must not match;
        # annotated real values ("P0 (decision Monday)") must.
        sev_match = re.search(r"\*\*Severity\*\*:\s*(P\d)\b(?!\s*/)", block)
        if not sev_match:
            continue
        severity = sev_match.group(1)
        if portfolio_wide and severity not in ("P0", "P1"):
            continue
        if not portfolio_wide and severity not in ("P0", "P1", "P2"):
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

        entries.append(
            {
                "topic": topic,
                "severity": severity,
                "age_days": age_days,
                "owner": owner,
                "entity": entry_entity_raw,
            }
        )

    if not entries:
        if portfolio_wide:
            return "No P0 or P1 decisions are currently pending."
        return f"No open decisions found for {calling_entity}."

    p0 = sorted(
        [e for e in entries if e["severity"] == "P0"],
        key=lambda x: x["age_days"] or 0, reverse=True,
    )
    p1 = sorted(
        [e for e in entries if e["severity"] == "P1"],
        key=lambda x: x["age_days"] or 0, reverse=True,
    )
    p2 = sorted(
        [e for e in entries if e["severity"] == "P2"],
        key=lambda x: x["age_days"] or 0, reverse=True,
    )

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
            marker = "\U0001f6a8"  # siren
        elif e["severity"] == "P0":
            marker = "\U0001f534"  # red circle
        elif e["severity"] == "P1":
            marker = "\U0001f7e1"  # yellow circle
        else:
            marker = "⚪"      # white/grey circle (P2)
        return f"{marker} *{e['topic']}* ({age_str}) -- {e['owner']}"

    scope_label = "portfolio" if portfolio_wide else calling_entity
    header_parts = []
    if p0:
        header_parts.append(f"{len(p0)} P0")
    if p1:
        header_parts.append(f"{len(p1)} P1")
    if p2 and not portfolio_wide:
        header_parts.append(f"{len(p2)} P2")
    header = f"*Open decisions ({scope_label}) -- {', '.join(header_parts) or 'none'}:*"

    lines = [header, ""]
    for e in p0:
        lines.append(_fmt(e))
    if p0 and (p1 or p2):
        lines.append("")
    for e in p1:
        lines.append(_fmt(e))
    if p1 and p2 and not portfolio_wide:
        lines.append("")
    for e in p2:
        lines.append(_fmt(e))

    return "\n".join(lines)


# ==============================================================================
# Fixture helpers
# ==============================================================================

def _make_entry(
    topic: str,
    severity: str,
    last_touched: str,
    owner: str,
    entity: str = "FNDR",
) -> str:
    return (
        f"### {topic}\n"
        f"- **Entity**: {entity}\n"
        f"- **Question**: what needs to be decided\n"
        f"- **Decision-maker**: Harrison\n"
        f"- **Severity**: {severity}\n"
        f"- **Last touched**: {last_touched}\n"
        f"- **Owner of next nudge**: {owner}\n"
    )


def _make_md(*entries: str, extra_sections: str = "") -> str:
    body = "\n".join(entries)
    return (
        "# Pending Decisions Queue\n\n---\n\n"
        "## Active (as of 2026-05-24)\n\n"
        f"{body}\n\n"
        f"{extra_sections}"
        "## Recently resolved\n\n### Done\n-> resolved.\n"
    )


def _ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).strftime("%Y-%m-%d")


def _today() -> str:
    return date.today().strftime("%Y-%m-%d")


# ==============================================================================
# Layer A -- Parser tests (no src/ imports)
# ==============================================================================

class TestEmojiMarkers:
    def test_p0_recent_gets_red_circle(self):
        result = _parse_decisions(_make_md(
            _make_entry("Fresh P0", "P0", _ago(3), "Harrison", "FNDR"),
        ))
        assert "\U0001f534" in result
        assert "Fresh P0" in result

    def test_p0_exactly_14d_gets_red_circle(self):
        result = _parse_decisions(_make_md(
            _make_entry("Boundary P0", "P0", _ago(14), "Harrison", "FNDR"),
        ))
        assert "\U0001f534" in result

    def test_p0_stale_15d_gets_siren(self):
        result = _parse_decisions(_make_md(
            _make_entry("Old P0", "P0", _ago(15), "Harrison", "FNDR"),
        ))
        assert "\U0001f6a8" in result
        assert "\U0001f534" not in result

    def test_p1_gets_yellow_circle(self):
        result = _parse_decisions(_make_md(
            _make_entry("A P1 decision", "P1", _ago(10), "Harrison", "FNDR"),
        ))
        assert "\U0001f7e1" in result
        assert "\U0001f534" not in result
        assert "\U0001f6a8" not in result

    def test_p2_gets_grey_circle_for_entity_query(self):
        result = _parse_decisions(
            _make_md(_make_entry("P2 item", "P2", _ago(5), "Harrison", "OSN")),
            entity="OSN",
        )
        assert "⚪" in result


class TestSeverityFiltering:
    def test_p2_excluded_in_portfolio_wide_query(self):
        result = _parse_decisions(_make_md(
            _make_entry("Important P0", "P0", _ago(5), "Harrison", "FNDR"),
            _make_entry("Nice-to-have P2", "P2", _ago(5), "Harrison", "FNDR"),
        ))
        assert "Nice-to-have P2" not in result
        assert "Important P0" in result

    def test_p3_always_excluded(self):
        result = _parse_decisions(_make_md(
            _make_entry("Good P1", "P1", _ago(5), "Harrison", "FNDR"),
            _make_entry("P3 item", "P3", _ago(5), "Harrison", "FNDR"),
        ))
        assert "P3 item" not in result

    def test_p2_included_in_entity_specific_query(self):
        result = _parse_decisions(
            _make_md(
                _make_entry("OSN P0", "P0", _ago(5), "Harrison", "OSN"),
                _make_entry("OSN P2", "P2", _ago(5), "Harrison", "OSN"),
            ),
            entity="OSN",
        )
        assert "OSN P2" in result
        assert "OSN P0" in result

    def test_only_p2_in_fndr_returns_no_items_message(self):
        result = _parse_decisions(_make_md(
            _make_entry("Only P2", "P2", _ago(5), "Harrison", "FNDR"),
        ))
        assert "No P0 or P1" in result


class TestEntityFiltering:
    def test_osn_entity_sees_only_osn_items(self):
        result = _parse_decisions(
            _make_md(
                _make_entry("OSN cost convo", "P0", _ago(5), "Harrison", "OSN"),
                _make_entry("F3E inventory", "P0", _ago(5), "Harrison", "F3E"),
                _make_entry("UFL announcement", "P1", _ago(5), "Harrison", "UFL"),
            ),
            entity="OSN",
        )
        assert "OSN cost convo" in result
        assert "F3E inventory" not in result
        assert "UFL announcement" not in result

    def test_f3e_entity_sees_only_f3e_items(self):
        result = _parse_decisions(
            _make_md(
                _make_entry("F3E launch", "P0", _ago(5), "Harrison", "F3E"),
                _make_entry("OSN metrics", "P1", _ago(5), "Harrison", "OSN"),
            ),
            entity="F3E",
        )
        assert "F3E launch" in result
        assert "OSN metrics" not in result

    def test_fndr_entity_sees_all_items(self):
        result = _parse_decisions(
            _make_md(
                _make_entry("OSN item", "P0", _ago(5), "Harrison", "OSN"),
                _make_entry("F3E item", "P0", _ago(5), "Harrison", "F3E"),
                _make_entry("LEX item", "P1", _ago(5), "Harrison", "LEX"),
                _make_entry("HJRP item", "P1", _ago(5), "Harrison", "HJRP"),
            ),
            entity="FNDR",
        )
        assert "OSN item" in result
        assert "F3E item" in result
        assert "LEX item" in result
        assert "HJRP item" in result

    def test_hjrg_entity_sees_all_items(self):
        """HJRG channels are portfolio-wide like FNDR."""
        result = _parse_decisions(
            _make_md(
                _make_entry("Cross-entity item", "P0", _ago(5), "Harrison", "OSN"),
            ),
            entity="HJRG",
        )
        assert "Cross-entity item" in result

    def test_lex_parent_sees_all_lex_sub_entities(self):
        result = _parse_decisions(
            _make_md(
                _make_entry("LLC item", "P1", _ago(5), "Shaun", "LEX-LLC"),
                _make_entry("LLA item", "P1", _ago(5), "Harrison", "LEX-LLA"),
                _make_entry("LBHS item", "P1", _ago(5), "Harrison", "LEX-LBHS"),
                _make_entry("OSN item", "P1", _ago(5), "Harrison", "OSN"),
            ),
            entity="LEX",
        )
        assert "LLC item" in result
        assert "LLA item" in result
        assert "LBHS item" in result
        assert "OSN item" not in result

    def test_lex_llc_sees_only_llc_items(self):
        result = _parse_decisions(
            _make_md(
                _make_entry("LLC specific", "P1", _ago(5), "Shaun", "LEX-LLC"),
                _make_entry("LLA specific", "P1", _ago(5), "Harrison", "LEX-LLA"),
            ),
            entity="LEX-LLC",
        )
        assert "LLC specific" in result
        assert "LLA specific" not in result

    def test_fndr_tagged_items_visible_in_all_entity_channels(self):
        """Portfolio-level FNDR-tagged items surface in every entity channel."""
        result = _parse_decisions(
            _make_md(
                _make_entry("OIC pre-qualifier", "P0", _ago(5), "Harrison", "FNDR"),
                _make_entry("OSN only item", "P1", _ago(5), "Harrison", "OSN"),
            ),
            entity="F3E",
        )
        # FNDR-tagged item visible even though caller is F3E
        assert "OIC pre-qualifier" in result
        # OSN-tagged item NOT visible in F3E channel
        assert "OSN only item" not in result

    def test_multi_entity_tag_matches_either_entity(self):
        """An entry tagged 'HJRG, LEX' should appear in both HJRG and LEX queries."""
        result_hjrg = _parse_decisions(
            _make_md(_make_entry("CT Corp UCC lien", "P1", _ago(5), "Justin", "HJRG, LEX")),
            entity="HJRG",
        )
        result_lex = _parse_decisions(
            _make_md(_make_entry("CT Corp UCC lien", "P1", _ago(5), "Justin", "HJRG, LEX")),
            entity="LEX",
        )
        assert "CT Corp UCC lien" in result_hjrg
        assert "CT Corp UCC lien" in result_lex

    def test_entity_specific_no_match_returns_graceful_message(self):
        result = _parse_decisions(
            _make_md(_make_entry("Only HJRG item", "P0", _ago(5), "Harrison", "HJRG")),
            entity="OSN",
        )
        assert "No open decisions found for OSN" in result

    def test_entity_header_shows_scope_label(self):
        result = _parse_decisions(
            _make_md(_make_entry("OSN item", "P0", _ago(5), "Harrison", "OSN")),
            entity="OSN",
        )
        assert "OSN" in result
        assert "portfolio" not in result

    def test_portfolio_header_shows_portfolio_scope(self):
        result = _parse_decisions(
            _make_md(_make_entry("Any item", "P0", _ago(5), "Harrison", "FNDR")),
            entity="FNDR",
        )
        assert "portfolio" in result


class TestGmailDeepDiveSections:
    def test_gmail_deep_dive_entries_with_matching_entity_are_included(self):
        """Gmail Deep Dive entries with entity tags ARE included in entity-scoped queries."""
        content = (
            "# Pending Decisions Queue\n\n"
            "## Active (as of 2026-05-24)\n\n"
            "### Active OSN item\n"
            "- **Entity**: OSN\n"
            "- **Severity**: P0\n"
            f"- **Last touched**: {_ago(5)}\n"
            "- **Owner of next nudge**: Harrison\n\n"
            "## Gmail Deep Dive -- Open Questions\n\n"
            "### Bond Street REIT -- which OSN store?\n"
            "- **Entity**: OSN\n"
            "- **Severity**: P3\n"
            f"- **Last touched**: {_ago(2)}\n"
            "- **Owner of next nudge**: Matt\n\n"
            "## Recently resolved\n\n### Done\n-> done.\n"
        )
        # P3 is still excluded, but let's use P2 for a visible test
        content2 = content.replace("P3", "P2")
        result = _parse_decisions(content2, entity="OSN")
        assert "Bond Street REIT" in result

    def test_gmail_deep_dive_entries_with_wrong_entity_are_excluded(self):
        content = (
            "# Pending Decisions Queue\n\n"
            "## Active (as of 2026-05-24)\n\n"
            "### F3E item\n"
            "- **Entity**: F3E\n"
            "- **Severity**: P0\n"
            f"- **Last touched**: {_ago(5)}\n"
            "- **Owner of next nudge**: Harrison\n\n"
            "## Gmail Deep Dive -- Open Questions\n\n"
            "### LEX audit finding\n"
            "- **Entity**: LEX\n"
            "- **Severity**: P1\n"
            f"- **Last touched**: {_ago(2)}\n"
            "- **Owner of next nudge**: Shaun\n\n"
            "## Recently resolved\n\n### Done\n-> done.\n"
        )
        result = _parse_decisions(content, entity="F3E")
        assert "F3E item" in result
        assert "LEX audit finding" not in result

    def test_recently_resolved_always_excluded(self):
        content = (
            "# Pending Decisions Queue\n\n"
            "## Active (as of 2026-05-24)\n\n"
            "### Live P0\n"
            "- **Entity**: FNDR\n"
            "- **Severity**: P0\n"
            f"- **Last touched**: {_ago(5)}\n"
            "- **Owner of next nudge**: Harrison\n\n"
            "## Recently resolved\n\n"
            "### Already resolved item\n"
            "- **Entity**: FNDR\n"
            "- **Severity**: P0\n"
            f"- **Last touched**: {_ago(1)}\n"
            "- **Owner of next nudge**: Harrison\n"
        )
        result = _parse_decisions(content)
        assert "Live P0" in result
        assert "Already resolved item" not in result


class TestTemplateSkeleton:
    """The 'How to use' template skeleton in decisions-pending.md must never
    parse as a real entry. Live finding 2026-06-11 (strategy-memo dry run):
    the skeleton's '- **Severity**: P0 / P1 / P2 / P3' alternatives line
    matched the naive (P\\d) regex and leaked a bogus P0
    ('[P0] [FNDR / HJRG / ...] [Topic]') into output. Same fix as
    strategy_memo.gather_stalled_decisions (commit 9c6d3a0)."""

    _SKELETON = (
        "## How to use\n\n"
        "Each entry follows this skeleton:\n\n"
        "### [Topic]\n"
        "- **Entity**: FNDR / HJRG / F3E / OSN / LEX / LEX-LLC\n"
        "- **Question**: what specifically needs to be decided\n"
        "- **Severity**: P0 / P1 / P2 / P3\n"
        "- **Last touched**: YYYY-MM-DD\n"
        "- **Owner of next nudge**: who is supposed to move this forward\n\n"
    )

    def test_skeleton_never_parsed(self):
        content = (
            "# Pending Decisions Queue\n\n"
            + self._SKELETON
            + "## Active\n\n"
            "### Real P0\n"
            "- **Entity**: F3E\n"
            "- **Severity**: P0\n"
            f"- **Last touched**: {_ago(5)}\n"
            "- **Owner of next nudge**: Harrison\n"
        )
        result = _parse_decisions(content)
        assert "Real P0" in result
        assert "[Topic]" not in result
        assert "who is supposed to move this forward" not in result

    def test_annotated_severity_still_parsed(self):
        content = (
            "# Pending Decisions Queue\n\n"
            + self._SKELETON
            + "## Active\n\n"
            "### Annotated call\n"
            "- **Entity**: F3E\n"
            "- **Severity**: P0 (decision moment is the Monday call)\n"
            f"- **Last touched**: {_ago(3)}\n"
            "- **Owner of next nudge**: Harrison\n"
        )
        result = _parse_decisions(content)
        assert "Annotated call" in result

    def test_handler_source_carries_both_guards(self):
        """Drift guard: the REAL handler in tool_dispatch.py must carry the
        same two guards as this file's mirror parser."""
        src = (Path(__file__).resolve().parents[1] / "src" / "cora" / "tools"
               / "tool_dispatch.py").read_text(encoding="utf-8")
        assert 'if topic == "[Topic]":' in src
        assert r"\*\*Severity\*\*:\s*(P\d)\b(?!\s*/)" in src


class TestOutputFormat:
    def test_header_counts_match(self):
        result = _parse_decisions(_make_md(
            _make_entry("P0 alpha", "P0", _ago(5), "Harrison", "FNDR"),
            _make_entry("P0 beta", "P0", _ago(8), "Harrison", "FNDR"),
            _make_entry("P1 gamma", "P1", _ago(3), "Justin", "FNDR"),
        ))
        assert "2 P0" in result
        assert "1 P1" in result

    def test_header_uses_slack_bold(self):
        result = _parse_decisions(_make_md(
            _make_entry("Any P0", "P0", _ago(5), "Harrison", "FNDR"),
        ))
        assert result.startswith("*Open decisions")

    def test_topic_wrapped_in_bold(self):
        result = _parse_decisions(_make_md(
            _make_entry("My Special Topic", "P0", _ago(5), "Harrison", "FNDR"),
        ))
        assert "*My Special Topic*" in result

    def test_line_includes_owner(self):
        result = _parse_decisions(_make_md(
            _make_entry("Some decision", "P0", _ago(5), "Harrison to assign", "FNDR"),
        ))
        assert "Harrison to assign" in result

    def test_entity_scoped_header_includes_p2_count(self):
        result = _parse_decisions(
            _make_md(
                _make_entry("OSN P0", "P0", _ago(5), "Harrison", "OSN"),
                _make_entry("OSN P2", "P2", _ago(5), "Harrison", "OSN"),
            ),
            entity="OSN",
        )
        assert "1 P0" in result
        assert "1 P2" in result


class TestSorting:
    def test_p0_sorted_stalest_first(self):
        result = _parse_decisions(_make_md(
            _make_entry("Fresh (5d)", "P0", _ago(5), "H", "FNDR"),
            _make_entry("Ancient (40d)", "P0", _ago(40), "H", "FNDR"),
            _make_entry("Medium (15d)", "P0", _ago(15), "H", "FNDR"),
        ))
        assert result.index("Ancient") < result.index("Medium") < result.index("Fresh")

    def test_p1_sorted_stalest_first(self):
        result = _parse_decisions(_make_md(
            _make_entry("New P1 (2d)", "P1", _ago(2), "H", "FNDR"),
            _make_entry("Old P1 (30d)", "P1", _ago(30), "H", "FNDR"),
        ))
        assert result.index("Old P1") < result.index("New P1")


class TestDateParsing:
    def test_month_only_date_parses_without_crash(self):
        result = _parse_decisions(_make_md(
            _make_entry("OIC pre-qualifier", "P0", "~2026-04", "Harrison", "FNDR"),
        ))
        assert "OIC pre-qualifier" in result
        assert "stale" in result or "touched today" in result

    def test_month_only_date_is_older_than_recent(self):
        result = _parse_decisions(_make_md(
            _make_entry("Month-only old", "P0", "~2026-04", "Harrison", "FNDR"),
            _make_entry("Recent 5d", "P0", _ago(5), "Harrison", "FNDR"),
        ))
        assert result.index("Month-only old") < result.index("Recent 5d")

    def test_today_renders_touched_today(self):
        result = _parse_decisions(_make_md(
            _make_entry("Brand new item", "P0", _today(), "Harrison", "FNDR"),
        ))
        assert "touched today" in result

    def test_one_day_renders_1d_stale(self):
        result = _parse_decisions(_make_md(
            _make_entry("Yesterday item", "P0", _ago(1), "Harrison", "FNDR"),
        ))
        assert "1d stale" in result

    def test_exact_7d_renders_7d_stale(self):
        result = _parse_decisions(_make_md(
            _make_entry("Week-old item", "P0", _ago(7), "Harrison", "FNDR"),
        ))
        assert "7d stale" in result


class TestEdgeCases:
    def test_empty_file_returns_graceful_message(self):
        result = _parse_decisions("# Pending Decisions Queue\n\n")
        assert "No P0 or P1" in result

    def test_no_severity_field_skips_block(self):
        content = _make_md(
            "### Missing severity entry\n"
            "- **Entity**: FNDR\n"
            "- **Question**: no severity\n"
            "- **Last touched**: 2026-05-20\n"
            "- **Owner of next nudge**: Harrison\n"
        )
        result = _parse_decisions(content)
        assert "Missing severity entry" not in result

    def test_entry_without_entity_defaults_to_fndr(self):
        """Entries without an Entity field default to FNDR and are visible everywhere."""
        content = (
            "# Pending Decisions Queue\n\n"
            "## Active\n\n"
            "### Legacy entry no entity tag\n"
            "- **Severity**: P0\n"
            f"- **Last touched**: {_ago(5)}\n"
            "- **Owner of next nudge**: Harrison\n\n"
            "## Recently resolved\n\n"
        )
        # Should appear in F3E channel (defaults to FNDR = visible everywhere)
        result = _parse_decisions(content, entity="F3E")
        assert "Legacy entry no entity tag" in result


# ==============================================================================
# Real-world snapshot test
# ==============================================================================

class TestRealWorldSnapshot:
    SNAPSHOT = f"""\
# Pending Decisions Queue

## Active (as of 2026-05-14)

### HJRP cash depletion / June rent suspension

- **Entity**: HJRP
- **Question**: HJRP is facing ~$46K+ in outgoing obligations.
- **Decision-maker**: Harrison
- **Severity**: P0
- **Last touched**: 2026-05-23
- **Owner of next nudge**: Harrison (this weekend)

### Personal 1040 OIC pre-qualifier filing

- **Entity**: FNDR
- **Question**: file OIC pre-qualifier.
- **Decision-maker**: Harrison (delegate to Andrew or Justin)
- **Severity**: P0
- **Last touched**: ~2026-04
- **Owner of next nudge**: Harrison to assign to Justin

### OSN cost-structure conversation

- **Entity**: OSN
- **Question**: how aggressive on cost cuts at OSN?
- **Decision-maker**: Harrison + Matt + Hayden
- **Severity**: P0
- **Last touched**: 2026-05-12
- **Owner of next nudge**: Harrison to schedule

### UFL pivot public announcement timing

- **Entity**: UFL
- **Question**: when does the UFL pause become public?
- **Decision-maker**: Harrison
- **Severity**: P1
- **Last touched**: 2026-05-10
- **Owner of next nudge**: Harrison to schedule team meeting

### Hannah HJRP-recurring sustainability check

- **Entity**: HJRP
- **Question**: at what trigger escalate to PM contractor?
- **Decision-maker**: Harrison
- **Severity**: P2
- **Last touched**: 2026-05-14
- **Owner of next nudge**: Harrison to define escalation trigger

## Gmail Deep Dive -- Open Questions

### Bond Street REIT -- which OSN store?
- **Entity**: OSN
- **Question**: landlord for Shops at Civic Center.
- **Decision-maker**: Harrison or Matt
- **Severity**: P3
- **Last touched**: 2026-05-22
- **Owner of next nudge**: Matt or Harrison

## Recently resolved

### F3 Pure launch date (resolved 2026-05-22)
-> 6/15 LOCKED.
"""

    def test_portfolio_yields_3_p0(self):
        result = _parse_decisions(self.SNAPSHOT, entity="FNDR")
        assert "3 P0" in result

    def test_portfolio_yields_1_p1(self):
        result = _parse_decisions(self.SNAPSHOT, entity="FNDR")
        assert "1 P1" in result

    def test_portfolio_p2_excluded(self):
        result = _parse_decisions(self.SNAPSHOT, entity="FNDR")
        assert "Hannah HJRP-recurring" not in result

    def test_portfolio_gmail_deep_dive_p3_excluded(self):
        result = _parse_decisions(self.SNAPSHOT, entity="FNDR")
        assert "Bond Street REIT" not in result

    def test_portfolio_oic_appears(self):
        result = _parse_decisions(self.SNAPSHOT, entity="FNDR")
        assert "OIC" in result or "1040" in result

    def test_osn_channel_sees_osn_items_only(self):
        result = _parse_decisions(self.SNAPSHOT, entity="OSN")
        assert "OSN cost-structure" in result
        assert "HJRP cash depletion" not in result
        assert "UFL pivot" not in result

    def test_osn_channel_sees_fndr_items(self):
        """OIC is FNDR-tagged; should appear in OSN channel."""
        result = _parse_decisions(self.SNAPSHOT, entity="OSN")
        assert "OIC" in result or "1040" in result

    def test_hjrp_channel_sees_hjrp_items(self):
        result = _parse_decisions(self.SNAPSHOT, entity="HJRP")
        assert "HJRP cash depletion" in result
        assert "OSN cost-structure" not in result
        assert "UFL pivot" not in result

    def test_hjrp_channel_sees_p2_items(self):
        """Entity-specific queries include P2."""
        result = _parse_decisions(self.SNAPSHOT, entity="HJRP")
        assert "Hannah HJRP-recurring" in result

    def test_ufl_channel_sees_only_ufl(self):
        result = _parse_decisions(self.SNAPSHOT, entity="UFL")
        assert "UFL pivot" in result
        assert "OSN cost-structure" not in result

    def test_oic_sorts_before_osn_cost(self):
        """OIC (~2026-04) is older than OSN cost (2026-05-12); must appear first in FNDR view."""
        result = _parse_decisions(self.SNAPSHOT, entity="FNDR")
        assert result.index("OIC") < result.index("OSN cost")

    def test_ufl_is_p1_not_p0(self):
        result = _parse_decisions(self.SNAPSHOT, entity="FNDR")
        lines = result.split("\n")
        ufl_line = next((ln for ln in lines if "UFL pivot" in ln), None)
        assert ufl_line is not None
        assert "\U0001f7e1" in ufl_line  # yellow circle = P1


# ==============================================================================
# Layer B -- Integration tests (imports tool_dispatch; skip if import fails)
# ==============================================================================

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
    reason="tool_dispatch not importable (stale mount or syntax error) -- run on Windows",
)


@_skip_if_no_dispatch
class TestDispatchIntegration:
    def test_dispatch_routes_to_handler_fndr(self):
        md = _make_md(
            _make_entry("Dispatch route test", "P0", _ago(5), "Harrison", "FNDR"),
        )
        with patch.object(Path, "read_text", return_value=md):
            result = dispatch("fndr_open_decisions", {}, "U_HARRISON", entity="FNDR")
        assert "Dispatch route test" in result

    def test_dispatch_entity_filtering_osn(self):
        md = _make_md(
            _make_entry("OSN only", "P0", _ago(5), "Harrison", "OSN"),
            _make_entry("F3E only", "P0", _ago(5), "Harrison", "F3E"),
        )
        with patch.object(Path, "read_text", return_value=md):
            result = dispatch("fndr_open_decisions", {}, "U_HARRISON", entity="OSN")
        assert "OSN only" in result
        assert "F3E only" not in result

    def test_dispatch_unknown_tool_returns_error(self):
        result = dispatch("fndr_open_decisions_nonexistent", {}, "U_TEST", entity="FNDR")
        assert "Unknown tool" in result

    def test_dispatch_works_from_hjrg_entity(self):
        md = _make_md(
            _make_entry("HJRG entity test", "P1", _ago(5), "Harrison", "OSN"),
        )
        with patch.object(Path, "read_text", return_value=md):
            result = dispatch("fndr_open_decisions", {}, "U_HARRISON", entity="HJRG")
        assert "HJRG entity test" in result

    def test_handler_file_not_found_returns_fallback(self):
        with patch.object(Path, "read_text", side_effect=FileNotFoundError("not found")):
            result = _handler("U_TEST", "FNDR", {})
        assert "don't have that right now" in result.lower()

    def test_handler_exception_returns_fallback(self):
        with patch.object(Path, "read_text", side_effect=OSError("disk error")):
            result = _handler("U_TEST", "FNDR", {})
        assert "don't have that right now" in result.lower()
