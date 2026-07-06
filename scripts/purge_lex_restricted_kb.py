#!/usr/bin/env python3
"""Purge already-ingested restricted-LEX (LBHS/LTS) KB chunks (audit W6-01, 2026-07-05).

Pairs with the ingest-time drop (store.upsert_documents Step 0a + lex_sub_entity.
is_restricted_lex_ingest): the drop stops FUTURE gmail/drive_sweep LBHS/LTS ingestion;
this removes what is already in the KB.

SCOPE (conservative-by-default, verify-first 2026-07-05):
  DEFAULT  = sub_entity IN (LEX-LBHS, LEX-LTS) AND source IN (gmail, drive_sweep)
             -- the exact deny-list gap the finding names (42 CFR Part 2 / PT-15 PHI that
             slipped in via the non-Slack sweeps). ~1,785 chunks.
  The 15 NON-default LBHS/LTS rows (static_md session-captures, #lex/#shaun-leadership
  slack threads, one drive_asset "LBHS unpaid management fees" business file) are
  REPORTED but NOT deleted by default -- they are curated / GM-level / business-ops
  content, exactly the "purge over-deleting non-PHI LEX-business rows" risk. Widen only
  on Harrison's explicit decision:
      --include-source slack --include-source static_md --include-source drive_asset
      --all-sources          # every LBHS/LTS chunk regardless of source (true 0-remaining)

Reversibility: --apply first writes a full row-backup (chunk_id + all columns) to
data/purge-lex-restricted-<UTC>.bak.jsonl BEFORE deleting, so the deleted rows can be
audited / re-inserted (vectors restore by re-ingest from source). Belt: copy cora_kb.db
before --apply.

Usage (--dry-run is read-only + safe anytime; STOP Cora before --apply):
    .venv\\Scripts\\python.exe scripts\\purge_lex_restricted_kb.py                 # dry-run
    .venv\\Scripts\\python.exe scripts\\purge_lex_restricted_kb.py --apply         # delete (Cora stopped)
    .venv\\Scripts\\python.exe scripts\\purge_lex_restricted_kb.py --all-sources   # widen scope
After --apply, reclaim disk: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py

Exit codes: 0 ok, 1 fatal, 3 Cora appears to be running (--apply refused; pass --force).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from cora.knowledge_base import schema  # noqa: E402
from cora.knowledge_base.lex_sub_entity import (  # noqa: E402
    RESTRICTED_INGEST_SOURCES,
    RESTRICTED_INGEST_SUB_ENTITIES,
)

KB_DB_PATH = _REPO / "data" / "cora_kb.db"
HEARTBEAT_PATH = _REPO / "data" / "health" / "heartbeat.txt"
_BATCH = 500


def _heartbeat_is_fresh(max_age_s: int = 180) -> bool:
    """True if the live bot's heartbeat was written < max_age_s ago (service running).
    Mirrors prune_kb_retention.py / migrate_kb_binary_index.py so the destructive-KB
    scripts share one running-bot guard."""
    try:
        return (time.time() - HEARTBEAT_PATH.stat().st_mtime) < max_age_s
    except OSError:
        return False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("purge-lex-restricted-kb")


def _tag_placeholders() -> str:
    return ",".join("?" * len(RESTRICTED_INGEST_SUB_ENTITIES))


def source_breakdown(conn) -> list[tuple[str, str, int]]:
    """Read-only. All LBHS/LTS chunks grouped by (sub_entity, source)."""
    ph = _tag_placeholders()
    return conn.execute(
        f"SELECT sub_entity, source, COUNT(*) FROM knowledge_chunks "
        f"WHERE sub_entity IN ({ph}) GROUP BY sub_entity, source ORDER BY sub_entity, 3 DESC",
        RESTRICTED_INGEST_SUB_ENTITIES,
    ).fetchall()


def target_chunk_ids(conn, sources: tuple[str, ...] | None) -> set[str]:
    """Read-only. chunk_ids for LBHS/LTS restricted to *sources* (None = all sources)."""
    tag_ph = _tag_placeholders()
    if sources is None:
        rows = conn.execute(
            f"SELECT chunk_id FROM knowledge_chunks WHERE sub_entity IN ({tag_ph})",
            RESTRICTED_INGEST_SUB_ENTITIES,
        ).fetchall()
    else:
        src_ph = ",".join("?" * len(sources))
        rows = conn.execute(
            f"SELECT chunk_id FROM knowledge_chunks "
            f"WHERE sub_entity IN ({tag_ph}) AND source IN ({src_ph})",
            (*RESTRICTED_INGEST_SUB_ENTITIES, *sources),
        ).fetchall()
    return {r[0] for r in rows}


def non_default_rows(conn) -> list[tuple]:
    """Read-only. Identity of LBHS/LTS chunks OUTSIDE the default (gmail/drive_sweep)
    scope -- the ones a widened purge would additionally delete (for Harrison review)."""
    tag_ph = _tag_placeholders()
    src_ph = ",".join("?" * len(RESTRICTED_INGEST_SOURCES))
    return conn.execute(
        f"SELECT sub_entity, source, title, source_id FROM knowledge_chunks "
        f"WHERE sub_entity IN ({tag_ph}) AND source NOT IN ({src_ph}) "
        f"ORDER BY source, sub_entity",
        (*RESTRICTED_INGEST_SUB_ENTITIES, *RESTRICTED_INGEST_SOURCES),
    ).fetchall()


def backup_rows(conn, chunk_ids: set[str], out_path: Path) -> int:
    """Write full deleted-row content to a JSONL backup BEFORE deletion (reversible-audit)."""
    ids = list(chunk_ids)
    written = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for i in range(0, len(ids), _BATCH):
            batch = ids[i:i + _BATCH]
            ph = ",".join("?" * len(batch))
            rows = conn.execute(
                f"SELECT chunk_id, source, source_id, entity, sub_entity, date_created, "
                f"date_modified, author, title, content, deep_link, metadata, ingested_at "
                f"FROM knowledge_chunks WHERE chunk_id IN ({ph})",
                batch,
            ).fetchall()
            cols = ["chunk_id", "source", "source_id", "entity", "sub_entity",
                    "date_created", "date_modified", "author", "title", "content",
                    "deep_link", "metadata", "ingested_at"]
            for row in rows:
                fh.write(json.dumps(dict(zip(cols, row)), ensure_ascii=False) + "\n")
                written += 1
    return written


def delete_chunks(conn, chunk_ids: set[str]) -> dict:
    """Batched delete from all 3 tables. Returns rows deleted per table."""
    totals = {"knowledge_vec_bin": 0, "knowledge_vec_f32": 0, "knowledge_chunks": 0}
    ids = list(chunk_ids)
    for i in range(0, len(ids), _BATCH):
        batch = ids[i:i + _BATCH]
        ph = ",".join("?" * len(batch))
        for tbl in ("knowledge_vec_bin", "knowledge_vec_f32", "knowledge_chunks"):
            cur = conn.execute(f"DELETE FROM {tbl} WHERE chunk_id IN ({ph})", batch)
            totals[tbl] += cur.rowcount
    conn.commit()
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge restricted-LEX (LBHS/LTS) KB chunks (W6-01).")
    ap.add_argument("--apply", action="store_true", help="Delete (default is a read-only dry-run).")
    ap.add_argument("--dry-run", action="store_true", help="Report only (this is the default).")
    ap.add_argument("--db", default=str(KB_DB_PATH), help="Path to the KB sqlite DB.")
    ap.add_argument("--include-source", action="append", default=[],
                    help="Add a source beyond the default gmail/drive_sweep (repeatable): "
                         "slack | static_md | drive_asset.")
    ap.add_argument("--all-sources", action="store_true",
                    help="Purge EVERY LBHS/LTS chunk regardless of source (true 0-remaining).")
    ap.add_argument("--force", action="store_true",
                    help="Skip the heartbeat (Cora-running) safety guard.")
    args = ap.parse_args()
    apply_changes = args.apply and not args.dry_run

    # Running-bot guard: never delete rows out from under the live service. delete_chunks
    # holds the WAL write lock across the batched loop; a concurrent bot writer would block
    # (busy_timeout) then raise. Refuse --apply while the heartbeat is fresh (D-051 finding 6).
    if apply_changes and _heartbeat_is_fresh() and not args.force:
        log.error("Cora heartbeat is fresh (<180s) -- the service appears to be RUNNING. "
                  "Stop the cowork-cora-service task first, or pass --force.")
        return 3

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("KB database not found: %s", db_path)
        return 1

    if args.all_sources:
        sources: tuple[str, ...] | None = None
    else:
        sources = tuple(RESTRICTED_INGEST_SOURCES) + tuple(
            s.strip() for s in args.include_source if s.strip()
        )

    conn = schema.connect(db_path)
    try:
        log.info("=== Restricted-LEX (LBHS/LTS) purge scope ===")
        log.info("  Full LBHS/LTS population by (sub_entity, source):")
        for sub, src, n in source_breakdown(conn):
            log.info("      %-10s %-12s %d", sub, src, n)

        ids = target_chunk_ids(conn, sources)
        scope_label = "ALL sources" if sources is None else ", ".join(sources)
        log.info("  Purge scope = sources[%s] -> %d chunk(s) targeted", scope_label, len(ids))

        # Always list the NON-default rows — the widest scope (--all-sources) is the MOST
        # destructive and must be the MOST transparent, not silent (D-051 finding 7).
        nd = non_default_rows(conn)
        if nd:
            in_scope_extra = {s.strip() for s in args.include_source if s.strip()}
            log.info("  --- NON-default LBHS/LTS rows (curated/GM/business — REVIEW; "
                     "add --include-source or --all-sources to purge) ---")
            for sub, src, title, sid in nd:
                will_purge = sources is None or src in in_scope_extra
                mark = "WILL PURGE" if will_purge else "kept"
                log.info("      [%s] %-10s %-11s | %s", mark, sub, src, (title or "(untitled)")[:70])

        if not apply_changes:
            log.info("Dry-run -- nothing deleted. Re-run with --apply (Cora STOPPED). "
                     "Harrison-gated: review the scope + NON-default rows above first.")
            return 0
        if not ids:
            log.info("Nothing to delete for this scope.")
            return 0

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bak = _REPO / "data" / f"purge-lex-restricted-{ts}.bak.jsonl"
        n_bak = backup_rows(conn, ids, bak)
        log.info("Backed up %d row(s) to %s (reversible-audit) before delete.", n_bak, bak)

        log.info("Deleting %d chunks from knowledge_chunks + both vec tables...", len(ids))
        totals = delete_chunks(conn, ids)
        log.info("Deleted: %s", totals)

        remaining = len(target_chunk_ids(conn, None))
        log.info("LBHS/LTS chunks remaining (all sources): %d", remaining)
        log.info("Reclaim disk with: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py")
    except Exception as exc:  # noqa: BLE001
        log.error("purge failed: %s", exc, exc_info=True)
        conn.close()
        return 1
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
