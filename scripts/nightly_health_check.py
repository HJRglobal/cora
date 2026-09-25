#!/usr/bin/env python3
"""Nightly Cora health check — checks all systems, auto-fixes what it can,
reports everything to Slack.

Runs at 8:45am AZ daily (after all nightly jobs finish) via Task Scheduler
as "cowork-cora-health-check".

Checks:
  1.  Cora service heartbeat (alive / stale)
  2.  All 24 scheduled tasks (Ready / Running / failed)
  3.  Log scanning — ERRORs, critical patterns across last 24h logs
  4.  KB database health — chunk counts by source vs yesterday baseline
  5.  API connectivity — Slack, Asana, HubSpot, Notion, Anthropic, OpenAI
  6.  Google Service Account JSON — file exists and parseable
  7.  Environment variables — all required vars present
  8.  Disk space — warn if C: < 5 GB free

Auto-fixes (applied immediately, included in report):
  • Stale Cora heartbeat → orphan-kill + restart service

Detections that WARN rather than auto-fix:
  • A scheduled task stuck in state "Running" for >2h. The docstring used to
    claim this was auto-fixed ("mark stuck, restart"); no such code has ever
    existed, and "Running" was scored OK by the task classifier, so the state
    was invisible. It is now surfaced -- but restarting someone's scheduled task
    is an irreversible action on a live host and stays Harrison's call, so this
    warns and names the task rather than acting (C5 / cq-7bac8008b140).

Report: posted to #cora-health (or HEALTH_REPORT_CHANNEL env var)
  ✅  OK  |  ⚠️  Warning  |  ❌  Critical  |  🔧  Auto-fixed

Manual run:
    python scripts/nightly_health_check.py [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

# Windows: spawn helper processes without a console window.
#
# A task wrapped by deployment/run_hidden.py already runs with a windowless
# console that children inherit, so this is defence-in-depth -- it also covers
# a manual run, an unwrapped task, and any future caller whose parent has no
# console at all (where a console child would get a BRAND NEW visible window).
# 0 where the constant does not exist (POSIX), so behaviour is unchanged there.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

_LOG_DIR   = _REPO_ROOT / "logs"
_KB_DB     = _REPO_ROOT / "data" / "cora_kb.db"
_BASELINE  = _REPO_ROOT / "data" / "health-kb-baseline.json"
_HEALTH_CH = os.environ.get("HEALTH_REPORT_CHANNEL", "hjrg-leadership")

# ── Severity ──────────────────────────────────────────────────────────────────

Status = Literal["ok", "warn", "critical", "fixed"]

_EMOJI = {"ok": "✅", "warn": "⚠️", "critical": "❌", "fixed": "🔧"}


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    fix_applied: str = ""


# ── Tasks expected to be in each state ────────────────────────────────────────

# Tasks intentionally Disabled (Harrison-directed). Single source of truth:
# data/maps/scheduled-task-state.yaml (loaded by _load_task_state_config below).
def _load_task_state_config() -> tuple[set[str], set[str]]:
    """Load intended (disabled, running) task-name sets from config (audit N8).

    Single source of truth: data/maps/scheduled-task-state.yaml. Reconcile it
    when you enable/disable a task. Falls back to a minimal safe default
    (service expected running, nothing expected disabled) so a missing or broken
    config never turns a benign state into a CRITICAL.
    """
    path = _REPO_ROOT / "data" / "maps" / "scheduled-task-state.yaml"
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        disabled = {str(x) for x in (data.get("disabled") or [])}
        running = {str(x) for x in (data.get("running") or [])} or {"cowork-cora-service"}
        return disabled, running
    except Exception:
        return set(), {"cowork-cora-service"}


# Intended scheduled-task state. NOTE (2026-06-18): "Cora - Meeting Action
# Capture" is now intended-DISABLED -- the PUSH auto-create model was retired in
# favor of the meeting_action_items PULL tool (supersedes the D-052 "ENABLED"
# note). A disabled-state drift is a WARNING, never a CRITICAL -- only the
# always-on service being down is CRITICAL.
def _load_run_marker_registry() -> list[dict]:
    """The `run_markers:` section of the SAME task-state YAML (session #11 S4).

    A FOCUSED accessor rather than an extra return value on
    _load_task_state_config: that function's 2-tuple shape is pinned by
    tests/test_nightly_health_check.py and unpacked at module import, so widening
    its arity would break existing pins for no gain. Same file, same single
    source of truth -- there is no second registry to drift against.

    Fail-soft: a missing or malformed section yields an empty registry, so a
    config problem can never turn a healthy fleet into alarms.
    """
    path = _REPO_ROOT / "data" / "maps" / "scheduled-task-state.yaml"
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = data.get("run_markers") or []
        return [e for e in entries if isinstance(e, dict) and e.get("name")]
    except Exception:
        return []


_EXPECTED_DISABLED, _EXPECTED_RUNNING = _load_task_state_config()

# Friendly labels for the report
_TASK_LABELS: dict[str, str] = {
    "Cora - Daily Briefing":         "Daily briefing",
    "Cora - Drive Sweep":            "Drive sweep",
    "Cora - Email Attachment Filer": "Email filer",
    "Cora - LinkedIn Spy":           "LinkedIn spy",
    "cowork-cora-backup":            "Backup",
    "cowork-cora-channel-sweep":     "Channel sweep",
    "cowork-cora-completion-sweep":  "Completion sweep",
    "cowork-cora-decision-capture":  "Decision capture",
    "cowork-cora-digest":            "Daily digest",
    "cowork-cora-feedback-health":   "Feedback health",
    "cowork-cora-gap-digest":        "Gap digest",
    "cowork-cora-influencer-scan":   "Influencer scan",
    "cowork-cora-kb-sync-asana":     "KB sync: Asana",
    "cowork-cora-kb-sync-drive":     "KB sync: Drive",
    "cowork-cora-kb-sync-fireflies": "KB sync: Fireflies",
    "cowork-cora-kb-sync-gmail":     "KB sync: Gmail",
    "cowork-cora-kb-sync-notion":    "KB sync: Notion",
    "cowork-cora-kb-sync-slack":     "KB sync: Slack",
    "cowork-cora-kb-sync-static":    "KB sync: Static MD",
    "cowork-cora-knowledge-review":  "Knowledge review",
    "cowork-cora-proactive-gaps":    "Proactive gaps",
    "cowork-cora-qbo-token-refresh": "QBO token refresh",
    "cowork-cora-reconciliation":    "Reconciliation",
    "cowork-cora-security-monitor":  "Security monitor",
    "cowork-cora-service":           "Cora service",
    "cowork-cora-health-check":      "Health check",
    "cowork-cora-feedback-health":   "Feedback health",
    "cowork-cora-proactive-gaps":    "Proactive gaps",
}

# Required env vars — subset that would break Cora if missing. The Asana PAT key
# is NOT listed here: which key is required depends on CORA_ASANA_IDENTITY
# (S-B, 2026-09-19) -- see _required_env_vars(), which appends the ACTIVE
# identity's key (ASANA_PAT for "harrison", ASANA_PAT_CORA for "cora").
_REQUIRED_ENV_VARS = [
    "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY",
    "NOTION_API_KEY", "OPENAI_API_KEY",
    "FIREFLIES_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GSHEETS_CASHFLOW_FILE_ID", "HUBSPOT_PRIVATE_APP_TOKEN",
    "SHOPIFY_F3E_ACCESS_TOKEN",
]


def _required_env_vars() -> list[str]:
    """The static list plus the ACTIVE Asana identity's PAT key name.

    Identity-aware by construction: after Harrison flips CORA_ASANA_IDENTITY=cora
    the check demands ASANA_PAT_CORA (and stops demanding ASANA_PAT, which he
    removes on day 14). An unrecognised flag value raises AsanaIdentityError;
    check_env_vars() turns that into a CRITICAL naming the flag, key names only.
    """
    from cora import asana_identity  # noqa: PLC0415
    return [*_REQUIRED_ENV_VARS, asana_identity.active_pat_key()]

# Critical log patterns — any match flags the log
_CRITICAL_LOG_PATTERNS = [
    r"ImportError",
    r"ModuleNotFoundError",
    r"UnicodeDecodeError",
    # cq-b2dee156caee (session #11 S6): was r"\bFATAL\b", which MATCHES the word
    # "non-fatal" -- the hyphen is a word boundary. Every health-ping failure logs
    # "ping failed (non-fatal)", so a benign, self-healing blip was escalated to a
    # CRITICAL health alarm. Verified: this was the ONLY one of the nine patterns
    # carrying the hazard (the others are multi-word). The lookbehind must stay
    # FIXED-WIDTH -- these patterns are joined into one alternation and compiled
    # with re.IGNORECASE -- and (?<![-\w]) is width 1 and succeeds at position 0.
    r"(?<![-\w])FATAL\b",
    r"Socket Mode disconnect",
    r"connection refused",
    r"SLACK_BOT_TOKEN.*invalid",
    r"API key.*invalid",
    # The liveness sentinel's own writer failing repeatedly (cq-7915a8647cff): the
    # watchdog and this check both trust heartbeat.txt, so a persistent write
    # failure is an outage of the signal, not a warning.
    r"HEARTBEAT_FILE_WRITE_FAILING",
    # Session #11 S4: the run-marker writer failing silently would convert
    # "this task wrote nothing" into "this task never ran" -- the exact
    # ambiguity the marker contract exists to remove. Same precedent as the
    # heartbeat token above: observability that can fail quietly is worse than
    # none, so its failure is itself a CRITICAL.
    r"RUN_MARKER_WRITE_FAILING",
    # Code #13 slice 9b: the repeat-signal escalation ledger (logs/repeat-signals.jsonl)
    # failing to write would convert "this signal has fired three times unacked"
    # into "first fire, normal surface" every night -- the escalation would never
    # climb and the suppression never lift. Same precedent as the two tokens above.
    r"REPEAT_SIGNAL_WRITE_FAILING",
]
_CRITICAL_RE = re.compile("|".join(_CRITICAL_LOG_PATTERNS), re.IGNORECASE)

# A real ERROR-LEVEL line: the level field immediately after the timestamp. Both
# live formats are accepted -- bot "2026-08-28T03:25:37 ERROR [Thread-1] n: msg"
# and script "2026-08-28 03:30:15,081 ERROR n: msg". Deliberately NOT IGNORECASE:
# the level field is upper-case by construction, and case-insensitivity would
# re-admit ordinary prose containing the word "error".
# D-051 review: the first cut said "both live formats" and there are THREE --
# a bracketed [%(levelname)s] form is used elsewhere in the estate, and its ERROR
# lines were invisible to the anchor. A volume metric that silently under-counts
# is the same silent-failure class this slice exists to close, so the bracket is
# optional now. Still deliberately NOT IGNORECASE: the level field is upper-case
# by construction, and case-insensitivity would re-admit prose containing "error".
_ERROR_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:,\d+)?\s+\[?ERROR\]?\b"
)

log = logging.getLogger("health-check")


# ── Individual checks ─────────────────────────────────────────────────────────


def check_heartbeat(dry_run: bool) -> CheckResult:
    """Check if Cora's heartbeat file is recent; auto-restart if stale."""
    heartbeat_file = _REPO_ROOT / "data" / "health" / "heartbeat.txt"
    if not heartbeat_file.exists():
        # Fall back to scanning today's log
        today = datetime.now().strftime("%Y-%m-%d")
        log_path = _LOG_DIR / f"cora-{today}.log"
        if not log_path.exists():
            return CheckResult("Cora heartbeat", "critical",
                               "No heartbeat file and no today's log found.")
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            return CheckResult("Cora heartbeat", "critical", f"Cannot read log: {exc}")
        # Find last heartbeat line
        hb_lines = [l for l in lines if "heartbeat alive" in l]
        if not hb_lines:
            return CheckResult("Cora heartbeat", "critical",
                               "No heartbeat found in today's log — service may be down.")
        last = hb_lines[-1]
        # Extract timestamp: "2026-05-31T21:57:16 INFO ..."
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", last)
        if m:
            hb_time = datetime.fromisoformat(m.group(1))
            age_sec = (datetime.now() - hb_time).total_seconds()
            if age_sec > 300:
                fix = _restart_cora(dry_run)
                return CheckResult("Cora heartbeat", "fixed",
                                   f"Heartbeat was {age_sec/60:.1f}min stale.",
                                   fix_applied=fix)
            return CheckResult("Cora heartbeat", "ok",
                               f"Alive — last beat {age_sec:.0f}s ago.")
        return CheckResult("Cora heartbeat", "warn", "Heartbeat line unparseable.")

    try:
        content = heartbeat_file.read_text(encoding="utf-8").strip()
        # heartbeat.txt written as ISO timestamp
        hb_time = datetime.fromisoformat(content.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        if hb_time.tzinfo is None:
            hb_time = hb_time.replace(tzinfo=timezone.utc)
        age_sec = (now_utc - hb_time).total_seconds()
        if age_sec > 300:
            fix = _restart_cora(dry_run)
            return CheckResult("Cora heartbeat", "fixed",
                               f"Heartbeat was {age_sec/60:.1f}min stale.",
                               fix_applied=fix)
        return CheckResult("Cora heartbeat", "ok",
                           f"Alive — last beat {age_sec:.0f}s ago.")
    except Exception as exc:
        return CheckResult("Cora heartbeat", "warn", f"Could not parse heartbeat file: {exc}")


def _restart_cora(dry_run: bool) -> str:
    if dry_run:
        return "[DRY RUN] Would have restarted cowork-cora-service."
    try:
        subprocess.run(
            ["schtasks", "/End", "/TN", "cowork-cora-service"],
            capture_output=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
        # Kill orphan BOT processes only. The old filter was "*cora*" anywhere in
        # the command line, which also matches scripts/run_mcp_server.py (path
        # contains "cora"), every scheduled cora script and any in-flight KB
        # backfill -- an auto-restart could shoot down a multi-hour migration.
        # Use the doctrine-5 filter: the bot chain and nothing else.
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='cora.exe'\" | "
             r"Where-Object { $_.CommandLine -like '*\Scripts\cora.exe*' -or "
             "$_.CommandLine -like '*cora.main*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
            capture_output=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
        time.sleep(2)
        subprocess.run(
            ["schtasks", "/Run", "/TN", "cowork-cora-service"],
            capture_output=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
        return "Auto-restarted cowork-cora-service (orphan-kill applied)."
    except Exception as exc:
        return f"Restart attempted but failed: {exc}"


_STUCK_RUNNING_HOURS = 2


def _classify_task_states(
    task_states: dict[str, str],
    intended_disabled: set[str],
    expected_running: set[str],
    stuck_running: set[str] | None = None,
) -> tuple[list[str], list[str], int]:
    """Pure classifier (audit N8). Returns (critical, warn, ok_count).

    A disabled-state drift is a WARNING, never a CRITICAL -- a Disabled task is a
    deliberate admin action, not an outage, and Task Scheduler never auto-flips
    it. The ONLY task-state CRITICAL is the always-on service not Running.
    """
    critical: list[str] = []
    warn: list[str] = []
    ok = 0
    for name, status in task_states.items():
        if name in intended_disabled:
            if "Disabled" in status:
                ok += 1
            else:
                warn.append(f"{name}: intended Disabled, found {status} "
                            f"(re-disable, or update scheduled-task-state.yaml)")
        elif name in expected_running:
            if "Running" in status:
                ok += 1
            else:
                critical.append(f"{name}: expected Running, found {status}")
        elif "Running" in status and name in (stuck_running or ()):
            # A task in Running with a last-start hours ago is not working, it is
            # wedged -- and while wedged it blocks every subsequent trigger.
            # Observed live: the watchdog sat State=Running with no process
            # behind it (0x80070420), which is exactly the failure the watchdog
            # exists to catch.
            warn.append(f"{name}: stuck in Running since its last start "
                        f"(>{_STUCK_RUNNING_HOURS}h) -- it is blocking its own "
                        f"triggers; end it from an elevated shell "
                        f"(schtasks /End /TN \"{name}\")")
        elif "Ready" in status or "Running" in status:
            ok += 1
        elif "Disabled" in status:
            warn.append(f"{name}: unexpectedly Disabled "
                        f"(add to scheduled-task-state.yaml if intentional)")
        else:
            warn.append(f"{name}: unexpected status '{status}'")
    return critical, warn, ok


def _stuck_running_tasks(running_names: list[str], *,
                        now: datetime | None = None) -> set[str]:
    """Of the tasks currently in state Running, which started >2h ago?

    `schtasks /Query /FO CSV` carries no duration, so this reads each candidate's
    "Start Time"/"Last Run Time" with /V. Only the Running set is queried, so on
    a healthy host this is zero or one extra call.

    Fail-soft: on any parse or query failure the task is NOT reported stuck. A
    false "your task is wedged" would send Harrison to an elevated shell for
    nothing, and the previous behaviour (silence) is the safe direction here.
    """
    if not running_names:
        return set()
    now = now or datetime.now()
    stuck: set[str] = set()
    for name in running_names:
        try:
            out = subprocess.run(
                ["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"],
                capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW).stdout
        except Exception:  # noqa: BLE001
            continue
        started = None
        for line in out.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            if key.strip().lower() not in ("start time", "last run time"):
                continue
            val = val.strip()
            for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S",
                        "%Y-%m-%d %H:%M:%S"):
                try:
                    started = datetime.strptime(val, fmt)
                    break
                except ValueError:
                    continue
            if started:
                break
        if not started:
            continue
        if (now - started).total_seconds() > _STUCK_RUNNING_HOURS * 3600:
            stuck.add(name)
    return stuck


def check_scheduled_tasks() -> list[CheckResult]:
    """Check all Cora scheduled tasks for unexpected states (audit N8)."""
    try:
        out = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30,
            creationflags=_NO_WINDOW,
        ).stdout
    except Exception as exc:
        return [CheckResult("Scheduled tasks", "warn", f"schtasks query failed: {exc}")]

    task_states: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.strip().strip('"').split('","')
        if len(parts) < 3:
            continue
        raw_name = parts[0].lstrip("\\")
        if not (raw_name.startswith("cowork-cora") or raw_name.startswith("Cora")):
            continue
        status = parts[2]
        prev = task_states.get(raw_name)
        # schtasks can emit one row per trigger; prefer a non-Ready status.
        if prev is None or (("Running" in status or "Disabled" in status) and "Ready" in prev):
            task_states[raw_name] = status

    # The always-on service is SUPPOSED to be Running for weeks -- exclude it
    # rather than querying it and relying on branch order to save us.
    stuck = _stuck_running_tasks(
        [n for n, st in task_states.items()
         if "Running" in st and n not in _EXPECTED_RUNNING])
    critical, warn, ok_count = _classify_task_states(
        task_states, _EXPECTED_DISABLED, _EXPECTED_RUNNING, stuck
    )

    results: list[CheckResult] = []
    if critical:
        results.append(CheckResult(
            "Scheduled tasks", "critical",
            f"{len(critical)} task(s) in a CRITICAL state:\n" +
            "\n".join(f"  - {p}" for p in critical)
        ))
    if warn:
        results.append(CheckResult(
            "Scheduled tasks", "warn",
            f"{len(warn)} task(s) drifted from intended state:\n" +
            "\n".join(f"  - {p}" for p in warn)
        ))
    if not critical and not warn:
        results.append(CheckResult(
            "Scheduled tasks", "ok",
            f"All {ok_count} tasks in expected state."
        ))
    return results


# ── W4-07: LastTaskResult probe ───────────────────────────────────────────────
# check_scheduled_tasks (above) classifies task STATE only (Ready/Running/
# Disabled) — so a task that is "Ready" but SIGKILLed or failing on every run
# (founders-os-sweep LastResult=267014; finance-receipt-digest LastResult=1)
# stayed green and nothing alarmed for weeks. This probe reads LastTaskResult and
# WARNs (never critical — a failing periodic job is not an outage) on a nonzero,
# non-benign result for any ENABLED task.

# Task Scheduler status codes that are NOT a failed run (the LastTaskResult holds
# a benign status, not an exit code).
_BENIGN_LAST_RESULTS: frozenset[int] = frozenset({
    0,        # success
    267008,   # 0x00041300 SCHED_S_TASK_READY
    267009,   # 0x00041301 SCHED_S_TASK_RUNNING (e.g. the always-on service)
    267011,   # 0x00041303 SCHED_S_TASK_HAS_NOT_RUN (freshly-registered weeklies)
})

# Tasks documented to exit NONZERO as a legitimate SIGNAL (not a fault). Each is
# covered by its own dedicated check, so gating LastResult here would only
# double-report a non-fault:
#   • "Cora - QBO Token Monitor" — exit 1 = a real token finding it already DM'd
#     (scripts/qbo_token_status.py: `return 1 if has_failure else 0`); its
#     liveness is freshness-monitored by check_qbo_monitor.
#   • "cowork-cora-health-check" — THIS check. Self-referential: yesterday's
#     exit 1 (from a real critical) would re-warn today; it monitors itself via
#     the criticals path, not its own LastResult.
_LASTRESULT_SIGNAL_OK: frozenset[str] = frozenset({
    "Cora - QBO Token Monitor",
    "cowork-cora-health-check",
    # Exit 1 is a documented SIGNAL here, not a fault: one realm going UNKNOWN on
    # a transient QBO 5xx, or a tie-out that could not run, still banks the
    # windows. Left out of this set it is a WEEKLY task, so LastTaskResult=1
    # persists until the next Monday and this check emits the same warn every
    # night for seven nights -- the crying-wolf pattern the actuals build spent a
    # whole slice avoiding elsewhere. Its own check_cashflow_actuals covers the
    # failures that matter.
    "cowork-cora-cashflow-actuals",
})

_LAST_RESULT_HINTS: dict[int, str] = {
    267014: "TASK_TERMINATED - likely hit its ExecutionTimeLimit",
    2147946720: ("ALREADY_RUNNING (0x80070420) - a stuck instance is blocking every "
                 "trigger under MultipleInstances=IgnoreNew; run schtasks /End on it"),
    1: "generic failure exit",
    2: "partial failure exit",
    267012: "NO_MORE_RUNS",
    267013: "NOT_SCHEDULED",
    267015: "NO_VALID_TRIGGERS",
    # deployment/run_hidden.py, the windowless launcher every Cora task now
    # runs through, reserves 0xE0-0xE3 for ITS OWN failures so a rewrap
    # regression cannot be mistaken for a script bug. These codes exist
    # precisely to be recognised here.
    # Raw strings: these messages carry Windows paths, and in a normal string
    # "logs\tasks" is logs + TAB + asks and "deployment\rewrap" embeds a
    # carriage return -- the message would render mangled in the Slack digest.
    224: ("run_hidden LAUNCHER: no child command line after ' -- ' -- the task's "
          r"wrapped action is malformed; re-run deployment\rewrap-tasks-hidden.ps1"),
    225: (r"run_hidden LAUNCHER: logs\tasks is unusable (full disk? permissions?) "
          "-- the child was NOT run"),
    226: ("run_hidden LAUNCHER: could not start the child (missing exe or bad path) "
          "-- check the command after ' -- ' in the task's action"),
    227: (r"run_hidden LAUNCHER: unexpected internal error -- see "
          r"logs\tasks\_launcher-errors.log"),
}


def _classify_task_last_results(
    task_results: dict[str, tuple[str, int | None]],
    benign_codes: frozenset[int],
    signal_ok_names: frozenset[str],
) -> tuple[list[str], int]:
    """Pure classifier (W4-07). Returns (warn_messages, ok_count).

    Only ENABLED tasks are judged — a Disabled task is idle by design and its
    stale LastResult is meaningless (this is what keeps the probe from
    false-alarming on the legitimately-disabled fleet). A result we could not
    read (None) never fabricates a warning. Signal-OK tasks are allow-listed.
    Everything else nonzero-and-non-benign on an enabled task -> WARN."""
    warn: list[str] = []
    ok = 0
    for name in sorted(task_results):
        state, result = task_results[name]
        if "Disabled" in state:
            continue  # idle by design — stale LastResult is meaningless
        if result is None:
            continue  # unreadable — never invent a warning
        if name in signal_ok_names:
            ok += 1
            continue
        if result in benign_codes:
            ok += 1
            continue
        hexr = f"0x{result & 0xFFFFFFFF:08X}"
        hint = _LAST_RESULT_HINTS.get(result, "")
        warn.append(
            # cq-b2dee156caee (session #11 S6): this line used to end with
            # "- this repeats silently every run", appended to EVERY nonzero result
            # with no recurrence check of any kind. The check reads a single Last
            # Task Result, so it cannot know that -- and the fabricated claim is
            # the direct source of a false premise that became a queue seed. State
            # the observation and its known limit instead of asserting recurrence.
            f"{name}: last run exited {result} ({hexr}"
            f"{' - ' + hint if hint else ''}) - most recent run only; "
            f"this check does not see run history"
        )
    return warn, ok


def _get_task_last_results() -> dict[str, tuple[str, int | None]]:
    """State + LastTaskResult for every Cora task in one PowerShell call.

    Returns {task_name: (state_str, last_result_int_or_None)}. Empty on failure
    (the probe then reports a soft warn rather than crashing the health run)."""
    ps = (
        "Get-ScheduledTask | Where-Object { $_.TaskName -like 'cowork-cora*' "
        "-or $_.TaskName -like 'Cora*' } | ForEach-Object { $i = $_ | "
        "Get-ScheduledTaskInfo; Write-Output ($_.TaskName + '|' + $_.State + "
        "'|' + $i.LastTaskResult) }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=60,
            creationflags=_NO_WINDOW,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        log.warning("check_task_last_results: query failed: %s", exc)
        return {}

    results: dict[str, tuple[str, int | None]] = {}
    for line in out.splitlines():
        line = line.rstrip("\r").strip()
        if not line or "|" not in line:
            continue
        # Task names never contain '|'; the last two fields are state + result.
        name, _, rest = line.partition("|")
        state, _, res = rest.partition("|")
        name, state, res = name.strip(), state.strip(), res.strip()
        try:
            res_int: int | None = int(res)
        except ValueError:
            res_int = None
        results[name] = (state, res_int)
    return results


def check_task_last_results() -> list[CheckResult]:
    """W4-07: WARN on any ENABLED Cora task whose LAST run exited nonzero."""
    task_results = _get_task_last_results()
    if not task_results:
        return [CheckResult(
            "Task last-results", "warn",
            "Could not read task LastTaskResult (PowerShell query returned "
            "nothing) — silently-failing tasks may go undetected this run.",
        )]
    warn, ok_count = _classify_task_last_results(
        task_results, _BENIGN_LAST_RESULTS, _LASTRESULT_SIGNAL_OK,
    )
    if warn:
        return [CheckResult(
            "Task last-results", "warn",
            f"{len(warn)} enabled task(s) exited nonzero on their last run:\n" +
            "\n".join(f"  - {w}" for w in warn),
        )]
    return [CheckResult(
        "Task last-results", "ok",
        f"All {ok_count} enabled task(s) last exited clean.",
    )]


_QBO_MONITOR_TASK = "Cora - QBO Token Monitor"


def check_run_markers(now: datetime | None = None) -> CheckResult:
    """Diff expected task cadence against observed run markers (session #11 S4).

    Retires the cq-a251dee3f5cf class. Task Scheduler reports that a task RAN and
    its exit code; it cannot report that the run produced NOTHING. The weekly
    Slack-clarity check fired 8/22 with a registry-confirmed lastRunAt and posted
    no digest, and no surface recorded it.

    Raises two DISTINCT alarms -- "MISSED FIRE" (no marker in the window) and
    "FIRED BUT WROTE NOTHING" (marker present and recent, outputs == 0 on a task
    declaring expects_output). Conflating them would hide the second, which is
    the one nothing else in the estate can see.

    WARN, never CRITICAL: a lane producing no output is a defect to investigate,
    not an outage of the bot.
    """
    registry = _load_run_marker_registry()
    if not registry:
        return CheckResult(
            "Task run markers", "ok",
            "no run-marker registry configured -- nothing to diff.")
    try:
        sys.path.insert(0, str(_REPO_ROOT / "src"))
        from cora import run_marker  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Task run markers", "warn",
                           f"run_marker module unavailable: {exc}")
    try:
        markers = run_marker.latest_by_task()
        findings = run_marker.evaluate(registry, markers, now=now)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Task run markers", "warn",
                           f"run-marker evaluation failed: {exc}")
    if not findings:
        # D-051 review: the first cut reported "all fired in-window with output"
        # even when ZERO markers existed and every task was merely inside its
        # registration grace -- a false OK, which is precisely the defect class
        # this check exists to surface. Say what was actually evaluated.
        seen = sum(1 for e in registry if markers.get(str(e.get("name") or "")))
        awaiting = len(registry) - seen
        detail = f"{len(registry)} task(s) tracked; {seen} with markers, all in-window"
        if awaiting:
            detail += f"; {awaiting} awaiting a first marker (registration grace)"
        return CheckResult("Task run markers", "ok", detail + ".")
    return CheckResult(
        "Task run markers", "warn",
        "%d issue(s): %s" % (len(findings), " | ".join(m for _sev, m in findings)))


def check_missed_nightly_catchup(now: datetime | None = None) -> CheckResult:
    """Code #13 slice 2 (cq-fb50c9e6c911): READ the missed-nightly catch-up lane's
    ledger for today's window. The lane itself runs as its own 08:30 task
    ("Cora - Missed Nightly Catch-Up", scripts/check_missed_nightly.py) -- never
    from here: this check runs inside run_hidden's kill-on-close job, so a
    replay spawned here would die with it, and a synchronous replay would delay
    the report by hours.

    WARN when: no decision row exists for today after the 08:30 fire (the lane is
    not registered or did not fire -- a lost night would go unnoticed again, the
    9/9 shape); any task was REPLAYED (the night was not clean; the replay is the
    audit trail); any task read cannot_check (Running / scheduler unreadable) or
    skipped_imminent; any ENABLED task read skipped_window / skipped_not_due /
    skipped_disabled -- NOT verified: nothing proved it fired and nothing replayed
    it (a host down until noon reads every task skipped_window; that is the 9/9
    shape one step longer, D-051 review B-1); the replay loop deferred a spawn
    (skipped_window / skipped_imminent / not-started at spawn time); a replay
    failed or timed out. OK only when every ENABLED task fired on schedule (or a
    replay finished clean) -- the tail is derived from the counts, never a
    constant. Never CRITICAL.

    T1 (ruling 9.2; Code #14 R14-7): the registered lane runs `--record`, which
    writes a mode=dry-run plan row and replays nothing. That row reads as "fired
    (T1 plan recorded)": OK when every enabled task fired, WARN "T1: would have
    replayed N task(s) (not replayed): ..." when the plan holds `run` rows; the
    unverified / skipped_window WARNs are unchanged.
    """
    name = "Missed-nightly catch-up"
    try:
        sys.path.insert(0, str(_REPO_ROOT / "src"))
        from cora import nightly_catchup as nc  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "warn", f"nightly_catchup module unavailable: {exc}")
    try:
        if now is None:
            now_az = datetime.now(nc.AZ)
        elif now.tzinfo is None:
            now_az = now.replace(tzinfo=nc.AZ)
        else:
            now_az = now.astimezone(nc.AZ)
        day = now_az.date()
        summary = nc.summarize_day(nc.read_ledger(), day)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "warn", f"catch-up ledger unreadable: {exc}")
    if not summary.get("present"):
        if (now_az.hour, now_az.minute) < (8, 40):
            return CheckResult(name, "ok",
                               f"no decision row yet for {day} -- the 08:30 fire has not happened")
        return CheckResult(
            name, "warn",
            f"NO catch-up decision row for {day} -- '{nc.TASK_NAME}' did not fire or is not "
            "registered (deployment/setup-missed-nightly-catchup-task.ps1); a lost night would "
            "go unnoticed (the 9/9 shape)")
    counts = summary.get("counts") or {}
    decisions = summary.get("decisions") or []
    enabled = [d for d in decisions if str(d.get("action")) != "skipped_disabled_in_set"]
    attention = [d for d in enabled if str(d.get("action")) in nc.ATTENTION_ACTIONS]
    unverified = [d for d in enabled if str(d.get("action")) not in nc.VERIFIED_ACTIONS
                  and str(d.get("action")) not in nc.ATTENTION_ACTIONS]   # an action this check does not know
    replays = summary.get("replays") or []
    deferred = summary.get("deferred") or []
    failed = [r for r in replays if str(r.get("action")) != "ran"]
    head = f"{day}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    # Code #14 R14-7 (4a): a mode=dry-run plan row is the T1 lane's RECORDED plan
    # (check_missed_nightly --record, ruling 9.2). It proves the lane FIRED; a `run`
    # decision in it means "would have replayed" -- nothing was, by design -- so it
    # WARNs in its own words instead of reading as a pending replay. Every other
    # attention action (skipped_window / cannot_check / ...) keeps its semantics.
    t1 = str(summary.get("mode") or "") == "dry-run"
    would_replay: list[dict] = []
    if t1:
        would_replay = [d for d in attention if str(d.get("action")) == "run"]
        attention = [d for d in attention if str(d.get("action")) != "run"]
    if attention or unverified or failed or deferred or would_replay:
        parts: list[str] = []
        if would_replay:
            parts.append(f"T1: would have replayed {len(would_replay)} task(s) (not replayed): "
                         + ", ".join(str(d.get("task")) for d in would_replay))
        parts += [f"{d.get('task')} {d.get('action')}"
                  + (" (not verified)" if str(d.get("action")) in nc.UNVERIFIED_ACTIONS else "")
                  for d in attention]
        parts += [f"{d.get('task')} {d.get('action')} (unknown action -- not verified)" for d in unverified]
        parts += [f"{r.get('task')} replay {r.get('action')} rc={r.get('rc')}" for r in failed]
        parts += [f"{r.get('task')} deferred at spawn time: {r.get('action')}" for r in deferred]
        return CheckResult(name, "warn", head + " -- " + "; ".join(parts))
    fired = sum(1 for d in enabled if str(d.get("action")) == "fired")
    if t1 and enabled and fired == len(enabled):
        tail = (f"; fired (T1 plan recorded) -- every task fired on schedule "
                f"({fired}/{len(enabled)} enabled)")
    elif replays:
        tail = f"; {len(replays)} replay(s) finished clean"
    elif enabled and fired == len(enabled):
        tail = f"; every task fired on schedule ({fired}/{len(enabled)} enabled)"
    else:
        tail = f"; {fired}/{len(enabled)} enabled task(s) fired on schedule"
    return CheckResult(name, "ok", head + tail)


def check_meet_join_audit() -> CheckResult:
    """Code #14 R14-8: READ the latest 07:22 meeting-capture audit row's
    `meet_audit_state` -- the Meet join audit lane's failing-capable monitor.

    The health vocabulary has no INFO status, so INFO = "ok" with the word in the
    detail: a DARK lane (dark:scope / dark:api / dark:subject -- the provisioned
    default until Harrison grants the Reports scope), a row written before the
    lane shipped (no key), or a `lag` read (run before the lag floor) is INFO.
    `live` is OK. `error`, `partial` and `contradiction` WARN: the lane is
    provisioned but could not decide, so every unconvened meeting stayed a
    presumption. Read-only; writes nothing (holds under --dry-run by construction).
    """
    name = "Meet join audit"
    try:
        sys.path.insert(0, str(_REPO_ROOT / "src"))
        from cora import meeting_capture as mc  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "warn", f"meeting_capture module unavailable: {exc}")
    latest: dict | None = None
    try:
        path = mc.ledger_path()
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"audit"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(row, dict) and row.get("lane") == "audit":
                        latest = row
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "warn", f"meeting-capture ledger unreadable: {type(exc).__name__}")
    if latest is None:
        return CheckResult(name, "ok", "INFO: no meeting-capture audit row yet")
    day = latest.get("day") or "?"
    if "meet_audit_state" not in latest or not latest.get("meet_audit_state"):
        return CheckResult(name, "ok", f"INFO: {day} audit row predates the lane (no meet_audit_state)")
    state = str(latest.get("meet_audit_state"))
    reason = str(latest.get("meet_audit_reason") or "")
    if state.startswith("dark:"):
        return CheckResult(
            name, "ok",
            f"INFO: {day} lane dark ({state}{'; ' + reason if reason else ''}) -- presumed-"
            "unconvened stands; the provisioned default until the Reports scope is granted")
    if state == "lag":
        return CheckResult(name, "ok", f"INFO: {day} read ran before the lag floor -- nothing decided")
    if state == "live":
        return CheckResult(
            name, "ok",
            f"{day} live: {int(latest.get('meet_events_read') or 0)} call_ended event(s) read; "
            f"{len(latest.get('unconvened_confirmed_event_ids') or [])} confirmed unconvened, "
            f"{len(latest.get('convened_event_ids') or [])} convened-but-missed, "
            f"{len(latest.get('unconvened_presumed_event_ids') or [])} still presumed")
    return CheckResult(
        name, "warn",
        f"{day} Meet join log read {state}{' (' + reason + ')' if reason else ''} -- the lane "
        "decided nothing; every unconvened meeting stayed a presumption")


def check_ladder_registry() -> CheckResult:
    """Code #13 slice 7 (cq-6afa86210ba0): the autonomy-ladder REGISTRY
    (data/ladder-registry.yaml) read for drift. WARN when the file is missing or
    unparseable (it is a Code #13 deliverable -- absence IS drift, unlike the
    mirror's ok-until-adopted posture), on any schema violation (a KNOWN lane with
    no row, a row not in KNOWN_LANES, a tier above T1 without a failing-capable
    monitor, a tier above T0 with no evidenced event ...), and on ACTING DRIFT: a
    lane whose live flag reads above its registered tier (S2' / the capability
    rail in enforce while registered T0; revops in tier1 while T0 ...). Pending
    Harrison confirms are rendered, not alarmed -- the batch confirm is his tap.
    """
    name = "Autonomy-ladder registry"
    try:
        sys.path.insert(0, str(_REPO_ROOT / "src"))
        from cora import ladder_registry as lr  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "warn", f"ladder_registry module unavailable: {exc}")
    try:
        reg = lr.load()
        s = lr.summary(reg)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "warn", f"registry read failed: {exc}")
    if not s.get("available"):
        return CheckResult(name, "warn",
                           f"registry UNAVAILABLE: {s.get('reason')} -- data/ladder-registry.yaml is a "
                           "Code #13 deliverable; absence IS drift")
    head = (f"{s['lanes']} lanes | " + " / ".join(f"{t} {s['by_tier'].get(t, 0)}" for t in lr.TIERS)
            + f" | pending Harrison confirm {len(s['pending_confirmation'])}")
    problems = list(s.get("schema_problems") or []) + list(s.get("acting_drift") or [])
    if problems:
        return CheckResult(name, "warn", head + f" | {len(problems)} drift: " + "; ".join(problems))
    return CheckResult(name, "ok", head + " | schema clean, no acting drift")


# ── Code #16 C1: the dead-channel archive lane's evidence monitor ────────────────
def check_channel_archive(*, dry_run: bool = False, client_factory=None,
                          now: float | None = None) -> CheckResult:
    """Code #16 C1 (ladder row slack-channel-archive): reconcile the lane's archive
    ledger against Slack's own record (cora.channel_archive.monitor.reconcile) -- the
    row's failing-capable evidence monitor. WARN on an archive by Cora the ledger
    cannot attribute to a tap (which also WRITES the demotion file, so the lane acts
    at T0 until Harrison clears it), on a ledger archive Slack does not show, on an
    unresolved intent, on a scan that never staged a card, while demoted, and on any
    BLIND read (a blind read never reads OK). ONE CheckResult.

    `dry_run` (the script's --dry-run, D-290) classifies exactly as a real run and
    writes NOTHING: no demotion file, no reconciled ledger/store rows. The Slack
    client is injectable; the default refuses to build a live client under pytest.
    """
    name = "Dead-channel archive lane"
    try:
        sys.path.insert(0, str(_REPO_ROOT / "src"))
        from cora.channel_archive import clients as ca_clients  # noqa: PLC0415
        from cora.channel_archive import monitor as ca_monitor  # noqa: PLC0415
        from cora.channel_archive import policy as ca_policy  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "warn", f"channel_archive module unavailable: {exc}")
    try:
        client = (client_factory or ca_clients.read_client)()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "warn", f"BLIND: no Slack client ({type(exc).__name__}) -- "
                                         "nothing was verified")
    try:
        out = ca_monitor.reconcile(client, now=now, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "warn", f"monitor crashed ({type(exc).__name__}) -- nothing verified")
    cov = ca_monitor.coverage_line(out.get("coverage") or {})
    head = f"acting {ca_policy.acting_tier()}"
    if out.get("status") == "ok":
        return CheckResult(name, "ok", f"{head} | ledger reconciled against Slack | {cov}")
    findings = list(out.get("findings") or [])
    shown = "; ".join(findings[:6]) + (f"; +{len(findings) - 6} more" if len(findings) > 6 else "")
    how = str(out.get("demotion") or "")          # the tail says what really happened
    tail = (" | demotion WRITTEN" if out.get("demotion_written")
            else " | demotion NOT written (dry run)" if how.startswith("dry_run")
            else " | DEMOTION WRITE FAILED" if how in ("failed", "update_failed") else "")
    return CheckResult(name, "warn", f"{head} | {shown}{tail} | {cov}")


# ── Code #16 C2: the travel shortlist lane's failing-capable monitor ─────────
def check_travel_shortlist(now: datetime | None = None) -> CheckResult:
    """Code #16 C2 (ladder row travel-shortlist, T0): READ the lane's thread store
    (data/state/travel-shortlist-threads.jsonl -- structured fields + events, never
    raw text) over the last 7 days. WARN on ANY belt_refused event (the allowlist
    belt refused a request that carried a token outside the parsed fields -- the
    lane's own egress regression, caught before anything was searched), on a
    search_failed share above 50% of >= 4 asks, on unreadable store lines (counts
    may be incomplete), and when the store exists but cannot be read (a blind read
    never reads clean). An absent store is INFO (no asks yet). Read-only; writes
    nothing (holds under --dry-run by construction). ONE CheckResult.
    """
    name = "Travel shortlist lane"
    try:
        sys.path.insert(0, str(_REPO_ROOT / "src"))
        from cora import travel_shortlist as tsl  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "warn", f"travel_shortlist module unavailable: {exc}")
    try:
        s = tsl.threads_summary(now=now)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "warn", f"thread store read failed ({type(exc).__name__}) "
                                         "-- the lane's monitor is blind")
    if not s.get("available"):
        return CheckResult(name, "warn", f"thread store UNREADABLE ({s.get('reason')}) -- the "
                                         "lane's monitor is blind; a belt refusal would be invisible")
    if not s.get("exists"):
        return CheckResult(name, "ok", "INFO: no lodging asks yet (thread store absent)")
    asks, posted = int(s.get("asks") or 0), int(s.get("posted") or 0)
    failed, refused = int(s.get("search_failed") or 0), int(s.get("belt_refused") or 0)
    post_failed, bad = int(s.get("post_failed") or 0), int(s.get("bad_lines") or 0)
    detail = (f"7d: {asks} ask(s), {posted} card(s) posted, {failed} search failure(s), "
              f"{refused} belt refusal(s)" + (f", {post_failed} card post failure(s)" if post_failed else ""))
    problems: list[str] = []
    if refused:
        problems.append(f"{refused} belt refusal(s) -- a lane request carried a token outside the "
                        "parsed-field allowlist and was refused (nothing searched); find the path "
                        "that built it before the next ask")
    if asks >= 4 and failed * 2 > asks:
        problems.append(f"search failures {failed}/{asks} (> 50%) -- the lane is mostly failing")
    if bad:
        problems.append(f"{bad} unreadable store line(s) -- counts may be incomplete")
    if problems:
        return CheckResult(name, "warn", detail + " | " + "; ".join(problems))
    return CheckResult(name, "ok", detail)


def check_qbo_monitor(now: datetime | None = None) -> CheckResult:
    """The QBO token monitor must keep FIRING daily -- if it silently stops, a
    realm could expire unnoticed and finance answers fail silently. WARN if it's
    missing or hasn't run in >36h. Last-result is deliberately NOT gated: the
    monitor's exit 1 = a real token finding it already DM'd, not a monitor fault.
    `now` is injectable for tests."""
    now = now or datetime.now()
    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", _QBO_MONITOR_TASK, "/V", "/FO", "LIST"],
            capture_output=True, text=True, timeout=30,
            creationflags=_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("QBO token monitor", "warn", f"schtasks query failed: {exc}")
    if proc.returncode != 0:
        return CheckResult(
            "QBO token monitor", "warn",
            f"'{_QBO_MONITOR_TASK}' not registered -- QBO token expiries go "
            r"unmonitored. Run deployment\setup-qbo-token-monitor-task.ps1.")
    last_run = ""
    for line in proc.stdout.splitlines():
        s = line.strip()
        if s.startswith("Last Run Time:"):
            last_run = s.split(":", 1)[1].strip()
            break
    if not last_run or last_run.upper().startswith("N/A") or "never" in last_run.lower():
        return CheckResult("QBO token monitor", "warn", f"'{_QBO_MONITOR_TASK}' has never run.")
    parsed = None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
        try:
            parsed = datetime.strptime(last_run, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return CheckResult("QBO token monitor", "ok", f"Registered; last run {last_run}.")
    age_h = (now - parsed).total_seconds() / 3600
    if age_h > 36:
        return CheckResult(
            "QBO token monitor", "warn",
            f"'{_QBO_MONITOR_TASK}' last ran {age_h:.0f}h ago (expected daily) -- "
            "it may have stopped firing.")
    return CheckResult("QBO token monitor", "ok", f"Registered; last ran {age_h:.0f}h ago.")


def check_decision_gates(today: date | None = None, *, dry_run: bool = False) -> CheckResult:
    """An Open decision past its GATE date that no surface has delivered.

    `dry_run=True` (the script's --dry-run, R14-1 / D-290) computes the SAME
    verdict and writes NOTHING: no repeat-signal clear / ack / fire /
    suppressed / implicit-ack row, no tier-3 card, no `ping:` delivery row. A
    dry run is a claim about every write site; this check has five (the clear
    reconciliation, the answered-alert ack, repeat_signal.fire's three row kinds,
    the card mint inside fire, and the ping record).

    cq-232fe6a541ff. Five decisions (OSN data source, Jerry DW access, BDM
    department lock, Eric LEX Learning Center, LEX Phase 2) sat Open past their
    2026-08-13 gate and never reached Harrison on any surface. The seed asked for
    "delivery verification for P-decisions older than N days"; implemented
    literally -- staleness on P0/P1 -- that check would have stayed GREEN through
    all five, because every one of them is P2. So this check is ANY SEVERITY, and
    it turns on DELIVERY (a surface actually emitted it), not on gathering.

    Nothing here expires anything: a passed gate makes a decision louder, never
    gone (the expiry semantics adopted with the 8/19 approval recon).

    A decision with no gate date is not reported -- it has no deadline to blow.
    That is why the transcription script carries the gate across from Airtable.
    Daily rather than weekly on purpose: the failure mode is silence, and a weekly
    check tolerates six more days of it.
    """
    try:
        sys.path.insert(0, str(_REPO_ROOT / "src"))
        from cora import decision_lane
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Decision gates", "warn",
                           f"decision_lane unavailable ({exc}) -- gate-date "
                           "escalation not evaluated this run.")
    try:
        entries = decision_lane.load_entries(today=today)
        overdue = decision_lane.undelivered_overdue(entries, today=today)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Decision gates", "warn",
                           f"gate check failed ({exc}).")

    gated = [e for e in entries if e.get("gate")]
    if not entries:
        # An unreadable file is an OUTAGE, not a clear: no repeat-signal
        # reconciliation runs on this branch, so a G: hiccup cannot reset a
        # live escalation.
        return CheckResult("Decision gates", "warn",
                           "decisions-pending.md could not be read or holds no "
                           "entries -- the decision lane cannot be verified.")
    # The file WAS read: any gate signal whose row is no longer overdue-undelivered
    # (CLOSED heading, "Recently resolved" tail, gate re-dated, real delivery)
    # has cleared. Runs before the no-gates return so a removed gate clears too.
    _gate_reconcile_clears(overdue, dry_run=dry_run)
    if not gated:
        # Truthful, and the actionable half: with no gate dates recorded, this
        # control has nothing to enforce. Do NOT report "ok".
        return CheckResult(
            "Decision gates", "warn",
            f"{len(entries)} open decision(s), NONE carrying a gate date -- so a "
            "blown deadline cannot be detected. Add **Gate**: YYYY-MM-DD to the "
            "entries that have one (see decisions-pending.md field rules).")
    # THE HEALTH DIGEST IS AN ESCALATION PING, NOT A DELIVERY (D-310, 2026-09-19
    # audit section 3; reverses the 8/19 premise that stood here). The old code
    # recorded this surface as "health_check", delivery_index counted it, and a
    # blown gate went CRITICAL on day N, silent for 8 days, CRITICAL again on
    # N+9 -- an 8-9 day cadence that self-cleared with no human in the loop. Now
    # the alarm is recorded under the "ping:" surface class (auditable in the
    # ledger; ignored by the control) and routed through repeat_signal, so it
    # alarms DAILY until a HUMAN ack: a threaded reply on the decision alert
    # (decision_alerts ANSWERED), the CLOSED/RESOLVED heading or the "Recently
    # resolved" tail (fact clears), or Harrison's tap on the tier-3 card.
    if overdue:
        alarmed, suppressed, acked = _gate_escalate(overdue, today=today, dry_run=dry_run)
        if dry_run:
            log.info("[DRY-RUN] check_decision_gates: would record %d ping row(s)",
                     len(alarmed))
        else:
            try:
                decision_lane.record_delivery(
                    [r.get("raw_topic") or r.get("topic", "") for r in alarmed],
                    decision_lane.PING_SURFACE_PREFIX + "health_check")
            except Exception:  # noqa: BLE001 -- evidence never breaks the check
                log.warning("check_decision_gates: ping record failed", exc_info=True)
        notes: list[str] = []
        for row in suppressed:
            notes.append(
                f"gate alarm suppressed pending ack (card {row.get('_card_update_id')}) -- "
                f"[{row.get('severity') or 'P?'}] {row.get('topic')} ({row.get('entity')}), "
                f"{row.get('gate_overdue_days')}d overdue")
        for row in acked:
            # row['topic'] is the REDACTED heading (never raw_topic) -- D-145.
            notes.append(
                f"acknowledged by a human reply on {row.get('_answered_on')} -- "
                f"[{row.get('severity') or 'P?'}] {row.get('topic')} ({row.get('entity')}); "
                f"alarm paused until {row.get('_alarm_paused_until')}, then the ladder "
                "restarts at tier 1")
        if alarmed:
            detail = decision_lane.format_alarm(alarmed)
            if notes:
                detail += "\n" + "\n".join("  - " + n for n in notes)
            return CheckResult("Decision gates", "critical", detail)
        return CheckResult("Decision gates", "warn", "\n".join(notes))
    return CheckResult(
        "Decision gates", "ok",
        f"{len(gated)} of {len(entries)} open decision(s) carry a gate date; "
        "none are past it undelivered.")


_GATE_SIGNAL_TASK = "decision-gate"


def _gate_signal_key(row: dict) -> str:
    """decision-gate|<topic_key>|<entity> -- topic_key is decision_lane's
    normalized heading (the file has no ids), entity the parsed field."""
    from cora import repeat_signal
    return repeat_signal.make_key(
        _GATE_SIGNAL_TASK,
        row.get("topic_key") or str(row.get("topic") or ""),
        str(row.get("entity") or ""))


def _gate_owner(row: dict) -> tuple[str, str]:
    """(slack_id, name) for 'Owner of next nudge', via org_roles.find_by_handle;
    fail-closed to Harrison with a WARN when the text does not resolve."""
    from cora import org_roles, repeat_signal
    owner_text = str(row.get("owner") or "")
    try:
        rec = org_roles.find_by_handle(owner_text)
    except Exception:  # noqa: BLE001
        rec = None
    if rec is not None and rec.slack_id:
        return rec.slack_id, rec.name
    log.warning("check_decision_gates: owner %r not on the roster -- routing the "
                "repeat signal to Harrison", owner_text)
    return repeat_signal.HARRISON_SLACK_ID, "Harrison"


def _gate_reconcile_clears(overdue: list[dict], *, dry_run: bool = False) -> list[str]:
    """A gate signal whose fact has gone away -- the heading now carries CLOSED /
    RESOLVED, the entry moved under '## Recently resolved', the gate date moved,
    or a real surface delivered it -- is no longer in `overdue`. CLEAR it so the
    ledger records the reset and the next blown gate starts at tier 1.

    Returns the keys that cleared (or, under `dry_run`, WOULD clear -- nothing is
    written)."""
    cleared: list[str] = []
    try:
        from cora import repeat_signal
        live = {_gate_signal_key(r) for r in overdue}
        for key in repeat_signal.active_keys(prefix=_GATE_SIGNAL_TASK + "|"):
            if key in live:
                continue
            cleared.append(key)
            if dry_run:
                log.info("[DRY-RUN] check_decision_gates: would clear %s", key)
                continue
            repeat_signal.clear(key, why="gate no longer overdue-undelivered "
                                         "(closed/resolved/delivered/re-dated)")
    except Exception:  # noqa: BLE001 -- bookkeeping never breaks the check
        log.warning("check_decision_gates: clear reconciliation failed", exc_info=True)
    return cleared


def _sticky_answer_day(resolved_at: datetime | None, today_az: date,
                       window_days: int) -> date | None:
    """The AZ date of an answer that still counts as an ack today, else None.

    Sticky iff -1 <= (today_az - answered_day).days <= window_days. None (missing
    or unparseable resolved_at) and anything further in the future than one day of
    skew are NOT sticky -- the fail-closed read is "alarm"."""
    if not isinstance(resolved_at, datetime):
        return None
    try:
        aware = resolved_at if resolved_at.tzinfo is not None else \
            resolved_at.replace(tzinfo=timezone.utc)
        answered_day = aware.astimezone(_AZ).date()
    except Exception:  # noqa: BLE001
        return None
    age = (today_az - answered_day).days
    return answered_day if -1 <= age <= window_days else None


def _gate_escalate(overdue: list[dict], *, today: date | None, dry_run: bool = False
                   ) -> tuple[list[dict], list[dict], list[dict]]:
    """Route each overdue row through repeat_signal.fire (fire_id = today).

    Returns (alarmed, suppressed, acked): `alarmed` renders as the CRITICAL,
    `suppressed` (tier-3 card pending Harrison's tap) renders as ONE warn line
    each, `acked` (a human answered the decision alert in-thread) is excluded --
    a blown gate alarms daily until a HUMAN ack, and this IS the ack. If
    repeat_signal is unavailable the check falls back to pass-through: every row
    alarms (the ladder row's demotion posture).

    `dry_run` classifies exactly as a real run would and writes nothing: the ack
    is skipped (the row still counts as acked) and fire() previews.

    THE ACK IS BOUNDED (Code #14 R14-5, ruling 9.10(ii): "sticky for ONE 8-day
    window, then the ladder restarts at tier 1"). It used to be permanent -- any
    ANSWERED alert on the topic, at any age, silenced the gate forever, including
    an answer recorded weeks BEFORE the gate blew. Now the answer counts only while
    -1 <= (today_az - resolved_at_az) <= decision_lane.DELIVERY_WINDOW_DAYS days
    (the -1 is clock skew, the decision_lane lower-bound posture). A missing,
    unparseable or further-future resolved_at is NOT sticky: the gate alarms. Past
    the window the row falls through to fire(); the first in-window ack reset the
    ledger (consecutive 0, cycle + 1), so the ladder restarts at tier 1 and a later
    tier-3 card carries the cycle-suffixed id."""
    try:
        from cora import decision_alerts, decision_lane, repeat_signal
    except Exception:  # noqa: BLE001
        log.warning("check_decision_gates: repeat_signal unavailable -- pass-through",
                    exc_info=True)
        return list(overdue), [], []
    try:
        answered_at = decision_alerts.answered_at_by_topic_key()
    except Exception:  # noqa: BLE001
        answered_at = {}
    today_az = today or datetime.now(_AZ).date()
    window_days = decision_lane.DELIVERY_WINDOW_DAYS
    fire_id = (today or date.today()).isoformat()
    alarmed: list[dict] = []
    suppressed: list[dict] = []
    acked: list[dict] = []
    for row in overdue:
        key = _gate_signal_key(row)
        try:
            raw_topic = str(row.get("raw_topic") or row.get("topic") or "")
            answered_day = _sticky_answer_day(
                answered_at.get(decision_alerts.topic_key(raw_topic)),
                today_az, window_days)
            if answered_day is not None:
                if not dry_run:
                    repeat_signal.ack(key, via="decision-alert-reply")
                item = dict(row)
                item["_answered_on"] = answered_day.isoformat()
                item["_alarm_paused_until"] = (
                    answered_day + timedelta(days=window_days)).isoformat()
                acked.append(item)
                continue
            owner_id, owner_name = _gate_owner(row)
            outcome = repeat_signal.fire(
                key, fire_id=fire_id,
                # the REDACTED topic (LEX counted-not-itemized / PHI withheld)
                # is what lands in the ledger and on any card
                subject=f"decision gate blown: {row.get('topic')}",
                entity=str(row.get("entity") or ""),
                owner_slack_id=owner_id, owner_name=owner_name,
                normal_surface="#cora-health",
                dry_run=dry_run)
        except Exception:  # noqa: BLE001 -- escalation never silences the alarm
            log.warning("check_decision_gates: repeat_signal.fire failed for %s -- alarming",
                        key, exc_info=True)
            alarmed.append(row)
            continue
        if outcome.suppressed:
            item = dict(row)
            item["_card_update_id"] = outcome.card_update_id
            suppressed.append(item)
        else:
            alarmed.append(row)
    return alarmed, suppressed, acked


_WATCHDOG_TASK = "cora-watchdog"
# The watchdog logs a tick at most hourly when healthy, so >3h of silence means it
# is not running -- not that it saw nothing wrong.
_WATCHDOG_TICK_STALE_HOURS = 3.0
_WATCHDOG_ESCALATE_EVENTS = frozenset({
    "watchdog_error",
    "restart_unverified",
    "restart_blocked_not_elevated",
    "no_heartbeat_file",
    "thrash_guard_hold",
})


def check_windowless_launcher() -> CheckResult:
    r"""The windowless launcher is a single point of failure for the estate.

    Every Cora task's action runs `pythonw.exe deployment
un_hidden.py -- ...`,
    so if the launcher or pythonw.exe goes missing -- a branch checkout, a venv
    rebuild -- ALL of them fail at once, and they fail SILENTLY: pythonw has no
    stderr to complain to. Worse, the things that would report it (this check,
    the security monitor, the watchdog) are themselves scheduled tasks behind
    the same launcher.

    This is the cheap out-of-band-ish guard: assert the two files exist, and
    report any Cora task whose action is NOT wrapped -- which is how a re-run
    setup script on a host without the venv silently puts a flashing window
    back (New-WrappedTaskAction degrades to a console action by design, because
    an unregistered task is worse than a visible one).
    """
    launcher = _REPO_ROOT / "deployment" / "run_hidden.py"
    pythonw = _REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    missing = [str(p) for p in (launcher, pythonw) if not p.exists()]
    if missing:
        return CheckResult(
            "Windowless launcher", "critical",
            "MISSING: " + ", ".join(missing) +
            " -- every wrapped Cora task fails silently until this is restored.",
        )

    if os.name != "nt":
        return CheckResult("Windowless launcher", "ok", "launcher present (task scan is Windows-only)")

    ps = (
        "Get-ScheduledTask | Where-Object { $_.TaskName -like 'Cora - *' -or "
        "$_.TaskName -like 'cowork-cora-*' -or $_.TaskName -eq 'cora-watchdog' } | "
        "ForEach-Object { Write-Output ($_.TaskName + '|' + $_.State + '|' + "
        "$_.Actions[0].Execute + '|' + $_.Actions[0].Arguments) }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=60,
            creationflags=_NO_WINDOW,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Windowless launcher", "warn", f"task scan failed: {exc}")

    unwrapped: list[str] = []
    total = 0
    for line in out.splitlines():
        parts = line.rstrip("\r").split("|", 3)
        if len(parts) < 3:
            continue
        name, state = parts[0].strip(), parts[1].strip()
        execute = parts[2].strip()
        args = parts[3].strip() if len(parts) > 3 else ""
        total += 1
        # A disabled task never fires, so it never flashes a window; the rewrap
        # script skips those by design and this must agree with it or it would
        # report 18 permanent "failures".
        if state.lower() == "disabled":
            continue
        if not (execute.lower().endswith("pythonw.exe") and "run_hidden.py" in args):
            unwrapped.append(name)

    if not total:
        return CheckResult("Windowless launcher", "warn", "task scan returned no tasks")
    if unwrapped:
        return CheckResult(
            "Windowless launcher", "warn",
            f"{len(unwrapped)} of {total} Cora task(s) are NOT windowless and will flash a "
            f"console window at every fire: {', '.join(sorted(unwrapped)[:8])}"
            + (" ..." if len(unwrapped) > 8 else "")
            + r" -- fix with (ELEVATED) deployment\rewrap-tasks-hidden.ps1 -Apply",
        )
    return CheckResult(
        "Windowless launcher", "ok",
        f"launcher present; all {total} enabled Cora task(s) run windowless",
    )


def check_watchdog_liveness(now: datetime | None = None) -> CheckResult:
    """Is the auto-recovery layer itself alive? (cq-7915a8647cff)

    The 8/18 forensics hit a wall precisely here: heartbeat.txt looked 29h stale
    with ZERO watchdog log lines, and there was no way to tell "the watchdog ran
    and was happy" (which logged nothing at all) from "the watchdog never ran".
    Live 8/19 the task was in fact stuck: State=Running with no process behind it
    and LastTaskResult=0x80070420 ALREADY_RUNNING, which under
    MultipleInstances=IgnoreNew silently rejects every 5-minute trigger for up to
    the task's ExecutionTimeLimit (PT72H as registered). Nothing alarmed.

    The watchdog now emits a periodic `tick`, so this check reads two things:
      * tick freshness -- silence is now unambiguous evidence it is not running;
      * escalation events in the last 24h (unverified restart, non-elevated
        watchdog, watchdog_error, thrash hold).

    A watchdog that is not running is CRITICAL: it is the only auto-recovery for a
    hung bot, and a hang produces no failure exit code for RestartOnFailure.
    `now` is injectable for tests.
    """
    now = now or datetime.now(timezone.utc)
    rows: list[dict] = []
    for path in sorted(_LOG_DIR.glob("watchdog-*.jsonl"))[-3:]:
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(row, dict):
                rows.append(row)

    def _age_h(row: dict) -> float | None:
        raw = str(row.get("ts") or "")
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 3600

    # NEGATIVE ages are discarded, and that is a fail-OPEN fix (D-051 lens-4 HIGH,
    # caught before merge). `min()` over a list containing a future-dated row picks
    # the most negative one, so ONE row stamped ahead -- an NTP correction, a VM
    # resume, a hand-edited file -- reported "newest watchdog line -500.0h old" and
    # returned OK while the watchdog was genuinely 29h dead. Worse, it stays
    # poisoned: the check reads the three newest FILES, and a dead watchdog creates
    # no new file, so the alarm would have been green permanently in exactly the
    # scenario it exists for.
    ages = [a for a in (_age_h(r) for r in rows) if a is not None and a >= 0]
    future = [a for a in (_age_h(r) for r in rows) if a is not None and a < 0]
    newest = min(ages) if ages else None

    escalations = []
    for row in rows:
        if row.get("event") in _WATCHDOG_ESCALATE_EVENTS or row.get("action") == "ESCALATE_ALERT":
            age = _age_h(row)
            if age is not None and age <= 24:
                escalations.append(f"{row.get('event')} ({age:.0f}h ago)")

    if future:
        # Reported, never silently dropped: a clock jump on this host is itself
        # worth knowing about, and it is the input that used to blind the check.
        log.warning("check_watchdog_liveness: %d future-dated watchdog row(s) "
                    "ignored (clock jump?)", len(future))

    if newest is None:
        return CheckResult(
            "Cora watchdog", "critical",
            f"No watchdog activity logged at all"
            + (f" ({len(future)} future-dated row(s) ignored -- clock jump?)" if future else "")
            + " -- the only auto-recovery for a HUNG "
            "bot may not be running. Check: schtasks /Query /TN cora-watchdog /V /FO LIST "
            "(a State=Running task with no process blocks every trigger; "
            "schtasks /End /TN cora-watchdog clears it).")

    if newest > _WATCHDOG_TICK_STALE_HOURS:
        return CheckResult(
            "Cora watchdog", "critical",
            f"Newest watchdog log line is {newest:.1f}h old (a tick is expected at least "
            f"hourly) -- the watchdog is not running. A stuck State=Running instance "
            f"silently rejects every trigger; clear it with "
            f"schtasks /End /TN {_WATCHDOG_TASK}.")

    if escalations:
        return CheckResult(
            "Cora watchdog", "warn",
            f"Running (newest line {newest:.1f}h old) but {len(escalations)} escalation(s) "
            f"in 24h: " + ", ".join(escalations[:5]))

    return CheckResult(
        "Cora watchdog", "ok",
        f"Alive -- newest watchdog line {newest:.1f}h old, no escalations in 24h.")


def check_info_for_cora_watermark(now: datetime | None = None) -> CheckResult:
    """Is the #info-for-cora sweep alive, and is it frozen?

    C7 (cq-77984df448c7). These are TWO facts and the old check conflated them.
    It used the watermark FILE's mtime as its liveness proxy, on the stated
    premise that "only a running sweep writes it". That premise is false against
    the live script: the watermark records the newest PROCESSED MESSAGE and is
    written only when `high_water` is set, so a run over a quiet channel
    completes, returns 0 and touches nothing. mtime therefore tracked CHANNEL
    TRAFFIC. With the sweep at 06:05 and this check at 08:45 against a 48h
    threshold, exactly two consecutive quiet days read as 50.7h and fired.

    The live history is a sawtooth keyed to channel posts -- 51/75/99/147/.../243h,
    silent 8/18-19, 51/75h, silent 8/22-23, 51h on 8/24 -- reported as a "3-week
    freeze". The sweep was never frozen: Ready, LastTaskResult 0, 0 missed runs,
    and it advanced correctly on 8/22 to the parent ts of Hannah's 8/21 thread.

    And a GENUINE freeze was undetectable: that path also returns 0 and writes
    nothing, byte-identical to a quiet day. So the warning could not be retired
    either -- it had simply never been able to see the thing it was built for.

    Now: the sweep stamps `info-for-cora-runstate.json` on EVERY run with its
    outcome, and this reads liveness from that and frozen-ness from the recorded
    outcome. `now` is injectable for tests."""
    now = now or datetime.now(timezone.utc)
    state_dir = _REPO_ROOT / "data" / "state"
    runstate = state_dir / "info-for-cora-runstate.json"
    watermark = state_dir / "info-for-cora-watermark.json"

    if not runstate.exists():
        # Pre-C7 hosts, and a fresh install, have no marker yet. Fall back to the
        # honest half of the old check -- the sweep bootstraps on first run --
        # rather than alarming on the absence of a file this build introduced.
        if not watermark.exists():
            return CheckResult(
                "info-for-cora sweep", "ok",
                "No watermark yet -- the sweep bootstraps on its first run.")
        return CheckResult(
            "info-for-cora sweep", "ok",
            "No run-state marker yet -- it appears at the sweep's next run. "
            "Watermark age is NOT a liveness signal (it tracks channel traffic).")

    try:
        data = json.loads(runstate.read_text(encoding="utf-8"))
        last_run = datetime.fromisoformat(str(data.get("last_run_ts") or ""))
        outcome = str(data.get("outcome") or "")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("info-for-cora sweep", "warn",
                           f"run-state marker unreadable ({exc}) -- cannot tell "
                           f"whether the sweep is running.")

    run_age_h = (now - last_run).total_seconds() / 3600
    if run_age_h > 48:
        return CheckResult(
            "info-for-cora sweep", "warn",
            f"last ran {run_age_h:.0f}h ago (expected daily) -- the task is not "
            f"firing. Check logs/info-for-cora-sweep-<date>.log.")
    if outcome == "frozen":
        return CheckResult(
            "info-for-cora sweep", "warn",
            f"ran {run_age_h:.0f}h ago but FROZE its watermark (poison-pill "
            f"message or unfetched tail) -- intake is stalled at the same point "
            f"every run. Check logs/info-for-cora-sweep-<date>.log.")
    if outcome == "locked":
        return CheckResult(
            "info-for-cora sweep", "warn",
            f"last run {run_age_h:.0f}h ago exited on a held lock -- if this "
            f"repeats, a stale lockfile is blocking every run.")
    return CheckResult("info-for-cora sweep", "ok",
                       f"Ran {run_age_h:.0f}h ago, outcome={outcome or 'unknown'}. "
                       f"(A quiet channel leaves the watermark untouched; that is "
                       f"not a fault.)")


def check_cashflow_forecast_snapshot(today: date | None = None) -> CheckResult:
    """The 13WCF forecast snapshot (S1) must fire every Monday.

    This is the most loss-critical job in the estate. The Standing ACTUALS sheet
    overwrites its FORECAST column in place once a week closes (D-121), so a
    Monday that goes unsnapshotted is forecast history destroyed permanently --
    no later run can recover it, and forecast accuracy stays unmeasurable for
    that week forever.

    WARNs from MONDAY, not Tuesday. This check runs once daily at 08:45 against
    a job that fires 06:15 with a 20-minute limit, so by the time it runs the
    Monday outcome is already final -- and the sheet refresh has been observed
    landing Monday afternoon. Warning on Monday leaves hours of recovery
    runway; waiting until Tuesday means every miss is reported only once it is
    permanently unrecoverable.

    A dated FILE is not evidence of a banked week: a run where every tab failed
    used to write an empty snapshot that read as green. Coverage is checked too.
    `today` is injectable for tests.
    """
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    label = "13wk cashflow snapshot"

    try:
        from cora import cashflow_ledger as cl  # noqa: PLC0415
        # not_after=today: a stray future-dated file must not masquerade as the
        # newest snapshot and blind this check for months.
        latest = cl.latest_snapshot_date(not_after=today)
        coverage = cl.snapshot_coverage(latest) if latest else None
    except Exception as exc:  # noqa: BLE001
        return CheckResult(label, "warn", f"could not read the snapshot store: {exc}")

    if latest is None:
        return CheckResult(
            label, "warn",
            "No forecast snapshot has ever been banked. Every week without one "
            "is forecast history lost permanently. Run "
            r"scripts\run_cashflow_forecast_snapshot.py.")

    if latest < monday:
        return CheckResult(
            label, "warn",
            f"No snapshot for the week of {monday.isoformat()} (latest is "
            f"{latest.isoformat()}). 'cowork-cora-cashflow-forecast-snapshot' fires "
            "Monday 06:15 -- if today is Monday there is still time to run it by "
            "hand before the sheet is refreshed; this week's forecast history is "
            "being lost.")

    if coverage is None:
        return CheckResult(
            label, "warn",
            f"Snapshot {latest.isoformat()} exists but its coverage could not be "
            "read -- it may be truncated or corrupt.")

    covered, expected = coverage
    if covered < expected:
        return CheckResult(
            label, "warn",
            f"Snapshot {latest.isoformat()} covers only {covered} of {expected} "
            "tabs -- the missing entities have no forecast banked for this week.")
    return CheckResult(
        label, "ok",
        f"This week's snapshot is banked ({latest.isoformat()}, "
        f"{covered}/{expected} tabs).")


def check_cashflow_actuals(today: date | None = None) -> CheckResult:
    """The 13WCF QBO actuals (S2) should fire every Monday.

    URGENCY IS DELIBERATELY LOWER THAN ITS SIBLING ABOVE, and saying so is the
    point. The forecast snapshot is irreplaceable -- the sheet overwrites the
    column it reads (D-121). These windows are NOT: QBO is re-readable, which is
    the whole premise of the finalized re-pull, so a missed Monday is recovered by
    running the script (this week) or with `--date` (an older week). Warning at
    the same pitch as an unrecoverable loss is how a reader learns to skip both.

    A dated FILE is not evidence of a banked week, so coverage is read from the
    payload rather than inferred from the filename (D-127c). `today` is injectable.
    """
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    label = "13wk cashflow actuals"

    try:
        from cora import cashflow_actuals as ca  # noqa: PLC0415

        weeks = [w for w in ca.list_finalized_weeks() if w <= today]
        latest = weeks[-1] if weeks else None
        coverage = ca.window_coverage(latest, ca.WINDOW_FINALIZED) if latest else None
    except Exception as exc:  # noqa: BLE001
        return CheckResult(label, "warn", f"could not read the actuals store: {exc}")

    if latest is None:
        return CheckResult(
            label, "warn",
            "No finalized actuals window has been banked yet. Register "
            "'cowork-cora-cashflow-actuals' or run "
            r"scripts\run_cashflow_actuals.py. Recoverable -- QBO is re-readable.")

    # HOLES, not just staleness. A skipped Monday is never recovered on its own:
    # StartWhenAvailable fires ONE catch-up and that run derives its windows from
    # the then-current date, so the intervening week gets no finalized file ever.
    # A check that only inspects the newest week reports green forever while a
    # permanent gap sits behind it -- and every accuracy consumer silently skips
    # that week. Verified by a D-051 reviewer against this exact code.
    missing = [
        (weeks[i] + timedelta(days=7 * step)).isoformat()
        for i in range(len(weeks) - 1)
        for step in range(1, (weeks[i + 1] - weeks[i]).days // 7)
    ]
    if missing:
        shown = ", ".join(missing[:4]) + ("..." if len(missing) > 4 else "")
        return CheckResult(
            label, "warn",
            f"{len(missing)} week(s) have NO finalized actuals window and never "
            f"will unless backfilled: {shown}. Recover each with "
            r"scripts\run_cashflow_actuals.py --window final --week <YYYY-MM-DD>.")

    # The finalized window trails the run by two weeks by design, so "current"
    # means its week-ending sits within ~16 days of this Monday. A looser bound
    # than the snapshot check because the lag is structural, not a fault.
    if (monday - latest).days > 16:
        return CheckResult(
            label, "warn",
            f"Newest finalized actuals window is {latest.isoformat()}, more than "
            "two weeks behind. 'cowork-cora-cashflow-actuals' fires Monday 06:25 "
            "-- re-run it, or backfill a specific week with "
            "--window final --week <YYYY-MM-DD>.")

    if coverage is None:
        return CheckResult(
            label, "warn",
            f"Window {latest.isoformat()} exists but its coverage could not be "
            "read -- it may be truncated or corrupt.")

    covered, expected = coverage
    payload = ca.load_finalized(latest) or {}
    awaiting = payload.get("awaiting_map_confirmation") or []
    pending = f" ({len(awaiting)} awaiting map confirmation)" if awaiting else ""

    if covered < expected:
        return CheckResult(
            label, "warn",
            f"Window {latest.isoformat()} covers only {covered} of {expected} "
            f"realms -- the rest have no actuals for that week.{pending}")
    return CheckResult(
        label, "ok",
        f"Finalized actuals current ({latest.isoformat()}, "
        f"{covered}/{expected} realms){pending}.")


def check_cashflow_worksheet(today: date | None = None) -> CheckResult:
    """The 13WCF Monday worksheet (S3) must appear every week.

    WHY THIS CHECK EXISTS. Unlike S1 and S2, the worksheet has no job of its
    own: it is built inside the close pack and written by
    `run_finance_close_pack.py`. Both layers are fail-soft by design -- the pack
    must survive a worksheet failure -- so a build that raises every Monday, or
    a local write that fails on a full disk, produces exactly one `log.error`
    line on a headless 09:00 task and nothing else. The worksheet is not a
    pack Section, so it never reaches Slack as an unavailable stub either.

    Meanwhile the pack keeps POINTING at it: the forecast-assist section renders
    "full table in the Monday worksheet" and names the folder. A reader sent to
    a folder that has not gained a file since the merge has no way to tell a
    broken job from a quiet week.

    Unlike the S1 snapshot this is RECOVERABLE -- the worksheet is derived from
    banked stores, so `--worksheet-only` regenerates it -- which is why this
    warns rather than shouting, and why it tolerates the pack's own dedup
    (a week the pack legitimately skipped has no worksheet and should not warn
    if no pack ran either). `today` is injectable for tests.
    """
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    label = "13wk cashflow worksheet"

    try:
        from cora import cashflow_worksheet as cw  # noqa: PLC0415
        directory = cw.WORKSHEET_DIR
        dates: list[date] = []
        if directory.exists():
            for path in directory.glob("*_fndr_cashflow-worksheet.md"):
                try:
                    stamp = date.fromisoformat(path.name.split("_", 1)[0])
                except ValueError:
                    continue
                # A stray future-dated file must not become the max() and blind
                # this check for weeks (the D-127(c) lesson).
                if stamp <= today:
                    dates.append(stamp)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(label, "warn", f"could not read the worksheet store: {exc}")

    if not dates:
        return CheckResult(
            label, "warn",
            "No Monday worksheet has ever been written, but the close pack's "
            "forecast-assist section points readers at "
            "cashflow-ledger/worksheets/. Regenerate with "
            "scripts/run_finance_close_pack.py --worksheet-only.")

    latest = max(dates)
    if latest < monday:
        return CheckResult(
            label, "warn",
            f"No worksheet for the week of {monday.isoformat()} (latest is "
            f"{latest.isoformat()}). The close pack builds it fail-soft, so a "
            "broken build is otherwise one log line. Regenerate with "
            "scripts/run_finance_close_pack.py --worksheet-only -- it is derived "
            "from banked stores, so this is recoverable.")

    return CheckResult(
        label, "ok", f"This week's worksheet is written ({latest.isoformat()}).")


_MCP_HTTP_TASK = "cowork-cora-mcp-http"


def check_mcp_http_bridge(port: int | None = None) -> CheckResult:
    """WARN if the MCP local-HTTP bridge task (scripts/run_mcp_server_http.py,
    2026-07-30 kickoff, extends D-092) is registered but has stopped answering
    on its loopback port. The task is OPTIONAL and opt-in (Harrison's manual
    GO/NO-GO smoke gates registration) -- if it was never registered, this is
    a silent OK, never a WARN; registering it is not required. `port` is
    injectable for tests."""
    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", _MCP_HTTP_TASK, "/FO", "LIST"],
            capture_output=True, text=True, timeout=30,
            creationflags=_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("MCP HTTP bridge", "ok", f"schtasks query failed (non-fatal): {exc}")
    if proc.returncode != 0:
        return CheckResult(
            "MCP HTTP bridge", "ok",
            "Not registered (optional; see deployment/setup-cora-mcp-http-task.ps1).")

    if port is None:
        try:
            port = int(os.environ.get("CORA_MCP_HTTP_PORT", "8791"))
        except ValueError:
            port = 8791
    import socket  # noqa: PLC0415
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass
    except OSError as exc:
        return CheckResult(
            "MCP HTTP bridge", "warn",
            f"Task '{_MCP_HTTP_TASK}' is registered but 127.0.0.1:{port} is not "
            f"answering: {exc}")
    return CheckResult("MCP HTTP bridge", "ok", f"Registered and 127.0.0.1:{port} is answering.")


_DYNAMIC_ANSWERS_DIR = _REPO_ROOT / "design" / "known-answers" / "dynamic"


def check_claude_mirror(now_epoch: float | None = None) -> CheckResult:
    """WARN on the claude-workspace mirror lane (S5). Reads the structured
    ``mirror-status.json`` the mirror writes to ZONE-K each run and alarms on:
    the report missing/unreadable, a run older than 26h (a stalled mirror silently
    freezes the out-of-tree knowledge parity), a source-class root NOT FOUND, a
    new skill not allowlisted, or a task-estate change (added/removed/model
    changed -- the registry-drop detector 1nnnn never had). New quarantine counts
    are surfaced in the Monday digest, not alarmed nightly (they are expected to
    be non-zero by design, D-194). Report, never absorb (D-214/D-249).

    Never registered / never run -> a single OK (the lane is Harrison-gated at S6),
    same posture as check_mcp_http_bridge. `now_epoch` accepted for signature
    parity; the age comes from the status file's own timestamp."""
    try:
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        import mirror_claude_workspace as mw  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Claude mirror", "ok",
                           f"mirror module not importable (non-fatal): {exc}")
    st = mw.read_parity_status()
    if not st.get("available"):
        # The lane may simply not have run yet (Harrison-gated first apply, S6).
        # A missing status file is an OK-until-adopted, not a nightly WARN.
        return CheckResult("Claude mirror", "ok",
                           f"No mirror-status.json yet ({st.get('error', 'absent')}) -- "
                           f"lane not yet run (register + first --apply is Harrison-gated).")
    problems: list[str] = []
    age = st.get("age_hours")
    age_s = f"{age:.0f}h" if isinstance(age, (int, float)) else "unknown-age"
    if age is None:
        # A present status file with an unparseable/absent timestamp is itself a
        # WARN (a schema drift or a partial write) -- but must NEVER crash the run
        # (D-051 bootstrap finding 1: f"{None:.0f}" is a TypeError that would abort
        # the whole nightly report).
        problems.append("status file has no parseable timestamp")
    if st.get("stale"):
        problems.append(f"last run {age_s} ago (> {st.get('max_age_hours', 26)}h)")
    if st.get("roots_missing"):
        problems.append("roots NOT FOUND: " + ", ".join(st["roots_missing"]))
    if st.get("unknown_skills"):
        problems.append("skills not allowlisted: " + ", ".join(st["unknown_skills"]))
    for k in ("added", "removed", "model_changed"):
        if st.get(k):
            problems.append(f"task-estate {k}: " + ", ".join(str(x) for x in st[k][:10]))
    # Code #13 slice 9c (cq-06045f418bd2): the Cowork-estate run-marker read. The
    # mirror diffs each task's newest _runs/<folder-id>/<date>.json against its
    # DECLARED cadence (data/maps/cowork-run-cadence.yaml) and summarizes here. A
    # task with a cadence row and no marker inside 2x its cadence is 'did not run'
    # -- never 'ok' -- and that MUST reach the nightly report, or the estate looks
    # healthy for exactly the reason the contract exists (a task that fires and
    # writes nothing is indistinguishable from one that never fired).
    rm = st.get("run_markers") or {}
    if isinstance(rm, dict):
        for key, label in _RUN_MARKER_ALARM_KEYS:
            ids = rm.get(key) or []
            if ids:
                problems.append(f"Cowork run markers {label}: "
                                + ", ".join(str(x) for x in ids[:10]))
    coverage = rm.get("coverage") if isinstance(rm, dict) else None
    if problems:
        return CheckResult("Claude mirror", "warn",
                           "claude-workspace mirror: " + "; ".join(problems)
                           + f" (quarantined={st.get('quarantined_count', '?')}"
                           + (f", run-markers={coverage}" if coverage else "") + ").")
    return CheckResult("Claude mirror", "ok",
                       f"mirror fresh ({age_s}), "
                       f"quarantined={st.get('quarantined_count', 0)}, "
                       f"unpinned={len(st.get('unpinned', []))}"
                       + (f", run-markers={coverage}" if coverage else "") + ".")


# (status-payload key, human label) -- the mirror's run-marker statuses the nightly
# report WARNs on. The wording mirrors run_marker.evaluate's two alarms (MISSED FIRE /
# FIRED BUT WROTE NOTHING) plus the two file-drop-specific ones.
_RUN_MARKER_ALARM_KEYS: tuple[tuple[str, str], ...] = (
    ("did_not_run", "did not run"),
    ("wrote_nothing", "fired but wrote nothing"),
    ("unreadable", "unreadable marker"),
    ("reported_error", "reported an error"),
)


def check_session_capture_quarantine(
    now: datetime | None = None, ledger_path: Path | None = None,
) -> CheckResult:
    """WARN when the session harvester QUARANTINED a capture in the last 26h
    (ingest-integrity I2, cq-bc5e5b7512bd, 2026-09-08).

    A quarantined note is a non-LEX Code/Cowork session whose transcript carried
    value-shaped PHI (a DOB value, a programme id number, a diagnosis tied to a
    named individual). It is HELD in the founder-only, KB-excluded folder
    _shared/projects/cora/_session-capture-quarantine/ instead of being filed --
    and never re-homed to LEX (Leak #2). The harvester ledger
    (logs/session-captures.jsonl) carries ``quarantined: true`` on those rows;
    this reads them so a hold is surfaced daily, not discovered months later.
    Missing / unreadable ledger -> OK (the harvester's own log is the primary
    record; this is the alert line). WARN, never CRITICAL."""
    now = now or datetime.now(timezone.utc)
    path = ledger_path or (_REPO_ROOT / "logs" / "session-captures.jsonl")
    if not path.exists():
        return CheckResult("Session-capture quarantine", "ok",
                           "no harvester ledger yet -- nothing to read.")
    recent: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not row.get("quarantined"):
                continue
            ts_raw = str(row.get("refiled_at") or row.get("captured_at") or "")
            try:
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if (now - ts).total_seconds() <= 26 * 3600:
                recent.append(str(row.get("session_id") or "?")[:8])
    except Exception as exc:  # noqa: BLE001 -- report, never crash the run
        return CheckResult("Session-capture quarantine", "ok",
                           f"ledger unreadable (non-fatal): {exc}")
    if recent:
        return CheckResult(
            "Session-capture quarantine", "warn",
            f"{len(recent)} capture(s) QUARANTINED in the last 26h (PHI-risk on a non-LEX "
            f"session; founder review under _shared/projects/cora/_session-capture-quarantine/): "
            + ", ".join(recent[:10]))
    return CheckResult("Session-capture quarantine", "ok", "no quarantined captures in the last 26h.")


def check_quarantine_folder_isolation(kb_path: Path | None = None,
                                      founder_os_root: Path | None = None) -> CheckResult:
    """WARN when a quarantined capture could reach a reader through the KB.

    The quarantine folder is founder-only BY PLACEMENT: it lives inside the
    pinned, path-excluded Cora workspace (_shared/projects/cora), its files carry
    a belt-tripping name, and the harvester never --with-kb ingests one. This
    probe checks the two things a code change could silently break: (1) the
    folder path still resolves as Cora-internal (kb_exclusions), and (2) no KB row
    carries a quarantine path or a quarantine-prefixed title. The Drive SHARING ACL
    of the folder is Harrison's to verify by eye (Drive UI); it is not probed here
    (D-051 lens A: the ACL was unstated).
    """
    try:
        from cora import kb_exclusions, session_capture as scap
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Quarantine folder isolation", "ok", f"modules unavailable (non-fatal): {exc}")
    root = founder_os_root or scap.FOUNDER_OS_ROOT
    qdir = scap.quarantine_root(root)
    problems: list[str] = []
    try:
        if not kb_exclusions.is_cora_internal_path(qdir / "2026-09" / "cora-quarantine-x.md"):
            problems.append("quarantine folder is NOT inside the Cora-internal path exclusion")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"path-rule check failed: {exc}")
    db = kb_path or _KB_DB
    if db.exists():
        try:
            from cora.knowledge_base import schema
            conn = schema.connect(db, read_only=True)
            try:
                n = conn.execute(
                    "SELECT count(*) FROM knowledge_chunks WHERE source_id LIKE ? OR title LIKE ? OR title LIKE ?",
                    (f"%{scap.QUARANTINE_DIRNAME}%", f"{scap.QUARANTINE_PREFIX}%",
                     f"Session capture \u2014 {scap.QUARANTINE_PREFIX}%"),
                ).fetchone()[0]
            finally:
                conn.close()
            if n:
                problems.append(f"{n} KB row(s) carry a quarantine path/title -- a quarantined capture was ingested")
        except Exception as exc:  # noqa: BLE001
            return CheckResult("Quarantine folder isolation", "ok", f"KB unreadable (non-fatal): {exc}")
    if problems:
        return CheckResult("Quarantine folder isolation", "warn", "; ".join(problems))
    return CheckResult("Quarantine folder isolation", "ok",
                       "quarantine folder is path-excluded and no KB row carries it (Drive sharing ACL: founder-verified by eye).")


def check_dynamic_snapshots(now_epoch: float | None = None) -> CheckResult:
    """WARN when a dynamic-answers snapshot is missing or stale past its yaml
    threshold (D-084). context_loader serves the yaml `fallback` in that case --
    honest but permanently stale if nothing refreshes the snapshot. Surfacing it
    daily turns silent rot into a visible signal. `now_epoch` is injectable for
    tests. Reads design/known-answers/dynamic/<entity>/*.yaml."""
    now = now_epoch if now_epoch is not None else time.time()
    if not _DYNAMIC_ANSWERS_DIR.exists():
        return CheckResult("Dynamic snapshots", "ok", "No dynamic-answers directory.")
    try:
        import yaml  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Dynamic snapshots", "warn", f"yaml import failed: {exc}")
    stale: list[str] = []
    checked = 0
    # D-051: the per-file body is fully fail-soft -- a malformed yaml (non-dict source,
    # non-numeric threshold, stat race) must never propagate and abort the WHOLE nightly
    # report before it posts to Slack.
    for yaml_path in sorted(_DYNAMIC_ANSWERS_DIR.glob("*/*.yaml")):
        label = f"{yaml_path.parent.name}/{yaml_path.stem}"
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                continue
            snap_rel = raw.get("snapshot_path")
            if not snap_rel:
                continue
            checked += 1
            src = raw.get("source")
            src = src if isinstance(src, dict) else {}
            threshold_h = float(src.get("staleness_threshold_hours", 24))
            snap = _REPO_ROOT / snap_rel
            if not snap.exists():
                stale.append(f"{label}: snapshot MISSING ({snap_rel})")
                continue
            age_h = (now - snap.stat().st_mtime) / 3600.0
            if age_h > threshold_h:
                stale.append(f"{label}: {age_h:.0f}h old > {threshold_h:.0f}h threshold")
        except Exception as exc:  # noqa: BLE001 -- one bad file never aborts the report
            stale.append(f"{label}: could not evaluate ({exc})")
            continue
    if stale:
        return CheckResult(
            "Dynamic snapshots", "warn",
            f"{len(stale)} of {checked} dynamic snapshot(s) stale/missing -- Cora is "
            f"serving the yaml fallback for these:\n"
            + "\n".join(f"  - {s}" for s in stale)
            + "\n  (No auto-refresh writer exists for these seeds -- wire a refresher "
            "or retire the dynamic-answers feature. See D-084.)",
        )
    return CheckResult("Dynamic snapshots", "ok",
                       f"{checked} dynamic snapshot(s) within staleness threshold.")


# Founder CLAUDE.md must be re-swept at least this often (daily static_md sweep + buffer).
_FOUNDER_KB_STALE_HOURS = 30


def check_founder_kb_freshness(now_epoch: float | None = None) -> CheckResult:
    """FNDR/HJRG current-state is now RETRIEVAL-ONLY (D-084 slim), so the founder
    CLAUDE.md's KB copy is FNDR's SOLE current-state path. WARN if it isn't indexed,
    or its newest chunk hasn't been re-ingested recently (a stalled static_md sweep
    would silently freeze FNDR's current-state without any other alarm firing).
    `now_epoch` injectable for tests."""
    now = now_epoch if now_epoch is not None else time.time()
    if not _KB_DB.exists():
        return CheckResult("Founder KB freshness", "warn", f"KB db missing at {_KB_DB}")
    try:
        conn = sqlite3.connect(str(_KB_DB))
        try:
            row = conn.execute(
                "SELECT COUNT(*), MAX(ingested_at) FROM knowledge_chunks "
                "WHERE entity='FNDR' AND source='static_md' AND source_id='CLAUDE.md'"
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Founder KB freshness", "warn", f"KB query failed: {exc}")
    count, newest = (row or (0, None))
    if not count or not newest:
        return CheckResult(
            "Founder KB freshness", "warn",
            "Founder CLAUDE.md is NOT indexed under FNDR/static_md -- FNDR/HJRG "
            "current-state (retrieval-only since D-084) has no source. Check the "
            "static_md KB sweep.")
    age_h = (now - float(newest)) / 3600.0
    if age_h > _FOUNDER_KB_STALE_HOURS:
        return CheckResult(
            "Founder KB freshness", "warn",
            f"Founder CLAUDE.md newest KB chunk is {age_h:.0f}h old (> "
            f"{_FOUNDER_KB_STALE_HOURS}h) -- the static_md sweep may have stalled; "
            "FNDR current-state retrieval is going stale (D-084).")
    return CheckResult(
        "Founder KB freshness", "ok",
        f"Founder CLAUDE.md indexed ({count} chunks, newest {age_h:.0f}h ago).")


_LOG_LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")


def _line_within(line: str, cutoff: datetime) -> bool:
    """Is this log line newer than `cutoff`?

    UNSTAMPED lines return True on purpose: a traceback's continuation lines carry
    no timestamp, and those are the lines a critical pattern most needs to keep.
    Both live formats parse -- "2026-08-19T15:28:25 INFO ..." (the bot) and
    "2026-08-19 06:33:41,929 [INFO] ..." (the scripts).
    """
    m = _LOG_LINE_TS_RE.match(line)
    if not m:
        return True
    try:
        stamped = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return stamped >= cutoff


# Code #14 S2 (cq-0f04ad6543a8): the health check's OWN log. main() logs every
# non-OK result as "<ts> INFO health-check: [<STATUS>] <name>: <detail>", and that
# file (logs/health-check-YYYY-MM-DD.log) is inside this scan's own glob and 26h
# window. The "Critical log patterns" detail QUOTES the matched line, so once the
# detail is logged in full (S2) the next morning's scan would re-match the quote and
# re-raise the same CRITICAL forever -- each day's report re-logging the previous
# hit (live precedent: health-check-2026-08-28.log:27-28 carries exactly such a
# quoted snippet; only the old [:100] cut kept the matched text out).
#
# The exclusion is deliberately NARROWER than "skip the whole file": a real
# writer failure raised INSIDE this process (REPEAT_SIGNAL_WRITE_FAILING from the
# decision-gate check, RUN_MARKER_WRITE_FAILING) is logged to this same file by
# the root handler and must stay visible. So in the check's own files ONLY the
# report lines (and the unstamped continuation lines of a legacy multi-line
# report detail) are skipped. Plain string tests -- no new regex (D-171).
_OWN_LOG_PREFIX = "health-check-"
_OWN_REPORT_MARKERS = tuple(
    f" health-check: [{s}] " for s in ("OK", "WARN", "CRITICAL", "FIXED"))


def _is_own_log(path: Path) -> bool:
    return path.name.startswith(_OWN_LOG_PREFIX)


def check_logs_24h() -> list[CheckResult]:
    """Scan last 24h log files for ERRORs and critical patterns."""
    results: list[CheckResult] = []
    cutoff = datetime.now() - timedelta(hours=26)

    # Recency is MTIME, never the date in the filename (cq-7915a8647cff).
    # TimedRotatingFileHandler pins the bot's live log to the process START date:
    # an instance started 8/17 was still appending 8/19's lines to
    # cora-2026-08-17.log. The old today/yesterday NAME filter ANDed that file
    # out, so on any instance older than a day this scan silently skipped the
    # live bot log -- i.e. every critical pattern below went unseen in exactly
    # the long-uptime case.
    # BOTH globs (D-051 lens-4 MEDIUM): the first cut fixed recency and left
    # coverage broken. The comment above correctly names the rotated form
    # "cora-<startdate>.log.<thatday>" -- and then filtered with a pattern that
    # cannot match it. Measured on this host: 91 rotated files, one of them
    # (140,849 bytes) written INSIDE the 26h window and still skipped.
    log_files = list(_LOG_DIR.glob("*.log")) + list(_LOG_DIR.glob("*.log.*"))
    recent = [f for f in log_files if f.stat().st_mtime > cutoff.timestamp()]

    if not recent:
        results.append(CheckResult("Log scan", "warn", "No recent log files found."))
        return results

    total_errors = 0
    critical_hits: list[str] = []
    critical_seen: set[str] = set()
    restart_count = 0

    for lf in sorted(recent):
        try:
            text = lf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        own_log = _is_own_log(lf)
        in_own_report = False  # inside a report line's unstamped continuation
        for line in text.splitlines():
            # S2: in the check's own log, a report line and its continuation lines
            # are this check's OUTPUT, never new evidence. Decided before the
            # recency test so an in-window continuation of an out-of-window
            # report line is still recognised as a continuation.
            report_line = False
            if own_log:
                if _LOG_LINE_TS_RE.match(line):
                    in_own_report = any(m in line for m in _OWN_REPORT_MARKERS)
                    report_line = in_own_report
                else:
                    report_line = in_own_report
            # PER-LINE recency (D-051 lens-4/lens-5 MEDIUM). A start-date-pinned
            # log accumulates the whole life of an instance and its mtime is always
            # fresh, so file-level filtering alone reports week-old criticals as
            # "last 24h" for as long as that instance lives -- and this branch is
            # what pulled those long-lived files into scope. A line with no
            # parseable timestamp is KEPT: unstamped lines are tracebacks and
            # continuation lines, which is exactly what a critical needs.
            if not _line_within(line, cutoff):
                continue
            # cq-b2dee156caee (session #11 S6): the ERROR tally used to be
            # text.count(" ERROR ") computed OUTSIDE this loop, so it was neither a
            # line count nor last-24h. It counted the whole life of a
            # start-date-pinned log file and matched any JSON payload containing
            # the substring -- including this check's OWN "N ERROR lines" report
            # (verified live: the 8/29 log scores 1 by substring, 0 by level).
            # Counting HERE inherits the per-line window above. Unlike the critical
            # scan, an UNSTAMPED line is deliberately NOT counted: keeping
            # traceback continuations is right for criticals and wrong for a volume
            # metric. Validated on the known 8/28 corpus: 56, not 57 and not 63.
            if _ERROR_LINE_RE.search(line):
                total_errors += 1
            # The ERROR tally above is deliberately NOT gated on report_line: a
            # report line is INFO-level by construction (never an ERROR-level
            # match) and its continuations are unstamped (never counted), so the
            # count is unchanged -- while a real ERROR this process logged stays in.
            if report_line:
                continue
            if _CRITICAL_RE.search(line):
                snippet = line[:120].strip()
                # Dedup on the BARE snippet -- the list stores a prefixed form, so
                # `snippet not in critical_hits` never matched and one failing
                # writer could emit 1,440 "distinct" hits a day. A set is also O(1)
                # where the old list scan was quadratic (measured 4.9s at 50k).
                if snippet not in critical_seen:
                    critical_seen.add(snippet)
                    critical_hits.append(f"[{lf.name}] {snippet}")
            if "Cora starting up" in line:
                restart_count += 1

    if critical_hits:
        results.append(CheckResult(
            "Critical log patterns", "critical",
            f"{len(critical_hits)} critical pattern(s) found:\n" +
            "\n".join(f"  • {h}" for h in critical_hits[:8])
        ))
    else:
        results.append(CheckResult("Critical log patterns", "ok",
                                   "No critical patterns detected."))

    if total_errors > 20:
        results.append(CheckResult(
            "Log error volume", "warn",
            f"{total_errors} ERROR lines across {len(recent)} log files in last 24h."
        ))
    elif total_errors > 0:
        results.append(CheckResult(
            "Log error volume", "ok",
            f"{total_errors} ERROR(s) in last 24h — within normal range."
        ))
    else:
        results.append(CheckResult("Log error volume", "ok", "Zero ERRORs in last 24h."))

    if restart_count > 4:
        results.append(CheckResult(
            "Cora restarts", "warn",
            f"Cora restarted {restart_count} time(s) in last 24h — possible instability."
        ))

    return results


def check_kb_health(*, dry_run: bool = False) -> list[CheckResult]:
    """Check KB chunk counts by source; compare to yesterday's baseline.

    `dry_run` compares but does NOT rewrite the baseline (R14-1 / D-290): a dry
    run that overwrote "yesterday" would hide a >20% drop that happened before it
    from the next real run."""
    results: list[CheckResult] = []
    if not _KB_DB.exists():
        return [CheckResult("KB database", "critical", "cora_kb.db not found.")]

    try:
        conn = sqlite3.connect(str(_KB_DB), timeout=5)
        rows = conn.execute(
            "SELECT source, COUNT(*) FROM knowledge_chunks GROUP BY source ORDER BY 2 DESC"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        conn.close()
    except Exception as exc:
        return [CheckResult("KB database", "critical", f"DB query failed: {exc}")]

    counts = {r[0]: r[1] for r in rows}

    # Load yesterday's baseline
    baseline: dict[str, int] = {}
    if _BASELINE.exists():
        try:
            baseline = json.loads(_BASELINE.read_text())
        except Exception:
            pass

    # Detect significant drops (>20% decrease in any source)
    problems: list[str] = []
    for source, count in counts.items():
        prev = baseline.get(source, 0)
        if prev > 50 and count < prev * 0.8:
            problems.append(f"{source}: {count} chunks (was {prev}, -{(prev-count)/prev*100:.0f}%)")

    # Save new baseline (never under --dry-run)
    if dry_run:
        log.info("[DRY-RUN] check_kb_health: baseline not rewritten")
    else:
        try:
            _BASELINE.parent.mkdir(parents=True, exist_ok=True)
            _BASELINE.write_text(json.dumps(counts))
        except Exception:
            pass

    source_summary = " | ".join(f"{s}: {c:,}" for s, c in sorted(counts.items()))

    if problems:
        results.append(CheckResult(
            "KB chunk counts", "warn",
            f"Significant drops detected:\n" +
            "\n".join(f"  • {p}" for p in problems) +
            f"\n  Total: {total:,} chunks"
        ))
    else:
        results.append(CheckResult(
            "KB chunk counts", "ok",
            f"Total: {total:,} chunks — {source_summary}"
        ))

    return results


def check_api_connectivity() -> list[CheckResult]:
    """Lightweight connectivity checks for all external APIs."""
    import httpx
    results: list[CheckResult] = []
    token = os.environ.get("SLACK_BOT_TOKEN", "")

    # Slack
    try:
        r = httpx.get(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10
        )
        data = r.json()
        if data.get("ok"):
            results.append(CheckResult("Slack API", "ok", f"Connected as {data.get('user','')}"))
        else:
            results.append(CheckResult("Slack API", "critical",
                                       f"auth.test failed: {data.get('error','')}"))
    except Exception as exc:
        results.append(CheckResult("Slack API", "critical", f"Connection error: {exc}"))

    # Asana -- the ACTIVE identity's token via the single resolver (S-B). The
    # users/me answer is COMPARED against the identity, not just interpolated:
    # under CORA_ASANA_IDENTITY=cora the token must answer as the cora@ seat
    # (asana_identity.CORA_SEAT_EMAIL) or the check is CRITICAL -- a Harrison PAT
    # mis-pasted into ASANA_PAT_CORA would otherwise keep full privilege after the
    # flip while every surface says "cora" (D-051 B-asana-identity-3). An answer
    # with no email is WARN "identity unverifiable", never ok.
    try:
        from cora import asana_identity  # noqa: PLC0415
        try:
            asana_pat, asana_ident = asana_identity.resolve_pat()
        except asana_identity.AsanaIdentityError as exc:
            asana_pat, asana_ident = "", ""
            results.append(CheckResult("Asana API", "warn", str(exc)))
        if asana_pat:
            r = httpx.get(
                "https://app.asana.com/api/1.0/users/me",
                params={"opt_fields": "name,email,gid"},
                headers={"Authorization": f"Bearer {asana_pat}"},
                timeout=10
            )
            if r.status_code == 200:
                data = r.json().get("data", {}) or {}
                name = str(data.get("name", "") or "")
                status, reason = asana_identity.classify_users_me(asana_ident, data.get("email"))
                results.append(CheckResult(
                    "Asana API", status,
                    f"Connected — {name} (identity: {asana_ident}; {reason})"))
            else:
                results.append(CheckResult("Asana API", "warn",
                                           f"Returned {r.status_code} (identity: {asana_ident})"))
    except Exception as exc:
        results.append(CheckResult("Asana API", "warn", f"Connection error: {exc}"))

    # HubSpot
    try:
        hs_token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "")
        r = httpx.get(
            "https://api.hubapi.com/crm/v3/owners",
            headers={"Authorization": f"Bearer {hs_token}"},
            timeout=10
        )
        if r.status_code == 200:
            results.append(CheckResult("HubSpot API", "ok", "Connected"))
        else:
            results.append(CheckResult("HubSpot API", "warn",
                                       f"Returned {r.status_code}"))
    except Exception as exc:
        results.append(CheckResult("HubSpot API", "warn", f"Connection error: {exc}"))

    # Notion
    try:
        notion_key = os.environ.get("NOTION_API_KEY", "")
        r = httpx.get(
            "https://api.notion.com/v1/users/me",
            headers={
                "Authorization": f"Bearer {notion_key}",
                "Notion-Version": "2022-06-28"
            },
            timeout=10
        )
        if r.status_code == 200:
            results.append(CheckResult("Notion API", "ok", "Connected"))
        else:
            results.append(CheckResult("Notion API", "warn",
                                       f"Returned {r.status_code}"))
    except Exception as exc:
        results.append(CheckResult("Notion API", "warn", f"Connection error: {exc}"))

    # Anthropic (key format check only — don't burn tokens)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key.startswith("sk-ant-"):
        results.append(CheckResult("Anthropic API", "ok", "Key present and valid format"))
    else:
        results.append(CheckResult("Anthropic API", "critical",
                                   "ANTHROPIC_API_KEY missing or wrong format"))

    # OpenAI (embeddings)
    oai_key = os.environ.get("OPENAI_API_KEY", "")
    if oai_key.startswith("sk-"):
        results.append(CheckResult("OpenAI API", "ok", "Key present and valid format"))
    else:
        results.append(CheckResult("OpenAI API", "critical",
                                   "OPENAI_API_KEY missing or wrong format"))

    # Google Service Account JSON
    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if sa_path and Path(sa_path).exists():
        try:
            data = json.loads(Path(sa_path).read_text())
            email = data.get("client_email", "")
            results.append(CheckResult("Google SA JSON", "ok", f"Valid — {email}"))
        except Exception as exc:
            results.append(CheckResult("Google SA JSON", "critical",
                                       f"File exists but unreadable: {exc}"))
    else:
        results.append(CheckResult("Google SA JSON", "critical",
                                   f"File not found: {sa_path}"))

    return results


def check_env_vars() -> CheckResult:
    """Verify all required environment variables are set (identity-aware for Asana)."""
    from cora import asana_identity  # noqa: PLC0415
    try:
        required = _required_env_vars()
        identity = asana_identity.active_identity()
    except asana_identity.AsanaIdentityError as exc:
        # Key/flag NAMES only in the detail (the resolver never echoes a value).
        return CheckResult("Environment variables", "critical", str(exc))
    missing = [v for v in required if not os.environ.get(v, "").strip()]
    if missing:
        return CheckResult(
            "Environment variables", "critical",
            f"{len(missing)} required var(s) missing: {', '.join(missing)} "
            f"(Asana identity: {identity} via {asana_identity.pat_key_for(identity)})"
        )
    return CheckResult(
        "Environment variables", "ok",
        f"All {len(required)} required vars present "
        f"(Asana identity: {identity} via {asana_identity.pat_key_for(identity)}).")


def check_disk_space() -> CheckResult:
    """Warn if C: drive free space is below 5 GB."""
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "(Get-PSDrive C).Free"],
            capture_output=True, text=True, timeout=10,
            creationflags=_NO_WINDOW,
        )
        free_bytes = int(result.stdout.strip())
        free_gb = free_bytes / (1024 ** 3)
        if free_gb < 2:
            return CheckResult("Disk space", "critical",
                               f"C: only {free_gb:.1f} GB free — immediate action needed.")
        if free_gb < 5:
            return CheckResult("Disk space", "warn",
                               f"C: {free_gb:.1f} GB free — getting low.")
        return CheckResult("Disk space", "ok", f"C: {free_gb:.1f} GB free.")
    except Exception as exc:
        return CheckResult("Disk space", "warn", f"Could not check disk: {exc}")


def check_flywheel(*, dry_run: bool = False) -> list[CheckResult]:
    """Knowledge-flywheel throughput (WS-2) — catch the loop silently dying.

    `dry_run` reads metrics without appending today's pending-size baseline
    (R14-1 / D-290: the only write this check makes).

    The flywheel flatlined for 2+ weeks in June 2026 (0 knowledge DMs, gap log
    dry since 6/15, zero shadow records) and nothing alarmed. Metrics +
    thresholds live in ONE place — cora.flywheel_metrics — shared with the
    weekly health report, so the two surfaces can never drift (the
    _EXPECTED_DISABLED false-CRITICAL failure mode). This is the only call
    site that updates the pending-size baseline history (one write/day).

    Severity is warn-only by design: throughput degradation is not an outage
    (a critical would flip the task's Last Result nonzero). First-week note:
    these WILL warn while the WS-1 starvation fixes bed in — that is correct;
    do not suppress.
    """
    try:
        from cora import flywheel_metrics as fm
        metrics = fm.collect(update_baseline=not dry_run)
        alarms = fm.evaluate(metrics)
        results = [
            CheckResult("Flywheel", "warn", msg) for _sev, msg in alarms
        ]
        # WS-3 guard: this script loads .env (override=True), so if
        # CORA_EVAL_MODE ever lands in .env (the HEALTH_PING_URL
        # template-append precedent), it is visible here -- and the BOT would
        # silently offer ZERO tools on its next restart. Catch it within 24h.
        if os.environ.get("CORA_EVAL_MODE"):
            results.append(CheckResult(
                "Flywheel", "critical",
                "CORA_EVAL_MODE is set in this environment -- if it is in "
                ".env, the bot offers NO tools after its next restart. "
                "Remove the line (only scripts/run_kb_evals.py may set it, "
                "in-process).",
            ))
        # One info-level OK line carrying the gauge numbers either way.
        summary = "; ".join(fm.format_lines(metrics)[:3])
        results.append(CheckResult(
            "Flywheel throughput", "ok" if not alarms else "warn", summary,
        ))
        return results
    except Exception as exc:  # noqa: BLE001 — a broken gauge never fails the run
        return [CheckResult("Flywheel", "warn",
                            f"Could not compute flywheel metrics: {exc}")]


def check_priority_kickoffs() -> CheckResult:
    """Slice 4 (pipeline-integrity bundle, 2026-08-05): WARN on any APPROVED
    P0/P1-class code-queue item older than the grace window with NO `staged` event.

    The structural net behind code_queue.ensure_kickoff_staged. Design (TOM 1fff)
    says a P0/P1 gets a full kickoff prompt at approval, but the rule lived only
    inside process_queue_action, so cq-f1236540b61e (P1, seeded straight to
    status="APPROVED") sat a full week approved-and-unstaged with nothing alarming.
    Now, whatever path approved an item -- card tap, Monday menu, a seeding script,
    or something not yet written -- a dropped priority kickoff surfaces within a day.

    WARN, never critical: an unstaged kickoff is a dropped ball, not an outage.

    A scan failure WARNs. priority_items_missing_kickoff deliberately RAISES rather
    than returning [] so "clean" and "blind" can never render identically here
    (D-051 lens-5 HIGH) -- a false all-clear is the one failure mode this monitor
    cannot afford, since it exists because an item sat unnoticed for a week.

    The detail names ids, not TITLES (lens-4 LOW): this report posts to a
    multi-person channel, and queue titles are user-authored intake text from
    arbitrary channels. Every other check there emits aggregates or task names. The
    id is enough to act on.
    """
    try:
        from cora import code_queue
        offenders = code_queue.priority_items_missing_kickoff()
    except Exception as exc:  # noqa: BLE001 -- a broken gauge never fails the run
        return CheckResult("Priority kickoffs", "warn",
                           f"Could not scan the code queue: {exc}")
    if not offenders:
        return CheckResult(
            "Priority kickoffs", "ok",
            "No APPROVED P0/P1 item is missing a kickoff prompt.")
    detail = "; ".join(
        f"{o['id']} [{o['severity']}/{o['entity']}] "
        f"{('%.0fh' % o['age_hours']) if o.get('age_hours') is not None else 'age unknown'}"
        f"{' (prompt file missing)' if o.get('prompt_path_missing') else ''}"
        for o in offenders[:5]
    )
    more = f" (+{len(offenders) - 5} more)" if len(offenders) > 5 else ""
    return CheckResult(
        "Priority kickoffs", "warn",
        f"{len(offenders)} APPROVED P0/P1 item(s) have NO kickoff prompt after "
        f"{code_queue.PRIORITY_KICKOFF_GRACE_HOURS}h -- tap Stage on each "
        f"(a SHIPPED item is refused, so a stale button is safe): {detail}{more}")


def check_code_queue_ledger_integrity() -> CheckResult:
    """F4 (Code #12, audit F1/F4): folded-vs-raw id reconciliation of the code-queue
    ledger. WARN on any id with events but no `captured` event (the reducer drops
    it -- a real, approved ask was invisible on every surface for 16 days this way)
    and on any event kind the reducer does not model. Names ids and kinds, never
    titles. A scan failure WARNs (blind is never clean)."""
    try:
        from cora import code_queue
        rep = code_queue.ledger_integrity()
    except Exception as exc:  # noqa: BLE001 -- a broken gauge never fails the run
        return CheckResult("Code-queue ledger integrity", "warn",
                           f"Could not reconcile the ledger: {exc}")
    orphans = rep.get("orphans") or []
    unknown = rep.get("unknown_event_types") or {}
    unparseable = int(rep.get("unparseable") or 0)
    if not orphans and not unknown and not unparseable:
        return CheckResult("Code-queue ledger integrity", "ok",
                           f"{rep.get('raw_ids', '?')} ids in the raw ledger, all fold "
                           f"({rep.get('folded_ids', '?')} captured); no unknown event kinds; "
                           "every line parses.")
    parts = []
    if unparseable:
        # D-051 lens B MED #3: a torn line is a LOST event the fold skips silently.
        parts.append(f"{unparseable} unparseable line(s) -- lost events (a torn append?)")
    if orphans:
        parts.append(f"{len(orphans)} orphan id(s) with events but no `captured` (fold "
                     f"drops them): " + "; ".join(
                         f"{o['id']} ({o['events']} ev: {', '.join(o['kinds'])})" for o in orphans[:5]))
    if unknown:
        parts.append("unknown event kind(s): " + ", ".join(
            f"{k!r} x{n}" for k, n in sorted(unknown.items())))
    return CheckResult("Code-queue ledger integrity", "warn",
                       " | ".join(parts) + " -- repair through code_queue.seed_item / the "
                       "module's own writers, never a jsonl hand-edit.")


def check_proposed_priority_aging() -> CheckResult:
    """F5 (Code #12, audit F5): WARN on PROPOSED P0/P1-class items older than
    PROPOSED_PRIORITY_AGING_DAYS -- the tier the 9/2 audit found rotting (8
    HIGH-class rows, three ~a month old) while check_priority_kickoffs above read
    "ok" on a population every approval path empties synchronously. Sibling, not a
    replacement: the older check still guards its (rare) class. Ids, never titles."""
    try:
        from cora import code_queue
        rep = code_queue.proposed_priority_aging()
    except Exception as exc:  # noqa: BLE001
        return CheckResult("PROPOSED priority aging", "warn",
                           f"Could not scan the code queue: {exc}")
    aged = rep.get("aged_priority") or []
    ctx = (f"{rep.get('aged_total', '?')} PROPOSED item(s) aged >= "
           f"{code_queue.PROPOSED_PRIORITY_AGING_DAYS}d of {rep.get('proposed_total', '?')}")
    if not aged:
        return CheckResult("PROPOSED priority aging", "ok",
                           f"No PROPOSED P0/P1-class item is older than "
                           f"{code_queue.PROPOSED_PRIORITY_AGING_DAYS}d ({ctx}).")
    detail = "; ".join(f"{a['id']} [{a['severity']}/{a['entity']}] {a['age_days']}d"
                       for a in aged[:5])
    more = f" (+{len(aged) - 5} more)" if len(aged) > 5 else ""
    return CheckResult("PROPOSED priority aging", "warn",
                       f"{len(aged)} PROPOSED P0/P1-class item(s) undecided for >= "
                       f"{code_queue.PROPOSED_PRIORITY_AGING_DAYS}d -- they ride the Monday "
                       f"menu's PROPOSED coverage (approve / park / dismiss): {detail}{more}. {ctx}.")


def check_parked_aging() -> CheckResult:
    """C3 (Code #12; D-051 lens B HIGH #2): WARN on PARKED rows whose trigger has
    FIRED -- the resume date passed, or the ask recurred since parking -- and that
    no human has re-triaged. A PARKED row leaves every menu section by design, so
    the Monday menu was its only surface and no gauge watched it: one park tap could
    silence a recurring P0 ask indefinitely. Ids, never titles."""
    try:
        from cora import code_queue
        rep = code_queue.parked_aging()
    except Exception as exc:  # noqa: BLE001
        return CheckResult("PARKED triggers", "warn", f"Could not scan the code queue: {exc}")
    due = rep.get("due") or []
    total = rep.get("parked_total", "?")
    if not due:
        return CheckResult("PARKED triggers", "ok",
                           f"No parked item has a fired trigger ({total} parked).")
    detail = "; ".join(
        f"{d['id']} [{d['severity']}/{d['entity']}] "
        + (f"re-asked x{d['parked_recurrences']}" if d.get("parked_recurrences")
           else f"due {d.get('park_until') or '?'}")
        + f", parked {d.get('days_parked', '?')}d" for d in due[:5])
    more = f" (+{len(due) - 5} more)" if len(due) > 5 else ""
    return CheckResult("PARKED triggers", "warn",
                       f"{len(due)} parked item(s) with a fired trigger await re-triage (the "
                       f"Monday menu offers Re-queue / Park again / Dismiss w/ note): "
                       f"{detail}{more}. {total} parked in all.")


def check_egress_rails(now: datetime | None = None) -> CheckResult:
    """Code #12 S3'(a) (cq-deca62a00719): the observe-week read for the two seam
    rails, SIDE BY SIDE -- `sentinel-egress-leak` (session #11 S1, a contract token
    echoed) and `phantom-write-claim` (Code #12 S2', a write asserted with zero
    tool_use, or an id that exists in no ledger) -- 24h and 7d, from the bot's own
    logs (cora.egress_rails, single-sourced with the Monday digest).

    THIS is the enforce-flip reminder, and it reads BOTH keys. The 8/30 -> 9/6
    observe week was called clean on the first key alone while a three-fold
    phantom-write incident happened in plain prose on day 3 (decisions.md
    2026-09-03): a criterion that counts one rail certifies the other's silence.
    WARN while the 7d window carries any hit in observe mode (the week is not
    clean; the flip stays gated); ok, with the explicit "criterion MET" sentence,
    when both read zero for a week. Never critical -- a phantom claim in observe
    mode is delivered text, not an outage; once ENFORCE is on the ERROR-volume
    triage above owns escalation. A counting failure WARNs, so blind can never
    render as clean (the lens-5 rule the priority-kickoff monitor already follows).
    """
    try:
        from cora import egress_rails
        read = egress_rails.observe_week_read(now)
    except Exception as exc:  # noqa: BLE001 -- a broken gauge never fails the run
        return CheckResult("Egress rails (observe week)", "warn",
                           f"Could not count the rail lines: {exc}")
    detail = egress_rails.format_line(read) + " -- " + str(read.get("flip_criterion", ""))
    if read.get("mode") == "enforce":
        hits = sum((read.get("counts_7d") or {}).values())
        return CheckResult("Egress rails (ENFORCE)", "warn" if hits else "ok", detail)
    return CheckResult("Egress rails (observe week)",
                       "ok" if read.get("clean_7d") else "warn", detail)


# ── Report builder ────────────────────────────────────────────────────────────


def _build_report(all_results: list[CheckResult], run_time: float,
                  now: datetime | None = None) -> str:
    # `now` exists for test determinism only (S2); main() never passes it, so the
    # posted report is byte-identical to before.
    today = (now if now is not None else datetime.now()).strftime("%Y-%m-%d %H:%M AZ")

    criticals  = [r for r in all_results if r.status == "critical"]
    warnings   = [r for r in all_results if r.status == "warn"]
    fixed      = [r for r in all_results if r.status == "fixed"]
    ok_count   = sum(1 for r in all_results if r.status == "ok")

    # Header
    if criticals:
        header = f":rotating_light: *Cora Health Check — {today}*"
    elif warnings:
        header = f":warning: *Cora Health Check — {today}*"
    elif fixed:
        header = f":wrench: *Cora Health Check — {today}*"
    else:
        header = f":white_check_mark: *Cora Health Check — {today}*"

    summary = (
        f"*Summary:* {len(criticals)} critical · {len(warnings)} warning · "
        f"{len(fixed)} auto-fixed · {ok_count} OK  _(ran in {run_time:.1f}s)_"
    )

    sections: list[str] = [header, summary]

    if criticals:
        sections.append("\n*:rotating_light: CRITICAL — action required:*")
        for r in criticals:
            sections.append(f"{_EMOJI[r.status]} *{r.name}*\n  {r.detail}")

    if fixed:
        sections.append("\n*:wrench: AUTO-FIXED:*")
        for r in fixed:
            sections.append(
                f"{_EMOJI[r.status]} *{r.name}*\n  {r.detail}\n  _Fix: {r.fix_applied}_"
            )

    if warnings:
        sections.append("\n*:warning: Warnings:*")
        for r in warnings:
            sections.append(f"{_EMOJI[r.status]} *{r.name}*\n  {r.detail}")

    if ok_count > 0 and (criticals or warnings or fixed):
        sections.append(f"\n_{ok_count} other check(s) passed without issue._")
    elif not criticals and not warnings and not fixed:
        sections.append("\n_All systems healthy. Nothing to fix._")

    return "\n".join(sections)


# ── Durable run artifact (Code #14 S2, cq-0f04ad6543a8) ───────────────────────
#
# Before S2 the only full read of a run was the Slack post: the task log cut every
# detail at 100 chars ("... task-estate added: fndr-notetak") and nothing else was
# kept. Each real run now leaves reports/health/YYYY-MM-DD.json (+ a .md twin) with
# every check's FULL detail. Local disk only, gitignored (/reports/), not under the
# KB static walk (that walks G:, not the repo), never routed anywhere shared -- it
# carries the same details the post already carries. A dry run never writes one
# (D-290): a same-date dry run would otherwise overwrite the real run's artifact.

_AZ = timezone(timedelta(hours=-7))  # Arizona: MST all year, no DST
ARTIFACT_SCHEMA = 1


def _health_report_dir() -> Path:
    """Resolved PER CALL (never an import-time constant): the conftest autouse
    fixture points CORA_HEALTH_REPORT_DIR at tmp so no test can write the real
    reports/ directory."""
    raw = (os.environ.get("CORA_HEALTH_REPORT_DIR") or "").strip()
    return Path(raw) if raw else _REPO_ROOT / "reports" / "health"


def _az_now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(_AZ)
    if now.tzinfo is None:  # the host clock is AZ; a naive stamp is AZ wall time
        return now.replace(tzinfo=_AZ)
    return now.astimezone(_AZ)


def _artifact_paths(now: datetime | None = None,
                    out_dir: Path | None = None) -> tuple[Path, Path]:
    d = out_dir if out_dir is not None else _health_report_dir()
    day = _az_now(now).date().isoformat()
    return d / f"{day}.json", d / f"{day}.md"


def _build_artifact(all_results: list[CheckResult], run_time: float, *,
                    exit_code: int, dry_run: bool, report_text: str,
                    now: datetime | None = None) -> dict:
    """The structured record of one run. `checks` round-trips: CheckResult(**c)
    for each entry rebuilds the exact inputs, so _build_report over the rebuilt
    list reproduces report_text for the same clock."""
    stamp = _az_now(now)
    counts = {s: sum(1 for r in all_results if r.status == s)
              for s in ("critical", "warn", "fixed", "ok")}
    heartbeat = next(({"status": r.status, "detail": r.detail}
                      for r in all_results if r.name == "Cora heartbeat"), None)
    rails = next((r for r in all_results if r.name.startswith("Egress rails")), None)
    rails_line = (f"{rails.name} [{rails.status}]: {rails.detail}"
                  if rails is not None else None)
    return {
        "schema": ARTIFACT_SCHEMA,
        "run_ts": stamp.isoformat(timespec="seconds"),
        "run_time_s": float(run_time),
        "exit_code": int(exit_code),
        "dry_run": bool(dry_run),
        "counts": counts,
        "heartbeat": heartbeat,
        "rails_line": rails_line,
        "checks": [{"name": r.name, "status": r.status, "detail": r.detail,
                    "fix_applied": r.fix_applied} for r in all_results],
        "report_text": report_text,
    }


def _md_cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _render_artifact_md(artifact: dict) -> str:
    c = artifact["counts"]
    lines = [
        f"# Cora health check -- {artifact['run_ts']}",
        "",
        f"- exit code: {artifact['exit_code']}  (dry run: {artifact['dry_run']})",
        f"- ran in {artifact['run_time_s']:.1f}s",
        f"- {c['critical']} critical / {c['warn']} warn / {c['fixed']} fixed / {c['ok']} ok",
        "- heartbeat: " + (f"{artifact['heartbeat']['status']} -- "
                            f"{artifact['heartbeat']['detail']}"
                            if artifact.get("heartbeat") else "(no heartbeat result)"),
        f"- egress rails: {artifact.get('rails_line') or '(no egress-rails result)'}",
        "",
        "## Every check (full detail)",
        "",
        "| status | check | detail | fix applied |",
        "|---|---|---|---|",
    ]
    for chk in artifact["checks"]:
        lines.append(f"| {chk['status']} | {_md_cell(chk['name'])} | "
                     f"{_md_cell(chk['detail'])} | {_md_cell(chk['fix_applied'])} |")
    lines += ["", "## Report as posted", "", "```", artifact["report_text"], "```", ""]
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_artifact(artifact: dict, out_dir: Path | None = None) -> Path | None:
    """Write <day>.json + <day>.md atomically (tmp + os.replace). Fail-soft: a
    write failure logs a warning and returns None -- it must never block the
    Slack post. A same-day re-run overwrites (last real run wins)."""
    try:
        json_path, md_path = _artifact_paths(
            datetime.fromisoformat(artifact["run_ts"]), out_dir)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(json_path, json.dumps(artifact, ensure_ascii=False, indent=2) + "\n")
        _atomic_write(md_path, _render_artifact_md(artifact))
        return json_path
    except Exception as exc:  # noqa: BLE001 -- observability must never block the post
        log.warning("health-check artifact write failed (report still posts): %s", exc)
        return None


def _flatten_detail(detail: str) -> str:
    """One log line per result: the FULL detail with newlines folded (S2). The old
    [:100] cut made the Slack post the only full read of record."""
    return (detail or "").replace("\r\n", "\n").replace("\n", " | ")


def _post_to_slack(message: str, token: str, channel: str) -> None:
    try:
        import httpx
        from cora.slack_egress import sanitize_text  # noqa: PLC0415 -- B1: raw POST bypasses the WebClient patch
        httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"channel": channel, "text": sanitize_text(message),
                  "unfurl_links": False, "unfurl_media": False},
            timeout=15,
        )
    except Exception as exc:
        log.error("Failed to post health report to Slack: %s", exc)


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Run checks but do not apply auto-fixes or post to Slack")
    parser.add_argument("--verbose", action="store_true",
                        help="Print all results including OK checks")
    args = parser.parse_args()

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    log_file = _LOG_DIR / f"health-check-{today_str}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(
                open(sys.stdout.fileno(), "w", encoding="utf-8", errors="replace", closefd=False)
            ),
        ],
    )

    log.info("=" * 60)
    log.info("Nightly health check starting (dry_run=%s)", args.dry_run)
    t0 = time.time()

    all_results: list[CheckResult] = []

    log.info("Checking Cora heartbeat...")
    all_results.append(check_heartbeat(args.dry_run))

    log.info("Checking scheduled tasks...")
    all_results.extend(check_scheduled_tasks())

    log.info("Checking scheduled-task last results (W4-07)...")
    all_results.extend(check_task_last_results())

    log.info("Checking watchdog liveness...")
    all_results.append(check_watchdog_liveness())
    all_results.append(check_windowless_launcher())

    log.info("Checking decision gate dates...")
    all_results.append(check_decision_gates(dry_run=args.dry_run))

    log.info("Checking QBO token monitor freshness...")
    all_results.append(check_qbo_monitor())
    all_results.append(check_run_markers())
    log.info("Reading the missed-nightly catch-up ledger (Code #13 slice 2)...")
    all_results.append(check_missed_nightly_catchup())
    all_results.append(check_meet_join_audit())
    all_results.append(check_info_for_cora_watermark())

    log.info("Checking dynamic-answers snapshot freshness...")
    all_results.append(check_dynamic_snapshots())

    log.info("Checking claude-workspace mirror freshness...")
    all_results.append(check_claude_mirror())
    all_results.append(check_session_capture_quarantine())
    all_results.append(check_quarantine_folder_isolation())

    log.info("Checking 13wk cashflow forecast snapshot (S1) freshness...")
    all_results.append(check_cashflow_forecast_snapshot())
    all_results.append(check_cashflow_actuals())
    all_results.append(check_cashflow_worksheet())

    log.info("Checking MCP HTTP bridge (if registered)...")
    all_results.append(check_mcp_http_bridge())

    log.info("Checking founder CLAUDE.md KB freshness (FNDR current-state path)...")
    all_results.append(check_founder_kb_freshness())

    log.info("Scanning logs (last 24h)...")
    all_results.extend(check_logs_24h())

    log.info("Checking KB health...")
    all_results.extend(check_kb_health(dry_run=args.dry_run))

    log.info("Checking API connectivity...")
    all_results.extend(check_api_connectivity())

    log.info("Checking environment variables...")
    all_results.append(check_env_vars())

    log.info("Checking disk space...")
    all_results.append(check_disk_space())

    log.info("Checking knowledge-flywheel throughput...")
    all_results.extend(check_flywheel(dry_run=args.dry_run))

    log.info("Checking for APPROVED P0/P1 items missing a kickoff prompt...")
    all_results.append(check_priority_kickoffs())

    log.info("Reconciling the code-queue ledger (orphans / unknown event kinds)...")
    all_results.append(check_code_queue_ledger_integrity())

    log.info("Checking PROPOSED P0/P1-class aging...")
    all_results.append(check_proposed_priority_aging())

    log.info("Checking PARKED rows whose trigger has fired...")
    all_results.append(check_parked_aging())

    log.info("Reading the autonomy-ladder registry for drift (Code #13 slice 7)...")
    all_results.append(check_ladder_registry())

    # Code #16 C1: the dead-channel lane's ledger reconciled against Slack
    log.info("Reconciling the dead-channel archive ledger against Slack (Code #16 C1)...")
    all_results.append(check_channel_archive(dry_run=args.dry_run))

    log.info("Reading the egress-rail observe week (sentinel + phantom-write-claim + phantom-capability-claim)...")
    all_results.append(check_egress_rails())

    # Code #16 C2: the travel shortlist lane's failing-capable monitor (read-only).
    log.info("Reading the travel shortlist lane's thread store (Code #16 C2)...")
    all_results.append(check_travel_shortlist())

    run_time = time.time() - t0

    # Log summary
    criticals = [r for r in all_results if r.status == "critical"]
    warnings  = [r for r in all_results if r.status == "warn"]
    fixed     = [r for r in all_results if r.status == "fixed"]

    for r in all_results:
        if args.verbose or r.status != "ok":
            # S2: the FULL detail, flattened to one line (was r.detail[:100]).
            # check_logs_24h skips these report lines in its own log, so a quoted
            # critical snippet can never re-raise itself the next morning.
            log.info("[%s] %s: %s%s",
                     r.status.upper(), r.name, _flatten_detail(r.detail),
                     f" | FIX: {r.fix_applied}" if r.fix_applied else "")

    log.info("Health check complete in %.1fs — %d critical, %d warn, %d fixed",
             run_time, len(criticals), len(warnings), len(fixed))

    # Build and post report
    report = _build_report(all_results, run_time)
    exit_code = 1 if criticals else 0
    artifact = _build_artifact(all_results, run_time, exit_code=exit_code,
                               dry_run=args.dry_run, report_text=report)

    if args.dry_run:
        sys.stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", errors="replace", closefd=False)
        print("\n=== REPORT (dry-run) ===\n")
        print(report)
        # D-290: a dry run writes NO artifact (a same-date dry run would overwrite
        # the real run's record).
        json_path, _md = _artifact_paths(datetime.fromisoformat(artifact["run_ts"]))
        print(f"\n[dry-run] would write {json_path} (+ .md twin)")
        sys.stdout.flush()
        return 0

    written = _write_artifact(artifact)
    if written is not None:
        log.info("Run artifact written: %s", written)

    # Always post — Harrison wants a daily all-clear or issue report every morning
    should_post = True

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if should_post and token:
        _post_to_slack(report, token, _HEALTH_CH)
        log.info("Report posted to #%s", _HEALTH_CH)
    elif not should_post:
        log.info("All clear — no Slack post needed (set should_post=True to always post)")
    else:
        log.warning("SLACK_BOT_TOKEN not set — report not posted")
        print(report)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
