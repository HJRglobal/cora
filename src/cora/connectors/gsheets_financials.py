"""Google Sheets financial connector — Standing ACTUALS cashflow reader.

Reads the HJR-Lexco_ENTITIES_Weekly Cash Flow Requirements_Standing ACTUALS
Google Sheet via Drive API CSV export (drive.readonly scope — no Sheets API
scope required). Returns a structured CashflowSummary with the most recent
week that has actual data, all entity rows, and portfolio totals.

Auth: reuses GOOGLE_SERVICE_ACCOUNT_JSON + CORA_DRIVE_IMPERSONATE from
drive_connector.py. drive.readonly is sufficient for files.export_media().

Behavioral contract (locked 2026-05-21):
  - Source-opaque: never log or surface file IDs, sheet names, or Drive links
  - 30-minute in-memory cache keyed by file_id
  - Raises GsheetsConnectorError on any auth/API failure so the caller can
    invoke financial_notify_gap instead of surfacing a traceback

Configuration:
  GSHEETS_CASHFLOW_FILE_ID  — Drive file ID for the Standing ACTUALS sheet
  GOOGLE_SERVICE_ACCOUNT_JSON — path to service account JSON key file
  CORA_DRIVE_IMPERSONATE    — email to impersonate (default harrison@hjrglobal.com)
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

log = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_DEFAULT_IMPERSONATE = "harrison@hjrglobal.com"

# Env var that pins the Standing ACTUALS file ID
_CASHFLOW_FILE_ID_ENV = "GSHEETS_CASHFLOW_FILE_ID"

# Canonical file ID (Standing ACTUALS — last modified 2026-05-22)
_DEFAULT_CASHFLOW_FILE_ID = "1bkMFetsIW-cLtYwLorgio01gLm7EOdJ7UgGHj_lTqPI"

# Cache TTL: 30 minutes. The sheet is updated weekly; we refresh aggressively
# enough that Justin/Hayden edits surface within the hour.
_CACHE_TTL_SECONDS = 1800

# Portfolio-level row labels (case-insensitive substring match)
_PORTFOLIO_TOTAL_LABELS = frozenset({
    "portfolio total", "total portfolio", "grand total",
    "net total", "total net", "portfolio net",
})
_OPENING_BALANCE_LABELS = frozenset({"opening balance", "beginning balance"})
_CLOSING_BALANCE_LABELS = frozenset({"closing balance", "ending balance"})

# Known entity display names → canonical entity code mapping
# These match the row labels in the sheet (fuzzy/substring match).
ENTITY_LABEL_MAP: dict[str, str] = {
    "lbhs":          "LEX-LBHS",
    "llc":           "LEX-LLC",
    "lts":           "LEX-LTS",
    "lla_mv":        "LEX-LLA-MV",
    "lla mv":        "LEX-LLA-MV",
    "lla maryvale":  "LEX-LLA-MV",
    "hjr properties":"HJRP",
    "hjr gs":        "HJRG",
    "hr llc":        "HJRG",
    "hjr podcast":   "HJRPROD-POD",
    "hjr prod":      "HJRPROD",
    "ufl":           "UFL",
    "f3":            "F3E",
    "osn warner":    "OSN-GW",
    "osn greenfield":"OSN-GF",
    "osn val vista": "OSN-VV",
    "osn mckellips": "OSN-MK",
    "bigdm":         "BDM",
    "big d":         "BDM",
    "lexcorp":       "LEX-CORP",
}


# ────────────────────────────────────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class EntityRow:
    """One entity's cash flow for a given week."""
    label: str              # raw label from the sheet
    entity_code: str        # canonical code (e.g. "OSN-GW"); "" if unknown
    forecast: Optional[float]
    actual: Optional[float]
    diff: Optional[float]

    @property
    def variance_pct(self) -> Optional[float]:
        """Actual vs forecast as a percentage (positive = over forecast)."""
        if self.forecast is None or self.actual is None:
            return None
        if self.forecast == 0:
            return None
        return ((self.actual - self.forecast) / abs(self.forecast)) * 100


@dataclass
class CashflowSummary:
    """Parsed snapshot of the Standing ACTUALS sheet."""
    week_label: str                        # e.g. "Week of 5/19/2026"
    as_of_date: str                        # ISO date of sheet last-modified
    entities: list[EntityRow] = field(default_factory=list)
    portfolio_forecast: Optional[float] = None
    portfolio_actual: Optional[float] = None
    portfolio_diff: Optional[float] = None
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    parse_warnings: list[str] = field(default_factory=list)

    def entity_by_code(self, code: str) -> Optional[EntityRow]:
        """Look up a single entity by canonical code (case-insensitive)."""
        code_up = code.upper()
        return next(
            (e for e in self.entities if e.entity_code.upper() == code_up),
            None,
        )

    def osn_entities(self) -> list[EntityRow]:
        return [e for e in self.entities if e.entity_code.startswith("OSN")]

    def lex_entities(self) -> list[EntityRow]:
        return [e for e in self.entities if e.entity_code.startswith("LEX")]


# ────────────────────────────────────────────────────────────────────────────
# Error type
# ────────────────────────────────────────────────────────────────────────────

class GsheetsConnectorError(Exception):
    """Raised when the Drive API call or CSV parse fails."""


# ────────────────────────────────────────────────────────────────────────────
# In-memory cache
# ────────────────────────────────────────────────────────────────────────────

# {file_id: (fetched_at_unix, CashflowSummary)}
_CACHE: dict[str, tuple[float, CashflowSummary]] = {}


def _cache_get(file_id: str) -> Optional[CashflowSummary]:
    entry = _CACHE.get(file_id)
    if entry is None:
        return None
    fetched_at, summary = entry
    if time.monotonic() - fetched_at > _CACHE_TTL_SECONDS:
        del _CACHE[file_id]
        return None
    return summary


def _cache_set(file_id: str, summary: CashflowSummary) -> None:
    _CACHE[file_id] = (time.monotonic(), summary)


def invalidate_cache(file_id: Optional[str] = None) -> None:
    """Force-expire cache for one file or all files. Useful for tests."""
    if file_id:
        _CACHE.pop(file_id, None)
    else:
        _CACHE.clear()


# ────────────────────────────────────────────────────────────────────────────
# Google Drive auth
# ────────────────────────────────────────────────────────────────────────────

def _sa_path() -> str:
    val = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not val:
        raise GsheetsConnectorError(
            "GOOGLE_SERVICE_ACCOUNT_JSON not set — Drive/Sheets connector disabled"
        )
    if not os.path.exists(val):
        raise GsheetsConnectorError(
            f"Service account key file not found: {val}"
        )
    return val


def _impersonate() -> str:
    return os.environ.get("CORA_DRIVE_IMPERSONATE", _DEFAULT_IMPERSONATE).strip()


def _build_drive_service():
    """Build a Drive v3 API service via service account DWD."""
    try:
        creds = service_account.Credentials.from_service_account_file(
            _sa_path(),
            scopes=_DRIVE_SCOPES,
        )
    except Exception as exc:
        raise GsheetsConnectorError(
            f"Failed to load service account credentials: {exc}"
        ) from exc
    delegated = creds.with_subject(_impersonate())
    return build("drive", "v3", credentials=delegated, cache_discovery=False)


# ────────────────────────────────────────────────────────────────────────────
# Drive API calls
# ────────────────────────────────────────────────────────────────────────────

def _get_modified_time(service, file_id: str) -> str:
    """Return the modifiedTime field as an ISO date string (YYYY-MM-DD)."""
    try:
        meta = service.files().get(
            fileId=file_id,
            fields="modifiedTime",
        ).execute()
        raw = meta.get("modifiedTime", "")  # e.g. "2026-05-22T14:23:11.000Z"
        return raw[:10] if raw else "unknown"
    except HttpError as exc:
        log.warning("Could not fetch modifiedTime for file: %s", exc)
        return "unknown"


def _export_csv(service, file_id: str) -> str:
    """Export a Google Sheet as CSV text (first/default tab)."""
    try:
        request = service.files().export_media(
            fileId=file_id,
            mimeType="text/csv",
        )
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue().decode("utf-8", errors="replace")
    except HttpError as exc:
        status = exc.resp.status if hasattr(exc, "resp") else "?"
        raise GsheetsConnectorError(
            f"Drive export_media failed (HTTP {status}): {exc}"
        ) from exc
    except Exception as exc:
        raise GsheetsConnectorError(
            f"Unexpected error exporting CSV: {exc}"
        ) from exc


# ────────────────────────────────────────────────────────────────────────────
# CSV parsing
# ────────────────────────────────────────────────────────────────────────────

def _parse_float(val: str) -> Optional[float]:
    """Parse a currency/number cell value. Returns None if blank or non-numeric."""
    if not val:
        return None
    cleaned = val.strip().replace("$", "").replace(",", "").replace("(", "-").replace(")", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _is_date_like(val: str) -> bool:
    """Return True if the cell looks like a week date (e.g. '5/19/2026', '5/19')."""
    val = val.strip()
    return bool(re.match(r"^\d{1,2}/\d{1,2}(/\d{2,4})?$", val))


def _normalize_label(val: str) -> str:
    return val.strip().lower()


def _classify_label(raw: str) -> str:
    """Map a raw row label to a canonical entity code, or '' if unknown."""
    norm = _normalize_label(raw)
    for key, code in ENTITY_LABEL_MAP.items():
        if key in norm:
            return code
    return ""


def _label_matches_any(label: str, targets: frozenset[str]) -> bool:
    norm = _normalize_label(label)
    return any(t in norm for t in targets)


def _find_header_rows(rows: list[list[str]]) -> tuple[int, int]:
    """Find (date_row_idx, column_header_row_idx) in the CSV.

    Strategy:
      - The "date row" contains cells that look like dates (M/D or M/D/YYYY)
      - The "column header row" immediately below it has FORECAST/ACTUAL/DIFF labels
      - If a row has both dates AND FORECAST/ACTUAL (single-row layout), date_row = col_row

    Returns (-1, -1) if not found.
    """
    for i, row in enumerate(rows):
        date_count = sum(1 for cell in row if _is_date_like(cell))
        upper = [c.strip().upper() for c in row]
        has_date = date_count >= 1
        has_fc_ac = "FORECAST" in upper and "ACTUAL" in upper

        if has_date and has_fc_ac:
            # Single-row layout: dates + FORECAST/ACTUAL in same row
            return i, i

        if date_count >= 2:
            # Multi-week layout: date row + separate column-header row below it
            col_row = i + 1 if (i + 1) < len(rows) else i
            return i, col_row

        if date_count == 1:
            # Single-week: one date in this row; check if next row has FORECAST/ACTUAL
            if (i + 1) < len(rows):
                next_upper = [c.strip().upper() for c in rows[i + 1]]
                if "FORECAST" in next_upper and "ACTUAL" in next_upper:
                    return i, i + 1

        if has_fc_ac and not has_date:
            # FORECAST/ACTUAL row found with no preceding date row identified;
            # date row is the row immediately above (if it exists)
            date_row = i - 1 if i > 0 else i
            return date_row, i

    return -1, -1


def _build_column_map(
    date_row: list[str],
    col_header_row: list[str],
) -> list[tuple[str, str]]:
    """Return list of (week_label, column_type) for each column index.

    column_type is one of: 'FORECAST', 'ACTUAL', 'DIFF', 'ENTITY', or ''
    week_label is the date string for that week, or '' for entity/blank columns.

    Handles two layout patterns:
      A) Date in the date_row, repeated in merged cell, FORECAST/ACTUAL/DIFF in col_header_row
      B) Date + FORECAST/ACTUAL/DIFF all in the same row (single-row header)
    """
    result: list[tuple[str, str]] = []
    n_cols = max(len(date_row), len(col_header_row))

    current_week = ""
    for i in range(n_cols):
        d_cell = date_row[i].strip() if i < len(date_row) else ""
        h_cell = (col_header_row[i].strip().upper() if i < len(col_header_row) else "")

        if _is_date_like(d_cell):
            current_week = d_cell

        if h_cell in ("FORECAST", "PROJECTED", "BUDGET"):
            result.append((current_week, "FORECAST"))
        elif h_cell in ("ACTUAL", "ACTUALS"):
            result.append((current_week, "ACTUAL"))
        elif h_cell in ("DIFF", "DIFFERENCE", "VARIANCE"):
            result.append((current_week, "DIFF"))
        elif i == 0:
            result.append(("", "ENTITY"))
        else:
            result.append((current_week, ""))

    return result


def _find_latest_actual_week(
    col_map: list[tuple[str, str]],
    data_rows: list[list[str]],
) -> Optional[str]:
    """Find the most recent week (rightmost) that has at least one non-empty ACTUAL cell."""
    # Collect all week labels that have ACTUAL columns
    actual_weeks: list[str] = []
    for week, col_type in col_map:
        if col_type == "ACTUAL" and week and week not in actual_weeks:
            actual_weeks.append(week)

    # Scan from the rightmost week backward
    for week in reversed(actual_weeks):
        actual_cols = [
            i for i, (w, ct) in enumerate(col_map)
            if w == week and ct == "ACTUAL"
        ]
        # Check if any data row has a non-empty value in these columns
        for row in data_rows:
            for ci in actual_cols:
                if ci < len(row) and _parse_float(row[ci]) is not None:
                    return week
    return None


def _parse_cashflow_csv(
    csv_text: str,
    modified_date: str,
) -> CashflowSummary:
    """Parse the exported CSV text into a CashflowSummary.

    Tolerates a wide variety of sheet layouts. Logs warnings for any rows
    it cannot classify rather than raising.
    """
    warnings: list[str] = []

    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)

    if not rows:
        raise GsheetsConnectorError("CSV export was empty")

    # ── Find header rows ───────────────────────────────────────────────────
    date_row_idx, col_row_idx = _find_header_rows(rows)

    if date_row_idx == -1:
        # Fallback: treat row 0 as date row, row 1 as column headers
        warnings.append("Could not find date header row — using rows 0+1 as fallback")
        date_row_idx, col_row_idx = 0, 1

    date_row = rows[date_row_idx]
    col_header_row = rows[col_row_idx] if col_row_idx < len(rows) else date_row
    col_map = _build_column_map(date_row, col_header_row)

    # ── Find data rows (below the header block) ────────────────────────────
    data_start = max(date_row_idx, col_row_idx) + 1
    data_rows = rows[data_start:]

    # ── Identify the latest week with actual data ──────────────────────────
    target_week = _find_latest_actual_week(col_map, data_rows)

    if not target_week:
        # No actuals yet — use the first/only FORECAST week
        forecast_weeks = [w for w, ct in col_map if ct == "FORECAST" and w]
        if forecast_weeks:
            target_week = forecast_weeks[-1]
            warnings.append("No actual data found — using most recent forecast week")
        else:
            raise GsheetsConnectorError(
                "Could not identify any FORECAST or ACTUAL columns in the sheet"
            )

    week_label = f"Week of {target_week}"

    # Indices for the target week columns
    target_forecast_cols = [
        i for i, (w, ct) in enumerate(col_map) if w == target_week and ct == "FORECAST"
    ]
    target_actual_cols = [
        i for i, (w, ct) in enumerate(col_map) if w == target_week and ct == "ACTUAL"
    ]
    target_diff_cols = [
        i for i, (w, ct) in enumerate(col_map) if w == target_week and ct == "DIFF"
    ]

    def _get_col(row: list[str], cols: list[int]) -> Optional[float]:
        """Extract first parseable value from any of the given column indices."""
        for ci in cols:
            if ci < len(row):
                v = _parse_float(row[ci])
                if v is not None:
                    return v
        return None

    # ── Parse entity rows and special rows ────────────────────────────────
    entity_rows: list[EntityRow] = []
    portfolio_forecast = portfolio_actual = portfolio_diff = None
    opening_balance = closing_balance = None

    for row in data_rows:
        if not row or not row[0].strip():
            continue  # skip blank rows

        label = row[0].strip()
        forecast = _get_col(row, target_forecast_cols)
        actual = _get_col(row, target_actual_cols)
        diff = _get_col(row, target_diff_cols)

        if _label_matches_any(label, _PORTFOLIO_TOTAL_LABELS):
            portfolio_forecast = forecast
            portfolio_actual = actual
            portfolio_diff = diff
            continue

        if _label_matches_any(label, _OPENING_BALANCE_LABELS):
            opening_balance = forecast if forecast is not None else actual
            continue

        if _label_matches_any(label, _CLOSING_BALANCE_LABELS):
            closing_balance = forecast if forecast is not None else actual
            continue

        # Skip rows that have no numeric data at all (section headers, etc.)
        if forecast is None and actual is None and diff is None:
            continue

        entity_code = _classify_label(label)
        entity_rows.append(EntityRow(
            label=label,
            entity_code=entity_code,
            forecast=forecast,
            actual=actual,
            diff=diff,
        ))

    if not entity_rows:
        warnings.append("No entity rows with numeric data were found in CSV")

    return CashflowSummary(
        week_label=week_label,
        as_of_date=modified_date,
        entities=entity_rows,
        portfolio_forecast=portfolio_forecast,
        portfolio_actual=portfolio_actual,
        portfolio_diff=portfolio_diff,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        parse_warnings=warnings,
    )


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────

def cashflow_file_id() -> str:
    """Return the configured cashflow file ID."""
    return os.environ.get(_CASHFLOW_FILE_ID_ENV, _DEFAULT_CASHFLOW_FILE_ID)


def get_cashflow(file_id: Optional[str] = None) -> CashflowSummary:
    """Return a CashflowSummary for the Standing ACTUALS sheet.

    Results are cached in-process for _CACHE_TTL_SECONDS (30 min).
    Raises GsheetsConnectorError on auth/API/parse failure.
    """
    fid = file_id or cashflow_file_id()

    cached = _cache_get(fid)
    if cached is not None:
        log.debug("Returning cached cashflow summary (file_id redacted)")
        return cached

    log.info("Fetching cashflow sheet from Drive (file_id redacted)")
    try:
        service = _build_drive_service()
        modified_date = _get_modified_time(service, fid)
        csv_text = _export_csv(service, fid)
    except GsheetsConnectorError:
        raise
    except Exception as exc:
        raise GsheetsConnectorError(f"Drive API error: {exc}") from exc

    summary = _parse_cashflow_csv(csv_text, modified_date)

    if summary.parse_warnings:
        for w in summary.parse_warnings:
            log.warning("Cashflow CSV parse warning: %s", w)

    _cache_set(fid, summary)
    log.info(
        "Cashflow summary loaded: %s, %d entities, as_of=%s",
        summary.week_label,
        len(summary.entities),
        summary.as_of_date,
    )
    return summary
