"""Tests for the binary-quantized KB fast path (store._search_binary).

The fast path (binary coarse hamming scan -> exact float32 L2 re-rank) must be
behaviourally identical to the float fallback for the entity / recency /
sub-entity filters. For a small DB (chunk count < coarse_k) the coarse scan
returns every candidate, so the exact re-rank reproduces the float ranking
EXACTLY — that equivalence is the core correctness guarantee under test here.

The binary-quantization recall tradeoff only appears at scale (coarse_k < total
matching rows); that is covered by scripts/bench_kb_search.py against the live DB.

Embeddings are mocked (offline) with deterministic, well-separated vectors.
"""

import hashlib

import pytest

from cora.knowledge_base import embeddings
from cora.knowledge_base.store import Document, KnowledgeBase, _BIN_READY_KEY

_DIM = 1536


def _vec_for(text: str) -> list[float]:
    """Deterministic, well-separated 1536-dim vector for a piece of text."""
    h = hashlib.sha256(text.encode()).digest()
    vec = [0.0] * _DIM
    for i in range(12):
        vec[(h[i] * 7 + i * 131) % _DIM] = (h[i] / 255.0) + 0.25
    return vec


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts: [_vec_for(t) for t in texts])
    monkeypatch.setattr(embeddings, "embed_query", _vec_for)


@pytest.fixture
def kb(tmp_path):
    db = KnowledgeBase(tmp_path / "bin_kb.db")
    yield db
    db.close()


def _doc(source_id, entity, content, **kw) -> Document:
    return Document(source="test", source_id=source_id, entity=entity,
                    content=content, title=f"doc {source_id}", **kw)


def _seed(kb, n_per_entity=8):
    docs = []
    for ent in ("F3E", "FNDR", "OSN", "LEX"):
        for i in range(n_per_entity):
            docs.append(_doc(f"{ent}-{i}", ent,
                             f"{ent} content number {i} about widgets and revenue"))
    kb.upsert_documents(docs)


def _mark_ready(kb):
    kb.set_checkpoint(_BIN_READY_KEY, {"ready": True})
    kb._bin_ready = None  # force re-read of the flag


def _ids(results):
    return [r.chunk_id for r in results]


# ── readiness gating ──────────────────────────────────────────────────────────

def test_not_ready_uses_float_path(kb):
    _seed(kb)
    assert kb._is_bin_index_ready() is False


def test_ready_flag_reflects_checkpoint(kb):
    _seed(kb)
    _mark_ready(kb)
    assert kb._is_bin_index_ready() is True


# ── fast == float equivalence ───────────────────────────────────────────────────

def test_fast_path_matches_float_path_ordering(kb):
    """With chunk count < coarse_k, the fast path must reproduce the float
    path's chunk_id ordering AND distances exactly."""
    _seed(kb)
    q = "F3E content number 3 about widgets and revenue"

    float_res = kb.search(q, entity="F3E", k=10, max_age_days=None)
    _mark_ready(kb)
    fast_res = kb.search(q, entity="F3E", k=10, max_age_days=None)

    assert _ids(fast_res) == _ids(float_res)
    for f, s in zip(float_res, fast_res):
        assert f.chunk_id == s.chunk_id
        assert s.distance == pytest.approx(f.distance, abs=1e-4)


def test_fast_path_entity_isolation(kb):
    """Fast path must only return the channel entity + FNDR, never siblings."""
    _seed(kb)
    _mark_ready(kb)
    res = kb.search("OSN content number 1", entity="OSN", k=20, max_age_days=None)
    ents = {r.entity for r in res}
    assert ents <= {"OSN", "FNDR"}
    assert "LEX" not in ents and "F3E" not in ents


def test_fast_path_excludes_fndr_when_disabled(kb):
    _seed(kb)
    _mark_ready(kb)
    res = kb.search("F3E content", entity="F3E", k=20,
                    max_age_days=None, include_fndr=False)
    assert {r.entity for r in res} == {"F3E"}


# ── sub-entity strict invariant (security) ──────────────────────────────────────

def test_fast_path_sub_entity_strict_matches_float(kb):
    """LEX sub-entity strict scoping must behave identically on both paths:
    NULL-tagged LEX chunks are excluded; only the tagged sub-entity passes."""
    docs = [
        _doc("llc-1", "LEX", "LLC tagged content alpha", sub_entity="LEX-LLC"),
        _doc("llc-2", "LEX", "LLC tagged content beta", sub_entity="LEX-LLC"),
        _doc("lla-1", "LEX", "LLA tagged content gamma", sub_entity="LEX-LLA"),
        _doc("gm-1", "LEX", "GM-level untagged content delta"),  # sub_entity NULL
    ]
    kb.upsert_documents(docs)
    q = "LLC tagged content alpha"

    float_res = kb.search(q, entity="LEX", k=10, max_age_days=None,
                          sub_entity="LEX-LLC")
    _mark_ready(kb)
    fast_res = kb.search(q, entity="LEX", k=10, max_age_days=None,
                         sub_entity="LEX-LLC")

    # Same results on both paths, and STRICT: no NULL/sibling leakage.
    assert _ids(fast_res) == _ids(float_res)
    returned = {r.chunk_id.split("-")[0] for r in fast_res}  # not meaningful; check source_id
    src_ids = {r.source_id for r in fast_res}
    assert src_ids <= {"llc-1", "llc-2"}
    assert "gm-1" not in src_ids and "lla-1" not in src_ids


# ── recency filter ──────────────────────────────────────────────────────────────

def test_fast_path_recency_filter(kb):
    import time
    now = int(time.time())
    old = now - (400 * 86400)
    recent = now - (10 * 86400)
    kb.upsert_documents([
        _doc("recent-1", "F3E", "fresh widget content", date_modified=recent),
        _doc("old-1", "F3E", "stale widget content", date_modified=old),
    ])
    _mark_ready(kb)
    res = kb.search("widget content", entity="F3E", k=10, max_age_days=365)
    src_ids = {r.source_id for r in res}
    assert "recent-1" in src_ids
    assert "old-1" not in src_ids


# ── bin + f32 kept in sync on upsert ────────────────────────────────────────────

def test_upsert_populates_bin_and_f32(kb):
    _seed(kb, n_per_entity=4)
    chunks = kb._conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
    bins = kb._conn.execute("SELECT COUNT(*) FROM knowledge_vec_bin").fetchone()[0]
    f32 = kb._conn.execute("SELECT COUNT(*) FROM knowledge_vec_f32").fetchone()[0]
    assert chunks == bins == f32 > 0


def test_get_shared_kb_is_singleton(tmp_path, monkeypatch):
    """Shared KB returns None with no db, then a cached singleton once it exists.
    Never touches the live data/cora_kb.db — _KB_DB_PATH is redirected to tmp."""
    from cora import context_loader
    monkeypatch.setattr(context_loader, "_shared_kb", None)
    monkeypatch.setattr(context_loader, "_KB_DB_PATH", tmp_path / "shared.db")

    assert context_loader.get_shared_kb() is None  # no db file yet

    KnowledgeBase(tmp_path / "shared.db").close()   # materialize the db
    a = context_loader.get_shared_kb()
    b = context_loader.get_shared_kb()
    assert a is not None and a is b
    a.close()


def test_reupsert_replaces_bin_and_f32(kb):
    kb.upsert_documents([_doc("dup", "F3E", "original content one two three")])
    kb.upsert_documents([_doc("dup", "F3E", "replacement content four five six")])
    # replace-on-conflict: exactly one logical doc's chunks remain in every table
    chunks = kb._conn.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE source_id='dup'").fetchone()[0]
    bins = kb._conn.execute("SELECT COUNT(*) FROM knowledge_vec_bin").fetchone()[0]
    f32 = kb._conn.execute("SELECT COUNT(*) FROM knowledge_vec_f32").fetchone()[0]
    assert chunks == bins == f32
