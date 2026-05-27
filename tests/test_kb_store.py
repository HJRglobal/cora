"""Unit tests for knowledge_base.store — KnowledgeBase operations.

Uses an in-memory SQLite DB (via tmp_path) and mocks out OpenAI embeddings so
tests run offline. The fake tiktoken stub (conftest.py) makes chunker work
without a network connection.

Key invariants under test:
  - upsert_documents is idempotent for the same (source, source_id)
  - search filters correctly by entity and sub_entity
  - STRICT MODE sub_entity filter excludes NULL-tagged rows (data-isolation
    invariant for LEX sub-entity channels — see the stale-test fix in
    test_lex_sub_entity_tagging.py for context)
  - stats() reflects the current DB state
  - sync_state round-trips correctly
"""

import pytest
from unittest.mock import patch

from cora.knowledge_base import embeddings
from cora.knowledge_base.store import (
    Document,
    KnowledgeBase,
    KnowledgeBaseError,
    SearchResult,
    build_sub_entity_filter,
)

# ── Embedding helpers ─────────────────────────────────────────────────────────

_DIM = 1536


def _unit_vec(axis: int = 0) -> list[float]:
    """Return a 1536-dim unit vector pointing along `axis`."""
    vec = [0.0] * _DIM
    vec[axis % _DIM] = 1.0
    return vec


def _embed_texts_mock(texts: list[str]) -> list[list[float]]:
    return [_unit_vec()] * len(texts)


def _embed_query_mock(query: str) -> list[float]:
    return _unit_vec()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def kb(tmp_path):
    db = KnowledgeBase(tmp_path / "test_kb.db")
    yield db
    db.close()


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    """Patch OpenAI embedding calls for every test in this module."""
    monkeypatch.setattr(embeddings, "embed_texts", _embed_texts_mock)
    monkeypatch.setattr(embeddings, "embed_query", _embed_query_mock)


def _doc(**overrides) -> Document:
    defaults = dict(
        source="test",
        source_id="doc-001",
        entity="F3E",
        content=(
            "This is sample content for the knowledge base. "
            "It contains multiple sentences. "
            "The chunker splits it appropriately."
        ),
        title="Sample Document",
    )
    defaults.update(overrides)
    return Document(**defaults)


# ── build_sub_entity_filter (no DB needed) ────────────────────────────────────

class TestBuildSubEntityFilter:
    def test_lex_llc_returns_in_filter(self):
        sql, params = build_sub_entity_filter("LEX-LLC")
        assert "sub_entity IN" in sql
        assert "LEX-LLC" in params

    def test_strict_mode_excludes_null_rows(self):
        sql, params = build_sub_entity_filter("LEX-LLC")
        # STRICT MODE: NULL-tagged rows must NOT pass through
        assert "IS NULL" not in sql

    def test_each_sub_entity_scoped_to_itself(self):
        for sub in ("LEX-LTS", "LEX-LBHS", "LEX-LLA"):
            sql, params = build_sub_entity_filter(sub)
            assert sub in params
            # Sibling sub-entities must not be present
            for sibling in {"LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA"} - {sub}:
                assert sibling not in params

    def test_non_lex_returns_none(self):
        for entity in ("F3E", "OSN", "BDM", "FNDR", "HJRG", "LEX"):
            assert build_sub_entity_filter(entity) is None


# ── upsert_documents ──────────────────────────────────────────────────────────

class TestUpsertDocuments:
    def test_empty_list_returns_zero_chunks(self, kb):
        assert kb.upsert_documents([]) == 0

    def test_single_doc_returns_positive_chunk_count(self, kb):
        n = kb.upsert_documents([_doc()])
        assert n >= 1

    def test_chunk_count_reflected_in_stats(self, kb):
        n = kb.upsert_documents([_doc()])
        assert kb.stats()["total_chunks"] == n

    def test_upsert_is_idempotent_for_same_source_id(self, kb):
        n1 = kb.upsert_documents([_doc(source_id="abc")])
        n2 = kb.upsert_documents([_doc(source_id="abc")])
        assert n1 == n2
        # Second upsert replaces first — total must not double
        assert kb.stats()["total_chunks"] == n1

    def test_different_source_ids_accumulate(self, kb):
        kb.upsert_documents([_doc(source_id="doc-a")])
        kb.upsert_documents([_doc(source_id="doc-b")])
        assert kb.stats()["total_chunks"] >= 2

    def test_sub_entity_stored_with_chunk(self, kb):
        kb.upsert_documents([_doc(source_id="sub-doc", sub_entity="LEX-LLC", entity="LEX")])
        # Retrieve via search and verify sub_entity is preserved through entity filter
        results = kb.search("sample", entity="LEX", include_fndr=False, sub_entity="LEX-LLC")
        assert len(results) >= 1

    def test_multiple_docs_in_one_call(self, kb):
        docs = [_doc(source_id=f"d{i}", entity="F3E") for i in range(3)]
        n = kb.upsert_documents(docs)
        assert n >= 3


# ── search ────────────────────────────────────────────────────────────────────

class TestSearch:
    def test_returns_search_results(self, kb):
        kb.upsert_documents([_doc(entity="F3E")])
        results = kb.search("sample query", entity="F3E")
        assert isinstance(results, list)
        assert len(results) >= 1
        assert all(isinstance(r, SearchResult) for r in results)

    def test_filters_by_entity(self, kb):
        kb.upsert_documents([
            _doc(source_id="f3e-doc", entity="F3E"),
            _doc(source_id="osn-doc", entity="OSN"),
        ])
        results = kb.search("query", entity="OSN", include_fndr=False)
        entities = {r.entity for r in results}
        assert "OSN" in entities
        assert "F3E" not in entities

    def test_fndr_entity_included_by_default(self, kb):
        kb.upsert_documents([
            _doc(source_id="f3e-doc", entity="F3E"),
            _doc(source_id="fndr-doc", entity="FNDR"),
        ])
        results = kb.search("query", entity="F3E", include_fndr=True)
        entities = {r.entity for r in results}
        assert "FNDR" in entities

    def test_fndr_excluded_when_include_fndr_false(self, kb):
        kb.upsert_documents([
            _doc(source_id="f3e-doc", entity="F3E"),
            _doc(source_id="fndr-doc", entity="FNDR"),
        ])
        results = kb.search("query", entity="F3E", include_fndr=False)
        assert all(r.entity == "F3E" for r in results)

    def test_sub_entity_strict_mode_excludes_null_tagged_rows(self, kb):
        """STRICT MODE invariant: LEX sub-entity search must not return NULL-tagged chunks."""
        kb.upsert_documents([
            _doc(source_id="lex-tagged", entity="LEX", sub_entity="LEX-LLC"),
            _doc(source_id="lex-null", entity="LEX", sub_entity=None),
        ])
        results = kb.search("query", entity="LEX", include_fndr=False, sub_entity="LEX-LLC")
        source_ids = {r.source_id for r in results}
        assert "lex-tagged" in source_ids
        assert "lex-null" not in source_ids

    def test_sub_entity_sibling_excluded(self, kb):
        """A LEX-LLC search must not return LEX-LTS tagged chunks."""
        kb.upsert_documents([
            _doc(source_id="llc-doc", entity="LEX", sub_entity="LEX-LLC"),
            _doc(source_id="lts-doc", entity="LEX", sub_entity="LEX-LTS"),
        ])
        results = kb.search("query", entity="LEX", include_fndr=False, sub_entity="LEX-LLC")
        source_ids = {r.source_id for r in results}
        assert "llc-doc" in source_ids
        assert "lts-doc" not in source_ids

    def test_result_fields_populated(self, kb):
        kb.upsert_documents([_doc(
            source_id="doc-fields",
            entity="F3E",
            title="My Title",
            deep_link="https://example.com/doc",
        )])
        results = kb.search("sample", entity="F3E")
        r = results[0]
        assert r.source_id == "doc-fields"
        assert r.entity == "F3E"
        assert r.title == "My Title"
        assert r.deep_link == "https://example.com/doc"
        assert isinstance(r.distance, float)

    def test_empty_db_returns_empty_list(self, kb):
        results = kb.search("anything", entity="F3E")
        assert results == []

    def test_k_limits_result_count(self, kb):
        docs = [_doc(source_id=f"d{i}", entity="F3E") for i in range(5)]
        kb.upsert_documents(docs)
        results = kb.search("query", entity="F3E", k=2)
        assert len(results) <= 2


# ── stats ─────────────────────────────────────────────────────────────────────

class TestStats:
    def test_empty_db_all_zeros(self, kb):
        stats = kb.stats()
        assert stats["total_chunks"] == 0
        assert stats["by_source"] == {}
        assert stats["by_entity"] == {}

    def test_counts_by_source(self, kb):
        kb.upsert_documents([_doc(source="fireflies", source_id="ff-1", entity="F3E")])
        stats = kb.stats()
        assert "fireflies" in stats["by_source"]
        assert stats["by_source"]["fireflies"] >= 1

    def test_counts_by_entity(self, kb):
        kb.upsert_documents([_doc(entity="OSN", source_id="osn-1")])
        stats = kb.stats()
        assert "OSN" in stats["by_entity"]

    def test_multiple_sources_counted_separately(self, kb):
        kb.upsert_documents([
            _doc(source="asana", source_id="a1", entity="F3E"),
            _doc(source="notion", source_id="n1", entity="F3E"),
        ])
        stats = kb.stats()
        assert "asana" in stats["by_source"]
        assert "notion" in stats["by_source"]

    def test_total_matches_sum_of_by_source(self, kb):
        kb.upsert_documents([
            _doc(source="s1", source_id="x1", entity="F3E"),
            _doc(source="s2", source_id="x2", entity="OSN"),
        ])
        stats = kb.stats()
        assert stats["total_chunks"] == sum(stats["by_source"].values())


# ── sync_state ────────────────────────────────────────────────────────────────

class TestSyncState:
    def test_get_returns_none_for_unknown_source(self, kb):
        assert kb.get_sync_state("fireflies") is None

    def test_set_and_get_round_trip(self, kb):
        kb.set_sync_state("fireflies", last_sync_at=1_000_000, last_source_modified=900_000)
        result = kb.get_sync_state("fireflies")
        assert result is not None
        assert result[0] == 1_000_000
        assert result[1] == 900_000

    def test_last_source_modified_can_be_none(self, kb):
        kb.set_sync_state("asana", last_sync_at=1_000_000, last_source_modified=None)
        result = kb.get_sync_state("asana")
        assert result[0] == 1_000_000
        assert result[1] is None

    def test_set_overwrites_existing_record(self, kb):
        kb.set_sync_state("notion", last_sync_at=1000, last_source_modified=900)
        kb.set_sync_state("notion", last_sync_at=2000, last_source_modified=1900)
        result = kb.get_sync_state("notion")
        assert result[0] == 2000
        assert result[1] == 1900

    def test_multiple_sources_tracked_independently(self, kb):
        kb.set_sync_state("source_a", last_sync_at=1000)
        kb.set_sync_state("source_b", last_sync_at=2000)
        assert kb.get_sync_state("source_a")[0] == 1000
        assert kb.get_sync_state("source_b")[0] == 2000
