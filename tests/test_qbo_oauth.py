"""Unit tests for connectors.qbo_oauth — token management and refresh logic.

All tests mock the token file and HTTP calls — no network or filesystem access.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

import cora.connectors.qbo_oauth as qbo_oauth
from cora.connectors.qbo_oauth import (
    QboAuthError,
    _get_entity_tokens,
    _load_all_tokens,
    _set_entity_tokens,
    get_valid_access_token,
    list_provisioned_entities,
    refresh_all_entities,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_entry(
    access_token: str = "at-test",
    refresh_token: str = "rt-test",
    realm_id: str = "12345",
    expires_at: int | None = None,
    refresh_expires_at: int | None = None,
) -> dict:
    now = int(time.time())
    return {
        "realm_id": realm_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "access_token_expires_at": expires_at or (now + 3600),
        "refresh_token_expires_at": refresh_expires_at or (now + 8_640_000),
        "last_refreshed_at": now,
        "environment": "production",
    }


# ── _load_all_tokens ──────────────────────────────────────────────────────────

class TestLoadAllTokens:
    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", tmp_path / "nonexistent.json")
        assert _load_all_tokens() == {}

    def test_valid_json_file_read(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        token_file.write_text(json.dumps({"HJRG": _make_entry()}), encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)
        data = _load_all_tokens()
        assert "HJRG" in data

    def test_corrupt_json_raises_auth_error(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        token_file.write_text("{invalid json", encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)
        with pytest.raises(QboAuthError, match="Failed to read"):
            _load_all_tokens()


# ── _get_entity_tokens ────────────────────────────────────────────────────────

class TestGetEntityTokens:
    def test_returns_entry_for_known_entity(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        entry = _make_entry(realm_id="99999")
        token_file.write_text(json.dumps({"F3E": entry}), encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)
        result = _get_entity_tokens("F3E")
        assert result["realm_id"] == "99999"

    def test_raises_for_unknown_entity(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        token_file.write_text(json.dumps({"F3E": _make_entry()}), encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)
        with pytest.raises(QboAuthError, match="No QBO tokens"):
            _get_entity_tokens("OSN")

    def test_hint_includes_entity_in_error_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", tmp_path / "qbo-tokens.json")
        with pytest.raises(QboAuthError) as exc_info:
            _get_entity_tokens("BDM")
        assert "BDM" in str(exc_info.value)


# ── _set_entity_tokens ────────────────────────────────────────────────────────

class TestSetEntityTokens:
    def test_creates_file_if_missing(self, tmp_path, monkeypatch):
        token_file = tmp_path / "creds" / "qbo-tokens.json"
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)
        _set_entity_tokens("F3E", _make_entry())
        assert token_file.exists()

    def test_round_trip(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)
        entry = _make_entry(access_token="at-abc", realm_id="77777")
        _set_entity_tokens("HJRG", entry)
        loaded = _load_all_tokens()
        assert loaded["HJRG"]["access_token"] == "at-abc"
        assert loaded["HJRG"]["realm_id"] == "77777"

    def test_preserves_other_entities(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        token_file.write_text(json.dumps({"F3E": _make_entry()}), encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)
        _set_entity_tokens("OSN", _make_entry(realm_id="88888"))
        loaded = _load_all_tokens()
        assert "F3E" in loaded
        assert "OSN" in loaded


# ── get_valid_access_token ────────────────────────────────────────────────────

class TestGetValidAccessToken:
    def test_returns_token_when_fresh(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        entry = _make_entry(access_token="fresh-token", realm_id="12345")
        token_file.write_text(json.dumps({"F3E": entry}), encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)

        token, realm = get_valid_access_token("F3E")
        assert token == "fresh-token"
        assert realm == "12345"

    def test_refreshes_when_near_expiry(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        # Expires in 5 minutes — within the 10-minute lead time
        near_expiry = int(time.time()) + 300
        entry = _make_entry(
            access_token="stale-token",
            refresh_token="rt-valid",
            expires_at=near_expiry,
        )
        token_file.write_text(json.dumps({"F3E": entry}), encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)

        new_entry = _make_entry(access_token="new-token", refresh_token="rt-new")
        with patch.object(qbo_oauth, "_refresh_access_token", return_value=new_entry) as mock_refresh:
            token, _ = get_valid_access_token("F3E")

        mock_refresh.assert_called_once_with("F3E")
        assert token == "new-token"

    def test_no_refresh_when_token_fresh(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        # Expires in 2 hours — well outside lead time
        entry = _make_entry(expires_at=int(time.time()) + 7200)
        token_file.write_text(json.dumps({"F3E": entry}), encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)

        with patch.object(qbo_oauth, "_refresh_access_token") as mock_refresh:
            get_valid_access_token("F3E")

        mock_refresh.assert_not_called()


# ── list_provisioned_entities ─────────────────────────────────────────────────

class TestListProvisionedEntities:
    def test_returns_sorted_entity_list(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        token_file.write_text(
            json.dumps({"OSN": _make_entry(), "F3E": _make_entry(), "HJRG": _make_entry()}),
            encoding="utf-8",
        )
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)
        result = list_provisioned_entities()
        assert result == ["F3E", "HJRG", "OSN"]

    def test_empty_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", tmp_path / "nonexistent.json")
        assert list_provisioned_entities() == []


# ── refresh_all_entities ──────────────────────────────────────────────────────

class TestRefreshAllEntities:
    def test_returns_ok_for_successful_refresh(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        token_file.write_text(json.dumps({"F3E": _make_entry()}), encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)

        with patch.object(qbo_oauth, "_refresh_access_token", return_value=_make_entry()):
            result = refresh_all_entities()

        assert result["F3E"] == "ok"

    def test_records_error_without_raising(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        token_file.write_text(
            json.dumps({"F3E": _make_entry(), "OSN": _make_entry()}),
            encoding="utf-8",
        )
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)

        def mock_refresh(entity):
            if entity == "OSN":
                raise QboAuthError("token expired")
            return _make_entry()

        with patch.object(qbo_oauth, "_refresh_access_token", side_effect=mock_refresh):
            result = refresh_all_entities()

        assert result["F3E"] == "ok"
        assert "error" in result["OSN"]

    def test_processes_all_entities(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        entities = {"F3E": _make_entry(), "OSN": _make_entry(), "HJRG": _make_entry()}
        token_file.write_text(json.dumps(entities), encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)

        with patch.object(qbo_oauth, "_refresh_access_token", return_value=_make_entry()):
            result = refresh_all_entities()

        assert set(result.keys()) == {"F3E", "OSN", "HJRG"}
        assert all(v == "ok" for v in result.values())
