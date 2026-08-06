"""Google Sheets financial connector — Standing ACTUALS cashflow reader.

Reads the HJR-Lexco_ENTITIES_Weekly Cash Flow Requirements_Standing ACTUALS
Google Sheet via Sheets API v4 values.get (spreadsheets.readonly scope).
Targets the CF_SUMMARY tab by name so the first/active tab does not matter.
Returns a structured CashflowSummary with the most recent week that has
actual data, all entity rows, and portfolio totals.

Auth: reuses GOOGLE_SERVICE_ACCOUNT_JSON + CORA_DRIVE_IMPERSONATE from
drive_connector.py. Requires spreadsheets.readonly scope only.
(Drive scope removed 2026-05-28 — modifiedTime is non-critical and the
two-scope combination triggered unauthorized_client on DWD token fetch.)

Behavioral contract (locked 2026-05-21):
  - Source-opaque: never log or surface file IDs, sheet names, or Drive links
  - 30-minute in-memory cache keyed by file_id
  - Raises GsheetsConnectorError on any auth/API failure so the caller can
    invoke financial_notify_gap instead of surfacing a traceback

Configuration:
  GSHEETS_CASHFLOW_FILE_ID   — Drive file ID for the Standing ACTUALS sheet
  GSHEETS_CASHFLOW_SHEET_NAME — Tab name to read (default: CF_SUMMARY)
  GOOGLE_SERVICE_ACCOUNT_JSON — path to service account JSON key file
  CORA_DRIVE_IMPERSONATE     — email to impersonate (default harrison@hjrglobal.com)
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import time
from datetime import date
from dataclasses import dataclass, field
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

_DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]
_DEFAULT_IMPERSONATE = "harrison@hjrglobal.com"

# Env var that pins the Standing ACTUALS file ID
_CASHFLOW_FILE_ID_ENV = "GSHEETS_CASHFLOW_FILE_ID"

# Canonical file ID (Standing ACTUALS — last modified 2026-05-22)
_DEFAULT_CASHFLOW_FILE_ID = "1bkMFetsIW-cLtYwLorgio01gLm7EOdJ7UgGHj_lTqPI"

# Env var + default for which tab to read inside the workbook
_CASHFLOW_SHEET_NAME_ENV = "GSHEETS_CASHFLOW_SHEET_NAME"
_DEFAULT_CASHFLOW_SHEET_NAME = "CF_SUMMARY"

# Cache TTL: 30 minutes. The sheet is updated weekly; we refresh aggressively
# enough that Justin/Hayden edits surface within the hour.
_CACHE_TTL_SECONDS = 1800

# A weekly Standing-ACTUALS figure older than this many days means the sheet is
# behind (not updated) -- consumers should surface it as stale, not as current
# (audit N1: the pulse showed the same 5/29 week for 2+ weeks because the SHEET
# was behind, not a read failure). The connector read itself is sound.
_STALE_AFTER_DAYS = 10

# Portfolio-level row labels (case-insensitive substring match)
_PORTFOLIO_TOTAL_LABELS = frozenset({
    "portfolio total", "total portfolio", "grand total",
    "net total", "total net", "portfolio net",
})
# Substring (case-insensitive) matches. The Standing ACTUALS tabs label these
# rows "BEGINNING Cash/CC - Book Balance" and "Ending Cash/CC Book Balance" — the
# generic "opening/closing balance" terms never matched, so balances came back
# None for every entity (cash pulse showed all '--'). The closing match must be
# "ending cash/cc book balance" (no dash) so it does NOT also hit the decoy row
# "Total Liquidity - ENDING Cash/CC - Book Balance-S/B ZERO" (value 0).
_OPENING_BALANCE_LABELS = frozenset({
    "opening balance", "beginning balance", "beginning cash/cc",
})
_CLOSING_BALANCE_LABELS = frozenset({
    "closing balance", "ending balance", "ending cash/cc book balance",
})

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
    # Chronological ending-cash series across ALL weeks in the sheet (WS7).
    # Each item: {"week": str, "ending_cash": Optional[float], "is_actual": bool}.
    # Empty when the sheet has no recognizable Ending-Cash row.
    ending_cash_series: list[dict] = field(default_factory=list)
    # NON-COLLAPSING companion to the above (A5 S2b). Each item:
    # {"week", "forecast", "actual", "forecast_overwritten"}. See
    # _forecast_overwritten for why that last flag is load-bearing.
    ending_cash_dual: list[dict] = field(default_factory=list)

    def completed_weeks_with_usable_forecast(self) -> list[dict]:
        """Dual-series weeks where a forecast-vs-actual comparison is MEANINGFUL.

        On this sheet that is usually the empty list -- see _forecast_overwritten.
        Callers must handle empty by saying so, never by inventing a variance.
        """
        return [
            w for w in self.ending_cash_dual
            if w.get("actual") is not None
            and w.get("forecast") is not None
            and not w.get("forecast_overwritten")
        ]

    def entity_by_code(self, code: str) -> Optional[EntityRow]:
        """Look up a single entity by canonical code (case-insensitive)."""
        code_up = code.upper()
        return next(
            (e for e in self.entities if e.entity_code.upper() == code_up),
            None,
        )

    def ending_cash_outlook(self, weeks: int = 4) -> list[dict]:
        """Return the current week's ending cash + the next `weeks` forecast weeks.

        Anchors on the latest-actual week (the one in week_label); returns that
        entry plus up to `weeks` chronologically-following entries. Empty if the
        sheet had no Ending-Cash row. Each item is a series dict (week / ending_cash
        / is_actual). The forward entries are FORECAST ending cash.
        """
        if not self.ending_cash_series:
            return []
        target = self.week_label.replace("Week of ", "").strip()
        idx = next(
            (i for i, e in enumerate(self.ending_cash_series) if e.get("week") == target),
            None,
        )
        if idx is None:
            # Target week not in the series -> no outlook (fail-CLOSED; never anchor
            # on the oldest week, which would present a stale runway as current).
            return []
        return self.ending_cash_series[idx: idx + 1 + max(0, weeks)]

    def osn_entities(self) -> list[EntityRow]:
        return [e for e in self.entities if e.entity_code.upper().startswith("OSN")]

    def lex_entities(self) -> list[EntityRow]:
        return [e for e in self.entities if e.entity_code.upper().startswith("LEX")]

    def data_age_days(self, today: Optional[date] = None) -> Optional[int]:
        """Age in days of the latest-actual week vs `today`.

        None if the week label can't be parsed. The connector read is sound; this
        measures whether the SHEET itself is behind (audit N1).
        """
        wd = _parse_week_date(self.week_label, today=today)
        if wd is None:
            return None
        return ((today or date.today()) - wd).days

    def is_stale(self, today: Optional[date] = None, max_age_days: int = _STALE_AFTER_DAYS) -> bool:
        """True if the latest-actual week is older than max_age_days (default 10).

        Consumers should label a stale figure "as of <week> (sheet may be behind)"
        rather than presenting it as the current week.
        """
        age = self.data_age_days(today)
        return age is not None and age > max_age_days


# ────────────────────────────────────────────────────────────────────────────
# Error type
# ────────────────────────────────────────────────────────────────────────────

class GsheetsConnectorError(Exception):
    """Raised when the Drive API call or CSV parse fails."""


# ────────────────────────────────────────────────────────────────────────────
# Entity -> tab mapping (Standing ACTUALS workbook)
# ────────────────────────────────────────────────────────────────────────────

# Maps Cora entity codes to the specific CF_* tab in the Standing ACTUALS sheet.
# Locked 2026-05-24: Harrison confirmed tab names + entity assignments.
# CF_HR LLC excluded (personal expense tracking, not business data).
ENTITY_TO_TAB: dict[str, str] = {
    "FNDR":        "CF_SUMMARY",
    "HJRG":        "CF_HJR GS",
    "F3E":         "CF_F3",
    "F3C":         "CF_F3",          # F3 Community shares F3 tab
    "OSN":         "OSN Consolidated",
    "OSN-GW":      "CF_OSN Warner",   # Gilbert & Warner (canonical code fixed 2026-05-28; was OSN-WR)
    "OSN-GF":      "CF_OSN Greenfield",
    "OSN-VV":      "CF_OSN ValVista",
    "OSN-MK":      "CF_OSN McKellips",
    "OSN-CORE4":   "CF_OSN Core4",    # Partner distributions / loan lens
    "LEX":         "CF_LEXCORP",
    "LEX-CORP":    "CF_LEXCORP",      # explicit sub-entity alias
    "LEX-LLC":     "CF_LLC",
    "LEX-LBHS":    "CF_LBHS",
    "LEX-LTS":     "CF_LTS",
    "LEX-LLA":     "CF_LLA_MV",       # Maryvale + all LLA locations share one tab
    "LEX-LLA-MV":  "CF_LLA_MV",
    "HJRP":        "CF_HJR Prop",
    "HJRP-CL":     "CF_HJR Prop",     # Cinema Lanes — no dedicated tab
    "HJRP-LCI":    "CF_HJR Prop",     # LCI Realty — no dedicated tab
    "HJRP-RR":     "CF_HJR Prop",     # Rogers Ranch — no dedicated tab
    "BDM":         "CF_BigDM",
    "UFL":         "CF_UFL",
    "HJRPROD":     "CF_HJR PROD",
    "HJRPROD-POD": "CF_HJR Podcast",
}

# Keywords in a user question that trigger the OSN Core4 (partner/distribution) tab
# instead of the default OSN Consolidated tab.
_OSN_CORE4_KEYWORDS = frozenset([
    "distribution", "distributions", "partner", "partners",
    "loan", "loans", "core4", "core 4",
])


def entity_to_tab(entity: str, question: str = "") -> str:
    """Return the correct tab name for the given entity code.

    Falls back to CF_SUMMARY for unknown entities.
    For OSN, switches to CF_OSN Core4 if the question mentions
    distributions, partner payments, or loans.
    """
    code = entity.upper().strip()

    # OSN special case: partner/distribution questions use Core4 tab
    if code == "OSN" and question:
        q_lower = question.lower()
        if any(kw in q_lower for kw in _OSN_CORE4_KEYWORDS):
            return ENTITY_TO_TAB["OSN-CORE4"]

    return ENTITY_TO_TAB.get(code, "CF_SUMMARY")


# ────────────────────────────────────────────────────────────────────────────
# In-memory cache
# ────────────────────────────────────────────────────────────────────────────

# {(file_id, tab_name): (fetched_at_unix, CashflowSummary)}
_CACHE: dict[tuple[str, str], tuple[float, CashflowSummary]] = {}


def _cache_get(file_id: str, tab_name: str) -> Optional[CashflowSummary]:
    key = (file_id, tab_name)
    entry = _CACHE.get(key)
    if entry is None:
        return None
    fetched_at, summary = entry
    if time.monotonic() - fetched_at > _CACHE_TTL_SECONDS:
        del _CACHE[key]
        return None
    return summary


def _cache_set(file_id: str, tab_name: str, summary: CashflowSummary) -> None:
    _CACHE[(file_id, tab_name)] = (time.monotonic(), summary)


def invalidate_cache(file_id: Optional[str] = None) -> None:
    """Force-expire cache for one file or all files. Useful for tests."""
    if file_id:
        stale_keys = [k for k in _CACHE if k[0] == file_id]
        for k in stale_keys:
            del _CACHE[k]
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


def _build_delegated_creds():
    """Build delegated service-account credentials with Drive + Sheets scopes."""
    try:
        creds = service_account.Credentials.from_service_account_file(
            _sa_path(),
            scopes=_DRIVE_SCOPES,
        )
    except Exception as exc:
        raise GsheetsConnectorError(
            f"Failed to load service account credentials: {exc}"
        ) from exc
    return creds.with_subject(_impersonate())


def _build_direct_sa_creds():
    """Build direct service-account credentials (no DWD / no impersonation).

    The Standing ACTUALS sheet is shared directly with the SA email as Editor,
    so the SA can authenticate as itself without needing to impersonate a user.
    This bypasses DWD entirely and avoids unauthorized_client errors on the
    Sheets scope, which affects only Sheets not Calendar/Gmail.

    Added 2026-05-28 after DWD remained broken for spreadsheets.readonly
    despite the scope being listed in the Google Admin grant.
    """
    try:
        creds = service_account.Credentials.from_service_account_file(
            _sa_path(),
            scopes=_DRIVE_SCOPES,
        )
    except Exception as exc:
        raise GsheetsConnectorError(
            f"Failed to load service account credentials: {exc}"
        ) from exc
    return creds  # No .with_subject() — direct SA authentication


def _build_drive_service(delegated_creds=None):
    """Build a Drive v3 API service via service account DWD."""
    creds = delegated_creds or _build_delegated_creds()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _build_sheets_service(delegated_creds=None):
    """Build a Sheets v4 API service via service account DWD."""
    creds = delegated_creds or _build_delegated_creds()
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


# ────────────────────────────────────────────────────────────────────────────
# Drive + Sheets API calls
# ────────────────────────────────────────────────────────────────────────────

def _get_modified_time(drive_service, file_id: str) -> str:
    """Return the modifiedTime field as an ISO date string (YYYY-MM-DD)."""
    try:
        meta = drive_service.files().get(
            fileId=file_id,
            fields="modifiedTime",
        ).execute()
        raw = meta.get("modifiedTime", "")  # e.g. "2026-05-22T14:23:11.000Z"
        return raw[:10] if raw else "unknown"
    except HttpError as exc:
        log.warning("Could not fetch modifiedTime for file: %s", exc)
        return "unknown"


def _cashflow_sheet_name() -> str:
    return os.environ.get(_CASHFLOW_SHEET_NAME_ENV, _DEFAULT_CASHFLOW_SHEET_NAME).strip()


def _export_sheet_as_csv(sheets_service, file_id: str, sheet_name: str) -> str:
    """Read a named sheet tab via Sheets API and return CSV text.

    Uses spreadsheets.values.get with FORMATTED_VALUE so currency strings
    are preserved in the format the existing parser expects.
    """
    try:
        # Single-quote the sheet name to handle spaces and special chars
        range_spec = f"'{sheet_name}'"
        # num_retries: a transient 429/503 on ONE tab permanently costs that
        # entity's forecast week in the weekly snapshot (the sheet overwrites
        # its forecast column before the next run). Same posture as the gmail
        # alias sweep.
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=file_id,
            range=range_spec,
            valueRenderOption="FORMATTED_VALUE",
            dateTimeRenderOption="FORMATTED_STRING",
        ).execute(num_retries=2)
    except HttpError as exc:
        status = exc.resp.status if hasattr(exc, "resp") else "?"
        raise GsheetsConnectorError(
            f"Sheets API values.get failed (HTTP {status}): {exc}"
        ) from exc
    except Exception as exc:
        raise GsheetsConnectorError(
            f"Unexpected error reading sheet tab: {exc}"
        ) from exc

    rows = result.get("values", [])
    if not rows:
        raise GsheetsConnectorError(
            f"Sheet tab returned no data (check tab name and permissions)"
        )

    # Convert list-of-lists to CSV text for the existing parser
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


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
    """Return True if the cell looks like a week date (e.g. '5/19/2026', '10-17')."""
    val = val.strip()
    return bool(
        re.match(r"^\d{1,2}/\d{1,2}(/\d{2,4})?$", val)   # slash: 5/19 or 5/19/2026
        or re.match(r"^\d{1,2}-\d{1,2}(-\d{2,4})?$", val) # dash:  10-17 or 10-17-2026
    )


# Largest forecast-vs-actual gap still treated as "the forecast was overwritten
# with the actual" rather than a real variance.
#
# WHY THIS EXISTS (verified live 2026-08-04, A5 Section 0 item 5): on the Standing
# ACTUALS sheet the FORECAST column is overwritten in place once a week closes. Of
# 43 completed weeks, 42 had FORECAST equal to ACTUAL to sub-dollar rounding
# (1,626,446.70 vs 1,626,447.00; 875,723.71 vs 875,724.00), and exactly one week
# (2-6) retained a genuine gap of $10,347.
#
# Without this check a "forecast accuracy" figure computed from the dual series
# would confidently report ~99.99% accuracy every single week -- a plausible,
# precise, and completely meaningless number. $1.00 cleanly separates the two
# populations: every observed rounding artifact was under a dollar and the one
# real variance was four orders of magnitude larger.
_FORECAST_OVERWRITE_EPSILON = 1.00


def _forecast_overwritten(forecast: Optional[float], actual: Optional[float]) -> bool:
    """True when a week's forecast cell no longer holds a genuine forecast."""
    if forecast is None or actual is None:
        return False
    return abs(actual - forecast) <= _FORECAST_OVERWRITE_EPSILON


# ────────────────────────────────────────────────────────────────────────────
# Forecast-vector support (13WCF shadow ledger, M1/S1) — ADDITIVE ONLY.
#
# Nothing below is read by an existing consumer. The frozensets, _parse_float,
# _parse_week_date, get_cashflow and CashflowSummary above are untouched on
# purpose (test-pinned in tests/test_cashflow_forecast_vector.py) — the shadow
# ledger must not be able to move a figure on a live finance surface.
# ────────────────────────────────────────────────────────────────────────────

# The Standing ACTUALS tabs write an accounting dash for a FORMATTED ZERO and
# leave a cell genuinely EMPTY when no value exists. _parse_float collapses both
# to None, which is right for its callers (they only ever ask "is there a number
# here") but wrong for the ledger: a real $0 forecast would be stored as UNKNOWN,
# and the triplet self-check below could never run on a zero week (verified live
# 2026-08-05 — e.g. CF_LLC "Services" week 10-17 is dash/dash/dash).
#
# UNKNOWN is never zero (D-117), so the two cases stay distinct here:
#   "- " / "$-"  -> 0.0   (the sheet said zero)
#   ""           -> None  (the sheet said nothing)
#
# Pattern, not a membership set: Sheets' accounting format PADS the dash to
# align columns, so the same logical zero arrives as "-", "$-", "$   -  " or
# "  -  " depending on the column width, and en/em dashes appear where someone
# typed one by hand. An exact-membership set silently degraded every unlisted
# spelling to UNKNOWN, which then propagated as a missing actual.
_ACCOUNTING_ZERO_RE = re.compile(r"^\s*\$?\s*[-‐-―]\s*$")


def _parse_accounting_cell(val: Optional[str]) -> Optional[float]:
    """Parse a Standing-ACTUALS money cell, honouring the accounting dash as 0.0.

    Returns None only when the cell is genuinely empty or unparseable.
    """
    if val is None:
        return None
    s = val.strip()
    if not s:
        return None
    if _ACCOUNTING_ZERO_RE.match(s):
        return 0.0
    return _parse_float(s)


# Largest |DIFF - (ACTUAL - FORECAST)| still attributable to display rounding.
#
# Cells are read with FORMATTED_VALUE, so FORECAST, ACTUAL and DIFF are each
# independently rounded to whole dollars -> the residual can reach 0.5*3 = 1.5
# from rounding alone. $2.00 leaves a margin above that derived bound while
# staying orders of magnitude below a genuine column misalignment (which moves
# thousands). Measured live 2026-08-05: 756 checkable weeks across all 18 CF
# tabs, worst residual under $1.00, zero failures.
_TRIPLET_RESIDUAL_TOLERANCE = 2.00

#: Same derivation as above for the three-row cash identity (ending = beginning
#: + net flow): three independently-rounded FORMATTED_VALUE cells -> 1.5 bound.
_IDENTITY_RESIDUAL_TOLERANCE = 2.00

# Row labels for the two measures the shadow ledger stores. Ending cash and
# beginning cash reuse the existing balance frozensets (same rows, same decoy
# protection); net cash flow needs its own because no existing consumer reads it.
#
# The decoy row "Total Liquidity - ENDING Cash/CC - Book Balance-S/B ZERO"
# (value 0) is excluded by construction: it matches none of these strings.
_NET_FLOW_LABELS = frozenset({"net cash flow"})

#: Measure key -> label frozenset, in stored order.
FORECAST_MEASURES: dict[str, frozenset[str]] = {
    "ending_cash": _CLOSING_BALANCE_LABELS,
    "net_cash_flow": _NET_FLOW_LABELS,
    "beginning_cash": _OPENING_BALANCE_LABELS,
}

#: Basis label for a week whose FORECAST cell still holds a real forecast.
BASIS_FORECAST = "forecast"

#: Basis label for a completed week's FORECAST cell (D-121). On this sheet the
#: forecast column is overwritten with the actual once a week closes — 42 of 43
#: historical weeks matched to sub-dollar rounding (A5 §0.5). Storing such a cell
#: as "forecast" would replant the exact defect the shadow ledger exists to fix,
#: so a week that carries an ACTUAL is labelled this instead, always.
BASIS_POST_CLOSE = "post_close_column_value"


class WeekGridError(Exception):
    """The week grid could not be resolved into absolute, uniform week-endings."""


#: A resolved anchor further than this from today means the grid was anchored on
#: the wrong occurrence. Half a year is the natural bound: beyond it the
#: nearest-occurrence choice would have picked the adjacent year instead.
_ANCHOR_MAX_DRIFT_DAYS = 183


def _nearest_occurrence(mo: int, da: int, today: date) -> Optional[date]:
    """The occurrence of month/day closest to ``today``, across adjacent years.

    Most-recent-PAST is wrong for an anchor: the live sheet carries tabs whose
    newest actual sits in a week that has not closed yet (CF_HJR Prop held an
    8-7 actual on 8-5), and past-only resolution would throw that a year back.
    """
    best: Optional[date] = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            cand = date(year, mo, da)
        except ValueError:
            continue          # 2-29 in a non-leap year
        if best is None or abs((cand - today).days) < abs((best - today).days):
            best = cand
    return best


def resolve_week_endings(
    labels: list[str],
    *,
    today: Optional[date] = None,
    anchor_label: Optional[str] = None,
) -> tuple[list[date], str]:
    """Resolve bare "M-D" week labels to absolute dates + the week-ending weekday.

    The sheet's headers carry no year ("10-17", "8-7", "10-30") and the grid spans
    a year boundary in BOTH directions from today. ``_parse_week_date``'s
    most-recent-past-occurrence rule is correct for its own callers (which only
    ever ask about a week that has already happened) but wrong here: on
    2026-08-05 it maps the forward week "10-30" to 2025-10-30, a year in the past.
    Since the ledger is keyed by absolute week-ending dates, that would silently
    corrupt every forward key.

    Columns are strictly chronological left-to-right, so we resolve ONE label to
    an absolute date and walk outward, bumping the year whenever the month/day
    wraps. The week-ending weekday is then DERIVED from the resolved grid rather
    than assumed (Fin-13), and uniformity is asserted: one weekday, every gap
    exactly 7 days.

    CHOOSING THE ANCHOR IS THE WHOLE PROBLEM. A calendar heuristic over the
    labels cannot do it. "The last label whose current-year reading is in the
    past" looks right and is catastrophically wrong for a quarter of the year:
    once the 13-week forward horizon crosses New Year, those January labels read
    as past-this-year, steal the anchor, and shift the ENTIRE grid back 365 days.
    Both uniformity guards still pass (a uniform shift preserves one weekday and
    7-day gaps), so it fails silently — ~12 Mondays a year, every year, first
    biting 2026-10-05. Caught by the D-051 review, not by the suite.

    So the anchor comes from the DATA, not the calendar: ``anchor_label`` should
    be the last week that carries an ACTUAL, which is necessarily within about a
    week of today, and is resolved to its NEAREST occurrence. That is
    unambiguous by construction. Without a hint we fall back to the middle label
    (furthest from either ambiguity edge) and still assert the result lands near
    today, so a bad anchor fails LOUDLY instead of shifting the year.

    Raises WeekGridError if the grid is unparseable, non-uniform, or anchors
    implausibly far from today — the caller renders the tab UNKNOWN rather than
    storing a guessed calendar.
    """
    today = today or date.today()
    if not labels:
        raise WeekGridError("no week columns found")

    def _split(raw: str) -> tuple[int, int, Optional[int]]:
        m = re.match(r"^\s*(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", raw or "")
        if not m:
            raise WeekGridError(f"unparseable week label {raw!r}")
        yr = m.group(3)
        year = None
        if yr:
            year = int(yr)
            year += 2000 if year < 100 else 0
        return int(m.group(1)), int(m.group(2)), year

    parsed = [_split(raw) for raw in labels]

    # An explicit year in the header is authoritative — never discard it and
    # then guess (CF_SUMMARY has historically used M/D/YYYY form).
    explicit = [(i, date(y, mo, da)) for i, (mo, da, y) in enumerate(parsed) if y]

    anchor_idx: Optional[int] = None
    anchor_date: Optional[date] = None

    if explicit:
        anchor_idx, anchor_date = explicit[0]
    else:
        hint_idx: Optional[int] = None
        if anchor_label:
            try:
                hint_idx = labels.index(anchor_label)
            except ValueError:
                # An anchor that is not in the grid means the caller's data and
                # this grid disagree. Falling back silently would re-open the
                # guessing this function exists to close.
                raise WeekGridError(
                    f"anchor week {anchor_label!r} is not one of the grid's "
                    "columns -- refusing to guess the year"
                ) from None
        if hint_idx is None:
            # No data anchor (no closed week on this tab). The midpoint is the
            # label furthest from either nearest-occurrence ambiguity edge.
            hint_idx = len(parsed) // 2
        mo, da, _ = parsed[hint_idx]
        anchor_idx, anchor_date = hint_idx, _nearest_occurrence(mo, da, today)

    if anchor_idx is None or anchor_date is None:
        raise WeekGridError("no week column resolves to a usable date")

    # Belt. _nearest_occurrence bounds drift to ~half a year by construction, so
    # this can only trip on an explicit-year header that is wildly out of range
    # — a sheet restructure, not a year-guessing slip.
    if abs((anchor_date - today).days) > _ANCHOR_MAX_DRIFT_DAYS + 366:
        raise WeekGridError(
            f"anchor week resolved to {anchor_date}, "
            f"{abs((anchor_date - today).days)} days from today — refusing"
        )

    out: list[Optional[date]] = [None] * len(parsed)
    out[anchor_idx] = anchor_date

    prev = anchor_date
    for i in range(anchor_idx + 1, len(parsed)):
        mo, da, yr = parsed[i]
        try:
            cand = date(yr, mo, da) if yr else date(prev.year, mo, da)
            if not yr and cand <= prev:
                cand = date(prev.year + 1, mo, da)
        except ValueError as exc:
            raise WeekGridError(f"invalid week date {mo}-{da}") from exc
        out[i] = cand
        prev = cand

    prev = anchor_date
    for i in range(anchor_idx - 1, -1, -1):
        mo, da, yr = parsed[i]
        try:
            cand = date(yr, mo, da) if yr else date(prev.year, mo, da)
            if not yr and cand >= prev:
                cand = date(prev.year - 1, mo, da)
        except ValueError as exc:
            raise WeekGridError(f"invalid week date {mo}-{da}") from exc
        out[i] = cand
        prev = cand

    resolved = [d for d in out if d is not None]
    if len(resolved) != len(parsed):  # pragma: no cover -- defensive
        raise WeekGridError("week grid did not fully resolve")

    weekdays = {d.strftime("%A") for d in resolved}
    if len(weekdays) != 1:
        raise WeekGridError(f"week endings span multiple weekdays: {sorted(weekdays)}")
    gaps = {(resolved[i + 1] - resolved[i]).days for i in range(len(resolved) - 1)}
    if gaps and gaps != {7}:
        raise WeekGridError(f"week endings are not uniformly 7 days apart: {sorted(gaps)}")

    return resolved, weekdays.pop()


def _parse_week_date(week_label: str, today: Optional[date] = None) -> Optional[date]:
    """Parse the date out of a week label ("Week of 5-29", "Week of 5/29/2026").

    Infers the year as the most recent past occurrence when none is present.
    Returns None if no date can be found (so freshness checks fail safe).
    """
    if not week_label:
        return None
    m = re.search(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?", week_label)
    if not m:
        return None
    today = today or date.today()
    mo, da, yr_raw = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if yr_raw:
            yr = int(yr_raw)
            yr += 2000 if yr < 100 else 0
            return date(yr, mo, da)
        d = date(today.year, mo, da)
        if d > today:                       # no year given + future -> last year's week
            d = date(today.year - 1, mo, da)
        return d
    except ValueError:
        return None


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
            # FORECAST/ACTUAL row found with no preceding date row.
            # Look forward up to 5 rows for the date row (sheet layout: headers above, dates below).
            # Fall back to the row immediately above if nothing found ahead.
            for j in range(i + 1, min(i + 6, len(rows))):
                fwd_count = sum(1 for cell in rows[j] if _is_date_like(cell))
                if fwd_count >= 1:
                    return j, i
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


def _ordered_weeks(col_map: list[tuple[str, str]]) -> list[str]:
    """Distinct week labels in column (left->right = chronological) order."""
    seen: list[str] = []
    for week, _ct in col_map:
        if week and week not in seen:
            seen.append(week)
    return seen


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
    ending_cash_row: Optional[list[str]] = None

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
            ending_cash_row = row  # capture for the multi-week outlook series
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

    # ── Build the chronological ending-cash series across all weeks (WS7) ──
    ending_cash_series: list[dict] = []
    if ending_cash_row is not None:
        for wk in _ordered_weeks(col_map):
            actual_cols = [i for i, (w, ct) in enumerate(col_map) if w == wk and ct == "ACTUAL"]
            forecast_cols = [i for i, (w, ct) in enumerate(col_map) if w == wk and ct == "FORECAST"]
            val = _get_col(ending_cash_row, actual_cols)
            is_actual = val is not None
            if val is None:
                val = _get_col(ending_cash_row, forecast_cols)
            ending_cash_series.append({"week": wk, "ending_cash": val, "is_actual": is_actual})

    # ── Non-collapsing dual series (A5 S2b) ──────────────────────────────────
    # ending_cash_series above keeps ONE number per week (actual-if-present, else
    # forecast), so once a week closes its original forecast is unrecoverable from
    # it. This series keeps BOTH columns so forecast-vs-actual is expressible.
    ending_cash_dual: list[dict] = []
    if ending_cash_row is not None:
        for wk in _ordered_weeks(col_map):
            actual_cols = [i for i, (w, ct) in enumerate(col_map) if w == wk and ct == "ACTUAL"]
            forecast_cols = [i for i, (w, ct) in enumerate(col_map) if w == wk and ct == "FORECAST"]
            actual = _get_col(ending_cash_row, actual_cols)
            forecast = _get_col(ending_cash_row, forecast_cols)
            ending_cash_dual.append({
                "week": wk,
                "forecast": forecast,
                "actual": actual,
                "forecast_overwritten": _forecast_overwritten(forecast, actual),
            })

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
        ending_cash_series=ending_cash_series,
        ending_cash_dual=ending_cash_dual,
    )


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────

def cashflow_file_id() -> str:
    """Return the configured cashflow file ID."""
    return os.environ.get(_CASHFLOW_FILE_ID_ENV, _DEFAULT_CASHFLOW_FILE_ID)


def get_cashflow(
    file_id: Optional[str] = None,
    tab_name: Optional[str] = None,
) -> CashflowSummary:
    """Return a CashflowSummary for the Standing ACTUALS sheet.

    tab_name: specific tab to read (e.g. "CF_LLC", "OSN Consolidated").
              Defaults to GSHEETS_CASHFLOW_SHEET_NAME env var, then "CF_SUMMARY".
              Pass the result of entity_to_tab(entity) to get the right tab per channel.

    Results are cached in-process for _CACHE_TTL_SECONDS (30 min), keyed by
    (file_id, tab_name) so different entity tabs cache independently.
    Raises GsheetsConnectorError on auth/API/parse failure.
    """
    fid = file_id or cashflow_file_id()
    tab = tab_name or _cashflow_sheet_name()

    cached = _cache_get(fid, tab)
    if cached is not None:
        log.debug("Returning cached cashflow summary tab=%s (file_id redacted)", tab)
        return cached

    log.info("Fetching cashflow sheet tab=%s from Sheets API (file_id redacted)", tab)
    try:
        # Use direct SA auth — sheet is shared with SA email directly.
        # DWD (.with_subject) remains broken for spreadsheets.readonly scope;
        # direct SA creds sidestep that entirely.
        sa_creds = _build_direct_sa_creds()
        sheets_service = _build_sheets_service(sa_creds)
        drive_service = build("drive", "v3", credentials=sa_creds, cache_discovery=False)
        modified_date = _get_modified_time(drive_service, fid)
        csv_text = _export_sheet_as_csv(sheets_service, fid, tab)
    except GsheetsConnectorError:
        raise
    except Exception as exc:
        raise GsheetsConnectorError(f"Sheets API error: {exc}") from exc

    summary = _parse_cashflow_csv(csv_text, modified_date)

    if summary.parse_warnings:
        for w in summary.parse_warnings:
            log.warning("Cashflow CSV parse warning (tab=%s): %s", tab, w)

    _cache_set(fid, tab, summary)
    log.info(
        "Cashflow summary loaded: tab=%s %s, %d entities, as_of=%s",
        tab,
        summary.week_label,
        len(summary.entities),
        summary.as_of_date,
    )
    return summary


# ────────────────────────────────────────────────────────────────────────────
# Forecast vectors (13WCF shadow ledger, M1/S1) — ADDITIVE public API
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class WeekPoint:
    """One week of one measure on one tab."""
    week_label: str                 # raw sheet header, e.g. "8-14"
    week_ending: str                # resolved ISO date, e.g. "2026-08-14"
    forecast: Optional[float]
    actual: Optional[float]
    diff: Optional[float]
    basis: str                      # BASIS_FORECAST | BASIS_POST_CLOSE

    @property
    def is_closed(self) -> bool:
        """True once the week carries an ACTUAL — i.e. its forecast cell is gone."""
        return self.actual is not None

    def as_dict(self) -> dict:
        return {
            "week_label": self.week_label,
            "week_ending": self.week_ending,
            "forecast": self.forecast,
            "actual": self.actual,
            "diff": self.diff,
            "basis": self.basis,
        }


@dataclass
class ForecastVector:
    """A tab's full week grid across the stored measures.

    ``status`` is "ok" or "unknown". An UNKNOWN vector carries no series at all —
    a tab whose grid or triplet check failed renders as UNKNOWN everywhere rather
    than contributing a guessed column to a finance surface (Fin-12/D-117).
    """
    tab: str
    status: str = "ok"
    unknown_reason: str = ""
    week_ending_weekday: str = ""
    # measure key -> list[WeekPoint]
    series: dict[str, list[WeekPoint]] = field(default_factory=dict)
    last_actual_week_ending: Optional[str] = None
    forward_week_endings: list[str] = field(default_factory=list)
    triplet_checked: int = 0
    identity_checked: int = 0
    triplet_worst_residual: float = 0.0
    missing_measures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict:
        return {
            "tab": self.tab,
            "status": self.status,
            "unknown_reason": self.unknown_reason,
            "week_ending_weekday": self.week_ending_weekday,
            "last_actual_week_ending": self.last_actual_week_ending,
            "forward_week_endings": list(self.forward_week_endings),
            "forward_weeks": len(self.forward_week_endings),
            "triplet_checked": self.triplet_checked,
            "identity_checked": self.identity_checked,
            "triplet_worst_residual": round(self.triplet_worst_residual, 2),
            "missing_measures": list(self.missing_measures),
            "series": {
                key: [p.as_dict() for p in points]
                for key, points in self.series.items()
            },
        }


def _find_measure_row(
    data_rows: list[list[str]], labels: frozenset[str]
) -> Optional[list[str]]:
    """First data row whose column-0 label matches any of ``labels``."""
    for row in data_rows:
        if row and row[0].strip() and _label_matches_any(row[0], labels):
            return row
    return None


def parse_forecast_vector(
    csv_text: str,
    tab: str,
    *,
    today: Optional[date] = None,
) -> ForecastVector:
    """Parse one CF tab's CSV into a ForecastVector. Pure; no I/O.

    Never raises on a data problem — a tab that cannot be trusted comes back
    ``status="unknown"`` with a reason, so one malformed tab degrades to UNKNOWN
    instead of killing the weekly sweep or, worse, contributing a guessed figure.
    """
    def _unknown(reason: str) -> ForecastVector:
        log.warning("forecast vector UNKNOWN for tab=%s: %s", tab, reason)
        return ForecastVector(tab=tab, status="unknown", unknown_reason=reason)

    rows = list(csv.reader(io.StringIO(csv_text)))
    if not rows:
        return _unknown("tab returned no rows")

    date_row_idx, col_row_idx = _find_header_rows(rows)
    if date_row_idx == -1:
        return _unknown("could not locate the date/column header rows")

    col_map = _build_column_map(
        rows[date_row_idx],
        rows[col_row_idx] if col_row_idx < len(rows) else rows[date_row_idx],
    )
    week_labels = _ordered_weeks(col_map)
    if not week_labels:
        return _unknown("no week columns found")

    data_rows = rows[max(date_row_idx, col_row_idx) + 1:]

    # Anchor the calendar on DATA, not on a guess: the newest week carrying an
    # actual is necessarily within about a week of today, so its nearest
    # occurrence is unambiguous. Without this the January wrap silently shifts
    # the whole grid a year (see resolve_week_endings).
    anchor_label = _find_latest_actual_week(col_map, data_rows)

    try:
        endings, weekday = resolve_week_endings(
            week_labels, today=today, anchor_label=anchor_label
        )
    except WeekGridError as exc:
        return _unknown(f"week grid unusable: {exc}")

    # Pre-index the triplet columns per week so the measure loop stays flat.
    cols: dict[str, dict[str, list[int]]] = {}
    for wk in week_labels:
        cols[wk] = {
            kind: [i for i, (w, ct) in enumerate(col_map) if w == wk and ct == kind]
            for kind in ("FORECAST", "ACTUAL", "DIFF")
        }

    def _cell(row: list[str], idxs: list[int]) -> Optional[float]:
        for ci in idxs:
            if ci < len(row):
                v = _parse_accounting_cell(row[ci])
                if v is not None:
                    return v
        return None

    series: dict[str, list[WeekPoint]] = {}
    missing: list[str] = []
    checked = 0
    worst = 0.0

    for measure, labels in FORECAST_MEASURES.items():
        row = _find_measure_row(data_rows, labels)
        if row is None:
            missing.append(measure)
            continue
        points: list[WeekPoint] = []
        for wk, ending in zip(week_labels, endings):
            f = _cell(row, cols[wk]["FORECAST"])
            a = _cell(row, cols[wk]["ACTUAL"])
            d = _cell(row, cols[wk]["DIFF"])
            # D-121: once a week closes, its FORECAST cell holds the actual.
            basis = BASIS_POST_CLOSE if a is not None else BASIS_FORECAST
            points.append(WeekPoint(
                week_label=wk,
                week_ending=ending.isoformat(),
                forecast=f,
                actual=a,
                diff=d,
                basis=basis,
            ))
            # Positional self-check (Fin-12). This does not verify that a column
            # is *labelled* right — it verifies the three cells we picked for this
            # week belong to the SAME week. A one-column slip pulls a neighbouring
            # week's balance in and blows the residual by orders of magnitude.
            if f is not None and a is not None and d is not None:
                checked += 1
                residual = abs(d - (a - f))
                worst = max(worst, residual)
                if residual > _TRIPLET_RESIDUAL_TOLERANCE:
                    return _unknown(
                        f"triplet self-check failed on {measure} week {wk}: "
                        f"DIFF {d} != ACTUAL {a} - FORECAST {f} "
                        f"(residual {residual:.2f} > {_TRIPLET_RESIDUAL_TOLERANCE:.2f})"
                    )
        series[measure] = points

    if "ending_cash" not in series:
        return _unknown("no Ending Cash/CC Book Balance row on this tab")

    # CROSS-MEASURE IDENTITY. The triplet check above verifies three cells belong
    # to the same week, but it is provably blind to a whole-GROUP shift: on a
    # closed week D-121 forces FORECAST == ACTUAL and DIFF == 0, so shifting all
    # three columns together still satisfies it (a D-051 reviewer reproduced a
    # $250K error that passed). This identity does not have that blind spot --
    # ending = beginning + net flow reads THREE DIFFERENT ROWS at the same
    # column, so a group shift pulls a neighbouring week's balance into one term
    # and breaks it. It also works on FORWARD weeks, which the triplet check
    # cannot reach at all (they have no ACTUAL).
    # Verified live 2026-08-05 on CF_LLC: 104,795 + (51,453) = 53,342.
    identity_checked = 0
    for kind in ("forecast", "actual"):
        beg = {p.week_ending: getattr(p, kind) for p in series.get("beginning_cash", [])}
        net = {p.week_ending: getattr(p, kind) for p in series.get("net_cash_flow", [])}
        for point in series["ending_cash"]:
            b = beg.get(point.week_ending)
            n = net.get(point.week_ending)
            e = getattr(point, kind)
            if b is None or n is None or e is None:
                continue
            identity_checked += 1
            residual = abs(e - (b + n))
            worst = max(worst, residual)
            if residual > _IDENTITY_RESIDUAL_TOLERANCE:
                return _unknown(
                    f"cash-identity check failed on {kind} week {point.week_label}: "
                    f"ending {e} != beginning {b} + net flow {n} "
                    f"(residual {residual:.2f} > {_IDENTITY_RESIDUAL_TOLERANCE:.2f}) "
                    "-- the week columns are misaligned"
                )

    anchor = series["ending_cash"]
    closed = [p for p in anchor if p.is_closed]
    last_actual = closed[-1].week_ending if closed else None
    # Forward = every week after the last CLOSED one. Derived per tab on purpose:
    # verified live 2026-08-05 the boundary is NOT uniform (CF_HJR Prop carried an
    # actual for 8-7 while all 17 other tabs stopped at 7-31), so a global week-0
    # would mis-slice this tab every time.
    forward = [
        p.week_ending for p in anchor
        if last_actual is None or p.week_ending > last_actual
    ]

    return ForecastVector(
        tab=tab,
        status="ok",
        week_ending_weekday=weekday,
        series=series,
        last_actual_week_ending=last_actual,
        forward_week_endings=forward,
        triplet_checked=checked,
        identity_checked=identity_checked,
        triplet_worst_residual=worst,
        missing_measures=missing,
    )


def build_sheets_service():
    """Public builder so a batch caller can authenticate once for many tabs.

    Direct SA credentials — the sheet is shared with the SA and DWD stays broken
    for the spreadsheets scope (see _build_direct_sa_creds).
    """
    return _build_sheets_service(_build_direct_sa_creds())


def get_forecast_vector(
    tab_name: str,
    *,
    file_id: Optional[str] = None,
    sheets_service=None,
    today: Optional[date] = None,
) -> ForecastVector:
    """Read one CF tab and return its ForecastVector.

    Deliberately NOT cached: ``get_cashflow``'s 30-minute cache is right for
    interactive reads, but the weekly snapshot must capture the sheet as it
    stands at that instant, and a cached grid would silently bank a stale one.

    Raises GsheetsConnectorError on an auth/API failure so the caller can mark
    that tab unreadable; a *parse* problem comes back as an UNKNOWN vector.
    """
    fid = file_id or cashflow_file_id()
    svc = sheets_service or build_sheets_service()
    csv_text = _export_sheet_as_csv(svc, fid, tab_name)
    return parse_forecast_vector(csv_text, tab_name, today=today)
