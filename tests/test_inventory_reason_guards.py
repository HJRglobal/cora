"""The two Reason-line guards, and which one reads what (cq-1b6554a58fae).

Reported after the 8/19 round3 acceptance smokes as an INVERSION: "the entity
guard reads the Reason, the HR guard does not". Verified against live behaviour
and the log, one half held and one half did not:

  * TRUE -- an F3E office write explaining itself as "...sent to the OSN pop-up"
    was redirected to OSN ("cross-entity redirect fired", 8/19 13:14:34 in
    #f3-hq-inventory-adjustments). Cause: guard_input recognized only the RIGID
    3-line template, and since the 7/21 inventory overhaul these writes are filed
    as prose -- the two that succeeded on that same run were 105- and 108-char
    natural sentences. No header, no "Reason:" field, nothing stripped, so the
    word "OSN" in the operator's own justification did the routing.

  * FALSE -- "PTO payout comp adjustment" previewing instead of refusing was
    CORRECT, and neither suspected cause (case-sensitivity on PTO; the HR guard
    no longer reading the Reason) is real. Measured: Alex, who IS blocked on HR,
    is refused on that exact text; "camptontozona" still passes for him (the 8/13
    word-bounding fix holds). The smoke was run by Harrison (U0B2RM2JYJ1 in the
    log), who is unrestricted on HR -- so is Hannah, who owns HR. There was
    nothing to fix on that side, and the tests below pin it so the next report of
    this shape is answered in seconds.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import cross_entity_guard as ceg  # noqa: E402
from cora import guard_input as gi  # noqa: E402
from cora import user_access as ua  # noqa: E402

HARRISON = "U0B2RM2JYJ1"
ALEX = "U0B3VGWJTMJ"      # F3E ops -- blocked on HR topics
HANNAH = "U0B3TPD2MEV"    # owns HR

# Shaped like the real messages (D-203). Prose, no template.
WRITE_PROSE = "add 2 Pure Citrus at the office, replacing the cases sent to the OSN pop-up"
WRITE_PROSE_REMOVE = "remove 1 Pure Original 12-pack from the office - sent to the OSN pop-up"
WRITE_SET = "set Pure Citrus to 120 at the office (restock after the OSN pop-up)"
WRITE_TEMPLATE = ("OFFICE INVENTORY UPDATE - 1337 S Gilbert Rd\n"
                  "Reason: replacing the cases sent to the OSN pop-up\n"
                  "PURE-Original: 12")
HR_REASON = ("OFFICE INVENTORY UPDATE - 1337 S Gilbert Rd\n"
             "Reason: PTO payout comp adjustment\n"
             "PURE-Original: 12")
CAMPTONTOZONA = ("OFFICE INVENTORY UPDATE\n"
                 "Reason: Handout at camptontozona ( ASU FOOTBALL)\n"
                 "PURE-Original: 12")


# ── the entity guard must NOT route on a write's justification ────────────────

INV_CHANNEL = "f3-hq-inventory-adjustments"


@pytest.mark.parametrize("message", [WRITE_PROSE, WRITE_PROSE_REMOVE, WRITE_SET])
def test_a_prose_inventory_write_naming_another_entity_is_not_redirected(message):
    assert gi.is_inventory_write_request(message) is True
    assert ceg.check_cross_entity(message, "F3E", channel_name=INV_CHANNEL) is None


@pytest.mark.parametrize("message", [WRITE_PROSE, WRITE_PROSE_REMOVE, WRITE_SET])
def test_the_exemption_is_scoped_to_the_write_channels(message):
    """Outside a configured office-inventory channel the guard is untouched. That
    scoping is what keeps two existing controls alive: an F3E write posted in an
    OSN channel still redirects (its BODY names F3 PURE), and an over-long Reason
    still redirects because the strip's length cap leaves it visible."""
    assert ceg.check_cross_entity(message, "F3E") is not None
    assert ceg.check_cross_entity(message, "F3E", channel_name="f3e-leadership") is not None


def test_the_write_channels_come_from_the_same_map_the_write_tool_reads():
    """Data, not a hardcoded channel name -- and the two readers must agree on the
    live file, or an added channel silently gets the exemption in one and not the
    other."""
    from cora.tools import tool_dispatch as td
    channels = gi.inventory_write_channels()
    assert channels, "the live map must configure at least one write channel"
    for name in channels:
        assert gi.is_inventory_write_channel(name) is True
        assert td._load_inventory_channel_config(name), (
            f"{name} is a guard-exempt channel but the write tool has no config "
            "for it -- the two readers have drifted")


def test_an_unconfigured_channel_is_never_exempt():
    for name in (None, "", "random-channel", "osn-leadership"):
        assert gi.is_inventory_write_channel(name) is False


def test_the_template_form_is_governed_by_the_STRIP_not_the_prose_exemption():
    """Two mechanisms, one per form. The template's own words carry no imperative
    verb, so it is not "prose"; its annotation Reason is blanked by
    scope_guard_text instead -- which is what keeps a REQUEST-shaped Reason
    visible to the guard."""
    assert gi.is_inventory_write_request(WRITE_TEMPLATE) is False
    assert gi.is_inventory_adjustment_request(WRITE_TEMPLATE) is True
    assert ceg.check_cross_entity(WRITE_TEMPLATE, "F3E") is None
    assert ceg.check_cross_entity(WRITE_TEMPLATE, "F3E",
                                  channel_name=INV_CHANNEL) is None


def test_a_question_smuggled_into_a_template_reason_is_still_redirected():
    """The hole the first cut of this fix opened: short-circuiting the prose
    predicate on "satisfies the template" made a smuggled question exempt, which
    is exactly what _REASON_IS_REQUEST_RE exists to prevent."""
    smuggled = ("OFFICE INVENTORY UPDATE - 1337 S Gilbert Rd\n"
                "Reason: what is the OSN revenue\n"
                "PURE-Original: 12")
    assert gi.is_inventory_write_request(smuggled) is False
    assert ceg.check_cross_entity(smuggled, "F3E", channel_name=INV_CHANNEL) is not None


# ── ...and MUST still route a genuine cross-entity question ──────────────────

@pytest.mark.parametrize("message", [
    "how are the OSN stores doing this week?",
    "what is the OSN inventory at the greenfield store",
    "can you set up the OSN store inventory count for 4 stores",
    "OSN sales are down 20 percent this month",
])
def test_a_genuine_cross_entity_question_still_redirects(message):
    """Verified live as the control on the same smoke run -- and it must hold IN
    the inventory channel, which is where it was observed."""
    assert gi.is_inventory_write_request(message) is False
    assert ceg.check_cross_entity(message, "F3E", channel_name=INV_CHANNEL) is not None


def test_a_question_stapled_to_a_write_does_not_get_the_write_exemption():
    """The question veto is HARD: the failure direction must be "guard runs"."""
    smuggled = "how are the OSN stores doing? also add 2 Pure Citrus at the office"
    assert gi.is_inventory_write_request(smuggled) is False
    assert ceg.check_cross_entity(smuggled, "F3E", channel_name=INV_CHANNEL) is not None


def test_every_condition_is_required():
    # no number
    assert gi.is_inventory_write_request(
        "add cases at the office for the OSN pop-up") is False
    # no inventory anchor -- otherwise "add 2 OSN stores to the list" would qualify
    assert gi.is_inventory_write_request(
        "add 2 OSN stores to the distribution list") is False
    # no write verb
    assert gi.is_inventory_write_request(
        "2 cases of Pure at the office, OSN pop-up") is False


def test_predicate_is_pure_and_total():
    for value in ("", None, 123, [], {"a": 1}):
        assert gi.is_inventory_write_request(value) is False


def test_the_two_predicates_stay_independent():
    """Neither form's mechanism may become a bypass for the other's."""
    assert gi.is_inventory_adjustment_request(WRITE_PROSE) is False
    assert gi.is_inventory_write_request(WRITE_TEMPLATE) is False


def test_the_exemption_does_not_reach_a_hard_block():
    """cross_entity_guard is the only place the write exemption applies. The
    LBHS confidential hard block reads the RAW message before any normalization
    (D-051 HIGH, 8/17) and must stay unreachable from here."""
    from cora import sibling_guard
    smuggled = ("add 2 Pure Citrus at the office, cases for the COPA diligence "
                "review with UnitedHealthcare")
    assert sibling_guard.check_redirect("LEX-LBHS", smuggled) is not None


# ── the HR guard reads the Reason, and always did ─────────────────────────────

def test_hr_topic_in_a_reason_refuses_for_a_blocked_user():
    """Alex is blocked on HR and files these writes daily -- the case that
    matters. Word-bounded and case-insensitive: "PTO" in caps still fires."""
    assert ua.check_access(ALEX, "F3E", HR_REASON) is not None
    assert "HR" in ua.check_access(ALEX, "F3E", HR_REASON)


def test_the_camptontozona_false_refusal_stays_fixed():
    """The 8/13 substring bug ("pto" inside "cam-PTO-ntozona"), fixed by
    word-bounding the topic patterns rather than by stripping."""
    assert ua.check_access(ALEX, "F3E", CAMPTONTOZONA) is None


def test_an_unrestricted_user_previewing_an_hr_flavoured_reason_is_correct():
    """The reported "PREVIEWED instead of refusing" was Harrison's own smoke.
    Neither he nor Hannah is blocked on HR, so a preview is the right outcome --
    there is no defect on this side."""
    assert ua.check_access(HARRISON, "F3E", HR_REASON) is None
    assert ua.check_access(HANNAH, "F3E", HR_REASON) is None


def test_user_access_reads_the_raw_message_not_the_stripped_one():
    """The security boundary: the strip is entity-guards-only. If user_access
    ever read normalized text, a declarative payload in a Reason would sail past
    every topic block."""
    src = (_REPO_ROOT / "src" / "cora" / "user_access.py").read_text(encoding="utf-8")
    assert "scope_guard_text" not in src.replace(
        "# ALWAYS the full RAW message. guard_input.scope_guard_text is deliberately", "")


def test_a_prose_write_is_not_an_hr_bypass():
    """The prose exemption is scoped to the ENTITY guard. A prose inventory write
    carrying an HR topic is still refused for a user blocked on HR."""
    prose_hr = ("add 2 Pure Citrus at the office, comp adjustment for "
                "the PTO payout")
    assert gi.is_inventory_write_request(prose_hr) is True
    assert ceg.check_cross_entity(prose_hr, "F3E", channel_name=INV_CHANNEL) is None
    assert ua.check_access(ALEX, "F3E", prose_hr) is not None
