"""Regression: every chunk-delete cascade must cover EVERY bin coarse table.

The Slice 2-1 (2026-07-28) partition migration added knowledge_vec_bin_v2 next
to the legacy knowledge_vec_bin. store.py already deletes across both via
_bin_write_tables(); this suite pins that the standalone purge/prune utilities
discover the SAME table set (schema.BIN_TABLE_CANDIDATES /
schema.vec_cascade_tables) in every migration state -- legacy-only, dual
(migration window), and v2-only (after migrate_kb_partition_key.py
--drop-legacy). A cascade that misses a bin table strands orphan vectors:
recall-safe (the re-rank JOINs knowledge_chunks) but it bloats the coarse scan
and defeats the point of a purge.
"""

from __future__ import annotations

import importlib
import re
import struct
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import prune_kb_retention as prune  # noqa: E402
from cora import kb_archive  # noqa: E402
from cora.knowledge_base import schema, store  # noqa: E402
from cora.knowledge_base.store import KnowledgeBase  # noqa: E402

_DUMMY_VEC = struct.pack("1536f", *([0.01] * 1536))

_V2_CREATE = f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vec_bin_v2 USING vec0(
        chunk_id TEXT PRIMARY KEY,
        entity TEXT PARTITION KEY,
        embedding bit[{schema.EMBEDDING_DIM}]
    )
"""


def _create_v2(conn) -> None:
    """Simulate the partition migration creating the v2 coarse table."""
    conn.execute(_V2_CREATE)
    conn.commit()


def _insert_everywhere(conn, chunk_id: str, entity: str = "FNDR") -> None:
    """One chunk row + a vector row in every vec table present."""
    conn.execute(
        """INSERT INTO knowledge_chunks
           (chunk_id, source, source_id, entity, content, ingested_at)
           VALUES (?, 'gmail', ?, ?, 'body', 1780000000)""",
        (chunk_id, f"src-{chunk_id}", entity),
    )
    conn.execute(
        "INSERT INTO knowledge_vec_f32 (chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, _DUMMY_VEC),
    )
    for tbl in schema.bin_tables_present(conn):
        conn.execute(
            f"INSERT INTO {tbl} (chunk_id, entity, embedding) "
            f"VALUES (?, ?, vec_quantize_binary(?))",
            (chunk_id, entity, _DUMMY_VEC),
        )
    conn.commit()


def _count(conn, table: str, chunk_id: str) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE chunk_id = ?", (chunk_id,)
    ).fetchone()[0]


@pytest.fixture()
def kb(tmp_path):
    db = KnowledgeBase(tmp_path / "cascade_kb.db")
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Source-of-truth lockstep: schema.BIN_TABLE_CANDIDATES <-> store.py constants
# ---------------------------------------------------------------------------

def test_schema_candidates_match_store_constants():
    """If store.py ever grows/renames a bin table, this forces the shared
    candidate list (and with it every purge utility) to follow."""
    assert schema.BIN_TABLE_CANDIDATES == (store._LEGACY_BIN, store._V2_BIN)


def test_prune_candidates_cover_all_bin_tables_plus_f32():
    assert set(schema.BIN_TABLE_CANDIDATES) <= set(prune._CANDIDATE_VEC_TABLES)
    assert "knowledge_vec_f32" in prune._CANDIDATE_VEC_TABLES


# ---------------------------------------------------------------------------
# The pinned invariant: cascade set == store._bin_write_tables() + f32 + chunks
# in every migration state.
# ---------------------------------------------------------------------------

def _assert_cascade_matches_store(kb: KnowledgeBase) -> None:
    expected = [*kb._bin_write_tables(), "knowledge_vec_f32", "knowledge_chunks"]
    assert schema.vec_cascade_tables(kb._conn) == expected


def test_cascade_matches_store_legacy_only(kb):
    # Fresh unarmed DB: legacy bin table only.
    assert kb._bin_write_tables() == ["knowledge_vec_bin"]
    _assert_cascade_matches_store(kb)


def test_cascade_matches_store_dual_tables(kb):
    # Migration window: legacy + v2 coexist; deletes must hit BOTH.
    _create_v2(kb._conn)
    assert kb._bin_write_tables() == ["knowledge_vec_bin", "knowledge_vec_bin_v2"]
    _assert_cascade_matches_store(kb)


def test_cascade_matches_store_v2_only(kb):
    # Post --drop-legacy: the cascade must keep working without the legacy table.
    _create_v2(kb._conn)
    kb._conn.execute("DROP TABLE knowledge_vec_bin")
    kb._conn.commit()
    assert kb._bin_write_tables() == ["knowledge_vec_bin_v2"]
    _assert_cascade_matches_store(kb)


# ---------------------------------------------------------------------------
# kb_archive.delete_chunks clears v2 (the reported defect)
# ---------------------------------------------------------------------------

def test_kb_archive_delete_clears_v2_no_orphans(kb):
    _create_v2(kb._conn)
    _insert_everywhere(kb._conn, "k1")
    _insert_everywhere(kb._conn, "k2")

    totals = kb_archive.delete_chunks(kb._conn, ["k1"])

    assert totals["knowledge_vec_bin_v2"] == 1
    assert totals["knowledge_vec_bin"] == 1
    assert totals["knowledge_vec_f32"] == 1
    assert totals["knowledge_chunks"] == 1
    for tbl in ("knowledge_chunks", "knowledge_vec_bin",
                "knowledge_vec_bin_v2", "knowledge_vec_f32"):
        assert _count(kb._conn, tbl, "k1") == 0, f"orphan left in {tbl}"
        assert _count(kb._conn, tbl, "k2") == 1, f"unrelated row lost from {tbl}"


def test_kb_archive_delete_works_after_legacy_drop(kb):
    _create_v2(kb._conn)
    kb._conn.execute("DROP TABLE knowledge_vec_bin")
    kb._conn.commit()
    _insert_everywhere(kb._conn, "k1")

    totals = kb_archive.delete_chunks(kb._conn, ["k1"])

    assert "knowledge_vec_bin" not in totals  # dropped table never touched
    assert totals["knowledge_vec_bin_v2"] == 1
    assert _count(kb._conn, "knowledge_vec_bin_v2", "k1") == 0


# ---------------------------------------------------------------------------
# prune_kb_retention covers v2 (the reported defect)
# ---------------------------------------------------------------------------

def test_prune_existing_vec_tables_includes_v2_when_present(kb):
    assert "knowledge_vec_bin_v2" not in prune.existing_vec_tables(kb._conn)
    _create_v2(kb._conn)
    tables = prune.existing_vec_tables(kb._conn)
    assert "knowledge_vec_bin" in tables
    assert "knowledge_vec_bin_v2" in tables
    assert "knowledge_vec_f32" in tables


def test_prune_chunks_clears_v2_no_orphans(kb):
    _create_v2(kb._conn)
    _insert_everywhere(kb._conn, "old-1")
    _insert_everywhere(kb._conn, "keep-1")

    removed = prune.prune_chunks(
        kb._conn, ["old-1"], prune.existing_vec_tables(kb._conn)
    )

    assert removed == 1
    for tbl in ("knowledge_chunks", "knowledge_vec_bin",
                "knowledge_vec_bin_v2", "knowledge_vec_f32"):
        assert _count(kb._conn, tbl, "old-1") == 0, f"orphan left in {tbl}"
        assert _count(kb._conn, tbl, "keep-1") == 1


# ---------------------------------------------------------------------------
# Standalone purge scripts share the same discovered cascade
# ---------------------------------------------------------------------------

_STANDALONE_PURGE_MODULES = (
    "purge_cora_internal_kb",
    "purge_dashboard_kb",
    "purge_denied_kb",
    "purge_lex_program_kb",
    "purge_lex_restricted_kb",
)


@pytest.mark.parametrize("mod_name", _STANDALONE_PURGE_MODULES)
def test_standalone_purge_delete_clears_v2(kb, mod_name):
    mod = importlib.import_module(mod_name)
    _create_v2(kb._conn)
    _insert_everywhere(kb._conn, "k1")

    totals = mod.delete_chunks(kb._conn, ["k1"])

    assert totals.get("knowledge_vec_bin_v2") == 1, f"{mod_name} missed v2"
    for tbl in ("knowledge_chunks", "knowledge_vec_bin",
                "knowledge_vec_bin_v2", "knowledge_vec_f32"):
        assert _count(kb._conn, tbl, "k1") == 0, f"{mod_name}: orphan in {tbl}"


# ---------------------------------------------------------------------------
# cleanup_stale_vec (the orphan-mitigation sweep) must see v2
# ---------------------------------------------------------------------------

def test_cleanup_stale_vec_sweeps_v2(kb):
    sweep = importlib.import_module("cleanup_stale_vec")
    assert "knowledge_vec_bin_v2" not in sweep.vec_tables(kb._conn)
    _create_v2(kb._conn)
    tables = sweep.vec_tables(kb._conn)
    assert "knowledge_vec_bin_v2" in tables and "knowledge_vec_f32" in tables

    # A v2 row with no knowledge_chunks parent is an orphan the sweep must find.
    kb._conn.execute(
        "INSERT INTO knowledge_vec_bin_v2 (chunk_id, entity, embedding) "
        "VALUES ('orph', 'FNDR', vec_quantize_binary(?))",
        (_DUMMY_VEC,),
    )
    kb._conn.commit()
    orphans = sweep.find_orphans(kb._conn, "knowledge_vec_bin_v2")
    assert orphans == ["orph"]
    assert sweep.delete_orphans(kb._conn, "knowledge_vec_bin_v2", orphans) == 1
    assert _count(kb._conn, "knowledge_vec_bin_v2", "orph") == 0


# ---------------------------------------------------------------------------
# CI guard: no new hard-coded pre-v2 cascade may enter the repo
# ---------------------------------------------------------------------------

def test_no_hardcoded_pre_v2_cascade_in_repo():
    """A quoted "knowledge_vec_bin", "knowledge_vec_f32" adjacency is the
    signature of a hard-coded pre-partition cascade (it skips v2). New delete
    code must discover its tables via schema.bin_tables_present /
    schema.vec_cascade_tables / store._bin_write_tables instead."""
    pat = re.compile(r'"knowledge_vec_bin"\s*,\s*"knowledge_vec_f32"')
    offenders = []
    for root in (_REPO_ROOT / "scripts", _REPO_ROOT / "src" / "cora"):
        for p in root.rglob("*.py"):
            if pat.search(p.read_text(encoding="utf-8", errors="replace")):
                offenders.append(str(p.relative_to(_REPO_ROOT)))
    assert offenders == [], (
        f"hard-coded pre-v2 vec cascade in: {offenders} -- use "
        "schema.vec_cascade_tables(conn) (or store._bin_write_tables) instead"
    )
