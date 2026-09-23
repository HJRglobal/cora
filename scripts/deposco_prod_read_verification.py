#!/usr/bin/env python3
"""F3E -- Deposco PROD read verification (V1-V4, SONNET-HANDOFF step 1).

Read-only, zero risk: every call here goes through the GET-only
`deposco_client.DeposcoClient`, which has no capacity to mutate anything (see
that module's write-impossibility invariant). This script exists to answer,
from LIVE data, the questions the order-push design left open rather than
guessed:

  V1  find_order("Sales Order", "081226") on prod -> proves the existence-check
      route the push preflight will use.
  V2  a line-level read-back route for 081226 -- does /status/order see Sales
      Order lines (item + qty), the same way it already does for Purchase
      Orders (get_purchase_order_receipts)?
  V3  the field-by-field shape of the three golden orders (081226, the two
      real FBA shipments) as prod actually keyed them: orderSource, shipVia,
      freight.termsType, otherReferenceNumber(2), unitPrice, ship-to, createdBy.
      Dumped GENERICALLY (every child element, not a hand-picked subset) --
      guessing field names here is exactly the D-182 mistake this step exists
      to avoid.
  V4  GET /items/F3E/{sku} for the 15 mapped SKUs, plus one deliberately-fake
      SKU as a negative control, to observe the real "item not found" shape on
      prod (only UA-proven before this).

Usage:
    python scripts/deposco_prod_read_verification.py

Prints a full report to stdout. The caller (a human, or the session running
this) copies the output into the dated `_notes/` record; this script does not
write to Drive itself (no Drive dependency, so it stays runnable standalone).
"""

from __future__ import annotations

import datetime
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass


def main() -> int:
    load_dotenv(_REPO_ROOT / ".env", override=True)
    from cora.connectors import deposco_client as dc  # noqa: E402

    out: list[str] = []

    def p(line: str = "") -> None:
        out.append(line)
        print(line)

    p("=" * 78)
    p("DEPOSCO PROD READ VERIFICATION -- V1-V4 (read-only, GET-only client)")
    p("generated: " + datetime.datetime.now(datetime.timezone.utc).isoformat())
    p("=" * 78)

    try:
        client = dc.DeposcoClient(env="prod")
    except dc.DeposcoAuthError as exc:
        p(f"FATAL: cannot build a prod client: {exc}")
        return 2

    # ── V1: existence-check route ───────────────────────────────────────────
    p("\n--- V1: find_order(\"Sales Order\", \"081226\") on PROD ---")
    try:
        resp = client.find_order("Sales Order", "081226")
        p(f"status={resp.status} content_type={resp.content_type} bytes={len(resp.text)}")
        p(f"'081226' in body: {'081226' in resp.text}")
        p("V1 RESULT: " + ("PASS -- route proven on prod" if resp.status == 200
                            and "081226" in resp.text else "FAIL -- see body below"))
        if not (resp.status == 200 and "081226" in resp.text):
            p(resp.text[:2000])
    except dc.DeposcoError as exc:
        p(f"V1 RESULT: FAIL -- {exc}")

    # ── V3 header shape: /search/Order for each golden order ───────────────
    targets = ["081226", "FBA19NG0MR50", "FBA19P6VCSJW"]
    p("\n--- V3a: header shape via /search/Order (OrderHeader schema) ---")
    for number in targets:
        p(f"\n  == {number} ==")
        try:
            resp = client.search_orders("Sales Order", number=number)
            p(f"  status={resp.status} bytes={len(resp.text)}")
            _dump_matching_orders(resp, number, p)
        except dc.DeposcoError as exc:
            p(f"  FAIL -- {exc}")

    # ── V2 + V3b: line-level + status-side shape via /status/order/{range} ─
    p("\n--- V2 / V3b: line-level + status shape via /status/order/{range} ---")
    start = "20260701"
    end = datetime.datetime.now().strftime("%Y%m%d")
    p(f"  window: {start},{end}")
    try:
        records = client.get_order_status_range(start, end)
        p(f"  {len(records)} order-status record(s) returned in the window")
        found_numbers = {r.order_number for r in records}
        p(f"  golden orders present: {[n for n in targets if n in found_numbers]}")
        p(f"  golden orders MISSING from this window: {[n for n in targets if n not in found_numbers]}")
        for record in records:
            if record.order_number not in targets:
                continue
            p(f"\n  == {record.order_number} ({record.order_type}, {record.order_status}) ==")
            p(f"  lines: {len(record.lines)}")
            for line in record.lines:
                p(f"    item={line.item_number!r} status={line.line_status!r} "
                  f"orderQty={line.order_pack_quantity} shippedQty={line.shipped_pack_quantity} "
                  f"receivedQty={line.received_pack_quantity}")
        if any(r.order_number in targets and r.lines for r in records):
            p("\n  V2 RESULT: PASS -- /status/order exposes Sales Order lines (item + qty) "
              "the same way it already does for Purchase Orders")
        else:
            p("\n  V2 RESULT: DEGRADED -- no line-level data found for the golden orders in "
              "this window; CONFIRMED read-back may have to degrade to header-only. FLAG FOR HARRISON.")
    except dc.DeposcoError as exc:
        p(f"  FAIL -- {exc}")

    # ── V4: item-existence route on prod ────────────────────────────────────
    p("\n--- V4: GET /items/F3E/{sku} for the 15 mapped SKUs + 1 negative control ---")
    import yaml  # noqa: E402
    sku_map_path = _REPO_ROOT / "data" / "maps" / "f3e-channel-sku-map.yaml"
    skus = sorted((yaml.safe_load(sku_map_path.read_text(encoding="utf-8")) or {}).get("skus", {}))
    p(f"  {len(skus)} SKU(s) loaded from {sku_map_path.name}")
    hits, misses = [], []
    for sku in skus:
        try:
            client.get_item(sku)
            hits.append(sku)
        except dc.DeposcoNotFound:
            misses.append((sku, "404 DeposcoNotFound"))
        except dc.DeposcoError as exc:
            misses.append((sku, str(exc)[:120]))
    p(f"  confirmed present: {len(hits)}/{len(skus)} -> {hits}")
    if misses:
        p(f"  NOT confirmed: {misses}")

    p("\n  -- negative control: a SKU that should not exist --")
    fake = "F3-DOES-NOT-EXIST-VERIFICATION-PROBE"
    try:
        client.get_item(fake)
        p(f"  UNEXPECTED: {fake} returned 200 -- it exists?!")
    except dc.DeposcoNotFound as exc:
        p(f"  observed shape for a miss: DeposcoNotFound -- {exc}")
    except dc.DeposcoError as exc:
        p(f"  observed shape for a miss: {exc.__class__.__name__} -- {exc}")

    p("\n" + "=" * 78)
    p("END OF REPORT")
    p("=" * 78)

    report_path = _REPO_ROOT / "logs" / "deposco-prod-read-verification-output.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(out), encoding="utf-8")
    print(f"\n(full report also written to {report_path})")
    return 0


def _dump_matching_orders(resp, number: str, p) -> None:
    """Generic dump: every child element of any <order> whose <number> matches,
    text-only, no field names assumed ahead of time."""
    try:
        root = resp.xml() if resp.looks_like_xml else None
    except Exception as exc:  # noqa: BLE001
        p(f"  could not parse as XML ({exc}); raw body follows:")
        p(resp.text[:3000])
        return
    if root is None:
        p("  response was not XML; raw body follows:")
        p(resp.text[:3000])
        return

    def strip_ns(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    matched = False
    for order_el in root.iter():
        if strip_ns(order_el.tag) != "order":
            continue
        num_el = next((c for c in order_el if strip_ns(c.tag) == "number"), None)
        if num_el is None or (num_el.text or "").strip() != number:
            continue
        matched = True
        for child in order_el:
            tag = strip_ns(child.tag)
            if len(child) == 0:
                p(f"    {tag} = {(child.text or '').strip()!r}")
            else:
                p(f"    {tag}: (nested, {len(child)} child element(s))")
                if tag == "orderLines":
                    for line_el in child:
                        parts = {strip_ns(c.tag): (c.text or "").strip() for c in line_el}
                        p(f"      line: {parts}")
                elif tag in ("shipToAddress", "billToAddress", "freight"):
                    parts = {strip_ns(c.tag): (c.text or "").strip() for c in child}
                    p(f"      {tag} fields: {parts}")
    if not matched:
        p(f"  no <order> element with number={number!r} found in this response")


if __name__ == "__main__":
    raise SystemExit(main())
