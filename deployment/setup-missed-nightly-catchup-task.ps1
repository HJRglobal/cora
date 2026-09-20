# Setup Windows Scheduled Task: "Cora - Missed Nightly Catch-Up" (Code #13 slice 2,
# cq-fb50c9e6c911). Runs scripts/check_missed_nightly.py --apply once a day at
# 08:30 AZ: for every task in data/maps/nightly-catchup-set.yaml with NO fire
# evidence for today's window (run marker / run_hidden header / scheduler
# LastRunTime) it replays the task ONCE, in trigger order, through run_hidden,
# and writes its own ledger (logs/nightly-catchup.jsonl) + a run marker. The
# 08:45 health check READS that ledger; it never runs the replay itself (it sits
# inside run_hidden's kill-on-close job, so a detached child would die with it).
#
# WHY 08:30: after every nightly deadline (the last trigger, Drive Sweep 06:00,
# plus the 150-minute grace = 08:30) and 15 minutes before the health check.
# WHY 4h: gmail (3h) + Drive Sweep (registered unlimited; bounded here to 3h) are
# replayed synchronously; the lane also refuses to START anything after 12:00 AZ.
#
# WINDOWLESS by construction (D-266..D-269) via the shared run_hidden helper.
# ASCII-only per D-016. D-005: absolute .venv python. RunLevel Limited.
#
# Registering a task = re-run deployment\pin-scheduled-task-models.ps1 -Apply
# afterwards (Code #13 kickoff section 3 guardrail).
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-missed-nightly-catchup-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'Cora - Missed Nightly Catch-Up' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "Cora - Missed Nightly Catch-Up"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\check_missed_nightly.py"
$FireAt     = "08:30"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at $PythonExe. Check the venv."
    exit 1
}
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found at $ScriptPath."
    exit 1
}

Write-Host "Setting up scheduled task: $TaskName" -ForegroundColor Cyan

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Removing existing task..." -ForegroundColor Yellow
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Windowless action via the shared helper.
. "$PSScriptRoot\_task-action.ps1"
$action = New-WrappedTaskAction -TaskName $TaskName `
    -Execute $PythonExe `
    -Argument "`"$ScriptPath`" --apply" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $FireAt

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Missed-start catch-up for the nightly ingest set: replays any nightly task with no fire evidence for today's window, once, in trigger order, via run_hidden. Deterministic, no LLM. Ledger logs/nightly-catchup.jsonl; the 08:45 health check reads it. T2 act-with-audit pending Harrison's tier confirm (Code #13)." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

$after = Get-ScheduledTask -TaskName $TaskName
Write-Host "  Registered: $TaskName (daily $FireAt AZ, limit 4h)" -ForegroundColor Green
Write-Host ""
Write-Host "Dry-run first (safe, writes nothing):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath'"
Write-Host "Replay the 9/9 outage as a dry-run:" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath' --day 2026-09-09 --no-scheduler"
Write-Host "Then re-pin task models:" -ForegroundColor Cyan
Write-Host "  .\deployment\pin-scheduled-task-models.ps1 -Apply"
Write-Host "Watch log:" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  $RepoRoot\logs\tasks\Cora-Missed-Nightly-Catch-Up-$today.log"
