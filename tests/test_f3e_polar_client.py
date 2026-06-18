"""Tests for Polar Analytics OAuth2 client-credentials connector.

[F3E] Category 2 — Option A: Polar OAuth rebuild.

Covers:
  - Credential resolution (explicit env vars, pipe-delimited POLAR_API_KEY, static fallback)
  - OAuth2 token exchange: success, 401, bad response shapes
  - Token cache: valid / expired / buffer
  - Token invalidation + single retry on 401 from report endpoint
  - generate_report: happy path, report-cache hit, 401-retry, connector errors
  - PolarReport dataclass round-trip
  - invalidate_cache clears both report cache and token
  - Legacy static Bearer mode (POLAR_API_KEY without "|")

Run from repo root:
    .venv\\Scripts\\python.exe -m pytest tests/test_f3e_polar_client.py -v
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
from src.cora.connectors import polar_client
from src.cora.connectors.polar_client import (
    PolarConnectorError,
    PolarReport,
    generate_report,
    invalidate_cache,
    _auth_mode,
    _client_credentials,
    _exchange_token,
    _get_bearer_token_any,
    _mcp_bearer,
    _invalidate_token,
    _token_is_valid,
    _TOKEN,
    _CACHE,
    _CONV,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_token_response(access_token: str = "tok_abc", expires_in: int = 3600) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"access_token": access_token, "expires_in": expires_in, "token_type": "Bearer"}
    resp.text = json.dumps({"access_token": access_token})
    return resp


def _fake_report_response(rows: int = 2) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "query_id": "qid_test",
        "deepLink": "https://app.polaranalytics.com/report/test",
        "tableData": [{"total_marketing_spend": i * 100} for i in range(rows)],
        "totalData": [{"total_marketing_spend": rows * 100}],
    }
    resp.text = ""
    return resp


def _clear_all():
    """Reset module-level caches so tests don't leak state."""
    polar_client._CACHE.clear()
    polar_client._TOKEN.clear()
    polar_client._CONV.clear()


# ---------------------------------------------------------------------------
# Category 1 — Credential resolution
# ---------------------------------------------------------------------------

class TestCredentialResolution:
    """_client_credentials() returns the right (client_id, client_secret) pair."""

    def test_explicit_env_vars_preferred(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid_123")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "csec_456")
        monkeypatch.delenv("POLAR_API_KEY", raising=False)
        client_id, secret = _client_credentials()
        assert client_id == "cid_123"
        assert secret == "csec_456"

    def test_composite_key_is_not_a_rest_credential(self, monkeypatch):
        """A piped POLAR_API_KEY is an MCP key -- the REST helper must refuse it
        (never pipe-split + OAuth-exchange it; that is the bug this fix closes)."""
        monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("POLAR_API_KEY", "id_aaa|sec_bbb")
        with pytest.raises(PolarConnectorError, match="composite MCP key"):
            _client_credentials()

    def test_mcp_bearer_returns_whole_composite(self, monkeypatch):
        """The composite is sent verbatim (pipe included) as the MCP Bearer."""
        monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("POLAR_API_KEY", "  id_aaa|sec_bbb  ")
        assert _mcp_bearer() == "id_aaa|sec_bbb"

    def test_mcp_bearer_requires_pipe(self, monkeypatch):
        monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("POLAR_API_KEY", "no_pipe_static")
        with pytest.raises(PolarConnectorError, match="composite"):
            _mcp_bearer()

    def test_static_bearer_token_no_pipe(self, monkeypatch):
        monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("POLAR_API_KEY", "static_bearer_xyz")
        client_id, secret = _client_credentials()
        assert client_id == "__static__"
        assert secret == "static_bearer_xyz"

    def test_no_credentials_raises(self, monkeypatch):
        monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("POLAR_API_KEY", raising=False)
        with pytest.raises(PolarConnectorError, match="No Polar credentials"):
            _client_credentials()

    def test_explicit_vars_override_api_key(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "winner_id")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "winner_sec")
        monkeypatch.setenv("POLAR_API_KEY", "loser_id|loser_sec")
        client_id, secret = _client_credentials()
        assert client_id == "winner_id"
        assert secret == "winner_sec"


# ---------------------------------------------------------------------------
# Category 2 — Token exchange
# ---------------------------------------------------------------------------

class TestTokenExchange:
    """_exchange_token() populates _TOKEN with access_token + expires_at."""

    def setup_method(self):
        _clear_all()

    def test_successful_exchange_populates_token(self, monkeypatch):
        fake_resp = _fake_token_response("tok_xyz", expires_in=7200)
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = fake_resp
            _exchange_token("cid", "csec")
        assert polar_client._TOKEN["access_token"] == "tok_xyz"
        assert polar_client._TOKEN["expires_at"] > time.monotonic()

    def test_expires_at_includes_buffer(self, monkeypatch):
        fake_resp = _fake_token_response("tok_buf", expires_in=3600)
        before = time.monotonic()
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = fake_resp
            _exchange_token("cid", "csec")
        after = time.monotonic()
        # expires_at should be ~3600 - 60 = 3540 seconds from now
        expected_min = before + 3540 - 1
        expected_max = after + 3541
        assert expected_min < polar_client._TOKEN["expires_at"] < expected_max

    def test_401_from_token_endpoint_raises(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 401
        fake_resp.text = "Unauthorized"
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = fake_resp
            with pytest.raises(PolarConnectorError, match="401"):
                _exchange_token("bad_id", "bad_sec")

    def test_non_200_raises(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 500
        fake_resp.text = "Server Error"
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = fake_resp
            with pytest.raises(PolarConnectorError, match="HTTP 500"):
                _exchange_token("cid", "sec")

    def test_missing_access_token_in_response_raises(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"token_type": "Bearer"}  # no access_token
        fake_resp.text = "{}"
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = fake_resp
            with pytest.raises(PolarConnectorError, match="access_token"):
                _exchange_token("cid", "sec")

    def test_default_expires_in_used_when_missing(self):
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"access_token": "tok_noexp"}  # no expires_in
        fake_resp.text = ""
        before = time.monotonic()
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = fake_resp
            _exchange_token("cid", "sec")
        # Default 3600 - 60 buffer = 3540s
        assert polar_client._TOKEN["expires_at"] > before + 3539

    def test_timeout_raises_connector_error(self):
        import httpx as _httpx
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = (
                _httpx.TimeoutException("timed out")
            )
            with pytest.raises(PolarConnectorError, match="timed out"):
                _exchange_token("cid", "sec")

    def test_request_error_raises_connector_error(self):
        import httpx as _httpx
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = (
                _httpx.RequestError("connection refused")
            )
            with pytest.raises(PolarConnectorError, match="connection refused"):
                _exchange_token("cid", "sec")

    def test_custom_oauth_url_used(self, monkeypatch):
        monkeypatch.setenv("POLAR_OAUTH_URL", "https://custom.example.com/token")
        fake_resp = _fake_token_response()
        captured_url = {}
        def fake_post(url, **kwargs):
            captured_url["url"] = url
            return fake_resp
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            _exchange_token("cid", "sec")
        assert captured_url["url"] == "https://custom.example.com/token"


# ---------------------------------------------------------------------------
# Category 3 — Token cache validity
# ---------------------------------------------------------------------------

class TestTokenCacheValidity:
    def setup_method(self):
        _clear_all()

    def test_empty_token_is_not_valid(self):
        assert not _token_is_valid()

    def test_freshly_set_token_is_valid(self):
        polar_client._TOKEN["access_token"] = "tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() + 3000
        assert _token_is_valid()

    def test_expired_token_is_not_valid(self):
        polar_client._TOKEN["access_token"] = "tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() - 1
        assert not _token_is_valid()

    def test_invalidate_token_clears_cache(self):
        polar_client._TOKEN["access_token"] = "tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() + 3000
        _invalidate_token()
        assert not polar_client._TOKEN
        assert not _token_is_valid()

    def test_valid_token_returned_without_exchange(self, monkeypatch):
        polar_client._TOKEN["access_token"] = "cached_tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() + 3000
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        with patch("src.cora.connectors.polar_client._exchange_token") as mock_ex:
            token = _get_bearer_token_any()
        mock_ex.assert_not_called()
        assert token == "cached_tok"

    def test_expired_token_triggers_exchange(self, monkeypatch):
        polar_client._TOKEN["access_token"] = "old_tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() - 1
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        def fake_exchange(client_id, secret):
            polar_client._TOKEN["access_token"] = "new_tok"
            polar_client._TOKEN["expires_at"] = time.monotonic() + 3600
        with patch("src.cora.connectors.polar_client._exchange_token", side_effect=fake_exchange):
            token = _get_bearer_token_any()
        assert token == "new_tok"


# ---------------------------------------------------------------------------
# Category 4 — Legacy static Bearer mode
# ---------------------------------------------------------------------------

class TestStaticBearerMode:
    def setup_method(self):
        _clear_all()

    def test_static_key_returned_directly(self, monkeypatch):
        monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("POLAR_API_KEY", "my_static_key")
        with patch("src.cora.connectors.polar_client._exchange_token") as mock_ex:
            token = _get_bearer_token_any()
        mock_ex.assert_not_called()
        assert token == "my_static_key"

    def test_static_key_does_not_populate_token_cache(self, monkeypatch):
        monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("POLAR_API_KEY", "my_static_key")
        _get_bearer_token_any()
        assert not polar_client._TOKEN  # no token cache entry for static mode


# ---------------------------------------------------------------------------
# Category 5 — generate_report happy path
# ---------------------------------------------------------------------------

class TestGenerateReportHappyPath:
    def setup_method(self):
        _clear_all()

    def _mock_oauth_and_report(self, monkeypatch, rows: int = 2):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        token_resp = _fake_token_response()
        report_resp = _fake_report_response(rows)
        return token_resp, report_resp

    def test_returns_polar_report(self, monkeypatch):
        token_resp, report_resp = self._mock_oauth_and_report(monkeypatch)
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            inst = MockClient.return_value.__enter__.return_value
            inst.post.side_effect = [token_resp, report_resp]
            result = generate_report(
                metrics=["total_marketing_spend"],
                dimensions=[],
                date_from="2026-04-01",
                date_to="2026-04-30",
            )
        assert isinstance(result, PolarReport)
        assert result.query_id == "qid_test"
        assert len(result.table_data) == 2
        assert result.total_data == {"total_marketing_spend": 200}

    def test_deep_link_preserved(self, monkeypatch):
        token_resp, report_resp = self._mock_oauth_and_report(monkeypatch)
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            inst = MockClient.return_value.__enter__.return_value
            inst.post.side_effect = [token_resp, report_resp]
            result = generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")
        assert result.deep_link == "https://app.polaranalytics.com/report/test"

    def test_view_id_in_request_body(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        monkeypatch.setenv("POLAR_VIEW_ID", "custom-view-99")
        token_resp = _fake_token_response()
        report_resp = _fake_report_response()
        captured_body = {}
        original_post_calls = []
        def fake_post(url, **kwargs):
            if "oauth" in url or "token" in url:
                return token_resp
            captured_body.update(kwargs.get("json", {}))
            return report_resp
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")
        assert captured_body.get("views") == ["custom-view-99"]

    def test_bearer_token_sent_in_report_request(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        token_resp = _fake_token_response("tok_sent")
        report_resp = _fake_report_response()
        captured_headers = {}
        def fake_post(url, **kwargs):
            if "oauth" in url or "token" in url:
                return token_resp
            captured_headers.update(kwargs.get("headers", {}))
            return report_resp
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")
        assert captured_headers.get("Authorization") == "Bearer tok_sent"


# ---------------------------------------------------------------------------
# Category 6 — Report cache
# ---------------------------------------------------------------------------

class TestReportCache:
    def setup_method(self):
        _clear_all()

    def test_cache_hit_skips_api_call(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        # Pre-populate token
        polar_client._TOKEN["access_token"] = "tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() + 3600

        report_resp = _fake_report_response()
        call_count = {"n": 0}
        def fake_post(url, **kwargs):
            call_count["n"] += 1
            return report_resp
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            r1 = generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")
            r2 = generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")

        assert r1 is r2  # same object from cache
        assert call_count["n"] == 1  # only one real request

    def test_different_metrics_different_cache_entry(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        polar_client._TOKEN["access_token"] = "tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() + 3600

        report_resp_1 = _fake_report_response(1)
        report_resp_2 = _fake_report_response(3)
        responses = [report_resp_1, report_resp_2]
        call_count = {"n": 0}
        def fake_post(url, **kwargs):
            resp = responses[call_count["n"]]
            call_count["n"] += 1
            return resp
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            r1 = generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")
            r2 = generate_report(["total_marketing_spend"], [], "2026-04-01", "2026-04-30")

        assert r1 is not r2
        assert call_count["n"] == 2

    def test_invalidate_cache_clears_report_cache(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        polar_client._TOKEN["access_token"] = "tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() + 3600

        report_resp = _fake_report_response()
        call_count = {"n": 0}
        def fake_post(url, **kwargs):
            call_count["n"] += 1
            return report_resp
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")
            invalidate_cache()
            # Token also cleared by invalidate_cache -- need new token
            polar_client._TOKEN["access_token"] = "tok"
            polar_client._TOKEN["expires_at"] = time.monotonic() + 3600
            generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")

        assert call_count["n"] == 2  # both went through


# ---------------------------------------------------------------------------
# Category 7 — 401 retry logic
# ---------------------------------------------------------------------------

class TestUnauthorizedRetry:
    def setup_method(self):
        _clear_all()

    def test_401_on_report_triggers_token_refresh_and_retry(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")

        token_resp_1 = _fake_token_response("tok_old")
        token_resp_2 = _fake_token_response("tok_new")
        report_resp_401 = MagicMock()
        report_resp_401.status_code = 401
        report_resp_401.text = "Unauthorized"
        report_resp_ok = _fake_report_response()

        responses = iter([token_resp_1, report_resp_401, token_resp_2, report_resp_ok])
        def fake_post(url, **kwargs):
            return next(responses)
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            result = generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")

        assert isinstance(result, PolarReport)

    def test_401_after_retry_raises_connector_error(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")

        token_resp_1 = _fake_token_response("tok_old")
        token_resp_2 = _fake_token_response("tok_new")
        report_resp_401 = MagicMock()
        report_resp_401.status_code = 401
        report_resp_401.text = "Still unauthorized"

        # Both report requests return 401
        responses = iter([token_resp_1, report_resp_401, token_resp_2, report_resp_401])
        def fake_post(url, **kwargs):
            return next(responses)
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            with pytest.raises(PolarConnectorError, match="401 after token refresh"):
                generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")

    def test_static_mode_no_retry_on_401(self, monkeypatch):
        """Static Bearer mode does NOT retry -- it just raises PolarConnectorError."""
        monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("POLAR_API_KEY", "static_key_no_pipe")

        report_resp_401 = MagicMock()
        report_resp_401.status_code = 401
        report_resp_401.text = "Unauthorized"

        call_count = {"n": 0}
        def fake_post(url, **kwargs):
            call_count["n"] += 1
            return report_resp_401
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = fake_post
            with pytest.raises(PolarConnectorError, match="401"):
                generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")

        assert call_count["n"] == 1  # no retry in static mode


# ---------------------------------------------------------------------------
# Category 8 — HTTP error handling in generate_report
# ---------------------------------------------------------------------------

class TestGenerateReportErrors:
    def setup_method(self):
        _clear_all()

    def test_403_raises_connector_error(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        polar_client._TOKEN["access_token"] = "tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() + 3600

        r403 = MagicMock()
        r403.status_code = 403
        r403.text = "Forbidden"
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = r403
            with pytest.raises(PolarConnectorError, match="403"):
                generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")

    def test_500_raises_connector_error(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        polar_client._TOKEN["access_token"] = "tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() + 3600

        r500 = MagicMock()
        r500.status_code = 500
        r500.text = "Server error"
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = r500
            with pytest.raises(PolarConnectorError, match="HTTP 500"):
                generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")

    def test_bad_json_response_raises_connector_error(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        polar_client._TOKEN["access_token"] = "tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() + 3600

        r_bad = MagicMock()
        r_bad.status_code = 200
        r_bad.json.side_effect = ValueError("not json")
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = r_bad
            with pytest.raises(PolarConnectorError, match="parse"):
                generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")

    def test_non_list_table_data_raises_connector_error(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        polar_client._TOKEN["access_token"] = "tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() + 3600

        r_bad = MagicMock()
        r_bad.status_code = 200
        r_bad.json.return_value = {"tableData": "not_a_list"}
        r_bad.text = ""
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.return_value = r_bad
            with pytest.raises(PolarConnectorError, match="tableData"):
                generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")

    def test_no_credentials_raises_before_request(self, monkeypatch):
        monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("POLAR_API_KEY", raising=False)
        with pytest.raises(PolarConnectorError, match="No Polar credentials"):
            generate_report(["blended_roas"], [], "2026-04-01", "2026-04-30")


# ---------------------------------------------------------------------------
# Category 9 — PolarReport dataclass
# ---------------------------------------------------------------------------

class TestPolarReportDataclass:
    def test_all_fields_set(self):
        r = PolarReport(
            query_id="q1",
            table_data=[{"a": 1}],
            total_data={"a": 99},
            deep_link="https://example.com/link",
            date_from="2026-04-01",
            date_to="2026-04-30",
            metrics=["total_marketing_spend"],
            dimensions=["custom_5621"],
        )
        assert r.query_id == "q1"
        assert r.table_data == [{"a": 1}]
        assert r.total_data == {"a": 99}
        assert r.deep_link == "https://example.com/link"
        assert r.metrics == ["total_marketing_spend"]
        assert r.dimensions == ["custom_5621"]

    def test_default_metrics_and_dimensions_empty(self):
        r = PolarReport(
            query_id="q2",
            table_data=[],
            total_data={},
            deep_link="",
            date_from="2026-04-01",
            date_to="2026-04-30",
        )
        assert r.metrics == []
        assert r.dimensions == []

    def test_empty_table_data(self):
        r = PolarReport(
            query_id="q3",
            table_data=[],
            total_data={},
            deep_link="",
            date_from="2026-04-01",
            date_to="2026-04-30",
        )
        assert r.table_data == []


# ---------------------------------------------------------------------------
# Category 10 — invalidate_cache clears both caches
# ---------------------------------------------------------------------------

class TestInvalidateCache:
    def setup_method(self):
        _clear_all()

    def test_invalidate_cache_clears_report_cache(self):
        polar_client._CACHE["some_key"] = (time.monotonic(), MagicMock())
        invalidate_cache()
        assert not polar_client._CACHE

    def test_invalidate_cache_clears_token(self):
        polar_client._TOKEN["access_token"] = "tok"
        polar_client._TOKEN["expires_at"] = time.monotonic() + 3600
        invalidate_cache()
        assert not polar_client._TOKEN

    def test_invalidate_cache_clears_conversation(self):
        polar_client._CONV["conversation_id"] = "conv_x"
        polar_client._CONV["expires_at"] = time.monotonic() + 600
        invalidate_cache()
        assert not polar_client._CONV


# ---------------------------------------------------------------------------
# Category 11 — Auth mode selection
# ---------------------------------------------------------------------------

class TestAuthMode:
    """_auth_mode() picks the transport from which credentials are present."""

    def test_oauth_when_explicit_creds(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        monkeypatch.delenv("POLAR_API_KEY", raising=False)
        assert _auth_mode() == "oauth"

    def test_mcp_when_piped_key(self, monkeypatch):
        monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("POLAR_API_KEY", "cid_123|sec_456")
        assert _auth_mode() == "mcp"

    def test_static_when_unpiped_key(self, monkeypatch):
        monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("POLAR_API_KEY", "lone_static_token")
        assert _auth_mode() == "static"

    def test_explicit_creds_beat_piped_key(self, monkeypatch):
        monkeypatch.setenv("POLAR_CLIENT_ID", "cid")
        monkeypatch.setenv("POLAR_CLIENT_SECRET", "sec")
        monkeypatch.setenv("POLAR_API_KEY", "cid_123|sec_456")
        assert _auth_mode() == "oauth"

    def test_no_creds_raises(self, monkeypatch):
        monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
        monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("POLAR_API_KEY", raising=False)
        with pytest.raises(PolarConnectorError, match="No Polar credentials"):
            _auth_mode()


# ---------------------------------------------------------------------------
# Category 12 — MCP transport (the live path for the composite key)
# ---------------------------------------------------------------------------

_MCP_REPORT = {
    "query_id": "qid_mcp",
    "deepLink": "https://app.polaranalytics.com/custom/report",
    "tableData": [{"total_marketing_spend": 546.87, "blended_roas": 7.05}],
    "totalData": [{"total_marketing_spend": 546.87, "blended_roas": 7.05}],
}


def _sse_response(jsonrpc_obj, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.headers = {"content-type": "text/event-stream"}
    m.text = "event: message\ndata: " + json.dumps(jsonrpc_obj) + "\n\n"
    return m


def _json_response(jsonrpc_obj, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.headers = {"content-type": "application/json"}
    m.text = json.dumps(jsonrpc_obj)
    m.json.return_value = jsonrpc_obj
    return m


def _http_error_response(status: int, text: str = "error") -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.headers = {"content-type": "text/html"}
    m.text = text
    return m


def _accepted_response() -> MagicMock:
    m = MagicMock()
    m.status_code = 202
    m.headers = {"content-type": "application/json"}
    m.text = ""
    return m


def _tool_result_obj(call_id, *, structured=None, content_text=None,
                     is_error=False, error_text="boom"):
    if is_error:
        result = {"isError": True, "content": [{"type": "text", "text": error_text}]}
    else:
        result = {}
        if structured is not None:
            result["structuredContent"] = structured
        if content_text is not None:
            result["content"] = [{"type": "text", "text": content_text}]
        elif structured is not None:
            result["content"] = [{"type": "text", "text": json.dumps(structured)}]
    return {"jsonrpc": "2.0", "id": call_id, "result": result}


def _jsonrpc_error_obj(call_id, message="bad request", code=-32000):
    return {"jsonrpc": "2.0", "id": call_id, "error": {"code": code, "message": message}}


class _McpServer:
    """Mock Polar MCP server: routes JSON-RPC posts to canned responses.

    gr_outcomes: list of per-call generate_report outcomes (last one repeats).
    Each is kwargs for _tool_result_obj, or {"http": (status, text)}, or
    {"jsonrpc_error": "msg"}.
    """

    def __init__(self, gr_outcomes=None, conv_id="conv_x", ctx_structured=None,
                 init_status=200, get_context_status=200, transport="sse"):
        self.gr_outcomes = gr_outcomes or [dict(structured=_MCP_REPORT)]
        self.conv_id = conv_id
        self.ctx_structured = ctx_structured
        self.init_status = init_status
        self.get_context_status = get_context_status
        self.transport = transport
        self.counts = {"initialize": 0, "initialized": 0, "get_context": 0, "generate_report": 0}
        self.captured_headers = []
        self.captured_gr_args = []
        self._gr_i = 0

    def _wrap(self, obj):
        return _json_response(obj) if self.transport == "json" else _sse_response(obj)

    def post(self, url, headers=None, json=None, **kw):
        self.captured_headers.append(headers or {})
        body = json or {}
        method = body.get("method")
        if method == "initialize":
            self.counts["initialize"] += 1
            if self.init_status != 200:
                return _http_error_response(self.init_status, "init error")
            return self._wrap({"jsonrpc": "2.0", "id": body.get("id"),
                               "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "serverInfo": {"name": "Polar-MCP", "version": "4.1"}}})
        if method == "notifications/initialized":
            self.counts["initialized"] += 1
            return _accepted_response()
        if method == "tools/call":
            name = body["params"]["name"]
            cid = body.get("id")
            if name == "get_context":
                self.counts["get_context"] += 1
                if self.get_context_status != 200:
                    return _http_error_response(self.get_context_status, "ctx error")
                structured = self.ctx_structured
                if structured is None:
                    structured = {"conversation_id": self.conv_id,
                                  "context": {"brand_name": "F3 Energy"},
                                  "account_status": {"activation": "activated", "data": "ready"}}
                return self._wrap(_tool_result_obj(cid, structured=structured))
            if name == "generate_report":
                self.counts["generate_report"] += 1
                self.captured_gr_args.append(body["params"]["arguments"])
                outcome = self.gr_outcomes[min(self._gr_i, len(self.gr_outcomes) - 1)]
                self._gr_i += 1
                if "http" in outcome:
                    return _http_error_response(*outcome["http"])
                if "jsonrpc_error" in outcome:
                    return self._wrap(_jsonrpc_error_obj(cid, outcome["jsonrpc_error"]))
                return self._wrap(_tool_result_obj(cid, **outcome))
        raise AssertionError(f"unexpected MCP method/tool: {method} / {body}")


def _mcp_env(monkeypatch, key="cid_123|sec_456"):
    monkeypatch.delenv("POLAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("POLAR_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("POLAR_API_KEY", key)


class TestMcpTransport:
    def setup_method(self):
        _clear_all()

    def _run(self, server, monkeypatch, **gen_kwargs):
        _mcp_env(monkeypatch, gen_kwargs.pop("key", "cid_123|sec_456"))
        metrics = gen_kwargs.pop("metrics", ["total_marketing_spend", "blended_roas"])
        dimensions = gen_kwargs.pop("dimensions", [])
        date_from = gen_kwargs.pop("date_from", "2026-05-19")
        date_to = gen_kwargs.pop("date_to", "2026-06-17")
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = server.post
            return generate_report(metrics, dimensions, date_from, date_to, **gen_kwargs)

    def test_composite_key_uses_mcp_returns_report(self, monkeypatch):
        server = _McpServer()
        result = self._run(server, monkeypatch)
        assert isinstance(result, PolarReport)
        assert result.query_id == "qid_mcp"
        assert result.table_data == [{"total_marketing_spend": 546.87, "blended_roas": 7.05}]
        assert result.total_data == {"total_marketing_spend": 546.87, "blended_roas": 7.05}
        assert result.deep_link == "https://app.polaranalytics.com/custom/report"
        assert server.counts["initialize"] == 1
        assert server.counts["get_context"] == 1
        assert server.counts["generate_report"] == 1

    def test_no_oauth_exchange_in_mcp_mode(self, monkeypatch):
        """The fix: composite key -> direct Bearer, NEVER an OAuth /oauth/token call."""
        server = _McpServer()
        with patch("src.cora.connectors.polar_client._exchange_token") as mock_ex:
            self._run(server, monkeypatch)
        mock_ex.assert_not_called()

    def test_bearer_is_whole_composite(self, monkeypatch):
        server = _McpServer()
        self._run(server, monkeypatch)
        auths = {h.get("Authorization") for h in server.captured_headers}
        assert auths == {"Bearer cid_123|sec_456"}

    def test_streamable_http_accept_header_sent(self, monkeypatch):
        server = _McpServer()
        self._run(server, monkeypatch)
        accepts = {h.get("Accept") for h in server.captured_headers}
        assert accepts == {"application/json, text/event-stream"}

    def test_report_args_mapped_to_mcp_shape(self, monkeypatch):
        server = _McpServer()
        self._run(
            server, monkeypatch,
            metrics=["total_marketing_spend", "blended_roas"],
            dimensions=["custom_5621"],
            ordering=[{"columnKey": "total_marketing_spend", "direction": "DESC"}],
            settings={"attribution_model": "linear"},
            limit=20,
        )
        args = server.captured_gr_args[0]
        assert args["metrics"] == "total_marketing_spend,blended_roas"
        assert args["dimensions"] == "custom_5621"
        assert args["ordering"] == "total_marketing_spend:DESC"
        assert args["settings"] == json.dumps({"attribution_model": "linear"})
        assert args["rules"] == "{}"
        assert args["metricRules"] == "{}"
        assert args["views"] == "31499-mot5h6ya"
        assert args["limit"] == "20"
        assert args["conversation_id"] == "conv_x"
        assert args["reflexion"]  # required, non-empty

    def test_structured_content_absent_falls_back_to_text(self, monkeypatch):
        server = _McpServer(gr_outcomes=[dict(content_text=json.dumps(_MCP_REPORT))])
        result = self._run(server, monkeypatch)
        assert result.query_id == "qid_mcp"
        assert result.total_data["blended_roas"] == 7.05

    def test_plain_json_transport_parsed(self, monkeypatch):
        server = _McpServer(transport="json")
        result = self._run(server, monkeypatch)
        assert isinstance(result, PolarReport)
        assert result.query_id == "qid_mcp"

    def test_conversation_cached_across_reports(self, monkeypatch):
        server = _McpServer()
        _mcp_env(monkeypatch)
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = server.post
            generate_report(["total_marketing_spend"], [], "2026-05-19", "2026-06-17")
            generate_report(["blended_roas"], [], "2026-05-19", "2026-06-17")  # diff cache key
        assert server.counts["initialize"] == 1
        assert server.counts["get_context"] == 1
        assert server.counts["generate_report"] == 2

    def test_report_cache_hit_skips_mcp(self, monkeypatch):
        server = _McpServer()
        _mcp_env(monkeypatch)
        with patch("src.cora.connectors.polar_client.httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.post.side_effect = server.post
            r1 = generate_report(["total_marketing_spend"], [], "2026-05-19", "2026-06-17")
            r2 = generate_report(["total_marketing_spend"], [], "2026-05-19", "2026-06-17")
        assert r1 is r2
        assert server.counts["generate_report"] == 1

    def test_tool_error_triggers_refresh_then_succeeds(self, monkeypatch):
        server = _McpServer(gr_outcomes=[dict(is_error=True), dict(structured=_MCP_REPORT)])
        result = self._run(server, monkeypatch)
        assert isinstance(result, PolarReport)
        assert server.counts["generate_report"] == 2
        assert server.counts["get_context"] == 2  # conversation refreshed before retry

    def test_persistent_tool_error_raises(self, monkeypatch):
        server = _McpServer(gr_outcomes=[dict(is_error=True), dict(is_error=True)])
        with pytest.raises(PolarConnectorError, match="tool error"):
            self._run(server, monkeypatch)
        assert server.counts["generate_report"] == 2

    def test_jsonrpc_error_retried_then_raises(self, monkeypatch):
        server = _McpServer(gr_outcomes=[dict(jsonrpc_error="boom1"), dict(jsonrpc_error="boom2")])
        with pytest.raises(PolarConnectorError, match="JSON-RPC error"):
            self._run(server, monkeypatch)
        assert server.counts["generate_report"] == 2

    def test_http_403_on_report_retried_then_raises(self, monkeypatch):
        server = _McpServer(gr_outcomes=[dict(http=(403, "forbidden")), dict(http=(403, "forbidden"))])
        with pytest.raises(PolarConnectorError, match="403"):
            self._run(server, monkeypatch)
        assert server.counts["generate_report"] == 2

    def test_http_401_on_initialize_raises_without_retry(self, monkeypatch):
        server = _McpServer(init_status=401)
        with pytest.raises(PolarConnectorError, match="401"):
            self._run(server, monkeypatch)
        # Handshake failure is raised before the report attempt -> no second initialize
        assert server.counts["initialize"] == 1
        assert server.counts["generate_report"] == 0

    def test_get_context_without_conversation_id_raises(self, monkeypatch):
        server = _McpServer(ctx_structured={"context": {"brand_name": "F3 Energy"}})
        with pytest.raises(PolarConnectorError, match="conversation_id"):
            self._run(server, monkeypatch)
        assert server.counts["generate_report"] == 0
