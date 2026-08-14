"""Daily F3E warehouse (3PL) inventory sync -- the Deposco-side writer (Phase 1).

READ-ONLY. Uses the GET-only `deposco_client`, which has no capacity to mutate
anything in Deposco (see that module's write-impossibility invariant).

Writes exactly ONE file in the cross-channel store:

    02-F3-Energy/inventory-state/f3e-inventory-deposco.json

One writer, one file (D-102) -- it sits alongside the Shopify-side, marketplace
sweep and manual-count files without ever contending for the same path.

THE COVERAGE FLOOR IS THE POINT OF THIS SCRIPT (D-133 class). A warehouse feed
that silently returns nothing is indistinguishable, downstream, from a warehouse
that holds nothing -- and the second reading would be a fabricated stockout. So:

  * a run that cannot reach the API, or that gets a structurally empty payload
    from a working API, is a FAILURE. It writes nothing and leaves the previous
    file in place, stale `as_of_utc` and all (D-094: a failed render keeps its
    stale stamp rather than faking a fresh one);
  * a run that reads some known SKUs but not others is PARTIAL, and names the
    missing ones in the payload rather than dropping them;
  * items present in Deposco but absent from the SKU map are reported as
    `unmapped`, never discarded;
  * a measure Deposco did not return is ABSENT from the payload, never zero.

`env` is stamped into the payload, so a sandbox figure can never be read as a
production one -- UA carries no inventory at all, which makes that mislabel
exactly the sort of "we hold nothing" lie the floor above exists to prevent.

Usage:
    python scripts/run_deposco_inventory_sync.py --dry-run
    python scripts/run_deposco_inventory_sync.py --env ua --dry-run   # smoke
    python scripts/run_deposco_inventory_sync.py

Exit codes: 0 = every known SKU read; 1 = partial (file written, gaps named);
2 = failure, nothing written, previous file left in place.
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

# D-119: --dry-run is the pre-flight gate; a cp1252 console must not break it.
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
            / f"deposco-inventory-sync-{datetime.datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("deposco-inventory-sync")

from cora import drive_io, inventory_state as inv  # noqa: E402
from cora.connectors import deposco_client as dc  # noqa: E402

#: This writer's own file. Deliberately NOT registered in
#: `inventory_state.STORE_FILES`: that store models per-SALES-CHANNEL counts, and
#: this is a per-FACILITY warehouse view with a different shape. Registering it
#: would inflate the merge's `expected_channels` with a source that can never
#: satisfy it.
STORE_FILENAME = "f3e-inventory-deposco.json"

#: The receipt pull is NOT date-windowed: purchase-order status takes no date
#: filter, and the documented date-ranged receiptLine search has no configured
#: search fields on this tenant. Naming the real scope beats implying a window.
_RECEIPT_WINDOW = "all purchase-order receipts the tenant returns"
RECEIPT_LOOKBACK_DAYS = 45  # retained for the CLI flag's signature; not a filter


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def store_path() -> Path:
    return inv.store_dir() / STORE_FILENAME


def build_payload(client: "dc.DeposcoClient", known_skus: list[str],
                  receipt_lookback_days: int = RECEIPT_LOOKBACK_DAYS) -> dict:
    """Assemble the Deposco-side store file.

    Raises `dc.DeposcoError` on a total read failure -- the caller turns that
    into "write nothing, leave the previous file alone".
    """
    result = client.get_enterprise_availability()
    by_item = result.by_item()

    items: dict[str, dict] = {}
    for sku in known_skus:
        row = by_item.get(sku)
        if row is None:
            continue
        items[sku] = {
            # Absent measures stay absent -- the consumer renders UNKNOWN.
            "measures": {k: v for k, v in row.measures.items() if v is not None},
            "unparseable_measures": sorted(k for k, v in row.measures.items() if v is None),
            "facilities": [
                {
                    "facility": f.facility,
                    "measures": {k: v for k, v in f.measures.items() if v is not None},
                }
                for f in row.facilities
            ],
        }

    missing = [sku for sku in known_skus if sku not in items]
    unmapped = sorted(n for n in by_item if n and n not in set(known_skus))

    if missing:
        log.warning(
            "deposco sync: %d of %d known SKU(s) absent from the availability "
            "response: %s", len(missing), len(known_skus), ", ".join(missing),
        )
    if unmapped:
        log.info("deposco sync: %d item(s) present but not in the SKU map", len(unmapped))

    payload: dict = {
        "source": "deposco",
        "env": client.env,
        "tenant": client.tenant,
        "business_unit": client.business_unit,
        "as_of_utc": _utc_now(),
        "items": items,
        "coverage": {
            "known_skus": len(known_skus),
            "read": len(items),
            "missing": missing,
            "unmapped": unmapped,
            "rows_returned": len(result.rows),
        },
        "truncated": result.truncated,
        "receipts": _receipt_summary(client, receipt_lookback_days),
    }
    payload["status"] = _status_for(payload)
    return payload


def _receipt_summary(client: "dc.DeposcoClient", lookback_days: int) -> dict:
    """Receipt lines -- the lot + expiry surface. FAIL-SOFT: the receipt lane
    going dark must not blank the inventory figures, but it must say so rather
    than reporting zero receipts.

    Sourced from purchase-order status, not the documented receiptLine search --
    that route has no configured search fields on this tenant. See
    `deposco_client.get_purchase_order_receipts`.
    """
    try:
        lines = client.get_purchase_order_receipts()
    except dc.DeposcoError as exc:
        log.warning("deposco sync: receipt-line pull unavailable: %s", exc)
        return {"status": "unavailable", "window": _RECEIPT_WINDOW, "detail": str(exc)[:200]}
    with_lot = sum(1 for line in lines if line.has_lot)
    return {
        "status": "ok",
        # NOT a date window. The status route takes no date filter, so claiming
        # "since <date>" here would describe a filter that never ran.
        "window": _RECEIPT_WINDOW,
        "lines": len(lines),
        "with_lot": with_lot,
        # Lot coverage is REPORTED, not assumed: `lotNumber` is documented on
        # receipt lines but absent from the doc's own example, so the honest
        # thing is to say how many actually carried one.
        "lot_coverage": (f"{with_lot} of {len(lines)}" if lines else "no receipts returned"),
    }


def _status_for(payload: dict) -> str:
    """ok | partial | failed. `failed` is what stops the write."""
    coverage = payload["coverage"]
    if coverage["rows_returned"] == 0:
        # A working API that returns no items at all is SUSPICIOUS, never zero.
        return "failed"
    if coverage["read"] == 0:
        return "failed"
    if coverage["missing"] or payload.get("truncated"):
        return "partial"
    return "ok"


def render_dry_run(payload: dict) -> str:
    out = [
        f"F3E WAREHOUSE INVENTORY -- Deposco sync ({payload['env'].upper()}) "
        f"(dry run -- nothing written)",
        f"  generated : {payload['as_of_utc']}",
        f"  tenant/BU : {payload['tenant']} / {payload['business_unit']}",
        f"  status    : {payload['status'].upper()}",
    ]
    coverage = payload["coverage"]
    out.append(
        f"  coverage  : {coverage['read']} of {coverage['known_skus']} known SKU(s); "
        f"{coverage['rows_returned']} row(s) returned"
    )
    if payload.get("truncated"):
        out.append("  !! PAGE CAP HIT -- results are INCOMPLETE")
    if coverage["missing"]:
        out.append(f"  !! not returned: {', '.join(coverage['missing'])}")
    if coverage["unmapped"]:
        out.append(f"  unmapped  : {', '.join(coverage['unmapped'][:10])}")
    out.append("")

    measures = ("totalOnHandQty", "atpQty", "qtyOnPO", "inTransitQty")
    out.append(f"  {'SKU':18s} " + " ".join(f"{m:>15s}" for m in measures))
    for sku, block in sorted(payload["items"].items()):
        cells = []
        for measure in measures:
            value = block["measures"].get(measure)
            cells.append(f"{value:>15,}" if value is not None else f"{'UNKNOWN':>15s}")
        out.append(f"  {sku:18s} " + " ".join(cells))
        for facility in block["facilities"]:
            on_hand = facility["measures"].get("totalOnHandQty")
            shown = f"{on_hand:,}" if on_hand is not None else "UNKNOWN"
            out.append(f"      @ {facility['facility']}: on-hand {shown}")

    receipts = payload["receipts"]
    if receipts.get("status") == "ok":
        out.append(
            f"\n  receipts  : {receipts['lines']} line(s) ({receipts['window']}); "
            f"lot present on {receipts['lot_coverage']}"
        )
    else:
        out.append(f"\n  receipts  : UNAVAILABLE -- {receipts.get('detail', '')[:120]}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="prod", choices=sorted(dc.ENVIRONMENTS),
                        help="prod for real figures; ua is a reachability smoke only "
                             "(the sandbox carries no inventory)")
    parser.add_argument("--dry-run", action="store_true", help="print; write nothing")
    parser.add_argument("--receipt-days", type=int, default=RECEIPT_LOOKBACK_DAYS)
    args = parser.parse_args(argv)

    sku_map = inv.load_sku_map()
    known = list((sku_map.get("skus") or {}).keys())
    if not known:
        log.error("SKU map is empty or unreadable -- previous file left in place")
        return 2

    try:
        client = dc.DeposcoClient(env=args.env)
    except dc.DeposcoAuthError as exc:
        log.error("cannot build a %s client: %s", args.env, exc)
        return 2

    try:
        payload = build_payload(client, known, args.receipt_days)
    except dc.DeposcoError as exc:
        log.error("availability read failed (%s) -- previous file left in place", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        log.error("assembly failed structurally: %s -- previous file left in place",
                  exc.__class__.__name__)
        return 2

    if payload["status"] == "failed":
        # The coverage floor. Writing here would replace an honestly-stale file
        # with a fresh-stamped empty one -- the exact green-when-blind failure.
        log.error(
            "coverage floor: status=failed (%d row(s) returned, %d known SKU(s) read) "
            "-- writing NOTHING, previous file left in place with its own timestamp",
            payload["coverage"]["rows_returned"], payload["coverage"]["read"],
        )
        if args.dry_run:
            print(render_dry_run(payload))
        return 2

    if args.dry_run:
        print(render_dry_run(payload))
        return 0

    if args.env != "prod":
        # UA has no inventory; letting a sandbox payload land in the store would
        # put "0 units" in front of an operator. Reachability only.
        log.warning("env=%s is a smoke target -- refusing to write the store file", args.env)
        print(render_dry_run(payload))
        return 0

    target = store_path()
    try:
        drive_io.write_text_atomic(target, json.dumps(payload, indent=2, sort_keys=True))
        log.info("wrote %s", target)
    except drive_io.DriveUnavailable as exc:
        log.error("Drive mount unavailable (%s) -- previous file left in place", exc)
        return 2
    except OSError as exc:
        log.error("write failed (%s) -- previous file left in place", exc)
        return 2

    if payload["status"] == "partial":
        log.error("partial coverage: missing %s", ", ".join(payload["coverage"]["missing"]))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
