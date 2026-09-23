"""Tests for the GET-only order-push preflight.

A `FakeClient` stands in for `deposco_client.DeposcoClient` so these tests
assert on the five checks without any network access -- and, per the module's
own contract, that a live-read failure propagates rather than being read as a
false "check failed"."""

from __future__ import annotations

import pytest

from cora.connectors import deposco_client as dc
from cora.deposco_orders import preflight as preflight_mod
from cora.deposco_orders import spec as spec_mod

SPEC = spec_mod.OrderSpec(
    channel="wholesale",
    buyer_or_fc_code="GOTHAM",
    reference="4471",
    authored_by="Harrison",
    lines=[
        spec_mod.OrderSpecLine(sku="PURE-Original", qty=208, unit_price="21.70"),
        spec_mod.OrderSpecLine(sku="PURE-Citrus", qty=208, unit_price="21.70"),
    ],
)


class FakeClient:
    def __init__(self, *, existing_order=None, reference_hits=None,
                 missing_items=(), atp=None, search_error=None, env="prod"):
        self._existing_order = existing_order
        self._reference_hits = reference_hits or []
        self._missing_items = set(missing_items)
        self._atp = atp or {}
        self._search_error = search_error
        self.item_exists_calls: list[str] = []
        self.env = env

    def find_order_detail(self, order_type, number):
        return self._existing_order

    def search_orders(self, order_type, **criteria):
        if self._search_error:
            raise self._search_error
        return dc.DeposcoResponse("prod", "/search/Order", 200, "<orders/>")

    def item_exists(self, item_number):
        self.item_exists_calls.append(item_number)
        return item_number not in self._missing_items

    def get_enterprise_availability(self, item_numbers=None, **kw):
        rows = [
            dc.EnterpriseInventoryRow(item_number=sku, measures={"atpQty": qty})
            for sku, qty in self._atp.items()
        ]
        return dc.AvailabilityResult(env="prod", rows=rows)


def _patch_reference_exists(monkeypatch, hit_refs):
    """`_reference_exists` is exercised for real via FakeClient.search_orders
    + the real parser in most tests; this helper is only for tests that need
    a deterministic hit without depending on XML parsing."""
    monkeypatch.setattr(
        preflight_mod, "_reference_exists",
        lambda client, reference: reference in hit_refs,
    )


class TestNumberMiss:
    def test_a_fresh_number_passes(self, monkeypatch):
        _patch_reference_exists(monkeypatch, [])
        client = FakeClient(existing_order=None, atp={"PURE-Original": 500, "PURE-Citrus": 500})
        result = preflight_mod.run_preflight(SPEC, client)
        check = next(c for c in result.checks if c.name == "number_miss")
        assert check.passed is True

    def test_an_existing_order_fails_the_check(self, monkeypatch):
        _patch_reference_exists(monkeypatch, [])
        existing = dc.OrderHeaderRecord(number="F3E-W-GOTHAM-4471")
        client = FakeClient(existing_order=existing, atp={"PURE-Original": 500, "PURE-Citrus": 500})
        result = preflight_mod.run_preflight(SPEC, client)
        check = next(c for c in result.checks if c.name == "number_miss")
        assert check.passed is False
        assert result.passed is False


class TestReferenceMiss:
    def test_no_matching_reference_passes(self):
        client = FakeClient(atp={"PURE-Original": 500, "PURE-Citrus": 500})
        result = preflight_mod.run_preflight(SPEC, client)
        check = next(c for c in result.checks if c.name == "reference_miss")
        assert check.passed is True

    def test_a_matching_reference_fails_even_with_a_different_number(self, monkeypatch):
        """Guards the scenario the number-miss check alone cannot: the same
        underlying PO already has an order, reachable under a different
        derived number (e.g. a buyer-code typo)."""

        def fake_reference_exists(client, reference):
            return reference == "4471"

        monkeypatch.setattr(preflight_mod, "_reference_exists", fake_reference_exists)
        client = FakeClient(atp={"PURE-Original": 500, "PURE-Citrus": 500})
        result = preflight_mod.run_preflight(SPEC, client)
        check = next(c for c in result.checks if c.name == "reference_miss")
        assert check.passed is False

    def test_a_search_failure_propagates_rather_than_reading_as_a_pass(self):
        """A preflight that CANNOT check must never silently pass."""
        client = FakeClient(search_error=dc.DeposcoUnavailable("network down"))
        with pytest.raises(dc.DeposcoUnavailable):
            preflight_mod.run_preflight(SPEC, client)


class TestItemsExist:
    def test_all_present_passes(self, monkeypatch):
        _patch_reference_exists(monkeypatch, [])
        client = FakeClient(atp={"PURE-Original": 500, "PURE-Citrus": 500})
        result = preflight_mod.run_preflight(SPEC, client)
        check = next(c for c in result.checks if c.name == "items_exist")
        assert check.passed is True
        assert set(client.item_exists_calls) == {"PURE-Original", "PURE-Citrus"}

    def test_a_missing_item_fails_and_names_it(self, monkeypatch):
        _patch_reference_exists(monkeypatch, [])
        client = FakeClient(missing_items={"PURE-Citrus"},
                            atp={"PURE-Original": 500, "PURE-Citrus": 500})
        result = preflight_mod.run_preflight(SPEC, client)
        check = next(c for c in result.checks if c.name == "items_exist")
        assert check.passed is False
        assert "PURE-Citrus" in check.detail


class TestAtpSufficiency:
    def test_sufficient_atp_passes(self, monkeypatch):
        _patch_reference_exists(monkeypatch, [])
        client = FakeClient(atp={"PURE-Original": 208, "PURE-Citrus": 300})
        result = preflight_mod.run_preflight(SPEC, client)
        check = next(c for c in result.checks if c.name == "atp_sufficient")
        assert check.passed is True

    def test_insufficient_atp_fails_and_shows_both_numbers(self, monkeypatch):
        _patch_reference_exists(monkeypatch, [])
        client = FakeClient(atp={"PURE-Original": 100, "PURE-Citrus": 300})
        result = preflight_mod.run_preflight(SPEC, client)
        check = next(c for c in result.checks if c.name == "atp_sufficient")
        assert check.passed is False
        assert "need 208" in check.detail and "ATP 100" in check.detail

    def test_unknown_atp_is_a_failure_not_a_pass(self, monkeypatch):
        """ABSENT IS NEVER ZERO, extended: an unreadable ATP must never be
        read as 'sufficient'."""
        _patch_reference_exists(monkeypatch, [])
        client = FakeClient(atp={"PURE-Original": 500})  # PURE-Citrus absent
        result = preflight_mod.run_preflight(SPEC, client)
        check = next(c for c in result.checks if c.name == "atp_sufficient")
        assert check.passed is False
        assert "ATP UNKNOWN" in check.detail

    def test_ua_insufficient_atp_does_not_block_staging(self, monkeypatch):
        """UA carries no inventory by design (Anthony, 8/5) -- an ATP
        shortfall there must never make R1/R6 structurally impossible."""
        _patch_reference_exists(monkeypatch, [])
        client = FakeClient(atp={"PURE-Original": -100, "PURE-Citrus": 0}, env="ua")
        result = preflight_mod.run_preflight(SPEC, client)
        check = next(c for c in result.checks if c.name == "atp_sufficient")
        assert check.passed is True
        assert "not enforced" in check.detail
        assert result.passed is True

    def test_prod_insufficient_atp_still_blocks(self, monkeypatch):
        _patch_reference_exists(monkeypatch, [])
        client = FakeClient(atp={"PURE-Original": 100, "PURE-Citrus": 300}, env="prod")
        result = preflight_mod.run_preflight(SPEC, client)
        check = next(c for c in result.checks if c.name == "atp_sufficient")
        assert check.passed is False
        assert result.passed is False


class TestShipViaPinned:
    def test_default_ship_via_is_the_pinned_value(self, monkeypatch):
        _patch_reference_exists(monkeypatch, [])
        client = FakeClient(atp={"PURE-Original": 500, "PURE-Citrus": 500})
        result = preflight_mod.run_preflight(SPEC, client)
        check = next(c for c in result.checks if c.name == "ship_via_pinned")
        assert check.passed is True

    def test_an_unconfirmed_ship_via_override_fails(self, monkeypatch):
        _patch_reference_exists(monkeypatch, [])
        spec_with_override = spec_mod.OrderSpec(
            channel="wholesale", buyer_or_fc_code="GOTHAM", reference="4471",
            authored_by="Harrison", ship_via="LTL",
            lines=[spec_mod.OrderSpecLine(sku="PURE-Original", qty=208, unit_price="21.70")],
        )
        client = FakeClient(atp={"PURE-Original": 500})
        result = preflight_mod.run_preflight(spec_with_override, client)
        check = next(c for c in result.checks if c.name == "ship_via_pinned")
        assert check.passed is False


class TestOverallResultAndAge:
    def test_all_checks_pass_means_overall_pass(self, monkeypatch):
        _patch_reference_exists(monkeypatch, [])
        client = FakeClient(atp={"PURE-Original": 500, "PURE-Citrus": 500})
        result = preflight_mod.run_preflight(SPEC, client)
        assert result.passed is True
        assert result.failed_checks() == []

    def test_any_single_failure_fails_the_whole_result(self, monkeypatch):
        _patch_reference_exists(monkeypatch, [])
        client = FakeClient(missing_items={"PURE-Original"},
                            atp={"PURE-Original": 500, "PURE-Citrus": 500})
        result = preflight_mod.run_preflight(SPEC, client)
        assert result.passed is False
        assert len(result.failed_checks()) == 1

    def test_checked_at_is_stamped_and_age_is_computable(self, monkeypatch):
        _patch_reference_exists(monkeypatch, [])
        client = FakeClient(atp={"PURE-Original": 500, "PURE-Citrus": 500})
        result = preflight_mod.run_preflight(SPEC, client)
        assert result.checked_at
        assert result.age_seconds() >= 0
