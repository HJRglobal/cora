"""Daily QBO bank snapshot -- live per-account balances + books freshness (A5 S1).

Read-only. Sweeps every provisioned QBO realm for its ACTIVE Bank / Credit Card
account balances and the newest posted bank-side transaction date, writes
`data/state/qbo-bank-latest.json`, and mirrors that file one-way into the
Founder-OS accounting folder for Harrison/Justin/Hayden.

Scheduled daily 07:05 AZ as `cowork-cora-bank-snapshot`
(deployment/setup-qbo-bank-snapshot-task.ps1).

Usage:
    python scripts/run_qbo_bank_snapshot.py --dry-run
    python scripts/run_qbo_bank_snapshot.py
    python scripts/run_qbo_bank_snapshot.py --entities F3E,BDM --no-mirror

Exit codes: 0 = every realm read cleanly; 1 = at least one realm errored (the
snapshot is still written, with those realms marked UNKNOWN); 2 = total failure,
previous snapshot left in place.
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

# D-119: Windows consoles default to cp1252. --dry-run is the ONLY pre-flight gate
# before this feeds a finance surface, so it must never be the thing that breaks.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover -- non-reconfigurable stream
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            _REPO_ROOT / "logs"
            / f"qbo-bank-snapshot-{datetime.datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("qbo-bank-snapshot")

from cora import qbo_bank_snapshot as qbs  # noqa: E402
from cora import drive_io  # noqa: E402


def _fmt(value: float | None) -> str:
    """ASCII-preferred money rendering; UNKNOWN is never 0 (D-117).

    Sign goes OUTSIDE the currency symbol ("-$1,300.54", not "$-1,300.54") --
    negative card balances are the normal case on this surface (see CC_SIGN) and
    a misplaced minus is exactly the kind of thing a reader mis-scans.
    """
    if value is None:
        return "UNKNOWN"
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def render_dry_run(snapshot: dict) -> str:
    """Plain-ASCII summary for Harrison's pre-flight review."""
    out: list[str] = []
    out.append("QBO BANK SNAPSHOT (dry run -- nothing written)")
    out.append(f"  generated : {snapshot.get('generated_at_utc')}")
    out.append(f"  basis     : {snapshot.get('basis')}")
    out.append(f"  coverage  : {snapshot.get('covered')} of {snapshot.get('expected')} realms")
    out.append("")
    out.append(
        f"  {'realm':8s} {'bank':>15s} {'cards':>13s} {'net of cards':>15s}"
        f"  {'newest bank txn':>16s}  status"
    )
    for code in sorted(snapshot.get("realms") or {}):
        block = snapshot["realms"][code]
        age = qbs.txn_age_days(block.get("newest_bank_txn_date"))
        newest = block.get("newest_bank_txn_date") or "UNKNOWN"
        age_txt = "" if age is None else f" ({age}d)"
        status = block.get("status")
        if block.get("shell"):
            status = f"{status} [shell]"
        out.append(
            f"  {code:8s} {_fmt(block.get('bank_total')):>15s} "
            f"{_fmt(block.get('cc_total')):>13s} "
            f"{_fmt(block.get('cash_net_of_cards')):>15s}"
            f"  {newest + age_txt:>16s}  {status}"
        )
        if block.get("error"):
            out.append(f"           error: {block['error']}")

    out.append("")
    portfolio = snapshot.get("portfolio")
    if portfolio:
        out.append(
            f"  PORTFOLIO  bank {_fmt(portfolio['bank_total'])}"
            f" | cards {_fmt(portfolio['cc_total'])}"
            f" | net of cards {_fmt(portfolio['cash_net_of_cards'])}"
        )
        out.append(f"    summed over: {', '.join(portfolio['realms_included'])}")
        if portfolio.get("shell_realms_excluded"):
            out.append(
                f"    footnote: shell realm(s) excluded -- "
                f"{', '.join(portfolio['shell_realms_excluded'])} (cash-less holding shell)"
            )
    else:
        out.append(f"  PORTFOLIO  WITHHELD -- {snapshot.get('portfolio_withheld_reason')}")

    out.append("")
    out.append("  NOTE: these are ACCOUNT REGISTER balances (query API). They are a")
    out.append("  different measure from the BalanceSheet-report figures the close")
    out.append("  pack's cash section uses, and can differ materially -- verified")
    out.append("  2026-08-04, including opposite signs on the same account. A gap")
    out.append("  between the two is NOT a reconciliation break.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the snapshot; write nothing (local or Drive)")
    parser.add_argument("--entities", default="",
                        help="comma-separated realm codes (default: all provisioned)")
    parser.add_argument("--no-mirror", action="store_true",
                        help="write locally but skip the Drive mirror")
    args = parser.parse_args(argv)

    from cora.connectors.qbo_oauth import list_provisioned_entities
    from cora.tools import qbo_client as qc

    try:
        if args.entities.strip():
            entities = [e.strip().upper() for e in args.entities.split(",") if e.strip()]
        else:
            entities = list_provisioned_entities()
    except Exception as exc:  # noqa: BLE001
        log.error("could not list provisioned entities: %s -- previous snapshot left in place", exc)
        return 2

    if not entities:
        log.error("no provisioned QBO entities -- previous snapshot left in place")
        return 2

    log.info("sweeping %d realm(s): %s", len(entities), ", ".join(entities))

    try:
        snapshot = qbs.build_snapshot(
            entities,
            query_accounts=qc.query_accounts,
            summarize=qc.summarize_accounts,
            freshness=qc.newest_bank_side_txn_date,
        )
    except Exception as exc:  # noqa: BLE001 -- build_snapshot is per-realm fail-soft,
        # so reaching here means something structural broke. Never overwrite a good
        # snapshot with a broken one: consumers would read stale-as-current.
        log.error("snapshot build failed structurally: %s -- previous snapshot left in place", exc)
        return 2

    if args.dry_run:
        print(render_dry_run(snapshot))
        return 0

    path = qbs.write_snapshot(snapshot)
    log.info("wrote %s (%d/%d realms)", path, snapshot["covered"], snapshot["expected"])

    if not args.no_mirror:
        _mirror(snapshot)

    errored = sorted(snapshot.get("errors") or {})
    if errored:
        log.error("realm(s) errored: %s", ", ".join(errored))
        return 1
    return 0


def _mirror(snapshot: dict) -> None:
    """One-way Drive mirror. Fail-soft and change-gated: a Drive blip must never
    kill the local write, and an unchanged payload must not churn the mount."""
    target = qbs.mirror_path()
    payload = json.dumps(snapshot, indent=2, sort_keys=True)
    try:
        existing = drive_io.read_text(target) if drive_io.exists(target) else None
        if existing is not None and _same_ignoring_stamps(existing, payload):
            log.info("Drive mirror unchanged -- skipping write")
            return
        drive_io.write_text_atomic(target, payload)
        log.info("mirrored to %s", target)
    except drive_io.DriveUnavailable as exc:
        log.warning("Drive mirror skipped (mount unavailable): %s", exc)
    except OSError as exc:
        log.warning("Drive mirror failed: %s", exc)


def _same_ignoring_stamps(left: str, right: str) -> bool:
    """Compare two snapshots ignoring the timestamps that change every run.

    Without this the mirror rewrites daily even when no balance moved, which is
    pure churn on a network mount.
    """
    def _strip(text: str) -> str:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
        data.pop("generated_at_utc", None)
        for block in (data.get("realms") or {}).values():
            if isinstance(block, dict):
                block.pop("as_of_utc", None)
        return json.dumps(data, sort_keys=True)

    return _strip(left) == _strip(right)


if __name__ == "__main__":
    raise SystemExit(main())
