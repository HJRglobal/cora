"""
[OSN] Tests for OSN entity system prompt content.

Verifies that osn.md contains the required guardrails, operating frames,
and disambiguation rules added in the 2026-05-24 system-prompt refresh.
"""

import pathlib
import pytest

# ---------------------------------------------------------------------------
# Fixture: load osn.md content once for all tests
# ---------------------------------------------------------------------------

_PROMPTS_DIR = pathlib.Path(__file__).parent.parent / "design" / "system-prompts"


@pytest.fixture(scope="module")
def osn_prompt() -> str:
    path = _PROMPTS_DIR / "osn.md"
    assert path.exists(), f"osn.md not found at {path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Category 2 — Operating frame
# ---------------------------------------------------------------------------


def test_90_day_horizon_present(osn_prompt):
    """osn.md must contain the 90-day operating horizon framing."""
    assert "90-day operating horizon" in osn_prompt


def test_90_day_horizon_check_in_date(osn_prompt):
    """The first check-in date (2026-05-25) must be referenced."""
    assert "2026-05-25" in osn_prompt


# ---------------------------------------------------------------------------
# Category 5 — Franchisor commitment refusal guardrail
# ---------------------------------------------------------------------------


def test_franchisor_refusal_section_present(osn_prompt):
    """osn.md must have a franchisor commitment refusal section."""
    assert "Franchisor commitment refusal" in osn_prompt


def test_section_32_2a_gate_referenced(osn_prompt):
    """Franchise agreement Section 32.2a must be explicitly called out."""
    assert "32.2a" in osn_prompt


def test_cbs_northstar_named_in_refusal(osn_prompt):
    """CBS NorthStar must be listed as a trigger for the franchisor refusal."""
    assert "CBS NorthStar" in osn_prompt or "CBSNS" in osn_prompt


def test_jennie_kerry_named_in_refusal(osn_prompt):
    """Jennie Kerry must be listed as a franchisor contact whose directives trigger the refusal."""
    assert "Jennie Kerry" in osn_prompt


# ---------------------------------------------------------------------------
# Category 5 — 5th store gate
# ---------------------------------------------------------------------------


def test_fifth_store_gate_present(osn_prompt):
    """osn.md must contain a 5th store gate section."""
    assert "5th store" in osn_prompt


def test_fifth_store_gate_requires_legal_review(osn_prompt):
    """The 5th store gate must explicitly require HJRG legal + capital review."""
    assert "HJRG legal" in osn_prompt
    assert "capital review" in osn_prompt


# ---------------------------------------------------------------------------
# Category 5 — Matt disambiguation rule
# ---------------------------------------------------------------------------


def test_matt_disambiguation_section_present(osn_prompt):
    """osn.md must have an explicit Matt disambiguation section."""
    assert "Matt disambiguation" in osn_prompt


def test_matt_petrovich_email_in_disambiguation(osn_prompt):
    """Matt Petrovich's email must appear in the disambiguation block."""
    assert "matt@hjrglobal.com" in osn_prompt


def test_matt_dennis_email_in_disambiguation(osn_prompt):
    """Matt Dennis's email must appear in the disambiguation block to prevent conflation."""
    assert "osnmatt@yahoo.com" in osn_prompt


# ---------------------------------------------------------------------------
# Category 2 — Passive investor discretion
# ---------------------------------------------------------------------------


def test_passive_investor_discretion_present(osn_prompt):
    """osn.md must have a passive investor discretion section."""
    assert "Passive investor discretion" in osn_prompt


def test_quinton_brandon_not_in_slack_noted(osn_prompt):
    """The fact that Quinton + Brandon are not in Slack must be noted."""
    assert "Quinton" in osn_prompt
    assert "Brandon" in osn_prompt
    assert "NOT in the Slack workspace" in osn_prompt


# ---------------------------------------------------------------------------
# Category 2 — APA signature gap
# ---------------------------------------------------------------------------


def test_apa_signature_gap_in_context(osn_prompt):
    """osn.md must surface the Matt Dennis promissory note signature gap."""
    assert "Matt Dennis" in osn_prompt
    assert "NOT signed" in osn_prompt or "has NOT signed" in osn_prompt


# ---------------------------------------------------------------------------
# Category 2 — HJRG management fee elimination
# ---------------------------------------------------------------------------


def test_management_fee_elimination_present(osn_prompt):
    """osn.md must note that the HJRG management fee was eliminated on 2026-05-19."""
    assert "management fee" in osn_prompt.lower()
    assert "4,300" in osn_prompt


# ---------------------------------------------------------------------------
# Category 2 — Hayden sunset delicacy
# ---------------------------------------------------------------------------


def test_hayden_sunset_delicacy_present(osn_prompt):
    """osn.md must warn against accelerating Hayden's sunset."""
    assert "sunset" in osn_prompt.lower()
    assert "Justin" in osn_prompt  # handoff path must be mentioned


# ---------------------------------------------------------------------------
# Structural: required base sections still present
# ---------------------------------------------------------------------------


def test_financial_guardrail_section_present(osn_prompt):
    """Financial guardrail section must remain intact after the patch."""
    assert "Financial guardrail (non-negotiable)" in osn_prompt
    assert "TIER_1" in osn_prompt
    assert "TIER_3" in osn_prompt


def test_cross_entity_scope_section_present(osn_prompt):
    """Cross-entity scope section must remain intact."""
    assert "Cross-entity scope" in osn_prompt


def test_voice_section_present(osn_prompt):
    """Voice & style section must remain intact."""
    assert "Voice & style" in osn_prompt


def test_knowledge_gap_marker_present(osn_prompt):
    """CORA_KNOWLEDGE_GAP marker instructions must remain intact."""
    assert "CORA_KNOWLEDGE_GAP" in osn_prompt
