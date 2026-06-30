# Setup Windows Scheduled Task: Cora per-person involvement-dossier refresh,
# weekly Sunday 16:30 AZ (North Star pillar 4).
#
# Iterates the roster and runs pull -> PHI-scrub -> Sonnet-synthesize -> write-back
# of each teammate's _brain/people/{slug}.md "Recent involvements" section. The
# script SELF-BOUNDS its time budget (the briefing-task lesson: a task
# ExecutionTimeLimit SIGKILLs the wrapper, not the python child -- the script's own
# budget is the real control), so the outer limit below is a generous backstop.
#
# Slot check: Sun 16:30 AZ is ahead of Friction Mining (17:30) and Strategy Memo
# (18:30), and outside the 03:00-09:00 morning stagger window.
#
# SCRIPT-SIDE: the bot does NOT need a restart to pick up edits to the refresh
# script (it spawns a fresh process). Registering this task does not touch the bot.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-person-dossier-refresh-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-person-dossier-refresh' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-person-dossier-refresh"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_person_dossier_refresh.py"
$HourMin    = "16:30"

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
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Cora per-person involvement-dossier refresh: weekly roster pull -> PHI-scrub -> synthesize -> write _brain/people/{slug}.md (North Star pillar 4)" `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (weekly Sunday at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
Write-Host ""
Write-Host "Dry-run test (safe -- synthesizes but writes NOTHING):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath' --dry-run --only tommy-anderson"
