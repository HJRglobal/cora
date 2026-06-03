# setup-deal-aging-alerts-task.ps1
# Register Windows Task Scheduler task for Cora Deal Aging Alerts.
# Fires daily at 15:00 UTC (8am AZ, America/Phoenix).
#
# Run from an elevated PowerShell prompt:
#   .\deployment\setup-deal-aging-alerts-task.ps1
#
# Doctrine: Task Scheduler has NO user PATH -- all paths must be absolute.

$RepoRoot    = "C:\Users\Harri\code\cora"
$PythonExe   = "$RepoRoot\.venv\Scripts\python.exe"
$ScriptPath  = "$RepoRoot\scripts\run_deal_aging_alerts.py"
$TaskName    = "Cora - Deal Aging Alerts"
$LogDir      = "$RepoRoot\logs"

# Ensure log directory exists
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $ScriptPath `
    -WorkingDirectory $RepoRoot

# Daily at 15:00 UTC
$Trigger = New-ScheduledTaskTrigger -Daily -At "15:00"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable

# Run as current user (inherits .env and credentials)
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Remove existing task if present
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Cora: DM HubSpot deal owners when deals stall past stage thresholds. Daily at 15:00 UTC (8am AZ)." | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Trigger: Daily at 15:00 UTC (8am AZ / America/Phoenix)"
Write-Host "Interpreter: $PythonExe"
Write-Host "Script: $ScriptPath"
Write-Host ""
Write-Host "To run immediately for testing:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "To test without sending messages:"
Write-Host "  & '$PythonExe' '$ScriptPath' --dry-run"
