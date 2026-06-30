# Setup Windows Scheduled Task: Cora LEX swept PHI re-scan, daily 07:06 AZ.
#
# Defense-in-depth net behind the materializer's inline _phi_wall: re-reads every
# _brain/swept/**/*.md written in the last ~26h and re-runs the SAME PHI detectors;
# on a hit it quarantines the file (rename -> *.QUARANTINED.md, stays KB-excluded) +
# alerts Harrison (entity/date/detector only, never the PHI text) + audit-logs.
#
# Slot: 07:06 AZ runs AFTER the 05:45 "Cora - Drive Materialization" writes the day's
# swept files, on a free minute (scheduler de-collide doctrine).
#
# SCRIPT-SIDE: the bot does NOT need a restart (this is a standalone scheduled script).
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-lex-swept-phi-check-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'Cora - LEX Swept PHI Check' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "Cora - LEX Swept PHI Check"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_lex_swept_phi_check.py"
$HourMin    = "07:06"

if (-not (Test-Path $PythonExe))  { Write-Error "Python not found at $PythonExe."; exit 1 }
if (-not (Test-Path $ScriptPath)) { Write-Error "Script not found at $ScriptPath."; exit 1 }

Write-Host "Setting up scheduled task: $TaskName" -ForegroundColor Cyan

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Removing existing task..." -ForegroundColor Yellow
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# D-005: absolute .venv python + absolute script path + WorkingDirectory.
# --all re-scans the whole (small: ~1 file/entity/day) swept tree so a >26h missed run
# (machine asleep / skipped fire) never leaves a written file unscanned. Quarantined
# files are skipped; clean files re-scan as cheap no-ops.
$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$ScriptPath`" --all" `
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
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Cora LEX swept PHI re-scan: daily defense-in-depth pass over _brain/swept/ behind the materializer's inline _phi_wall (quarantine + alert + audit on a hit)" `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (daily at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
Write-Host ""
Write-Host "Dry-run test (safe -- scan + report, no quarantine, no alert):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath' --dry-run --all"
