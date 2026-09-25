#!/usr/bin/env python
"""Dead-channel proposal card (Code #16 C1, ladder row slack-channel-archive, born T0).

A metadata-only scan of the channels Cora belongs to -> ONE proposal card in
Harrison's DM (continuation messages when it is long). NOTHING is ever archived by
this script: archiving happens only on Harrison's tap, in the bot, and only once the
lane has been promoted (registry `promoted` event + CORA_CHANNEL_ARCHIVE=act + one
restart).

Usage (from the repo root, main's tree checked out):
    # DRY RUN (DEFAULT) -- reads Slack, stages NOTHING, writes NO run marker; prints the
    # scan counts, the candidate channel IDS (ids only, never names), the section-B ids
    # with their reason, the blind cause if any, and the archive scopes' state
    .venv\\Scripts\\python.exe scripts\\run_channel_archive_proposal.py

    # deliver a card now (manual; no run marker)
    .venv\\Scripts\\python.exe scripts\\run_channel_archive_proposal.py --apply

    # THE SCHEDULED TASK (cowork-cora-channel-archive-proposal, weekly Monday 07:07 AZ):
    # delivers when today (AZ) is on/after the month's first Monday AND no monthly card
    # went out this calendar month (so a Tuesday StartWhenAvailable catch-up still
    # delivers), else skips. Writes the run marker (ok delivered / ok skipped /
    # FAILED month_undelivered + exit 1).
    .venv\\Scripts\\python.exe scripts\\run_channel_archive_proposal.py --apply --monthly

    # Harrison only: show / clear the automatic demotion (clearing appends an
    # `acknowledged` ledger row for EVERY archive event the demotion lists first)
    .venv\\Scripts\\python.exe scripts\\run_channel_archive_proposal.py --clear-demotion
    .venv\\Scripts\\python.exe scripts\\run_channel_archive_proposal.py --clear-demotion --apply

Script-side: a change here needs no bot restart. Its card's BUTTONS are handled by the
bot, so the task must be registered only AFTER the restart that loads the lane.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import run_marker  # noqa: E402
from cora.channel_archive import deliver  # noqa: E402
from cora.channel_archive import store as st  # noqa: E402

TASK_NAME = "cowork-cora-channel-archive-proposal"
SCRIPT = "scripts/run_channel_archive_proposal.py"
_AZ = timezone(timedelta(hours=-7))


def first_monday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(7 - d.weekday()) % 7)


def monthly_due(now: float, fold: st.Fold) -> tuple[bool, str]:
    """(due, why). Due on/after the month's first Monday (AZ) until a MONTHLY card has
    been delivered this calendar month."""
    today = datetime.fromtimestamp(now, _AZ).date()
    if today < first_monday(today.year, today.month):
        return False, "not_due_before_first_monday"
    for p in fold.proposals.values():
        if p.trigger != "monthly" or not p.delivered:
            continue
        made = datetime.fromtimestamp(p.created, _AZ).date()
        if (made.year, made.month) == (today.year, today.month):
            return False, "already_delivered_this_month"
    return True, "due"


def _print(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def _crashed(trigger: str, now: float, exc: BaseException) -> None:
    """The script's crash path: log the traceback, and settle the monitor's stall
    finding for the scan this run started (``scan_failed``; the class name only)."""
    import traceback  # noqa: PLC0415
    traceback.print_exc(file=sys.stderr)
    st.record_scan_failed(trigger=trigger, since=now, error=type(exc).__name__)


def _dry_run(now: float) -> int:
    out = deliver.deliver_proposal(trigger="dry_run", now=now, dry_run=True)
    if out.get("reason") in ("off", "eval_mode"):
        _print(f"Dead-channel dry run: lane {out['reason']} -- nothing scanned.")
        return 0
    c = out.get("counts") or {}
    _print("Dead-channel dry run (reads only; nothing staged, no run marker, nothing archived)")
    _print(f"  lane acting tier: {out.get('acting_tier')} | registry allows T1: {out.get('registry_t1')}")
    scopes = out.get("scopes") or {}
    _print("  archive scopes: " + ", ".join(f"{k}={v.upper() if v != 'present' else v}"
                                            for k, v in scopes.items()))
    missing = [k for k, v in scopes.items() if v != "present"]
    if missing:
        _print(f"  SCOPE NOT CONFIRMED: {', '.join(missing)} -- T1 cannot archive those channel "
               "types; Harrison adds the scope in the Slack app config and reinstalls. The T0 "
               "proposal card still works (read-only).")
    if out.get("blind"):
        _print(f"  BLIND: {out['blind']} {out.get('blind_detail') or ''} -- a live run would propose "
               "nothing and say why")
        return 0
    _print(f"  scanned {out.get('scanned', 0)} member channels | active {c.get('active', 0)} | "
           f"could not be read {c.get('unreadable', 0)} {c.get('unreadable_codes') or ''}")
    _print(f"  exempt (not listed): {c.get('exempt') or {}}")
    ids = out.get("candidate_ids") or []
    _print(f"  SECTION A candidates ({len(ids)}): {', '.join(ids) if ids else '-'}")
    b = out.get("b_reasons") or {}
    _print(f"  SECTION B per-row ({len(b)}): "
           + (", ".join(f"{cid}[{why}]" for cid, why in b.items()) if b else "-"))
    return 0


def main(argv: list[str] | None = None, *, now: float | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dead-channel proposal card (Code #16 C1).")
    ap.add_argument("--apply", action="store_true", help="stage + DM the card (default: dry run)")
    ap.add_argument("--monthly", action="store_true",
                    help="the scheduled task: deliver only when this month's card is due")
    ap.add_argument("--clear-demotion", action="store_true",
                    help="Harrison only: show (or with --apply, clear) the automatic demotion")
    args = ap.parse_args(argv)
    now = time.time() if now is None else float(now)

    if args.clear_demotion:
        out = st.clear_demotion(actor=deliver.HARRISON_ID, dry_run=not args.apply)
        if out.get("reason") == "not demoted" and not out.get("cleared"):
            _print("The dead-channel lane is not demoted -- nothing to clear.")
            return 0
        events = out.get("events") or []
        listing = "; ".join(f"{e['channel_id']} archive {e['archive_ts']}" for e in events) or "none listed"
        if not args.apply:
            _print(f"DEMOTED since {out.get('since')} -- {len(events)} unattributed archive event(s): "
                   f"{listing} ({out.get('reason')}). Re-run with --apply to clear it (an "
                   "`acknowledged` ledger row is written for EVERY listed archive event first).")
            return 0
        if out.get("cleared"):
            _print(f"Demotion CLEARED -- acknowledged {len(events)} archive event(s): {listing}.")
            return 0
        _print(f"NOT cleared: {out.get('reason')}")
        return 1

    if not args.apply:
        return _dry_run(now)

    t0 = time.time()
    if args.monthly:
        try:
            due, why = monthly_due(now, st.fold(now=now))
            if not due:
                run_marker.write(TASK_NAME, script=SCRIPT, ok=True, outputs=0, outcome=f"skipped:{why}",
                                 detail=why, elapsed_s=round(time.time() - t0, 1))
                _print(f"Monthly dead-channel card not due ({why}).")
                return 0
            out = deliver.deliver_proposal(trigger="monthly", now=now)
        except Exception as exc:  # noqa: BLE001 -- a crash is an undelivered month (A28), said out loud
            _crashed("monthly", now, exc)
            run_marker.write(TASK_NAME, script=SCRIPT, ok=False, outputs=0, outcome="month_undelivered",
                             detail=f"crashed: {type(exc).__name__}", elapsed_s=round(time.time() - t0, 1))
            _print(f"FAILED: the monthly dead-channel scan crashed ({type(exc).__name__}) -- no card.")
            return 1
        if out.get("reason") == "off":
            run_marker.write(TASK_NAME, script=SCRIPT, ok=True, outputs=0, outcome="skipped:lane_off",
                             detail="CORA_CHANNEL_ARCHIVE=off", elapsed_s=round(time.time() - t0, 1))
            _print("Lane switched off (CORA_CHANNEL_ARCHIVE=off) -- no card.")
            return 0
        if out.get("delivered"):
            run_marker.write(TASK_NAME, script=SCRIPT, ok=True, outputs=int(out.get("pages") or 0),
                             outcome="delivered",
                             detail=f"proposal {out.get('proposal_id')}: {out.get('rows', 0)} rows, "
                                    f"{len(out.get('candidate_ids') or [])} in section A, blind="
                                    f"{out.get('blind') or 'no'}",
                             elapsed_s=round(time.time() - t0, 1))
            _print(f"Monthly dead-channel card delivered ({out.get('pages')} message(s)).")
            return 0
        run_marker.write(TASK_NAME, script=SCRIPT, ok=False, outputs=0, outcome="month_undelivered",
                         detail=f"this month's card is due and was not delivered: {out.get('reason')}",
                         elapsed_s=round(time.time() - t0, 1))
        _print(f"FAILED: the monthly card is due and was not delivered ({out.get('reason')}).")
        return 1

    try:
        out = deliver.deliver_proposal(trigger="manual", now=now)
    except Exception as exc:  # noqa: BLE001
        _crashed("manual", now, exc)
        _print(f"FAILED: the dead-channel scan crashed ({type(exc).__name__}) -- no card.")
        return 1
    _print(f"Manual dead-channel card: delivered={out.get('delivered')} reason={out.get('reason')} "
           f"pages={out.get('pages')}")
    return 0 if out.get("delivered") or out.get("reason") == "off" else 1


if __name__ == "__main__":
    sys.exit(main())
