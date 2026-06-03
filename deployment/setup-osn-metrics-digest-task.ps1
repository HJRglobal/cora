# Setup Windows Task Scheduler task for Cora OSN Weekly Metrics Digest
# Fires weekly Monday at 15:00 UTC (8am AZ)
# Run this script from an elevated PowerShell prompt.

$TaskName = "Cora - OSN Metrics Digest"
$RepoRoot = "C:\Users\Harri\code\cora"
$Python   = "$RepoRoot\.venv\Scripts\python.exe"
$Script   = "$RepoRoot\scripts\run_osn_metrics_digest.py"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$Action  = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument $Script `
    -WorkingDirectory $RepoRoot

# Weekly on Monday at 15:00 UTC (8am AZ)
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "15:00"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -RunLevel Highest `
    -Force | Out-Null

Write-Host "Registered: $TaskName (weekly Monday 15:00 UTC)"
