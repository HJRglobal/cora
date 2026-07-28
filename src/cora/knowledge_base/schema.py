"""sqlite + sqlite-vec schema for the Knowledge Base.

Tables:
- knowledge_chunks: one row per chunk with source/entity/date/content/deep_link/metadata
- knowledge_vec_bin: binary-quantized vec0 table (fast coarse hamming scan)
- knowledge_vec_f32: float32 blob table (exact re-rank + brute-force fallback)
  (the legacy float vec0 table `knowledge_vec` was dropped 2026-06-08)
- sync_state: per-source watermark tracking for incremental ingest

The embedding dimension (1536) is fixed by OpenAI text-embedding-3-small. If we ever
switch to a different model, this table needs to be rebuilt.
"""

import json
import logging
import sqlite3
from pathlib import Path

import sqlite_vec

log = logging.getLogger(__name__)

EMBEDDING_DIM = 1536  # text-embedding-3-small


def connect(db_path: Path | str, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a sqlite connection with the vec0 extension loaded + WAL mode.

    check_same_thread=False is used for the long-lived shared KB instance that
    the prewarm thread creates and request threads reuse (access is serialized
    by a lock in context_loader, so concurrent use on one connection is safe).
    """
    conn = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait up to 30s for a held write lock instead of failing instantly with
    # "database is locked". WAL permits concurrent readers + one writer, but two
    # writers (e.g. the live bot ingestion + a manual Gmail backfill) still
    # contend; without a busy timeout the loser raises OperationalError mid-run.
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables + indexes idempotently. Safe to call on every boot."""
    # Base tables without sub_entity index (index added after migration below)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            chunk_id      TEXT PRIMARY KEY,
            source        TEXT NOT NULL,
            source_id     TEXT NOT NULL,
            entity        TEXT NOT NULL,
            date_created  INTEGER,
            date_modified INTEGER,
            author        TEXT,
            title         TEXT,
            content       TEXT NOT NULL,
            deep_link     TEXT,
            metadata      TEXT,
            ingested_at   INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_chunks_source       ON knowledge_chunks(source);
        CREATE INDEX IF NOT EXISTS idx_chunks_entity       ON knowledge_chunks(entity);
        CREATE INDEX IF NOT EXISTS idx_chunks_date_mod     ON knowledge_chunks(date_modified DESC);
        CREATE INDEX IF NOT EXISTS idx_chunks_source_id    ON knowledge_chunks(source, source_id);
        -- Serves the Drive-materialization watermark query (get_chunks_since):
        -- WHERE source=? AND entity=? AND ingested_at>? — lets sqlite seek to the
        -- watermark instead of scanning a whole (source,entity) partition nightly.
        CREATE INDEX IF NOT EXISTS idx_chunks_src_ent_ing  ON knowledge_chunks(source, entity, ingested_at);

        CREATE TABLE IF NOT EXISTS sync_state (
            source                TEXT PRIMARY KEY,
            last_sync_at          INTEGER NOT NULL,
            last_source_modified  INTEGER
        );

        CREATE TABLE IF NOT EXISTS checkpoint_state (
            key        TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        """
    )

    # Migration: add sub_entity column to existing databases (idempotent)
    # Must run before creating the sub_entity index below.
    try:
        conn.execute("ALTER TABLE knowledge_chunks ADD COLUMN sub_entity TEXT")
        conn.commit()
        log.info("Migrated knowledge_chunks: added sub_entity column")
    except sqlite3.OperationalError:
        pass  # column already exists

    # sub_entity index — created after migration so the column is guaranteed present
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chunks_sub_entity ON knowledge_chunks(sub_entity)"
    )
    conn.commit()

    # NOTE: the legacy float vec0 table `knowledge_vec` (~1.4 GB) was dropped
    # 2026-06-08 and is no longer created. The binary index (coarse scan) + the
    # float32 blob table (exact re-rank AND the brute-force fallback in
    # _search_float) are the only vector stores. Do not re-add knowledge_vec.

    # Binary-quantized vec0 coarse index. Two shapes exist across the Slice 2-1 (2026-07-28)
    # migration: the legacy metadata-column `knowledge_vec_bin` (entity as a filterable
    # column) and the partitioned `knowledge_vec_bin_v2` (entity as a PARTITION KEY, so the
    # entity IN (...) filter prunes to the named partitions). Once the migration ARMS
    # kb_bin_partition_ready, v2 is the coarse index -- ensure it exists and do NOT recreate
    # the legacy table (so `migrate_kb_partition_key.py --drop-legacy` stays dropped and its
    # ~space reclaim sticks across restarts). A fresh / unmigrated DB gets the legacy table
    # exactly as before -- deploy stays inert until the migration is run + armed.
    _armed_row = conn.execute(
        "SELECT value_json FROM checkpoint_state WHERE key = 'kb_bin_partition_ready'"
    ).fetchone()
    try:
        _partition_armed = bool(_armed_row and json.loads(_armed_row[0]).get("ready"))
    except Exception:  # noqa: BLE001 -- malformed checkpoint -> treat as unarmed (safe)
        _partition_armed = False

    if _partition_armed:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vec_bin_v2 USING vec0(
                chunk_id TEXT PRIMARY KEY,
                entity TEXT PARTITION KEY,
                embedding bit[{EMBEDDING_DIM}]
            )
            """
        )
    else:
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vec_bin USING vec0(
                chunk_id TEXT PRIMARY KEY,
                entity TEXT,
                embedding bit[{EMBEDDING_DIM}]
            )
            """
        )

    # Plain btree-indexed blob table holding the exact float32 embeddings for
    # re-rank. vec0 point-lookups by chunk_id degrade to a full scan; this table
    # gives true O(log n) PK reads (COVERING INDEX), so re-ranking ~200 binary
    # candidates with vec_distance_l2 is sub-millisecond.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_vec_f32 (
            chunk_id  TEXT PRIMARY KEY,
            embedding BLOB NOT NULL
        )
        """
    )

    conn.commit()
    # debug, not info: init_schema runs idempotently on every KB instantiation;
    # an info line here pollutes request logs and falsely reads as "cold start".
    log.debug("Knowledge Base schema initialized (embedding_dim=%d)", EMBEDDING_DIM)
