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


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a sqlite connection with the vec0 extension loaded + WAL mode."""
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables + indexes idempotently. Safe to call on every boot."""
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            chunk_id      TEXT PRIMARY KEY,
            source        TEXT NOT NULL,
            source_id     TEXT NOT NULL,
            entity        TEXT NOT NULL,
            sub_entity    TEXT,
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
        CREATE INDEX IF NOT EXISTS idx_chunks_sub_entity   ON knowledge_chunks(sub_entity);
        CREATE INDEX IF NOT EXISTS idx_chunks_date_mod     ON knowledge_chunks(date_modified DESC);
        CREATE INDEX IF NOT EXISTS idx_chunks_source_id    ON knowledge_chunks(source, source_id);

        CREATE TABLE IF NOT EXISTS sync_state (
            source                TEXT PRIMARY KEY,
            last_sync_at          INTEGER NOT NULL,
            last_source_modified  INTEGER
        );
        """
    )

    # Migration: add sub_entity column to existing databases (idempotent)
    try:
        conn.execute("ALTER TABLE knowledge_chunks ADD COLUMN sub_entity TEXT")
        conn.commit()
        log.info("Migrated knowledge_chunks: added sub_entity column")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Virtual vec0 table — must be created separately (DDL has its own syntax)
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vec USING vec0(
            chunk_id TEXT PRIMARY KEY,
            embedding FLOAT[{EMBEDDING_DIM}]
        )
        """
    )

    conn.commit()
    log.info("Knowledge Base schema initialized (embedding_dim=%d)", EMBEDDING_DIM)
