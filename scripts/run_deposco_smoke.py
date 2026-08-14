"""Deposco Phase-1 acceptance smoke -- READ-ONLY, three-outcome reporting.

Exercises the GET-only client end to end and reports CONFIRMED / FAILED /
UNKNOWN per leg (D-101), rather than a single pass/fail that hides which half
worked.

UA LEG (`--env ua`)
    Reachability only. The sandbox carries NO inventory by design, so an empty
    availability response there is EXPECTED and is reported as such -- it is the
    one place "empty" is not suspicious. The known-good target is the order
    pushed on 2026-08-14, `TEST-GOTHAM-001`, which Anthony confirmed can live in
    the sandbox permanently: it must read back with 4 lines.

PROD LEG (`--env prod`)
    Read-only availability + receipts. Safe by construction -- the client cannot
    write. This is the leg Harrison/Alex reconcile against the warehouse UI and
    the manual weekly Sheet; two consecutive clean weekly checks is the gate that
    turns on the digest consumers.

THE GREP-GATE runs on every leg: everything this script printed is scanned for
credential material before it exits, and a hit is a hard failure. That check is
the point -- an acceptance run is exactly when a credential would end up pasted
into a note or a ticket.

Usage:
    python scripts/run_deposco_smoke.py --env ua
    python scripts/run_deposco_smoke.py --env prod

Exit codes: 0 = every leg CONFIRMED (or EXPECTED-EMPTY on UA); 1 = at least one
leg UNKNOWN; 2 = at least one leg FAILED, or the grep-gate tripped.
"""

from __future__ import annotations

import argparse
import datetime
import io
import os
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

from cora import inventory_state as inv  # noqa: E402
from cora.connectors import deposco_client as dc  # noqa: E402

#: Pushed 2026-08-14; stays in the sandbox permanently (Anthony: "it's a SB, it
#: can live there with no issues"). 4 lines x 156 twelve-packs.
UA_KNOWN_ORDER = ("Sales Order", "TEST-GOTHAM-001")
UA_EXPECTED_LINES = 4

CONFIRMED, FAILED, UNKNOWN, EXPECTED_EMPTY = "CONFIRMED", "FAILED", "UNKNOWN", "EXPECTED-EMPTY"


class Report:
    """Collects per-leg outcomes AND every line printed, so the grep-gate can
    scan exactly what an operator would copy out of this run."""

    def __init__(self) -> None:
        self.legs: list[tuple[str, str, str]] = []
        self.buffer = io.StringIO()

    def say(self, text: str = "") -> None:
        print(text)
        self.buffer.write(text + "\n")

    def leg(self, name: str, outcome: str, detail: str = "") -> None:
        self.legs.append((name, outcome, detail))
        self.say(f"  [{outcome:<14s}] {name}" + (f" -- {detail}" if detail else ""))

    @property
    def worst(self) -> int:
        outcomes = {o for _, o, _ in self.legs}
        if FAILED in outcomes:
            return 2
        if UNKNOWN in outcomes:
            return 1
        return 0


def _secrets_in_scope() -> list[str]:
    """Every credential value reachable from this process's environment.

    Scanned for regardless of which env the run targeted: a UA smoke that leaked
    the PROD password would be just as bad.
    """
    keys = [spec.user_key for spec in dc.ENVIRONMENTS.values()]
    keys += [spec.pass_key for spec in dc.ENVIRONMENTS.values()]
    return [v for v in (os.environ.get(k, "").strip() for k in keys) if v]


def grep_gate(report: Report) -> bool:
    """True when the transcript is clean. Reports WHICH key leaked, never the value."""
    text = report.buffer.getvalue()
    leaked = []
    for spec in dc.ENVIRONMENTS.values():
        for key in (spec.user_key, spec.pass_key):
            value = os.environ.get(key, "").strip()
            if value and value in text:
                leaked.append(key)
    if leaked:
        print(f"\n!! GREP-GATE FAILED: credential material for {', '.join(sorted(set(leaked)))} "
              f"appeared in this run's output. Do not paste this transcript anywhere.")
        return False
    return True


def smoke_ua(client: "dc.DeposcoClient", report: Report) -> None:
    order_type, number = UA_KNOWN_ORDER

    # Read back via /search/Order -- the one route verified to filter by number
    # server-side on this tenant. The status-search route ignores `number` and
    # returns the whole tenant, so a miss there proves nothing.
    try:
        headers = dc.parse_order_headers(client.find_order(order_type, number))
    except dc.DeposcoAuthError as exc:
        report.leg("auth + order read-back", FAILED, f"credentials rejected ({exc})")
        return
    except dc.DeposcoError as exc:
        report.leg("auth + order read-back", FAILED, str(exc)[:200])
        return

    match = [(n, count) for n, count in headers if n == number]
    if not match:
        report.leg(
            f"order read-back ({number})", FAILED,
            f"not returned by the server-filtered search route ({len(headers)} order(s) "
            f"came back) -- this route DOES filter, so absence here is real",
        )
    else:
        lines = match[0][1]
        report.leg(
            f"order read-back ({number})",
            CONFIRMED if lines == UA_EXPECTED_LINES else UNKNOWN,
            f"{lines} line(s), expected {UA_EXPECTED_LINES}",
        )

    # Sandbox has no inventory -- empty here is the EXPECTED state, and is the one
    # place this codebase does not treat empty as suspicious.
    try:
        result = client.get_enterprise_availability()
        report.leg(
            "enterprise-inventory reachable",
            EXPECTED_EMPTY if not result.rows else CONFIRMED,
            f"{len(result.rows)} row(s) -- the sandbox carries no inventory by design",
        )
    except dc.DeposcoError as exc:
        report.leg("enterprise-inventory reachable", FAILED, str(exc)[:200])

    try:
        lines_out = client.get_purchase_order_receipts()
        report.leg(
            "PO receipt lines reachable",
            EXPECTED_EMPTY if not lines_out else CONFIRMED,
            f"{len(lines_out)} line(s); "
            f"{sum(1 for x in lines_out if x.has_lot)} with a lot number",
        )
    except dc.DeposcoError as exc:
        report.leg("receipt-line search reachable", FAILED, str(exc)[:200])

    try:
        end = datetime.date.today()
        start = end - datetime.timedelta(days=30)
        client.get_shipments(f"{start:%Y-%m-%d}T00:00:00", f"{end:%Y-%m-%d}T23:59:59")
        report.leg("shipment search reachable", CONFIRMED)
    except dc.DeposcoError as exc:
        report.leg("shipment search reachable", FAILED, str(exc)[:200])


def smoke_prod(client: "dc.DeposcoClient", report: Report) -> None:
    sku_map = inv.load_sku_map()
    known = list((sku_map.get("skus") or {}).keys())
    if not known:
        report.leg("SKU map", FAILED, "empty or unreadable")
        return

    try:
        result = client.get_enterprise_availability()
    except dc.DeposcoError as exc:
        report.leg("enterprise-inventory", FAILED, str(exc)[:200])
        return

    rows = result.by_item()
    read = [sku for sku in known if sku in rows]
    missing = [sku for sku in known if sku not in rows]

    if not result.rows:
        # In PROD this is the suspicious case, not the expected one.
        report.leg("enterprise-inventory", FAILED,
                   "a working API returned NO items -- treat as blind, never as zero stock")
        return
    report.leg(
        "enterprise-inventory",
        CONFIRMED if not missing and not result.truncated else UNKNOWN,
        f"{len(read)} of {len(known)} known SKU(s)"
        + (f"; NOT returned: {', '.join(missing)}" if missing else "")
        + ("; PAGE CAP HIT -- partial" if result.truncated else ""),
    )

    report.say("")
    report.say(f"  {'SKU':18s} {'on-hand':>12s} {'ATP':>12s} {'on PO':>12s} {'in transit':>12s}")
    for sku in known:
        row = rows.get(sku)
        if row is None:
            report.say(f"  {sku:18s} {'NOT RETURNED':>12s}")
            continue

        def cell(measure: str) -> str:
            value = row.measure(measure)
            return f"{value:>12,}" if value is not None else f"{'UNKNOWN':>12s}"

        report.say(
            f"  {sku:18s} {cell('totalOnHandQty')} {cell('atpQty')} "
            f"{cell('qtyOnPO')} {cell('inTransitQty')}"
        )
        for facility in row.facilities:
            on_hand = facility.measures.get("totalOnHandQty")
            shown = f"{on_hand:,}" if on_hand is not None else "UNKNOWN"
            report.say(f"      @ {facility.facility}: on-hand {shown}")
    report.say("")

    try:
        receipts = client.get_purchase_order_receipts()
        with_lot = sum(1 for line in receipts if line.has_lot)
        report.leg(
            "receipt lines (lot + expiry)",
            CONFIRMED if receipts else UNKNOWN,
            f"{len(receipts)} line(s); lot present on {with_lot}",
        )
    except dc.DeposcoError as exc:
        report.leg("receipt lines (lot + expiry)", FAILED, str(exc)[:200])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="ua", choices=sorted(dc.ENVIRONMENTS))
    args = parser.parse_args(argv)

    report = Report()
    report.say(f"Deposco Phase-1 smoke -- {args.env.upper()} (READ-ONLY; the client cannot write)")
    report.say("=" * 72)

    if not _secrets_in_scope():
        report.say("  no Deposco credentials in the environment at all")

    try:
        client = dc.DeposcoClient(env=args.env)
    except dc.DeposcoAuthError as exc:
        report.leg("client construction", FAILED, str(exc))
        report.say("\n" + "=" * 72)
        report.say("FAILED: credentials are not in place for this environment.")
        return 2 if grep_gate(report) else 2

    report.say(f"  tenant/BU : {client.tenant} / {client.business_unit}")
    report.say(f"  base      : {client.base_url}")
    report.say("")

    if args.env == "ua":
        smoke_ua(client, report)
    else:
        smoke_prod(client, report)

    report.say("")
    report.say("=" * 72)
    counts: dict[str, int] = {}
    for _, outcome, _ in report.legs:
        counts[outcome] = counts.get(outcome, 0) + 1
    report.say("  " + " | ".join(f"{o}: {n}" for o, n in sorted(counts.items())))

    if not grep_gate(report):
        return 2
    report.say("  grep-gate : PASS (no credential material in this transcript)")
    return report.worst


if __name__ == "__main__":
    raise SystemExit(main())
