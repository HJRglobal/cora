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


# ── build_snapshot ──────────────────────────────────────────────────────────

def test_build_snapshot_shape_and_labels():
    snap = wcs.build_snapshot(_summary(), "2026-06-19T21:00:00+00:00", weeks=2)
    assert snap["generated_at_utc"] == "2026-06-19T21:00:00+00:00"
    assert snap["week_label"] == "Week of 6/09/2026"
    assert snap["as_of_date"] == "2026-06-16"
    assert snap["portfolio_ending_cash"] == 1347657.0
    # entities carry both canonical code + human label
    codes = {e["code"] for e in snap["entities"]}
    assert {"F3E", "LEX-LTS"} <= codes
    # outlook = current + next 2 weeks
    assert [o["week"] for o in snap["ending_cash_outlook"]] == ["6/09/2026", "6/16/2026", "6/23/2026"]
    # lex_lts convenience pointer
    assert snap["lex_lts"]["code"] == "LEX-LTS"
    assert snap["lex_lts"]["actual"] == 4200.0


def test_build_snapshot_lts_none_when_absent():
    s = _summary()
    s.entities = [e for e in s.entities if e.entity_code != "LEX-LTS"]
    snap = wcs.build_snapshot(s, "2026-06-19T21:00:00+00:00")
    assert snap["lex_lts"] is None


def test_build_snapshot_is_source_opaque():
    snap = wcs.build_snapshot(_summary(), "2026-06-19T21:00:00+00:00")
    blob = json.dumps(snap).lower()
    # no file id, sheet/tab name, or drive link leaks into the snapshot
    for forbidden in ("cf_summary", "spreadsheet", "drive.google", "docs.google", "1bkmfet"):
        assert forbidden not in blob


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
