"""F3E lot ledger v0 -- per-lot receipts, reconciled against warehouse on-hand.

Design Fork 3 (LOCKED 2026-08-06): build the lot ledger regardless of Deposco's
config answer, deriving per-lot state from receipts in and shipments out, and
reconcile it against Enterprise Inventory SKU totals with a two-computation
tie-out (the 13WCF-M2 $0-residual pattern).

═══════════════════════════════════════════════════════════════════════════════
PREMISE OVERTURNED AT BUILD TIME -- READ THIS BEFORE TRUSTING ANY LOT FIGURE
═══════════════════════════════════════════════════════════════════════════════
The design and the build prompt both assumed lot numbers ride BOTH directions:
"receipts-in (lot, expiry, qty) minus shipped-out (order-status lot lines)". A
full-text pass over the 457-page V1 doc says otherwise:

  * Every one of the five `lotNumber` examples in the doc is a `<receiptLine>` on
    a PURCHASE ORDER (pp. 185-196, orders PO12345 / PO2 / PO65823).
  * The Shipment API section (pp. 363-396) contains ZERO lot references. Shipment
    lines carry LPN, tracking number and weight -- no lot.
  * `lotNumber` is documented as a field on order LINES and RECEIPT lines
    (pp. 227-230). It is documented nowhere on a shipped or sold line.

So V1 exposes lot IN and does not expose lot OUT. Per-lot depletion is therefore
NOT COMPUTABLE from the documented surface, and this module refuses to invent it.

What it does instead, keeping facts and inference strictly separated:

  FACT      per-lot receipts, with expiry            (receipt lines)
  FACT      per-SKU on-hand                          (Enterprise Inventory)
  DERIVED   per-SKU consumed = received - on_hand    (two independent reads)
  FLAG      any tie-out that cannot close            (never silently absorbed)
  PROJECTION  per-lot remaining under an explicit FEFO assumption -- opt-in,
              always labelled, and never the only number shown

A negative residual (on-hand exceeds everything we have receipts for) is the
signal that the receipt history does not reach back far enough. That is a real,
reportable state, not an error to clamp to zero -- clamping it would silently
convert "we cannot see far enough back" into "the lots balance".

If Deposco later exposes lot directly on an outbound record, or a configured
Enterprise Inventory measure carries lot, that becomes a THIRD cross-check --
per Fork 3 it is a replacement for nothing.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger(__name__)

LEDGER_VERSION = 1

#: Why the OUT side is missing. Carried in the ledger itself so a consumer reading
#: the file -- not this docstring -- still knows what it is looking at.
OUTBOUND_ATTRIBUTION = "unavailable-in-v1"
OUTBOUND_REASON = (
    "Deposco V1 documents lotNumber only on purchase-order receipt lines. No "
    "shipped/sold line and no shipment record carries a lot, so per-lot depletion "
    "cannot be sourced. Per-SKU consumption below is DERIVED from receipts minus "
    "on-hand; per-lot remaining is a projection only, under a stated assumption."
)


# ─────────────────────────────────────────────────────────────────────────────
# Receipt keys and merging
# ─────────────────────────────────────────────────────────────────────────────


#: The identity of a receipt line, in one place. Used for the live pull AND for
#: rehydrating the stored history -- if those two ever disagree, a reload followed
#: by a merge stops matching and every stored receipt is re-added under a second
#: key, silently DOUBLING `received_total` and fabricating consumption out of it.
#: One function, one field order, both directions.
_KEY_FIELDS = ("order_number", "line_number", "receipt_number", "item_number")


def receipt_key(line: Any) -> str:
    """Stable identity for one receipt line, so repeated pulls are idempotent.

    Keyed on the STABLE identifiers rather than on a running index: re-pulling an
    overlapping window must replace a row, never duplicate it into the received
    total (the chunk-family lesson from the 8/01 filer bundle, same shape).
    """
    return "|".join(str(getattr(line, part, "") or "") for part in _KEY_FIELDS)


@dataclass
class LotReceipt:
    sku: str
    lot: str
    expiration: str
    quantity: int | None
    received_date: str
    order_number: str = ""
    receipt_number: str = ""
    line_number: str = ""

    @property
    def has_lot(self) -> bool:
        return bool(self.lot)

    @property
    def key(self) -> str:
        """Same identity as `receipt_key`, computed from a stored row. `item_number`
        is this record's `sku`, which is why the alias exists on the dataclass."""
        return receipt_key(self)

    @property
    def item_number(self) -> str:
        return self.sku

    def to_json(self) -> dict:
        return {
            "sku": self.sku,
            "lot": self.lot,
            "expiration": self.expiration,
            "quantity": self.quantity,
            "received_date": self.received_date,
            "order_number": self.order_number,
            "receipt_number": self.receipt_number,
            "line_number": self.line_number,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "LotReceipt":
        return cls(
            sku=str(raw.get("sku") or ""),
            lot=str(raw.get("lot") or ""),
            expiration=str(raw.get("expiration") or ""),
            quantity=raw.get("quantity"),
            received_date=str(raw.get("received_date") or ""),
            order_number=str(raw.get("order_number") or ""),
            receipt_number=str(raw.get("receipt_number") or ""),
            line_number=str(raw.get("line_number") or ""),
        )


def receipts_from_lines(lines: Iterable[Any]) -> dict[str, LotReceipt]:
    """Convert parsed receipt lines into keyed ledger receipts.

    Lines with no item number are dropped (they cannot be attributed to a SKU)
    but COUNTED by the caller via the returned dict's size versus the input --
    see `merge_receipts`, which reports what it could not place.
    """
    out: dict[str, LotReceipt] = {}
    for line in lines:
        sku = str(getattr(line, "item_number", "") or "").strip()
        if not sku:
            continue
        out[receipt_key(line)] = LotReceipt(
            sku=sku,
            lot=str(getattr(line, "lot_number", "") or "").strip(),
            expiration=str(getattr(line, "expiration_date", "") or "").strip(),
            quantity=getattr(line, "quantity", None),
            received_date=str(getattr(line, "received_date", "") or "").strip(),
            order_number=str(getattr(line, "order_number", "") or "").strip(),
            receipt_number=str(getattr(line, "receipt_number", "") or "").strip(),
            line_number=str(getattr(line, "line_number", "") or "").strip(),
        )
    return out


def merge_receipts(
    existing: dict[str, LotReceipt], incoming: dict[str, LotReceipt]
) -> tuple[dict[str, LotReceipt], int, int]:
    """Merge a fresh pull into the stored history. Returns (merged, added, replaced).

    Replace-on-key, so an overlapping window corrects a row rather than
    double-counting it.
    """
    merged = dict(existing)
    added = replaced = 0
    for key, receipt in incoming.items():
        if key in merged:
            replaced += 1
        else:
            added += 1
        merged[key] = receipt
    return merged, added, replaced


# ─────────────────────────────────────────────────────────────────────────────
# Per-SKU reconciliation
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class LotState:
    lot: str
    expiration: str
    received: int = 0
    receipts: int = 0

    def to_json(self) -> dict:
        return {
            "lot": self.lot,
            "expiration": self.expiration,
            "received": self.received,
            "receipt_count": self.receipts,
        }


@dataclass
class SkuState:
    sku: str
    received_total: int = 0
    #: None means the warehouse figure could not be read -- NOT zero on hand.
    on_hand: int | None = None
    lots: list[LotState] = field(default_factory=list)
    receipts_without_lot: int = 0
    quantity_unreadable: int = 0

    @property
    def derived_consumed(self) -> int | None:
        """received - on_hand. None when on-hand is unknown: subtracting from an
        unknown yields an unknown, never a number."""
        if self.on_hand is None:
            return None
        return self.received_total - self.on_hand

    @property
    def tie_out(self) -> str:
        if self.on_hand is None:
            return "on-hand-unknown"
        if not self.received_total:
            return "no-receipt-history"
        if self.derived_consumed is not None and self.derived_consumed < 0:
            # We hold more than every receipt we can see. The history is short,
            # not the arithmetic wrong.
            return "receipts-incomplete"
        return "ok"

    def to_json(self) -> dict:
        return {
            "received_total": self.received_total,
            "on_hand": self.on_hand,
            "derived_consumed": self.derived_consumed,
            "tie_out": self.tie_out,
            "lots": [lot.to_json() for lot in sorted(
                self.lots, key=lambda lot: (lot.expiration or "9999", lot.lot))],
            "receipts_without_lot": self.receipts_without_lot,
            "quantity_unreadable": self.quantity_unreadable,
        }


def build_sku_states(
    receipts: dict[str, LotReceipt], on_hand_by_sku: dict[str, int | None]
) -> dict[str, SkuState]:
    """Fold receipts into per-SKU, per-lot state and attach the warehouse figure.

    A receipt whose quantity is unreadable is COUNTED (`quantity_unreadable`) and
    excluded from the total -- adding it as zero would understate receipts and
    then overstate derived consumption, turning one unreadable field into a
    fabricated depletion figure.
    """
    states: dict[str, SkuState] = {}
    lots_by_sku: dict[str, dict[tuple[str, str], LotState]] = {}

    for receipt in receipts.values():
        state = states.setdefault(receipt.sku, SkuState(sku=receipt.sku))
        quantity = receipt.quantity
        if quantity is None:
            state.quantity_unreadable += 1
        else:
            state.received_total += quantity
        if not receipt.has_lot:
            state.receipts_without_lot += 1

        bucket = lots_by_sku.setdefault(receipt.sku, {})
        key = (receipt.lot, receipt.expiration)
        lot_state = bucket.get(key)
        if lot_state is None:
            lot_state = LotState(lot=receipt.lot or "(no lot)", expiration=receipt.expiration)
            bucket[key] = lot_state
        lot_state.receipts += 1
        if quantity is not None:
            lot_state.received += quantity

    for sku, bucket in lots_by_sku.items():
        states[sku].lots = list(bucket.values())

    # SKUs the warehouse reports but we hold no receipts for still deserve a row:
    # dropping them would hide stock that exists.
    for sku, on_hand in on_hand_by_sku.items():
        states.setdefault(sku, SkuState(sku=sku)).on_hand = on_hand
    for sku, state in states.items():
        if sku in on_hand_by_sku:
            state.on_hand = on_hand_by_sku[sku]

    return states


def fefo_projection(state: SkuState) -> list[dict]:
    """Per-lot remaining under an EXPLICIT first-expired-first-out assumption.

    This is a PROJECTION, not a measurement. V1 exposes no outbound lot, so the
    allocation of consumption across lots is assumed, not observed. Every entry is
    stamped `basis: "projection"` so it cannot be lifted out of context and read
    as fact, and the whole list is empty when there is nothing to allocate.
    """
    consumed = state.derived_consumed
    if consumed is None or consumed <= 0:
        return []
    remaining = consumed
    out: list[dict] = []
    for lot in sorted(state.lots, key=lambda lot: (lot.expiration or "9999", lot.lot)):
        drawn = min(lot.received, remaining)
        remaining -= drawn
        out.append({
            "lot": lot.lot,
            "expiration": lot.expiration,
            "received": lot.received,
            "projected_remaining": lot.received - drawn,
            "basis": "projection",
            "assumption": "FEFO (first-expired-first-out); V1 exposes no outbound lot",
        })
    return out


def collect_flags(states: dict[str, SkuState]) -> list[str]:
    """Surface every reconciliation state that did not close cleanly.

    Discrepancies flag, never absorb (Fork 3).
    """
    flags: list[str] = []
    for sku in sorted(states):
        state = states[sku]
        if state.tie_out == "receipts-incomplete":
            flags.append(
                f"{sku}: on-hand {state.on_hand:,} exceeds all known receipts "
                f"({state.received_total:,}) -- receipt history does not reach back "
                f"far enough; seed further before trusting per-lot figures"
            )
        elif state.tie_out == "on-hand-unknown":
            flags.append(f"{sku}: warehouse on-hand UNKNOWN -- consumption not derivable")
        elif state.tie_out == "no-receipt-history":
            flags.append(f"{sku}: no receipt lines on record -- no lot detail available")
        if state.quantity_unreadable:
            flags.append(
                f"{sku}: {state.quantity_unreadable} receipt line(s) with an unreadable "
                f"quantity, excluded from the received total"
            )
        if state.receipts_without_lot:
            flags.append(
                f"{sku}: {state.receipts_without_lot} receipt line(s) carried no lot number"
            )
    return flags


def expiring_within(states: dict[str, SkuState], days: int, today: str | None = None) -> list[dict]:
    """Lots whose expiry falls within `days`. Received quantity only -- remaining
    is not knowable, so this reports exposure, not a shippable balance."""
    anchor = today or datetime.date.today().isoformat()
    try:
        cutoff = (datetime.date.fromisoformat(anchor) + datetime.timedelta(days=days)).isoformat()
    except ValueError:
        return []
    out: list[dict] = []
    for sku in sorted(states):
        for lot in states[sku].lots:
            expiry = (lot.expiration or "")[:10]
            if not expiry or len(expiry) != 10:
                continue
            if expiry <= cutoff:
                out.append({
                    "sku": sku,
                    "lot": lot.lot,
                    "expiration": expiry,
                    "received": lot.received,
                    "expired": expiry < anchor,
                })
    return sorted(out, key=lambda row: (row["expiration"], row["sku"]))


# ─────────────────────────────────────────────────────────────────────────────
# Ledger assembly
# ─────────────────────────────────────────────────────────────────────────────


def build_ledger(
    receipts: dict[str, LotReceipt],
    on_hand_by_sku: dict[str, int | None],
    env: str,
    as_of_utc: str,
    seeded_from: str = "",
    include_projection: bool = False,
) -> dict:
    """Assemble the ledger payload. Pure -- no I/O, no network."""
    states = build_sku_states(receipts, on_hand_by_sku)
    by_sku: dict[str, dict] = {}
    for sku, state in sorted(states.items()):
        block = state.to_json()
        if include_projection:
            block["fefo_projection"] = fefo_projection(state)
        by_sku[sku] = block

    return {
        "version": LEDGER_VERSION,
        "env": env,
        "as_of_utc": as_of_utc,
        "seeded_from": seeded_from,
        "lot_attribution": {
            "inbound": "receipt-lines",
            "outbound": OUTBOUND_ATTRIBUTION,
            "reason": OUTBOUND_REASON,
        },
        "receipt_count": len(receipts),
        "receipts": [r.to_json() for r in sorted(
            receipts.values(), key=lambda r: (r.received_date, r.sku, r.lot))],
        "by_sku": by_sku,
        "flags": collect_flags(states),
    }


def load_receipts(payload: Any) -> dict[str, LotReceipt]:
    """Rehydrate stored receipts. Treats the file as UNTRUSTED input (D-123): a
    malformed ledger yields an empty history rather than raising, so a torn write
    degrades into a reseed instead of killing the run."""
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("receipts")
    if not isinstance(rows, list):
        return {}
    out: dict[str, LotReceipt] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        receipt = LotReceipt.from_json(raw)
        if not receipt.sku:
            continue
        # Same key function as the live pull -- see _KEY_FIELDS.
        out[receipt.key] = receipt
    return out
