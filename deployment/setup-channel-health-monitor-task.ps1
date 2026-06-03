# Setup Windows Task Scheduler task for Cora Channel Health Monitor
# Fires weekly Sunday at 04:00 UTC (9pm AZ Saturday)
# Run this script from an elevated PowerShell prompt.

$TaskName = "Cora - Channel Health Monitor"
$RepoRoot = "C:\Users\Harri\code\cora"
$Python   = "$RepoRoot\.venv\Scripts\python.exe"
$Script   = "$RepoRoot\scripts\run_channel_health_monitor.py"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$Action  = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument $Script `
    -WorkingDirectory $RepoRoot

# Weekly on Sunday at 04:00 UTC
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "04:00"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
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

Write-Host "Registered: $TaskName (weekly Sunday 04:00 UTC)"
