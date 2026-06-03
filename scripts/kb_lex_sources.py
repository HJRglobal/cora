"""Check what source documents are indexed for LEX from drive_sweep."""
import sqlite3, os

DB = os.path.join(os.path.dirname(__file__), "..", "data", "cora_kb.db")

conn = sqlite3.connect(DB)
cur = conn.cursor()

# Schema
cur.execute("PRAGMA table_info(knowledge_chunks)")
cols = [c[1] for c in cur.fetchall()]
print("COLUMNS:", cols)
print()

# Distinct source_ids (document identifiers) for LEX drive_sweep
print("=== LEX DRIVE_SWEEP -- distinct source_ids (first 60) ===")
cur.execute("""
    SELECT DISTINCT source_id, COUNT(*) n
    FROM knowledge_chunks
    WHERE entity='LEX' AND source='drive_sweep'
    GROUP BY source_id ORDER BY n DESC LIMIT 60
""")
for r in cur.fetchall():
    print(f"  {r[1]:>4}  {r[0]}")

print()
# Any source_id that mentions Shaun, Jen, Sean, dump, DDD, manual, policy
print("=== LEX -- chunks mentioning Shaun/Jen/Sean/dump/DDD/policy in source_id or content ===")
cur.execute("""
    SELECT DISTINCT source_id, source, COUNT(*) n
    FROM knowledge_chunks
    WHERE entity='LEX'
    AND (LOWER(source_id) LIKE '%shaun%'
      OR LOWER(source_id) LIKE '%jen%'
      OR LOWER(source_id) LIKE '%sean%'
      OR LOWER(source_id) LIKE '%dump%'
      OR LOWER(source_id) LIKE '%ddd%'
      OR LOWER(source_id) LIKE '%policy%'
      OR LOWER(source_id) LIKE '%manual%'
      OR LOWER(source_id) LIKE '%contract%')
    GROUP BY source_id, source ORDER BY n DESC
""")
rows = cur.fetchall()
if rows:
    for r in rows:
        print(f"  [{r[1]}] {r[2]:>4}  {r[0]}")
else:
    print("  (none found by source_id)")

# Search chunk text content for DDD/policy keywords
print()
print("=== LEX -- chunks with DDD/policy/manual/training content (sample 10) ===")
cur.execute("""
    SELECT source, source_id, SUBSTR(content,1,120)
    FROM knowledge_chunks
    WHERE entity='LEX'
    AND (LOWER(content) LIKE '%ddd policy%'
      OR LOWER(content) LIKE '%ddd manual%'
      OR LOWER(content) LIKE '%ddd contract%'
      OR LOWER(content) LIKE '%shaun%jen%'
      OR LOWER(content) LIKE '%dump folder%')
    LIMIT 10
""")
rows = cur.fetchall()
if rows:
    for r in rows:
        print(f"  [{r[0]}] {r[1][:50]}  |  {r[2]}")
else:
    print("  (none found by content keywords)")

conn.close()
