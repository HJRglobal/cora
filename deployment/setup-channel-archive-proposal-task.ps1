# Setup Windows Scheduled Task: cowork-cora-channel-archive-proposal (Code #16 C1,
# cq-be90cea867c3, ladder row slack-channel-archive, born T0).
#
# Runs scripts/run_channel_archive_proposal.py --apply --monthly once a week. The
# script itself decides whether this month's card is due: it delivers inside the
# month's first-Monday week (AZ) until a non-blind, fully delivered monthly card went
# out this calendar month (and on every later Monday of a month whose monthly attempt
# failed, until a full card lands), otherwise it skips. A weekly trigger + that
# self-gate (rather than a schtasks /MO FIRST trigger, which this repo has never
# exercised and which registers with StartWhenAvailable=false) means a first Monday
# the host was off is caught up the next time it is on that week; a registration late
# in a month waits for the next first Monday. NOTHING is archived by this task:
# archiving happens only on Harrison's tap in the bot, and only after the lane is
# promoted.
#
# WHEN: Monday 07:07 AZ. 07:05 is cowork-cora-bank-snapshot's daily slot; 07:07 was
# unclaimed in the live-registry manifest (deployment\manifest\task-estate.md,
# 2026-09-25) and is re-checked against the LIVE registry below before registering.
# It fires after the 07:00 knowledge-review run that sends the Monday menu.
#
# WINDOWLESS by construction (D-266..D-269): registered through the shared
# run_hidden helper. ASCII-only per D-016. D-005: absolute .venv python.
# Script-side: no restart for script changes -- BUT the card's buttons are handled by
# the BOT, so register only AFTER the restart that loads the Code #16 lane.
#
# HARRISON REGISTERS THIS (never a Code session). Without -Register this script only
# checks the slot and prints what it would do; it changes nothing. Dry-run first, from
# a normal (non-elevated) PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\.venv\Scripts\python.exe scripts\run_channel_archive_proposal.py
# then register from elevated PowerShell:
#     .\deployment\setup-channel-archive-proposal-task.ps1 -Register
# and regenerate the DR task-estate manifest afterwards (elevated; the Monday digest
# shows the new task as drift until then):
#     .\.venv\Scripts\python.exe scripts\generate_task_estate_manifest.py --update-docs
#     .\.venv\Scripts\python.exe scripts\dr_manifest_probes.py --update-docs
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-channel-archive-proposal' -Confirm:$false

param(
    [switch]$Register
)

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-channel-archive-proposal"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_channel_archive_proposal.py"
$HourMin    = "07:07"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at $PythonExe. Check the venv."
    exit 1
}
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found at $ScriptPath."
    exit 1
}

Write-Host "Scheduled task: $TaskName (weekly Monday $HourMin AZ, --apply --monthly)" -ForegroundColor Cyan

# Guard the slot against the LIVE registry (read-only): refuse to stack onto a minute
# another Cora task already owns. Checks every trigger's StartBoundary, not just
# NextRunTime (which reports only the next occurrence and is null when disabled).
$collisions = @()
foreach ($t in (Get-ScheduledTask | Where-Object { $_.TaskName -like "*cora*" -and $_.TaskName -ne $TaskName })) {
    $hit = $false
    foreach ($trig in $t.Triggers) {
        if ($trig.StartBoundary) {
            try {
                if (([datetime]$trig.StartBoundary).ToString("HH:mm") -eq $HourMin) { $hit = $true }
            } catch {}
        }
    }
    $nextRun = (Get-ScheduledTaskInfo -TaskName $t.TaskName -ErrorAction SilentlyContinue).NextRunTime
    if ($nextRun -and $nextRun.ToString("HH:mm") -eq $HourMin) { $hit = $true }
    if ($hit) { $collisions += $t.TaskName }
}
if ($collisions.Count -gt 0) {
    Write-Error "Slot $HourMin is already used by: $($collisions -join ', '). Pick a free minute and update `$HourMin."
    exit 1
}
Write-Host "  Slot $HourMin is free in the live registry." -ForegroundColor Green

if (-not $Register) {
    Write-Host ""
    Write-Host "Check only -- nothing was registered or changed." -ForegroundColor Yellow
    Write-Host "Registration is Harrison's act, AFTER the restart that loads the lane:"
    Write-Host "  .\deployment\setup-channel-archive-proposal-task.ps1 -Register"
    exit 0
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Removing existing task..." -ForegroundColor Yellow
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Windowless action via the shared helper (the --apply --monthly flags MUST be in the
# action, or every fire is a silent dry run).
. "$PSScriptRoot\_task-action.ps1"
$action = New-WrappedTaskAction -TaskName $TaskName `
    -Execute $PythonExe `
    -Argument "`"$ScriptPath`" --apply --monthly" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "07:07"

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# The scan paces its Slack reads (>= 2 s per history call), ~5 minutes over ~130
# channels; 30 minutes bounds a hung read without killing a slow-but-working run.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Monthly dead-channel proposal card to Harrison's DM (weekly fire, first-Monday self-gate). Metadata-only scan; nothing is archived by this task. Code #16 C1, ladder row slack-channel-archive (T0)." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

$after = Get-ScheduledTask -TaskName $TaskName
Write-Host "  Registered: $TaskName ($($after.Triggers.Count) trigger: Monday $HourMin AZ)" -ForegroundColor Green
if ($after.Triggers.Count -ne 1) {
    Write-Host "  WARNING: expected 1 trigger, found $($after.Triggers.Count)" -ForegroundColor Red
}
foreach ($trig in $after.Triggers) {
    $at = ""
    try { $at = ([datetime]$trig.StartBoundary).ToString("HH:mm") } catch {}
    if ($at -ne $HourMin) {
        Write-Host "  WARNING: trigger reads back at '$at', expected $HourMin" -ForegroundColor Red
    }
}
Write-Host ""
Write-Host "Dry-run (safe, writes nothing):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath'"
Write-Host "Watch log:" -ForegroundColor Cyan
Write-Host "  $RepoRoot\logs\tasks\cowork-cora-channel-archive-proposal-<YYYY-MM-DD>.log"
