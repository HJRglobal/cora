"""F3E production-pipeline read lane -- price-free by construction (slice 6).

cq-fe9ec84a5ca2. The Airtable base and the Asana project behind this program are
the TEAM- and PARTNER-visible surface class; every cost, tolling rate, margin and
negotiation term lives in the f3-cost-lab artifact and is never served. The
program's standing guardrail (2026-08-09 design doc) is "zero-dollar by
construction -- any future edit re-runs the counterparty grep before deploy", and
that grep is the pattern this reader screens with.

TWO rails, tested separately, because a field allowlist is only as good as the
field names staying put:
  1. PROJECTION -- a fixed non-cost column list per table. A cost column is not
     filtered downstream; it is never fetched.
  2. A VALUE SCREEN on everything rendered -- which is what catches the realistic
     leak: a dollar figure typed into a free-text "notes" field.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import dashboard_access  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

DASH = "f3-production-pipeline"
HARRISON = "U0B2RM2JYJ1"
STRANGER = "U_NOBODY"


def _runs():
    return [{"Run": "Run 2E", "Brand/Line": "F3 Energy", "Co-packer": "Allen Flavors",
             "Run date": "2026-08-11", "Status": "In production",
             "Volume plan": "40,000 cans", "Notes": "QA sample pulled"}]


def _items():
    return [
        {"Item": "Citrus flavor", "Type": "ingredient", "Supplier": "Allen",
         "Status": "blocked", "COA status": "pending", "Lot #": "L-2201"},
        {"Item": "Sleeves", "Type": "packaging", "Supplier": "Cotton",
         "Status": "received", "COA status": "n/a"},
    ]


def _partners():
    return [{"Partner": "Allen Flavors", "Role": "co-packer"},
            {"Name": "Nimbl", "Role": "3PL"}]


# ── rail 1: projection ───────────────────────────────────────────────────────

def test_no_requested_column_is_cost_shaped():
    for table, fields in td._PROD_FIELDS.items():
        for name in fields:
            assert not td._PROD_MONEY_RE.search(name), f"{table}.{name} is cost-shaped"


def test_the_projection_matches_the_documented_schema():
    """From the 2026-08-09 design doc: Runs (run / brand-line / co-packer / date /
    status / volume plan / notes), Run Items (item / type / supplier / qty / lot /
    status / COA / linked run), Partners (names + roles only, no rates)."""
    assert "Co-packer" in td._PROD_FIELDS["runs"]
    assert "COA status" in td._PROD_FIELDS["run_items"]
    assert "Role" in td._PROD_FIELDS["partners"]
    assert set(td._PROD_FIELDS) == {"runs", "run_items", "partners"}


# ── rail 2: the value screen ─────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "$1.42 per can",
    "tolling at 0.18/case",
    "margin looks thin",
    "COGS up 6%",
    "price agreed with Allen",
    "deposit wired",
    "20% discount on the second run",
    "quoted last week",
    "invoice due on receipt",
    "per kg on the citrus",
])
def test_cost_language_in_a_free_text_field_is_withheld(value):
    out = td._prod_value(value)
    assert "withheld" in out
    assert value.split()[0].strip("$") not in out or "withheld" in out


@pytest.mark.parametrize("value", [
    "QA sample pulled",
    "40,000 cans",
    "Allen Flavors",
    "lot L-2201 received",
    "COA pending with the supplier",
])
def test_ordinary_operational_language_survives(value):
    assert td._prod_value(value) == value


def test_a_dollar_figure_never_reaches_the_rendered_output():
    runs = _runs()
    runs[0]["Notes"] = "agreed $0.42/can tolling with Allen"
    out = td._format_production_pipeline(runs, _items(), _partners())
    assert "$" not in out
    assert "0.42" not in out
    assert "tolling" not in out.lower()
    assert "withheld" in out


def test_over_screening_is_the_intended_direction():
    """A run note reading "flow rate stable" loses that value rather than risking
    a rate reaching a partner-visible surface. Documented, not accidental."""
    assert "withheld" in td._prod_value("flow rate stable")


def test_the_reply_says_costs_are_not_served():
    out = td._format_production_pipeline(_runs(), _items(), _partners())
    assert "not served here" in out


# ── rendering ────────────────────────────────────────────────────────────────

def test_runs_items_and_partners_are_summarized():
    out = td._format_production_pipeline(_runs(), _items(), _partners())
    assert "Run 2E" in out
    assert "In production" in out
    assert "Citrus flavor" in out          # the blocked item is named
    assert "Sleeves" not in out            # a received item is not
    assert "Allen Flavors" in out and "Nimbl" in out


def test_empty_store_degrades_to_a_stub_not_a_crash():
    out = td._format_production_pipeline([], [], [])
    assert "nothing recorded" in out


def test_a_url_or_platform_token_in_a_field_is_scrubbed():
    """VERBATIM_TABLE_TOOL: format_reply is bypassed, so the dashboard scrub is
    the only thing between an Airtable free-text field and Slack (D-051 7/11)."""
    assert "[link]" in td._prod_value("see https://airtable.com/app123/tbl456")
    assert "airtable" not in td._prod_value("pulled from Airtable").lower()


# ── the gate ─────────────────────────────────────────────────────────────────

def test_registry_entry_is_entity_scoped_and_never_cached():
    entry = dashboard_access.entry_for(DASH) if hasattr(dashboard_access, "entry_for") \
        else None
    store = dashboard_access.store_for(DASH)
    assert store.get("base") == "app1hWKmTAnvp09rR"
    assert set(store.get("tables") or {}) == {"runs", "run_items", "partners"}
    if entry:
        assert entry.get("no_cache") is True


def test_the_tool_never_caches():
    """Membership in VERBATIM_TABLE_TOOLS is the never-cache mechanism ("Verbatim
    tables are never cached" in app.py) AND what makes _dash_scrub the only thing
    between an Airtable field and Slack."""
    assert "f3e_production_pipeline" in td.VERBATIM_TABLE_TOOLS


def test_an_unlisted_channel_is_refused():
    refusal = dashboard_access.check_dashboard_access(DASH, STRANGER, "osn-leadership")
    assert refusal


def test_an_f3e_channel_is_allowed_for_a_teammate():
    assert dashboard_access.check_dashboard_access(DASH, STRANGER, "f3e-leadership") is None


def test_harrison_dm_is_allowed():
    assert dashboard_access.check_dashboard_access(DASH, HARRISON, "dm") is None


def test_a_stranger_dm_is_refused():
    assert dashboard_access.check_dashboard_access(DASH, STRANGER, "dm")


def test_the_tool_refuses_before_touching_airtable(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not reach Airtable when the gate refuses")
    monkeypatch.setattr(td.airtable_client, "list_records", _boom)
    out = td._tool_f3e_production_pipeline(
        STRANGER, "OSN", {"_channel_name": "osn-leadership"})
    assert out and "production pipeline" not in out.lower()


def test_the_tool_is_wired_and_exposed_to_f3e():
    assert "f3e_production_pipeline" in td._TOOL_FUNCTIONS
    names = {t["name"] for t in td.TOOL_DEFINITIONS}
    assert "f3e_production_pipeline" in names
    exposed = {t["name"] for t in td.tools_for_entity("F3E", False)}
    assert "f3e_production_pipeline" in exposed
    assert "f3e_production_pipeline" in td._TOOL_TIMEOUTS


def test_the_tool_description_states_the_price_free_property():
    spec = next(t for t in td.TOOL_DEFINITIONS if t["name"] == "f3e_production_pipeline")
    assert "PRICE-FREE" in spec["description"]
    assert "cost lab" in spec["description"]


def test_an_unavailable_store_degrades_gracefully(monkeypatch):
    class _R:
        available = False
        records: list = []
        error = "AIRTABLE_API_KEY not set"

    monkeypatch.setattr(td.airtable_client, "list_records", lambda *a, **k: _R())
    out = td._tool_f3e_production_pipeline(
        HARRISON, "F3E", {"_channel_name": "f3e-leadership"})
    assert "isn't connected yet" in out


def test_the_tool_projects_columns_on_every_table(monkeypatch):
    seen: list[tuple[str, list[str] | None]] = []

    class _R:
        available = True
        records: list = []
        error = ""

    def _capture(base, table, *, fields=None, **kw):
        seen.append((table, fields))
        return _R()

    monkeypatch.setattr(td.airtable_client, "list_records", _capture)
    td._tool_f3e_production_pipeline(HARRISON, "F3E", {"_channel_name": "dm"})
    assert [t for t, _ in seen] == ["Runs", "Run Items", "Partners"]
    for _, fields in seen:
        assert fields, "every table must be read with an explicit column projection"


def test_table_names_with_spaces_are_url_quoted():
    """The base's table IDs are not recorded anywhere in the repo, so the registry
    uses NAMES -- and "Run Items" must not produce an invalid URL."""
    from urllib.parse import quote
    assert quote("Run Items", safe="") == "Run%20Items"
    src = (_REPO_ROOT / "src" / "cora" / "connectors"
           / "airtable_client.py").read_text(encoding="utf-8")
    assert "quote(str(table)" in src
