#!/usr/bin/env python3
"""Purge the SELF-POISONED pricing chunk from the KB (cq-4d73879917fa).

On 2026-07-20 Cora fabricated a Pure wholesale answer in #f3e-sales ("$22 per
12-pack ... 55% of the retail price ... Harrison locked it in on 7/13" -- none
of it real; the locked structure is the 3-tier ladder off $36.99). The nightly
kb-sync-slack sweep then ingested that reply, and the fabricated chunk now
OUTRANKS the real canon for pricing queries (live probe 2026-07-31: distance
0.7912 vs decisions.md at 0.8187) -- a self-poisoning loop. The known-answers
canonical block (live since 7/31) is the primary mitigation; this removes the
poisoned competitor. The sweep is watermark-driven, so a purged old chunk will
NOT re-ingest.

Targeting is EXACT (never LIKE-broad): one chunk, pinned by BOTH chunk_id and
source_id, verified live 2026-07-31. The 7/13 thread chunk (Harrison's real,
since-superseded words) is deliberately NOT touched -- it is a genuine
historical record; the Known-Answers-wins rule handles its staleness.

Usage (--dry-run default is read-only; STOP Cora before --apply):
    .venv\\Scripts\\python.exe scripts\\purge_fabricated_pricing_chunk.py
    .venv\\Scripts\\python.exe scripts\\purge_fabricated_pricing_chunk.py --apply

Exit codes: 0 ok, 1 fatal / target-mismatch (nothing deleted).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
KB_DB_PATH = _REPO / "data" / "cora_kb.db"

# The one poisoned chunk -- BOTH must match or the script refuses (a re-chunked
# ingest would change chunk_id; refusing beats deleting the wrong row).
_TARGET_CHUNK_ID = "4445492c-7e63-4a8d-acb6-f7a8027a3a27"
_TARGET_SOURCE_ID = "slack:C0B3K6DEEAF:1784574740.618059"  # #f3e-sales 7/20 thread
# Content fingerprint (belt): the fabricated phrase must be present in the row.
_TARGET_MARKER = "22 per 12-pack"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete (default: read-only dry run). Stop Cora first.")
    args = ap.parse_args()

    if not KB_DB_PATH.exists():
        print(f"FATAL: KB not found at {KB_DB_PATH}")
        return 1

    uri = f"file:{KB_DB_PATH}?mode={'rw' if args.apply else 'ro'}"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = list(conn.execute(
            "SELECT chunk_id, source_id, entity, substr(content, 1, 200) "
            "FROM knowledge_chunks WHERE chunk_id = ? AND source_id = ?",
            (_TARGET_CHUNK_ID, _TARGET_SOURCE_ID)))
        if not rows:
            print("Target chunk not found (already purged, or re-chunked -- re-verify "
                  "identity before editing this script). Nothing to do.")
            return 0
        chunk_id, source_id, entity, preview = rows[0]
        if _TARGET_MARKER not in preview:
            print("FATAL: target row exists but the content fingerprint "
                  f"({_TARGET_MARKER!r}) is absent -- identity mismatch, refusing.")
            return 1
        print(f"TARGET: {chunk_id} | {source_id} | entity={entity}")
        print(f"   {preview[:160]!r}")

        # Report-only companion scan (never deleted here): the 7/21-22 drive-doc
        # "$39.99 (Shopify) vs $32.99" claim, if it ever surfaces in the corpus.
        companions = list(conn.execute(
            "SELECT chunk_id, source, source_id FROM knowledge_chunks "
            "WHERE content LIKE '%$39.99 (Shopify)%' AND chunk_id != ?",
            (_TARGET_CHUNK_ID,)))
        if companions:
            print(f"NOTE: {len(companions)} companion chunk(s) carry the 7/21-22 "
                  "'$39.99 (Shopify)' claim -- review before any separate action:")
            for c in companions:
                print(f"   {c[0]} | {c[1]} | {c[2]}")

        if not args.apply:
            print("DRY RUN -- nothing deleted. Re-run with --apply (Cora stopped).")
            return 0

        # Every vec table that may exist, incl. the partition-key v2 index the
        # pending migration populates (D-051 bundle review: omitting v2 leaves an
        # orphan that permanently blocks the migrate --swap v2==f32 verification).
        for tbl in ("knowledge_vec_bin", "knowledge_vec_bin_v2",
                    "knowledge_vec_f32", "knowledge_chunks"):
            try:
                cur = conn.execute(f"DELETE FROM {tbl} WHERE chunk_id = ?", (chunk_id,))
                print(f"   {tbl}: deleted {cur.rowcount}")
            except sqlite3.OperationalError as exc:
                # A vec table absent on this host is fine (fallback-path DBs).
                print(f"   {tbl}: skipped ({exc})")
        conn.commit()
        print("APPLIED. Verify: re-run the pricing KB probe -- the top hit should "
              "now be the decisions.md/canon chunk.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
