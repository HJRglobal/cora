"""MMA Lab x F3 Fighter Tracker -- Google Sheets reader.

Reads the "MMA Lab x F3 Fighters Tracker" spreadsheet via the Google Sheets API.
This tracks the MMA Lab sponsorship: 57 fighters must each post monthly on Instagram
(2 stories + 1 hard post tagging @f3energy + #DrinkF3). F3 pays MMA Lab $125 per
fighter who completes all 3 deliverables that month (max $6,250/month if all 57 post).

Sheet ID: FIGHTER_TRACKER_SHEET_ID env var
Sheet URL: https://docs.google.com/spreadsheets/d/1tPpsdUrvXaYq7Cz77L5yYwEC6plptO_xcGY3JncPK28

Tab structure -- one tab per MONTH (not platform):
  June 2026 | July 2026 | August 2026 | ... | December 2026

Columns per tab:
  A: Fighter Name
  B: Instagram Handle (without @)
  C: Hard Post (Date completed, or blank)
  D: Story 1 (Date completed, or blank)
  E: Story 2 (Date completed, or blank)
  F: All 3 Complete? (formula -- "YES" when C+D+E all filled)

Value tracker rows at bottom of each tab (auto-calculated):
  - Fighters completed all 3 deliverables
  - Amount owed to MMA Lab ($125 x count)
  - Maximum possible this month
  - Completion rate

Make.com writes dates into C/D/E when fighters post.
Cora reads on demand to answer Alex's status questions.
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
# Tab names follow "Month YYYY" pattern
_MONTH_TABS = [
    "June 2026","July 2026","August 2026","September 2026",
    "October 2026","November 2026","December 2026",
]

# Column indices (0-based) -- no Campaign Month column any more
_COL_NAME    = 0
_COL_HANDLE  = 1
_COL_POST    = 2   # C: Hard Post
_COL_STORY1  = 3   # D: Story 1
_COL_STORY2  = 4   # E: Story 2
_COL_DONE    = 5   # F: All 3 Complete? (formula, "YES" or "")

_PAY_PER_FIGHTER = 125


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


def _current_month_tab() -> str:
    """Return the current month's tab name, e.g. 'June 2026'."""
    return date.today().strftime("%B %Y")


def read_month_tab(month_tab: str) -> list[dict]:
    """Read one monthly tab and return a list of fighter dicts.

    month_tab: e.g. "June 2026", "July 2026"
    Each dict: name, handle, post, story1, story2,
               post_done, story1_done, story2_done, all_done, deliverables_done
    """
    service = _get_service()
    sid = _sheet_id()

    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sid, range=f"'{month_tab}'!A2:F80")
            .execute()
        )
    except Exception as exc:
        raise FighterTrackerError(f"Sheets API error reading tab '{month_tab}': {exc}") from exc

    rows = result.get("values", [])
    fighters = []
    for row in rows:
        while len(row) < 6:
            row.append("")

        name = row[_COL_NAME].strip()
        if not name or name.lower().startswith("mma lab"):
            continue  # skip tracker rows at bottom

        handle    = row[_COL_HANDLE].strip()
        post_date = row[_COL_POST].strip()
        s1_date   = row[_COL_STORY1].strip()
        s2_date   = row[_COL_STORY2].strip()
        done_flag = row[_COL_DONE].strip().upper()

        done_count = sum(1 for d in (post_date, s1_date, s2_date) if d)
        all_done   = done_count == 3 or done_flag == "YES"

        fighters.append({
            "name":            name,
            "handle":          handle,
            "post":            post_date,
            "story1":          s1_date,
            "story2":          s2_date,
            "post_done":       bool(post_date),
            "story1_done":     bool(s1_date),
            "story2_done":     bool(s2_date),
            "all_done":        all_done,
            "deliverables_done": done_count,
        })
    return fighters


def format_compliance_for_slack(
    month_tab: str | None = None,
    show_complete: bool = False,
) -> str:
    """Return a Slack-formatted MMA Lab compliance summary.

    month_tab: e.g. "June 2026". Defaults to current month.
    show_complete: if True, also lists fighters who finished all 3.
    """
    tab = month_tab or _current_month_tab()

    try:
        fighters = read_month_tab(tab)
    except FighterTrackerError as exc:
        return f"Could not read fighter tracker for {tab}: {exc}"

    if not fighters:
        return f"No fighter data found for {tab}. Check the sheet tab name."

    total    = len(fighters)
    complete = sum(1 for f in fighters if f["all_done"])
    pending  = total - complete
    owed     = complete * _PAY_PER_FIGHTER
    max_pay  = total * _PAY_PER_FIGHTER

    lines = [
        f"*MMA Lab x F3 -- Fighter Compliance -- {tab}*",
        f"",
        f"*{complete}/{total}* fighters completed all 3 deliverables",
        f"*Amount owed to MMA Lab: ${owed:,}* (of ${max_pay:,} max)",
        f"",
    ]

    incomplete = [f for f in fighters if not f["all_done"]]
    if incomplete:
        lines.append(f"*Still outstanding ({len(incomplete)} fighters):*")
        for f in sorted(incomplete, key=lambda x: x["deliverables_done"], reverse=True):
            done   = f["deliverables_done"]
            name   = f["name"]
            handle = f"@{f['handle']}" if f["handle"] else ""
            missing = []
            if not f["post_done"]:   missing.append("hard post")
            if not f["story1_done"]: missing.append("story 1")
            if not f["story2_done"]: missing.append("story 2")
            lines.append(f"  {done}/3 -- *{name}* {handle} -- needs: {', '.join(missing)}")

    if show_complete and complete > 0:
        lines.append("")
        lines.append(f"*Completed ({complete} fighters -- ${owed:,} earned):*")
        for f in [f for f in fighters if f["all_done"]]:
            lines.append(f"  ✅ {f['name']} (@{f['handle']})")

    sheet_url = f"https://docs.google.com/spreadsheets/d/{_sheet_id()}"
    lines.append("")
    lines.append(f"_<{sheet_url}|Open tracker> -- Make.com logs dates automatically when fighters post._")

    return "\n".join(lines)
