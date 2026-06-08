"""Tests for ingest-time LEX sub-entity tagging (Part 2 of the 5/23 siloing fix).

The shared detection module (cora.knowledge_base.lex_sub_entity) is wired into
KnowledgeBase.upsert_documents so every connector's LEX docs get tagged at the
choke point. Invariants:

  - LEX doc with UNAMBIGUOUS sub-entity signal + sub_entity=None -> tagged at insert
  - LEX doc with an EXPLICIT sub_entity -> never overridden by detection
  - LEX doc with general content -> stays NULL (GM-level by design)
  - LEX doc matching 2+ sub-entities -> ambiguous, stays NULL
  - Non-LEX docs -> never touched, even if text contains LEX keywords

Companion files: tests/test_backfill_lex_sub_entity.py (detection cases),
tests/test_kb_store.py (store behavior), tests/test_lex_sub_entity_tagging.py
(retrieval-side strict filter).
"""

import pytest

from cora.knowledge_base import embeddings
from cora.knowledge_base.lex_sub_entity import detect_sub_entity
from cora.knowledge_base.store import Document, KnowledgeBase

_DIM = 1536


def _unit_vec() -> list:
    vec = [0.0] * _DIM
    vec[0] = 1.0
    return vec


def _embed_texts_mock(texts):
    return [_unit_vec() for _ in texts]


def _embed_query_mock(query):
    return _unit_vec()


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", _embed_texts_mock)
    monkeypatch.setattr(embeddings, "embed_query", _embed_query_mock)


@pytest.fixture
def kb(tmp_path):
    db = KnowledgeBase(tmp_path / "test_kb.db")
    yield db
    db.close()


def _doc(**overrides) -> Document:
    defaults = dict(
        source="gmail",
        source_id="msg-001",
        entity="LEX",
        content="General Lexington payroll summary for May 2026.",
        title="Payroll summary",
    )
    defaults.update(overrides)
    return Document(**defaults)


def _stored_sub_entities(kb: KnowledgeBase, source_id: str) -> set:
    cur = kb._conn.cursor()
    cur.execute(
        "SELECT DISTINCT sub_entity FROM knowledge_chunks WHERE source_id = ?",
        (source_id,),
    )
    return {row[0] for row in cur.fetchall()}


class TestIngestTimeTagging:
    def test_unambiguous_llc_doc_tagged(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-llc",
            title="HCBS billing report Q1",
            content="Supported Living placements and HCBS claims for the quarter.",
        )])
        assert _stored_sub_entities(kb, "msg-llc") == {"LEX-LLC"}

    def test_unambiguous_lts_doc_tagged(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-lts",
            title="Provider Type 15 deadline",
            content="DDD Therapy Revalidation paperwork is due June 30.",
        )])
        assert _stored_sub_entities(kb, "msg-lts") == {"LEX-LTS"}

    def test_unambiguous_lbhs_doc_tagged(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-lbhs",
            title="LBHS Q2 census numbers",
            content="Census report attached.",
        )])
        assert _stored_sub_entities(kb, "msg-lbhs") == {"LEX-LBHS"}

    def test_unambiguous_lla_doc_tagged(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-lla",
            title="Lex Life Academy enrollment",
            content="Maryvale site enrollment numbers.",
        )])
        assert _stored_sub_entities(kb, "msg-lla") == {"LEX-LLA"}

    def test_general_lex_doc_stays_null(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-general",
            title="Staff training slides Q2",
            content="Training material for all Lexington staff.",
        )])
        assert _stored_sub_entities(kb, "msg-general") == {None}

    def test_ambiguous_doc_stays_null(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-ambig",
            title="[LEX-LLC] LBHS billing overlap",
            content="Crossover items between the two entities.",
        )])
        assert _stored_sub_entities(kb, "msg-ambig") == {None}

    def test_explicit_sub_entity_never_overridden(self, kb):
        # Content looks like LLC, but the connector explicitly tagged LBHS.
        kb.upsert_documents([_doc(
            source_id="msg-explicit",
            sub_entity="LEX-LBHS",
            title="HCBS billing report",
            content="Supported Living claims.",
        )])
        assert _stored_sub_entities(kb, "msg-explicit") == {"LEX-LBHS"}

    def test_non_lex_entity_untouched(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-osn",
            entity="OSN",
            title="HCBS mention in a non-LEX doc",
            content="Sandy Patel stopped by the Gilbert store.",
        )])
        assert _stored_sub_entities(kb, "msg-osn") == {None}

    def test_fndr_entity_untouched(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-fndr",
            entity="FNDR",
            title="Portfolio note mentioning LBHS",
            content="LBHS COPA diligence continues.",
        )])
        assert _stored_sub_entities(kb, "msg-fndr") == {None}

    def test_tagged_chunk_retrievable_in_sub_entity_scope(self, kb):
        """End-to-end: an auto-tagged chunk passes the strict sub-entity filter."""
        kb.upsert_documents([_doc(
            source_id="msg-search",
            title="HCBS billing report Q1",
            content="Supported Living placements for the quarter.",
        )])
        results = kb.search("billing report", entity="LEX", sub_entity="LEX-LLC")
        assert any(r.source_id == "msg-search" for r in results)
        # And it must NOT surface in a sibling sub-entity scope.
        sibling = kb.search("billing report", entity="LEX", sub_entity="LEX-LBHS")
        assert not any(r.source_id == "msg-search" for r in sibling)


class TestSharedModuleParity:
    """The script aliases must point at the shared module (no drift)."""

    def test_script_aliases_are_shared_module(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import backfill_lex_sub_entity as script
        assert script._detect_sub_entity is detect_sub_entity

    def test_detection_unchanged_from_locked_5_31_behavior(self):
        # Spot checks mirroring the locked test cases
        assert detect_sub_entity("[LEX-LLC] Grow to 750 Members", "") == "LEX-LLC"
        assert detect_sub_entity("Sandy Patel membership repurchase 2023", "") == "LEX-LLA"
        assert detect_sub_entity("Lexington payroll report May 2026", "") is None
        assert detect_sub_entity("[LEX-LLC] LBHS billing overlap", "") is None
