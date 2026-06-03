# setup-finance-weekly-task.ps1
# Registers the Finance Weekly recap task in Windows Task Scheduler.
# Run from elevated PowerShell in C:\Users\Harri\code\cora:
#   .\deployment\setup-finance-weekly-task.ps1
#
# Schedule: Every Monday at 7:30am AZ (14:30 UTC in summer / 14:30 winter)
# Script posts to #hjrg-finance (C0B3V5SDNAG) ONLY -- no fallback channels.

$TaskName   = "cowork-cora-finance-weekly"
$RepoRoot   = "C:\Users\Harri\code\cora"
$Python     = "$RepoRoot\.venv\Scripts\python.exe"
$Script     = "$RepoRoot\scripts\run_finance_weekly_recap.py"
$LogDir     = "$RepoRoot\logs"

# Orphan-kill: stop any prior instance before re-registering
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Stopping existing task: $TaskName"
    schtasks /End /TN $TaskName 2>$null
    Get-WmiObject Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*finance_weekly_recap*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$Script`"" `
    -WorkingDirectory $RepoRoot

# Every Monday at 14:30 UTC = 7:30am AZ (UTC-7)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "14:30"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Finance Weekly recap -- posts to #hjrg-finance every Monday" `
    -Force | Out-Null

Write-Host "Registered: $TaskName (Monday 14:30 UTC / 7:30am AZ)"
Write-Host "To test:    schtasks /Run /TN $TaskName"
Write-Host "To dry-run: $Python `"$Script`" --dry-run"
