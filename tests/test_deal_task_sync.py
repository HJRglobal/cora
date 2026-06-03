"""Tests for run_deal_task_sync.py -- Feature #15."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import run_deal_task_sync as sync  # noqa: E402


# ---------------------------------------------------------------------------
# _add_business_days
# ---------------------------------------------------------------------------

def test_add_business_days_skips_weekend():
    # Friday + 1 business day = Monday
    friday = date(2026, 6, 5)  # actual Friday
    result = sync._add_business_days(friday, 1)
    assert result.weekday() == 0  # Monday


def test_add_business_days_three():
    monday = date(2026, 6, 1)  # Monday
    result = sync._add_business_days(monday, 3)
    assert result == date(2026, 6, 4)  # Thursday


def test_add_business_days_zero():
    d = date(2026, 6, 3)
    assert sync._add_business_days(d, 0) == d


# ---------------------------------------------------------------------------
# _deal_url
# ---------------------------------------------------------------------------

def test_deal_url_format():
    url = sync._deal_url("12345")
    assert "246351746" in url
    assert "12345" in url
    assert url.startswith("https://")


# ---------------------------------------------------------------------------
# _load_state / _save_state
# ---------------------------------------------------------------------------

def test_load_state_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "STATE_PATH", tmp_path / "nonexistent.json")
    assert sync._load_state() == {}


def test_save_and_load_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "STATE_PATH", tmp_path / "state.json")
    state = {"deal_111": {"task_gid": "abc", "synced_at": 123, "stage_id": sync.PROPOSAL_STAGE_ID}}
    sync._save_state(state)
    loaded = sync._load_state()
    assert loaded == state


# ---------------------------------------------------------------------------
# _get_proposal_deals
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path, deals: list[dict]) -> Path:
    db_path = tmp_path / "deal_snapshots.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE deal_last_stage (
            deal_id TEXT PRIMARY KEY, deal_name TEXT, pipeline_id TEXT,
            stage_id TEXT, stage_name TEXT, amount TEXT, owner_id TEXT, last_seen_ts INTEGER
        )
    """)
    for d in deals:
        conn.execute(
            "INSERT INTO deal_last_stage VALUES (?,?,?,?,?,?,?,?)",
            (d["deal_id"], d.get("deal_name", "Test"), d.get("pipeline_id", "p1"),
             d.get("stage_id", ""), d.get("stage_name", ""), d.get("amount", "0"),
             d.get("owner_id", ""), d.get("last_seen_ts", int(time.time()))),
        )
    conn.commit()
    conn.close()
    return db_path


def test_get_proposal_deals_returns_matching(tmp_path, monkeypatch):
    db = _make_db(tmp_path, [
        {"deal_id": "d1", "stage_id": sync.PROPOSAL_STAGE_ID, "deal_name": "Deal A"},
        {"deal_id": "d2", "stage_id": "other_stage", "deal_name": "Deal B"},
    ])
    monkeypatch.setattr(sync, "DB_PATH", db)
    deals = sync._get_proposal_deals()
    assert len(deals) == 1
    assert deals[0]["deal_id"] == "d1"


def test_get_proposal_deals_no_db(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "DB_PATH", tmp_path / "missing.db")
    assert sync._get_proposal_deals() == []


# ---------------------------------------------------------------------------
# _load_asana_map_from_slack
# ---------------------------------------------------------------------------

def test_load_asana_map_from_slack_cross_references():
    result = sync._load_asana_map_from_slack()
    # Harrison is in both files: hubspot_owner_id=160459333, slack=U0B2RM2JYJ1, asana=1204525779609669
    assert "160459333" in result
    assert result["160459333"] == "1204525779609669"


# ---------------------------------------------------------------------------
# run() -- integration with mocks
# ---------------------------------------------------------------------------

def _make_proposal_deal(deal_id="d100", owner_id="160459333", amount="5000"):
    return {
        "deal_id": deal_id,
        "deal_name": f"Test Deal {deal_id}",
        "pipeline_id": "2313722582",
        "stage_id": sync.PROPOSAL_STAGE_ID,
        "stage_name": "Proposal",
        "amount": amount,
        "owner_id": owner_id,
        "last_seen_ts": int(time.time()),
    }


def test_run_dry_run_no_asana_call(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "STATE_PATH", tmp_path / "state.json")
    deal = _make_proposal_deal()

    with patch.object(sync, "_get_proposal_deals", return_value=[deal]), \
         patch.object(sync, "_get_all_deals", return_value=[deal]), \
         patch("run_deal_task_sync.create_task") as mock_ct:
        result = sync.run(dry_run=True)

    mock_ct.assert_not_called()
    assert result["tasks_created"] == 1


def test_run_creates_asana_task(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "STATE_PATH", tmp_path / "state.json")
    deal = _make_proposal_deal()
    mock_task = {"gid": "T999", "permalink_url": "https://app.asana.com/0/0/T999"}

    with patch.object(sync, "_get_proposal_deals", return_value=[deal]), \
         patch.object(sync, "_get_all_deals", return_value=[deal]), \
         patch("run_deal_task_sync.create_task", return_value=mock_task) as mock_ct:
        result = sync.run(dry_run=False)

    mock_ct.assert_called_once()
    call_kwargs = mock_ct.call_args.kwargs
    assert "Send proposal for" in call_kwargs["name"]
    assert result["tasks_created"] == 1


def test_run_skips_already_synced_deal(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "STATE_PATH", tmp_path / "state.json")
    deal = _make_proposal_deal(deal_id="d200")

    # Pre-populate state with recent sync
    sync._save_state({
        "d200": {
            "task_gid": "T_OLD",
            "synced_at": int(time.time()) - 3600,  # 1 hour ago
            "stage_id": sync.PROPOSAL_STAGE_ID,
        }
    })

    with patch.object(sync, "_get_proposal_deals", return_value=[deal]), \
         patch.object(sync, "_get_all_deals", return_value=[deal]), \
         patch("run_deal_task_sync.create_task") as mock_ct:
        result = sync.run(dry_run=False)

    mock_ct.assert_not_called()
    assert result["skipped"] == 1


def test_run_task_name_contains_deal_name(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "STATE_PATH", tmp_path / "state.json")
    deal = _make_proposal_deal()
    deal["deal_name"] = "American Discount Foods"
    mock_task = {"gid": "T111"}

    with patch.object(sync, "_get_proposal_deals", return_value=[deal]), \
         patch.object(sync, "_get_all_deals", return_value=[deal]), \
         patch("run_deal_task_sync.create_task", return_value=mock_task) as mock_ct:
        sync.run(dry_run=False)

    name = mock_ct.call_args.kwargs["name"]
    assert "American Discount Foods" in name


def test_run_due_date_is_3_business_days_out(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "STATE_PATH", tmp_path / "state.json")
    deal = _make_proposal_deal()
    mock_task = {"gid": "T222"}

    with patch.object(sync, "_get_proposal_deals", return_value=[deal]), \
         patch.object(sync, "_get_all_deals", return_value=[deal]), \
         patch("run_deal_task_sync.create_task", return_value=mock_task) as mock_ct:
        sync.run(dry_run=False)

    due = mock_ct.call_args.kwargs["due_on"]
    expected = sync._add_business_days(date.today(), 3).isoformat()
    assert due == expected


def test_run_no_proposal_deals(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "STATE_PATH", tmp_path / "state.json")

    with patch.object(sync, "_get_proposal_deals", return_value=[]), \
         patch.object(sync, "_get_all_deals", return_value=[]), \
         patch("run_deal_task_sync.create_task") as mock_ct:
        result = sync.run(dry_run=False)

    mock_ct.assert_not_called()
    assert result == {"deals_checked": 0, "tasks_created": 0, "skipped": 0}


def test_run_handles_asana_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "STATE_PATH", tmp_path / "state.json")
    deal = _make_proposal_deal()

    from cora.tools.asana_client import AsanaClientError
    with patch.object(sync, "_get_proposal_deals", return_value=[deal]), \
         patch.object(sync, "_get_all_deals", return_value=[deal]), \
         patch("run_deal_task_sync.create_task", side_effect=AsanaClientError("fail")):
        result = sync.run(dry_run=False)

    assert result["tasks_created"] == 0


def test_run_state_cleanup_when_deal_leaves_proposal(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "STATE_PATH", tmp_path / "state.json")

    # State says deal was in Proposal
    sync._save_state({
        "d300": {
            "task_gid": "T_OLD",
            "synced_at": int(time.time()) - 3600,
            "stage_id": sync.PROPOSAL_STAGE_ID,
        }
    })

    # Deal is now in a different stage
    all_deals = [{"deal_id": "d300", "stage_id": "won_stage"}]

    with patch.object(sync, "_get_proposal_deals", return_value=[]), \
         patch.object(sync, "_get_all_deals", return_value=all_deals):
        result = sync.run(dry_run=False)

    # State should reflect updated stage
    loaded = sync._load_state()
    assert loaded["d300"]["stage_id"] == "won_stage"
