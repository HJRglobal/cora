"""Word-boundary regression tests for user_access sensitive-topic matching.

Root cause pinned here (live incident 2026-08-13, #f3-hq-inventory-adjustments):
`check_access` matched the hr / phi / cap_table topic keyword lists with a naive
``any(p in msg_lower for p in patterns)`` substring scan. Alex Cordova submitted
a routine F3 PURE office-inventory removal whose free-text Reason read "Handout
at camptontozona ( ASU FOOTBALL)". "cam-PTO-ntozona" contains "pto", so the
ENTIRE write was refused with "HR matters go to Hannah Grant or Harrison."

Two halves to every test class below, and the second half is the important one:
  * the false positives the boundary fix must KILL, and
  * every true positive the old substring form caught VIA INFLECTION
    (fired/firing, sickness, clients, percentage) which the fix must KEEP.
A precision fix that silently stops catching real HR/PHI/cap-table content would
be a worse defect than the one it closes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import user_access  # noqa: E402

ALEX = "U0B3VGWJTMJ"      # F3E/UFL/HJRG; blocks financials + hr + cap_table
HARRISON = "U0B2RM2JYJ1"  # root authority, no topic blocks

R_HR = "HR matters go to Hannah Grant or Harrison."
R_CAP = "Ownership details need Harrison."
R_PHI = "Client-specific health info stays in the EHR. Ask the clinical lead."

# The live 8/13 request, verbatim from the Slack thread (ts 1786663035.377059).
LIVE_INVENTORY_REQUEST = (
    "*<@U0B44MDGC5R|Cora>* make the following update:\n"
    "*OFFICE INVENTORY UPDATE - 1337 S Gilbert Rd*\n"
    "*Reason:* Handout at camptontozona ( ASU FOOTBALL)\n\n\n"
    "*F3 PURE - 12 Packs (cases removed)*\n"
    "• PURE-Original: 2\n"
    "• PURE-Citrus: 2\n"
    "• PURE-Tropical: 2\n"
    "• PURESL: 2"
)


@pytest.fixture
def block(monkeypatch):
    """Force a specific blocked-topic list so each topic is tested in isolation
    without depending on the live roster YAML."""
    def _apply(topics):
        monkeypatch.setattr(user_access, "blocked_topics", lambda _uid: list(topics))
    return _apply


class TestLiveIncident:
    """The exact 2026-08-13 false refusal, against the REAL roster config."""

    def test_camptontozona_inventory_write_is_not_refused(self):
        assert user_access.check_access(ALEX, "F3E", LIVE_INVENTORY_REQUEST) is None

    def test_pto_substring_alone_does_not_trip_hr(self, block):
        block(["hr"])
        for text in (
            "handout at camptontozona (asu football)",
            "we shipped to compton yesterday",
            "hampton inn receipt for the trip",
            "crypto payment option for the store",
        ):
            assert user_access.check_access(ALEX, "F3E", text) is None, text

    def test_real_pto_request_still_refused(self, block):
        block(["hr"])
        for text in ("can i take pto next friday", "how much PTO do i have left"):
            assert user_access.check_access(ALEX, "F3E", text) == R_HR, text


class TestLatentSiblings:
    """Substring collisions the same scan carried but nobody had hit yet."""

    def test_fireflies_is_not_an_hr_matter(self, block):
        block(["hr"])
        text = "can you pull the fireflies transcript from the standup"
        assert user_access.check_access(ALEX, "F3E", text) is None

    def test_firewall_and_misfire_are_not_hr(self, block):
        block(["hr"])
        for text in ("the firewall rule blocked it", "that guard misfired again"):
            assert user_access.check_access(ALEX, "F3E", text) is None, text

    def test_mistake_is_not_a_cap_table_matter(self, block):
        block(["cap_table"])
        text = "that was my mistake on the order quantity"
        assert user_access.check_access(ALEX, "F3E", text) is None

    def test_hampshire_is_not_hiring(self, block):
        block(["hr"])
        text = "the new hampshire distributor wants a quote"
        assert user_access.check_access(ALEX, "F3E", text) is None


class TestTruePositivesPreserved:
    """Everything the substring form matched, including via inflection."""

    @pytest.mark.parametrize("text", [
        "what is her salary",
        "what are the salaries for the ops team",
        "what's the compensation package",
        "what is his pay rate",
        "are we going to hire someone",
        "we hired two people last month",
        "who is hiring for that role",
        "did we fire him",
        "he was fired last week",
        "are we firing anyone",
        "we terminated her contract",          # hr 'terminate' inflection
        "when is his performance review",
        "there is an employee complaint",
        "start a disciplinary process",
        "what benefits do we offer staff",
        "how much vacation is left",
        "he called in sick",
        "her sickness leave paperwork",         # inflection
        "what does our 401k match",
        "what does our 401(k) match",           # punctuation form, previously MISSED
    ])
    def test_hr_true_positives(self, block, text):
        block(["hr"])
        assert user_access.check_access(ALEX, "F3E", text) == R_HR, text

    @pytest.mark.parametrize("text", [
        "what is the cap table",
        "what is my equity",
        "who has ownership of that",
        "how many shares outstanding",
        "what percent do i own",
        "what percentage do i own",             # inflection
        "what is his stake in the company",
        "what are the stakes for investors",
        "which investor led the round",
        "what is the dilution",
        "what is the valuation",
        "when is the funding round",
    ])
    def test_cap_table_true_positives(self, block, text):
        block(["cap_table"])
        assert user_access.check_access(ALEX, "F3E", text) == R_CAP, text

    @pytest.mark.parametrize("text", [
        "what is the client name",
        "pull up the clients list",              # inflection
        "the patient's diagnosis",
        "what diagnoses are recorded",
        "what treatment is planned",
        "which medications are listed",
        "review the care plan",
        "read the progress notes",
        "what does the clinical team say",
        "the ddd rate for that service",
        "is that hcbs billable",
        "behavioral health intake",
        "the therapy session notes",
    ])
    def test_phi_true_positives(self, block, text):
        # Entity must be one ALEX is authorized for -- the entity check runs
        # BEFORE the topic check, so an unauthorized entity would mask the
        # assertion with the entity refusal instead of the PHI redirect.
        block(["phi"])
        assert user_access.check_access(ALEX, "F3E", text) == R_PHI, text


class TestUnchangedBehavior:
    def test_harrison_never_topic_blocked(self):
        assert user_access.check_access(HARRISON, "F3E", "what is her salary") is None

    def test_unknown_topic_label_never_blocks(self, block):
        block(["not_a_real_topic"])
        assert user_access.check_access(ALEX, "F3E", "anything at all") is None

    def test_cross_entity_topic_is_entity_check_only(self, block):
        block(["cross_entity"])
        assert user_access.check_access(ALEX, "F3E", "anything at all") is None

    def test_empty_message_never_blocks(self, block):
        block(["hr", "phi", "cap_table"])
        assert user_access.check_access(ALEX, "F3E", "") is None
