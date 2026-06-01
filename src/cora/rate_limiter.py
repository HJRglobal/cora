"""Sliding-window rate limiter for app_mention events.

Hits are persisted to a small SQLite file (data/rate_limiter.db) so rate-limit
windows survive process restarts.  The in-memory deque cache is the fast path;
SQLite is only written on allowed requests and read on startup (lazy per key).
"""

import sqlite3
import time
from collections import deque
from pathlib import Path
from threading import Lock

_USER_LIMIT = 10
_CHANNEL_LIMIT = 50
_WINDOW = 3600  # seconds

_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "rate_limiter.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_hits (
    kind      TEXT NOT NULL,   -- 'user' or 'channel'
    key_id    TEXT NOT NULL,
    hit_ts    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_hits_kind_key ON rate_hits(kind, key_id);
CREATE INDEX IF NOT EXISTS rate_hits_ts       ON rate_hits(hit_ts);
"""


def _open_db(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or str(_DB_PATH)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


class RateLimiter:
    def __init__(self, db_path: str | None = None) -> None:
        """Create a RateLimiter.

        Args:
            db_path: Path to the SQLite database file.  Pass ``":memory:"``
                for an isolated in-memory store (useful in tests).  If
                ``None``, uses the default ``data/rate_limiter.db``.
        """
        self._user: dict[str, deque[float]] = {}
        self._channel: dict[str, deque[float]] = {}
        self._lock = Lock()
        try:
            self._db: sqlite3.Connection | None = _open_db(db_path)
        except Exception:
            self._db = None  # fall back to in-memory only

    def _load_key(self, kind: str, key_id: str) -> deque[float]:
        """Load timestamps from SQLite for a key not yet in memory."""
        dq: deque[float] = deque()
        if self._db is None:
            return dq
        cutoff = time.time() - _WINDOW
        try:
            rows = self._db.execute(
                "SELECT hit_ts FROM rate_hits WHERE kind=? AND key_id=? AND hit_ts>?",
                (kind, key_id, cutoff),
            ).fetchall()
            dq.extend(sorted(r[0] for r in rows))
        except Exception:
            pass
        return dq

    def _persist(self, kind: str, key_id: str, ts: float) -> None:
        if self._db is None:
            return
        try:
            cutoff = ts - _WINDOW
            self._db.execute(
                "DELETE FROM rate_hits WHERE kind=? AND key_id=? AND hit_ts<=?",
                (kind, key_id, cutoff),
            )
            self._db.execute(
                "INSERT INTO rate_hits(kind, key_id, hit_ts) VALUES(?,?,?)",
                (kind, key_id, ts),
            )
            self._db.commit()
        except Exception:
            pass

    @staticmethod
    def _evict(dq: deque[float], now: float) -> None:
        while dq and now - dq[0] > _WINDOW:
            dq.popleft()

    def check(self, user_id: str, channel_id: str) -> tuple[bool, str | None]:
        """Return (True, None) if allowed, (False, "user"|"channel") if capped.

        On allow, records the timestamp in both counters and persists to SQLite.
        User cap is evaluated before channel cap.
        Thread-safe.
        """
        now = time.time()
        with self._lock:
            if user_id not in self._user:
                self._user[user_id] = self._load_key("user", user_id)
            if channel_id not in self._channel:
                self._channel[channel_id] = self._load_key("channel", channel_id)

            user_dq = self._user[user_id]
            channel_dq = self._channel[channel_id]

            self._evict(user_dq, now)
            self._evict(channel_dq, now)

            if len(user_dq) >= _USER_LIMIT:
                return (False, "user")
            if len(channel_dq) >= _CHANNEL_LIMIT:
                return (False, "channel")

            user_dq.append(now)
            channel_dq.append(now)

        self._persist("user", user_id, now)
        self._persist("channel", channel_id, now)
        return (True, None)


_LIMITER = RateLimiter()


def check(user_id: str, channel_id: str) -> tuple[bool, str | None]:
    return _LIMITER.check(user_id, channel_id)
