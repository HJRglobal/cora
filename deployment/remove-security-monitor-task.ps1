# remove-security-monitor-task.ps1
#
# Removes the cowork-cora-security-monitor scheduled task.
# Does NOT delete the script or the data/security/ baseline files.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File "C:\Users\Harri\code\cora\deployment\remove-security-monitor-task.ps1"

$ErrorActionPreference = "Stop"

$TASK_NAME = "cowork-cora-security-monitor"

Write-Host ""
Write-Host "=== Cora Security Monitor Removal ==="
Write-Host ""

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

Write-Host "[2/2] Removing task..."
Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false

$check = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if ($check) {
    Write-Host "  ERROR: Task still present after removal." -ForegroundColor Red
    exit 1
}
Write-Host "  OK  Task removed."

Write-Host ""
Write-Host "=== Removal complete ==="
Write-Host ""
Write-Host "Baseline and alert history in data\security\ were NOT deleted."
Write-Host "To re-enable: powershell -ExecutionPolicy Bypass -File deployment\setup-security-monitor-task.ps1"
Write-Host ""
