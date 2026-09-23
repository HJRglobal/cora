r"""Task-estate manifest generator + drift monitor (DR/VM step 1, slice M1; cq-a296aa8e0a2e).

WHY THIS EXISTS (2026-09-22)
    deployment/bootstrap-new-machine.md registered 3 of 96 scheduled tasks, the
    runbook table listed ~7 of them with 3 rows provably wrong, and NOTHING derived
    the task estate from the machine that actually runs it. Run markers cannot be
    the source either: logs/task-runs.jsonl carries rows for 4 task names while
    logs/tasks/ carries 75 per-task log prefixes (measured 2026-09-22). So the
    manifest is derived from the LIVE REGISTRY -- Windows Task Scheduler via the
    CIM cmdlets -- and cross-checked against two independent enumerations
    (schtasks CSV and the in-process cora_health view); the three must agree or the
    disagreement is written into the manifest.

    The bot estate = the always-on service task + the Task Scheduler tasks named
    `cowork-cora-*`, `Cora - *` and `cora-watchdog`. The Cowork estate (scheduled
    tasks inside the Claude desktop app) is NOT part of this manifest -- it stays on
    the office machine (charter D4) -- but is cross-referenced here through the
    mirror's own reader (scripts/mirror_claude_workspace._task_dirs) so there is
    ONE implementation of "enumerate the Cowork estate".

WHAT IT PRODUCES
    deployment/manifest/task-estate.json   the machine-readable manifest
    deployment/manifest/task-estate.md     the rendered table
    --update-docs also regenerates the GENERATED blocks in
    deployment/bootstrap-new-machine.md (scheduled-estate) and
    deployment/runbook.md (task-registry) between their marker comments.

    Per task: name, trigger (cron-like text), action (the child command behind the
    windowless run_hidden wrapper) + script path, run-as (logon type, run level),
    start_when_available, enabled/state, last result + last run, run marker
    (yes/no, from logs/task-runs.jsonl), per-task log path (+ newest file),
    intent (data/maps/scheduled-task-state.yaml), ladder-registry lane if one names
    the task's script (heuristic, labelled), and the deployment/setup-*.ps1 that
    registers it (or "none" -- those tasks are re-registered from this manifest).

DRIFT (--diff / the Monday digest)
    diff_manifests(previous_tasks, live_tasks) -> WARN lines
        task-estate-drift: added <name>
        task-estate-drift: removed <name>
        task-estate-drift: cron_changed <name>: <old> -> <new>
        task-estate-drift: enabled_changed <name>: <old> -> <new>
        task-estate-drift: action_changed <name>
        task-estate-drift: principal_changed <name>: <old> -> <new>
    Volatile fields (last run, next run, last result, log mtimes, marker times) are
    NEVER part of the diff -- a manifest must not read as drifted because time
    passed. scripts/cora_health_report.task_estate_section reads
    diff_against_committed() for the Monday digest (read-only: it never writes).

READ-ONLY. This script never registers, edits, enables or disables a task and never
writes outside deployment/manifest/ and the two GENERATED doc blocks. It carries no
secret VALUE: task actions are paths + flags, the task XML carries triggers/settings/
principal SID only, and the .env is read ONLY as a side effect of importing the mirror's
Cowork-estate reader (whose module loads it) -- the environment is snapshotted and
restored around that import and nothing from it is written. scripts/secrets_scan.py
gates every emitted file in CI.

RESTORE. deployment/manifest/tasks/<slug>.xml = one full-fidelity Task Scheduler export
per task; deployment/register-estate-from-manifest.ps1 -Apply registers the whole estate
from them (bootstrap Phase 5). The setup-*.ps1 scripts CREATE new tasks; the manifest
carries them from then on.

    .venv\Scripts\python.exe scripts\generate_task_estate_manifest.py            # write manifest + diff vs committed
    .venv\Scripts\python.exe scripts\generate_task_estate_manifest.py --diff-only
    .venv\Scripts\python.exe scripts\generate_task_estate_manifest.py --update-docs
    .venv\Scripts\python.exe scripts\generate_task_estate_manifest.py --fixture tests\fixtures\task_estate_small.json --out-dir <tmp>
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

log = logging.getLogger("task_estate_manifest")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # D-266: every spawn windowless
AZ = timezone(timedelta(hours=-7))                        # Arizona has no DST

SCHEMA_VERSION = 1
OUT_DIR = _REPO_ROOT / "deployment" / "manifest"
MANIFEST_JSON = "task-estate.json"
MANIFEST_MD = "task-estate.md"
BOOTSTRAP_DOC = _REPO_ROOT / "deployment" / "bootstrap-new-machine.md"
RUNBOOK_DOC = _REPO_ROOT / "deployment" / "runbook.md"
TASK_STATE_YAML = _REPO_ROOT / "data" / "maps" / "scheduled-task-state.yaml"
DEPLOYMENT_DIR = _REPO_ROOT / "deployment"
TASK_LOG_DIR = _REPO_ROOT / "logs" / "tasks"

BOOTSTRAP_BLOCK = "scheduled-estate"
RUNBOOK_BLOCK = "task-registry"

#: The bot-estate naming set. A task outside it is NOT Cora's (the Cowork pin task
#: `cowork-model-pin-weekly` is deliberately outside: it belongs to the Cowork estate).
NAME_PREFIXES: tuple[str, ...] = ("cowork-cora-", "Cora - ")
NAME_EXACT: tuple[str, ...] = ("cora-watchdog",)
COWORK_PIN_TASK = "cowork-model-pin-weekly"
COWORK_PIN_SCRIPT = r"C:\Users\Harri\code\code\pin-scheduled-task-models.ps1"

#: Fields that participate in the drift diff (everything else is volatile).
DIFF_KINDS: tuple[str, ...] = ("added", "removed", "cron_changed", "enabled_changed",
                               "action_changed", "principal_changed", "settings_changed")
#: Non-volatile settings that participate in `settings_changed` (the 2026-06-09 gmail
#: ExecutionTimeLimit incident class; the service's RestartCount/Interval).
SETTINGS_DIFF_KEYS: tuple[str, ...] = ("execution_time_limit", "multiple_instances", "restart_count",
                                       "restart_interval", "wake_to_run", "run_only_if_network_available")
WARN_PREFIX = "task-estate-drift"

_DAYS = (("Sun", 1), ("Mon", 2), ("Tue", 4), ("Wed", 8), ("Thu", 16), ("Fri", 32), ("Sat", 64))
_SCRIPT_TOKEN_RE = re.compile(r'"?([A-Za-z]:\\[^"\s]+?\.(?:py|ps1)|scripts\\[^"\s]+?\.py|scripts/[^"\s]+?\.py|[^"\s]+?\.(?:py|ps1))"?',
                              re.IGNORECASE)


def name_matches(name: str) -> bool:
    n = str(name or "")
    return n in NAME_EXACT or any(n.startswith(p) for p in NAME_PREFIXES)


# ── enumeration 1: Task Scheduler via CIM (the source of the manifest) ────────
_PS_ENUMERATE = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ts = @(Get-ScheduledTask | Where-Object { $_.TaskName -like 'cowork-cora-*' -or $_.TaskName -like 'Cora - *' -or $_.TaskName -eq 'cora-watchdog' })
$rows = foreach ($t in $ts) {
  $i = $null
  try { $i = $t | Get-ScheduledTaskInfo } catch {}
  $trig = @(foreach ($tr in @($t.Triggers)) {
    if ($null -eq $tr) { continue }
    $en = $true; if ($null -ne $tr.Enabled) { $en = [bool]$tr.Enabled }
    $ri = ''; $rd = ''; $stop = $false
    if ($null -ne $tr.Repetition) { $ri = [string]$tr.Repetition.Interval; $rd = [string]$tr.Repetition.Duration; $stop = [bool]$tr.Repetition.StopAtDurationEnd }
    [pscustomobject]@{
      type = [string]$tr.CimClass.CimClassName; enabled = $en; start_boundary = [string]$tr.StartBoundary
      days_interval = $tr.DaysInterval; days_of_week = $tr.DaysOfWeek; weeks_interval = $tr.WeeksInterval
      rep_interval = $ri; rep_duration = $rd; rep_stop_at_end = $stop; exec_limit = [string]$tr.ExecutionTimeLimit; user_id = [string]$tr.UserId
    }
  })
  $acts = @(foreach ($a in @($t.Actions)) {
    if ($null -eq $a) { continue }
    [pscustomobject]@{ execute = [string]$a.Execute; arguments = [string]$a.Arguments; working_directory = [string]$a.WorkingDirectory }
  })
  $lr = ''; $nr = ''; $res = $null; $miss = $null
  if ($i) {
    if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $lr = $i.LastRunTime.ToString('yyyy-MM-ddTHH:mm:ss') }
    if ($i.NextRunTime -and $i.NextRunTime.Year -gt 2000) { $nr = $i.NextRunTime.ToString('yyyy-MM-ddTHH:mm:ss') }
    $res = $i.LastTaskResult; $miss = $i.NumberOfMissedRuns
  }
  [pscustomobject]@{
    name = $t.TaskName; path = $t.TaskPath; state = [string]$t.State
    description = [string]$t.Description; author = [string]$t.Author
    principal = [pscustomobject]@{ user_id = [string]$t.Principal.UserId; logon_type = [string]$t.Principal.LogonType; run_level = [string]$t.Principal.RunLevel }
    settings = [pscustomobject]@{
      start_when_available = [bool]$t.Settings.StartWhenAvailable; execution_time_limit = [string]$t.Settings.ExecutionTimeLimit
      multiple_instances = [string]$t.Settings.MultipleInstances; restart_count = $t.Settings.RestartCount
      restart_interval = [string]$t.Settings.RestartInterval; wake_to_run = [bool]$t.Settings.WakeToRun
      run_only_if_network_available = [bool]$t.Settings.RunOnlyIfNetworkAvailable
      disallow_start_if_on_batteries = [bool]$t.Settings.DisallowStartIfOnBatteries
      stop_if_going_on_batteries = [bool]$t.Settings.StopIfGoingOnBatteries; hidden = [bool]$t.Settings.Hidden
    }
    triggers = $trig; actions = $acts
    info = [pscustomobject]@{ last_run_time = $lr; last_task_result = $res; next_run_time = $nr; number_of_missed_runs = $miss }
  }
}
ConvertTo-Json -InputObject @($rows) -Depth 6 -Compress
"""

_PS_PIN_TASK = (
    "$t = Get-ScheduledTask -TaskName '" + COWORK_PIN_TASK + "' -ErrorAction SilentlyContinue; "
    "if ($t) { Write-Output ([string]$t.State) } else { Write-Output 'ABSENT' }"
)


def _run_powershell(script: str, timeout: int = 180) -> str:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW,
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"powershell rc={proc.returncode}: {proc.stderr.strip()[:400]}")
    return proc.stdout


_GENERIC_TRIGGER = "MSFT_TaskTrigger"   # CIM hides calendar detail for schtasks-registered monthly/minute triggers
_TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


def fetch_task_xml(names: list[str]) -> dict[str, str]:
    """Export-ScheduledTask for the named tasks (read-only), name -> XML text."""
    if not names:
        return {}
    arr = ",".join("'" + n.replace("'", "''") + "'" for n in names)
    ps = ("$out = @{}; foreach ($n in @(" + arr + ")) { try { $out[$n] = [string](Export-ScheduledTask -TaskName $n) } "
          "catch { $out[$n] = '' } }; ConvertTo-Json -InputObject $out -Depth 3 -Compress")
    try:
        data = json.loads(_run_powershell(ps, timeout=120).strip() or "{}")
    except Exception as exc:  # noqa: BLE001
        log.info("task XML export failed: %s", exc)
        return {}
    return {str(k): str(v or "") for k, v in data.items()} if isinstance(data, dict) else {}


def triggers_from_xml(xml_text: str) -> list[dict[str, Any]]:
    """Normalised trigger dicts parsed from a task's XML <Triggers> (the shape
    enumerate_cim produces, plus the calendar detail CIM omits)."""
    import xml.etree.ElementTree as ET  # noqa: PLC0415
    # Export-ScheduledTask hands back an already-decoded str whose declaration still
    # says UTF-16; ElementTree refuses a str carrying an encoding declaration, so strip it.
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", xml_text.lstrip("﻿"), count=1).strip()
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        log.info("task XML unparseable: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    trig_parent = root.find(f"{_TASK_NS}Triggers")
    if trig_parent is None:
        return []

    def _txt(el, tag: str) -> str:
        c = el.find(f"{_TASK_NS}{tag}")
        return (c.text or "").strip() if c is not None and c.text else ""

    def _child_tags(el, tag: str) -> list[str]:
        c = el.find(f"{_TASK_NS}{tag}")
        return [x.tag.replace(_TASK_NS, "") for x in list(c)] if c is not None else []

    for tr in trig_parent:
        kind = tr.tag.replace(_TASK_NS, "")
        en_txt = _txt(tr, "Enabled")
        rep = tr.find(f"{_TASK_NS}Repetition")
        row: dict[str, Any] = {
            "type": f"XML:{kind}", "enabled": (en_txt.lower() != "false"), "start_boundary": _txt(tr, "StartBoundary"),
            "days_interval": None, "days_of_week": None, "weeks_interval": None,
            "rep_interval": _txt(rep, "Interval") if rep is not None else "",
            "rep_duration": _txt(rep, "Duration") if rep is not None else "",
            "exec_limit": _txt(tr, "ExecutionTimeLimit"), "user_id": _txt(tr, "UserId"),
        }
        sched_month = tr.find(f"{_TASK_NS}ScheduleByMonth")
        sched_mdow = tr.find(f"{_TASK_NS}ScheduleByMonthDayOfWeek")
        sched_day = tr.find(f"{_TASK_NS}ScheduleByDay")
        sched_week = tr.find(f"{_TASK_NS}ScheduleByWeek")
        if sched_month is not None:
            row["type"] = "XML:CalendarTrigger/ScheduleByMonth"
            row["days_of_month"] = [d.text.strip() for d in sched_month.findall(f"{_TASK_NS}DaysOfMonth/{_TASK_NS}Day") if d.text]
            row["months"] = _child_tags(sched_month, "Months")
        elif sched_mdow is not None:
            row["type"] = "XML:CalendarTrigger/ScheduleByMonthDayOfWeek"
            row["weeks_of_month"] = [w.text.strip() for w in sched_mdow.findall(f"{_TASK_NS}Weeks/{_TASK_NS}Week") if w.text]
            row["days_of_week_names"] = _child_tags(sched_mdow, "DaysOfWeek")
            row["months"] = _child_tags(sched_mdow, "Months")
        elif sched_day is not None:
            row["type"] = "XML:CalendarTrigger/ScheduleByDay"
            row["days_interval"] = int(_txt(sched_day, "DaysInterval") or 1)
        elif sched_week is not None:
            row["type"] = "XML:CalendarTrigger/ScheduleByWeek"
            row["weeks_interval"] = int(_txt(sched_week, "WeeksInterval") or 1)
            row["days_of_week_names"] = _child_tags(sched_week, "DaysOfWeek")
        out.append(row)
    return out


def enumerate_cim() -> list[dict[str, Any]]:
    """Every Cora-named task from Get-ScheduledTask + Get-ScheduledTaskInfo (one call).
    Tasks whose triggers CIM reports only as the generic MSFT_TaskTrigger (schtasks
    /SC MONTHLY etc.) get their triggers re-read from the task XML so the manifest
    records a RESTORABLE schedule, not 'trigger from <date>'."""
    if os.name != "nt":
        raise RuntimeError("Task Scheduler enumeration needs Windows")
    out = _run_powershell(_PS_ENUMERATE).strip()
    if not out:
        return []
    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]
    tasks = [d for d in data if isinstance(d, dict) and name_matches(d.get("name", ""))]
    # Every task's full-fidelity XML is the RESTORE form (deployment/manifest/tasks/<slug>.xml,
    # registered by deployment/register-estate-from-manifest.ps1). Tasks whose triggers CIM
    # reports only as the generic MSFT_TaskTrigger also get their triggers re-read from it.
    xmls = fetch_task_xml([t["name"] for t in tasks])
    for t in tasks:
        xml_text = xmls.get(t["name"], "")
        t["_xml"] = xml_text
        generic = any(str(tr.get("type") or "") == _GENERIC_TRIGGER for tr in (t.get("triggers") or []) if isinstance(tr, dict))
        if xml_text and generic:
            parsed = triggers_from_xml(xml_text)
            if parsed:
                t["triggers"] = parsed
                t["triggers_source"] = "xml"
    return tasks


# ── enumeration 2: schtasks CSV (independent code path in Windows) ────────────
def enumerate_schtasks_csv() -> set[str] | None:
    """None when schtasks failed or parsed to nothing -- a failed second method must read
    as UNAVAILABLE, never as 'DISAGREE: 0 tasks' (D-051 lens A)."""
    proc = subprocess.run(["schtasks", "/query", "/fo", "CSV", "/v"], capture_output=True, text=True,
                          timeout=120, creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        log.info("schtasks rc=%s: %s", proc.returncode, (proc.stderr or proc.stdout).strip()[:200])
        return None
    names: set[str] = set()
    rows = 0
    for row in csv.DictReader(io.StringIO(proc.stdout)):
        rows += 1
        raw = str(row.get("TaskName") or "")
        if raw == "TaskName":       # /v repeats the header before each block on some builds
            continue
        name = raw.rsplit("\\", 1)[-1]
        if name_matches(name):
            names.add(name)
    return names if rows else None


# ── enumeration 3: the in-process cora_health view (the service's own table) ──
def enumerate_health_view() -> set[str] | None:
    """The same code path the live MCP `cora_health` view runs. None when the module
    cannot be imported here (no mcp package on this interpreter, non-Windows)."""
    try:
        from cora.mcp_server import _read_task_last_results  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log.info("health view unavailable: %s", exc)
        return None
    try:
        return {n for n in _read_task_last_results() if name_matches(n)}
    except Exception as exc:  # noqa: BLE001
        log.info("health view failed: %s", exc)
        return None


def enumeration_agreement(cim_names: set[str], csv_names: set[str] | None,
                          health_names: set[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"cim_count": len(cim_names), "agree": True, "methods": {}}
    for label, other in (("schtasks_csv", csv_names), ("cora_health_view", health_names)):
        if other is None or (not other and cim_names):
            # a method that returned NOTHING while CIM sees tasks failed; it is not a disagreement
            out["methods"][label] = {"available": False, "reason": "no rows returned" if other is not None else "call failed"}
            continue
        only_cim = sorted(cim_names - other)
        only_other = sorted(other - cim_names)
        agree = not only_cim and not only_other
        out["methods"][label] = {"available": True, "count": len(other), "agree": agree,
                                 "only_in_cim": only_cim, "only_in_method": only_other}
        out["agree"] = out["agree"] and agree
    return out


# ── normalisation ──────────────────────────────────────────────────────────────
def _days_text(mask: Any) -> str:
    try:
        m = int(mask)
    except (TypeError, ValueError):
        return ""
    names = [n for n, bit in _DAYS if m & bit]
    if len(names) == 7:
        return "every day"
    return ",".join(names)


def _clock(start_boundary: str) -> str:
    s = str(start_boundary or "")
    if "T" in s and len(s) >= 16:
        return s[11:16]
    return ""


def trigger_text(trig: dict[str, Any]) -> str:
    """Human, stable, cron-like text for one trigger (the value the diff compares)."""
    t = str(trig.get("type") or "")
    clock = _clock(trig.get("start_boundary"))
    rep = str(trig.get("rep_interval") or "")
    dis = "" if trig.get("enabled", True) else " [trigger disabled]"
    if t.endswith("LogonTrigger"):
        return "at logon" + dis
    if t.endswith("BootTrigger"):
        return "at boot" + dis
    months = trig.get("months") or []
    month_note = f" in {','.join(m[:3] for m in months)}" if months and len(months) < 12 else ""
    if t.endswith("DailyTrigger") or t.endswith("ScheduleByDay"):
        di = trig.get("days_interval")
        every = f" every {di} days" if di and int(di) > 1 else ""
        base = f"daily {clock}{every}"
    elif t.endswith("WeeklyTrigger") or t.endswith("ScheduleByWeek"):
        wi = trig.get("weeks_interval")
        every = f" every {wi} weeks" if wi and int(wi) > 1 else ""
        names = trig.get("days_of_week_names")
        days = ",".join(n[:3] for n in names) if names else _days_text(trig.get("days_of_week"))
        base = f"weekly {days} {clock}{every}".replace("  ", " ")
    elif t.endswith("ScheduleByMonth"):
        days = ",".join(str(d) for d in (trig.get("days_of_month") or [])) or "?"
        base = f"monthly day {days} {clock}{month_note}"
    elif t.endswith("ScheduleByMonthDayOfWeek"):
        weeks = ",".join(str(w) for w in (trig.get("weeks_of_month") or [])) or "?"
        names = ",".join(n[:3] for n in (trig.get("days_of_week_names") or [])) or "?"
        base = f"monthly week {weeks} {names} {clock}{month_note}"
    elif t.endswith("TimeTrigger"):
        base = (f"from {str(trig.get('start_boundary') or '')[:16]}" if rep
                else f"once {str(trig.get('start_boundary') or '')[:16]}")
    else:
        # generic MSFT_TaskTrigger with no XML detail available, or an unknown class
        base = f"{t.replace('MSFT_Task', '').replace('XML:', '').replace('Trigger', '') or 'trigger'} from {str(trig.get('start_boundary') or '')[:16]}"
    if rep:
        dur = str(trig.get("rep_duration") or "")
        stop = " stop-at-end" if trig.get("rep_stop_at_end") else ""
        base = f"every {rep}" + (f" for {dur}{stop}" if dur else "") + f" ({base})"
    return base.strip() + dis


def child_command(action: dict[str, Any]) -> str:
    from cora import nightly_catchup as nc  # noqa: PLC0415
    return nc.child_command_from_action(str(action.get("execute") or ""), str(action.get("arguments") or ""))


def is_windowless(action: dict[str, Any]) -> bool:
    return str(action.get("execute") or "").lower().endswith("pythonw.exe") and \
        "run_hidden.py" in str(action.get("arguments") or "")


#: The checkout the ESTATE is registered against (every action hardcodes it). A run from
#: a git worktree still relativizes script paths against this root, so the manifest
#: reads `scripts/backup_logs.py` wherever it was generated. --live-root overrides.
LIVE_ROOT = Path(os.environ.get("CORA_LIVE_ROOT", r"C:\Users\Harri\code\cora"))


def _relativize(raw: str) -> str:
    p = Path(raw)
    if not p.is_absolute():
        return raw.replace("\\", "/")
    for root in (LIVE_ROOT, _REPO_ROOT):
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            continue
    return raw.replace("\\", "/")


def script_path_of(child: str) -> str:
    """The first .py/.ps1 token of the child command, repo-relative when under the live
    checkout (or this one). `-m cora.main` (the service) reads as `cora.main`."""
    if " -m cora.main" in child or child.endswith("-m cora.main"):
        return "cora.main"
    m = _SCRIPT_TOKEN_RE.search(child)
    if not m:
        return ""
    return _relativize(m.group(1).strip('"'))


def task_slug(name: str, actions: list[dict[str, Any]]) -> str:
    from cora import nightly_catchup as nc  # noqa: PLC0415
    for a in actions:
        s = nc.slug_from_action(str(a.get("arguments") or ""))
        if s:
            return s
    return nc.sanitize_slug(name)


def newest_task_log(slug: str, log_dir: Path = TASK_LOG_DIR) -> dict[str, Any]:
    try:
        files = sorted(log_dir.glob(f"{slug}-????-??-??.log"))
    except OSError:
        files = []
    if not files:
        return {"pattern": f"logs/tasks/{slug}-<YYYY-MM-DD>.log", "newest": "", "newest_mtime_utc": "", "count": 0}
    newest = files[-1]
    try:
        mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        mtime = ""
    return {"pattern": f"logs/tasks/{slug}-<YYYY-MM-DD>.log", "newest": f"logs/tasks/{newest.name}",
            "newest_mtime_utc": mtime, "count": len(files)}


def load_intent(path: Path = TASK_STATE_YAML) -> dict[str, Any]:
    """running / disabled / enabled / run_markers from scheduled-task-state.yaml."""
    try:
        import yaml  # noqa: PLC0415
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc), "running": set(), "disabled": set(),
                "enabled": set(), "run_markers": {}}
    rm = {}
    for row in data.get("run_markers") or []:
        if isinstance(row, dict) and row.get("name"):
            rm[str(row["name"])] = row
    return {"available": True, "running": {str(x) for x in data.get("running") or []},
            "disabled": {str(x) for x in data.get("disabled") or []},
            "enabled": {str(x) for x in data.get("enabled") or []}, "run_markers": rm}


def intent_for(name: str, enabled: bool, intent: dict[str, Any]) -> tuple[str, str]:
    """(intent, drift) -- drift is non-empty when the host state contradicts the yaml."""
    if name in intent.get("running", ()):
        # the yaml's ONLY task-state CRITICAL: the always-on service must be enabled + Running
        return "running", ("host DISABLED but intent running" if not enabled else "")
    if name in intent.get("disabled", ()):
        return "disabled", ("host ENABLED but intent disabled" if enabled else "")
    if name in intent.get("enabled", ()):
        return "enabled", ("host DISABLED but intent enabled" if not enabled else "")
    return "UNROWED", ("host DISABLED, not in the disabled list" if not enabled else "")


def load_setup_scripts(deploy_dir: Path = DEPLOYMENT_DIR) -> dict[str, str]:
    """path-name -> text for every deployment/*.ps1 (read once)."""
    out: dict[str, str] = {}
    try:
        for p in sorted(deploy_dir.glob("*.ps1")):
            try:
                out[p.name] = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    except OSError:
        pass
    return out


_PS_COMMENT_LINE_RE = re.compile(r"^\s*#.*$", re.MULTILINE)
_PS_BLOCK_COMMENT_RE = re.compile(r"<#.*?#>", re.DOTALL)


def _strip_ps_comments(text: str) -> str:
    """Drop `# ...` lines and `<# ... #>` blocks: a comment that names another task
    (`#     schtasks /Change /TN "Cora - F3E Daily Ecom Brief" /Disable`) must never read
    as a registration (D-051 lens A, 2026-09-23)."""
    return _PS_COMMENT_LINE_RE.sub("", _PS_BLOCK_COMMENT_RE.sub("", text))


def _registration_re(name: str) -> re.Pattern[str]:
    """A REGISTRATION-shaped mention on a NON-comment line: `$TaskName = "<name>"`,
    `-TaskName "<name>"`, or `schtasks /Create ... /TN "<name>"` (any quote style). A
    `/TN` on a `/Change`, `/Delete` or `/End` line is not a registration."""
    q = re.escape(name)
    return re.compile(r"""(?:\$\w*(?:TaskName|TASK_NAME|TASKNAME)\w*\s*=\s*|-TaskName\s+|/Create\b[^\r\n]*?/TN\s+)["']""" + q + r"""["']""")


def setup_scripts_for(name: str, scripts: dict[str, str]) -> list[str]:
    """The setup-*.ps1 files that REGISTER this exact task name (comments stripped
    first). Registration-shaped matches win; a bare quoted mention on a non-comment
    line is the fallback only when no script registers the name in that shape."""
    reg = _registration_re(name)
    setup = {fn: _strip_ps_comments(text) for fn, text in scripts.items() if fn.startswith("setup-")}
    strong = sorted(fn for fn, text in setup.items() if reg.search(text))
    if strong:
        return strong
    quoted = (f'"{name}"', f"'{name}'")
    return sorted(fn for fn, text in setup.items() if any(q in text for q in quoted))


def load_ladder_index() -> list[tuple[str, str, str]]:
    """(lane, tier, searchable text) per registry row -- heuristic script/name match."""
    try:
        from cora import ladder_registry as lr  # noqa: PLC0415
        reg = lr.load()
        if not reg.get("available"):
            return []
        rows = []
        for r in lr.lanes(reg):
            em = r.get("evidence_monitor") or {}
            text = " ".join(str(x) for x in (r.get("title"), em.get("description") if isinstance(em, dict) else "",
                                              r.get("audit_surface"), r.get("promotion_criteria")))
            rows.append((str(r.get("lane")), str(r.get("tier")), text.lower()))
        return rows
    except Exception as exc:  # noqa: BLE001
        log.info("ladder registry unavailable: %s", exc)
        return []


def ladder_lane_for(name: str, script: str, index: list[tuple[str, str, str]]) -> str:
    keys = [k for k in (Path(script).name if script else "", name) if k and k != "cora.main"]
    for lane, tier, text in index:
        if any(k.lower() in text for k in keys):
            return f"{lane} ({tier})"
    return "UNROWED"


def load_run_markers(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Latest marker per task from logs/task-runs.jsonl (an explicit `path` wins over the
    module default AND over any ambient TASK_RUNS_LEDGER_PATH)."""
    try:
        from cora import run_marker  # noqa: PLC0415
        return run_marker.latest_by_task(path) if path is not None else run_marker.latest_by_task()
    except Exception as exc:  # noqa: BLE001
        log.info("run markers unavailable: %s", exc)
        return {}


def normalize_task(raw: dict[str, Any], *, intent: dict[str, Any], scripts: dict[str, str],
                   ladder: list[tuple[str, str, str]], markers: dict[str, dict[str, Any]],
                   log_dir: Path = TASK_LOG_DIR) -> dict[str, Any]:
    name = str(raw.get("name") or "")
    state = str(raw.get("state") or "")
    enabled = state.lower() != "disabled"
    triggers = [t for t in (raw.get("triggers") or []) if isinstance(t, dict)]
    actions = [a for a in (raw.get("actions") or []) if isinstance(a, dict)]
    principal = raw.get("principal") or {}
    settings = raw.get("settings") or {}
    info = raw.get("info") or {}
    child = child_command(actions[0]) if actions else ""
    script = script_path_of(child)
    slug = task_slug(name, actions)
    it, drift = intent_for(name, enabled, intent)
    marker = markers.get(name)
    return {
        "name": name,
        "path": str(raw.get("path") or "\\"),
        "state": state,
        "enabled": enabled,
        "trigger_text": " + ".join(trigger_text(t) for t in triggers) or "(no trigger)",
        "triggers": triggers,
        "action_text": " | ".join(f"{a.get('execute','')} {a.get('arguments','')}".strip() for a in actions),
        "working_directory": str(actions[0].get("working_directory") or "") if actions else "",
        "child_command": child,
        "script": script,
        "windowless": bool(actions) and is_windowless(actions[0]),
        "run_as": {"user_id": str(principal.get("user_id") or ""),
                   "logon_type": str(principal.get("logon_type") or ""),
                   "run_level": str(principal.get("run_level") or "")},
        "start_when_available": bool(settings.get("start_when_available")),
        "execution_time_limit": str(settings.get("execution_time_limit") or ""),
        "multiple_instances": str(settings.get("multiple_instances") or ""),
        "restart_count": settings.get("restart_count"),
        "restart_interval": str(settings.get("restart_interval") or ""),
        "wake_to_run": bool(settings.get("wake_to_run")),
        "run_only_if_network_available": bool(settings.get("run_only_if_network_available")),
        "last_task_result": info.get("last_task_result"),
        "last_run_time": str(info.get("last_run_time") or ""),
        "next_run_time": str(info.get("next_run_time") or ""),
        "number_of_missed_runs": info.get("number_of_missed_runs"),
        "run_marker": {"registered": name in intent.get("run_markers", {}),
                       "present": marker is not None,
                       "last_ts": str(marker.get("ts") or "") if marker else ""},
        "log_slug": slug,
        "log": newest_task_log(slug, log_dir),
        "intent": it,
        "intent_drift": drift,
        "ladder_lane": ladder_lane_for(name, script, ladder),
        "setup_scripts": setup_scripts_for(name, scripts),
        "description": str(raw.get("description") or "")[:200],
        "_xml": str(raw.get("_xml") or ""),   # lifted out of the JSON by build_manifest -> tasks/<slug>.xml
    }


# ── the Cowork estate cross-reference (ONE reader: the mirror's) ──────────────
def cowork_estate_crossref(pin_state: str | None = None) -> dict[str, Any]:
    """Count + a digest of the Cowork task ids -- NEVER the id list. The ids name personal
    and staff-adjacent tasks (D-051 lens B); a committed inventory of them is a leak. The
    digest still lets the mirror's own delta detect adds/removes."""
    out: dict[str, Any] = {"migrates": False, "doctrine": "charter D4 (2026-09-01): the Cowork estate stays on the office machine; revisit at Phase 3",
                           "pin_task": COWORK_PIN_TASK, "pin_script": COWORK_PIN_SCRIPT.replace("code\\code", "code"),
                           "reader": "scripts/mirror_claude_workspace._task_dirs"}
    # mirror_claude_workspace loads the live .env at import (module-level load_dotenv). Nothing
    # from it is written anywhere here, but the environment is snapshotted and restored so the
    # generator's own process never carries those values past this call.
    saved_env = dict(os.environ)
    try:
        import mirror_claude_workspace as mw  # noqa: PLC0415
        cfg = mw.load_config()
        dirs = mw._task_dirs(cfg)  # noqa: SLF001 -- the one implementation, reused on purpose
        import hashlib  # noqa: PLC0415
        digest = hashlib.sha256("\n".join(sorted(d.name for d in dirs)).encode("utf-8")).hexdigest()[:16]
        out.update({"available": True, "root": str(mw._tasks_root(cfg)), "count": len(dirs),  # noqa: SLF001
                    "task_ids_sha256_16": digest})
    except Exception as exc:  # noqa: BLE001
        out.update({"available": False, "reason": str(exc)})
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
    if pin_state is not None:
        out["pin_task_state"] = pin_state
    return out


def pin_task_state() -> str:
    try:
        return _run_powershell(_PS_PIN_TASK, timeout=60).strip() or "UNKNOWN"
    except Exception as exc:  # noqa: BLE001
        return f"UNKNOWN ({exc})"


# ── manifest ───────────────────────────────────────────────────────────────────
def run_marker_coverage(tasks: list[dict[str, Any]], log_dir: Path = TASK_LOG_DIR) -> dict[str, Any]:
    with_marker = sorted(t["name"] for t in tasks if t["run_marker"]["present"])
    try:
        prefixes = sorted({re.sub(r"-\d{4}-\d{2}-\d{2}(?:\.log.*)?$", "", p.name) for p in log_dir.glob("*.log")})
    except OSError:
        prefixes = []
    task_slugs = {t["log_slug"] for t in tasks}
    return {"tasks_total": len(tasks), "tasks_with_marker": len(with_marker), "with_marker": with_marker,
            "per_task_log_prefixes": len(prefixes),
            "log_prefixes_not_matching_a_task": sorted(p for p in prefixes if p not in task_slugs)}


def build_manifest(tasks: list[dict[str, Any]], *, agreement: dict[str, Any] | None = None,
                   cowork: dict[str, Any] | None = None, now: datetime | None = None,
                   host: str = "", log_dir: Path = TASK_LOG_DIR, host_time_zone: str = "") -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    tasks = sorted(tasks, key=lambda t: t["name"].lower())
    # the per-task XML is written as files by write_outputs, never embedded in the JSON
    task_xml = {t["log_slug"]: t.pop("_xml", "") for t in tasks}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_az": now.astimezone(AZ).strftime("%Y-%m-%d %H:%M AZ"),
        "generator": "scripts/generate_task_estate_manifest.py",
        "host": host or os.environ.get("COMPUTERNAME", ""),
        "host_time_zone": host_time_zone,
        "task_xml_dir": "deployment/manifest/tasks",
        "_task_xml": task_xml,
        "source": "Get-ScheduledTask + Get-ScheduledTaskInfo (CIM), cross-checked vs schtasks CSV + cora_health view",
        "name_filters": {"prefixes": list(NAME_PREFIXES), "exact": list(NAME_EXACT)},
        "count": len(tasks),
        "enabled_count": sum(1 for t in tasks if t["enabled"]),
        "disabled": sorted(t["name"] for t in tasks if not t["enabled"]),
        "interactive_logon_count": sum(1 for t in tasks if t["run_as"]["logon_type"].lower().startswith("interactive")),
        "start_when_available_false": sorted(t["name"] for t in tasks if not t["start_when_available"]),
        "highest_run_level": sorted(t["name"] for t in tasks if t["run_as"]["run_level"].lower() == "highest"),
        "without_setup_script": sorted(t["name"] for t in tasks if not t["setup_scripts"]),
        "intent_drift": {t["name"]: t["intent_drift"] for t in tasks if t["intent_drift"]},
        "enumeration_agreement": agreement or {},
        "run_marker_coverage": run_marker_coverage(tasks, log_dir),
        "cowork_estate": cowork or {},
        "tasks": tasks,
    }


# ── drift ──────────────────────────────────────────────────────────────────────
def _principal_text(t: dict[str, Any]) -> str:
    ra = t.get("run_as") or {}
    return f"{ra.get('logon_type','')}/{ra.get('run_level','')}/SWA={t.get('start_when_available')}"


def diff_manifests(prev_tasks: list[dict[str, Any]], cur_tasks: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Kind -> WARN lines. Only the non-volatile fields participate."""
    prev = {t["name"]: t for t in prev_tasks}
    cur = {t["name"]: t for t in cur_tasks}
    out: dict[str, list[str]] = {k: [] for k in DIFF_KINDS}
    for n in sorted(set(cur) - set(prev)):
        out["added"].append(f"{WARN_PREFIX}: added {n}")
    for n in sorted(set(prev) - set(cur)):
        out["removed"].append(f"{WARN_PREFIX}: removed {n}")
    for n in sorted(set(prev) & set(cur)):
        p, c = prev[n], cur[n]
        if p.get("trigger_text") != c.get("trigger_text"):
            out["cron_changed"].append(f"{WARN_PREFIX}: cron_changed {n}: {p.get('trigger_text')} -> {c.get('trigger_text')}")
        if bool(p.get("enabled")) != bool(c.get("enabled")):
            out["enabled_changed"].append(f"{WARN_PREFIX}: enabled_changed {n}: {p.get('enabled')} -> {c.get('enabled')}")
        if (p.get("action_text"), p.get("working_directory")) != (c.get("action_text"), c.get("working_directory")):
            out["action_changed"].append(f"{WARN_PREFIX}: action_changed {n}")
        if _principal_text(p) != _principal_text(c):
            out["principal_changed"].append(f"{WARN_PREFIX}: principal_changed {n}: {_principal_text(p)} -> {_principal_text(c)}")
        ps, cs = _settings_text(p), _settings_text(c)
        if ps != cs:
            out["settings_changed"].append(f"{WARN_PREFIX}: settings_changed {n}: {ps} -> {cs}")
    return out


def _settings_text(t: dict[str, Any]) -> str:
    parts = [f"{k}={t.get(k)}" for k in SETTINGS_DIFF_KEYS]
    parts.append(f"user_id={(t.get('run_as') or {}).get('user_id', '')}")
    return ";".join(parts)


def warn_lines(diff: dict[str, list[str]]) -> list[str]:
    return [line for k in DIFF_KINDS for line in diff.get(k, [])]


def load_manifest(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("tasks"), list) else None
    except (OSError, ValueError):
        return None


def diff_against_committed(out_dir: Path = OUT_DIR, live_tasks: list[dict[str, Any]] | None = None,
                           enumerate: Callable[[], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Read-only: live registry vs the committed manifest. Used by the Monday digest."""
    committed = load_manifest(out_dir / MANIFEST_JSON)
    if committed is None:
        return {"available": False, "reason": f"no committed manifest at {out_dir / MANIFEST_JSON}"}
    try:
        if live_tasks is None:
            live_tasks = normalize_all((enumerate or enumerate_cim)())
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"live enumeration failed: {exc}",
                "manifest_count": committed.get("count"), "manifest_generated_at": committed.get("generated_at_utc")}
    diff = diff_manifests(committed["tasks"], live_tasks)
    lines = warn_lines(diff)
    # host time zone: clock triggers are host-local, so a different zone IS drift
    tz_live = host_time_zone() if os.name == "nt" else ""
    tz_manifest = str(committed.get("host_time_zone") or "")
    if tz_live and tz_manifest and tz_live != tz_manifest:
        lines.append(f"{WARN_PREFIX}: timezone_changed host: {tz_manifest} -> {tz_live}")
        diff["timezone_changed"] = [lines[-1]]
    return {"available": True, "manifest_count": committed.get("count"), "live_count": len(live_tasks),
            "manifest_generated_at": committed.get("generated_at_utc"),
            "drift": {k: len(v) for k, v in diff.items()}, "warn_lines": lines, "drifted": bool(lines)}


def host_time_zone() -> str:
    """The host's Windows time-zone id ((Get-TimeZone).Id); '' when unreadable."""
    try:
        return _run_powershell("(Get-TimeZone).Id", timeout=30).strip()
    except Exception as exc:  # noqa: BLE001
        log.info("time zone unreadable: %s", exc)
        return ""


def normalize_all(raw_tasks: list[dict[str, Any]], *, log_dir: Path = TASK_LOG_DIR,
                  marker_path: Path | None = None) -> list[dict[str, Any]]:
    intent = load_intent()
    scripts = load_setup_scripts()
    ladder = load_ladder_index()
    markers = load_run_markers(marker_path)
    return [normalize_task(r, intent=intent, scripts=scripts, ladder=ladder, markers=markers, log_dir=log_dir)
            for r in raw_tasks]


# ── rendering ──────────────────────────────────────────────────────────────────
def _cell(s: Any) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def render_markdown(m: dict[str, Any]) -> str:
    L = [f"# Cora task-estate manifest ({m['count']} tasks)", "",
         f"_Generated {m['generated_at_utc']} ({m['generated_at_az']}) by `{m['generator']}` from the LIVE Task Scheduler "
         f"registry on `{m.get('host') or 'this host'}`. Read-only; the machine-readable twin is "
         f"`deployment/manifest/{MANIFEST_JSON}`. DO NOT HAND-EDIT -- regenerate._", ""]
    ag = m.get("enumeration_agreement") or {}
    L.append("## Enumeration cross-check")
    L.append(f"- CIM (Get-ScheduledTask): **{ag.get('cim_count', m['count'])}**")
    for label, r in (ag.get("methods") or {}).items():
        if not r.get("available"):
            L.append(f"- {label}: unavailable")
        else:
            extra = "" if r.get("agree") else f" -- only in CIM: {r.get('only_in_cim')}; only in {label}: {r.get('only_in_method')}"
            L.append(f"- {label}: **{r.get('count')}** ({'agree' if r.get('agree') else 'DISAGREE'}){extra}")
    L.append("")
    L.append("## Estate facts")
    L += [f"- enabled: {m['enabled_count']} / disabled: {len(m['disabled'])} ({', '.join(m['disabled']) or 'none'})",
          f"- logon type Interactive: {m['interactive_logon_count']} of {m['count']} -- the estate needs a signed-in session to fire at all (the 9/9 incident)",
          f"- StartWhenAvailable=false: {len(m['start_when_available_false'])} ({', '.join(m['start_when_available_false']) or 'none'})",
          f"- RunLevel Highest: {len(m['highest_run_level'])} ({', '.join(m['highest_run_level']) or 'none'})",
          f"- tasks with NO deployment/setup-*.ps1 naming them: {len(m['without_setup_script'])} ({', '.join(m['without_setup_script']) or 'none'})",
          f"- intent drift vs data/maps/scheduled-task-state.yaml: {len(m['intent_drift'])} ({'; '.join(f'{k}: {v}' for k, v in m['intent_drift'].items()) or 'none'})"]
    rm = m.get("run_marker_coverage") or {}
    L.append(f"- run-marker coverage: {rm.get('tasks_with_marker')} of {rm.get('tasks_total')} tasks have a logs/task-runs.jsonl row; "
             f"{rm.get('per_task_log_prefixes')} per-task log prefixes in logs/tasks/ (MEASURED, not widened -- cq-06045f418bd2 governs widening)")
    cw = m.get("cowork_estate") or {}
    L.append("")
    L.append("## Cowork estate (cross-reference only -- NOT in this manifest, NOT migrated; charter D4)")
    if cw.get("available"):
        L.append(f"- {cw.get('count')} task folders under `{cw.get('root')}` (reader: `{cw.get('reader')}`); pin task `{cw.get('pin_task')}` state: {cw.get('pin_task_state', 'n/a')}; pin script `{cw.get('pin_script')}`")
    else:
        L.append(f"- reader unavailable: {cw.get('reason', 'n/a')}")
    L.append("")
    L.append("## Tasks")
    L.append("| Task | Trigger | Child command (script) | Run-as | SWA | State | Last result (last run) | Marker | Log | Intent | Ladder | Setup script |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for t in m["tasks"]:
        ra = t["run_as"]
        marker = ("yes" if t["run_marker"]["present"] else "no") + (" (registered)" if t["run_marker"]["registered"] else "")
        last = f"{t['last_task_result']} ({t['last_run_time'] or 'never'})"
        intent = t["intent"] + (f" **DRIFT: {t['intent_drift']}**" if t["intent_drift"] else "")
        L.append("| " + " | ".join(_cell(x) for x in (
            f"`{t['name']}`", t["trigger_text"], f"`{t['child_command']}`" + (f" -> `{t['script']}`" if t["script"] else ""),
            f"{ra['logon_type']}/{ra['run_level']}", "yes" if t["start_when_available"] else "**no**", t["state"],
            last, marker, t["log"]["pattern"], intent, t["ladder_lane"], ", ".join(t["setup_scripts"]) or "**none**")) + " |")
    L.append("")
    return "\n".join(L) + "\n"


def render_bootstrap_block(m: dict[str, Any]) -> str:
    """The `## Scheduled estate` body for bootstrap-new-machine.md. Date-only stamp so a
    same-day regeneration is byte-identical."""
    date = m["generated_at_az"][:10]   # AZ date, same as the DR block: an evening run must not stamp tomorrow
    L = [f"_Generated {date} by `scripts/generate_task_estate_manifest.py --update-docs` from the live registry: "
         f"**{m['count']} tasks** ({m['enabled_count']} enabled). Source of truth: `deployment/manifest/{MANIFEST_JSON}`. "
         f"Do not hand-list tasks here -- regenerate._", "",
         f"**Register the estate FROM THE MANIFEST** (from an ELEVATED PowerShell at the repo root; host time zone must be `{m.get('host_time_zone') or 'the office host zone'}` first -- `Set-TimeZone`):",
         "1. `.\\deployment\\register-estate-from-manifest.ps1` (dry-run plan) then `.\\deployment\\register-estate-from-manifest.ps1 -Apply` -- registers every task below from "
         "`deployment/manifest/tasks/<slug>.xml` (full-fidelity Task Scheduler exports: triggers, windowless action, settings, run level, and `Enabled=false` for the "
         f"{len(m['disabled'])} intent-disabled tasks). The `setup-*.ps1` scripts are NOT run on a rebuild: several carry drifted clocks or would re-enable disabled tasks; they create NEW tasks only.",
         "2. Restore `.env` (Phase 4) BEFORE starting anything; then `Start-ScheduledTask -TaskName cowork-cora-service` (the service otherwise starts at the next logon).",
         "3. Verify: `Get-ScheduledTask | Where-Object { $_.TaskName -like 'cowork-cora-*' -or $_.TaskName -like 'Cora - *' -or $_.TaskName -eq 'cora-watchdog' } | Measure-Object`"
         f" -> expect **{m['count']}**, then `.venv\\Scripts\\python.exe scripts\\generate_task_estate_manifest.py --diff-only` -> expect ZERO `task-estate-drift` lines "
         "(cron / enabled / action / principal / settings / time zone are all compared).",
         "4. The Cowork estate (Claude desktop scheduled tasks) is NOT registered by any of this: it stays on the office machine (charter D4); "
         f"its weekly pin task is `{COWORK_PIN_TASK}`.", "",
         "| Task | Trigger | XML (restore form) | Created by (setup script, informational) | Run level / logon | SWA | Intent |", "|---|---|---|---|---|---|---|"]
    for t in m["tasks"]:
        L.append("| " + " | ".join(_cell(x) for x in (
            f"`{t['name']}`", t["trigger_text"], f"`tasks/{t['log_slug']}.xml`",
            ", ".join(f"`{s}`" for s in t["setup_scripts"]) or "(none -- manifest XML only)",
            f"{t['run_as']['run_level']} / {t['run_as']['logon_type']}", "yes" if t["start_when_available"] else "**no**",
            t["intent"])) + " |")
    return "\n".join(L) + "\n"


def render_runbook_table(m: dict[str, Any]) -> str:
    date = m["generated_at_az"][:10]   # AZ date, same as the DR block: an evening run must not stamp tomorrow
    L = [f"_Generated {date} by `scripts/generate_task_estate_manifest.py --update-docs` from the live registry "
         f"({m['count']} tasks, {m['enabled_count']} enabled). Full columns: `deployment/manifest/{MANIFEST_MD}`. Do not hand-edit._", "",
         "| Task Name | Schedule | Script | State | Run level | Notes |", "|---|---|---|---|---|---|"]
    for t in m["tasks"]:
        notes = []
        if t["intent"] == "disabled":
            notes.append("intent: disabled")
        if t["intent_drift"]:
            notes.append(f"DRIFT: {t['intent_drift']}")
        if not t["start_when_available"]:
            notes.append("SWA=false")
        if not t["windowless"]:
            notes.append("console action (not run_hidden-wrapped)")
        L.append("| " + " | ".join(_cell(x) for x in (
            f"`{t['name']}`", t["trigger_text"], f"`{t['script']}`" if t["script"] else f"`{t['child_command'][:60]}`",
            t["state"], t["run_as"]["run_level"], "; ".join(notes))) + " |")
    return "\n".join(L) + "\n"


def _markers(name: str) -> tuple[str, str]:
    return f"<!-- BEGIN GENERATED: {name} -->", f"<!-- END GENERATED: {name} -->"


def replace_generated_block(text: str, name: str, body: str) -> str:
    """Replace the text between the BEGIN/END markers (exclusive). Raises when either
    marker is missing or repeated -- a doc must opt in ONCE by hand."""
    begin, end = _markers(name)
    if text.count(begin) != 1 or text.count(end) != 1:
        raise ValueError(f"markers for '{name}' must appear exactly once (BEGIN x{text.count(begin)}, END x{text.count(end)})")
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{begin}\n{body.rstrip()}\n{end}{tail}"


def update_docs(m: dict[str, Any], *, bootstrap: Path = BOOTSTRAP_DOC, runbook: Path = RUNBOOK_DOC) -> list[str]:
    changed = []
    for path, block, body in ((bootstrap, BOOTSTRAP_BLOCK, render_bootstrap_block(m)),
                              (runbook, RUNBOOK_BLOCK, render_runbook_table(m))):
        old = path.read_text(encoding="utf-8")
        new = replace_generated_block(old, block, body)
        if new != old:
            path.write_text(new, encoding="utf-8", newline="\n")
            try:
                changed.append(str(path.relative_to(_REPO_ROOT)))
            except ValueError:          # a doc outside the repo (tests, ad-hoc runs)
                changed.append(str(path))
    return changed


TASK_XML_SUBDIR = "tasks"


def write_outputs(m: dict[str, Any], out_dir: Path = OUT_DIR) -> list[Path]:
    """JSON + MD + one XML per task under <out_dir>/tasks/ (stale XML files for tasks no
    longer in the manifest are removed, so a removed task cannot be resurrected by the
    registration script). The `_task_xml` map never enters the JSON."""
    out_dir.mkdir(parents=True, exist_ok=True)
    task_xml = m.pop("_task_xml", {}) or {}
    jp, mp = out_dir / MANIFEST_JSON, out_dir / MANIFEST_MD
    jp.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    mp.write_text(render_markdown(m), encoding="utf-8", newline="\n")
    written = [jp, mp]
    if any(task_xml.values()):
        xdir = out_dir / TASK_XML_SUBDIR
        xdir.mkdir(parents=True, exist_ok=True)
        keep: set[str] = set()
        for slug, xml_text in sorted(task_xml.items()):
            if not xml_text:
                continue
            body = re.sub(r"^\s*<\?xml[^>]*\?>", '<?xml version="1.0" encoding="UTF-8"?>', xml_text.lstrip("﻿"), count=1).strip()
            xp = xdir / f"{slug}.xml"
            xp.write_text(body + "\n", encoding="utf-8", newline="\n")
            written.append(xp)
            keep.add(xp.name)
        for stale in xdir.glob("*.xml"):
            if stale.name not in keep:
                stale.unlink()
    m["_task_xml"] = task_xml      # leave the in-memory manifest as it was handed in
    return written


# ── CLI ────────────────────────────────────────────────────────────────────────
def _load_fixture(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "tasks" in data:
        data = data["tasks"]
    return [d for d in data if isinstance(d, dict)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the Cora task-estate manifest from the live registry (read-only).")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--diff-only", action="store_true", help="print drift vs the committed manifest; write nothing")
    ap.add_argument("--previous", type=Path, default=None, help="diff against THIS manifest instead of the committed one")
    ap.add_argument("--update-docs", action="store_true", help="regenerate the GENERATED blocks in bootstrap-new-machine.md + runbook.md")
    ap.add_argument("--fixture", type=Path, default=None, help="raw task JSON instead of the host (tests / non-Windows)")
    ap.add_argument("--json", action="store_true", help="print the manifest JSON to stdout")
    ap.add_argument("--live-root", type=Path, default=None,
                    help="the LIVE checkout whose logs/ to read (run markers + per-task logs) when this "
                         "script runs from a git worktree that has no logs/ of its own")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO, format="%(levelname)s %(message)s")

    if args.fixture and args.update_docs:
        print("REFUSED: --update-docs with --fixture would rewrite the REAL bootstrap/runbook blocks with fixture data.")
        return 2

    log_dir = TASK_LOG_DIR
    marker_path: Path | None = None
    if args.live_root:
        global LIVE_ROOT
        LIVE_ROOT = args.live_root
        log_dir = args.live_root / "logs" / "tasks"
        marker_path = args.live_root / "logs" / "task-runs.jsonl"   # explicit; an ambient env var must not win

    try:
        raw = _load_fixture(args.fixture) if args.fixture else enumerate_cim()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: enumeration failed: {exc}")
        return 2
    tasks = normalize_all(raw, log_dir=log_dir, marker_path=marker_path)
    names = {t["name"] for t in tasks}

    agreement: dict[str, Any] = {"cim_count": len(names), "agree": True, "methods": {}}
    cowork: dict[str, Any] = {"available": False, "reason": "fixture run"}
    if not args.fixture:
        try:
            csv_names: set[str] | None = enumerate_schtasks_csv()
        except Exception as exc:  # noqa: BLE001
            log.info("schtasks CSV unavailable: %s", exc)
            csv_names = None
        agreement = enumeration_agreement(names, csv_names, enumerate_health_view())
        cowork = cowork_estate_crossref(pin_task_state())

    manifest = build_manifest(tasks, agreement=agreement, cowork=cowork, log_dir=log_dir,
                              host_time_zone=("" if args.fixture else host_time_zone()))

    prev_path = args.previous or (args.out_dir / MANIFEST_JSON)
    prev = load_manifest(prev_path)
    if prev is None:
        print(f"no previous manifest at {prev_path} -- first run, nothing to diff")
        lines: list[str] = []
    else:
        lines = warn_lines(diff_manifests(prev["tasks"], tasks))
        for line in lines:
            print("WARN " + line)
        if not lines:
            print(f"no task-estate drift vs {prev_path.name} ({prev.get('count')} -> {len(tasks)} tasks)")

    if not agreement.get("agree", True):
        print("WARN enumeration methods DISAGREE: " + json.dumps(agreement["methods"]))
    rm = manifest["run_marker_coverage"]
    print(f"tasks={len(tasks)} enabled={manifest['enabled_count']} run_markers={rm['tasks_with_marker']}/{rm['tasks_total']} "
          f"log_prefixes={rm['per_task_log_prefixes']} no_setup_script={len(manifest['without_setup_script'])} "
          f"intent_drift={len(manifest['intent_drift'])}")

    if args.json:
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
    if args.diff_only:
        return 0
    for p in write_outputs(manifest, args.out_dir):
        print(f"wrote {p}")
    if args.update_docs:
        changed = update_docs(manifest)
        print("docs updated: " + (", ".join(changed) if changed else "already current (idempotent)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
