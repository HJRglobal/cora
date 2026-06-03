"""Tests for run_channel_health_monitor.py -- Feature #12."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import run_channel_health_monitor as chm  # noqa: E402


# ---------------------------------------------------------------------------
# _load_entity_channel_ids
# ---------------------------------------------------------------------------

def test_load_entity_channel_ids_returns_set():
    ids = chm._load_entity_channel_ids()
    assert isinstance(ids, set)
    assert len(ids) > 0


def test_load_entity_channel_ids_contains_known_channels():
    ids = chm._load_entity_channel_ids()
    assert "C0B3K67J10T" in ids   # hjrg-leadership
    assert "C0B4KRQT3LY" in ids   # f3e-leadership


def test_load_entity_channel_ids_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(chm, "ENTITY_CHANNELS_FILE", tmp_path / "missing.yaml")
    assert chm._load_entity_channel_ids() == set()


# ---------------------------------------------------------------------------
# _check_channel_activity
# ---------------------------------------------------------------------------

def test_check_channel_activity_active():
    mock_client = MagicMock()
    mock_client.conversations_history.return_value = {
        "messages": [{"text": "hello", "ts": "1234"}]
    }
    result = chm._check_channel_activity(mock_client, "C0B123", 30 * 86400)
    assert result is True


def test_check_channel_activity_dead():
    mock_client = MagicMock()
    mock_client.conversations_history.return_value = {"messages": []}
    result = chm._check_channel_activity(mock_client, "C0B123", 30 * 86400)
    assert result is False


def test_check_channel_activity_error_returns_true():
    mock_client = MagicMock()
    mock_client.conversations_history.side_effect = Exception("API error")
    result = chm._check_channel_activity(mock_client, "C0B123", 30 * 86400)
    assert result is True  # assume active on error


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------

def test_build_report_contains_date():
    from datetime import date
    report = chm.build_report(10, [], [])
    assert date.today().isoformat() in report


def test_build_report_no_dead_channels():
    report = chm.build_report(10, [], [])
    assert "none" in report.lower() or "Dead channels" in report


def test_build_report_lists_dead_channels():
    dead = [{"id": "C0BDEAD1", "name": "old-channel"}]
    report = chm.build_report(10, dead, [])
    assert "old-channel" in report
    assert "C0BDEAD1" in report


def test_build_report_lists_missing_channels():
    missing = [{"id": "C0BNEW1", "name": "new-channel"}]
    report = chm.build_report(10, [], missing)
    assert "new-channel" in report
    assert "entity-channels.yaml" in report


def test_build_report_healthy_count():
    dead = [{"id": "C0B1", "name": "dead1"}, {"id": "C0B2", "name": "dead2"}]
    report = chm.build_report(10, dead, [])
    assert "8 channels healthy" in report


def test_build_report_no_sheet_names():
    report = chm.build_report(5, [], [])
    assert "CF_" not in report
    assert "spreadsheet" not in report.lower()


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

def _make_channels(ids_names: list[tuple[str, str]]) -> list[dict]:
    return [
        {"id": id_, "name": name, "is_im": False, "is_mpim": False, "is_private": False}
        for id_, name in ids_names
    ]


def test_run_no_token_returns_early(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    result = chm.run(dry_run=True)
    assert result == {"channels_checked": 0, "dead": 0, "missing": 0}


def test_run_dry_run_no_post(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    channels = _make_channels([("C0B3K67J10T", "hjrg-leadership")])

    with patch.object(chm, "list_joined_channels", return_value=channels), \
         patch.object(chm, "_check_channel_activity", return_value=True), \
         patch("run_channel_health_monitor.WebClient") as mock_wc:
        client = MagicMock()
        mock_wc.return_value = client
        result = chm.run(dry_run=True)

    client.chat_postMessage.assert_not_called()
    assert result["channels_checked"] == 1


def test_run_posts_to_hjrg_leadership(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    channels = _make_channels([("C0B3K67J10T", "hjrg-leadership")])

    with patch.object(chm, "list_joined_channels", return_value=channels), \
         patch.object(chm, "_check_channel_activity", return_value=True), \
         patch("run_channel_health_monitor.WebClient") as mock_wc:
        client = MagicMock()
        mock_wc.return_value = client
        chm.run(dry_run=False)

    client.chat_postMessage.assert_called_once()
    call_kwargs = client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "C0B3K67J10T"


def test_run_detects_dead_channel(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    channels = _make_channels([("C0BDEAD", "ghost-channel")])

    with patch.object(chm, "list_joined_channels", return_value=channels), \
         patch.object(chm, "_check_channel_activity", return_value=False), \
         patch("run_channel_health_monitor.WebClient") as mock_wc:
        mock_wc.return_value = MagicMock()
        result = chm.run(dry_run=True)

    assert result["dead"] == 1


def test_run_detects_unmapped_channel(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    channels = _make_channels([("C0BUNKNOWN", "mystery-channel")])

    with patch.object(chm, "list_joined_channels", return_value=channels), \
         patch.object(chm, "_check_channel_activity", return_value=True), \
         patch("run_channel_health_monitor.WebClient") as mock_wc:
        mock_wc.return_value = MagicMock()
        result = chm.run(dry_run=True)

    assert result["missing"] == 1


def test_run_skips_dm_channels(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    # Mix of DM and regular channel
    channels = [
        {"id": "D0B1234", "name": "user", "is_im": True, "is_mpim": False},
        {"id": "C0BREAL", "name": "real-channel", "is_im": False, "is_mpim": False},
    ]

    with patch.object(chm, "list_joined_channels", return_value=channels), \
         patch.object(chm, "_check_channel_activity", return_value=True), \
         patch("run_channel_health_monitor.WebClient") as mock_wc:
        mock_wc.return_value = MagicMock()
        result = chm.run(dry_run=True)

    # Only 1 real channel (DM is skipped)
    assert result["channels_checked"] == 1


def test_run_handles_list_channels_error(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    from cora.connectors.slack_connector import SlackConnectorError

    with patch.object(chm, "list_joined_channels", side_effect=SlackConnectorError("fail")), \
         patch("run_channel_health_monitor.WebClient"):
        result = chm.run(dry_run=True)

    assert result == {"channels_checked": 0, "dead": 0, "missing": 0}
