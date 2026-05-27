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
