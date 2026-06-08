"""sqlite + sqlite-vec schema for the Knowledge Base.

Tables:
- knowledge_chunks: one row per chunk with source/entity/date/content/deep_link/metadata
- knowledge_vec: sqlite-vec virtual table holding the 1536-dim float embeddings
- sync_state: per-source watermark tracking for incremental ingest

The embedding dimension (1536) is fixed by OpenAI text-embedding-3-small. If we ever
switch to a different model, this table needs to be rebuilt.
"""

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

    # Virtual vec0 table — must be created separately (DDL has its own syntax).
    # FLOAT vec0: default distance metric is L2 (euclidean). Kept as the
    # fallback brute-force scan when the binary index isn't ready yet.
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vec USING vec0(
            chunk_id TEXT PRIMARY KEY,
            embedding FLOAT[{EMBEDDING_DIM}]
        )
        """
    )

    # Binary-quantized vec0 table for the fast coarse scan (~1/32 the bytes of
    # the float index). `entity` is a vec0 metadata column so the hamming knn
    # can be entity-pre-filtered (prevents result starvation in narrow channels).
    # Coarse candidates are re-ranked against the exact float vectors below.
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
