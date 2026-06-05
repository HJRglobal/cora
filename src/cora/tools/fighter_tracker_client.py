"""Fighter Influencer Tracker — Google Sheets reader for F3 sponsored athletes.

Reads the F3 Fighter Influencer Deliverable Tracker spreadsheet via the
Google Sheets API using the same service account as the cashflow connector.

Sheet ID stored in FIGHTER_TRACKER_SHEET_ID env var.
Sheet URL: https://docs.google.com/spreadsheets/d/1tPpsdUrvXaYq7Cz77L5yYwEC6plptO_xcGY3JncPK28

Tab structure (one per platform):
  Instagram | Facebook | TikTok

Columns per tab:
  A: Fighter Name
  B: Handle
  C: Campaign Month (YYYY-MM, e.g. 2026-06)
  D: Hard Post (Date completed, or blank)
  E: Story 1 (Date completed, or blank)
  F: Story 2 (Date completed, or blank)

Make.com writes the date cells when a fighter posts.
Cora reads this sheet on demand to answer Alex's status questions.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SHEET_ID_ENV = "FIGHTER_TRACKER_SHEET_ID"
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
_PLATFORMS = ["Instagram", "Facebook", "TikTok"]

# Column indices (0-based after reading values)
_COL_NAME     = 0
_COL_HANDLE   = 1
_COL_MONTH    = 2
_COL_POST     = 3
_COL_STORY1   = 4
_COL_STORY2   = 5


class FighterTrackerError(Exception):
    pass


def _sheet_id() -> str:
    val = os.environ.get(_SHEET_ID_ENV, "")
    if not val:
        raise FighterTrackerError(
            f"{_SHEET_ID_ENV} not set -- fighter tracker sheet not configured."
        )
    return val


def _get_service():
    """Build a Sheets API service using the Cora calendar service account."""
    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_path or not Path(sa_path).exists():
        raise FighterTrackerError("GOOGLE_SERVICE_ACCOUNT_JSON not set or file not found.")
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds = service_account.Credentials.from_service_account_file(
            sa_path, scopes=_SCOPES
        )
        return build("sheets", "v4", credentials=creds, cache_discovery=False)
    except Exception as exc:
        raise FighterTrackerError(f"Could not build Sheets service: {exc}") from exc


def read_tab(platform: str, campaign_month: str | None = None) -> list[dict]:
    """Read one platform tab and return a list of fighter dicts.

    Each dict has keys: name, handle, month, post, story1, story2,
    post_done, story1_done, story2_done, all_done, deliverables_done (int 0-3).

    If campaign_month is provided (YYYY-MM), filters to rows matching that month
    OR rows where the month column is blank (treat as current month).
    """
    service = _get_service()
    sid = _sheet_id()

    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sid, range=f"{platform}!A2:F200")
            .execute()
        )
    except Exception as exc:
        raise FighterTrackerError(f"Sheets API error reading {platform}: {exc}") from exc

    rows = result.get("values", [])
    fighters = []
    for row in rows:
        # Pad row to 6 columns
        while len(row) < 6:
            row.append("")

        name      = row[_COL_NAME].strip()
        handle    = row[_COL_HANDLE].strip()
        month     = row[_COL_MONTH].strip()
        post_date = row[_COL_POST].strip()
        s1_date   = row[_COL_STORY1].strip()
        s2_date   = row[_COL_STORY2].strip()

        if not name:
            continue

        # Month filter
        if campaign_month and month and month != campaign_month:
            continue

        done_count = sum(1 for d in (post_date, s1_date, s2_date) if d)
        fighters.append({
            "name":       name,
            "handle":     handle,
            "month":      month or campaign_month or "",
            "post":       post_date,
            "story1":     s1_date,
            "story2":     s2_date,
            "post_done":  bool(post_date),
            "story1_done": bool(s1_date),
            "story2_done": bool(s2_date),
            "all_done":   done_count == 3,
            "deliverables_done": done_count,
        })
    return fighters


def format_compliance_for_slack(
    platform: str = "Instagram",
    campaign_month: str | None = None,
    show_complete: bool = False,
) -> str:
    """Return a Slack-formatted compliance summary for one platform tab.

    By default only shows fighters who are NOT fully complete (actionable view).
    Pass show_complete=True to include everyone.
    """
    try:
        fighters = read_tab(platform, campaign_month)
    except FighterTrackerError as exc:
        return f"Could not read fighter tracker sheet: {exc}"

    if not fighters:
        return f"No data found in the {platform} tab{' for ' + campaign_month if campaign_month else ''}."

    month_label = campaign_month or date.today().strftime("%Y-%m")
    total = len(fighters)
    complete = sum(1 for f in fighters if f["all_done"])
    pending  = total - complete

    lines = [
        f"*F3 Fighter Compliance -- {platform} -- {month_label}*",
        f"{complete}/{total} fighters complete | {pending} outstanding",
        "",
    ]

    incomplete = [f for f in fighters if not f["all_done"]]
    if incomplete:
        lines.append("*Still outstanding:*")
        for f in sorted(incomplete, key=lambda x: x["deliverables_done"]):
            name   = f["name"]
            handle = f"@{f['handle']}" if f["handle"] else ""
            post   = "✅" if f["post_done"]   else "❌ post"
            s1     = "✅" if f["story1_done"] else "❌ story1"
            s2     = "✅" if f["story2_done"] else "❌ story2"
            done   = f["deliverables_done"]
            missing_parts = [p for p in [
                ("post" if not f["post_done"] else None),
                ("story1" if not f["story1_done"] else None),
                ("story2" if not f["story2_done"] else None),
            ] if p]
            missing_str = ", ".join(missing_parts)
            lines.append(f"  {done}/3 -- *{name}* {handle} -- missing: {missing_str}")

    if show_complete:
        done_fighters = [f for f in fighters if f["all_done"]]
        if done_fighters:
            lines.append("")
            lines.append("*Complete:*")
            for f in done_fighters:
                lines.append(f"  ✅ {f['name']} (@{f['handle']})")

    sheet_url = f"https://docs.google.com/spreadsheets/d/{_sheet_id()}"
    lines.append("")
    lines.append(f"_<{sheet_url}|View full tracker> -- Make.com updates automatically when fighters post._")

    return "\n".join(lines)
