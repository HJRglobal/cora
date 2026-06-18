#!/usr/bin/env python3
"""Age out old high-volume KB chunks (Phase 3.1 retention prune).

The KB is dominated by personal-mailbox + drive-sweep chunks (gmail + drive_sweep
~= 89% of the corpus) with no age-out, so the DB grows without bound. This prunes
chunks from THOSE TWO sources only, older than a retention window, from every
vector table + knowledge_chunks. Disk is then reclaimed with a truncating VACUUM
(scripts/reclaim_kb_space.py, D-035).

WHY THIS IS SAFE (unlike int8 quantization): pruning only removes rows; it never
changes the embedding or the L2 distance metric, so the surviving rows rank and
threshold exactly as before. There is no recall guard to clear -- the only risk is
deleting something you wanted to keep, which the conservative selection below
guards against.

Sources pruned: ONLY gmail + drive_sweep. EXCLUDED permanently: static_md (the
canonical briefs/memory), fireflies, asana, notion, user_note -- these are
curated/low-volume/owner-private and must never be aged out here.

Age key: ingested_at (the only NOT-NULL timestamp + the correct "how long it has
been IN the KB" measure; drive_sweep leaves date_created NULL so a content-date
key is unreliable). A chunk is pruned ONLY when it is old by BOTH ingested_at AND
its best content date COALESCE(date_modified, date_created, ingested_at) -- so a
recently-ingested old-dated backfill (recent ingested_at) AND a recently-dated
item swept long ago (recent content date) are both KEPT.

Usage (--dry-run is the safe default; stop Cora before --apply):
    python scripts/prune_kb_retention.py                  # dry-run report
    python scripts/prune_kb_retention.py --months 18      # dry-run, 18-month window
    python scripts/prune_kb_retention.py --apply          # delete (Cora stopped)
After --apply: reclaim disk with scripts/reclaim_kb_space.py (truncating VACUUM).

Exit codes: 0 ok, 1 fatal error, 2 db missing, 3 Cora appears to be running.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cora.knowledge_base import schema  # noqa: E402

KB_DB_PATH = REPO_ROOT / "data" / "cora_kb.db"
HEARTBEAT_PATH = REPO_ROOT / "data" / "health" / "heartbeat.txt"

# ONLY these sources are eligible for retention pruning. Everything else
# (static_md / fireflies / asana / notion / user_note / ...) is never pruned here.
PRUNE_SOURCES: tuple[str, ...] = ("gmail", "drive_sweep")

# Vector tables that may hold a row per chunk. Only the ones that actually exist
# in the DB are touched (knowledge_vec_i8 is forward-compat -- absent today).
_CANDIDATE_VEC_TABLES: tuple[str, ...] = (
    "knowledge_vec_bin",
    "knowledge_vec_f32",
    "knowledge_vec_i8",
)

_DAYS_PER_MONTH = 30.44  # average; the precise knob is --days

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("prune-kb-retention")


def compute_cutoff(now_epoch: int, months: float | None, days: int | None) -> int:
    """Return the epoch cutoff -- chunks older than this (by both timestamps) prune.

    --days takes precedence when given; otherwise months * 30.44.
    """
    retention_days = days if days is not None else int(round((months or 0) * _DAYS_PER_MONTH))
    if retention_days <= 0:
        raise ValueError("retention window must be positive")
    return int(now_epoch) - retention_days * 86400


def existing_vec_tables(conn) -> list[str]:
    """Subset of _CANDIDATE_VEC_TABLES that actually exist in this DB."""
    present = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
    }
    return [t for t in _CANDIDATE_VEC_TABLES if t in present]


def _predicate(mode: str) -> tuple[str, int]:
    """Return (where-fragment after the source filter, number of cutoff params).

    mode="ingested" (default, the plan's policy): old by BOTH ingested_at AND the
      best content date -- conservative; a chunk survives if recent by EITHER. This
      protects a freshly bulk-backfilled corpus (recent ingested_at) for the whole
      window regardless of content age.
    mode="content": old by content date only (COALESCE(date_modified, date_created,
      ingested_at)). Use to age out genuinely-old content immediately even when it
      was re-ingested recently. (drive_sweep's date_created is NULL but its
      date_modified is populated, so COALESCE gives a usable content date.)
    """
    if mode == "content":
        return "AND COALESCE(date_modified, date_created, ingested_at) < ?", 1
    if mode == "ingested":
        return ("AND ingested_at < ? "
                "AND COALESCE(date_modified, date_created, ingested_at) < ?"), 2
    raise ValueError(f"unknown mode {mode!r} (use 'ingested' or 'content')")


def select_prunable_chunk_ids(
    conn, cutoff_epoch: int, sources: tuple[str, ...], mode: str = "ingested"
) -> list[str]:
    """chunk_ids in `sources` that are old per the retention `mode` (see _predicate)."""
    src_ph = ",".join("?" * len(sources))
    frag, n_cut = _predicate(mode)
    rows = conn.execute(
        f"SELECT chunk_id FROM knowledge_chunks WHERE source IN ({src_ph}) {frag}",
        [*sources, *([cutoff_epoch] * n_cut)],
    ).fetchall()
    return [r[0] for r in rows]


def prune_chunks(conn, chunk_ids: list[str], vec_tables: list[str], batch_size: int = 500) -> int:
    """Delete chunk_ids from every vec table + knowledge_chunks, in batches.

    Batched to stay under SQLite's bound-variable limit (the live KB has hundreds
    of thousands of candidates). Returns rows removed from knowledge_chunks.
    """
    removed = 0
    for i in range(0, len(chunk_ids), batch_size):
        batch = chunk_ids[i:i + batch_size]
        ph = ",".join("?" * len(batch))
        for vt in vec_tables:
            conn.execute(f"DELETE FROM {vt} WHERE chunk_id IN ({ph})", batch)
        cur = conn.execute(f"DELETE FROM knowledge_chunks WHERE chunk_id IN ({ph})", batch)
        removed += cur.rowcount
        conn.commit()
    return removed


def _heartbeat_is_fresh(max_age_s: int = 180) -> bool:
    try:
        return (time.time() - HEARTBEAT_PATH.stat().st_mtime) < max_age_s
    except OSError:
        return False


def _report_corpus(conn, cutoff_epoch: int, mode: str) -> None:
    """Log per-source totals + prunable counts so the operator sees the win/blast."""
    frag, n_cut = _predicate(mode)
    for src in PRUNE_SOURCES:
        total = conn.execute(
            "SELECT COUNT(*) FROM knowledge_chunks WHERE source = ?", (src,)
        ).fetchone()[0]
        prunable = conn.execute(
            f"SELECT COUNT(*) FROM knowledge_chunks WHERE source = ? {frag}",
            (src, *([cutoff_epoch] * n_cut)),
        ).fetchone()[0]
        log.info("source=%s: %d total, %d prunable (mode=%s)", src, total, prunable, mode)
    grand_total = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
    log.info("KB total chunks: %d", grand_total)


def main() -> int:
    ap = argparse.ArgumentParser(description="Age out old gmail/drive_sweep KB chunks.")
    ap.add_argument("--db", type=Path, default=KB_DB_PATH)
    ap.add_argument("--months", type=float, default=18.0,
                    help="Retention window in months (default 18). Ignored if --days given.")
    ap.add_argument("--days", type=int, default=None,
                    help="Retention window in days (overrides --months when set).")
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--apply", action="store_true",
                    help="Delete prunable chunks (default is a dry-run report).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report only (this is the default; kept for symmetry).")
    ap.add_argument("--force", action="store_true",
                    help="Skip the heartbeat (Cora-running) safety guard.")
    ap.add_argument("--by-content-date", action="store_true",
                    help="Prune by content date (date_modified) instead of ingested_at. "
                         "Use to age out genuinely-old content even after a fresh "
                         "re-ingest; default keys on ingested_at (the plan's policy).")
    args = ap.parse_args()
    apply_changes = args.apply and not args.dry_run
    mode = "content" if args.by_content_date else "ingested"

    if not args.db.exists():
        log.error("KB database not found: %s", args.db)
        return 2

    if apply_changes and _heartbeat_is_fresh() and not args.force:
        log.error("Cora heartbeat is fresh (<180s) -- the service appears to be running. "
                  "Stop Cora first (off the 02:00-06:30 AZ sync window), or pass --force.")
        return 3

    try:
        cutoff = compute_cutoff(int(time.time()), args.months, args.days)
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    window = args.days if args.days is not None else f"{args.months} months"
    log.info("Opening KB database: %s (retention window: %s; mode: %s; cutoff epoch %d)",
             args.db, window, mode, cutoff)
    conn = schema.connect(args.db)
    try:
        _report_corpus(conn, cutoff, mode)
        chunk_ids = select_prunable_chunk_ids(conn, cutoff, PRUNE_SOURCES, mode)
        vec_tables = existing_vec_tables(conn)
        log.info("Prunable chunks: %d  |  vector tables present: %s",
                 len(chunk_ids), ", ".join(vec_tables) or "(none)")

        if not chunk_ids:
            log.info("Nothing to prune at this retention window.")
            return 0

        if not apply_changes:
            log.info("Dry-run: would delete %d chunk(s) (gmail/drive_sweep only) from "
                     "knowledge_chunks + %d vector table(s). Re-run with --apply "
                     "(Cora stopped), then VACUUM (reclaim_kb_space.py).",
                     len(chunk_ids), len(vec_tables))
            return 0

        removed = prune_chunks(conn, chunk_ids, vec_tables, batch_size=args.batch_size)
        log.info("Deleted %d chunk(s) + their vectors. Reclaim disk with "
                 "reclaim_kb_space.py (truncating VACUUM).", removed)
    except Exception as exc:  # noqa: BLE001
        log.error("prune failed: %s", exc, exc_info=True)
        conn.close()
        return 1
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
