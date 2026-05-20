"""KnowledgeBase — store + search interface over sqlite-vec.

This is the canonical API surface for Phase 3 RAG. Connectors call `upsert_documents`
to add content; retrieval calls `search` at query time to pull relevant chunks.

Doctrine:
- Entity-scoped filtering at retrieval (channel entity ∈ {target_entity, FNDR})
- Recency filtering (default 365-day window)
- Source-aware ranking (newer + more authoritative sources weighted higher — future)
- All chunks include a Slack-mrkdwn `<url|label>` deep_link for citation rendering
"""

import json
import logging
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import embeddings, schema
from .chunker import chunk_text

log = logging.getLogger(__name__)


@dataclass
class Document:
    """One unit of content from a connector — chunked + embedded by KnowledgeBase."""
    source: str                # "fireflies" | "gmail" | "notion" | "drive" | "asana" | "hubspot" | "slack" | "static_md"
    source_id: str             # native id from the source system
    entity: str                # "F3E" | "OSN" | "LEX" | "BDM" | "HJRG" | "FNDR" | "UFL" | "HJRP" | "HJRPROD"
    content: str               # raw text to chunk + embed
    date_created: int | None = None     # unix epoch seconds
    date_modified: int | None = None    # unix epoch seconds
    author: str = ""
    title: str = ""
    deep_link: str = ""        # clickable URL (raw or Slack mrkdwn-wrapped)
    metadata: dict[str, Any] | None = None


@dataclass
class SearchResult:
    """One retrieved chunk from a vector search."""
    chunk_id: str
    source: str
    source_id: str
    entity: str
    title: str
    content: str
    deep_link: str
    date_modified: int | None
    distance: float            # cosine distance (0 = identical, 2 = opposite)


class KnowledgeBaseError(Exception):
    """Raised on KB operation failure."""


def _serialize_vec(embedding: list[float]) -> bytes:
    """Pack a float list into the binary format sqlite-vec expects."""
    return struct.pack(f"{len(embedding)}f", *embedding)


class KnowledgeBase:
    """High-level KB API. Wraps sqlite + sqlite-vec + OpenAI embeddings."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = schema.connect(self.db_path)
        schema.init_schema(self._conn)

    def close(self) -> None:
        self._conn.close()

    # --- Ingest ---

    def upsert_documents(self, docs: Iterable[Document]) -> int:
        """Chunk + embed + store a batch of Documents. Returns count of chunks written.

        Replace-on-conflict for (source, source_id): existing chunks are deleted before
        the new chunks for the same source_id are inserted. This makes incremental sync
        idempotent — re-ingesting a modified Fireflies transcript correctly replaces
        the prior chunks.
        """
        docs_list = list(docs)
        if not docs_list:
            return 0

        # Step 1: chunk each doc, build flat list of (doc, chunk_text, chunk_id)
        chunk_tuples: list[tuple[Document, str, str]] = []
        for doc in docs_list:
            chunks = chunk_text(doc.content)
            for chunk_str in chunks:
                chunk_tuples.append((doc, chunk_str, str(uuid.uuid4())))

        if not chunk_tuples:
            log.info("No non-empty chunks generated from %d docs", len(docs_list))
            return 0

        # Step 2: embed all chunks in batch (OpenAI handles internal batching)
        chunk_texts = [c[1] for c in chunk_tuples]
        try:
            vectors = embeddings.embed_texts(chunk_texts)
        except embeddings.EmbeddingError as exc:
            raise KnowledgeBaseError(f"Embedding failed during upsert: {exc}") from exc

        if len(vectors) != len(chunk_tuples):
            raise KnowledgeBaseError(
                f"Embedding count mismatch: {len(vectors)} vectors for {len(chunk_tuples)} chunks"
            )

        # Step 3: delete existing chunks for these (source, source_id) pairs, then insert
        now = int(time.time())
        cur = self._conn.cursor()

        # Collect distinct (source, source_id) for replace-on-conflict
        seen_keys: set[tuple[str, str]] = set()
        for doc, _, _ in chunk_tuples:
            seen_keys.add((doc.source, doc.source_id))

        for source, source_id in seen_keys:
            # Find existing chunk_ids to delete from vec table too
            cur.execute(
                "SELECT chunk_id FROM knowledge_chunks WHERE source = ? AND source_id = ?",
                (source, source_id),
            )
            old_ids = [row[0] for row in cur.fetchall()]
            if old_ids:
                placeholders = ",".join("?" * len(old_ids))
                cur.execute(
                    f"DELETE FROM knowledge_vec WHERE chunk_id IN ({placeholders})",
                    old_ids,
                )
                cur.execute(
                    f"DELETE FROM knowledge_chunks WHERE chunk_id IN ({placeholders})",
                    old_ids,
                )

        # Insert new chunks
        for (doc, chunk_str, chunk_id), vec in zip(chunk_tuples, vectors):
            cur.execute(
                """INSERT INTO knowledge_chunks
                   (chunk_id, source, source_id, entity, date_created, date_modified,
                    author, title, content, deep_link, metadata, ingested_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chunk_id,
                    doc.source,
                    doc.source_id,
                    doc.entity,
                    doc.date_created,
                    doc.date_modified,
                    doc.author,
                    doc.title,
                    chunk_str,
                    doc.deep_link,
                    json.dumps(doc.metadata) if doc.metadata else None,
                    now,
                ),
            )
            cur.execute(
                "INSERT INTO knowledge_vec (chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, _serialize_vec(vec)),
            )

        self._conn.commit()
        log.info(
            "Upserted %d chunks from %d documents across %d source/source_id keys",
            len(chunk_tuples), len(docs_list), len(seen_keys),
        )
        return len(chunk_tuples)

    # --- Search ---

    def search(
        self,
        query: str,
        entity: str,
        k: int = 10,
        max_age_days: int | None = 365,
        include_fndr: bool = True,
    ) -> list[SearchResult]:
        """Vector search top-K chunks. Filters by entity (incl. FNDR) and recency.

        entity: channel's routed entity code (F3E, OSN, etc.). Chunks for this entity
        AND for FNDR (when include_fndr=True) are eligible.
        k: number of results to return after filtering.
        max_age_days: drop chunks with date_modified older than this. None disables.
        """
        try:
            query_vec = embeddings.embed_query(query)
        except embeddings.EmbeddingError as exc:
            raise KnowledgeBaseError(f"Query embedding failed: {exc}") from exc

        # Build entity filter
        if entity == "FNDR" or not include_fndr:
            entity_filter = (entity,)
        else:
            entity_filter = (entity, "FNDR")

        # sqlite-vec requires LIMIT to be on the vec0 scan directly (not an outer JOIN).
        # Use a CTE to do the knn scan first, then join+filter metadata.
        # Over-fetch by 5x so entity filtering doesn't starve the result set.
        knn_limit = int(k) * 5
        sql = f"""
            WITH vec_knn AS (
                SELECT chunk_id, distance
                FROM knowledge_vec
                WHERE embedding MATCH ?
                LIMIT {knn_limit}
            )
            SELECT
                k.chunk_id, k.source, k.source_id, k.entity, k.title, k.content,
                k.deep_link, k.date_modified, vk.distance
            FROM vec_knn vk
            JOIN knowledge_chunks k ON k.chunk_id = vk.chunk_id
            WHERE k.entity IN ({','.join('?' * len(entity_filter))})
              {f'AND (k.date_modified IS NULL OR k.date_modified > ?)' if max_age_days else ''}
            ORDER BY vk.distance
            LIMIT {int(k)}
        """
        params: list[Any] = [_serialize_vec(query_vec), *entity_filter]
        if max_age_days:
            cutoff = int(time.time()) - (max_age_days * 86400)
            params.append(cutoff)

        rows = self._conn.execute(sql, params).fetchall()

        return [
            SearchResult(
                chunk_id=r[0],
                source=r[1],
                source_id=r[2],
                entity=r[3],
                title=r[4] or "",
                content=r[5],
                deep_link=r[6] or "",
                date_modified=r[7],
                distance=r[8],
            )
            for r in rows
        ]

    # --- Maintenance / introspection ---

    def stats(self) -> dict[str, Any]:
        """Return counts by source + entity for visibility into KB state."""
        cur = self._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM knowledge_chunks")
        total = cur.fetchone()[0]

        cur.execute("SELECT source, COUNT(*) FROM knowledge_chunks GROUP BY source ORDER BY 2 DESC")
        by_source = dict(cur.fetchall())

        cur.execute("SELECT entity, COUNT(*) FROM knowledge_chunks GROUP BY entity ORDER BY 2 DESC")
        by_entity = dict(cur.fetchall())

        return {
            "total_chunks": total,
            "by_source": by_source,
            "by_entity": by_entity,
        }

    def get_sync_state(self, source: str) -> tuple[int, int | None] | None:
        """Return (last_sync_at, last_source_modified) for a source, or None if no record."""
        row = self._conn.execute(
            "SELECT last_sync_at, last_source_modified FROM sync_state WHERE source = ?",
            (source,),
        ).fetchone()
        return tuple(row) if row else None

    def set_sync_state(
        self, source: str, last_sync_at: int, last_source_modified: int | None = None
    ) -> None:
        self._conn.execute(
            """INSERT INTO sync_state (source, last_sync_at, last_source_modified)
               VALUES (?, ?, ?)
               ON CONFLICT(source) DO UPDATE SET
                 last_sync_at = excluded.last_sync_at,
                 last_source_modified = excluded.last_source_modified""",
            (source, last_sync_at, last_source_modified),
        )
        self._conn.commit()
