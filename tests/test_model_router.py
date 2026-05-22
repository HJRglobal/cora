"""Unit tests for model_router.choose_model() — Haiku vs Sonnet selection."""

import pytest

from cora.model_router import (
    LONG_MESSAGE_CHAR_THRESHOLD,
    MODEL_HAIKU,
    MODEL_SONNET,
    choose_model,
    is_haiku,
    short_label,
)


# ---- Short / simple queries → Haiku ----


@pytest.mark.parametrize("msg", [
    "what's on my plate?",
    "show me my tasks",
    "what's Tommy's open work?",
    "list my deals",
    "do I have any meetings today?",
    "am I free Friday?",
    "what's Shaun working on",
    "get me the latest f3 pure brand guidelines",
    "find the contract with Tierra Brandt",
])
def test_simple_lookups_route_to_haiku(msg):
    assert choose_model(msg) == MODEL_HAIKU


# ---- Reasoning / analysis indicators → Sonnet ----


@pytest.mark.parametrize("msg", [
    "analyze the OSN April financials",
    "compare F3E sales this quarter vs last",
    "recommend what we should do about Vine & Branches",
    "should I sign the new vendor contract?",
    "explain why the AHCCCS audit is taking so long",
    "what's the strategy for Pure launch?",
    "deep dive on UFL sponsorship pipeline",
    "thorough breakdown of OSN G Warner",
    "everything about the Allen Flavors situation",
    "break down the gsheets migration",
    "pros and cons of Cloudflare Tunnel for Cora",
    "what are the tradeoffs of using Haiku for everything?",
    "push back on my plan for Q3",
    "draft an email to Shaun about the audit",
    "draft a reply to Mitch",
    "draft a post about Pure launch",
    "draft a memo for the board",
])
def test_reasoning_queries_route_to_sonnet(msg):
    assert choose_model(msg) == MODEL_SONNET


# ---- Length-based routing ----


def test_long_messages_route_to_sonnet():
    # Construct a message longer than the threshold
    long_msg = "x " * (LONG_MESSAGE_CHAR_THRESHOLD // 2 + 5)
    assert len(long_msg.strip()) > LONG_MESSAGE_CHAR_THRESHOLD
    assert choose_model(long_msg) == MODEL_SONNET


def test_short_message_at_threshold_uses_haiku():
    # Exactly at threshold — still Haiku (strict greater-than)
    msg = "a" * LONG_MESSAGE_CHAR_THRESHOLD
    assert choose_model(msg) == MODEL_HAIKU


# ---- Edge cases ----


def test_empty_message_defaults_to_sonnet():
    assert choose_model("") == MODEL_SONNET


def test_whitespace_only_defaults_to_sonnet():
    assert choose_model("   \n   ") == MODEL_SONNET


def test_indicator_with_punctuation_still_matches():
    assert choose_model("analyze, please") == MODEL_SONNET
    assert choose_model("Should I cancel?") == MODEL_SONNET


def test_word_boundary_avoids_false_positives():
    # "analyzer" should NOT match \banaly[sz]e\b (boundary protects this)
    assert choose_model("show me the analyzer report") == MODEL_HAIKU
    # "comparable" does NOT match \bcompare\b
    assert choose_model("find me a comparable deal") == MODEL_HAIKU


def test_drafting_overrides_short_length():
    # Short message but mentions drafting → Sonnet
    assert choose_model("draft an email to Jen") == MODEL_SONNET


# ---- Convenience helpers ----


def test_short_label_haiku():
    assert short_label(MODEL_HAIKU) == "haiku"


def test_short_label_sonnet():
    assert short_label(MODEL_SONNET) == "sonnet"


def test_short_label_unknown():
    assert short_label("claude-opus-7") == "claude-opus-7"


def test_is_haiku():
    assert is_haiku(MODEL_HAIKU) is True
    assert is_haiku(MODEL_SONNET) is False
