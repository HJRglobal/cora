"""
[HJRP] Tests for HJRP entity system prompt content.

Verifies that hjrp.md contains the required guardrails, Rogers Ranch context,
lease state, team lanes, and structural sections added in the 2026-05-24 ship.
"""

import pathlib

import pytest

from cora.prompt_loader import load_prompt, clear_cache

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_PROMPTS_DIR = pathlib.Path(__file__).parent.parent / "design" / "system-prompts"


@pytest.fixture(scope="module")
def hjrp_prompt() -> str:
    path = _PROMPTS_DIR / "hjrp.md"
    assert path.exists(), f"hjrp.md not found at {path}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def clear_prompt_cache():
    """Clear the prompt_loader cache before each test so monkeypatches take effect."""
    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# prompt_loader integration: HJRP must get its own prompt, not FNDR fallback
# ---------------------------------------------------------------------------


def test_hjrp_loads_without_error(hjrp_prompt):
    """hjrp.md must be non-empty and readable."""
    assert len(hjrp_prompt) > 500


def test_prompt_loader_returns_hjrp_not_fndr():
    """prompt_loader.load_prompt('HJRP') must return HJRP-specific content, not FNDR fallback."""
    text = load_prompt("HJRP")
    # HJRP-specific phrase that does not appear in fndr.md
    assert "HJR Properties" in text
    # Should NOT be falling back to the FNDR prompt identity line
    assert "founder-level / HJR Global" not in text


def test_prompt_loader_hjrp_not_identical_to_fndr():
    """HJRP prompt must differ from FNDR prompt (regression: was fndr.md before 2026-05-24)."""
    hjrp_text = load_prompt("HJRP")
    fndr_text = load_prompt("FNDR")
    assert hjrp_text != fndr_text


# ---------------------------------------------------------------------------
# Category 2 — Sub-entity awareness
# ---------------------------------------------------------------------------


def test_three_sub_entities_present(hjrp_prompt):
    """hjrp.md must list all three sub-entities."""
    assert "HJRP-RR" in hjrp_prompt
    assert "HJRP-CL" in hjrp_prompt
    assert "HJRP-LCI" in hjrp_prompt


def test_rogers_ranch_pre_launch_status(hjrp_prompt):
    """Rogers Ranch PRE-LAUNCH status must be noted."""
    assert "PRE-LAUNCH" in hjrp_prompt


# ---------------------------------------------------------------------------
# Category 2 — Cap table
# ---------------------------------------------------------------------------


def test_cap_table_99_1_present(hjrp_prompt):
    """99/1 cap table (Harrison/Mikenna) must be stated."""
    assert "99" in hjrp_prompt
    assert "Mikenna" in hjrp_prompt


# ---------------------------------------------------------------------------
# Category 2 — Rogers Ranch sub-entity block
# ---------------------------------------------------------------------------


def test_rogers_ranch_three_modes_present(hjrp_prompt):
    """All three Rogers Ranch customer modes must be named."""
    assert "Couples" in hjrp_prompt or "couples" in hjrp_prompt
    assert "Corporate retreat" in hjrp_prompt or "corporate retreat" in hjrp_prompt
    assert "Wedding" in hjrp_prompt or "wedding" in hjrp_prompt


def test_rogers_ranch_cabin_ids_present(hjrp_prompt):
    """Airbnb cabin IDs must be in the prompt for accurate Q&A."""
    assert "1362316960015926021" in hjrp_prompt  # L1
    assert "1359436723147407559" in hjrp_prompt  # L2


def test_rogers_ranch_superhost_noted(hjrp_prompt):
    """Superhost rating must be referenced."""
    assert "Superhost" in hjrp_prompt or "superhost" in hjrp_prompt


def test_rogers_ranch_mikenna_ops_anchor(hjrp_prompt):
    """Mikenna as guest messaging + Airbnb ops anchor must be explicitly stated."""
    assert "Mikenna" in hjrp_prompt
    assert "guest messaging" in hjrp_prompt or "Airbnb" in hjrp_prompt


def test_rogers_ranch_google_calendar_id_present(hjrp_prompt):
    """Rogers Ranch Google Calendar ID must be present for booking context."""
    assert "533b99e0" in hjrp_prompt


def test_rogers_ranch_wedding_harrison_signoff(hjrp_prompt):
    """Wedding contracts must require Harrison sign-off per sole-authority doctrine."""
    assert "Harrison sign-off" in hjrp_prompt or "Harrison" in hjrp_prompt


# ---------------------------------------------------------------------------
# Category 2 + Cat 5 — Active lease state
# ---------------------------------------------------------------------------


def test_vitalant_lease_renewed(hjrp_prompt):
    """Vitalant lease renewal (June 2026) must be stated as closed/resolved."""
    assert "Vitalant" in hjrp_prompt
    assert "June 2026" in hjrp_prompt


def test_vine_branches_not_renewing(hjrp_prompt):
    """Vine & Branches non-renewal + 6/30 expiry must be present."""
    assert "Vine & Branches" in hjrp_prompt
    assert "2026-06-30" in hjrp_prompt or "6/30" in hjrp_prompt


def test_sharon_carstens_broker_present(hjrp_prompt):
    """Sharon Carstens broker contact must be in the prompt."""
    assert "Sharon Carstens" in hjrp_prompt
    assert "brokerandtrainer@gmail.com" in hjrp_prompt


def test_hampton_cams_noted(hjrp_prompt):
    """Hampton CAMS finding must be noted (no formal CAMS since 1992)."""
    assert "Hampton CAMS" in hjrp_prompt or "CAMS" in hjrp_prompt


def test_mma_lab_dispute_present(hjrp_prompt):
    """MMA Lab / Carbas CAM dispute must be flagged as sensitive."""
    assert "MMA Lab" in hjrp_prompt


# ---------------------------------------------------------------------------
# Category 5 — Tenant confidentiality guardrail
# ---------------------------------------------------------------------------


def test_tenant_confidentiality_section_present(hjrp_prompt):
    """Tenant confidentiality (non-negotiable) section must be present."""
    assert "Tenant confidentiality" in hjrp_prompt


def test_tenant_details_stay_in_hjrp_channels(hjrp_prompt):
    """Prompt must specify tenant details stay inside #hjrp-* channels."""
    assert "#hjrp-" in hjrp_prompt


# ---------------------------------------------------------------------------
# Category 5 — Harrison sole-authority doctrine
# ---------------------------------------------------------------------------


def test_sole_authority_section_present(hjrp_prompt):
    """Harrison sole-authority section must be present."""
    assert "sole-authority" in hjrp_prompt or "sole authority" in hjrp_prompt


def test_operators_not_approval_gates(hjrp_prompt):
    """Prompt must state operators are not approval gates."""
    assert "approval gate" in hjrp_prompt or "approval gates" in hjrp_prompt


# ---------------------------------------------------------------------------
# Category 5 — Real-estate source-opacity
# ---------------------------------------------------------------------------


def test_source_opacity_section_present(hjrp_prompt):
    """Real-estate source-opacity section must be present."""
    assert "source-opacity" in hjrp_prompt or "source opacity" in hjrp_prompt


# ---------------------------------------------------------------------------
# Category 5 — Cross-entity firewall
# ---------------------------------------------------------------------------


def test_cross_entity_scope_section_present(hjrp_prompt):
    """Cross-entity scope section must be present."""
    assert "Cross-entity scope" in hjrp_prompt


def test_f3_energy_excluded_from_hjrp(hjrp_prompt):
    """F3 Energy must be listed in the scope exclusions."""
    assert "F3 Energy" in hjrp_prompt


def test_lexington_excluded_from_hjrp(hjrp_prompt):
    """Lexington Services must be listed in the scope exclusions."""
    assert "Lexington" in hjrp_prompt


# ---------------------------------------------------------------------------
# Category 6 — Team lanes
# ---------------------------------------------------------------------------


def test_justin_financial_lane_present(hjrp_prompt):
    """Justin Moran's financial lane for HJRP must be referenced."""
    assert "Justin" in hjrp_prompt


def test_hannah_ops_lane_present(hjrp_prompt):
    """Hannah Grant's HJRP recurring ops lane must be referenced."""
    assert "Hannah" in hjrp_prompt


def test_tessa_lease_coord_lane_present(hjrp_prompt):
    """Tessa Miller's lease-renewal coordination lane must be referenced."""
    assert "Tessa" in hjrp_prompt
    assert "lease" in hjrp_prompt.lower()


# ---------------------------------------------------------------------------
# Structural: required base sections
# ---------------------------------------------------------------------------


def test_financial_guardrail_section_present(hjrp_prompt):
    """Financial guardrail section must be present with TIER_1 / TIER_3."""
    assert "Financial guardrail (non-negotiable)" in hjrp_prompt
    assert "TIER_1" in hjrp_prompt
    assert "TIER_3" in hjrp_prompt


def test_financial_data_unknown_response_present(hjrp_prompt):
    """The exact UNKNOWN_RESPONSE text must be present for financial data fallback."""
    assert "I will notify the finance department immediately" in hjrp_prompt


def test_voice_section_present(hjrp_prompt):
    """Voice & style section must be present."""
    assert "Voice & style" in hjrp_prompt


def test_knowledge_gap_marker_present(hjrp_prompt):
    """CORA_KNOWLEDGE_GAP marker instructions must be present."""
    assert "CORA_KNOWLEDGE_GAP" in hjrp_prompt


def test_no_sign_off_instruction_present(hjrp_prompt):
    """Sign-off section telling Cora not to close with fluff must be present."""
    assert "Sign-off" in hjrp_prompt
