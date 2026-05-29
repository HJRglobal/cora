"""Active-thread registry — tracks Slack threads where Cora has responded.

When Cora answers an @mention in a thread, that thread is registered here.
Subsequent messages posted in the same thread (without @mentioning Cora) are
detected by handle_message_event and routed through the full Q&A pipeline,
giving the team a natural back-and-forth conversation experience.

A thread is considered "active" for up to TTL_SECONDS after Cora's last
interaction in it. After that window the thread goes cold; new questions need
a fresh @mention to re-activate it.

Storage: SQLite table in data/active_threads.db. Survives bot restarts so
long-running conversations don't lose context mid-session.

Thread lifecycle:
  @mention → register()      thread becomes active
  follow-up → touch()        idle timer resets
  TTL expires → cleanup()    row removed; next message requires @mention
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent.parent / "data" / "active_threads.db"

# How long (seconds) a thread stays active after the last interaction.
# 2 hours covers any realistic back-and-forth work session.
TTL_SECONDS = 7_200  # 2 hours

# Run cleanup every N register() calls so stale rows don't accumulate forever.
_CLEANUP_EVERY = 50
_register_count = 0


# ── DB helpers ────────────────────────────────────────────────────────────────


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_threads (
            channel_id    TEXT NOT NULL,
            thread_ts     TEXT NOT NULL,
            last_activity REAL NOT NULL,
            PRIMARY KEY (channel_id, thread_ts)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_last_activity ON active_threads(last_activity)")
    conn.commit()
    return conn


# ── Public API ────────────────────────────────────────────────────────────────


def register(channel_id: str, thread_ts: str) -> None:
    """Mark a thread as active. Called after Cora successfully responds to an @mention.

    Uses UPSERT so re-registering (e.g. multiple @mentions in the same thread)
    simply refreshes the last_activity timestamp.
    """
    global _register_count
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO active_threads (channel_id, thread_ts, last_activity) "
                "VALUES (?, ?, ?)",
                (channel_id, thread_ts, time.time()),
            )
        log.debug("active_thread_store: registered channel=%s thread_ts=%s", channel_id, thread_ts)
    except Exception as exc:
        log.warning("active_thread_store: register failed: %s", exc)
        return

    _register_count += 1
    if _register_count % _CLEANUP_EVERY == 0:
        cleanup()


def is_active(channel_id: str, thread_ts: str) -> bool:
    """Return True if Cora was active in this thread within TTL_SECONDS."""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT last_activity FROM active_threads WHERE channel_id=? AND thread_ts=?",
                (channel_id, thread_ts),
            ).fetchone()
        if row is None:
            return False
        return (time.time() - row[0]) < TTL_SECONDS
    except Exception as exc:
        log.warning("active_thread_store: is_active failed: %s", exc)
        return False


def touch(channel_id: str, thread_ts: str) -> None:
    """Refresh last_activity so the thread stays warm after a follow-up reply."""
    try:
        with _conn() as conn:
            conn.execute(
                "UPDATE active_threads SET last_activity=? WHERE channel_id=? AND thread_ts=?",
                (time.time(), channel_id, thread_ts),
            )
    except Exception as exc:
        log.warning("active_thread_store: touch failed: %s", exc)


def cleanup() -> int:
    """Delete threads older than TTL_SECONDS. Returns number of rows removed."""
    cutoff = time.time() - TTL_SECONDS
    try:
        with _conn() as conn:
            cur = conn.execute("DELETE FROM active_threads WHERE last_activity < ?", (cutoff,))
            deleted = cur.rowcount
        if deleted:
            log.info("active_thread_store: cleaned up %d stale threads", deleted)
        return deleted
    except Exception as exc:
        log.warning("active_thread_store: cleanup failed: %s", exc)
        return 0
