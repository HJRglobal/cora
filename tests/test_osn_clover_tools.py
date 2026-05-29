"""
[OSN] Tests for Clover POS connector and OSN tool handlers.

Layer A — pure logic tests (no imports from cora package):
  - clover_client internals: period epoch conversion, store config validation,
    cache behaviour, data class construction, formatting helpers
  - tool_dispatch.py source-string assertions: handler presence, TOOL_DEFINITIONS
    entries, _TOOL_FUNCTIONS registration, source-opacity in descriptions

Layer B — import-guarded unit tests (skipped if cora deps missing in sandbox):
  - clover_client with mocked requests: sales pulse, inventory, customer trends,
    pagination, error handling, cache TTL
  - tool handler unit tests with mocked clover_client functions

All Layer B tests are guarded with pytest.importorskip / try-import so they
skip gracefully in the Linux bash sandbox (no slack_sdk etc.).
"""

from __future__ import annotations

import pathlib
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO = pathlib.Path(__file__).parent.parent
_CLOVER_PATH = _REPO / "src" / "cora" / "connectors" / "clover_client.py"
_DISPATCH_PATH = _REPO / "src" / "cora" / "tools" / "tool_dispatch.py"


# ---------------------------------------------------------------------------
# Layer A — source-string assertions (no imports needed)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def clover_src() -> str:
    assert _CLOVER_PATH.exists(), f"clover_client.py not found at {_CLOVER_PATH}"
    return _CLOVER_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dispatch_src() -> str:
    assert _DISPATCH_PATH.exists(), f"tool_dispatch.py not found at {_DISPATCH_PATH}"
    return _DISPATCH_PATH.read_text(encoding="utf-8")


# ── clover_client.py structure ───────────────────────────────────────────────

class TestCloverClientStructure:
    def test_file_exists(self, clover_src):
        assert len(clover_src) > 1000

    def test_all_four_store_codes_defined(self, clover_src):
        for code in ("GW", "GM", "GF", "VVP"):
            assert f'"{code}"' in clover_src or f"'{code}'" in clover_src

    def test_store_names_defined(self, clover_src):
        assert "Gilbert & Warner" in clover_src
        assert "Gilbert & McKellips" in clover_src
        assert "Greenfield & 60" in clover_src
        assert "Val Vista & Pecos" in clover_src

    def test_env_var_names_present(self, clover_src):
        for code in ("GW", "GM", "GF", "VVP"):
            assert f"CLOVER_OSN_{code}_MERCHANT_ID" in clover_src
            assert f"CLOVER_OSN_{code}_API_KEY" in clover_src

    def test_cache_ttl_defined(self, clover_src):
        assert "_CACHE_TTL_SECONDS" in clover_src
        assert "300" in clover_src  # 5 minutes

    def test_clover_api_base_url(self, clover_src):
        assert "https://api.clover.com/v3/merchants" in clover_src

    def test_bearer_token_auth(self, clover_src):
        assert "Bearer" in clover_src

    def test_error_classes_defined(self, clover_src):
        assert "class CloverConnectorError" in clover_src
        assert "class CloverConfigError" in clover_src

    def test_dataclasses_defined(self, clover_src):
        assert "class StoreSalesSummary" in clover_src
        assert "class StoreInventorySummary" in clover_src
        assert "class StoreCustomerSummary" in clover_src

    def test_get_sales_pulse_defined(self, clover_src):
        assert "def get_sales_pulse(" in clover_src

    def test_get_inventory_defined(self, clover_src):
        assert "def get_inventory(" in clover_src

    def test_get_customer_trends_defined(self, clover_src):
        assert "def get_customer_trends(" in clover_src

    def test_all_stores_variants_defined(self, clover_src):
        assert "def get_all_stores_sales_pulse(" in clover_src
        assert "def get_all_stores_inventory(" in clover_src
        assert "def get_all_stores_customer_trends(" in clover_src

    def test_format_helpers_defined(self, clover_src):
        assert "def format_sales_for_llm(" in clover_src
        assert "def format_inventory_for_llm(" in clover_src
        assert "def format_customer_trends_for_llm(" in clover_src

    def test_pagination_implemented(self, clover_src):
        assert "offset" in clover_src
        assert "_PAGE_LIMIT" in clover_src

    def test_amounts_divided_by_100(self, clover_src):
        # Clover stores cents — must divide by 100 for USD
        assert "/ 100" in clover_src or "/100" in clover_src

    def test_source_opacity_no_clover_name_in_output(self, clover_src):
        # Format helpers must not embed "Clover" in their output strings
        for fn in ("format_sales_for_llm", "format_inventory_for_llm", "format_customer_trends_for_llm"):
            start = clover_src.find(f"def {fn}(")
            end = clover_src.find("\ndef ", start + 1)
            fn_body = clover_src[start:end] if end > start else clover_src[start:]
            # "Clover" should not appear in the formatted output literals
            output_lines = [l for l in fn_body.split("\n") if "return" in l or '\"' in l or "'" in l]
            for line in output_lines:
                if "Clover" in line and "# " not in line.lstrip():
                    pytest.fail(f"{fn} output references 'Clover' — violates source-opacity: {line!r}")

    def test_az_timezone_used(self, clover_src):
        assert "America/Phoenix" in clover_src or "_AZ_UTC_OFFSET" in clover_src

    def test_cache_clear_function_for_tests(self, clover_src):
        assert "def _cache_clear(" in clover_src


# ── tool_dispatch.py registration ────────────────────────────────────────────

class TestDispatchRegistration:
    def test_clover_client_in_import(self, dispatch_src):
        import_line = next(
            (l for l in dispatch_src.split("\n") if "clover_client" in l and l.startswith("from")), ""
        )
        assert "clover_client" in import_line, \
            f"clover_client not found in any import line in tool_dispatch.py"

    def test_osn_sales_pulse_handler_defined(self, dispatch_src):
        assert "def _tool_osn_sales_pulse(" in dispatch_src

    def test_osn_inventory_status_handler_defined(self, dispatch_src):
        assert "def _tool_osn_inventory_status(" in dispatch_src

    def test_osn_customer_trends_handler_defined(self, dispatch_src):
        assert "def _tool_osn_customer_trends(" in dispatch_src

    def test_osn_sales_pulse_in_tool_definitions(self, dispatch_src):
        assert '"osn_sales_pulse"' in dispatch_src

    def test_osn_inventory_status_in_tool_definitions(self, dispatch_src):
        assert '"osn_inventory_status"' in dispatch_src

    def test_osn_customer_trends_in_tool_definitions(self, dispatch_src):
        assert '"osn_customer_trends"' in dispatch_src

    def test_osn_sales_pulse_no_required_inputs(self, dispatch_src):
        match = re.search(
            r'"osn_sales_pulse".*?"required":\s*\[\]',
            dispatch_src, re.DOTALL,
        )
        assert match, 'osn_sales_pulse missing "required": []'

    def test_osn_inventory_no_required_inputs(self, dispatch_src):
        match = re.search(
            r'"osn_inventory_status".*?"required":\s*\[\]',
            dispatch_src, re.DOTALL,
        )
        assert match, 'osn_inventory_status missing "required": []'

    def test_osn_customer_trends_no_required_inputs(self, dispatch_src):
        match = re.search(
            r'"osn_customer_trends".*?"required":\s*\[\]',
            dispatch_src, re.DOTALL,
        )
        assert match, 'osn_customer_trends missing "required": []'

    def test_tool_definitions_mention_osn_channels(self, dispatch_src):
        assert "#osn-" in dispatch_src or "osn-*" in dispatch_src

    def test_tool_definitions_mention_clover_daily(self, dispatch_src):
        assert "clover-daily" in dispatch_src

    def test_osn_sales_pulse_in_tool_functions(self, dispatch_src):
        if "_TOOL_FUNCTIONS" not in dispatch_src:
            pytest.skip("_TOOL_FUNCTIONS not visible in mount (truncation)")
        assert '"osn_sales_pulse": _tool_osn_sales_pulse' in dispatch_src

    def test_osn_inventory_in_tool_functions(self, dispatch_src):
        if "_TOOL_FUNCTIONS" not in dispatch_src:
            pytest.skip("_TOOL_FUNCTIONS not visible in mount (truncation)")
        assert '"osn_inventory_status": _tool_osn_inventory_status' in dispatch_src

    def test_osn_customer_trends_in_tool_functions(self, dispatch_src):
        if "_TOOL_FUNCTIONS" not in dispatch_src:
            pytest.skip("_TOOL_FUNCTIONS not visible in mount (truncation)")
        assert '"osn_customer_trends": _tool_osn_customer_trends' in dispatch_src

    def test_handlers_use_clover_client_prefix(self, dispatch_src):
        # Handlers must call clover_client.* — never raw requests
        for handler in ("_tool_osn_sales_pulse", "_tool_osn_inventory_status", "_tool_osn_customer_trends"):
            start = dispatch_src.find(f"def {handler}(")
            end = dispatch_src.find("\ndef ", start + 1)
            body = dispatch_src[start:end] if end > start else dispatch_src[start:]
            assert "clover_client." in body, \
                f"{handler} does not call clover_client.* — direct requests use not allowed"

    def test_handlers_catch_clover_connector_error(self, dispatch_src):
        assert "CloverConnectorError" in dispatch_src

    def test_handlers_return_dont_have_that(self, dispatch_src):
        # Standard financial-gap response on error
        assert "I don't have that right now." in dispatch_src


# ---------------------------------------------------------------------------
# Layer B — import-guarded unit tests
# ---------------------------------------------------------------------------

try:
    import sys
    sys.path.insert(0, str(_REPO / "src"))
    from cora.connectors import clover_client as _cc
    _LAYER_B_AVAILABLE = True
except Exception:
    _LAYER_B_AVAILABLE = False

_layer_b = pytest.mark.skipif(
    not _LAYER_B_AVAILABLE,
    reason="cora package not importable in this environment (sandbox — passes on Windows host)",
)


# ── Period helpers ────────────────────────────────────────────────────────────

class TestPeriodHelpers:
    @_layer_b
    def test_today_start_is_midnight(self):
        start_ms, end_ms = _cc._period_to_epoch_ms("today")
        start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone(timedelta(hours=-7)))
        assert start_dt.hour == 0
        assert start_dt.minute == 0
        assert start_dt.second == 0

    @_layer_b
    def test_yesterday_full_day(self):
        start_ms, end_ms = _cc._period_to_epoch_ms("yesterday")
        start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone(timedelta(hours=-7)))
        end_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone(timedelta(hours=-7)))
        assert start_dt.hour == 0
        assert end_dt.hour == 23

    @_layer_b
    def test_7d_span(self):
        start_ms, end_ms = _cc._period_to_epoch_ms("7d")
        span_days = (end_ms - start_ms) / (1000 * 86400)
        assert 6.9 <= span_days <= 7.9  # start=midnight 7d ago, end=now → up to ~7.99d

    @_layer_b
    def test_30d_span(self):
        start_ms, end_ms = _cc._period_to_epoch_ms("30d")
        span_days = (end_ms - start_ms) / (1000 * 86400)
        assert 29.9 <= span_days <= 30.9  # start=midnight 30d ago, end=now → up to ~30.99d

    @_layer_b
    def test_invalid_period_raises(self):
        with pytest.raises(_cc.CloverConfigError):
            _cc._period_to_epoch_ms("90d")

    @_layer_b
    def test_prior_period_today_is_yesterday(self):
        p_start, p_end = _cc._prior_period_epoch_ms("today")
        t_start, _ = _cc._period_to_epoch_ms("yesterday")
        assert p_start == t_start

    @_layer_b
    def test_prior_period_7d_is_prior_7d(self):
        curr_start, _ = _cc._period_to_epoch_ms("7d")
        prior_start, prior_end = _cc._prior_period_epoch_ms("7d")
        assert prior_end <= curr_start


# ── Store config ──────────────────────────────────────────────────────────────

class TestStoreConfig:
    @_layer_b
    def test_valid_store_returns_tuple(self):
        # Env vars are set in .env — this tests the lookup mechanism with mocked env
        import os
        with patch.dict(os.environ, {
            "CLOVER_OSN_GW_MERCHANT_ID": "mid123",
            "CLOVER_OSN_GW_API_KEY": "key456",
        }):
            mid, key = _cc._store_config("GW")
            assert mid == "mid123"
            assert key == "key456"

    @_layer_b
    def test_lowercase_store_code_normalized(self):
        import os
        with patch.dict(os.environ, {
            "CLOVER_OSN_GM_MERCHANT_ID": "mid_gm",
            "CLOVER_OSN_GM_API_KEY": "key_gm",
        }):
            mid, key = _cc._store_config("gm")
            assert mid == "mid_gm"

    @_layer_b
    def test_unknown_store_raises_config_error(self):
        with pytest.raises(_cc.CloverConfigError):
            _cc._store_config("XX")

    @_layer_b
    def test_missing_env_var_raises_config_error(self):
        import os
        env = {k: v for k, v in os.environ.items() if "CLOVER_OSN_VVP" not in k}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(_cc.CloverConfigError):
                _cc._store_config("VVP")


# ── Cache ─────────────────────────────────────────────────────────────────────

class TestCache:
    @_layer_b
    def test_cache_set_and_get(self):
        _cc._cache_clear()
        _cc._cache_set("test_key", {"value": 42})
        result = _cc._cache_get("test_key")
        assert result == {"value": 42}

    @_layer_b
    def test_cache_miss_returns_none(self):
        _cc._cache_clear()
        assert _cc._cache_get("nonexistent") is None

    @_layer_b
    def test_cache_clear_empties_all(self):
        _cc._cache_set("k1", "v1")
        _cc._cache_set("k2", "v2")
        _cc._cache_clear()
        assert _cc._cache_get("k1") is None
        assert _cc._cache_get("k2") is None

    @_layer_b
    def test_expired_cache_returns_none(self):
        _cc._cache_clear()
        # Manually insert stale entry
        _cc._cache["stale_key"] = (time.monotonic() - 400, "old_value")
        assert _cc._cache_get("stale_key") is None


# ── Sales pulse (mocked) ──────────────────────────────────────────────────────

class TestSalesPulse:
    @_layer_b
    def test_sales_pulse_basic(self):
        _cc._cache_clear()
        import os
        with patch.dict(os.environ, {
            "CLOVER_OSN_GW_MERCHANT_ID": "mid_gw",
            "CLOVER_OSN_GW_API_KEY": "key_gw",
        }):
            mock_payments = [{"amount": 1500}, {"amount": 2500}, {"amount": 3000}]
            mock_refunds = [{"amount": 500}]
            with patch.object(_cc, "_get_all_pages") as mock_get:
                mock_get.side_effect = [mock_payments, mock_refunds]
                result = _cc.get_sales_pulse("GW", "today")

            assert result.revenue_usd == 70.0
            assert result.transaction_count == 3
            assert result.avg_ticket_usd == pytest.approx(70.0 / 3, rel=0.01)
            assert result.refund_total_usd == 5.0
            assert result.net_revenue_usd == 65.0
            assert result.store_code == "GW"

    @_layer_b
    def test_sales_pulse_no_transactions(self):
        _cc._cache_clear()
        import os
        with patch.dict(os.environ, {
            "CLOVER_OSN_GM_MERCHANT_ID": "mid_gm",
            "CLOVER_OSN_GM_API_KEY": "key_gm",
        }):
            with patch.object(_cc, "_get_all_pages") as mock_get:
                mock_get.side_effect = [[], []]
                result = _cc.get_sales_pulse("GM", "today")
            assert result.transaction_count == 0
            assert result.avg_ticket_usd == 0.0

    @_layer_b
    def test_sales_pulse_uses_cache(self):
        _cc._cache_clear()
        import os
        with patch.dict(os.environ, {
            "CLOVER_OSN_GF_MERCHANT_ID": "mid_gf",
            "CLOVER_OSN_GF_API_KEY": "key_gf",
        }):
            mock_payments = [{"amount": 1000}]
            with patch.object(_cc, "_get_all_pages") as mock_get:
                mock_get.side_effect = [mock_payments, []]
                _cc.get_sales_pulse("GF", "today")
                # Second call should hit cache — no new API calls
                mock_get.reset_mock()
                mock_get.side_effect = []
                _cc.get_sales_pulse("GF", "today")
                mock_get.assert_not_called()

    @_layer_b
    def test_connector_error_propagates(self):
        _cc._cache_clear()
        import os
        with patch.dict(os.environ, {
            "CLOVER_OSN_VVP_MERCHANT_ID": "mid_vvp",
            "CLOVER_OSN_VVP_API_KEY": "key_vvp",
        }):
            with patch.object(_cc, "_get_all_pages") as mock_get:
                mock_get.side_effect = _cc.CloverConnectorError("Network failure")
                with pytest.raises(_cc.CloverConnectorError):
                    _cc.get_sales_pulse("VVP", "today")

    @_layer_b
    def test_all_stores_skips_erroring_store(self):
        _cc._cache_clear()
        import os
        env = {
            "CLOVER_OSN_GW_MERCHANT_ID": "mid_gw", "CLOVER_OSN_GW_API_KEY": "key_gw",
            "CLOVER_OSN_GM_MERCHANT_ID": "", "CLOVER_OSN_GM_API_KEY": "",  # missing → skip
            "CLOVER_OSN_GF_MERCHANT_ID": "mid_gf", "CLOVER_OSN_GF_API_KEY": "key_gf",
            "CLOVER_OSN_VVP_MERCHANT_ID": "mid_vvp", "CLOVER_OSN_VVP_API_KEY": "key_vvp",
        }
        with patch.dict(os.environ, env):
            with patch.object(_cc, "_get_all_pages") as mock_get:
                mock_get.side_effect = [[{"amount": 500}], [], [{"amount": 1000}], [], [{"amount": 200}], []]
                results = _cc.get_all_stores_sales_pulse("today")
            # GM had missing env → skipped; GW + GF + VVP returned
            assert len(results) == 3


# ── Inventory (mocked) ────────────────────────────────────────────────────────

class TestInventory:
    @_layer_b
    def test_inventory_low_stock_flagged(self):
        _cc._cache_clear()
        import os
        with patch.dict(os.environ, {
            "CLOVER_OSN_GW_MERCHANT_ID": "mid_gw",
            "CLOVER_OSN_GW_API_KEY": "key_gw",
        }):
            items_raw = [
                {"name": "Protein Bar", "sku": "PB001", "price": 299, "itemStock": {"quantity": 3}},
                {"name": "Pre-workout", "sku": "PW002", "price": 4999, "itemStock": {"quantity": 20}},
                {"name": "Creatine", "sku": "CR003", "price": 3999, "itemStock": {"quantity": 1}},
            ]
            with patch.object(_cc, "_get_all_pages", return_value=items_raw):
                result = _cc.get_inventory("GW", low_stock_threshold=5)
            assert result.total_items == 3
            low_names = {i.name for i in result.low_stock_items}
            assert "Protein Bar" in low_names
            assert "Creatine" in low_names
            assert "Pre-workout" not in low_names

    @_layer_b
    def test_inventory_price_converted_from_cents(self):
        _cc._cache_clear()
        import os
        with patch.dict(os.environ, {
            "CLOVER_OSN_GM_MERCHANT_ID": "mid_gm",
            "CLOVER_OSN_GM_API_KEY": "key_gm",
        }):
            items_raw = [{"name": "Item", "sku": "X", "price": 1999, "itemStock": {"quantity": 10}}]
            with patch.object(_cc, "_get_all_pages", return_value=items_raw):
                result = _cc.get_inventory("GM")
            assert result.all_items[0].price_usd == 19.99

    @_layer_b
    def test_inventory_missing_itemstock_defaults_zero(self):
        _cc._cache_clear()
        import os
        with patch.dict(os.environ, {
            "CLOVER_OSN_GF_MERCHANT_ID": "mid_gf",
            "CLOVER_OSN_GF_API_KEY": "key_gf",
        }):
            items_raw = [{"name": "Item", "sku": "", "price": 0}]  # no itemStock
            with patch.object(_cc, "_get_all_pages", return_value=items_raw):
                result = _cc.get_inventory("GF")
            assert result.all_items[0].qty_on_hand == 0
            assert result.all_items[0].low_stock is True  # 0 ≤ 5


# ── Customer trends (mocked) ──────────────────────────────────────────────────

class TestCustomerTrends:
    @_layer_b
    def test_customer_trends_pct_change_calculated(self):
        _cc._cache_clear()
        import os
        with patch.dict(os.environ, {
            "CLOVER_OSN_GW_MERCHANT_ID": "mid_gw",
            "CLOVER_OSN_GW_API_KEY": "key_gw",
        }):
            # current period: 80 new customers; prior: 100
            curr_customers = [{"id": str(i)} for i in range(80)]
            curr_payments = [{"amount": 1000}] * 200
            prior_customers = [{"id": str(i)} for i in range(100)]
            with patch.object(_cc, "_get_all_pages") as mock_get:
                mock_get.side_effect = [curr_customers, curr_payments, prior_customers]
                result = _cc.get_customer_trends("GW", "30d")
            assert result.stats.new_customers == 80
            assert result.stats.prior_period_new_customers == 100
            assert result.stats.pct_change == pytest.approx(-20.0, rel=0.01)

    @_layer_b
    def test_customer_trends_no_prior_data(self):
        _cc._cache_clear()
        import os
        with patch.dict(os.environ, {
            "CLOVER_OSN_GM_MERCHANT_ID": "mid_gm",
            "CLOVER_OSN_GM_API_KEY": "key_gm",
        }):
            curr_customers = [{"id": "1"}, {"id": "2"}]
            curr_payments = [{"amount": 500}]
            with patch.object(_cc, "_get_all_pages") as mock_get:
                mock_get.side_effect = [curr_customers, curr_payments, _cc.CloverConnectorError("timeout")]
                result = _cc.get_customer_trends("GM", "30d")
            assert result.stats.new_customers == 2
            assert result.stats.pct_change is None


# ── Format helpers ─────────────────────────────────────────────────────────────

class TestFormatHelpers:
    @_layer_b
    def test_format_sales_includes_store_name(self):
        from cora.connectors.clover_client import (
            StoreSalesSummary, format_sales_for_llm
        )
        summaries = [
            StoreSalesSummary("GW", "Gilbert & Warner", "today", 1250.0, 42, 29.76, 0.0, 0, 1250.0),
        ]
        output = format_sales_for_llm(summaries, "today")
        assert "Gilbert & Warner" in output
        assert "1,250.00" in output or "1250" in output
        assert "42" in output

    @_layer_b
    def test_format_sales_no_clover_reference(self):
        from cora.connectors.clover_client import StoreSalesSummary, format_sales_for_llm
        summaries = [
            StoreSalesSummary("GW", "Gilbert & Warner", "today", 500.0, 10, 50.0, 0.0, 0, 500.0),
        ]
        output = format_sales_for_llm(summaries, "today")
        assert "Clover" not in output
        assert "merchant" not in output.lower()
        assert "api" not in output.lower()

    @_layer_b
    def test_format_sales_portfolio_total_shown_for_multiple(self):
        from cora.connectors.clover_client import StoreSalesSummary, format_sales_for_llm
        summaries = [
            StoreSalesSummary("GW", "Gilbert & Warner", "today", 1000.0, 30, 33.33, 0.0, 0, 1000.0),
            StoreSalesSummary("GM", "Gilbert & McKellips", "today", 800.0, 25, 32.0, 0.0, 0, 800.0),
        ]
        output = format_sales_for_llm(summaries, "today")
        assert "Portfolio total" in output or "total" in output.lower()

    @_layer_b
    def test_format_inventory_shows_low_stock(self):
        from cora.connectors.clover_client import (
            InventoryItem, StoreInventorySummary, format_inventory_for_llm
        )
        low = InventoryItem("Protein Bar", "PB001", 2, True, 2.99)
        summary = StoreInventorySummary("GW", "Gilbert & Warner", 10, [low], [low])
        output = format_inventory_for_llm([summary], low_stock_only=True)
        assert "Protein Bar" in output
        assert "2" in output

    @_layer_b
    def test_format_customer_trends_shows_delta(self):
        from cora.connectors.clover_client import (
            CustomerPeriodStats, StoreCustomerSummary, format_customer_trends_for_llm
        )
        stats = CustomerPeriodStats(80, 200, 100, -20.0)
        summary = StoreCustomerSummary("GW", "Gilbert & Warner", "30d", stats)
        output = format_customer_trends_for_llm([summary])
        assert "20.0" in output
        assert "80" in output

    @_layer_b
    def test_format_empty_returns_no_data_message(self):
        from cora.connectors.clover_client import format_sales_for_llm
        output = format_sales_for_llm([], "today")
        assert "No" in output or "no" in output


# ── HTTP pagination (mocked) ──────────────────────────────────────────────────

class TestPagination:
    @_layer_b
    def test_single_page_no_extra_request(self):
        import os
        with patch.dict(os.environ, {
            "CLOVER_OSN_GW_MERCHANT_ID": "mid",
            "CLOVER_OSN_GW_API_KEY": "key",
        }):
            mock_resp = MagicMock()
            mock_resp.ok = True
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"elements": [{"amount": 100}] * 5}
            with patch("requests.get", return_value=mock_resp) as mock_get:
                result = _cc._get_all_pages("mid", "key", "payments")
            assert len(result) == 5
            assert mock_get.call_count == 1

    @_layer_b
    def test_401_raises_connector_error(self):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 401
        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(_cc.CloverConnectorError, match="Auth failed"):
                _cc._get_all_pages("mid", "key", "payments")

    @_layer_b
    def test_404_raises_connector_error(self):
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 404
        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(_cc.CloverConnectorError, match="Merchant not found"):
                _cc._get_all_pages("mid", "key", "payments")

    @_layer_b
    def test_network_exception_raises_connector_error(self):
        import requests as req_module
        with patch("requests.get", side_effect=req_module.RequestException("timeout")):
            with pytest.raises(_cc.CloverConnectorError, match="Network error"):
                _cc._get_all_pages("mid", "key", "payments")
