"""Tests for the four dashboard read-layer tools in tool_dispatch:
personal_oneamerica_portfolio, personal_capital_program_state, f3e_creator_crm,
fndr_content_pipeline. Covers wiring, guard-first refusal (reader untouched),
fail-soft, source-opacity, and the format helpers."""

from __future__ import annotations

from datetime import date

import pytest

from cora import dashboard_access
from cora.connectors import airtable_client
from cora.tools import tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"
OTHER = "U0BSOMEONE"

DASH_TOOLS = [
    "personal_oneamerica_portfolio",
    "personal_capital_program_state",
    "f3e_creator_crm",
    "fndr_content_pipeline",
]

# Terms a source-opaque reply must NEVER contain.
_OPACITY_FORBIDDEN = [
    "airtable", "notion", "oneamerica", "one america", "quickbooks", "shopify",
    "drive.google", "docs.google", "http://", "https://", "appw", "appx", "tbl",
    "1ini4", "1bzi6", "1npb", "carta",
]


def _assert_opaque(text: str) -> None:
    low = text.lower()
    for term in _OPACITY_FORBIDDEN:
        assert term not in low, f"source-opacity leak {term!r} in: {text!r}"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    dashboard_access.invalidate_cache()
    monkeypatch.setenv("AIRTABLE_API_KEY", "patTEST")  # so "not connected" path isn't hit
    yield
    dashboard_access.invalidate_cache()


# --------------------------------------------------------------------------- #
# Wiring                                                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", DASH_TOOLS)
def test_tool_registered_everywhere(name):
    assert name in td._TOOL_FUNCTIONS
    assert name in td._TOOL_TIMEOUTS
    assert name in td.VERBATIM_TABLE_TOOLS
    assert any(t["name"] == name for t in td.TOOL_DEFINITIONS)


def test_exposure_creator_crm_in_f3e_only():
    f3e = {t["name"] for t in td.tools_for_entity("F3E")}
    assert "f3e_creator_crm" in f3e
    # The founder-only tools are NOT exposed to a plain F3E channel.
    for n in ["fndr_content_pipeline", "personal_oneamerica_portfolio", "personal_capital_program_state"]:
        assert n not in f3e


@pytest.mark.parametrize("name", DASH_TOOLS)
def test_all_four_in_founder_full_set(name):
    fndr = {t["name"] for t in td.tools_for_entity("FNDR")}
    assert name in fndr


def test_personal_tools_never_in_global_core():
    for n in ["personal_oneamerica_portfolio", "personal_capital_program_state", "fndr_content_pipeline"]:
        assert n not in td._GLOBAL_CORE_TOOLS


def test_cache_skip_membership():
    # Membership in VERBATIM_TABLE_TOOLS is what keeps these out of the shared
    # semantic cache (app.py: `if cache_storable and not is_structured_table`).
    assert {"personal_oneamerica_portfolio", "personal_capital_program_state"} <= td.VERBATIM_TABLE_TOOLS


# --------------------------------------------------------------------------- #
# Guard-first: a refused channel returns the guard string and NEVER reads.     #
# --------------------------------------------------------------------------- #
def test_oneamerica_guard_refuses_before_read(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("reader must not run when the guard refuses")

    monkeypatch.setattr(td.dashboard_drive_reader, "read_json_by_id", _boom)
    out = td._tool_personal_oneamerica_portfolio(HARRISON, "FNDR", {"_channel_name": "cora-build"})
    assert out == "I don't have that here -- ask me in a DM."


def test_capital_guard_refuses_before_read(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("reader must not run when the guard refuses")

    monkeypatch.setattr(td.dashboard_drive_reader, "newest_json_by_title", _boom)
    out = td._tool_personal_capital_program_state(OTHER, "F3E", {"_channel_name": "f3e-leadership"})
    assert out == "I don't have that here -- ask me in a DM."


def test_creator_guard_refuses_before_read(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("airtable must not run when the guard refuses")

    monkeypatch.setattr(td.airtable_client, "list_records", _boom)
    out = td._tool_f3e_creator_crm(OTHER, "OSN", {"_channel_name": "osn-leadership"})
    assert out == "That's not available in this channel."


def test_content_guard_refuses_before_read(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("airtable must not run when the guard refuses")

    monkeypatch.setattr(td.airtable_client, "list_records", _boom)
    out = td._tool_fndr_content_pipeline(OTHER, "F3E", {"_channel_name": "f3-athletes"})
    assert out == "That's not available in this channel."


# --------------------------------------------------------------------------- #
# Guard passes -> fail-soft when the store is unavailable.                     #
# --------------------------------------------------------------------------- #
def test_oneamerica_failsoft_when_drive_none(monkeypatch):
    monkeypatch.setattr(td.dashboard_drive_reader, "read_json_by_id", lambda fid: None)
    out = td._tool_personal_oneamerica_portfolio(HARRISON, "FNDR", {"_channel_name": "dm"})
    assert "couldn't pull" in out.lower()
    _assert_opaque(out)


def test_creator_failsoft_when_not_connected(monkeypatch):
    monkeypatch.setattr(
        td.airtable_client, "list_records",
        lambda *a, **k: airtable_client.AirtableResult(base_id="b", table="t", available=False, error="AIRTABLE_API_KEY not set"),
    )
    out = td._tool_f3e_creator_crm(OTHER, "F3E", {"_channel_name": "f3-athletes"})
    assert "isn't connected" in out.lower()
    _assert_opaque(out)


def test_content_failsoft_when_not_connected(monkeypatch):
    monkeypatch.setattr(
        td.airtable_client, "list_records",
        lambda *a, **k: airtable_client.AirtableResult(base_id="b", table="t", available=False),
    )
    out = td._tool_fndr_content_pipeline(HARRISON, "FNDR", {"_channel_name": "dm"})
    assert "isn't connected" in out.lower()


# --------------------------------------------------------------------------- #
# Guard passes -> real data flows through, source-opaque.                      #
# --------------------------------------------------------------------------- #
def test_oneamerica_happy_path_opaque(monkeypatch):
    data = {
        "meta": {"values_as_of": "2026-07-08"},
        "policies": [
            {"insured": "Phil Rogers", "product": "Whole Life", "total_db": 422635.97,
             "guar_cv": 42294.97, "pua_cv": 69768.82, "loan_balance": 96969.09,
             "avail_loan": 14550.27, "premium": 7999.99, "paid_to_date": "2027-03-12", "flags": ""},
            {"insured": "Jared Harker", "product": "Whole Life 121", "total_db": 660699.80,
             "guar_cv": 15659.54, "pua_cv": 46179.92, "loan_balance": 59630.85,
             "avail_loan": None, "premium": 5400.0, "paid_to_date": "2026-06-09",
             "flags": "PAID-TO-DATE IN PAST"},
        ],
    }
    monkeypatch.setattr(td.dashboard_drive_reader, "read_json_by_id", lambda fid: data)
    out = td._tool_personal_oneamerica_portfolio(HARRISON, "FNDR", {"_channel_name": "dm"})
    assert "2 policies" in out
    assert "Jared Harker" in out  # overdue surfaced
    _assert_opaque(out)


def test_creator_happy_path_opaque(monkeypatch):
    roster = [
        {"Name": "Alpha", "Program": ["MMA Fighters"], "Stage": "Active", "Tier": "A", "GMV": 5000},
        {"Name": "Beta", "Program": ["Streamers", "MMA Fighters"], "Stage": "Prospect", "Tier": "B", "GMV": 12000},
    ]
    activity = [{"Entry": "Ping Beta about renewal", "Date": "2026-07-10", "Type": "Note", "Follow-up date": "2026-01-01"}]

    def _fake(base, table, *, fields=None, max_records=None):
        recs = roster if fields and "Program" in fields else activity
        return airtable_client.AirtableResult(base_id=base, table=table, records=recs)

    monkeypatch.setattr(td.airtable_client, "list_records", _fake)
    out = td._tool_f3e_creator_crm(OTHER, "F3E", {"_channel_name": "f3-athletes"})
    assert "2 people" in out
    assert "Beta" in out  # top GMV
    assert "Follow-ups due" in out
    _assert_opaque(out)


def test_creator_person_lookup(monkeypatch):
    roster = [{"Name": "Casey Cruz", "Stage": "Active", "Tier": "A", "GMV": 900, "Handle": "@casey"}]

    def _fake(base, table, *, fields=None, max_records=None):
        return airtable_client.AirtableResult(
            base_id=base, table=table,
            records=(roster if fields and "Program" in fields else []),
        )

    monkeypatch.setattr(td.airtable_client, "list_records", _fake)
    out = td._tool_f3e_creator_crm(HARRISON, "FNDR", {"_channel_name": "dm", "person": "casey"})
    assert "Casey Cruz" in out
    out2 = td._tool_f3e_creator_crm(HARRISON, "FNDR", {"_channel_name": "dm", "person": "nobody"})
    assert "don't have a creator" in out2.lower()


# --------------------------------------------------------------------------- #
# Format helpers (deterministic, pinned today).                               #
# --------------------------------------------------------------------------- #
def test_format_capital_seed_not_synced():
    seed = {
        "meta": {"synced_at": "2026-07-10T21:45:00-07:00"},
        "locked": {
            "raise_usd": 2000000, "post_money_valuation_usd": 25000000,
            "price_per_share_usd": 0.6536, "founder_conversion_usd": 6000000,
            "ambassador_pool_usd": 1500000, "ambassador_pool_pct": 6.0,
            "operator_seat_usd": 500000, "operator_seat_pct": 2.0,
            "recap": "Consolidate Class A+B",
            "carta": {"fully_diluted": 26011250, "harrison_shares": 25000000, "harrison_pct": 96.11},
        },
        "calc": None, "roster": None, "phases": None, "legal": None,
        "open_items": None, "tracker": None, "candidates": [],
        "note": "Counsel brief emailed",
    }
    out = td._format_capital_program(seed)
    assert "$2,000,000" in out and "$25,000,000" in out
    assert "$0.6536" in out
    assert "96.11%" in out
    assert "hasn't been synced" in out.lower()
    _assert_opaque(out)


def test_format_capital_with_edit_state():
    data = {
        "meta": {"synced_at": "2026-07-12T09:00:00-07:00"},
        "locked": {"raise_usd": 2000000, "post_money_valuation_usd": 25000000},
        "candidates": [{"name": "Investor A", "status": "confirmed"}, {"name": "Investor B", "status": "pipeline"}],
        "phases": {"legal": "in review", "outreach": "active"},
        "open_items": ["counsel reply", "sign NDA"],
    }
    out = td._format_capital_program(data)
    assert "Live state" in out
    assert "2 in pipeline" in out
    assert "1 confirmed" in out and "Investor A" in out
    _assert_opaque(out)


def test_format_content_pipeline_ordering_and_budget():
    deliverables = [
        {"Deliverable": "A", "Action flag": "On track"},
        {"Deliverable": "B", "Action flag": "Overdue", "Due date": "2026-07-01"},
        {"Deliverable": "C", "Action flag": "Unassigned"},
        {"Deliverable": "D", "Action flag": "Overdue", "Due date": "2026-07-02"},
    ]
    budget = [
        {"Bucket": "Media", "Planned $": 10000, "Actual $": 4000},
        {"Bucket": "Media", "Planned $": 5000, "Actual $": 1000},
        {"Bucket": "Production", "Planned $": 8000, "Actual $": 8000},
    ]
    events = [{"Status": "Outreach"}, {"Status": "Outreach"}, {"Status": "Booked"}]
    out = td._format_content_pipeline(deliverables, [], [], budget, events, today=date(2026, 7, 11))
    # Overdue counted first in the summary line
    assert out.index("Overdue 2") < out.index("On track 1")
    assert "Media: $5,000 of $15,000" in out
    assert "Events pipeline: Outreach 2, Booked 1" in out
    _assert_opaque(out)


def test_oneamerica_exact_85_not_counted():
    # Exactly 85.0% borrowed must NOT be labelled ">85% borrowed".
    at85 = {"meta": {"values_as_of": "2026-07-08"}, "policies": [
        {"insured": "X", "total_db": 100, "guar_cv": 1, "pua_cv": 1,
         "loan_balance": 8500, "avail_loan": 1500, "premium": 1, "paid_to_date": "2027-01-01", "flags": ""}]}
    out = td._format_oneamerica(at85, today=date(2026, 7, 11))
    assert ">85% borrowed" not in out
    over = {"meta": {"values_as_of": "2026-07-08"}, "policies": [
        {"insured": "X", "total_db": 100, "guar_cv": 1, "pua_cv": 1,
         "loan_balance": 8600, "avail_loan": 1400, "premium": 1, "paid_to_date": "2027-01-01", "flags": ""}]}
    out2 = td._format_oneamerica(over, today=date(2026, 7, 11))
    assert "1 at >85% borrowed" in out2


def test_oneamerica_non_dict_meta_no_crash():
    out = td._format_oneamerica({"meta": 5, "policies": []}, today=date(2026, 7, 11))
    assert "0 policies" in out


def test_capital_non_dict_nested_no_crash():
    weird = {"meta": "oops", "locked": {"raise_usd": 2000000, "post_money_valuation_usd": 25000000, "carta": "bad"},
             "calc": None, "candidates": []}
    out = td._format_capital_program(weird)  # must not raise
    assert "$2,000,000" in out
    assert "hasn't been synced" in out.lower()


def test_creator_lookup_scrubs_urls_and_vendors():
    roster = [{"Name": "Foo", "Handle": "https://instagram.com/foo",
               "Next step": "sync via airtable.com/appX", "Stage": "Active"}]
    out = td._creator_person_lookup(roster, "foo")
    low = out.lower()
    assert "http" not in low          # full URL neutralized
    assert "airtable" not in low      # vendor token neutralized
    assert "Foo" in out


def test_content_pipeline_priority_bullets_ordered():
    # Raw order puts Unassigned before Overdue; bullets must still lead with Overdue.
    deliverables = [
        {"Deliverable": "U1", "Action flag": "Unassigned"},
        {"Deliverable": "O1", "Action flag": "Overdue", "Due date": "2026-07-01"},
    ]
    out = td._format_content_pipeline(deliverables, [], [], [], [], today=date(2026, 7, 11))
    assert out.index("[Overdue] O1") < out.index("[Unassigned] U1")


def test_format_creator_counts():
    roster = [
        {"Name": "A", "Stage": "Active", "Tier": "A", "Program": ["MMA"], "GMV": 100},
        {"Name": "B", "Stage": "Active", "Tier": "B", "Program": ["MMA", "Streamers"], "GMV": 500},
        {"Name": "C", "Stage": "Prospect", "Tier": "A", "Program": ["Streamers"], "GMV": 0},
    ]
    out = td._format_creator_crm(roster, [], today=date(2026, 7, 11))
    assert "3 people" in out
    assert "Active 2" in out
    assert "MMA 2" in out
    # only GMV>0 creators listed as top, highest first
    assert out.index("B:") < out.index("A:")
    assert "C:" not in out.split("Top creators")[-1]
