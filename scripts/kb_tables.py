import sqlite3
conn = sqlite3.connect("data/cora_kb.db")
cur = conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
for r in cur.fetchall():
    print(r[0])

print("\n-- sync state tables --")
for t in ["sync_state", "kb_sync_state", "sync_watermarks", "source_watermarks"]:
    try:
        cur.execute(f"SELECT * FROM {t} ORDER BY 1")
        rows = cur.fetchall()
        print(f"{t}: {rows}")
    except Exception as e:
        print(f"{t}: not found")
conn.close()
