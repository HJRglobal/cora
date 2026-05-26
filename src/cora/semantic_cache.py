"""Semantic response cache for Cora.

Caches responses keyed by question-embedding similarity, entity-scoped.

Cache hit: cosine similarity between new question and stored question >= SIMILARITY_THRESHOLD
           AND the stored entry has not expired (TTL).

Cache bypass: financial questions (caller passes bypass=True via intent routing).

Design notes:
- Stored in a `semantic_cache` table in the same SQLite DB as the KB.
- Embeddings stored as packed float32 blobs (same format as knowledge_vec).
- Lookup: fetch all live entries for entity → cosine dot product in Python.
  OpenAI text-embedding-3-small returns L2-normalised vectors, so
  cosine_similarity == dot_product. No additional normalisation needed.
- Scales well up to ~500 cached entries per entity (Python loop is ~microseconds).
  Beyond that, we can index via sqlite-vec — not needed yet.
- Entity-scoped: same question in #f3e-leadership vs #osn-leadership returns
  different cached answers because entity is part of the cache key.
"""

import logging
import sqlite3
import struct
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.95          # cosine similarity gate for a cache hit
DEFAULT_TTL = 1800                   # 30 minutes — most operational questions
MAX_ENTRIES_PER_ENTITY = 500         # prune oldest beyond this per entity


# ── Embedding serialisation (matches knowledge_vec blob format) ──────────────

def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product — equals cosine similarity for L2-normalised vectors."""
    return sum(x * y for x, y in zip(a, b))


# ── Cache class ───────────────────────────────────────────────────────────────

class SemanticCache:
    """Entity-scoped semantic response cache backed by SQLite.

    Shares the KB database file. Does NOT load the sqlite-vec extension —
    plain sqlite3 is sufficient for the cache table.
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_table()

    def _init_table(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS semantic_cache (
                cache_id    TEXT PRIMARY KEY,
                entity      TEXT NOT NULL,
                question    TEXT NOT NULL,
                embedding   BLOB NOT NULL,
                response    TEXT NOT NULL,
                created_at  INTEGER NOT NULL,
                ttl_seconds INTEGER NOT NULL DEFAULT 1800,
                hit_count   INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_cache_entity  ON semantic_cache(entity);
            CREATE INDEX IF NOT EXISTS idx_cache_created ON semantic_cache(created_at DESC);
        """)
        self._conn.commit()

    # ── Public API ────────────────────────────────────────────────────────────

    def lookup(
        self,
        entity: str,
        question_embedding: list[float],
    ) -> str | None:
        """Return a cached response if a semantically similar question was answered
        recently, else None.

        Fetches all live (non-expired) entries for the entity and scores each by
        cosine similarity. The entry with the highest similarity above
        SIMILARITY_THRESHOLD is returned (and its hit_count incremented).
        """
        now = int(time.time())
        rows = self._conn.execute(
            """
            SELECT cache_id, embedding, response, hit_count
            FROM semantic_cache
            WHERE entity = ?
              AND (created_at + ttl_seconds) > ?
            """,
            (entity, now),
        ).fetchall()

        if not rows:
            return None

        best_score = -1.0
        best_id: str | None = None
        best_response: str | None = None
        best_hits = 0

        for cache_id, blob, response, hit_count in rows:
            score = _dot(question_embedding, _unpack(blob))
            if score > best_score:
                best_score = score
                best_id = cache_id
                best_response = response
                best_hits = hit_count

        if best_score >= SIMILARITY_THRESHOLD and best_id:
            self._conn.execute(
                "UPDATE semantic_cache SET hit_count = ? WHERE cache_id = ?",
                (best_hits + 1, best_id),
            )
            self._conn.commit()
            log.info(
                "semantic_cache HIT entity=%s similarity=%.4f total_hits=%d",
                entity, best_score, best_hits + 1,
            )
            return best_response

        log.debug(
            "semantic_cache MISS entity=%s candidates=%d best_sim=%.4f",
            entity, len(rows), best_score,
        )
        return None

    def store(
        self,
        entity: str,
        question: str,
        question_embedding: list[float],
        response: str,
        ttl_seconds: int = DEFAULT_TTL,
    ) -> None:
        """Store a question + response. Prunes stale and excess entries after write."""
        now = int(time.time())
        self._conn.execute(
            """
            INSERT INTO semantic_cache
              (cache_id, entity, question, embedding, response, created_at, ttl_seconds, hit_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                str(uuid.uuid4()),
                entity,
                question,
                _pack(question_embedding),
                response,
                now,
                ttl_seconds,
            ),
        )
        self._conn.commit()
        self._prune(entity)
        log.debug(
            "semantic_cache STORE entity=%s q_chars=%d ttl=%ds",
            entity, len(question), ttl_seconds,
        )

    def invalidate_entity(self, entity: str) -> int:
        """Delete all cache entries for an entity. Returns count deleted.

        Call this when a significant knowledge update lands for an entity
        (e.g., after a KB sync that changed many chunks) to prevent stale hits.
        """
        cur = self._conn.execute(
            "DELETE FROM semantic_cache WHERE entity = ?", (entity,)
        )
        self._conn.commit()
        deleted = cur.rowcount
        log.info("semantic_cache invalidated entity=%s deleted=%d", entity, deleted)
        return deleted

    def stats(self) -> dict:
        """Return per-entity cache stats for observability."""
        rows = self._conn.execute(
            """
            SELECT entity, COUNT(*), SUM(hit_count), MAX(created_at)
            FROM semantic_cache
            GROUP BY entity
            ORDER BY 2 DESC
            """
        ).fetchall()
        return {
            r[0]: {
                "entries": r[1],
                "total_hits": r[2] or 0,
                "newest_entry_age_s": int(time.time()) - (r[3] or 0),
            }
            for r in rows
        }

    def close(self) -> None:
        self._conn.close()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _prune(self, entity: str) -> None:
        """Remove expired entries and trim to MAX_ENTRIES_PER_ENTITY per entity."""
        now = int(time.time())
        # Delete all expired
        self._conn.execute(
            "DELETE FROM semantic_cache WHERE (created_at + ttl_seconds) <= ?",
            (now,),
        )
        # Trim to max per entity — keep the newest MAX_ENTRIES_PER_ENTITY entries
        self._conn.execute(
            """
            DELETE FROM semantic_cache
            WHERE cache_id IN (
                SELECT cache_id FROM semantic_cache
                WHERE entity = ?
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (entity, MAX_ENTRIES_PER_ENTITY),
        )
        self._conn.commit()


# ── Module-level singleton ────────────────────────────────────────────────────
# Initialised lazily on first use via get_cache(). Uses the same DB path as the KB.

_cache_instance: SemanticCache | None = None
_KB_DB_PATH = Path(__file__).parent.parent.parent / "data" / "cora_kb.db"


def get_cache() -> SemanticCache:
    """Return the process-lifetime SemanticCache singleton."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = SemanticCache(_KB_DB_PATH)
    return _cache_instance
