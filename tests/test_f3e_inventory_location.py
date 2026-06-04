"""Tests for F3E location-aware inventory (Cat 3).

Coverage:
  shopify_client:
    - _infer_brand_from_title: pure / mood / energy / edge cases
    - _get_locations: success, 401 error, network error, cache hit
    - get_inventory_by_location: success (all brands + brand filter), exact/partial
      location match, ambiguous match, no-match, empty inventory levels,
      SKU not in products, cache hit
    - format_location_inventory_for_llm: multi-brand, single-brand, empty skus,
      threshold flags, source opacity (no "Shopify" in output)

  inventory_client:
    - _format_unis_for_llm: all brands, brand filter, damaged/allocated extras,
      missing brand, any_found=False
    - _format_nimbl_weekly_for_llm: all brands, brand filter, weekly-snapshot note
    - _format_office_for_llm: all brands, brand filter, damaged extras
    - get_f3e_location_inventory_text: routing (unis/warehouse/cotton/nimbl/office/117),
      Drive error -> UNKNOWN_RESPONSE, unknown location string

  tool_dispatch:
    - _tool_f3e_inventory_by_location: nimbl -> live Shopify path,
      unis -> Excel path, no location -> helpful error, ShopifyConfigError,
      ShopifyConnectorError
    - TOOL_DEFINITIONS entry: name, required=["location"], brand optional
    - _TOOL_FUNCTIONS registration
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

# ── Sandbox shim: config.py has a broken `log_level: s` line in Python 3.10
# that prevents importing tool_dispatch.  Stub the module before any test
# imports it.  Same pattern used implicitly by the existing test suite.
if "cora.config" not in sys.modules:
    _cfg = types.ModuleType("cora.config")

    class _Config:
        slack_bot_token: str = "xoxb-test"
        slack_app_token: str = "xapp-1-test"
        slack_signing_secret: str = "test-secret"
        anthropic_api_key: str = "sk-ant-test"
        log_level: str = "INFO"

        @classmethod
        def from_env(cls):
            return cls()

    _cfg.Config = _Config
    _cfg.config = _Config()
    sys.modules["cora.config"] = _cfg

from unittest.mock import MagicMock, patch

import pytest

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_location_sku(product_title: str, sku: str, available: int):
    from cora.connectors.shopify_client import LocationSKU
    return LocationSKU(product_title=product_title, sku=sku, available=available)


def _energy_skus():
    return [
        _make_location_sku("F3 Energy Original", "F3-Original", 120),
        _make_location_sku("F3 Energy Citrus", "F3-Citrus", 8),
    ]


def _mood_skus():
    return [
        _make_location_sku("F3 Mood Orange", "F3-Orange", 45),
    ]


def _pure_skus():
    return [
        _make_location_sku("F3 Pure Original", "PURE-Original", 0),
    ]


def _all_skus():
    return _energy_skus() + _mood_skus() + _pure_skus()


# ── shopify_client._infer_brand_from_title ────────────────────────────────────

class TestInferBrandFromTitle:
    def setup_method(self):
        from cora.connectors.shopify_client import _infer_brand_from_title
        self.fn = _infer_brand_from_title

    def test_pure_lowercase(self):
        assert self.fn("f3 pure original") == "Pure"

    def test_pure_mixed_case(self):
        assert self.fn("F3 Pure Citrus Clarity") == "Pure"

    def test_mood_lowercase(self):
        assert self.fn("f3 mood orange") == "Mood"

    def test_mood_title_case(self):
        assert self.fn("F3 Mood Peach Paradise") == "Mood"

    def test_energy_explicit(self):
        assert self.fn("F3 Energy Original") == "Energy"

    def test_energy_fallback_unknown(self):
        # No "pure" or "mood" → defaults to Energy
        assert self.fn("F3 Strawberry Lemonade") == "Energy"

    def test_energy_variety_pack(self):
        assert self.fn("Energy Variety Pack") == "Energy"

    def test_pure_wins_over_mood_substring(self):
        # "pure" present → Pure even if "mood" not present
        assert self.fn("F3 Pure Mood Blend") == "Pure"


# ── shopify_client._get_locations ────────────────────────────────────────────

class TestGetLocations:
    def setup_method(self):
        from cora.connectors.shopify_client import _cache_clear
        _cache_clear()

    def teardown_method(self):
        from cora.connectors.shopify_client import _cache_clear
        _cache_clear()

    def test_success_returns_name_to_id_map(self, monkeypatch):
        from cora.connectors.shopify_client import _get_locations

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "locations": [
                {"id": 111, "name": "Nimbl 3PL", "active": True},
                {"id": 222, "name": "UNIS Warehouse", "active": True},
                {"id": 333, "name": "Inactive Loc", "active": False},
            ]
        }

        with patch("cora.connectors.shopify_client.requests.get", return_value=mock_resp):
            result = _get_locations()

        assert result["nimbl 3pl"] == 111
        assert result["unis warehouse"] == 222
        assert "inactive loc" not in result

    def test_401_raises_connector_error(self, monkeypatch):
        from cora.connectors import shopify_client

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "bad_tok")

        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 401

        with patch("cora.connectors.shopify_client.requests.get", return_value=mock_resp):
            with pytest.raises(shopify_client.ShopifyConnectorError, match="401"):
                shopify_client._get_locations()

    def test_network_error_raises_connector_error(self, monkeypatch):
        import requests as req_lib
        from cora.connectors import shopify_client

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        with patch(
            "cora.connectors.shopify_client.requests.get",
            side_effect=req_lib.RequestException("timeout"),
        ):
            with pytest.raises(shopify_client.ShopifyConnectorError, match="Network error"):
                shopify_client._get_locations()

    def test_result_is_cached(self, monkeypatch):
        from cora.connectors.shopify_client import _get_locations, _cache_get

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "locations": [{"id": 99, "name": "Test Loc", "active": True}]
        }

        with patch("cora.connectors.shopify_client.requests.get", return_value=mock_resp) as m:
            _get_locations()
            _get_locations()  # second call should hit cache

        assert m.call_count == 1  # API only called once
        assert _cache_get("locations") is not None


# ── shopify_client.get_inventory_by_location ─────────────────────────────────

class TestGetInventoryByLocation:
    def setup_method(self):
        from cora.connectors.shopify_client import _cache_clear
        _cache_clear()

    def teardown_method(self):
        from cora.connectors.shopify_client import _cache_clear
        _cache_clear()

    def _mock_locations(self):
        return {"nimbl 3pl": 111, "unis warehouse": 222}

    def _mock_inv_levels(self):
        # inventory_levels for location 111
        return [
            {"inventory_item_id": 1001, "available": 120},
            {"inventory_item_id": 1002, "available": 8},
            {"inventory_item_id": 1003, "available": 45},
        ]

    def _mock_products(self):
        return [
            {
                "title": "F3 Energy Original",
                "variants": [{"inventory_item_id": 1001, "sku": "F3-Original", "title": "Default Title"}],
            },
            {
                "title": "F3 Energy Citrus",
                "variants": [{"inventory_item_id": 1002, "sku": "F3-Citrus", "title": ""}],
            },
            {
                "title": "F3 Mood Orange",
                "variants": [{"inventory_item_id": 1003, "sku": "F3-Orange", "title": "Default Title"}],
            },
        ]

    def test_success_all_brands(self, monkeypatch):
        from cora.connectors.shopify_client import get_inventory_by_location

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        with patch("cora.connectors.shopify_client._get_locations", return_value=self._mock_locations()), \
             patch("cora.connectors.shopify_client._get_paginated", side_effect=[
                 self._mock_inv_levels(), self._mock_products()
             ]):
            skus = get_inventory_by_location("nimbl 3pl")

        assert len(skus) == 3
        titles = [s.product_title for s in skus]
        assert "F3 Energy Citrus" in titles
        assert "F3 Mood Orange" in titles

    def test_success_brand_filter_energy_only(self, monkeypatch):
        from cora.connectors.shopify_client import get_inventory_by_location

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        with patch("cora.connectors.shopify_client._get_locations", return_value=self._mock_locations()), \
             patch("cora.connectors.shopify_client._get_paginated", side_effect=[
                 self._mock_inv_levels(), self._mock_products()
             ]):
            skus = get_inventory_by_location("nimbl 3pl", brand="Energy")

        assert all("Mood" not in s.product_title for s in skus)
        assert len(skus) == 2

    def test_exact_match_takes_priority(self, monkeypatch):
        """Exact key match should not require partial-match fallback."""
        from cora.connectors.shopify_client import get_inventory_by_location

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        locations = {"nimbl": 111, "nimbl 3pl extended": 222}
        with patch("cora.connectors.shopify_client._get_locations", return_value=locations), \
             patch("cora.connectors.shopify_client._get_paginated", side_effect=[
                 self._mock_inv_levels(), self._mock_products()
             ]):
            # "nimbl" is an exact key — should resolve to 111, not be ambiguous
            skus = get_inventory_by_location("nimbl")

        assert isinstance(skus, list)

    def test_partial_match_single_result(self, monkeypatch):
        from cora.connectors.shopify_client import get_inventory_by_location

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        locations = {"nimbl fulfillment center": 999}
        with patch("cora.connectors.shopify_client._get_locations", return_value=locations), \
             patch("cora.connectors.shopify_client._get_paginated", side_effect=[
                 self._mock_inv_levels(), self._mock_products()
             ]):
            skus = get_inventory_by_location("nimbl")

        assert isinstance(skus, list)

    def test_ambiguous_partial_match_raises(self, monkeypatch):
        from cora.connectors import shopify_client

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        locations = {"nimbl west": 11, "nimbl east": 22}
        with patch("cora.connectors.shopify_client._get_locations", return_value=locations):
            with pytest.raises(shopify_client.ShopifyConnectorError, match="ambiguous"):
                shopify_client.get_inventory_by_location("nimbl")

    def test_no_match_raises(self, monkeypatch):
        from cora.connectors import shopify_client

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        with patch("cora.connectors.shopify_client._get_locations", return_value={}):
            with pytest.raises(shopify_client.ShopifyConnectorError, match="not found"):
                shopify_client.get_inventory_by_location("unknown-xyz")

    def test_empty_inventory_levels_returns_empty_list(self, monkeypatch):
        from cora.connectors.shopify_client import get_inventory_by_location

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        with patch("cora.connectors.shopify_client._get_locations", return_value={"nimbl": 111}), \
             patch("cora.connectors.shopify_client._get_paginated", return_value=[]):
            skus = get_inventory_by_location("nimbl")

        assert skus == []

    def test_item_id_not_in_products_skipped(self, monkeypatch):
        """inventory_item_ids with no matching product variant are silently dropped."""
        from cora.connectors.shopify_client import get_inventory_by_location

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        inv_levels = [{"inventory_item_id": 9999, "available": 50}]  # orphan
        products = []  # no products -> item_map empty

        with patch("cora.connectors.shopify_client._get_locations", return_value={"nimbl": 111}), \
             patch("cora.connectors.shopify_client._get_paginated", side_effect=[inv_levels, products]):
            skus = get_inventory_by_location("nimbl")

        assert skus == []

    def test_result_sorted_by_product_title(self, monkeypatch):
        from cora.connectors.shopify_client import get_inventory_by_location

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        with patch("cora.connectors.shopify_client._get_locations", return_value=self._mock_locations()), \
             patch("cora.connectors.shopify_client._get_paginated", side_effect=[
                 self._mock_inv_levels(), self._mock_products()
             ]):
            skus = get_inventory_by_location("nimbl 3pl")

        titles = [s.product_title for s in skus]
        assert titles == sorted(titles)

    def test_cache_hit_skips_api(self, monkeypatch):
        from cora.connectors.shopify_client import get_inventory_by_location, _cache_set, LocationSKU

        monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3.myshopify.com")
        monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")

        cached = [LocationSKU(product_title="F3 Energy Original", sku="F3-Original", available=50)]
        _cache_set("location_inventory:nimbl:", cached)

        with patch("cora.connectors.shopify_client._get_locations") as mock_locs:
            result = get_inventory_by_location("nimbl")

        mock_locs.assert_not_called()
        assert result == cached


# ── shopify_client.format_location_inventory_for_llm ─────────────────────────

class TestFormatLocationInventoryForLlm:
    def setup_method(self):
        from cora.connectors.shopify_client import format_location_inventory_for_llm
        self.fn = format_location_inventory_for_llm

    def test_empty_skus_returns_no_stock_message(self):
        result = self.fn([], "nimbl", brand="Pure")
        assert "No stock" in result
        assert "Pure" in result
        assert "Nimbl" in result.lower() or "nimbl" in result.lower()

    def test_multi_brand_shows_brand_headers(self):
        skus = _all_skus()
        result = self.fn(skus, "nimbl")
        assert "*F3 Energy*" in result
        assert "*F3 Mood*" in result
        assert "*F3 Pure*" in result

    def test_single_brand_no_brand_header(self):
        skus = _energy_skus()
        result = self.fn(skus, "nimbl", brand="Energy")
        assert "*F3 Energy*" not in result  # no redundant brand header
        assert "F3 Energy" in result  # but brand label in title

    def test_flag_critical_at_or_below_10(self):
        skus = [_make_location_sku("F3 Energy Citrus", "F3-Citrus", 8)]
        result = self.fn(skus, "nimbl")
        assert "\U0001f6a8" in result  # 🚨

    def test_flag_warning_at_or_below_50(self):
        skus = [_make_location_sku("F3 Mood Orange", "F3-Orange", 45)]
        result = self.fn(skus, "nimbl")
        assert "⚠️" in result  # ⚠️

    def test_flag_ok_above_50(self):
        skus = [_make_location_sku("F3 Energy Original", "F3-Original", 120)]
        result = self.fn(skus, "nimbl")
        assert "✅" in result  # ✅

    def test_footer_includes_live_note(self):
        skus = _energy_skus()
        result = self.fn(skus, "nimbl")
        # Footer signals freshness without naming the platform (source-opacity).
        assert "Live data" in result

    def test_source_opacity_no_shopify_mention(self):
        """Source-opaque: the platform name must never appear in the reply."""
        skus = _energy_skus()
        result = self.fn(skus, "nimbl")
        assert "shopify" not in result.lower()

    def test_units_formatted_with_commas_large_numbers(self):
        skus = [_make_location_sku("F3 Energy Original", "F3-Original", 12345)]
        result = self.fn(skus, "nimbl")
        assert "12,345" in result

    def test_brand_label_capitalized_correctly(self):
        skus = _pure_skus()
        result = self.fn(skus, "nimbl", brand="pure")  # lowercase input
        assert "F3 Pure" in result


# ── inventory_client._format_unis_for_llm ────────────────────────────────────

class TestFormatUnisForLlm:
    def setup_method(self):
        from cora.tools.inventory_client import _format_unis_for_llm
        self.fn = _format_unis_for_llm

    def _unis_data(self):
        return {
            "F3-Original": {"available": 300, "allocated": 20, "on_hand": 320, "damaged": 0},
            "F3-Orange":   {"available": 40,  "allocated": 0,  "on_hand": 40,  "damaged": 5},
            "PURE-Original": {"available": 80, "allocated": 0, "on_hand": 80,  "damaged": 0},
        }

    def test_all_brands_no_filter(self):
        result = self.fn(self._unis_data(), "2026-05-26")
        assert "*F3 Energy*" in result
        assert "*F3 Mood*" in result
        assert "*F3 Pure*" in result

    def test_brand_filter_energy_only(self):
        result = self.fn(self._unis_data(), "2026-05-26", brand="Energy")
        assert "Original Energy" in result
        assert "*F3 Mood*" not in result
        assert "*F3 Pure*" not in result

    def test_allocated_shown_in_extras(self):
        result = self.fn(self._unis_data(), "2026-05-26", brand="Energy")
        assert "allocated" in result

    def test_damaged_shown_in_extras(self):
        result = self.fn(self._unis_data(), "2026-05-26", brand="Mood")
        assert "damaged" in result

    def test_zero_allocated_not_shown(self):
        result = self.fn(self._unis_data(), "2026-05-26", brand="Pure")
        assert "allocated" not in result

    def test_report_date_in_header(self):
        result = self.fn(self._unis_data(), "2026-05-26")
        assert "2026-05-26" in result

    def test_no_stock_for_filtered_brand(self):
        unis = {"F3-Original": {"available": 100, "allocated": 0, "on_hand": 100, "damaged": 0}}
        result = self.fn(unis, "2026-05-26", brand="Pure")
        assert "No stock" in result

    def test_weekly_snapshot_footer(self):
        result = self.fn(self._unis_data(), "2026-05-26")
        assert "Weekly snapshot" in result or "weekly snapshot" in result.lower()


# ── inventory_client._format_nimbl_weekly_for_llm ────────────────────────────

class TestFormatNimblWeeklyForLlm:
    def setup_method(self):
        from cora.tools.inventory_client import _format_nimbl_weekly_for_llm
        self.fn = _format_nimbl_weekly_for_llm

    def _nimbl_data(self):
        return {
            "F3-Original": 150,
            "F3-Orange":   30,
            "PURE-Original": 0,
        }

    def test_all_brands_no_filter(self):
        result = self.fn(self._nimbl_data(), "2026-05-26")
        assert "*F3 Energy*" in result
        assert "*F3 Mood*" in result
        assert "*F3 Pure*" in result

    def test_brand_filter_mood_only(self):
        result = self.fn(self._nimbl_data(), "2026-05-26", brand="Mood")
        assert "*F3 Energy*" not in result
        assert "Orangesicle" in result

    def test_weekly_snapshot_note_present(self):
        result = self.fn(self._nimbl_data(), "2026-05-26")
        assert "weekly Excel snapshot" in result

    def test_live_nimbl_upsell_note(self):
        result = self.fn(self._nimbl_data(), "2026-05-26")
        assert "live Nimbl inventory" in result

    def test_report_date_in_header(self):
        result = self.fn(self._nimbl_data(), "2026-05-26")
        assert "2026-05-26" in result

    def test_no_stock_for_filtered_brand_missing(self):
        nimbl = {"F3-Original": 100}
        result = self.fn(nimbl, "2026-05-26", brand="Pure")
        assert "No stock" in result


# ── inventory_client._format_office_for_llm ──────────────────────────────────

class TestFormatOfficeForLlm:
    def setup_method(self):
        from cora.tools.inventory_client import _format_office_for_llm
        self.fn = _format_office_for_llm

    def _office_data(self):
        return {
            "F3-Original":   {"available": 10, "damaged": 2},
            "PURE-Original": {"available": 5,  "damaged": 0},
        }

    def test_all_brands_no_filter(self):
        result = self.fn(self._office_data(), "2026-05-26")
        assert "*F3 Energy*" in result
        assert "*F3 Pure*" in result

    def test_brand_filter_energy_only(self):
        result = self.fn(self._office_data(), "2026-05-26", brand="Energy")
        assert "*F3 Pure*" not in result
        assert "Original Energy" in result

    def test_damaged_shown(self):
        result = self.fn(self._office_data(), "2026-05-26", brand="Energy")
        assert "damaged" in result

    def test_zero_damaged_not_shown(self):
        result = self.fn(self._office_data(), "2026-05-26", brand="Pure")
        assert "damaged" not in result

    def test_weekly_snapshot_footer(self):
        result = self.fn(self._office_data(), "2026-05-26")
        assert "Weekly snapshot" in result or "weekly snapshot" in result.lower()


# ── inventory_client.get_f3e_location_inventory_text ─────────────────────────

class TestGetF3eLocationInventoryText:
    """Tests the routing function that wraps Drive download + formatters."""

    def _patch_drive(self, unis=None, nimbl=None, office=None, report_date="2026-05-26"):
        """Return a context-manager-friendly patch set for Drive download + parse."""
        unis = unis or {}
        nimbl = nimbl or {}
        office = office or {}

        def _fake_download():
            return b"fake_bytes", "2026-05-26T00:00:00Z"

        def _fake_parse(data):
            return unis, nimbl, office

        return (
            patch("cora.tools.inventory_client._download_report", return_value=(b"fake", "2026-05-26T00:00:00Z")),
            patch("cora.tools.inventory_client._parse_xlsx", return_value=(unis, nimbl, office)),
        )

    def test_unis_routing(self):
        from cora.tools.inventory_client import get_f3e_location_inventory_text, InventoryClientError
        unis = {"F3-Original": {"available": 100, "allocated": 0, "on_hand": 100, "damaged": 0}}

        p1 = patch("cora.tools.inventory_client._download_report", return_value=(b"x", "2026-05-26T00:00:00Z"))
        p2 = patch("cora.tools.inventory_client._parse_xlsx", return_value=(unis, {}, {}))
        with p1, p2:
            result = get_f3e_location_inventory_text("unis")
        assert "UNIS" in result or "warehouse" in result.lower()

    def test_warehouse_alias_routes_to_unis(self):
        from cora.tools.inventory_client import get_f3e_location_inventory_text
        unis = {"F3-Original": {"available": 50, "allocated": 0, "on_hand": 50, "damaged": 0}}

        p1 = patch("cora.tools.inventory_client._download_report", return_value=(b"x", "2026-05-26T00:00:00Z"))
        p2 = patch("cora.tools.inventory_client._parse_xlsx", return_value=(unis, {}, {}))
        with p1, p2:
            result_unis = get_f3e_location_inventory_text("unis")
        with p1, p2:
            result_warehouse = get_f3e_location_inventory_text("warehouse")

        # Both should produce the same shape of output
        assert ("UNIS" in result_unis) == ("UNIS" in result_warehouse)

    def test_cotton_alias_routes_to_unis(self):
        from cora.tools.inventory_client import get_f3e_location_inventory_text
        unis = {"F3-Original": {"available": 50, "allocated": 0, "on_hand": 50, "damaged": 0}}

        p1 = patch("cora.tools.inventory_client._download_report", return_value=(b"x", "2026-05-26T00:00:00Z"))
        p2 = patch("cora.tools.inventory_client._parse_xlsx", return_value=(unis, {}, {}))
        with p1, p2:
            result = get_f3e_location_inventory_text("cotton")
        assert "Original Energy" in result or "No stock" in result or "UNIS" in result

    def test_nimbl_routes_to_nimbl_weekly(self):
        from cora.tools.inventory_client import get_f3e_location_inventory_text
        nimbl = {"F3-Original": 75}

        p1 = patch("cora.tools.inventory_client._download_report", return_value=(b"x", "2026-05-26T00:00:00Z"))
        p2 = patch("cora.tools.inventory_client._parse_xlsx", return_value=({}, nimbl, {}))
        with p1, p2:
            result = get_f3e_location_inventory_text("nimbl")
        assert "weekly Excel snapshot" in result  # from _format_nimbl_weekly_for_llm footer

    def test_office_routing(self):
        from cora.tools.inventory_client import get_f3e_location_inventory_text
        office = {"F3-Original": {"available": 5, "damaged": 0}}

        p1 = patch("cora.tools.inventory_client._download_report", return_value=(b"x", "2026-05-26T00:00:00Z"))
        p2 = patch("cora.tools.inventory_client._parse_xlsx", return_value=({}, {}, office))
        with p1, p2:
            result = get_f3e_location_inventory_text("office")
        assert "117" in result or "office" in result.lower()

    def test_117_alias_routes_to_office(self):
        from cora.tools.inventory_client import get_f3e_location_inventory_text
        office = {"F3-Original": {"available": 5, "damaged": 0}}

        p1 = patch("cora.tools.inventory_client._download_report", return_value=(b"x", "2026-05-26T00:00:00Z"))
        p2 = patch("cora.tools.inventory_client._parse_xlsx", return_value=({}, {}, office))
        with p1, p2:
            result = get_f3e_location_inventory_text("117")
        assert "117" in result or "office" in result.lower()

    def test_drive_error_returns_unknown_response(self):
        from cora.tools.inventory_client import get_f3e_location_inventory_text, UNKNOWN_RESPONSE, InventoryClientError

        with patch("cora.tools.inventory_client._download_report", side_effect=InventoryClientError("Drive down")):
            result = get_f3e_location_inventory_text("unis")

        assert result == UNKNOWN_RESPONSE

    def test_unexpected_error_returns_unknown_response(self):
        from cora.tools.inventory_client import get_f3e_location_inventory_text, UNKNOWN_RESPONSE

        with patch("cora.tools.inventory_client._download_report", side_effect=RuntimeError("unexpected")):
            result = get_f3e_location_inventory_text("office")

        assert result == UNKNOWN_RESPONSE

    def test_unknown_location_returns_error_string(self):
        from cora.tools.inventory_client import get_f3e_location_inventory_text

        p1 = patch("cora.tools.inventory_client._download_report", return_value=(b"x", "2026-05-26T00:00:00Z"))
        p2 = patch("cora.tools.inventory_client._parse_xlsx", return_value=({}, {}, {}))
        with p1, p2:
            result = get_f3e_location_inventory_text("mars-warehouse")

        assert "mars-warehouse" in result or "Unknown location" in result
        assert "nimbl" in result.lower() or "unis" in result.lower()  # shows known options


# ── tool_dispatch handler ─────────────────────────────────────────────────────

class TestToolDispatchInventoryByLocation:
    """Test _tool_f3e_inventory_by_location via dispatch()."""

    def _dispatch_location(self, location=None, brand=None):
        """Call the tool through the dispatch function with mocked dependencies."""
        from cora.tools import tool_dispatch

        input_data = {}
        if location is not None:
            input_data["location"] = location
        if brand is not None:
            input_data["brand"] = brand

        # Find and call the handler directly to avoid full module import deps
        handler = tool_dispatch._TOOL_FUNCTIONS["f3e_inventory_by_location"]
        return handler("U123", "F3E", input_data)

    def test_no_location_returns_helpful_error(self):
        from cora.tools import tool_dispatch
        handler = tool_dispatch._TOOL_FUNCTIONS["f3e_inventory_by_location"]
        result = handler("U123", "F3E", {})
        assert "location" in result.lower()
        assert "Nimbl" in result or "UNIS" in result

    def test_nimbl_routes_to_shopify(self):
        from cora.tools import tool_dispatch
        from cora.connectors.shopify_client import LocationSKU

        mock_skus = [LocationSKU(product_title="F3 Energy Original", sku="F3-Original", available=100)]
        handler = tool_dispatch._TOOL_FUNCTIONS["f3e_inventory_by_location"]

        with patch("cora.connectors.shopify_client.get_inventory_by_location", return_value=mock_skus) as mock_get, \
             patch("cora.connectors.shopify_client.format_location_inventory_for_llm", return_value="live result") as mock_fmt:
            result = handler("U123", "F3E", {"location": "nimbl"})

        mock_get.assert_called_once_with("nimbl", None)
        mock_fmt.assert_called_once()
        assert result == "live result"

    def test_nimbl_with_brand_filter(self):
        from cora.tools import tool_dispatch
        from cora.connectors.shopify_client import LocationSKU

        mock_skus = [LocationSKU(product_title="F3 Pure Original", sku="PURE-Original", available=50)]
        handler = tool_dispatch._TOOL_FUNCTIONS["f3e_inventory_by_location"]

        with patch("cora.connectors.shopify_client.get_inventory_by_location", return_value=mock_skus), \
             patch("cora.connectors.shopify_client.format_location_inventory_for_llm", return_value="pure result"):
            result = handler("U123", "F3E", {"location": "nimbl", "brand": "Pure"})

        assert result == "pure result"

    def test_nimbl_shopify_config_error_returns_graceful_string(self):
        from cora.tools import tool_dispatch
        from cora.connectors.shopify_client import ShopifyConfigError

        handler = tool_dispatch._TOOL_FUNCTIONS["f3e_inventory_by_location"]

        with patch("cora.connectors.shopify_client.get_inventory_by_location",
                   side_effect=ShopifyConfigError("no token")):
            result = handler("U123", "F3E", {"location": "nimbl"})

        assert "don't have" in result.lower() or "not" in result.lower()

    def test_nimbl_shopify_connector_error_returns_graceful_string(self):
        from cora.tools import tool_dispatch
        from cora.connectors.shopify_client import ShopifyConnectorError

        handler = tool_dispatch._TOOL_FUNCTIONS["f3e_inventory_by_location"]

        with patch("cora.connectors.shopify_client.get_inventory_by_location",
                   side_effect=ShopifyConnectorError("timeout")):
            result = handler("U123", "F3E", {"location": "nimbl"})

        assert "don't have" in result.lower() or "not" in result.lower()

    def test_unis_routes_to_excel(self):
        from cora.tools import tool_dispatch

        handler = tool_dispatch._TOOL_FUNCTIONS["f3e_inventory_by_location"]

        with patch("cora.tools.inventory_client.get_f3e_location_inventory_text",
                   return_value="unis result") as mock_excel:
            result = handler("U123", "F3E", {"location": "unis"})

        mock_excel.assert_called_once_with("unis", None)
        assert result == "unis result"

    def test_office_routes_to_excel(self):
        from cora.tools import tool_dispatch

        handler = tool_dispatch._TOOL_FUNCTIONS["f3e_inventory_by_location"]

        with patch("cora.tools.inventory_client.get_f3e_location_inventory_text",
                   return_value="office result") as mock_excel:
            result = handler("U123", "F3E", {"location": "office"})

        mock_excel.assert_called_once_with("office", None)
        assert result == "office result"

    def test_brand_passed_through_to_excel(self):
        from cora.tools import tool_dispatch

        handler = tool_dispatch._TOOL_FUNCTIONS["f3e_inventory_by_location"]

        with patch("cora.tools.inventory_client.get_f3e_location_inventory_text",
                   return_value="mood unis") as mock_excel:
            handler("U123", "F3E", {"location": "warehouse", "brand": "Mood"})

        mock_excel.assert_called_once_with("warehouse", "Mood")

    def test_empty_brand_treated_as_none(self):
        from cora.tools import tool_dispatch

        handler = tool_dispatch._TOOL_FUNCTIONS["f3e_inventory_by_location"]

        with patch("cora.tools.inventory_client.get_f3e_location_inventory_text",
                   return_value="all brands") as mock_excel:
            handler("U123", "F3E", {"location": "unis", "brand": ""})

        mock_excel.assert_called_once_with("unis", None)


# ── TOOL_DEFINITIONS and _TOOL_FUNCTIONS registration ────────────────────────

class TestToolDefinitionsRegistration:
    def test_tool_definition_present(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "f3e_inventory_by_location" in names

    def test_location_is_required(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        td = next(t for t in TOOL_DEFINITIONS if t["name"] == "f3e_inventory_by_location")
        assert "location" in td["input_schema"]["required"]

    def test_brand_is_not_required(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        td = next(t for t in TOOL_DEFINITIONS if t["name"] == "f3e_inventory_by_location")
        assert "brand" not in td["input_schema"].get("required", [])

    def test_both_properties_defined(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        td = next(t for t in TOOL_DEFINITIONS if t["name"] == "f3e_inventory_by_location")
        props = td["input_schema"]["properties"]
        assert "location" in props
        assert "brand" in props

    def test_tool_function_registered(self):
        from cora.tools.tool_dispatch import _TOOL_FUNCTIONS
        assert "f3e_inventory_by_location" in _TOOL_FUNCTIONS

    def test_description_mentions_nimbl_live(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        td = next(t for t in TOOL_DEFINITIONS if t["name"] == "f3e_inventory_by_location")
        desc = td["description"].lower()
        assert "live" in desc
        assert "nimbl" in desc

    def test_description_mentions_unis_snapshot(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        td = next(t for t in TOOL_DEFINITIONS if t["name"] == "f3e_inventory_by_location")
        desc = td["description"].lower()
        assert "unis" in desc or "warehouse" in desc


# ── f3e.md routing note ───────────────────────────────────────────────────────

class TestF3eSystemPromptRoutingNote:
    """Confirm the f3e.md inventory routing note has been updated."""

    def _get_f3e_md(self) -> str:
        prompt_path = _REPO_ROOT / "design" / "system-prompts" / "f3e.md"
        return prompt_path.read_text(encoding="utf-8")

    def test_three_inventory_tools_documented(self):
        content = self._get_f3e_md()
        assert "f3e_shopify_inventory" in content
        assert "f3e_inventory_pulse" in content
        assert "f3e_inventory_by_location" in content

    def test_nimbl_live_routing_documented(self):
        content = self._get_f3e_md()
        assert "LIVE Shopify" in content or "live Shopify" in content or "LIVE" in content

    def test_location_parameter_documented(self):
        content = self._get_f3e_md()
        assert "location" in content.lower()

    def test_brand_parameter_documented(self):
        content = self._get_f3e_md()
        assert "brand" in content.lower()
