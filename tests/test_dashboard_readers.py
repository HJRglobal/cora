"""Tests for the dashboard read-layer connectors: airtable_client (read-only,
base-allowlisted, fail-soft) and dashboard_drive_reader (JSON by id + newest by
title). All network is mocked -- no live Airtable/Drive calls."""

from __future__ import annotations

import pytest

from cora.connectors import airtable_client
from cora.connectors import dashboard_drive_reader as ddr

ALLOWED = "appwF6W6eVTvPFjct"
CONTENT = "appxbEBjIBf8Wwlbd"


# --------------------------------------------------------------------------- #
# airtable_client                                                             #
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("HTTP 500")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, pages):
        self._pages = list(pages)
        self._i = 0
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        page = self._pages[min(self._i, len(self._pages) - 1)]
        self._i += 1
        return _FakeResp(page)


def _patch_client(monkeypatch, pages):
    holder = {}

    def factory(*a, **k):
        client = _FakeClient(pages)
        holder["client"] = client
        return client

    monkeypatch.setattr(airtable_client.httpx, "Client", factory)
    return holder


def test_airtable_base_allowlist_refused_without_network(monkeypatch):
    # A non-allowlisted base must refuse BEFORE any HTTP client is built.
    def _boom(*a, **k):
        raise AssertionError("httpx.Client must not be constructed for a bad base")

    monkeypatch.setattr(airtable_client.httpx, "Client", _boom)
    monkeypatch.setenv("AIRTABLE_API_KEY", "patTEST")
    res = airtable_client.list_records("appDEADBEEFDEADBE", "Roster")
    assert res.available is False
    assert "allowlist" in res.error


def test_airtable_missing_key_failsoft(monkeypatch):
    monkeypatch.delenv("AIRTABLE_API_KEY", raising=False)
    res = airtable_client.list_records(ALLOWED, "Roster")
    assert res.available is False
    assert "AIRTABLE_API_KEY" in res.error
    assert res.records == []


def test_airtable_pagination(monkeypatch):
    pages = [
        {"records": [{"fields": {"Name": "A"}}], "offset": "o1"},
        {"records": [{"fields": {"Name": "B"}}, {"fields": {"Name": "C"}}]},
    ]
    holder = _patch_client(monkeypatch, pages)
    monkeypatch.setenv("AIRTABLE_API_KEY", "patTEST")
    res = airtable_client.list_records(ALLOWED, "Roster", fields=["Name"])
    assert res.available is True
    assert [r["Name"] for r in res.records] == ["A", "B", "C"]
    # second call carried the offset cursor
    assert holder["client"].calls[1]["params"].get("offset") == "o1"
    assert holder["client"].calls[0]["params"].get("fields[]") == ["Name"]
    assert holder["client"].calls[0]["headers"]["Authorization"] == "Bearer patTEST"


def test_airtable_max_records_cap(monkeypatch):
    pages = [{"records": [{"fields": {"n": i}} for i in range(100)], "offset": "o1"}]
    _patch_client(monkeypatch, pages)
    monkeypatch.setenv("AIRTABLE_API_KEY", "patTEST")
    res = airtable_client.list_records(ALLOWED, "Roster", max_records=5)
    assert len(res.records) == 5
    assert res.available is True


def test_airtable_http_error_failsoft(monkeypatch):
    class _ErrResp(_FakeResp):
        def raise_for_status(self):
            raise RuntimeError("boom")

    class _ErrClient(_FakeClient):
        def get(self, url, headers=None, params=None):
            return _ErrResp({})

    monkeypatch.setattr(airtable_client.httpx, "Client", lambda *a, **k: _ErrClient([{}]))
    monkeypatch.setenv("AIRTABLE_API_KEY", "patTEST")
    res = airtable_client.list_records(ALLOWED, "Roster")
    assert res.available is False
    assert res.error


def test_airtable_retries_without_fields_on_unknown_field(monkeypatch):
    import httpx

    calls = []

    class _Resp:
        def __init__(self, payload, status=200, text=""):
            self._p = payload
            self.status_code = status
            self.text = text

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "err",
                    request=httpx.Request("GET", "http://x"),
                    response=httpx.Response(self.status_code, text=self.text),
                )

        def json(self):
            return self._p

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, params=None):
            calls.append(params or {})
            if "fields[]" in (params or {}):
                return _Resp({}, 422, '{"error":{"type":"UNKNOWN_FIELD_NAME"}}')
            return _Resp({"records": [{"fields": {"Name": "A"}}]})

    monkeypatch.setattr(airtable_client.httpx, "Client", lambda *a, **k: _Client())
    monkeypatch.setenv("AIRTABLE_API_KEY", "patTEST")
    res = airtable_client.list_records(ALLOWED, "Roster", fields=["BadField"])
    assert res.available is True  # NOT surfaced as "not connected"
    assert [r["Name"] for r in res.records] == ["A"]
    assert any("fields[]" in c for c in calls)      # first attempt used fields
    assert any("fields[]" not in c for c in calls)  # then retried fieldless


def test_airtable_allowed_bases_are_the_two_dashboards():
    assert airtable_client.ALLOWED_BASES == frozenset({ALLOWED, CONTENT})


def test_airtable_no_write_methods():
    # Read-only by construction: no create/update/delete callables exported.
    for banned in ("create_record", "update_record", "delete_record", "create", "update", "delete"):
        assert not hasattr(airtable_client, banned)


# --------------------------------------------------------------------------- #
# dashboard_drive_reader                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clear_ddr_cache():
    ddr.clear_cache()
    yield
    ddr.clear_cache()


def test_read_json_by_id_ok(monkeypatch):
    monkeypatch.setattr(ddr, "_download_file_bytes", lambda fid: b'{"meta": {"x": 1}, "policies": []}')
    data = ddr.read_json_by_id("file123")
    assert data == {"meta": {"x": 1}, "policies": []}


def test_read_json_by_id_download_error_failsoft(monkeypatch):
    def _boom(fid):
        raise RuntimeError("drive down")

    monkeypatch.setattr(ddr, "_download_file_bytes", _boom)
    assert ddr.read_json_by_id("file123") is None


def test_read_json_by_id_bad_json_failsoft(monkeypatch):
    monkeypatch.setattr(ddr, "_download_file_bytes", lambda fid: b"not json at all")
    assert ddr.read_json_by_id("file123") is None


def test_read_json_by_id_empty_id():
    assert ddr.read_json_by_id("") is None


class _FakeListReq:
    def __init__(self, files, capture):
        self._files = files
        self._capture = capture

    def execute(self):
        return {"files": self._files}


class _FakeFiles:
    def __init__(self, files, capture):
        self._files = files
        self._capture = capture

    def list(self, **kwargs):
        self._capture.update(kwargs)
        return _FakeListReq(self._files, self._capture)


class _FakeService:
    def __init__(self, files, capture):
        self._files = files
        self._capture = capture

    def files(self):
        return _FakeFiles(self._files, self._capture)


def test_newest_file_id_by_title_picks_newest(monkeypatch):
    capture = {}
    # orderBy modifiedTime desc => the API returns newest first; we take files[0].
    files = [
        {"id": "NEW", "name": "state.json", "modifiedTime": "2026-07-11T21:45:00Z"},
        {"id": "OLD", "name": "state.json", "modifiedTime": "2026-07-10T09:00:00Z"},
    ]
    monkeypatch.setattr(ddr, "_build_drive_service", lambda: _FakeService(files, capture))
    fid = ddr.newest_file_id_by_title("folderABC", "state.json")
    assert fid == "NEW"
    assert capture["orderBy"] == "modifiedTime desc"
    assert "folderABC" in capture["q"] and "state.json" in capture["q"]
    assert "trashed = false" in capture["q"]


def test_newest_file_id_by_title_none_when_empty(monkeypatch):
    monkeypatch.setattr(ddr, "_build_drive_service", lambda: _FakeService([], {}))
    assert ddr.newest_file_id_by_title("folderABC", "state.json") is None


def test_newest_json_by_title_reads_newest(monkeypatch):
    files = [
        {"id": "NEW", "name": "state.json", "modifiedTime": "2026-07-11T21:45:00Z"},
        {"id": "OLD", "name": "state.json", "modifiedTime": "2026-07-10T09:00:00Z"},
    ]
    monkeypatch.setattr(ddr, "_build_drive_service", lambda: _FakeService(files, {}))
    monkeypatch.setattr(
        ddr, "_download_file_bytes",
        lambda fid: b'{"which": "NEW"}' if fid == "NEW" else b'{"which": "OLD"}',
    )
    data = ddr.newest_json_by_title("folderABC", "state.json")
    assert data == {"which": "NEW"}


def test_newest_json_by_title_service_error_failsoft(monkeypatch):
    def _boom():
        raise RuntimeError("no creds")

    monkeypatch.setattr(ddr, "_build_drive_service", _boom)
    assert ddr.newest_json_by_title("folderABC", "state.json") is None
