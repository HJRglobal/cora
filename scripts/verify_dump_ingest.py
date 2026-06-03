import sqlite3
conn = sqlite3.connect("data/cora_kb.db")
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE metadata LIKE '%Shaun x Jen%'")
print("Dump folder chunks:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM knowledge_chunks WHERE entity='LEX'")
print("Total LEX chunks:", cur.fetchone()[0])
cur.execute("SELECT COUNT(*) FROM knowledge_chunks")
print("Total KB chunks:", cur.fetchone()[0])
cur.execute("SELECT DISTINCT title, COUNT(*) n FROM knowledge_chunks WHERE metadata LIKE '%Shaun x Jen%' GROUP BY title ORDER BY title")
print("\nIngested files:")
for r in cur.fetchall():
    print(f"  {r[1]:>4} chunks  {r[0]}")
conn.close()
