"""S4 (2026-09-03, claude-workspace mirror): the two scheduling PowerShell
scripts. Static assertions (ASCII-only per D-016 + the safety invariants) plus a
syntax parse -- they touch the live task estate, so we do NOT execute their
mutating paths in the suite, mirroring test_run_hidden's approach."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SETUP = REPO / "deployment" / "setup-claude-mirror-task.ps1"
MIDDAY = REPO / "deployment" / "add-midday-sync-triggers.ps1"


def _code_only(path: Path) -> str:
    return "\n".join(
        ln for ln in path.read_text(encoding="ascii").splitlines()
        if not ln.lstrip().startswith("#")
    )


@pytest.mark.parametrize("path", [SETUP, MIDDAY])
def test_ascii_only(path):
    """D-016: PowerShell 5.1 reads UTF-8 as Windows-1252."""
    raw = path.read_bytes()
    assert all(b < 128 for b in raw), f"{path.name} must be ASCII-only"


@pytest.mark.skipif(os.name != "nt", reason="PowerShell parser")
@pytest.mark.parametrize("path", [SETUP, MIDDAY])
def test_parses(path):
    ps = (f"$ErrorActionPreference='Stop'; "
          f"$null = [System.Management.Automation.PSParser]::Tokenize("
          f"(Get-Content -Raw '{path}'), [ref]$null); 'OK'")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0 and "OK" in proc.stdout, f"{proc.stdout}\n{proc.stderr}"


def test_setup_registers_two_triggers_windowless_and_apply():
    code = _code_only(SETUP)
    assert "cowork-cora-claude-mirror" in code
    assert 'New-ScheduledTaskTrigger -Daily -At "03:45"' in code
    assert 'New-ScheduledTaskTrigger -Daily -At "12:15"' in code
    assert "@($trigger1, $trigger2)" in code, "must register BOTH triggers"
    # windowless: goes through the shared helper, not a bare console action
    assert "_task-action.ps1" in code and "New-WrappedTaskAction" in code
    assert "--apply" in code, "the scheduled steady state applies"
    # reads back the trigger count
    assert "$after.Triggers.Count" in code


def test_midday_is_dry_run_default_and_mutates_in_place():
    code = _code_only(MIDDAY)
    assert "[switch]$Apply" in code, "must be opt-in (dry-run default)"
    # in-place: exports XML backup BEFORE Set-ScheduledTask, does NOT re-register
    assert "Export-ScheduledTask" in code
    assert "Set-ScheduledTask" in code
    assert "Register-ScheduledTask" not in code, (
        "must NOT re-register (registry-drop class 1nnnn) -- mutate in place")
    # order: the backup export must precede the Set (rollback point exists first)
    assert code.index("Export-ScheduledTask") < code.index("Set-ScheduledTask -TaskName")
    # idempotency + read-back
    assert "already has" in code
    assert "read-back mismatch" in code
    # the two exact slots + preserving existing triggers
    assert "12:30" in code and "12:20" in code
    assert "@($task.Triggers) + $newTrigger" in code, "must preserve existing triggers"
    assert "cowork-cora-kb-sync-static" in code and "cowork-cora-session-capture" in code


def test_midday_backup_uses_unicode_encoding():
    """Export-ScheduledTask emits a UTF-16 prolog; writing UTF-8+BOM makes the
    bytes disagree and schtasks /Create /XML refuses it (rewrap doctrine)."""
    code = _code_only(MIDDAY)
    assert "-Encoding Unicode" in code
