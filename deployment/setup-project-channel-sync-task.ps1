# setup-project-channel-sync-task.ps1
# Registers the auto-create-project-channels task in Windows Task Scheduler.
# Run from elevated PowerShell in C:\Users\Harri\code\cora:
#   .\deployment\setup-project-channel-sync-task.ps1

$TaskName   = "cowork-cora-project-channel-sync"
$RepoRoot   = "C:\Users\Harri\code\cora"
$Python     = "$RepoRoot\.venv\Scripts\python.exe"
$Script     = "$RepoRoot\scripts\run_project_channel_sync.py"

# Orphan-kill: stop any prior instance before re-registering
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Stopping existing task: $TaskName"
    schtasks /End /TN $TaskName 2>$null
    Get-WmiObject Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*project_channel_sync*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$Script`"" `
    -WorkingDirectory $RepoRoot

# Daily at 16:00 UTC = 9:00am AZ (UTC-7)
$trigger = New-ScheduledTaskTrigger -Daily -At "16:00"

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
    -Description "Auto-create Slack channels for new Asana projects" `
    -Force | Out-Null

Write-Host "Registered: $TaskName (Daily 16:00 UTC / 9:00am AZ)"
Write-Host "To test (dry-run): $Python `"$Script`" --dry-run"
Write-Host "To run now:        schtasks /Run /TN $TaskName"
