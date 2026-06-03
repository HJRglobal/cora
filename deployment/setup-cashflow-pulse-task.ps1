# Setup Windows Task Scheduler task for Cora Cash Flow Pulse
# Fires daily at 15:30 UTC (8:30 AM AZ / MST)
# Run this script from an elevated PowerShell prompt.

$TaskName = "Cora - Cash Flow Pulse"
$RepoRoot = "C:\Users\Harri\code\cora"
$Python   = "$RepoRoot\.venv\Scripts\python.exe"
$Script   = "$RepoRoot\scripts\run_cashflow_pulse.py"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$Action  = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument $Script `
    -WorkingDirectory $RepoRoot

# Daily at 15:30 UTC -- 8:30 AM Arizona (MST, UTC-7)
$Trigger = New-ScheduledTaskTrigger -Daily -At "15:30"

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

Write-Host "Registered: $TaskName (daily 15:30 UTC)"
