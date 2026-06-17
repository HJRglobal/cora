r"""Fireflies DWD coverage monitor — weekly check that every DWD user's meetings
are actually being captured by Fireflies (not just Harrison's).

Enumerates Fireflies workspace members (admin `users` query), cross-references the
in-repo DWD roster (monitored-email-accounts.yaml, alias-collapsed), and reports who
is COVERED / MEMBER_NO_RECORDINGS / NOT_A_MEMBER. First ship is digest-only: a DM to
Harrison so he can eyeball the gap list before any teammate is nudged.

Usage:
    .venv\Scripts\python.exe scripts/run_fireflies_coverage.py --dry-run
    .venv\Scripts\python.exe scripts/run_fireflies_coverage.py --digest-only
    .venv\Scripts\python.exe scripts/run_fireflies_coverage.py --nudge

Flags:
    --dry-run       no Slack writes; print the report to stdout
    --digest-only   send the Harrison digest; do NOT nudge teammates (default-safe)
    --nudge         also DM each uncovered user (7-day throttle per user)
    --days N        recency window for the COVERED cross-check (default 30)

Environment variables required (in .env):
    SLACK_BOT_TOKEN     Cora's bot token
    FIREFLIES_API_KEY   Fireflies admin key (Harrison)
"""

import argparse
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

from cora.connectors import fireflies_connector, fireflies_coverage

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DB_PATH = _REPO_ROOT / "data" / "fireflies_coverage.db"
_HARRISON_USER_ID = "U0B2RM2JYJ1"
_THROTTLE_DAYS = 7


# ---------------------------------------------------------------------------
# DB helpers (nudge throttle)
# ---------------------------------------------------------------------------

def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_nudge_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coverage_nudge_log (
            slack_user_id  TEXT PRIMARY KEY,
            nudged_at      TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _get_last_nudged_at(conn: sqlite3.Connection, slack_user_id: str) -> datetime | None:
    row = conn.execute(
        "SELECT nudged_at FROM coverage_nudge_log WHERE slack_user_id = ?",
        (slack_user_id,),
    ).fetchone()
    if not row:
        return None
    try:
        dt = datetime.fromisoformat(row["nudged_at"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _upsert_nudge_log(conn: sqlite3.Connection, slack_user_id: str, nudged_at: datetime) -> None:
    conn.execute(
        """
        INSERT INTO coverage_nudge_log (slack_user_id, nudged_at)
        VALUES (?, ?)
        ON CONFLICT(slack_user_id) DO UPDATE SET nudged_at = excluded.nudged_at
        """,
        (slack_user_id, nudged_at.isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Slack helper
# ---------------------------------------------------------------------------

def _post_slack_message(channel: str, text: str) -> bool:
    import requests

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("fireflies_coverage: SLACK_BOT_TOKEN not set — cannot post")
        return False

    from cora.slack_egress import sanitize_text  # noqa: PLC0415 -- B1: raw POST bypasses the WebClient patch
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"channel": channel, "text": sanitize_text(text), "mrkdwn": True},
        timeout=15,
    )
    data = resp.json() if resp.ok else {}
    if not data.get("ok"):
        log.warning(
            "fireflies_coverage: Slack post failed channel=%s error=%s",
            channel, data.get("error", resp.status_code),
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Report assembly (network + classification)
# ---------------------------------------------------------------------------

def build_report(days: int) -> fireflies_coverage.CoverageReport:
    """Enumerate members + classify the DWD roster. Fail-closed on enumeration error.

    Returns a CoverageReport. If the Fireflies `users` query fails, returns a report
    with enumerate_failed=True (roster only) so the digest still sends.
    """
    humans = fireflies_coverage.load_dwd_humans()

    try:
        members = fireflies_connector.list_team_members()
    except fireflies_connector.FirefliesConnectorError as exc:
        log.error("fireflies_coverage: could not enumerate members: %s", exc)
        report = fireflies_coverage.classify(humans, [])
        report.enumerate_failed = True
        return report

    # Optional recency cross-check: probe only members that have transcripts (small set).
    # If ANY probe fails, disable refinement entirely (pass None) so we never wrongly
    # demote a covered member on a transient error.
    recent_host_emails: set[str] | None = set()
    member_emails_with_transcripts = {
        m["email"] for m in members if int(m.get("num_transcripts") or 0) > 0
    }
    try:
        for email in member_emails_with_transcripts:
            if fireflies_connector.has_recent_host_meeting(email, days=days):
                recent_host_emails.add(email)
    except fireflies_connector.FirefliesConnectorError as exc:
        log.warning("fireflies_coverage: recency probe failed (%s) — skipping refinement", exc)
        recent_host_emails = None

    return fireflies_coverage.classify(humans, members, recent_host_emails=recent_host_emails)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_coverage(dry_run: bool = False, nudge: bool = False, days: int = 30) -> None:
    report = build_report(days)
    digest = fireflies_coverage.format_digest(report, days=days)

    log.info("fireflies_coverage: %s", report.summary_line)

    if dry_run:
        print(digest)
        print("\n--- summary:", report.summary_line)
        if nudge:
            print("--- (nudge requested, but --dry-run: no DMs sent)")
        return

    # Always send the Harrison digest first.
    if _post_slack_message(_HARRISON_USER_ID, digest):
        log.info("fireflies_coverage: digest DM sent to Harrison")
    else:
        log.error("fireflies_coverage: failed to send digest DM to Harrison")

    if not nudge:
        return

    if report.enumerate_failed:
        log.warning("fireflies_coverage: enumeration failed — skipping all nudges")
        return

    now_utc = datetime.now(tz=timezone.utc)
    throttle_cutoff = now_utc - timedelta(days=_THROTTLE_DAYS)
    uncovered = report.not_a_member + report.member_no_recordings

    nudged = 0
    throttled = 0
    skipped_no_slack = 0
    with _db_conn() as conn:
        _ensure_nudge_table(conn)
        for r in uncovered:
            sid = r.human.slack_user_id
            if not sid:
                skipped_no_slack += 1
                continue
            last = _get_last_nudged_at(conn, sid)
            if last and last > throttle_cutoff:
                throttled += 1
                continue
            if _post_slack_message(sid, fireflies_coverage.nudge_text(r)):
                _upsert_nudge_log(conn, sid, now_utc)
                nudged += 1
                log.info("fireflies_coverage: nudged %s (%s)", r.human.name, r.status)
            else:
                log.error("fireflies_coverage: failed to nudge %s", r.human.name)

    log.info(
        "fireflies_coverage: nudges done — %d nudged, %d throttled, %d no-slack",
        nudged, throttled, skipped_no_slack,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fireflies DWD coverage monitor")
    p.add_argument("--dry-run", action="store_true", help="no Slack writes; print report")
    p.add_argument("--digest-only", action="store_true", help="send Harrison digest, no nudges")
    p.add_argument("--nudge", action="store_true", help="also DM each uncovered user")
    p.add_argument("--days", type=int, default=30, help="recency window (default 30)")
    return p.parse_args(argv)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                _REPO_ROOT / "logs" / f"fireflies-coverage-{date.today().strftime('%Y-%m-%d')}.log",
                encoding="utf-8",
            ),
        ],
    )
    args = _parse_args()
    # --nudge enables nudging; --digest-only (or neither) stays digest-only.
    run_coverage(dry_run=args.dry_run, nudge=args.nudge, days=args.days)
