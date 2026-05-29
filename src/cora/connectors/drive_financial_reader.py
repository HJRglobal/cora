"""Drive-based financial file reader.

Reads two types of financial files from the HJR accounting Drive folder:

1. Financial pulse files (.md) — read by stable file ID, 1-hour TTL cache.
2. Monthly close pack files (.xlsx) — searched by filename in the
   monthly-reports/ folder tree, 4-hour TTL cache.

Source-opaque contract: no file IDs, sheet names, or Drive links appear in
any return value. Callers receive formatted Slack text or None on failure.
"""

from __future__ import annotations

import io
import logging
import time
from typing import Optional

import openpyxl
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from .drive_connector import DriveConnectorError, _build_drive_service

log = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# File ID registry
# ────────────────────────────────────────────────────────────────────────────

# Pulse .md file IDs — stable per SOP (edit-in-place, never re-upload)
_ENTITY_PULSE_FILE_IDS: dict[str, str] = {
    "OSN":     "1C3C_mjS6f9COUA1CeMoreAfoAqSdZdfy",
    "OSN-GW":  "1C3C_mjS6f9COUA1CeMoreAfoAqSdZdfy",
    "OSN-GF":  "1C3C_mjS6f9COUA1CeMoreAfoAqSdZdfy",
    "OSN-GM":  "1C3C_mjS6f9COUA1CeMoreAfoAqSdZdfy",
    "OSN-VVP": "1C3C_mjS6f9COUA1CeMoreAfoAqSdZdfy",
    "F3E":     "1FcHR3-rW8OIFy6nKWa8leGE9EOqsfhru",
    "F3C":     "1FcHR3-rW8OIFy6nKWa8leGE9EOqsfhru",
    "LEX":     "1va9JxHra8AVxx_5W-a41sKeyOaB1l73U",
    "LEX-LLC": "1va9JxHra8AVxx_5W-a41sKeyOaB1l73U",
    "LEX-LTS": "1va9JxHra8AVxx_5W-a41sKeyOaB1l73U",
    "LEX-LBHS":"1va9JxHra8AVxx_5W-a41sKeyOaB1l73U",
    "LEX-LLA": "1va9JxHra8AVxx_5W-a41sKeyOaB1l73U",
}

# monthly-reports/ Drive folder — search within this tree for close pack files
_MONTHLY_REPORTS_FOLDER_ID = "1nt9lwS54moDwkFQjNnB6Sg5BEkQXpr5o"

# Cora entity code → filename report code
# Naming convention: {data-period}_{report_code}_{doctype}.xlsx
ENTITY_TO_REPORT_CODE: dict[str, str] = {
    "FNDR":    "hjrg",
    "HJRG":    "hjrg",
    "HJRLLC":  "hjrllc",
    "HJRP":    "hjrp",
    "HJRPROD": "hjrprod",
    "HJRPOD":  "hjrpod",
    "F3E":     "f3e",
    "F3C":     "f3comm",
    "OSN":     "osn-core4",
    "OSN-GW":  "osn-gw",
    "OSN-GF":  "osn-gf",
    "OSN-GM":  "osn-gm",
    "OSN-VVP": "osn-vvp",
    "LEX":     "lexcorp",
    "LEX-LLC": "llc",
    "LEX-LBHS":"lbhs",
    "LEX-LTS": "lts",
    "LEX-LLA": "mv",
    "UFL":     "ufl",
    "BDM":     "bdm",
}

# ────────────────────────────────────────────────────────────────────────────
# TTL cache
# ────────────────────────────────────────────────────────────────────────────

_PULSE_TTL = 3600     # 1 hour
_REPORT_TTL = 14400   # 4 hours

# key → (content, cached_at)
_cache: dict[str, tuple[str, float]] = {}


def _cache_get(key: str, ttl: int) -> Optional[str]:
    entry = _cache.get(key)
    if entry and (time.time() - entry[1]) < ttl:
        return entry[0]
    return None


def _cache_set(key: str, value: str) -> None:
    _cache[key] = (value, time.time())


def clear_cache() -> None:
    """Drop all cached content. Useful in tests and after manual file updates."""
    _cache.clear()


# ────────────────────────────────────────────────────────────────────────────
# Drive I/O helpers
# ────────────────────────────────────────────────────────────────────────────

def _download_file_bytes(file_id: str) -> bytes:
    """Download a binary file from Drive by ID. Returns raw bytes."""
    service = _build_drive_service()
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def _search_report_file_id(period: str, report_code: str, doctype: str) -> Optional[str]:
    """Return the Drive file ID for a close pack file, or None if not found.

    Searches by exact filename within the monthly-reports/ folder tree.
    Folder name = upload month (irrelevant); filename encodes the data period.
    """
    filename = f"{period}_{report_code}_{doctype}.xlsx"
    service = _build_drive_service()
    try:
        resp = service.files().list(
            q=(
                f"name = '{filename}' "
                f"and '{_MONTHLY_REPORTS_FOLDER_ID}' in ancestors "
                f"and trashed = false"
            ),
            fields="files(id)",
            pageSize=5,
        ).execute()
    except HttpError as exc:
        raise DriveConnectorError(f"Drive search failed for {filename}: {exc}") from exc

    files = resp.get("files", [])
    return files[0]["id"] if files else None


# ────────────────────────────────────────────────────────────────────────────
# Pulse reader
# ────────────────────────────────────────────────────────────────────────────

def get_pulse_text(entity: str) -> Optional[str]:
    """Read the financial pulse .md file for an entity.

    Returns the file content (markdown) if available, None if this entity has
    no pulse file or if the read fails. Cached with a 1-hour TTL.
    """
    file_id = _ENTITY_PULSE_FILE_IDS.get(entity.upper())
    if not file_id:
        return None

    cache_key = f"pulse:{file_id}"
    cached = _cache_get(cache_key, _PULSE_TTL)
    if cached is not None:
        return cached or None

    try:
        raw = _download_file_bytes(file_id)
        text = raw.decode("utf-8", errors="replace").strip()
        _cache_set(cache_key, text)
        log.info("pulse_reader entity=%s chars=%d", entity, len(text))
        return text
    except DriveConnectorError as exc:
        log.warning("pulse_reader entity=%s drive error: %s", entity, exc)
        return None
    except Exception as exc:
        log.error("pulse_reader entity=%s unexpected error: %s", entity, exc)
        return None


# ────────────────────────────────────────────────────────────────────────────
# xlsx parser for monthly close packs
# ────────────────────────────────────────────────────────────────────────────

# Key label patterns per doctype — (display_label, [lowercase match strings])
# Patterns match on the lowercased, stripped cell text (startswith or exact).
_PL_LABELS: list[tuple[str, list[str]]] = [
    ("Revenue",           ["total revenue", "net revenue", "gross revenue", "total sales",
                           "total income", "net sales", "revenue"]),
    ("COGS",              ["total cost of goods sold", "cost of goods sold", "cogs",
                           "cost of sales", "total cogs"]),
    ("Gross Profit",      ["gross profit", "gross margin"]),
    ("Total Expenses",    ["total expenses", "total operating expenses", "operating expenses",
                           "total other expenses"]),
    ("Operating Income",  ["operating income", "income from operations", "operating profit",
                           "total operating income"]),
    ("Net Income",        ["net income", "net loss", "net profit", "net income / loss",
                           "net income/loss", "net earnings", "net profit / loss",
                           "net income (loss)"]),
]

_BS_LABELS: list[tuple[str, list[str]]] = [
    ("Total Current Assets",      ["total current assets"]),
    ("Total Assets",              ["total assets"]),
    ("Total Current Liabilities", ["total current liabilities"]),
    ("Total Liabilities",         ["total liabilities"]),
    ("Total Equity",              ["total equity", "total stockholders equity",
                                   "total owner equity", "total owners equity",
                                   "owners equity", "owner equity"]),
    ("Total Liabilities & Equity",["total liabilities and equity",
                                   "total liabilities & equity",
                                   "liabilities and equity"]),
]

_CF_LABELS: list[tuple[str, list[str]]] = [
    ("Operating Activities",  ["net cash from operating", "net cash provided by operating",
                               "total operating activities", "net cash operating",
                               "cash from operating"]),
    ("Investing Activities",  ["net cash from investing", "net cash provided by investing",
                               "total investing activities", "cash from investing"]),
    ("Financing Activities",  ["net cash from financing", "net cash provided by financing",
                               "total financing activities", "cash from financing"]),
    ("Net Change in Cash",    ["net change in cash", "net increase in cash",
                               "net decrease in cash", "change in cash", "net cash change"]),
    ("Closing Cash",          ["ending cash", "closing cash", "cash at end",
                               "end of period cash", "cash end of period"]),
]

_AR_LABELS: list[tuple[str, list[str]]] = [
    ("Total Owed",   ["total", "total a/r", "total ar", "accounts receivable total",
                      "total accounts receivable"]),
    ("Current",      ["current", "0 - 30", "0-30"]),
    ("31-60 days",   ["31 - 60", "31-60"]),
    ("61-90 days",   ["61 - 90", "61-90"]),
    ("91+ days",     ["91 and over", "91+", "over 90", "> 90", "over 91"]),
]

_AP_LABELS: list[tuple[str, list[str]]] = [
    ("Total Owed",   ["total", "total a/p", "total ap", "accounts payable total",
                      "total accounts payable"]),
    ("Current",      ["current", "0 - 30", "0-30"]),
    ("31-60 days",   ["31 - 60", "31-60"]),
    ("61-90 days",   ["61 - 90", "61-90"]),
    ("91+ days",     ["91 and over", "91+", "over 90", "> 90", "over 91"]),
]

_DOCTYPE_LABELS: dict[str, list[tuple[str, list[str]]]] = {
    "pl": _PL_LABELS,
    "bs": _BS_LABELS,
    "cf": _CF_LABELS,
    "ar": _AR_LABELS,
    "ap": _AP_LABELS,
}

_DOCTYPE_TITLE: dict[str, str] = {
    "pl": "Profit & Loss",
    "bs": "Balance Sheet",
    "cf": "Cash Flow",
    "ar": "Accounts Receivable Aging",
    "ap": "Accounts Payable Aging",
}


def _fmt(val: float) -> str:
    sign = "-" if val < 0 else ""
    return f"{sign}${abs(val):,.0f}"


def _label_matches(text: str, patterns: list[str]) -> bool:
    t = text.lower().strip()
    return any(t == p or t.startswith(p) for p in patterns)


def _first_numeric_in_row(cells: list) -> Optional[float]:
    """Return the first numeric value from cells[1:] (skip label column)."""
    for cell in cells[1:]:
        val = getattr(cell, "value", None) if hasattr(cell, "value") else cell
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return float(val)
    return None


def _parse_xlsx(data: bytes, doctype: str, entity_label: str, period: str) -> str:
    """Extract key rows from a close pack xlsx and return Slack-formatted text.

    Scans every sheet for rows matching known financial labels. Returns empty
    string if no relevant rows are found. Source-opaque: no sheet/file details.
    """
    label_defs = _DOCTYPE_LABELS.get(doctype, _PL_LABELS)
    title = _DOCTYPE_TITLE.get(doctype, doctype.upper())

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        log.warning("openpyxl load failed (entity=%s period=%s): %s", entity_label, period, exc)
        return ""

    # Collect (label_str, numeric_value) pairs from every sheet
    row_data: list[tuple[str, float]] = []
    try:
        for sheet in wb.worksheets:
            for row in sheet.iter_rows(values_only=False):
                cells = list(row)
                if not cells:
                    continue
                first = cells[0]
                label_val = getattr(first, "value", None) if hasattr(first, "value") else first
                if not isinstance(label_val, str) or not label_val.strip():
                    continue
                num = _first_numeric_in_row(cells)
                if num is not None:
                    row_data.append((label_val.strip(), num))
    except Exception as exc:
        log.warning("xlsx row scan error (entity=%s): %s", entity_label, exc)
    finally:
        wb.close()

    if not row_data:
        return ""

    # Match key labels (first occurrence per display_label wins)
    found: dict[str, float] = {}
    for display_label, patterns in label_defs:
        if display_label in found:
            continue
        for label_str, val in row_data:
            if _label_matches(label_str, patterns):
                found[display_label] = val
                break

    if not found:
        return ""

    lines = [f"*{entity_label} {title} — {period}*", ""]
    for display_label, _ in label_defs:
        if display_label in found:
            lines.append(f"  {display_label}: {_fmt(found[display_label])}")

    return "\n".join(lines).strip()


# ────────────────────────────────────────────────────────────────────────────
# Monthly close pack reader
# ────────────────────────────────────────────────────────────────────────────

def get_monthly_report_text(
    entity: str,
    period: str,
    doctype: str,
) -> Optional[str]:
    """Read and parse a monthly close pack xlsx file.

    entity:  Cora entity code (e.g. "F3E", "LEX-LLC", "OSN-GW")
    period:  Data period as YYYY-MM (e.g. "2026-04")
    doctype: One of pl, bs, cf, ar, ap

    Returns a Slack-formatted string, or None if the file is not found or the
    parse yields no rows. Cached with a 4-hour TTL.
    Source-opaque: no file IDs, sheet names, or Drive links in the output.
    """
    entity_up = entity.upper()
    report_code = ENTITY_TO_REPORT_CODE.get(entity_up)
    if not report_code:
        log.warning("monthly_report: no report code for entity=%s", entity)
        return None

    doctype_lo = doctype.lower()
    if doctype_lo not in _DOCTYPE_LABELS:
        log.warning("monthly_report: invalid doctype=%s", doctype)
        return None

    cache_key = f"report:{period}:{report_code}:{doctype_lo}"
    cached = _cache_get(cache_key, _REPORT_TTL)
    if cached is not None:
        return cached or None

    try:
        file_id = _search_report_file_id(period, report_code, doctype_lo)
    except DriveConnectorError as exc:
        log.warning(
            "monthly_report search error entity=%s period=%s doctype=%s: %s",
            entity, period, doctype_lo, exc,
        )
        return None

    if not file_id:
        log.info(
            "monthly_report not found entity=%s report_code=%s period=%s doctype=%s",
            entity, report_code, period, doctype_lo,
        )
        _cache_set(cache_key, "")  # negative cache: avoid repeated Drive searches
        return None

    try:
        raw = _download_file_bytes(file_id)
    except DriveConnectorError as exc:
        log.warning("monthly_report download error entity=%s: %s", entity, exc)
        return None

    result = _parse_xlsx(raw, doctype_lo, entity_up, period)
    _cache_set(cache_key, result)
    log.info(
        "monthly_report entity=%s period=%s doctype=%s report_code=%s chars=%d",
        entity, period, doctype_lo, report_code, len(result),
    )
    return result if result else None
