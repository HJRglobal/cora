"""KB audit with correct table/schema names."""
import sqlite3, os, time

DB = os.path.join(os.path.dirname(__file__), "..", "data", "cora_kb.db")

def ts(epoch):
    if not epoch:
        return "never"
    import datetime
    return datetime.datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%d %H:%M UTC")

def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    print("=" * 62)
    print("CORA KB AUDIT  (all sources, all entities)")
    print("=" * 62)

    cur.execute("SELECT COUNT(*) FROM knowledge_chunks")
    total = cur.fetchone()[0]
    print(f"\nTOTAL CHUNKS: {total:,}\n")

    print("--- BY SOURCE ---")
    cur.execute("SELECT source, COUNT(*) n FROM knowledge_chunks GROUP BY source ORDER BY n DESC")
    for r in cur.fetchall():
        print(f"  {r[0]:<20} {r[1]:>7,}")

    print("\n--- BY ENTITY (all) ---")
    cur.execute("SELECT COALESCE(entity,'NULL'), COUNT(*) n FROM knowledge_chunks GROUP BY entity ORDER BY n DESC")
    for r in cur.fetchall():
        print(f"  {r[0]:<20} {r[1]:>7,}")

    print("\n--- LEX SUB-ENTITY DETAIL ---")
    cur.execute("""
        SELECT COALESCE(sub_entity,'(untagged)'), COUNT(*) n
        FROM knowledge_chunks WHERE entity='LEX'
        GROUP BY sub_entity ORDER BY n DESC
    """)
    for r in cur.fetchall():
        print(f"  {r[0]:<20} {r[1]:>7,}")

    print("\n--- SYNC WATERMARKS (last successful sync) ---")
    cur.execute("SELECT source, high_water FROM sync_state ORDER BY source")
    rows = cur.fetchall()
    if rows:
        for r in rows:
            print(f"  {r[0]:<30} {ts(r[1])}")
    else:
        print("  (no watermarks found)")

    # Check gmail/slack/drive_sweep watermarks separately if they exist elsewhere
    print("\n--- GMAIL / SLACK / DRIVE SWEEP (checkpoint_state) ---")
    try:
        cur.execute("SELECT source, last_processed FROM checkpoint_state ORDER BY source")
        for r in cur.fetchall():
            print(f"  {r[0]:<30} {r[1]}")
    except Exception:
        print("  checkpoint_state has different schema -- checking...")
        cur.execute("PRAGMA table_info(checkpoint_state)")
        cols = [c[1] for c in cur.fetchall()]
        print(f"  columns: {cols}")
        cur.execute("SELECT * FROM checkpoint_state LIMIT 10")
        for r in cur.fetchall():
            print(f"  {r}")

    conn.close()

if __name__ == "__main__":
    main()
