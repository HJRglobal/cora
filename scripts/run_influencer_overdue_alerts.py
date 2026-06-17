r"""Proactive overdue alerts — DMs Alex once per 72 h for each overdue deliverable.

Queries all deliverables with status=pending AND due_date < today, then checks a
throttle table (overdue_alert_log) to avoid spamming. Only fires a DM if the last
alert for that deliverable_id was more than 72 hours ago (or never sent).

Usage (called by Windows Task Scheduler — e.g. daily or every few hours):
    .venv\Scripts\python.exe scripts/run_influencer_overdue_alerts.py

Environment variables required (in .env):
    SLACK_BOT_TOKEN   Cora's bot token
"""

import logging
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path bootstrap — must happen before any cora imports
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora.tools import influencer_client

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DB_PATH       = _REPO_ROOT / "data" / "influencer_tracker.db"
_ALEX_USER_ID  = "U0B3VGWJTMJ"   # Alex Cordova
_THROTTLE_HOURS = 72              # minimum hours between alerts for the same deliverable


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db_conn() -> sqlite3.Connection:
    """Open the influencer tracker DB with Row factory and WAL mode."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_alert_log_table(conn: sqlite3.Connection) -> None:
    """Create overdue_alert_log if it doesn't exist yet."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS overdue_alert_log (
            deliverable_id  INTEGER PRIMARY KEY,
            alerted_at      TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _get_last_alerted_at(conn: sqlite3.Connection, deliverable_id: int) -> datetime | None:
    """Return the last alerted_at datetime for this deliverable, or None if never alerted."""
    row = conn.execute(
        "SELECT alerted_at FROM overdue_alert_log WHERE deliverable_id = ?",
        (deliverable_id,),
    ).fetchone()
    if not row:
        return None
    try:
        # Stored as ISO format; parse and make UTC-aware if needed
        dt = datetime.fromisoformat(row["alerted_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _upsert_alert_log(conn: sqlite3.Connection, deliverable_id: int, alerted_at: datetime) -> None:
    """Insert or update the alert log row for the given deliverable."""
    conn.execute(
        """
        INSERT INTO overdue_alert_log (deliverable_id, alerted_at)
        VALUES (?, ?)
        ON CONFLICT(deliverable_id) DO UPDATE SET alerted_at = excluded.alerted_at
        """,
        (deliverable_id, alerted_at.isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Slack helper
# ---------------------------------------------------------------------------

def _post_slack_message(channel: str, text: str) -> bool:
    """POST a Slack message via chat.postMessage. Returns True on success."""
    import requests

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("influencer_overdue_alerts: SLACK_BOT_TOKEN not set — cannot post")
        return False

    from cora.slack_egress import sanitize_text  # noqa: PLC0415 -- B1: raw POST bypasses the WebClient patch
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"channel": channel, "text": sanitize_text(text), "mrkdwn": True},
        timeout=15,
    )
    data = resp.json() if resp.ok else {}
    if not data.get("ok"):
        log.warning(
            "influencer_overdue_alerts: Slack post failed channel=%s error=%s",
            channel, data.get("error", resp.status_code),
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Alert message formatter
# ---------------------------------------------------------------------------

def _format_overdue_dm(row: dict) -> str:
    """Format the DM text for a single overdue deliverable."""
    due_date_str = row.get("due_date") or "unknown date"

    # Calculate days overdue
    try:
        due_date = date.fromisoformat(due_date_str)
        days_overdue = (date.today() - due_date).days
        days_label = f"{days_overdue} day{'s' if days_overdue != 1 else ''} overdue"
    except ValueError:
        days_label = "overdue"

    platform     = row.get("platform", "unknown").capitalize()
    d_type       = (row.get("deliverable_type") or "deliverable").replace("_", " ")
    athlete_name = row.get("athlete_name", "unknown athlete")
    row_id       = row["id"]

    return (
        f"🔴 *Overdue deliverable*: #{row_id} — {athlete_name} owed a "
        f"{platform} {d_type} (was due {due_date_str}, {days_label}). "
        f"Use `@Cora complete deliverable {row_id}` when they post."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_overdue_alerts() -> None:
    """Check all overdue deliverables and DM Alex for any not alerted in the last 72 h."""
    now_utc = datetime.now(tz=timezone.utc)
    throttle_cutoff = now_utc - timedelta(hours=_THROTTLE_HOURS)

    # Fetch all overdue deliverables (status=pending, due_date < today)
    all_open = influencer_client.get_deliverables(
        include_complete=False,
        include_waived=False,
        limit=500,
    )
    overdue_rows = [r for r in all_open if r["display_status"] == "overdue"]

    if not overdue_rows:
        log.info("influencer_overdue_alerts: no overdue deliverables — nothing to alert")
        return

    log.info("influencer_overdue_alerts: found %d overdue deliverable(s)", len(overdue_rows))

    alerted_count  = 0
    throttled_count = 0

    with _db_conn() as conn:
        _ensure_alert_log_table(conn)

        for row in overdue_rows:
            deliverable_id = row["id"]

            # Check throttle
            last_alerted = _get_last_alerted_at(conn, deliverable_id)
            if last_alerted and last_alerted > throttle_cutoff:
                hours_ago = (now_utc - last_alerted).total_seconds() / 3600
                log.debug(
                    "influencer_overdue_alerts: skipping #%d — alerted %.1fh ago (throttle=%dh)",
                    deliverable_id, hours_ago, _THROTTLE_HOURS,
                )
                throttled_count += 1
                continue

            # Send DM to Alex
            dm_text = _format_overdue_dm(row)
            sent = _post_slack_message(_ALEX_USER_ID, dm_text)

            if sent:
                _upsert_alert_log(conn, deliverable_id, now_utc)
                alerted_count += 1
                log.info(
                    "influencer_overdue_alerts: alerted Alex about #%d (%s — %s %s, due %s)",
                    deliverable_id,
                    row.get("athlete_name"),
                    row.get("platform"),
                    row.get("deliverable_type"),
                    row.get("due_date"),
                )
            else:
                log.error(
                    "influencer_overdue_alerts: failed to DM Alex for #%d", deliverable_id
                )

    log.info(
        "influencer_overdue_alerts: done — %d alerted, %d throttled",
        alerted_count, throttled_count,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                _REPO_ROOT / "logs" / f"influencer-overdue-alerts-{date.today().strftime('%Y-%m-%d')}.log",
                encoding="utf-8",
            ),
        ],
    )
    run_overdue_alerts()
