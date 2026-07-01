# Setup Windows Task Scheduler task for Cora False-Deflection Watch (R6).
# Fires weekly Monday at 08:00 UTC (1am AZ). Posts a summary to #cora-health.
# Script-side (reads on-disk logs, imports no bot module) -- no Cora restart needed.
# Run this script from an elevated PowerShell prompt.

$TaskName = "Cora - False Deflection Watch"
$RepoRoot = "C:\Users\Harri\code\cora"
$Python   = "$RepoRoot\.venv\Scripts\python.exe"
$Script   = "$RepoRoot\scripts\run_false_deflection_watch.py"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$Action  = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument $Script `
    -WorkingDirectory $RepoRoot

# Weekly on Monday at 08:00 UTC (1am AZ) -- after the weekend, distinct minute
# from other morning tasks (B1 de-collision doctrine).
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "08:00"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
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

Write-Host "Registered: $TaskName (weekly Monday 08:00 UTC / 1am AZ -> #cora-health)"
