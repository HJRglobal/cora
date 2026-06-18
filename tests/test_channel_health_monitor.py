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
# Unmapped = router catch-all (NOT entity-channels.yaml). This was the ~10x
# over-report bug: every well-routed operational/sub channel read "unmapped"
# because it wasn't in entity-channels.yaml's ~22 leadership/finance IDs.
# ---------------------------------------------------------------------------

def test_routed_operational_channels_are_not_unmapped():
    from cora import entity_router
    # None of these are in entity-channels.yaml, but all have a real route.
    for name in ("osn-recon-pilot", "f3-pure-launch", "bdm-osn", "llc-operations"):
        assert entity_router.is_mapped(name) is True, name


def test_truly_unrouted_channel_is_unmapped():
    from cora import entity_router
    assert entity_router.is_mapped("mystery-channel-xyz") is False


# ---------------------------------------------------------------------------
# _find_duplicate_channels
# ---------------------------------------------------------------------------

def test_find_duplicates_flags_dash_n_when_base_exists():
    channels = [
        {"id": "C1", "name": "retail-portfolio"},
        {"id": "C2", "name": "retail-portfolio-2"},
        {"id": "C3", "name": "osn"},
        {"id": "C4", "name": "osn-2"},
    ]
    dups = chm._find_duplicate_channels(channels)
    ids = {d["id"] for d in dups}
    assert ids == {"C2", "C4"}
    assert {d["base"] for d in dups} == {"retail-portfolio", "osn"}


def test_find_duplicates_skips_dash_n_without_base():
    # No bare #ecom-portfolio joined -> the -2 is not a duplicate of a known channel.
    channels = [{"id": "C1", "name": "ecom-portfolio-2"}]
    assert chm._find_duplicate_channels(channels) == []


def test_find_duplicates_ignores_building_and_store_codes():
    # #hjrp-1337 / #hjrp-1555 must NOT be read as duplicates of #hjrp.
    channels = [
        {"id": "C1", "name": "hjrp"},
        {"id": "C2", "name": "hjrp-1337"},
        {"id": "C3", "name": "hjrp-1555"},
    ]
    assert chm._find_duplicate_channels(channels) == []


# ---------------------------------------------------------------------------
# _find_sprawl_channels
# ---------------------------------------------------------------------------

def _after_cutoff() -> int:
    return int(chm.SPRAWL_CUTOFF_EPOCH) + 86400


def _before_cutoff() -> int:
    return int(chm.SPRAWL_CUTOFF_EPOCH) - 86400


def test_sprawl_cutoff_is_2026_06_03_az():
    from datetime import datetime, timedelta, timezone
    expected = datetime(2026, 6, 3, tzinfo=timezone(timedelta(hours=-7))).timestamp()
    assert chm.SPRAWL_CUTOFF_EPOCH == expected


def test_find_sprawl_flags_cora_created_after_cutoff():
    channels = [
        {"id": "C1", "name": "events-mood", "creator": chm.CORA_BOT_USER_ID, "created": _after_cutoff()},
    ]
    sprawl = chm._find_sprawl_channels(channels)
    assert [s["id"] for s in sprawl] == ["C1"]


def test_find_sprawl_skips_other_creator():
    channels = [
        {"id": "C1", "name": "f3e-leadership", "creator": "U0HARRISON", "created": _after_cutoff()},
    ]
    assert chm._find_sprawl_channels(channels) == []


def test_find_sprawl_skips_before_cutoff():
    # #cora-kq-* were created by Cora on 2026-05-30, before the sprawl wave.
    channels = [
        {"id": "C1", "name": "cora-kq-f3e", "creator": chm.CORA_BOT_USER_ID, "created": _before_cutoff()},
    ]
    assert chm._find_sprawl_channels(channels) == []


def test_find_sprawl_skips_missing_metadata():
    channels = [{"id": "C1", "name": "legacy-channel"}]
    assert chm._find_sprawl_channels(channels) == []


# ---------------------------------------------------------------------------
# _build_archive_candidates
# ---------------------------------------------------------------------------

def test_archive_candidates_always_include_duplicates():
    dups = [{"id": "C2", "name": "osn-2", "base": "osn"}]
    out = chm._build_archive_candidates(dups, [], dead_ids=set())
    assert len(out) == 1
    assert out[0]["id"] == "C2"
    assert "duplicate of #osn (active)" in out[0]["reason"]


def test_archive_candidates_mark_dead_duplicates():
    dups = [{"id": "C2", "name": "osn-2", "base": "osn"}]
    out = chm._build_archive_candidates(dups, [], dead_ids={"C2"})
    assert "dead 30d" in out[0]["reason"]


def test_archive_candidates_include_dead_sprawl_only():
    sprawl = [
        {"id": "C3", "name": "events-mood", "created": 0},   # dead -> candidate
        {"id": "C4", "name": "llc-leadership", "created": 0},  # active -> excluded
    ]
    out = chm._build_archive_candidates([], sprawl, dead_ids={"C3"})
    assert [c["id"] for c in out] == ["C3"]
    assert "sprawl" in out[0]["reason"]


def test_archive_candidates_dedup_dup_and_sprawl():
    dups = [{"id": "C2", "name": "osn-2", "base": "osn"}]
    sprawl = [{"id": "C2", "name": "osn-2", "created": 0}]
    out = chm._build_archive_candidates(dups, sprawl, dead_ids={"C2"})
    assert [c["id"] for c in out] == ["C2"]  # appears once, as the duplicate


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


def test_build_report_lists_unmapped_channels():
    unmapped = [{"id": "C0BNEW1", "name": "new-channel"}]
    report = chm.build_report(10, [], unmapped)
    assert "new-channel" in report
    assert "channel-routing.yaml" in report
    assert "entity-channels.yaml" not in report  # old (wrong) source must be gone


def test_build_report_lists_archive_candidates():
    cands = [{"id": "C0BDUP", "name": "osn-2", "reason": "duplicate of #osn (dead 30d)"}]
    report = chm.build_report(10, [], [], archive_candidates=cands)
    assert "Archive candidates" in report
    assert "osn-2" in report
    assert "duplicate of #osn" in report


def test_build_report_no_archive_candidates():
    report = chm.build_report(10, [], [])
    assert "Archive candidates:* none" in report


def test_build_report_healthy_count():
    dead = [{"id": "C0B1", "name": "dead1"}, {"id": "C0B2", "name": "dead2"}]
    report = chm.build_report(10, dead, [])
    assert "8 channels healthy" in report


def test_build_report_no_sheet_names():
    report = chm.build_report(5, [], [])
    assert "CF_" not in report
    assert "spreadsheet" not in report.lower()


def test_build_report_caps_long_lists():
    # audit N9: a 400+ line raw dump must become a capped summary + file reference
    dead = [{"id": f"C{i:05d}", "name": f"dead-{i}"} for i in range(40)]
    report = chm.build_report(100, dead, [], full_report_path="logs/channel-health-x.md")
    assert "dead-0 (" in report                  # first item shown
    assert "dead-39" not in report               # beyond the preview cap
    assert "...and 25 more" in report            # 40 - 15 = 25
    assert "logs/channel-health-x.md" in report  # full list referenced
    assert "40 total" in report                  # total count surfaced


def test_build_report_short_list_not_capped():
    report = chm.build_report(10, [{"id": "C1", "name": "only-one"}], [])
    assert "only-one" in report
    assert "...and" not in report                # no truncation marker for short lists


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
         patch("slack_sdk.WebClient") as mock_wc:
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
         patch("slack_sdk.WebClient") as mock_wc:
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
         patch("slack_sdk.WebClient") as mock_wc:
        mock_wc.return_value = MagicMock()
        result = chm.run(dry_run=True)

    assert result["dead"] == 1


def test_run_detects_unmapped_channel(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    channels = _make_channels([("C0BUNKNOWN", "mystery-channel")])

    with patch.object(chm, "list_joined_channels", return_value=channels), \
         patch.object(chm, "_check_channel_activity", return_value=True), \
         patch("slack_sdk.WebClient") as mock_wc:
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
         patch("slack_sdk.WebClient") as mock_wc:
        mock_wc.return_value = MagicMock()
        result = chm.run(dry_run=True)

    # Only 1 real channel (DM is skipped)
    assert result["channels_checked"] == 1


def test_run_handles_list_channels_error(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    from cora.connectors.slack_connector import SlackConnectorError

    with patch.object(chm, "list_joined_channels", side_effect=SlackConnectorError("fail")), \
         patch("slack_sdk.WebClient"):
        result = chm.run(dry_run=True)

    assert result == {"channels_checked": 0, "dead": 0, "missing": 0}
