"""WS7 — cash-flow snapshot writer (scripts/write_cashflow_snapshot.py).

build_snapshot() serialization + atomic write + fail-soft main().
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import write_cashflow_snapshot as wcs  # noqa: E402
from cora.connectors.gsheets_financials import (  # noqa: E402
    CashflowSummary,
    EntityRow,
    GsheetsConnectorError,
)


def _summary() -> CashflowSummary:
    return CashflowSummary(
        week_label="Week of 6/09/2026",
        as_of_date="2026-06-16",
        entities=[
            EntityRow(label="F3", entity_code="F3E", forecast=3000.0, actual=1680.0, diff=-1320.0),
            EntityRow(label="lts", entity_code="LEX-LTS", forecast=5000.0, actual=4200.0, diff=-800.0),
        ],
        closing_balance=1347657.0,
        ending_cash_series=[
            {"week": "6/09/2026", "ending_cash": 1347657.0, "is_actual": True},
            {"week": "6/16/2026", "ending_cash": 1290000.0, "is_actual": False},
            {"week": "6/23/2026", "ending_cash": 1250000.0, "is_actual": False},
        ],
    )


def _lts_summary() -> CashflowSummary:
    """A CF_LTS-tab read (separate from CF_SUMMARY): LTS ending cash + outlook."""
    return CashflowSummary(
        week_label="Week of 6/09/2026",
        as_of_date="2026-06-16",
        closing_balance=214861.0,
        ending_cash_series=[
            {"week": "6/09/2026", "ending_cash": 214861.0, "is_actual": True},
            {"week": "6/16/2026", "ending_cash": 175751.0, "is_actual": False},
            {"week": "6/23/2026", "ending_cash": 84751.0, "is_actual": False},
        ],
    )


# ── build_snapshot ──────────────────────────────────────────────────────────

def test_build_snapshot_shape_and_labels():
    snap = wcs.build_snapshot(_summary(), "2026-06-19T21:00:00+00:00", weeks=2,
                              lts_summary=_lts_summary())
    assert snap["generated_at_utc"] == "2026-06-19T21:00:00+00:00"
    assert snap["week_label"] == "Week of 6/09/2026"
    assert snap["as_of_date"] == "2026-06-16"
    assert snap["portfolio_ending_cash"] == 1347657.0
    # the dead per-entity `entities` field is gone (CF_SUMMARY has no per-entity rows)
    assert "entities" not in snap
    # outlook = current + next 2 weeks
    assert [o["week"] for o in snap["ending_cash_outlook"]] == ["6/09/2026", "6/16/2026", "6/23/2026"]
    # lex_lts comes from the separate CF_LTS read: ending cash + matching outlook
    assert snap["lex_lts"]["code"] == "LEX-LTS"
    assert snap["lex_lts"]["ending_cash"] == 214861.0
    assert [o["week"] for o in snap["lex_lts"]["ending_cash_outlook"]] == \
        ["6/09/2026", "6/16/2026", "6/23/2026"]


def test_build_snapshot_lts_none_when_absent():
    # No CF_LTS read provided (the separate read failed) -> lex_lts is null, and
    # the portfolio headline still populates.
    snap = wcs.build_snapshot(_summary(), "2026-06-19T21:00:00+00:00")
    assert snap["lex_lts"] is None
    assert snap["portfolio_ending_cash"] == 1347657.0


def test_build_snapshot_is_source_opaque():
    snap = wcs.build_snapshot(_summary(), "2026-06-19T21:00:00+00:00")
    blob = json.dumps(snap).lower()
    # no file id, sheet/tab name, or drive link leaks into the snapshot
    for forbidden in ("cf_summary", "spreadsheet", "drive.google", "docs.google", "1bkmfet"):
        assert forbidden not in blob


def test_portfolio_ending_cash_mirrors_outlook_anchor():
    # D-051: headline must equal the outlook anchor (same actual-first precedence),
    # not the legacy forecast-first closing_balance, or the brief shows two numbers.
    s = CashflowSummary(
        week_label="Week of 6/09/2026",
        as_of_date="2026-06-16",
        closing_balance=999.0,  # legacy forecast-first value (intentionally different)
        ending_cash_series=[
            {"week": "6/09/2026", "ending_cash": 110000.0, "is_actual": True},
            {"week": "6/16/2026", "ending_cash": 90000.0, "is_actual": False},
        ],
    )
    snap = wcs.build_snapshot(s, "2026-06-19T21:00:00+00:00", weeks=1)
    assert snap["portfolio_ending_cash"] == 110000.0
    assert snap["ending_cash_outlook"][0]["ending_cash"] == 110000.0


def test_portfolio_ending_cash_falls_back_to_closing_when_no_outlook():
    s = CashflowSummary(week_label="Week of 6/09/2026", as_of_date="2026-06-16",
                        closing_balance=500.0)  # empty series -> no outlook
    snap = wcs.build_snapshot(s, "2026-06-19T21:00:00+00:00")
    assert snap["ending_cash_outlook"] == []
    assert snap["portfolio_ending_cash"] == 500.0


def test_is_stale_failclosed_on_unparseable_week():
    # D-051: an unparseable week label -> data_age_days None -> is_stale must be True
    s = CashflowSummary(week_label="Week of (no date)", as_of_date="2026-06-16",
                        closing_balance=100.0)
    snap = wcs.build_snapshot(s, "2026-06-19T21:00:00+00:00")
    assert snap["data_age_days"] is None
    assert snap["is_stale"] is True


# ── main(): write + fail-soft ─────────────────────────────────────────────────

def test_main_writes_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(wcs.gf, "get_cashflow", lambda tab_name=None: _summary())
    out = tmp_path / "sub" / "cashflow-latest.json"
    rc = wcs.main(["--out", str(out)])
    assert rc == 0
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["portfolio_ending_cash"] == 1347657.0
    assert data["ending_cash_outlook"]  # non-empty


def test_main_lts_failsoft_still_writes(tmp_path, monkeypatch):
    # CF_SUMMARY ok but the SEPARATE CF_LTS read fails -> lex_lts null, snapshot
    # still written (rc 0). The portfolio headline must not depend on CF_LTS.
    def _by_tab(tab_name=None):
        if tab_name == "CF_LTS":
            raise GsheetsConnectorError("LTS read failed")
        return _summary()

    monkeypatch.setattr(wcs.gf, "get_cashflow", _by_tab)
    out = tmp_path / "cashflow-latest.json"
    rc = wcs.main(["--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["lex_lts"] is None
    assert data["portfolio_ending_cash"] == 1347657.0


def test_main_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(wcs.gf, "get_cashflow", lambda tab_name=None: _summary())
    out = tmp_path / "cashflow-latest.json"
    rc = wcs.main(["--out", str(out), "--dry-run"])
    assert rc == 0
    assert not out.exists()


def test_main_failsoft_leaves_previous_snapshot(tmp_path, monkeypatch):
    out = tmp_path / "cashflow-latest.json"
    out.write_text('{"previous": true}', encoding="utf-8")

    def _boom(tab_name=None):
        raise GsheetsConnectorError("read failed")

    monkeypatch.setattr(wcs.gf, "get_cashflow", _boom)
    rc = wcs.main(["--out", str(out)])
    assert rc == 1
    # Previous snapshot untouched (no stale overwrite, no deletion).
    assert json.loads(out.read_text(encoding="utf-8")) == {"previous": True}


def test_main_failsoft_on_write_error(tmp_path, monkeypatch):
    # D-051: a WRITE error (e.g. Drive mount G: not present) must fail-soft
    # (return 1), not crash with an unhandled traceback.
    monkeypatch.setattr(wcs.gf, "get_cashflow", lambda tab_name=None: _summary())

    def _raise(path, payload):
        raise OSError("G: not mounted")

    monkeypatch.setattr(wcs, "_atomic_write_json", _raise)
    rc = wcs.main(["--out", str(tmp_path / "x.json")])
    assert rc == 1
