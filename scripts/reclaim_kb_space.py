"""Reclaim disk space after dropping knowledge_vec (game-plan section 10.6, finish).

The drop (scripts/drop_legacy_float_vec.py) removed the legacy float vec0 table,
but an in-place VACUUM in WAL mode does NOT truncate the main db file -- it writes
the compacted pages into the WAL, leaving the main file at its old size. This does
the truncating VACUUM the reliable way: checkpoint -> switch out of WAL ->
VACUUM (which truncates the file) -> switch back to WAL.

REQUIRES EXCLUSIVE ACCESS. Every process with cora_kb.db open (the Cora service
AND any stuck/elevated python holding a connection) must be stopped first, or the
journal-mode switch fails with "database is locked". Run from ELEVATED PowerShell
via the orchestration in this file's header comment:

    # elevated PS, in C:\\Users\\Harri\\code\\cora
    schtasks /End /TN cowork-cora-service
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
      Where-Object { $_.ExecutablePath -like '*cora*' } | Stop-Process -Force
    .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py
    schtasks /Run /TN cowork-cora-service

--dry-run reports sizes and changes nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from cora.knowledge_base import schema  # noqa: E402

KB_DB_PATH = REPO_ROOT / "data" / "cora_kb.db"


def main() -> int:
    ap = argparse.ArgumentParser(description="Truncating VACUUM to reclaim KB disk space.")
    ap.add_argument("--db", type=Path, default=KB_DB_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"ERROR: KB db not found at {args.db}", file=sys.stderr)
        return 2

    before = args.db.stat().st_size
    conn = schema.connect(args.db)   # loads vec0 (needed: knowledge_vec_bin is vec0)
    conn.isolation_level = None      # autocommit -- PRAGMAs/VACUUM run outside a txn

    # Confirm we actually have exclusive access before changing journal mode.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:  # noqa: BLE001
        print(f"ABORT: cannot checkpoint ({exc}). Another process holds the db -- "
              f"stop the Cora service AND kill any stuck cora python first.",
              file=sys.stderr)
        return 3

    print(f"db size before: {before/1e9:.2f} GB")
    if args.dry_run:
        print("[dry-run] would: journal_mode=DELETE; VACUUM; journal_mode=WAL")
        conn.close()
        return 0

    try:
        mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if mode.lower() != "delete":
            print(f"ABORT: could not switch out of WAL (mode={mode}); a connection is "
                  f"still open. Kill all cora python and retry.", file=sys.stderr)
            return 4
        print("VACUUM (truncating)...")
        conn.execute("VACUUM")
        conn.execute("PRAGMA journal_mode=WAL")
    finally:
        conn.close()

    after = args.db.stat().st_size
    print(f"db size after : {after/1e9:.2f} GB")
    print(f"reclaimed     : {(before-after)/1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
