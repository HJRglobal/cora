# Setup Windows Scheduled Task: Cora daily meeting-capture audit (cq-ffcf6e4ffe7c).
#
# Diffs yesterday's roster calendar events against Fireflies transcripts and posts
# misses / duplicates / unexpected captures to #founder-operations.
#
# WHY THIS IS LOAD-BEARING. Under the One Cora Notetaker architecture there is one
# capture seat and no per-seat fallback, so a meeting the ensure lane misses is
# captured by NOTHING. This restores the capture-gap slice of a Cowork-side sweep
# that went dark 2026-07-24; its absence is why duplicate captures ran unnoticed
# for three months.
#
# READ-ONLY. Calendar list + Fireflies query + one Slack post. It never writes a
# calendar and never touches the KB. Deterministic -- no model call anywhere.
#
# Schedule: daily 07:22 AZ.
# Slot check against the LIVE registry (2026-08-27): 07:22 is unused. Neighbours
# are cowork-cora-knowledge-check-report 07:20 and "Cora - Daily Briefing" 07:30.
# It sits inside the 03:00-09:00 window the weekly health metric watches, but it is
# unique there, so it adds no new clock collision.
# It runs well after the 03:30 Fireflies KB sync, though it does not depend on it --
# the audit queries the Fireflies API directly, not the KB.
#
# Exit codes: 0 = ran (with or without findings), 1 = could not run (bad roster,
# bad --day, or a failed Slack post). Task Scheduler surfaces these as Last Result.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-meeting-capture-audit-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-meeting-capture-audit' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-meeting-capture-audit"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_meeting_capture_audit.py"
$HourMin    = "07:22"

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
$Action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument "scripts\run_meeting_capture_audit.py --post" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -Daily -At $HourMin

# One calendar read per roster member (9 today) plus a paginated Fireflies query.
# Measured at roughly 25 seconds end to end on 2026-08-27; 15 minutes is a generous
# backstop that still bounds a hung calendar call.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Daily meeting-capture audit: yesterday's roster calendar events vs Fireflies transcripts -> misses, duplicate captures, and captures with no matching calendar event, posted to #founder-operations. Read-only and deterministic. LEX meeting titles are never rendered." `
    | Out-Null

Write-Host "  Registered: $TaskName (daily $HourMin)" -ForegroundColor Green

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "State:    $($task.State)"
Write-Host "Next run: $($info.NextRunTime)"
Write-Host ""
Write-Host "Preview any time (non-elevated is fine, posts nothing):" -ForegroundColor Cyan
Write-Host "  .venv\Scripts\python.exe scripts\run_meeting_capture_audit.py --day 2026-08-26"
