"""BDM entity — system prompt content + channel routing tests.

Verifies:
  1. Channel routing: #bdm, #bdm-*, and #media all route to BDM.
  2. System prompt loads without error.
  3. Locked guardrail phrases are present in the loaded prompt:
       - Role architecture (Harrison/internal marketing = creative)
       - Daniel Sion removal
       - Three-client F3 financial model ($2K / $6K)
       - BDM client confidentiality (Berry Divine, RedBull, McLaren, Lifted Trucks)
  4. Prompt does NOT contain known stale content (old $13K bundled model reference).

Commit: [BDM] tests/test_bdm_system_prompt.py — 2026-05-24
"""

import pytest

from cora.entity_router import route
from cora.prompt_loader import load_prompt, clear_cache


# ── Channel routing ──────────────────────────────────────────────────────────


def test_bdm_bare_routes_to_bdm():
    """Bare #bdm channel routes to BDM."""
    assert route("bdm") == "BDM"


def test_bdm_leadership_routes_to_bdm():
    assert route("bdm-leadership") == "BDM"


def test_bdm_finance_routes_to_bdm():
    assert route("bdm-finance") == "BDM"


def test_bdm_ops_routes_to_bdm():
    assert route("bdm-ops") == "BDM"


def test_media_channel_routes_to_bdm():
    """#media is BDM's cross-client production catch-all (C0B2Z7Z7C84). Added 2026-05-24."""
    assert route("media") == "BDM"


def test_media_does_not_leak_to_fndr():
    """Regression: before 2026-05-24, #media fell through to FNDR."""
    assert route("media") != "FNDR"


# ── Prompt loads ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_prompt_cache():
    """Clear the prompt cache before each test so edits to bdm.md are picked up."""
    clear_cache()
    yield
    clear_cache()


def _bdm_prompt() -> str:
    return load_prompt("BDM")


def test_bdm_prompt_loads():
    """BDM system prompt loads without raising."""
    prompt = _bdm_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 100


# ── Role architecture (LOCKED 2026-05-22) ────────────────────────────────────


def test_prompt_contains_role_architecture_lock():
    """Prompt must state Harrison + internal marketing own creative direction."""
    prompt = _bdm_prompt().lower()
    assert "harrison" in prompt
    assert "internal marketing" in prompt
    assert "production layer" in prompt


def test_prompt_never_suggests_bdm_owns_creative():
    """Cora must not suggest BDM iterates on creative direction."""
    prompt = _bdm_prompt()
    assert "NEVER suggest BDM iterate" in prompt or "NEVER propose BDM iterating" in prompt or "never suggest bdm" in prompt.lower()


# ── Daniel Sion removal (LOCKED 2026-05-22) ──────────────────────────────────


def test_prompt_contains_daniel_removal():
    """Prompt must explicitly block Daniel Sion from being proposed as executor."""
    prompt = _bdm_prompt()
    assert "Daniel Sion" in prompt


def test_prompt_blocks_daniel_as_owner():
    """The removal phrase must include 'never propose' or 'NEVER propose'."""
    prompt = _bdm_prompt()
    # Accept either case — the actual text uses "NEVER"
    assert "never propose daniel sion" in prompt.lower() or "must never propose daniel" in prompt.lower()


# ── Three-client F3 model (LOCKED 2026-05-19) ────────────────────────────────


def test_prompt_contains_f3_three_client_model():
    """Prompt must reference the $2K/mo per brand model."""
    prompt = _bdm_prompt()
    assert "$2,000" in prompt or "$2K" in prompt or "2,000/mo" in prompt


def test_prompt_contains_f3_total_billing():
    """Prompt must reference $6K/mo total."""
    prompt = _bdm_prompt()
    assert "$6,000" in prompt or "$6K" in prompt or "6,000/mo" in prompt


def test_prompt_does_not_contain_old_13k_model():
    """Old $13K bundled model must not appear — it's been replaced."""
    prompt = _bdm_prompt()
    assert "$13K" not in prompt
    assert "$13,000" not in prompt
    assert "13K" not in prompt


# ── BDM client confidentiality (LOCKED) ──────────────────────────────────────


def test_prompt_contains_external_client_list():
    """Prompt must name all four external BDM clients."""
    prompt = _bdm_prompt()
    assert "Berry Divine" in prompt
    assert "RedBull" in prompt or "Red Bull" in prompt
    assert "McLaren" in prompt
    assert "Lifted Trucks" in prompt


def test_prompt_contains_confidentiality_guardrail():
    """Prompt must instruct Cora not to discuss external clients outside BDM channels."""
    prompt = _bdm_prompt().lower()
    assert "confidential" in prompt or "never discuss" in prompt or "must never discuss" in prompt


def test_prompt_non_bdm_channel_redirect():
    """Prompt must provide a redirect pattern for non-BDM channel requests."""
    prompt = _bdm_prompt()
    assert "#bdm" in prompt  # redirect to bdm channels must be named


# ── F3 brand guidelines V1 (SHIPPED 2026-05-22) ──────────────────────────────


def test_prompt_contains_f3_brand_v1_shipped():
    """Prompt must reference the V1 brand guidelines being shipped to BDM."""
    prompt = _bdm_prompt()
    assert "brand-guidelines" in prompt or "brand guidelines" in prompt.lower()


def test_prompt_contains_production_handoff_date():
    """Prompt must reference the 5/26 production handoff meeting."""
    prompt = _bdm_prompt()
    assert "5/26" in prompt or "2026-05-26" in prompt or "May 26" in prompt


def test_prompt_contains_all_three_f3_sub_brands():
    """Prompt must name Pure, Mood, and Energy."""
    prompt = _bdm_prompt()
    assert "Pure" in prompt
    assert "Mood" in prompt
    assert "Energy" in prompt


# ── Cap table / OA structure ──────────────────────────────────────────────────


def test_prompt_contains_micah_kessler():
    """Prompt must name Micah Kessler (not just 'Micah') to avoid ambiguity."""
    prompt = _bdm_prompt()
    assert "Micah Kessler" in prompt


def test_prompt_contains_demi_bagby():
    """Prompt must name Demi Bagby."""
    prompt = _bdm_prompt()
    assert "Demi Bagby" in prompt or "Demi" in prompt


# ── UFL paused / Hannah weekly review ────────────────────────────────────────


def test_prompt_ufl_paused():
    """Prompt must state UFL is paused."""
    prompt = _bdm_prompt().lower()
    assert "ufl" in prompt
    assert "paused" in prompt


def test_prompt_hannah_weekly_review():
    """Prompt must attribute weekly BDM content review to Hannah (not Larry)."""
    prompt = _bdm_prompt()
    assert "Hannah" in prompt
    assert "weekly" in prompt.lower()
