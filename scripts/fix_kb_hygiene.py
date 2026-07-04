"""Gated KB hygiene fixes (audit Slice A: W6-05 + W6-04). --dry-run is the DEFAULT.

W6-05  Re-tag the single misrouted chunk entity='F3' (an OSN Val Vista point-of-sale
       receipt, mis-tagged from a filename token) -> 'OSN'. Aborts unless EXACTLY
       ONE 'F3' chunk exists AND its content looks like the audited receipt, so it
       can never mass-retag or touch an unexpected chunk. The drive_sweep resolver
       guard (this same slice) prevents new 'F3'-class mis-tags going forward.

W6-04  Refresh the cosmetic checkpoint_state 'kb_bin_index_ready' count field to the
       live chunk count. ONLY the informational 'count' changes; 'ready' (the sole
       load-bearing field per store._is_bin_index_ready) and 'migrated_at' are
       preserved. Verified in the audit that NO code reads 'count'. (The migration
       originally stored the vector-table row count; with ready==true every chunk
       has a vector, so live chunk count == vector count -- the refreshed value is
       exact, only the field's sense shifts from "vectors migrated" to "KB size".)

Usage:
    python scripts/fix_kb_hygiene.py            # dry-run report (SAFE DEFAULT)
    python scripts/fix_kb_hygiene.py --apply    # write the fixes (Harrison-gated)
    python scripts/fix_kb_hygiene.py --force --apply   # re-tag even if the content
                                                       # safety marker is absent

Both edits are tiny + reversible. WAL + busy_timeout make a quick UPDATE safe even
with Cora running; running with Cora stopped is cleaner but not required.

Reversal (if ever needed):
    W6-05: UPDATE knowledge_chunks SET entity='F3' WHERE chunk_id='<printed id>';
    W6-04: cosmetic; re-run the migration or set count back -- nothing reads it.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DB = Path(os.environ.get("CORA_KB_DB_PATH") or _REPO_ROOT / "data" / "cora_kb.db")

_BAD_ENTITY = "F3"          # the audited non-canonical code
_CORRECT_ENTITY = "OSN"     # 425 S Val Vista Dr, Mesa AZ == an OSN store receipt
_CONTENT_MARKER = "VAL VISTA"   # safety: the audited chunk's content contains this
_CKPT_KEY = "kb_bin_index_ready"


def _connect(db: Path, *, read_only: bool) -> sqlite3.Connection:
    uri = f"file:{db}?mode=ro" if read_only else f"file:{db}"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")  # D-039: don't crash on a WAL writer
    return con


def _fix_f3_chunk(con: sqlite3.Connection, *, apply: bool, force: bool) -> int:
    rows = con.execute(
        "SELECT chunk_id, source, source_id, entity, sub_entity, substr(content,1,120) "
        "FROM knowledge_chunks WHERE entity = ?", (_BAD_ENTITY,)
    ).fetchall()
    print(f"\n[W6-05] chunks with entity='{_BAD_ENTITY}': {len(rows)}")
    if not rows:
        print("  nothing to do (already re-tagged or absent).")
        return 0
    if len(rows) > 1:
        print(f"  ABORT: expected exactly 1 '{_BAD_ENTITY}' chunk, found {len(rows)}. "
              "Not mass-retagging -- investigate manually.")
        return 0
    chunk_id, source, source_id, entity, sub_entity, preview = rows[0]
    print(f"  chunk_id={chunk_id}")
    print(f"  source={source} source_id={source_id} sub_entity={sub_entity}")
    print(f"  content[:120]={preview!r}")
    marker_ok = _CONTENT_MARKER.lower() in (preview or "").lower()
    if not marker_ok and not force:
        print(f"  SKIP: content does not contain the expected marker '{_CONTENT_MARKER}'. "
              "Re-run with --force if this is still the receipt to re-tag.")
        return 0
    print(f"  {'APPLY' if apply else 'DRY-RUN'}: entity '{entity}' -> '{_CORRECT_ENTITY}', "
          f"sub_entity {sub_entity!r} -> None")
    if apply:
        con.execute(
            "UPDATE knowledge_chunks SET entity = ?, sub_entity = NULL WHERE chunk_id = ?",
            (_CORRECT_ENTITY, chunk_id),
        )
    return 1


def _refresh_bin_count(con: sqlite3.Connection, *, apply: bool) -> int:
    row = con.execute(
        "SELECT value_json FROM checkpoint_state WHERE key = ?", (_CKPT_KEY,)
    ).fetchone()
    print(f"\n[W6-04] checkpoint_state '{_CKPT_KEY}':")
    if not row:
        print("  not present -- nothing to do.")
        return 0
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError) as exc:
        print(f"  ABORT: value_json not parseable ({exc}).")
        return 0
    live = con.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
    old = payload.get("count")
    if old == live:
        print(f"  count already current ({live}). nothing to do.")
        return 0
    print(f"  ready={payload.get('ready')!r} (preserved)  migrated_at={payload.get('migrated_at')!r} (preserved)")
    print(f"  {'APPLY' if apply else 'DRY-RUN'}: count {old} -> {live} (cosmetic; nothing reads it)")
    if apply:
        payload["count"] = live
        con.execute(
            "UPDATE checkpoint_state SET value_json = ?, updated_at = ? WHERE key = ?",
            (json.dumps(payload), int(time.time()), _CKPT_KEY),
        )
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Gated KB hygiene fixes (W6-05 + W6-04).")
    ap.add_argument("--db", type=Path, default=_DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="Write the fixes (default is a dry-run).")
    ap.add_argument("--dry-run", action="store_true", help="Explicit dry-run (overrides --apply).")
    ap.add_argument("--force", action="store_true",
                    help="Re-tag the F3 chunk even if the content safety marker is absent.")
    args = ap.parse_args()

    apply = args.apply and not args.dry_run
    if not args.db.exists():
        print(f"KB not found: {args.db}", file=sys.stderr)
        return 1

    print(f"KB: {args.db}")
    print(f"Mode: {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}")
    con = _connect(args.db, read_only=not apply)
    try:
        n1 = _fix_f3_chunk(con, apply=apply, force=args.force)
        n2 = _refresh_bin_count(con, apply=apply)
        if apply:
            con.commit()
    finally:
        con.close()

    print(f"\nSummary: W6-05 re-tag={'done' if n1 else 'no-op'}, "
          f"W6-04 count-refresh={'done' if n2 else 'no-op'}"
          f"{'' if apply else '  (dry-run -- re-run with --apply to write)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
