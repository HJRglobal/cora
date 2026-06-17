#!/usr/bin/env python3
"""Purge already-ingested sensitive Slack/LEX content from the KB (Phase 1.4).

Pairs with the deny-list (src/cora/slack_sweep_policy.py): the deny-list stops
FUTURE ingestion; this removes what is already in the KB. Scope = source + tag
(Harrison's G-A.2 decision):

  TAG   -- chunks with sub_entity IN (LEX-LBHS, LEX-LTS, LEX-LBH)
  SLACK -- chunks from the deny-listed sensitive channels (personal/family, NDA,
           LBHS/LTS, general-do-not-use), matched by channel ID from
           slack-sweep-policy.yaml deny_by_id against source_id 'slack:<id>:%'

Does NOT touch the ~172K keyword-less NULL LEX residual: drive/gmail source_id
cannot isolate LBHS/LTS from the shared @lexingtonservices.com accounts, and the
source+tag decision deliberately leaves indistinguishable-from-general-LEX content.

Usage (--dry-run is read-only + safe anytime; STOP Cora before --apply):
    .venv\\Scripts\\python.exe scripts\\purge_denied_kb.py            # dry-run report
    .venv\\Scripts\\python.exe scripts\\purge_denied_kb.py --apply    # delete (Cora stopped)
After --apply, reclaim disk: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py

Exit codes: 0 ok, 1 fatal.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from cora import slack_sweep_policy  # noqa: E402
from cora.knowledge_base import schema  # noqa: E402

KB_DB_PATH = _REPO / "data" / "cora_kb.db"
_SUB_ENTITY_TAGS = ("LEX-LBHS", "LEX-LTS", "LEX-LBH")
_BATCH = 500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("purge-denied-kb")


def _deny_channel_ids() -> list[str]:
    pol = slack_sweep_policy._load()
    return [str(x).strip() for x in (pol.get("deny_by_id") or []) if str(x).strip()]


def target_chunk_ids(conn) -> tuple[set[str], dict]:
    """Read-only. Return (set of chunk_ids to purge, breakdown dict)."""
    breakdown: dict = {}
    ids: set[str] = set()

    ph = ",".join("?" * len(_SUB_ENTITY_TAGS))
    tag_ids = {
        r[0] for r in conn.execute(
            f"SELECT chunk_id FROM knowledge_chunks WHERE sub_entity IN ({ph})",
            _SUB_ENTITY_TAGS,
        ).fetchall()
    }
    ids |= tag_ids
    breakdown["tag LEX-LBHS/LTS/LBH"] = len(tag_ids)

    slack_total = 0
    per_channel: dict = {}
    for cid in _deny_channel_ids():
        rows = conn.execute(
            "SELECT chunk_id FROM knowledge_chunks WHERE source='slack' AND source_id LIKE ?",
            (f"slack:{cid}:%",),
        ).fetchall()
        if rows:
            per_channel[cid] = len(rows)
            slack_total += len(rows)
            ids |= {r[0] for r in rows}
    breakdown["slack denied-channel total"] = slack_total
    breakdown["_slack_per_channel"] = per_channel
    return ids, breakdown


def _tag_source_breakdown(conn) -> dict:
    ph = ",".join("?" * len(_SUB_ENTITY_TAGS))
    rows = conn.execute(
        f"SELECT source, COUNT(*) FROM knowledge_chunks WHERE sub_entity IN ({ph}) "
        f"GROUP BY source ORDER BY 2 DESC",
        _SUB_ENTITY_TAGS,
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def delete_chunks(conn, chunk_ids) -> dict:
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
    ap = argparse.ArgumentParser(description="Purge sensitive Slack/LEX KB content (Phase 1.4).")
    ap.add_argument("--apply", action="store_true", help="Delete (default is a read-only dry-run).")
    ap.add_argument("--dry-run", action="store_true", help="Report only (this is the default).")
    args = ap.parse_args()
    apply_changes = args.apply and not args.dry_run

    if not KB_DB_PATH.exists():
        log.error("KB database not found: %s", KB_DB_PATH)
        return 1

    conn = schema.connect(KB_DB_PATH)
    try:
        ids, breakdown = target_chunk_ids(conn)
        log.info("=== Purge scope (source + tag) ===")
        log.info("  TAG (LEX-LBHS/LTS/LBH): %d", breakdown["tag LEX-LBHS/LTS/LBH"])
        for src, n in _tag_source_breakdown(conn).items():
            log.info("      %-12s %d", src, n)
        log.info("  SLACK denied-channel chunks: %d", breakdown["slack denied-channel total"])
        for cid, n in breakdown["_slack_per_channel"].items():
            log.info("      %s  %d", cid, n)
        log.info("  TOTAL unique chunks to purge: %d", len(ids))

        if not apply_changes:
            log.info("Dry-run -- nothing deleted. Re-run with --apply (Cora STOPPED), "
                     "then reclaim_kb_space.py.")
            return 0

        log.info("Deleting %d chunks from knowledge_chunks + both vec tables...", len(ids))
        totals = delete_chunks(conn, ids)
        log.info("Deleted: %s", totals)
        log.info("Reclaim disk with: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py")
    except Exception as exc:  # noqa: BLE001
        log.error("purge failed: %s", exc, exc_info=True)
        conn.close()
        return 1
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
