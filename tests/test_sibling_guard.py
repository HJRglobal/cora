"""Unit tests for sibling_guard.check_redirect().

The sibling guard is a deterministic pre-LLM data-isolation boundary that
prevents LEX sub-entity channels from receiving information about sibling
sub-entities. These tests cover the routing logic, false-positive avoidance,
and the format of the generated redirect message.
"""

import pytest

from cora.sibling_guard import check_redirect


# ── Non-LEX entities / GM-level LEX ──────────────────────────────────────────

def test_non_lex_entity_returns_none():
    assert check_redirect("F3E", "Tell me about LLA") is None
    assert check_redirect("OSN", "What is LTS revenue?") is None
    assert check_redirect("FNDR", "LBHS compliance status?") is None
    assert check_redirect("HJRG", "LTS census") is None


def test_gm_level_lex_not_scoped_returns_none():
    # Bare "LEX" entity has no sub-entity scoping — all siblings visible
    assert check_redirect("LEX", "What's LLA enrollment?") is None
    assert check_redirect("LEX", "LBHS census") is None


# ── LEX-LLC redirects ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected_name,expected_code", [
    ("What's LLA's enrollment numbers?", "Lex Life Academy", "lla"),
    ("Lex Life Academy tuition schedule?", "Lex Life Academy", "lla"),
    ("LEX-LLA performance?", "Lex Life Academy", "lla"),
    ("LBHS compliance status?", "Lexington Behavioral Health Services", "lbhs"),
    ("What's the Lexington Behavioral Health census?", "Lexington Behavioral Health Services", "lbhs"),
    ("Behavioral Health headcount?", "Lexington Behavioral Health Services", "lbhs"),
    ("LTS revenue last month?", "Lexington Therapies", "lts"),
    ("Lexington Therapies staff plan?", "Lexington Therapies", "lts"),
])
def test_llc_channel_redirects(message, expected_name, expected_code):
    result = check_redirect("LEX-LLC", message)
    assert result is not None
    assert expected_name in result
    assert f"#{expected_code}-" in result
    assert "Lexington LLC" in result  # self-scope in closing clause


# ── LEX-LTS redirects ────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected_name", [
    ("LLA student headcount?", "Lex Life Academy"),
    ("What's going on with LBHS?", "Lexington Behavioral Health Services"),
    ("Lexington LLC revenue this quarter?", "Lexington LLC"),
])
def test_lts_channel_redirects(message, expected_name):
    result = check_redirect("LEX-LTS", message)
    assert result is not None
    assert expected_name in result
    assert "Lexington Therapies" in result  # self-scope


# ── LEX-LBHS redirects ───────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected_name", [
    ("LLA enrollment?", "Lex Life Academy"),
    ("LTS staff plan?", "Lexington Therapies"),
    ("Lexington LLC cap table?", "Lexington LLC"),
])
def test_lbhs_channel_redirects(message, expected_name):
    result = check_redirect("LEX-LBHS", message)
    assert result is not None
    assert expected_name in result
    assert "Lexington Behavioral Health Services" in result  # self-scope


# ── LEX-LLA redirects ────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected_name", [
    ("LBHS census numbers?", "Lexington Behavioral Health Services"),
    ("LTS headcount?", "Lexington Therapies"),
    ("Lexington LLC ownership?", "Lexington LLC"),
])
def test_lla_channel_redirects(message, expected_name):
    result = check_redirect("LEX-LLA", message)
    assert result is not None
    assert expected_name in result
    assert "Lex Life Academy" in result  # self-scope


# ── False positives that must NOT trigger ────────────────────────────────────

def test_villa_does_not_trigger_lla_redirect():
    # "VILLA" contains the letters LLA but not at a word boundary
    assert check_redirect("LEX-LLC", "The villa project timeline?") is None
    assert check_redirect("LEX-LLC", "VILLA renovations") is None


def test_standalone_llc_does_not_redirect_in_lts_channel():
    # The LTS pattern for Lexington LLC requires the full "LEXINGTON LLC" phrase,
    # not bare "LLC" (which is too common a term)
    assert check_redirect("LEX-LTS", "We need an LLC agreement here") is None
    assert check_redirect("LEX-LTS", "File it under LLC") is None


def test_lexington_alone_does_not_redirect():
    # "Lexington" without the qualifying noun doesn't match any sibling pattern
    assert check_redirect("LEX-LTS", "What's happening in Lexington this week?") is None
    assert check_redirect("LEX-LLC", "Lexington market overview") is None


def test_unrelated_message_returns_none():
    assert check_redirect("LEX-LLC", "What are the open Asana tasks?") is None
    assert check_redirect("LEX-LTS", "What's on my calendar today?") is None
    assert check_redirect("LEX-LBHS", "Summary of last week's P&L") is None
    assert check_redirect("LEX-LLA", "Draft an email to Harrison") is None


# ── Case insensitivity ────────────────────────────────────────────────────────

def test_lowercase_keywords_still_match():
    result = check_redirect("LEX-LLC", "what is lbhs doing?")
    assert result is not None
    assert "Lexington Behavioral Health Services" in result


def test_mixed_case_lla():
    result = check_redirect("LEX-LLC", "lla enrollment")
    assert result is not None
    assert "Lex Life Academy" in result


# ── Redirect message structure ────────────────────────────────────────────────

def test_redirect_message_is_complete_sentence():
    result = check_redirect("LEX-LLC", "LTS revenue?")
    assert isinstance(result, str)
    assert result.strip().endswith(".")
    # No newlines — should be a single sentence delivered as-is
    assert "\n" not in result


def test_redirect_message_contains_channel_wildcard():
    # Channel reference should use the -* wildcard pattern so users know any
    # subtype of that channel is acceptable
    result = check_redirect("LEX-LLC", "LTS revenue?")
    assert "-*" in result


def test_redirect_does_not_expose_sibling_data():
    # The redirect text must name the sibling entity but must NOT contain any
    # financial or operational data — just the name + channel pointer
    result = check_redirect("LEX-LLC", "LLA tuition revenue?")
    assert result is not None
    assert "$" not in result
    assert "%" not in result


# -- NDA hard block: scope + ordering (cq-12bd309c93a8) ----------------------
#
# Live 2026-08-19 13:14:38, #llc-leadership (entity LEX-LLC): an ask naming the
# LBHS COPA transcripts got "ask in an #lbhs-* channel" -- a pointer at material
# purged 2026-07-21 with a permanent title-level ingest exclusion. The block was
# gated on LEX-LBHS, so it never ran in the sibling channels at all.
#
# The FIRST fix widened the whole LBHS term list family-wide, and the D-051
# review caught that as a worse error than the one it fixed: BHRF is an
# AHCCCS/ADHS licensure category all over the DDD manuals that D-046 ingested on
# purpose, and UnitedHealthcare is Lexington's own group-health carrier. Both
# are ordinary LEX vocabulary, and the widened refusal claimed "I don't hold it
# in any channel" about a corpus Cora holds thousands of chunks of. Only COPA
# goes family-wide, and only COPA gets the "nowhere" sentence.

_NDA_ASK = "what do the LBHS COPA diligence transcripts say?"
_LEX_SCOPES = ["LEX", "LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA"]
_SIBLING_SCOPES = ["LEX", "LEX-LLC", "LEX-LTS", "LEX-LLA"]


@pytest.mark.parametrize("entity", _LEX_SCOPES)
def test_copa_blocks_across_the_whole_lex_family(entity):
    result = check_redirect(entity, _NDA_ASK)
    assert result is not None
    assert "COPA" in result


@pytest.mark.parametrize("entity", _LEX_SCOPES)
def test_the_copa_refusal_never_points_at_another_channel(entity):
    """The defect was the POINTER, not the refusal: material that exists
    nowhere must not be described as living somewhere else."""
    result = check_redirect(entity, _NDA_ASK)
    assert "#lbhs" not in result
    assert "-*" not in result
    assert "any channel" in result


@pytest.mark.parametrize("entity", _SIBLING_SCOPES)
def test_copa_beats_the_sibling_redirect_on_ordering(entity):
    """Both guards match a message that names COPA and a sibling. The hard
    block has to win, in every LEX scope, whichever sibling is named."""
    for ask in (_NDA_ASK,
                "pull the Lexington Behavioral COPA file",
                "did the LTS team see the COPA diligence?"):
        result = check_redirect(entity, ask)
        assert result is not None
        assert "COPA" in result, (entity, ask)


@pytest.mark.parametrize("entity", ["F3E", "OSN", "FNDR", "HJRG", "UFL", ""])
def test_the_copa_block_is_not_portfolio_wide(entity):
    """LEX-scoped on purpose. What this closes is a MISLEADING POINTER, not a
    leak -- the content is purged, so a non-LEX channel that reaches the model
    finds nothing and says so. NOTE: the first version of this test's docstring
    claimed cross_entity_guard redirects such an ask into LEX where it "meets
    this block there". It does not -- that guard returns its own complete
    response and app.py posts it and RETURNS. The assertion was always just
    this one; the reasoning beside it is now the real one."""
    assert check_redirect(entity, _NDA_ASK) is None


# -- the terms that are NOT NDA codenames -------------------------------------

@pytest.mark.parametrize("entity", _SIBLING_SCOPES)
@pytest.mark.parametrize("ask", [
    "what is the BHRF continued-stay documentation requirement?",
    "what are the BHRF admission exclusionary criteria?",
    "when is the UnitedHealthcare invoice due?",
    "did the UHC eligibility file go out?",
])
def test_bhrf_and_the_payer_stay_answerable_outside_lbhs(entity, ask):
    """The regression the D-051 review caught. These are DDD-manual and
    benefits-invoice questions -- the DDD manuals were ingested under D-046 so
    Shaun's team could get exactly these answers. Blocking them in #llc-* /
    #lts-* / #lex-* refuses the corpus its own purpose."""
    assert check_redirect(entity, ask) is None


@pytest.mark.parametrize("ask", [
    "BHRF admission criteria?",
    "what did UnitedHealthcare say?",
    "UHC renewal timing?",
])
def test_bhrf_and_the_payer_are_still_blocked_inside_lbhs(ask):
    """Unchanged from before this branch -- lbhs.md forbids surfacing them in
    LBHS scope, and that wording ("cannot be discussed here") is TRUE there."""
    result = check_redirect("LEX-LBHS", ask)
    assert result is not None
    assert "confidential to LBHS" in result


def test_an_ordinary_sibling_ask_still_gets_its_pointer():
    """The block must not swallow the redirect it now outranks."""
    result = check_redirect("LEX-LLC", "how did LBHS do this quarter?")
    assert result is not None
    assert "#lbhs-*" in result
    assert "confidential" not in result.lower()


@pytest.mark.parametrize("entity", ["LEX-LLC", "LEX-LBHS"])
def test_both_refusals_still_count_as_deflections(entity):
    """gap_detection vetoes gap logging by matching refusal PHRASES. An
    unmatched refusal would file NDA'd content as a knowledge gap. Both
    wordings must match, not just the one that changed."""
    from cora import gap_detection
    ask = _NDA_ASK if entity == "LEX-LLC" else "BHRF admission criteria?"
    assert gap_detection.is_deflection(check_redirect(entity, ask)) is True
