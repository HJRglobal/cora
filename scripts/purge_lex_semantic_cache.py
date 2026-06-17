#!/usr/bin/env python3
"""One-time deploy step (Phase 2.3): purge LEX rows from the persisted semantic cache.

The Phase 2.3 PHI cache-leak guard (app.py cache_storable `and not phi_custodian`)
stops a custodian's un-scrubbed LEX answer from being CACHED going forward. But the
semantic cache is SQLite-backed (data/cora_kb.db, table `semantic_cache`) and
survives a restart, and lookups key only on (entity, embedding) with no asker
dimension. So any custodian LEX answer cached in the ~30 min (DEFAULT_TTL=1800s)
BEFORE the coordinated restart could still be replayed to a non-custodian within
the TTL window after restart. Run this ONCE at the coordinated restart to drop all
LEX-scoped cache rows (they are 30-min-TTL operational answers -- cheap to rebuild).

Read-only by default; pass --apply to delete.

    .venv\\Scripts\\python.exe scripts\\purge_lex_semantic_cache.py            # report
    .venv\\Scripts\\python.exe scripts\\purge_lex_semantic_cache.py --apply    # delete
"""

import argparse
import sqlite3
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "cora_kb.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually delete (default: report only)")
    args = parser.parse_args()

    if not _DB_PATH.exists():
        print(f"KB DB not found at {_DB_PATH} -- nothing to purge.")
        return 0

    # 30s busy timeout: the live bot holds the same DB open, so wait out a brief
    # concurrent write rather than racing it.
    conn = sqlite3.connect(str(_DB_PATH), timeout=30)
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM semantic_cache WHERE entity LIKE 'LEX%'"
        )
        n = cur.fetchone()[0]
        if not args.apply:
            print(f"[DRY RUN] {n} LEX semantic-cache row(s) would be deleted. "
                  f"Re-run with --apply to delete.")
            return 0
        conn.execute("DELETE FROM semantic_cache WHERE entity LIKE 'LEX%'")
        conn.commit()
        print(f"Deleted {n} LEX semantic-cache row(s). LEX answers will rebuild on demand.")
        return 0
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "no such table" in msg:
            # Cache never written (no cached replies yet) -- nothing to purge.
            print("semantic_cache table absent (no cache writes yet) -- nothing to purge.")
            return 0
        # A lock (DB busy) or other operational error is NOT success -- surface it.
        print(f"ERROR: could not purge ({exc}). DB may be locked -- re-run in a moment.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
