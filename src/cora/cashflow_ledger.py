"""13-week cashflow SHADOW LEDGER -- forecast snapshot store (M1/S1).

WHAT THIS IS. The machine-side archive of the Standing ACTUALS sheet's forward
forecast, banked once a week before Justin's Monday refresh overwrites it.

WHY IT EXISTS (D-121). On that sheet the FORECAST column is overwritten in place
once a week closes -- 42 of 43 historical weeks had FORECAST equal to ACTUAL to
sub-dollar rounding (A5 section 0 item 5). The sheet therefore destroys its own
forecast history: after a week closes there is no way to ask "what did we think
this week would look like?" Nothing downstream can measure forecast accuracy,
and no amount of later cleverness recovers a week nobody snapshotted. Every
Monday that goes unsnapshotted is history lost permanently -- which is why S1 is
the most loss-critical job in the estate and why the nightly health check
asserts it fired.

WHAT THIS IS NOT.

  * NOT canonical. The sheet stays canonical in v1 (fork F1, two-phase shadow
    ledger). Nothing here writes to the sheet, ever (A5 lock).
  * NOT an actuals source. Completed weeks' column values are stored under the
    ``post_close_column_value`` basis, never ``forecast`` -- they are overwritten
    actuals. Forecast-accuracy math may only read weeks whose forecast was
    captured BEFORE the week closed, and only from snapshots that are not
    ``post_refresh_suspect``.
  * NOT freshness-by-modifiedTime. The service account gets a 403 on Drive file
    metadata (cq-2ff81156f53a), so no signal in this system may key on it. Roll
    state is decided STRUCTURALLY, from the grid itself.

HR LLC is Harrison's personal books and is excluded at COLLECTION, not merely
un-rendered -- this file is mirrored into a folder Justin and Hayden work in.
Same posture as the QBO bank snapshot's ``excluded_realms``.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from cora.connectors import gsheets_financials as gf

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Local canonical store. One file per snapshot date; append-only across weeks.
STORE_DIR = _REPO_ROOT / "data" / "state" / "cashflow-ledger"
FORECAST_SNAPSHOT_DIR = STORE_DIR / "forecast-snapshots"

#: One-way Drive mirror root, relative to the Founder-OS root.
MIRROR_RELDIR = Path("01-HJR-Global") / "accounting" / "cashflow-ledger"
MIRROR_FORECAST_RELDIR = MIRROR_RELDIR / "forecast-snapshots"

#: Payload contract version -- bump when a consumer-visible field changes shape.
SCHEMA_VERSION = 1

#: The basis label every consumer must render alongside a figure from this file.
SNAPSHOT_BASIS = "Standing ACTUALS sheet -- forecast columns as read that morning"

#: Tabs that must NEVER be collected. CF_HR LLC is Harrison's personal expense
#: tracking; the mirror lands in a shared accounting folder, so it is excluded at
#: the sweep rather than filtered downstream (D-124 posture).
EXCLUDED_TABS: frozenset[str] = frozenset({"CF_HR LLC", "INPUTS_HR LLC"})


class LedgerError(Exception):
    """A structural failure that must not overwrite a good snapshot."""


#: Fixed reason codes for the MIRRORED payload. The underlying messages carry
#: the spreadsheet id (googleapiclient HttpError embeds the request URI) and, in
#: the self-check case, three raw cash figures for a tab whose grid was
#: otherwise withheld -- and the mirror lands in a shared accounting folder.
#: Detail stays in the local log, which is not mirrored.
_REASON_CODES: tuple[tuple[str, str], ...] = (
    ("triplet self-check failed", "triplet_mismatch"),
    ("cash-identity check failed", "identity_mismatch"),
    ("week grid unusable", "week_grid_unusable"),
    ("Ending Cash", "missing_ending_cash_row"),
    ("no week columns", "no_week_columns"),
    ("no rows", "empty_tab"),
    ("header rows", "header_not_found"),
)


def _reason_code(reason: str) -> str:
    """Map a free-text parse reason to a fixed, figure-free code."""
    text = str(reason or "")
    for needle, code in _REASON_CODES:
        if needle in text:
            return code
    return "unknown"


# ────────────────────────────────────────────────────────────────────────────
# Paths
# ────────────────────────────────────────────────────────────────────────────

def founder_os_root() -> Path:
    env = os.environ.get("FOUNDER_OS_ROOT", "").strip()
    return Path(env) if env else Path(r"G:\My Drive\HJR-Founder-OS")


def snapshot_filename(snapshot_date: datetime.date) -> str:
    return f"{snapshot_date.isoformat()}_forecast.json"


def snapshot_path(snapshot_date: datetime.date) -> Path:
    return FORECAST_SNAPSHOT_DIR / snapshot_filename(snapshot_date)


def mirror_path(snapshot_date: datetime.date) -> Path:
    return founder_os_root() / MIRROR_FORECAST_RELDIR / snapshot_filename(snapshot_date)


def list_snapshot_dates() -> list[datetime.date]:
    """Every banked snapshot date, oldest first."""
    if not FORECAST_SNAPSHOT_DIR.exists():
        return []
    out: list[datetime.date] = []
    for p in FORECAST_SNAPSHOT_DIR.glob("*_forecast.json"):
        try:
            out.append(datetime.date.fromisoformat(p.name.split("_", 1)[0]))
        except ValueError:
            log.warning("ignoring unparseable snapshot filename: %s", p.name)
    return sorted(out)


def latest_snapshot_date(
    before: Optional[datetime.date] = None,
    *,
    not_after: Optional[datetime.date] = None,
) -> Optional[datetime.date]:
    """Most recent banked snapshot date.

    ``before`` excludes that date and later; ``not_after`` caps the result so a
    stray future-dated file cannot masquerade as the newest snapshot.
    """
    dates = [
        d for d in list_snapshot_dates()
        if (before is None or d < before) and (not_after is None or d <= not_after)
    ]
    return dates[-1] if dates else None


def snapshot_coverage(snapshot_date: datetime.date) -> Optional[tuple[int, int]]:
    """(covered, expected) for a banked snapshot, or None if unreadable.

    The missed-run check needs this: a dated FILE is not evidence of a banked
    week -- it could hold zero tabs.
    """
    snap = load_snapshot(snapshot_date)
    if not isinstance(snap, dict):
        return None
    covered, expected = snap.get("covered"), snap.get("expected")
    if not isinstance(covered, int) or not isinstance(expected, int):
        return None
    return covered, expected


def load_snapshot(snapshot_date: datetime.date) -> Optional[dict]:
    path = snapshot_path(snapshot_date)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read snapshot %s: %s", path.name, exc)
        return None


def load_prior_snapshot(before: datetime.date) -> Optional[dict]:
    """The most recent snapshot strictly older than ``before``, if any."""
    prior_date = latest_snapshot_date(before=before)
    return load_snapshot(prior_date) if prior_date else None


# ────────────────────────────────────────────────────────────────────────────
# Roll-state detection (structural, never temporal)
# ────────────────────────────────────────────────────────────────────────────

def last_completed_week_ending(
    weekday_name: str, today: datetime.date
) -> Optional[datetime.date]:
    """The most recent week-ending date that has already passed.

    ``weekday_name`` comes from the sheet's own resolved grid (never hardcoded).
    Returns None if the name is not a weekday.
    """
    names = ["Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday"]
    try:
        target = names.index(weekday_name)
    except ValueError:
        return None
    delta = (today.weekday() - target) % 7
    if delta == 0:
        # The week-ending day IS today; it has not finished, so the last
        # COMPLETED one is a week back.
        delta = 7
    return today - datetime.timedelta(days=delta)


def workbook_boundary(vectors: dict[str, gf.ForecastVector]) -> Optional[str]:
    """The workbook's last-actual week: the MODE across tabs, ties going newest.

    The weekly refresh is a WORKBOOK event, not a per-tab one, so roll state has
    to be judged workbook-wide. Judging it per tab misreads a tab that simply
    runs ahead: CF_HJR Prop carries an actual for a week that has not closed
    yet, so a per-tab rule stamps it post-refresh EVERY week forever and its
    forecast accuracy could never be measured (D-051 Fin-7).
    """
    counts: dict[str, int] = {}
    for v in vectors.values():
        if v.ok and v.last_actual_week_ending:
            counts[v.last_actual_week_ending] = counts.get(v.last_actual_week_ending, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def classify_roll_state(
    vector: gf.ForecastVector,
    prior_tab: Optional[dict],
    *,
    today: datetime.date,
    boundary: Optional[str] = None,
    prior_snapshot_date: Optional[datetime.date] = None,
) -> dict:
    """Decide whether this tab was read BEFORE or AFTER the weekly refresh.

    "Before Justin's refresh" is an assumption, not a guarantee (Fin-5/Mig-3):
    the observed 8/3 refresh landed Monday ~5:30 PM, Hayden co-edits, and the SOP
    only says "Mon AM". A snapshot taken after the refresh holds an ENTERED
    ACTUAL in the cell a forecast-accuracy calculation would read as a forecast,
    so it must be excluded from that math rather than silently averaged in.

    THE TEST IS ABSOLUTE, NOT RELATIVE. On a pre-refresh Monday the workbook's
    last actual is the week BEFORE the one that just closed; after the refresh
    it is the week that just closed. So "are actuals present for the most
    recently completed week?" answers the question exactly, needs no history,
    and correctly stamps the very first snapshot (taken manually at merge,
    mid-week, necessarily post-refresh).

    A relative "did the boundary advance since last snapshot?" rule CANNOT work
    and the first cut of this function got it wrong: between two consecutive
    CORRECT pre-refresh Mondays exactly one week closes, so it fires every
    single week and the ledger would bank data it then refuses to use, forever
    (D-051, found independently by two lenses). The relative comparison is kept
    only as an ANOMALY detector -- a boundary that jumped MORE weeks than
    actually elapsed means something unexpected happened to the sheet.

    Judged on the WORKBOOK boundary, not this tab's: the refresh is a workbook
    event, and a tab that simply runs ahead (CF_HJR Prop) must not be stamped
    suspect every week for the rest of time.
    """
    signals: list[str] = []

    expected_pre: Optional[datetime.date] = None
    if vector.week_ending_weekday:
        expected_pre = last_completed_week_ending(vector.week_ending_weekday, today)

    last_actual = vector.last_actual_week_ending
    judged_on = boundary or last_actual

    if expected_pre is not None and judged_on:
        if judged_on >= expected_pre.isoformat():
            signals.append("actuals_for_last_completed_week_present")

    # Informational, never a suspect trigger.
    if boundary and last_actual and last_actual > boundary:
        signals.append("tab_runs_ahead_of_workbook")

    if prior_tab is None:
        signals.append("no_prior_snapshot")
    else:
        prior_last = prior_tab.get("last_actual_week_ending")
        if prior_last and last_actual and last_actual > prior_last:
            advanced = (
                datetime.date.fromisoformat(last_actual)
                - datetime.date.fromisoformat(prior_last)
            ).days // 7
            elapsed = None
            if prior_snapshot_date:
                elapsed = (today - prior_snapshot_date).days // 7
            if elapsed is not None and advanced > max(elapsed, 1):
                # More weeks closed than actually passed -- a backfill or a
                # hand edit, not the normal weekly rhythm.
                signals.append("boundary_jumped_more_than_elapsed")
            else:
                signals.append("boundary_advanced_normally")

    suspect = "actuals_for_last_completed_week_present" in signals
    return {
        "post_refresh_suspect": suspect,
        "roll_signals": signals,
        "expected_pre_refresh_boundary": (
            expected_pre.isoformat() if expected_pre else None
        ),
        "workbook_boundary": boundary,
    }


# ────────────────────────────────────────────────────────────────────────────
# Snapshot build
# ────────────────────────────────────────────────────────────────────────────

def sweepable_tabs() -> list[str]:
    """Every CF tab the ledger covers, excluded tabs removed. Stable order."""
    tabs = sorted(set(gf.ENTITY_TO_TAB.values()) - EXCLUDED_TABS)
    leaked = sorted(set(tabs) & EXCLUDED_TABS)
    if leaked:  # pragma: no cover -- defensive; the set difference above prevents it
        raise LedgerError(f"excluded tab reached the sweep list: {leaked}")
    return tabs


def entity_codes_for_tab(tab: str) -> list[str]:
    """Canonical entity codes that route to this tab (informational)."""
    return sorted(code for code, t in gf.ENTITY_TO_TAB.items() if t == tab)


def build_snapshot(
    tabs: list[str],
    *,
    read_vector: Callable[[str], gf.ForecastVector],
    today: Optional[datetime.date] = None,
    prior: Optional[dict] = None,
    full_scope: Optional[list[str]] = None,
) -> dict:
    """Read every tab and assemble the snapshot payload. Per-tab fail-soft.

    A tab that errors or parses UNKNOWN is RECORDED as such and does not abort
    the run -- but it is also not counted as covered (D-117), so a downstream
    reader can never mistake a missing tab for a flat one.
    """
    today = today or datetime.date.today()
    scope = list(full_scope if full_scope is not None else tabs)

    prior_tabs = (prior or {}).get("tabs") or {}
    prior_date: Optional[datetime.date] = None
    if (prior or {}).get("snapshot_date"):
        try:
            prior_date = datetime.date.fromisoformat(prior["snapshot_date"])
        except ValueError:
            prior_date = None

    # PASS 1 -- read every tab. Roll state cannot be judged until the whole
    # workbook is in hand (the boundary is a workbook fact, not a tab fact).
    vectors: dict[str, gf.ForecastVector] = {}
    unreadable: dict[str, str] = {}
    for tab in tabs:
        if tab in EXCLUDED_TABS:
            log.warning("refusing to collect excluded tab: %s", tab)
            continue
        try:
            vector = read_vector(tab)
        except Exception as exc:  # noqa: BLE001 -- one bad tab must not lose the week
            # Reason CODE, not str(exc): the raw googleapiclient HttpError text
            # carries the spreadsheet id and the request URI, and this payload is
            # mirrored into a shared accounting folder. Detail stays in the log.
            log.error("tab %s unreadable: %s", tab, exc)
            unreadable[tab] = "api_error"
            continue
        if not vector.ok:
            log.warning("tab %s UNKNOWN: %s", tab, vector.unknown_reason)
            unreadable[tab] = _reason_code(vector.unknown_reason)
            continue
        vectors[tab] = vector

    # A weekday disagreement means one tab's grid is off, not that the workbook
    # is unusable. Bank the majority and quarantine the outliers -- refusing the
    # whole week would throw away 18 good tabs permanently, which contradicts
    # the loss-criticality this store exists for.
    weekday_counts: dict[str, int] = {}
    for v in vectors.values():
        if v.week_ending_weekday:
            weekday_counts[v.week_ending_weekday] = (
                weekday_counts.get(v.week_ending_weekday, 0) + 1
            )
    majority_weekday = (
        max(weekday_counts.items(), key=lambda kv: kv[1])[0] if weekday_counts else None
    )
    if len(weekday_counts) > 1:
        for tab in [t for t, v in vectors.items()
                    if v.week_ending_weekday != majority_weekday]:
            log.error("tab %s disagrees on week-ending weekday (%s vs majority %s)",
                      tab, vectors[tab].week_ending_weekday, majority_weekday)
            unreadable[tab] = "weekday_disagreement"
            vectors.pop(tab)

    boundary = workbook_boundary(vectors)

    # PASS 2 -- classify against the workbook boundary.
    out_tabs: dict[str, dict] = {}
    for tab, vector in vectors.items():
        block = vector.as_dict()
        block.pop("tab", None)
        block["entity_codes"] = entity_codes_for_tab(tab)
        block.update(classify_roll_state(
            vector, prior_tabs.get(tab), today=today,
            boundary=boundary, prior_snapshot_date=prior_date,
        ))
        out_tabs[tab] = block

    covered = len(out_tabs)
    expected = len([t for t in scope if t not in EXCLUDED_TABS])

    # COVERAGE FLOOR. A zero-tab snapshot is not a snapshot -- writing one lets
    # the health check (which sees a dated file) report green on a total
    # failure. Refuse, leaving the previous file and the missed-run WARN intact.
    if covered == 0:
        raise LedgerError(
            f"no tab was readable (0 of {expected}) -- refusing to write an "
            "empty snapshot that would read as a banked week"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "snapshot_date": today.isoformat(),
        "basis": SNAPSHOT_BASIS,
        "week_ending_weekday": majority_weekday,
        "covered": covered,
        "expected": expected,
        "workbook_boundary": boundary,
        "partial_sweep": covered < expected and len(tabs) < len(scope),
        "prior_snapshot_date": (prior or {}).get("snapshot_date"),
        "excluded_tabs": sorted(EXCLUDED_TABS),
        "tabs": out_tabs,
        "unreadable_tabs": unreadable,
        "notes": [
            "Completed weeks carry basis=post_close_column_value: on this sheet a "
            "closed week's FORECAST cell holds the entered ACTUAL (D-121). Never "
            "read one as a forecast.",
            "Tabs stamped post_refresh_suspect were read AFTER the weekly refresh "
            "and are excluded from forecast-accuracy math.",
            "UNKNOWN is never zero. An unreadable tab is listed in unreadable_tabs "
            "and is not counted as covered.",
        ],
    }


def write_snapshot(
    snapshot: dict,
    *,
    overwrite: bool = False,
    today: Optional[datetime.date] = None,
) -> Path:
    """Write the snapshot atomically to the local store.

    REFUSES to replace an existing snapshot for the same date unless
    ``overwrite=True``. The documented exit-1 path (one unreadable tab) invites
    exactly the operator response -- re-run -- that would destroy the week: the
    06:15 pre-refresh capture gets overwritten by a mid-morning post-refresh
    read whose forecast cells now hold entered actuals. The stamp on the
    replacement would record that IT is unusable, but the good one is already
    gone. Overwriting is sometimes right; it must be deliberate.
    """
    date_str = snapshot.get("snapshot_date")
    if not date_str:
        raise LedgerError("snapshot has no snapshot_date")
    snap_date = datetime.date.fromisoformat(date_str)

    # A future-dated file (typo'd --date, clock skew) would become the store's
    # max date and blind the missed-run check for months.
    if snap_date > (today or datetime.date.today()):
        raise LedgerError(f"refusing to write a future-dated snapshot: {snap_date}")

    path = snapshot_path(snap_date)
    if path.exists() and not overwrite:
        raise LedgerError(
            f"{path.name} already exists. Re-running would replace a snapshot "
            "taken earlier today -- if that one was pre-refresh and this one is "
            "not, the week's real forecast is destroyed. Pass --overwrite if "
            "you are sure."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    # Process-unique tmp: a manual run overlapping the scheduled one would
    # otherwise race on one fixed path and land a half-written payload.
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def same_ignoring_stamps(left: str, right: str) -> bool:
    """Compare two snapshot payloads ignoring per-run timestamps.

    Without this the Drive mirror rewrites even when the sheet has not moved --
    pure churn on a network mount.
    """
    def _strip(text: str) -> Any:
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
        if isinstance(data, dict):
            data.pop("generated_at_utc", None)
        return json.dumps(data, sort_keys=True)

    return _strip(left) == _strip(right)
