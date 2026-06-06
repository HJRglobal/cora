"""Tests for the HubSpot D-030 runtime portal guard.

The guard (src/cora/tools/hubspot_client._assert_portal) ensures Cora never
operates on the wrong HubSpot portal after the 2026-05-31 migration
(old 243870963 → canonical 246351746).

conftest sets CORA_DISABLE_HUBSPOT_PORTAL_GUARD=1 for the broad suite; every test
here clears it and resets the per-process verification flag so the real logic runs.
"""

from unittest.mock import MagicMock, patch

import pytest

import cora.tools.hubspot_client as hc
from cora.tools.hubspot_client import HubSpotClientError

CANON = "246351746"
OLD = "243870963"


@pytest.fixture(autouse=True)
def _enable_guard(monkeypatch):
    """Undo conftest's global disable + reset cache so each test exercises the guard."""
    monkeypatch.delenv("CORA_DISABLE_HUBSPOT_PORTAL_GUARD", raising=False)
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "fake-token-for-guard-tests")
    monkeypatch.delenv("HUBSPOT_PORTAL_ID", raising=False)
    monkeypatch.setattr(hc, "_portal_verified", False)
    yield
    monkeypatch.setattr(hc, "_portal_verified", False)


# ── _expected_portal_id: env override validation (no network) ──────────────────

class TestExpectedPortalId:
    def test_no_env_returns_canonical(self):
        assert hc._expected_portal_id() == CANON

    def test_matching_env_returns_canonical(self, monkeypatch):
        monkeypatch.setenv("HUBSPOT_PORTAL_ID", CANON)
        assert hc._expected_portal_id() == CANON

    def test_old_portal_env_raises_without_network(self, monkeypatch):
        monkeypatch.setenv("HUBSPOT_PORTAL_ID", OLD)
        # Patch the live probe to blow up if it is ever called — it must not be.
        with patch.object(hc, "_verify_portal_live", side_effect=AssertionError("probed")):
            with pytest.raises(HubSpotClientError, match="non-canonical"):
                hc._expected_portal_id()


# ── _assert_portal: live verification paths ───────────────────────────────────

class TestAssertPortal:
    def test_match_sets_verified(self):
        with patch.object(hc, "_verify_portal_live", return_value=CANON):
            hc._assert_portal()
        assert hc._portal_verified is True

    def test_mismatch_raises_and_does_not_cache(self):
        with patch.object(hc, "_verify_portal_live", return_value=OLD):
            with pytest.raises(HubSpotClientError, match="portal mismatch"):
                hc._assert_portal()
        assert hc._portal_verified is False

    def test_inconclusive_probe_fails_open(self):
        # Probe error (network/non-200) -> no raise, not cached, retried next call.
        with patch.object(
            hc, "_verify_portal_live", side_effect=HubSpotClientError("account-info 503")
        ):
            hc._assert_portal()  # must not raise
        assert hc._portal_verified is False

    def test_verified_short_circuits_no_reprobe(self):
        hc._portal_verified = True
        probe = MagicMock()
        with patch.object(hc, "_verify_portal_live", probe):
            hc._assert_portal()
        probe.assert_not_called()

    def test_env_disable_short_circuits(self, monkeypatch):
        monkeypatch.setenv("CORA_DISABLE_HUBSPOT_PORTAL_GUARD", "1")
        probe = MagicMock()
        with patch.object(hc, "_verify_portal_live", probe):
            hc._assert_portal()
        probe.assert_not_called()
        assert hc._portal_verified is False


# ── _verify_portal_live: account-info probe shape ─────────────────────────────

class TestVerifyPortalLive:
    def _mock_client(self, status, payload, text="err"):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload
        resp.text = text
        client = MagicMock()
        client.__enter__ = lambda s: client
        client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = resp
        return client

    def test_returns_portal_id_from_account_info(self):
        client = self._mock_client(200, {"portalId": 246351746})
        with patch("httpx.Client", return_value=client):
            assert hc._verify_portal_live() == CANON
        # confirm it hit the account-info endpoint
        called_url = client.get.call_args[0][0]
        assert called_url.endswith("/account-info/v3/details")

    def test_non_200_raises(self):
        client = self._mock_client(401, {}, text="unauthorized")
        with patch("httpx.Client", return_value=client):
            with pytest.raises(HubSpotClientError, match="account-info 401"):
                hc._verify_portal_live()


# ── _headers integration: guard runs through the shared header builder ─────────

class TestHeadersGuard:
    def test_headers_blocks_on_wrong_portal(self):
        with patch.object(hc, "_verify_portal_live", return_value=OLD):
            with pytest.raises(HubSpotClientError, match="portal mismatch"):
                hc._headers()

    def test_headers_pass_on_right_portal(self):
        with patch.object(hc, "_verify_portal_live", return_value=CANON):
            hdrs = hc._headers()
        assert hdrs["Authorization"].startswith("Bearer ")
        assert hdrs["Content-Type"] == "application/json"
