"""Unit tests for instagram_monitor.py and the handle registry / detection log."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cora.tools.influencer_client as ic
from cora.connectors import instagram_monitor as ig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    """Isolate DB and watermark file per test."""
    monkeypatch.setattr(ic, "_DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(ig, "_WATERMARK_PATH", tmp_path / "watermarks.json")
    yield


# ---------------------------------------------------------------------------
# Handle registry
# ---------------------------------------------------------------------------

class TestRegisterHandle:
    def test_happy_path(self):
        row = ic.register_handle(athlete_name="Luis Pena", platform="Instagram", handle="@luispena_ufc")
        assert row["athlete_name"] == "Luis Pena"
        assert row["platform"] == "instagram"
        assert row["handle"] == "luispena_ufc"  # @ stripped, lowercased

    def test_handle_stored_without_at(self):
        ic.register_handle(athlete_name="A", platform="instagram", handle="@testhandle")
        row = ic.get_athlete_by_handle("instagram", "@testhandle")
        assert row is not None
        assert row["handle"] == "testhandle"

    def test_upsert_updates_athlete_name(self):
        ic.register_handle(athlete_name="Old Name", platform="instagram", handle="samehandle")
        ic.register_handle(athlete_name="New Name", platform="instagram", handle="samehandle")
        row = ic.get_athlete_by_handle("instagram", "samehandle")
        assert row["athlete_name"] == "New Name"

    def test_missing_athlete_raises(self):
        with pytest.raises(ic.InfluencerClientError, match="athlete_name"):
            ic.register_handle(athlete_name="", platform="instagram", handle="handle")

    def test_missing_handle_raises(self):
        with pytest.raises(ic.InfluencerClientError, match="handle"):
            ic.register_handle(athlete_name="A", platform="instagram", handle="")

    def test_get_unknown_handle_returns_none(self):
        assert ic.get_athlete_by_handle("instagram", "nobody") is None

    def test_list_handles_entity_filter(self):
        ic.register_handle(athlete_name="F3E Star", platform="instagram", handle="f3estar", entity="F3E")
        ic.register_handle(athlete_name="UFL Fighter", platform="instagram", handle="uflfighter", entity="UFL")
        f3e = ic.list_handles(entity="F3E")
        ufl = ic.list_handles(entity="UFL")
        assert all(r["entity"] == "F3E" for r in f3e)
        assert all(r["entity"] == "UFL" for r in ufl)

    def test_list_handles_platform_filter(self):
        ic.register_handle(athlete_name="A", platform="instagram", handle="ig_a")
        ic.register_handle(athlete_name="A", platform="tiktok", handle="tt_a")
        ig_rows = ic.list_handles(platform="instagram")
        assert all(r["platform"] == "instagram" for r in ig_rows)


# ---------------------------------------------------------------------------
# Detection log
# ---------------------------------------------------------------------------

class TestDetectionLog:
    def test_is_not_detected_initially(self):
        assert not ic.is_already_detected("instagram", "post_123")

    def test_log_and_detect(self):
        ic.log_detection(platform="instagram", post_id="post_abc", brand_handle="f3energyofficial")
        assert ic.is_already_detected("instagram", "post_abc")

    def test_duplicate_log_silently_ignored(self):
        ic.log_detection(platform="instagram", post_id="dupe_1", brand_handle="f3energyofficial")
        ic.log_detection(platform="instagram", post_id="dupe_1", brand_handle="f3energyofficial")  # no error
        assert ic.is_already_detected("instagram", "dupe_1")

    def test_mark_notified(self):
        ic.log_detection(platform="instagram", post_id="note_1", brand_handle="f3energyofficial")
        ic.mark_detection_notified("instagram", "note_1")
        # No assertion API exposed — just verifying no exception raised and idempotent
        ic.mark_detection_notified("instagram", "note_1")

    def test_different_platforms_are_distinct(self):
        ic.log_detection(platform="instagram", post_id="shared_id", brand_handle="f3e")
        assert ic.is_already_detected("instagram", "shared_id")
        assert not ic.is_already_detected("tiktok", "shared_id")


# ---------------------------------------------------------------------------
# Watermark helpers
# ---------------------------------------------------------------------------

class TestWatermarks:
    def test_get_watermark_none_initially(self):
        assert ig.get_watermark("instagram", "f3energyofficial") is None

    def test_set_and_get_watermark(self):
        ig.set_watermark("instagram", "f3energyofficial", "2026-05-23T10:00:00Z")
        result = ig.get_watermark("instagram", "f3energyofficial")
        assert result == "2026-05-23T10:00:00Z"

    def test_watermarks_are_per_account(self):
        ig.set_watermark("instagram", "f3energyofficial", "2026-05-23T10:00:00Z")
        ig.set_watermark("instagram", "f3pure", "2026-05-23T11:00:00Z")
        assert ig.get_watermark("instagram", "f3energyofficial") == "2026-05-23T10:00:00Z"
        assert ig.get_watermark("instagram", "f3pure") == "2026-05-23T11:00:00Z"


# ---------------------------------------------------------------------------
# scan_tagged_media — mocked Graph API
# ---------------------------------------------------------------------------

_MOCK_TAGGED_RESPONSE = {
    "data": [
        {
            "id": "post_001",
            "media_type": "REEL",
            "timestamp": "2026-05-23T15:00:00+0000",
            "permalink": "https://www.instagram.com/p/post001/",
            "caption": "Training hard with @F3EnergyOfficial #F3Energy #ad",
            "username": "luispena_ufc",
        },
        {
            "id": "post_002",
            "media_type": "IMAGE",
            "timestamp": "2026-05-20T10:00:00+0000",  # older — should be filtered by since
            "permalink": "https://www.instagram.com/p/post002/",
            "caption": "Old post",
            "username": "anotherathlte",
        },
    ]
}


class TestScanTaggedMedia:
    def _mock_get(self, path, params, token):
        return _MOCK_TAGGED_RESPONSE

    def test_returns_new_posts_since_watermark(self):
        with patch.object(ig, "_graph_get", side_effect=self._mock_get):
            results = ig.scan_tagged_media(
                ig_user_id="123",
                access_token="token",
                brand_handle="f3energyofficial",
                since_timestamp="2026-05-22T00:00:00+0000",
            )
        # post_001 is after the since timestamp; post_002 is before → only 1 result
        assert len(results) == 1
        assert results[0]["post_id"] == "post_001"
        assert results[0]["username"] == "luispena_ufc"
        assert results[0]["media_type"] == "reel"

    def test_returns_all_when_no_watermark(self):
        with patch.object(ig, "_graph_get", side_effect=self._mock_get):
            results = ig.scan_tagged_media(
                ig_user_id="123",
                access_token="token",
                brand_handle="f3energyofficial",
                since_timestamp=None,
            )
        assert len(results) == 2

    def test_api_error_returns_empty_list(self):
        def _fail(path, params, token):
            raise ig.InstagramMonitorError("403 forbidden")

        with patch.object(ig, "_graph_get", side_effect=_fail):
            results = ig.scan_tagged_media(
                ig_user_id="123", access_token="token", brand_handle="f3energyofficial"
            )
        assert results == []


# ---------------------------------------------------------------------------
# scan_hashtag — mocked Graph API
# ---------------------------------------------------------------------------

_MOCK_HASHTAG_ID_RESPONSE = {"data": [{"id": "ht_987"}]}
_MOCK_HASHTAG_MEDIA_RESPONSE = {
    "data": [
        {
            "id": "ht_post_001",
            "media_type": "REEL",
            "timestamp": "2026-05-23T16:00:00+0000",
            "permalink": "https://www.instagram.com/p/htpost001/",
            "caption": "Feeling the #F3Energy boost today! #workout",
        }
    ]
}


class TestScanHashtag:
    def _mock_get_hashtag(self, path, params, token):
        if "ig_hashtag_search" in path:
            return _MOCK_HASHTAG_ID_RESPONSE
        return _MOCK_HASHTAG_MEDIA_RESPONSE

    def test_returns_hashtag_posts(self):
        with patch.object(ig, "_graph_get", side_effect=self._mock_get_hashtag):
            results = ig.scan_hashtag(
                ig_user_id="123",
                access_token="token",
                hashtag="F3Energy",
                brand_handle="f3energyofficial",
            )
        assert len(results) == 1
        assert results[0]["post_id"] == "ht_post_001"
        assert results[0]["username"] == ""  # not available via hashtag search

    def test_hashtag_search_failure_returns_empty(self):
        def _fail(path, params, token):
            raise ig.InstagramMonitorError("rate limited")

        with patch.object(ig, "_graph_get", side_effect=_fail):
            results = ig.scan_hashtag(
                ig_user_id="123", access_token="token",
                hashtag="F3Energy", brand_handle="f3energyofficial",
            )
        assert results == []


# ---------------------------------------------------------------------------
# format_detection_for_slack
# ---------------------------------------------------------------------------

class TestFormatDetection:
    def test_known_athlete_shows_name(self):
        det = {
            "post_id": "x1",
            "media_type": "reel",
            "timestamp": "2026-05-23T15:00:00+0000",
            "permalink": "https://www.instagram.com/p/x1/",
            "caption": "Big gains with @F3EnergyOfficial",
            "username": "luispena_ufc",
            "detection_method": "tagged_media",
            "brand_handle": "f3energyofficial",
        }
        text = ig.format_detection_for_slack(det, athlete_name="Luis Pena", brand_display_name="F3 Energy")
        assert "Luis Pena" in text
        assert "Reel" in text
        assert "instagram.com" in text
        assert "registered" in text

    def test_unknown_handle_shows_warning(self):
        det = {
            "post_id": "x2",
            "media_type": "image",
            "timestamp": "2026-05-23T14:00:00+0000",
            "permalink": "https://www.instagram.com/p/x2/",
            "caption": "",
            "username": "unknown_athlete",
            "detection_method": "tagged_media",
            "brand_handle": "f3energyofficial",
        }
        text = ig.format_detection_for_slack(det, athlete_name=None, brand_display_name="F3 Energy")
        assert "not in handle registry" in text
        assert "unknown_athlete" in text

    def test_hashtag_detection_no_username(self):
        det = {
            "post_id": "x3",
            "media_type": "reel",
            "timestamp": "2026-05-23T13:00:00+0000",
            "permalink": "https://www.instagram.com/p/x3/",
            "caption": "#F3Pure love",
            "username": "",
            "detection_method": "hashtag:#F3Pure",
            "brand_handle": "f3pure",
        }
        text = ig.format_detection_for_slack(det, athlete_name=None, brand_display_name="F3 Pure")
        assert "username unavailable" in text
        assert "hashtag scan" in text


# ---------------------------------------------------------------------------
# tool_dispatch.influencer_add_handle
# ---------------------------------------------------------------------------

_MOCK_MAP = {
    "U_ALEX": {"display_name": "Alex Cordova", "asana_email": "alex@hjrglobal.com", "asana_user_gid": "999"},
}


class TestToolInfluencerAddHandle:
    def _call(self, input_data, user_id="U_ALEX", entity="F3E"):
        import cora.tools.tool_dispatch as td
        with patch("cora.tools.tool_dispatch._load_slack_asana_map", return_value=_MOCK_MAP):
            return td._tool_influencer_add_handle(user_id, entity, input_data)

    def test_refuses_without_confirmed(self):
        result = self._call({"athlete_name": "A", "platform": "instagram", "handle": "@a"})
        assert "refused" in result.lower()

    def test_refuses_confirmed_false(self):
        result = self._call({"athlete_name": "A", "platform": "instagram", "handle": "@a", "confirmed": False})
        assert "refused" in result.lower()

    def test_missing_athlete_name(self):
        result = self._call({"platform": "instagram", "handle": "@a", "confirmed": True})
        assert "athlete_name" in result.lower()

    def test_missing_platform(self):
        result = self._call({"athlete_name": "A", "handle": "@a", "confirmed": True})
        assert "platform" in result.lower()

    def test_successful_registration(self):
        result = self._call({
            "athlete_name": "Luis Pena",
            "platform": "instagram",
            "handle": "@luispena_ufc",
            "confirmed": True,
        })
        assert "REGISTERED" in result.upper() or "registered" in result.lower()
        assert "Luis Pena" in result
        # Verify it's actually in the DB
        row = ic.get_athlete_by_handle("instagram", "luispena_ufc")
        assert row is not None
        assert row["athlete_name"] == "Luis Pena"

    def test_entity_defaults_to_channel_entity(self):
        self._call({
            "athlete_name": "UFC Athlete",
            "platform": "tiktok",
            "handle": "@ufcathlte",
            "confirmed": True,
        }, entity="UFL")
        row = ic.get_athlete_by_handle("tiktok", "ufcathlte")
        assert row["entity"] == "UFL"
