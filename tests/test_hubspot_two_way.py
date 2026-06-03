"""Tests for Feature #5 -- Two-way HubSpot updates from Slack.

Coverage:
  - hubspot_client.get_deal(): happy path, 404, network error
  - hubspot_client.update_deal_stage(): happy path, API error, network error
  - _tool_hubspot_update_deal_stage: LEX block, preview (confirmed=False),
    confirmed=True executes update, missing deal_id, missing stage_id,
    deal fetch failure, update failure, WRITE_CONFIRMED prefix
  - _tool_hubspot_add_note: LEX block, preview (confirmed=False),
    confirmed=True calls create_note, missing deal_id, missing note_body,
    note creation failure, WRITE_CONFIRMED prefix
  - TOOL_DEFINITIONS: both entries exist with required fields
  - _TOOL_FUNCTIONS: both callables registered
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.tools import hubspot_client
from cora.tools.tool_dispatch import (
    TOOL_DEFINITIONS,
    _TOOL_FUNCTIONS,
    _tool_hubspot_update_deal_stage,
    _tool_hubspot_add_note,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_DEAL_PROPS = {
    "dealname": "Hensley Distribution",
    "dealstage": "stage_outreach",
    "pipeline": "2313722582",
    "amount": "12000",
    "hubspot_owner_id": "160459333",
}

_FAKE_STAGE_CACHE = {
    "stage_outreach": "Outreach",
    "stage_qualified": "Qualified",
}

_SLACK_USER = "U0B2RM2JYJ1"
_ENTITY_F3E = "F3E"
_ENTITY_LEX = "LEX-LLC"
_ENTITY_HJRG = "HJRG"


def _input(**kwargs) -> dict:
    base = {"_channel_name": "f3-leadership"}
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# hubspot_client.get_deal tests
# ---------------------------------------------------------------------------

def _mock_httpx_client(method: str, response: Any):
    """Context manager helper: mock httpx.Client so `method` returns `response`."""
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    setattr(mock_client, method, MagicMock(return_value=response))
    return mock_client


class TestGetDeal:
    def test_happy_path_returns_properties(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"properties": _FAKE_DEAL_PROPS}

        with patch.object(hubspot_client, "_STAGE_NAME_CACHE", _FAKE_STAGE_CACHE):
            with patch.object(hubspot_client, "_token", return_value="tok"):
                with patch("httpx.Client", return_value=_mock_httpx_client("get", mock_resp)):
                    result = hubspot_client.get_deal("12345")
                    assert result["dealname"] == "Hensley Distribution"

    def test_404_raises_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "Not found"

        with patch.object(hubspot_client, "_STAGE_NAME_CACHE", _FAKE_STAGE_CACHE):
            with patch.object(hubspot_client, "_token", return_value="tok"):
                with patch("httpx.Client", return_value=_mock_httpx_client("get", mock_resp)):
                    with pytest.raises(hubspot_client.HubSpotClientError, match="404"):
                        hubspot_client.get_deal("99999")

    def test_non_200_raises_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Server error"

        with patch.object(hubspot_client, "_STAGE_NAME_CACHE", _FAKE_STAGE_CACHE):
            with patch.object(hubspot_client, "_token", return_value="tok"):
                with patch("httpx.Client", return_value=_mock_httpx_client("get", mock_resp)):
                    with pytest.raises(hubspot_client.HubSpotClientError):
                        hubspot_client.get_deal("12345")

    def test_network_error_raises_client_error(self):
        import httpx as _httpx

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = _httpx.RequestError("timeout")

        with patch.object(hubspot_client, "_STAGE_NAME_CACHE", _FAKE_STAGE_CACHE):
            with patch.object(hubspot_client, "_token", return_value="tok"):
                with patch("httpx.Client", return_value=mock_client):
                    with pytest.raises(hubspot_client.HubSpotClientError, match="network error"):
                        hubspot_client.get_deal("12345")


# ---------------------------------------------------------------------------
# hubspot_client.update_deal_stage tests
# ---------------------------------------------------------------------------

class TestUpdateDealStage:
    def test_happy_path_returns_properties(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"properties": {"dealstage": "stage_qualified"}}

        with patch.object(hubspot_client, "_token", return_value="tok"):
            with patch("httpx.Client", return_value=_mock_httpx_client("patch", mock_resp)):
                result = hubspot_client.update_deal_stage("12345", "stage_qualified")
                assert result["dealstage"] == "stage_qualified"

    def test_non_200_raises_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad request"

        with patch.object(hubspot_client, "_token", return_value="tok"):
            with patch("httpx.Client", return_value=_mock_httpx_client("patch", mock_resp)):
                with pytest.raises(hubspot_client.HubSpotClientError, match="update_deal_stage 400"):
                    hubspot_client.update_deal_stage("12345", "bad_stage")

    def test_network_error_raises_client_error(self):
        import httpx as _httpx

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.patch.side_effect = _httpx.RequestError("timeout")

        with patch.object(hubspot_client, "_token", return_value="tok"):
            with patch("httpx.Client", return_value=mock_client):
                with pytest.raises(hubspot_client.HubSpotClientError, match="network error"):
                    hubspot_client.update_deal_stage("12345", "stage_x")


# ---------------------------------------------------------------------------
# _tool_hubspot_update_deal_stage tests
# ---------------------------------------------------------------------------

class TestToolUpdateDealStage:
    def _call(self, entity: str = _ENTITY_F3E, **kwargs) -> str:
        return _tool_hubspot_update_deal_stage(_SLACK_USER, entity, _input(**kwargs))

    def test_lex_channel_blocked(self):
        result = self._call(entity=_ENTITY_LEX, deal_id="123", stage_id="s1", confirmed=True)
        assert "blocked" in result.lower()

    def test_missing_deal_id_returns_error(self):
        result = self._call(stage_id="s1", confirmed=False)
        assert "deal_id" in result

    def test_missing_stage_id_returns_error(self):
        result = self._call(deal_id="123", confirmed=False)
        assert "stage_id" in result

    def test_unconfirmed_returns_preview(self):
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            with patch.object(hubspot_client, "_STAGE_NAME_CACHE", _FAKE_STAGE_CACHE):
                result = self._call(deal_id="12345", stage_id="stage_qualified", confirmed=False)
                assert "WRITE_PREVIEW" in result
                assert "Hensley Distribution" in result
                assert "Outreach" in result
                assert "Qualified" in result

    def test_confirmed_true_calls_update(self):
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            with patch.object(hubspot_client, "_STAGE_NAME_CACHE", _FAKE_STAGE_CACHE):
                with patch.object(hubspot_client, "update_deal_stage", return_value={}) as mock_update:
                    result = self._call(deal_id="12345", stage_id="stage_qualified", confirmed=True)
                    mock_update.assert_called_once_with("12345", "stage_qualified")
                    assert "WRITE_CONFIRMED" in result

    def test_confirmed_result_contains_deal_name(self):
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            with patch.object(hubspot_client, "_STAGE_NAME_CACHE", _FAKE_STAGE_CACHE):
                with patch.object(hubspot_client, "update_deal_stage", return_value={}):
                    result = self._call(deal_id="12345", stage_id="stage_qualified", confirmed=True)
                    assert "Hensley Distribution" in result

    def test_deal_fetch_failure_returns_error(self):
        with patch.object(hubspot_client, "get_deal",
                          side_effect=hubspot_client.HubSpotClientError("404")):
            result = self._call(deal_id="99999", stage_id="stage_x", confirmed=False)
            assert "99999" in result

    def test_update_failure_returns_error(self):
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            with patch.object(hubspot_client, "_STAGE_NAME_CACHE", _FAKE_STAGE_CACHE):
                with patch.object(hubspot_client, "update_deal_stage",
                                  side_effect=hubspot_client.HubSpotClientError("API error")):
                    result = self._call(deal_id="12345", stage_id="stage_q", confirmed=True)
                    assert "failed" in result.lower() or "error" in result.lower()

    def test_fndr_entity_is_allowed(self):
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            with patch.object(hubspot_client, "_STAGE_NAME_CACHE", _FAKE_STAGE_CACHE):
                result = self._call(entity="FNDR", deal_id="12345", stage_id="stage_outreach", confirmed=False)
                assert "blocked" not in result.lower()

    def test_bdm_entity_is_allowed(self):
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            with patch.object(hubspot_client, "_STAGE_NAME_CACHE", _FAKE_STAGE_CACHE):
                result = self._call(entity="BDM", deal_id="12345", stage_id="stage_outreach", confirmed=False)
                assert "blocked" not in result.lower()


# ---------------------------------------------------------------------------
# _tool_hubspot_add_note tests
# ---------------------------------------------------------------------------

class TestToolAddNote:
    def _call(self, entity: str = _ENTITY_F3E, **kwargs) -> str:
        return _tool_hubspot_add_note(_SLACK_USER, entity, _input(**kwargs))

    def test_lex_channel_blocked(self):
        result = self._call(entity=_ENTITY_LEX, deal_id="123", note_body="hello", confirmed=True)
        assert "blocked" in result.lower()

    def test_missing_deal_id_returns_error(self):
        result = self._call(note_body="hello", confirmed=False)
        assert "deal_id" in result

    def test_missing_note_body_returns_error(self):
        result = self._call(deal_id="123", confirmed=False)
        assert "note_body" in result

    def test_unconfirmed_returns_preview(self):
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            result = self._call(deal_id="12345", note_body="Following up on delivery timeline.", confirmed=False)
            assert "WRITE_PREVIEW" in result
            assert "Hensley Distribution" in result
            assert "Following up" in result

    def test_confirmed_true_calls_create_note(self):
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            with patch.object(hubspot_client, "create_note", return_value="note_123") as mock_create:
                result = self._call(deal_id="12345", note_body="Test note.", confirmed=True)
                mock_create.assert_called_once_with(body="Test note.", deal_id="12345")
                assert "WRITE_CONFIRMED" in result

    def test_confirmed_result_contains_deal_name(self):
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            with patch.object(hubspot_client, "create_note", return_value="note_123"):
                result = self._call(deal_id="12345", note_body="Test note.", confirmed=True)
                assert "Hensley Distribution" in result

    def test_note_creation_failure_returns_error(self):
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            with patch.object(hubspot_client, "create_note",
                              side_effect=hubspot_client.HubSpotClientError("API error")):
                result = self._call(deal_id="12345", note_body="Test.", confirmed=True)
                assert "failed" in result.lower() or "error" in result.lower()

    def test_deal_fetch_failure_returns_error(self):
        with patch.object(hubspot_client, "get_deal",
                          side_effect=hubspot_client.HubSpotClientError("404")):
            result = self._call(deal_id="99999", note_body="Note.", confirmed=False)
            assert "99999" in result

    def test_long_note_preview_is_truncated(self):
        long_note = "A" * 400
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            result = self._call(deal_id="12345", note_body=long_note, confirmed=False)
            assert "WRITE_PREVIEW" in result
            assert "..." in result

    def test_hjrg_entity_is_allowed(self):
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            result = self._call(entity="HJRG", deal_id="12345", note_body="Note.", confirmed=False)
            assert "blocked" not in result.lower()

    def test_osn_entity_is_allowed(self):
        with patch.object(hubspot_client, "get_deal", return_value=_FAKE_DEAL_PROPS):
            result = self._call(entity="OSN", deal_id="12345", note_body="Note.", confirmed=False)
            assert "blocked" not in result.lower()


# ---------------------------------------------------------------------------
# TOOL_DEFINITIONS tests
# ---------------------------------------------------------------------------

class TestToolDefinitions:
    def _get_def(self, name: str) -> dict | None:
        return next((t for t in TOOL_DEFINITIONS if t["name"] == name), None)

    def test_hubspot_update_deal_stage_definition_exists(self):
        assert self._get_def("hubspot_update_deal_stage") is not None

    def test_hubspot_add_note_definition_exists(self):
        assert self._get_def("hubspot_add_note") is not None

    def test_update_deal_stage_has_description(self):
        d = self._get_def("hubspot_update_deal_stage")
        assert d["description"]

    def test_update_deal_stage_has_required_fields(self):
        d = self._get_def("hubspot_update_deal_stage")
        props = d["input_schema"]["properties"]
        required = d["input_schema"]["required"]
        assert "deal_id" in props
        assert "stage_id" in props
        assert "confirmed" in props
        assert "deal_id" in required
        assert "stage_id" in required
        assert "confirmed" in required

    def test_add_note_has_required_fields(self):
        d = self._get_def("hubspot_add_note")
        props = d["input_schema"]["properties"]
        required = d["input_schema"]["required"]
        assert "deal_id" in props
        assert "note_body" in props
        assert "confirmed" in props
        assert "deal_id" in required
        assert "note_body" in required
        assert "confirmed" in required

    def test_update_deal_stage_description_mentions_staged_write(self):
        d = self._get_def("hubspot_update_deal_stage")
        assert "STAGED-WRITE" in d["description"].upper() or "staged-write" in d["description"].lower()

    def test_add_note_description_mentions_staged_write(self):
        d = self._get_def("hubspot_add_note")
        assert "STAGED-WRITE" in d["description"].upper() or "staged-write" in d["description"].lower()

    def test_update_deal_stage_description_mentions_lex_block(self):
        d = self._get_def("hubspot_update_deal_stage")
        assert "LEX" in d["description"]


# ---------------------------------------------------------------------------
# _TOOL_FUNCTIONS tests
# ---------------------------------------------------------------------------

class TestToolFunctions:
    def test_hubspot_update_deal_stage_registered(self):
        assert "hubspot_update_deal_stage" in _TOOL_FUNCTIONS

    def test_hubspot_add_note_registered(self):
        assert "hubspot_add_note" in _TOOL_FUNCTIONS

    def test_hubspot_update_deal_stage_callable(self):
        assert callable(_TOOL_FUNCTIONS["hubspot_update_deal_stage"])

    def test_hubspot_add_note_callable(self):
        assert callable(_TOOL_FUNCTIONS["hubspot_add_note"])
