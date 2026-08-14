"""F3E lot ledger sync -- receipts in, reconciled against warehouse on-hand.

READ-ONLY against Deposco (GET-only client). Single writer of exactly one file:

    data/state/deposco-lot-ledger.json

Design Fork 3. Read `src/cora/deposco_lot_ledger.py`'s header first -- it records
the build-time premise overturn: V1 exposes lot on RECEIPTS ONLY, so per-lot
depletion is not computable and this ledger does not pretend otherwise.

WHAT A RUN DOES
  1. rehydrate the stored receipt history (untrusted input -- a torn file
     degrades to a reseed, never a crash);
  2. pull every purchase-order receipt line the tenant will surface (there is no
     date filter available -- see RECEIPT_SCOPE);
  3. pull Enterprise Inventory on-hand per SKU -- the SECOND, independent
     computation the receipts are tied out against;
  4. merge by stable receipt key (re-pulling an overlapping window corrects rows,
     never double-counts them), rebuild, write atomically.

The tie-out never absorbs a discrepancy: a SKU holding more than every receipt we
can see is reported as `receipts-incomplete`, which means "seed further back",
not "the numbers balance".

Usage:
    python scripts/run_deposco_lot_ledger.py --dry-run
    python scripts/run_deposco_lot_ledger.py

Exit codes: 0 = clean; 1 = written with reconciliation flags; 2 = failure,
nothing written, previous ledger left in place with its own timestamp.
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
            / f"deposco-lot-ledger-{datetime.datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("deposco-lot-ledger")

from cora import deposco_lot_ledger as ledger, inventory_state as inv  # noqa: E402
from cora.connectors import deposco_client as dc  # noqa: E402

LEDGER_PATH = _REPO_ROOT / "data" / "state" / "deposco-lot-ledger.json"

#: There is no date knob. Purchase-order status takes no date filter, and the
#: documented date-ranged receiptLine search has no configured search fields on
#: tenant ESM -- so every run pulls everything the tenant will surface and merges
#: replace-on-key. That makes seeding and the daily run the same operation, and
#: removes the "did I seed far enough back?" question entirely.
RECEIPT_SCOPE = "all purchase-order receipts the tenant returns"


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def read_existing(path: Path) -> dict:
    """Load the stored ledger. Untrusted input (D-123): anything unreadable
    yields an empty history, which reseeds rather than crashing the run."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("lot ledger unreadable (%s) -- rebuilding history from the API", exc)
        return {}


def on_hand_by_sku(client: "dc.DeposcoClient", known: list[str]) -> dict[str, int | None]:
    """Per-SKU warehouse on-hand -- the independent second computation.

    A SKU the warehouse did not return maps to None (UNKNOWN), never 0: a missing
    figure must not become a derived-consumption number.
    """
    result = client.get_enterprise_availability(item_numbers=known)
    rows = result.by_item()
    return {sku: (rows[sku].measure("totalOnHandQty") if sku in rows else None) for sku in known}


def render_dry_run(payload: dict) -> str:
    out = [
        f"F3E LOT LEDGER ({payload['env'].upper()}) -- dry run, nothing written",
        f"  generated : {payload['as_of_utc']}",
        f"  receipts  : {payload['receipt_count']} line(s) on record",
        f"  lot OUT   : {payload['lot_attribution']['outbound']}",
        "",
        f"  {'SKU':18s} {'received':>10s} {'on-hand':>10s} {'consumed':>10s}  tie-out",
    ]
    for sku, block in sorted(payload["by_sku"].items()):
        def cell(value):
            return f"{value:>10,}" if isinstance(value, int) else f"{'UNKNOWN':>10s}"
        out.append(
            f"  {sku:18s} {cell(block['received_total'])} {cell(block['on_hand'])} "
            f"{cell(block['derived_consumed'])}  {block['tie_out']}"
        )
        for lot in block["lots"][:5]:
            expiry = lot["expiration"][:10] or "no expiry"
            out.append(f"      lot {lot['lot']:<16s} exp {expiry:<12s} received {lot['received']:,}")

    if payload["flags"]:
        out.append("\n  FLAGS (discrepancies are surfaced, never absorbed):")
        out.extend(f"    - {flag}" for flag in payload["flags"])
    else:
        out.append("\n  No reconciliation flags.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="prod", choices=sorted(dc.ENVIRONMENTS))
    parser.add_argument("--dry-run", action="store_true", help="print; write nothing")
    parser.add_argument("--project", action="store_true",
                        help="include the per-lot FEFO PROJECTION (an assumption, not "
                             "a measurement -- see the module header)")
    args = parser.parse_args(argv)

    sku_map = inv.load_sku_map()
    known = list((sku_map.get("skus") or {}).keys())
    if not known:
        log.error("SKU map is empty or unreadable -- previous ledger left in place")
        return 2

    try:
        client = dc.DeposcoClient(env=args.env)
    except dc.DeposcoAuthError as exc:
        log.error("cannot build a %s client: %s", args.env, exc)
        return 2

    stored = read_existing(LEDGER_PATH)
    history = ledger.load_receipts(stored)

    try:
        incoming = ledger.receipts_from_lines(client.get_purchase_order_receipts())
        on_hand = on_hand_by_sku(client, known)
    except dc.DeposcoError as exc:
        log.error("Deposco read failed (%s) -- previous ledger left in place", exc)
        return 2

    merged, added, replaced = ledger.merge_receipts(history, incoming)
    log.info(
        "receipts: %d stored + %d pulled (%s) -> %d total (%d new, %d corrected)",
        len(history), len(incoming), RECEIPT_SCOPE, len(merged), added, replaced,
    )

    payload = ledger.build_ledger(
        receipts=merged,
        on_hand_by_sku=on_hand,
        env=client.env,
        as_of_utc=_utc_now(),
        seeded_from=RECEIPT_SCOPE,
        include_projection=args.project,
    )

    if args.dry_run:
        print(render_dry_run(payload))
        return 0

    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(LEDGER_PATH)
        log.info("wrote %s", LEDGER_PATH)
    except OSError as exc:
        log.error("write failed (%s) -- previous ledger left in place", exc)
        return 2

    for flag in payload["flags"]:
        log.warning("reconciliation flag: %s", flag)
    return 1 if payload["flags"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
