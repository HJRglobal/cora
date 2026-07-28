"""Tests for the partitioned binary KB index (Slice 2-1, 2026-07-28).

knowledge_vec_bin_v2 has `entity` as a vec0 PARTITION KEY. The coarse scan uses the
IDENTICAL SQL as the legacy metadata-column table (empirically verified on 0.1.9), so for
a small DB (chunk count < coarse_k, all candidates fetched) the partitioned path must
reproduce the legacy path's results EXACTLY. Invariants under test:

  * fast (legacy bin) == partitioned (v2) results: same ids, same distances
  * entity isolation + FNDR co-scan + FNDR-only shapes preserved
  * strict LEX sub_entity scoping preserved on the partitioned path
  * upsert dual-writes to both bin tables while v2 exists (no drift); delete removes both
  * checkpoint gating: unarmed -> legacy table; armed -> v2

Embeddings mocked offline with deterministic, well-separated vectors.
"""

import hashlib

import pytest

from cora.knowledge_base import embeddings, schema
from cora.knowledge_base.store import (
    Document, KnowledgeBase, _LEGACY_BIN, _V2_BIN, _PARTITION_READY_KEY,
)

_DIM = 1536


def _vec_for(text: str) -> list[float]:
    h = hashlib.sha256(text.encode()).digest()
    vec = [0.0] * _DIM
    for i in range(12):
        vec[(h[i] * 7 + i * 131) % _DIM] = (h[i] / 255.0) + 0.25
    return vec


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts: [_vec_for(t) for t in texts])
    monkeypatch.setattr(embeddings, "embed_query", _vec_for)


@pytest.fixture
def kb(tmp_path):
    db = KnowledgeBase(tmp_path / "part_kb.db")
    yield db
    db.close()


def _doc(source_id, entity, content, **kw) -> Document:
    return Document(source="test", source_id=source_id, entity=entity,
                    content=content, title=f"doc {source_id}", **kw)


def _create_v2(kb: KnowledgeBase) -> None:
    kb._conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {_V2_BIN} USING vec0("
        f"chunk_id TEXT PRIMARY KEY, entity TEXT PARTITION KEY, embedding bit[{schema.EMBEDDING_DIM}])"
    )
    kb._conn.commit()


def _arm(kb: KnowledgeBase) -> None:
    kb.set_checkpoint(_PARTITION_READY_KEY, {"ready": True})
    kb._partition_ready = None      # force re-read
    kb._search_bin = None           # force re-resolve of the search table


def _seed(kb, n=8, **kw):
    docs = []
    for ent in ("F3E", "FNDR", "OSN", "LEX"):
        for i in range(n):
            docs.append(_doc(f"{ent}-{i}", ent, f"{ent} content {i} about widgets revenue", **kw))
    kb.upsert_documents(docs)


def _count(kb, tbl):
    return kb._conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]


def _ids(results):
    return [r.chunk_id for r in results]


# ── table selection gating ──────────────────────────────────────────────────────
def test_unarmed_uses_legacy_table(kb):
    _create_v2(kb)
    _seed(kb)
    assert kb._bin_search_table() == _LEGACY_BIN


def test_armed_uses_v2_table(kb):
    _create_v2(kb)
    _seed(kb)
    _arm(kb)
    assert kb._bin_search_table() == _V2_BIN


def test_armed_but_v2_absent_falls_back_to_legacy(kb):
    _seed(kb)
    _arm(kb)                       # armed, but v2 was never created
    assert kb._bin_search_table() == _LEGACY_BIN


# ── dual-write / dual-delete keep bin tables in sync ──────────────────────────────
def test_upsert_dual_writes_both_bin_tables(kb):
    _create_v2(kb)
    _seed(kb)
    f32 = _count(kb, "knowledge_vec_f32")
    assert _count(kb, _LEGACY_BIN) == f32
    assert _count(kb, _V2_BIN) == f32


def test_v2_absent_writes_only_legacy(kb):
    _seed(kb)                      # v2 never created
    assert _count(kb, _LEGACY_BIN) == _count(kb, "knowledge_vec_f32")
    assert not kb._table_exists(_V2_BIN)


def test_replace_on_conflict_syncs_both(kb):
    _create_v2(kb)
    kb.upsert_documents([_doc("dup", "F3E", "first version widgets")])
    kb.upsert_documents([_doc("dup", "F3E", "second version widgets revenue")])  # replaces
    # one chunk per table (replace deleted the old chunk from BOTH bin tables)
    assert _count(kb, _LEGACY_BIN) == _count(kb, "knowledge_vec_f32") == _count(kb, _V2_BIN)


# ── equivalence: legacy path vs partitioned path ──────────────────────────────────
def test_partitioned_matches_legacy_ordering_and_distance(kb):
    _create_v2(kb)
    _seed(kb)
    q = "F3E content 3 about widgets revenue"
    legacy = kb.search(q, entity="F3E", k=10, max_age_days=None)      # unarmed -> legacy
    _arm(kb)
    part = kb.search(q, entity="F3E", k=10, max_age_days=None)        # armed -> v2
    assert _ids(legacy) == _ids(part)
    for a, b in zip(legacy, part):
        assert abs(a.distance - b.distance) < 1e-9


def test_partitioned_entity_isolation_and_fndr_coscan(kb):
    _create_v2(kb)
    _seed(kb)
    _arm(kb)
    res = kb.search("content widgets revenue", entity="F3E", k=50, max_age_days=None)
    ents = {r.entity for r in res}
    assert ents <= {"F3E", "FNDR"}                 # co-scan own + FNDR only
    assert "OSN" not in ents and "LEX" not in ents


def test_partitioned_fndr_only_shape(kb):
    _create_v2(kb)
    _seed(kb)
    _arm(kb)
    res = kb.search("content widgets", entity="FNDR", k=50, max_age_days=None)
    assert {r.entity for r in res} == {"FNDR"}


def test_partitioned_no_fndr_when_disabled(kb):
    _create_v2(kb)
    _seed(kb)
    _arm(kb)
    res = kb.search("content widgets", entity="F3E", k=50, include_fndr=False, max_age_days=None)
    assert {r.entity for r in res} == {"F3E"}


# ── strict LEX sub_entity scoping preserved on the partitioned path ───────────────
def test_partitioned_lex_sub_entity_strict(kb):
    _create_v2(kb)
    kb.upsert_documents([
        _doc("llc-1", "LEX", "LLC service note widgets", sub_entity="LEX-LLC"),
        _doc("lts-1", "LEX", "LTS service note widgets", sub_entity="LEX-LTS"),
        _doc("gm-1", "LEX", "general LEX note widgets"),        # sub_entity NULL (GM-level)
    ])
    _arm(kb)
    llc = kb.search("service note widgets", entity="LEX", sub_entity="LEX-LLC",
                    include_fndr=False, max_age_days=None)
    got = {r.source_id for r in llc}
    assert "llc-1" in got                                       # own sub visible
    assert "lts-1" not in got                                   # sibling excluded (leak guard)
    assert "gm-1" not in got                                    # strict mode excludes NULL
