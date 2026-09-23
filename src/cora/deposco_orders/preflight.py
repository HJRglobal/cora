"""GET-only pre-flight, run at stage time AND again live at tap.

Every check here is a READ against `deposco_client.DeposcoClient` (or the
pinned tables `spec.py` already loaded) -- nothing in this module can write.
`/status/order/search` is NEVER the existence check (it does not filter by
`number` on this tenant, per the 8/14 finding); `find_order_detail`
(`/search/Order`) is, per the 2026-09-23 verification.

A LIVE CHECK THAT ITSELF FAILS (auth, network, an unexpected API error) IS
LEFT TO PROPAGATE, not converted into a false "check failed" result --
`run_preflight` only produces pass/fail judgments for the deliberate
business checks below. A caller that cannot tell "preflight found a problem"
apart from "preflight could not run" would be one step from staging on a
guess, which is exactly what this module exists to prevent.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from ..connectors import deposco_client as dc
from . import payload as payload_mod
from . import spec as spec_mod
from .spec import OrderSpec

_AZ = datetime.timezone(datetime.timedelta(hours=-7))


def _now_iso() -> str:
    return datetime.datetime.now(_AZ).isoformat(timespec="seconds")


@dataclass
class PreflightCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class PreflightResult:
    passed: bool
    checks: list[PreflightCheck] = field(default_factory=list)
    checked_at: str = ""

    def age_seconds(self, now: datetime.datetime | None = None) -> float:
        checked = datetime.datetime.fromisoformat(self.checked_at)
        current = now or datetime.datetime.now(checked.tzinfo)
        return (current - checked).total_seconds()

    def failed_checks(self) -> list[PreflightCheck]:
        return [c for c in self.checks if not c.passed]


def _reference_exists(client: dc.DeposcoClient, reference: str) -> bool:
    """Client-side filtered, same defensive posture as `get_order_status`
    against a server search -- a miss here is only ever a MISS if we can
    actually see that no returned record's `customerOrderNumber` matches.

    LIVE FINDING (2026-09-23, prod): `otherReferenceNumber` is NOT a
    configured search field for the Order entity on the PROD tenant --
    `/search/Order?otherReferenceNumber=...` returns a hard 400 ("Following
    search fields [otherReferenceNumber] are not found or configured for
    entity [Order]"), even though the SAME query works cleanly on UA (200,
    empty/matching result). This is a tenant-configuration difference, not a
    vendor-doc question. `customerOrderNumber` IS configured on both tenants
    and carries the identical value (`payload.py` sets both fields from
    `spec.reference`) -- verified live to genuinely filter server-side
    (found exactly 1 record for a known real order, 081226, by
    `customerOrderNumber`). Using it here is what makes this check work on
    prod at all, not just in UA rehearsal.
    """
    response = client.search_orders("Sales Order", customerOrderNumber=reference)
    records = dc.parse_order_header_detail(response)
    return any(r.customer_order_number == reference for r in records)


def run_preflight(order_spec: OrderSpec, client: dc.DeposcoClient) -> PreflightResult:
    """All five checks. Any live-read failure (auth, network, an unexpected
    API shape) PROPAGATES as itself -- see the module docstring."""
    number = payload_mod.build_order_number(order_spec)
    checks: list[PreflightCheck] = []

    existing = client.find_order_detail("Sales Order", number)
    checks.append(PreflightCheck(
        "number_miss", existing is None,
        "" if existing is None else f"order {number!r} already exists",
    ))

    ref_hit = _reference_exists(client, order_spec.reference)
    checks.append(PreflightCheck(
        "reference_miss", not ref_hit,
        "" if not ref_hit else f"an order already references {order_spec.reference!r}",
    ))

    missing_items = [
        line.sku for line in order_spec.lines if not client.item_exists(line.sku)
    ]
    checks.append(PreflightCheck(
        "items_exist", not missing_items,
        "" if not missing_items else f"not found in Deposco: {missing_items}",
    ))

    skus = [line.sku for line in order_spec.lines]
    atp_result = client.get_enterprise_availability(item_numbers=skus) if skus else None
    by_item = atp_result.by_item() if atp_result else {}
    shortfalls: list[str] = []
    for line in order_spec.lines:
        row = by_item.get(line.sku)
        atp = row.measure("atpQty") if row else None
        if atp is None or atp < line.qty:
            shown = "UNKNOWN" if atp is None else str(atp)
            shortfalls.append(f"{line.sku}: need {line.qty}, ATP {shown}")
    # UA carries no inventory by design (Anthony, 8/5) -- a UA push would
    # ALWAYS shortfall here, which would make the R1/R6 rehearsal structurally
    # impossible. ATP stays a hard block in prod (the real safety-critical
    # case); in UA it is recorded for visibility but never blocks staging.
    atp_enforced = getattr(client, "env", "prod") != "ua"
    atp_detail = "; ".join(shortfalls)
    if shortfalls and not atp_enforced:
        atp_detail += " (UA carries no inventory -- not enforced in this environment)"
    checks.append(PreflightCheck(
        "atp_sufficient", (not shortfalls) or not atp_enforced, atp_detail,
    ))

    ship_via = order_spec.ship_via or next(iter(spec_mod.PINNED_SHIP_VIA))
    ship_via_ok = ship_via in spec_mod.PINNED_SHIP_VIA
    checks.append(PreflightCheck(
        "ship_via_pinned", ship_via_ok,
        "" if ship_via_ok else
        f"{ship_via!r} is not in the pinned prod set {sorted(spec_mod.PINNED_SHIP_VIA)}",
    ))

    return PreflightResult(
        passed=all(c.passed for c in checks), checks=checks, checked_at=_now_iso(),
    )
