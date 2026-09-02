# Setup Windows Scheduled Task: Cora weekly finance close-support pack.
#
# Builds the deterministic close-support pack (QBO across the 11 provisioned
# realms cross-checked against the Standing ACTUALS cash sheet, AR/AP aging
# week-over-week, P&L month-over-month sanity, close-prep notes, renewal radar)
# and delivers three cuts:
#     FULL pack   -> #hjrg-finance    (C0B3V5SDNAG)
#     FULL pack   -> DM Justin Moran  (U0B3AEJCYGP)
#     FOUNDER cut -> #founder-finance (C0BCXPJDP42)
#
# NOTE on the target: #hjr-finance (C0BAK65N4TA) is ARCHIVED as of 2026-08-04, so
# a post there fails is_archived and reaches nobody. #hjrg-finance is live and
# classifies TIER_1 (function "finance"), so the finance firewall is preserved.
#
# Schedule: Monday 09:00 AZ -- after the weekly cash flow refresh, and just outside
# the crowded 03:00-09:00 sync window the weekly health metric alarms on (the
# detector window is 3 <= hour < 9, so 09:00 is outside it entirely).
# Slot check against the LIVE registry (2026-08-04): no other Monday-weekly task
# fires at 09:00. Neighbours are cowork-cora-finance-adherence 08:15 (this bundle),
# Cora - KB Evals 09:05, cowork-cora-finance-receipt-digest Mon 10:30, and
# cowork-cora-finance-weekly Mon 14:30.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-finance-close-pack-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-finance-close-pack' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-finance-close-pack"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_finance_close_pack.py"
$HourMin    = "09:00"

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

# D-005: absolute .venv python; script path is relative to WorkingDirectory, which
# is pinned to the repo root. Never uv in a scheduled task.
# Windowless action: route the command through run_hidden.py under pythonw.exe
. "$PSScriptRoot\_task-action.ps1"
$Action = New-WrappedTaskAction -TaskName $TaskName -Execute $PythonExe `
    -Argument "scripts\run_finance_close_pack.py" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $HourMin

# Limit sized for the real TAIL, not the measured case. Measured: 1-3 min. Tail:
# 54 QBO report GETs at a 30s client timeout, each able to retry once on a 401
# (~54 min), plus ~10 OAuth token refreshes at 30s (most access tokens have expired
# by Monday morning), plus 10 Sheets values.get bounded at the httplib2 default 60s
# (~10 min) => ~69 min, which would have been killed by a 1-hour limit mid-delivery.
# Per-target dedup makes a kill recoverable, but the limit should not be the thing
# that causes one.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Weekly finance close-support pack: QBO vs Standing ACTUALS cross-check, AR/AP aging WoW, P&L MoM sanity, close-prep notes, renewal radar. Delivers to #hjrg-finance + DM Justin + #founder-finance founder cut. Deterministic (script computes; no model figures)." `
    | Out-Null

Write-Host "  Registered: $TaskName (Monday $HourMin)" -ForegroundColor Green

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "State:    $($task.State)"
Write-Host "Next run: $($info.NextRunTime)"
Write-Host ""
Write-Host "Preview before the first live fire (non-elevated is fine):" -ForegroundColor Cyan
Write-Host "  .venv\Scripts\python.exe scripts\run_finance_close_pack.py --dry-run"
