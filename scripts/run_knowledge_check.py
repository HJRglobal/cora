#!/usr/bin/env python3
"""Daily Personalized Knowledge Check -- the ASK half (weekday AM).

Sends ONE grounded, personalized question to each person in the locked pilot
roster (data/maps/knowledge-check-roster.yaml). The other three stages --
CAPTURE, CONFIRM-BACK and PROMOTE -- live in the always-on bot process, because
they are driven by the person's DM reply and their button tap.

    ASK   <- this script (scheduled)
    CAPTURE / CONFIRM / PROMOTE  <- src/cora/app.py (bot)

RUN ORDER MATTERS. This must run AFTER cowork-cora-gap-autofill (06:00 AZ):
Tier-2 questions are claimed out of gap_autofill's own state ledger, so letting
that job take its picks first keeps the two flows from asking the same gap of
two different people, and keeps the two writers off the ledger at the same time.

IDEMPOTENT BY CONSTRUCTION. A per-(person, AZ date) ledger is checked before
every send and a RESERVATION is appended before the Slack call. Re-running this
script -- by hand, by a double-fired task, or after a crash -- cannot produce a
second DM to the same person on the same day.

Scheduled as: Cora - Knowledge Check   Mon-Fri 08:05 AZ
Register with: deployment\\setup-knowledge-check-task.ps1 (elevated PowerShell)

Usage:
    .venv\\Scripts\\python.exe scripts\\run_knowledge_check.py [--dry-run]
        [--user U0B...] [--dogfood] [--check-reachability] [--no-stagger]
        [--max-sends N] [--report]

Gate: CORA_KNOWLEDGE_CHECK=off|dry|on (default off -- nothing is sent).
      --dry-run forces dry behaviour regardless of the flag.

Exit codes: 0 = clean, 1 = fatal error
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)

sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import knowledge_check as kc  # noqa: E402

LOG_DIR = _REPO_ROOT / "logs"


def _setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"knowledge-check-{datetime.now().strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("run_knowledge_check")


def _slack_client():
    import os
    from slack_sdk import WebClient
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN not set")
    return WebClient(token=token)


def _check_reachability(log: logging.Logger, people: list[dict]) -> int:
    """Confirm every roster member has a working DM path BEFORE any live send.

    Opening a DM channel does NOT message anyone -- this is a read-only probe.
    Aaron's Asana invite has been pending since June, so his Slack reachability
    specifically is worth confirming rather than assuming (kickoff step 4.2).
    """
    client = _slack_client()
    bad = 0
    for p in people:
        ch = kc.open_dm(client, p["slack_id"])
        if ch:
            log.info("REACHABLE   %-22s %s -> %s", p["name"], p["slack_id"], ch)
        else:
            bad += 1
            log.error("UNREACHABLE %-22s %s", p["name"], p["slack_id"])
    log.info("reachability: %d/%d reachable", len(people) - bad, len(people))
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily personalized knowledge check (ASK)")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and log the plan; send nothing, write nothing")
    ap.add_argument("--user", default="",
                    help="restrict to one slack_id (applied AFTER roster scoping)")
    ap.add_argument("--dogfood", action="store_true",
                    help="target the dogfood entry (Harrison) INSTEAD of the roster")
    ap.add_argument("--check-reachability", action="store_true",
                    help="probe every DM path and exit (sends nothing)")
    ap.add_argument("--no-stagger", action="store_true", help="send back-to-back")
    ap.add_argument("--max-sends", type=int, default=0, help="cap sends (0 = no cap)")
    ap.add_argument("--report", action="store_true",
                    help="print the participation report and exit")
    args = ap.parse_args()

    log = _setup_logging()
    today = kc.az_date()
    mode = kc.mode()
    dry = args.dry_run or mode == "dry"

    log.info("knowledge check: date=%s mode=%s dry=%s", today, mode, dry)

    problems = kc.validate_roster()
    if problems:
        for p in problems:
            log.error("roster problem: %s", p)
        log.error("refusing to run on an invalid roster")
        return 1

    if args.report:
        for line in kc.participation_report():
            log.info("%s", line)
        return 0

    people = ([p for p in kc.load_roster() if p.get("dogfood_only")] if args.dogfood
              else kc.pilot_roster())
    if args.user:
        people = [p for p in people if p["slack_id"] == args.user]
        if not people:
            log.error("--user %s is not in the selected roster", args.user)
            return 1

    if args.check_reachability:
        return 0 if _check_reachability(log, people) == 0 else 1

    if mode == "off" and not args.dry_run:
        log.info("CORA_KNOWLEDGE_CHECK=off -- nothing to do (use --dry-run to preview)")
        return 0

    if not kc.is_weekday() and not args.dogfood:
        log.info("weekend -- no questions today")
        return 0

    # Close out anything still in flight from a previous day BEFORE selecting,
    # so a stale cycle can never be mistaken for today's live one.
    state = kc.fold_state()
    if not dry:
        for row in kc.expire_stale_cycles(state, today=today):
            log.info("expired cycle=%s reason=%s", row.get("cycle_id"), row.get("reason"))
        state = kc.fold_state()

    try:
        from cora import gap_autofill as ga
        open_gaps = ga.load_open_gaps()
    except Exception:  # noqa: BLE001 -- Tier 1 must still work if the gap log is unreadable
        log.warning("could not load open gaps -- Tier 1 only this run", exc_info=True)
        open_gaps = []

    client = None if dry else _slack_client()
    claimed: set[str] = set()
    sent = skipped = failed = already = 0
    tier2_used = 0

    for idx, person in enumerate(people):
        sid, name = person["slack_id"], person["name"]

        if kc.handled_today(state, sid, today):
            already += 1
            log.info("SKIP(already) %-22s already handled today", name)
            continue

        picked = kc.select_question(
            person, state,
            open_gaps=open_gaps if tier2_used < kc.MAX_TIER2_PER_RUN else [],
            claimed=claimed, today=today)

        if picked is None:
            skipped += 1
            log.info("SKIP(no gap)  %-22s no Tier-1 item off cooldown, no Tier-2 gap",
                     name)
            if not dry:
                kc.append_event("skipped_no_gap", user=sid, date=today,
                                entity=person["entity"])
            continue

        log.info("ASK  T%-1d       %-22s %s", picked["tier"], name,
                 picked["question"][:90])
        if dry:
            sent += 1
            continue

        if args.max_sends and sent >= args.max_sends:
            log.info("max-sends reached -- stopping")
            break

        cycle_id = kc.new_cycle_id()

        # Tier-2 claim BEFORE the reservation: if the gap is already spoken for,
        # fall through rather than burning this person's slot on a duplicate.
        if picked["tier"] == 2:
            if not kc.claim_gap(picked["gap_ts"], cycle_id):
                log.info("SKIP(claimed) %-22s gap %s was claimed elsewhere",
                         name, picked["gap_ts"])
                continue
            claimed.add(picked["gap_ts"])
            tier2_used += 1

        # RESERVE, then send. A crash in between costs this person one day's
        # question -- deliberately preferred over any chance of a duplicate DM.
        kc.append_event("reserved", cycle_id=cycle_id, user=sid, date=today,
                        entity=person["entity"], tier=picked["tier"],
                        item_key=picked["item_key"], gap_ts=picked["gap_ts"] or None,
                        question=picked["question"])

        channel = kc.open_dm(client, sid)
        if not channel:
            failed += 1
            log.error("FAIL         %-22s no DM channel", name)
            kc.append_event("ask_failed", cycle_id=cycle_id, user=sid, date=today,
                            reason="no_dm_channel")
            continue
        try:
            resp = client.chat_postMessage(
                channel=channel,
                text=kc.ask_text(picked["question"], name),
                blocks=kc.build_ask_blocks(picked["question"], cycle_id, name),
                unfurl_links=False, unfurl_media=False,
            )
            kc.append_event("asked", cycle_id=cycle_id, user=sid, date=today,
                            channel=channel, message_ts=resp.get("ts", ""))
            sent += 1
        except Exception as exc:  # noqa: BLE001 -- one bad DM must not end the run
            failed += 1
            log.error("FAIL         %-22s send failed: %s", name, exc)
            kc.append_event("ask_failed", cycle_id=cycle_id, user=sid, date=today,
                            reason="send_error")
            continue

        if not args.no_stagger and idx < len(people) - 1:
            # Spread the roster across the window instead of one timestamp.
            # Jittered so the cadence does not read as a machine-gun either.
            per = max(1.0, (kc.STAGGER_MINUTES * 60.0) / max(1, len(people)))
            time.sleep(per * random.uniform(0.6, 1.4))

    log.info("done: sent=%d skipped_no_gap=%d already_handled=%d failed=%d "
             "(tier2=%d)", sent, skipped, already, failed, tier2_used)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        logging.getLogger("run_knowledge_check").error("fatal", exc_info=True)
        sys.exit(1)
