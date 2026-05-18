# remove-windows-task.ps1
#
# Removes the cowork-cora-service scheduled task from Task Scheduler.
# Does NOT stop a currently running instance — kill that separately if needed.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File "C:\Users\Harri\code\cora\deployment\remove-windows-task.ps1"

$ErrorActionPreference = "Stop"

$TASK_NAME = "cowork-cora-service"

Write-Host ""
Write-Host "=== Cora Task Scheduler Removal ==="
Write-Host ""

# ------------------------------------------------------------------
# [1/2] Check task exists
# ------------------------------------------------------------------
Write-Host "[1/2] Looking for task '$TASK_NAME'..."
$task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "  Task not found — nothing to remove."
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
Write-Host "Note: any currently running Cora process was NOT stopped."
Write-Host "If Cora is still running, stop it with:"
Write-Host "  Stop-Process -Name python -Force   # or find the PID from Task Manager"
Write-Host ""
