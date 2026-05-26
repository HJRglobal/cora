"""Unit tests for scripts/capture_decisions.py."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from capture_decisions import (  # type: ignore  # noqa: E402
    _fingerprint,
    build_slack_message,
    deduplicate,
    extract_decision_sentences,
    load_surfaced,
    save_surfaced,
    score_sentence,
)


# ── score_sentence() ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,min_score", [
    ("We have decided to cancel the July 10th event.", 3),
    ("Going forward, Tessa is part-time remote.",     2),
    ("We locked down the F3 Pure launch date.",       2),
    ("The deal is confirmed and approved.",           2),
    ("We will not be pursuing UFL this quarter.",     2),
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
])
def test_score_sentence_low_or_zero(text):
    assert score_sentence(text) < 2


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
    import capture_decisions as cd
    monkeypatch.setattr(cd, "MAX_DECISIONS_PER_POST", 3)
    candidates = [
        {"entity": "F3E", "title": f"Meeting {i}", "sentence": f"We decided to do thing {i}.",
         "fingerprint": f"fp{i}", "date_created": None, "deep_link": ""}
        for i in range(10)
    ]
    msg = build_slack_message(candidates, 3)
    assert "more" in msg.lower()  # overflow indicator
