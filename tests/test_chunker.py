"""Unit tests for knowledge_base.chunker.

The conftest.py fake tiktoken stub makes each Unicode code point count as one
token (len(text) == token count). Tests use that property to write assertions
about chunk boundaries and overlap without needing a real network connection.
"""

import pytest

from cora.knowledge_base.chunker import (
    HARD_MAX_TOKENS,
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    chunk_text,
    count_tokens,
)


# ── count_tokens ──────────────────────────────────────────────────────────────

def test_count_tokens_empty():
    assert count_tokens("") == 0


def test_count_tokens_equals_char_count():
    # With the fake tiktoken stub, each char == 1 token
    assert count_tokens("hello") == 5
    assert count_tokens("A" * 100) == 100


# ── Empty / whitespace inputs ─────────────────────────────────────────────────

def test_empty_string_returns_empty():
    assert chunk_text("") == []


def test_whitespace_only_returns_empty():
    assert chunk_text("   ") == []
    assert chunk_text("\n\n\t") == []


# ── Single short text → single chunk, unchanged ───────────────────────────────

def test_short_text_single_chunk():
    result = chunk_text("Hello world.")
    assert result == ["Hello world."]


def test_text_exactly_at_limit_is_one_chunk():
    # A single sentence with exactly chunk_tokens chars should not split
    text = "W" * DEFAULT_CHUNK_TOKENS + "."
    result = chunk_text(text)
    assert len(result) == 1


def test_single_long_sentence_stays_as_one_chunk():
    # One sentence longer than chunk_tokens but below HARD_MAX stays whole
    # (no mid-sentence split is possible — the sentence IS the only sentence)
    text = "X" * (DEFAULT_CHUNK_TOKENS + 100) + "."
    result = chunk_text(text)
    assert len(result) == 1
    assert "X" in result[0]


# ── Hard-max truncation ───────────────────────────────────────────────────────

def test_single_sentence_over_hard_max_is_truncated():
    # A sentence longer than HARD_MAX_TOKENS must be hard-truncated, not dropped
    text = "Y" * (HARD_MAX_TOKENS + 500)
    result = chunk_text(text)
    assert len(result) == 1
    assert len(result[0]) == HARD_MAX_TOKENS


def test_hard_max_truncation_preserves_content_up_to_limit():
    text = "A" * HARD_MAX_TOKENS + "B" * 500
    result = chunk_text(text)
    assert result[0] == "A" * HARD_MAX_TOKENS  # truncated before the B's


def test_previous_chunk_flushed_before_hard_max_sentence():
    # If there's accumulated content before a giant sentence, it should be flushed
    # as its own chunk before the giant sentence is hard-truncated
    short = "Short sentence. "  # under chunk_tokens
    giant = "Z" * (HARD_MAX_TOKENS + 100)
    text = short + giant
    result = chunk_text(text)
    # short sentence flushed first, giant sentence truncated second
    assert len(result) == 2
    assert "Short" in result[0]
    assert len(result[1]) == HARD_MAX_TOKENS


# ── Multi-sentence splitting ──────────────────────────────────────────────────

def test_two_large_sentences_produce_two_chunks():
    # Each sentence is ~300 chars; together 600 > chunk_tokens(500) → must split
    # The sentence-split regex fires on ". " followed by an uppercase letter
    sent1 = "A" * 298 + "."
    sent2 = "B" * 298 + "."
    text = sent1 + " " + sent2   # ". B" triggers the split
    result = chunk_text(text, overlap_tokens=0)
    assert len(result) == 2
    assert result[0] == sent1
    assert result[1] == sent2


def test_three_short_sentences_stay_in_fewer_chunks():
    # Three sentences of 150 chars each: 150+150=300 < 500, so two fit per chunk
    sent1 = "A" * 148 + "."
    sent2 = "B" * 148 + "."
    sent3 = "C" * 148 + "."
    text = sent1 + " " + sent2 + " " + sent3
    result = chunk_text(text, overlap_tokens=0)
    # sent1+sent2 = 300 < 500; adding sent3 makes 450 < 500 → all in one chunk
    assert len(result) == 1


def test_sentence_split_requires_uppercase_after_period():
    # If the character after ". " is lowercase, no sentence split fires
    # → single sentence, single chunk regardless of length
    text = "First sentence. second starts lowercase and goes on " + "X" * 200 + "."
    result = chunk_text(text)
    # No split fired — treated as one long sentence
    assert len(result) == 1


# ── Overlap ───────────────────────────────────────────────────────────────────

def test_overlap_carries_small_sentences_into_next_chunk():
    # Two tiny sentences (3 chars each) fit in the 50-char overlap window.
    # A large sentence forces a chunk boundary, after which the tiny sentences
    # reappear at the start of chunk 2 as overlap.
    tiny1 = "Aa."
    tiny2 = "Bb."
    large = "C" * 500 + "."
    # sentence split: "Aa." + " " + "Bb." + " " + "CCC..."
    text = tiny1 + " " + tiny2 + " " + large
    result = chunk_text(text)
    assert len(result) == 2
    # chunk 1: tiny1 + tiny2 (forced out when large sentence arrives)
    assert "Aa" in result[0]
    assert "Bb" in result[0]
    # chunk 2: overlap (tiny1+tiny2) prepended to large
    assert "Aa" in result[1]
    assert "Bb" in result[1]
    assert "C" in result[1]


def test_no_overlap_when_overlap_tokens_zero():
    sent1 = "Alpha end."   # 10 chars, small enough to fit in default overlap
    large = "B" * 500 + "."
    text = sent1 + " " + large
    result = chunk_text(text, overlap_tokens=0)
    assert len(result) == 2
    # Without overlap, sent1 must NOT appear in chunk 2
    assert "Alpha" not in result[1]


# ── Line-based fallback ───────────────────────────────────────────────────────

def test_line_based_fallback_when_no_sentence_terminators():
    # Text with newlines but no sentence-ending punctuation → lines become sentences
    text = "First line of content here\nSecond line of content here\nThird line is here"
    result = chunk_text(text, chunk_tokens=20, overlap_tokens=0)
    assert len(result) == 3
    assert "First" in result[0]
    assert "Second" in result[1]
    assert "Third" in result[2]


def test_line_based_fallback_short_lines_merge():
    # Very short lines all fit in one chunk
    text = "Line A\nLine B\nLine C"
    result = chunk_text(text, chunk_tokens=100)
    assert len(result) == 1
    assert "Line A" in result[0]
    assert "Line C" in result[0]


# ── Edge cases ───────────────────────────────────────────────────────────────

def test_single_word_no_punctuation():
    result = chunk_text("hello")
    assert result == ["hello"]


def test_only_punctuation():
    result = chunk_text("...")
    assert len(result) == 1


def test_very_small_custom_chunk_tokens():
    # Each sentence is 5 chars; chunk_tokens=4 means each goes in its own chunk
    text = "One. Two. Three."
    result = chunk_text(text, chunk_tokens=4, overlap_tokens=0)
    assert len(result) == 3


def test_chunk_text_preserves_all_content():
    # No content should be silently dropped (apart from hard-truncation)
    sent1 = "First sentence content. "
    sent2 = "Second sentence content. "
    sent3 = "Third sentence content."
    text = sent1 + sent2 + sent3
    joined = " ".join(chunk_text(text, overlap_tokens=0))
    for word in ["First", "Second", "Third"]:
        assert word in joined
