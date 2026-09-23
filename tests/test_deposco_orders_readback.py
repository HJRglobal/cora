"""Tests for the D-110 read-back comparison."""

from __future__ import annotations

from cora.connectors import deposco_client as dc
from cora.deposco_orders import readback

PAYLOAD = {"order": [{
    "number": "F3E-W-GOTHAM-4471",
    "type": "Sales Order",
    "otherReferenceNumber": "4471",
    "shipToAddress": {"postalCode": "11101"},
    "orderLines": {"orderLine": [
        {"itemNumber": "PURE-Original", "orderPackQuantity": "208.0"},
        {"itemNumber": "PURE-Citrus", "orderPackQuantity": "208.0"},
    ]},
}]}


def _record(**kw):
    defaults = dict(
        number="F3E-W-GOTHAM-4471", customer_order_number="4471",
        ship_to_postal_code="11101",
        lines=[
            dc.OrderHeaderLine(item_number="PURE-Original", order_pack_quantity=208),
            dc.OrderHeaderLine(item_number="PURE-Citrus", order_pack_quantity=208),
        ],
    )
    defaults.update(kw)
    return dc.OrderHeaderRecord(**defaults)


class FakeClient:
    def __init__(self, records):
        self._records = list(records)  # returned in order, one per call

    def find_order_detail(self, order_type, number):
        if not self._records:
            return None
        return self._records.pop(0)


class TestExactMatch:
    def test_matching_order_is_clean(self):
        client = FakeClient([_record()])
        result = readback.read_back(client, PAYLOAD)
        assert result.found is True
        assert result.clean is True
        assert result.mismatches == []


class TestNotFound:
    def test_missing_order_is_not_found_and_not_clean(self):
        client = FakeClient([None])
        result = readback.read_back(client, PAYLOAD)
        assert result.found is False
        assert result.clean is False
        assert result.line_match is None


class TestLineMismatch:
    def test_wrong_quantity_is_a_mismatch(self):
        client = FakeClient([_record(lines=[
            dc.OrderHeaderLine(item_number="PURE-Original", order_pack_quantity=100),
            dc.OrderHeaderLine(item_number="PURE-Citrus", order_pack_quantity=208),
        ])])
        result = readback.read_back(client, PAYLOAD)
        assert result.found is True
        assert result.line_match is False
        assert result.clean is False
        assert "multiset differs" in result.mismatches[0]

    def test_a_stray_extra_line_is_a_mismatch(self):
        """The exact 081226 defect class: an extra line the pushed payload
        never had."""
        client = FakeClient([_record(lines=[
            dc.OrderHeaderLine(item_number="PURE-Original", order_pack_quantity=208),
            dc.OrderHeaderLine(item_number="PURE-Citrus", order_pack_quantity=208),
            dc.OrderHeaderLine(item_number="F3-Original", order_pack_quantity=1),
        ])])
        result = readback.read_back(client, PAYLOAD)
        assert result.line_match is False

    def test_a_missing_line_is_a_mismatch(self):
        client = FakeClient([_record(lines=[
            dc.OrderHeaderLine(item_number="PURE-Original", order_pack_quantity=208),
        ])])
        result = readback.read_back(client, PAYLOAD)
        assert result.line_match is False


class TestShipToAndReferenceMismatch:
    def test_wrong_postal_code_is_a_mismatch(self):
        client = FakeClient([_record(ship_to_postal_code="90210")])
        result = readback.read_back(client, PAYLOAD)
        assert result.ship_to_postal_match is False
        assert result.clean is False

    def test_wrong_reference_is_a_mismatch(self):
        client = FakeClient([_record(customer_order_number="9999")])
        result = readback.read_back(client, PAYLOAD)
        assert result.reference_match is False
        assert result.clean is False

    def test_line_match_alone_does_not_make_it_clean(self):
        """Every dimension must independently pass -- lines matching does not
        excuse a changed ship-to."""
        client = FakeClient([_record(ship_to_postal_code="00000")])
        result = readback.read_back(client, PAYLOAD)
        assert result.line_match is True
        assert result.clean is False


class TestRetries:
    def test_found_on_first_attempt_does_not_retry(self):
        client = FakeClient([_record()])
        calls = []
        result = readback.read_back_with_retries(
            client, PAYLOAD, attempts=3, sleep=lambda s: calls.append(s),
        )
        assert result.found is True
        assert calls == []

    def test_found_on_a_later_attempt_after_not_found(self):
        client = FakeClient([None, None, _record()])
        calls = []
        result = readback.read_back_with_retries(
            client, PAYLOAD, attempts=3, delay_seconds=1, sleep=lambda s: calls.append(s),
        )
        assert result.found is True
        assert len(calls) == 2

    def test_exhausting_retries_returns_not_found(self):
        """This IS the 'a 201 whose read-back misses after retries' UNKNOWN
        path (R4) -- handler.py classifies on `result.found is False`."""
        client = FakeClient([None, None, None])
        result = readback.read_back_with_retries(
            client, PAYLOAD, attempts=3, delay_seconds=0, sleep=lambda s: None,
        )
        assert result.found is False

    def test_a_found_but_not_matching_result_is_never_retried_away(self):
        """A MISMATCH signal must survive, never be silently retried into a
        different-looking outcome."""
        client = FakeClient([_record(ship_to_postal_code="00000")])
        calls = []
        result = readback.read_back_with_retries(
            client, PAYLOAD, attempts=3, sleep=lambda s: calls.append(s),
        )
        assert result.found is True
        assert result.clean is False
        assert calls == []
