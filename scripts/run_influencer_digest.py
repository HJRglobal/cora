r"""Weekly compliance digest — posts Monday summaries to #f3-athletes and DMs Alex.

Sections:
  🔴 Overdue       — status=pending AND due_date < today
  📅 Due this week — status=pending AND due_date BETWEEN today AND today+7
  ✅ Completed     — status=complete AND updated_at >= 7 days ago

Usage (called by Windows Task Scheduler every Monday morning):
    .venv\Scripts\python.exe scripts/run_influencer_digest.py

Environment variables required (in .env):
    SLACK_BOT_TOKEN   Cora's bot token
"""

import logging
import os
import sqlite3
import sys
from datetime import date, timedelta
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

_DB_PATH = _REPO_ROOT / "data" / "influencer_tracker.db"

_ATHLETES_CHANNEL = "C0B6GT3117Y"   # #f3-athletes
_ALEX_USER_ID     = "U0B3VGWJTMJ"   # Alex Cordova (DM target)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db_conn() -> sqlite3.Connection:
    """Open the influencer tracker DB with Row factory."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _query_completed_since(since_date: date) -> list[dict]:
    """Return deliverables marked complete on or after since_date."""
    with _db_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM influencer_deliverables
            WHERE status = 'complete'
              AND updated_at >= ?
            ORDER BY updated_at DESC
            """,
            (since_date.isoformat(),),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Slack helper
# ---------------------------------------------------------------------------

def _post_slack_message(channel: str, text: str) -> bool:
    """POST a Slack message via chat.postMessage. Returns True on success."""
    import requests

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("influencer_digest: SLACK_BOT_TOKEN not set — cannot post")
        return False

    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"channel": channel, "text": text, "mrkdwn": True},
        timeout=15,
    )
    data = resp.json() if resp.ok else {}
    if not data.get("ok"):
        log.warning(
            "influencer_digest: Slack post failed channel=%s error=%s",
            channel, data.get("error", resp.status_code),
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Digest builder
# ---------------------------------------------------------------------------

def _build_digest(monday: date) -> str:
    """Build the full digest message text for the given Monday."""
    today     = monday
    week_end  = today + timedelta(days=7)
    last_week = today - timedelta(days=7)

    header = f"*F3 Athletes — Weekly Compliance Digest* | Week of {monday.isoformat()}"

    # --- 🔴 Overdue ---
    all_open = influencer_client.get_deliverables(
        include_complete=False,
        include_waived=False,
        limit=500,
    )
    overdue_rows = [r for r in all_open if r["display_status"] == "overdue"]

    # --- 📅 Due this week ---
    due_this_week = [
        r for r in all_open
        if r["display_status"] == "pending"
        and r.get("due_date")
        and today.isoformat() <= r["due_date"] <= week_end.isoformat()
    ]

    # --- ✅ Completed since last Monday ---
    completed_rows = _query_completed_since(last_week)
    # Attach display_status so _format_deliverable_line works correctly
    for r in completed_rows:
        r["display_status"] = "complete"

    # --- Assemble ---
    sections = [header, ""]

    # Overdue section
    if overdue_rows:
        sections.append(f"*🔴 Overdue ({len(overdue_rows)})*")
        for r in overdue_rows:
            sections.append(influencer_client._format_deliverable_line(r))
    else:
        sections.append("*🔴 Overdue* — none! 🎉")

    sections.append("")

    # Due this week section
    if due_this_week:
        sections.append(f"*📅 Due this week ({len(due_this_week)})*")
        for r in due_this_week:
            sections.append(influencer_client._format_deliverable_line(r))
    else:
        sections.append("*📅 Due this week* — nothing due in the next 7 days.")

    sections.append("")

    # Completed section
    if completed_rows:
        sections.append(f"*✅ Completed since last Monday ({len(completed_rows)})*")
        for r in completed_rows:
            sections.append(influencer_client._format_deliverable_line(r))
    else:
        sections.append("*✅ Completed since last Monday* — none logged yet.")

    sections.append("")
    sections.append(
        "_Use `@Cora complete deliverable <id>` to mark a post done, "
        "or ask Cora for a full compliance report._"
    )

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_digest() -> None:
    """Build and send the weekly digest to #f3-athletes and Alex's DM."""
    # Compute the most recent Monday (or today if today is Monday)
    today = date.today()
    days_since_monday = today.weekday()  # 0=Mon … 6=Sun
    monday = today - timedelta(days=days_since_monday)

    log.info("influencer_digest: building digest for week of %s", monday)

    digest_text = _build_digest(monday)

    # 1. Post to #f3-athletes channel
    ok_channel = _post_slack_message(_ATHLETES_CHANNEL, digest_text)
    if ok_channel:
        log.info("influencer_digest: posted to #f3-athletes (%s)", _ATHLETES_CHANNEL)
    else:
        log.error("influencer_digest: failed to post to #f3-athletes")

    # 2. DM Alex directly (Slack opens DM automatically when channel=USER_ID)
    ok_dm = _post_slack_message(_ALEX_USER_ID, digest_text)
    if ok_dm:
        log.info("influencer_digest: DMed Alex (%s)", _ALEX_USER_ID)
    else:
        log.error("influencer_digest: failed to DM Alex")


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
                _REPO_ROOT / "logs" / f"influencer-digest-{date.today().strftime('%Y-%m-%d')}.log",
                encoding="utf-8",
            ),
        ],
    )
    run_digest()
