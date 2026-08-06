"""Weekly 13-week cashflow QBO ACTUALS (13WCF shadow ledger S2).

Read-only. Pulls each provisioned QBO realm's BANK-CASH activity for two windows
and banks them under `data/state/cashflow-ledger/actuals/`, mirrored one-way into
the Founder-OS accounting tree.

    PRELIMINARY  the week that just ended. QBO is structurally INCOMPLETE for it
                 at this hour -- the bank feed has not downloaded Friday through
                 Sunday. Stamped `posted-through` per realm and NEVER used for
                 comparison or forecast-accuracy math.
    FINALIZED    a re-pull of the week before that, now matured in QBO. It
                 supersedes its own preliminary file. Comparison and accuracy
                 consumers bind here and nowhere else.

Scheduled Mon 06:25 AZ as `cowork-cora-cashflow-actuals`
(deployment/setup-cashflow-actuals-task.ps1) -- 10 minutes after the S1 forecast
snapshot, whose banked week grid tells this job what "week ending" means.

THE CASH PERIMETER: a cash event is a transaction touching a BANK account. A card
purchase is not one; the bank-to-card PAYMENT is. Card spend is therefore
excluded by construction, and the map loader refuses to point a credit-card
liability account at an expense category.

Nothing here writes to the sheet, ever (A5 lock). HR LLC (personal books) and the
cash-less OSN shell are excluded at COLLECTION by the map loader, not merely
un-rendered -- the mirror lands in a folder Justin and Hayden work in.

Usage:
    python scripts/run_cashflow_actuals.py --dry-run
    python scripts/run_cashflow_actuals.py
    python scripts/run_cashflow_actuals.py --window final --dry-run
    python scripts/run_cashflow_actuals.py --realms F3E,BDM --dry-run

`--dry-run` writes nothing, but is NOT side-effect-free: it makes live read-only
QBO calls, and authenticating a realm whose ACCESS token has expired rotates that
token through the normal OAuth refresh. True of any QBO read.

Exit codes: 0 = every realm read cleanly; 1 = at least one realm errored or a
window is not usable for comparison (the windows are still written, those realms
marked UNKNOWN); 2 = total failure or a refused write, previous files left in
place.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

# D-119: Windows consoles default to cp1252, and QBO memo text carries characters
# outside it. --dry-run is the only pre-flight gate before this feeds a finance
# surface, so it must never be the thing that breaks.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            _REPO_ROOT / "logs"
            / f"cashflow-actuals-{datetime.datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("cashflow-actuals")

from cora import cashflow_actuals as ca  # noqa: E402
from cora import cashflow_ledger as cl  # noqa: E402
from cora import cashflow_maps as cm  # noqa: E402
from cora import drive_io  # noqa: E402
from cora import qbo_bank_snapshot as qbs  # noqa: E402


def _fmt(value: float | None) -> str:
    """ASCII money; UNKNOWN is never 0 (D-117). Sign outside the symbol."""
    if value is None:
        return "UNKNOWN"
    if abs(value) < 0.5:
        return "$0"
    return f"-${abs(value):,.0f}" if value < 0 else f"${value:,.0f}"


def render_dry_run(payload: dict) -> str:
    """Plain-ASCII summary for Harrison's pre-flight review."""
    out: list[str] = []
    kind = str(payload.get("window_kind", "")).upper()
    out.append(f"13WCF QBO ACTUALS -- {kind} (dry run -- nothing written)")
    out.append(f"  week          : {payload.get('week_start')} .. "
               f"{payload.get('week_ending')} "
               f"(ends {payload.get('week_ending_weekday')})")
    out.append(f"  week grid from: {payload.get('week_source')}")
    out.append(f"  basis         : {payload.get('basis')}")
    out.append(f"  coverage      : {payload.get('covered')} of "
               f"{payload.get('expected')} realms")
    out.append(f"  entity map    : {payload.get('map_confirmed_pairs')} confirmed pair(s)")
    if payload.get("supersedes"):
        out.append(f"  supersedes    : {payload['supersedes']}")
    out.append("")
    out.append(f"  {'realm':7s} {'net flow':>13s} {'receipts':>13s} {'disburse':>13s}"
               f" {'closing':>13s} {'posted':>11s} {'tie':>5s}  status")

    for realm in sorted(payload.get("realms") or {}):
        block = payload["realms"][realm]
        tie = (block.get("tie_out") or {}).get("status", "-")
        status = str(block.get("status"))
        if block.get("status") == "ok" and not block.get("map_confirmed"):
            status += " UNCONFIRMED"
        if block.get("reason_code"):
            status += f" [{block['reason_code']}]"
        out.append(
            f"  {realm:7s} {_fmt(block.get('net_flow')):>13s} "
            f"{_fmt(block.get('receipts')):>13s} "
            f"{_fmt(block.get('disbursements')):>13s} "
            f"{_fmt(block.get('closing_bank_balance')):>13s} "
            f"{str(block.get('posted_through') or '-'):>11s} "
            f"{tie[:5]:>5s}  {status}")
        if block.get("internal_transfers_excluded"):
            out.append(f"          internal bank-to-bank moves excluded from "
                       f"receipts/disbursements: "
                       f"{_fmt(block['internal_transfers_excluded'])}")
        unplaced = block.get("uncategorized") or {}
        if unplaced.get("rows"):
            out.append(f"          uncategorised: {_fmt(unplaced.get('amount'))} "
                       f"over {unplaced['rows']} row(s) -- {unplaced.get('reasons')}")
        if block.get("categories"):
            named = ", ".join(f"{k} {_fmt(v)}"
                              for k, v in sorted(block["categories"].items()))
            out.append(f"          categorised: {named}")
        chain = block.get("chain_check") or {}
        if chain.get("residual"):
            out.append(f"          chain vs {chain['prior_week_ending']}: "
                       f"{_fmt(chain['residual'])} booked into a finalised week "
                       "(advisory)")
        tie_block = block.get("tie_out") or {}
        if tie_block.get("status") == "failed":
            out.append(f"          TIE-OUT FAILED residual "
                       f"{_fmt(tie_block.get('residual'))} -- banked but NOT "
                       "usable for comparison")
        if tie_block.get("unexpected_keys"):
            out.append(f"          UNEXPECTED QUERY KEY(S): "
                       f"{tie_block['unexpected_keys']} -- a QBO response key "
                       "changed; treat this realm as UNKNOWN")
        if tie_block.get("capped_types"):
            out.append(f"          PAGE CAP HIT on {tie_block['capped_types']} -- "
                       "the window may be incomplete")

    refused = sorted(r for r, b in (payload.get("realms") or {}).items()
                     if b.get("status") == "refused")
    if refused:
        out.append("")
        out.append(f"  REFUSED ({len(refused)}) -- not collected, not counted as covered:")
        for realm in refused:
            block = payload["realms"][realm]
            detail = ""
            if block.get("candidate_tabs"):
                detail = (f" -- could be any of {', '.join(block['candidate_tabs'])}; "
                          "needs scope_attested or filters")
            out.append(f"    {realm}: {block.get('reason_code')}{detail}")

    out.append("")
    if payload.get("manual_entry_tabs"):
        out.append("  manual-entry tabs (no QBO source, not a gap): "
                   + ", ".join(payload["manual_entry_tabs"]))
    if payload.get("derived_tabs"):
        out.append("  derived roll-ups (nothing to map): "
                   + ", ".join(payload["derived_tabs"]))
    out.append("  never collected (excluded realms): "
               + ", ".join(payload.get("excluded_realms") or []))
    out.append("")
    for note in payload.get("notes") or []:
        out.append(f"  NOTE: {note}")
    return "\n".join(out)


def _resolve_realms(args_realms: str, entity_map: cm.EntityMap) -> tuple[list[str], list[str]]:
    """(realms to sweep, full scope). Raises SystemExit(2) on an out-of-scope name."""
    from cora.connectors.qbo_oauth import list_provisioned_entities  # noqa: PLC0415

    full_scope = [r for r in sorted(list_provisioned_entities())
                  if not entity_map.is_excluded(r)]
    if not args_realms.strip():
        return list(full_scope), full_scope

    # Case-insensitive ALLOWLIST against sweepable scope, never a denylist against
    # a name someone else normalises (D-127f -- an exact-string exclusion of the
    # personal-books tab was defeated by different casing in M1).
    by_fold = {r.casefold(): r for r in full_scope}
    requested = [r.strip() for r in args_realms.split(",") if r.strip()]
    realms: list[str] = []
    rejected: list[str] = []
    for name in requested:
        canonical = by_fold.get(name.casefold())
        if canonical is None:
            rejected.append(name)
        elif canonical not in realms:
            realms.append(canonical)
    if rejected:
        log.error("not in sweepable scope, refusing: %s", ", ".join(sorted(rejected)))
        raise SystemExit(2)
    return realms, full_scope


def _mirror(payload: dict) -> None:
    """One-way Drive mirror. Fail-soft and change-gated: a Drive blip must never
    kill the local write, and an unchanged payload must not churn the mount.

    The full payload is mirrored. There is no name-leak class left to strip: the
    General Ledger parse never captures memo/name/description, and account
    identifiers in the payload are numeric ids, not names (D-124).
    """
    week_ending = datetime.date.fromisoformat(payload["week_ending"])
    target = ca.mirror_actuals_path(week_ending, payload["window_kind"])
    body = json.dumps(payload, indent=2, sort_keys=True)
    try:
        existing = drive_io.read_text(target) if drive_io.exists(target) else None
        if existing is not None and ca.same_ignoring_stamps(existing, body):
            log.info("Drive mirror unchanged -- skipping write")
            return
        drive_io.write_text_atomic(target, body)
        log.info("mirrored to %s", target)
    except drive_io.DriveUnavailable as exc:
        log.warning("Drive mirror skipped (mount unavailable): %s", exc)
    except OSError as exc:
        log.warning("Drive mirror failed: %s", exc)


def _build_one(
    *,
    window_kind: str,
    week_ending: datetime.date,
    weekday_name: str,
    realms: list[str],
    full_scope: list[str],
    entity_map: cm.EntityMap,
    category_map: cm.CategoryMap,
    today: datetime.date,
    week_source: str,
) -> dict | None:
    """Build + annotate one window, or None when it broke structurally."""
    from cora.tools import qbo_client as qc  # noqa: PLC0415

    try:
        payload = ca.build_window(
            realms,
            window_kind=window_kind,
            week_ending=week_ending,
            weekday_name=weekday_name,
            entity_map=entity_map,
            category_map=category_map,
            query_accounts=qc.query_accounts,
            ledger_rows=qc.general_ledger_bank_rows,
            recompute=qc.bank_side_flow,
            freshness=qc.newest_bank_side_txn_date,
            today=today,
            full_scope=full_scope,
            week_source=week_source,
        )
    except ca.ActualsError as exc:
        # build_window is per-realm fail-soft, so reaching here means something
        # structural broke. Never overwrite a good window with a broken one.
        log.error("%s window build failed: %s -- previous file left in place",
                  window_kind, exc)
        return None

    prior = None
    if window_kind == ca.WINDOW_FINALIZED:
        earlier = [w for w in ca.list_finalized_weeks() if w < week_ending]
        if earlier:
            prior = ca.load_finalized(earlier[-1])
    return ca.annotate_advisory(
        payload,
        register_snapshot=qbs.load_snapshot(),
        prior_finalized=prior,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help=("print the windows; write no file and no Drive mirror. Still makes "
              "live read-only QBO calls, which can rotate an expired access "
              "token as a normal side effect of authenticating"))
    parser.add_argument("--window", choices=("both", "prelim", "final"),
                        default="both",
                        help="which window(s) to pull (default: both)")
    parser.add_argument("--realms", default="",
                        help="comma-separated realm codes (default: all provisioned)")
    parser.add_argument("--no-mirror", action="store_true",
                        help="write locally but skip the Drive mirror")
    parser.add_argument("--date", default="",
                        help="override today (YYYY-MM-DD); testing only")
    parser.add_argument(
        "--overwrite", action="store_true",
        help=("allow a write that would otherwise be refused as destructive: "
              "replacing a matured FINALIZED week with a preliminary pull, or "
              "overwriting a full file with a narrowed --realms sweep"))
    args = parser.parse_args(argv)

    try:
        today = (datetime.date.fromisoformat(args.date.strip())
                 if args.date.strip() else datetime.date.today())
    except ValueError:
        log.error("--date must be YYYY-MM-DD")
        return 2

    try:
        entity_map = cm.load_entity_map()
        category_map = cm.load_category_map()
    except cm.MapError as exc:
        # A bad map must fail LOUDLY at load rather than quietly producing a wrong
        # attribution -- that is the whole reason the loader validates.
        log.error("map unusable: %s", exc)
        return 2

    try:
        weekday_name, w1, w2 = ca.resolve_windows(today)
    except ca.ActualsError as exc:
        log.error("%s", exc)
        return 2

    snapshot_dates = cl.list_snapshot_dates()
    week_source = (f"forecast snapshot {snapshot_dates[-1].isoformat()}"
                   if snapshot_dates else "unknown")
    log.info("week ending %s (from %s); PRELIMINARY W-1=%s, FINALIZED W-2=%s",
             weekday_name, week_source, w1, w2)

    try:
        realms, full_scope = _resolve_realms(args.realms, entity_map)
    except SystemExit:
        return 2
    if not realms:
        log.error("no sweepable QBO realms -- previous files left in place")
        return 2
    log.info("sweeping %d of %d realm(s): %s", len(realms), len(full_scope),
             ", ".join(realms))
    log.info("never collected (excluded_realms): %s",
             ", ".join(sorted(entity_map.excluded_realms)))

    wanted: list[tuple[str, datetime.date]] = []
    if args.window in ("both", "prelim"):
        wanted.append((ca.WINDOW_PRELIMINARY, w1))
    if args.window in ("both", "final"):
        wanted.append((ca.WINDOW_FINALIZED, w2))

    built = 0
    degraded = False
    for window_kind, week_ending in wanted:
        payload = _build_one(
            window_kind=window_kind, week_ending=week_ending,
            weekday_name=weekday_name, realms=realms, full_scope=full_scope,
            entity_map=entity_map, category_map=category_map, today=today,
            week_source=week_source,
        )
        if payload is None:
            degraded = True
            continue

        if args.dry_run:
            print(render_dry_run(payload))
            print()
            built += 1
            continue

        try:
            path = ca.write_window(payload, overwrite=args.overwrite, today=today)
        except ca.ActualsError as exc:
            log.error("%s", exc)
            degraded = True
            continue

        built += 1
        log.info("wrote %s (%d/%d realms)", path, payload["covered"],
                 payload["expected"])

        if not args.no_mirror:
            _mirror(payload)

        problems = sorted(r for r, b in payload["realms"].items()
                          if b.get("status") in ("error", "unknown"))
        if problems:
            log.error("realm(s) UNKNOWN in %s window: %s", window_kind,
                      ", ".join(problems))
            degraded = True

        untied = sorted(r for r, b in payload["realms"].items()
                        if (b.get("tie_out") or {}).get("status") == "failed")
        if untied:
            # Loud on purpose: the figure is banked and looks fine, so nothing
            # else in the estate would tell anybody it disagrees with the ledger.
            log.error("TIE-OUT FAILED in %s window for: %s -- banked but excluded "
                      "from comparison", window_kind, ", ".join(untied))
            degraded = True

    if built == 0:
        return 2
    return 1 if degraded else 0


if __name__ == "__main__":
    raise SystemExit(main())
