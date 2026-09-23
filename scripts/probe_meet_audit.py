#!/usr/bin/env python3
"""STAGED, READ-ONLY probe for the Meet join audit lane (Code #14 R14-8).

Harrison runs this ONCE after granting `admin.reports.audit.readonly` to the Cora
service account's domain-wide-delegation grant, to prove the lane before it
decides anything in the 07:22 audit:

    .venv\\Scripts\\python.exe scripts\\probe_meet_audit.py --day 2026-09-22

It prints ONLY:
  * the lane state (live | dark:scope | dark:api | dark:subject | partial | error)
    and its fixed reason class;
  * counts: pages, call_ended events read, joins usable, human vs bot endpoints,
    distinct endpoints, distinct meetings;
  * the parameter KEY NAMES the log actually carries (never their values) -- this
    is how the parser's VERIFY-AT-BUILD field names are confirmed;
  * the age of the newest event (how far behind real time the log runs).

It NEVER prints an email, a display name, an IP, a meeting code, an event id or a
title, and it writes nothing anywhere (no ledger, no Slack, no KB). The only API
call is meet_audit.read_call_ended (Reports activities.list, applicationName=meet).

Exit codes: 0 = the lane read LIVE; 2 = any other state (dark / partial / error).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import meet_audit as ma  # noqa: E402

_AZ = timezone(timedelta(hours=-7))


def summarize(read: "ma.MeetAuditRead", *, now: datetime | None = None) -> list[str]:
    """The printable lines for one read: state, counts and key NAMES only."""
    now = now or datetime.now(timezone.utc)
    joins = list(read.joins)
    humans = [j for j in joins if j.is_human]
    meetings = {(j.meeting_code or j.calendar_event_id) for j in joins}
    lines = [
        f"state: {read.state}" + (f" ({read.reason})" if read.reason else ""),
        f"pages: {read.pages}",
        f"call_ended events read: {read.events_read}",
        f"joins usable: {len(joins)} (human {len(humans)}, bot {len(joins) - len(humans)})",
        f"distinct endpoints: {len({j.endpoint_key for j in joins})}",
        f"distinct meetings: {len(meetings)}",
        "parameter key names: " + (", ".join(read.param_keys) if read.param_keys else "(none observed)"),
    ]
    ends = [j.end_ts for j in joins if j.end_ts]
    if ends:
        age_min = max(0, int((now.timestamp() - max(ends)) // 60))
        lines.append(f"newest event age: {age_min} min")
    else:
        lines.append("newest event age: n/a (no events)")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only probe of the Meet join audit lane")
    ap.add_argument("--day", default="", help="AZ day YYYY-MM-DD (default: yesterday, AZ)")
    args = ap.parse_args(argv)
    day = args.day.strip() or (datetime.now(_AZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_AZ)
    except ValueError:
        print("--day must be YYYY-MM-DD")
        return 2
    end = start + timedelta(days=1)
    read = ma.read_call_ended(start - timedelta(hours=1), end + timedelta(hours=6))
    print(f"Meet join audit probe -- AZ day {day} (read-only)")
    for line in summarize(read):
        print("  " + line)
    return 0 if read.state == ma.STATE_LIVE else 2


if __name__ == "__main__":
    raise SystemExit(main())
