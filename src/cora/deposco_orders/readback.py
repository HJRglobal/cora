"""D-110 read-back: verify every state change from the LIVE surface. Tool
success (a 201) is never itself CONFIRMED -- only a read-back that finds the
order AND whose line item/quantity multiset, ship-to postal code and
reference match the pushed payload earns that word.

Built on `DeposcoClient.find_order_detail` / `OrderHeaderRecord`, the route
the 2026-09-23 prod verification found actually carries line detail (see that
module's docstring for why `/status/order` is NOT used here).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..connectors import deposco_client as dc

#: A 201 does not guarantee the order is IMMEDIATELY visible on read; a few
#: short retries smooth over eventual-consistency lag without holding a
#: synchronous Slack tap open anywhere near the design's ~2-minute figure
#: (which describes the OUTER bound before UNKNOWN is declared, not a single
#: blocking wait). Exhausting these IS the "201 whose read-back misses after
#: retries" UNKNOWN path (R4).
READBACK_ATTEMPTS = 3
READBACK_DELAY_SECONDS = 3.0


@dataclass
class ReadBackResult:
    found: bool
    line_match: bool | None = None
    ship_to_postal_match: bool | None = None
    reference_match: bool | None = None
    record: "dc.OrderHeaderRecord | None" = None
    mismatches: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Every dimension must independently be True -- a stray line that
        happens to balance the multiset total but change ship-to would
        otherwise slip through."""
        return bool(
            self.found and self.line_match and self.ship_to_postal_match
            and self.reference_match
        )


def _expected_line_multiset(order: dict) -> dict[str, int]:
    totals: dict[str, int] = {}
    for line in (order.get("orderLines") or {}).get("orderLine", []):
        sku = line.get("itemNumber")
        if not sku:
            continue
        qty = int(float(line.get("orderPackQuantity") or 0))
        totals[sku] = totals.get(sku, 0) + qty
    return totals


def _compare(order: dict, record: "dc.OrderHeaderRecord") -> ReadBackResult:
    mismatches: list[str] = []

    expected_lines = _expected_line_multiset(order)
    actual_lines = record.line_item_qty_multiset()
    line_match = expected_lines == actual_lines
    if not line_match:
        mismatches.append(
            f"line item/qty multiset differs: expected {expected_lines}, got {actual_lines}"
        )

    expected_postal = (order.get("shipToAddress") or {}).get("postalCode", "")
    postal_match = (not expected_postal) or (record.ship_to_postal_code == expected_postal)
    if not postal_match:
        mismatches.append(
            f"ship-to postal code differs: expected {expected_postal!r}, "
            f"got {record.ship_to_postal_code!r}"
        )

    expected_ref = order.get("otherReferenceNumber", "")
    ref_match = (not expected_ref) or (record.customer_order_number == expected_ref)
    if not ref_match:
        mismatches.append(
            f"reference differs: expected {expected_ref!r}, "
            f"got {record.customer_order_number!r}"
        )

    return ReadBackResult(
        found=True, line_match=line_match, ship_to_postal_match=postal_match,
        reference_match=ref_match, record=record, mismatches=mismatches,
    )


def read_back(client: dc.DeposcoClient, payload: dict) -> ReadBackResult:
    """One read-back attempt, no retry. `payload` is the exact envelope that
    was pushed (`{"order": [{...}]}`)."""
    order = payload["order"][0]
    record = client.find_order_detail(order.get("type", "Sales Order"), order["number"])
    if record is None:
        return ReadBackResult(found=False)
    return _compare(order, record)


def read_back_with_retries(
    client: dc.DeposcoClient, payload: dict,
    *, attempts: int = READBACK_ATTEMPTS, delay_seconds: float = READBACK_DELAY_SECONDS,
    sleep=time.sleep,
) -> ReadBackResult:
    """Retries ONLY a not-found result (eventual-consistency lag) -- a FOUND
    record that does not match is returned immediately as the MISMATCH
    signal, never retried away."""
    result = ReadBackResult(found=False)
    for attempt in range(1, attempts + 1):
        result = read_back(client, payload)
        if result.found:
            return result
        if attempt < attempts:
            sleep(delay_seconds)
    return result
