"""Daily QBO bank snapshot -- live per-account balances + books freshness (A5 S1).

Read-only. Sweeps every provisioned QBO realm for its ACTIVE Bank / Credit Card
account balances and the newest posted bank-side transaction date, writes
`data/state/qbo-bank-latest.json`, and mirrors that file one-way into the
Founder-OS accounting folder for Harrison/Justin/Hayden.

Scheduled daily 07:05 AZ as `cowork-cora-bank-snapshot`
(deployment/setup-qbo-bank-snapshot-task.ps1).

Realms listed under `excluded_realms` in the config are NEVER swept -- HR LLC is
Harrison's personal books and this file is mirrored into a folder Justin and
Hayden work in, so it is excluded at collection rather than merely un-rendered.

Usage:
    python scripts/run_qbo_bank_snapshot.py --dry-run
    python scripts/run_qbo_bank_snapshot.py
    python scripts/run_qbo_bank_snapshot.py --entities F3E,BDM --dry-run

`--dry-run` writes no snapshot and no mirror, but it is NOT side-effect-free: it
makes live read-only QBO calls, and authenticating a realm whose ACCESS token has
expired rotates that token through the normal OAuth refresh. That is true of any
QBO read, not something this flag adds.

A narrowed `--entities` run REFUSES to overwrite the daily snapshot (exit 2) --
the realms it never asked about would otherwise read as missing to every consumer.

Exit codes: 0 = every realm read cleanly; 1 = at least one realm errored (the
snapshot is still written, with those realms marked UNKNOWN); 2 = total failure
or a refused partial sweep, previous snapshot left in place.
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
    parser.add_argument(
        "--dry-run", action="store_true",
        help=("print the snapshot; write no snapshot file and no Drive mirror. "
              "NOTE: it still makes live read-only QBO calls, which can rotate an "
              "expired ACCESS token as a normal side effect of authenticating"))
    parser.add_argument("--entities", default="",
                        help="comma-separated realm codes (default: all provisioned)")
    parser.add_argument("--no-mirror", action="store_true",
                        help="write locally but skip the Drive mirror")
    args = parser.parse_args(argv)

    from cora.connectors.qbo_oauth import list_provisioned_entities
    from cora.tools import qbo_client as qc

    config = qbs.load_config()
    excluded = qbs.excluded_realms(config)

    try:
        # The full scope this snapshot is SUPPOSED to cover, minus never-swept
        # realms. HR LLC is personal books and this file is mirrored into a folder
        # Justin and Hayden work in -- excluded at the sweep, so it is never
        # collected at all rather than merely un-rendered downstream.
        full_scope = [e for e in list_provisioned_entities() if e not in excluded]
        if args.entities.strip():
            requested = [e.strip().upper() for e in args.entities.split(",") if e.strip()]
            entities = [e for e in requested if e not in excluded]
            refused = sorted(set(requested) & excluded)
            if refused:
                log.warning("refusing to sweep excluded realm(s): %s", ", ".join(refused))
        else:
            entities = list(full_scope)
    except Exception as exc:  # noqa: BLE001
        log.error("could not list provisioned entities: %s -- previous snapshot left in place", exc)
        return 2

    if not entities:
        log.error("no sweepable QBO entities -- previous snapshot left in place")
        return 2

    log.info("sweeping %d of %d realm(s): %s",
             len(entities), len(full_scope), ", ".join(entities))
    if excluded:
        log.info("never swept (config excluded_realms): %s", ", ".join(sorted(excluded)))

    try:
        snapshot = qbs.build_snapshot(
            entities,
            query_accounts=qc.query_accounts,
            summarize=qc.summarize_accounts,
            freshness=qc.newest_bank_side_txn_date,
            config=config,
            full_scope=full_scope,
        )
    except Exception as exc:  # noqa: BLE001 -- build_snapshot is per-realm fail-soft,
        # so reaching here means something structural broke. Never overwrite a good
        # snapshot with a broken one: consumers would read stale-as-current.
        log.error("snapshot build failed structurally: %s -- previous snapshot left in place", exc)
        return 2

    if args.dry_run:
        print(render_dry_run(snapshot))
        return 0

    # A narrowed --entities run must not overwrite the daily file: the realms it
    # never asked about would read as missing, and every consumer would see a
    # one-realm snapshot where a full one had been.
    if snapshot.get("partial_sweep"):
        log.error(
            "PARTIAL SWEEP (%d of %d realms) -- refusing to overwrite the daily "
            "snapshot. Re-run without --entities, or add --dry-run to preview.",
            snapshot["covered"], snapshot["expected"],
        )
        return 2

    path = qbs.write_snapshot(snapshot)
    log.info("wrote %s (%d/%d realms)", path, snapshot["covered"], snapshot["expected"])

    if not args.no_mirror:
        _mirror(snapshot)

    errored = sorted(snapshot.get("errors") or {})
    if errored:
        log.error("realm(s) errored: %s", ", ".join(errored))
        return 1
    return 0


def _mirror_payload(snapshot: dict) -> dict:
    """The snapshot MINUS per-account detail.

    The mirror lands in a folder Justin and Hayden work in. Its job is per-realm
    balances and freshness, which needs no account-level breakdown -- and the
    accounts[] array carries every realm's account NAMES, including LEX's. A
    shared accounting folder is not a LEX-custodian surface, which is the same
    premise _NAME_OPAQUE_REALMS encodes for Slack. Dropping the array removes the
    whole class rather than special-casing one realm; the LOCAL snapshot keeps
    full detail for the pack.
    """
    out = dict(snapshot)
    out["realms"] = {
        code: {k: v for k, v in block.items() if k != "accounts"}
        for code, block in (snapshot.get("realms") or {}).items()
    }
    out["_mirror_note"] = (
        "Per-account detail is intentionally omitted from this shared copy; "
        "totals and freshness only."
    )
    return out


def _mirror(snapshot: dict) -> None:
    """One-way Drive mirror. Fail-soft and change-gated: a Drive blip must never
    kill the local write, and an unchanged payload must not churn the mount."""
    target = qbs.mirror_path()
    payload = json.dumps(_mirror_payload(snapshot), indent=2, sort_keys=True)
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
