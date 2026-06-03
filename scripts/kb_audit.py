"""One-shot KB audit script -- prints coverage report across all entities and sources."""
import sqlite3, sys, os

DB = os.path.join(os.path.dirname(__file__), "..", "data", "cora_kb.db")

def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    print("=" * 60)
    print("CORA KB AUDIT")
    print("=" * 60)

    cur.execute("SELECT COUNT(*) FROM knowledge_chunks")
    total = cur.fetchone()[0]
    print(f"\nTOTAL CHUNKS: {total}\n")

    print("--- BY SOURCE ---")
    cur.execute("SELECT source, COUNT(*) n FROM knowledge_chunks GROUP BY source ORDER BY n DESC")
    for row in cur.fetchall():
        print(f"  {row[0]:<20} {row[1]:>6}")

    print("\n--- BY ENTITY ---")
    cur.execute("SELECT COALESCE(entity,'NULL'), COUNT(*) n FROM knowledge_chunks GROUP BY entity ORDER BY n DESC")
    for row in cur.fetchall():
        print(f"  {row[0]:<20} {row[1]:>6}")

    print("\n--- LEX SUB-ENTITY DETAIL ---")
    cur.execute("""
        SELECT COALESCE(sub_entity,'(none)'), COUNT(*) n
        FROM knowledge_chunks
        WHERE entity='LEX'
        GROUP BY sub_entity ORDER BY n DESC
    """)
    for row in cur.fetchall():
        print(f"  {row[0]:<20} {row[1]:>6}")

    print("\n--- BY ENTITY + SOURCE ---")
    cur.execute("""
        SELECT COALESCE(entity,'NULL'), source, COUNT(*) n
        FROM knowledge_chunks
        GROUP BY entity, source ORDER BY entity, source
    """)
    for row in cur.fetchall():
        print(f"  {row[0]:<20} {row[1]:<20} {row[2]:>6}")

    print("\n--- SYNC WATERMARKS ---")
    cur.execute("SELECT source, watermark FROM kb_sync_state ORDER BY source")
    for row in cur.fetchall():
        print(f"  {row[0]:<30} {row[1]}")

    conn.close()

if __name__ == "__main__":
    main()
