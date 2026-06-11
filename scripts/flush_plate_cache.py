"""One-shot: flush stale whats_on_my_plate replies from the semantic cache.

The 2026-06-11 00:51 AZ live crash cached two bad FNDR replies ("No open Asana
tasks") built on the crashed/degraded tool result. Safe to run while the bot is
up (WAL + busy_timeout). Idempotent; keep for future targeted flushes:

    .venv\\Scripts\\python.exe scripts\\flush_plate_cache.py
"""

import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "cora_kb.db"

con = sqlite3.connect(str(DB), timeout=30)
con.execute("PRAGMA busy_timeout=30000")
rows = con.execute(
    "SELECT entity, substr(question, 1, 70), created_at"
    " FROM semantic_cache WHERE question LIKE '%plate%'"
).fetchall()
for r in rows:
    print("match:", r)
cur = con.execute("DELETE FROM semantic_cache WHERE question LIKE '%plate%'")
con.commit()
print(f"deleted: {cur.rowcount}")
con.close()
