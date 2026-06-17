"""Tests for the capture custom-field attach helpers + script (Phase 1.10).

Covers asana_client.add_project_custom_field_setting (passes the EXACT field GID
so it can never create a duplicate) + list_project_custom_field_gids (idempotency
read), and the script's config parsing (dedup catch-all GIDs, skip empty).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

from cora.tools import asana_client  # noqa: E402
import attach_capture_custom_fields as acf  # noqa: E402


class _Resp:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


def _client_returning(resp):
    """A MagicMock factory suitable for patching httpx.Client (context manager)."""
    client = MagicMock()
    client.post.return_value = resp
    client.get.return_value = resp
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm), client


# ---- add_project_custom_field_setting -----------------------------------------

def test_add_setting_posts_exact_gid_and_returns_data():
    resp = _Resp(200, {"data": {"gid": "S1", "custom_field": {"gid": "F1"}}})
    factory, client = _client_returning(resp)
    with patch.object(asana_client, "_pat", return_value="pat"), \
         patch.object(asana_client.httpx, "Client", factory):
        out = asana_client.add_project_custom_field_setting("P1", "F1")
    assert out["gid"] == "S1"
    url = client.post.call_args.args[0]
    body = client.post.call_args.kwargs["json"]
    assert url.endswith("/projects/P1/addCustomFieldSetting")
    # exact existing GID -> can never create a duplicate field
    assert body["data"]["custom_field"] == "F1"


def test_add_setting_raises_on_error():
    factory, _ = _client_returning(_Resp(403, text="forbidden"))
    with patch.object(asana_client, "_pat", return_value="pat"), \
         patch.object(asana_client.httpx, "Client", factory):
        with pytest.raises(asana_client.AsanaClientError):
            asana_client.add_project_custom_field_setting("P1", "F1")


# ---- list_project_custom_field_gids -------------------------------------------

def test_list_field_gids_parses_settings():
    resp = _Resp(200, {"data": {"custom_field_settings": [
        {"custom_field": {"gid": "F1"}},
        {"custom_field": {"gid": "F2"}},
        {"custom_field": {}},  # no gid -> ignored
    ]}})
    factory, _ = _client_returning(resp)
    with patch.object(asana_client, "_pat", return_value="pat"), \
         patch.object(asana_client.httpx, "Client", factory):
        gids = asana_client.list_project_custom_field_gids("P1")
    assert gids == {"F1", "F2"}


def test_list_field_gids_raises_on_error():
    factory, _ = _client_returning(_Resp(500, text="boom"))
    with patch.object(asana_client, "_pat", return_value="pat"), \
         patch.object(asana_client.httpx, "Client", factory):
        with pytest.raises(asana_client.AsanaClientError):
            asana_client.list_project_custom_field_gids("P1")


# ---- script config parsing ----------------------------------------------------

def test_field_gids_extracts_three():
    cfg = {"custom_fields": {"entity_field_gid": "E", "status_field_gid": "S",
                             "priority_field_gid": "P", "status_not_started_option": "x"}}
    assert acf.field_gids(cfg) == {"Entity": "E", "Status": "S", "Priority": "P"}


def test_project_gids_dedup_and_skip_empty():
    cfg = {"projects": {"HJRG": "A", "FNDR": "A", "F3E": "B", "BDM": "",
                        "LEX": "C", "LEX-LLC": "C"}}
    # deduped by GID (first entity wins), empty BDM skipped
    assert acf.project_gids(cfg) == {"A": "HJRG", "B": "F3E", "C": "LEX"}


def test_real_config_loads_fields_and_projects():
    cfg = acf.load_cfg()
    f = acf.field_gids(cfg)
    assert f.get("Entity") == "1214487026542596"
    assert f.get("Status") and f.get("Priority")
    assert "1215470928454227" in acf.project_gids(cfg)  # F3E catch-all
