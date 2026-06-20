"""Unit tests for src/cora/connectors/shopify_client.py."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from cora.connectors.shopify_client import (
    LOW_STOCK_THRESHOLD,
    VALID_PERIODS,
    InventoryVariant,
    LocationSKU,
    SalesSummary,
    ShopifyConfigError,
    ShopifyConnectorError,
    TopProduct,
    _az_now,
    _base_url,
    _cache_clear,
    _cache_get,
    _cache_set,
    _get_locations,
    _headers,
    _infer_brand_from_title,
    _period_to_iso,
    _store_config,
    format_inventory_for_llm,
    format_location_inventory_for_llm,
    format_sales_for_llm,
    get_inventory_by_location,
    get_inventory_status,
    get_sales_pulse,
    graphql,
    is_beverage_product,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_module_cache():
    """Clear the in-memory cache before every test."""
    _cache_clear()
    yield
    _cache_clear()


@pytest.fixture()
def env_vars(monkeypatch):
    monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3energy.myshopify.com")
    monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "shpat_test_token_abc123")


# ── _cache helpers ────────────────────────────────────────────────────────────

def test_cache_miss_returns_none():
    assert _cache_get("no_such_key") is None


def test_cache_set_then_get():
    _cache_set("k", {"result": 42})
    assert _cache_get("k") == {"result": 42}


def test_cache_clear():
    _cache_set("k", "v")
    _cache_clear()
    assert _cache_get("k") is None


def test_cache_expires_after_ttl(monkeypatch):
    """Monkeypatching monotonic so we don't actually wait 5 minutes."""
    import cora.connectors.shopify_client as sc

    monkeypatch.setattr(sc.time, "monotonic", lambda: 1000.0)
    _cache_set("k", "v")

    # 299 seconds later — still fresh
    monkeypatch.setattr(sc.time, "monotonic", lambda: 1299.0)
    assert _cache_get("k") == "v"

    # 300 seconds later — expired
    monkeypatch.setattr(sc.time, "monotonic", lambda: 1300.0)
    assert _cache_get("k") is None


# ── _store_config ─────────────────────────────────────────────────────────────

def test_store_config_returns_values(env_vars):
    store, token = _store_config()
    assert store == "f3energy.myshopify.com"
    assert token == "shpat_test_token_abc123"


def test_store_config_missing_store(monkeypatch):
    monkeypatch.delenv("SHOPIFY_F3E_STORE", raising=False)
    monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "tok")
    with pytest.raises(ShopifyConfigError, match="not set"):
        _store_config()


def test_store_config_missing_token(monkeypatch):
    monkeypatch.setenv("SHOPIFY_F3E_STORE", "store.myshopify.com")
    monkeypatch.delenv("SHOPIFY_F3E_ACCESS_TOKEN", raising=False)
    with pytest.raises(ShopifyConfigError, match="not set"):
        _store_config()


def test_store_config_both_missing(monkeypatch):
    monkeypatch.delenv("SHOPIFY_F3E_STORE", raising=False)
    monkeypatch.delenv("SHOPIFY_F3E_ACCESS_TOKEN", raising=False)
    with pytest.raises(ShopifyConfigError):
        _store_config()


# ── _base_url / _headers ──────────────────────────────────────────────────────

def test_base_url_format():
    url = _base_url("mystore.myshopify.com")
    assert url.startswith("https://mystore.myshopify.com/admin/api/")
    assert "/admin/api/" in url


def test_headers_contains_access_token():
    h = _headers("tok123")
    assert h["X-Shopify-Access-Token"] == "tok123"
    assert "Content-Type" in h


# ── _period_to_iso ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("period", VALID_PERIODS)
def test_period_to_iso_returns_utc_strings(period):
    start, end = _period_to_iso(period)
    # Both should be parseable ISO-8601 UTC strings ending in Z
    datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
    datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")


def test_period_today_start_before_end():
    start, end = _period_to_iso("today")
    s = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
    e = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")
    assert s <= e


def test_period_yesterday_start_before_end():
    start, end = _period_to_iso("yesterday")
    s = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
    e = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")
    assert s < e


def test_period_7d_range_roughly_seven_days():
    start, end = _period_to_iso("7d")
    s = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
    e = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")
    diff = (e - s).total_seconds()
    # Should be between 6 and 8 days worth of seconds
    assert 6 * 86400 < diff < 8 * 86400


def test_period_30d_range_roughly_thirty_days():
    start, end = _period_to_iso("30d")
    s = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
    e = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")
    diff = (e - s).total_seconds()
    assert 29 * 86400 < diff < 31 * 86400


def test_period_invalid_raises():
    with pytest.raises(ShopifyConfigError, match="Unknown period"):
        _period_to_iso("weekly")


# ── get_sales_pulse ──────────────────────────────────────────────────────────

def _make_order(total="50.00", discounts="5.00", line_items=None, refunds=None):
    return {
        "id": "order_1",
        "total_price": total,
        "total_discounts": discounts,
        "refunds": refunds or [],
        "line_items": line_items or [
            {"title": "F3 Energy 12-Pack", "quantity": 1, "price": "50.00"},
        ],
    }


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_sales_pulse_basic(mock_paginate, env_vars):
    mock_paginate.return_value = [
        _make_order(total="100.00", discounts="10.00",
                    line_items=[{"title": "F3 Energy", "quantity": 2, "price": "50.00"}]),
        _make_order(total="50.00", discounts="0.00",
                    line_items=[{"title": "F3 Pure", "quantity": 1, "price": "50.00"}]),
    ]
    result = get_sales_pulse("today")
    assert isinstance(result, SalesSummary)
    assert result.order_count == 2
    assert result.gross_revenue_usd == 150.0
    assert result.discounts_usd == 10.0
    assert result.net_revenue_usd == 150.0  # no refunds
    assert result.avg_order_value_usd == 75.0
    assert result.period == "today"


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_sales_pulse_no_orders(mock_paginate, env_vars):
    mock_paginate.return_value = []
    result = get_sales_pulse("yesterday")
    assert result.order_count == 0
    assert result.gross_revenue_usd == 0.0
    assert result.avg_order_value_usd == 0.0
    assert result.top_products == []


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_sales_pulse_refunds(mock_paginate, env_vars):
    order = _make_order(
        total="60.00",
        refunds=[{"refund_line_items": [{"subtotal": "20.00"}]}],
    )
    mock_paginate.return_value = [order]
    result = get_sales_pulse("7d")
    assert result.refunds_usd == 20.0
    assert result.net_revenue_usd == 40.0  # 60 - 20


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_sales_pulse_top_products_capped_at_5(mock_paginate, env_vars):
    line_items = [
        {"title": f"Product {i}", "quantity": 1, "price": str(10 + i)}
        for i in range(10)
    ]
    mock_paginate.return_value = [
        {"id": "o1", "total_price": "100.00", "total_discounts": "0",
         "refunds": [], "line_items": line_items}
    ]
    result = get_sales_pulse("30d")
    assert len(result.top_products) <= 5


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_sales_pulse_top_products_sorted_by_revenue(mock_paginate, env_vars):
    line_items = [
        {"title": "Cheap", "quantity": 5, "price": "1.00"},
        {"title": "Expensive", "quantity": 1, "price": "100.00"},
    ]
    mock_paginate.return_value = [
        {"id": "o1", "total_price": "105.00", "total_discounts": "0",
         "refunds": [], "line_items": line_items}
    ]
    result = get_sales_pulse("today")
    assert result.top_products[0].title == "Expensive"


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_sales_pulse_uses_cache_on_second_call(mock_paginate, env_vars):
    mock_paginate.return_value = []
    get_sales_pulse("today")
    get_sales_pulse("today")
    # _get_paginated should only be called once (second hit is from cache)
    assert mock_paginate.call_count == 1


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_sales_pulse_different_periods_not_cached_together(mock_paginate, env_vars):
    mock_paginate.return_value = []
    get_sales_pulse("today")
    get_sales_pulse("yesterday")
    assert mock_paginate.call_count == 2


def test_get_sales_pulse_config_error(monkeypatch):
    monkeypatch.delenv("SHOPIFY_F3E_STORE", raising=False)
    monkeypatch.delenv("SHOPIFY_F3E_ACCESS_TOKEN", raising=False)
    with pytest.raises(ShopifyConfigError):
        get_sales_pulse("today")


# ── get_inventory_status ──────────────────────────────────────────────────────

def _make_product(title, variants):
    return {"title": title, "variants": variants}


def _make_variant(title="Default Title", sku="SKU001", qty=100):
    return {"title": title, "sku": sku, "inventory_quantity": qty}


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_inventory_status_basic(mock_paginate, env_vars):
    mock_paginate.return_value = [
        _make_product("F3 Energy Drink", [
            _make_variant("12-Pack", "F3E-12", qty=150),
            _make_variant("24-Pack", "F3E-24", qty=5),
        ])
    ]
    result = get_inventory_status()
    assert len(result) == 2
    titles = [v.product_title for v in result]
    assert all(t == "F3 Energy Drink" for t in titles)


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_inventory_status_low_stock_flag(mock_paginate, env_vars):
    mock_paginate.return_value = [
        _make_product("F3 Pure", [
            _make_variant("Single", "F3P-1", qty=3),   # low
            _make_variant("6-Pack", "F3P-6", qty=50),  # ok
        ])
    ]
    result = get_inventory_status(low_stock_threshold=10)
    low = [v for v in result if v.low_stock]
    ok = [v for v in result if not v.low_stock]
    assert len(low) == 1
    assert low[0].sku == "F3P-1"
    assert len(ok) == 1


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_inventory_status_default_title_stripped(mock_paginate, env_vars):
    mock_paginate.return_value = [
        _make_product("F3 Mood", [_make_variant("Default Title", qty=20)])
    ]
    result = get_inventory_status()
    assert result[0].variant_title == ""


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_inventory_status_empty_store(mock_paginate, env_vars):
    mock_paginate.return_value = []
    result = get_inventory_status()
    assert result == []


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_inventory_status_cached(mock_paginate, env_vars):
    mock_paginate.return_value = [
        _make_product("F3 Energy", [_make_variant(qty=50)])
    ]
    get_inventory_status()
    get_inventory_status()
    assert mock_paginate.call_count == 1


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_inventory_status_uses_constant_threshold(mock_paginate, env_vars):
    mock_paginate.return_value = [
        _make_product("F3 Energy", [_make_variant(qty=LOW_STOCK_THRESHOLD)])
    ]
    result = get_inventory_status()
    # qty == threshold means low_stock = True (<=)
    assert result[0].low_stock is True


@patch("cora.connectors.shopify_client._get_paginated")
def test_get_inventory_status_captures_product_type(mock_paginate, env_vars):
    """product_type is fetched + passed through to the variant (v1.1)."""
    mock_paginate.return_value = [
        {"title": "F3 Pure Variety Pack", "product_type": "Pure Drink",
         "variants": [_make_variant(qty=0)]},
        {"title": "UFL Globe T-Shirt", "variants": [_make_variant(qty=0)]},  # no product_type key
    ]
    result = get_inventory_status()
    by_title = {v.product_title: v for v in result}
    assert by_title["F3 Pure Variety Pack"].product_type == "Pure Drink"
    assert by_title["UFL Globe T-Shirt"].product_type == ""  # missing -> ""
    # the fetch requests product_type so the field is populated
    _, kwargs = mock_paginate.call_args
    assert "product_type" in kwargs["params"]["fields"]


# ── is_beverage_product ───────────────────────────────────────────────────────

@pytest.mark.parametrize("product_type, title", [
    # Live beverage product_types (verified 2026-06-18) -> beverages.
    ("Pure Drink", "F3 PURE Original - 12 Pack"),
    ("Mood Drink", "Orangesicle Mood - 12 Pack"),
    ("Energy Drink", "Citrus Clarity Energy - 12 Pack"),
    ("Energy & Mood Drink", "Energy Variety - 12 Pack"),
    ("Energy", "Strawberry Lemonade Energy - 12 Pack"),
    ("Pure Drink", "F3 Pure Variety Pack - 12 Pack (4 Flavors)"),  # the buried signal
])
def test_is_beverage_product_true_for_beverages(product_type, title):
    assert is_beverage_product(product_type, title) is True


@pytest.mark.parametrize("title", [
    # Live apparel/merch (verified 2026-06-18) -> all have BLANK product_type.
    "F3 Calm the Noise Pullover",
    "F3 Hat",
    "F3 Rising Tide T-Shirt",
    "UFL Globe T-Shirt",
    "UFL Hat",
])
def test_is_beverage_product_false_for_apparel(title):
    assert is_beverage_product("", title) is False


def test_is_beverage_product_merch_type_wins_over_beverage_title():
    """A merch item with a beverage word in its title is still excluded."""
    assert is_beverage_product("Apparel", "F3 Energy Tee") is False
    # Blank type, merch-exclude-first on the title:
    assert is_beverage_product("", "F3 Energy Koozie") is False


def test_is_beverage_product_untyped_beverage_title_fallback():
    """A beverage that someone forgot to type still passes via the title."""
    assert is_beverage_product("", "F3 Energy - 12 Pack") is True
    assert is_beverage_product(None, "Mood Variety Pack") is True


def test_is_beverage_product_word_boundaries():
    """Substring collisions must not false-match in the title fallback
    ('cap' in 'escapade', 'pack' in 'backpack')."""
    # Blank product_type forces the title path; 'cap' must NOT fire inside
    # 'Escapade', so the beverage word 'Energy' wins -> True.
    assert is_beverage_product("", "Escapade Energy - 12 Pack") is True
    # 'pack' must not match inside 'backpack' (a hypothetical merch item)
    assert is_beverage_product("", "F3 Backpack") is False
    # truly ambiguous (no type, no beverage/merch word) -> not a confirmed beverage
    assert is_beverage_product("", "Mystery Box") is False


def test_is_beverage_product_bottle_is_not_merch():
    """A bottled/glass beverage must NOT be false-excluded -- 'bottle' is a
    beverage-vocabulary word, so it is deliberately NOT a merch token (a
    silent false-exclude of a low-stock beverage is the cardinal failure)."""
    assert is_beverage_product("Energy Drink", "Energy 16oz Bottle - 12 Pack") is True
    assert is_beverage_product("Glass Bottle Drink", "") is True
    assert is_beverage_product("", "F3 Pure Sparkling Water Bottle - 12 Pack") is True
    # canned beverages stay safe (regression on the existing 'cans?' token)
    assert is_beverage_product("", "F3 Energy 16oz Can") is True


# ── _get_paginated error handling ─────────────────────────────────────────────

@patch("cora.connectors.shopify_client.requests.get")
def test_get_paginated_401_raises(mock_get, env_vars):
    resp = MagicMock()
    resp.status_code = 401
    resp.ok = False
    mock_get.return_value = resp
    from cora.connectors.shopify_client import _get_paginated
    with pytest.raises(ShopifyConnectorError, match="401"):
        _get_paginated("https://store/orders.json", "bad_token")


@patch("cora.connectors.shopify_client.requests.get")
def test_get_paginated_500_raises(mock_get, env_vars):
    resp = MagicMock()
    resp.status_code = 500
    resp.ok = False
    resp.text = "Internal Server Error"
    mock_get.return_value = resp
    from cora.connectors.shopify_client import _get_paginated
    with pytest.raises(ShopifyConnectorError, match="500"):
        _get_paginated("https://store/orders.json", "tok")


@patch("cora.connectors.shopify_client.requests.get")
def test_get_paginated_network_error(mock_get, env_vars):
    import requests as req
    mock_get.side_effect = req.RequestException("timeout")
    from cora.connectors.shopify_client import _get_paginated
    with pytest.raises(ShopifyConnectorError, match="Network error"):
        _get_paginated("https://store/orders.json", "tok")


@patch("cora.connectors.shopify_client.requests.get")
def test_get_paginated_follows_link_header(mock_get, env_vars):
    """Pagination: first page returns Link header, second page returns nothing."""
    page1 = MagicMock()
    page1.status_code = 200
    page1.ok = True
    page1.json.return_value = {"orders": [{"id": "1"}]}
    page1.headers = {
        "Link": '<https://store/orders.json?page_info=abc>; rel="next"'
    }

    page2 = MagicMock()
    page2.status_code = 200
    page2.ok = True
    page2.json.return_value = {"orders": [{"id": "2"}]}
    page2.headers = {"Link": ""}

    mock_get.side_effect = [page1, page2]

    from cora.connectors.shopify_client import _get_paginated
    results = _get_paginated("https://store/orders.json", "tok")
    assert len(results) == 2
    assert mock_get.call_count == 2


# ── format_sales_for_llm ──────────────────────────────────────────────────────

def _make_summary(**kwargs):
    defaults = dict(
        period="today",
        order_count=3,
        gross_revenue_usd=120.0,
        discounts_usd=0.0,
        refunds_usd=0.0,
        net_revenue_usd=120.0,
        avg_order_value_usd=40.0,
        top_products=[],
    )
    defaults.update(kwargs)
    return SalesSummary(**defaults)


def test_format_sales_contains_period_label():
    s = _make_summary(period="today")
    text = format_sales_for_llm(s)
    assert "Today" in text


def test_format_sales_period_yesterday():
    s = _make_summary(period="yesterday")
    text = format_sales_for_llm(s)
    assert "Yesterday" in text


def test_format_sales_period_7d():
    s = _make_summary(period="7d")
    text = format_sales_for_llm(s)
    assert "7" in text


def test_format_sales_contains_order_count():
    s = _make_summary(order_count=5)
    assert "5" in format_sales_for_llm(s)


def test_format_sales_contains_gross_revenue():
    s = _make_summary(gross_revenue_usd=999.99)
    assert "999.99" in format_sales_for_llm(s)


def test_format_sales_discounts_shown_when_nonzero():
    s = _make_summary(discounts_usd=15.0)
    assert "15" in format_sales_for_llm(s)


def test_format_sales_discounts_hidden_when_zero():
    s = _make_summary(discounts_usd=0.0)
    text = format_sales_for_llm(s)
    assert "Discounts" not in text


def test_format_sales_refunds_shown_when_nonzero():
    s = _make_summary(refunds_usd=8.5, net_revenue_usd=91.5)
    text = format_sales_for_llm(s)
    assert "Refunds" in text


def test_format_sales_top_products_listed():
    products = [TopProduct("F3 Energy", 10, 200.0), TopProduct("F3 Pure", 3, 60.0)]
    s = _make_summary(top_products=products)
    text = format_sales_for_llm(s)
    assert "F3 Energy" in text
    assert "F3 Pure" in text


def test_format_sales_no_shopify_mention():
    """Source-opaque: the word 'Shopify' must not appear in the output."""
    s = _make_summary()
    assert "Shopify" not in format_sales_for_llm(s)


def test_format_sales_no_aov_when_no_orders():
    s = _make_summary(order_count=0, avg_order_value_usd=0.0)
    text = format_sales_for_llm(s)
    assert "AOV" not in text


# ── format_inventory_for_llm ──────────────────────────────────────────────────

def _make_variant_obj(product_title="F3 Energy", variant_title="", sku="SKU1", qty=50, low=False):
    return InventoryVariant(
        product_title=product_title,
        variant_title=variant_title,
        sku=sku,
        qty_on_hand=qty,
        low_stock=low,
    )


def test_format_inventory_empty():
    text = format_inventory_for_llm([])
    assert "No inventory" in text


def test_format_inventory_all_stocked_low_stock_only():
    variants = [_make_variant_obj(low=False), _make_variant_obj(low=False)]
    text = format_inventory_for_llm(variants, low_stock_only=True)
    assert "adequately stocked" in text   # WS11: now "<summary> -- all adequately stocked."


def test_format_inventory_shows_low_stock_items():
    variants = [
        _make_variant_obj(sku="LOW", qty=2, low=True),
        _make_variant_obj(sku="OK", qty=100, low=False),
    ]
    text = format_inventory_for_llm(variants, low_stock_only=True)
    assert "LOW" in text
    assert "OK" not in text


def test_format_inventory_full_view_groups_by_product():
    variants = [
        _make_variant_obj("F3 Energy", "12-Pack", qty=50),
        _make_variant_obj("F3 Energy", "24-Pack", qty=30),
        _make_variant_obj("F3 Pure", "Single", qty=0, low=True),
    ]
    text = format_inventory_for_llm(variants, low_stock_only=False)
    assert "F3 Energy" in text
    assert "F3 Pure" in text
    assert "12-Pack" in text


def test_format_inventory_low_flag_in_full_view():
    variants = [_make_variant_obj("F3 Pure", qty=3, low=True)]
    text = format_inventory_for_llm(variants, low_stock_only=False)
    assert "LOW" in text


def test_format_inventory_no_shopify_mention():
    """Source-opaque check."""
    variants = [_make_variant_obj()]
    assert "Shopify" not in format_inventory_for_llm(variants)
    assert "Shopify" not in format_inventory_for_llm(variants, low_stock_only=False)


# ── HTTP response helper (for requests-backed functions) ────────────────────────

def _make_resp(status: int = 200, json_body=None, text: str = ""):
    """Build a fake requests.Response-like object."""
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.json.return_value = {} if json_body is None else json_body
    resp.text = text
    return resp


# ── _az_now ─────────────────────────────────────────────────────────────────────

def test_az_now_is_utc_minus_7():
    """Phoenix has no DST -- offset is always exactly UTC-7."""
    from datetime import timedelta

    now = _az_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=-7)


# ── _infer_brand_from_title ─────────────────────────────────────────────────────

def test_infer_brand_pure():
    assert _infer_brand_from_title("F3 Pure Variety Pack") == "Pure"


def test_infer_brand_mood():
    assert _infer_brand_from_title("F3 Mood 12-Pack") == "Mood"


def test_infer_brand_energy_default():
    assert _infer_brand_from_title("F3 Energy 24-Pack") == "Energy"
    # Anything that isn't Pure/Mood falls back to Energy
    assert _infer_brand_from_title("Mystery Beverage") == "Energy"


def test_infer_brand_case_insensitive():
    assert _infer_brand_from_title("f3 PURE single") == "Pure"
    assert _infer_brand_from_title("F3 mOOd") == "Mood"


# ── _get_locations ──────────────────────────────────────────────────────────────

@patch("cora.connectors.shopify_client.requests.get")
def test_get_locations_filters_and_lowercases(mock_get, env_vars):
    mock_get.return_value = _make_resp(200, {
        "locations": [
            {"id": 1, "name": "Nimbl 3PL", "active": True},
            {"id": 2, "name": "Warehouse", "active": True},
            {"id": 3, "name": "Closed Spot", "active": False},   # inactive -> dropped
            {"id": 4, "name": "", "active": True},                # no name -> dropped
            {"name": "No ID", "active": True},                    # no id -> dropped
        ]
    })
    result = _get_locations()
    assert result == {"nimbl 3pl": 1, "warehouse": 2}


@patch("cora.connectors.shopify_client.requests.get")
def test_get_locations_cached(mock_get, env_vars):
    mock_get.return_value = _make_resp(200, {"locations": [
        {"id": 1, "name": "Nimbl", "active": True},
    ]})
    _get_locations()
    _get_locations()
    assert mock_get.call_count == 1  # second call served from cache


@patch("cora.connectors.shopify_client.requests.get")
def test_get_locations_auth_error(mock_get, env_vars):
    mock_get.return_value = _make_resp(401, text="unauthorized")
    with pytest.raises(ShopifyConnectorError, match="401"):
        _get_locations()


@patch("cora.connectors.shopify_client.requests.get")
def test_get_locations_http_error(mock_get, env_vars):
    mock_get.return_value = _make_resp(500, text="boom")
    with pytest.raises(ShopifyConnectorError, match="500"):
        _get_locations()


@patch("cora.connectors.shopify_client.requests.get")
def test_get_locations_network_error(mock_get, env_vars):
    import requests
    mock_get.side_effect = requests.exceptions.RequestException("conn reset")
    with pytest.raises(ShopifyConnectorError, match="Network error"):
        _get_locations()


def test_get_locations_config_error(monkeypatch):
    monkeypatch.delenv("SHOPIFY_F3E_STORE", raising=False)
    monkeypatch.delenv("SHOPIFY_F3E_ACCESS_TOKEN", raising=False)
    with pytest.raises(ShopifyConfigError):
        _get_locations()


# ── get_inventory_by_location ───────────────────────────────────────────────────

_INV_LEVELS = [
    {"inventory_item_id": 111, "available": 5},
    {"inventory_item_id": 222, "available": 80},
    {"inventory_item_id": 333, "available": 0},
]

_PRODUCTS = [
    {"id": 1, "title": "F3 Energy 12-Pack", "variants": [
        {"inventory_item_id": 111, "sku": "EN12", "title": "Default Title"},
    ]},
    {"id": 2, "title": "F3 Pure", "variants": [
        {"inventory_item_id": 222, "sku": "PU24", "title": "24-Pack"},
    ]},
    {"id": 3, "title": "F3 Mood", "variants": [
        {"inventory_item_id": 333, "sku": "MO12", "title": "Default Title"},
    ]},
]


@patch("cora.connectors.shopify_client._get_paginated")
@patch("cora.connectors.shopify_client._get_locations")
def test_get_inventory_by_location_exact_match(mock_locs, mock_paginate, env_vars):
    mock_locs.return_value = {"nimbl": 999}
    mock_paginate.side_effect = [_INV_LEVELS, _PRODUCTS]
    skus = get_inventory_by_location("nimbl")
    # sorted by product_title
    titles = [s.product_title for s in skus]
    assert titles == ["F3 Energy 12-Pack", "F3 Mood", "F3 Pure - 24-Pack"]
    by_sku = {s.sku: s.available for s in skus}
    assert by_sku == {"EN12": 5, "PU24": 80, "MO12": 0}


@patch("cora.connectors.shopify_client._get_paginated")
@patch("cora.connectors.shopify_client._get_locations")
def test_get_inventory_by_location_partial_match(mock_locs, mock_paginate, env_vars):
    mock_locs.return_value = {"nimbl 3pl warehouse": 999}
    mock_paginate.side_effect = [_INV_LEVELS, _PRODUCTS]
    skus = get_inventory_by_location("nimbl")  # partial substring match
    assert len(skus) == 3


@patch("cora.connectors.shopify_client._get_paginated")
@patch("cora.connectors.shopify_client._get_locations")
def test_get_inventory_by_location_brand_filter(mock_locs, mock_paginate, env_vars):
    mock_locs.return_value = {"nimbl": 999}
    mock_paginate.side_effect = [_INV_LEVELS, _PRODUCTS]
    skus = get_inventory_by_location("nimbl", brand="Pure")
    assert len(skus) == 1
    assert skus[0].sku == "PU24"


@patch("cora.connectors.shopify_client._get_paginated")
@patch("cora.connectors.shopify_client._get_locations")
def test_get_inventory_by_location_not_found(mock_locs, mock_paginate, env_vars):
    mock_locs.return_value = {"nimbl": 1, "warehouse": 2}
    with pytest.raises(ShopifyConnectorError, match="not found"):
        get_inventory_by_location("atlantis")
    mock_paginate.assert_not_called()


@patch("cora.connectors.shopify_client._get_paginated")
@patch("cora.connectors.shopify_client._get_locations")
def test_get_inventory_by_location_ambiguous(mock_locs, mock_paginate, env_vars):
    mock_locs.return_value = {"east warehouse": 1, "west warehouse": 2}
    with pytest.raises(ShopifyConnectorError, match="ambiguous"):
        get_inventory_by_location("warehouse")
    mock_paginate.assert_not_called()


@patch("cora.connectors.shopify_client._get_paginated")
@patch("cora.connectors.shopify_client._get_locations")
def test_get_inventory_by_location_empty_levels(mock_locs, mock_paginate, env_vars):
    mock_locs.return_value = {"nimbl": 999}
    mock_paginate.side_effect = [[], _PRODUCTS]  # no inventory levels
    skus = get_inventory_by_location("nimbl")
    assert skus == []
    # products are never fetched when there are no levels
    assert mock_paginate.call_count == 1


@patch("cora.connectors.shopify_client._get_paginated")
@patch("cora.connectors.shopify_client._get_locations")
def test_get_inventory_by_location_skips_unmapped_item(mock_locs, mock_paginate, env_vars):
    mock_locs.return_value = {"nimbl": 999}
    # level for item 444 has no matching product variant -> skipped
    mock_paginate.side_effect = [
        [{"inventory_item_id": 444, "available": 9}, {"inventory_item_id": 111, "available": 5}],
        _PRODUCTS,
    ]
    skus = get_inventory_by_location("nimbl")
    assert [s.sku for s in skus] == ["EN12"]


@patch("cora.connectors.shopify_client._get_paginated")
@patch("cora.connectors.shopify_client._get_locations")
def test_get_inventory_by_location_cached(mock_locs, mock_paginate, env_vars):
    mock_locs.return_value = {"nimbl": 999}
    mock_paginate.side_effect = [_INV_LEVELS, _PRODUCTS]
    get_inventory_by_location("nimbl")
    get_inventory_by_location("nimbl")
    # two paginated calls total (levels + products), not four
    assert mock_paginate.call_count == 2


def test_get_inventory_by_location_config_error(monkeypatch):
    monkeypatch.delenv("SHOPIFY_F3E_STORE", raising=False)
    monkeypatch.delenv("SHOPIFY_F3E_ACCESS_TOKEN", raising=False)
    with pytest.raises(ShopifyConfigError):
        get_inventory_by_location("nimbl")


# ── format_location_inventory_for_llm ───────────────────────────────────────────

def test_format_location_inventory_empty():
    text = format_location_inventory_for_llm([], "nimbl")
    assert "No stock on hand" in text
    assert "Nimbl" in text


def test_format_location_inventory_single_brand_label():
    skus = [LocationSKU(product_title="F3 Pure", sku="PU24", available=80)]
    text = format_location_inventory_for_llm(skus, "nimbl", brand="pure")
    assert "F3 Pure inventory at Nimbl" in text
    # single-brand view does not print a per-brand header
    assert "*F3 Pure*\n" not in text


def test_format_location_inventory_multi_brand_grouping():
    skus = [
        LocationSKU(product_title="F3 Energy 12-Pack", sku="EN12", available=100),
        LocationSKU(product_title="F3 Pure", sku="PU24", available=80),
    ]
    text = format_location_inventory_for_llm(skus, "nimbl")
    assert "F3E inventory at Nimbl" in text
    assert "*F3 Energy*" in text
    assert "*F3 Pure*" in text
    # Energy is rendered before Pure
    assert text.index("*F3 Energy*") < text.index("*F3 Pure*")


def test_format_location_inventory_flag_thresholds():
    skus = [
        LocationSKU(product_title="F3 Energy Critical", sku="A", available=5),    # <=10 -> alarm
        LocationSKU(product_title="F3 Energy Warn", sku="B", available=40),       # <=50 -> warn
        LocationSKU(product_title="F3 Energy Healthy", sku="C", available=200),   # >50 -> ok
    ]
    text = format_location_inventory_for_llm(skus, "nimbl", brand="energy")
    assert "\U0001f6a8" in text   # 🚨
    assert "⚠️" in text  # ⚠️
    assert "✅" in text        # ✅


def test_format_location_inventory_quantity_comma_formatted():
    skus = [LocationSKU(product_title="F3 Energy", sku="A", available=12345)]
    text = format_location_inventory_for_llm(skus, "nimbl", brand="energy")
    assert "12,345" in text


def test_format_location_inventory_no_shopify_mention():
    """Source-opaque check -- must not name the platform (matches sibling formatter)."""
    skus = [
        LocationSKU(product_title="F3 Energy 12-Pack", sku="EN12", available=100),
        LocationSKU(product_title="F3 Pure", sku="PU24", available=80),
    ]
    # multi-brand view
    assert "Shopify" not in format_location_inventory_for_llm(skus, "nimbl")
    # single-brand view
    assert "Shopify" not in format_location_inventory_for_llm(skus, "nimbl", brand="energy")
    # empty view
    assert "Shopify" not in format_location_inventory_for_llm([], "nimbl")


# ── graphql ─────────────────────────────────────────────────────────────────────

@patch("cora.connectors.shopify_client.requests.post")
def test_graphql_success(mock_post, env_vars):
    mock_post.return_value = _make_resp(200, {"data": {"shop": {"name": "F3"}}})
    body = graphql("query { shop { name } }", {})
    assert body["data"]["shop"]["name"] == "F3"


@patch("cora.connectors.shopify_client.requests.post")
def test_graphql_body_errors_raise(mock_post, env_vars):
    mock_post.return_value = _make_resp(200, {
        "errors": [{"message": "Field 'bogus' doesn't exist"}]
    })
    with pytest.raises(ShopifyConnectorError, match="bogus"):
        graphql("query { bogus }", {})


@patch("cora.connectors.shopify_client.requests.post")
def test_graphql_auth_error(mock_post, env_vars):
    mock_post.return_value = _make_resp(401, text="unauthorized")
    with pytest.raises(ShopifyConnectorError, match="401"):
        graphql("query { shop { name } }", {})


@patch("cora.connectors.shopify_client.requests.post")
def test_graphql_http_error(mock_post, env_vars):
    mock_post.return_value = _make_resp(500, text="server error")
    with pytest.raises(ShopifyConnectorError, match="500"):
        graphql("query { shop { name } }", {})


@patch("cora.connectors.shopify_client.requests.post")
def test_graphql_network_error(mock_post, env_vars):
    import requests
    mock_post.side_effect = requests.exceptions.RequestException("timeout")
    with pytest.raises(ShopifyConnectorError, match="network error"):
        graphql("query { shop { name } }", {})


def test_graphql_config_error(monkeypatch):
    monkeypatch.delenv("SHOPIFY_F3E_STORE", raising=False)
    monkeypatch.delenv("SHOPIFY_F3E_ACCESS_TOKEN", raising=False)
    with pytest.raises(ShopifyConfigError):
        graphql("query { shop { name } }", {})
