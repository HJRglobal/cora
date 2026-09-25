"""Code #16 C1 -- the monthly card's setup PS1. Static assertions (ASCII-only per D-016,
the windowless action, the trigger, the -Register gate) + a syntax parse. The script
touches the live task estate, so its mutating path is never executed here.

DELIBERATELY NOT COPIED from test_hygiene_drive_schedule_ps1.py: its "not registered
yet / not in the generated block" asserts, which the task's own registration turned
red on main (the inherited red Code #16 flipped first). The live-estate belt below
excludes this task's own row, so registering it can never redden this file.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SETUP = REPO / "deployment" / "setup-channel-archive-proposal-task.ps1"
TASK = "cowork-cora-channel-archive-proposal"
FIRE_AT = "07:07"


def _code_only(path: Path) -> str:
    text = path.read_text(encoding="ascii")
    text = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def test_ascii_only():
    assert all(b < 128 for b in SETUP.read_bytes()), "D-016: PS1 files are ASCII-only"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell parser")
def test_parses():
    ps = (f"$ErrorActionPreference='Stop'; "
          f"$null = [System.Management.Automation.PSParser]::Tokenize("
          f"(Get-Content -Raw '{SETUP}'), [ref]$null); 'OK'")
    proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                          capture_output=True, text=True, timeout=120,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0 and "OK" in proc.stdout, f"{proc.stdout}\n{proc.stderr}"


def test_one_weekly_monday_trigger_windowless_apply_monthly():
    code = _code_only(SETUP)
    assert f'$TaskName   = "{TASK}"' in code
    assert f'New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "{FIRE_AT}"' in code
    assert f'$HourMin    = "{FIRE_AT}"' in code
    assert "_task-action.ps1" in code and "New-WrappedTaskAction" in code
    assert "New-ScheduledTaskAction" not in code
    assert "run_channel_archive_proposal.py" in code and "--apply --monthly" in code
    assert "-RunLevel Limited" in code and "-LogonType Interactive" in code
    assert "-StartWhenAvailable" in code and "New-TimeSpan -Minutes 30" in code
    assert r"C:\Users\Harri\code\cora\.venv\Scripts\python.exe" in code          # D-005
    assert "$after.Triggers.Count" in code and "StartBoundary" in code        # read-back


def test_nothing_registers_or_unregisters_without_the_register_switch():
    code = _code_only(SETUP)
    assert re.search(r"param\(\s*\[switch\]\$Register\s*\)", code)
    gate = code.index("if (-not $Register)")
    exit0 = code.index("exit 0", gate)
    for verb in ("Register-ScheduledTask", "Unregister-ScheduledTask", "Stop-ScheduledTask"):
        first = code.index(verb)
        assert first > exit0, f"{verb} is reachable without -Register"


def test_the_slot_guard_reads_the_live_registry_before_anything():
    code = _code_only(SETUP)
    assert code.index("Get-ScheduledTask | Where-Object") < code.index("if (-not $Register)")
    assert "$collisions.Count -gt 0" in code


def test_the_fire_minute_is_unclaimed_by_every_other_script_and_the_live_estate():
    claimed = {}
    for p in (REPO / "deployment").glob("*.ps1"):
        if p == SETUP:
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        found = set(re.findall(r"-At\s+\"?'?(\d\d:\d\d)", src))
        found |= set(re.findall(r'\$(?:FireAt|HourMin)\s*=\s*"(\d\d:\d\d)"', src))
        found |= set(re.findall(r"New\s*=\s*'(\d\d:\d\d)'", src))
        found |= set(re.findall(r"/ST\s+(\d\d:\d\d)", src))
        if FIRE_AT in found:
            claimed[p.name] = found
    assert not claimed, f"{FIRE_AT} is claimed by {sorted(claimed)}"
    estate = (REPO / "deployment" / "manifest" / "task-estate.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| `([^`]+)` \| ([^|]+) \|", estate, flags=re.M)
    assert rows, "the committed live-registry manifest must parse, or this belt is vacuous"
    others = [(name, trig) for name, trig in rows if name != TASK]
    assert not any(FIRE_AT in trig for _, trig in others), "another live task already fires at 07:07"


def test_the_run_marker_row_and_enabled_row_exist():
    import yaml
    data = yaml.safe_load((REPO / "data" / "maps" / "scheduled-task-state.yaml").read_text(encoding="utf-8"))
    rows = [r for r in data.get("run_markers") or [] if r.get("name") == TASK]
    assert len(rows) == 1 and rows[0]["cadence_hours"] == 168 and rows[0]["expects_output"] is False
    assert rows[0].get("registered")
    assert TASK in (data.get("enabled") or []) and TASK not in (data.get("disabled") or [])


def test_the_runbook_entry_lives_outside_the_generated_block():
    rb = (REPO / "deployment" / "runbook.md").read_text(encoding="utf-8")
    head, rest = rb.split("<!-- BEGIN GENERATED: task-registry -->", 1)
    _gen, tail = rest.split("<!-- END GENERATED: task-registry -->", 1)
    assert "setup-channel-archive-proposal-task.ps1 -Register" in head + tail
    assert TASK in head + tail and "--clear-demotion --apply" not in _gen


def test_the_ladder_row_names_this_script_and_no_other_tasks():
    from cora import ladder_registry as lr
    reg = lr.load()
    assert lr.validate(reg) == []
    row = lr.row_for("slack-channel-archive", reg)
    em = row["evidence_monitor"]
    text = " ".join(str(x) for x in (row["title"], em["description"], row["audit_surface"],
                                     row["promotion_criteria"])).lower()
    assert "run_channel_archive_proposal.py" in text
    estate = (REPO / "deployment" / "manifest" / "task-estate.md").read_text(encoding="utf-8")
    scripts = set(re.findall(r"-> `scripts/([a-z0-9_]+\.py)`", estate))
    names = set(re.findall(r"^\| `([^`]+)` \|", estate, flags=re.M))
    for s in scripts - {"run_channel_archive_proposal.py"}:
        assert s not in text, f"the row names another task's script {s} (manifest heuristic)"
    for n in names - {TASK}:
        assert n.lower() not in text, f"the row names another task {n}"
