"""Tests for the Haiku LLM-as-judge classifier (Haiku call mocked).

The disambiguation guard (F3 Nation must NOT count as an F3 Energy hit) is the
headline case per the build brief acceptance criteria.
"""

from __future__ import annotations

import json

import pytest

from cora.ai_visibility import classifier as clf
from cora.ai_visibility.prompts import Brand, Prompt

ENERGY = Brand(
    key="energy",
    brand_name="F3 Energy",
    aliases=("F3 Energy", "F3Energy", "F3 energy drink"),
    disambiguation="Beverage brand (functional energy drink). NOT F3 Nation fitness, NOT Formula 3.",
    positioning="premium functional energy",
    competitor_set=("Red Bull", "Monster", "Celsius", "Alani Nu", "Ghost"),
    prompts=(),
)
PROMPT = Prompt(id="ENG-D01", text="What's the best functional energy drink in 2026?",
                intent="discovery", aided=False, brand="energy")


def _patch_judge(monkeypatch, payload):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(clf, "_judge_raw", lambda _p: raw)


def test_clean_hit_counts(monkeypatch):
    _patch_judge(monkeypatch, {
        "mentioned": True, "is_correct_brand": True, "position": 1,
        "sentiment": "positive", "competitors_mentioned": ["Celsius", "Ghost"],
        "cited_sources": ["https://f3energy.com"],
    })
    c = clf.classify_answer(ENERGY, PROMPT, "F3 Energy is the top pick, ahead of Celsius and Ghost.")
    assert c.is_hit is True
    assert c.position == 1
    assert c.sentiment == "positive"
    assert c.competitors_mentioned == ["Celsius", "Ghost"]
    assert c.cited_sources == ["https://f3energy.com"]
    assert c.error is None


def test_disambiguation_f3_nation_not_counted(monkeypatch):
    """A namesake ('F3 Nation' fitness) is detected but is_correct_brand=false ->
    NOT a hit -> excluded from presence."""
    _patch_judge(monkeypatch, {
        "mentioned": True, "is_correct_brand": False, "position": 2,
        "sentiment": "positive", "competitors_mentioned": [], "cited_sources": [],
    })
    c = clf.classify_answer(ENERGY, PROMPT, "F3 Nation is a great free workout group.")
    assert c.mentioned is True
    assert c.is_correct_brand is False
    assert c.is_hit is False
    # non-hit -> position/sentiment neutralized
    assert c.position is None
    assert c.sentiment == "neutral"


def test_not_mentioned(monkeypatch):
    _patch_judge(monkeypatch, {
        "mentioned": False, "is_correct_brand": False, "position": None,
        "sentiment": "neutral", "competitors_mentioned": ["Red Bull"], "cited_sources": [],
    })
    c = clf.classify_answer(ENERGY, PROMPT, "Red Bull and Monster dominate the category.")
    assert c.is_hit is False
    assert c.competitors_mentioned == ["Red Bull"]  # competitors still captured when absent


def test_fenced_json_is_parsed(monkeypatch):
    _patch_judge(monkeypatch, "```json\n" + json.dumps({
        "mentioned": True, "is_correct_brand": True, "position": 3,
        "sentiment": "neutral", "competitors_mentioned": [], "cited_sources": [],
    }) + "\n```")
    c = clf.classify_answer(ENERGY, PROMPT, "Options include ... F3 Energy.")
    assert c.is_hit is True
    assert c.position == 3


def test_competitor_normalization_maps_and_drops(monkeypatch):
    _patch_judge(monkeypatch, {
        "mentioned": True, "is_correct_brand": True, "position": 1,
        "sentiment": "positive",
        "competitors_mentioned": ["celsius", "RED BULL", "Prime", "Poppi"],  # last two not in set
        "cited_sources": [],
    })
    c = clf.classify_answer(ENERGY, PROMPT, "answer")
    # canonical-cased, filtered to the brand's competitor_set, deduped
    assert c.competitors_mentioned == ["Celsius", "Red Bull"]


def test_sentiment_clamped_to_valid_set(monkeypatch):
    _patch_judge(monkeypatch, {
        "mentioned": True, "is_correct_brand": True, "position": 1,
        "sentiment": "ecstatic", "competitors_mentioned": [], "cited_sources": [],
    })
    c = clf.classify_answer(ENERGY, PROMPT, "answer")
    assert c.sentiment == "neutral"


def test_position_bool_and_bad_values_coerced(monkeypatch):
    _patch_judge(monkeypatch, {
        "mentioned": True, "is_correct_brand": True, "position": True,  # bool, not a rank
        "sentiment": "positive", "competitors_mentioned": [], "cited_sources": [],
    })
    c = clf.classify_answer(ENERGY, PROMPT, "answer")
    assert c.position is None


def test_citation_fallback_to_connector_when_judge_empty(monkeypatch):
    _patch_judge(monkeypatch, {
        "mentioned": True, "is_correct_brand": True, "position": 1,
        "sentiment": "positive", "competitors_mentioned": [], "cited_sources": [],
    })
    c = clf.classify_answer(ENERGY, PROMPT, "answer",
                            answer_citations=["https://f3energy.com", "bad", "https://x.com"])
    # judge returned no cited_sources -> fall back to (cleaned) connector citations
    assert c.cited_sources == ["https://f3energy.com", "https://x.com"]


def test_empty_answer_short_circuits_no_judge(monkeypatch):
    def _boom(_p):
        raise AssertionError("judge must not be called for an empty answer")

    monkeypatch.setattr(clf, "_judge_raw", _boom)
    c = clf.classify_answer(ENERGY, PROMPT, "   ")
    assert c.is_hit is False
    assert c.error is None


def test_api_error_fails_closed(monkeypatch):
    def _boom(_p):
        raise RuntimeError("anthropic 500")

    monkeypatch.setattr(clf, "_judge_raw", _boom)
    c = clf.classify_answer(ENERGY, PROMPT, "F3 Energy is great.")
    assert c.is_hit is False
    assert c.mentioned is False
    assert "anthropic 500" in (c.error or "")


def test_unparseable_output_fails_closed(monkeypatch):
    _patch_judge(monkeypatch, "I cannot answer that as JSON, sorry!")
    c = clf.classify_answer(ENERGY, PROMPT, "F3 Energy is great.")
    assert c.is_hit is False
    assert "unparseable" in (c.error or "")


def test_missing_key_fails_closed(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Real _judge_raw path (no monkeypatch) -> raises -> fail-closed
    c = clf.classify_answer(ENERGY, PROMPT, "F3 Energy is great.")
    assert c.is_hit is False
    assert c.error is not None


def test_build_prompt_includes_disambiguation_and_competitors():
    p = clf.build_prompt(ENERGY, PROMPT, "some answer")
    assert "F3 Nation" in p  # disambiguation carried in
    assert "Celsius" in p
    assert "best functional energy drink" in p
