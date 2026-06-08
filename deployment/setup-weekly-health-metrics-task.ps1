# Setup Windows Task Scheduler task for the Cora weekly health metrics report.
# Fires weekly Monday 09:30 (host local = AZ), after the nightly KB-sync window
# so the metrics reflect the completed syncs. Posts a compact digest +
# section-5 threshold alarms to Slack #cora-health (the infra-health channel).
# ASCII-only per D-016. Run from an elevated PowerShell prompt (registration
# requires admin).

$TaskName = "Cora - Weekly Health Metrics"
$RepoRoot = "C:\Users\Harri\code\cora"
$Python   = "$RepoRoot\.venv\Scripts\python.exe"
$Script   = "$RepoRoot\scripts\cora_health_report.py"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# --slack posts the digest; offline char/4 token heuristic (no API key needed).
# Channel pinned to cora-health so it does not depend on HEALTH_REPORT_CHANNEL.
$Action  = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "$Script --slack --channel cora-health --log-days 7" `
    -WorkingDirectory $RepoRoot

# Weekly Monday 09:30 local (AZ)
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "09:30"

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

Write-Host "Registered: $TaskName (weekly Monday 09:30 AZ)"
Write-Host "Smoke test now:"
Write-Host "  $Python $Script --slack --channel cora-health --log-days 7"
