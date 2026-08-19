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
import re
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
# The producer is WEEKLY and fires 45 minutes before this pack, so anything beyond a
# couple of days means the adherence job did not run this morning. A 10-day window
# let a fully-missed week (7-day-old facts) present as current.
ADHERENCE_MAX_AGE_DAYS = 3

# Adherence statuses that mean "a human should look". Mirrors
# ``finance_adherence.PROBLEM_STATUSES`` -- duplicated as literals rather than
# imported so this consumer never depends on the producer module being present.
_ADHERENCE_PROBLEM_STATUSES = frozenset({"missing", "stale"})

# Cap on adherence fact lines rendered into the pack. An unbounded producer would
# push the Slack post past the 40k text limit, which fails delivery outright.
_MAX_ADHERENCE_LINES = 40

# Any money-shaped token. Used to CODE-enforce that the optional narration restates
# only figures this module computed -- see narrate().
_MONEY_TOKEN_RE = re.compile(r"\$\s?[\d,]+(?:\.\d+)?")


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

# Sheet rows that are CONSOLIDATIONS of several QBO realms rather than the books
# of a realm that holds cash itself (cq-6fbb9d717512).
#
# Verified live 2026-08-04: the sheet's "OSN" row is the tab "OSN Consolidated"
# and closed the week at $37,605 -- exactly the four store tabs summed
# (Warner 4,722 + McKellips 6,936 + Greenfield 4,365 + Val Vista 21,581). The QBO
# realm OSN, meanwhile, is a cash-less holding shell: ONE bank account at $0.00
# and no credit cards.
#
# So comparing sheet-OSN against realm-OSN's books compared a consolidation against
# an empty shell and flagged a phantom ~$37.6K delta EVERY week. Re-based here to
# the sum of the member realms' books, which is the comparison the sheet row
# actually describes -- and a genuinely useful check that the consolidation ties.
#
# The member realms are still cross-checked individually against their own tabs;
# this row additionally checks that the roll-up agrees.
SHEET_ROLLUPS: dict[str, tuple[str, ...]] = {
    "OSN": ("OSNGF", "OSNGM", "OSNGW", "OSNVV"),
}

# QBO realms deliberately EXCLUDED from the ENTIRE pack, with the reason.
#
# HRLLC is personal expense tracking, not business data -- gsheets_financials
# excludes its CF_HR LLC tab for that reason (locked 2026-05-24), and the
# Cross-Entity Cash Pulse omits it too. The exclusion is therefore about
# SENSITIVITY, not about which sources happen to carry the entity -- so it must
# apply pack-wide. Scoping it to the cash cross-check alone (because that is the
# section needing a sheet counterpart) still posted HR LLC's AR/AP totals, aged
# tails and P&L to a multi-member finance channel and Justin's DM every Monday.
#
# The exclusion is stated in the cash section's footer rather than being silent, so
# reinstating an entity is a visible decision either way.
PACK_EXCLUDED_ENTITIES: dict[str, str] = {
    "HRLLC": "personal expense tracking, not business data",
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
    # Coverage, as STRUCTURE rather than only as footer prose. The founder cut and
    # the close-prep summary both need to know that a section ran on 1 of 10
    # entities, and neither can be asked to parse an italic footer to find out --
    # that is how "0 items flagged" ends up meaning "nothing was checked".
    covered: Optional[int] = None
    expected: Optional[int] = None

    @property
    def is_partial(self) -> bool:
        """True when the section ran, but not on everything it was asked to cover."""
        if not self.available or self.covered is None or self.expected is None:
            return False
        return self.covered < self.expected

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
    #: The rendered Monday worksheet (13WCF S3), or None when it could not be
    #: built. Carried ON the pack so the delivery script writes the SAME
    #: computation the sections were rendered from -- rebuilding it from a fresh
    #: Sources would let the file and the posted pack disagree about the same
    #: Monday, which is the failure the supersession exists to prevent.
    worksheet: Optional[str] = None

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

def _scrub_external(text: str, cap: int = 120) -> str:
    """Neutralize Slack control syntax in a string this module did not author.

    ``slack_egress.sanitize_text`` deliberately PRESERVES ``<...>`` tokens -- they
    are the sanctioned citation form -- so a value carried in from a human-maintained
    YAML file or from the adherence facts block can render a live ``<url|label>``
    link or an ``<!channel>`` ping inside a finance channel, in a message signed by
    Cora. The finance surfaces are exactly where a payment link is most likely to be
    trusted, so every externally-sourced string is stripped of the angle brackets,
    collapsed to one line, and length-capped before it reaches a renderer.
    """
    if not text:
        return ""
    flattened = " ".join(str(text).split())
    return flattened.replace("<", "").replace(">", "")[:cap]


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

    A zero (or missing) base is the MOST extreme relative move -- the percentage is
    undefined, not small -- so it must not fall through to "no flag". Treating
    ``base == 0`` as unflaggable made two identically-rendered rows behave
    oppositely: sheet $0 vs books $4,900 stayed silent while sheet $0.01 vs books
    $600 flagged. Zero balances are live in this portfolio, so the floor-only arm
    applies instead.
    """
    if delta is None:
        return False
    if abs(delta) >= abs_thr:
        return True
    if not base:
        return abs(delta) >= abs_thr / 10.0
    return abs(delta) / abs(base) >= pct_thr and abs(delta) >= abs_thr / 10.0


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


def _named_section_total(report: dict[str, Any], names: set[str]) -> float | None:
    """Amount from the first section whose header OR summary label is in ``names``.

    Reads the LAST summary column. Returns None when no such section exists, so a
    caller can distinguish "absent" from "zero".
    """
    from .tools.qbo_client import _parse_money  # noqa: PLC0415

    for row in _iter_rows(report):
        cells = _summary_cells(row)
        if not cells:
            continue
        candidates = {
            _section_name(row).lower(),
            str(cells[0].get("value") or "").strip().lower(),
        }
        if candidates & names:
            return _parse_money(str(cells[-1].get("value") or ""))
    return None


def extract_bank_balance(report: dict[str, Any]) -> float | None:
    """Total cash in bank accounts from a QBO BalanceSheet, as float USD.

    Targets the ``Bank Accounts`` section (ASSETS -> Current Assets -> Bank
    Accounts) only. Returns None when no such section exists so the caller reports
    the entity unavailable rather than substituting total assets -- which would
    silently include AR and fixed assets and make every cash delta wrong.
    """
    return _named_section_total(report, {"bank accounts", "total bank accounts"})


def extract_credit_card_balance(report: dict[str, Any]) -> float | None:
    """Total credit-card liability from a QBO BalanceSheet, as float USD.

    Needed because the cash sheet's row is literally
    ``Ending Cash/CC Book Balance`` -- cash NET OF credit cards -- while QBO reports
    cards in their own ``Credit Cards`` section under liabilities, invisible to
    :func:`extract_bank_balance`. Comparing Bank-Accounts-only against a Cash/CC row
    yields a delta equal to the card balance for every card-carrying entity (F3E ad
    spend, BDM media buying, the OSN stores), against a $5,000 threshold -- a
    recurring false "unreconciled-looking" flag on the pack's headline section.

    Returns None when the section is absent, which the caller treats as $0 and says
    so rather than silently assuming it.
    """
    return _named_section_total(report, {"credit cards", "total credit cards"})


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
        # A grand-total row EXISTS but its amount cell is blank. For an aging report
        # that is how QBO renders "nothing outstanding" -- so returning None here
        # made clean books ($0 AR and $0 AP) render as
        # "unavailable — no aging totals returned" and drop out of the coverage
        # count. Structure present + blank amount is a truthful zero; NO structure
        # at all still returns None above.
        total = 0.0

    # Oldest bucket = last data column before the Total column. Read the label
    # positionally from ``Columns`` -- but ONLY when the summary row and the column
    # header row are index-aligned. On a short summary row the same index lands on
    # a different column, which once labelled the NEWEST bucket ("Current") as the
    # aged tail. Misaligned -> omit the callout rather than mislabel it.
    oldest_label = ""
    oldest_amount: float | None = None
    if len(best) >= 3 and len(titles) == len(best):
        idx = len(best) - 2
        oldest_amount = _parse_money(str(best[idx].get("value") or ""))
        oldest_label = titles[idx] or "oldest bucket"

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
    bank_snapshot: Callable[[], dict[str, Any] | None] | None = None
    # ── 13WCF shadow-ledger stores (M1/M2), injected so the pack's forecast and
    # parallel sections are testable without touching disk. These are the SOLE
    # forecast baseline as of M3 -- there is deliberately no sheet-side or
    # pack-history fallback behind them (migration Mig-1: a superseded lane that
    # keeps a quiet fallback is a second variance system, not a supersession).
    cashflow_snapshot: Callable[[], dict[str, Any] | None] | None = None
    cashflow_snapshot_dates: Callable[[], list[datetime.date]] | None = None
    cashflow_load_snapshot: Callable[[datetime.date], dict[str, Any] | None] | None = None
    cashflow_finalized: Callable[[], dict[str, Any] | None] | None = None
    cashflow_preliminary: Callable[[str], dict[str, Any] | None] | None = None
    cashflow_newest_preliminary: Callable[[], dict[str, Any] | None] | None = None
    cashflow_entity_map: Callable[[], Any] | None = None

    def get_provisioned(self) -> list[str]:
        if self.provisioned_entities:
            return self.provisioned_entities()
        from .connectors.qbo_oauth import list_provisioned_entities  # noqa: PLC0415
        return list_provisioned_entities()

    def get_cash_closing(self, sheet_entity: str) -> dict[str, Any]:
        """``{"closing", "is_actual", "week_label", "stale", "age_days"}``.

        Reads the ACTUAL-first ending cash, NOT ``CashflowSummary.closing_balance``.

        This distinction is the whole validity of the cross-check.
        ``closing_balance`` is FORECAST-first
        (``gsheets_financials``: ``forecast if forecast is not None else actual``),
        while ``ending_cash_series`` / ``ending_cash_outlook`` is actual-first. A
        D-051 review already found and fixed exactly this divergence in
        ``scripts/write_cashflow_snapshot.py`` -- "they disagreed mid-week".
        Comparing the sheet's FORECAST against the books' ACTUAL would report the
        sheet's own forecast variance (which the sheet already has a DIFF column
        for) as a books-vs-sheet reconciliation break: reconciled entities would
        flag every week and genuinely broken ones could read clean.

        ``is_actual`` travels with the figure so the caller can label a
        forecast-only week and decline to flag it -- a forecast is not a
        reconciliation signal in either direction.
        """
        if self.cash_closing:
            return self.cash_closing(sheet_entity)
        from .connectors.gsheets_financials import (  # noqa: PLC0415
            entity_to_tab,
            get_cashflow,
        )
        summary = get_cashflow(tab_name=entity_to_tab(sheet_entity))
        outlook = summary.ending_cash_outlook(weeks=0)
        if outlook:
            closing = outlook[0].get("ending_cash")
            is_actual = bool(outlook[0].get("is_actual"))
        else:
            # Target week absent from the series: fall back to the forecast-first
            # value and mark it not-actual, so it is LABELLED rather than silently
            # compared as though it were an actual.
            closing = summary.closing_balance
            is_actual = False
        return {
            "closing": closing,
            "is_actual": is_actual,
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

    # `get_cash_dual` was REMOVED at 13WCF M3 (2026-08-18) with the forecast_assist
    # supersession. It read the sheet's own forecast/actual dual series -- the
    # column the sheet OVERWRITES at week close (D-121) -- and it no longer has a
    # consumer. Deleting it rather than leaving it unused is the point of the
    # supersession: a live accessor to the retired source is the thing someone
    # re-wires. The connector-side `CashflowSummary.ending_cash_dual` is
    # untouched; this only removes the pack's door to it.

    # ── 13WCF stores ────────────────────────────────────────────────────────

    def get_cashflow_snapshot(self) -> dict[str, Any] | None:
        from . import cashflow_worksheet as cw  # noqa: PLC0415
        try:
            return (self.cashflow_snapshot or cw.latest_snapshot)()
        except Exception as exc:  # noqa: BLE001
            log.warning("finance_close: forecast snapshot unavailable: %s", exc)
            return None

    def get_cashflow_snapshot_dates(self) -> list[datetime.date]:
        from . import cashflow_ledger as cl  # noqa: PLC0415
        try:
            return (self.cashflow_snapshot_dates or cl.list_snapshot_dates)()
        except Exception as exc:  # noqa: BLE001
            log.warning("finance_close: snapshot index unavailable: %s", exc)
            return []

    def load_cashflow_snapshot(self, day: datetime.date) -> dict[str, Any] | None:
        from . import cashflow_ledger as cl  # noqa: PLC0415
        try:
            return (self.cashflow_load_snapshot or cl.load_snapshot)(day)
        except Exception as exc:  # noqa: BLE001
            log.warning("finance_close: snapshot %s unreadable: %s", day, exc)
            return None

    def get_cashflow_finalized(self) -> dict[str, Any] | None:
        from . import cashflow_worksheet as cw  # noqa: PLC0415
        try:
            return (self.cashflow_finalized or cw.latest_finalized)()
        except Exception as exc:  # noqa: BLE001
            log.warning("finance_close: finalized actuals unavailable: %s", exc)
            return None

    def get_cashflow_preliminary(self, week_ending: str) -> dict[str, Any] | None:
        """The PRELIMINARY window for a NAMED week (the maturation leg)."""
        from . import cashflow_worksheet as cw  # noqa: PLC0415
        try:
            if self.cashflow_preliminary:
                return self.cashflow_preliminary(week_ending)
            return cw.preliminary_for(datetime.date.fromisoformat(week_ending))
        except Exception as exc:  # noqa: BLE001
            log.warning("finance_close: preliminary %s unreadable: %s",
                        week_ending, exc)
            return None

    def get_cashflow_newest_preliminary(self) -> dict[str, Any] | None:
        """The newest PRELIMINARY window (last week's actuals for the worksheet).

        Deliberately a DIFFERENT accessor from the one above: the worksheet
        shows W-1, the parallel section needs W-2's own preliminary to measure
        maturation. Serving one from the other would silently compare two
        different weeks.
        """
        from . import cashflow_worksheet as cw  # noqa: PLC0415
        try:
            return (self.cashflow_newest_preliminary or cw.newest_preliminary)()
        except Exception as exc:  # noqa: BLE001
            log.warning("finance_close: preliminary window unavailable: %s", exc)
            return None

    def get_cashflow_entity_map(self) -> Any:
        from . import cashflow_maps as cm  # noqa: PLC0415
        try:
            return (self.cashflow_entity_map or cm.load_entity_map)()
        except Exception as exc:  # noqa: BLE001
            # FAIL-CLOSED to an EMPTY map, never to None: an empty map has zero
            # confirmed pairs, so the debut gate stays shut and every
            # QBO-attributed leg stubs. Returning None would hand every caller
            # an AttributeError instead -- and a section that raises stubs with
            # "section builder failed", which reads as a code bug rather than as
            # the unreadable map it is.
            log.warning("finance_close: entity map unreadable: %s", exc)
            return cm.EntityMap()

    def get_bank_snapshot(self) -> dict[str, Any] | None:
        """Daily QBO bank snapshot, or None when it has not run.

        A FILE read, not a live QBO sweep -- same reasoning as get_adherence():
        the snapshot job runs on its own cadence earlier the same morning, and the
        pack must degrade to an honest stub rather than recompute 55 API calls
        inside the pack build.
        """
        if self.bank_snapshot:
            return self.bank_snapshot()
        from .qbo_bank_snapshot import load_snapshot  # noqa: PLC0415
        return load_snapshot()


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

    # PACK_EXCLUDED_ENTITIES must govern here too. build_pack hands this section
    # the UNFILTERED list so the footer can report the exclusion, but the
    # cross-check itself must not run on an excluded realm -- HR LLC was kept
    # out only by the accident of having no sheet mapping.
    checkable = [e for e in entities
                 if e in QBO_TO_SHEET_ENTITY and e not in PACK_EXCLUDED_ENTITIES]
    # An entity with no cash-sheet mapping must be NAMED, not filtered into
    # invisibility. Dropping it made the footer claim "2 of 2" while a third
    # provisioned realm was never cross-checked at all -- full coverage asserted
    # over a silently shrunk denominator.
    unmapped = [e for e in entities if e not in QBO_TO_SHEET_ENTITY]
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
        is_actual = False
        try:
            cash = sources.get_cash_closing(sheet_entity)
            closing = cash.get("closing")
            is_actual = bool(cash.get("is_actual"))
            week_label = cash.get("week_label") or ""
            stale = bool(cash.get("stale"))
            age_days = cash.get("age_days")
            if closing is not None:
                sheet_values_seen += 1
        except Exception as exc:  # noqa: BLE001 -- per-entity fail-soft
            sheet_read_failures += 1
            log.warning("finance_close: cash sheet read failed for %s: %s", sheet_entity, exc)

        as_of, exact = _week_close_as_of(week_label or "", today=today)
        bank = cards = None
        try:
            report = sources.get_balance_sheet(entity, as_of)
            bank = extract_bank_balance(report)
            cards = extract_credit_card_balance(report)
        except Exception as exc:  # noqa: BLE001 -- per-entity fail-soft
            log.warning("finance_close: balance sheet failed for %s: %s", entity, exc)

        # The sheet row is "Ending Cash/CC Book Balance" -- cash NET of cards -- so
        # the books leg must net the card liability out too. An absent Credit Cards
        # section means the entity carries none; that is reported, not assumed.
        cards_present = cards is not None
        books_net = None if bank is None else bank - (cards or 0.0)
        delta = None if (books_net is None or closing is None) else books_net - closing
        rows.append({
            "entity": entity,
            "sheet_closing": closing,
            "is_actual": is_actual,
            "bank": bank,
            "cards": cards,
            "cards_present": cards_present,
            "books_net": books_net,
            "delta": delta,
            "week_label": week_label,
            "as_of": as_of,
            "as_of_exact": exact,
            "stale": stale,
            "age_days": age_days,
        })

    # ── Roll-up rows become a CONSOLIDATION TIE-OUT (cq-6fbb9d717512) ────────
    #
    # First cut re-based the books leg to sum(member books) and compared THAT to
    # the consolidated sheet row. Algebraically that equals
    #   sum(member deltas) + (sum(member sheet closings) - sheet_consolidated)
    # -- so it re-reported the four member variances (already rendered below it)
    # as a fifth independent-looking flag. Live 2026-08-05: roll-up delta
    # -$8,026.16 vs sum-of-member-deltas -$8,025.16, i.e. 8 flags for 7 distinct
    # cash positions, and close-prep then read "8 entity(ies)". The pack forbids
    # exactly that double-count for restatements; the same rule applies here.
    #
    # Only the SECOND term is new information, so that is all this row reports:
    # does the consolidated sheet row tie to its own member rows? It touches no
    # books at all -- the realm has none -- which also removes the latent
    # period-mismatch of summing member balance sheets pulled at each member's
    # own as_of and comparing them to the consolidated tab's week.
    by_entity = {r["entity"]: r for r in rows}
    for row in rows:
        members = SHEET_ROLLUPS.get(QBO_TO_SHEET_ENTITY.get(row["entity"], ""))
        if not members:
            continue
        row["rollup_members"] = members
        row["is_rollup"] = True
        row["bank"] = row["cards"] = row["books_net"] = None
        row["cards_present"] = False
        member_closings = [
            by_entity[m]["sheet_closing"] for m in members
            if m in by_entity and by_entity[m]["sheet_closing"] is not None
        ]
        if len(member_closings) != len(members) or row["sheet_closing"] is None:
            row["rollup_incomplete"] = True
            row["delta"] = None
            continue
        row["member_sum"] = round(sum(member_closings), 2)
        row["delta"] = round(row["member_sum"] - row["sheet_closing"], 2)

    none_count = sum(1 for r in rows if r["sheet_closing"] is None) - sheet_read_failures

    # Label-drift detector (2026-06-04 doctrine). Closing balances coming back None
    # from reads that SUCCEEDED is the signature of a renamed row, not of an outage
    # -- and it is exactly the state that once rendered as a wall of '--' reading
    # like zeros. Degrade the whole section, and keep the drift diagnosis even when
    # a minority of reads also errored: one transient failure must not replace the
    # actionable "row labels renamed" message (the diagnosis the 2026-06-04 incident
    # cost a day to find) with a generic "unreadable".
    if sheet_values_seen == 0 and (
        sheet_read_failures == 0 or none_count >= max(2 * sheet_read_failures, 1)
    ):
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
    week_labels = {r["week_label"] for r in rows if r["week_label"]}
    if week_label:
        multi = (
            f" (NOTE: entities report {len(week_labels)} different weeks; each row is "
            "as-of its own tab's latest-actual week)"
            if len(week_labels) > 1 else ""
        )
        section.lines.append(f"Cash-sheet week: {week_label}{multi}")
    stale_rows = [r for r in rows if r["stale"]]
    if stale_rows:
        ages = [r["age_days"] for r in stale_rows if r["age_days"] is not None]
        age_note = f" (~{max(ages)}d old)" if ages else ""
        names = ", ".join(entity_label(r["entity"]) for r in stale_rows)
        section.lines.append(
            f":warning: cash sheet appears BEHIND for {len(stale_rows)} entity(ies)"
            f"{age_note} — {names}. Those figures are as-of that week, not today."
        )

    # Most-entities-None while a minority returned values: the section is usable but
    # drift is still the likely story, so say it on the AVAILABLE path too.
    if none_count >= max(2 * sheet_values_seen, 2):
        section.lines.append(
            f":warning: {none_count} of {len(checkable)} entities returned NO closing "
            "balance from the cash sheet — suspect row-label drift, not an outage."
        )

    complete = 0
    forecast_only = 0
    for row in rows:
        label = entity_label(row["entity"])

        # A consolidated row is a SHEET-INTERNAL tie-out, not a books comparison.
        # It gets its own rendering so it can never be read as a fifth cash
        # position, and it is excluded from `snap` so close-prep's "N entity(ies)
        # show a cash delta" count stays one-per-entity.
        if row.get("is_rollup"):
            member_names = ", ".join(entity_label(m) for m in row["rollup_members"])
            if row.get("rollup_incomplete"):
                section.lines.append(
                    f"• {label}: unavailable — the consolidated row could not be tied "
                    f"out (a member row or the consolidated total is missing)"
                )
                continue
            crosses = abs(row["delta"]) >= CASH_DELTA_ABS
            if crosses:
                section.flags += 1
            mark = ":triangular_flag_on_post: " if crosses else ""
            section.lines.append(
                f"• {mark}{label}: consolidation tie-out — sheet total "
                f"{fmt_money(row['sheet_closing'])} vs its own member rows summing "
                f"{fmt_money(row['member_sum'])} ({member_names}) — difference "
                f"{fmt_delta(row['delta'])}. The {label} realm holds no cash of its "
                f"own; each store's books comparison is its own row below."
            )
            continue

        if row["sheet_closing"] is None or row["books_net"] is None:
            missing = []
            if row["sheet_closing"] is None:
                missing.append("cash sheet")
            if row["books_net"] is None:
                missing.append(
                    "books for every consolidated member" if row.get("rollup_incomplete")
                    else "books"
                )
            section.lines.append(
                f"• {label}: unavailable — no figure from {' and '.join(missing)}"
            )
            continue
        complete += 1
        as_of_note = "" if row["as_of_exact"] else f" [books as of {row['as_of']}, week date unparsed]"
        if not row["cards_present"]:
            books_note = " (no credit-card section in the books)"
        elif row["cards"]:
            books_note = f" (cash {fmt_money(row['bank'])} less cards {fmt_money(row['cards'])})"
        else:
            books_note = ""

        if not row["is_actual"]:
            # The sheet has no ACTUAL for this week (or the week fell outside the
            # series). Forecast-vs-books is not a reconciliation signal in either
            # direction, so it is labelled and deliberately NOT flagged.
            forecast_only += 1
            section.lines.append(
                f"• {label}: sheet {fmt_money(row['sheet_closing'])} (FORECAST — no "
                f"actual for this week) vs books {fmt_money(row['books_net'])}"
                f"{books_note} — difference {fmt_delta(row['delta'])}, not a "
                f"reconciliation comparison{as_of_note}"
            )
        else:
            flagged = _crosses(
                row["delta"], row["sheet_closing"], CASH_DELTA_ABS, CASH_DELTA_PCT,
            )
            mark = ":triangular_flag_on_post: " if flagged else ""
            if flagged:
                section.flags += 1
            section.lines.append(
                f"• {mark}{label}: sheet {fmt_money(row['sheet_closing'])} (actual) vs "
                f"books {fmt_money(row['books_net'])}{books_note} — delta "
                f"{fmt_delta(row['delta'])}{as_of_note}"
            )
        snap[row["entity"]] = {
            "sheet_closing": row["sheet_closing"],
            "bank": row["bank"],
            "cards": row["cards"],
            "books_net": row["books_net"],
            "delta": row["delta"],
            "is_actual": row["is_actual"],
        }

    for entity in unmapped:
        if entity in PACK_EXCLUDED_ENTITIES:
            continue
        section.lines.append(
            f"• {entity_label(entity)}: NOT cross-checked — no cash-sheet mapping for "
            f"this accounting entity"
        )

    excluded = [e for e in entities if e in PACK_EXCLUDED_ENTITIES]
    # Denominator spans every entity offered, so an unmapped realm cannot shrink it.
    section.expected = len([e for e in entities if e not in PACK_EXCLUDED_ENTITIES])
    section.covered = complete
    footer = (
        f"_Cross-checked {complete} of {section.expected} entity(ies)"
        + (f"; {forecast_only} compared against a FORECAST week" if forecast_only else "")
        + "."
    )
    if excluded:
        footer += (
            " Excluded: "
            + ", ".join(f"{entity_label(e)} ({PACK_EXCLUDED_ENTITIES[e]})" for e in excluded)
            + "."
        )
    section.lines.append(footer + "_")
    return section, snap


# ─────────────────────────────────────────────────────────────────────────────
# Section 1b — QBO bank & books freshness (A5 S2)
# ─────────────────────────────────────────────────────────────────────────────

# Method-difference disclosure. NOT a "tolerance": the design anticipated naming an
# expected tolerance between this section and the cash cross-check above, and the
# 2026-08-04 live probe REFUTED that framing. The two surfaces disagreed by more
# than 100% and with OPPOSITE SIGNS on the same account at the same instant (BDM
# "Big D Media Chase": register +11,758.94 vs report -8,483.22; HJRP bank register
# 128,128.02 vs report 26,879.52). Not clock skew and not future-dated activity --
# the BDM report figure is identical at as-of dates through 2030. They are
# different measures, so no tolerance band would be honest.
_BANK_METHOD_FOOTER = (
    "_Balances above are ACCOUNT REGISTER figures read from the QBO Account API. "
    "The cash cross-check section reads the BalanceSheet REPORT instead — a "
    "different endpoint and a different measure, which can differ by large amounts "
    "and even in sign on the same account. A gap between the two sections is NOT a "
    "reconciliation break and is deliberately not flagged._"
)


def build_bank_section(
    entities: list[str],
    sources: Sources,
    *,
    today: datetime.date | None = None,
) -> tuple[Section, dict[str, Any]]:
    """QBO bank balances + books-freshness. Returns (section, snapshot fragment)."""
    from .qbo_bank_snapshot import (  # noqa: PLC0415
        BALANCE_BASIS, DEFAULT_MAX_AGE_HOURS, snapshot_age_hours, stale_txn_days, txn_age_days,
    )

    section = Section(key="qbo_bank", title=":bank: QBO bank & books freshness")
    snap: dict[str, Any] = {}
    day = today or _today()

    data = sources.get_bank_snapshot()
    if not data:
        section.available = False
        section.stub_reason = (
            "the daily QBO bank snapshot has not run yet "
            "(cowork-cora-bank-snapshot, daily 07:05 AZ)"
        )
        return section, snap

    # Staleness is reported, never silently tolerated: a snapshot from three days
    # ago presented as "current cash" is the exact failure this label prevents.
    age_h = snapshot_age_hours(data)
    if age_h is None:
        section.lines.append(
            ":warning: snapshot carries no usable timestamp — treat these figures as "
            "of UNKNOWN age, not as current."
        )
    elif age_h < 0:
        # A future stamp is a broken clock or a hand-edited file, not freshness.
        # Without this it passes the `> MAX_AGE` test and renders as current with
        # a nonsense "~-128h ago".
        section.lines.append(
            f":warning: snapshot is stamped in the FUTURE "
            f"({data.get('generated_at_utc')}) — its age cannot be trusted; treat "
            f"these figures as of UNKNOWN age."
        )
    elif age_h > DEFAULT_MAX_AGE_HOURS:
        section.lines.append(
            f":warning: snapshot is ~{age_h:.0f}h old (generated "
            f"{data.get('generated_at_utc')}) — figures are as of then, not today."
        )
    else:
        section.lines.append(
            f"Snapshot generated {data.get('generated_at_utc')} (~{age_h:.0f}h ago); "
            f"basis: {data.get('basis') or BALANCE_BASIS}."
        )

    realms = data.get("realms") or {}
    threshold = stale_txn_days()

    # The snapshot spans EVERY provisioned realm, including pack-excluded ones. The
    # pack's exclusions are about sensitivity, so they must hold here too -- and
    # that is also why the snapshot's own portfolio total is NOT reused below: it
    # includes HR LLC, whose balance must not reach a multi-member finance channel.
    renderable = [e for e in entities if e not in PACK_EXCLUDED_ENTITIES]
    covered = 0
    totals_usable = True
    contributing: list[str] = []
    shell_realms: list[str] = []

    for entity in renderable:
        label = entity_label(entity)
        block = realms.get(entity)
        if not block:
            totals_usable = False
            section.lines.append(f"• {label}: unavailable — not present in the snapshot")
            continue
        if block.get("status") != "ok":
            totals_usable = False
            # D-118: the error string originates outside this module (QBO / httpx),
            # so it is scrubbed before it reaches a Slack surface.
            reason = _scrub_external(str(block.get("error") or "read failed"), 100)
            section.lines.append(f"• {label}: unavailable — {reason}")
            continue

        if block.get("shell"):
            # A shell realm is a footnote, not a balance row (design 3 S1). It is
            # also removed from the DENOMINATOR: a realm with no bank accounts is
            # not something we failed to read, and counting it as a permanent
            # coverage gap would make the section report itself partial EVERY week
            # -- which trains the reader to ignore the one signal that separates a
            # normal week from a realm that actually 401'd.
            shell_realms.append(entity)
            section.lines.append(
                f"• {label}: cash-less holding shell — no bank accounts to report."
            )
            continue

        bank = block.get("bank_total")
        cards = block.get("cc_total")
        net = block.get("cash_net_of_cards")
        newest = block.get("newest_bank_txn_date")
        age_d = txn_age_days(newest, today=day)

        # A realm whose balances are INCOMPLETE was not fully read, so it must not
        # count toward coverage -- otherwise is_partial stays False and the whole
        # section vanishes from the founder cut (which is a flag/gap filter).
        bank_unknown = int(block.get("bank_unknown") or 0)
        cc_unknown = int(block.get("cc_unknown") or 0)
        complete = bool(block.get("balances_complete", True))
        if complete:
            covered += 1
            contributing.append(entity)
        else:
            totals_usable = False

        flagged = age_d is not None and age_d > threshold
        if flagged:
            section.flags += 1
        mark = ":triangular_flag_on_post: " if flagged else ""

        if newest is None:
            fresh_txt = "newest posted bank-side txn: UNKNOWN"
        else:
            fresh_txt = f"newest posted bank-side txn {newest} ({age_d}d ago)"
            if flagged:
                fresh_txt += f" — over the {threshold}d threshold"

        # The freshness date is a max() over several transaction surfaces. If some
        # of those queries failed, the date is a floor, not the answer -- and a
        # realm can be flagged stale purely because the surfaces that would have
        # disproved it were never read.
        f_cov = block.get("freshness_types_covered")
        f_exp = block.get("freshness_types_expected")
        if isinstance(f_cov, int) and isinstance(f_exp, int) and f_cov < f_exp:
            fresh_txt += (
                f" [only {f_cov} of {f_exp} txn surfaces read — this date is a "
                f"floor, not a confirmed latest]"
            )

        # Accounts whose balance QBO did not return are EXCLUDED from the totals
        # above, so the figure is short by however many. Say so on the row itself;
        # a portfolio-level withholding line never names the realm.
        gap_txt = ""
        if not complete:
            missing = bank_unknown + cc_unknown
            gap_txt = (
                f" — INCOMPLETE: {missing} account(s) returned no balance, so this "
                f"row understates the realm"
            )

        cards_txt = "" if not cards else f", cards {fmt_money(cards)}"
        section.lines.append(
            f"• {mark}{label}: bank {fmt_money(bank)}{cards_txt}, net of cards "
            f"{fmt_money(net)} — {fresh_txt}{gap_txt}"
        )
        snap[entity] = {
            "bank_total": bank, "cc_total": cards, "cash_net_of_cards": net,
            "newest_bank_txn_date": newest, "txn_age_days": age_d,
            "balances_complete": complete,
        }

    # Section total over exactly the rows rendered above -- never the snapshot's
    # own portfolio figure, which spans pack-excluded realms.
    if contributing and totals_usable:
        section.lines.append(
            f"Total across {len(contributing)} realm(s): net of cards "
            + fmt_money(round(sum(realms[e]["cash_net_of_cards"] for e in contributing), 2))
        )
    elif contributing:
        # Phrased with "unavailable —" deliberately: build_founder_cut collects
        # coverage lines by that literal substring, so any other wording makes the
        # whole section invisible in the cut Harrison reads.
        section.lines.append(
            "Total unavailable — at least one realm could not be read or carries "
            "an unknown balance, so a sum would understate the portfolio."
        )

    section.covered = covered
    section.expected = len(renderable) - len(shell_realms)
    section.lines.append(
        f"_Read {covered} of {section.expected} realm(s) from the snapshot._"
    )
    section.lines.append(_BANK_METHOD_FOOTER)
    return section, snap


# ─────────────────────────────────────────────────────────────────────────────
# Section 1c — Forecast assist (A5 S2b; SUPERSEDED IN PLACE at 13WCF M3)
# ─────────────────────────────────────────────────────────────────────────────
#
# A worksheet Justin types FROM while doing his Monday refresh. Cora never writes
# the Standing ACTUALS sheet -- stewardship stays his (SOP rev 4; the 2026-06-04
# row-label fragility doctrine; D-011).
#
# WHAT M3 CHANGED, AND WHY IT IS A SUPERSESSION RATHER THAN AN ADDITION
# ---------------------------------------------------------------------
# The accuracy leg used to read the SHEET's own dual forecast/actual series and
# then say -- correctly -- that a variance computed from it is meaningless,
# because the sheet overwrites its forecast column at week close (D-121: live
# 2026-08-04, 41 of 42 completed weeks matched to the dollar; re-verified
# 2026-08-17, DIFF read 0.00 on every closed week but one). Behind that honest
# statement sat a "pack-history fallback" that quietly reported week-over-week
# movement in the pack's OWN prior snapshot as an accuracy source.
#
# M1 built the store that actually answers the question, so as of M3:
#   * the S1 forecast-snapshot store is the SOLE forecast baseline;
#   * the sheet-dual accuracy leg is GONE (not demoted -- gone);
#   * the pack-history fallback is DELETED, not kept as a quiet second source.
# Two systems computing "forecast accuracy" from different bases in one pack is
# the Mig-1 failure this supersession exists to prevent, and a fallback that
# only fires when the primary is missing is the hardest kind to notice.
#
# The durable WORKSHEET FILE moved with it: `cashflow-ledger/worksheets/` is the
# one path (see cashflow_worksheet.render_worksheet). `render_forecast_worksheet`
# is retained ONLY to render this section into that file's predecessor shape for
# any caller still holding a Section; it writes nothing itself, and nothing in
# production calls it -- verified 2026-08-18, its only callers were tests.

#: Where the retired lane pointed. Kept as a POINTER, so the supersession is
#: discoverable from the old name instead of the name simply disappearing.
FORECAST_ASSIST_RELDIR = "01-HJR-Global/accounting/forecast-assist"

#: Where the Monday worksheet lives now.
CASHFLOW_WORKSHEET_RELDIR = "01-HJR-Global/accounting/cashflow-ledger/worksheets"

#: A weekly snapshot older than this means the Monday S1 job is not firing.
#: Eight days, not seven: a Monday-to-Monday cadence is exactly 7, so 7 would
#: warn on a healthy week whose pack ran a few hours after the snapshot.
_SNAPSHOT_MAX_AGE_DAYS = 8

#: How many accuracy rows the PACK renders inline. The full table lives in the
#: worksheet; a 19-tab list inside a Slack section pushes every later section
#: past the point anyone reads.
_ACCURACY_MAX_ROWS = 5

_SUPERSESSION_NOTE = (
    "_Forecast baseline: the S1 snapshot store (`cashflow-ledger/forecast-snapshots/`) "
    "is the ONLY source — the sheet's own forecast column is overwritten at week "
    "close, and this pack no longer keeps a history fallback behind that. The "
    "Monday worksheet moved to `cashflow-ledger/worksheets/`._"
)


def build_forecast_assist_section(
    entities: list[str],
    sources: Sources,
    *,
    cash_fragment: dict[str, Any] | None = None,
    today: datetime.date | None = None,
) -> tuple[Section, dict[str, Any]]:
    """Next-week carry-in references + forecast accuracy from banked forecasts.

    ``prior`` is deliberately GONE from the signature. It carried the pack-history
    fallback, and leaving it as an ignored parameter would let a caller keep
    passing a second forecast source that silently does nothing.
    """
    from . import cashflow_worksheet as cw  # noqa: PLC0415
    from .qbo_bank_snapshot import BALANCE_BASIS  # noqa: PLC0415

    day = today or _today()
    section = Section(key="forecast_assist", title=":chart_with_upwards_trend: Forecast assist")
    snap: dict[str, Any] = {}

    snapshot = sources.get_cashflow_snapshot()
    if not snapshot:
        # NO FALLBACK. The sheet cannot answer this question and the pack's own
        # history is not a forecast record; an unavailable store is unavailable.
        section.available = False
        section.stub_reason = (
            "no forecast snapshot has been banked yet — the Monday 06:15 S1 job "
            "is the only forecast baseline and there is deliberately no fallback"
        )
        return section, snap

    # ── accuracy, from VERIFIED pre-close snapshots only ─────────────────────
    # Derived roll-ups are excluded from accuracy: measuring "OSN Consolidated"
    # beside its four member tabs restates one variance as five.
    entity_map = sources.get_cashflow_entity_map()
    accuracy_week, rows, pending = cw.resolve_accuracy(
        latest=snapshot,
        load_snapshot=sources.load_cashflow_snapshot,
        snapshot_dates=sources.get_cashflow_snapshot_dates(),
        derived_tabs=getattr(entity_map, "derived_tabs", None),
    )

    # STALENESS. Every sibling leg in this pack warns when its source ages --
    # the carry-in on the bank snapshot, the parallel section on a lagging
    # finalized window. The accuracy leg had none, so a broken Monday S1 job
    # would keep re-rendering the SAME measurable week from the same frozen
    # snapshot, week after week, reading as a normal healthy section on the
    # three finance surfaces while the only signal sat on a different one.
    snap_date = str(snapshot.get("snapshot_date") or "")
    try:
        snap_age = (day - datetime.date.fromisoformat(snap_date)).days
    except ValueError:
        snap_age = None
    if snap_age is None or snap_age > _SNAPSHOT_MAX_AGE_DAYS:
        section.lines.append(
            ":warning: the forecast snapshot behind this section is "
            + (f"{snap_age} days old ({_scrub_external(snap_date, 12)})"
               if snap_age is not None else "of UNKNOWN age")
            + " — the Monday 06:15 job may not be firing, and these figures are "
            "not this week's."
        )
        section.flags += 1

    if not accuracy_week:
        section.lines.append(
            "Forecast accuracy: no completed week is present in the snapshot yet."
        )
    elif rows:
        worst = max(rows, key=lambda r: abs(r.variance))
        section.lines.append(
            f"Forecast accuracy, week ending {_scrub_external(accuracy_week, 12)} "
            f"— {len(rows)} tab(s) measured against a forecast banked before the "
            f"week closed. Largest miss: {_scrub_external(worst.tab, 40)} forecast "
            f"{fmt_money(worst.forecast)} vs actual {fmt_money(worst.actual)}, "
            f"variance {fmt_delta(worst.variance)} ({worst.horizon_days}-day "
            f"horizon; snapshot {_scrub_external(snap_date, 12)})."
        )
        for row in sorted(rows, key=lambda r: abs(r.variance), reverse=True)[:_ACCURACY_MAX_ROWS]:
            section.lines.append(
                f"• {_scrub_external(row.tab, 40)}: forecast {fmt_money(row.forecast)} "
                # ASCII arrow ON PURPOSE. The pack renders to a cp1252 Windows
                # console, which has no U+2192 -- and --dry-run is the only
                # pre-flight gate before three finance surfaces, so it must not
                # be the thing that breaks (the D-119 lesson, re-learned here).
                f"-> actual {fmt_money(row.actual)}, variance {fmt_delta(row.variance)}"
            )
        if len(rows) > _ACCURACY_MAX_ROWS:
            section.lines.append(
                f"• _…and {len(rows) - _ACCURACY_MAX_ROWS} more — full table in the "
                f"Monday worksheet._"
            )
        snap["accuracy"] = {
            "week_ending": accuracy_week,
            "measured": len(rows),
            "pending": len(pending),
            "worst": worst.as_dict(),
        }
    else:
        # NOT "0 weeks" and NOT 100%: name which of the two reasons applies.
        section.lines.append(
            f"Forecast accuracy: NOT COMPUTABLE for week ending "
            f"{_scrub_external(accuracy_week, 12)} — no tab has both a closed "
            f"actual and a verified pre-close forecast. First runs read this way "
            f"until a full week has passed since the first snapshot."
        )

    # ── next-week carry-in references ────────────────────────────────────────
    next_week = cw.next_forecast_week(snapshot, today=day)
    bank = sources.get_bank_snapshot()

    # The bank section warns about a stale snapshot under its OWN heading; this
    # section asserts figures are "on hand now" under a different one, and
    # render_forecast_worksheet emits ONLY these lines -- so the warning has to
    # travel with them.
    from .qbo_bank_snapshot import DEFAULT_MAX_AGE_HOURS, snapshot_age_hours  # noqa: PLC0415
    age_h = snapshot_age_hours(bank) if bank else None
    if bank and (age_h is None or age_h < 0 or age_h > DEFAULT_MAX_AGE_HOURS):
        section.lines.append(
            ":warning: the balances below are from a snapshot of UNKNOWN or stale age"
            + (f" (~{age_h:.0f}h old)" if age_h and age_h > 0 else "")
            + " — they are NOT 'as of now'."
        )

    renderable = [e for e in entities if e not in PACK_EXCLUDED_ENTITIES]
    carry = cw.build_carry_in(
        renderable, bank_snapshot=bank, book_balances=cash_fragment)
    shell_realms = [r.entity for r in carry if r.status == "shell"]
    covered = 0

    if next_week is None:
        section.lines.append(
            "Next-week carry-in: the snapshot carries no forward forecast week."
        )
    else:
        label = _scrub_external(next_week, 12)
        portfolio = cw.portfolio_carry_in(snapshot, next_week)
        section.lines.append(
            f"Carry-in references — for the week ending {label}"
            + (f" (CF_SUMMARY forecast carry-in {fmt_money(portfolio)} "
               f"portfolio-wide — the BEGINNING-cash row, not the week's close)"
               if portfolio is not None else
               " (CF_SUMMARY portfolio carry-in UNKNOWN)")
            + ". " + cw.CARRY_IN_POSTURE
        )
        for row in carry:
            if row.status == "shell":
                # A cash-less shell has no carry-in, so it leaves the DENOMINATOR
                # rather than counting as a coverage gap -- a permanent
                # false-partial trains the reader to ignore the real ones.
                continue
            if row.status == "shell_holding":
                # But a shell that is HOLDING money is a flag, not a footnote.
                section.lines.append(
                    f":warning: {entity_label(row.entity)}: configured as a "
                    f"cash-less shell but holding "
                    f"{fmt_money(row.shell_balance)} — portfolio totals are "
                    f"withheld while this is true."
                )
                section.flags += 1
                continue
            if row.status == "ok" and not row.balances_complete:
                section.lines.append(
                    f":warning: {entity_label(row.entity)}: unavailable — QBO "
                    f"returned no balance for {row.unknown_accounts} account(s), "
                    f"so its totals understate the realm; do not carry it in."
                )
                section.flags += 1
                continue
            if row.status != "ok":
                # "unavailable —" is the literal substring build_founder_cut
                # collects; any other wording makes this invisible in the cut.
                section.lines.append(
                    f"• {entity_label(row.entity)}: unavailable — {row.reason}"
                )
                continue
            covered += 1
            books = (f"books {fmt_money(row.book_total)} [BalanceSheet report — the "
                     f"basis the opening row uses]"
                     if row.book_total is not None else
                     "books UNKNOWN this run")
            section.lines.append(
                f"• {entity_label(row.entity)}: register {fmt_money(row.register_total)} "
                f"[{BALANCE_BASIS}], {books}; posted through "
                f"{row.posted_through or 'UNKNOWN'} (realm-level). The two are "
                f"different measures — do NOT substitute one for the other."
            )
            snap[row.entity] = {
                "register_reference": row.register_total,
                "book_reference": row.book_total,
                "week_ending": next_week,
            }

    section.covered = covered
    section.expected = len(renderable) - len(shell_realms)
    section.lines.append(
        f"_Carry-in references for {covered} of {section.expected} entity(ies). "
        f"Cora never writes the cash sheet — this is a worksheet to type from._"
    )
    section.lines.append(f"_{cw.CARRY_IN_PROPOSAL}_")
    section.lines.append(_SUPERSESSION_NOTE)
    return section, snap


def render_forecast_worksheet(section: Section, today: datetime.date | None = None) -> str:
    """The predecessor worksheet shape. Retained for callers holding a Section.

    The DURABLE Monday worksheet is `cashflow_worksheet.render_worksheet`, which
    writes to `cashflow-ledger/worksheets/`. Nothing in production calls this.
    """
    day = today or _today()
    return "\n".join([
        f"# Forecast assist — {day.isoformat()}",
        "",
        "_Generated by Cora. Deterministic; every figure is a direct source read._",
        "_Cora does NOT write the Standing ACTUALS sheet — type from this._",
        f"_SUPERSEDED: the Monday worksheet is now {CASHFLOW_WORKSHEET_RELDIR}/._",
        "",
        *(section.lines if section.available
          else [f"Section unavailable — {section.stub_reason}"]),
        "",
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Section 1c-2 — Parallel run: sheet actuals vs QBO actuals (13WCF M3 / S4)
# ─────────────────────────────────────────────────────────────────────────────
#
# WHAT IT COMPARES, AND WHAT IT REFUSES TO CLAIM
# -----------------------------------------------
# One FINALIZED QBO week against the sheet's entered actual for that same week.
# Never the just-ended week: the sheet's column does not exist until Monday
# evening and QBO is structurally incomplete at 06:25 (Fin-1/Fin-2), so the
# comparison lags one matured week.
#
# THE DELTA IS "UNATTRIBUTED", NOT "UNEXPLAINED", AND THAT WORD IS THE FINDING.
# The sheet's rows are Cash/CC; M2's perimeter is bank accounts only, where a
# card PAYMENT is the cash event and card PURCHASES are not (D-120/Fin-3). Any
# week with card spending diverges by construction, and nothing available in v1
# splits that component out. Measured on the live 2026-08-07 finalized window
# against the 2026-08-17 snapshot, every single covered realm was four or five
# figures away from a gate of max($100, 0.5%): BDM +$16,065, F3E +$20,675,
# HJRP +$19,834, OSNGW -$6,118, OSNVV -$1,293.
#
# So the flip gate is reported as BLOCKED with that stated as its reason,
# instead of starting a four-week clock on a number no amount of clean
# bookkeeping can bring inside tolerance. Calling an undecomposable delta
# "unexplained" would have manufactured a permanent alarm; calling the two
# figures simply non-comparable and moving on would leave nothing watching
# either (the D-129 companion trap). The section does the third thing: it
# publishes the delta, names what it cannot separate, and points at the one
# component it CAN measure.
#
# THAT COMPONENT IS MATURATION: finalized net minus the SAME week's preliminary
# net is what posted into QBO after the first pull -- the timing half, measured
# rather than asserted. Live on 2026-08-07 it ran from +$586 (OSNGW) to
# -$79,054 (HJRP), which is exactly why the design mandated two windows.

_PARALLEL_METHOD_FOOTER = (
    "_Method: this section is FLOW-grain (a week's net cash movement) read at a "
    "different instant from the cash section's BALANCE-grain sheet-vs-books "
    "cross-check above. They answer different questions and are never "
    "cross-flagged — a difference here is not evidence about that one._"
)


def build_cashflow_parallel_section(
    sources: Sources,
    *,
    today: datetime.date | None = None,
) -> tuple[Section, dict[str, Any]]:
    """Sheet-entered actuals vs QBO finalized actuals for one matured week."""
    from . import cashflow_worksheet as cw  # noqa: PLC0415

    day = today or _today()
    section = Section(
        key="cashflow_parallel",
        title=":scales: Parallel run — sheet vs QBO actuals",
    )
    frag: dict[str, Any] = {}

    entity_map = sources.get_cashflow_entity_map()
    gate = cw.debut_gate(entity_map)
    snapshot = sources.get_cashflow_snapshot()
    finalized = sources.get_cashflow_finalized()
    prelim = None
    if finalized and finalized.get("week_ending"):
        prelim = sources.get_cashflow_preliminary(str(finalized["week_ending"]))

    parallel = cw.build_parallel(
        snapshot=snapshot, finalized=finalized, preliminary=prelim,
        entity_map=entity_map, gate=gate, today=day,
    )

    if not parallel.available:
        section.available = False
        section.stub_reason = _scrub_external(parallel.reason, 300)
        return section, frag

    week = _scrub_external(parallel.week_ending or "unknown", 12)
    section.lines.append(
        f"Comparing week ending *{week}* — the sheet's entered actual against "
        f"QBO's FINALIZED re-pull. The just-ended week is never compared: its "
        f"sheet column does not exist yet and its QBO side is not mature."
    )
    if parallel.stale_window:
        # A monitor that watches only the newest artifact cannot see a hole
        # behind it (D-130(d)). Say the window is behind rather than presenting
        # a stale week as this week's comparison.
        section.lines.append(
            f":warning: the newest finalized window is week {week}, but this "
            f"Monday should have produced "
            f"{_scrub_external(parallel.expected_week_ending or 'a later week', 12)} "
            f"— the actuals job appears to have missed a run."
        )
        section.flags += 1

    section.lines.append(
        "_Bases differ by construction: the sheet's rows are Cash/CC, QBO's "
        "perimeter is bank accounts only (a card PAYMENT is the cash event; card "
        "PURCHASES are not). v1 cannot split that component out, so deltas below "
        "are UNATTRIBUTED — not 'unexplained', and not evidence of a bookkeeping "
        "error on their own._"
    )

    for row in parallel.rows:
        label = entity_label(row.realm)
        tab = _scrub_external(row.tab or "unpaired", 40)
        if row.status == "unavailable":
            section.lines.append(
                f"• {label}: unavailable — {_scrub_external(row.reason, 80)}"
            )
            continue
        if row.status == "sheet_unfilled":
            section.lines.append(
                f"• {label} ({tab}): NOT COMPARED — {_scrub_external(row.reason, 140)}. "
                f"QBO reads {fmt_money(row.qbo_net)} for the week. This is a "
                f"data-entry gap, not a reconciliation break."
            )
            continue
        if row.status == "pending_sheet":
            section.lines.append(
                f"• {label} ({tab}): sheet actual not entered for this week yet — "
                f"QBO reads {fmt_money(row.qbo_net)}. Not a zero and not a break."
            )
            continue

        # NOT "N of it". maturation = final_net - prelim_net, i.e. how much the
        # QBO figure MOVED between the two pulls -- it can exceed the delta and
        # carry the opposite sign (live: HJRP delta +$19,834 with maturation
        # -$79,054). Calling it a component of the current gap is a wrong
        # attribution on the one line the section exists to make attributable.
        maturation = (
            f"; QBO's own figure moved {fmt_delta(row.maturation)} between the "
            f"preliminary and finalized pulls"
            if row.maturation is not None else
            "; no preliminary window for this week, so the timing component is "
            "UNKNOWN"
        )
        section.lines.append(
            f"• {label} ({tab}): sheet {fmt_money(row.sheet_net)} vs QBO "
            f"{fmt_money(row.qbo_net)} — delta {fmt_delta(row.delta)}{maturation}."
        )
        if row.qbo_ending is not None and row.sheet_ending is not None:
            # Renders only when QBO produced a COMPLETE balance. It usually does
            # not: the General Ledger omits accounts with no activity, so M2
            # withholds the figure rather than publish a partial as a total
            # (D-129). Live to date this line has never rendered.
            section.lines.append(
                f"    ending cash: sheet {fmt_money(row.sheet_ending)} vs QBO "
                f"{fmt_money(row.qbo_ending)} "
                f"[QBO balance complete for every bank account]"
            )

    out = parallel.out_of_tolerance
    section.covered = parallel.covered
    section.expected = parallel.expected

    # ── flip gate ────────────────────────────────────────────────────────────
    # Reported as BLOCKED rather than as a streak count. A gate whose metric
    # cannot reach its threshold for a STRUCTURAL reason must say so; rendering
    # "0 of 4 weeks" would read as progress toward something achievable.
    section.lines.append(
        f"_Flip gate: BLOCKED, not failing. It needs "
        f"{cw.FLIP_GATE_WEEKS} consecutive weeks with every covered entity's "
        f"delta inside max(${cw.FLIP_GATE_ABS:,.0f}, "
        f"{cw.FLIP_GATE_PCT * 100:.1f}%), but the Cash/CC-vs-bank-cash basis "
        f"difference above is not separable in v1, so the metric cannot reach "
        f"that threshold however clean the books are. This week "
        f"{len(out)} of {parallel.covered} compared entity(ies) sit outside it. "
        f"Unblocking it needs card-side flows (the BILL/Divvy pull, "
        f"`cq-f3bfa4e9ca5b`) or a Cash/CC-basis QBO perimeter — a decision, not "
        f"a fix._"
    )
    if parallel.window_partial:
        section.lines.append(
            f":warning: the finalized window this reads was written by a SCOPED "
            f"run — it states {parallel.window_expected} realm(s) expected, so "
            f"the coverage below is against a partial file."
        )
        section.flags += 1
    section.lines.append(
        f"_Compared {parallel.covered} of {parallel.expected} entity(ies); "
        f"snapshot {_scrub_external(parallel.snapshot_date or 'unknown', 12)}, "
        f"window run {_scrub_external(parallel.window_run_date or 'unknown', 12)}._"
    )
    section.lines.append(_PARALLEL_METHOD_FOOTER)

    frag = {
        "week_ending": parallel.week_ending,
        "covered": parallel.covered,
        "expected": parallel.expected,
        "out_of_tolerance": [r.realm for r in out],
        "pending_sheet": [r.realm for r in parallel.rows
                          if r.status == "pending_sheet"],
        "flip_gate": "blocked",
        "stale_window": parallel.stale_window,
    }
    return section, frag

# ─────────────────────────────────────────────────────────────────────────────
# Section 1d — Intercompany discovery / check (A5 S3, absorbed L2)
# ─────────────────────────────────────────────────────────────────────────────
#
# THE LOAD-BEARING CORRECTION. The existing _named_section_total / _summary_cells
# read Section-SUMMARY rows only. A scan for intercompany ACCOUNTS built on them
# returns zero candidates on every realm -- structurally blind, and indistinguishable
# from an honest all-clear, which no coverage counter would catch (it would report
# "scanned 10 of 10 realms, found nothing"). So this adds a real Data-row walker,
# and its test fixture uses a realistic nested Section->Data shape.

INTERCOMPANY_MAP_PATH = _REPO_ROOT / "data" / "maps" / "qbo-intercompany-accounts.yaml"

#: Patterns that mark an account as intercompany-ish. Deliberately broad at
#: discovery time -- a false positive costs Justin one glance, a false negative
#: hides a real imbalance.
_INTERCOMPANY_PATTERNS = ("intercompany", "inter-company", "due to", "due from", "i/c")

#: Realms whose ACCOUNT NAMES must never free-render on a finance surface.
#: is_any_phi cannot catch a bare person name in an account title ("Due from Jane
#: Smith" trips none of its predicates), and #hjrg-finance / #founder-finance /
#: Justin's DM are not LEX-custodian surfaces.
#: Matched by PREFIX after normalizing, not by exact string. An exact allow-list
#: was the wrong shape for a guard whose whole premise is that a human cannot spot
#: a bare person name in an account title: a future LEX-LLC / LEXLLC realm (HRLLC
#: already proves per-sub-entity realms get provisioned), or an operator passing
#: `--entities lex`, would have fallen straight through to free rendering.
_NAME_OPAQUE_REALMS: frozenset[str] = frozenset({"LEX"})


def is_name_opaque_realm(entity: str) -> bool:
    """True if this realm's ACCOUNT NAMES must never render on a finance surface."""
    code = re.sub(r"[^A-Z]", "", str(entity or "").upper())
    return any(code.startswith(prefix) for prefix in _NAME_OPAQUE_REALMS)

INTERCOMPANY_DELTA_ABS = 500.0


def intercompany_delta_threshold() -> float:
    raw = os.environ.get("FINANCE_INTERCOMPANY_DELTA_ABS", "").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return INTERCOMPANY_DELTA_ABS
    return value if value > 0 else INTERCOMPANY_DELTA_ABS


def iter_account_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Every DATA row in a QBO report, as ``{"name", "balance", "id"}``.

    QBO renders a BalanceSheet as nested Sections; individual GL accounts are
    ``type == "Data"`` leaves whose first ColData cell is the account name and
    whose LAST money-bearing cell is the balance. Confirmed against live F3E and
    BDM BalanceSheets 2026-08-04, including accounts nested one level under a
    parent account's own sub-Section.
    """
    from .tools.qbo_client import _parse_money  # noqa: PLC0415

    out: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "Data":
            cells = node.get("ColData") or []
            if cells:
                name = str((cells[0] or {}).get("value") or "").strip()
                balance = None
                for cell in reversed(cells[1:]):
                    balance = _parse_money(str((cell or {}).get("value") or ""))
                    if balance is not None:
                        break
                if name:
                    out.append({
                        "name": name,
                        "balance": balance,
                        "id": str((cells[0] or {}).get("id") or ""),
                    })
        # Sections carry both Header/Summary and nested Rows; recurse regardless
        # of type so a Data row under a parent-account sub-Section is not missed.
        walk((node.get("Rows") or {}).get("Row") or [])

    walk((report.get("Rows") or {}).get("Row") or [])
    return out


def is_intercompany_account(name: str) -> bool:
    lowered = (name or "").lower()
    return any(p in lowered for p in _INTERCOMPANY_PATTERNS)


def load_intercompany_map(path: Path | None = None) -> dict[str, Any]:
    """Confirmed intercompany pairs. FAIL-SOFT: unreadable means discovery-only."""
    import yaml  # noqa: PLC0415

    target = path or INTERCOMPANY_MAP_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("finance_close: intercompany map unreadable (%s)", exc)
        return {"pairs": []}
    if not isinstance(raw, dict):
        return {"pairs": []}
    pairs = raw.get("pairs")
    return {"pairs": [p for p in pairs if isinstance(p, dict)] if isinstance(pairs, list) else []}


def _candidate_label(entity: str, index: int, name: str) -> str:
    """How a discovered account is allowed to appear on a finance surface.

    LEX realm names go out as opaque placeholders; the real names reach Harrison's
    DM only (the script's separate delivery), and enter the YAML seed as ids plus
    Harrison-approved display labels.
    """
    if is_name_opaque_realm(entity):
        return f"{entity_label(entity)} candidate account #{index}"
    return _scrub_external(name, 60)


def build_intercompany_section(
    entities: list[str],
    sources: Sources,
    *,
    today: datetime.date | None = None,
) -> tuple[Section, dict[str, Any]]:
    """Discovery-first intercompany scan. Never blocks on Justin's confirmations."""
    section = Section(key="intercompany", title=":left_right_arrow: Intercompany")
    snap: dict[str, Any] = {}
    day = today or _today()
    as_of = day.isoformat()

    renderable = [e for e in entities if e not in PACK_EXCLUDED_ENTITIES]
    found: dict[str, list[dict[str, Any]]] = {}
    # EVERY account row per realm, kept separately for confirmed-pair lookup. The
    # YAML calls account_id "the stable key (ids do not change when an account is
    # renamed)" -- but searching only the PATTERN-MATCHED candidates defeated
    # exactly that: rename "Intercompany Clearing" to "IC Clearing" and the row
    # leaves `found`, so the id could never be located and a live pair silently
    # stopped reconciling.
    all_rows: dict[str, list[dict[str, Any]]] = {}
    covered = 0

    for entity in renderable:
        try:
            report = sources.get_balance_sheet(entity, as_of)
        except Exception as exc:  # noqa: BLE001 -- per-realm fail-soft
            log.warning("finance_close: intercompany scan failed for %s: %s", entity, exc)
            continue
        covered += 1
        every = iter_account_rows(report)
        all_rows[entity] = every
        rows = [r for r in every if is_intercompany_account(r["name"])]
        if rows:
            found[entity] = rows

    if covered == 0:
        section.available = False
        section.stub_reason = "no realm's balance sheet could be read"
        return section, snap

    confirmed = load_intercompany_map().get("pairs") or []
    active = [p for p in confirmed if p.get("confirmed") is True]

    total = sum(len(r) for r in found.values())
    if not found:
        section.lines.append(
            f"No intercompany-named accounts found across {covered} realm(s)."
        )
    else:
        section.lines.append(
            f"Discovery: {total} candidate account(s) across {len(found)} realm(s)"
            + ("" if active else " — pairing awaits Justin's confirmation.")
        )
        for entity in sorted(found):
            for index, row in enumerate(found[entity], start=1):
                label = _candidate_label(entity, index, row["name"])
                section.lines.append(
                    f"• {entity_label(entity)}: {label} — balance "
                    f"{fmt_money(row['balance'])} [UNCONFIRMED pairing]"
                )
        snap["candidates"] = {e: len(r) for e, r in found.items()}

    # ── confirmed-pair mismatch check ────────────────────────────────────────
    if active:
        threshold = intercompany_delta_threshold()
        pairs_checked = 0
        for pair in active:
            left, right = pair.get("left") or {}, pair.get("right") or {}
            lv = _pair_balance(all_rows, left)
            rv = _pair_balance(all_rows, right)
            name = _scrub_external(str(pair.get("name") or "pair"), 40)
            # Defence in depth: _candidate_label opaques discovery rows, but a
            # confirmed pair rendered its YAML label free-form. That label is
            # Harrison-authored, yet a pair touching an opaque realm should not
            # depend on hand-authoring discipline to stay safe.
            if any(is_name_opaque_realm(str((side or {}).get("entity") or ""))
                   for side in (left, right)):
                name = f"pair #{active.index(pair) + 1} (name withheld)"
            if lv is None or rv is None:
                # "unavailable —" is the literal substring build_founder_cut
                # collects coverage lines by; any other wording makes an unrun
                # reconciliation invisible in the cut Harrison reads. The wording
                # also stays honest about the two distinct causes: the account was
                # absent, OR it was present with an unreadable balance.
                section.lines.append(
                    f"• {name}: unavailable — one side's account was not found, or "
                    f"returned no readable balance, so this pair was NOT checked"
                )
                continue
            pairs_checked += 1
            # Sign convention is recorded PER PAIR, never inferred from names: an
            # asset-side "Due from" and a liability-side "Due to" may be stored
            # with the same or opposite signs depending on how the books were set up.
            delta = round(lv + rv, 2) if pair.get("opposite_signs") else round(lv - rv, 2)
            if abs(delta) >= threshold:
                section.flags += 1
                section.lines.append(
                    f"• :triangular_flag_on_post: {name}: out of balance by "
                    f"{fmt_delta(delta)} (threshold {fmt_money(threshold)})"
                )
            else:
                section.lines.append(f"• {name}: in balance ({fmt_delta(delta)})")
    elif found:
        section.lines.append(
            "_No confirmed pairs yet — this is a discovery list, not a reconciliation._"
        )

    section.covered = covered
    section.expected = len(renderable)
    section.lines.append(f"_Scanned {covered} of {section.expected} realm(s)._")
    if any(is_name_opaque_realm(e) for e in found):
        section.lines.append(
            "_Account names for one or more realms are withheld here and sent to "
            "Harrison directly._"
        )
    return section, snap


def _pair_balance(found: dict[str, list[dict[str, Any]]], side: dict[str, Any]) -> float | None:
    """Balance for one confirmed side, matched on ACCOUNT ID first (stable) and
    falling back to an exact name match."""
    rows = found.get(str(side.get("entity") or ""), [])
    account_id = str(side.get("account_id") or "")
    if account_id:
        for row in rows:
            if row.get("id") == account_id:
                return row.get("balance")
    name = str(side.get("account_name") or "").strip().lower()
    if name:
        for row in rows:
            if row["name"].strip().lower() == name:
                return row.get("balance")
    return None


def build_worksheet(
    entities: list[str],
    sources: Sources,
    *,
    cash_fragment: dict[str, Any] | None = None,
    today: datetime.date | None = None,
) -> str:
    """Render the Monday worksheet v2 from the same stores the pack sections read.

    ONE COMPUTATION, TWO RENDERS. The accuracy rows here come from the same
    ``cashflow_worksheet.forecast_accuracy`` call the pack section makes, over
    the same store, for the same week -- because two systems computing "forecast
    accuracy" in one Monday is exactly the Mig-1 failure the supersession exists
    to prevent, and a worksheet that disagreed with the pack posted beside it
    would be worse than either alone.

    THE DEBUT GATE APPLIES TO THE MAP-DEPENDENT LEG ONLY, and that is a
    deliberate, named narrowing of the design's wording ("S3/S4 sections render
    only once the entity map has >=N confirmed pairs"). The gate exists so no
    figure is published against a realm-tab pairing nobody has verified. The
    accuracy, carry-in and candidates legs carry no QBO attribution at all --
    accuracy compares the sheet against its own banked forecast, carry-in reads
    the bank register per realm, candidates are cited text -- so withholding
    them would suppress verified figures for a reason that does not apply to
    them. The QBO-actuals leg, which is entirely map-dependent, renders the
    gate's stub; so does the whole ``cashflow_parallel`` pack section.
    """
    from . import cashflow_worksheet as cw  # noqa: PLC0415

    day = today or _today()
    snapshot = sources.get_cashflow_snapshot()
    entity_map = sources.get_cashflow_entity_map()
    gate = cw.debut_gate(entity_map)

    accuracy_week, accuracy, pending = cw.resolve_accuracy(
        latest=snapshot,
        load_snapshot=sources.load_cashflow_snapshot,
        snapshot_dates=sources.get_cashflow_snapshot_dates(),
        derived_tabs=getattr(entity_map, "derived_tabs", None),
    )

    renderable = [e for e in entities if e not in PACK_EXCLUDED_ENTITIES]
    carry = cw.build_carry_in(
        renderable,
        bank_snapshot=sources.get_bank_snapshot(),
        book_balances=cash_fragment,
    )

    return cw.render_worksheet(
        today=day,
        snapshot=snapshot,
        preliminary=sources.get_cashflow_newest_preliminary(),
        accuracy=accuracy,
        accuracy_week=accuracy_week,
        accuracy_pending=pending,
        carry_in=carry,
        candidates=cw.read_candidates(),
        entity_map=entity_map,
        gate=gate,
    )



# ─────────────────────────────────────────────────────────────────────────────
# Section 2 — AR/AP aging, week-over-week
# ─────────────────────────────────────────────────────────────────────────────

def _snapshot_gap_days(baseline: str, today: datetime.date | None = None) -> int | None:
    """Days between the baseline snapshot date and ``today``, or None if unparseable."""
    try:
        return ((today or _today()) - datetime.date.fromisoformat(baseline)).days
    except (TypeError, ValueError):
        return None


def build_aging_section(
    entities: list[str],
    sources: Sources,
    prior: dict[str, Any] | None,
    *,
    today: datetime.date | None = None,
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

    # Aging is always as-of the RUN date (the QBO aging endpoints take no date),
    # unlike the cash section's balance sheet which is as-of the cash-sheet week.
    # Unstated, that difference reads as an unexplained gap between two sections of
    # the same pack.
    section.lines.append(f"Aging is as of today ({(today or _today()).isoformat()}).")

    # Name the comparison baseline. "WoW" asserted against an unknown-age snapshot
    # is a claim the data may not support: a missed week or an aborted run leaves a
    # much older baseline while the thresholds are calibrated for seven days.
    baseline = str((prior or {}).get("_snapshot_date") or "") if prior else ""
    if not prior_aging:
        section.lines.append(
            "_First run (no prior snapshot with aging), so no week-over-week deltas yet._"
        )
    elif baseline:
        gap = _snapshot_gap_days(baseline, today)
        note = (
            f" :warning: that is {gap}d ago, not one week — deltas below span that gap"
            if gap is not None and gap > 10 else ""
        )
        section.lines.append(f"Compared against the {baseline} snapshot.{note}")

    metric_reads = {"ar": 0, "ap": 0}
    for row in rows:
        entity = row["entity"]
        label = entity_label(entity)
        if not (row.get("ar") or row.get("ap")):
            section.lines.append(f"• {label}: unavailable — no aging totals returned")
            continue

        parts: list[str] = []
        snap_entry: dict[str, Any] = {}
        crossed: list[str] = []
        for kind in ("ar", "ap"):
            data = row.get(kind)
            if not data:
                parts.append(f"{kind.upper()} n/a")
                continue
            metric_reads[kind] += 1
            total = data["total"]
            snap_entry[kind] = total
            prior_total = (prior_aging.get(entity) or {}).get(kind)
            delta = None if prior_total is None else total - prior_total
            if delta is None:
                # No prior figure for THIS entity. Without the qualifier a bare total
                # sits beside a neighbour's "(+$30,000 WoW)" and reads as no change.
                parts.append(f"{kind.upper()} {fmt_money(total)} (no prior)")
                continue
            if _crosses(delta, prior_total, AGING_DELTA_ABS, AGING_DELTA_PCT):
                crossed.append(kind.upper())
            parts.append(f"{kind.upper()} {fmt_money(total)} ({fmt_delta(delta)})")

        mark = ":triangular_flag_on_post: " if crossed else ""
        if crossed:
            section.flags += 1
        # Name WHICH metric crossed -- both branches previously rendered the same
        # string, so a line-level flag was not attributable.
        crossed_note = f" [{'/'.join(crossed)} moved materially]" if crossed else ""
        section.lines.append(f"• {mark}{label}: " + ", ".join(parts) + crossed_note)

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
                f"    -> {kind.upper()} aged tail — {bucket}: {fmt_money(amount)}"
            )
        if snap_entry:
            snap[entity] = snap_entry

    # Coverage PER METRIC. An entity-level count let every AP read in the portfolio
    # fail while the footer still claimed full coverage, because "either leg" was
    # enough to count the entity as read.
    section.covered = min(metric_reads["ar"], metric_reads["ap"])
    section.expected = len(entities)
    section.lines.append(
        f"_AR read for {metric_reads['ar']} of {len(entities)} entity(ies); "
        f"AP read for {metric_reads['ap']} of {len(entities)}._"
    )
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

    metric_reads = {"revenue": 0, "expenses": 0}
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
        crossed: list[str] = []
        for kind, cur_key, prior_key in (
            ("revenue", "cur_revenue", "prior_revenue"),
            ("expenses", "cur_expenses", "prior_expenses"),
        ):
            cur, prev = row.get(cur_key), row.get(prior_key)
            if cur is None:
                parts.append(f"{kind} n/a")
                continue
            metric_reads[kind] += 1
            delta = None if prev is None else cur - prev
            if delta is None:
                parts.append(f"{kind} {fmt_money(cur)} (no prior month)")
                continue
            if not basis_mismatch and _crosses(delta, prev, PNL_SWING_ABS, PNL_SWING_PCT):
                crossed.append(kind)
            parts.append(f"{kind} {fmt_money(cur)} ({fmt_delta(delta)} MoM)")

        flagged_here = bool(crossed)
        mark = ":triangular_flag_on_post: " if flagged_here else ""
        if flagged_here:
            section.flags += 1
            basis_note = f" [{'/'.join(crossed)} moved materially]" + basis_note
        section.lines.append(f"• {mark}{label}: " + ", ".join(parts) + basis_note)
        if basis_mismatch:
            section.lines.append(
                f"    -> basis changed between months ({prior_basis} to {basis}); "
                "swing is not comparable and was not flagged"
            )
        snap[entity] = {
            "revenue": row.get("cur_revenue"),
            "expenses": row.get("cur_expenses"),
            "period": cur_range[0],
        }

    # Coverage PER METRIC -- see the same fix in build_aging_section.
    section.covered = min(metric_reads["revenue"], metric_reads["expenses"])
    section.expected = len(entities)
    section.lines.append(
        f"_Revenue read for {metric_reads['revenue']} of {len(entities)} entity(ies); "
        f"expenses read for {metric_reads['expenses']} of {len(entities)}._"
    )
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

    # Unreconciled-looking: a material cash delta IS the reconciliation signal, so
    # it is RESTATED here (as a count) rather than independently derived -- and a
    # restatement must not add to the pack's flag total, or three cash findings
    # would be reported as four.
    if cash_section.available:
        if cash_section.flags:
            section.lines.append(
                f"• :triangular_flag_on_post: {cash_section.flags} entity(ies) show a cash "
                f"delta over threshold (>{fmt_money(CASH_DELTA_ABS)} or "
                f"{CASH_DELTA_PCT:.0%}) — unreconciled-looking; review before close."
            )
        else:
            section.lines.append(
                "• Cash sheet and books agree within threshold for every entity that "
                "COULD be checked."
            )
        # Partial coverage is its own finding. Without this, the run where 9 of 10
        # QBO realms 401 (token lifetimes are ~100 days and there is a monitor for
        # exactly that) summarised as "agree within threshold" and the founder cut
        # said "no item crossed a flag threshold".
        if cash_section.is_partial:
            gap = (cash_section.expected or 0) - (cash_section.covered or 0)
            section.lines.append(
                f"• :triangular_flag_on_post: reconciliation status UNKNOWN for {gap} of "
                f"{cash_section.expected} entity(ies) — a source was unreadable, so those "
                "were never cross-checked. Not an all-clear."
            )
            section.flags += 1
    else:
        section.lines.append(
            "• :triangular_flag_on_post: Cash cross-check unavailable this run — "
            "reconciliation status unknown for every entity."
        )
        section.flags += 1

    facts = None
    try:
        facts = sources.get_adherence()
    except Exception as exc:  # noqa: BLE001
        log.warning("finance_close: adherence facts unreadable: %s", exc)

    if not facts:
        section.lines.append(
            "• :triangular_flag_on_post: _Adherence facts unavailable_ — the "
            "finance-adherence check produced no facts block, so cash-sheet freshness, "
            "monthly-filing presence and bank-statement staleness are unknown this run."
        )
        section.flags += 1
        return section

    generated = str(facts.get("generated_date") or facts.get("generated_at") or "")[:10]
    age_days: int | None = None
    try:
        age_days = (day - datetime.date.fromisoformat(generated)).days
    except ValueError:
        pass
    if age_days is None:
        # Absent or unparseable date. Previously BOTH branches were skipped, so
        # arbitrarily old facts rendered with no as-of line at all.
        section.lines.append(
            "• :warning: Adherence facts carry no readable generation date — their age "
            "cannot be established, so treat them as possibly stale."
        )
        section.flags += 1
    elif age_days > ADHERENCE_MAX_AGE_DAYS:
        section.lines.append(
            f"• :triangular_flag_on_post: Adherence facts are STALE (generated {generated}, "
            f"{age_days}d ago; the check runs weekly, 45 min before this pack) — the "
            "adherence job did not run this morning. Treat the items below as historical."
        )
        section.flags += 1
    else:
        section.lines.append(f"• Adherence facts as of {generated}.")

    # An all-unknown facts block (e.g. the G: mount was down at 08:15) otherwise
    # rendered zero flags and read as checked-and-clear, while the previous good
    # block had already been overwritten.
    unknown_count = facts.get("unknown_count")
    if isinstance(unknown_count, int) and unknown_count > 0:
        section.lines.append(
            f"• :warning: {unknown_count} adherence check(s) could NOT be read this run "
            "(Drive unreachable or unreadable) — their status is unknown, not clear."
        )
        section.flags += 1

    fact_lines = [
        ln.strip() for ln in (facts.get("facts") or [])
        if isinstance(ln, str) and ln.strip()
    ]
    if not fact_lines:
        section.lines.append(
            "• :warning: the adherence facts block contained no fact lines — nothing was "
            "actually checked."
        )
        section.flags += 1
        return section

    # Severity comes from the producer's PARALLEL status list when supplied. Prose
    # matching is brittle in both directions -- the earlier "no_content" token never
    # matched the real "(no content)" text -- and a rolled-up line's synthetic key
    # matches no per-check status key, so key lookup alone under-flags exactly the
    # grouped findings the roll-up exists to surface. Token matching remains only as
    # the fallback for a producer that supplies no statuses.
    raw_status = facts.get("facts_status")
    statuses: list[str] = (
        [str(s).lower() for s in raw_status]
        if isinstance(raw_status, list) and len(raw_status) == len(fact_lines)
        else []
    )

    # Cap the rendered lines: this is the one place the pack renders text it did not
    # compute, and an unbounded producer would push the post past Slack's 40k limit
    # (a delivery failure, i.e. nobody sees the pack at all).
    shown = fact_lines[:_MAX_ADHERENCE_LINES]
    for idx, line in enumerate(shown):
        if statuses:
            flagged = statuses[idx] in _ADHERENCE_PROBLEM_STATUSES
        else:
            flagged = any(
                token in line.lower()
                for token in ("missing", "stale", "overdue", "absent", "no content")
            )
        if flagged:
            section.flags += 1
        section.lines.append(
            f"• {':triangular_flag_on_post: ' if flagged else ''}{_scrub_external(line, 300)}"
        )
    if len(fact_lines) > len(shown):
        section.lines.append(
            f"• _{len(fact_lines) - len(shown)} further adherence line(s) not shown "
            "(see the facts block in the accounting folder)._"
        )

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
    unconfirmed = 0
    for days_out, item in dated:
        if days_out > horizon_days:
            continue
        shown += 1
        # Every field below comes from a human-maintained YAML file -- scrubbed so a
        # typo'd or crafted value cannot inject Slack control syntax into a finance
        # channel (see _scrub_external).
        name = _scrub_external(item.get("name") or "unnamed")
        ent = _scrub_external(item.get("entity") or "", 24)
        ent_tag = f" [{ent}]" if ent else ""
        amount = item.get("amount")
        amount_val = float(amount) if isinstance(amount, (int, float)) else None
        cost = f" — {fmt_money(amount_val)}" if amount_val is not None else ""
        cadence = _scrub_external(item.get("cadence") or "", 32)
        cadence_tag = f" ({cadence})" if cadence else ""
        # A seeded placeholder date must not masquerade as a verified renewal date --
        # otherwise the radar fires PAST DUE into the founder cut every week off a
        # date nobody confirmed.
        confirmed = item.get("confirmed")
        provisional = " — UNCONFIRMED date/amount, verify before acting" if confirmed is False else ""
        if confirmed is False:
            unconfirmed += 1
        if days_out < 0:
            section.flags += 1
            when = f":rotating_light: PAST DUE {abs(days_out)}d"
        elif days_out <= 7:
            section.flags += 1
            when = f":warning: due in {days_out}d"
        else:
            when = f"due in {days_out}d"
        section.lines.append(f"• {when} — {name}{ent_tag}{cost}{cadence_tag}{provisional}")

    if not shown:
        section.lines.append(f"• Nothing due or past due within {horizon_days} days.")
    if undated:
        names = ", ".join(_scrub_external(i.get("name") or "unnamed", 60) for i in undated[:5])
        more = f" (+{len(undated) - 5} more)" if len(undated) > 5 else ""
        section.lines.append(
            f"• _{len(undated)} entry(ies) have no parseable next_due and were not "
            f"assessed: {names}{more}_"
        )
    footer = f"_Radar covers {len(items)} tracked item(s); {shown} within {horizon_days}d."
    if unconfirmed:
        footer += (
            f" {unconfirmed} shown entry(ies) are UNCONFIRMED seeds — this radar is a "
            "partial list, not full subscription coverage."
        )
    section.lines.append(footer + "_")
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
        offered = entities if entities is not None else src.get_provisioned()
    except Exception as exc:  # noqa: BLE001
        log.error("finance_close: could not list provisioned entities: %s", exc)
        offered = []

    # Pack-wide sensitivity exclusion, applied ONCE here so it governs every
    # section. The cash section still receives the full list so it can report the
    # exclusion in its footer.
    provisioned = [e for e in offered if e not in PACK_EXCLUDED_ENTITIES]

    def guarded_simple(key: str, title: str, fn: Callable[[], Section]) -> Section:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- section-level fail-soft
            log.error("finance_close: section %s raised: %s", key, exc)
            return Section(
                key=key, title=title, available=False,
                stub_reason=f"section builder failed ({type(exc).__name__})",
            )

    pack = ClosePack(generated_at=day.isoformat())
    if not provisioned:
        for key, title in (
            ("cash", ":bank: Cash — cash sheet vs books"),
            ("qbo_bank", ":bank: QBO bank & books freshness"),
            ("forecast_assist", ":chart_with_upwards_trend: Forecast assist"),
            ("cashflow_parallel", ":scales: Parallel run — sheet vs QBO actuals"),
            ("intercompany", ":left_right_arrow: Intercompany"),
            ("aging", ":inbox_tray: AR / AP aging — week over week"),
            ("pnl", ":bar_chart: P&L sanity — month over month"),
        ):
            pack.sections.append(Section(
                key=key, title=title, available=False,
                stub_reason="no provisioned accounting entities could be listed",
            ))
        # These two were previously called UNGUARDED on this path, so an exception
        # here lost the whole pack instead of stubbing one section.
        pack.sections.append(guarded_simple(
            "close_prep", ":clipboard: Close-prep notes",
            lambda: build_close_prep_section(src, cash_section=pack.sections[0], today=day),
        ))
        pack.sections.append(guarded_simple(
            "renewals", ":calendar: Renewal / payment radar",
            lambda: build_renewal_section(src, today=day),
        ))
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
        lambda: build_cash_section(offered, src, today=day),
    )
    bank_section = guarded(
        "qbo_bank", ":bank: QBO bank & books freshness",
        lambda: build_bank_section(provisioned, src, today=day),
    )
    forecast_section = guarded(
        "forecast_assist", ":chart_with_upwards_trend: Forecast assist",
        # The cash fragment carries each entity's BOOK balance, already computed
        # above. Passing it means the carry-in figure is on the same basis as the
        # sheet row it goes into, with no extra API call.
        lambda: build_forecast_assist_section(
            provisioned, src,
            cash_fragment=snapshot.get("cash"), today=day,
        ),
    )
    parallel_section = guarded(
        "cashflow_parallel", ":scales: Parallel run — sheet vs QBO actuals",
        lambda: build_cashflow_parallel_section(src, today=day),
    )
    intercompany_section = guarded(
        "intercompany", ":left_right_arrow: Intercompany",
        lambda: build_intercompany_section(provisioned, src, today=day),
    )
    aging_section = guarded(
        "aging", ":inbox_tray: AR / AP aging — week over week",
        lambda: build_aging_section(provisioned, src, prior, today=day),
    )
    pnl_section = guarded(
        "pnl", ":bar_chart: P&L sanity — month over month",
        lambda: build_pnl_section(provisioned, src, today=day),
    )
    close_prep = guarded_simple(
        "close_prep", ":clipboard: Close-prep notes",
        lambda: build_close_prep_section(src, cash_section=cash_section, today=day),
    )
    renewals = guarded_simple(
        "renewals", ":calendar: Renewal / payment radar",
        lambda: build_renewal_section(src, today=day),
    )

    pack.sections = [
        cash_section, bank_section, forecast_section, parallel_section,
        intercompany_section, aging_section, pnl_section, close_prep, renewals,
    ]

    # Guarded like a section: a worksheet failure must cost the worksheet, never
    # the pack. The delivery script treats `worksheet is None` as "write nothing"
    # rather than writing a stub file that would look like this week's worksheet.
    try:
        pack.worksheet = build_worksheet(
            provisioned, src, cash_fragment=snapshot.get("cash"), today=day)
    except Exception as exc:  # noqa: BLE001
        log.error("finance_close: worksheet build raised: %s", exc)

    # Only persist a snapshot that actually carries data. A run where every section
    # stubbed would otherwise become next week's "most recent prior snapshot" --
    # losing the real week-over-week baseline AND mislabelling the result
    # "First run - no prior snapshot".
    has_data = any(k in snapshot for k in ("cash", "aging", "pnl"))
    if persist_snapshot and has_data:
        try:
            write_snapshot(snapshot, today=day, snapshot_dir=snapshot_dir)
        except OSError as exc:
            log.warning("finance_close: snapshot write failed: %s", exc)
    elif persist_snapshot:
        log.warning(
            "finance_close: every section stubbed -- NOT persisting an empty snapshot "
            "(it would become next week's WoW baseline)"
        )

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
        if not text:
            return None
        # CODE-enforce the "no figure Python did not compute" rule. Prompting alone
        # left the model free to sum two deltas or invent a portfolio total, and the
        # narration is prefixed ABOVE the "every figure is a direct source read"
        # line, so it visually inherits that guarantee. Any money token absent from
        # the facts text drops the narration entirely.
        invented = [tok for tok in _MONEY_TOKEN_RE.findall(text) if tok not in facts]
        if invented:
            log.warning(
                "finance_close: narration contained %d figure(s) not present in the "
                "facts block -- dropping narration", len(invented),
            )
            return None
        return text
    except Exception as exc:  # noqa: BLE001 -- narration is never load-bearing
        log.warning("finance_close: narration failed, posting facts only: %s", exc)
        return None
