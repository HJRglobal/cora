# setup-clover-daily-summary-task.ps1
# Register Windows Task Scheduler task for Cora Clover Daily Store Summary.
# Fires daily at 01:00 UTC (6pm AZ, America/Phoenix).
#
# Run from an elevated PowerShell prompt:
#   .\deployment\setup-clover-daily-summary-task.ps1
#
# Doctrine: Task Scheduler has NO user PATH -- all paths must be absolute.

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "$RepoRoot\.venv\Scripts\python.exe"
$ScriptPath = "$RepoRoot\scripts\run_clover_daily_summary.py"
$TaskName   = "Cora - Clover Daily Summary"
$LogDir     = "$RepoRoot\logs"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $ScriptPath `
    -WorkingDirectory $RepoRoot

# Daily at 01:00 UTC (6pm AZ)
$Trigger = New-ScheduledTaskTrigger -Daily -At "01:00"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

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
    -Description "Cora: Post yesterday's OSN Clover sales summary to #osn-leadership. Daily at 01:00 UTC (6pm AZ)." | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Trigger: Daily at 01:00 UTC (6pm AZ / America/Phoenix)"
Write-Host "Interpreter: $PythonExe"
Write-Host "Script: $ScriptPath"
Write-Host ""
Write-Host "To run immediately for testing:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "To test without posting:"
Write-Host "  & '$PythonExe' '$ScriptPath' --dry-run"
