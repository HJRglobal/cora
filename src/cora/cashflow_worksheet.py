"""13-week cashflow shadow ledger -- S3 worksheet v2 + S4 parallel-run join.

WHAT THIS IS
------------
The derived layer over M1 (`cashflow_ledger`, forecast snapshots) and M2
(`cashflow_actuals`, QBO bank-cash windows). It produces two things:

  * `render_worksheet()` -- the Monday markdown worksheet Justin types from,
    written to `cashflow-ledger/worksheets/YYYY-MM-DD_fndr_cashflow-worksheet.md`.
    This REPLACES the A5 S2b `forecast-assist/` lane (one worksheet, one path).
  * `build_parallel()` -- the joined sheet-vs-QBO comparison the close pack
    renders as its `cashflow_parallel` section.

Pure-ish: every figure is computed here in Python (D-095); nothing in this
module calls a model, and the two callers inject their sources.

THREE THINGS THE LIVE DATA CHANGED, MEASURED BEFORE THEY WERE CODED
-------------------------------------------------------------------
1. **Ending-cash grain does not exist in v1.** The design specified comparison
   at "ending-cash/net-flow grain". M2 withholds `closing_bank_balance` unless
   QBO's General Ledger rendered EVERY bank account (D-129), and across all
   live windows to date it never has -- 0 of 8 realms carried one on the
   2026-08-07 finalized window. So v1 compares NET FLOW, and the ending-cash
   line renders only on the realms that actually have a complete balance.
   Claiming a grain the data cannot produce is the failure this program exists
   to stop.

2. **The two net flows are on DIFFERENT BASES, and the difference is not
   decomposable here.** The sheet's rows are Cash/CC ("BEGINNING Cash/CC - Book
   Balance"); M2's perimeter is bank accounts only, where a card PAYMENT is the
   cash event and card PURCHASES are not (D-120/Fin-3). A week with card
   spending therefore diverges by construction. Measured on the 2026-08-07
   finalized window against the 2026-08-17 snapshot: BDM +$16,065, F3E +$20,675,
   OSNGW -$6,118 -- four and five figures on every entity, against a gate of
   max($100, 0.5%). Nothing available in v1 splits the card component out, so
   the delta is reported as **unattributed**, never as "unexplained", and the
   flip gate is BLOCKED with that stated as its reason. Calling an
   undecomposable delta "unexplained" would have started a four-week clock on a
   number no amount of clean bookkeeping can bring inside tolerance.

3. **The sheet's actual entry is per-tab and uneven, so "week W-2" is not a
   week the sheet necessarily holds.** On 2026-08-17, 8 of 19 tabs carried an
   actual for 8-14 while 9 were still at 7-31 and one at 8-07. The join
   therefore treats a missing sheet actual as a NAMED pending row, not as a
   zero and not as a silent omission.

WHAT IS COMPARABLE, AND IT WORKS
--------------------------------
Forecast accuracy does NOT go through any of that. It compares the S1 store
against itself: a forecast banked in a VERIFIED PRE-CLOSE snapshot versus the
actual the sheet later recorded for that same week, same tab, same measure,
same basis. Measured live on week ending 2026-08-14 from the 2026-08-10
snapshot: 8 tabs, real variances (F3E forecast $241 vs actual -$22,636). This
is the number the shadow ledger exists to produce and the sheet cannot -- its
own forecast column is overwritten at close (D-121), which is why the sheet's
DIFF read 0.00 on every closed week but one.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import cashflow_actuals as ca
from . import cashflow_ledger as cl
from . import cashflow_maps as cm

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Local canonical worksheet store; mirrored into the Founder-OS accounting tree.
WORKSHEET_DIR = cl.STORE_DIR / "worksheets"
MIRROR_WORKSHEET_RELDIR = cl.MIRROR_RELDIR / "worksheets"

#: S6 forecast-delta candidates (writer = the Mon 13:00 Cowork review task).
#: READ-ONLY here, and read as UNTRUSTED INPUT (D-123).
CANDIDATES_DIR = cl.STORE_DIR / "candidates"
MIRROR_CANDIDATES_RELDIR = cl.MIRROR_RELDIR / "candidates"

#: Where the retired A5 S2b lane used to point. Kept as a POINTER so the
#: supersession is discoverable from the old name rather than silently vanishing.
SUPERSEDED_FORECAST_ASSIST_RELDIR = "01-HJR-Global/accounting/forecast-assist"

#: The pack-debut gate (design §5, migration F8). S4 and the worksheet's
#: QBO-actuals leg stay stubbed until the entity map carries this many CONFIRMED
#: realm<->tab pairs, so the pack never debuts an all-UNCONFIRMED table.
DEFAULT_DEBUT_MIN_CONFIRMED = 5

#: Flip-gate tolerance (design §4.2 S4): max($100, 0.5% of the compared figure),
#: four consecutive weeks. Both halves env-tunable.
FLIP_GATE_ABS = 100.0
FLIP_GATE_PCT = 0.005
FLIP_GATE_WEEKS = 4

#: Basis strings. Every figure on every surface carries one (D-116).
SHEET_BASIS = "Standing ACTUALS sheet, Cash/CC book rows"
QBO_BASIS = "QBO bank-account cash flow (card purchases excluded; a card PAYMENT is the cash event)"

#: The one line that makes a PRELIMINARY window safe to read (design §4.2 S3).
FRI_SUN_WARNING = (
    "expect Fri-Sun activity to be missing -- verify against the bank portal "
    "before you type it"
)


class WorksheetError(Exception):
    """The worksheet could not be assembled from the stores."""


# ─────────────────────────────────────────────────────────────────────────────
# Untrusted-input chokepoint (D-123)
# ─────────────────────────────────────────────────────────────────────────────
#
# Everything this module renders that it did not author goes through here: sheet
# tab names and row labels (human-typed), QBO account names, and the S6
# candidates table (written by a MODEL in another process, from meeting
# transcripts). One function, so there is one place to audit.

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: URL-ish and platform-handle shapes. Stripped, not linkified: this text lands
#: in a finance channel and in a file Justin reads, which is exactly where a
#: payment link is most likely to be trusted.
_URL_RE = re.compile(r"\b(?:https?://|www\.|ftp://)\S*", re.IGNORECASE)
_MAILTO_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]*\w", re.IGNORECASE)

#: Slack control syntax. `slack_egress.sanitize_text` deliberately PRESERVES
#: `<...>` (the sanctioned citation form), so an externally-authored string
#: could otherwise render a live `<url|label>` or an `<!channel>` ping in a
#: message signed by Cora.
_ANGLE_RE = re.compile(r"[<>]")

#: Directive shapes a model-authored candidates file must never smuggle into a
#: renderer that a person then acts on. Matched at line start after stripping,
#: case-insensitive; the line is dropped whole rather than partially rewritten
#: (rewriting text a guard reads makes the guard a smuggling channel).
_DIRECTIVE_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:"
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|preceding)"
    r"|disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)"
    r"|system\s*:|assistant\s*:|<\|"
    r"|you\s+are\s+(?:now\s+)?(?:an?\s+)?(?:ai|assistant|chatbot)"
    r"|new\s+instructions?\b"
    r"|act\s+as\s+(?:an?\s+)?"
    r")",
    re.IGNORECASE,
)


def scrub(value: Any, cap: int = 120) -> str:
    """Neutralize an externally-authored string for a finance surface.

    Flattens to one line, strips control characters, URLs, e-mail addresses and
    Slack angle-bracket syntax, then length-caps. Returns "" for empty input --
    callers decide what an empty value means; this never invents a placeholder.
    """
    if value is None:
        return ""
    text = _CONTROL_CHARS.sub(" ", str(value))
    text = _URL_RE.sub("[link removed]", text)
    text = _MAILTO_RE.sub("[address removed]", text)
    text = _ANGLE_RE.sub("", text)
    return " ".join(text.split())[:cap]


def scrub_lines(text: str, *, max_lines: int = 40, cap: int = 200) -> list[str]:
    """Scrub a multi-line untrusted document into renderable lines.

    Directive-shaped lines are DROPPED WHOLE and counted, never rewritten into
    something that looks like content. Truncation is reported by the caller, not
    hidden: a silently-cut candidates table reads as "that is all there was".
    """
    out: list[str] = []
    for raw in str(text or "").splitlines():
        if not raw.strip():
            continue
        if _DIRECTIVE_RE.search(raw):
            log.warning("cashflow_worksheet: dropped a directive-shaped candidates line")
            continue
        cleaned = scrub(raw, cap=cap)
        if cleaned:
            out.append(cleaned)
        if len(out) >= max_lines:
            break
    return out


def _account_label(realm: str, index: int, name: str) -> str:
    """How one bank account may appear on a finance surface.

    LEX account names never free-render (D-124): `is_any_phi` cannot catch a
    bare person name in an account title ("Due from Jane Smith" trips none of
    its predicates), and #hjrg-finance / #founder-finance / Justin's DM are not
    LEX-custodian surfaces. Prefix-matched via `cashflow_maps`, so a future
    LEX-LLC / LEXLLC realm is covered without another edit.
    """
    if cm.realm_names_are_opaque(realm):
        return f"{realm} account #{index}"
    return scrub(name, 60)


# ─────────────────────────────────────────────────────────────────────────────
# Formatting
# ─────────────────────────────────────────────────────────────────────────────

def fmt_money(value: Optional[float]) -> str:
    """Currency, or an explicit `UNKNOWN` -- never an empty cell.

    An empty cell in a finance table reads as zero.
    """
    if value is None:
        return "UNKNOWN"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def fmt_delta(value: Optional[float]) -> str:
    if value is None:
        return "UNKNOWN"
    return f"{'+' if value >= 0 else '-'}${abs(value):,.0f}"


# ─────────────────────────────────────────────────────────────────────────────
# Pack-debut gate
# ─────────────────────────────────────────────────────────────────────────────

def debut_min_confirmed() -> int:
    """`CASHFLOW_PACK_DEBUT_MIN_CONFIRMED`, default 5."""
    raw = os.environ.get("CASHFLOW_PACK_DEBUT_MIN_CONFIRMED", "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DEBUT_MIN_CONFIRMED
    return value if value >= 0 else DEFAULT_DEBUT_MIN_CONFIRMED


@dataclass
class DebutGate:
    confirmed: int
    required: int
    mappable: int

    @property
    def open(self) -> bool:
        return self.confirmed >= self.required

    @property
    def stub_line(self) -> str:
        return (
            f"Awaiting entity-map confirmation ({self.confirmed} of {self.mappable} "
            f"realm-tab pairs confirmed; {self.required} needed). Until Justin "
            f"confirms, every QBO-attributed figure would carry a pairing nobody "
            f"has verified, so this leg is withheld rather than shown UNCONFIRMED."
        )


def debut_gate(entity_map: cm.EntityMap, *, required: Optional[int] = None) -> DebutGate:
    """How many confirmed pairs exist versus how many are needed to debut.

    The denominator counts realms that COULD be confirmed -- excluded realms are
    not pending decisions, and counting them would make the gate unreachable by
    construction.
    """
    mappable = len([
        r for r in entity_map.pairs
        if not entity_map.is_excluded(r)
    ])
    return DebutGate(
        confirmed=entity_map.confirmed_count(),
        required=debut_min_confirmed() if required is None else required,
        mappable=mappable,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Forecast accuracy -- verified pre-close snapshots only (design §4.2 S3, Fin-6)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AccuracyRow:
    tab: str
    week_ending: str
    forecast: float
    actual: float
    variance: float
    snapshot_date: str
    horizon_days: int

    def as_dict(self) -> dict:
        return {
            "tab": self.tab, "week_ending": self.week_ending,
            "forecast": self.forecast, "actual": self.actual,
            "variance": self.variance, "snapshot_date": self.snapshot_date,
            "horizon_days": self.horizon_days,
        }


def _series_point(
    snapshot: dict, tab: str, measure: str, week_ending: str
) -> Optional[dict]:
    for point in (snapshot.get("tabs", {}).get(tab, {})
                  .get("series", {}).get(measure) or []):
        if point.get("week_ending") == week_ending:
            return point
    return None


def _tab_is_verified_pre_close(
    snapshot: dict, tab: str, week_ending: str
) -> bool:
    """True if this snapshot's forecast for ``week_ending`` is usable for accuracy.

    THREE conditions, all necessary:

      * the snapshot predates the week's close -- otherwise the cell may already
        hold the entered actual (D-121);
      * the tab is not stamped `post_refresh_suspect` -- a post-refresh read's
        forecast cells are overwritten actuals, which would report ~100%
        accuracy forever (the exact defect this store exists to fix);
      * the point itself carries `basis == forecast` and no actual. This is the
        belt: the two stamps above are ABOUT the read, this one is about the
        cell, and a whole-tab stamp cannot know that one week closed early
        (CF_HJR Prop legitimately runs a week ahead of the workbook).
    """
    block = snapshot.get("tabs", {}).get(tab) or {}
    if block.get("post_refresh_suspect"):
        return False
    try:
        snap_date = datetime.date.fromisoformat(str(snapshot.get("snapshot_date")))
        week = datetime.date.fromisoformat(week_ending)
    except (TypeError, ValueError):
        return False
    if snap_date > week:
        return False
    point = _series_point(snapshot, tab, "ending_cash", week_ending)
    if not point:
        return False
    return (
        point.get("basis") == "forecast"
        and point.get("actual") is None
        and point.get("forecast") is not None
    )


#: How far back the accuracy anchor will look for a measurable week. Bounded so
#: a store with a long unmeasurable tail cannot turn one pack build into a scan
#: of the year; 8 weeks is two months of Mondays.
_ACCURACY_SEARCH_WEEKS = 8


def closed_weeks(snapshot: Optional[dict]) -> list[str]:
    """Distinct week-endings that at least one tab has closed, newest first."""
    weeks: set[str] = set()
    for block in (snapshot or {}).get("tabs", {}).values():
        for point in (block.get("series") or {}).get("ending_cash") or []:
            if point.get("actual") is not None and point.get("week_ending"):
                weeks.add(str(point["week_ending"]))
    return sorted(weeks, reverse=True)


def resolve_accuracy(
    *,
    latest: Optional[dict],
    load_snapshot: Callable[[datetime.date], Optional[dict]],
    snapshot_dates: list[datetime.date],
) -> tuple[Optional[str], list["AccuracyRow"], list[str]]:
    """Pick the newest closed week that is actually MEASURABLE, and measure it.

    WHY THIS IS NOT SIMPLY "THE LAST CLOSED WEEK". Two independent facts make the
    newest closed week frequently the worst one to anchor on:

      * the sheet's per-tab entry cadence is uneven, so the modal last-closed
        week lags the leading tabs by two weeks or more (measured 2026-08-17:
        modal 7-31, leading tabs 8-14);
      * a week is only measurable if a VERIFIED pre-close snapshot banked a
        forecast for it, and the store's early snapshots may all be
        post-refresh-suspect.

    Anchoring on the modal week produced "NOT COMPUTABLE" against live data for
    week 7-31 while week 8-14 was measurable on 8 tabs -- the flagship figure of
    the whole program, reported as unavailable because of how the anchor was
    chosen. So: walk closed weeks newest-first and return the first that yields
    at least one row. If none does, return the newest closed week so the
    "not computable" statement still names a real week.
    """
    weeks = closed_weeks(latest)[:_ACCURACY_SEARCH_WEEKS]
    if not latest or not weeks:
        return None, [], []

    first_pending: list[str] = []
    for week in weeks:
        try:
            rows, pending = forecast_accuracy(
                latest=latest, week_ending=week,
                load_snapshot=load_snapshot, snapshot_dates=snapshot_dates,
            )
        except WorksheetError as exc:
            log.warning("cashflow_worksheet: accuracy for %s failed: %s", week, exc)
            continue
        if rows:
            return week, rows, pending
        if not first_pending:
            first_pending = pending
    return weeks[0], [], first_pending


def forecast_accuracy(
    *,
    latest: dict,
    week_ending: str,
    load_snapshot: Callable[[datetime.date], Optional[dict]],
    snapshot_dates: list[datetime.date],
) -> tuple[list[AccuracyRow], list[str]]:
    """Per-tab forecast-vs-actual for one closed week, from banked forecasts only.

    Returns ``(rows, pending_tabs)``. A tab lands in ``pending`` -- named, never
    dropped -- when the actual is not on the sheet yet or when no VERIFIED
    pre-close snapshot banked a forecast for that week.

    The forecast is taken from the NEWEST verified pre-close snapshot, so the
    reported variance is the shortest-horizon forecast the store holds, and the
    horizon travels with the row: "we missed by $22K" means something different
    at 4 days than at 90.
    """
    rows: list[AccuracyRow] = []
    pending: list[str] = []

    try:
        week = datetime.date.fromisoformat(week_ending)
    except (TypeError, ValueError) as exc:
        raise WorksheetError(f"bad week_ending {week_ending!r}") from exc

    candidates = sorted((d for d in snapshot_dates if d <= week), reverse=True)

    for tab in sorted(latest.get("tabs") or {}):
        actual_point = _series_point(latest, tab, "ending_cash", week_ending)
        actual = (actual_point or {}).get("actual")
        if actual is None:
            pending.append(tab)
            continue

        matched: Optional[tuple[dict, dict]] = None
        for snap_date in candidates:
            snap = load_snapshot(snap_date)
            if not snap:
                continue
            if _tab_is_verified_pre_close(snap, tab, week_ending):
                point = _series_point(snap, tab, "ending_cash", week_ending)
                if point:
                    matched = (snap, point)
                    break
        if matched is None:
            pending.append(tab)
            continue

        snap, point = matched
        forecast = float(point["forecast"])
        snap_date = str(snap.get("snapshot_date"))
        rows.append(AccuracyRow(
            tab=tab, week_ending=week_ending,
            forecast=forecast, actual=float(actual),
            variance=round(float(actual) - forecast, 2),
            snapshot_date=snap_date,
            horizon_days=(week - datetime.date.fromisoformat(snap_date)).days,
        ))
    return rows, pending


# ─────────────────────────────────────────────────────────────────────────────
# S4 -- the parallel-run join
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParallelRow:
    """One realm's sheet-vs-QBO comparison for one finalized week."""
    realm: str
    tab: Optional[str]
    status: str                       # compared | pending_sheet | unavailable
    reason: str = ""
    sheet_net: Optional[float] = None
    qbo_net: Optional[float] = None
    delta: Optional[float] = None
    within_tolerance: Optional[bool] = None
    maturation: Optional[float] = None    # finalized net - preliminary net
    sheet_ending: Optional[float] = None
    qbo_ending: Optional[float] = None
    map_confirmed: bool = False

    def as_dict(self) -> dict:
        return {
            "realm": self.realm, "tab": self.tab, "status": self.status,
            "reason": self.reason, "sheet_net": self.sheet_net,
            "qbo_net": self.qbo_net, "delta": self.delta,
            "within_tolerance": self.within_tolerance,
            "maturation": self.maturation, "sheet_ending": self.sheet_ending,
            "qbo_ending": self.qbo_ending, "map_confirmed": self.map_confirmed,
        }


@dataclass
class Parallel:
    week_ending: Optional[str] = None
    rows: list[ParallelRow] = field(default_factory=list)
    covered: int = 0
    expected: int = 0
    available: bool = True
    reason: str = ""
    window_run_date: Optional[str] = None
    snapshot_date: Optional[str] = None
    expected_week_ending: Optional[str] = None
    stale_window: bool = False

    @property
    def compared(self) -> list[ParallelRow]:
        return [r for r in self.rows if r.status == "compared"]

    @property
    def out_of_tolerance(self) -> list[ParallelRow]:
        return [r for r in self.compared if r.within_tolerance is False]


def tolerance_for(figure: Optional[float]) -> float:
    """max($100, 0.5%) -- a flat threshold is unreachable on entities that move
    +/-$50-300K a week (Fin-4)."""
    abs_floor = _env_float("CASHFLOW_FLIP_GATE_ABS", FLIP_GATE_ABS)
    pct = _env_float("CASHFLOW_FLIP_GATE_PCT", FLIP_GATE_PCT)
    return max(abs_floor, abs(figure or 0.0) * pct)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def expected_finalized_week(
    today: datetime.date, weekday_name: str
) -> Optional[datetime.date]:
    """The W-2 week-ending this Monday's run should have finalized.

    Derived from the sheet's own week-ending weekday (never a hardcoded Friday,
    Fin-13): the most recent occurrence strictly before today, then one further
    week back -- W-1 just ended and is too immature in QBO to finalize.
    """
    weekdays = ["monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday"]
    try:
        target = weekdays.index(str(weekday_name or "").strip().lower())
    except ValueError:
        return None
    back = (today.weekday() - target - 1) % 7 + 1
    return today - datetime.timedelta(days=back + 7)


def build_parallel(
    *,
    snapshot: Optional[dict],
    finalized: Optional[dict],
    preliminary: Optional[dict] = None,
    entity_map: cm.EntityMap,
    gate: DebutGate,
    today: Optional[datetime.date] = None,
) -> Parallel:
    """Join the newest FINALIZED QBO window against the sheet actuals for that week.

    PRELIMINARY windows are never compared -- `usable_for_comparison` is False on
    them by construction (D-130(b)) and the accuracy math binds to finalized only
    (Fin-1). The preliminary for the SAME week is used for one purpose: its
    difference from the finalized re-pull is the measurable TIMING component
    (what matured in QBO over the week), which is the only part of the delta this
    layer can attribute.
    """
    today = today or datetime.date.today()
    out = Parallel()

    if not gate.open:
        out.available = False
        out.reason = gate.stub_line
        return out
    if not snapshot:
        out.available = False
        out.reason = "no forecast snapshot has been banked yet"
        return out
    if not finalized:
        out.available = False
        out.reason = (
            "no FINALIZED QBO actuals window exists yet -- a preliminary window "
            "is never compared"
        )
        return out

    week_ending = str(finalized.get("week_ending") or "")
    out.week_ending = week_ending
    out.window_run_date = str(finalized.get("run_date") or "") or None
    out.snapshot_date = str(snapshot.get("snapshot_date") or "") or None

    expected = expected_finalized_week(
        today, str(snapshot.get("week_ending_weekday") or ""))
    if expected:
        out.expected_week_ending = expected.isoformat()
        # A monitor that watches only the newest artifact cannot see a hole
        # behind it (D-130(d)). If the newest finalized window is older than the
        # week this Monday should have produced, S2 missed a run -- say so
        # rather than quietly comparing a stale week as if it were current.
        out.stale_window = week_ending < expected.isoformat()

    for realm in sorted(finalized.get("realms") or {}):
        block = finalized["realms"][realm] or {}
        pairing = entity_map.pairing(realm)
        tab = block.get("tab")
        row = ParallelRow(
            realm=realm, tab=tab, status="unavailable",
            map_confirmed=bool(pairing and pairing.usable_for_accuracy),
        )

        if not block.get("usable_for_comparison"):
            row.reason = scrub(
                block.get("reason_code")
                or ("realm-tab pairing not confirmed yet"
                    if not row.map_confirmed else "window not usable for comparison"),
                60,
            )
            out.rows.append(row)
            continue

        if not tab or tab not in (snapshot.get("tabs") or {}):
            row.reason = "the paired sheet tab was not readable in this snapshot"
            out.rows.append(row)
            continue

        sheet_point = _series_point(snapshot, tab, "net_cash_flow", week_ending)
        sheet_net = (sheet_point or {}).get("actual")
        row.qbo_net = block.get("net_flow")

        if sheet_net is None:
            # NOT a zero and NOT an omission: the sheet's per-tab entry cadence
            # is uneven (measured: 8 of 19 tabs held an 8-14 actual on 8-17),
            # so a missing cell is a pending row that must keep its name.
            row.status = "pending_sheet"
            row.reason = "the sheet has no actual entered for this week yet"
            out.rows.append(row)
            continue

        row.status = "compared"
        row.sheet_net = float(sheet_net)
        if row.qbo_net is not None:
            row.delta = round(float(row.qbo_net) - float(sheet_net), 2)
            row.within_tolerance = abs(row.delta) <= tolerance_for(sheet_net)

        # Ending cash renders ONLY when QBO produced a COMPLETE balance. It
        # almost never does -- the General Ledger omits accounts with no
        # activity, so M2 withholds the figure rather than publish a partial sum
        # under a total's name (D-129).
        ending_point = _series_point(snapshot, tab, "ending_cash", week_ending)
        row.sheet_ending = (ending_point or {}).get("actual")
        row.qbo_ending = block.get("closing_bank_balance")

        prelim_block = ((preliminary or {}).get("realms") or {}).get(realm) or {}
        if (str((preliminary or {}).get("week_ending") or "") == week_ending
                and prelim_block.get("net_flow") is not None
                and row.qbo_net is not None):
            row.maturation = round(
                float(row.qbo_net) - float(prelim_block["net_flow"]), 2)

        out.rows.append(row)

    out.covered = len(out.compared)
    # Denominator excludes realms whose pairing is a KNOWN pending decision --
    # the same corollary M2 applies. A permanently-false "partial" trains the
    # reader to ignore the real ones.
    out.expected = len([
        r for r in out.rows
        if not (r.status == "unavailable"
                and r.reason in {"realm_not_in_entity_map", "realm_scope_undeclared"})
    ])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# S6 candidates -- untrusted input, read through one chokepoint (D-123)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Candidates:
    date: Optional[str] = None
    lines: list[str] = field(default_factory=list)
    status: str = "none"          # ok | none | unreadable | last_good
    truncated: bool = False


def _candidate_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        (p for p in directory.glob("*.md")
         if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem)),
        reverse=True,
    )


def read_candidates(
    directory: Optional[Path] = None, *, max_lines: int = 40
) -> Candidates:
    """The newest S6 candidates table, scrubbed.

    FAIL-SOFT with a `.last-good` fallback: this file is written by a model in
    another process from meeting transcripts, so an unreadable or malformed one
    must degrade to the previous good table rather than take down the worksheet
    -- and must SAY which it served.
    """
    target = directory or CANDIDATES_DIR
    files = _candidate_files(target)
    if not files:
        return Candidates(status="none")

    for index, path in enumerate(files):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("cashflow_worksheet: candidates %s unreadable: %s",
                        path.name, exc)
            continue
        lines = scrub_lines(raw, max_lines=max_lines)
        if not lines:
            continue
        total = len([ln for ln in raw.splitlines() if ln.strip()])
        return Candidates(
            date=path.stem, lines=lines,
            status="ok" if index == 0 else "last_good",
            truncated=total > len(lines),
        )
    return Candidates(status="unreadable")


# ─────────────────────────────────────────────────────────────────────────────
# Carry-in (design §4.2 S3 / D-120(d); v1 posture cleared 2026-08-18)
# ─────────────────────────────────────────────────────────────────────────────

#: The v1 decision, stated on the surface so nobody has to infer it: Justin
#: sources and types the carry-in from the bank. Cora renders references.
CARRY_IN_POSTURE = (
    "v1: carry-in stays BANK-SOURCED and Justin-entered. The figures below are "
    "REFERENCES for cross-checking what you type -- none of them is the number "
    "to copy."
)

#: The deferred fork, PROPOSED not decided (Harrison/Justin call).
CARRY_IN_PROPOSAL = (
    "PROPOSED, NOT DECIDED -- Cora could pull a per-account as-of BOOK balance "
    "from QBO to pre-fill this row. Caveat that has to be weighed first: a QBO "
    "balance is only as current as the bank FEED, typically ~1 day behind the "
    "portal, so a Monday-morning pull can miss Friday-to-Sunday postings that "
    "the portal already shows. Harrison and Justin decide; nothing here changes "
    "until they do."
)


#: Per-account rows are capped and zero-balance accounts are dropped. Two
#: reasons, both learned from the live render: HJRP carries TWENTY bank accounts
#: of which seventeen sit at $0, so the useful three were buried; and every row
#: is an arbitrary QBO-authored string on a finance surface, so the fewer that
#: render, the smaller the untrusted-text surface. The dropped count is always
#: stated -- a silently shortened list reads as the whole list.
_MAX_ACCOUNT_ROWS = 8


@dataclass
class CarryInRow:
    entity: str
    status: str                       # ok | unavailable | shell
    reason: str = ""
    register_total: Optional[float] = None      # cash NET OF CARDS
    bank_total: Optional[float] = None          # what the account rows sum to
    card_total: Optional[float] = None
    book_total: Optional[float] = None
    posted_through: Optional[str] = None
    accounts: list[tuple[str, Optional[float]]] = field(default_factory=list)
    accounts_hidden: int = 0


def build_carry_in(
    entities: list[str],
    *,
    bank_snapshot: Optional[dict],
    book_balances: Optional[dict] = None,
) -> list[CarryInRow]:
    """Per-entity carry-in references from the A5 bank snapshot.

    TWO MEASURES, NEVER SUBSTITUTED FOR EACH OTHER (D-120(d)):
      * the REGISTER total (QBO Account API) -- what the bank side reads;
      * the BOOK total (BalanceSheet report) -- the basis the sheet's
        "BEGINNING Cash/CC - Book Balance" row actually uses.
    Live 2026-08-05 they differed by ~$101K on HJRP and flipped sign on BDM, so
    presenting either as "the" carry-in would manufacture a break.

    POSTED-THROUGH IS PER REALM, NOT PER ACCOUNT. The design asked for
    per-account stamps; the bank snapshot carries `newest_bank_txn_date` per
    realm (and per transaction TYPE), not per account. The finest available
    grain is rendered and labelled as such rather than a per-account stamp being
    faked from a realm-level date.
    """
    realms = (bank_snapshot or {}).get("realms") or {}
    out: list[CarryInRow] = []

    for entity in entities:
        block = realms.get(entity)
        if not block:
            out.append(CarryInRow(
                entity=entity, status="unavailable",
                reason="no bank snapshot for this realm"))
            continue
        if block.get("shell"):
            out.append(CarryInRow(entity=entity, status="shell",
                                  reason="cash-less shell realm"))
            continue
        if block.get("status") != "ok":
            out.append(CarryInRow(
                entity=entity, status="unavailable",
                reason=scrub(block.get("error") or "realm read failed", 80)))
            continue

        accounts: list[tuple[str, Optional[float]]] = []
        for index, acct in enumerate(block.get("accounts") or [], start=1):
            if str(acct.get("type") or "").strip().lower() != "bank":
                continue
            balance = acct.get("balance")
            # An account with a KNOWN zero balance carries no carry-in
            # information. An account whose balance is UNKNOWN does, and is kept.
            if balance is not None and abs(float(balance)) < 0.005:
                continue
            accounts.append((
                _account_label(entity, index, str(acct.get("name") or "")),
                balance,
            ))
        hidden = max(0, len(accounts) - _MAX_ACCOUNT_ROWS)

        out.append(CarryInRow(
            entity=entity, status="ok",
            register_total=block.get("cash_net_of_cards"),
            bank_total=block.get("bank_total"),
            card_total=block.get("cc_total"),
            book_total=(book_balances or {}).get(entity, {}).get("books_net"),
            posted_through=scrub(block.get("newest_bank_txn_date"), 10) or None,
            accounts=accounts[:_MAX_ACCOUNT_ROWS],
            accounts_hidden=hidden,
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Worksheet v2
# ─────────────────────────────────────────────────────────────────────────────

def worksheet_filename(day: datetime.date) -> str:
    return f"{day.isoformat()}_fndr_cashflow-worksheet.md"


def worksheet_path(day: datetime.date) -> Path:
    return WORKSHEET_DIR / worksheet_filename(day)


def mirror_worksheet_path(day: datetime.date) -> Path:
    return (cl.founder_os_root() / MIRROR_WORKSHEET_RELDIR
            / worksheet_filename(day))


def render_worksheet(
    *,
    today: datetime.date,
    snapshot: Optional[dict],
    preliminary: Optional[dict],
    accuracy: list[AccuracyRow],
    accuracy_week: Optional[str],
    accuracy_pending: list[str],
    carry_in: list[CarryInRow],
    candidates: Candidates,
    entity_map: cm.EntityMap,
    gate: DebutGate,
) -> str:
    """The durable Monday worksheet. Every figure is a direct store read."""
    lines: list[str] = [
        f"# Cashflow worksheet -- {today.isoformat()}",
        "",
        "_Generated by Cora from the 13-week shadow ledger. Deterministic: every "
        "figure below is a direct read of a banked store, never a model output._",
        "_Cora does NOT write the Standing ACTUALS sheet. This is a worksheet to "
        "type from._",
        "",
        f"_Supersedes the `{SUPERSEDED_FORECAST_ASSIST_RELDIR}/` worksheet lane -- "
        "one Monday worksheet, one path. Nothing is written there any more._",
        "",
    ]

    # ── 1. prior-week actuals (PRELIMINARY) ──────────────────────────────────
    lines.append("## 1. Last week's actuals (preliminary)")
    lines.append("")
    if not gate.open:
        lines += [gate.stub_line, ""]
    elif not preliminary:
        lines += ["No preliminary actuals window has been written yet.", ""]
    else:
        week = scrub(preliminary.get("week_ending"), 10)
        lines += [
            f"Week ending **{week}**, basis: {QBO_BASIS}.",
            "",
            f":warning: PRELIMINARY -- {FRI_SUN_WARNING}.",
            "",
        ]
        rendered = 0
        for realm in sorted(preliminary.get("realms") or {}):
            block = preliminary["realms"][realm] or {}
            tab = scrub(block.get("tab"), 40) or "unpaired"
            if block.get("status") != "ok":
                lines.append(
                    f"- **{realm}** ({tab}): UNKNOWN -- "
                    f"{scrub(block.get('reason_code') or 'not readable', 60)}"
                )
                continue
            rendered += 1
            stamp = scrub(block.get("posted_through"), 10) or "UNKNOWN"
            confirmed = "" if block.get("map_confirmed") else "  _[pairing UNCONFIRMED]_"
            lines.append(
                f"- **{realm}** ({tab}): net {fmt_money(block.get('net_flow'))} "
                f"(in {fmt_money(block.get('receipts'))} / out "
                f"{fmt_money(block.get('disbursements'))}), posted through "
                f"{stamp}{confirmed}"
            )
        lines += [
            "",
            f"_Covered {preliminary.get('covered', 0)} of "
            f"{preliminary.get('expected', 0)} realm(s); {rendered} rendered._",
            "",
        ]
        manual = [scrub(t, 40) for t in (preliminary.get("manual_entry_tabs") or [])]
        if manual:
            lines += [
                f"_Manual-entry (no QBO source), forecast rows only: "
                f"{', '.join(manual)}._",
                "",
            ]

    # ── 2. carry-in ──────────────────────────────────────────────────────────
    lines += ["## 2. Carry-in (opening row)", "", CARRY_IN_POSTURE, ""]
    for row in carry_in:
        if row.status == "shell":
            continue
        if row.status != "ok":
            lines.append(f"- **{row.entity}**: UNKNOWN -- {row.reason}")
            continue
        book = (f"books {fmt_money(row.book_total)} [BalanceSheet report -- the "
                f"basis the opening row uses]" if row.book_total is not None else
                "books UNKNOWN this run")
        # BOTH register figures, because they are not the same number and the
        # account rows below sum to only one of them. Showing "register $X" over
        # rows that sum to something else invites a reconciliation that cannot
        # succeed -- exactly the manufactured break this section exists to avoid.
        lines.append(
            f"- **{row.entity}**: bank accounts {fmt_money(row.bank_total)}, "
            f"cards {fmt_money(row.card_total)}, net of cards "
            f"{fmt_money(row.register_total)} [QBO account register]; {book}. "
            f"Posted through {row.posted_through or 'UNKNOWN'} (realm-level -- "
            f"the snapshot carries no per-account date)."
        )
        for label, balance in row.accounts:
            lines.append(f"    - {label}: {fmt_money(balance)}")
        if row.accounts_hidden:
            lines.append(
                f"    - _({row.accounts_hidden} more non-zero account(s) not "
                f"listed; zero-balance accounts are omitted throughout.)_"
            )
    lines += [
        "",
        "_Register and books are DIFFERENT MEASURES (posting order vs report "
        "basis); they are expected to differ and must never be substituted for "
        "each other._",
        "",
        f"_{CARRY_IN_PROPOSAL}_",
        "",
    ]

    # ── 3. forecast accuracy ─────────────────────────────────────────────────
    lines += ["## 3. Forecast accuracy", ""]
    if not accuracy and not accuracy_pending:
        lines += [
            "First run -- no snapshot history yet; accuracy begins once a week "
            "closes against a banked pre-close forecast.",
            "",
        ]
    elif not accuracy:
        lines += [
            f"No tab has both a closed actual for week {scrub(accuracy_week, 10)} "
            "and a VERIFIED pre-close forecast, so accuracy is NOT COMPUTABLE "
            "this week. It is not 100% and it is not zero.",
            "",
        ]
    else:
        lines += [
            f"Week ending **{scrub(accuracy_week, 10)}**, ending-cash measure. "
            "Forecasts come only from snapshots banked BEFORE the week closed "
            "and not stamped post-refresh -- the sheet's own forecast column is "
            "overwritten at close, so it cannot supply this.",
            "",
        ]
        for row in sorted(accuracy, key=lambda r: abs(r.variance), reverse=True):
            lines.append(
                f"- **{scrub(row.tab, 40)}**: forecast {fmt_money(row.forecast)} "
                f"vs actual {fmt_money(row.actual)} -- variance "
                f"{fmt_delta(row.variance)} (banked {row.snapshot_date}, "
                f"{row.horizon_days}-day horizon)"
            )
        lines.append("")
    if accuracy_pending:
        lines += [
            f"_No accuracy row yet for: "
            f"{', '.join(scrub(t, 40) for t in sorted(accuracy_pending))} "
            "-- either the sheet has no actual for the week, or no verified "
            "pre-close snapshot banked a forecast for it._",
            "",
        ]

    # ── 4. candidates ────────────────────────────────────────────────────────
    lines += ["## 4. Forecast delta candidates", ""]
    if candidates.status == "none":
        lines += [
            "No candidates file has been written yet (the Monday 13:00 review "
            "task writes it; this worksheet reads last week's, so there is one "
            "week of latency by design).",
            "",
        ]
    elif candidates.status == "unreadable":
        lines += [
            "The candidates file could not be read and no earlier good one "
            "exists. Nothing is shown rather than something guessed.",
            "",
        ]
    else:
        stale = (" _(serving the last good file -- the newest one was "
                 "unreadable)_" if candidates.status == "last_good" else "")
        lines += [
            f"From `candidates/{candidates.date}.md`{stale}. These are CITED "
            "candidates from meeting and synthesis sources, one week behind. "
            "**Verify every amount at source before touching the sheet** -- this "
            "table is untrusted input and is rendered scrubbed.",
            "",
        ]
        lines += [f"> {line}" for line in candidates.lines]
        if candidates.truncated:
            lines.append("> _(truncated -- read the file for the rest)_")
        lines.append("")

    # ── footer ───────────────────────────────────────────────────────────────
    snap_date = scrub((snapshot or {}).get("snapshot_date"), 10) or "none"
    lines += [
        "---",
        "",
        f"_Sources: forecast snapshot {snap_date} ({SHEET_BASIS}); QBO actuals "
        f"({QBO_BASIS}); bank register snapshot. Entity map: "
        f"{gate.confirmed} of {gate.mappable} pairs confirmed._",
        "",
    ]
    return "\n".join(lines)


def write_worksheet(text: str, day: datetime.date) -> Path:
    """Write the worksheet atomically to the local store."""
    path = worksheet_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".md.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Store loading helpers (thin, injectable)
# ─────────────────────────────────────────────────────────────────────────────

def latest_snapshot() -> Optional[dict]:
    day = cl.latest_snapshot_date()
    return cl.load_snapshot(day) if day else None


def next_forecast_week(
    snapshot: Optional[dict], *, today: Optional[datetime.date] = None
) -> Optional[str]:
    """The next week-ending that has not happened yet, as a CALENDAR fact.

    ANCHORED ON THE CALENDAR, NOT ON WHICH CELLS ARE EMPTY. A tab's
    "forward_week_endings" is every week with no actual entered -- which, on a
    tab whose actuals are two weeks behind, includes weeks that already closed.
    Both obvious data-side rules are wrong here and wrong in the same direction:

      * the earliest forward week on any tab relabels a PAST week as next
        week's target the moment one actual is late (the defect the predecessor
        section already carried a comment about);
      * the MODAL earliest forward week does the same thing, just with a
        quorum -- measured on the live 2026-08-17 snapshot it returned
        2026-08-07, because 9 of 19 tabs were still un-entered back to 7-31
        and outvoted the 8 that were current.

    So take the smallest week-ending strictly after today, across every tab.
    Entry lag cannot move it, and a tab that legitimately runs a week ahead
    (CF_HJR Prop, D-127(b)) cannot pull it backwards either.
    """
    day = today or datetime.date.today()
    best: Optional[str] = None
    for block in (snapshot or {}).get("tabs", {}).values():
        for week in block.get("forward_week_endings") or []:
            text = str(week)
            try:
                if datetime.date.fromisoformat(text) <= day:
                    continue
            except ValueError:
                continue
            if best is None or text < best:
                best = text
    return best


def portfolio_forecast(
    snapshot: Optional[dict], week_ending: Optional[str]
) -> Optional[float]:
    """The portfolio ending-cash forecast for a week, from CF_SUMMARY ONLY.

    Never a sum across tabs (Fin-9): the workbook carries derived roll-ups
    (OSN Consolidated, CF_OSN Core4) alongside the entity tabs, so summing
    double-counts the OSN stores and grosses up intercompany.
    """
    if not snapshot or not week_ending:
        return None
    point = _series_point(snapshot, "CF_SUMMARY", "ending_cash", week_ending)
    if not point:
        return None
    value = point.get("forecast")
    return None if value is None else float(value)


def latest_finalized() -> Optional[dict]:
    weeks = ca.list_finalized_weeks()
    return ca.load_finalized(weeks[-1]) if weeks else None


def preliminary_for(week_ending: Optional[datetime.date]) -> Optional[dict]:
    if not week_ending:
        return None
    return ca.load_window(week_ending, ca.WINDOW_PRELIMINARY)


def newest_preliminary() -> Optional[dict]:
    """The newest PRELIMINARY window on disk, by the week it describes."""
    if not ca.ACTUALS_DIR.exists():
        return None
    best: Optional[tuple[str, dict]] = None
    for path in ca.ACTUALS_DIR.glob("*_prelim-actuals.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("cashflow_worksheet: %s unreadable: %s", path.name, exc)
            continue
        week = str(payload.get("week_ending") or "")
        if week and (best is None or week > best[0]):
            best = (week, payload)
    return best[1] if best else None
