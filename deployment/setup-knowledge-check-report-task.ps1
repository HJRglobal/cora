# Setup Windows Scheduled Task: Cora weekly knowledge-check participation report.
#
# DMs HANNAH the per-person knowledge-check participation breakdown (asked /
# answered / confirmed / skipped / no-response / no-confirm / pool-exhausted) for
# the last 7 days. Spec of record: the 2026-08-11 addendum Harrison locked --
# "DMs Hannah directly (not a channel post)", Monday morning, ahead of the MWF
# training-readiness audit she runs. She owns training readiness; this is the
# surface that audit reads from.
#
# Schedule: weekly, Monday 07:20 AZ. Chosen deliberately:
#   * BEFORE the MWF 7:30a training-readiness audit it feeds.
#   * A distinct minute -- the weekly health metric alarms when two or more tasks
#     share the SAME clock time inside 03:00-09:00. Shift it if that ever flags.
#   * The ask run is 08:05 and is a DIFFERENT SCRIPT. Nothing here can ask
#     anyone anything; see run_knowledge_check_report.py's docstring for why that
#     separation exists rather than a --send flag on the runner.
#
# ASCII-only (D-016): PowerShell 5.1 reads UTF-8 as Windows-1252.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-knowledge-check-report-task.ps1
#
# Preview without sending anything:
#     .venv\Scripts\python.exe scripts\run_knowledge_check_report.py --dry-run
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-knowledge-check-report' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-knowledge-check-report"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_knowledge_check_report.py"
$HourMin    = "07:20"

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

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $HourMin

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Cora weekly knowledge-check participation report: per-person asked/answered/confirmed/skipped/no-response over 7d. DM to Hannah, ahead of the Monday training-readiness audit." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (runs Monday at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
