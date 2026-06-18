"""Tests for scripts/prune_kb_retention.py (Phase 3.1 retention prune).

Builds a tiny real KB (schema.connect loads sqlite-vec) in a temp dir and
exercises the selection + cascade-delete logic. No live DB, no network.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import prune_kb_retention as prune  # noqa: E402
from cora.knowledge_base import schema  # noqa: E402

NOW = 1_780_000_000  # fixed reference epoch
DAY = 86400
OLD = NOW - 600 * DAY      # older than an 18-month (~547d) window
RECENT = NOW - 10 * DAY    # well inside the window
_DUMMY_VEC = struct.pack("1536f", *([0.01] * 1536))


@pytest.fixture()
def kb(tmp_path):
    conn = schema.connect(tmp_path / "test_kb.db")
    schema.init_schema(conn)
    yield conn
    conn.close()


def _insert(conn, chunk_id, source, ingested_at, *, date_modified=None,
            date_created=None, entity="FNDR", with_vecs=True):
    conn.execute(
        """INSERT INTO knowledge_chunks
           (chunk_id, source, source_id, entity, date_created, date_modified,
            content, ingested_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (chunk_id, source, f"src-{chunk_id}", entity, date_created, date_modified,
         "body", ingested_at),
    )
    if with_vecs:
        conn.execute(
            "INSERT INTO knowledge_vec_f32 (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, _DUMMY_VEC),
        )
        conn.execute(
            "INSERT INTO knowledge_vec_bin (chunk_id, entity, embedding) "
            "VALUES (?, ?, vec_quantize_binary(?))",
            (chunk_id, entity, _DUMMY_VEC),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# compute_cutoff
# ---------------------------------------------------------------------------

def test_compute_cutoff_months():
    assert prune.compute_cutoff(NOW, 18, None) == NOW - int(round(18 * 30.44)) * DAY


def test_compute_cutoff_days_overrides_months():
    assert prune.compute_cutoff(NOW, 18, 30) == NOW - 30 * DAY


def test_compute_cutoff_zero_raises():
    with pytest.raises(ValueError):
        prune.compute_cutoff(NOW, 0, None)
    with pytest.raises(ValueError):
        prune.compute_cutoff(NOW, 18, 0)


# ---------------------------------------------------------------------------
# existing_vec_tables
# ---------------------------------------------------------------------------

def test_existing_vec_tables_finds_bin_and_f32_not_i8(kb):
    tables = prune.existing_vec_tables(kb)
    assert "knowledge_vec_bin" in tables
    assert "knowledge_vec_f32" in tables
    assert "knowledge_vec_i8" not in tables  # forward-compat; not created today


# ---------------------------------------------------------------------------
# select_prunable_chunk_ids
# ---------------------------------------------------------------------------

def test_select_only_prunes_gmail_and_drive_sweep(kb):
    # One old chunk from every source -- only gmail + drive_sweep are eligible.
    for src in ("gmail", "drive_sweep", "static_md", "fireflies", "asana",
                "notion", "user_note"):
        _insert(kb, f"c-{src}", src, OLD, date_modified=OLD, with_vecs=False)
    cutoff = prune.compute_cutoff(NOW, 18, None)
    selected = set(prune.select_prunable_chunk_ids(kb, cutoff, prune.PRUNE_SOURCES))
    assert selected == {"c-gmail", "c-drive_sweep"}


def test_select_requires_old_by_both_timestamps(kb):
    cutoff = prune.compute_cutoff(NOW, 18, None)
    # old by both -> pruned
    _insert(kb, "old-both", "gmail", OLD, date_modified=OLD, with_vecs=False)
    # old ingested but RECENT content date -> kept
    _insert(kb, "recent-content", "gmail", OLD, date_modified=RECENT, with_vecs=False)
    # RECENT ingested, old content date -> kept
    _insert(kb, "recent-ingest", "gmail", RECENT, date_modified=OLD, with_vecs=False)
    selected = set(prune.select_prunable_chunk_ids(kb, cutoff, prune.PRUNE_SOURCES))
    assert selected == {"old-both"}


def test_predicate_modes():
    frag_i, n_i = prune._predicate("ingested")
    assert n_i == 2 and "ingested_at <" in frag_i and "COALESCE" in frag_i
    frag_c, n_c = prune._predicate("content")
    assert n_c == 1 and "COALESCE" in frag_c and "ingested_at <" not in frag_c
    with pytest.raises(ValueError):
        prune._predicate("bogus")


def test_content_mode_prunes_old_content_despite_recent_ingest(kb):
    # A freshly re-ingested but content-old chunk: ingested mode KEEPS it (protects
    # the backfill); content mode PRUNES it.
    cutoff = prune.compute_cutoff(NOW, 18, None)
    _insert(kb, "fresh-ingest-old-content", "gmail", RECENT, date_modified=OLD, with_vecs=False)
    assert prune.select_prunable_chunk_ids(kb, cutoff, prune.PRUNE_SOURCES, "ingested") == []
    assert prune.select_prunable_chunk_ids(kb, cutoff, prune.PRUNE_SOURCES, "content") == [
        "fresh-ingest-old-content"
    ]


def test_content_mode_still_scopes_to_prune_sources(kb):
    cutoff = prune.compute_cutoff(NOW, 18, None)
    _insert(kb, "g-old", "gmail", RECENT, date_modified=OLD, with_vecs=False)
    _insert(kb, "fireflies-old", "fireflies", RECENT, date_modified=OLD, with_vecs=False)
    selected = set(prune.select_prunable_chunk_ids(kb, cutoff, prune.PRUNE_SOURCES, "content"))
    assert selected == {"g-old"}  # fireflies is never eligible, even in content mode


def test_select_coalesces_null_dates_to_ingested_at(kb):
    # drive_sweep leaves date_created/date_modified NULL -> COALESCE falls back to
    # ingested_at, so an old-ingested NULL-dated chunk IS pruned.
    cutoff = prune.compute_cutoff(NOW, 18, None)
    _insert(kb, "null-dates-old", "drive_sweep", OLD, with_vecs=False)
    _insert(kb, "null-dates-recent", "drive_sweep", RECENT, with_vecs=False)
    selected = set(prune.select_prunable_chunk_ids(kb, cutoff, prune.PRUNE_SOURCES))
    assert selected == {"null-dates-old"}


# ---------------------------------------------------------------------------
# prune_chunks (cascade)
# ---------------------------------------------------------------------------

def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_prune_chunks_cascades_all_tables(kb):
    _insert(kb, "keep", "gmail", RECENT, date_modified=RECENT)
    _insert(kb, "del1", "gmail", OLD, date_modified=OLD)
    _insert(kb, "del2", "drive_sweep", OLD, date_modified=OLD)
    vec_tables = prune.existing_vec_tables(kb)

    removed = prune.prune_chunks(kb, ["del1", "del2"], vec_tables)
    assert removed == 2
    assert _count(kb, "knowledge_chunks") == 1
    assert _count(kb, "knowledge_vec_f32") == 1
    assert _count(kb, "knowledge_vec_bin") == 1
    # the survivor is the recent chunk
    assert kb.execute("SELECT chunk_id FROM knowledge_chunks").fetchone()[0] == "keep"


def test_prune_chunks_batches_under_bound_var_limit(kb):
    ids = [f"b-{i}" for i in range(1200)]
    for cid in ids:
        _insert(kb, cid, "gmail", OLD, date_modified=OLD, with_vecs=False)
    vec_tables = prune.existing_vec_tables(kb)
    removed = prune.prune_chunks(kb, ids, vec_tables, batch_size=500)
    assert removed == 1200
    assert _count(kb, "knowledge_chunks") == 0


def test_prune_chunks_empty_is_noop(kb):
    _insert(kb, "keep", "gmail", RECENT, date_modified=RECENT, with_vecs=False)
    removed = prune.prune_chunks(kb, [], prune.existing_vec_tables(kb))
    assert removed == 0
    assert _count(kb, "knowledge_chunks") == 1
