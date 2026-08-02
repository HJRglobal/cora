"""Flywheel throughput metrics (WS-2) -- the knowledge loop's vital signs.

The North-Star knowledge flywheel silently flatlined for 2+ weeks in June 2026
(0 knowledge items DM'd to Harrison every weekday since ~6/23; the gap log dry
since 6/15; the graduated-trust shadow with zero records) and nothing alarmed:
the health checks watched tasks and heartbeats, not throughput. This module is
the single source both health surfaces consume --

  * scripts/nightly_health_check.py  (daily 8:45 AZ -> HEALTH_REPORT_CHANNEL)
  * scripts/cora_health_report.py    (Mon 09:30 AZ -> #cora-health)

so the thresholds can never drift between them (the _EXPECTED_DISABLED
false-CRITICAL of 2026-06-04 was exactly a two-copies-of-expected-state bug).

Everything here is READ-ONLY over ledgers/logs/state (the one exception is the
pending-size baseline history file, written only when update_baseline=True --
the nightly check passes True, the weekly report passes False). All failure
paths degrade to partial metrics; a broken data source never crashes a health
run.

First-week note (by design): these WILL warn while the WS-1 starvation fixes
bed in. That is correct behavior -- do not suppress.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Thresholds -- ONE constant block; both health surfaces read these (spec WS-2).
# ---------------------------------------------------------------------------

# WARN when zero knowledge items were DM'd to Harrison over this window.
WARN_ZERO_KNOWLEDGE_DMS_DAYS = 7
# WARN when the newest knowledge-gaps.jsonl entry is older than this.
# Recalibrated 7 -> 21 (Slice 6, 2026-07-29 audit): post-D-088 the LIVE intake is the
# `unknown_response` detector, which is naturally SPORADIC (a good week can log zero
# gaps). The 7d threshold cried wolf on normal lulls -- the 2026-07-27 weekly health
# post alarmed "gap intake may be dead" during a routine 2026-07-14 -> 07-28 gap, yet
# the detector was live (entries landed 7/28 + 7/29). 21d still catches a genuine
# multi-week plumbing regression (the June-2026 "dry since 6/15" outage) without firing
# on an ordinary quiet stretch. Do NOT drop below this to "catch it sooner" -- the
# sporadic signal makes a tighter window pure noise.
WARN_GAP_LOG_STALE_DAYS = 21
# WARN when the live ledger's PENDING count exceeds this.
WARN_PENDING_SIZE = 6_000
# WARN when PENDING grew by more than this over the last ~7 days.
WARN_PENDING_GROWTH_7D = 500
# Rolling window used for every 7d metric below.
_WINDOW_DAYS = 7
# Baseline history retention (days of daily pending-size snapshots).
_BASELINE_KEEP_DAYS = 21

# Knowledge-vs-operational split. Duplicated from run_knowledge_review.py
# (_KNOWLEDGE_TYPES + the generic/info-for-cora special case) because scripts/
# is not an importable package; tests/test_flywheel_metrics.py pins the two
# so they can never drift.
_KNOWLEDGE_TYPES = frozenset({"known_answer", "efficiency"})


def is_knowledge_item(update: dict) -> bool:
    ut = update.get("update_type")
    if ut in _KNOWLEDGE_TYPES:
        return True
    if ut == "generic":
        payload = update.get("payload") or {}
        return payload.get("source") == "info-for-cora"
    return False


# ---------------------------------------------------------------------------
# Paths (env overrides mirror the writers' overrides where they exist)
# ---------------------------------------------------------------------------

def _paths(repo_root: Path | None = None) -> dict[str, Path]:
    root = Path(repo_root) if repo_root else _REPO_ROOT
    return {
        "ledger_live": root / "data" / "cora-proposed-memory-updates.jsonl",
        "ledger_archive": root / "data" / "cora-proposed-memory-updates.archive.jsonl",
        "gaps_log": Path(os.environ.get("KNOWLEDGE_GAPS_LOG_PATH")
                         or root / "logs" / "knowledge-gaps.jsonl"),
        "gap_detection_state": Path(os.environ.get("GAP_DETECTION_STATE_PATH")
                                    or root / "data" / "state" / "gap_detection_state.json"),
        "gap_autofill_state": Path(os.environ.get("GAP_AUTOFILL_STATE_PATH")
                                   or root / "data" / "state" / "gap_autofill_state.json"),
        "shadow_dir": Path(os.environ.get("CORA_GRADUATED_SHADOW_DIR")
                           or root / "logs"),
        "baseline": root / "data" / "health-flywheel-baseline.json",
        # Fork 5a (Wave-1 flywheel-conversion calibration, 2026-07-30):
        "resolved_gaps": Path(os.environ.get("RESOLVED_GAPS_PATH")
                              or root / "design" / "known-answers" / ".resolved-gaps.jsonl"),
        "code_queue_ledger": root / "data" / "state" / "code-session-queue.jsonl",
        "t0_baseline": root / "data" / "state" / "flywheel-t0-baseline.json",
    }


def _iter_jsonl(path: Path):
    """Yield parsed dict records from a JSONL file; malformed lines and
    valid-JSON-but-non-dict lines (a bare 'null'/'[]' from a partial write) are
    skipped -- one bad line must never abort a whole gauge scan (adversarial
    review LOW)."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _parse_slack_ts(value) -> datetime | None:
    """dm_message_ts is a Slack epoch-seconds string like '1782136811.404859'."""
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect(now: datetime | None = None, repo_root: Path | None = None,
            update_baseline: bool = False) -> dict:
    """Gather the flywheel metrics dict. Never raises; degrades per-section."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=_WINDOW_DAYS)
    root = Path(repo_root) if repo_root else _REPO_ROOT
    p = _paths(repo_root)
    out: dict = {"available": True}

    # -- Ledger scan (live + archive, one pass each) --------------------------
    knowledge_dms_7d = 0
    pending_total = 0
    proposed_7d = 0
    resolved_7d = 0
    expired_unrouted_7d = 0
    routed_7d = 0
    try:
        # A rotation crash window can leave the same row in BOTH files (archive
        # is appended first, live rewritten second) -- dedup by update_id so
        # the 7d gauges never double-count (adversarial review LOW). First
        # occurrence (live file) wins.
        counted_ids: set[str] = set()
        for path in (p["ledger_live"], p["ledger_archive"]):
            for rec in _iter_jsonl(path):
                uid = rec.get("update_id") or ""
                if uid:
                    if uid in counted_ids:
                        continue
                    counted_ids.add(uid)
                if path is p["ledger_live"] and rec.get("state") == "PENDING":
                    pending_total += 1
                proposed_at = _parse_iso(rec.get("proposed_at") or "")
                if proposed_at and proposed_at >= cutoff:
                    proposed_7d += 1
                resolved_at = _parse_iso(rec.get("resolved_at") or "")
                if resolved_at and resolved_at >= cutoff:
                    resolved_7d += 1
                    reason = rec.get("resolved_reason") or ""
                    if reason == "expired_unrouted":
                        expired_unrouted_7d += 1
                    elif reason.startswith("routed_to_owner:"):
                        routed_7d += 1
                if is_knowledge_item(rec):
                    dm_at = _parse_slack_ts(rec.get("dm_message_ts"))
                    if dm_at and dm_at >= cutoff:
                        knowledge_dms_7d += 1
        out.update(
            knowledge_dms_7d=knowledge_dms_7d,
            pending_total=pending_total,
            proposed_7d=proposed_7d,
            resolved_7d=resolved_7d,
            routed_to_owner_7d=routed_7d,
            expired_unrouted_7d=expired_unrouted_7d,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("flywheel_metrics: ledger scan failed: %s", exc)
        out["ledger_error"] = str(exc)

    # -- Gap log freshness -----------------------------------------------------
    try:
        last_ts: datetime | None = None
        total = 0
        by_detector: dict[str, int] = {}
        # NOTE (D-066 follow-up): kb_miss=0 here is EXPECTED for now, not a
        # defect -- the kb_miss detector requires 0 chunks under the 1.30 gate,
        # empirically unreachable at ~560K chunks. unknown_response carries the
        # deterministic intake; kb_miss stays a dead backstop until it is
        # recalibrated to a distance FLOOR from the best_distance data now on
        # each gap record. Do not "fix" a kb_miss=0 breakdown.
        for rec in _iter_jsonl(p["gaps_log"]):
            total += 1
            det = rec.get("detector") or "llm_sentinel"
            by_detector[det] = by_detector.get(det, 0) + 1
            ts = _parse_iso(rec.get("ts") or "")
            if ts and (last_ts is None or ts > last_ts):
                last_ts = ts
        out["gaps_total"] = total
        out["gaps_by_detector"] = by_detector
        out["gaps_last_entry_age_days"] = (
            round((now - last_ts).total_seconds() / 86400, 1) if last_ts else None
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("flywheel_metrics: gaps log scan failed: %s", exc)
        out["gaps_error"] = str(exc)

    # -- Today's deterministic-detection counters ------------------------------
    try:
        state = json.loads(p["gap_detection_state"].read_text(encoding="utf-8"))
        if state.get("day") == now.strftime("%Y-%m-%d"):
            out["gaps_detected_today"] = int(state.get("count") or 0)
            out["gaps_overflow_today"] = int(state.get("overflow") or 0)
        else:
            out["gaps_detected_today"] = 0
            out["gaps_overflow_today"] = 0
    except Exception:
        out["gaps_detected_today"] = 0
        out["gaps_overflow_today"] = 0

    # -- gap_autofill mined+proposed, 7d ---------------------------------------
    try:
        state = json.loads(p["gap_autofill_state"].read_text(encoding="utf-8"))
        mined = 0
        for entry in state.values():
            if not isinstance(entry, dict) or entry.get("state") != "proposed":
                continue
            at = _parse_iso(entry.get("at") or "")
            if at and at >= cutoff:
                mined += 1
        out["gap_autofill_proposed_7d"] = mined
    except Exception:
        out["gap_autofill_proposed_7d"] = 0

    # -- Graduated-trust shadow accrual (the flip-readiness gauge, D-063) ------
    try:
        records = 0
        days: set[str] = set()
        for f in sorted(p["shadow_dir"].glob("graduated-trust-shadow-*.jsonl")):
            n = sum(1 for _ in _iter_jsonl(f))
            if n:
                records += n
                days.add(f.stem.replace("graduated-trust-shadow-", ""))
        out["shadow_records"] = records
        out["shadow_days"] = len(days)
    except Exception:
        out["shadow_records"] = 0
        out["shadow_days"] = 0

    # -- PENDING growth vs baseline history ------------------------------------
    out["pending_growth_7d"] = None
    try:
        baseline_path = p["baseline"]
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except Exception:
            baseline = {}
        history: dict[str, int] = dict(baseline.get("history") or {})
        today_key = now.strftime("%Y-%m-%d")
        if update_baseline and "pending_total" in out:
            history[today_key] = out["pending_total"]
            keep_cutoff = (now - timedelta(days=_BASELINE_KEEP_DAYS)).strftime("%Y-%m-%d")
            history = {k: v for k, v in history.items() if k >= keep_cutoff}
            baseline_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = baseline_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"history": history}, indent=1),
                           encoding="utf-8")
            tmp.replace(baseline_path)
        # Growth = today's pending minus the oldest snapshot within the window.
        window_keys = sorted(
            k for k in history
            if k >= (now - timedelta(days=_WINDOW_DAYS)).strftime("%Y-%m-%d")
            and k != today_key
        )
        if window_keys and "pending_total" in out:
            out["pending_growth_7d"] = out["pending_total"] - history[window_keys[0]]
        out["baseline_days"] = len(history)
    except Exception as exc:  # noqa: BLE001
        log.warning("flywheel_metrics: baseline handling failed: %s", exc)

    # -- Fork 5a (Wave-1 flywheel-conversion calibration, 2026-07-30) ----------
    # Conversion-of-ELIGIBLE rate, per lane -- not absolute volume (inflow is
    # ~1-3/wk; an absolute-N target manufactures false failure at this scale).
    # Code-queue routings are tracked under a SEPARATE denominator, never folded
    # into the knowledge-conversion numerator (see section 0 framing in the
    # 2026-07-30 handoff doc). Fail-soft: any error here degrades this section
    # only, never the metrics already collected above.
    try:
        out.update(_collect_wave1_conversion_metrics(cutoff, p, root))
    except Exception as exc:  # noqa: BLE001
        log.warning("flywheel_metrics: wave1 conversion metrics failed: %s", exc)
        out["wave1_conversion_error"] = str(exc)

    return out


def _collect_wave1_conversion_metrics(
    cutoff: datetime, p: dict[str, Path], root: Path,
) -> dict:
    """Fork 5a gauges: eligible-signal inflow, per-lane knowledge conversions,
    code-queue routing-completeness (separate denominator), gap routing-
    completeness (>7d-old gaps that have SOME disposition vs none), a
    read-through of the STEP-0 T0 baseline snapshot if present, and Fork 1's
    quality-rejection aggregation. Never raises -- the caller wraps this in a
    try/except, but each sub-section is independently fail-soft too so one
    broken source never blanks the rest.

    D-051 adversarial review LOW: `_ga` (gap_autofill) MUST be genuinely used
    below, not just imported -- an unused defensive import at the top of this
    function would blank ALL FIVE Fork-5a gauges if that import ever failed,
    for no behavioral benefit."""
    from . import gap_autofill as _ga
    from . import knowledge_gaps as _kg

    out: dict = {}

    # Eligible-signal inflow, 7d: new gap-log rows that are NOT LEX (walled) and
    # NOT capability-ask-shaped (post-Fork-3a these no longer even reach the gap
    # log, but the check stays as defense-in-depth for any pre-fix stragglers).
    # ACCEPTED APPROXIMATION (documented, not silent): this does not attempt to
    # detect the finance / DM-test walled classes -- no reliable deterministic
    # signal exists for those, and STEP 0's one-time hand triage is authoritative
    # for the T0 denominator. This is a LEADING INDICATOR, not an exact count.
    try:
        eligible_inflow = 0
        for rec in _iter_jsonl(p["gaps_log"]):
            ts = _parse_iso(rec.get("ts") or "")
            if not ts or ts < cutoff:
                continue
            entity = str(rec.get("entity") or "FNDR").strip().upper()
            if entity.startswith("LEX"):
                continue
            if _kg.is_capability_ask(rec.get("question") or ""):
                continue
            eligible_inflow += 1
        out["eligible_signal_inflow_7d"] = eligible_inflow
    except Exception:  # noqa: BLE001
        out["eligible_signal_inflow_7d"] = None

    # Per-lane knowledge conversions, 7d -- state=="APPROVED" (Harrison's
    # thumbs-up) within the window, split by update_type/answer_source. Code-
    # queue routings NEVER count here (tracked separately below).
    conversions = {
        "known_answer_mined": 0, "known_answer_escalation_asker": 0,
        "friction_efficiency": 0,
        # Fork 4 (decisions lane) is NOT built in Wave 1 -- always 0 until its
        # own dedicated session ships; kept here so the shape is stable for the
        # 2-week review from day one.
        "decision_staged": 0,
        # Lexicon Flywheel (2026-08-01): mined = lane A/B proposals approved;
        # taught = cora_lexicon_add teaches (incl. founder fast-path, which
        # records an APPROVED row). Keys present from day one (stable shape).
        "lexicon_mined": 0, "lexicon_taught": 0,
    }
    try:
        for path in (p["ledger_live"], p["ledger_archive"]):
            for rec in _iter_jsonl(path):
                if rec.get("state") != "APPROVED":
                    continue
                resolved_at = _parse_iso(rec.get("resolved_at") or "")
                if not resolved_at or resolved_at < cutoff:
                    continue
                ut = rec.get("update_type")
                if ut == "known_answer":
                    payload = rec.get("payload") or {}
                    if payload.get("answer_source") == "teammate_dm":
                        conversions["known_answer_escalation_asker"] += 1
                    else:
                        conversions["known_answer_mined"] += 1
                elif ut == "efficiency":
                    conversions["friction_efficiency"] += 1
                elif ut == "lexicon":
                    payload = rec.get("payload") or {}
                    if (payload.get("lane") or "") == "taught":
                        conversions["lexicon_taught"] += 1
                    else:
                        conversions["lexicon_mined"] += 1
        out["conversions_by_lane_7d"] = conversions
    except Exception:  # noqa: BLE001
        out["conversions_by_lane_7d"] = conversions

    # Lexicon inflow + resolver telemetry, 7d (leading indicators; None = source
    # unreadable, and the render line is suppressed -- never a fake zero).
    try:
        out["lexicon_candidates_7d"] = _lexicon_candidates_7d(cutoff)
    except Exception:  # noqa: BLE001
        out["lexicon_candidates_7d"] = None
    try:
        out["lexicon_resolver_7d"] = _lexicon_resolver_7d(cutoff)
    except Exception:  # noqa: BLE001
        out["lexicon_resolver_7d"] = None

    # Code-queue routing, 7d -- a SEPARATE denominator (routing-completeness),
    # never folded into the knowledge numerator above.
    try:
        capability_routed = 0
        for rec in _iter_jsonl(p["code_queue_ledger"]):
            if rec.get("event") != "captured" or rec.get("signal") != "capability":
                continue
            ts = _parse_iso(rec.get("ts") or "")
            if ts and ts >= cutoff:
                capability_routed += 1
        out["code_queue_capability_routed_7d"] = capability_routed
    except Exception:  # noqa: BLE001
        out["code_queue_capability_routed_7d"] = None

    # Gap routing-completeness: of every gap-log row older than 7d, how many
    # have SOME disposition (resolved -- answered/expired/capability_routed --
    # or an autofill state entry -- proposed/asked/exhausted) vs none at all
    # (silently rotting, the state Fork 5 must make visible).
    try:
        resolved_ids = _resolved_ids_from(p["resolved_gaps"])
        state_ids = set(_json_object(p["gap_autofill_state"]).keys())
        total_over_7d = routed = 0
        for rec in _iter_jsonl(p["gaps_log"]):
            ts = _parse_iso(rec.get("ts") or "")
            if not ts or ts >= cutoff:
                continue
            total_over_7d += 1
            gid = rec.get("ts", "")
            if gid in resolved_ids or gid in state_ids:
                routed += 1
        out["gap_routing_completeness_7d"] = {
            "total_over_7d": total_over_7d, "routed": routed,
            "rotting": max(0, total_over_7d - routed),
        }
    except Exception:  # noqa: BLE001
        out["gap_routing_completeness_7d"] = None

    # T0 baseline read-through (written once by STEP 0's triage script) -- lets
    # the 2-week review compute a real before/after delta. None if not yet run.
    try:
        out["t0_baseline"] = _json_object(p["t0_baseline"]) or None
    except Exception:  # noqa: BLE001
        out["t0_baseline"] = None

    # Fork 1 (2026-07-30): wire the rejection-log aggregation into the shared
    # metrics source so it actually reaches the digest, per Fork 1's own stated
    # goal ("so the 2-week review can read it as a summary instead of grepping
    # logs by hand") -- D-051 review LOW: the function existed but had zero
    # production callers until this line.
    try:
        out["quality_rejections_14d"] = _ga.aggregate_quality_rejections(
            days=14, repo_root=root)
    except Exception:  # noqa: BLE001
        out["quality_rejections_14d"] = None

    return out


def _lexicon_candidates_7d(cutoff: datetime) -> int:
    """Lexicon inflow gauge: NEW candidate rows (lane-B no-canonical ledger) +
    NEW proposal fingerprints within the window."""
    from .lexicon_mining import _candidates_path, _fingerprints_path
    cutoff_epoch = cutoff.timestamp()
    count = 0
    for path, key in ((_candidates_path(), "seen_at"),
                      (_fingerprints_path(), "proposed_at")):
        for rec in _iter_jsonl(path):
            try:
                stamp = datetime.strptime(str(rec.get(key) or ""),
                                          "%Y-%m-%dT%H:%M:%S")
                if stamp.timestamp() >= cutoff_epoch:
                    count += 1
            except Exception:  # noqa: BLE001
                continue
    return count


def _lexicon_resolver_7d(cutoff: datetime) -> dict:
    """Resolver chokepoint telemetry counts by status within the window."""
    from .lexicon import _log_path
    cutoff_epoch = cutoff.timestamp()
    counts = {"exact": 0, "ambiguous": 0, "suggestion": 0, "miss": 0,
              "confirmed": 0}
    for rec in _iter_jsonl(_log_path()):
        if rec.get("ts", 0) < cutoff_epoch:
            continue
        status = str(rec.get("status") or "")
        if status in counts:
            counts[status] += 1
    return counts


def _resolved_ids_from(path: Path) -> set[str]:
    return {str(rec.get("id")) for rec in _iter_jsonl(path) if rec.get("id")}


def _json_object(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Evaluation -- shared alarm logic
# ---------------------------------------------------------------------------

def evaluate(metrics: dict) -> list[tuple[str, str]]:
    """Return (severity, message) pairs. Severity is 'warn' only -- flywheel
    degradation is never a CRITICAL (that would flip the scheduled task's
    Last Result nonzero for a throughput dip; reserve critical for outages)."""
    alarms: list[tuple[str, str]] = []
    kd = metrics.get("knowledge_dms_7d")
    if kd == 0:
        alarms.append((
            "warn",
            f"0 knowledge items DM'd to Harrison in {WARN_ZERO_KNOWLEDGE_DMS_DAYS}d "
            "-- the learning loop is starved",
        ))
    age = metrics.get("gaps_last_entry_age_days")
    if age is None and "gaps_error" not in metrics:
        alarms.append(("warn", "knowledge-gaps.jsonl has no entries"))
    elif age is not None and age > WARN_GAP_LOG_STALE_DAYS:
        alarms.append((
            "warn",
            f"no new knowledge-gaps in {age:.0f}d "
            f"(threshold {WARN_GAP_LOG_STALE_DAYS}d) -- verify the unknown_response "
            f"gap detector is still wired (it may just be a quiet stretch)",
        ))
    pending = metrics.get("pending_total")
    if isinstance(pending, int) and pending > WARN_PENDING_SIZE:
        alarms.append((
            "warn",
            f"proposed-updates ledger PENDING={pending:,} exceeds {WARN_PENDING_SIZE:,}",
        ))
    growth = metrics.get("pending_growth_7d")
    if isinstance(growth, int) and growth > WARN_PENDING_GROWTH_7D:
        alarms.append((
            "warn",
            f"PENDING grew +{growth:,} in ~7d (threshold +{WARN_PENDING_GROWTH_7D}) "
            "-- producers outrunning the drain",
        ))
    return alarms


def format_lines(metrics: dict) -> list[str]:
    """Human-readable metric lines shared by both reports (ASCII-safe)."""
    if not metrics.get("available"):
        return ["flywheel metrics unavailable"]
    det = metrics.get("gaps_by_detector") or {}
    det_str = ", ".join(f"{k}={v}" for k, v in sorted(det.items())) or "none"
    age = metrics.get("gaps_last_entry_age_days")
    lines = [
        f"knowledge items DM'd to Harrison, 7d: {metrics.get('knowledge_dms_7d', '?')}",
        f"gap log: {metrics.get('gaps_total', '?')} total ({det_str}); "
        + ("no entries" if age is None else f"newest {age:.1f}d old")
        + f"; today detected={metrics.get('gaps_detected_today', 0)}"
          f" overflow={metrics.get('gaps_overflow_today', 0)}",
        f"gap_autofill mined+proposed, 7d: {metrics.get('gap_autofill_proposed_7d', '?')}",
        f"shadow records (flip gauge): {metrics.get('shadow_records', 0)} across "
        f"{metrics.get('shadow_days', 0)} day(s)",
        f"ledger: PENDING={metrics.get('pending_total', '?')}"
        + (f" (7d growth {metrics.get('pending_growth_7d'):+,})"
           if isinstance(metrics.get("pending_growth_7d"), int) else " (growth n/a)"),
        f"producer vs drain, 7d: proposed={metrics.get('proposed_7d', '?')} vs "
        f"resolved={metrics.get('resolved_7d', '?')} "
        f"(routed={metrics.get('routed_to_owner_7d', '?')}, "
        f"expired_unrouted={metrics.get('expired_unrouted_7d', '?')})",
    ]
    # Fork 5a (Wave-1 flywheel-conversion calibration): conversion-of-eligible,
    # per lane -- surfaced alongside the leading inflow indicator so a low
    # conversion count is read against available supply, not in a vacuum.
    conv = metrics.get("conversions_by_lane_7d")
    if conv is not None:
        conv_str = ", ".join(f"{k}={v}" for k, v in conv.items())
        lines.append(
            f"eligible-signal inflow, 7d: {metrics.get('eligible_signal_inflow_7d', '?')} "
            f"| knowledge conversions by lane, 7d: {conv_str}"
        )
    # Lexicon Flywheel: resolver traffic + candidate inflow (suppressed when
    # the sources are unreadable -- Fork 5 rules: a quiet week renders zeros,
    # never STARVED; a broken source renders nothing, never a fake zero).
    lex_res = metrics.get("lexicon_resolver_7d")
    lex_cand = metrics.get("lexicon_candidates_7d")
    if lex_res is not None or lex_cand is not None:
        res_str = (", ".join(f"{k}={v}" for k, v in lex_res.items())
                   if lex_res is not None else "n/a")
        lines.append(
            f"lexicon, 7d: resolver [{res_str}] | candidate inflow: "
            f"{lex_cand if lex_cand is not None else 'n/a'}"
        )
    cq_routed = metrics.get("code_queue_capability_routed_7d")
    if cq_routed is not None:
        lines.append(
            f"code-queue capability routings, 7d: {cq_routed} "
            "(separate denominator -- NOT a knowledge conversion)"
        )
    routing = metrics.get("gap_routing_completeness_7d")
    if routing is not None:
        lines.append(
            f"gap routing-completeness (>7d old): {routing['routed']}/"
            f"{routing['total_over_7d']} routed, {routing['rotting']} rotting"
        )
    t0 = metrics.get("t0_baseline")
    if t0:
        lines.append(
            f"T0 baseline ({t0.get('computed_at', '?')}): "
            f"eligible_open={t0.get('eligible_open_count', '?')} "
            f"disposition={t0.get('disposition_counts', {})}"
        )
    qr = metrics.get("quality_rejections_14d")
    if qr:
        lines.append(
            f"MINE draft rejections, 14d: {qr.get('total_rejections', 0)} "
            f"({qr.get('by_reason', {})}, time_decaying={qr.get('time_decaying', 0)})"
        )
    return lines
