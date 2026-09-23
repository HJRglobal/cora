"""Pure payload builder: a validated `OrderSpec` -> the exact JSON envelope
`deposco_push.DeposcoPushClient.push_order` sends.

PURE means no I/O, no network, no clock (a caller passes `plannedShipDate`
explicit or not at all) -- every field is deterministic from the spec plus
the pinned address-book tables `spec.py` already loaded and validated
against. That determinism is what makes `build_order_number` idempotent: the
same spec always derives the same number, so a retry can only ever collide
with itself (the design's whole idempotency argument, SS3.2).

WHAT THIS FUNCTION CANNOT EMIT, BY CONSTRUCTION:
  * `type` other than `"Sales Order"` -- a MODULE CONSTANT, no parameter path
    exists to it. This is what makes the 081226 "keyed as a Purchase Order"
    defect unreachable through this builder.
  * a `channel` / `channels` block -- the proven flat-400 culprit (8/14
    finding); never constructed anywhere in this module.
  * line-level `notes` -- also a proven flat-400 culprit; notes are
    order-level only.
  * `lotNumber` / `expirationDate` on an order line -- not SO line fields
    (PO/receipt only, per the 8/6 design SS2b correction); FBA expiry rides
    the order-level notes text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from . import spec as spec_mod
from .spec import OrderSpec, OrderSpecLine

ORDER_TYPE = "Sales Order"  # MODULE CONSTANT -- no parameter, ever.

#: All F3 beverage 12-pack items share this pack weight (the UA-proven shape,
#: `deposco_ua_test_order.py`'s PACK_WEIGHT_LB); no per-SKU variance has ever
#: been observed or documented.
PACK_WEIGHT_LB = "9.85"

_NUMBER_RE = re.compile(r"^[A-Z0-9-]{1,30}$")

_SECONDARY_SOURCE = {"fba": "AMAZON", "wfs": "WALMART"}

_NOTE_TITLE = {
    "wholesale": "F3E Wholesale Order",
    "fba": "F3E Amazon FBA Replenishment",
    "wfs": "F3E Walmart WFS Replenishment",
}


class PayloadBuildError(Exception):
    """The spec is individually valid but the DERIVED payload would violate a
    hard Deposco constraint (the number's charset/length limit). Refuses
    rather than truncating or mangling."""


def build_order_number(order_spec: OrderSpec) -> str:
    """`F3E-W-{BUYER}-{PO}` / `F3E-AMZ-{ShipmentID}` / `F3E-WMT-{ShipmentID}`.

    Deterministic and idempotent: called twice on the same spec, always
    returns the same string. The uniqueness constraint plus the pre-flight
    `find_order` check (preflight.py) are what make a retry structurally
    unable to create a second order -- this function's only job is to never
    itself vary.
    """
    code = order_spec.buyer_or_fc_code.strip().upper()
    reference = order_spec.reference.strip().upper()
    if order_spec.channel == "wholesale":
        raw = f"F3E-W-{code}-{reference}"
    elif order_spec.channel == "fba":
        raw = f"F3E-AMZ-{reference}"
    elif order_spec.channel == "wfs":
        raw = f"F3E-WMT-{reference}"
    else:
        raise PayloadBuildError(f"unknown channel {order_spec.channel!r}")
    if not _NUMBER_RE.match(raw):
        raise PayloadBuildError(
            f"derived order number {raw!r} violates Deposco's <=30-char "
            f"[A-Z0-9-] limit -- refusing rather than truncating or mangling it"
        )
    return raw


def _ship_to_and_bill_to(order_spec: OrderSpec) -> tuple[dict, dict | None]:
    """`(ship_to, bill_to)`. `bill_to` is None when the channel provides none
    (FBA/WFS -- see the module docstring's V3 finding: the proven UA shape for
    wholesale reuses the buyer's own AP contact; no confirmed F3-side billing
    address exists in canon to invent one for a marketplace transfer)."""
    if order_spec.channel == "wholesale":
        customers = spec_mod.load_customers()
        entry = customers.get(order_spec.buyer_or_fc_code, {})
        return dict(entry.get("ship_to") or {}), dict(entry.get("bill_to") or {}) or None
    facilities = spec_mod.load_amazon_fcs()
    entry = facilities.get(order_spec.buyer_or_fc_code, {})
    return dict(entry.get("ship_to") or {}), None


def _notes_body(order_spec: OrderSpec) -> str:
    """Site-constraint text from the address book (wholesale only) folded
    ahead of the human-authored spec notes -- SS3.3: "notes template carries
    site constraints from the address book"."""
    parts: list[str] = []
    if order_spec.channel == "wholesale":
        customers = spec_mod.load_customers()
        entry = customers.get(order_spec.buyer_or_fc_code, {})
        site_notes = str(entry.get("site_notes") or "").strip()
        if site_notes:
            parts.append(site_notes)
    if order_spec.notes:
        parts.append(order_spec.notes)
    return "\n\n".join(parts)


def _order_line(line: OrderSpecLine, index: int, order_number: str) -> dict:
    return {
        "businessUnit": "F3E",
        "lineNumber": f"{order_number}--{index}",
        "customerLineNumber": f"{order_number}--{index}",
        "lineStatus": "New",
        "itemNumber": line.sku,
        "orderPackQuantity": str(float(line.qty)),
        "shortagePackQuantity": "0.0",
        "pack": {"type": "Each", "quantity": "1", "weight": PACK_WEIGHT_LB},
        "unitPrice": str(line.unit_price),
        "unitCost": "0.0",
    }


def build_payload(order_spec: OrderSpec) -> dict:
    """`{"order": [{...}]}` -- the exact envelope `deposco_push` POSTs."""
    number = build_order_number(order_spec)
    ship_to, bill_to = _ship_to_and_bill_to(order_spec)
    lines = [
        _order_line(line, i, number)
        for i, line in enumerate(order_spec.lines, start=1)
    ]
    # Decimal throughout, never float: a float-accumulated sum rounded once
    # at the end can diverge by a cent from what the individually-displayed
    # per-line unitPrice values (also in this payload) would sum to (D-051
    # review, 2026-09-23, reproduced ~4.9% of randomized multi-line trials
    # diverging). spec.py's validation already refuses a sub-cent unit_price,
    # so this sum is exact.
    subtotal = sum(
        (Decimal(str(line.qty)) * Decimal(line.unit_price) for line in order_spec.lines),
        Decimal("0"),
    )

    order: dict = {
        "businessUnit": "F3E",
        "number": number,
        "type": ORDER_TYPE,
        "status": "New",
        "orderPriority": "10",
        "orderSource": f"F3E-API-{order_spec.channel.upper()}",
        "secondaryOrderSource": _SECONDARY_SOURCE.get(
            order_spec.channel, order_spec.buyer_or_fc_code
        ),
        "otherReferenceNumber": order_spec.reference,
        "customerOrderNumber": order_spec.reference,
        "shipToAddress": ship_to,
        "shipVia": order_spec.ship_via or next(iter(spec_mod.PINNED_SHIP_VIA)),
        "dropShip": "false",
        "residentialDelivery": "false",
        "freight": {"termsType": order_spec.freight_terms},
        "orderSubTotal": f"{subtotal:.2f}",
        "orderShipTotal": "0.0",
        "orderShippingTotal": "0.0",
        "orderTaxTotal": "0.0",
        "orderTotal": f"{subtotal:.2f}",
        "orderLines": {"orderLine": lines},
    }
    if order_spec.reference2:
        order["otherReferenceNumber2"] = order_spec.reference2
    if order_spec.planned_ship_date:
        order["plannedShipDate"] = order_spec.planned_ship_date
    if bill_to:
        order["billToAddress"] = bill_to

    notes_body = _notes_body(order_spec)
    if notes_body:
        order["notes"] = {"note": [{
            "title": _NOTE_TITLE.get(order_spec.channel, "F3E Order"),
            "body": notes_body,
        }]}

    return {"order": [order]}
