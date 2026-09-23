"""Tests for the pure payload builder.

Golden-file style: assert the exact shape the builder can and cannot emit,
per the SONNET-HANDOFF step 4 acceptance bar.
"""

from __future__ import annotations

import re

import pytest

from cora.deposco_orders import payload as payload_mod
from cora.deposco_orders import spec as spec_mod

WHOLESALE_SPEC = spec_mod.OrderSpec(
    channel="wholesale",
    buyer_or_fc_code="GOTHAM",
    reference="4471",
    authored_by="Harrison",
    freight_terms="Prepaid",
    notes="Confirm dock height before dispatch.",
    lines=[
        spec_mod.OrderSpecLine(sku="PURE-Original", qty=208, unit_price="21.70"),
        spec_mod.OrderSpecLine(sku="PURE-Citrus", qty=208, unit_price="21.70"),
        spec_mod.OrderSpecLine(sku="PURE-Tropical", qty=208, unit_price="21.70"),
        spec_mod.OrderSpecLine(sku="PURESL", qty=208, unit_price="21.70"),
    ],
)

FBA_SPEC = spec_mod.OrderSpec(
    channel="fba",
    buyer_or_fc_code="GEU3",
    reference="FBA19P6VCSJW",
    authored_by="Alex",
    reference2="779356-180",
    notes="Manufacturer barcode, no FNSKU. EXP 2028-08-12.",
    lines=[
        spec_mod.OrderSpecLine(sku="F3VPM4", qty=180, unit_price="32.99", msku="F3VPM"),
    ],
)


class TestOrderNumberDerivation:
    def test_wholesale_number_shape(self):
        assert payload_mod.build_order_number(WHOLESALE_SPEC) == "F3E-W-GOTHAM-4471"

    def test_fba_number_shape(self):
        assert payload_mod.build_order_number(FBA_SPEC) == "F3E-AMZ-FBA19P6VCSJW"

    def test_wfs_number_shape(self):
        wfs_spec = spec_mod.OrderSpec(
            channel="wfs", buyer_or_fc_code="WALFC1", reference="9480757WFA",
            authored_by="Harrison", lines=[],
        )
        assert payload_mod.build_order_number(wfs_spec) == "F3E-WMT-9480757WFA"

    def test_deterministic_and_idempotent(self):
        """Same spec -> same number, every time. This IS the idempotency
        argument: a retry can only ever collide with itself."""
        assert (payload_mod.build_order_number(WHOLESALE_SPEC)
                == payload_mod.build_order_number(WHOLESALE_SPEC))

    def test_number_is_at_most_30_chars_and_charset_restricted(self):
        number = payload_mod.build_order_number(WHOLESALE_SPEC)
        assert len(number) <= 30
        assert re.fullmatch(r"[A-Z0-9-]+", number)

    def test_oversized_derived_number_is_refused_not_truncated(self):
        long_spec = spec_mod.OrderSpec(
            channel="wholesale", buyer_or_fc_code="GOTHAM",
            reference="A" * 25, authored_by="Harrison", lines=[],
        )
        with pytest.raises(payload_mod.PayloadBuildError):
            payload_mod.build_order_number(long_spec)


class TestOrderTypeConstant:
    def test_type_is_always_sales_order(self):
        assert payload_mod.build_payload(WHOLESALE_SPEC)["order"][0]["type"] == "Sales Order"
        assert payload_mod.build_payload(FBA_SPEC)["order"][0]["type"] == "Sales Order"

    def test_builder_has_no_type_parameter(self):
        """There is no code path -- inspect the actual function signature --
        through which a caller could ask for a different order type."""
        import inspect
        sig = inspect.signature(payload_mod.build_payload)
        assert "type" not in sig.parameters
        assert "order_type" not in sig.parameters


class TestForbiddenShapes:
    """The proven flat-400 culprits, and the fields that are simply not SO
    line fields -- never emitted, by construction."""

    def test_no_channel_or_channels_key_anywhere(self):
        for spec_obj in (WHOLESALE_SPEC, FBA_SPEC):
            body = payload_mod.build_payload(spec_obj)
            order = body["order"][0]
            assert "channel" not in order
            assert "channels" not in order

    def test_no_line_level_notes(self):
        for spec_obj in (WHOLESALE_SPEC, FBA_SPEC):
            for line in payload_mod.build_payload(spec_obj)["order"][0]["orderLines"]["orderLine"]:
                assert "notes" not in line

    def test_no_lot_number_or_expiration_date_on_any_line(self):
        for spec_obj in (WHOLESALE_SPEC, FBA_SPEC):
            for line in payload_mod.build_payload(spec_obj)["order"][0]["orderLines"]["orderLine"]:
                assert "lotNumber" not in line
                assert "expirationDate" not in line

    def test_source_never_constructs_a_channel_block(self):
        import inspect
        source = inspect.getsource(payload_mod)
        assert '"channel"' not in source
        assert "'channel'" not in source


class TestWholesalePayloadShape:
    def test_header_fields(self):
        order = payload_mod.build_payload(WHOLESALE_SPEC)["order"][0]
        assert order["number"] == "F3E-W-GOTHAM-4471"
        assert order["orderSource"] == "F3E-API-WHOLESALE"
        assert order["secondaryOrderSource"] == "GOTHAM"
        assert order["otherReferenceNumber"] == "4471"
        assert order["customerOrderNumber"] == "4471"
        assert order["freight"] == {"termsType": "Prepaid"}
        assert order["shipVia"] == "General Freight"
        assert "otherReferenceNumber2" not in order  # blank for wholesale

    def test_ship_to_from_the_address_book_never_hand_typed(self):
        order = payload_mod.build_payload(WHOLESALE_SPEC)["order"][0]
        assert order["shipToAddress"]["name"] == "Gotham DSD"
        assert order["shipToAddress"]["postalCode"] == "11101"

    def test_bill_to_reuses_the_buyers_own_ap_contact(self):
        """The PROVEN UA shape (8/14, 201 + read-back) -- not an invented
        'F3 Energy LLC' address, unconfirmed anywhere in canon."""
        order = payload_mod.build_payload(WHOLESALE_SPEC)["order"][0]
        assert order["billToAddress"]["contactName"] == "Justin Moran (AP)"

    def test_lines_match_the_spec(self):
        order = payload_mod.build_payload(WHOLESALE_SPEC)["order"][0]
        lines = order["orderLines"]["orderLine"]
        assert len(lines) == 4
        assert [l["itemNumber"] for l in lines] == [
            "PURE-Original", "PURE-Citrus", "PURE-Tropical", "PURESL",
        ]
        assert lines[0]["orderPackQuantity"] == "208.0"
        assert lines[0]["unitPrice"] == "21.70"
        assert lines[0]["pack"] == {"type": "Each", "quantity": "1", "weight": "9.85"}

    def test_order_total_sums_the_lines(self):
        order = payload_mod.build_payload(WHOLESALE_SPEC)["order"][0]
        assert order["orderTotal"] == "18054.40"
        assert order["orderSubTotal"] == "18054.40"

    def test_order_total_is_decimal_exact_not_float_accumulated(self):
        """D-051 review, 2026-09-23: sum(qty * float(price)) rounded once at
        the end diverges from the exact decimal sum for ~4.9% of randomized
        multi-line combinations. This spec (odd quantities, mixed cent-level
        prices) is one of the reproducible cases against the OLD float code."""
        odd_spec = spec_mod.OrderSpec(
            channel="wholesale", buyer_or_fc_code="GOTHAM", reference="4473",
            authored_by="Harrison",
            lines=[
                spec_mod.OrderSpecLine(sku="PURE-Original", qty=3, unit_price="19.99"),
                spec_mod.OrderSpecLine(sku="PURE-Citrus", qty=7, unit_price="21.70"),
                spec_mod.OrderSpecLine(sku="PURE-Tropical", qty=11, unit_price="18.33"),
                spec_mod.OrderSpecLine(sku="PURESL", qty=13, unit_price="17.01"),
            ],
        )
        from decimal import Decimal
        order = payload_mod.build_payload(odd_spec)["order"][0]
        exact = sum(
            (Decimal(str(l.qty)) * Decimal(l.unit_price) for l in odd_spec.lines), Decimal("0"),
        )
        assert order["orderTotal"] == f"{exact:.2f}"

    def test_site_notes_folded_ahead_of_the_human_notes(self):
        order = payload_mod.build_payload(WHOLESALE_SPEC)["order"][0]
        body = order["notes"]["note"][0]["body"]
        assert "13 ft dock height" in body
        assert "Confirm dock height before dispatch." in body
        assert body.index("13 ft dock height") < body.index("Confirm dock height before dispatch.")


class TestFbaPayloadShape:
    """Golden file: matches the conventions read from the two real FBA SOs
    (2026-09-23 verification) -- ship-via, ship-to FC address, item/qty."""

    def test_header_fields(self):
        order = payload_mod.build_payload(FBA_SPEC)["order"][0]
        assert order["number"] == "F3E-AMZ-FBA19P6VCSJW"
        assert order["orderSource"] == "F3E-API-FBA"
        assert order["secondaryOrderSource"] == "AMAZON"
        assert order["otherReferenceNumber"] == "FBA19P6VCSJW"
        assert order["otherReferenceNumber2"] == "779356-180"
        assert order["shipVia"] == "General Freight"

    def test_ship_to_is_the_fc_address_from_the_address_book(self):
        order = payload_mod.build_payload(FBA_SPEC)["order"][0]
        assert order["shipToAddress"]["attention"] == "GEU3"
        assert order["shipToAddress"]["city"] == "Buckeye"

    def test_no_bill_to_address_for_fba(self):
        """No confirmed F3-side billing address exists in canon for a
        marketplace transfer -- omitted, not invented."""
        order = payload_mod.build_payload(FBA_SPEC)["order"][0]
        assert "billToAddress" not in order

    def test_single_mood_variety_line_matches_the_real_shipment(self):
        order = payload_mod.build_payload(FBA_SPEC)["order"][0]
        lines = order["orderLines"]["orderLine"]
        assert len(lines) == 1
        assert lines[0]["itemNumber"] == "F3VPM4"
        assert lines[0]["orderPackQuantity"] == "180.0"

    def test_notes_carry_the_fba_expiry_instruction_as_text(self):
        """lotNumber/expirationDate are not SO line fields -- FBA expiry
        rides order-level notes as text (8/6 design SS2b correction)."""
        order = payload_mod.build_payload(FBA_SPEC)["order"][0]
        assert "EXP 2028-08-12" in order["notes"]["note"][0]["body"]


class TestOptionalFields:
    def test_planned_ship_date_included_only_when_given(self):
        no_date = payload_mod.build_payload(WHOLESALE_SPEC)["order"][0]
        assert "plannedShipDate" not in no_date

        with_date = spec_mod.OrderSpec(
            channel="wholesale", buyer_or_fc_code="GOTHAM", reference="4472",
            authored_by="Harrison", planned_ship_date="2026-09-18",
            lines=[spec_mod.OrderSpecLine(sku="PURE-Original", qty=10, unit_price="21.70")],
        )
        assert payload_mod.build_payload(with_date)["order"][0]["plannedShipDate"] == "2026-09-18"
