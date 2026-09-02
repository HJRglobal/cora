# Setup Windows Scheduled Task: Cora meeting-ask capture (S3, cq-f52c6b691127).
#
# Polls Fireflies on a short interval and DMs a PROPOSE-ONLY card to whoever
# asked Cora for something out loud in a meeting ("Cora, make a task to ...").
# Nothing is created by this task. A card's Confirm button is what acts, and only
# the person the card is addressed to can tap it.
#
# THIS DOES NOT RE-ENABLE THE RETIRED PUSH. "Cora - Meeting Action Capture" stays
# Disabled -- it auto-CREATED Asana tasks from Fireflies' AI-generated action
# items and D-054 retired it after "Demi's 14 unwanted tasks". It is also recorded
# as intended-Disabled in data/maps/scheduled-task-state.yaml, so enabling it
# would raise a nightly health WARN. This is a different script that shares none
# of that code path.
#
# SCHEDULE: every 15 minutes, 07:08 to 20:08 AZ. Chosen deliberately:
#   * 15 minutes is what makes the kickoff's "within hours of the meeting" bar
#     comfortable. The daily 03:30 KB ingest would leave a 10am ask waiting ~17h.
#   * BUSINESS HOURS ONLY. It deliberately does not run 20:00-07:00: meetings are
#     not recorded then, and the 02:00-06:30 window is the heavy sync block.
#   * :08 / :23 / :38 / :53 collide with NO existing cora task clock time
#     (checked against the live registry 2026-08-25 -- the taken minutes in this
#     window are :00 :05 :06 :10 :15 :20 :25 :30 :31 :33 :35 :37 :39 :40 :45 :50
#     :52 :54 :56 :58). The weekly health metric alarms when two tasks share a
#     clock time inside 03:00-09:00; shift this if it ever flags.
#   * Each firing is cheap when nothing new landed -- one GraphQL call over a
#     recent window. The window OVERLAPS on purpose (see _window_start in the
#     script: Fireflies filters on MEETING DATE, so a transcript that lands an
#     hour after its meeting is only visible to a window that reaches back past
#     it). Re-reading a transcript is free because dedup is per ASK.
#
# ExecutionTimeLimit is 10 minutes: shorter than the 15-minute interval, so a
# wedged run can never overlap the next one.
#
# ASCII-only (D-016): PowerShell 5.1 reads UTF-8 as Windows-1252.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-meeting-ask-capture-task.ps1
#
# INSPECT FIRST -- this is the recommended order. Dry-run sends no DM and moves
# no watermark:
#     .venv\Scripts\python.exe scripts\run_meeting_ask_capture.py --dry-run --lookback-hours 168
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'Cora - Meeting Ask Capture' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "Cora - Meeting Ask Capture"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_meeting_ask_capture.py"
$StartAt    = "07:08"
$EveryMin   = 15
$ForHours   = 13

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
# Windowless action: route the command through run_hidden.py under pythonw.exe
. "$PSScriptRoot\_task-action.ps1"
$action = New-WrappedTaskAction -TaskName 'Cora - Meeting Ask Capture' `
    -Execute $PythonExe `
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $StartAt
$trigger.Repetition = (New-ScheduledTaskTrigger `
    -Once -At $StartAt `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMin) `
    -RepetitionDuration (New-TimeSpan -Hours $ForHours)).Repetition

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# MultipleInstances defaults to IgnoreNew, which is what we want here: a slow run
# must not stack with the next firing. Combined with the 10-minute limit below,
# an overlap is impossible.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "S3 (cq-f52c6b691127): scans new Fireflies transcripts for explicit in-meeting Cora asks and DMs the requester a PROPOSE-ONLY card. Creates nothing; a Confirm tap by the addressee is what acts. Does NOT re-enable the D-054-retired auto-create push." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName" -ForegroundColor Green
Write-Host "  Runs every $EveryMin min from $StartAt AZ for $ForHours hours." -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
Write-Host "  (schtasks /query /tn '$TaskName' /xml) -match 'Interval'"
