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


def latest_snapshot_date(before: Optional[datetime.date] = None) -> Optional[datetime.date]:
    """Most recent banked snapshot date, optionally strictly before ``before``."""
    dates = [d for d in list_snapshot_dates() if before is None or d < before]
    return dates[-1] if dates else None


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


def classify_roll_state(
    vector: gf.ForecastVector,
    prior_tab: Optional[dict],
    *,
    today: datetime.date,
) -> dict:
    """Decide whether this tab was read BEFORE or AFTER the weekly refresh.

    "Before Justin's refresh" is an assumption, not a guarantee (Fin-5/Mig-3):
    the observed 8/3 refresh landed Monday ~5:30 PM, Hayden co-edits, and the SOP
    only says "Mon AM". A snapshot taken after the refresh holds an ENTERED
    ACTUAL in the cell a forecast-accuracy calculation would read as a forecast,
    so it must be excluded from that math rather than silently averaged in.

    TWO independent signals, because either alone has a blind spot:

      * ABSOLUTE (calendar): actuals for the most recently completed week are
        already present. Works with no history at all -- which matters, because
        the design's relative-only rule would leave the very FIRST snapshot
        (taken manually at merge, mid-week, necessarily post-refresh) unstamped
        and therefore trusted for accuracy math it cannot support.
      * RELATIVE (vs the prior snapshot): the actual boundary advanced, or the
        week grid rolled, since we last looked.

    Suspect if ANY fires. Signals are recorded individually so a reader can see
    which one tripped.
    """
    signals: list[str] = []

    expected_pre: Optional[datetime.date] = None
    if vector.week_ending_weekday:
        expected_pre = last_completed_week_ending(vector.week_ending_weekday, today)

    last_actual = vector.last_actual_week_ending
    if expected_pre is not None and last_actual:
        if last_actual >= expected_pre.isoformat():
            signals.append("actuals_for_last_completed_week_present")

    if prior_tab is None:
        signals.append("no_prior_snapshot")
    else:
        prior_last = prior_tab.get("last_actual_week_ending")
        if prior_last and last_actual and last_actual > prior_last:
            signals.append("last_actual_advanced_since_prior")
        prior_forward = list(prior_tab.get("forward_week_endings") or [])
        if prior_forward and vector.forward_week_endings:
            if prior_forward[-1] != vector.forward_week_endings[-1]:
                signals.append("week_grid_rolled_since_prior")

    # "no_prior_snapshot" alone is not evidence of a refresh -- it is absence of
    # evidence. Only the three real signals mark a snapshot unusable for accuracy.
    suspect = any(
        s in signals for s in (
            "actuals_for_last_completed_week_present",
            "last_actual_advanced_since_prior",
            "week_grid_rolled_since_prior",
        )
    )
    return {
        "post_refresh_suspect": suspect,
        "roll_signals": signals,
        "expected_pre_refresh_boundary": (
            expected_pre.isoformat() if expected_pre else None
        ),
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
    out_tabs: dict[str, dict] = {}
    unreadable: dict[str, str] = {}
    weekdays: set[str] = set()

    for tab in tabs:
        if tab in EXCLUDED_TABS:
            log.warning("refusing to collect excluded tab: %s", tab)
            continue
        try:
            vector = read_vector(tab)
        except Exception as exc:  # noqa: BLE001 -- one bad tab must not lose the week
            log.error("tab %s unreadable: %s", tab, exc)
            unreadable[tab] = str(exc)
            continue

        if not vector.ok:
            unreadable[tab] = vector.unknown_reason or "UNKNOWN"
            continue

        roll = classify_roll_state(vector, prior_tabs.get(tab), today=today)
        if vector.week_ending_weekday:
            weekdays.add(vector.week_ending_weekday)

        block = vector.as_dict()
        block.pop("tab", None)
        block["entity_codes"] = entity_codes_for_tab(tab)
        block.update(roll)
        out_tabs[tab] = block

    covered = len(out_tabs)
    expected = len([t for t in scope if t not in EXCLUDED_TABS])

    if len(weekdays) > 1:
        # Tabs disagreeing on the week-ending day means the workbook is not one
        # coherent grid; refuse rather than bank a snapshot nothing can join.
        raise LedgerError(
            f"tabs disagree on the week-ending weekday: {sorted(weekdays)}"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "snapshot_date": today.isoformat(),
        "basis": SNAPSHOT_BASIS,
        "week_ending_weekday": next(iter(weekdays), None),
        "covered": covered,
        "expected": expected,
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


def write_snapshot(snapshot: dict) -> Path:
    """Write the snapshot atomically to the local store."""
    date_str = snapshot.get("snapshot_date")
    if not date_str:
        raise LedgerError("snapshot has no snapshot_date")
    path = snapshot_path(datetime.date.fromisoformat(date_str))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
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
