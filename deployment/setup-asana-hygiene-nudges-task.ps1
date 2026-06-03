# Setup Windows Task Scheduler task for Cora Asana Hygiene Nudges
# Fires daily at 06:30 UTC (after reconciliation at 05:30 UTC)
# Run this script from an elevated PowerShell prompt.

$TaskName = "Cora - Asana Hygiene Nudges"
$RepoRoot = "C:\Users\Harri\code\cora"
$Python   = "$RepoRoot\.venv\Scripts\python.exe"
$Script   = "$RepoRoot\scripts\run_asana_hygiene_nudges.py"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$Action  = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument $Script `
    -WorkingDirectory $RepoRoot

# Daily at 06:30 UTC (11:30 PM AZ / MST)
$Trigger = New-ScheduledTaskTrigger -Daily -At "06:30"

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

Write-Host "Registered: $TaskName (daily 06:30 UTC)"
