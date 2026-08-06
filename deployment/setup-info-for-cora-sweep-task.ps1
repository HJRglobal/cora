# Setup Windows Scheduled Task: #info-for-cora reconciling intake sweep.
#
# Intake route 3 of 3. Channel 'message' events do not reach the Cora app, so
# the D1 event-driven intake has never fired. The @mention route covers posts
# that @-mention Cora; this sweep is the ONLY route that can see a post which
# generates no event at all -- notably the Cowork connector's un-@-mentioned
# posts (Harrison's 2026-07-10 F3 Pure pricing note is the reference case).
#
# 06:05 AZ daily: a free minute (06:00 already carries Drive Sweep +
# proactive-gaps) and BEFORE the 07:00 knowledge-review DM, so a contribution
# posted overnight rides the same morning review batch.
#
# Script-side: activates from the working tree at its next fire. NO Cora restart.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-info-for-cora-sweep-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-info-for-cora-sweep' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-info-for-cora-sweep"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_info_for_cora_sweep.py"
$HourMin    = "06:05"

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
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Cora #info-for-cora reconciling intake sweep: queue human contributions the event path cannot see (Harrison-gated)" `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (runs daily at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Review BEFORE the first live fire (safe -- writes nothing, posts nothing):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath' --dry-run --since-days 120"
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
