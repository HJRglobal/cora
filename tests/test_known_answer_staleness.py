"""Two-tier known-answer staleness + the non-answer floor (session #11 S3).

Implements the C10b ruling AS RULED: cash/balances/AR/inventory -> label as-of,
warn 7d, stop serving 30d; prices/tiers/MSRP/contract values -> warn 90d, NEVER
auto-expire.

WHAT THIS DELIBERATELY DOES NOT CLOSE. The seed names "the superseded $36.99 note
in f3e.md" as the target. Age cannot separate it: the WRONG $36.99 note and the
CORRECT $39.99 note carry the SAME 2026-08-07 stamp. Under the ruled price tier
warn fires 2026-11-05 and withhold never fires. That defect is
supersession-by-contradiction, not staleness. Pinned below so the limitation is
recorded rather than assumed away.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.cora.known_answer_staleness import (
    CASH,
    PRICE,
    WARN_DAYS_CASH,
    WARN_DAYS_PRICE,
    WITHHOLD_DAYS_CASH,
    apply_staleness,
    classify,
)

TODAY = date(2026, 8, 30)


def _doc(*entries: str) -> str:
    return "## Known facts\n\n" + "\n\n".join(entries) + "\n"


def _entry(stamp: str, title: str, body: str) -> str:
    return f"**[{stamp}] {title}** _(gap autofill)_\n{body}"


class TestClassifier:
    @pytest.mark.parametrize("text", [
        "Portfolio cash balance is $1,347,657",
        "AR aging shows $42,000 over 90 days",
        "Ending cash/CC book balance is $77,629",
        "On-hand count is 412 units",
    ])
    def test_cash_class(self, text):
        assert classify(text) == CASH

    @pytest.mark.parametrize("text", [
        "F3 Pure MSRP is $39.99",
        "Wholesale price is $24 per case",
        "The contract value is $120,000",
    ])
    def test_price_class(self, text):
        assert classify(text) == PRICE

    @pytest.mark.parametrize("text", [
        "The Gilbert office is at 123 Main St",
        "Alex Cordova runs F3E athlete ops",
        "",
    ])
    def test_unmatched_falls_through(self, text):
        """Withholding is the only reader-destructive action here, so anything
        unmatched must get NO action rather than a guess."""
        assert classify(text) is None

    def test_cash_wins_when_both_match(self):
        assert classify("cash position for the wholesale price tier") == CASH


class TestCashTier:
    def test_fresh_cash_is_labelled_with_as_of(self):
        out = apply_staleness(
            _doc(_entry("2026-08-29", "cash", "A: Cash position is $500.")), TODAY)
        assert "_[as of 2026-08-29]_" in out
        assert "$500" in out

    def test_cash_warns_after_7_days(self):
        stamp = date(2026, 8, 30 - WARN_DAYS_CASH).isoformat()
        out = apply_staleness(_doc(_entry(stamp, "cash", "A: Cash balance is $900.")), TODAY)
        assert "AS OF" in out and "Verify before relying" in out
        assert "$900" in out          # warned, still served

    def test_cash_is_withheld_after_30_days(self):
        """ACCEPTANCE 1 from the kickoff."""
        out = apply_staleness(
            _doc(_entry("2026-07-13", "portfolio cash",
                        "A: Portfolio cash balance is $1,347,657.")), TODAY)
        assert "$1,347,657" not in out           # the figure is gone
        assert "WITHHELD" in out
        assert "2026-07-13" in out               # the as-of date is still stated

    def test_withheld_entry_keeps_its_header(self):
        """Dropping it silently would look like the fact never existed, which is
        its own kind of false state."""
        out = apply_staleness(
            _doc(_entry("2026-07-13", "portfolio cash", "A: Cash balance is $1.")), TODAY)
        assert "portfolio cash" in out

    def test_boundary_exactly_30_days_withholds(self):
        stamp = date(2026, 7, 31).isoformat()     # exactly 30 days before TODAY
        out = apply_staleness(_doc(_entry(stamp, "cash", "A: Cash balance is $7.")), TODAY)
        assert (TODAY - date(2026, 7, 31)).days == WITHHOLD_DAYS_CASH
        assert "WITHHELD" in out


class TestPriceTierNeverExpires:
    def test_91_day_price_serves_with_a_warn_label(self):
        """ACCEPTANCE 2 from the kickoff."""
        stamp = date(2026, 5, 31).isoformat()      # 91 days before TODAY
        out = apply_staleness(
            _doc(_entry(stamp, "pure price", "A: F3 Pure MSRP is $39.99.")), TODAY)
        assert (TODAY - date(2026, 5, 31)).days == 91
        assert "$39.99" in out                     # STILL SERVED
        assert "AS OF" in out
        assert "do not expire automatically" in out

    def test_very_old_price_still_serves(self):
        out = apply_staleness(
            _doc(_entry("2024-01-01", "old price", "A: Wholesale price is $24.")), TODAY)
        assert "$24" in out
        assert "WITHHELD" not in out

    def test_fresh_price_gets_no_label(self):
        out = apply_staleness(
            _doc(_entry("2026-08-29", "price", "A: MSRP is $39.99.")), TODAY)
        assert "AS OF" not in out
        assert "$39.99" in out

    def test_warn_threshold_is_the_ruled_90_days(self):
        assert WARN_DAYS_PRICE == 90
        assert WARN_DAYS_CASH == 7
        assert WITHHOLD_DAYS_CASH == 30


class TestUndatedContentIsNeverTouched:
    def test_hand_written_subsection_untouched(self):
        """9 of 43 live items are hand-written `###` blocks with no machine
        stamp -- and they include the authoritative F3E price ladder."""
        doc = ("## Known facts\n\n"
               "### F3 pricing\nWholesale $24, retail $39.99. Cash balance rules apply.\n")
        assert apply_staleness(doc, TODAY) == doc

    def test_document_without_the_section_is_returned_unchanged(self):
        doc = "# Known Answers\n\nSome prose about cash balance.\n"
        assert apply_staleness(doc, TODAY) == doc

    def test_empty_input(self):
        assert apply_staleness("", TODAY) == ""


class TestSupersessionIsNotClosedByThisSlice:
    def test_same_day_contradiction_is_untouched(self):
        """The measured limitation, pinned. Both f3e price notes carry the SAME
        2026-08-07 stamp, so age cannot separate them and BOTH keep serving."""
        out = apply_staleness(_doc(
            _entry("2026-08-07", "pure price", "A: F3 Pure MSRP was raised to $39.99."),
            _entry("2026-08-07", "pure price", "A: F3 Pure retail price is $36.99 everywhere."),
        ), TODAY)
        assert "$39.99" in out and "$36.99" in out
        assert "WITHHELD" not in out


class TestAppliedToEveryReadView:
    """The store has three readers. A withhold in only one means a 30-day-stale
    cash figure is withheld from Slack and quoted verbatim to a Code session."""

    def test_context_loader_applies_it(self):
        import io

        src = io.open("src/cora/context_loader.py", encoding="utf-8").read()
        assert "known_answer_staleness.apply_staleness" in src

    def test_mcp_server_applies_it(self):
        import io

        src = io.open("src/cora/mcp_server.py", encoding="utf-8").read()
        assert "known_answer_staleness.apply_staleness" in src

    def test_section_header_constant_untouched(self):
        """claude_client._STATIC_SECTION_HEADERS pins this literal to define the
        never-trim protected tail. Renaming it would make known-answers
        trimmable under budget pressure."""
        from src.cora.context_loader import KNOWN_ANSWERS_SECTION_HEADER

        assert KNOWN_ANSWERS_SECTION_HEADER == "# Known Answers (from prior gap reviews)"


class TestContributedNoteQualityFloor:
    """apply_contributed_note had NO quality screen, so a deflection or a
    conversational fragment became always-injected canon."""

    def test_the_live_fragment_is_now_refused(self):
        """fndr.md carries "and he can update inventory for us" (2026-08-24).
        MEASURED: the pre-existing answer_quality_ok PASSES it -- it is long
        enough, not a deflection, not in-progress, not a snapshot. Wiring the
        existing floor forward would NOT have prevented this case."""
        from src.cora.gap_autofill import answer_quality_ok, contributed_note_quality_ok

        text = "and he can update inventory for us"
        assert answer_quality_ok(text)[0] is True          # the old floor passes it
        assert contributed_note_quality_ok(text)[0] is False

    @pytest.mark.parametrize("text", [
        "I do not know, ask Justin.",
        "That is still being worked on.",
    ])
    def test_existing_floor_classes_are_inherited(self, text):
        from src.cora.gap_autofill import contributed_note_quality_ok

        assert contributed_note_quality_ok(text)[0] is False

    @pytest.mark.parametrize("text", [
        "Payment terms are Net 30.",
        "Alex Cordova runs F3E athlete ops.",
        "However, the Gilbert office moved to Suite 200.",   # leading adverb + comma
        "Also, Alex owns athlete ops.",                      # comma-separated marker
        "And the office moved to Suite 200.",                # capitalised opener
    ])
    def test_real_facts_still_pass(self, text):
        """Measuring what a precision fix STOPS catching is the discipline: the
        first draft was case-insensitive and rejected the 'However,' line."""
        from src.cora.gap_autofill import contributed_note_quality_ok

        ok, reason = contributed_note_quality_ok(text)
        assert ok, reason

    def test_apply_known_answer_stays_screen_free(self):
        """Deliberate asymmetry: a Harrison thumbs-up must ALWAYS produce a
        write, or you get "approved but nothing saved"."""
        import io

        src = io.open("src/cora/gap_autofill.py", encoding="utf-8").read()
        head = src.index("def apply_known_answer(")
        body = src[head:head + 3000]
        assert "contributed_note_quality_ok" not in body


class TestFloorIsKindAware:
    """A BOOKMARK is a pointer to a document by design ("The retail deck lives
    in the F3E Drive"), so answer_quality_ok's vague-deflection rule -- "answer
    punts to a person/doc/tool instead of stating the fact" -- is precisely
    wrong for it. Screening a bookmark with a rule written for mined ANSWERS is
    the reuse-a-screen-on-the-wrong-text-shape error; the first draft of this
    floor broke test_contributed_note_bookmark_provenance."""

    POINTER = "The retail deck lives in the F3E Drive."

    def test_bookmark_pointer_is_allowed(self):
        from src.cora.gap_autofill import contributed_note_quality_ok

        assert contributed_note_quality_ok(self.POINTER, "bookmark")[0] is True

    def test_same_text_as_a_note_is_still_a_deflection(self):
        from src.cora.gap_autofill import contributed_note_quality_ok

        assert contributed_note_quality_ok(self.POINTER, "note")[0] is False

    def test_bookmark_still_gets_substance_and_fragment_checks(self):
        """Only the deflection rule is lifted, not the whole floor."""
        from src.cora.gap_autofill import contributed_note_quality_ok

        assert contributed_note_quality_ok("ok", "bookmark")[0] is False
        assert contributed_note_quality_ok(
            "and he can update inventory for us", "bookmark")[0] is False
