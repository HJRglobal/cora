"""WS9 Tier 1C — canonical Drive fileId resolution for the weekly inventory report.

The lookup PREFERS a pinned canonical fileId (deterministic) and falls back to
name-based search on 404 / trashed / unreadable, so a stale or moved id can never
hard-break the inventory pull.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import cora.tools.inventory_client as ic


def _service():
    """A MagicMock Drive service with independently-settable get/list returns."""
    svc = MagicMock()
    return svc


def _set_get(svc, meta):
    svc.files.return_value.get.return_value.execute.return_value = meta


def _set_list(svc, files):
    svc.files.return_value.list.return_value.execute.return_value = {"files": files}


# ── _canonical_inventory_file_id ────────────────────────────────────────────

def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("F3E_INVENTORY_FILE_ID", "ENV-ID-123")
    assert ic._canonical_inventory_file_id() == "ENV-ID-123"


def test_reads_pinned_id_from_yaml(monkeypatch):
    monkeypatch.delenv("F3E_INVENTORY_FILE_ID", raising=False)
    fid = ic._canonical_inventory_file_id()
    # The committed canonical-files.yaml pins the confirmed Cotton report id.
    assert fid == "1sI_ejagawGdenipksSCwdU611Rlt4bxm"


# ── _find_latest_file — canonical-first with name fallback ──────────────────

def test_uses_canonical_when_present_and_not_trashed(monkeypatch):
    monkeypatch.setattr(ic, "_canonical_inventory_file_id", lambda: "PINNED-1")
    svc = _service()
    _set_get(svc, {"id": "PINNED-1", "modifiedTime": "2026-06-18T10:00:00Z", "trashed": False})
    # If the canonical path is taken, list() must NOT be consulted.
    svc.files.return_value.list.side_effect = AssertionError("name search should not run")
    fid, modified = ic._find_latest_file(svc)
    assert fid == "PINNED-1"
    assert modified == "2026-06-18T10:00:00Z"


def test_falls_back_to_name_search_when_canonical_trashed(monkeypatch):
    monkeypatch.setattr(ic, "_canonical_inventory_file_id", lambda: "PINNED-1")
    svc = _service()
    _set_get(svc, {"id": "PINNED-1", "modifiedTime": "x", "trashed": True})
    _set_list(svc, [{"id": "NAME-9", "modifiedTime": "2026-06-17T09:00:00Z", "size": "5000"}])
    fid, _ = ic._find_latest_file(svc)
    assert fid == "NAME-9"


def test_falls_back_to_name_search_when_canonical_get_raises(monkeypatch):
    monkeypatch.setattr(ic, "_canonical_inventory_file_id", lambda: "PINNED-1")
    svc = _service()
    svc.files.return_value.get.return_value.execute.side_effect = RuntimeError("404")
    _set_list(svc, [{"id": "NAME-9", "modifiedTime": "2026-06-17T09:00:00Z", "size": "5000"}])
    fid, _ = ic._find_latest_file(svc)
    assert fid == "NAME-9"


def test_name_search_largest_on_mtime_tie(monkeypatch):
    monkeypatch.setattr(ic, "_canonical_inventory_file_id", lambda: None)
    svc = _service()
    _set_list(svc, [
        {"id": "small", "modifiedTime": "2026-06-17T09:00:00Z", "size": "1000"},
        {"id": "big", "modifiedTime": "2026-06-17T09:00:00Z", "size": "9000"},
    ])
    fid, _ = ic._find_latest_file(svc)
    assert fid == "big"


def test_name_search_raises_when_no_files(monkeypatch):
    monkeypatch.setattr(ic, "_canonical_inventory_file_id", lambda: None)
    svc = _service()
    _set_list(svc, [])
    with pytest.raises(ic.InventoryClientError):
        ic._find_latest_file(svc)
