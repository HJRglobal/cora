"""Tests for scripts/run_f3e_ecom_brief.py + asana_client.get_project_tasks."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import run_f3e_ecom_brief as brief  # noqa: E402
from cora.connectors.polar_client import PolarConnectorError, PolarReport  # noqa: E402
from cora.connectors.shopify_client import (  # noqa: E402
    InventoryVariant,
    SalesSummary,
    ShopifyConnectorError,
    TopProduct,
)

_TODAY = date(2026, 6, 18)


def _sales(period, net, orders, aov, top=None):
    return SalesSummary(
        period=period, order_count=orders, gross_revenue_usd=net, discounts_usd=0.0,
        refunds_usd=0.0, net_revenue_usd=net, avg_order_value_usd=aov,
        top_products=([TopProduct(title=top, quantity_sold=1, revenue_usd=net)] if top else []),
    )


def _polar(total, table=None):
    return PolarReport(
        query_id="q", table_data=table or [], total_data=total, deep_link="",
        date_from="", date_to="", metrics=[], dimensions=[],
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_num_coerces():
    assert brief._num(None) == 0.0
    assert brief._num("1,234.5") == 1234.5
    assert brief._num("$2,000") == 2000.0
    assert brief._num("bad") == 0.0


def test_pct_delta():
    assert brief._pct_delta(110, 100) == "(+10% vs prior 30d)"
    assert brief._pct_delta(90, 100) == "(-10% vs prior 30d)"
    assert brief._pct_delta(100, 0) == ""        # no base -> no delta
    assert brief._pct_delta(100, None) == ""


def test_windows_strictly_non_overlapping():
    cur, prior = brief._windows(_TODAY)
    assert cur == ("2026-05-19", "2026-06-18")
    assert prior == ("2026-04-19", "2026-05-18")  # ends the day BEFORE current starts
    assert prior[1] < cur[0]  # no shared boundary day


# ---------------------------------------------------------------------------
# DTC
# ---------------------------------------------------------------------------

def test_dtc_line_renders():
    def fake(period):
        return _sales(period, 5000 if period == "7d" else 20000, 40, 125, top="Energy Variety")
    with patch.object(brief.shopify_client, "get_sales_pulse", side_effect=fake):
        line = brief._dtc_line()
    assert line.startswith("- *DTC:*")          # opaque label (no "Shopify")
    assert "Shopify" not in line
    assert "$5,000 net" in line and "40 ord" in line and "$125 AOV" in line
    assert "30d $20,000 net" in line
    assert "top Energy Variety" in line


def test_dtc_line_degrades():
    with patch.object(brief.shopify_client, "get_sales_pulse", side_effect=ShopifyConnectorError("no token")):
        assert brief._dtc_line() == "- *DTC:* not available"


# ---------------------------------------------------------------------------
# Paid (Polar)
# ---------------------------------------------------------------------------

def test_paid_line_renders_with_delta_opaque():
    cur = _polar({"total_marketing_spend": 8000, "blended_roas": 3.5, "blended_net_sales": 28000})
    prior = _polar({"blended_net_sales": 25000})
    with patch.object(brief, "_polar_report", side_effect=[cur, prior]):
        line = brief._paid_line(_TODAY)
    assert line.startswith("- *Paid (blended):*")  # opaque (no "Polar")
    assert "Polar" not in line and "Meta" not in line  # no platform / ad-network names
    assert "$8,000 spend" in line
    assert "3.50x MER" in line
    assert "$28,000 net" in line
    assert "(+12% vs prior 30d)" in line  # 28000 vs 25000


def test_paid_line_polar_not_connected():
    with patch.object(brief, "_polar_report", side_effect=PolarConnectorError("no creds")):
        assert brief._paid_line(_TODAY) == "- *Paid (blended):* not connected yet"


# ---------------------------------------------------------------------------
# Subs (Polar / ReCharge)
# ---------------------------------------------------------------------------

def test_subs_line_renders():
    cur = _polar({
        "recharge_sales_products.computed.net_sales": 112,
        "recharge_sales_products.raw.total_active_subscriptions": 4,
    })
    prior = _polar({"recharge_sales_products.computed.net_sales": 100})
    with patch.object(brief, "_polar_report", side_effect=[cur, prior]):
        line = brief._subs_line(_TODAY)
    assert line.startswith("- *Subscriptions:*")  # opaque (no "ReCharge")
    assert "ReCharge" not in line
    assert "$112 net" in line and "4 active" in line and "(+12% vs prior 30d)" in line


def test_subs_line_polar_not_connected():
    with patch.object(brief, "_polar_report", side_effect=PolarConnectorError("x")):
        assert brief._subs_line(_TODAY) == "- *Subscriptions:* not connected yet"


# ---------------------------------------------------------------------------
# Retail (HubSpot)
# ---------------------------------------------------------------------------

def test_retail_line_filters_closed_and_sorts():
    deals = [
        {"properties": {"dealname": "Sprouts", "amount": "50000", "dealstage": "3760204497"}},
        {"properties": {"dealname": "WonDeal", "amount": "99999", "dealstage": "3760235206"}},  # Closed Won -> excluded
        {"properties": {"dealname": "LostDeal", "amount": "88888", "dealstage": "3760235207"}},  # Closed Lost -> excluded
        {"properties": {"dealname": "Whole Foods", "amount": "30000", "dealstage": "3760235201"}},
    ]
    with patch.object(brief.hubspot_client, "get_deals_by_pipeline", return_value=deals):
        line = brief._retail_line()
    assert line.startswith("- *Retail pipeline:*")  # opaque (no "HubSpot")
    assert "HubSpot" not in line
    assert "$80,000 open across 2 deals" in line   # 50000 + 30000, closed excluded
    assert "Sprouts" in line and "Whole Foods" in line
    assert "WonDeal" not in line and "LostDeal" not in line


def test_retail_line_degrades():
    with patch.object(brief.hubspot_client, "get_deals_by_pipeline",
                      side_effect=brief.hubspot_client.HubSpotClientError("x")):
        assert brief._retail_line() == "- *Retail pipeline:* not available"


def test_retail_closed_ids_match_module_constants():
    # Drift guard: the brief's closed-stage set IS the module's two terminal IDs.
    assert brief._CLOSED_STAGE_IDS == frozenset({
        brief.hubspot_client._F3E_CLOSED_WON_ID,
        brief.hubspot_client._F3E_CLOSED_LOST_ID,
    })


def test_retail_line_null_dealname_does_not_crash():
    # Fail-soft: a null dealname (HubSpot returns these) must not raise.
    deals = [{"properties": {"dealname": None, "amount": "50000", "dealstage": "3760235201"}}]
    with patch.object(brief.hubspot_client, "get_deals_by_pipeline", return_value=deals):
        line = brief._retail_line()
    assert "$50,000 open across 1 deals" in line
    assert "?" in line  # null name rendered as '?'


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def _variant(title, vtitle, qty, low):
    return InventoryVariant(product_title=title, variant_title=vtitle, sku="s", qty_on_hand=qty, low_stock=low)


def test_inventory_all_healthy():
    with patch.object(brief.shopify_client, "get_inventory_status",
                      return_value=[_variant("Energy", "12pk", 500, False)]):
        assert brief._inventory_line() == "- *Inventory:* all healthy"


def test_inventory_flags_low():
    variants = [_variant("Pure", "Variety", 3, True), _variant("Mood", "12pk", 8, True)]
    with patch.object(brief.shopify_client, "get_inventory_status", return_value=variants):
        line = brief._inventory_line()
    assert "2 low/critical" in line
    assert "Pure Variety (3)" in line  # lowest qty first


def test_inventory_degrades():
    with patch.object(brief.shopify_client, "get_inventory_status",
                      side_effect=ShopifyConnectorError("x")):
        assert brief._inventory_line() == "- *Inventory:* not available"


# ---------------------------------------------------------------------------
# Ops (Asana)
# ---------------------------------------------------------------------------

def test_ops_lines_overdue_and_next_due():
    tasks = [
        {"name": "Order cans", "due_on": "2026-06-10", "completed": False},   # overdue
        {"name": "Ship samples", "due_on": "2026-06-25", "completed": False},  # upcoming
        {"name": "Done thing", "due_on": "2026-06-09", "completed": True},     # completed -> ignored
    ]
    with patch.object(brief.asana_client, "get_project_tasks", return_value=tasks):
        lines = brief._ops_lines(_TODAY)
    assert any("2 open, 1 overdue" in l for l in lines)
    assert any("next due 2026-06-25" in l for l in lines)
    assert any("Order cans" in l for l in lines)


def test_ops_lines_degrade():
    with patch.object(brief.asana_client, "get_project_tasks",
                      side_effect=brief.asana_client.AsanaClientError("x")):
        lines = brief._ops_lines(_TODAY)
    assert lines == ["- *Production (Run-2):* not available"]


# ---------------------------------------------------------------------------
# build_brief + run
# ---------------------------------------------------------------------------

def _patch_all_sections(stack):
    stack.enter_context(patch.object(brief, "_dtc_line", return_value="- *DTC (Shopify):* x"))
    stack.enter_context(patch.object(brief, "_paid_line", return_value="- *Paid (Polar):* x"))
    stack.enter_context(patch.object(brief, "_subs_line", return_value="- *Subs (ReCharge):* x"))
    stack.enter_context(patch.object(brief, "_retail_line", return_value="- *Retail (HubSpot):* x"))
    stack.enter_context(patch.object(brief, "_inventory_line", return_value="- *Inventory:* x"))
    stack.enter_context(patch.object(brief, "_ops_lines", return_value=["- *Production (Run-2):* x"]))


def test_build_brief_has_entity_tag_and_no_cash_figure():
    import contextlib
    with contextlib.ExitStack() as stack:
        _patch_all_sections(stack)
        out = brief.build_brief(_TODAY)
    assert "[F3 Energy] Daily Ecom + Ops" in out
    assert "Cash -> #f3-finance" in out          # pointer present
    assert "CF_F3" not in out                     # no sheet name
    assert "ending cash" not in out.lower()       # no cash figure/line


def test_run_dry_run_does_not_post():
    import contextlib
    with contextlib.ExitStack() as stack:
        _patch_all_sections(stack)
        with patch("slack_sdk.WebClient") as wc:
            result = brief.run(dry_run=True)
            wc.assert_not_called()
    assert result["posted"] is False


def test_run_posts_to_default_cockpit():
    import contextlib
    with contextlib.ExitStack() as stack:
        _patch_all_sections(stack)
        client = MagicMock()
        with patch("slack_sdk.WebClient", return_value=client), \
             patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}):
            result = brief.run(dry_run=False)
    client.chat_postMessage.assert_called_once()
    assert client.chat_postMessage.call_args.kwargs["channel"] == brief.COCKPIT_CHANNEL
    assert result["posted"] is True


def test_run_no_token():
    import contextlib
    with contextlib.ExitStack() as stack:
        _patch_all_sections(stack)
        with patch.dict("os.environ", {"SLACK_BOT_TOKEN": ""}):
            result = brief.run(dry_run=False)
    assert result["posted"] is False
    assert result["error"] == "no_token"


def test_run_total_degradation_still_builds():
    # Every source down -> the brief still composes (fail-soft), never raises.
    import contextlib
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(brief.shopify_client, "get_sales_pulse",
                                         side_effect=ShopifyConnectorError("x")))
        stack.enter_context(patch.object(brief.shopify_client, "get_inventory_status",
                                         side_effect=ShopifyConnectorError("x")))
        stack.enter_context(patch.object(brief, "_polar_report", side_effect=PolarConnectorError("x")))
        stack.enter_context(patch.object(brief.hubspot_client, "get_deals_by_pipeline",
                                         side_effect=brief.hubspot_client.HubSpotClientError("x")))
        stack.enter_context(patch.object(brief.asana_client, "get_project_tasks",
                                         side_effect=brief.asana_client.AsanaClientError("x")))
        out = brief.build_brief(_TODAY)
    assert "not available" in out
    assert "not connected yet" in out
    assert "Polar" not in out  # opaque even when degraded
    assert "Cash -> #f3-finance" in out


def test_build_brief_section_exception_degrades_not_crashes():
    # Fail-soft invariant: an unexpected error in ONE section must degrade that line,
    # not raise out of build_brief.
    import contextlib
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(brief, "_dtc_line", side_effect=RuntimeError("boom")))
        stack.enter_context(patch.object(brief, "_paid_line", return_value="- *Paid (blended):* ok"))
        stack.enter_context(patch.object(brief, "_subs_line", return_value="- *Subscriptions:* ok"))
        stack.enter_context(patch.object(brief, "_retail_line", return_value="- *Retail pipeline:* ok"))
        stack.enter_context(patch.object(brief, "_inventory_line", return_value="- *Inventory:* ok"))
        stack.enter_context(patch.object(brief, "_ops_lines", side_effect=RuntimeError("boom2")))
        out = brief.build_brief(_TODAY)
    assert "- *DTC:* not available" in out             # _safe caught the RuntimeError
    assert "- *Production (Run-2):* not available" in out  # _safe_lines caught it
    assert "- *Paid (blended):* ok" in out             # healthy sections still render


def test_ops_null_name_overdue_does_not_crash():
    tasks = [{"name": None, "due_on": "2026-06-10", "completed": False}]  # overdue, null name
    with patch.object(brief.asana_client, "get_project_tasks", return_value=tasks):
        lines = brief._ops_lines(_TODAY)
    assert any("1 overdue" in l for l in lines)
    assert any("?" in l for l in lines)  # null name -> '?'


def test_ops_none_overdue_and_no_due():
    tasks = [
        {"name": "Future", "due_on": "2026-07-01", "completed": False},
        {"name": "Undated", "completed": False},
    ]
    with patch.object(brief.asana_client, "get_project_tasks", return_value=tasks):
        lines = brief._ops_lines(_TODAY)
    assert any("none overdue" in l for l in lines)
    assert any("next due 2026-07-01" in l for l in lines)


def test_ops_uses_due_at_when_due_on_missing():
    tasks = [{"name": "Timed", "due_at": "2026-06-10T17:00:00Z", "completed": False}]
    with patch.object(brief.asana_client, "get_project_tasks", return_value=tasks):
        lines = brief._ops_lines(_TODAY)
    assert any("1 overdue" in l for l in lines)  # due_at counted as overdue


def test_inventory_preview_caps_at_5():
    variants = [_variant(f"P{i}", "12pk", i, True) for i in range(8)]  # 8 low
    with patch.object(brief.shopify_client, "get_inventory_status", return_value=variants):
        line = brief._inventory_line()
    assert "8 low/critical" in line
    assert "+3 more" in line       # 8 - 5
    assert "P0 12pk (0)" in line   # lowest qty first
    assert "P6" not in line        # beyond the 5-item preview


def test_run_post_failure_returns_error_and_notifies():
    import contextlib
    with contextlib.ExitStack() as stack:
        _patch_all_sections(stack)
        client = MagicMock()
        client.chat_postMessage.side_effect = [Exception("not_in_channel"), None]  # post fails, notice ok
        with patch("slack_sdk.WebClient", return_value=client), \
             patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-test"}):
            result = brief.run(dry_run=False)
    assert result["posted"] is False
    assert "not_in_channel" in result["error"]
    # second call = the failure notice to #cora-build
    assert client.chat_postMessage.call_count == 2
    assert client.chat_postMessage.call_args_list[1].kwargs["channel"] == brief.SMOKE_CHANNEL


# ---------------------------------------------------------------------------
# asana_client.get_project_tasks
# ---------------------------------------------------------------------------

from cora.tools import asana_client as ac  # noqa: E402


def _mock_httpx(pages):
    """pages: list of (status_code, json_dict). Returns a patch target for httpx.Client."""
    responses = []
    for status, payload in pages:
        r = MagicMock()
        r.status_code = status
        r.json.return_value = payload
        r.text = str(payload)
        responses.append(r)
    client_cm = MagicMock()
    client_cm.__enter__.return_value.get.side_effect = responses
    return client_cm


def test_get_project_tasks_hits_project_endpoint(monkeypatch):
    monkeypatch.setenv("ASANA_PAT", "pat")
    captured = {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            r = MagicMock(status_code=200)
            r.json.return_value = {"data": [{"gid": "1", "name": "T", "completed": False}]}
            return r

    with patch.object(ac.httpx, "Client", return_value=_C()):
        tasks = ac.get_project_tasks("PROJ123")
    assert captured["url"].endswith("/projects/PROJ123/tasks")
    assert captured["params"]["completed_since"] == "now"
    assert tasks == [{"gid": "1", "name": "T", "completed": False}]


def test_get_project_tasks_raises_on_401(monkeypatch):
    monkeypatch.setenv("ASANA_PAT", "pat")

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            return MagicMock(status_code=401, text="nope")

    with patch.object(ac.httpx, "Client", return_value=_C()):
        with pytest.raises(ac.AsanaClientError):
            ac.get_project_tasks("PROJ123")


def test_get_project_tasks_paginates(monkeypatch):
    monkeypatch.setenv("ASANA_PAT", "pat")
    calls = {"n": 0, "offsets": []}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            calls["n"] += 1
            calls["offsets"].append(params.get("offset"))
            r = MagicMock(status_code=200)
            if calls["n"] == 1:
                r.json.return_value = {"data": [{"gid": "1"}], "next_page": {"offset": "OFF2"}}
            else:
                r.json.return_value = {"data": [{"gid": "2"}]}  # no next_page -> stop
            return r

    with patch.object(ac.httpx, "Client", return_value=_C()):
        tasks = ac.get_project_tasks("PROJ123", max_tasks=100)
    assert [t["gid"] for t in tasks] == ["1", "2"]   # both pages concatenated
    assert calls["offsets"] == [None, "OFF2"]         # 2nd call passed the offset


def test_get_project_tasks_raises_on_403(monkeypatch):
    monkeypatch.setenv("ASANA_PAT", "pat")

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params=None, headers=None):
            return MagicMock(status_code=403, text="forbidden")

    with patch.object(ac.httpx, "Client", return_value=_C()):
        with pytest.raises(ac.AsanaClientError):
            ac.get_project_tasks("PROJ123")
