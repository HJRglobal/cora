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
# _build_archive_candidates -- "-N" duplicate pairs ONLY (sprawl is demoted)
# ---------------------------------------------------------------------------

def test_archive_candidates_include_duplicates():
    dups = [{"id": "C2", "name": "osn-2", "base": "osn", "base_id": "C1"}]
    out = chm._build_archive_candidates(dups, dead_ids=set())
    assert len(out) == 1
    assert out[0]["id"] == "C2"
    assert "duplicate of #osn" in out[0]["reason"]


def test_archive_candidates_dead_dup_active_base():
    # The '-N' dup is the dead one -> candidate is the dup; line names #osn active.
    dups = [{"id": "C2", "name": "osn-2", "base": "osn", "base_id": "C1"}]
    out = chm._build_archive_candidates(dups, dead_ids={"C2"})
    assert out[0]["id"] == "C2"
    assert "dead 30d" in out[0]["reason"]
    assert "#osn active" in out[0]["reason"]


def test_archive_candidates_inverted_dead_base_active_dup():
    # CONFIRMED MEDIUM fix: team migrated TO #osn-2; #osn (base) is the dead one.
    # The candidate must be the ABANDONED ORIGINAL #osn, NOT the live #osn-2.
    dups = [{"id": "C2", "name": "osn-2", "base": "osn", "base_id": "C1"}]
    out = chm._build_archive_candidates(dups, dead_ids={"C1"})
    assert out[0]["id"] == "C1"            # the dead original, not the live dup
    assert out[0]["name"] == "osn"
    assert "abandoned original" in out[0]["reason"]
    assert "#osn-2" in out[0]["reason"]    # names the live one so it can't mislead


def test_archive_candidates_exclude_sprawl():
    # Sprawl is NO LONGER an archive candidate -- duplicates only.
    dups = [{"id": "C2", "name": "osn-2", "base": "osn", "base_id": "C1"}]
    out = chm._build_archive_candidates(dups, dead_ids=set())
    assert [c["id"] for c in out] == ["C2"]


# ---------------------------------------------------------------------------
# _build_sprawl_review -- INFORMATIONAL (dead + unmapped Cora-created sprawl)
# ---------------------------------------------------------------------------

def test_sprawl_review_includes_dead_unmapped():
    sprawl = [
        {"id": "C3", "name": "events-mood", "created": 0},   # dead + unmapped -> review
        {"id": "C4", "name": "events-pure", "created": 0},   # active -> excluded
    ]
    out = chm._build_sprawl_review(sprawl, dead_ids={"C3"})
    assert [c["id"] for c in out] == ["C3"]
    assert out[0]["name"] == "events-mood"


def test_sprawl_review_excludes_dead_but_routed():
    # A Cora-created leadership channel (real entity route) that went quiet must NOT
    # appear -- a route means it was adopted for real work.
    sprawl = [{"id": "C5", "name": "llc-leadership", "created": 0}]
    out = chm._build_sprawl_review(sprawl, dead_ids={"C5"})
    assert out == []


def test_sprawl_review_excludes_active():
    sprawl = [{"id": "C6", "name": "tucson-site-launch", "created": 0}]
    out = chm._build_sprawl_review(sprawl, dead_ids=set())
    assert out == []


# ---------------------------------------------------------------------------
# _check_channel_activity -- newest NON-system message vs the 30d cutoff
# ---------------------------------------------------------------------------

def _ts(days_ago: float) -> str:
    import time
    return str(time.time() - days_ago * 86400)


def _hist(*messages):
    mock = MagicMock()
    mock.conversations_history.return_value = {"messages": list(messages)}
    return mock


def test_check_channel_activity_recent_real_message_active():
    client = _hist({"text": "hi", "ts": _ts(0.5)})
    assert chm._check_channel_activity(client, "C0B123", 30 * 86400) is True


def test_check_channel_activity_old_real_message_dead():
    client = _hist({"text": "stale", "ts": _ts(40)})
    assert chm._check_channel_activity(client, "C0B123", 30 * 86400) is False


def test_check_channel_activity_recent_join_old_real_is_dead():
    # The #bdm case proved live: newest entry is a channel_join (recent), but the
    # newest REAL message is 40d old -> dead. System events must not count.
    client = _hist(
        {"subtype": "channel_join", "ts": _ts(18)},
        {"text": "last real post", "ts": _ts(40)},
    )
    assert chm._check_channel_activity(client, "C0B123", 30 * 86400) is False


def test_check_channel_activity_skips_system_to_recent_real():
    client = _hist(
        {"subtype": "channel_join", "ts": _ts(2)},
        {"text": "recent real", "ts": _ts(3)},
    )
    assert chm._check_channel_activity(client, "C0B123", 30 * 86400) is True


def test_check_channel_activity_only_system_messages_dead():
    client = _hist(
        {"subtype": "channel_join", "ts": _ts(1)},
        {"subtype": "channel_purpose", "ts": _ts(1)},
    )
    assert chm._check_channel_activity(client, "C0B123", 30 * 86400) is False


def test_check_channel_activity_empty_dead():
    assert chm._check_channel_activity(_hist(), "C0B123", 30 * 86400) is False


def test_check_channel_activity_error_returns_true():
    client = MagicMock()
    client.conversations_history.side_effect = Exception("API error")
    assert chm._check_channel_activity(client, "C0B123", 30 * 86400) is True


def test_check_channel_activity_unparseable_ts_returns_true():
    client = _hist({"text": "weird", "ts": "not-a-number"})
    assert chm._check_channel_activity(client, "C0B123", 30 * 86400) is True


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


def test_build_report_lists_sprawl_review():
    review = [{"id": "C0BSPR", "name": "tucson-site-launch"}]
    report = chm.build_report(10, [], [], sprawl_review=review)
    assert "Sprawl review" in report
    assert "NOT archive recommendations" in report
    assert "tucson-site-launch" in report


def test_build_report_no_sprawl_review_section_when_empty():
    # An empty sprawl-review must NOT add a header (keeps the weekly post tight).
    report = chm.build_report(10, [], [])
    assert "Sprawl review" not in report
    assert "0 sprawl-review" in report  # but the summary line still reports the count


def test_build_report_caps_archive_candidates():
    cands = [{"id": f"C{i:03d}", "name": f"cand-{i}", "reason": "duplicate of #x"} for i in range(20)]
    report = chm.build_report(100, [], [], archive_candidates=cands, full_report_path="logs/x.md")
    assert "cand-0 (" in report           # first shown
    assert "cand-19" not in report        # beyond the 15-item preview cap
    assert "...and 5 more" in report      # 20 - 15
    assert "logs/x.md" in report          # full list referenced


def test_write_full_list_renders_all_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(chm, "_REPO_ROOT", tmp_path)
    path = chm._write_full_list(
        dead_channels=[{"id": "C1", "name": "dead1"}],
        unmapped_channels=[{"id": "C2", "name": "unmapped1"}],
        duplicates=[{"id": "C3", "name": "x-2", "base": "x", "base_id": "C9"}],
        sprawl=[{"id": "C4", "name": "sprawl1", "created": 0}],
        archive_candidates=[{"id": "C3", "name": "x-2", "reason": "duplicate of #x"}],
    )
    text = path.read_text(encoding="utf-8")
    assert "Archive candidates" in text
    assert "Dead channels" in text
    assert "Unmapped channels (no route in channel-routing.yaml)" in text
    assert "duplicate channels" in text
    assert "Cora-created since 2026-06-03" in text
    for name in ("dead1", "unmapped1", "x-2", "sprawl1"):
        assert name in text


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
    assert result == chm._zero_result()
    # uniform 6-key shape on every path (no KeyError for a future consumer)
    assert set(result) == {
        "channels_checked", "dead", "missing", "duplicates",
        "sprawl", "archive_candidates", "sprawl_review",
    }


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

    assert result == chm._zero_result()


def test_run_excludes_denied_channels(monkeypatch):
    # Deny-listed sensitive channels (slack-sweep-policy.yaml) must NOT appear in
    # the monitor report at all -- no "add a route", no weekly name re-broadcast.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    channels = _make_channels([
        ("C0BLBHS", "lbhs-leadership"),          # denied (lbhs* glob + by id-list)
        ("C0BKIDS", "kids-schedules-and-tasks"),  # denied (by name)
        ("C0BREAL", "f3e-leadership"),            # kept
    ])
    with patch.object(chm, "list_joined_channels", return_value=channels), \
         patch.object(chm, "_check_channel_activity", return_value=True), \
         patch("slack_sdk.WebClient") as mock_wc:
        mock_wc.return_value = MagicMock()
        result = chm.run(dry_run=True)

    assert result["channels_checked"] == 1  # only f3e-leadership survives the deny-list


def test_run_integration_duplicates_sprawl_archive(monkeypatch):
    # End-to-end wiring inside run(): a base+'-2' pair feeds archive_candidates;
    # a Cora-created dead unmapped channel feeds the INFORMATIONAL sprawl_review
    # (NOT archive_candidates -- the post-merge demote).
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    after = _after_cutoff()
    channels = [
        {"id": "C_BASE", "name": "retail-portfolio", "is_im": False, "is_mpim": False, "is_private": False},
        {"id": "C_DUP", "name": "retail-portfolio-2", "is_im": False, "is_mpim": False, "is_private": False},
        {"id": "C_SPR", "name": "events-mood", "is_im": False, "is_mpim": False, "is_private": False,
         "creator": chm.CORA_BOT_USER_ID, "created": after},
    ]
    dead = {"C_DUP", "C_SPR"}  # base active; dup + sprawl dead

    def _activity(_client, ch_id, _lookback):
        return ch_id not in dead

    with patch.object(chm, "list_joined_channels", return_value=channels), \
         patch.object(chm, "_check_channel_activity", side_effect=_activity), \
         patch("slack_sdk.WebClient") as mock_wc:
        mock_wc.return_value = MagicMock()
        result = chm.run(dry_run=True)

    assert result["channels_checked"] == 3
    assert result["duplicates"] == 1          # retail-portfolio-2
    assert result["sprawl"] == 1              # events-mood
    assert result["archive_candidates"] == 1  # the dup pair only
    assert result["sprawl_review"] == 1       # events-mood (dead + unmapped, informational)
