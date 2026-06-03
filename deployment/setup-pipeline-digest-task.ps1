# setup-pipeline-digest-task.ps1
# Register Windows Task Scheduler task for Cora Weekly Pipeline Digest.
# Fires every Monday at 15:00 UTC (8am AZ, America/Phoenix).
#
# Run from an elevated PowerShell prompt:
#   .\deployment\setup-pipeline-digest-task.ps1
#
# Doctrine: Task Scheduler has NO user PATH -- all paths must be absolute.

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "$RepoRoot\.venv\Scripts\python.exe"
$ScriptPath = "$RepoRoot\scripts\run_pipeline_digest.py"
$TaskName   = "Cora - Weekly Pipeline Digest"
$LogDir     = "$RepoRoot\logs"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $ScriptPath `
    -WorkingDirectory $RepoRoot

# Weekly on Monday at 15:00 UTC (8am AZ)
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "15:00"

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
    -Description "Cora: DM Tommy with F3E pipeline summary + Alex with UFL pipeline. Every Monday at 15:00 UTC (8am AZ)." | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "Trigger: Weekly Monday at 15:00 UTC (8am AZ / America/Phoenix)"
Write-Host "Interpreter: $PythonExe"
Write-Host "Script: $ScriptPath"
Write-Host ""
Write-Host "To run immediately for testing:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "To test without sending messages:"
Write-Host "  & '$PythonExe' '$ScriptPath' --dry-run"
