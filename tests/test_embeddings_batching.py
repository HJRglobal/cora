"""Tests for token-aware embedding batching (embeddings.py, 2026-06-08).

Root issue: embed_texts batched purely by count (BATCH_SIZE=100). A batch of
100 dense chunks can exceed OpenAI's 300,000-token-per-request limit and 400,
which previously dropped large spreadsheets (e.g. Rita Tracking.xlsx) from the
KB. embed_texts now caps each batch by BOTH count and a token budget.

Under conftest's fake tiktoken encoder, one token == one character, so token
budgets here are expressed in characters.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.knowledge_base import embeddings as emb


def test_count_tokens_uses_encoder():
    # Fake tiktoken in conftest counts one token per character.
    assert emb._count_tokens("hello") == 5
    assert emb._count_tokens("") == 0


def test_count_cap_splits_at_batch_size():
    batches = list(emb._iter_token_batches(["x"] * 250))
    assert [len(b) for b in batches] == [emb.BATCH_SIZE, emb.BATCH_SIZE, 50]


def test_token_cap_splits_under_budget():
    # Each text ~100K tokens; budget 250K -> two per batch, then remainder.
    size = 100_000
    batches = list(emb._iter_token_batches(["a" * size] * 5))
    assert [len(b) for b in batches] == [2, 2, 1]
    # Every batch stays within the token budget.
    for b in batches:
        assert sum(emb._count_tokens(t) for t in b) <= emb.MAX_BATCH_TOKENS


def test_single_oversized_input_is_its_own_batch():
    # A single input larger than the budget must never be silently dropped.
    huge = "a" * (emb.MAX_BATCH_TOKENS + 50_000)
    batches = list(emb._iter_token_batches([huge]))
    assert len(batches) == 1
    assert len(batches[0]) == 1


def test_small_then_oversized_splits():
    small = "x" * 10
    huge = "a" * (emb.MAX_BATCH_TOKENS + 50_000)
    batches = list(emb._iter_token_batches([small, huge]))
    assert [len(b) for b in batches] == [1, 1]


def test_empty_input():
    assert list(emb._iter_token_batches([])) == []


def test_order_and_completeness_preserved():
    texts = [f"t{i}" for i in range(305)]
    flat = [t for b in emb._iter_token_batches(texts) for t in b]
    assert flat == texts
