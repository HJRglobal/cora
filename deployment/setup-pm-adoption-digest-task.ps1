# Setup Windows Scheduled Task: Cora weekly PM-adoption digest.
#
# Reads logs/pm-actions.jsonl (Cora-attributed task actions) + enumerates Asana task
# state across the roster, then DMs Harrison a weekly adoption digest: tasks
# created/completed via Cora vs directly in Asana, overdue WoW trend, staleness, and
# per-person engagement. This digest IS the Phase-2 go/no-go instrument.
#
# Schedule: weekly, Monday 08:20 AZ. Distinct minute (the weekly health metric only
# alarms on two-or-more tasks sharing the SAME clock time in 03:00-09:00); shift it if
# the health monitor ever flags a collision.
#
# Delivery defaults to Harrison DM only. To also post to #founder-operations, add
# --also-channel to the -Argument line below and re-run this script.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-pm-adoption-digest-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-pm-adoption-digest' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-pm-adoption-digest"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_pm_adoption_digest.py"
$HourMin    = "08:20"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at $PythonExe. Check the venv."
    exit 1
}
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found at $ScriptPath."
    exit 1
}

Write-Host "Setting up scheduled task: $TaskName" -ForegroundColor Cyan

# Remove existing task if present (stop first so a running instance dies)
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
    -Description "Cora weekly PM-adoption digest: Cora-vs-UI task activity, overdue WoW, staleness, per-person engagement. DM to Harrison; the Phase-2 go/no-go instrument." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (runs Monday at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
Write-Host ""
Write-Host "Dry-run test (safe, writes no state, posts nothing):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath' --dry-run"
Write-Host ""
Write-Host "Watch log:" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  $RepoRoot\logs\pm-adoption-digest-$today.log"
