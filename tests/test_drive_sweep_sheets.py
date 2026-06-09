"""Tests for the Sheets-API oversized-export fallback + row cap (drive_sweep.py).

Large Google Sheets exceed Drive's export ceiling and raise
'exportSizeLimitExceeded', which previously dropped the whole sheet. The sweep
now falls back to the Sheets API `values` reader (no export size limit) and
ingests up to _MAX_SHEET_ROWS rows per tab.

Fakes stand in for the Google API client objects; the real _retry_execute is
used (it just calls request.execute()).
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import drive_sweep as ds


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def execute(self):
        if self._exc is not None:
            raise self._exc
        return self._result


class _Values:
    def __init__(self, data, recorder):
        self._data = data
        self._recorder = recorder

    def get(self, spreadsheetId, range, valueRenderOption=None):  # noqa: A002
        self._recorder.append(range)
        # Range looks like "'Tab Name'!A1:ZZ5000"; pull the quoted title back out.
        title = range.split("'!")[0].lstrip("'").replace("''", "'")
        return _Req(result={"values": self._data.get(title, [])})


class _Spreadsheets:
    def __init__(self, meta, data, recorder):
        self._meta = meta
        self._values = _Values(data, recorder)

    def get(self, spreadsheetId, fields):
        return _Req(result=self._meta)

    def values(self):
        return self._values


class _SheetsService:
    def __init__(self, meta, data, recorder):
        self._s = _Spreadsheets(meta, data, recorder)

    def spreadsheets(self):
        return self._s


class _DriveExportFail:
    """Drive service whose Sheet export always 403s (exportSizeLimitExceeded)."""

    def files(self):
        return self

    def export(self, fileId, mimeType):  # noqa: A002
        return _Req(exc=Exception("exportSizeLimitExceeded"))


def _meta(*titles):
    return {"sheets": [{"properties": {"title": t}} for t in titles]}


# ---------------------------------------------------------------------------
# _extract_sheet_via_api
# ---------------------------------------------------------------------------

def test_extract_sheet_via_api_builds_text():
    rec: list[str] = []
    data = {
        "June 2026": [["Fighter", "Handle"], ["Alice", "@a"], []],
        "Summary": [["x", 1]],
    }
    svc = _SheetsService(_meta("June 2026", "Summary"), data, rec)
    out = ds._extract_sheet_via_api(svc, "fid")
    assert "[Sheet: June 2026]" in out
    assert "Fighter\tHandle" in out
    assert "Alice\t@a" in out
    assert "[Sheet: Summary]" in out
    assert "x\t1" in out
    # Every range request caps at _MAX_SHEET_ROWS.
    assert rec and all(r.endswith(str(ds._MAX_SHEET_ROWS)) for r in rec)


def test_extract_sheet_via_api_empty_meta():
    svc = _SheetsService(_meta(), {}, [])
    assert ds._extract_sheet_via_api(svc, "fid") == ""


def test_extract_sheet_via_api_escapes_quoted_titles():
    rec: list[str] = []
    data = {"Bob's Tab": [["v"]]}
    svc = _SheetsService(_meta("Bob's Tab"), data, rec)
    out = ds._extract_sheet_via_api(svc, "fid")
    assert "[Sheet: Bob's Tab]" in out
    assert any("Bob''s Tab" in r for r in rec)


# ---------------------------------------------------------------------------
# _extract_google_sheet fallback behaviour
# ---------------------------------------------------------------------------

def test_google_sheet_falls_back_on_export_error():
    rec: list[str] = []
    data = {"Tab1": [["Fighter", "Handle"], ["Alice", "@a"]]}
    sheets = _SheetsService(_meta("Tab1"), data, rec)
    out = ds._extract_google_sheet(_DriveExportFail(), "fid", sheets_service=sheets)
    assert "Fighter\tHandle" in out
    assert "Alice\t@a" in out


def test_google_sheet_no_fallback_without_service_returns_empty():
    out = ds._extract_google_sheet(_DriveExportFail(), "fid", sheets_service=None)
    assert out == ""


# ---------------------------------------------------------------------------
# Layer A: wiring source assertions
# ---------------------------------------------------------------------------

def test_sweep_wires_sheets_service_and_entity_override():
    src = (Path(ds.__file__)).read_text(encoding="utf-8")
    # Sheets service is built and threaded into extraction in the per-user sweep.
    assert "_build_sheets_service(sa_json_path, email)" in src
    assert "sheets_service=sheets_service" in src
    # Founder-OS path builds the direct-SA Sheets service too.
    assert "_build_sa_sheets_service_direct(sa_json_path)" in src
    # Deterministic filename entity override is applied after Haiku.
    assert "detect_entity_from_filename(filename)" in src


def test_row_cap_raised_above_legacy_200():
    assert ds._MAX_SHEET_ROWS >= 5000
