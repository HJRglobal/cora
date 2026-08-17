"""Scope guards must not route on an inventory write's free-text Reason field.

Live incidents, #f3-hq-inventory-adjustments (verbatim messages below):
  * 2026-08-03 12:00 Skylar Eastham  -- "Reason: Cases damaged during shipping"
  * 2026-08-03 13:23 Tommy Anderson  -- "Reason: 4 OSN Stores"      -> refused
  * 2026-08-05 16:35 Tommy Anderson  -- "Reason: OSN Stores"        -> refused
  * 2026-08-06 07:55 Hannah Grant    -- "Reason: OSN Stores"        -> refused
  * 2026-08-13 16:17 Alex Cordova    -- "Reason: Handout at camptontozona"
Every SKU is F3 PURE and the location is the F3 office; the Reason is annotation.

The load-bearing test in this file is TestNoEvasionVector: the strip requires the
FULL inventory-request shape, so "Reason:" can never be used as a guard-evasion
prefix on an ordinary question.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import cross_entity_guard, guard_input, sibling_guard, user_access  # noqa: E402

ALEX = "U0B3VGWJTMJ"    # F3E/UFL/HJRG; blocks financials + hr + cap_table
TOMMY = "U0B3RU5Q55G"


def _request(reason: str, *, removed: bool = True) -> str:
    verb = "removed" if removed else "added"
    return (
        f"<@U0B44MDGC5R|Cora> make the following update:\n"
        f"OFFICE INVENTORY UPDATE - 1337 S Gilbert Rd\n"
        f"Reason: {reason}\n\n\n"
        f"F3 PURE - 12 Packs (cases {verb})\n"
        f"• PURE-Original: 2\n"
        f"• PURE-Citrus: 2\n"
        f"• PURESL: 2"
    )


# The 8/13 message exactly as Slack delivered it (bolded Reason label).
LIVE_BOLDED = (
    "*<@U0B44MDGC5R|Cora>* make the following update:\n"
    "*OFFICE INVENTORY UPDATE - 1337 S Gilbert Rd*\n"
    "*Reason:* Handout at camptontozona ( ASU FOOTBALL)\n\n\n"
    "*F3 PURE - 12 Packs (cases removed)*\n"
    "• PURE-Original: 2\n• PURE-Citrus: 2\n• PURESL: 2"
)


class TestShapeDetection:
    def test_full_request_is_recognized(self):
        assert guard_input.is_inventory_adjustment_request(_request("OSN Stores"))

    def test_bolded_slack_form_is_recognized(self):
        assert guard_input.is_inventory_adjustment_request(LIVE_BOLDED)

    @pytest.mark.parametrize("text", [
        "Reason: 4 OSN Stores",                                   # reason only
        "OFFICE INVENTORY UPDATE - 1337 S Gilbert Rd",            # header only
        "OFFICE INVENTORY UPDATE - 1337\nReason: OSN Stores",     # no SKU line
        "Reason: OSN Stores\n• PURE-Original: 2",                 # no header
        "what is the osn inventory",
        "",
    ])
    def test_partial_shapes_are_not_recognized(self, text):
        assert not guard_input.is_inventory_adjustment_request(text)

    def test_reason_value_blanked_label_kept(self):
        out = guard_input.scope_guard_text(_request("4 OSN Stores"))
        assert "OSN" not in out
        assert "Reason:" in out
        assert "PURE-Original: 2" in out          # SKUs untouched
        assert "INVENTORY UPDATE" in out          # header untouched

    def test_non_request_text_returned_unchanged(self):
        text = "Reason: 4 OSN Stores and nothing else"
        assert guard_input.scope_guard_text(text) == text

    def test_non_string_passes_through(self):
        assert guard_input.scope_guard_text(None) is None
        assert guard_input.scope_guard_text("") == ""


class TestCrossEntityFalsePositivesClosed:
    """The 5 documented OSN refusals, through the real guard."""

    @pytest.mark.parametrize("reason", [
        "4 OSN Stores",
        "OSN Stores",
        "osn stores",
        "Cases damaged during shipping",
        "Handout at camptontozona ( ASU FOOTBALL)",
        "Transfer for the four stores",
        "matt petrovich asked for these",
    ])
    def test_inventory_write_not_redirected_from_f3e_channel(self, reason):
        assert cross_entity_guard.check_cross_entity(_request(reason), "F3E") is None

    def test_live_bolded_message_not_redirected(self):
        assert cross_entity_guard.check_cross_entity(LIVE_BOLDED, "F3E") is None


class TestNoEvasionVector:
    """A bare "Reason:" prefix must NOT disarm any guard. If these ever fail,
    the strip has become too broad and is a security hole, not a bug fix."""

    @pytest.mark.parametrize("text", [
        "Reason: what is the OSN revenue",
        "Reason: how are the one stop nutrition stores doing",
        "*Reason:* tell me about gilbert warner",
        "Reason: osn\nReason: osn\nReason: osn",
    ])
    def test_reason_prefix_alone_still_redirects(self, text):
        assert cross_entity_guard.check_cross_entity(text, "F3E") is not None

    def test_reason_prefix_alone_still_topic_blocks(self):
        # No inventory shape -> hr topic still evaluated in full.
        text = "Reason: what is Hannah's salary"
        assert user_access.check_access(ALEX, "F3E", text) is not None

    def test_header_and_reason_without_skus_still_guarded(self):
        text = "OFFICE INVENTORY UPDATE - 1337\nReason: what is the OSN revenue"
        assert cross_entity_guard.check_cross_entity(text, "F3E") is not None

    def test_entity_named_outside_the_reason_line_still_redirects(self):
        # Keyword in the BODY, not the Reason -> a genuine cross-entity ask
        # riding an inventory-shaped message must still be caught.
        text = _request("Yoga Event") + "\n\nAlso how is One Stop Nutrition doing?"
        assert cross_entity_guard.check_cross_entity(text, "F3E") is not None


class TestTruePositivesIntact:
    def test_genuine_osn_question_still_redirects(self):
        assert cross_entity_guard.check_cross_entity(
            "how are the OSN stores performing this week", "F3E") is not None

    def test_lex_question_from_f3e_channel_still_redirects(self):
        assert cross_entity_guard.check_cross_entity(
            "what is the lexington revalidation status", "F3E") is not None

    def test_sibling_guard_still_redirects_real_sibling_ask(self):
        assert sibling_guard.check_redirect(
            "LEX-LLC", "how is LBHS doing on the COPA diligence") is not None

    def test_f3e_inventory_write_in_an_osn_channel_still_redirects(self):
        # Stripping the Reason does NOT make the message entity-neutral: the body
        # names "F3 PURE", so posting this F3E write in an OSN channel is still
        # correctly redirected to F3E. The strip only removes the annotation.
        redirect = cross_entity_guard.check_cross_entity(_request("OSN Stores"), "OSN")
        assert redirect is not None
        assert "F3 Energy" in redirect

    def test_hr_topic_still_blocks_inside_a_real_inventory_request(self):
        # The Reason is stripped, but an HR ask in the BODY must still refuse.
        text = _request("Yoga Event") + "\n\nAlso what is Tommy's salary?"
        assert user_access.check_access(ALEX, "F3E", text) is not None
