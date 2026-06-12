# Setup Windows Scheduled Task: Cora strategy memo, weekly Sunday 18:30 AZ.
#
# Org Synthesis Phase 4 -- gathers the cross-entity fact base (cash, pipeline,
# stalled decisions, deadline radar, efficiency findings, KB momentum, health),
# computes week-over-week deltas from snapshots, synthesizes a strategy memo
# with Sonnet (fail-closed), DMs Harrison ONLY, and files a copy to
# 00-Founder/_strategy-memos/ for the nightly static_md KB ingest.
#
# Slot check (2026-06-11): Sunday 18:30 is free -- Friction Mining is Sun
# 17:30 (1h exec limit), Channel Health Monitor Sun 4:00 AM, nothing else
# Sunday PM; outside the 03:00-09:00 heavy window. Running an hour after
# friction mining means the memo sees that run's pending findings.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-strategy-memo-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'Cora - Strategy Memo' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "Cora - Strategy Memo"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_strategy_memo.py"
$HourMin    = "18:30"

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

# D-005: absolute .venv python + absolute script path + WorkingDirectory
$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $HourMin

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Cora strategy memo: weekly founder portfolio synthesis (cash, pipeline, decisions, deadlines, efficiency); Sonnet fail-closed; Harrison-only DM + memo file (Org Synthesis Phase 4)" `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (weekly Sunday at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
Write-Host ""
Write-Host "Dry-run test (safe, no snapshot/file/DM writes):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath' --dry-run"
Write-Host ""
Write-Host "Watch log:" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  $RepoRoot\logs\strategy-memo-$today.log"
