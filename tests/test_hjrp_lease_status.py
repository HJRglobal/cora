"""Unit tests for src/cora/tools/hjrp_client.py (hjrp_lease_status)."""

from __future__ import annotations

from datetime import date

import pytest

from cora.tools.hjrp_client import (
    HjrpClientError,
    _broker_name,
    _days_phrase,
    _load_properties,
    _marker,
    _parse_end,
    format_lease_status,
    get_lease_status,
)

_M_ALARM = "\U0001f6a8"
_M_RED = "\U0001f534"
_M_YELLOW = "\U0001f7e1"
_M_OK = "✅"

TODAY = date(2026, 6, 4)


# ── _parse_end ───────────────────────────────────────────────────────────────

def test_parse_end_iso():
    assert _parse_end("2026-10-31") == date(2026, 10, 31)


@pytest.mark.parametrize("val", ["MTM", "mtm", None, "", "not-a-date", "12/31/2026"])
def test_parse_end_non_date(val):
    assert _parse_end(val) is None


# ── _marker ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("days,expected", [
    (-5, _M_ALARM),   # expired
    (0, _M_ALARM),
    (30, _M_ALARM),
    (31, _M_RED),
    (90, _M_RED),
    (91, _M_YELLOW),
    (180, _M_YELLOW),
    (181, _M_OK),
    (5000, _M_OK),
])
def test_marker_thresholds(days, expected):
    assert _marker(days) == expected


# ── _days_phrase ─────────────────────────────────────────────────────────────

def test_days_phrase_expired():
    assert _days_phrase(-3) == "expired 3d ago"


def test_days_phrase_today():
    assert _days_phrase(0) == "expires today"


def test_days_phrase_future():
    assert _days_phrase(149) == "149d"


# ── _broker_name ─────────────────────────────────────────────────────────────

def test_broker_name_strips_email():
    assert _broker_name("Sharon Carstens <brokerandtrainer@gmail.com>") == "Sharon Carstens"


def test_broker_name_plain():
    assert _broker_name("Sharon Carstens") == "Sharon Carstens"


# ── format_lease_status (synthetic) ──────────────────────────────────────────

def _synthetic():
    return [
        {
            "name": "North Hampton",
            "address": "1337 S Gilbert Rd, Mesa AZ",
            "leases": [
                {"suite": "111", "tenant": "HJR Global", "monthly_rent": 5351,
                 "lease_end": "2026-10-31", "status": "active"},
                {"suite": "121", "tenant": "Vine and Branches", "monthly_rent": 1144,
                 "lease_end": "2026-06-30", "status": "not_renewing",
                 "broker": "Sharon Carstens <brokerandtrainer@gmail.com>"},
                {"suite": "101", "tenant": "Vitalant", "monthly_rent": 7370,
                 "lease_end": "2026-05-31", "status": "renewing",
                 "note": "New lease starts June 2026",
                 "broker": "Sharon Carstens <brokerandtrainer@gmail.com>"},
                {"suite": "104", "tenant": "F3 Storage", "monthly_rent": 0,
                 "lease_end": "MTM", "status": "internal"},
                {"suite": "114", "tenant": "Big D Media", "monthly_rent": 5844,
                 "lease_end": None, "status": "active"},
            ],
        },
        {
            "name": "South Hampton",
            "address": "1555 S Gilbert Rd, Mesa AZ",
            "leases": [
                {"suite": "108", "tenant": "LLC Admin", "monthly_rent": 2049,
                 "lease_end": "2026-10-31", "status": "active"},
                {"suite": "110", "tenant": "LLC - DTA", "monthly_rent": 9895,
                 "lease_end": "2026-10-31", "status": "active"},
                {"suite": "112", "tenant": "LLC - DTT", "monthly_rent": 6540,
                 "lease_end": "2026-10-31", "status": "active"},
            ],
        },
    ]


def test_format_detects_oct_cluster_and_rent_at_risk():
    out = format_lease_status(_synthetic(), TODAY)
    # 4 leases on 2026-10-31: 5351 + 2049 + 9895 + 6540 = 23,835
    assert "Renewal cluster 2026-10-31" in out
    assert "4 leases" in out
    assert "$23,835/mo at risk" in out


def test_format_vacancy_line():
    out = format_lease_status(_synthetic(), TODAY)
    # Vine & Branches expires 6/30 -> vacant 7/1, Sharon relisting
    assert "Upcoming vacancy" in out
    assert "Vine and Branches" in out
    assert "vacant 2026-07-01" in out
    assert "Sharon Carstens relisting" in out


def test_format_renewing_not_alarmed():
    out = format_lease_status(_synthetic(), TODAY)
    # Vitalant is renewing -> shown as renewing, never an expiry alarm line
    assert "Vitalant" in out
    assert "renewing" in out.lower()
    # It must not appear with an "expired" phrase
    assert "Vitalant (suite 101): 2026-05-31 (expired" not in out


def test_format_mtm_and_no_date_grouped():
    out = format_lease_status(_synthetic(), TODAY)
    assert "Month-to-month:" in out
    assert "F3 Storage" in out
    assert "Term not on file:" in out
    assert "Big D Media" in out


def test_format_broker_line():
    out = format_lease_status(_synthetic(), TODAY)
    assert "*Brokers:*" in out
    assert "Sharon Carstens <brokerandtrainer@gmail.com>" in out


def test_format_header_has_as_of_date():
    out = format_lease_status(_synthetic(), TODAY)
    assert out.startswith("*HJRP Lease Status* — as of 2026-06-04")


def test_format_no_cluster_when_single():
    props = [{
        "name": "Solo", "address": "x",
        "leases": [{"suite": "1", "tenant": "A", "monthly_rent": 100,
                    "lease_end": "2026-09-01", "status": "active"}],
    }]
    out = format_lease_status(props, TODAY)
    assert "Renewal cluster" not in out  # need >=2 leases sharing a date


# ── real register integration ────────────────────────────────────────────────

def test_real_register_loads():
    props = _load_properties()
    names = {p["name"] for p in props}
    assert "North Hampton" in names
    assert "South Hampton" in names


def test_real_register_oct_2026_cluster():
    """Regression-proof the headline fact: the real register yields the
    ~$23,835/mo October 2026 cluster of 4 leases."""
    out = get_lease_status(today=TODAY)
    assert "Renewal cluster 2026-10-31" in out
    assert "$23,835/mo at risk" in out
    assert "Vine and Branches" in out  # vacancy surfaced


def test_real_register_both_buildings_present():
    out = get_lease_status(today=TODAY)
    assert "North Hampton" in out
    assert "South Hampton" in out


def test_get_lease_status_missing_file(monkeypatch):
    import cora.tools.hjrp_client as hc

    def _boom(*a, **k):
        raise HjrpClientError("missing")

    monkeypatch.setattr(hc, "_load_properties", _boom)
    assert hc.get_lease_status(today=TODAY) == "I don't have that right now."


# ── tool_dispatch wiring (Definition-of-Done: 4 wiring points) ───────────────

def test_dispatch_wiring_complete():
    from cora.tools import tool_dispatch as td

    assert "hjrp_lease_status" in td._TOOL_FUNCTIONS
    assert hasattr(td, "_tool_hjrp_lease_status")
    assert any(t["name"] == "hjrp_lease_status" for t in td.TOOL_DEFINITIONS)


def test_dispatch_handler_blocks_non_hjrp_entity():
    from cora.tools import tool_dispatch as td

    out = td._tool_hjrp_lease_status("U1", "F3E", {"_channel_name": "f3e-leadership"})
    assert "scoped to HJR Properties" in out


def test_dispatch_handler_blocks_tier3_channel():
    from cora.tools import tool_dispatch as td

    # An HJRP entity but a non-tier1 (operations) channel -> finance-required refusal
    out = td._tool_hjrp_lease_status("U1", "HJRP", {"_channel_name": "hjrp-operations"})
    assert out == td._FINANCE_CHANNEL_REQUIRED


def test_dispatch_handler_allows_tier1_channel():
    from cora.tools import tool_dispatch as td

    out = td._tool_hjrp_lease_status("U1", "HJRP", {"_channel_name": "hjrp-leadership"})
    assert "HJRP Lease Status" in out
