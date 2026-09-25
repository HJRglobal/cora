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
    # delivers only inside the month's first-Monday week (AZ; so a Tuesday
    # StartWhenAvailable catch-up still delivers, but a late-month registration waits
    # for the NEXT first Monday) and only until a non-blind, fully delivered monthly
    # card went out this calendar month; else skips. Writes the run marker (ok
    # delivered / ok skipped / FAILED month_undelivered | month_blind | month_partial
    # + exit 1).
    .venv\\Scripts\\python.exe scripts\\run_channel_archive_proposal.py --apply --monthly

    # Harrison only: show / clear the automatic demotion (clearing appends an
    # `acknowledged` ledger row for EVERY archive event the demotion lists first)
    .venv\\Scripts\\python.exe scripts\\run_channel_archive_proposal.py --clear-demotion
    .venv\\Scripts\\python.exe scripts\\run_channel_archive_proposal.py --clear-demotion --apply

    # Harrison only, after a LEGITIMATE registry trim of more than 10% (every scan then
    # reads BLIND "N ids < floor M"): show / record the registry's current id count as the
    # new last good count (a `registry_rebaselined` store event; no marker, no Slack call)
    .venv\\Scripts\\python.exe scripts\\run_channel_archive_proposal.py --rebaseline-registry
    .venv\\Scripts\\python.exe scripts\\run_channel_archive_proposal.py --rebaseline-registry --apply

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
from cora.channel_archive import cards, deliver  # noqa: E402
from cora.channel_archive import registry as reg  # noqa: E402
from cora.channel_archive import store as st  # noqa: E402

TASK_NAME = "cowork-cora-channel-archive-proposal"
SCRIPT = "scripts/run_channel_archive_proposal.py"
_AZ = timezone(timedelta(hours=-7))


def first_monday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(7 - d.weekday()) % 7)


def fully_delivered(p: st.Proposal) -> bool:
    """Every page the proposal's rows paginate to was posted (a message ts per page)."""
    n = len(cards.paginate(p.rows))
    return all((p.pages.get(i) or {}).get("message_ts") for i in range(1, n + 1))


def monthly_due(now: float, fold: st.Fold) -> tuple[bool, str]:
    """(due, why). Due ONLY inside the month's first-Monday week (AZ: first Monday <=
    today < first Monday + 7 days), and only until a NON-blind, FULLY delivered MONTHLY
    card went out this calendar month (D-051 r1 registry-ops#0 + #4).

    The week bound keeps the Tuesday StartWhenAvailable catch-up (A28) but stops a task
    registered late in a month from firing a surprise catch-up card the next Monday
    (which would supersede an open ask card, A14) -- it waits for the NEXT first Monday.
    A blind card proposes nothing and a partial one leaves rows unposted, so neither is
    the month's card: a re-run inside the week retries."""
    today = datetime.fromtimestamp(now, _AZ).date()
    fm = first_monday(today.year, today.month)
    if today < fm:
        return False, "not_due_before_first_monday"
    for p in fold.proposals.values():
        if p.trigger != "monthly" or p.blind or not fully_delivered(p):
            continue
        made = datetime.fromtimestamp(p.created, _AZ).date()
        if (made.year, made.month) == (today.year, today.month):
            return False, "already_delivered_this_month"
    if today >= fm + timedelta(days=7):
        return False, "not_due_after_first_monday_week"
    return True, "due"


def _print(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def _month_not_done(out: dict, now: float) -> tuple[str, str] | None:
    """A delivered monthly card that is NOT this month's card (D-051 r1 registry-ops#0):
    a BLIND one proposed nothing (-> month_blind), a PARTIAL one left rows unposted
    (-> month_partial). Both are ok=False + exit 1 so the run-marker check WARNs, and
    monthly_due retries them inside the first-Monday week. None = a full, sighted card."""
    pid = str(out.get("proposal_id") or "")
    p = st.fold(now=now).proposals.get(pid)
    if out.get("blind"):
        detail = (p.blind_detail if p is not None else "") or ""
        return "month_blind", (f"proposal {pid}: the scan was BLIND ({out.get('blind')}"
                               f"{': ' + detail if detail else ''}) -- the card proposed nothing; fix "
                               "the cause, then re-run --apply --monthly inside the first-Monday week "
                               "(or ask 'archive the dead channels' in the DM)")
    expected = len(cards.paginate(p.rows)) if p is not None else 0
    posted = int(out.get("pages") or 0)
    if str(out.get("reason") or "").startswith("post_failed") or out.get("partial") \
            or (p is not None and not fully_delivered(p)):
        return "month_partial", (f"proposal {pid}: {posted} of {expected or '?'} message(s) posted "
                                 f"({out.get('reason')}) -- the unposted rows were not proposed; re-run "
                                 "--apply --monthly inside the first-Monday week")
    return None


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
        shrink = reg.shrink_copy(out.get("blind_detail")) if out["blind"] == "registry_unreadable" else None
        if shrink is not None:
            _print(f"  {shrink[0]}. {shrink[1]}".rstrip())
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


def _rebaseline(now: float, *, apply: bool) -> int:
    """Harrison's escape hatch for a LEGITIMATE registry trim of more than 10% (D-051 r1
    registry-ops#1): a blind scan persists no count, so the A9 floor would otherwise
    never move. Accepts only a STRUCTURALLY complete registry (all nine sections, the
    coverage sentinel, >= the 100-id minimum); --apply appends a ``registry_rebaselined``
    store event the fold honours. No run marker; no Slack call."""
    f = st.fold(now=now)
    if not f.ok:
        _print("REFUSED: the proposals store could not be read -- nothing re-baselined.")
        return 1
    r = reg.load_registry(last_good_count=0)
    prev = f.last_registry_count
    if not r.ok:
        _print(f"REFUSED: the registry is not complete ({r.reason}) -- a re-baseline accepts only "
               "a registry with every entity section, the coverage line and at least "
               f"{reg.MIN_REGISTRY_IDS} ids.")
        return 1
    new_floor = reg.registry_floor(r.count)
    if not apply:
        _print(f"Registry re-baseline (dry run): the registry parses complete with {r.count} ids; the "
               f"stored last good count is {prev or 'none'} (floor {reg.registry_floor(prev)}). "
               f"Re-run with --apply to record {r.count} (the floor becomes {new_floor}).")
        return 0
    if not st.append_event(st.REBASELINE_EVENT, registry_count=r.count, previous=prev or None,
                           by=deliver.HARRISON_ID, ts=now):
        _print("NOT re-baselined: the proposals store write failed.")
        return 1
    _print(f"Registry RE-BASELINED: last good count {prev or 'none'} -> {r.count} (floor now {new_floor}). "
           f"The blind card stays the latest until a sighted scan is staged -- {reg.FRESH_SCAN_HINT}.")
    return 0


def main(argv: list[str] | None = None, *, now: float | None = None) -> int:
    ap = argparse.ArgumentParser(description="Dead-channel proposal card (Code #16 C1).")
    ap.add_argument("--apply", action="store_true", help="stage + DM the card (default: dry run)")
    ap.add_argument("--monthly", action="store_true",
                    help="the scheduled task: deliver only when this month's card is due")
    ap.add_argument("--clear-demotion", action="store_true",
                    help="Harrison only: show (or with --apply, clear) the automatic demotion")
    ap.add_argument("--rebaseline-registry", action="store_true",
                    help="Harrison only: show (or with --apply, record) the channel registry's "
                         "current id count as the new last good count, after a legitimate trim")
    args = ap.parse_args(argv)
    now = time.time() if now is None else float(now)

    if args.rebaseline_registry:
        return _rebaseline(now, apply=args.apply)

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
        failed = _month_not_done(out, now) if out.get("delivered") else None
        if failed is not None:
            outcome, detail = failed
            run_marker.write(TASK_NAME, script=SCRIPT, ok=False, outputs=int(out.get("pages") or 0),
                             outcome=outcome, detail=detail, elapsed_s=round(time.time() - t0, 1))
            _print(f"FAILED ({outcome}): {detail}")
            return 1
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
