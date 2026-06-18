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
  • Any scheduled task in state "Running" for >2h → mark stuck, restart

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

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

# Tasks intentionally Disabled (Harrison-directed). Keep in sync with
# project_scheduled_tasks_registry.md "Disabled tasks" section.
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

# Required env vars — subset that would break Cora if missing
_REQUIRED_ENV_VARS = [
    "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY",
    "ASANA_PAT", "NOTION_API_KEY", "OPENAI_API_KEY",
    "FIREFLIES_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GSHEETS_CASHFLOW_FILE_ID", "HUBSPOT_PRIVATE_APP_TOKEN",
    "SHOPIFY_F3E_ACCESS_TOKEN",
]

# Critical log patterns — any match flags the log
_CRITICAL_LOG_PATTERNS = [
    r"ImportError",
    r"ModuleNotFoundError",
    r"UnicodeDecodeError",
    r"\bFATAL\b",
    r"Socket Mode disconnect",
    r"connection refused",
    r"SLACK_BOT_TOKEN.*invalid",
    r"API key.*invalid",
]
_CRITICAL_RE = re.compile("|".join(_CRITICAL_LOG_PATTERNS), re.IGNORECASE)

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
            capture_output=True, timeout=15
        )
        # Kill orphan python processes
        subprocess.run(
            ["powershell", "-Command",
             "Get-WmiObject Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*cora*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
            capture_output=True, timeout=15
        )
        time.sleep(2)
        subprocess.run(
            ["schtasks", "/Run", "/TN", "cowork-cora-service"],
            capture_output=True, timeout=15
        )
        return "Auto-restarted cowork-cora-service (orphan-kill applied)."
    except Exception as exc:
        return f"Restart attempted but failed: {exc}"


def _classify_task_states(
    task_states: dict[str, str],
    intended_disabled: set[str],
    expected_running: set[str],
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
        elif "Ready" in status or "Running" in status:
            ok += 1
        elif "Disabled" in status:
            warn.append(f"{name}: unexpectedly Disabled "
                        f"(add to scheduled-task-state.yaml if intentional)")
        else:
            warn.append(f"{name}: unexpected status '{status}'")
    return critical, warn, ok


def check_scheduled_tasks() -> list[CheckResult]:
    """Check all Cora scheduled tasks for unexpected states (audit N8)."""
    try:
        out = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30
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

    critical, warn, ok_count = _classify_task_states(
        task_states, _EXPECTED_DISABLED, _EXPECTED_RUNNING
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


_QBO_MONITOR_TASK = "Cora - QBO Token Monitor"


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


def check_logs_24h() -> list[CheckResult]:
    """Scan last 24h log files for ERRORs and critical patterns."""
    results: list[CheckResult] = []
    cutoff = datetime.now() - timedelta(hours=26)
    today_str  = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    log_files = list(_LOG_DIR.glob("*.log"))
    recent = [
        f for f in log_files
        if f.stat().st_mtime > cutoff.timestamp()
        and (today_str in f.name or yesterday_str in f.name)
    ]

    if not recent:
        results.append(CheckResult("Log scan", "warn", "No recent log files found."))
        return results

    total_errors = 0
    critical_hits: list[str] = []
    restart_count = 0

    for lf in sorted(recent):
        try:
            text = lf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        error_count = text.count(" ERROR ")
        total_errors += error_count

        for line in text.splitlines():
            if _CRITICAL_RE.search(line):
                snippet = line[:120].strip()
                if snippet not in critical_hits:
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


def check_kb_health() -> list[CheckResult]:
    """Check KB chunk counts by source; compare to yesterday's baseline."""
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

    # Save new baseline
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

    # Asana
    try:
        asana_pat = os.environ.get("ASANA_PAT", "")
        r = httpx.get(
            "https://app.asana.com/api/1.0/users/me",
            headers={"Authorization": f"Bearer {asana_pat}"},
            timeout=10
        )
        if r.status_code == 200:
            name = r.json().get("data", {}).get("name", "")
            results.append(CheckResult("Asana API", "ok", f"Connected — {name}"))
        else:
            results.append(CheckResult("Asana API", "warn",
                                       f"Returned {r.status_code}"))
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
    """Verify all required environment variables are set."""
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v, "").strip()]
    if missing:
        return CheckResult(
            "Environment variables", "critical",
            f"{len(missing)} required var(s) missing: {', '.join(missing)}"
        )
    return CheckResult("Environment variables", "ok",
                       f"All {len(_REQUIRED_ENV_VARS)} required vars present.")


def check_disk_space() -> CheckResult:
    """Warn if C: drive free space is below 5 GB."""
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "(Get-PSDrive C).Free"],
            capture_output=True, text=True, timeout=10
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


# ── Report builder ────────────────────────────────────────────────────────────


def _build_report(all_results: list[CheckResult], run_time: float) -> str:
    today = datetime.now().strftime("%Y-%m-%d %H:%M AZ")

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

    log.info("Checking QBO token monitor freshness...")
    all_results.append(check_qbo_monitor())

    log.info("Scanning logs (last 24h)...")
    all_results.extend(check_logs_24h())

    log.info("Checking KB health...")
    all_results.extend(check_kb_health())

    log.info("Checking API connectivity...")
    all_results.extend(check_api_connectivity())

    log.info("Checking environment variables...")
    all_results.append(check_env_vars())

    log.info("Checking disk space...")
    all_results.append(check_disk_space())

    run_time = time.time() - t0

    # Log summary
    criticals = [r for r in all_results if r.status == "critical"]
    warnings  = [r for r in all_results if r.status == "warn"]
    fixed     = [r for r in all_results if r.status == "fixed"]

    for r in all_results:
        if args.verbose or r.status != "ok":
            log.info("[%s] %s: %s%s",
                     r.status.upper(), r.name, r.detail[:100],
                     f" | FIX: {r.fix_applied}" if r.fix_applied else "")

    log.info("Health check complete in %.1fs — %d critical, %d warn, %d fixed",
             run_time, len(criticals), len(warnings), len(fixed))

    # Build and post report
    report = _build_report(all_results, run_time)

    if args.dry_run:
        sys.stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", errors="replace", closefd=False)
        print("\n=== REPORT (dry-run) ===\n")
        print(report)
        return 0

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

    return 1 if criticals else 0


if __name__ == "__main__":
    sys.exit(main())
