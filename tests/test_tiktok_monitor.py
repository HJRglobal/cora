"""Unit tests for connectors.tiktok_monitor — stub behavior."""

import pytest

from cora.connectors.tiktok_monitor import TikTokMonitorError, scan_hashtag


class TestScanHashtag:
    def test_raises_before_api_configured(self):
        with pytest.raises(TikTokMonitorError):
            scan_hashtag("#F3Energy", "f3energy_official")

    def test_error_message_mentions_research_api(self):
        with pytest.raises(TikTokMonitorError, match="Research API"):
            scan_hashtag("#F3Energy", "f3energy_official")

    def test_raises_regardless_of_args(self):
        with pytest.raises(TikTokMonitorError):
            scan_hashtag("anything", "any_handle", since_timestamp="2026-01-01")

    def test_error_type_is_tiktok_monitor_error(self):
        with pytest.raises(TikTokMonitorError) as exc_info:
            scan_hashtag("#OSN", "onestoputrition")
        assert isinstance(exc_info.value, TikTokMonitorError)
