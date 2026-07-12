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
    redact_links_and_ids,
)


class TestRedactLinksAndIds:
    """The SAFETY subset used by the egress boundary -- redact URLs/IDs WITHOUT
    flattening structure."""

    def test_redacts_bare_drive_url(self):
        out = redact_links_and_ids("Filed to https://drive.google.com/file/d/abc today")
        assert "drive.google.com" not in out

    def test_preserves_sanctioned_link(self):
        msg = "See <https://drive.google.com/file/d/abc|the doc>"
        assert redact_links_and_ids(msg) == msg

    def test_redacts_gid_and_long_id(self):
        out = redact_links_and_ids("gid 1204525779609669 and 9876543210987654")
        assert "1204525779609669" not in out and "9876543210987654" not in out

    def test_does_not_flatten_structure(self):
        table = "```\nA    B\n1    2\n```"
        assert redact_links_and_ids(table) == table  # fences + alignment preserved

    def test_does_not_strip_emoji_or_emdash(self):
        msg = "Sync failed — retry 🔴"
        out = redact_links_and_ids(msg)
        assert "—" in out and "🔴" in out

    def test_redacts_bare_intuit_qbo_url(self):
        # B2 defense-in-depth: a fabricated bare QBO report link is redacted.
        out = redact_links_and_ids("Open it at qbo.intuit.com/app/profitandloss?reset=1 today")
        assert "intuit.com" not in out
        assert "Open it at" in out and "today" in out

    def test_preserves_sanctioned_intuit_link(self):
        msg = "See <https://qbo.intuit.com/app/profitandloss|the report>"
        assert redact_links_and_ids(msg) == msg

    def test_intuit_pattern_no_redos(self):
        import time
        start = time.perf_counter()
        redact_links_and_ids("a" * 5000 + " no-intuit-here")
        assert time.perf_counter() - start < 1.0


class TestSheetNameRedaction:
    """format_reply replaces named sheet identifiers with a neutral phrase
    (conversational source-opacity) -- grammatically, not by deletion."""

    def test_standing_actuals_sheet_replaced(self):
        out = format_reply("See the Standing ACTUALS sheet for details.")
        assert "Standing ACTUALS" not in out
        assert "the cash flow model" in out
        assert "for details." in out  # sentence stays grammatical

    def test_cf_summary_replaced(self):
        out = format_reply("Numbers live in the CF_SUMMARY tab.")
        assert "CF_SUMMARY" not in out
        assert "the cash flow model" in out

    def test_cf_summary_no_double_article(self):
        # S3.4 (2026-07-12): the determiner was swallowed only on the Standing
        # ACTUALS alternative, so "the CF_SUMMARY sheet" -> "the the cash flow
        # model". The (?:the\s+)? now spans the whole alternation.
        out = format_reply("It lives at the CF_SUMMARY sheet.")
        assert "the the" not in out.lower()
        assert "the cash flow model" in out
        # And the CF SUMMARY (space, not underscore) tab variant.
        out2 = format_reply("Check the CF SUMMARY tab.")
        assert "the the" not in out2.lower()
        assert "the cash flow model" in out2

    def test_standing_actuals_no_double_article(self):
        # The pre-existing alternative kept working after the restructure.
        out = format_reply("Pull the Standing ACTUALS sheet.")
        assert "the the" not in out.lower()
        assert "the cash flow model" in out

    def test_sheet_name_with_interior_bold_still_redacted(self):
        # Step 2 converts **ACTUALS** -> *ACTUALS*; the inserted '*' must NOT let the
        # sheet identifier slip past the (no-egress-backstop) conversational lint.
        # Regression guard for the 2026-06-30 convert-vs-strip change.
        out = format_reply("See the Standing **ACTUALS** sheet for details.")
        assert "Standing ACTUALS" not in out
        assert "Standing *ACTUALS*" not in out
        assert "the cash flow model" in out

    def test_cf_summary_with_interior_bold_still_redacted(self):
        out = format_reply("Numbers live in the CF_**SUMMARY** tab.")
        assert "CF_SUMMARY" not in out
        assert "CF_*SUMMARY*" not in out
        assert "the cash flow model" in out


class TestDrivePathRedaction:
    """format_reply redacts HJR-Founder-OS Drive document PATHS (B0, green-lit
    2026-06-17) to a neutral phrase -- conversational source-opacity. Must NOT
    match ordinary prose: the slash-segments + doc extension are the prose guard."""

    # --- positives: a named Drive document path must be redacted -------------
    def test_live_leak_xlsx_path_redacted(self):
        out = format_reply(
            "The figures live in 02-F3-Energy/production/f3-production-master-register.xlsx today.")
        assert "02-F3-Energy" not in out
        assert ".xlsx" not in out
        assert "a portfolio document" in out
        assert "today." in out  # sentence stays grammatical

    def test_shared_pdf_path_redacted(self):
        out = format_reply("It is in _shared/projects/cora/design/spec.pdf right now.")
        assert "_shared/projects" not in out
        assert "a portfolio document" in out

    def test_hjrg_gsheet_path_redacted(self):
        out = format_reply(
            "Pulled from 01-HJR-Global/accounting/live-sheets/hjrg_weekly-cash-flow_LIVE.gsheet.")
        assert "01-HJR-Global" not in out
        assert ".gsheet" not in out
        assert "a portfolio document" in out

    def test_strategy_memo_docx_path_redacted(self):
        out = format_reply("See 00-Founder/_strategy-memos/2026-06/memo.docx for the writeup.")
        assert "00-Founder/_strategy-memos" not in out
        assert "a portfolio document" in out

    def test_pptx_and_csv_extensions_redacted(self):
        out = format_reply("Deck 07-Big-D-Media/decks/q3.pptx and 09-One-Stop-Nutrition/data/sales.csv.")
        assert ".pptx" not in out
        assert ".csv" not in out
        assert out.count("a portfolio document") == 2

    def test_drive_path_with_bold_extension_still_redacted(self):
        # **xlsx** -> *xlsx* splits the \\.ext anchor of _DRIVE_PATH_RE; the
        # de-emphasized fallback must still redact the path (no egress backstop).
        out = format_reply(
            "Figures live in 02-F3-Energy/production/register.**xlsx** today.")
        assert "02-F3-Energy" not in out
        assert ".xlsx" not in out
        assert ".*xlsx*" not in out
        assert "a portfolio document" in out

    def test_ordinary_bolded_reply_keeps_emphasis(self):
        # The de-emphasized fallback must NOT fire on a normal bolded reply that
        # contains no sheet/path identifier -- formatting is preserved.
        out = format_reply("The **deck** is ready and the **budget** is approved.")
        assert "*deck*" in out and "*budget*" in out

    # --- negatives: ordinary prose must survive verbatim ---------------------
    def test_bare_word_production_survives(self):
        out = format_reply("We are ramping production this quarter.")
        assert "ramping production this quarter." in out
        assert "a portfolio document" not in out

    def test_shared_drive_phrase_survives(self):
        out = format_reply("It is in the _shared drive somewhere.")
        assert "_shared drive" in out
        assert "a portfolio document" not in out

    def test_pdf_word_without_path_survives(self):
        out = format_reply("I attached a pdf of the deck.")
        assert "a pdf of the deck." in out
        assert "a portfolio document" not in out

    def test_md_path_not_redacted(self):
        # Doc-only by design: a bare .md path is left alone (avoids "README.md" prose).
        out = format_reply("It is logged in memory/decisions.md for the record.")
        assert "memory/decisions.md" in out
        assert "a portfolio document" not in out

    def test_two_digit_building_number_survives(self):
        out = format_reply("The 02 building has 11 tenants.")
        assert "The 02 building has 11 tenants." in out
        assert "a portfolio document" not in out

    def test_sanctioned_link_with_path_label_survives(self):
        # A sanctioned <url|label> is token-protected; its internals are untouched.
        raw = "See <https://drive.google.com/file/d/abc|02-F3-Energy/x.pdf>."
        out = format_reply(raw)
        assert "<https://drive.google.com/file/d/abc|02-F3-Energy/x.pdf>" in out

    def test_tool_output_bypass_leaves_path(self):
        raw = "Stored at 02-F3-Energy/production/register.xlsx"
        assert format_reply(raw, is_tool_output=True) == raw

    def test_no_catastrophic_backtracking(self):
        # ReDoS guard (adversarial review HIGH): a long path-shaped reply with no
        # trailing doc extension must NOT explode the regex. The non-slash segment
        # class makes matching linear; the old (?:/[^space]+)+ form took ~40s at 30
        # segments. 200 segments must complete near-instantly.
        import time
        evil = "Here is the file: 02-F3-Energy/" + "/".join(["a"] * 200) + "/final-version"
        start = time.perf_counter()
        out = format_reply(evil)
        assert time.perf_counter() - start < 2.0  # linear; old exponential = minutes
        assert "a portfolio document" not in out  # no doc extension -> not a match

    def test_generic_outputs_path_survives(self):
        # Roots limited to NN-Entity + _shared -> a generic build/log path is left alone.
        out = format_reply("Build wrote outputs/dist/report.csv last run.")
        assert "outputs/dist/report.csv" in out
        assert "a portfolio document" not in out

    def test_generic_memory_path_survives(self):
        out = format_reply("Cache is in memory/objects/blob.pdf on disk.")
        assert "memory/objects/blob.pdf" in out
        assert "a portfolio document" not in out


# --- markdown stripping --------------------------------------------------


class TestMarkdownStripping:
    def test_double_star_bold_to_slack(self):
        # ** ** -> Slack single-asterisk bold, NOT stripped (Slack renders *x*).
        assert format_reply("The status is **open** today") == "The status is *open* today"

    def test_double_underscore_bold_to_slack(self):
        assert format_reply("It is __urgent__ now") == "It is *urgent* now"

    def test_header_to_bold_label(self):
        out = format_reply("# Summary\nCash is fine")
        assert "#" not in out
        assert "*Summary*" in out
        assert "Cash is fine" in out

    def test_subheader_to_bold_label(self):
        out = format_reply("### Details here\nbody")
        assert not out.startswith("#")
        assert "*Details here*" in out

    def test_header_with_interior_bold_no_dangling_asterisk(self):
        # '## **Key** takeaways' -> step 2 -> '## *Key* takeaways' -> the header
        # conversion must yield a balanced single label, not '*Key* takeaways*'.
        out = format_reply("## **Key** takeaways\nbody")
        assert "*Key takeaways*" in out
        assert "takeaways*" not in out.replace("*Key takeaways*", "")  # no dangling *
        assert out.count("*") == 2  # exactly one balanced bold span on the label

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


class TestEmojiAllowlist:
    def test_nonallowlisted_siren_stripped(self):
        out = format_reply("🚨 Deadline is today")
        assert "🚨" not in out
        assert "Deadline is today" in out

    def test_check_emoji_survives(self):
        # ✅ is a functional allowlisted marker -> kept.
        assert "✅" in format_reply("Done ✅")

    def test_warning_emoji_survives(self):
        assert "⚠" in format_reply("⚠️ over the cap")

    def test_status_circles_survive(self):
        out = format_reply("🔴 over budget 🟡 watch 🟢 ok")
        for e in ("🔴", "🟡", "🟢"):
            assert e in out

    def test_pushpin_survives(self):
        assert "📌" in format_reply("📌 deadline 6/30")

    def test_decorative_emoji_stripped(self):
        out = format_reply("great work 🎉 keep it 🔥 up 💪")
        for e in ("🎉", "🔥", "💪"):
            assert e not in out
        assert "great work" in out and "keep it" in out

    def test_mixed_run_keeps_allowed_drops_decorative(self):
        # ✅ (allowed) adjacent to 🎉 (decorative): keep ✅, drop 🎉.
        assert format_reply("shipped ✅🎉") == "shipped ✅"

    def test_allowed_shortcode_survives(self):
        assert ":white_check_mark:" in format_reply("done :white_check_mark:")

    def test_decorative_shortcode_stripped(self):
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


# --- ~900-char soft cap --------------------------------------------------


class TestCharCap:
    def test_cap_constant(self):
        assert CONVERSATIONAL_CHAR_CAP == 900

    def test_over_cap_not_truncated(self):
        long = "word " * 250  # ~1250 chars
        out = format_reply(long)
        # Not hard-truncated -- the full (cleaned) answer is returned.
        assert len(out) > CONVERSATIONAL_CHAR_CAP

    def test_over_cap_logs_warning(self, caplog):
        long = "x" * 1000
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
        assert "Status is *open*" in out


# --- redaction shells (2026-06-11 live artifact) ---------------------------


class TestRedactionShells:
    def test_url_in_parens_leaves_no_empty_shell(self):
        out = format_reply("The doc (https://docs.google.com/spreadsheets/d/abc) covers it.")
        assert "()" not in out
        assert "docs.google.com" not in out
        assert "The doc covers it." in out

    def test_markdown_link_keeps_label(self):
        out = format_reply("See [Q3 forecast](https://docs.google.com/spreadsheets/d/abc) for detail.")
        assert "docs.google.com" not in out
        assert "[" not in out and "()" not in out
        assert "Q3 forecast" in out

    def test_asana_url_in_parens(self):
        out = format_reply("Task created (https://app.asana.com/0/123/456).")
        assert "()" not in out
        assert "app.asana.com" not in out

    def test_legit_parenthetical_preserved(self):
        out = format_reply("Revenue grew (per the weekly review) by a lot.")
        assert "(per the weekly review)" in out

    def test_sanctioned_slack_link_untouched(self):
        out = format_reply("Open <https://app.asana.com/0/1/2|the task> when ready.")
        assert "<https://app.asana.com/0/1/2|the task>" in out


# --- lists + code (B4, 2026-06-13) ---------------------------------------


class TestListsAndCode:
    def test_bullet_dash_to_slack_bullet(self):
        out = format_reply("- send the deck\n- ping Tommy")
        assert "• send the deck" in out and "• ping Tommy" in out
        # old markdown markers are gone (converted, not left as -, *, +)
        assert not any(ln.lstrip().startswith(("- ", "* ", "+ ")) for ln in out.split("\n"))

    def test_bullet_star_to_slack_bullet(self):
        out = format_reply("* one\n* two")
        assert "• one" in out and "• two" in out
        assert "* one" not in out

    def test_numbered_list_kept(self):
        # Numbered lists render natively in Slack -> keep them intact.
        out = format_reply("1. first thing\n2. second thing")
        assert "1. first thing" in out and "2. second thing" in out

    def test_numbered_paren_kept(self):
        out = format_reply("1) alpha\n2) beta")
        assert "1) alpha" in out and "2) beta" in out

    def test_inline_code_unwrapped(self):
        out = format_reply("Run the `restagger` script now")
        assert "restagger" in out
        assert "`" not in out

    def test_code_fence_flattened(self):
        out = format_reply("Do this:\n```bash\nls -la\n```\nthen stop")
        assert "```" not in out
        assert "ls -la" in out

    def test_inline_code_gid_still_redacted(self):
        # Source-opacity must still win after the backtick unwrap.
        out = format_reply("the task is `gid 1215472268404903`")
        assert "1215472268404903" not in out

    def test_hyphenated_word_preserved(self):
        out = format_reply("This is about well-being and follow-up")
        assert "well-being" in out and "follow-up" in out

    def test_midline_dash_separator_preserved(self):
        # A non-line-start " - " (e.g. flattened table output) must survive.
        out = format_reply("Status - open and ready")
        assert "Status - open and ready" in out

    def test_slack_bold_label_not_eaten_by_list_strip(self):
        # "*Status:*" has no space after the star -> not a bullet, must survive.
        out = format_reply("*Status:* open")
        assert "*Status:*" in out
