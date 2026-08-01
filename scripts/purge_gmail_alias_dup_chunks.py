#!/usr/bin/env python3
"""Purge exact-duplicate gmail KB chunks created by alias multi-sweep (2026-07-31 audit).

ROOT CAUSE (fixed forward in gmail_threaded_sweep._dedup_alias_accounts): the
same physical Gmail mailbox was enrolled in monitored-email-accounts.yaml under
2-3 alias addresses (harrison@hjrglobal + harrison@f3energy +
harrison@lexingtonservices, tommy x2, justin x2, larry x2, alex x2). source_id
embeds user_email, so the store's replace-on-conflict could never fold the
copies: ~48% of the gmail partition is exact-duplicate rows.

SCOPE — deliberately the zero-information-loss class ONLY:
  * source='gmail' rows grouped by (gmail-api message_id, content, entity).
  * A gmail-api message id is MAILBOX-LOCAL: identical ids across different
    metadata.user_email values prove the rows came from the same physical
    mailbox (same person) — cross-owner merges are impossible by construction,
    so D-043 ownership semantics are preserved.
  * The group key includes entity, so every entity partition keeps a survivor
    (no entity-scoped retrieval coverage is lost).
  * Keep rule (deterministic): prefer the @hjrglobal.com user_email row, else
    lexicographically-smallest user_email, else smallest chunk_id.
  * A multi-email group can also contain several rows under ONE email
    (re-chunk churn across sweeps produced identical-content rows with
    different chunk_ids); those extras delete too — still identical content,
    same entity, same person. Groups with only ONE distinct email are skipped
    entirely (not the alias class).

Cascade: deletes across knowledge_chunks + every knowledge_vec* table that
exists (discovered from sqlite_master — includes knowledge_vec_bin_v2, which
prune_kb_retention.py's hard-coded list misses).

DRY-RUN BY DEFAULT. --apply performs the deletes (Harrison-gated: KB delete;
take the standing pre-purge DB backup first). Batched <=500 per commit with
busy_timeout — the live bot can keep running (WAL deletes), though a quiet
window is the conservative choice.

Standalone script — does NOT import the bot process; no restart needed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DB_PATH = _REPO_ROOT / "data" / "cora_kb.db"

_BATCH = 500
_PRIMARY_DOMAIN = "@hjrglobal.com"


def _keep_rank(user_email: str, chunk_id: str) -> tuple:
    """Sort key: the FIRST row in this order is kept."""
    email = (user_email or "").lower()
    return (0 if email.endswith(_PRIMARY_DOMAIN) else 1, email, chunk_id or "")


# Named candidates existence-checked -- NEVER a bare LIKE 'knowledge_vec%'
# discovery, which also matches vec0's internal shadow tables
# (knowledge_vec_bin_chunks/_rowids/_info/...); deleting from those directly
# corrupts the virtual table. Caught live by this script's first dry-run.
_CANDIDATE_VEC_TABLES = (
    "knowledge_vec_bin",
    "knowledge_vec_bin_v2",
    "knowledge_vec_f32",
    "knowledge_vec_i8",
)


def _vec_tables(conn: sqlite3.Connection) -> list[str]:
    """Subset of _CANDIDATE_VEC_TABLES that actually exist in this DB."""
    present = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
    }
    return [t for t in _CANDIDATE_VEC_TABLES if t in present]


def plan_purge(conn: sqlite3.Connection) -> tuple[list[str], dict]:
    """Compute the chunk_ids to delete. Returns (delete_ids, stats).

    Streams the gmail partition and groups on a CONTENT HASH (not the content
    itself) so memory stays bounded at ~hundreds of MB of ids, not GBs of text,
    across the ~425K-row live partition.
    """
    cursor = conn.execute(
        """
        SELECT chunk_id, entity, content,
               json_extract(metadata, '$.message_id') AS mid,
               json_extract(metadata, '$.user_email') AS user_email
        FROM knowledge_chunks
        WHERE source = 'gmail'
          AND json_extract(metadata, '$.message_id') IS NOT NULL
        """
    )

    import hashlib
    groups: dict[tuple, list[tuple]] = {}
    scanned = 0
    for chunk_id, entity, content, mid, user_email in cursor:
        scanned += 1
        digest = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
        key = (mid, entity or "", digest)
        groups.setdefault(key, []).append((chunk_id, user_email or ""))

    delete_ids: list[str] = []
    dup_groups = 0
    kept_by_mailbox: Counter = Counter()
    deleted_by_mailbox: Counter = Counter()
    for key, members in groups.items():
        if len(members) < 2:
            continue
        emails = {m[1] for m in members}
        if len(emails) < 2:
            # Same-mailbox duplicate rows share a source_id lineage and are the
            # sweep's own churn — out of scope for the alias class (leave for
            # a later pass; replace-on-conflict handles them going forward).
            continue
        dup_groups += 1
        members_sorted = sorted(members, key=lambda m: _keep_rank(m[1], m[0]))
        keeper = members_sorted[0]
        kept_by_mailbox[keeper[1]] += 1
        for chunk_id, email in members_sorted[1:]:
            delete_ids.append(chunk_id)
            deleted_by_mailbox[email] += 1

    stats = {
        "gmail_rows_scanned": scanned,
        "dup_groups": dup_groups,
        "rows_to_delete": len(delete_ids),
        "kept_by_mailbox": dict(kept_by_mailbox.most_common()),
        "deleted_by_mailbox": dict(deleted_by_mailbox.most_common()),
    }
    return delete_ids, stats


def apply_purge(conn: sqlite3.Connection, delete_ids: list[str]) -> dict[str, int]:
    """Batched cascade delete across knowledge_chunks + all vec tables."""
    tables = _vec_tables(conn) + ["knowledge_chunks"]
    deleted: dict[str, int] = {t: 0 for t in tables}
    for i in range(0, len(delete_ids), _BATCH):
        batch = delete_ids[i:i + _BATCH]
        placeholders = ",".join("?" * len(batch))
        for table in tables:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE chunk_id IN ({placeholders})", batch)
            deleted[table] += cur.rowcount
        conn.commit()
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Perform the deletes (default: dry-run report only)")
    parser.add_argument("--db", default=str(KB_DB_PATH))
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"KB DB not found: {db_path}")
        return 1

    mode = "rw" if args.apply else "ro"
    conn = sqlite3.connect(f"file:{db_path}?mode={mode}", uri=True)
    conn.execute("PRAGMA busy_timeout=30000")

    delete_ids, stats = plan_purge(conn)
    print(f"gmail rows scanned:      {stats['gmail_rows_scanned']}")
    print(f"alias-dup groups:        {stats['dup_groups']}")
    print(f"rows to DELETE:          {stats['rows_to_delete']}")
    print(f"vec tables in cascade:   {', '.join(_vec_tables(conn))}")
    print("\nkept per mailbox:")
    for email, n in stats["kept_by_mailbox"].items():
        print(f"  {email or '(none)'}: {n}")
    print("deleted per mailbox:")
    for email, n in stats["deleted_by_mailbox"].items():
        print(f"  {email or '(none)'}: {n}")

    if not args.apply:
        print(f"\nDRY RUN — no writes. Re-run with --apply to purge "
              f"{stats['rows_to_delete']} rows (take a DB backup first).")
        conn.close()
        return 0

    deleted = apply_purge(conn, delete_ids)
    conn.close()
    print("\nAPPLIED:")
    print(json.dumps(deleted, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
