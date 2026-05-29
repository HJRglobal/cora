"""LinkedIn Spy prospect tracker — SQLite-backed dedup store.

Tracks every Apollo person ID the weekly scanner has surfaced so repeat runs
don't resurface the same prospects. Each stored row carries a Claude-generated
brand fit label and personalized LinkedIn connection draft.

DB: data/linkedin_spy.db
"""

import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DB_PATH = _REPO_ROOT / "data" / "linkedin_spy.db"


class LinkedInSpyClientError(Exception):
    pass


def _get_conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS prospect_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            apollo_id       TEXT    NOT NULL UNIQUE,
            name            TEXT,
            title           TEXT    NOT NULL DEFAULT '',
            company         TEXT    NOT NULL DEFAULT '',
            linkedin_url    TEXT    NOT NULL DEFAULT '',
            city            TEXT    NOT NULL DEFAULT '',
            state           TEXT    NOT NULL DEFAULT '',
            country         TEXT    NOT NULL DEFAULT 'US',
            brand_fit       TEXT    NOT NULL DEFAULT '',
            message_draft   TEXT    NOT NULL DEFAULT '',
            scanned_at      TEXT    NOT NULL,
            slack_notified  INTEGER NOT NULL DEFAULT 0,
            outreach_sent   INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_prospect_apollo_id  ON prospect_log(apollo_id);
        CREATE INDEX IF NOT EXISTS idx_prospect_scanned_at ON prospect_log(scanned_at);
        CREATE INDEX IF NOT EXISTS idx_prospect_notified   ON prospect_log(slack_notified);
    """)
    conn.commit()


def is_already_seen(apollo_id: str) -> bool:
    """Return True if this Apollo person ID has already been logged."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM prospect_log WHERE apollo_id = ?", (apollo_id,)
        ).fetchone()
    return row is not None


def log_prospect(
    *,
    apollo_id: str,
    name: str | None,
    title: str,
    company: str,
    linkedin_url: str,
    city: str = "",
    state: str = "",
    country: str = "US",
    brand_fit: str = "",
    message_draft: str = "",
) -> dict[str, Any]:
    """Insert a new prospect. Silently skips duplicates. Returns the stored row."""
    now_str = date.today().isoformat()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO prospect_log
                (apollo_id, name, title, company, linkedin_url,
                 city, state, country, brand_fit, message_draft,
                 scanned_at, slack_notified, outreach_sent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (
                apollo_id, name, title, company, linkedin_url,
                city, state, country, brand_fit, message_draft, now_str,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM prospect_log WHERE apollo_id = ?", (apollo_id,)
        ).fetchone()
    return dict(row)


def mark_slack_notified(prospect_id: int) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE prospect_log SET slack_notified = 1 WHERE id = ?", (prospect_id,)
        )
        conn.commit()


def get_pending_report_prospects(limit: int = 10) -> list[dict[str, Any]]:
    """Return the most-recently scanned prospects not yet included in a Slack report."""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM prospect_log
            WHERE slack_notified = 0
            ORDER BY scanned_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_total_seen() -> int:
    """Total unique prospects logged across all scans."""
    with _get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) FROM prospect_log").fetchone()
    return row[0] if row else 0
