# disable-meeting-action-capture-2026-06-18.ps1
# -----------------------------------------------------------------------------
# Purpose: RETIRE the PUSH meeting-action-capture model. The hourly
# "Cora - Meeting Action Capture" task auto-created/auto-assigned Asana tasks
# from every meeting (the source of the "14 unwanted tasks / no delete tool"
# frustration). It is replaced by the PULL flow: a meeting attendee asks Cora in
# Slack for their action items and confirms which to create (the new
# meeting_action_items tool, registered in tool_dispatch).
#
# This script DISABLES the scheduled task. It does NOT delete the task or any
# code -- fireflies_action_extractor.py + its maps stay in place (they are the
# reuse source for the pull flow). Re-enable with:
#   Enable-ScheduledTask -TaskName "Cora - Meeting Action Capture"
#
# SAFETY:
#   - Idempotent: re-running on an already-disabled task is a no-op.
#   - Only toggles the enabled state; triggers/actions/principal untouched.
#   - Run from an ELEVATED PowerShell (the task was registered elevated).
#   - Does NOT touch the Fireflies KB ingest task (cowork-cora-kb-sync-fireflies)
#     -- recall of meeting content stays fully live.
#
# Pass -Status to only report the current state without changing it.
# ASCII-only per repo doctrine (no em-dashes / smart quotes).
# -----------------------------------------------------------------------------
param([switch]$Status)

$taskName = 'Cora - Meeting Action Capture'

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "Task '$taskName' not found on this host. Nothing to do."
    return
}

Write-Host "Task   : $taskName"
Write-Host "State  : $($task.State)"

if ($Status) {
    Write-Host "(-Status: reporting only, no change made.)"
    return
}

if ($task.State -eq 'Disabled') {
    Write-Host "Already Disabled -- no change."
    return
}

Disable-ScheduledTask -TaskName $taskName | Out-Null
$after = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Write-Host "New state: $($after.State)"
if ($after.State -eq 'Disabled') {
    Write-Host "DONE: PUSH meeting-action-capture retired. The PULL tool (meeting_action_items) is the way to turn meeting items into tasks now."
} else {
    Write-Host "WARNING: task did not report Disabled -- re-run from an elevated PowerShell."
}
