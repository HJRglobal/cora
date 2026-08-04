"""Weekly finance close-support pack — deterministic, read-only.

WHAT THIS IS
------------
A weekly close-support pack for Justin / Hayden / Harrison: QuickBooks (the 11
provisioned realms) cross-checked against the Standing ACTUALS cash sheet, with
anomaly flags and close-prep notes. Cora as finance analyst-of-record for the
boring 80%.

D-095 CONTRACT (load-bearing)
-----------------------------
**This module computes. A model never does.** Every figure, delta, threshold
comparison and flag in the output is produced by Python from a source read. The
optional narration layer (``FINANCE_CLOSE_NARRATE``, default OFF) may only
*restate* the facts block that this module already produced -- it is never given
raw reports, and if it fails the pack posts the facts block alone.

FAIL-SOFT / HONEST-STUB CONTRACT (load-bearing)
-----------------------------------------------
Every section is independently fail-soft. A dead source produces an honest STUB
("section unavailable -- <reason>"), never a blank and never a silent omission:

* A per-entity read that fails yields an explicit ``unavailable`` line for that
  entity AND the section footer states coverage ("N of M entities") -- the
  silent-partial-digest defect a D-051 review caught on the OSN metrics digest
  (2026-06-17) was exactly a failed entity vanishing from both the ranking and
  the total.
* When EVERY entity's Standing-ACTUALS closing balance comes back ``None`` the
  section degrades to a stub naming row-label drift as the likely cause. This is
  the 2026-06-04 doctrine: a renamed sheet row makes ``gsheets_financials``
  return ``None`` rather than raise, which once rendered a portfolio-wide wall of
  ``--`` that read as "all zero" instead of "connector blind". A label rename must
  surface as an unavailable SECTION, never as blank figures.

READ-ONLY: no writes anywhere except this module's own WoW snapshot under
``data/state/finance-close-snapshots/``. The TIER_3 finance firewall is untouched
-- delivery-side channel scoping lives in ``scripts/run_finance_close_pack.py``.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# WoW snapshots. One JSON per run date; the pack diffs against the most recent
# PRIOR snapshot, so the first-ever run honestly reports "no deltas yet".
SNAPSHOT_DIR = _REPO_ROOT / "data" / "state" / "finance-close-snapshots"
SNAPSHOT_KEEP = 26

# Renewal/payment radar source. Harrison/Justin maintained; a missing or empty
# file yields an honest stub, never a silently empty radar section.
RENEWAL_MAP_PATH = _REPO_ROOT / "data" / "maps" / "finance-renewal-radar.yaml"

# Facts block written by scripts/run_finance_adherence_check.py (slice 3). Read
# by the close-prep section when present. Deliberately a FILE read, not an
# import: the adherence job runs earlier in the morning on its own cadence, and
# the pack must degrade to a stub when it has not run rather than recompute.
ADHERENCE_FACTS_PATH = _REPO_ROOT / "data" / "state" / "finance-adherence-facts.json"

# ── thresholds ───────────────────────────────────────────────────────────────
# Absolute-OR-relative so neither a large entity's rounding nor a small entity's
# proportional swing is missed. Both must be crossed to flag on the relative arm.

# Cash cross-check: QBO bank balance vs Standing ACTUALS week-close.
CASH_DELTA_ABS = 5_000.0
CASH_DELTA_PCT = 0.05

# AR/AP week-over-week movement.
AGING_DELTA_ABS = 10_000.0
AGING_DELTA_PCT = 0.20

# P&L month-over-month revenue / expense swing.
PNL_SWING_ABS = 5_000.0
PNL_SWING_PCT = 0.25

# Renewal radar horizon (days ahead). Past-due always surfaces.
RENEWAL_HORIZON_DAYS = 45

# An adherence facts block older than this is reported as stale rather than current.
ADHERENCE_MAX_AGE_DAYS = 10


# ─────────────────────────────────────────────────────────────────────────────
# QBO entity <-> Standing ACTUALS entity mapping
# ─────────────────────────────────────────────────────────────────────────────
#
# The two systems use DIFFERENT codes for the same store, so the cross-check
# cannot key on a shared string. QBO token keys (11, verified live 2026-08-04)
# are BDM F3E HJRG HJRP HRLLC LEX OSN OSNGF OSNGM OSNGW OSNVV; the cash sheet
# uses gsheets_financials.ENTITY_TO_TAB codes (OSN-GF / OSN-MK / ...).
#
# OSNGM -> OSN-MK by elimination: the sheet carries Warner / Greenfield / ValVista
# / McKellips tabs, QBO carries GW / GF / VV / GM, and GW/GF/VV match by name, so
# the remaining pair is GM = McKellips. The bank-statements tree corroborates it
# ("OSN GMK"). If a future OSN store makes that inference wrong, the cross-check
# would compare two different stores -- so the mapping is pinned by a test.
QBO_TO_SHEET_ENTITY: dict[str, str] = {
    "BDM":   "BDM",
    "F3E":   "F3E",
    "HJRG":  "HJRG",
    "HJRP":  "HJRP",
    "LEX":   "LEX",
    "OSN":   "OSN",
    "OSNGF": "OSN-GF",
    "OSNGM": "OSN-MK",
    "OSNGW": "OSN-GW",
    "OSNVV": "OSN-VV",
}

# QBO realms deliberately EXCLUDED from the cash cross-check, with the reason.
# HRLLC is personal expense tracking, not business data -- gsheets_financials
# excludes its CF_HR LLC tab for the same reason (locked 2026-05-24). Excluded
# entities still appear in the AR/AP and P&L sections, which are QBO-only and
# need no sheet counterpart.
CASH_CHECK_EXCLUDED: dict[str, str] = {
    "HRLLC": "personal expense tracking, not business data (no cash-sheet tab)",
}

# Display labels. Falls back to the raw code, so a newly provisioned realm is
# never dropped for want of a label.
ENTITY_LABELS: dict[str, str] = {
    "BDM":   "Big D Media",
    "F3E":   "F3 Energy",
    "HJRG":  "HJR Global",
    "HJRP":  "HJR Properties",
    "HRLLC": "HR LLC",
    "LEX":   "Lexington Services",
    "OSN":   "One Stop Nutrition",
    "OSNGF": "OSN Greenfield",
    "OSNGM": "OSN McKellips",
    "OSNGW": "OSN Warner",
    "OSNVV": "OSN Val Vista",
}


def entity_label(code: str) -> str:
    return ENTITY_LABELS.get(code.upper(), code)


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Section:
    """One pack section. ``available=False`` means render the stub, not the lines."""
    key: str
    title: str
    lines: list[str] = field(default_factory=list)
    available: bool = True
    stub_reason: Optional[str] = None
    flags: int = 0

    def render(self) -> list[str]:
        out = [f"*{self.title}*"]
        if not self.available:
            out.append(f"  _section unavailable — {self.stub_reason or 'no data'}_")
            return out
        if not self.lines:
            # Belt: an available section with no lines would render as a blank
            # heading, which reads as "nothing wrong". Say so explicitly.
            out.append("  _no data rows returned_")
            return out
        out.extend(f"  {line}" for line in self.lines)
        return out


@dataclass
class ClosePack:
    generated_at: str
    sections: list[Section] = field(default_factory=list)

    @property
    def total_flags(self) -> int:
        return sum(s.flags for s in self.sections)

    @property
    def unavailable_sections(self) -> list[str]:
        return [s.key for s in self.sections if not s.available]

    def facts_lines(self) -> list[str]:
        """The deterministic facts block: every line computed here, none by a model."""
        out: list[str] = []
        for section in self.sections:
            out.extend(section.render())
            out.append("")
        return out

    def render(self) -> str:
        header = [
            ":ledger: *Weekly Finance Close-Support Pack*",
            f"_Generated {self.generated_at} — deterministic; every figure is a direct source read._",
            "",
        ]
        footer = [
            f"_{self.total_flags} item(s) flagged"
            + (
                f"; {len(self.unavailable_sections)} section(s) unavailable: "
                + ", ".join(self.unavailable_sections)
                if self.unavailable_sections
                else ""
            )
            + "._",
        ]
        return "\n".join(header + self.facts_lines() + footer)


# ─────────────────────────────────────────────────────────────────────────────
# Formatting primitives
# ─────────────────────────────────────────────────────────────────────────────

def fmt_money(value: float | None) -> str:
    """Currency, or an explicit ``n/a`` -- never an empty string.

    An empty cell in a finance table reads as zero. Every unknown is named.
    """
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def fmt_delta(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.0f}"


def _crosses(delta: float | None, base: float | None, abs_thr: float, pct_thr: float) -> bool:
    """True when ``delta`` is material against ``base``.

    Absolute arm alone is enough. The relative arm additionally requires the
    absolute delta to clear a tenth of the absolute floor, so a 100% swing on a
    $12 base never flags.
    """
    if delta is None:
        return False
    if abs(delta) >= abs_thr:
        return True
    if base:
        return abs(delta) / abs(base) >= pct_thr and abs(delta) >= abs_thr / 10.0
    return False


# ─────────────────────────────────────────────────────────────────────────────
# QBO report extractors
#
# Every extractor returns None when the shape it needs is absent. Intuit
# occasionally reshapes report payloads, and a wrong-but-plausible number in a
# close pack is far worse than an honest "unavailable" -- so none of these guess.
# ─────────────────────────────────────────────────────────────────────────────

def _iter_rows(report: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Depth-first walk over every Row node in a QBO report tree."""
    def walk(node: Any) -> Iterable[dict[str, Any]]:
        rows = (node.get("Rows") or {}).get("Row") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            yield row
            yield from walk(row)
    yield from walk(report or {})


def _section_name(row: dict[str, Any]) -> str:
    header = (row.get("Header") or {}).get("ColData") or []
    if header and isinstance(header[0], dict):
        return str(header[0].get("value") or "").strip()
    return ""


def _summary_cells(row: dict[str, Any]) -> list[dict[str, Any]]:
    cells = (row.get("Summary") or {}).get("ColData") or []
    return [c for c in cells if isinstance(c, dict)]


def extract_bank_balance(report: dict[str, Any]) -> float | None:
    """Total cash in bank accounts from a QBO BalanceSheet, as float USD.

    Targets the ``Bank Accounts`` section (ASSETS -> Current Assets -> Bank
    Accounts) by name, matching either the section header or its summary label,
    and reads the LAST summary column (the amount). Returns None when no such
    section exists so the caller reports the entity unavailable rather than
    substituting total assets -- which would silently include AR and fixed assets
    and make every cash delta wrong.
    """
    from .tools.qbo_client import _parse_money  # noqa: PLC0415

    for row in _iter_rows(report):
        cells = _summary_cells(row)
        if not cells:
            continue
        name = _section_name(row)
        summary_label = str(cells[0].get("value") or "").strip()
        candidates = {name.lower(), summary_label.lower()}
        if candidates & {"bank accounts", "total bank accounts"}:
            return _parse_money(str(cells[-1].get("value") or ""))
    return None


def _column_titles(report: dict[str, Any]) -> list[str]:
    cols = (report.get("Columns") or {}).get("Column") or []
    return [str(c.get("ColTitle") or "").strip() for c in cols if isinstance(c, dict)]


def extract_aging(report: dict[str, Any]) -> dict[str, Any] | None:
    """Grand total + oldest-bucket callout from a QBO AgedReceivables/AgedPayables.

    Returns ``{"total": float, "oldest_label": str, "oldest_amount": float}``
    or None if no grand-total summary row is present.

    The aging report's grand-total row is the last top-level Row carrying a
    Summary whose cells align with ``Columns``. The oldest bucket is the
    rightmost non-Total column (e.g. "91 and over") -- read positionally from
    Columns rather than by matching bucket names, because Intuit localises and
    re-labels the bucket headers.
    """
    from .tools.qbo_client import _parse_money  # noqa: PLC0415

    titles = _column_titles(report)
    best: list[dict[str, Any]] | None = None
    for row in _iter_rows(report):
        cells = _summary_cells(row)
        if len(cells) >= 2 and (best is None or len(cells) >= len(best)):
            best = cells
    if not best:
        return None

    total = _parse_money(str(best[-1].get("value") or ""))
    if total is None:
        return None

    # Oldest bucket = last data column before the Total column. Column 0 is the
    # customer/vendor name. Require at least name + one bucket + total.
    oldest_label = ""
    oldest_amount: float | None = None
    if len(best) >= 3:
        idx = len(best) - 2
        oldest_amount = _parse_money(str(best[idx].get("value") or ""))
        if idx < len(titles) and titles[idx]:
            oldest_label = titles[idx]
        else:
            oldest_label = "oldest bucket"

    return {
        "total": total,
        "oldest_label": oldest_label,
        "oldest_amount": oldest_amount,
    }


def extract_pnl_expenses(report: dict[str, Any]) -> float | None:
    """Total operating expenses from a QBO P&L, as float USD.

    Mirrors ``qbo_client.extract_pnl_revenue``: exact "Expenses"/"Total Expenses"
    first, then an expense-ish section that is neither Other Expenses nor a COGS
    line. Returns None when nothing matches.
    """
    from .tools.qbo_client import _extract_top_level_sections, _parse_money  # noqa: PLC0415

    totals = _extract_top_level_sections(report)
    if not totals:
        return None
    for name, value in totals.items():
        if name.strip().lower() in ("expenses", "total expenses"):
            return _parse_money(value)
    for name, value in totals.items():
        nl = name.strip().lower()
        if "expense" in nl and "other" not in nl and "cost of goods" not in nl:
            return _parse_money(value)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Snapshots (week-over-week)
# ─────────────────────────────────────────────────────────────────────────────

def _today() -> datetime.date:
    return datetime.date.today()


def load_prior_snapshot(
    *, today: datetime.date | None = None, snapshot_dir: Path | None = None
) -> dict[str, Any] | None:
    """Most recent snapshot STRICTLY before ``today``, or None if there is none.

    Strictly-before matters: a same-day re-run (a retry, or ``--force``) must not
    diff against the snapshot it is about to overwrite, which would report every
    delta as zero and hide real movement.
    """
    day = today or _today()
    directory = snapshot_dir or SNAPSHOT_DIR
    try:
        files = sorted(p for p in directory.glob("*.json") if p.is_file())
    except OSError:
        return None
    cutoff = day.isoformat()
    for path in reversed(files):
        if path.stem >= cutoff:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data.setdefault("_snapshot_date", path.stem)
            return data
    return None


def write_snapshot(
    payload: dict[str, Any],
    *,
    today: datetime.date | None = None,
    snapshot_dir: Path | None = None,
    keep: int = SNAPSHOT_KEEP,
) -> Path:
    """Persist today's snapshot and prune to the newest ``keep`` files."""
    day = today or _today()
    directory = snapshot_dir or SNAPSHOT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day.isoformat()}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    files = sorted(p for p in directory.glob("*.json") if p.is_file())
    for stale in files[:-keep] if keep > 0 else []:
        try:
            stale.unlink()
        except OSError:
            log.warning("finance_close: could not prune snapshot %s", stale.name)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Source adapters — the seams the tests drive
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Sources:
    """Injectable source callables.

    Defaults bind the live connectors lazily so importing this module never
    reaches the network and the tests can substitute pure functions.
    """
    provisioned_entities: Callable[[], list[str]] | None = None
    cash_closing: Callable[[str], dict[str, Any]] | None = None
    balance_sheet: Callable[[str, str], dict[str, Any]] | None = None
    ar_aging: Callable[[str], dict[str, Any]] | None = None
    ap_aging: Callable[[str], dict[str, Any]] | None = None
    profit_loss: Callable[[str, str, str], dict[str, Any]] | None = None
    renewals: Callable[[], list[dict[str, Any]] | None] | None = None
    adherence_facts: Callable[[], dict[str, Any] | None] | None = None

    def get_provisioned(self) -> list[str]:
        if self.provisioned_entities:
            return self.provisioned_entities()
        from .connectors.qbo_oauth import list_provisioned_entities  # noqa: PLC0415
        return list_provisioned_entities()

    def get_cash_closing(self, sheet_entity: str) -> dict[str, Any]:
        """``{"closing": float|None, "week_label": str, "stale": bool, "age_days": int|None}``."""
        if self.cash_closing:
            return self.cash_closing(sheet_entity)
        from .connectors.gsheets_financials import (  # noqa: PLC0415
            entity_to_tab,
            get_cashflow,
        )
        summary = get_cashflow(tab_name=entity_to_tab(sheet_entity))
        return {
            "closing": summary.closing_balance,
            "week_label": summary.week_label,
            "stale": summary.is_stale(),
            "age_days": summary.data_age_days(),
        }

    def get_balance_sheet(self, entity: str, as_of: str) -> dict[str, Any]:
        if self.balance_sheet:
            return self.balance_sheet(entity, as_of)
        from .tools import qbo_client  # noqa: PLC0415
        return qbo_client.get_balance_sheet(entity, as_of_date=as_of)

    def get_ar(self, entity: str) -> dict[str, Any]:
        if self.ar_aging:
            return self.ar_aging(entity)
        from .tools import qbo_client  # noqa: PLC0415
        return qbo_client.get_ar_aging(entity)

    def get_ap(self, entity: str) -> dict[str, Any]:
        if self.ap_aging:
            return self.ap_aging(entity)
        from .tools import qbo_client  # noqa: PLC0415
        return qbo_client.get_ap_aging(entity)

    def get_pnl(self, entity: str, start: str, end: str) -> dict[str, Any]:
        if self.profit_loss:
            return self.profit_loss(entity, start, end)
        from .tools import qbo_client  # noqa: PLC0415
        # Basis deliberately NOT pinned. The comparison is an entity against
        # ITSELF across two months, so the company's own default basis is
        # consistent on both legs -- and pinning Accrual portfolio-wide would
        # produce confidently-wrong figures for the genuinely cash-basis books
        # (LEX-LLC, LBHS). The rendered basis is read back and labelled.
        return qbo_client.get_profit_loss(entity, start_date=start, end_date=end)

    def get_renewals(self) -> list[dict[str, Any]] | None:
        if self.renewals:
            return self.renewals()
        return load_renewals()

    def get_adherence(self) -> dict[str, Any] | None:
        if self.adherence_facts:
            return self.adherence_facts()
        return load_adherence_facts()


def load_renewals(path: Path | None = None) -> list[dict[str, Any]] | None:
    """Renewal/subscription entries, or None when the map is absent/unreadable."""
    import yaml  # noqa: PLC0415

    target = path or RENEWAL_MAP_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("finance_close: renewal radar unreadable (%s)", exc)
        return None
    items = raw.get("renewals")
    if not isinstance(items, list):
        return None
    return [i for i in items if isinstance(i, dict)]


def load_adherence_facts(path: Path | None = None) -> dict[str, Any] | None:
    """Facts block from the adherence job, or None when it has not run."""
    target = path or ADHERENCE_FACTS_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ─────────────────────────────────────────────────────────────────────────────
# Period helpers
# ─────────────────────────────────────────────────────────────────────────────

def last_completed_months(today: datetime.date | None = None) -> tuple[tuple[str, str], tuple[str, str], str, str]:
    """(current_range, prior_range, current_label, prior_label) for the MoM check.

    Both legs are FULL calendar months -- the last completed month and the one
    before it. A month-to-date leg would read as a collapse in revenue on any run
    before month end, which is the classic false swing in a Monday digest.
    """
    day = today or _today()
    first_this = day.replace(day=1)
    cur_end = first_this - datetime.timedelta(days=1)
    cur_start = cur_end.replace(day=1)
    prior_end = cur_start - datetime.timedelta(days=1)
    prior_start = prior_end.replace(day=1)
    return (
        (cur_start.isoformat(), cur_end.isoformat()),
        (prior_start.isoformat(), prior_end.isoformat()),
        cur_start.strftime("%b %Y"),
        prior_start.strftime("%b %Y"),
    )


def _week_close_as_of(week_label: str, today: datetime.date | None = None) -> tuple[str, bool]:
    """(as_of ISO date, exact) for the balance-sheet leg of the cash cross-check.

    Uses the cash sheet's own week date so both legs describe the same moment.
    Falls back to today when the week label cannot be parsed, and says so via
    ``exact=False`` -- an unlabelled mismatched as-of date would make every delta
    look like a reconciliation break.
    """
    from .connectors.gsheets_financials import _parse_week_date  # noqa: PLC0415

    day = today or _today()
    parsed = _parse_week_date(week_label, today=day)
    if parsed is None:
        return day.isoformat(), False
    return parsed.isoformat(), True


# ─────────────────────────────────────────────────────────────────────────────
# Section 1 — per-entity cash: Standing ACTUALS vs QBO bank balances
# ─────────────────────────────────────────────────────────────────────────────

def build_cash_section(
    entities: list[str],
    sources: Sources,
    *,
    today: datetime.date | None = None,
) -> tuple[Section, dict[str, Any]]:
    """Cash cross-check. Returns (section, snapshot fragment)."""
    section = Section(key="cash", title=":bank: Cash — cash sheet vs books")
    snap: dict[str, Any] = {}

    checkable = [e for e in entities if e in QBO_TO_SHEET_ENTITY]
    if not checkable:
        section.available = False
        section.stub_reason = "no provisioned entity maps to a cash-sheet tab"
        return section, snap

    rows: list[dict[str, Any]] = []
    sheet_values_seen = 0
    sheet_read_failures = 0

    for entity in checkable:
        sheet_entity = QBO_TO_SHEET_ENTITY[entity]
        closing = week_label = age_days = None
        stale = False
        try:
            cash = sources.get_cash_closing(sheet_entity)
            closing = cash.get("closing")
            week_label = cash.get("week_label") or ""
            stale = bool(cash.get("stale"))
            age_days = cash.get("age_days")
            if closing is not None:
                sheet_values_seen += 1
        except Exception as exc:  # noqa: BLE001 -- per-entity fail-soft
            sheet_read_failures += 1
            log.warning("finance_close: cash sheet read failed for %s: %s", sheet_entity, exc)

        as_of, exact = _week_close_as_of(week_label or "", today=today)
        bank = None
        try:
            report = sources.get_balance_sheet(entity, as_of)
            bank = extract_bank_balance(report)
        except Exception as exc:  # noqa: BLE001 -- per-entity fail-soft
            log.warning("finance_close: balance sheet failed for %s: %s", entity, exc)

        delta = None if (bank is None or closing is None) else bank - closing
        rows.append({
            "entity": entity,
            "sheet_closing": closing,
            "bank": bank,
            "delta": delta,
            "week_label": week_label,
            "as_of": as_of,
            "as_of_exact": exact,
            "stale": stale,
            "age_days": age_days,
        })

    # Label-drift detector (2026-06-04 doctrine). Every closing balance None
    # while reads themselves SUCCEEDED is the signature of a renamed row, not of
    # an outage -- and it is exactly the state that once rendered as a wall of
    # '--' reading like zeros. Degrade the whole section.
    if sheet_values_seen == 0 and sheet_read_failures == 0:
        section.available = False
        section.stub_reason = (
            "Standing ACTUALS returned no closing balance for any entity — the "
            "cash-sheet row labels may have been renamed (expected a row matching "
            "'Ending Cash/CC Book Balance'). Cross-check cannot run until the "
            "connector's label set is reconciled with the sheet."
        )
        return section, snap
    if sheet_values_seen == 0:
        section.available = False
        section.stub_reason = (
            f"cash sheet unreadable for all {len(checkable)} entities "
            f"({sheet_read_failures} read failure(s))"
        )
        return section, snap

    week_label = next((r["week_label"] for r in rows if r["week_label"]), "")
    if week_label:
        section.lines.append(f"Cash-sheet week: {week_label}")
    stale_rows = [r for r in rows if r["stale"]]
    if stale_rows:
        ages = [r["age_days"] for r in stale_rows if r["age_days"] is not None]
        age_note = f" (~{max(ages)}d old)" if ages else ""
        section.lines.append(
            f":warning: cash sheet appears BEHIND for {len(stale_rows)} entity(ies){age_note} "
            "— figures below are as-of that week, not today."
        )

    complete = 0
    for row in rows:
        label = entity_label(row["entity"])
        if row["sheet_closing"] is None or row["bank"] is None:
            missing = []
            if row["sheet_closing"] is None:
                missing.append("cash sheet")
            if row["bank"] is None:
                missing.append("books")
            section.lines.append(
                f"• {label}: unavailable — no figure from {' and '.join(missing)}"
            )
            continue
        complete += 1
        flagged = _crosses(row["delta"], row["sheet_closing"], CASH_DELTA_ABS, CASH_DELTA_PCT)
        mark = ":triangular_flag_on_post: " if flagged else ""
        if flagged:
            section.flags += 1
        as_of_note = "" if row["as_of_exact"] else f" [books as of {row['as_of']}, week date unparsed]"
        section.lines.append(
            f"• {mark}{label}: sheet {fmt_money(row['sheet_closing'])} vs books "
            f"{fmt_money(row['bank'])} — delta {fmt_delta(row['delta'])}{as_of_note}"
        )
        snap[row["entity"]] = {
            "sheet_closing": row["sheet_closing"],
            "bank": row["bank"],
            "delta": row["delta"],
        }

    excluded = [e for e in entities if e in CASH_CHECK_EXCLUDED]
    section.lines.append(
        f"_Cross-checked {complete} of {len(checkable)} mapped entity(ies)."
        + (
            f" Excluded: {', '.join(f'{entity_label(e)} ({CASH_CHECK_EXCLUDED[e]})' for e in excluded)}."
            if excluded
            else ""
        )
        + "_"
    )
    return section, snap


# ─────────────────────────────────────────────────────────────────────────────
# Section 2 — AR/AP aging, week-over-week
# ─────────────────────────────────────────────────────────────────────────────

def build_aging_section(
    entities: list[str],
    sources: Sources,
    prior: dict[str, Any] | None,
) -> tuple[Section, dict[str, Any]]:
    section = Section(key="aging", title=":inbox_tray: AR / AP aging — week over week")
    snap: dict[str, Any] = {}
    prior_aging = ((prior or {}).get("aging") or {}) if isinstance(prior, dict) else {}

    rows: list[dict[str, Any]] = []
    for entity in entities:
        entry: dict[str, Any] = {"entity": entity}
        for kind, getter in (("ar", sources.get_ar), ("ap", sources.get_ap)):
            try:
                entry[kind] = extract_aging(getter(entity))
            except Exception as exc:  # noqa: BLE001 -- per-entity, per-kind fail-soft
                log.warning("finance_close: %s aging failed for %s: %s", kind.upper(), entity, exc)
                entry[kind] = None
        rows.append(entry)

    with_data = [r for r in rows if r.get("ar") or r.get("ap")]
    if not with_data:
        section.available = False
        section.stub_reason = (
            f"no aging report returned a grand total for any of {len(entities)} entity(ies)"
        )
        return section, snap

    if not prior_aging:
        section.lines.append("_First run — no prior snapshot, so no week-over-week deltas yet._")

    for row in rows:
        entity = row["entity"]
        label = entity_label(entity)
        if not (row.get("ar") or row.get("ap")):
            section.lines.append(f"• {label}: unavailable — no aging totals returned")
            continue

        parts: list[str] = []
        snap_entry: dict[str, Any] = {}
        flagged_here = False
        for kind in ("ar", "ap"):
            data = row.get(kind)
            if not data:
                parts.append(f"{kind.upper()} n/a")
                continue
            total = data["total"]
            snap_entry[kind] = total
            prior_total = (prior_aging.get(entity) or {}).get(kind)
            delta = None if prior_total is None else total - prior_total
            if _crosses(delta, prior_total, AGING_DELTA_ABS, AGING_DELTA_PCT):
                flagged_here = True
                parts.append(f"{kind.upper()} {fmt_money(total)} ({fmt_delta(delta)} WoW)")
            elif delta is not None:
                parts.append(f"{kind.upper()} {fmt_money(total)} ({fmt_delta(delta)} WoW)")
            else:
                parts.append(f"{kind.upper()} {fmt_money(total)}")

        mark = ":triangular_flag_on_post: " if flagged_here else ""
        if flagged_here:
            section.flags += 1
        section.lines.append(f"• {mark}{label}: " + ", ".join(parts))

        # Oldest-bucket callout: the aged tail is the close-prep signal, so it is
        # surfaced separately from the total rather than folded into it.
        for kind in ("ar", "ap"):
            data = row.get(kind)
            if not data:
                continue
            amount = data.get("oldest_amount")
            if amount is None or abs(amount) < 1.0:
                continue
            bucket = data.get("oldest_label") or "oldest bucket"
            section.lines.append(
                f"    ↳ {kind.upper()} aged tail — {bucket}: {fmt_money(amount)}"
            )
        if snap_entry:
            snap[entity] = snap_entry

    section.lines.append(f"_Aging read for {len(with_data)} of {len(entities)} entity(ies)._")
    return section, snap


# ─────────────────────────────────────────────────────────────────────────────
# Section 3 — P&L sanity, month over month
# ─────────────────────────────────────────────────────────────────────────────

def build_pnl_section(
    entities: list[str],
    sources: Sources,
    *,
    today: datetime.date | None = None,
) -> tuple[Section, dict[str, Any]]:
    from .tools.qbo_client import _report_basis, extract_pnl_revenue  # noqa: PLC0415

    section = Section(key="pnl", title=":bar_chart: P&L sanity — month over month")
    snap: dict[str, Any] = {}
    cur_range, prior_range, cur_label, prior_label = last_completed_months(today)

    section.lines.append(f"Comparing {cur_label} vs {prior_label} (full calendar months).")

    rows: list[dict[str, Any]] = []
    for entity in entities:
        entry: dict[str, Any] = {"entity": entity}
        for key, rng in (("cur", cur_range), ("prior", prior_range)):
            try:
                report = sources.get_pnl(entity, rng[0], rng[1])
                entry[f"{key}_revenue"] = extract_pnl_revenue(report)
                entry[f"{key}_expenses"] = extract_pnl_expenses(report)
                entry[f"{key}_basis"] = _report_basis(report)
            except Exception as exc:  # noqa: BLE001 -- per-entity, per-period fail-soft
                log.warning("finance_close: P&L %s failed for %s: %s", key, entity, exc)
                entry[f"{key}_revenue"] = None
                entry[f"{key}_expenses"] = None
                entry[f"{key}_basis"] = None
        rows.append(entry)

    usable = [
        r for r in rows
        if r.get("cur_revenue") is not None or r.get("cur_expenses") is not None
    ]
    if not usable:
        section.available = False
        section.stub_reason = (
            f"no P&L returned an income or expense total for any of {len(entities)} entity(ies)"
        )
        return section, snap

    for row in rows:
        entity = row["entity"]
        label = entity_label(entity)
        if row.get("cur_revenue") is None and row.get("cur_expenses") is None:
            section.lines.append(f"• {label}: unavailable — no P&L totals returned for {cur_label}")
            continue

        basis = row.get("cur_basis")
        prior_basis = row.get("prior_basis")
        # A basis change between the two legs makes the swing an artifact of the
        # report setting, not of the business. Say so rather than flag a phantom.
        basis_note = f" [{basis} basis]" if basis else ""
        basis_mismatch = bool(basis and prior_basis and basis != prior_basis)

        parts: list[str] = []
        flagged_here = False
        for kind, cur_key, prior_key in (
            ("revenue", "cur_revenue", "prior_revenue"),
            ("expenses", "cur_expenses", "prior_expenses"),
        ):
            cur, prev = row.get(cur_key), row.get(prior_key)
            if cur is None:
                parts.append(f"{kind} n/a")
                continue
            delta = None if prev is None else cur - prev
            if delta is None:
                parts.append(f"{kind} {fmt_money(cur)} (no prior month)")
                continue
            if not basis_mismatch and _crosses(delta, prev, PNL_SWING_ABS, PNL_SWING_PCT):
                flagged_here = True
            parts.append(f"{kind} {fmt_money(cur)} ({fmt_delta(delta)} MoM)")

        mark = ":triangular_flag_on_post: " if flagged_here else ""
        if flagged_here:
            section.flags += 1
        section.lines.append(f"• {mark}{label}: " + ", ".join(parts) + basis_note)
        if basis_mismatch:
            section.lines.append(
                f"    ↳ basis changed between months ({prior_basis} → {basis}); "
                "swing is not comparable and was not flagged"
            )
        snap[entity] = {
            "revenue": row.get("cur_revenue"),
            "expenses": row.get("cur_expenses"),
            "period": cur_range[0],
        }

    section.lines.append(f"_P&L read for {len(usable)} of {len(entities)} entity(ies)._")
    return section, snap


# ─────────────────────────────────────────────────────────────────────────────
# Section 4 — close-prep notes
# ─────────────────────────────────────────────────────────────────────────────

def build_close_prep_section(
    sources: Sources,
    *,
    cash_section: Section,
    today: datetime.date | None = None,
) -> Section:
    """Close-prep notes: adherence facts + unreconciled-looking items.

    Consumes the adherence job's facts block (slice 3) rather than recomputing
    it, so there is exactly one producer of those facts.
    """
    section = Section(key="close_prep", title=":clipboard: Close-prep notes")
    day = today or _today()

    # Unreconciled-looking: a material cash delta IS the reconciliation signal,
    # so it is restated here (as a count) instead of being independently derived.
    if cash_section.available:
        if cash_section.flags:
            section.lines.append(
                f"• :triangular_flag_on_post: {cash_section.flags} entity(ies) show a cash "
                f"delta over threshold (>{fmt_money(CASH_DELTA_ABS)} or "
                f"{CASH_DELTA_PCT:.0%}) — unreconciled-looking; review before close."
            )
            section.flags += 1
        else:
            section.lines.append("• Cash sheet and books agree within threshold for every checked entity.")
    else:
        section.lines.append("• Cash cross-check unavailable this run — reconciliation status unknown.")

    facts = None
    try:
        facts = sources.get_adherence()
    except Exception as exc:  # noqa: BLE001
        log.warning("finance_close: adherence facts unreadable: %s", exc)

    if not facts:
        section.lines.append(
            "• _Adherence facts unavailable_ — the finance-adherence check has not "
            "produced a facts block (cash-sheet freshness, monthly-filing presence and "
            "bank-statement staleness are therefore unknown this run)."
        )
        return section

    generated = str(facts.get("generated_date") or facts.get("generated_at") or "")[:10]
    age_days: int | None = None
    try:
        age_days = (day - datetime.date.fromisoformat(generated)).days
    except ValueError:
        pass
    if age_days is not None and age_days > ADHERENCE_MAX_AGE_DAYS:
        section.lines.append(
            f"• :warning: Adherence facts are STALE (generated {generated}, {age_days}d ago) "
            "— treat the items below as historical, not current."
        )
        section.flags += 1
    elif generated:
        section.lines.append(f"• Adherence facts as of {generated}.")

    for line in facts.get("facts") or []:
        if not isinstance(line, str) or not line.strip():
            continue
        flagged = any(
            token in line.lower() for token in ("missing", "stale", "overdue", "absent", "no_content")
        )
        if flagged:
            section.flags += 1
        section.lines.append(f"• {':triangular_flag_on_post: ' if flagged else ''}{line.strip()}")

    return section


# ─────────────────────────────────────────────────────────────────────────────
# Section 5 — renewal / payment radar
# ─────────────────────────────────────────────────────────────────────────────

def build_renewal_section(
    sources: Sources,
    *,
    today: datetime.date | None = None,
    horizon_days: int = RENEWAL_HORIZON_DAYS,
) -> Section:
    section = Section(key="renewals", title=":calendar: Renewal / payment radar")
    day = today or _today()

    try:
        items = sources.get_renewals()
    except Exception as exc:  # noqa: BLE001
        log.warning("finance_close: renewal radar failed: %s", exc)
        items = None

    if items is None:
        section.available = False
        section.stub_reason = (
            "renewal radar map missing or unreadable "
            "(data/maps/finance-renewal-radar.yaml)"
        )
        return section
    if not items:
        section.available = False
        section.stub_reason = "renewal radar map has no entries"
        return section

    dated: list[tuple[int, dict[str, Any]]] = []
    undated: list[dict[str, Any]] = []
    for item in items:
        raw = str(item.get("next_due") or "").strip()
        try:
            due = datetime.date.fromisoformat(raw)
        except ValueError:
            undated.append(item)
            continue
        dated.append(((due - day).days, item))

    dated.sort(key=lambda pair: pair[0])
    shown = 0
    for days_out, item in dated:
        if days_out > horizon_days:
            continue
        shown += 1
        name = str(item.get("name") or "unnamed")
        ent = str(item.get("entity") or "").strip()
        ent_tag = f" [{ent}]" if ent else ""
        amount = item.get("amount")
        amount_val = float(amount) if isinstance(amount, (int, float)) else None
        cost = f" — {fmt_money(amount_val)}" if amount_val is not None else ""
        cadence = str(item.get("cadence") or "").strip()
        cadence_tag = f" ({cadence})" if cadence else ""
        if days_out < 0:
            section.flags += 1
            when = f":rotating_light: PAST DUE {abs(days_out)}d"
        elif days_out <= 7:
            section.flags += 1
            when = f":warning: due in {days_out}d"
        else:
            when = f"due in {days_out}d"
        section.lines.append(f"• {when} — {name}{ent_tag}{cost}{cadence_tag}")

    if not shown:
        section.lines.append(f"• Nothing due or past due within {horizon_days} days.")
    if undated:
        names = ", ".join(str(i.get("name") or "unnamed") for i in undated[:5])
        more = f" (+{len(undated) - 5} more)" if len(undated) > 5 else ""
        section.lines.append(
            f"• _{len(undated)} entry(ies) have no parseable next_due and were not "
            f"assessed: {names}{more}_"
        )
    section.lines.append(
        f"_Radar covers {len(items)} tracked item(s); {shown} within {horizon_days}d._"
    )
    return section


# ─────────────────────────────────────────────────────────────────────────────
# Pack assembly
# ─────────────────────────────────────────────────────────────────────────────

def build_pack(
    sources: Sources | None = None,
    *,
    entities: list[str] | None = None,
    today: datetime.date | None = None,
    snapshot_dir: Path | None = None,
    persist_snapshot: bool = True,
) -> ClosePack:
    """Assemble the full close-support pack.

    Every section is built inside its own guard, so an unforeseen exception in one
    becomes a stub rather than losing the whole pack.
    """
    src = sources or Sources()
    day = today or _today()

    try:
        provisioned = entities if entities is not None else src.get_provisioned()
    except Exception as exc:  # noqa: BLE001
        log.error("finance_close: could not list provisioned entities: %s", exc)
        provisioned = []

    pack = ClosePack(generated_at=day.isoformat())
    if not provisioned:
        for key, title in (
            ("cash", ":bank: Cash — cash sheet vs books"),
            ("aging", ":inbox_tray: AR / AP aging — week over week"),
            ("pnl", ":bar_chart: P&L sanity — month over month"),
        ):
            pack.sections.append(Section(
                key=key, title=title, available=False,
                stub_reason="no provisioned accounting entities could be listed",
            ))
        pack.sections.append(build_close_prep_section(
            src, cash_section=pack.sections[0], today=day,
        ))
        pack.sections.append(build_renewal_section(src, today=day))
        return pack

    prior = load_prior_snapshot(today=day, snapshot_dir=snapshot_dir)
    snapshot: dict[str, Any] = {"generated_at": day.isoformat()}

    def guarded(key: str, title: str, fn: Callable[[], tuple[Section, dict[str, Any]]]) -> Section:
        try:
            section, frag = fn()
        except Exception as exc:  # noqa: BLE001 -- section-level fail-soft
            log.error("finance_close: section %s raised: %s", key, exc)
            return Section(
                key=key, title=title, available=False,
                stub_reason=f"section builder failed ({type(exc).__name__})",
            )
        if frag:
            snapshot[key] = frag
        return section

    cash_section = guarded(
        "cash", ":bank: Cash — cash sheet vs books",
        lambda: build_cash_section(provisioned, src, today=day),
    )
    aging_section = guarded(
        "aging", ":inbox_tray: AR / AP aging — week over week",
        lambda: build_aging_section(provisioned, src, prior),
    )
    pnl_section = guarded(
        "pnl", ":bar_chart: P&L sanity — month over month",
        lambda: build_pnl_section(provisioned, src, today=day),
    )

    try:
        close_prep = build_close_prep_section(src, cash_section=cash_section, today=day)
    except Exception as exc:  # noqa: BLE001
        log.error("finance_close: close-prep section raised: %s", exc)
        close_prep = Section(
            key="close_prep", title=":clipboard: Close-prep notes", available=False,
            stub_reason=f"section builder failed ({type(exc).__name__})",
        )
    try:
        renewals = build_renewal_section(src, today=day)
    except Exception as exc:  # noqa: BLE001
        log.error("finance_close: renewal section raised: %s", exc)
        renewals = Section(
            key="renewals", title=":calendar: Renewal / payment radar", available=False,
            stub_reason=f"section builder failed ({type(exc).__name__})",
        )

    pack.sections = [cash_section, aging_section, pnl_section, close_prep, renewals]

    if persist_snapshot:
        try:
            write_snapshot(snapshot, today=day, snapshot_dir=snapshot_dir)
        except OSError as exc:
            log.warning("finance_close: snapshot write failed: %s", exc)

    return pack


# ─────────────────────────────────────────────────────────────────────────────
# Optional narration (default OFF)
# ─────────────────────────────────────────────────────────────────────────────

def narration_enabled() -> bool:
    """``FINANCE_CLOSE_NARRATE`` gate. Default OFF.

    The facts block is the deliverable; narration only restates it. Shipping the
    gate default-OFF means the first live packs are pure computed facts with zero
    fabrication surface, and the flag can be flipped once Justin has read a few.
    """
    return os.environ.get("FINANCE_CLOSE_NARRATE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def narrate(pack: ClosePack, *, api_key: str | None = None) -> str | None:
    """One-paragraph restatement of the facts block, or None.

    FAIL-CLOSED in every direction: the gate must be on, a key must exist, and any
    API or parse failure returns None so the caller posts the facts block alone.
    The model is handed ONLY the already-computed facts lines -- never a raw
    report -- so it cannot introduce a figure that Python did not compute.
    """
    if not narration_enabled():
        return None
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        log.warning("finance_close: narration enabled but ANTHROPIC_API_KEY is unset")
        return None

    facts = "\n".join(pack.facts_lines())
    prompt = (
        "Below is a finance close-support facts block. Write ONE short paragraph "
        "(max 60 words) telling the reader where to look first.\n\n"
        "Rules: use ONLY figures that appear below — never compute, infer, or "
        "estimate a number. Do not restate every line. Name the flagged items and "
        "any unavailable section. No PHI, no client-level detail.\n\n"
        f"---\n{facts}\n---"
    )
    try:
        import anthropic  # noqa: PLC0415

        from .llm_usage import log_usage  # noqa: PLC0415

        model = "claude-haiku-4-5-20251001"
        msg = anthropic.Anthropic(api_key=key).messages.create(
            model=model,
            max_tokens=300,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        )
        log_usage(msg, caller="finance_close_pack", model=model)
        text = (msg.content[0].text or "").strip() if msg.content else ""
        return text or None
    except Exception as exc:  # noqa: BLE001 -- narration is never load-bearing
        log.warning("finance_close: narration failed, posting facts only: %s", exc)
        return None
