#!/usr/bin/env python3
"""UA rehearsal of the NEW push module (R1, R5, R6) -- SONNET-HANDOFF step 6.

R2 (duplicate refused at stage AND at tap with no API call; a forced re-POST
in a harness proves 200 UPDATED) and R4 (blank-200 x3, timeout, 200-on-create)
are proven via INJECTED-TRANSPORT tests per the design's own instruction ("In
a test harness only (injected transport)") --
`tests/test_deposco_orders_handler.py`'s `TestOutcomeClassification` and
`TestLiveDriftReCheck` classes cover exactly these scenarios; this script
does not repeat them. R3 (stray/unmapped SKU, qty<=0 refused) is pure
validation logic with no live-API dependency, proven in
`tests/test_deposco_orders_spec.py`'s `TestStrayLineRefusal` /
`TestLineFieldValidation` classes.

This script exercises what genuinely needs a REAL UA call, through the
ACTUAL module stack (spec -> payload -> preflight -> pending -> push ->
read-back), not a fake:
  R1  a real Gotham-shape wholesale push, env=ua -> 201 + clean read-back.
      Also carries `planned_ship_date` (part of R5).
  R6  a real FBA push to the GEU3 address, env=ua -> 201 + clean read-back.

R5 (customAttribute1 "run id"): NOT implemented in `payload.py` this build --
the design frames it as optional traceability added "only after a UA 201
with it present" for the OTHER optional fields, which R1 below proves.
Flagged as a follow-up, not silently dropped.

Ships nothing real: UA carries no inventory (Anthony, 8/5); this rehearses
the push + read-back loop only, same posture as the sandbox-pinned
`deposco_ua_test_order.py`. Unlike that script, this ONE calls through
`deposco_client`/`deposco_push`, which are env-selected (not sandbox-pinned),
so it takes `--env` for the record but MUST NOT be run with prod (no code
path here defaults there; env is UA-only by construction below).

Usage:
    python scripts/deposco_ua_rehearsal_push_module.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))


def main() -> int:
    load_dotenv(_REPO_ROOT / ".env", override=True)

    from cora.deposco_orders import handler, spec as spec_mod

    ENV = "ua"
    report: list[str] = []

    def p(line: str = "") -> None:
        report.append(line)
        print(line)

    p("=" * 78)
    p("DEPOSCO UA REHEARSAL -- push module R1 / R5 / R6")
    p("=" * 78)

    # ── R1: real Gotham-shape wholesale push, also carrying plannedShipDate (R5) ──
    p("\n--- R1: wholesale push (TEST reference TEST006) ---")
    r1_raw = {
        "channel": "wholesale",
        "buyer_or_fc_code": "GOTHAM",
        "reference": "TEST006",
        "authored_by": "Claude Code UA rehearsal",
        "freight_terms": "Prepaid",
        "planned_ship_date": "2026-09-25",
        "notes": "UA rehearsal of the new push module. Ships nothing (sandbox has no inventory).",
        "lines": [
            {"sku": "PURE-Original", "qty": 10, "unit_price": "21.70"},
            {"sku": "PURE-Citrus", "qty": 10, "unit_price": "21.70"},
        ],
    }
    r1_ok = _run_one(p, handler, spec_mod, r1_raw, env=ENV, label="R1")

    # ── R6: real FBA push to GEU3 ────────────────────────────────────────────
    p("\n--- R6: FBA push to GEU3 (TEST reference FBA00000004) ---")
    r6_raw = {
        "channel": "fba",
        "buyer_or_fc_code": "GEU3",
        "reference": "FBA00000004",
        "authored_by": "Claude Code UA rehearsal",
        "freight_terms": "Prepaid",
        "notes": "UA rehearsal of the new push module (FBA shape). Ships nothing.",
        "lines": [
            {"sku": "F3VPM4", "qty": 5, "unit_price": "32.99", "msku": "F3VPM"},
        ],
    }
    r6_ok = _run_one(p, handler, spec_mod, r6_raw, env=ENV, label="R6")

    p("\n" + "=" * 78)
    p(f"R1 (wholesale): {'PASS' if r1_ok else 'FAIL'}")
    p(f"R6 (FBA): {'PASS' if r6_ok else 'FAIL'}")
    p("R2/R4: proven via injected-transport tests (test_deposco_orders_handler.py)")
    p("R3: proven via validation-only tests (test_deposco_orders_spec.py)")
    p("R5: partially proven by R1 (plannedShipDate/customerOrderNumber/"
      "secondaryOrderSource); customAttribute1 not implemented this build")
    p("=" * 78)

    report_path = _REPO_ROOT / "logs" / "deposco-ua-rehearsal-push-module-output.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(report), encoding="utf-8")
    print(f"\n(full report also written to {report_path})")

    return 0 if (r1_ok and r6_ok) else 1


def _run_one(p, handler, spec_mod, raw: dict, *, env: str, label: str) -> bool:
    result = spec_mod.validate_spec(raw)
    if not result.ok:
        p(f"{label}: SPEC INVALID: {result.errors}")
        return False

    outcome = handler.stage_order(result.spec, env=env, spec_raw=raw, spec_path=f"({label} rehearsal)")
    if not outcome.ok:
        p(f"{label}: STAGE FAILED: blocked={outcome.blocked_reason!r} errors={outcome.errors}")
        return False
    entry = outcome.entry
    p(f"{label}: staged {entry['number']} (id={entry['id']})")

    tap_outcome, message = handler.process_push_tap(entry["id"], handler.HARRISON_ID)
    p(f"{label}: tap outcome = {tap_outcome}")
    p(f"{label}: {message}")

    resolved = None
    from cora.deposco_orders import pending
    resolved = pending.get_entry(entry["id"])
    p(f"{label}: final state = {resolved.get('state') if resolved else 'UNKNOWN'}")
    return tap_outcome == "confirmed"


if __name__ == "__main__":
    raise SystemExit(main())
