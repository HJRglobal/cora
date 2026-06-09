# setup-founders-os-sweep-task.ps1
# Registers the nightly HJR-Founder-OS Drive sweep as a Windows Scheduled Task.
# Run from elevated PowerShell: .\deployment\setup-founders-os-sweep-task.ps1
#
# Schedule: nightly at 4:15 AM AZ (runs AFTER user Drive sweep at 3:30am
# and KB static sync at 4:00am, so all sources land before the 5:30am
# reconciliation engine fires).
#
# Re-run this script any time you want to update the schedule.

$TaskName    = "cowork-cora-founders-os-sweep"
$RepoRoot    = "C:\Users\Harri\code\cora"
$Python      = "$RepoRoot\.venv\Scripts\python.exe"
$Script      = "$RepoRoot\scripts\ingest_founders_os.py"
$LogDir      = "$RepoRoot\logs"

# Remove existing task if present
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

# Action: run nightly incremental (no flags = watermark-based, picks up new/changed files only)
$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument $Script `
    -WorkingDirectory $RepoRoot

# Trigger: daily at 6:30 AM local (AZ). Moved off 4:15 so this heavy Drive BFS
# does not bunch with the 4:00/4:30 KB-sync band; 30 min after the Drive Sweep.
$Trigger = New-ScheduledTaskTrigger -Daily -At "06:30AM"

# Settings: run whether logged on or not, 2-hour execution limit
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

# Register
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -RunLevel Highest `
    -Force | Out-Null

Write-Host "Registered: $TaskName (nightly 6:30 AM AZ)"
Write-Host ""
Write-Host "To run immediately (incremental):"
Write-Host "  schtasks /Run /TN $TaskName"
Write-Host ""
Write-Host "To run a dry-run first (RECOMMENDED before first backfill):"
Write-Host "  $Python $Script --dry-run"
Write-Host ""
Write-Host "To run phased backfill:"
Write-Host "  $Python $Script --phase 1 --backfill   # Foundation"
Write-Host "  $Python $Script --phase 2 --backfill   # F3E + OSN"
Write-Host "  $Python $Script --phase 3 --backfill   # HJRP + BDM + HJRPROD"
Write-Host "  $Python $Script --phase 4 --backfill   # LEX (PHI guard active)"
Write-Host "  $Python $Script --phase 5 --backfill   # F3C + UFL"
Write-Host ""
Write-Host "IMPORTANT -- one-time Harrison action required before first run:"
Write-Host "  1. Open Google Drive"
Write-Host "  2. Right-click HJR-Founder-OS folder"
Write-Host "  3. Share with: cora-calendar@cora-calendar-readonly.iam.gserviceaccount.com"
Write-Host "  4. Permission level: Viewer"
Write-Host "  5. Run dry-run to verify access, then run phase 1"
