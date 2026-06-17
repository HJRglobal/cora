"""Unit tests for scripts/capture_decisions.py."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import capture_decisions as cd  # type: ignore  # noqa: E402
from capture_decisions import (  # type: ignore  # noqa: E402
    _fingerprint,
    _strip_speaker,
    build_slack_message,
    deduplicate,
    extract_decision_sentences,
    load_surfaced,
    save_surfaced,
    score_sentence,
    verify_decisions_with_haiku,
)


# ── score_sentence() ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,min_score", [
    ("We have decided to cancel the July 10th event.", 3),
    ("Going forward, Tessa is part-time remote.",     2),
    ("We locked down the F3 Pure launch date.",       2),
    ("This is pivoting to a new approach.",           2),
    ("F3E shipping today, let's go.",                 2),
])
def test_score_sentence_high(text, min_score):
    assert score_sentence(text) >= min_score


@pytest.mark.parametrize("text", [
    "The weather in Phoenix is nice today.",
    "Please review the attached document.",
    "Let me know if you have questions.",
    "Thanks!",
    # Single weak signal no longer qualifies (down-weighted to 1) — these were
    # the exact garbage that flooded the 7am digest.
    "And so we will.",
    "Verify confirmed.",
    "Yep, verify confirmed.",
    "We won't touch it unless we use it or we won't pay unless we touch it.",
])
def test_score_sentence_low_or_zero(text):
    assert score_sentence(text) < 2


def test_two_weak_signals_qualify():
    """Two independent weak signals together clear the bar (sum >= 2)."""
    # "confirmed" (1) + "cancelled" (1) = 2
    assert score_sentence("The order was confirmed and then cancelled.") >= 2


# ── extract_decision_sentences() ─────────────────────────────────────────────

def test_extract_finds_decisions():
    text = (
        "We had a great meeting today. We have decided to lock down F3 Pure for June 15th. "
        "The team was aligned. Going forward, no more changes to the launch date. "
        "Sandy said hi."
    )
    decisions = extract_decision_sentences(text)
    assert len(decisions) >= 2
    assert any("decided" in d.lower() or "going forward" in d.lower() for d in decisions)


def test_extract_skips_short_sentences():
    text = "Yes. No. OK. We have decided to ship F3 Pure on June 15th."
    decisions = extract_decision_sentences(text)
    # Short sentences ("Yes.", "No.", "OK.") should be filtered out
    for d in decisions:
        assert len(d) >= 20


def test_extract_skips_very_long_sentences():
    # >500 char sentence should be excluded
    long = "We have decided to do this " + ("word " * 100) + "."
    decisions = extract_decision_sentences(long)
    for d in decisions:
        assert len(d) <= 500


def test_extract_empty_text():
    assert extract_decision_sentences("") == []


# ── _fingerprint() ───────────────────────────────────────────────────────────

def test_fingerprint_deterministic():
    assert _fingerprint("Hello World") == _fingerprint("Hello World")


def test_fingerprint_case_insensitive():
    assert _fingerprint("HELLO WORLD") == _fingerprint("hello world")


def test_fingerprint_12_chars():
    fp = _fingerprint("test sentence")
    assert len(fp) == 12


def test_fingerprint_different_texts():
    a = _fingerprint("We decided to ship")
    b = _fingerprint("We decided not to ship")
    assert a != b


# ── load_surfaced() and save_surfaced() ───────────────────────────────────────

def test_load_surfaced_empty(tmp_path):
    result = load_surfaced(tmp_path / "nonexistent.jsonl")
    assert result == set()


def test_save_and_load_surfaced(tmp_path):
    path = tmp_path / "surfaced.jsonl"
    fingerprints = ["abc123def456", "111222333444", "aaabbbcccddd"]
    save_surfaced(path, fingerprints)

    loaded = load_surfaced(path)
    assert loaded == set(fingerprints)


def test_save_surfaced_appends(tmp_path):
    path = tmp_path / "surfaced.jsonl"
    save_surfaced(path, ["fp1", "fp2"])
    save_surfaced(path, ["fp3"])
    loaded = load_surfaced(path)
    assert loaded == {"fp1", "fp2", "fp3"}


# ── deduplicate() ────────────────────────────────────────────────────────────

def test_deduplicate_removes_surfaced():
    candidates = [
        {"fingerprint": "aaa", "sentence": "We decided X"},
        {"fingerprint": "bbb", "sentence": "Going forward Y"},
        {"fingerprint": "ccc", "sentence": "Locked down Z"},
    ]
    already = {"aaa", "ccc"}
    result = deduplicate(candidates, already)
    assert len(result) == 1
    assert result[0]["fingerprint"] == "bbb"


def test_deduplicate_removes_internal_duplicates():
    candidates = [
        {"fingerprint": "dup", "sentence": "We decided X"},
        {"fingerprint": "dup", "sentence": "We decided X"},
        {"fingerprint": "unique", "sentence": "Going forward Y"},
    ]
    result = deduplicate(candidates, set())
    fps = [c["fingerprint"] for c in result]
    assert fps.count("dup") == 1
    assert "unique" in fps


def test_deduplicate_all_new():
    candidates = [
        {"fingerprint": "a", "sentence": "Decision A"},
        {"fingerprint": "b", "sentence": "Decision B"},
    ]
    result = deduplicate(candidates, set())
    assert len(result) == 2


# ── build_slack_message() ────────────────────────────────────────────────────

def test_build_slack_message_empty():
    msg = build_slack_message([], 3)
    assert "No new decision signals" in msg


def test_build_slack_message_with_candidates():
    candidates = [
        {"entity": "F3E", "title": "F3 Weekly 5/22", "sentence": "We have decided to lock down June 15.",
         "fingerprint": "abc", "date_created": None, "deep_link": ""},
        {"entity": "OSN", "title": "OSN Finance 5/22", "sentence": "Going forward, monthly reviews.",
         "fingerprint": "def", "date_created": None, "deep_link": "https://app.fireflies.ai/meeting/123"},
    ]
    msg = build_slack_message(candidates, 3)
    assert "F3E" in msg
    assert "OSN" in msg
    assert "decided" in msg.lower() or "June" in msg
    assert "Going forward" in msg


def test_build_slack_message_caps_at_max(monkeypatch):
    """More than MAX candidates should still produce a valid message."""
    monkeypatch.setattr(cd, "MAX_DECISIONS_PER_POST", 3)
    candidates = [
        {"entity": "F3E", "title": f"Meeting {i}", "sentence": f"We decided to do thing {i}.",
         "fingerprint": f"fp{i}", "date_created": None, "deep_link": ""}
        for i in range(10)
    ]
    msg = build_slack_message(candidates, 3)
    assert "more" in msg.lower()  # overflow indicator


# ── _strip_speaker() + speaker-aware extraction ──────────────────────────────

def test_strip_speaker_removes_prefix():
    assert _strip_speaker("[Harrison Rogers] We decided to ship.") == "We decided to ship."
    assert _strip_speaker("[Justin Moran] Verify confirmed.") == "Verify confirmed."


def test_strip_speaker_noop_without_prefix():
    assert _strip_speaker("We decided to ship.") == "We decided to ship."


def test_extract_strips_speaker_prefix():
    text = "[Harrison Rogers] We have decided to lock down the launch for June 15th."
    decisions = extract_decision_sentences(text)
    assert decisions, "should still extract the decision"
    assert not decisions[0].startswith("["), "speaker prefix must be stripped"


def test_extract_filters_short_backchannel():
    """The exact garbage from the 7am digest must not survive extraction."""
    garbage = (
        "[Justin Moran] Verify confirmed. [Justin Moran] Yep, verify confirmed. "
        "[Harrison Rogers] And so we will."
    )
    assert extract_decision_sentences(garbage) == []


def test_extract_min_word_count():
    # Scores >=3 (decision:) and >=20 chars, but only 4 words -> filtered.
    assert extract_decision_sentences("Decision: cancel it now.") == []


# ── normalized fingerprint / near-duplicate dedup ────────────────────────────

def test_fingerprint_strips_speaker():
    assert _fingerprint("[Harrison Rogers] We decided to ship.") == _fingerprint("We decided to ship.")


def test_fingerprint_collapses_punctuation_near_dupes():
    a = "We won't touch it unless we use it or we won't pay unless we touch it."
    b = "We won't touch it unless we use it, or we won't pay unless we touch it."
    assert _fingerprint(a) == _fingerprint(b)


def test_deduplicate_collapses_near_duplicates():
    a = "We won't touch it unless we use it or we won't pay unless we touch it."
    b = "We won't touch it unless we use it, or we won't pay unless we touch it."
    candidates = [
        {"fingerprint": _fingerprint(a), "sentence": a},
        {"fingerprint": _fingerprint(b), "sentence": b},
    ]
    assert len(deduplicate(candidates, set())) == 1


# ── verify_decisions_with_haiku() ────────────────────────────────────────────

def _haiku_response(text: str):
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    return resp


def _candidates():
    return [
        {"entity": "FNDR", "sentence": "We decided to cancel the July 10 event.", "fingerprint": "a"},
        {"entity": "FNDR", "sentence": "Verify confirmed.", "fingerprint": "b"},
    ]


def test_verify_keeps_only_decisions(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cands = _candidates()
    verdict_json = json.dumps([
        {"index": 0, "is_decision": True, "summary": "Cancel the July 10 event."},
        {"index": 1, "is_decision": False},
    ])
    with patch.object(cd, "anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = _haiku_response(verdict_json)
        kept = verify_decisions_with_haiku(cands)
    assert len(kept) == 1
    assert kept[0]["sentence"] == "Cancel the July 10 event."   # summary substituted
    assert kept[0]["raw_sentence"] == "We decided to cancel the July 10 event."
    # both were evaluated (so the rejected one can be recorded + suppressed)
    assert all(c.get("_haiku_evaluated") for c in cands)


def test_verify_strips_code_fences(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    fenced = "```json\n" + json.dumps([{"index": 0, "is_decision": True, "summary": "Ship it."}]) + "\n```"
    cands = [{"entity": "FNDR", "sentence": "We decided to ship.", "fingerprint": "a"}]
    with patch.object(cd, "anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = _haiku_response(fenced)
        kept = verify_decisions_with_haiku(cands)
    assert len(kept) == 1


def test_verify_extracts_array_from_prose(monkeypatch):
    """Haiku sometimes wraps the JSON array in explanation — extract it anyway."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    prose = (
        "Here are the results:\n"
        + json.dumps([{"index": 0, "is_decision": True, "summary": "Ship it."},
                      {"index": 1, "is_decision": False}])
        + "\nLet me know if you need anything else."
    )
    cands = _candidates()
    with patch.object(cd, "anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = _haiku_response(prose)
        kept = verify_decisions_with_haiku(cands)
    assert len(kept) == 1
    assert kept[0]["sentence"] == "Ship it."


def test_verify_fail_open_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cands = _candidates()
    assert verify_decisions_with_haiku(cands) == cands  # unchanged


def test_verify_fail_open_on_api_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cands = _candidates()
    with patch.object(cd, "anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.side_effect = RuntimeError("503")
        kept = verify_decisions_with_haiku(cands)
    assert kept == cands  # fail-open: never silently lose decisions


def test_verify_fail_open_on_bad_json(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cands = _candidates()
    with patch.object(cd, "anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = _haiku_response("not json at all")
        assert verify_decisions_with_haiku(cands) == cands


def test_verify_empty_input():
    assert verify_decisions_with_haiku([]) == []


# ── _post_enabled (audit N2/N3: decision-capture posting muted by default) ────

@pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", " true "])
def test_post_enabled_true(monkeypatch, value):
    monkeypatch.setenv("DECISION_CAPTURE_POST_ENABLED", value)
    assert cd._post_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "  "])
def test_post_enabled_false(monkeypatch, value):
    monkeypatch.setenv("DECISION_CAPTURE_POST_ENABLED", value)
    assert cd._post_enabled() is False


def test_post_disabled_by_default(monkeypatch):
    monkeypatch.delenv("DECISION_CAPTURE_POST_ENABLED", raising=False)
    assert cd._post_enabled() is False


# ── _is_nondecision (Phase 1.5 precision pre-filter) ─────────────────────────

@pytest.mark.parametrize("text", [
    "Should we cancel the July 10 event?",
    "Going forward, should we ship on Friday?",
    "We will ship if the margins improve.",
    "We're considering remote-only work.",
    "Maybe we lock the date next week.",
    "Let's discuss the launch date.",
    "We might pivot to a new vendor.",
])
def test_is_nondecision_true(text):
    assert cd._is_nondecision(text) is True


@pytest.mark.parametrize("text", [
    "We have decided to cancel the July 10 event.",
    "Going forward, Tessa is part-time remote.",
    "We are going with vendor X.",
    "The launch is locked for June 15.",
])
def test_is_nondecision_false(text):
    assert cd._is_nondecision(text) is False


def test_extract_rejects_question_despite_score():
    # "going forward," scores 2 + "cancel" 1 = 3, but the question form is not a decision.
    assert extract_decision_sentences("Going forward, should we cancel the event?") == []


def test_extract_rejects_contingency_despite_score():
    # "going forward," (2) + "we will" (1) = 3, but the "if" makes it hypothetical.
    assert extract_decision_sentences("Going forward, we will ship if the margins improve.") == []


def test_extract_keeps_real_decision_over_prefilter():
    out = extract_decision_sentences("Going forward, we are going with the lower-cost vendor.")
    assert any("going with" in d.lower() for d in out)


def test_verify_prompt_anchored_with_examples():
    assert "KEEP" in cd._VERIFY_PROMPT and "REJECT" in cd._VERIFY_PROMPT
    assert "?" in cd._VERIFY_PROMPT  # includes a question reject example
