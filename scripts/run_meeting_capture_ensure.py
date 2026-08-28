#!/usr/bin/env python3
"""DWD ensure lane (cq-ffcf6e4ffe7c) -- put every qualifying roster meeting on the
capture identity's calendar, so its Fireflies seat auto-joins exactly once.

SHIPS DARK. Nothing here can write until BOTH:
  * CORA_ONECORA_ENSURE=live in .env, AND
  * this script is invoked with --apply.
Either gate alone leaves the lane read-only. That is deliberate -- these are
writes to real people's calendars, and no single mistake should start making them.

DO NOT ENABLE YET. As of 2026-08-27 cora@hjrglobal.com's Fireflies seat is INVITED
but NOT ACTIVE (verified live: it does not appear in the Fireflies `users` query).
Enabling this lane before the seat activates would put meetings on a calendar that
nothing is listening to, which looks like coverage and is not. The order is:
Harrison completes the cora@ sign-in -> the daily auditor shows the seat ACTIVE ->
then flip the flag.

HYBRID MECHANIC (plan of record, Option A + B):
  * guest-add  -- for in-domain-organised events; transparent, and the organiser's
                  own updates and cancellations flow to the capture identity.
  * event copy -- for externally-organised events we cannot guest-add to. The copy
                  carries the ORIGINAL Meet link, never a new one.

Run:  python scripts/run_meeting_capture_ensure.py [--day YYYY-MM-DD] [--apply]
      (default day: today AND tomorrow, matching the T+0/T+1 sweep)
Exit codes: 0 = ran, 1 = could not run.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

# Windows console guard -- real event titles carry emoji and cp1252 raises on them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

from cora import meeting_capture as mc  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("meeting-capture-ensure")

_AZ = timezone(timedelta(hours=-7))


def _run_day(day: str, cfg, *, apply: bool) -> mc.EnsureResult:
    result = mc.plan_ensure(day, cfg)
    result = mc.execute_ensure(result, cfg, apply=apply)

    print(f"\n=== {day} (mode={result.mode}, writing={result.applied}) ===")
    if result.failed_calendars:
        for email, err in result.failed_calendars:
            print(f"  !! calendar unreadable: {email} -- {err[:90]}")
    for act in sorted(result.actions, key=lambda a: (a.start_label, a.title)):
        mark = "APPLIED" if act.applied else ("ERROR" if act.error else "planned")
        if act.action == "skip":
            mark = "skipped"
        elif act.action == "none":
            mark = "ok"
        print(f"  [{mark:8s}] {act.start_label}  {act.action:9s}  {act.title[:52]:54s} {act.reason[:60]}")
        if act.error:
            print(f"             error: {act.error[:120]}")

    # HONEST DEGRADE: N qualifying, M ensured, K skipped -- and never a clean-looking
    # summary when a calendar could not be read.
    print(
        f"  -- {result.qualifying} qualifying, {result.ensured} already-covered-or-ensured, "
        f"{result.skipped} skipped by carve-out/structure"
        + (f", {len(result.failed_calendars)} CALENDAR(S) UNREADABLE" if result.failed_calendars else "")
    )

    mc.write_ledger([{
        "ts": datetime.now(timezone.utc).isoformat(),
        "lane": "ensure",
        "day": day,
        "mode": result.mode,
        "applied": result.applied,
        "action": a.action,
        "reason": a.reason,
        "event_id": a.event_id,       # ids only, never titles (D-082)
        "calendar": a.calendar_email,
        "was_applied": a.applied,
        "error": a.error,
    } for a in result.actions])
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="DWD meeting-capture ensure lane")
    ap.add_argument("--day", default="", help="YYYY-MM-DD (default: today and tomorrow)")
    ap.add_argument("--apply", action="store_true",
                    help="perform calendar writes (also requires CORA_ONECORA_ENSURE=live)")
    args = ap.parse_args()

    mode = mc.ensure_mode()
    if mode == "off":
        log.warning(
            "CORA_ONECORA_ENSURE is off -- the ensure lane is disabled. "
            "Set it to 'plan' to see what it would do, or 'live' (plus --apply) to enable writes."
        )
        return 0
    if args.apply and mode != "live":
        log.warning("--apply ignored: CORA_ONECORA_ENSURE=%s (needs 'live')", mode)

    try:
        cfg = mc.load_config()
    except mc.MeetingCaptureConfigError as exc:
        log.error("roster unusable: %s", exc)
        return 1

    if args.day.strip():
        try:
            datetime.strptime(args.day.strip(), "%Y-%m-%d")
        except ValueError:
            log.error("--day must be YYYY-MM-DD, got %r", args.day)
            return 1
        days = [args.day.strip()]
    else:
        today = datetime.now(_AZ)
        days = [today.strftime("%Y-%m-%d"), (today + timedelta(days=1)).strftime("%Y-%m-%d")]

    for day in days:
        _run_day(day, cfg, apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
