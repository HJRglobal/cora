# remove-digest-task.ps1
#
# Removes the cowork-cora-digest scheduled task from Task Scheduler.
# Does NOT affect the cowork-cora-service (bot) task.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File "C:\Users\Harri\code\cora\deployment\remove-digest-task.ps1"

$ErrorActionPreference = "Stop"

$TASK_NAME = "cowork-cora-digest"

Write-Host ""
Write-Host "=== Cora Digest Task Removal ==="
Write-Host ""

# ------------------------------------------------------------------
# [1/2] Check task exists
# ------------------------------------------------------------------
Write-Host "[1/2] Looking for task '$TASK_NAME'..."
$task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "  Task not found - nothing to remove."
    Write-Host ""
    Write-Host "=== Done (no-op) ==="
    Write-Host ""
    exit 0
}
Write-Host "  Found task in state: $($task.State)"

# ------------------------------------------------------------------
# [2/2] Remove
# ------------------------------------------------------------------
Write-Host "[2/2] Removing task..."
Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false

$check = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if ($check) {
    Write-Host "  ERROR: Task still present after removal attempt." -ForegroundColor Red
    exit 1
}
Write-Host "  OK  Task removed."

Write-Host ""
Write-Host "=== Removal complete ==="
Write-Host ""
Write-Host "Note: cowork-cora-service (the bot) was NOT affected."
Write-Host ""
