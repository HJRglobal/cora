"""Tests for the F3E lot ledger (design Fork 3).

The load-bearing group is `TestReceiptIdentity`: the ledger's whole value is that
`received_total` is a trustworthy number, and the one thing that can quietly
destroy it is a key mismatch between the live pull and the stored rehydrate --
which double-counts every historical receipt on the next run and then fabricates
consumption out of the inflated total. That defect existed in the first cut of
this module and these tests are what pin it shut.
"""

from __future__ import annotations

import pytest

from cora import deposco_lot_ledger as ll


class FakeLine:
    """Shape-compatible with deposco_client.ReceiptLine."""

    def __init__(self, order_number="PO1", line_number="PO1--1", receipt_number="1",
                 item_number="PURE-Original", quantity=100, received_date="2026-05-01",
                 lot_number="L-A", expiration_date="2027-01-15"):
        self.order_number = order_number
        self.line_number = line_number
        self.receipt_number = receipt_number
        self.item_number = item_number
        self.quantity = quantity
        self.received_date = received_date
        self.lot_number = lot_number
        self.expiration_date = expiration_date


def build(receipts, on_hand, **kw):
    return ll.build_ledger(receipts, on_hand, env="prod", as_of_utc="2026-08-14T00:00:00+00:00",
                           **kw)


# ── Receipt identity: the double-count guard ─────────────────────────────────


class TestReceiptIdentity:
    def test_key_uses_all_four_stable_fields(self):
        base = FakeLine()
        key = ll.receipt_key(base)
        assert key == "PO1|PO1--1|1|PURE-Original"
        for field, value in (("order_number", "PO2"), ("line_number", "PO1--2"),
                             ("receipt_number", "9"), ("item_number", "PURESL")):
            other = FakeLine(**{field: value})
            assert ll.receipt_key(other) != key, f"{field} must participate in the key"

    def test_stored_row_rehydrates_under_the_same_key_as_the_live_pull(self):
        """THE regression pin.

        A stored receipt must come back under the identical key the live pull
        produced. If it does not, `merge_receipts` treats it as a new row and the
        received total silently doubles.
        """
        live = ll.receipts_from_lines([FakeLine()])
        payload = build(live, {"PURE-Original": 40})
        rehydrated = ll.load_receipts(payload)
        assert set(rehydrated) == set(live), "rehydrated key must equal the live-pull key"

    def test_reload_then_remerge_does_not_double_the_received_total(self):
        """The consequence, asserted on the number an operator would actually read."""
        live = ll.receipts_from_lines([FakeLine(quantity=100)])
        first = build(live, {"PURE-Original": 40})
        assert first["by_sku"]["PURE-Original"]["received_total"] == 100

        # simulate the next day: reload the file, re-pull an overlapping window
        history = ll.load_receipts(first)
        merged, added, replaced = ll.merge_receipts(history, live)
        second = build(merged, {"PURE-Original": 40})

        assert second["by_sku"]["PURE-Original"]["received_total"] == 100
        assert (added, replaced) == (0, 1), "an overlapping re-pull corrects, never appends"

    def test_repeated_pulls_are_idempotent_across_many_cycles(self):
        receipts = ll.receipts_from_lines([FakeLine(), FakeLine(receipt_number="2", quantity=50)])
        payload = build(receipts, {"PURE-Original": 10})
        for _ in range(5):
            history = ll.load_receipts(payload)
            merged, _, _ = ll.merge_receipts(history, receipts)
            payload = build(merged, {"PURE-Original": 10})
        assert payload["receipt_count"] == 2
        assert payload["by_sku"]["PURE-Original"]["received_total"] == 150

    def test_line_number_survives_the_json_round_trip(self):
        """It is part of the key, so dropping it from the payload would break
        identity for two receipts that differ only by line."""
        receipts = ll.receipts_from_lines([
            FakeLine(line_number="PO1--1", quantity=10),
            FakeLine(line_number="PO1--2", quantity=20),
        ])
        assert len(receipts) == 2
        payload = build(receipts, {"PURE-Original": 5})
        assert all(row["line_number"] for row in payload["receipts"])
        assert len(ll.load_receipts(payload)) == 2

    def test_merge_reports_added_versus_replaced(self):
        first = ll.receipts_from_lines([FakeLine()])
        second = ll.receipts_from_lines([FakeLine(), FakeLine(receipt_number="2")])
        merged, added, replaced = ll.merge_receipts(first, second)
        assert (len(merged), added, replaced) == (2, 1, 1)


# ── The premise overturn: no outbound lot in V1 ──────────────────────────────


class TestOutboundAttribution:
    def test_ledger_records_that_lot_out_is_unavailable(self):
        payload = build({}, {})
        assert payload["lot_attribution"]["outbound"] == "unavailable-in-v1"
        assert payload["lot_attribution"]["inbound"] == "receipt-lines"
        assert "no shipment record carries a lot" in payload["lot_attribution"]["reason"].lower()

    def test_projection_is_opt_in(self):
        receipts = ll.receipts_from_lines([FakeLine(quantity=100)])
        assert "fefo_projection" not in build(receipts, {"PURE-Original": 40})["by_sku"][
            "PURE-Original"
        ]
        opted = build(receipts, {"PURE-Original": 40}, include_projection=True)
        assert "fefo_projection" in opted["by_sku"]["PURE-Original"]

    def test_projection_entries_are_labelled_as_projections(self):
        receipts = ll.receipts_from_lines([
            FakeLine(receipt_number="1", quantity=100, lot_number="OLD", expiration_date="2026-09-01"),
            FakeLine(receipt_number="2", quantity=100, lot_number="NEW", expiration_date="2027-09-01"),
        ])
        state = ll.build_sku_states(receipts, {"PURE-Original": 150})["PURE-Original"]
        projection = ll.fefo_projection(state)
        assert [p["lot"] for p in projection] == ["OLD", "NEW"]   # first-expired drawn first
        assert [p["projected_remaining"] for p in projection] == [50, 100]
        assert all(p["basis"] == "projection" for p in projection)
        assert all("FEFO" in p["assumption"] for p in projection)

    def test_projection_is_empty_when_nothing_is_consumed(self):
        receipts = ll.receipts_from_lines([FakeLine(quantity=100)])
        state = ll.build_sku_states(receipts, {"PURE-Original": 100})["PURE-Original"]
        assert ll.fefo_projection(state) == []

    def test_projection_is_empty_when_on_hand_is_unknown(self):
        receipts = ll.receipts_from_lines([FakeLine(quantity=100)])
        state = ll.build_sku_states(receipts, {"PURE-Original": None})["PURE-Original"]
        assert ll.fefo_projection(state) == []


# ── Tie-out honesty ──────────────────────────────────────────────────────────


class TestTieOut:
    def test_clean_tie_out(self):
        receipts = ll.receipts_from_lines([FakeLine(quantity=100)])
        block = build(receipts, {"PURE-Original": 40})["by_sku"]["PURE-Original"]
        assert block["tie_out"] == "ok"
        assert block["derived_consumed"] == 60

    def test_unknown_on_hand_yields_unknown_consumption_not_a_number(self):
        receipts = ll.receipts_from_lines([FakeLine(quantity=100)])
        block = build(receipts, {"PURE-Original": None})["by_sku"]["PURE-Original"]
        assert block["on_hand"] is None
        assert block["derived_consumed"] is None
        assert block["tie_out"] == "on-hand-unknown"

    def test_on_hand_exceeding_receipts_flags_rather_than_clamping(self):
        """Holding more than we can account for means the history is short. That
        must surface as 'seed further back', never be clamped to a clean zero."""
        receipts = ll.receipts_from_lines([FakeLine(quantity=10)])
        payload = build(receipts, {"PURE-Original": 500})
        block = payload["by_sku"]["PURE-Original"]
        assert block["tie_out"] == "receipts-incomplete"
        assert block["derived_consumed"] == -490          # preserved, not clamped
        assert any("does not reach back" in f for f in payload["flags"])

    def test_zero_on_hand_is_a_real_number_not_unknown(self):
        receipts = ll.receipts_from_lines([FakeLine(quantity=100)])
        block = build(receipts, {"PURE-Original": 0})["by_sku"]["PURE-Original"]
        assert block["on_hand"] == 0
        assert block["derived_consumed"] == 100
        assert block["tie_out"] == "ok"

    def test_unreadable_quantity_is_excluded_and_counted(self):
        """Treating it as 0 would understate receipts and overstate consumption --
        one unreadable field becoming a fabricated depletion number."""
        receipts = ll.receipts_from_lines([
            FakeLine(receipt_number="1", quantity=100),
            FakeLine(receipt_number="2", quantity=None),
        ])
        payload = build(receipts, {"PURE-Original": 40})
        block = payload["by_sku"]["PURE-Original"]
        assert block["received_total"] == 100
        assert block["quantity_unreadable"] == 1
        assert any("unreadable quantity" in f for f in payload["flags"])

    def test_receipt_without_a_lot_is_counted_and_flagged(self):
        receipts = ll.receipts_from_lines([FakeLine(lot_number="")])
        payload = build(receipts, {"PURE-Original": 0})
        assert payload["by_sku"]["PURE-Original"]["receipts_without_lot"] == 1
        assert any("no lot number" in f for f in payload["flags"])

    def test_warehouse_sku_with_no_receipts_still_gets_a_row(self):
        """Dropping it would hide stock that demonstrably exists."""
        payload = build({}, {"PURE-Citrus": 220})
        assert payload["by_sku"]["PURE-Citrus"]["on_hand"] == 220
        assert payload["by_sku"]["PURE-Citrus"]["tie_out"] == "no-receipt-history"
        assert any("no receipt lines" in f for f in payload["flags"])


# ── Lot rollup + expiry ──────────────────────────────────────────────────────


class TestLotRollup:
    def test_receipts_group_by_lot_and_expiry(self):
        receipts = ll.receipts_from_lines([
            FakeLine(receipt_number="1", quantity=10, lot_number="L1"),
            FakeLine(receipt_number="2", quantity=15, lot_number="L1"),
            FakeLine(receipt_number="3", quantity=20, lot_number="L2",
                     expiration_date="2027-06-01"),
        ])
        lots = build(receipts, {"PURE-Original": 5})["by_sku"]["PURE-Original"]["lots"]
        by_lot = {lot["lot"]: lot for lot in lots}
        assert by_lot["L1"]["received"] == 25 and by_lot["L1"]["receipt_count"] == 2
        assert by_lot["L2"]["received"] == 20

    def test_lots_sort_by_expiry(self):
        receipts = ll.receipts_from_lines([
            FakeLine(receipt_number="1", lot_number="LATE", expiration_date="2028-01-01"),
            FakeLine(receipt_number="2", lot_number="EARLY", expiration_date="2026-01-01"),
        ])
        lots = build(receipts, {"PURE-Original": 5})["by_sku"]["PURE-Original"]["lots"]
        assert [lot["lot"] for lot in lots] == ["EARLY", "LATE"]

    def test_missing_lot_renders_as_no_lot_bucket(self):
        receipts = ll.receipts_from_lines([FakeLine(lot_number="")])
        lots = build(receipts, {"PURE-Original": 0})["by_sku"]["PURE-Original"]["lots"]
        assert lots[0]["lot"] == "(no lot)"

    def test_expiring_within_window(self):
        receipts = ll.receipts_from_lines([
            FakeLine(receipt_number="1", lot_number="SOON", expiration_date="2026-08-20"),
            FakeLine(receipt_number="2", lot_number="LATER", expiration_date="2027-08-20"),
            FakeLine(receipt_number="3", lot_number="GONE", expiration_date="2026-08-01"),
        ])
        states = ll.build_sku_states(receipts, {"PURE-Original": 5})
        rows = ll.expiring_within(states, days=30, today="2026-08-14")
        assert [r["lot"] for r in rows] == ["GONE", "SOON"]
        assert rows[0]["expired"] is True and rows[1]["expired"] is False

    def test_expiring_within_skips_unparseable_dates(self):
        receipts = ll.receipts_from_lines([FakeLine(lot_number="X", expiration_date="")])
        states = ll.build_sku_states(receipts, {"PURE-Original": 5})
        assert ll.expiring_within(states, days=3650, today="2026-08-14") == []

    def test_expiry_datetimes_are_trimmed_to_a_date(self):
        receipts = ll.receipts_from_lines([
            FakeLine(lot_number="TZ", expiration_date="2026-08-20T00:00:00-04:00")
        ])
        states = ll.build_sku_states(receipts, {"PURE-Original": 5})
        rows = ll.expiring_within(states, days=30, today="2026-08-14")
        assert rows[0]["expiration"] == "2026-08-20"


# ── Untrusted input (D-123) ──────────────────────────────────────────────────


class TestUntrustedLedgerFile:
    @pytest.mark.parametrize(
        "payload", [None, "", 42, [], {}, {"receipts": "nope"}, {"receipts": [1, 2]}]
    )
    def test_malformed_file_yields_empty_history_not_a_crash(self, payload):
        assert ll.load_receipts(payload) == {}

    def test_rows_without_a_sku_are_dropped(self):
        assert ll.load_receipts({"receipts": [{"lot": "L1", "quantity": 5}]}) == {}

    def test_partial_row_still_rehydrates(self):
        loaded = ll.load_receipts({"receipts": [{"sku": "PURE-Citrus", "quantity": 7}]})
        assert list(loaded.values())[0].quantity == 7

    def test_lines_without_an_item_number_are_not_placed(self):
        assert ll.receipts_from_lines([FakeLine(item_number="")]) == {}


class TestLedgerEnvelope:
    def test_env_and_stamp_are_carried(self):
        payload = build({}, {})
        assert payload["env"] == "prod"
        assert payload["as_of_utc"] == "2026-08-14T00:00:00+00:00"
        assert payload["version"] == ll.LEDGER_VERSION

    def test_receipts_are_sorted_deterministically(self):
        receipts = ll.receipts_from_lines([
            FakeLine(receipt_number="2", received_date="2026-05-02"),
            FakeLine(receipt_number="1", received_date="2026-05-01"),
        ])
        dates = [row["received_date"] for row in build(receipts, {})["receipts"]]
        assert dates == sorted(dates)

    def test_no_flags_when_everything_reconciles(self):
        receipts = ll.receipts_from_lines([FakeLine(quantity=100)])
        assert build(receipts, {"PURE-Original": 40})["flags"] == []
