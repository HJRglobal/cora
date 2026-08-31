#!/usr/bin/env python3
"""Daily meeting-capture auditor (cq-ffcf6e4ffe7c) -- READ-ONLY.

Diffs yesterday's roster calendar events against Fireflies transcripts and posts
misses / duplicates / unexpected captures to #founder-operations.

WHY THIS IS LOAD-BEARING. Under the One Cora Notetaker architecture there is one
capture seat and no per-seat fallback, so a meeting the ensure lane misses is
captured by NOTHING. This restores the capture-gap slice of a Cowork-side sweep
that went dark 2026-07-24 -- the absence of which is why duplicate captures ran
unnoticed for three months. It does NOT restore that sweep's other jobs (Asana
drafting, decisions appends, summaries); none of those are rebuilt here.

DETERMINISTIC, NO LLM. A capture-gap report that hallucinates a meeting or
smooths away a miss is worse than no report at all.

READ-ONLY BY CONSTRUCTION: this script calls only calendar list + Fireflies query
+ one Slack post. It never writes a calendar, never touches the KB.

Dry-run is the DEFAULT: prints the report, posts nothing. `--post` sends it.

Run:  python scripts/run_meeting_capture_audit.py [--day YYYY-MM-DD] [--post]
Schedule: daily, 07:22 AZ (see deployment/setup-meeting-capture-audit-task.ps1).
Exit codes: 0 = ran (with or without findings), 1 = could not run.
"""

from __future__ import annotations

import argparse
import logging
from cora import run_marker
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

# Windows console guard. Real calendar titles carry emoji (a live probe on
# 2026-08-27 hit UnicodeEncodeError on a check-mark in an event title) and the
# default Windows stdout codec is cp1252, which RAISES on them. pytest captures
# stdout through a UTF-8 pipe and never sees this, so it can only be caught by
# running the thing.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

from cora import meeting_capture as mc  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("meeting-capture-audit")

_AZ = timezone(timedelta(hours=-7))  # Phoenix, no DST


def _post(text: str) -> bool:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("SLACK_BOT_TOKEN not set -- cannot post")
        return False
    try:
        from slack_sdk import WebClient  # noqa: PLC0415

        # B1 egress doctrine: route every outbound line through the boundary.
        from cora.reply_formatter import normalize_slack_bold  # noqa: PLC0415
        from cora.slack_egress import sanitize_text  # noqa: PLC0415

        WebClient(token=token).chat_postMessage(
            channel=mc.OPS_CHANNEL,
            text=normalize_slack_bold(sanitize_text(text)),
            unfurl_links=False,
            unfurl_media=False,
        )
        log.info("posted meeting-capture audit to #founder-operations")
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("post failed: %s", exc)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily meeting-capture audit (read-only)")
    ap.add_argument("--day", default="", help="YYYY-MM-DD to audit (default: yesterday, AZ)")
    ap.add_argument("--post", action="store_true", help="post to #founder-operations")
    args = ap.parse_args()

    day = args.day.strip() or (datetime.now(_AZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        log.error("--day must be YYYY-MM-DD, got %r", args.day)
        return 1

    try:
        cfg = mc.load_config()
    except mc.MeetingCaptureConfigError as exc:
        log.error("roster unusable: %s", exc)
        return 1

    log.info("auditing %s across %d roster calendars", day, len(cfg.active_members))
    report = mc.audit_day(day, cfg)
    text = mc.render_report(report)

    print("\n" + text + "\n")
    log.info(
        "audit %s: scheduled=%d captured=%d missed=%d dup=%d unmatched=%d "
        "skipped=%d failed_calendars=%d",
        day, report.scheduled, report.captured, len(report.misses),
        len(report.duplicates), len(report.unmatched_transcripts),
        len(report.skipped), len(report.failed_calendars),
    )

    mc.write_ledger([{
        "ts": datetime.now(timezone.utc).isoformat(),
        "lane": "audit",
        "day": day,
        "scheduled": report.scheduled,
        "captured": report.captured,
        "missed": len(report.misses),
        "duplicated": len(report.duplicates),
        "unmatched": len(report.unmatched_transcripts),
        "skipped": len(report.skipped),
        # The most serious finding this auditor can produce: a recording exists of
        # a meeting a carve-out excluded. Counted here so a breach is durable even
        # if the Slack post fails.
        "carve_out_breaches": len(report.carve_out_breaches),
        "failed_calendars": [e for e, _ in report.failed_calendars],
        "transcript_error": report.transcript_error,
        # Event ids only -- never titles. A LEX title must not reach an at-rest
        # log any more than it may reach the ops channel (D-082).
        "missed_event_ids": [m.event_id for m in report.misses],
        "duplicated_event_ids": [m.event_id for m in report.duplicates],
    }])

    if args.post:
        if not _post(text):
            # S4: a failed post is a run that produced NO output -- record it as
            # such rather than letting a non-zero exit be the only trace.
            run_marker.write("cowork-cora-meeting-capture-audit",
                             script="run_meeting_capture_audit.py", ok=False,
                             outputs=0, outcome="post_failed",
                             detail="Slack post returned falsy")
            return 1
    else:
        log.info("dry-run (no --post): nothing sent to Slack")
    # S4 run marker. `outputs` counts what this run actually PRODUCED: the ledger
    # row it appended, plus the Slack post when one was sent. A dry run writes the
    # ledger row but sends nothing, which is a legitimate 1.
    run_marker.write("cowork-cora-meeting-capture-audit",
                     script="run_meeting_capture_audit.py", ok=True,
                     outputs=1 + (1 if args.post else 0),
                     outcome="posted" if args.post else "dry_run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
