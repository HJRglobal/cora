"""Check titles of LEX drive_sweep documents and search for dump folder content."""
import sqlite3, os, sys

DB = os.path.join(os.path.dirname(__file__), "..", "data", "cora_kb.db")
conn = sqlite3.connect(DB)
cur = conn.cursor()

# Distinct titles for LEX drive_sweep (top 60 by chunk count)
print("=== LEX DRIVE_SWEEP -- top 60 documents by chunk count ===")
cur.execute("""
    SELECT COALESCE(title,'(no title)'), source_id, COUNT(*) n
    FROM knowledge_chunks
    WHERE entity='LEX' AND source='drive_sweep'
    GROUP BY source_id ORDER BY n DESC LIMIT 60
""")
for r in cur.fetchall():
    title = r[0].encode('ascii','replace').decode('ascii')
    print(f"  {r[2]:>4}  {title[:70]}")

print()
print("=== LEX -- title search: policy / manual / training / DDD / contract ===")
cur.execute("""
    SELECT DISTINCT COALESCE(title,'(no title)'), source, sub_entity, COUNT(*) n
    FROM knowledge_chunks
    WHERE entity='LEX'
    AND (LOWER(COALESCE(title,'')) LIKE '%policy%'
      OR LOWER(COALESCE(title,'')) LIKE '%manual%'
      OR LOWER(COALESCE(title,'')) LIKE '%training%'
      OR LOWER(COALESCE(title,'')) LIKE '%procedure%'
      OR LOWER(COALESCE(title,'')) LIKE '%ddd%'
      OR LOWER(COALESCE(title,'')) LIKE '%ahcccs%'
      OR LOWER(COALESCE(title,'')) LIKE '%hcbs%'
      OR LOWER(COALESCE(title,'')) LIKE '%contract%'
      OR LOWER(COALESCE(title,'')) LIKE '%dump%'
      OR LOWER(COALESCE(title,'')) LIKE '%shaun%'
      OR LOWER(COALESCE(title,'')) LIKE '%jen%')
    GROUP BY title, source, sub_entity ORDER BY n DESC
""")
rows = cur.fetchall()
if rows:
    for r in rows:
        title = r[0].encode('ascii','replace').decode('ascii')
        print(f"  [{r[1]}] sub={r[2]}  {r[3]:>4}  {title[:80]}")
else:
    print("  (none found)")

conn.close()
