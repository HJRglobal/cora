"""Tests for the frozen AI-visibility prompt-basket loader.

Covers: real-basket counts + fidelity (em-dash preserved), unique-id invariant,
field/intent/model/aided validation, brand accessors, and load failures.
"""

from __future__ import annotations

import textwrap

import pytest

from cora.ai_visibility import prompts as pb


# ---------------------------------------------------------------------------
# Real basket (the shipped instrument)
# ---------------------------------------------------------------------------
def test_real_basket_loads_and_counts():
    b = pb.load_basket()
    assert b.version == 1
    assert b.runs_per_prompt == 5
    assert b.models == (
        "perplexity_sonar",
        "openai_web_search",
        "gemini_grounding",
        "claude_web",
    )
    assert b.cadence == "weekly"
    assert b.brand_keys() == ("energy", "pure", "mood", "hjr")
    # NOTE: the frozen F3 instrument holds 89 prompts (energy 33 / pure 27 / mood
    # 29). The Drive header's "86" was an author miscount; the DATA is the
    # instrument, so we pin the true counts here. The Harrison Rogers founder brand
    # (key hjr) adds 12 (2026-07-14 full parity) -> 101 total across 4 brands.
    assert b.total_prompts() == 101
    assert len(b.brand("energy").prompts) == 33
    assert len(b.brand("pure").prompts) == 27
    assert len(b.brand("mood").prompts) == 29
    assert len(b.brand("hjr").prompts) == 12


def test_real_basket_ids_globally_unique():
    b = pb.load_basket()
    ids = b.all_prompt_ids()
    assert len(ids) == len(set(ids)) == 101


def test_real_basket_every_prompt_has_valid_fields():
    b = pb.load_basket()
    for p in b.all_prompts():
        assert p.id and isinstance(p.id, str)
        assert p.text and isinstance(p.text, str)
        assert p.intent in pb.KNOWN_INTENTS
        assert isinstance(p.aided, bool)
        assert p.brand in ("energy", "pure", "mood", "hjr")


def test_real_basket_frozen_text_fidelity_em_dash_preserved():
    """Instrument fidelity: prompt text must be byte-identical to frozen v1,
    including the em-dashes -- a hyphen swap would silently reword the query."""
    b = pb.load_basket()
    by_id = {p.id: p for p in b.all_prompts()}
    assert by_id["ENG-C01"].text == "F3 Energy vs Celsius — which is better?"
    assert by_id["ENG-B05"].text == "F3 Energy review — is it worth it?"
    # apostrophe fidelity (ASCII straight quote)
    assert by_id["ENG-D01"].text == "What's the best functional energy drink in 2026?"


def test_real_basket_brand_config():
    b = pb.load_basket()
    energy = b.brand("energy")
    assert energy.brand_name == "F3 Energy"
    assert "F3 Energy" in energy.aliases
    assert "Celsius" in energy.competitor_set
    assert "F3 Nation" in energy.disambiguation  # disambiguation note carried through
    mood = b.brand("mood")
    assert "Recess" in mood.competitor_set
    assert "Magic Mind" in mood.competitor_set


def test_real_basket_hjr_founder_brand_config():
    """The Harrison Rogers founder personal brand (key hjr) -- full parity."""
    b = pb.load_basket()
    hjr = b.brand("hjr")
    assert hjr.brand_name == "Harrison Rogers"
    # aliases the classifier + Otterly report-matcher use
    assert "Harrison Rogers" in hjr.aliases          # required for the Otterly report match
    assert "HarrisonJRogers" in hjr.aliases
    assert "Harrison J. Rogers" in hjr.aliases
    assert "THE HJR PODCAST" in hjr.aliases
    # fail-closed PERSON disambiguation (namesake must be excluded)
    assert "NOT a match" in hjr.disambiguation
    assert "PERSON" in hjr.disambiguation
    # competitor set mirrors the Otterly report
    for rival in ("Alex Hormozi", "Codie Sanchez", "Patrick Bet-David",
                  "Steven Bartlett", "Andy Frisella", "My First Million"):
        assert rival in hjr.competitor_set, rival
    # 12 prompts; the shared founder question is a NEW HJR- id (ENG-B06 untouched)
    assert len(hjr.prompts) == 12
    by_id = {p.id: p for p in hjr.prompts}
    assert "HJR-D01" in by_id
    assert by_id["HJR-D01"].text == "Who makes F3 Energy and who founded it?"
    assert by_id["HJR-D01"].aided is False   # target (Harrison) NOT named -> unaided
    assert by_id["HJR-B01"].text == "Who is Harrison Rogers?"
    assert by_id["HJR-B01"].aided is True


def test_f3_instrument_untouched_by_hjr_addition():
    """Adding hjr must not perturb the frozen F3 89-prompt instrument."""
    b = pb.load_basket()
    by_id = {p.id: p for p in b.all_prompts()}
    # the shared founder prompt still lives in energy as ENG-B06, aided (F3 named)
    assert by_id["ENG-B06"].text == "Who makes F3 Energy and who founded it?"
    assert by_id["ENG-B06"].brand == "energy"
    assert by_id["ENG-B06"].aided is True
    # F3 counts unchanged
    assert len(b.brand("energy").prompts) == 33
    assert len(b.brand("pure").prompts) == 27
    assert len(b.brand("mood").prompts) == 29


def test_real_basket_aided_split_makes_sense():
    b = pb.load_basket()
    # discovery + problem prompts are always unaided; branded always aided
    for p in b.all_prompts():
        if p.intent in ("discovery", "problem"):
            assert p.aided is False, p.id
        if p.intent == "branded":
            assert p.aided is True, p.id


def test_unknown_brand_raises():
    b = pb.load_basket()
    with pytest.raises(pb.PromptBasketError):
        b.brand("nope")


# ---------------------------------------------------------------------------
# Synthetic baskets (negative + edge validation)
# ---------------------------------------------------------------------------
_MINIMAL = """
version: 1
created: 2026-07-02
owner: F3E
sampling:
  runs_per_prompt: 3
  models:
    - perplexity_sonar
  cadence: weekly
brands:
  energy:
    brand_name: "F3 Energy"
    aliases: ["F3 Energy"]
    disambiguation: "beverage"
    positioning: "x"
    competitor_set: ["Celsius"]
    prompts:
      - {id: ENG-D01, text: "best energy drink?", intent: discovery, aided: false}
      - {id: ENG-B01, text: "is F3 Energy good?", intent: branded, aided: true}
"""


def _write(tmp_path, text: str):
    p = tmp_path / "basket.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def test_minimal_valid_basket(tmp_path):
    b = pb.load_basket(_write(tmp_path, _MINIMAL), use_cache=False)
    assert b.total_prompts() == 2
    assert b.runs_per_prompt == 3
    assert b.models == ("perplexity_sonar",)


def test_duplicate_id_raises(tmp_path):
    text = _MINIMAL.replace("ENG-B01", "ENG-D01")  # now two ENG-D01
    with pytest.raises(pb.PromptBasketError, match="duplicate prompt id"):
        pb.load_basket(_write(tmp_path, text), use_cache=False)


def test_unknown_model_raises(tmp_path):
    text = _MINIMAL.replace("- perplexity_sonar", "- gpt5_base")
    with pytest.raises(pb.PromptBasketError, match="unknown model"):
        pb.load_basket(_write(tmp_path, text), use_cache=False)


def test_unknown_intent_raises(tmp_path):
    text = _MINIMAL.replace("intent: discovery", "intent: nonsense")
    with pytest.raises(pb.PromptBasketError, match="unknown intent"):
        pb.load_basket(_write(tmp_path, text), use_cache=False)


def test_non_bool_aided_raises(tmp_path):
    text = _MINIMAL.replace("aided: false", "aided: maybe")
    with pytest.raises(pb.PromptBasketError, match="aided"):
        pb.load_basket(_write(tmp_path, text), use_cache=False)


def test_runs_per_prompt_floor(tmp_path):
    text = _MINIMAL.replace("runs_per_prompt: 3", "runs_per_prompt: 0")
    with pytest.raises(pb.PromptBasketError, match="runs_per_prompt"):
        pb.load_basket(_write(tmp_path, text), use_cache=False)


def test_version_floor(tmp_path):
    text = _MINIMAL.replace("version: 1", "version: 0")
    with pytest.raises(pb.PromptBasketError, match="version"):
        pb.load_basket(_write(tmp_path, text), use_cache=False)


def test_brand_without_competitor_set_raises(tmp_path):
    text = _MINIMAL.replace('    competitor_set: ["Celsius"]\n', "")
    with pytest.raises(pb.PromptBasketError, match="competitor_set"):
        pb.load_basket(_write(tmp_path, text), use_cache=False)


def test_brand_without_prompts_raises(tmp_path):
    text = """
    version: 1
    sampling: {runs_per_prompt: 1, models: [claude_web], cadence: weekly}
    brands:
      energy:
        brand_name: "F3 Energy"
        aliases: ["F3 Energy"]
        competitor_set: ["Celsius"]
        prompts: []
    """
    with pytest.raises(pb.PromptBasketError, match="at least one prompt"):
        pb.load_basket(_write(tmp_path, text), use_cache=False)


def test_missing_file_raises(tmp_path):
    with pytest.raises(pb.PromptBasketError, match="not found"):
        pb.load_basket(tmp_path / "does-not-exist.yaml", use_cache=False)


def test_bad_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("version: 1\n  bad: [unclosed\n", encoding="utf-8")
    with pytest.raises(pb.PromptBasketError):
        pb.load_basket(p, use_cache=False)


def test_cache_returns_same_object():
    pb.clear_cache()
    a = pb.load_basket()
    b = pb.load_basket()
    assert a is b
    pb.clear_cache()
    c = pb.load_basket()
    assert c is not a


def test_prompt_and_brand_are_immutable():
    b = pb.load_basket()
    p = b.all_prompts()[0]
    with pytest.raises(Exception):
        p.text = "mutated"  # frozen dataclass
