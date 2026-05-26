"""Unit tests for semantic_cache.SemanticCache.

Uses an in-memory SQLite DB (via tmp_path fixture) to avoid touching real data.
"""

import struct
import time
import uuid
from pathlib import Path

import pytest

from cora.semantic_cache import (
    DEFAULT_TTL,
    MAX_ENTRIES_PER_ENTITY,
    SIMILARITY_THRESHOLD,
    SemanticCache,
    _dot,
    _pack,
    _unpack,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

DIM = 8  # Small dimension for tests — real embeddings are 1536


def _vec(values: list[float]) -> list[float]:
    """Return a unit-normalised vector from raw values (L2 norm)."""
    mag = sum(v * v for v in values) ** 0.5
    if mag == 0:
        return values
    return [v / mag for v in values]


def _identical_vec() -> list[float]:
    """A fixed unit vector — identical to itself (similarity = 1.0)."""
    return _vec([1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])


def _orthogonal_vec() -> list[float]:
    """A unit vector orthogonal to _identical_vec (similarity = 0.0)."""
    return _vec([0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])


def _near_vec(eps: float = 0.001) -> list[float]:
    """A unit vector very close to _identical_vec — similarity > 0.95."""
    return _vec([1.0, eps, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])


def _far_vec() -> list[float]:
    """A unit vector somewhat different — similarity ~ 0.7071 (below 0.95)."""
    return _vec([1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])


@pytest.fixture
def cache(tmp_path: Path) -> SemanticCache:
    db = tmp_path / "test_cache.db"
    c = SemanticCache(db)
    yield c
    c.close()


# ── Serialisation round-trips ─────────────────────────────────────────────────

def test_pack_unpack_round_trip():
    original = [0.1, 0.2, -0.3, 0.999, -1.0]
    assert _unpack(_pack(original)) == pytest.approx(original, abs=1e-6)


def test_dot_identical():
    v = _identical_vec()
    assert _dot(v, v) == pytest.approx(1.0, abs=1e-6)


def test_dot_orthogonal():
    a = _identical_vec()
    b = _orthogonal_vec()
    assert _dot(a, b) == pytest.approx(0.0, abs=1e-6)


# ── Table creation ────────────────────────────────────────────────────────────

def test_cache_table_created(cache: SemanticCache):
    rows = cache._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='semantic_cache'"
    ).fetchall()
    assert len(rows) == 1


# ── Store and retrieve (hit) ──────────────────────────────────────────────────

def test_lookup_hit_identical_embedding(cache: SemanticCache):
    vec = _identical_vec()
    cache.store("F3E", "what's the tagline?", vec, "Real energy for real life.", ttl_seconds=3600)

    result = cache.lookup("F3E", vec)
    assert result == "Real energy for real life."


def test_lookup_hit_near_embedding(cache: SemanticCache):
    """A very close (but not identical) vector should still hit."""
    stored_vec = _identical_vec()
    query_vec = _near_vec(eps=0.001)

    cache.store("F3E", "tagline?", stored_vec, "Real energy for real life.", ttl_seconds=3600)
    result = cache.lookup("F3E", query_vec)
    assert result is not None


def test_lookup_miss_orthogonal_embedding(cache: SemanticCache):
    """An orthogonal vector should miss (similarity = 0 < 0.95)."""
    stored_vec = _identical_vec()
    query_vec = _orthogonal_vec()

    cache.store("F3E", "tagline?", stored_vec, "Some answer.", ttl_seconds=3600)
    result = cache.lookup("F3E", query_vec)
    assert result is None


def test_lookup_miss_far_embedding(cache: SemanticCache):
    """A vector with similarity ~0.707 should miss."""
    stored_vec = _identical_vec()
    query_vec = _far_vec()

    cache.store("F3E", "tagline?", stored_vec, "Some answer.", ttl_seconds=3600)
    result = cache.lookup("F3E", query_vec)
    assert result is None


# ── TTL expiry ────────────────────────────────────────────────────────────────

def test_lookup_miss_expired_ttl(cache: SemanticCache):
    """An entry with ttl_seconds=0 is already expired."""
    vec = _identical_vec()
    # Manually insert with created_at in the past so it's expired
    now = int(time.time()) - 10
    cache._conn.execute(
        """INSERT INTO semantic_cache
             (cache_id, entity, question, embedding, response, created_at, ttl_seconds, hit_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
        (str(uuid.uuid4()), "F3E", "q", _pack(vec), "answer", now, 5),
    )
    cache._conn.commit()
    result = cache.lookup("F3E", vec)
    assert result is None


def test_lookup_hit_not_yet_expired(cache: SemanticCache):
    """A freshly stored entry should still be live."""
    vec = _identical_vec()
    cache.store("F3E", "q", vec, "answer", ttl_seconds=3600)
    assert cache.lookup("F3E", vec) == "answer"


# ── Entity scoping ────────────────────────────────────────────────────────────

def test_lookup_misses_wrong_entity(cache: SemanticCache):
    """Cache is entity-scoped — F3E answer should not be returned for OSN."""
    vec = _identical_vec()
    cache.store("F3E", "tagline?", vec, "F3E answer.", ttl_seconds=3600)

    result = cache.lookup("OSN", vec)
    assert result is None


def test_lookup_correct_entity_returned(cache: SemanticCache):
    vec = _identical_vec()
    cache.store("F3E", "q", vec, "F3E answer.", ttl_seconds=3600)
    cache.store("OSN", "q", vec, "OSN answer.", ttl_seconds=3600)

    assert cache.lookup("F3E", vec) == "F3E answer."
    assert cache.lookup("OSN", vec) == "OSN answer."


# ── hit_count increment ───────────────────────────────────────────────────────

def test_hit_count_increments_on_lookup(cache: SemanticCache):
    vec = _identical_vec()
    cache.store("F3E", "q", vec, "answer", ttl_seconds=3600)

    cache.lookup("F3E", vec)
    cache.lookup("F3E", vec)

    row = cache._conn.execute(
        "SELECT hit_count FROM semantic_cache WHERE entity = 'F3E'"
    ).fetchone()
    assert row[0] == 2


# ── Empty cache ───────────────────────────────────────────────────────────────

def test_lookup_empty_cache_returns_none(cache: SemanticCache):
    result = cache.lookup("F3E", _identical_vec())
    assert result is None


# ── invalidate_entity ─────────────────────────────────────────────────────────

def test_invalidate_entity_clears_entries(cache: SemanticCache):
    vec = _identical_vec()
    cache.store("F3E", "q", vec, "answer", ttl_seconds=3600)
    cache.store("F3E", "q2", _near_vec(), "answer2", ttl_seconds=3600)
    cache.store("OSN", "q", vec, "osn answer", ttl_seconds=3600)

    deleted = cache.invalidate_entity("F3E")
    assert deleted == 2
    assert cache.lookup("F3E", vec) is None
    # OSN untouched
    assert cache.lookup("OSN", vec) == "osn answer"


# ── stats() ───────────────────────────────────────────────────────────────────

def test_stats_returns_per_entity(cache: SemanticCache):
    vec = _identical_vec()
    cache.store("F3E", "q", vec, "answer", ttl_seconds=3600)
    cache.lookup("F3E", vec)

    stats = cache.stats()
    assert "F3E" in stats
    assert stats["F3E"]["entries"] == 1
    assert stats["F3E"]["total_hits"] == 1


def test_stats_empty_cache(cache: SemanticCache):
    assert cache.stats() == {}


# ── _prune: excess entries trimmed ───────────────────────────────────────────

def test_prune_caps_entity_entries(cache: SemanticCache):
    """Insert MAX+5 entries; after prune only MAX should remain."""
    limit = MAX_ENTRIES_PER_ENTITY
    # Override constant locally — insert limit + 5 entries
    for i in range(limit + 5):
        vec = _vec([float(i + 1)] + [0.0] * (DIM - 1))
        cache.store("F3E", f"q{i}", vec, f"answer{i}", ttl_seconds=3600)

    count = cache._conn.execute(
        "SELECT COUNT(*) FROM semantic_cache WHERE entity = 'F3E'"
    ).fetchone()[0]
    assert count <= limit


# ── Best match wins ───────────────────────────────────────────────────────────

def test_best_match_returned_among_multiple(cache: SemanticCache):
    """When multiple entries exceed threshold, the closest one is returned."""
    base = _identical_vec()
    near = _near_vec(eps=0.001)
    # Store two answers — base is closer to itself
    cache.store("F3E", "exact q", base, "exact answer", ttl_seconds=3600)
    cache.store("F3E", "near q", near, "near answer", ttl_seconds=3600)

    result = cache.lookup("F3E", base)
    assert result == "exact answer"
