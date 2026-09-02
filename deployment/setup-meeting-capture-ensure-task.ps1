# Setup Windows Scheduled Task: Cora meeting-capture ENSURE lane (cq-ffcf6e4ffe7c).
#
# Sweeps roster calendars (T+0 and T+1) and puts every qualifying meeting on the
# capture identity's calendar (cora@hjrglobal.com) -- guest-add for in-domain
# organizers, event-copy (original Meet link) for external -- so its Fireflies
# seat auto-joins exactly once.
#
# TWO INDEPENDENT WRITE GATES (by design; either alone = read-only):
#   1. CORA_ONECORA_ENSURE=live in .env   (named enum; "1"/"true"/"on" read as OFF)
#   2. --apply on the invocation          (this task passes it)
# Registering this task BEFORE the .env flag flips is safe -- every fire is a
# dry-run -- but it still costs ~9 calendar reads per 15 minutes, so the intended
# order is: a few clean daily audits -> flip .env -> THEN run this setup script.
#
# Schedule: every 15 minutes, 06:07-20:07 AZ (meetings window; overnight sweeps
# add nothing -- the T+0/T+1 window means the 06:07 fire covers early meetings).
# 06:07 start chosen clear of the 06:05 info-for-cora sweep; sub-hourly repetition
# carries no fixed HH:MM string for the clock-collision alarm to stack on.
#
# Exit codes: 0 = ran, 1 = could not run. Ledger: only changes, failures, and
# consent-blocked rows are stored (plus one summary row per run).
#
# Run from elevated PowerShell (directly -- never as a nested `powershell -File`):
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-meeting-capture-ensure-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-meeting-capture-ensure' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-meeting-capture-ensure"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_meeting_capture_ensure.py"
$StartAt    = "06:07"

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

# D-005: absolute .venv python + relative script path + WorkingDirectory = repo root.
# Windowless action: route the command through run_hidden.py under pythonw.exe
. "$PSScriptRoot\_task-action.ps1"
$Action = New-WrappedTaskAction -TaskName $TaskName -Execute $PythonExe `
    -Argument "scripts\run_meeting_capture_ensure.py --apply" `
    -WorkingDirectory $RepoRoot

# Daily trigger with 15-minute repetition over a 14-hour window. PowerShell's
# -Daily trigger doesn't take -RepetitionInterval directly, so the repetition
# block is borrowed from a throwaway -Once trigger (standard pattern).
$Trigger = New-ScheduledTaskTrigger -Daily -At $StartAt
$rep = New-ScheduledTaskTrigger -Once -At $StartAt `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Hours 14)
$Trigger.Repetition = $rep.Repetition

# ExecutionTimeLimit UNDER the 15-minute cadence so a hung calendar call can
# never stack instances (the cq-7915a8647cff ALREADY_RUNNING trap class).
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Meeting-capture ensure lane: puts every qualifying roster meeting on cora@hjrglobal.com's calendar (guest-add in-domain, event-copy external) so the single Fireflies seat auto-joins exactly once. Dark until CORA_ONECORA_ENSURE=live in .env; --apply alone writes nothing. Carve-outs and roster: data/maps/meeting-capture-roster.yaml." `
    | Out-Null

Write-Host "  Registered: $TaskName (every 15 min, $StartAt + 14h daily)" -ForegroundColor Green

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "State:    $($task.State)"
Write-Host "Next run: $($info.NextRunTime)"
Write-Host ""
Write-Host "Dry-run preview any time (non-elevated, writes nothing):" -ForegroundColor Cyan
Write-Host "  .venv\Scripts\python.exe scripts\run_meeting_capture_ensure.py"
Write-Host "Reminder: the lane stays read-only until CORA_ONECORA_ENSURE=live is set in .env." -ForegroundColor Yellow
