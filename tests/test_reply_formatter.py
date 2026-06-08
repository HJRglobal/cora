"""Tests for src/cora/reply_formatter.py — the conversational reply post-processor.

Covers the voice/style contract enforcement (fndr.md) + source-opacity lint, plus
the is_tool_output bypass.
"""

import logging
import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.reply_formatter import (  # noqa: E402
    CONVERSATIONAL_CHAR_CAP,
    format_reply,
)


# --- markdown stripping --------------------------------------------------


class TestMarkdownStripping:
    def test_double_star_bold_flattened(self):
        assert format_reply("The status is **open** today") == "The status is open today"

    def test_double_underscore_bold_flattened(self):
        assert format_reply("It is __urgent__ now") == "It is urgent now"

    def test_header_removed(self):
        out = format_reply("# Summary\nCash is fine")
        assert "#" not in out
        assert "Summary" in out
        assert "Cash is fine" in out

    def test_subheader_removed(self):
        out = format_reply("### Details here\nbody")
        assert not out.startswith("#")
        assert "Details here" in out

    def test_horizontal_rule_removed(self):
        out = format_reply("Above\n---\nBelow")
        assert "---" not in out
        assert "Above" in out and "Below" in out

    def test_asterisk_rule_removed(self):
        out = format_reply("Above\n***\nBelow")
        assert "***" not in out

    def test_table_flattened_to_prose(self):
        table = "| Name | Status |\n| --- | --- |\n| F3E | open |"
        out = format_reply(table)
        assert "|" not in out
        assert "F3E" in out and "open" in out

    def test_table_separator_row_dropped(self):
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        out = format_reply(table)
        assert "---" not in out

    def test_single_star_slack_bold_preserved(self):
        # Slack label-before-value bold (*Status:*) is allowed by the contract.
        out = format_reply("*Status:* open")
        assert "*Status:*" in out


# --- dashes --------------------------------------------------------------


class TestDashes:
    def test_em_dash_replaced(self):
        out = format_reply("F3E is fine — OSN is not")
        assert "—" not in out
        assert "-" in out

    def test_en_dash_replaced(self):
        out = format_reply("Q1–Q2 results")
        assert "–" not in out

    def test_no_em_dash_anywhere(self):
        out = format_reply("one — two — three")
        assert "—" not in out and "–" not in out


# --- emoji + shortcodes --------------------------------------------------


class TestEmojiStripping:
    def test_siren_emoji_stripped(self):
        out = format_reply("🚨 Deadline is today")
        assert "🚨" not in out
        assert "Deadline is today" in out

    def test_check_emoji_stripped(self):
        out = format_reply("Done ✅")
        assert "✅" not in out

    def test_colored_circle_emoji_stripped(self):
        out = format_reply("🔴 over budget 🟡 watch 🟢 ok")
        for e in ("🔴", "🟡", "🟢"):
            assert e not in out

    def test_shortcode_stripped(self):
        out = format_reply("Nice work :tada: team")
        assert ":tada:" not in out
        assert "Nice work" in out and "team" in out

    def test_timestamp_not_treated_as_shortcode(self):
        # 12:30:45 must survive — shortcodes must start with a letter.
        out = format_reply("Meeting at 12:30:45 today")
        assert "12:30:45" in out

    def test_arrow_ascii_preserved(self):
        out = format_reply("Identify -> Proposal")
        assert "->" in out


# --- source-opacity lint -------------------------------------------------


class TestSourceOpacity:
    def test_bare_google_docs_url_redacted(self):
        out = format_reply("See https://docs.google.com/spreadsheets/d/abc123/edit for details")
        assert "docs.google.com" not in out
        assert "See" in out and "details" in out

    def test_bare_drive_url_redacted(self):
        out = format_reply("File at https://drive.google.com/file/d/xyz/view")
        assert "drive.google.com" not in out

    def test_bare_asana_url_redacted(self):
        out = format_reply("Task https://app.asana.com/0/123/456 is open")
        assert "app.asana.com" not in out

    def test_bare_notion_url_redacted(self):
        out = format_reply("Page https://www.notion.so/abc-def is updated")
        assert "notion.so" not in out

    def test_sanctioned_link_preserved(self):
        # The <url|label> task link is sanctioned and must survive.
        text = "The deal is <https://app.asana.com/0/1/2|American Discount Foods>"
        out = format_reply(text)
        assert "<https://app.asana.com/0/1/2|American Discount Foods>" in out

    def test_slack_mention_preserved(self):
        out = format_reply("Flag this to <@U0B2RM2JYJ1> please")
        assert "<@U0B2RM2JYJ1>" in out

    def test_gid_redacted(self):
        out = format_reply("Created gid 1215472268404903 for you")
        assert "1215472268404903" not in out
        assert "gid 1215472268404903" not in out

    def test_naked_long_id_redacted(self):
        out = format_reply("Reference 1204525779609669 in the system")
        assert "1204525779609669" not in out

    def test_short_numbers_preserved(self):
        # Normal numbers (counts, money, years) must NOT be redacted.
        out = format_reply("We have 42 deals worth 399740 closing in 2026")
        assert "42" in out and "399740" in out and "2026" in out


# --- tool-output bypass --------------------------------------------------


class TestToolOutputBypass:
    def test_bypass_preserves_everything(self):
        raw = (
            "*Press Pipeline — 12 contacts:*\n"
            "🔴 F3 Energy: 0/3 published\n"
            "See https://docs.google.com/x — gid 1215472268404903"
        )
        assert format_reply(raw, is_tool_output=True) == raw

    def test_bypass_preserves_emoji_and_em_dash(self):
        raw = "✅ done — 🚨 alert"
        assert format_reply(raw, is_tool_output=True) == raw

    def test_bypass_preserves_table(self):
        raw = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        assert format_reply(raw, is_tool_output=True) == raw


# --- 280-char cap --------------------------------------------------------


class TestCharCap:
    def test_cap_constant(self):
        assert CONVERSATIONAL_CHAR_CAP == 280

    def test_over_cap_not_truncated(self):
        long = "word " * 100  # 500 chars
        out = format_reply(long)
        # Not hard-truncated -- the full (cleaned) answer is returned.
        assert len(out) > CONVERSATIONAL_CHAR_CAP

    def test_over_cap_logs_warning(self, caplog):
        long = "x" * 400
        with caplog.at_level(logging.WARNING):
            format_reply(long)
        assert any("reply_over_cap" in r.message for r in caplog.records)

    def test_under_cap_no_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            format_reply("short answer")
        assert not any("reply_over_cap" in r.message for r in caplog.records)


# --- edge cases ----------------------------------------------------------


class TestEdgeCases:
    def test_empty_string(self):
        assert format_reply("") == ""

    def test_none_passthrough(self):
        assert format_reply(None) is None

    def test_plain_text_unchanged(self):
        plain = "OSN cash is 77629 as of Monday."
        assert format_reply(plain) == plain

    def test_combined_kitchen_sink(self):
        text = (
            "## Update\n"
            "Status is **open** — see https://docs.google.com/x 🚨\n"
            "gid 1215472268404903 :tada:"
        )
        out = format_reply(text)
        for bad in ("##", "**", "—", "🚨", ":tada:", "docs.google.com", "1215472268404903"):
            assert bad not in out
        assert "Status is open" in out
