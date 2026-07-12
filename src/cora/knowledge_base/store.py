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
from .lex_sub_entity import (
    detect_sub_entity,
    is_restricted_lex_ingest,
    restricted_lex_phi_content_drop,
)
from ..finance_doc_classifier import is_financial_document
from ..kb_exclusions import is_dashboard_store_path

log = logging.getLogger(__name__)


def _lex_staff_names() -> set[str]:
    """Staff roster to PRESERVE when deciding the W6-01 PHI-content drop (so a staff
    possessive like 'Harrison Rogers's billing ...' is not read as a care recipient). Fail-soft
    to empty on any org_roles error.

    IMPORTANT (D-073 re-gate): here an EMPTY roster is NOT "the safe direction" — unlike the
    egress redactors (drive_materializer / person_dossier) where over-redact is safe, this is a
    permanent, un-backed ingest DROP whose intent is to KEEP business. With an empty roster,
    _reveals_individual_care_recipient reads every staff possessive as a care recipient and the
    drop OVER-removes staff-attributed business. So the caller SKIPS the drop for a batch when
    this returns empty (see upsert_documents Step 0a); the docs stay + are guarded at retrieval
    by W2-01."""
    try:
        from .. import org_roles
        return {r.name for r in org_roles.all_roles() if getattr(r, "name", "")}
    except Exception:  # noqa: BLE001
        return set()

# Binary-index fast-path tuning.
# Coarse hamming scan over-fetches generously so binary-quantization loss is
# recovered by the exact float re-rank. coarse_k = max(_COARSE_MIN, k*_COARSE_MULT).
# Sized from a recall sweep on worst-case (random-gaussian) vectors: 1000 gives
# ~98% recall@10 there, and real (clustered) embeddings recall higher still. The
# extra candidates cost ~2-3ms of re-rank — negligible against the latency budget.
_COARSE_MIN = 1000
_COARSE_MULT = 50
# checkpoint_state key set by the migration once every chunk has a bin + f32 row.
_BIN_READY_KEY = "kb_bin_index_ready"

# Personal user notes (Org Synthesis Phase 5). HARD INVARIANT (D-034 pattern):
# user_note chunks are EXCLUDED from the general search() paths at the SQL
# layer — they are retrievable ONLY via search_user_notes(), which filters on
# metadata.owner_slack. This is what makes a personal note blast-radius-1:
# every existing caller (Q&A retrieval, sweeps, digests, reconciliation,
# friction mining) excludes notes by construction, with no per-caller opt-out.
USER_NOTE_SOURCE = "user_note"


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
    sub_entity: str | None = None       # intra-entity scope (e.g. "LEX-LLC", "LEX-LTS")


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
    # Added 2026-06-10 for per-user email/Drive access control: the owner
    # check needs metadata.user_email and the Tier-1 strip needs author.
    # Defaulted so existing positional constructions stay valid.
    author: str = ""
    metadata: dict[str, Any] | None = None


# LEX sub-entity visibility: which sub_entity values a given sub-entity channel can see.
# "LEX" (no sub_entity tag) means the chunk is GM-level / cross-sub-entity, always visible.
_LEX_SUB_ENTITY_VISIBILITY: dict[str, tuple[str, ...]] = {
    "LEX-LLC":  ("LEX-LLC",),
    "LEX-LTS":  ("LEX-LTS",),
    "LEX-LBHS": ("LEX-LBHS",),
    "LEX-LLA":  ("LEX-LLA",),
}


def build_sub_entity_filter(sub_entity: str) -> tuple[str, list[str]] | None:
    """Return (sql_fragment, params) to scope KB results to a LEX sub-entity, or None.

    STRICT MODE — only chunks explicitly tagged for this sub-entity are returned.
    Untagged chunks (sub_entity IS NULL) are excluded.

    Rationale: the LEX parent CLAUDE.md, Asana tasks, and Fireflies transcripts
    are all indexed with sub_entity=NULL (GM-level tagging). Those documents
    contain financial data, cap tables, and ownership details for ALL Lex
    sub-entities. Allowing NULL-tagged chunks to pass through is the vector
    by which sibling entity data leaks into sub-entity channels.

    Tradeoff: fewer KB results until sub-entity tagging coverage improves.
    That is acceptable — the sub-entity CLAUDE.md stub provides essential
    context, and an empty KB result is safer than a leaking one.
    """
    visibility = _LEX_SUB_ENTITY_VISIBILITY.get(sub_entity)
    if not visibility:
        return None
    placeholders = ",".join("?" * len(visibility))
    return f"sub_entity IN ({placeholders})", list(visibility)


class KnowledgeBaseError(Exception):
    """Raised on KB operation failure."""


def _serialize_vec(embedding: list[float]) -> bytes:
    """Pack a float list into the binary format sqlite-vec expects."""
    return struct.pack(f"{len(embedding)}f", *embedding)


class KnowledgeBase:
    """High-level KB API. Wraps sqlite + sqlite-vec + OpenAI embeddings."""

    def __init__(self, db_path: Path | str, check_same_thread: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = schema.connect(self.db_path, check_same_thread=check_same_thread)
        schema.init_schema(self._conn)
        # Lazily resolved on first search; cached for the life of the instance.
        # The migration runs with Cora stopped, so a fresh post-restart instance
        # always reads the correct value.
        self._bin_ready: bool | None = None

    def close(self) -> None:
        self._conn.close()

    # --- Ingest ---

    def _delete_chunks_for_keys(self, cur, keys: set[tuple[str, str]]) -> None:
        """Delete every chunk (from knowledge_chunks + both vec tables) for the given
        (source, source_id) keys. Does NOT commit — the caller owns the transaction.
        Used by upsert_documents for replace-on-conflict AND to purge the stale chunks of a
        now-restricted (W6-01-dropped) doc (deletes by key regardless of stored sub_entity)."""
        for source, source_id in keys:
            cur.execute(
                "SELECT chunk_id FROM knowledge_chunks WHERE source = ? AND source_id = ?",
                (source, source_id),
            )
            old_ids = [row[0] for row in cur.fetchall()]
            if not old_ids:
                continue
            placeholders = ",".join("?" * len(old_ids))
            for tbl in ("knowledge_vec_bin", "knowledge_vec_f32", "knowledge_chunks"):
                cur.execute(
                    f"DELETE FROM {tbl} WHERE chunk_id IN ({placeholders})", old_ids
                )

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

        # Step 0-guard: NEVER ingest personal / highly-confidential dashboard stores
        # (capital-raise, oneamerica, travel-points). Universal chokepoint for every
        # path-bearing source -- static_md (source_id IS the path) and drive_asset
        # (metadata.path is the Drive path). drive_sweep carries no path (source_id =
        # bare file id) and is excluded at enumeration instead (kb_exclusions
        # KB_EXCLUDED_FOLDER_IDS wired into drive_sweep). Dashboard read layer,
        # 2026-07-11; "capital-raise KB-exclusion" is a standing directive.
        kept = []
        dropped = 0
        for doc in docs_list:
            meta_path = str((doc.metadata or {}).get("path", ""))
            if is_dashboard_store_path(doc.source_id) or is_dashboard_store_path(meta_path):
                dropped += 1
                continue
            kept.append(doc)
        if dropped:
            log.info("upsert_documents: dropped %d dashboard-store doc(s) (KB-excluded)", dropped)
        docs_list = kept
        if not docs_list:
            return 0

        # Step 0: ingest-time LEX sub-entity tagging (Part 2 of the 5/23 siloing fix).
        # Connectors that don't know the sub-entity write LEX docs with sub_entity=None;
        # without this, nightly syncs accumulate untagged chunks that sub-entity channels
        # can never retrieve (strict filter excludes NULL). Tag here at the choke point
        # so every source is covered. Conservative exactly-one-match rule -- ambiguous or
        # general-LEX content stays NULL (GM-level) by design.
        # metadata.lex_gm_level=True opts a doc OUT of detection (2026-06-11): published
        # DDD policy manuals are deliberately GM-level, but their text mentions
        # sub-entity keywords (HCBS, Day Program) constantly -- auto-detection would
        # scatter chunks of a cross-sub-entity manual into single sub-entity scopes.
        for doc in docs_list:
            if (
                doc.entity == "LEX"
                and doc.sub_entity is None
                and not (doc.metadata or {}).get("lex_gm_level")
            ):
                detected = detect_sub_entity(doc.title, doc.content)
                if detected:
                    doc.sub_entity = detected

        # W6-01 restricted-LEX PHI-content drop is applied PER-CHUNK in Step 1a below (after
        # chunking), NOT here on the whole doc. (Fix-A / D-073 + D-051 re-gate 2026-07-06:
        # a whole-doc decision over a large mixed LBHS/LTS-tagged business doc -- a cash-flow
        # spreadsheet, a P&L, a tracking sheet -- trips the billing leg on billing-words +
        # "Lexington" + SOME name spread across the doc, over-dropping critical BUSINESS.
        # Per-chunk keeps a business chunk unless a client name + billing/dx co-occur LOCALLY.)

        # Step 0b: ingest-time financial-document tagging (Tier 2-Finance).
        # Personal-source docs (gmail/drive_sweep) that look like receipts/
        # invoices/statements get metadata.financial_document=True at the
        # choke point, so every connector — including bulk gmail backfills —
        # is covered. Deterministic + precision-biased (see
        # finance_doc_classifier); absence of the key means "not financial".
        for doc in docs_list:
            if doc.source in ("gmail", "drive_sweep") and not (
                doc.metadata or {}
            ).get("financial_document"):
                if is_financial_document(doc.title, doc.content, doc.author):
                    doc.metadata = {**(doc.metadata or {}), "financial_document": True}

        # Step 1: chunk each doc, build flat list of (doc, chunk_text, chunk_id)
        chunk_tuples: list[tuple[Document, str, str]] = []
        for doc in docs_list:
            for chunk_str in chunk_text(doc.content):
                chunk_tuples.append((doc, chunk_str, str(uuid.uuid4())))

        # Step 1a: W6-01 restricted-LEX PHI-content drop, PER-CHUNK (Fix-A / D-073).
        # Drop a gmail/drive_sweep LBHS/LTS chunk ONLY when THAT chunk's text carries PHI
        # (restricted_lex_phi_content_drop -> phi_guard.non_lex_phi_backstop_trips_individual):
        # clinical framing, or a bare dx/med term WITH a specific named individual, or
        # named-individual program billing. Business chunks (payroll / fees / PTO / aggregate
        # billing, rows that merely mention a dx/med descriptor) are KEPT + retrievable. Per
        # CHUNK (not whole-doc) so a large mixed LBHS/LTS business doc keeps its business
        # chunks; the W2-01 retrieval backstop is the second layer. GM-level LEX / LLC / LLA /
        # non-gmail-drive chunks are out of scope (restricted_lex_phi_content_drop scope gate).
        _phi_filtered_keys: set[tuple[str, str]] = set()
        _has_candidate = any(
            is_restricted_lex_ingest(d.source, d.sub_entity) for d in docs_list
        )
        if _has_candidate and chunk_tuples:
            _staff = _lex_staff_names()
            if not _staff:
                # Roster unavailable/empty -> the PHI decision can't exclude staff possessives,
                # so it would OVER-DROP staff-billing business. Defer for this batch; chunks
                # stay + are guarded at retrieval by W2-01 (D-051 re-gate finding 2).
                log.warning(
                    "W6-01: LEX staff roster unavailable/empty -- deferring restricted-LEX PHI "
                    "chunk drop for this batch (business kept; W2-01 guards at retrieval)"
                )
            else:
                from collections import Counter
                kept_tuples: list[tuple[Document, str, str]] = []
                dropped_counts: Counter = Counter()
                for doc, chunk_str, cid in chunk_tuples:
                    if restricted_lex_phi_content_drop(
                        doc.source, doc.sub_entity, doc.title, chunk_str, _staff
                    ):
                        dropped_counts[(doc.source, doc.source_id, doc.sub_entity, doc.title)] += 1
                        continue
                    kept_tuples.append((doc, chunk_str, cid))
                if dropped_counts:
                    # Per-doc audit (D-051 finding 2B) + record keys so a doc whose chunks were
                    # (partly or wholly) dropped still has its STALE prior chunks replaced (F5).
                    for (src, sid, sub, title), n in dropped_counts.items():
                        _phi_filtered_keys.add((src, sid))
                        log.warning(
                            "W6-01: dropped %d restricted-LEX PHI chunk(s) source=%s "
                            "source_id=%s sub_entity=%s title=%r",
                            n, src, sid, sub, (title or "")[:80],
                        )
                    chunk_tuples = kept_tuples

        if not chunk_tuples:
            # Nothing to insert (empty content, or every restricted chunk PHI-filtered). If a
            # restricted doc had all its chunks dropped, still purge its stale prior chunks
            # (F5); otherwise preserve the original empty-content no-op.
            if _phi_filtered_keys:
                cur = self._conn.cursor()
                self._delete_chunks_for_keys(cur, _phi_filtered_keys)
                self._conn.commit()
            log.info("No chunks to store from %d docs (%d PHI-filtered key(s) purged)",
                     len(docs_list), len(_phi_filtered_keys))
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

        # Replace-on-conflict keys: every doc that still has chunks, PLUS any doc whose chunks
        # were PHI-filtered (so a now-PHI re-ingest replaces its stale prior chunks -- F5).
        seen_keys: set[tuple[str, str]] = set(_phi_filtered_keys)
        for doc, _, _ in chunk_tuples:
            seen_keys.add((doc.source, doc.source_id))

        self._delete_chunks_for_keys(cur, seen_keys)

        # Insert new chunks
        for (doc, chunk_str, chunk_id), vec in zip(chunk_tuples, vectors):
            cur.execute(
                """INSERT INTO knowledge_chunks
                   (chunk_id, source, source_id, entity, sub_entity, date_created, date_modified,
                    author, title, content, deep_link, metadata, ingested_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    chunk_id,
                    doc.source,
                    doc.source_id,
                    doc.entity,
                    doc.sub_entity,
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
            vec_bytes = _serialize_vec(vec)
            # The legacy float vec0 table (knowledge_vec) was dropped 2026-06-08;
            # the binary index (coarse scan) + the float32 blob table (exact
            # re-rank AND the fallback path) are the only vector stores now.
            cur.execute(
                "INSERT INTO knowledge_vec_bin (chunk_id, entity, embedding) "
                "VALUES (?, ?, vec_quantize_binary(?))",
                (chunk_id, doc.entity, vec_bytes),
            )
            cur.execute(
                "INSERT INTO knowledge_vec_f32 (chunk_id, embedding) VALUES (?, ?)",
                (chunk_id, vec_bytes),
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
        sub_entity: str | None = None,
        query_vec: list[float] | None = None,
    ) -> list[SearchResult]:
        """Vector search top-K chunks. Filters by entity (incl. FNDR) and recency.

        entity: channel's routed entity code (F3E, OSN, etc.). Chunks for this entity
        AND for FNDR (when include_fndr=True) are eligible.
        k: number of results to return after filtering.
        max_age_days: drop chunks with date_modified older than this. None disables.
        sub_entity: when set (e.g. "LEX-LLC"), apply intra-entity visibility scoping
        so only chunks tagged for that sub-entity (or untagged) are returned.
        query_vec: pre-computed embedding vector. When provided, skips the OpenAI
        embed_query() call entirely -- caller is responsible for ensuring it was
        produced by the same model (text-embedding-3-small, 1536 dims).
        """
        if query_vec is None:
            try:
                query_vec = embeddings.embed_query(query)
            except embeddings.EmbeddingError as exc:
                raise KnowledgeBaseError(f"Query embedding failed: {exc}") from exc

        # Build entity filter
        if entity == "FNDR" or not include_fndr:
            entity_filter = (entity,)
        else:
            entity_filter = (entity, "FNDR")

        # Build optional sub-entity visibility clause. This is a SECURITY
        # invariant (strict LEX sub-entity scoping); it is applied identically
        # in both the fast and fallback paths, against the authoritative
        # knowledge_chunks table.
        sub_entity_clause = ""
        sub_entity_params: list[Any] = []
        if sub_entity:
            result = build_sub_entity_filter(sub_entity)
            if result:
                sub_entity_clause, sub_entity_params = result

        cutoff = (
            int(time.time()) - (max_age_days * 86400) if max_age_days else None
        )

        if self._is_bin_index_ready():
            return self._search_binary(
                query_vec, entity_filter, k, cutoff,
                sub_entity_clause, sub_entity_params,
            )
        return self._search_float(
            query_vec, entity_filter, k, cutoff,
            sub_entity_clause, sub_entity_params,
        )

    def _is_bin_index_ready(self) -> bool:
        """True once the migration has backfilled bin + f32 rows for every chunk.

        Cached for the instance lifetime — the migration runs with Cora stopped,
        so a fresh post-restart instance always reads the right value.
        """
        if self._bin_ready is None:
            cp = self.get_checkpoint(_BIN_READY_KEY)
            self._bin_ready = bool(cp and cp.get("ready"))
        return self._bin_ready

    def _rows_to_results(self, rows: list) -> list[SearchResult]:
        results: list[SearchResult] = []
        for r in rows:
            metadata = None
            if len(r) > 10 and r[10]:
                try:
                    metadata = json.loads(r[10])
                except (ValueError, TypeError):
                    metadata = None
            results.append(
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
                    author=(r[9] or "") if len(r) > 9 else "",
                    metadata=metadata,
                )
            )
        return results

    def _search_binary(
        self,
        query_vec: list[float],
        entity_filter: tuple[str, ...],
        k: int,
        cutoff: int | None,
        sub_entity_clause: str,
        sub_entity_params: list[Any],
    ) -> list[SearchResult]:
        """Fast path: binary coarse hamming scan -> exact float32 L2 re-rank.

        Distances returned are vec_distance_l2 — identical metric to the float
        fallback (vec0 FLOAT default is L2), so the caller's distance threshold
        is unchanged.
        """
        qbytes = _serialize_vec(query_vec)
        coarse_k = max(_COARSE_MIN, int(k) * _COARSE_MULT)
        ent_ph = ",".join("?" * len(entity_filter))

        # Stage 1: coarse candidate generation over the binary index, entity
        # pre-filtered (vec0 metadata column) so narrow channels aren't starved.
        cand_ids = [
            row[0]
            for row in self._conn.execute(
                f"""
                SELECT chunk_id FROM knowledge_vec_bin
                WHERE embedding MATCH vec_quantize_binary(?)
                  AND entity IN ({ent_ph})
                  AND k = ?
                ORDER BY distance
                """,
                [qbytes, *entity_filter, coarse_k],
            ).fetchall()
        ]
        if not cand_ids:
            return []

        # Stage 2: exact re-rank of candidates against float32 vectors (true PK
        # reads from knowledge_vec_f32), applying the authoritative entity /
        # recency / sub-entity filters against knowledge_chunks.
        cand_ph = ",".join("?" * len(cand_ids))
        sql = f"""
            SELECT
                k.chunk_id, k.source, k.source_id, k.entity, k.title, k.content,
                k.deep_link, k.date_modified,
                vec_distance_l2(?, f.embedding) AS distance,
                k.author, k.metadata
            FROM knowledge_chunks k
            JOIN knowledge_vec_f32 f ON f.chunk_id = k.chunk_id
            WHERE k.chunk_id IN ({cand_ph})
              AND k.entity IN ({ent_ph})
              AND k.source != '{USER_NOTE_SOURCE}'
              {'AND (k.date_modified IS NULL OR k.date_modified > ?)' if cutoff is not None else ''}
              {f'AND {sub_entity_clause}' if sub_entity_clause else ''}
            ORDER BY distance
            LIMIT {int(k)}
        """
        params: list[Any] = [qbytes, *cand_ids, *entity_filter]
        if cutoff is not None:
            params.append(cutoff)
        params.extend(sub_entity_params)

        rows = self._conn.execute(sql, params).fetchall()
        return self._rows_to_results(rows)

    def _search_float(
        self,
        query_vec: list[float],
        entity_filter: tuple[str, ...],
        k: int,
        cutoff: int | None,
        sub_entity_clause: str,
        sub_entity_params: list[Any],
    ) -> list[SearchResult]:
        """Fallback: exact brute-force float32 L2 scan over knowledge_vec_f32.

        Used only when the binary index is not ready (fresh DB pre-migration, or
        the checkpoint cleared). Computes vec_distance_l2 against every float
        vector for the entity -- correct but O(n), so it is the slow safety net,
        not the hot path. The legacy float vec0 table (knowledge_vec) this used
        to scan was dropped 2026-06-08; knowledge_vec_f32 holds the same vectors,
        so the fallback survives the drop with no re-migration needed.
        """
        qbytes = _serialize_vec(query_vec)
        ent_ph = ",".join("?" * len(entity_filter))
        sql = f"""
            SELECT
                k.chunk_id, k.source, k.source_id, k.entity, k.title, k.content,
                k.deep_link, k.date_modified,
                vec_distance_l2(?, f.embedding) AS distance,
                k.author, k.metadata
            FROM knowledge_chunks k
            JOIN knowledge_vec_f32 f ON f.chunk_id = k.chunk_id
            WHERE k.entity IN ({ent_ph})
              AND k.source != '{USER_NOTE_SOURCE}'
              {'AND (k.date_modified IS NULL OR k.date_modified > ?)' if cutoff is not None else ''}
              {f'AND {sub_entity_clause}' if sub_entity_clause else ''}
            ORDER BY distance
            LIMIT {int(k)}
        """
        params: list[Any] = [qbytes, *entity_filter]
        if cutoff is not None:
            params.append(cutoff)
        params.extend(sub_entity_params)

        rows = self._conn.execute(sql, params).fetchall()
        return self._rows_to_results(rows)

    # --- Owner-scoped retrieval (Tier 2 / Tier 2-Finance) ---

    def search_owned(
        self,
        query: str,
        owner_emails: frozenset[str] | set[str] | None,
        sources: tuple[str, ...] = ("gmail", "drive_sweep"),
        k: int = 12,
        financial_only: bool = False,
        query_vec: list[float] | None = None,
        recency_first: bool = False,
    ) -> list[SearchResult]:
        """Exact vector search restricted to specific mailbox owners.

        Used by the Tier-2 explicit-retrieval paths (historical_access /
        finance_receipts), NEVER by general Q&A retrieval. Differences from
        search():
          - owner filter: metadata.user_email IN owner_emails. None means ANY
            mailbox — only the Tier 2-Finance path may pass None, and only
            with financial_only=True.
          - source filter: personal sources only (gmail/drive_sweep).
          - financial_only: metadata.financial_document must be true.
          - NO entity filter and NO recency cutoff — this is deliberately a
            historical search over the asker's own (or finance-authorized)
            corpus. Entity scoping is a channel concept; a mailbox owner may
            always see their own mail.

        Implementation: brute-force exact vec_distance_l2 over the FILTERED
        subset via the knowledge_vec_f32 join. A single mailbox is a small
        fraction of the corpus, so the O(n-subset) scan is the simple, exact,
        recall-perfect choice over coarse-then-filter (which can starve small
        mailboxes out of the candidate set).
        """
        if owner_emails is None and not financial_only:
            raise KnowledgeBaseError(
                "search_owned: owner_emails=None (any mailbox) requires financial_only=True"
            )
        if USER_NOTE_SOURCE in sources:
            raise KnowledgeBaseError(
                "search_owned: user_note retrieval must go through search_user_notes "
                "(owner_slack-filtered) — never the mailbox path"
            )
        if query_vec is None:
            try:
                query_vec = embeddings.embed_query(query)
            except embeddings.EmbeddingError as exc:
                raise KnowledgeBaseError(f"Query embedding failed: {exc}") from exc

        qbytes = _serialize_vec(query_vec)
        src_ph = ",".join("?" * len(sources))
        where = [f"k.source IN ({src_ph})"]
        params: list[Any] = [qbytes, *sources]

        if owner_emails is not None:
            owners = sorted({e.strip().lower() for e in owner_emails if e})
            if not owners:
                return []
            own_ph = ",".join("?" * len(owners))
            where.append(
                f"LOWER(json_extract(k.metadata, '$.user_email')) IN ({own_ph})"
            )
            params.extend(owners)

        if financial_only:
            where.append("json_extract(k.metadata, '$.financial_document') = 1")

        # F-21: for a "latest / most recent email" ask, ordering purely by vector
        # distance can rank a January email above a July one. Pull a WIDER relevant
        # candidate set by distance (so a recent-but-less-similar message is not
        # starved out), then re-order those by date_modified DESC and take k. General
        # (non-recency) retrieval keeps the pure best-match ordering.
        pull_k = max(int(k), 40) if recency_first else int(k)
        sql = f"""
            SELECT
                k.chunk_id, k.source, k.source_id, k.entity, k.title, k.content,
                k.deep_link, k.date_modified,
                vec_distance_l2(?, f.embedding) AS distance,
                k.author, k.metadata
            FROM knowledge_chunks k
            JOIN knowledge_vec_f32 f ON f.chunk_id = k.chunk_id
            WHERE {" AND ".join(where)}
            ORDER BY distance
            LIMIT {pull_k}
        """
        rows = self._conn.execute(sql, params).fetchall()
        results = self._rows_to_results(rows)
        if recency_first:
            results.sort(key=lambda r: (r.date_modified or 0), reverse=True)
            results = results[: int(k)]
        return results

    # --- Personal user notes (Org Synthesis Phase 5, deliverable 1) ---

    def search_user_notes(
        self,
        query: str,
        owner_slack: str,
        k: int = 5,
        entity_scope: tuple[str, ...] | None = None,
        unrestricted: bool = False,
        query_vec: list[float] | None = None,
    ) -> list[SearchResult]:
        """Vector search over personal user notes — the ONLY retrieval path
        for source='user_note' chunks (general search() excludes them in SQL).

        HARD INVARIANT (enforced here, never in prompts — D-034): a note is
        returned ONLY when metadata.owner_slack equals owner_slack. The single
        exception is unrestricted=True (the D-043 historical-access allowlist,
        i.e. Harrison) — the CALLER must verify that via
        historical_access.is_unrestricted before passing it.

        entity_scope: acceptable note entity values for the current channel
        (e.g. ("F3E", "FNDR")). None means no entity filter — DM retrieval,
        where the asker's whole note set is theirs to search. This is how a
        LEX-scoped (potentially PHI-bearing, custodian-only by save-time
        enforcement) note never surfaces in a non-LEX channel reply.

        Exact brute-force scan over the filtered subset (search_owned pattern):
        the notes partition is tiny, and coarse-then-filter could starve it
        out of the binary candidate set entirely.
        """
        if not owner_slack and not unrestricted:
            return []
        if query_vec is None:
            try:
                query_vec = embeddings.embed_query(query)
            except embeddings.EmbeddingError as exc:
                raise KnowledgeBaseError(f"Query embedding failed: {exc}") from exc

        qbytes = _serialize_vec(query_vec)
        where = ["k.source = ?"]
        params: list[Any] = [qbytes, USER_NOTE_SOURCE]
        if not unrestricted:
            where.append("json_extract(k.metadata, '$.owner_slack') = ?")
            params.append(owner_slack)
        if entity_scope is not None:
            ents = [e for e in entity_scope if e]
            if not ents:
                return []
            ent_ph = ",".join("?" * len(ents))
            where.append(f"k.entity IN ({ent_ph})")
            params.extend(ents)

        sql = f"""
            SELECT
                k.chunk_id, k.source, k.source_id, k.entity, k.title, k.content,
                k.deep_link, k.date_modified,
                vec_distance_l2(?, f.embedding) AS distance,
                k.author, k.metadata
            FROM knowledge_chunks k
            JOIN knowledge_vec_f32 f ON f.chunk_id = k.chunk_id
            WHERE {" AND ".join(where)}
            ORDER BY distance
            LIMIT {int(k)}
        """
        rows = self._conn.execute(sql, params).fetchall()
        return self._rows_to_results(rows)

    def list_user_notes(self, owner_slack: str, limit: int = 50) -> list[dict[str, Any]]:
        """All notes owned by owner_slack, newest first — one dict per note
        (source_id), not per chunk. Owner filter is SQL-layer; there is no
        unrestricted variant here by design (note management is owner-only
        in deliverable 1)."""
        if not owner_slack:
            return []
        rows = self._conn.execute(
            """
            SELECT source_id, title, content, date_created, entity, metadata
            FROM knowledge_chunks
            WHERE source = ?
              AND json_extract(metadata, '$.owner_slack') = ?
            ORDER BY date_created DESC, source_id
            """,
            (USER_NOTE_SOURCE, owner_slack),
        ).fetchall()
        notes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source_id, title, content, date_created, entity, metadata in rows:
            if source_id in seen:
                continue  # multi-chunk note — first (full-text) chunk wins
            seen.add(source_id)
            notes.append({
                "note_id": source_id,
                "title": title or "",
                "content": content or "",
                "date_created": date_created,
                "entity": entity,
                "metadata": json.loads(metadata) if metadata else {},
            })
            if len(notes) >= limit:
                break
        return notes

    def delete_user_note(self, note_id: str, owner_slack: str) -> int:
        """Delete a personal note (all its chunks + vector rows). Returns the
        number of chunks removed (0 = no such note, or not the caller's note).

        Owner check is part of the SQL WHERE — a non-owner delete is a no-op,
        indistinguishable from a missing note (no existence leak)."""
        if not note_id or not owner_slack:
            return 0
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT chunk_id FROM knowledge_chunks
            WHERE source = ? AND source_id = ?
              AND json_extract(metadata, '$.owner_slack') = ?
            """,
            (USER_NOTE_SOURCE, note_id, owner_slack),
        )
        chunk_ids = [row[0] for row in cur.fetchall()]
        if not chunk_ids:
            return 0
        ph = ",".join("?" * len(chunk_ids))
        cur.execute(f"DELETE FROM knowledge_vec_bin WHERE chunk_id IN ({ph})", chunk_ids)
        cur.execute(f"DELETE FROM knowledge_vec_f32 WHERE chunk_id IN ({ph})", chunk_ids)
        cur.execute(f"DELETE FROM knowledge_chunks WHERE chunk_id IN ({ph})", chunk_ids)
        self._conn.commit()
        return len(chunk_ids)

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

    def get_chunks_since(
        self,
        *,
        source: str,
        entity: str,
        since_ts: int,
        exclude_sub_entities: tuple[str, ...] | None = None,
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """Non-vector metadata query: chunks for (source, entity) ingested after since_ts.

        Drive-materialization (2026-06-29): the nightly materializer reads the day's
        NEW swept chunks WITHOUT an embedding query (no vector search, no index rebuild).
        Returned OLDEST-FIRST so the caller can advance a per-(entity, source) watermark
        to the max ingested_at it actually processed (a fail-closed per-entity skip then
        never drops a day). `ingested_at` is unix-epoch seconds.

        exclude_sub_entities drops those sub_entity tags while KEEPING NULL (GM-level)
        rows — used for LEX to hard-exclude LEX-LBHS (42 CFR Part 2) from materialization
        while still materializing GM-level + LLC/LTS/LLA content.

        user_note is never a valid source here (the blast-radius-1 invariant): callers
        pass only swept sources, and this guard makes that structural.
        """
        if source == USER_NOTE_SOURCE:
            return []
        sql = (
            "SELECT chunk_id, source_id, title, content, date_modified, "
            "ingested_at, sub_entity, author, deep_link "
            "FROM knowledge_chunks WHERE source = ? AND entity = ? AND ingested_at > ?"
        )
        params: list[Any] = [source, entity, int(since_ts)]
        if exclude_sub_entities:
            ph = ",".join("?" * len(exclude_sub_entities))
            sql += f" AND (sub_entity IS NULL OR sub_entity NOT IN ({ph}))"
            params.extend(exclude_sub_entities)
        # chunk_id secondary key makes the LIMIT page deterministic (a whole upsert batch
        # shares one ingested_at second, so ties are common); the caller pairs this with a
        # boundary-second re-fetch when the page is full (see drive_materializer.run()).
        sql += " ORDER BY ingested_at ASC, chunk_id ASC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "chunk_id": r[0],
                "source_id": r[1],
                "title": r[2] or "",
                "content": r[3] or "",
                "date_modified": r[4],
                "ingested_at": r[5],
                "sub_entity": r[6],
                "author": r[7] or "",
                "deep_link": r[8] or "",
            }
            for r in rows
        ]

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

    # ── Resumable-sweep checkpoint helpers ─────────────────────────────────────

    def get_checkpoint(self, key: str) -> dict | None:
        """Return the checkpoint dict stored under *key*, or None if absent."""
        import json as _json
        row = self._conn.execute(
            "SELECT value_json FROM checkpoint_state WHERE key = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        try:
            return _json.loads(row[0])
        except Exception:
            return None

    def set_checkpoint(self, key: str, data: dict) -> None:
        """Persist a checkpoint dict under *key* (create or replace)."""
        import json as _json
        import time as _time
        self._conn.execute(
            """INSERT INTO checkpoint_state (key, value_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value_json = excluded.value_json,
                 updated_at = excluded.updated_at""",
            (key, _json.dumps(data), int(_time.time())),
        )
        self._conn.commit()

    def delete_checkpoint(self, key: str) -> None:
        """Remove a checkpoint record (idempotent — no error if key missing)."""
        self._conn.execute(
            "DELETE FROM checkpoint_state WHERE key = ?",
            (key,),
        )
        self._conn.commit()
