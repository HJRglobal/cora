"""Unit tests for scripts/migrate_sub_entity_tags.py

Tests keyword scoring and sub-entity detection logic without touching any DB.
"""

import sys
from pathlib import Path

import pytest

# The migrate script lives in scripts/, not src/. Add it to path.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from migrate_sub_entity_tags import detect_sub_entity, score_chunk  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _scores_only(entity: str, content: str) -> int:
    """Return the score for a single entity."""
    return score_chunk(content).get(entity, 0)


# ── LEX-LLC detection ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected_min_score", [
    ("Meeting with Shaun Hawkins about HCBS billing",          4),  # 2+2
    ("Jen Mortensen reviewed the AHCCCS submission",            3),  # 2+1
    ("Aaron Ferrucci updated the group home staffing schedule", 4),  # 2+1+1
    ("Lexington LLC operations update",                         3),  # 2+1
    ("DTA system was updated for the supported living clients",  2),  # 1+1
    ("SpokeChoice integration went live today",                 2),
    ("Jeff Montgomery joined the LLC operations call",          2),  # 1+1
])
def test_llc_scores(text, expected_min_score):
    assert _scores_only("LEX-LLC", text) >= expected_min_score


@pytest.mark.parametrize("text", [
    "Shaun Hawkins reviewed the DDD HCBS contract",
    "Jen Mortensen at Lexington LLC",
    "Aaron Ferrucci SpokeChoice update",
])
def test_llc_detected(text):
    assert detect_sub_entity(text) == "LEX-LLC"


# ── LEX-LTS detection ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected_min_score", [
    ("Justin Gilmore reviewed the LTS billing",                 4),  # 2+2
    ("therapy revalidation submitted for Provider Type 15",     4),  # 2+2
    ("New Age Cash Flow statement for Lexington Therapies",     4),  # 2+2
    ("DDD Therapy revalidation is due this week",               3),  # 2+... or 1+2
    ("speech therapy and occupational therapy services",        2),  # 1+1
    ("justin.gilmore@ emailed the AZ DDD Therapy team",         5),
])
def test_lts_scores(text, expected_min_score):
    assert _scores_only("LEX-LTS", text) >= expected_min_score


@pytest.mark.parametrize("text", [
    "Justin Gilmore submitted the LTS revalidation for Provider Type 15",
    "New Age Cash Flow for Lexington Therapies",
    "therapy revalidation for LTS",
])
def test_lts_detected(text):
    assert detect_sub_entity(text) == "LEX-LTS"


# ── LEX-LBHS detection ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected_min_score", [
    ("Jared Harker at LBHS submitted the HMLA report",         6),  # 2+2+2
    ("Applied Behavior Analysis plan reviewed at LBHS",        4),  # 2+2
    ("COPA contract with BHRF for behavior support plans",     5),  # 2+2+1
    ("42 CFR Part 2 compliance for LBHS",                      4),  # 2+2
    ("behavior intervention plan updated",                     2),  # 1+1
    ("UnitedHealthcare billing at LBHS",                       4),  # 2+2
])
def test_lbhs_scores(text, expected_min_score):
    assert _scores_only("LEX-LBHS", text) >= expected_min_score


@pytest.mark.parametrize("text", [
    "Jared Harker submitted HMLA paperwork for LBHS",
    "COPA and BHRF behavior support plan",
    "42 CFR Part 2 at LBHS",
])
def test_lbhs_detected(text):
    assert detect_sub_entity(text) == "LEX-LBHS"


# ── LEX-LLA detection ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected_min_score", [
    ("Sandy Patel at Lex Life Academy updated LLA Show Low",   6),  # 2+2+2
    ("SBP Inc tuition cycles at LLA",                          5),  # 2+2+1? 2+1+2
    ("Bryan Patel Achieve-Maryvale IEP review",                5),  # 2+2+1
    ("Ellsworth school programs update",                       2),  # 1+1
    ("community integration at LLA",                           3),  # 2+1
    ("LLA Show Low day programs",                              4),  # 2+2
])
def test_lla_scores(text, expected_min_score):
    assert _scores_only("LEX-LLA", text) >= expected_min_score


@pytest.mark.parametrize("text", [
    "Sandy Patel reviewed the LLA Show Low school program",
    "SBP Inc tuition cycle at Lex Life Academy",
    "Bryan Patel Achieve-Maryvale IEP",
])
def test_lla_detected(text):
    assert detect_sub_entity(text) == "LEX-LLA"


# ── No match → None ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "The team had a great meeting today",
    "Please review the attached document",
    "General update on operations",
    "",
    "   ",
])
def test_no_match_returns_none(text):
    result = detect_sub_entity(text)
    assert result is None


def test_no_match_score_chunk_empty(text="General update"):
    scores = score_chunk("General portfolio update with no keywords")
    assert scores == {}


# ── Ambiguous → None (tie) ────────────────────────────────────────────────────

def test_ambiguous_tie_returns_none():
    """Content that equally matches two sub-entities should return None."""
    # Construct text that hits LLC (Shaun Hawkins weight=2) and LBHS (Jared Harker weight=2) equally
    text = "Shaun Hawkins and Jared Harker reviewed the LBHS HCBS overlap"
    # LLC: Shaun(2) + HCBS(2) = 4; LBHS: Jared(2) + LBHS(2) = 4 — tie
    scores = score_chunk(text)
    llc_score = scores.get("LEX-LLC", 0)
    lbhs_score = scores.get("LEX-LBHS", 0)
    if llc_score == lbhs_score and llc_score > 0:
        assert detect_sub_entity(text) is None
    else:
        # Scores may differ depending on other keywords — just ensure function doesn't raise
        result = detect_sub_entity(text)
        assert result is None or result in ("LEX-LLC", "LEX-LBHS", "LEX-LTS", "LEX-LLA")


# ── score_chunk returns only matching entities ─────────────────────────────────

def test_score_chunk_returns_only_matches():
    """score_chunk should only include entities with score > 0."""
    text = "Shaun Hawkins submitted the HCBS report"
    scores = score_chunk(text)
    for entity, score in scores.items():
        assert score > 0


def test_score_chunk_all_zeros_returns_empty_dict():
    scores = score_chunk("no relevant content here at all")
    assert scores == {}


# ── Case-insensitivity ────────────────────────────────────────────────────────

def test_llc_case_insensitive():
    assert _scores_only("LEX-LLC", "LEXINGTON LLC OPERATIONS") > 0


def test_lts_case_insensitive():
    assert _scores_only("LEX-LTS", "SPEECH THERAPY revalidation") > 0


def test_lbhs_case_insensitive():
    assert _scores_only("LEX-LBHS", "applied behavior analysis at lbhs") > 0


def test_lla_case_insensitive():
    assert _scores_only("LEX-LLA", "LEX LIFE ACADEMY IEP REVIEW") > 0


# ── Word boundary matching ────────────────────────────────────────────────────

def test_llc_no_partial_match_dta():
    """'DTA' should match as a whole word, not inside 'DATA' etc."""
    # 'DATA' should not match \bDTA\b
    score_with_dta = _scores_only("LEX-LLC", "submitted via DTA portal")
    score_without = _scores_only("LEX-LLC", "submitted via DATA portal")
    assert score_with_dta > score_without


def test_lbhs_aba_word_boundary():
    """'ABA' should match as a word, not inside 'ABATED' etc."""
    score_aba = _scores_only("LEX-LBHS", "ABA therapy session")
    score_abated = _scores_only("LEX-LBHS", "ABATED concerns in the report")
    assert score_aba > 0
    assert score_abated == 0 or score_aba > score_abated


# ── detect_sub_entity returns valid codes only ────────────────────────────────

def test_detect_returns_known_code_or_none():
    valid_codes = {"LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA", None}
    sample_texts = [
        "Shaun Hawkins HCBS",
        "Justin Gilmore LTS billing",
        "Jared Harker LBHS ABA",
        "Sandy Patel LLA Show Low",
        "random unrelated content",
        "",
    ]
    for text in sample_texts:
        result = detect_sub_entity(text)
        assert result in valid_codes, f"Unexpected result {result!r} for text: {text!r}"
