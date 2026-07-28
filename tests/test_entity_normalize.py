"""Tests for canonical entity-code normalization (Part 2 Slice 2-2, 2026-07-28).

Pure map (cora.knowledge_base.entity_normalize.normalize_entity) + the Step-0e wiring in
KnowledgeBase.upsert_documents that folds stray codes onto their canonical parent so
retrieval's entity filter (and the coarse bin pre-filter) can see them.
"""

import logging

import pytest

from cora.knowledge_base import embeddings
from cora.knowledge_base.entity_normalize import normalize_entity
from cora.knowledge_base.store import Document, KnowledgeBase

_DIM = 1536


def _unit_vec() -> list:
    v = [0.0] * _DIM
    v[0] = 1.0
    return v


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts: [_unit_vec() for _ in texts])
    monkeypatch.setattr(embeddings, "embed_query", lambda q: _unit_vec())


@pytest.fixture
def kb(tmp_path):
    db = KnowledgeBase(tmp_path / "test_kb.db")
    yield db
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Pure map
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("entity,sub_in,expect", [
    # LEX-* -> LEX + the code as sub_entity (canonical LEX-XXX form), preserve existing.
    ("LEX-LLC", None, ("LEX", "LEX-LLC")),
    ("LEX-LLA", None, ("LEX", "LEX-LLA")),
    ("LEX-LTS", "LEX-LTS", ("LEX", "LEX-LTS")),
    ("lex-llc", None, ("LEX", "LEX-LLC")),                 # case-insensitive
    # OSN franchise stores -> OSN, no sub_entity.
    ("OSNGF", None, ("OSN", None)),
    ("OSNGM", None, ("OSN", None)),
    ("OSNGW", None, ("OSN", None)),
    ("OSNVV", None, ("OSN", None)),
    ("osngf", None, ("OSN", None)),
    # HJRP properties -> HJRP.
    ("HJRP-LCI", None, ("HJRP", None)),
    ("HJRP-1337", None, ("HJRP", None)),
    ("HJRP-1555", None, ("HJRP", None)),
    # F3 -> F3E.
    ("F3", None, ("F3E", None)),
    # Canonical codes pass through unchanged (sub preserved).
    ("LEX", None, ("LEX", None)),
    ("OSN", None, ("OSN", None)),
    ("F3E", None, ("F3E", None)),
    ("HJRP", None, ("HJRP", None)),
    ("FNDR", None, ("FNDR", None)),
    ("F3C", None, ("F3C", None)),
])
def test_normalize_map(entity, sub_in, expect):
    assert normalize_entity(entity, sub_in) == expect


def test_lex_existing_sub_entity_preserved():
    # An explicit sub already set on a LEX-* stray is NOT overwritten by the code.
    assert normalize_entity("LEX-LLC", "LEX-LTS") == ("LEX", "LEX-LTS")


def test_unknown_code_fails_open_with_warn(caplog):
    with caplog.at_level(logging.WARNING):
        out = normalize_entity("ZZZ-WEIRD", None)
    assert out == ("ZZZ-WEIRD", None)                      # passed through, not dropped
    assert any("unrecognized entity code" in r.message for r in caplog.records)


def test_empty_entity_returns_empty():
    assert normalize_entity("", None) == ("", None)
    assert normalize_entity(None, None) == ("", None)


def test_idempotent():
    for e in ("LEX-LLC", "OSNGF", "HJRP-1337", "F3", "OSN", "LEX", "ZZZ"):
        once = normalize_entity(e, None)
        assert normalize_entity(*once) == once


# ─────────────────────────────────────────────────────────────────────────────
# Step-0e wiring in upsert_documents
# ─────────────────────────────────────────────────────────────────────────────
def _stored(kb: KnowledgeBase, source_id: str) -> tuple[str, str | None, list[str]]:
    """(entity, sub_entity, [bin entity codes]) for a source_id's chunks."""
    rows = kb._conn.execute(
        "SELECT chunk_id, entity, sub_entity FROM knowledge_chunks WHERE source_id = ?",
        (source_id,),
    ).fetchall()
    assert rows, f"no chunks stored for {source_id}"
    chunk_ids = [r[0] for r in rows]
    ph = ",".join("?" * len(chunk_ids))
    bin_ents = [r[0] for r in kb._conn.execute(
        f"SELECT entity FROM knowledge_vec_bin WHERE chunk_id IN ({ph})", chunk_ids
    ).fetchall()]
    return rows[0][1], rows[0][2], bin_ents


def _doc(**ov) -> Document:
    d = dict(source="slack", source_id="s1", entity="OSNGF",
             content="One Stop Nutrition Greenfield store thread.", title="thread")
    d.update(ov)
    return Document(**d)


def test_ingest_osn_store_folds_to_osn(kb):
    kb.upsert_documents([_doc(entity="OSNGF", source_id="osngf-1")])
    ent, sub, bin_ents = _stored(kb, "osngf-1")
    assert ent == "OSN" and sub is None
    assert set(bin_ents) == {"OSN"}                        # coarse pre-filter also canonical


def test_ingest_lex_sub_folds_to_lex_with_sub(kb):
    kb.upsert_documents([_doc(entity="LEX-LLC", source_id="lex-1",
                              content="LLC admin note", title="note")])
    ent, sub, bin_ents = _stored(kb, "lex-1")
    assert ent == "LEX" and sub == "LEX-LLC"               # sub scoping preserved
    assert set(bin_ents) == {"LEX"}


def test_ingest_f3_folds_to_f3e(kb):
    kb.upsert_documents([_doc(entity="F3", source_id="f3-1")])
    ent, _, bin_ents = _stored(kb, "f3-1")
    assert ent == "F3E" and set(bin_ents) == {"F3E"}


def test_ingest_hjrp_property_folds_to_hjrp(kb):
    kb.upsert_documents([_doc(entity="HJRP-1337", source_id="hjrp-1")])
    ent, _, bin_ents = _stored(kb, "hjrp-1")
    assert ent == "HJRP" and set(bin_ents) == {"HJRP"}


def test_ingest_canonical_untouched(kb):
    kb.upsert_documents([_doc(entity="F3E", source_id="f3e-1")])
    ent, _, _ = _stored(kb, "f3e-1")
    assert ent == "F3E"


def test_ingest_user_note_entity_NOT_normalized(kb):
    # A user_note scopes on the RAW channel entity via search_user_notes -- Step-0e must
    # NOT fold it, or note containment breaks (LEX-LLC note leaking into an LEX-LTS scope).
    kb.upsert_documents([Document(
        source="user_note", source_id="note-1", entity="LEX-LLC",
        content="alpha owner note", title="note",
        metadata={"owner_slack": "U_OWNER"})])
    row = kb._conn.execute(
        "SELECT entity, sub_entity FROM knowledge_chunks WHERE source_id = 'note-1'"
    ).fetchone()
    assert row == ("LEX-LLC", None)                        # verbatim, un-normalized


def test_normalized_lex_sub_is_retrievable_and_scoped(kb):
    # A LEX-LLC-entity doc is now visible in the LEX parent scope with sub LEX-LLC, and
    # a strict LEX-LTS scope must NOT see it (leak guard).
    kb.upsert_documents([_doc(entity="LEX-LLC", source_id="lex-scope",
                              content="LLC-specific service note", title="svc")])
    hit_llc = kb.search("service note", entity="LEX", sub_entity="LEX-LLC",
                        include_fndr=False, max_age_days=None)
    assert any(r.source_id == "lex-scope" for r in hit_llc)
    hit_lts = kb.search("service note", entity="LEX", sub_entity="LEX-LTS",
                        include_fndr=False, max_age_days=None)
    assert not any(r.source_id == "lex-scope" for r in hit_lts)
