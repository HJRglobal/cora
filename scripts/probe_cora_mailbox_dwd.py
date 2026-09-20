#!/usr/bin/env python3
"""READ-ONLY DWD probe for cora@hjrglobal.com (Code #13 Rider 1 S-A smoke 1).

STAGED FOR HARRISON -- the build session did NOT run this (no live probes in a
worktree builder). It answers one question: can the Cora service account
impersonate cora@ for Gmail AND Calendar? Two list calls, one result each, IDS
ONLY printed -- never a subject, sender, body, title or attendee.

  * Gmail: gmail_reader._build_service (gmail.modify scope, exactly the scope the
    nightly sweep uses) -> users().messages().list(userId="me", maxResults=1).
  * Calendar: calendar_client._build_service(write=True) -> events().list(
    calendarId="primary", maxResults=1). write=True selects the calendar.events
    SCOPE NAME, which is REQUIRED for events.list (probed 2026-06-11: the
    freebusy scope 403s it). The call is still a read; nothing is created.

Exit codes: 0 both succeed; 1 either fails (the exact error CLASS + HTTP status is
printed, never a token or a payload). A failure for a reason other than a missing
scope (e.g. the SA client id is not tenant-wide) is a kickoff section 8 STOP.

Usage:
    python scripts/probe_cora_mailbox_dwd.py
    python scripts/probe_cora_mailbox_dwd.py --mailbox cora@hjrglobal.com
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
load_dotenv(_REPO_ROOT / ".env", override=True)

DEFAULT_MAILBOX = "cora@hjrglobal.com"


def _status(exc: BaseException) -> str:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    return f"{type(exc).__name__}" + (f" HTTP {status}" if status else "")


def probe_gmail(mailbox: str) -> tuple[bool, str]:
    """(ok, one message id or the error class). Read-only."""
    try:
        from cora.connectors.gmail_reader import _build_service
        svc = _build_service(mailbox)
        resp = svc.users().messages().list(userId="me", maxResults=1).execute()
        ids = [m.get("id", "") for m in (resp.get("messages") or [])]
        return True, (ids[0] if ids else "(mailbox empty -- list succeeded)")
    except Exception as exc:  # noqa: BLE001 -- report the class, never the payload
        return False, _status(exc)


def probe_calendar(mailbox: str) -> tuple[bool, str]:
    """(ok, one event id or the error class). Read-only; write=True is the SCOPE
    NAME events.list requires, not a write."""
    try:
        from cora.tools.calendar_client import _build_service
        svc = _build_service(mailbox, write=True)
        resp = svc.events().list(calendarId="primary", maxResults=1).execute()
        ids = [e.get("id", "") for e in (resp.get("items") or [])]
        return True, (ids[0] if ids else "(calendar empty -- list succeeded)")
    except Exception as exc:  # noqa: BLE001
        return False, _status(exc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only DWD probe for the cora@ mailbox.")
    ap.add_argument("--mailbox", default=DEFAULT_MAILBOX)
    args = ap.parse_args(argv)

    ok_g, g = probe_gmail(args.mailbox)
    ok_c, c = probe_calendar(args.mailbox)
    print(f"gmail    {args.mailbox}: {'OK' if ok_g else 'FAIL'} {g}")
    print(f"calendar {args.mailbox}: {'OK' if ok_c else 'FAIL'} {c}")
    return 0 if (ok_g and ok_c) else 1


if __name__ == "__main__":
    raise SystemExit(main())
