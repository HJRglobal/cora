"""Daily F3E inventory-state sync -- the Shopify-side writer (A5 Part 2).

Read-only against Shopify. Writes exactly ONE file in the cross-channel store:

    02-F3-Energy/inventory-state/f3e-inventory-shopify.json

One writer, one file (D-102): the Cowork channel sweep and the manual-count
transcription own their own files, so no two processes ever write the same one.

Covers the four locations that carry F3E beverage stock, each labelled with what
it actually is -- the UNIS number is fed by a WEEKLY upstream batch, and the
TikTok FBT location is a marketplace-managed MIRROR that can drift from Seller
Center. Those caveats ride the data so no consumer can present them as live.

Scheduled daily 07:20 AZ as `cowork-cora-inventory-state-sync`
(deployment/setup-inventory-state-sync-task.ps1).

Usage:
    python scripts/run_inventory_state_sync.py --dry-run
    python scripts/run_inventory_state_sync.py

Exit codes: 0 = every location read; 1 = at least one location failed (the file
is still written, those channels marked UNKNOWN); 2 = total failure, previous
file left in place.
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
            / f"inventory-state-sync-{datetime.datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("inventory-state-sync")

from cora import drive_io, inventory_state as inv  # noqa: E402


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def build_payload(known_skus: list[str], fetch) -> dict:
    """Assemble the Shopify-side store file.

    `fetch(location_name) -> list of objects with .sku / .available`, injected so
    the assembly is testable without Shopify.
    """
    channels: dict[str, dict] = {}
    covered = 0

    for channel in inv.SHOPIFY_CHANNELS:
        location_id = inv.SHOPIFY_LOCATIONS[channel]
        block: dict = {
            "location_id": location_id,
            "as_of_utc": _utc_now(),
            "caveat": inv.CHANNEL_CAVEATS.get(channel, ""),
        }
        try:
            rows = fetch(channel)
        except Exception as exc:  # noqa: BLE001 -- one dead location must not blank the rest
            log.error("location %s failed: %s", channel, exc)
            block.update({"status": "error", "error": f"{type(exc).__name__}"[:80], "skus": {}})
            channels[channel] = block
            continue

        # Filter to KNOWN beverage SKUs via the canonical map. Apparel and merch
        # carry no SKU at all on this store, so this is also the apparel exclusion.
        skus = {
            row.sku: row.available
            for row in rows
            if getattr(row, "sku", None) and row.sku in known_skus
        }
        block.update({"status": "ok", "skus": skus})
        channels[channel] = block
        covered += 1
        log.info("location %s: %d known SKU(s)", channel, len(skus))

    return {
        "source": "shopify",
        "as_of_utc": _utc_now(),
        "channels": channels,
        "covered": covered,
        "expected": len(inv.SHOPIFY_CHANNELS),
    }


def render_dry_run(payload: dict, sku_map: dict) -> str:
    out = ["F3E INVENTORY STATE -- Shopify-side sync (dry run -- nothing written)"]
    out.append(f"  generated: {payload['as_of_utc']}")
    out.append(f"  coverage : {payload['covered']} of {payload['expected']} location(s)")
    out.append("")
    for channel, block in payload["channels"].items():
        label = inv.channel_label(sku_map, channel)
        caveat = f" [{block['caveat']}]" if block.get("caveat") else ""
        if block.get("status") != "ok":
            out.append(f"  {label}{caveat}: UNAVAILABLE -- {block.get('error')}")
            continue
        out.append(f"  {label}{caveat}: {len(block['skus'])} SKU(s)")
        for sku, units in sorted(block["skus"].items()):
            out.append(f"     {sku:16s} {units:>8,}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print; write nothing")
    args = parser.parse_args(argv)

    sku_map = inv.load_sku_map()
    known = list((sku_map.get("skus") or {}).keys())
    if not known:
        log.error("SKU map is empty or unreadable -- previous file left in place")
        return 2

    from cora.connectors import shopify_client as sc

    def fetch(channel: str):
        # Resolve by the pinned location ID via the name the connector indexes on.
        names = {"office": "1337 S Gilbert Rd", "dtc_3pl": "Nimbl",
                 "unis": "UNIS", "tiktok_fbt": "TikTok FBT Warehouse"}
        return sc.get_inventory_by_location(names[channel])

    try:
        payload = build_payload(known, fetch)
    except Exception as exc:  # noqa: BLE001
        log.error("assembly failed structurally: %s -- previous file left in place", exc)
        return 2

    if args.dry_run:
        print(render_dry_run(payload, sku_map))
        return 0

    target = inv.store_path("shopify")
    try:
        drive_io.write_text_atomic(target, json.dumps(payload, indent=2, sort_keys=True))
        log.info("wrote %s", target)
    except drive_io.DriveUnavailable as exc:
        log.error("Drive mount unavailable (%s) -- previous file left in place", exc)
        return 2
    except OSError as exc:
        log.error("write failed (%s) -- previous file left in place", exc)
        return 2

    # A good parse observed here is what makes a later torn write survivable.
    inv.promote_last_good("shopify", payload)

    failed = [c for c, b in payload["channels"].items() if b.get("status") != "ok"]
    if failed:
        log.error("location(s) failed: %s", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
