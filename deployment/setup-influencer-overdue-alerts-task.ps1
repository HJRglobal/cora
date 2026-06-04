# setup-influencer-overdue-alerts-task.ps1
# Registers the Cora influencer overdue alerts as a Windows scheduled task.
# Runs daily at 9:00 AM -- DMs Alex once per overdue deliverable (72h throttle).
#
# Run once from PowerShell:
#   cd C:\Users\Harri\code\cora
#   .\deployment\setup-influencer-overdue-alerts-task.ps1

$TaskName   = "cowork-cora-influencer-overdue-alerts"
$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonPath = "$RepoRoot\.venv\Scripts\python.exe"
$ScriptPath = "$RepoRoot\scripts\run_influencer_overdue_alerts.py"
$LogDir     = "$RepoRoot\logs"

if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found: $ScriptPath"
    exit 1
}
if (-not (Test-Path $PythonPath)) {
    Write-Error "Venv python not found: $PythonPath  (run 'uv sync' first)"
    exit 1
}

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
    Write-Host "Created log directory: $LogDir"
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $RepoRoot

# Daily at 9:00 AM
$Trigger = New-ScheduledTaskTrigger `
    -Daily `
    -At "09:00AM"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName   $TaskName `
    -Action     $Action `
    -Trigger    $Trigger `
    -Settings   $Settings `
    -Description "Cora influencer overdue alerts - DMs Alex once per overdue deliverable (72h throttle)" `
    | Out-Null

Write-Host ""
Write-Host "Task registered: $TaskName"
Write-Host "  Schedule : Daily at 9:00 AM"
Write-Host "  Python   : $PythonPath"
Write-Host "  Script   : $ScriptPath"
Write-Host "  Logs     : $LogDir\influencer-overdue-alerts-YYYY-MM-DD.log"
Write-Host ""
Write-Host "To run immediately for a smoke test:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
