# Remove the three KB incremental-sync scheduled tasks registered by setup-kb-sync-tasks.ps1.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\remove-kb-sync-tasks.ps1
#
# Idempotent — safe to run if any subset of the tasks already doesn't exist.

$ErrorActionPreference = "Stop"

$Tasks = @(
    "cowork-cora-kb-sync-asana",
    "cowork-cora-kb-sync-fireflies",
    "cowork-cora-kb-sync-static"
)

foreach ($taskName in $Tasks) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Removing $taskName..." -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "  Removed." -ForegroundColor Green
    } else {
        Write-Host "Skipping $taskName (not registered)." -ForegroundColor Gray
    }
}

Write-Host ""
Write-Host "All KB sync tasks removed." -ForegroundColor Green
