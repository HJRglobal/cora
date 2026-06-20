# Setup Windows Scheduled Task: Cora cash-flow snapshot writer (WS7), daily 06:40 AZ.
#
# Does ONE read of the CF_SUMMARY tab and writes a labeled JSON snapshot to
# 00-Founder/_cash-snapshot/cashflow-latest.json on the Google-Drive mount, so the
# Cowork daily-morning-brief SKILL can read portfolio cash (its own QBO connector
# is F3-Energy-Holdings-only). Fail-soft: a read error leaves the previous snapshot
# in place and exits non-zero. NOT bot-loaded -- runs as its own task; no restart.
#
# Slot: 06:40 AZ daily, AHEAD of the morning briefs (~07:00/07:30) so the snapshot
# is fresh when they run. Confirm 06:40 does not collide with another enabled task
# in the 03:00-09:00 window (scheduler de-collide doctrine, B1 2026-06-13); adjust
# -HourMin if needed.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-cashflow-snapshot-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'Cora - Cash Snapshot' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "Cora - Cash Snapshot"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\write_cashflow_snapshot.py"
$HourMin    = "06:40"

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

$trigger = New-ScheduledTaskTrigger -Daily -At $HourMin

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
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Cora cash-flow snapshot: one CF_SUMMARY read -> labeled JSON at 00-Founder/_cash-snapshot/ for the Cowork morning brief (WS7). Fail-soft, source-opaque." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (daily at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
Write-Host ""
Write-Host "Dry-run test (safe, writes nothing):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath' --dry-run"
