"""Human-authored order spec: parse + validate, refusing before a payload is
ever built.

SPEC AUTHORING IS HUMAN-ONLY (2026-09-09 ruling item 4, re-ruled not gray):
an LLM transcribing a PO into a spec is IN the order path -- a 208-to-280
transposition would pass every check below and ship. Nothing in this module
accepts free text; it only parses a human-typed YAML file and validates it
against pinned tables. `authored_by` is an attestation field, not a technical
control -- there is no way for code to prove a human typed the YAML, which is
exactly why the ruling is a process discipline, not a code gate.

VALIDATION HAPPENS BEFORE A SINGLE LINE OF THE PAYLOAD IS BUILT, and it is
the ONLY place the SO 081226 defect classes are refused before push:
  * `type` -- not even a field here; `payload.py`'s module CONSTANT is what
    prevents "Purchase Order" from ever reaching a wholesale order.
  * the stray-line class -- a SKU outside the tenant map, or outside the
    BUYER's allowed set (Gotham = Pure only), is refused HERE, before staging.
ATP-live and shipVia-pinned-set checks are NOT here -- those need a live API
call and belong to `preflight.py`, which runs at stage time and again at tap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]

CHANNELS: tuple[str, ...] = ("wholesale", "fba", "wfs")

#: Per-channel reference-number shape. Wholesale POs vary in house style
#: (numeric, alnum); the two real Amazon shipment ids observed live
#: (`FBA19NG0MR50`, `FBA19P6VCSJW`) are `FBA` + alnum. WFS is carried forward
#: from the one historical pattern in canon (`9480757WFA`) -- unrehearsed,
#: not gated on in this build, kept so the enum is not a silent trap later.
#: Restricted to [A-Za-z0-9-] (no underscore/space): the derived order
#: `number` (payload.py) upper-cases and concatenates this with the buyer/FC
#: code under a strict `[A-Z0-9-]`-only, <=30-char limit, so the reference
#: itself must already be safe to fold into that shape without a silent
#: transform -- refuse an incompatible reference here, never mangle it later.
_REFERENCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "wholesale": re.compile(r"^[A-Za-z0-9-]{1,29}$"),
    "fba": re.compile(r"^FBA[0-9A-Z]{6,20}$"),
    "wfs": re.compile(r"^[0-9A-Z]{6,20}$"),
}

CUSTOMERS_PATH = _REPO_ROOT / "data" / "maps" / "deposco-customers.yaml"
AMAZON_FC_PATH = _REPO_ROOT / "data" / "maps" / "deposco-amazon-fc.yaml"
SKU_MAP_PATH = _REPO_ROOT / "data" / "maps" / "f3e-channel-sku-map.yaml"
AMAZON_MSKU_MAP_PATH = _REPO_ROOT / "data" / "maps" / "amazon-msku-map.yaml"

#: Only value confirmed live on prod as of the 2026-09-23 verification
#: (three real orders, all `General Freight`). An unconfirmed value is
#: flagged, never silently accepted -- see `preflight.py`.
PINNED_SHIP_VIA: frozenset[str] = frozenset({"General Freight"})


def _load_yaml(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_customers() -> dict[str, dict]:
    return (_load_yaml(CUSTOMERS_PATH) or {}).get("customers", {}) or {}


def load_amazon_fcs() -> dict[str, dict]:
    return (_load_yaml(AMAZON_FC_PATH) or {}).get("facilities", {}) or {}


def load_sku_map() -> dict[str, dict]:
    return (_load_yaml(SKU_MAP_PATH) or {}).get("skus", {}) or {}


def load_amazon_msku_map() -> dict[str, dict]:
    return (_load_yaml(AMAZON_MSKU_MAP_PATH) or {}).get("items", {}) or {}


@dataclass
class OrderSpecLine:
    sku: str
    qty: int
    unit_price: str  # kept as a string -- money is never coerced to float here
    msku: str = ""


@dataclass
class OrderSpec:
    channel: str
    buyer_or_fc_code: str
    reference: str
    authored_by: str
    reference2: str = ""
    planned_ship_date: str = ""
    freight_terms: str = "Prepaid"
    ship_via: str = ""
    notes: str = ""
    lines: list[OrderSpecLine] = field(default_factory=list)


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    spec: OrderSpec | None = None


def parse_spec_yaml(text: str) -> tuple[dict | None, list[str]]:
    """Parse only -- no validation. Returns (raw_dict_or_None, parse_errors)."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [f"invalid YAML: {exc}"]
    if raw is None:
        return None, ["spec file is empty"]
    if not isinstance(raw, dict):
        return None, ["spec root must be a mapping"]
    return raw, []


def _address_book_for(channel: str, code: str) -> tuple[dict | None, list[str]]:
    if channel == "wholesale":
        customers = load_customers()
        entry = customers.get(code)
        if entry is None:
            return None, [f"buyer code {code!r} not found in deposco-customers.yaml"]
        return entry, []
    if channel == "wfs":
        # UNCONDITIONAL -- checked before ever consulting the Amazon FC map.
        # D-051 review, 2026-09-23: WFS and FBA shared the exact same
        # lookup (data/maps/deposco-amazon-fc.yaml, an Amazon-only facility
        # map) with the refusal firing only on a MISS -- so a WFS spec whose
        # buyer_or_fc_code happened to match a real Amazon FC code would have
        # silently resolved to that Amazon facility's address.
        return None, [
            "WFS channel has no address book yet in this build -- "
            "do not stage a WFS spec (deferred per the 2026-09-09 ruling)"
        ]
    if channel == "fba":
        facilities = load_amazon_fcs()
        entry = facilities.get(code)
        if entry is None:
            return None, [f"FC code {code!r} not found in deposco-amazon-fc.yaml"]
        return entry, []
    return None, [f"unknown channel {channel!r}"]


def validate_spec(raw: dict) -> ValidationResult:
    """Every check that can run WITHOUT a live API call. Live checks (ATP,
    order/reference existence, shipVia confirmation) are `preflight.py`'s job."""
    errors: list[str] = []

    channel = str(raw.get("channel") or "").strip().lower()
    if channel not in CHANNELS:
        errors.append(f"channel must be one of {CHANNELS}, got {raw.get('channel')!r}")
        return ValidationResult(ok=False, errors=errors)

    authored_by = str(raw.get("authored_by") or "").strip()
    if not authored_by:
        errors.append(
            "authored_by is required (human attestation -- spec authoring is "
            "human-only, 2026-09-09 ruling item 4)"
        )

    code = str(raw.get("buyer_or_fc_code") or "").strip().upper()
    if not code:
        errors.append("buyer_or_fc_code is required")

    reference = str(raw.get("reference") or "").strip()
    pattern = _REFERENCE_PATTERNS.get(channel)
    if not reference:
        errors.append("reference is required")
    elif pattern and not pattern.match(reference):
        errors.append(
            f"reference {reference!r} does not match the expected {channel} shape"
        )
    else:
        # Canonicalize to uppercase HERE, once, so every downstream use (the
        # derived order number, AND otherReferenceNumber/customerOrderNumber
        # in the payload) agrees. D-051 review, 2026-09-23: build_order_number
        # already uppercases its OWN copy for the derived number, but the
        # ORIGINAL-case reference was what landed in the payload's reference
        # fields -- so "4471a" and "4471A" derived the IDENTICAL order number
        # (a false idempotency collision) while looking like different POs in
        # the payload. One canonical form removes the mismatch entirely.
        reference = reference.upper()
        # The reference-shape regexes cap length independently of the BUYER
        # code's length, but payload.py's derived order number
        # (F3E-W-{BUYER}-{PO} / F3E-AMZ-{ShipmentID} / F3E-WMT-{ShipmentID})
        # has its OWN <=30-char limit -- D-051 review, 2026-09-23: for the
        # only configured buyer (GOTHAM, 6 chars) a reference past ~17 chars
        # passed HERE but only raised PayloadBuildError later, at staging
        # time, contradicting this module's own "refused HERE, never mangled
        # later" claim. Mirror payload.py's exact prefix shape so the two
        # never drift out of sync.
        _prefix_len = {
            "wholesale": len("F3E-W-") + len(code) + len("-"),
            "fba": len("F3E-AMZ-"),
            "wfs": len("F3E-WMT-"),
        }.get(channel, 0)
        if _prefix_len + len(reference) > 30:
            errors.append(
                f"reference {reference!r} is too long for buyer/FC {code!r}: the "
                f"derived order number would be {_prefix_len + len(reference)} "
                f"chars, over Deposco's 30-char limit"
            )

    reference2 = str(raw.get("reference2") or "").strip()
    planned_ship_date = str(raw.get("planned_ship_date") or "").strip()
    freight_terms = str(raw.get("freight_terms") or "Prepaid").strip() or "Prepaid"
    if freight_terms not in ("Prepaid", "Third Party"):
        errors.append(f"freight_terms must be 'Prepaid' or 'Third Party', got {freight_terms!r}")
    ship_via = str(raw.get("ship_via") or "").strip()
    notes = str(raw.get("notes") or "").strip()

    address_book_entry: dict | None = None
    if code:
        address_book_entry, addr_errors = _address_book_for(channel, code)
        errors.extend(addr_errors)

    sku_map = load_sku_map()
    allowed_skus: set[str] | None = None
    if address_book_entry is not None and channel == "wholesale":
        allowed_skus = set(address_book_entry.get("allowed_skus") or [])
        if not allowed_skus:
            errors.append(f"buyer {code!r} has no allowed_skus configured")

    msku_map = load_amazon_msku_map()

    raw_lines = raw.get("lines")
    parsed_lines: list[OrderSpecLine] = []
    if not raw_lines or not isinstance(raw_lines, list):
        errors.append("at least one line is required")
    else:
        for i, raw_line in enumerate(raw_lines, start=1):
            if not isinstance(raw_line, dict):
                errors.append(f"line {i}: must be a mapping")
                continue
            sku = str(raw_line.get("sku") or "").strip()
            if not sku:
                errors.append(f"line {i}: sku is required")
            elif sku not in sku_map:
                errors.append(f"line {i}: sku {sku!r} is not in the pinned SKU map")
            elif allowed_skus is not None and sku not in allowed_skus:
                errors.append(
                    f"line {i}: sku {sku!r} is not in buyer {code!r}'s allowed-SKU set"
                )

            qty_raw = raw_line.get("qty")
            qty = None
            # int() TRUNCATES a float (int(208.5) == 208) rather than
            # refusing it -- D-051 review, 2026-09-23: a fractional qty must
            # be refused, never silently rounded down to a different number
            # than what was typed.
            if isinstance(qty_raw, bool) or not isinstance(qty_raw, (int, float, str)):
                errors.append(f"line {i}: qty must be an integer, got {qty_raw!r}")
            else:
                try:
                    qty_decimal = Decimal(str(qty_raw))
                except InvalidOperation:
                    errors.append(f"line {i}: qty must be an integer, got {qty_raw!r}")
                else:
                    if qty_decimal != qty_decimal.to_integral_value():
                        errors.append(
                            f"line {i}: qty must be a whole number, got {qty_raw!r}"
                        )
                    else:
                        qty = int(qty_decimal)
                        if qty <= 0:
                            errors.append(f"line {i}: qty must be > 0, got {qty}")

            unit_price_raw = raw_line.get("unit_price")
            unit_price = str(unit_price_raw if unit_price_raw is not None else "").strip()
            if not unit_price:
                errors.append(f"line {i}: unit_price is required")
            else:
                try:
                    price = Decimal(unit_price)
                except InvalidOperation:
                    errors.append(f"line {i}: unit_price {unit_price!r} is not a number")
                else:
                    # is_finite() FIRST: Decimal("nan") < 0 RAISES InvalidOperation
                    # (uncaught, would crash validation) and Decimal("inf").as_tuple()
                    # has a non-numeric exponent ('F'), which the decimal-places
                    # check below would crash on -- D-051 review, 2026-09-23. "nan"
                    # and "inf"/"Infinity" are refused here, before either comparison.
                    if not price.is_finite():
                        errors.append(
                            f"line {i}: unit_price {unit_price!r} must be a finite number"
                        )
                    elif price < 0:
                        errors.append(f"line {i}: unit_price must be >= 0, got {unit_price!r}")
                    # A sub-cent price would carry undefined rounding into
                    # payload.py's order-total sum (D-051 review, 2026-09-23:
                    # a float-summed, once-rounded total can diverge from the
                    # displayed per-line prices by a cent). Refuse it here
                    # rather than silently rounding a human-typed dollar figure.
                    elif -price.as_tuple().exponent > 2:
                        errors.append(
                            f"line {i}: unit_price {unit_price!r} has more than 2 "
                            f"decimal places -- money is dollars and cents only"
                        )

            msku = str(raw_line.get("msku") or "").strip()
            if msku and sku in msku_map:
                expected = str(msku_map[sku].get("msku") or "")
                if expected and msku != expected:
                    errors.append(
                        f"line {i}: msku {msku!r} does not match the pinned Amazon "
                        f"merchant SKU {expected!r} for {sku!r} -- this is the "
                        f"F3VPM/F3VPM4 class of error, refusing rather than guessing"
                    )

            if qty is not None and sku:
                parsed_lines.append(OrderSpecLine(
                    sku=sku, qty=qty, unit_price=unit_price, msku=msku,
                ))

    if errors:
        return ValidationResult(ok=False, errors=errors)

    spec = OrderSpec(
        channel=channel,
        buyer_or_fc_code=code,
        reference=reference,
        authored_by=authored_by,
        reference2=reference2,
        planned_ship_date=planned_ship_date,
        freight_terms=freight_terms,
        ship_via=ship_via,
        notes=notes,
        lines=parsed_lines,
    )
    return ValidationResult(ok=True, spec=spec)


def parse_and_validate(text: str) -> ValidationResult:
    raw, parse_errors = parse_spec_yaml(text)
    if parse_errors:
        return ValidationResult(ok=False, errors=parse_errors)
    assert raw is not None
    return validate_spec(raw)
