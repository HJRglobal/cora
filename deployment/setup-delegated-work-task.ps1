# Registers the delegated-work runner task (cowork-cora-delegated-work).
# Run from ELEVATED PowerShell. ASCII-only (D-016). Absolute .venv python (D-005).
#
#   .\deployment\setup-delegated-work-task.ps1
#
# Every 15 minutes; the script self-bounds at --time-budget-min 12 and the
# ExecutionTimeLimit (PT30M) is only a backstop -- the task limit kills the
# cmd wrapper, not the python child, so script-side self-bounding is the real
# control (2026-06-12 briefing-task lesson).

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Script = Join-Path $RepoRoot "scripts\run_delegated_work_runner.py"
$TaskName = "cowork-cora-delegated-work"

if (-not (Test-Path $Python)) { throw "venv python not found: $Python" }
if (-not (Test-Path $Script)) { throw "runner script not found: $Script" }

# Windowless action: route the command through run_hidden.py under pythonw.exe
. "$PSScriptRoot\_task-action.ps1"
$Action = New-WrappedTaskAction -TaskName $TaskName -Execute $Python `
    -Argument "`"$Script`" --time-budget-min 12" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Set-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings | Out-Null
    Write-Host "Updated task $TaskName"
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings | Out-Null
    Write-Host "Registered task $TaskName (every 15 min)"
}

Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State
Write-Host "NOTE: the runner reads CORA_DELEGATED_WORK from .env fresh each fire."
Write-Host "off = claims nothing; log = claim + SIMULATED; live = full execution."
