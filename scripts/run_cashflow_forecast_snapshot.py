"""Weekly 13-week cashflow FORECAST snapshot (13WCF shadow ledger S1).

Read-only. Reads every CF tab of the Standing ACTUALS sheet, banks each tab's
full week grid (forecast / actual / diff, basis-labelled) to
`data/state/cashflow-ledger/forecast-snapshots/YYYY-MM-DD_forecast.json`, and
mirrors it one-way into the Founder-OS accounting tree.

Scheduled Mon 06:10 AZ as `cowork-cora-cashflow-forecast-snapshot`
(deployment/setup-cashflow-forecast-snapshot-task.ps1) -- BEFORE Justin's Monday
refresh, which overwrites the forecast column in place (D-121).

WHY THE TIMING MATTERS. This is the most loss-critical job in the estate: a
Monday that goes unsnapshotted is forecast history destroyed permanently, and no
later run recovers it. The nightly health check asserts the job fired. A run
that lands AFTER the refresh is not wasted -- it is stamped `post_refresh_suspect`
per tab and excluded from forecast-accuracy math rather than silently averaged
in as if it were a real forecast.

Nothing here writes to the sheet, ever (A5 lock). CF_HR LLC is excluded at
COLLECTION -- personal books, and the mirror lands in a folder Justin and Hayden
work in.

Usage:
    python scripts/run_cashflow_forecast_snapshot.py --dry-run
    python scripts/run_cashflow_forecast_snapshot.py
    python scripts/run_cashflow_forecast_snapshot.py --tabs CF_LLC,CF_UFL --dry-run

`--dry-run` writes no snapshot and no mirror, but still makes live read-only
Sheets API calls.

A narrowed `--tabs` run REFUSES to overwrite the dated snapshot (exit 2) -- the
tabs it never read would otherwise look unreadable to every consumer.

Exit codes: 0 = every tab read cleanly; 1 = at least one tab unreadable (the
snapshot is still written, those tabs listed in `unreadable_tabs`); 2 = total
failure or a refused partial sweep, previous snapshot left in place.
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

# D-119: Windows consoles default to cp1252. --dry-run is the ONLY pre-flight
# gate before this feeds a finance surface, so it must never be the thing that
# breaks.
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
            / f"cashflow-forecast-snapshot-{datetime.datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("cashflow-forecast-snapshot")

from cora import cashflow_ledger as cl  # noqa: E402
from cora import drive_io  # noqa: E402
from cora.connectors import gsheets_financials as gf  # noqa: E402


def _fmt(value: float | None) -> str:
    """ASCII money rendering; UNKNOWN is never 0 (D-117).

    Sign goes OUTSIDE the currency symbol. A value that rounds to zero renders
    as a clean "$0" -- CF_OSN Core4 holds a sub-dollar negative that otherwise
    prints "$-0", which reads as a formatting fault on a finance surface.
    """
    if value is None:
        return "UNKNOWN"
    if abs(value) < 0.5:
        return "$0"
    return f"-${abs(value):,.0f}" if value < 0 else f"${value:,.0f}"


def _week0_forecast(block: dict) -> float | None:
    """The first FORWARD week's forecast ending cash for a tab block."""
    forward = block.get("forward_week_endings") or []
    if not forward:
        return None
    points = (block.get("series") or {}).get("ending_cash") or []
    return next(
        (p.get("forecast") for p in points if p.get("week_ending") == forward[0]),
        None,
    )


def render_dry_run(snapshot: dict) -> str:
    """Plain-ASCII summary for Harrison's pre-flight review."""
    out: list[str] = []
    out.append("13-WEEK CASHFLOW FORECAST SNAPSHOT (dry run -- nothing written)")
    out.append(f"  generated    : {snapshot.get('generated_at_utc')}")
    out.append(f"  snapshot date: {snapshot.get('snapshot_date')}")
    out.append(f"  basis        : {snapshot.get('basis')}")
    out.append(f"  week ending  : {snapshot.get('week_ending_weekday')} (derived from the sheet)")
    out.append(f"  coverage     : {snapshot.get('covered')} of {snapshot.get('expected')} tabs")
    out.append(f"  prior snapshot: {snapshot.get('prior_snapshot_date') or 'none -- this is the first'}")
    out.append("")
    out.append(
        f"  {'tab':22s} {'last actual':12s} {'fwd':>4s} {'wk0 forecast':>15s}"
        f"  {'chk':>4s}  roll state"
    )
    for tab in sorted(snapshot.get("tabs") or {}):
        block = snapshot["tabs"][tab]
        roll = "PRE-REFRESH (usable for accuracy)"
        if block.get("post_refresh_suspect"):
            roll = "POST-REFRESH SUSPECT -- " + ",".join(
                s for s in block.get("roll_signals") or [] if s != "no_prior_snapshot"
            )
        out.append(
            f"  {tab:22s} {block.get('last_actual_week_ending') or 'UNKNOWN':12s} "
            f"{block.get('forward_weeks', 0):>4d} {_fmt(_week0_forecast(block)):>15s}"
            f"  {block.get('triplet_checked', 0):>4d}  {roll}"
        )

    unreadable = snapshot.get("unreadable_tabs") or {}
    if unreadable:
        out.append("")
        out.append(f"  UNREADABLE ({len(unreadable)}) -- not counted as covered:")
        for tab in sorted(unreadable):
            out.append(f"    {tab}: {unreadable[tab]}")

    suspect = [t for t, b in (snapshot.get("tabs") or {}).items()
               if b.get("post_refresh_suspect")]
    out.append("")
    if suspect:
        out.append(
            f"  NOTE: {len(suspect)} tab(s) were read AFTER the weekly refresh. Their "
            "forecast\n  columns for closed weeks hold ENTERED ACTUALS, not forecasts "
            "(D-121), so they\n  are excluded from forecast-accuracy math. The snapshot "
            "is still worth banking."
        )
    else:
        out.append("  All tabs read PRE-REFRESH -- forecast columns are genuine forecasts.")
    out.append("")
    out.append("  Completed weeks are stored under basis=post_close_column_value and must")
    out.append("  NEVER be read as forecasts. UNKNOWN is never zero.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help=("print the snapshot; write no file and no Drive mirror. Still makes "
              "live read-only Sheets API calls."))
    parser.add_argument("--tabs", default="",
                        help="comma-separated CF tab names (default: all)")
    parser.add_argument("--no-mirror", action="store_true",
                        help="write locally but skip the Drive mirror")
    parser.add_argument("--date", default="",
                        help="override the snapshot date (YYYY-MM-DD); testing only")
    parser.add_argument("--overwrite", action="store_true",
                        help=("replace an existing snapshot for this date. Refused by "
                              "default: a re-run later in the day would overwrite a "
                              "pre-refresh capture with a post-refresh one and destroy "
                              "the week's real forecast."))
    args = parser.parse_args(argv)

    try:
        today = (
            datetime.date.fromisoformat(args.date.strip())
            if args.date.strip() else datetime.date.today()
        )
    except ValueError:
        log.error("--date must be YYYY-MM-DD")
        return 2

    full_scope = cl.sweepable_tabs()
    if args.tabs.strip():
        requested = [t.strip() for t in args.tabs.split(",") if t.strip()]
        # Allowlist, case-insensitively. An exact-string exclusion is not enough:
        # Sheets resolves tab names case-insensitively, so "--tabs 'cf_hr llc'"
        # slipped past the EXCLUDED_TABS check and would have published
        # Harrison's personal books into the shared accounting folder. Accepting
        # only names that are already in scope closes the whole class, including
        # typos that would otherwise read as unreadable tabs.
        by_fold = {t.casefold(): t for t in full_scope}
        tabs, rejected = [], []
        for name in requested:
            canonical = by_fold.get(name.casefold())
            if canonical is None:
                rejected.append(name)
            elif canonical not in tabs:
                tabs.append(canonical)
        if rejected:
            log.error("not in scope, refusing: %s", ", ".join(sorted(rejected)))
            return 2
    else:
        tabs = list(full_scope)

    if not tabs:
        log.error("no readable tabs -- previous snapshot left in place")
        return 2

    log.info("reading %d of %d tab(s)", len(tabs), len(full_scope))
    log.info("never collected (excluded): %s", ", ".join(sorted(cl.EXCLUDED_TABS)))

    try:
        service = gf.build_sheets_service()
    except Exception as exc:  # noqa: BLE001
        log.error("could not authenticate to Sheets: %s -- previous snapshot left "
                  "in place", exc)
        return 2

    prior = cl.load_prior_snapshot(today)
    if prior:
        log.info("comparing against prior snapshot %s", prior.get("snapshot_date"))
    else:
        log.info("no prior snapshot -- roll state falls back to the calendar signal")

    try:
        snapshot = cl.build_snapshot(
            tabs,
            read_vector=lambda tab: gf.get_forecast_vector(
                tab, sheets_service=service, today=today
            ),
            today=today,
            prior=prior,
            full_scope=full_scope,
        )
    except cl.LedgerError as exc:
        # build_snapshot is per-tab fail-soft, so reaching here means something
        # structural broke. Never overwrite a good snapshot with a broken one.
        log.error("snapshot build failed structurally: %s -- previous snapshot left "
                  "in place", exc)
        return 2

    if args.dry_run:
        print(render_dry_run(snapshot))
        return 0

    if snapshot.get("partial_sweep"):
        log.error(
            "PARTIAL SWEEP (%d of %d tabs) -- refusing to overwrite the dated "
            "snapshot. Re-run without --tabs, or add --dry-run to preview.",
            snapshot["covered"], snapshot["expected"],
        )
        return 2

    try:
        path = cl.write_snapshot(snapshot, overwrite=args.overwrite, today=today)
    except cl.LedgerError as exc:
        log.error("%s", exc)
        return 2
    log.info("wrote %s (%d/%d tabs)", path, snapshot["covered"], snapshot["expected"])

    # A run that reads cleanly but lands AFTER the refresh banks entered actuals,
    # not forecasts. Exit code and health check both look fine, so say it here or
    # nobody ever learns the week's forecast was lost.
    suspect = [t for t, b in (snapshot.get("tabs") or {}).items()
               if b.get("post_refresh_suspect")]
    if suspect:
        log.warning(
            "POST-REFRESH: %d of %d tab(s) were read after the weekly refresh; "
            "their closed-week forecast cells hold entered actuals and are "
            "excluded from accuracy math.", len(suspect), snapshot["covered"])

    if not args.no_mirror:
        _mirror(snapshot)

    unreadable = sorted(snapshot.get("unreadable_tabs") or {})
    if unreadable:
        log.error("tab(s) unreadable: %s", ", ".join(unreadable))
        return 1
    return 0


def _mirror(snapshot: dict) -> None:
    """One-way Drive mirror. Fail-soft and change-gated: a Drive blip must never
    kill the local write, and an unchanged payload must not churn the mount.

    The full payload is mirrored (unlike the bank snapshot, which drops account
    names). There is no equivalent leak class here -- these are entity cash
    figures for a folder whose audience already co-maintains the source sheet,
    LexCorp tab included (design section 4.1, accepted explicitly).
    """
    date_str = snapshot.get("snapshot_date")
    target = cl.mirror_path(datetime.date.fromisoformat(date_str))
    payload = json.dumps(snapshot, indent=2, sort_keys=True)
    try:
        existing = drive_io.read_text(target) if drive_io.exists(target) else None
        if existing is not None and cl.same_ignoring_stamps(existing, payload):
            log.info("Drive mirror unchanged -- skipping write")
            return
        drive_io.write_text_atomic(target, payload)
        log.info("mirrored to %s", target)
    except drive_io.DriveUnavailable as exc:
        log.warning("Drive mirror skipped (mount unavailable): %s", exc)
    except OSError as exc:
        log.warning("Drive mirror failed: %s", exc)


if __name__ == "__main__":
    raise SystemExit(main())
