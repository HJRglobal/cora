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
# Schedule: Monday 09:00 AZ -- after the weekly cash flow refresh, and outside the
# crowded 03:00-09:00 sync window that the weekly health metric alarms on.
# Slot check (2026-08-04): the finance receipt digest is Mon 10:30 and the finance
# weekly recap is Mon 07:30, so 09:00 collides with neither.
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

# D-005: absolute .venv python + absolute script path + WorkingDirectory = repo root.
# Never uv in a scheduled task.
$Action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument "scripts\run_finance_close_pack.py" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $HourMin

# 55 QBO report calls at up to 30s each is the worst case, so the limit is generous.
# The script has no internal wall-clock budget; this is the only backstop.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

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
