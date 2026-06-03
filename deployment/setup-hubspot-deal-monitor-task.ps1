# setup-hubspot-deal-monitor-task.ps1
# Registers a Windows Task Scheduler task that runs the HubSpot deal stage monitor
# every 1 hour. Snapshots all active deals and posts Slack notifications for
# any deals that changed stage since the last run.
#
# Run once from an elevated PowerShell prompt:
#   Set-ExecutionPolicy RemoteSigned -Scope Process
#   .\deployment\setup-hubspot-deal-monitor-task.ps1
#
# Prerequisites:
#   1. HUBSPOT_PRIVATE_APP_TOKEN in .env
#   2. SLACK_BOT_TOKEN in .env
#
# To remove the task:
#   Unregister-ScheduledTask -TaskName "Cora - HubSpot Deal Monitor" -Confirm:$false
#
# To run immediately (for testing):
#   Start-ScheduledTask -TaskName "Cora - HubSpot Deal Monitor"
#
# To test without posting to Slack:
#   & "$RepoRoot\.venv\Scripts\python.exe" "$RepoRoot\scripts\run_hubspot_deal_monitor.py" --dry-run

$TaskName = "Cora - HubSpot Deal Monitor"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $RepoRoot "scripts\run_hubspot_deal_monitor.py"
$PythonPath = Join-Path $RepoRoot ".venv\Scripts\python.exe"

# Verify script and interpreter exist before registering
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found: $ScriptPath"
    exit 1
}
if (-not (Test-Path $PythonPath)) {
    Write-Error "Venv python not found: $PythonPath  (run 'uv sync' first)"
    exit 1
}

# Remove existing task if present
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

# Action: run via venv python (full path -- Task Scheduler has no user PATH)
$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $RepoRoot

# Trigger: every 1 hour starting at the next :00 mark
$Trigger = New-ScheduledTaskTrigger `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -Once `
    -At (Get-Date -Minute 0 -Second 0).AddHours(1)

# Settings: don't start if on battery; stop if it runs > 10 minutes
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries:$false

# Run as the current user (who has access to .env)
$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

$result = Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force `
    -ErrorAction Stop 2>&1

Write-Host ""
Write-Host "Task registered: '$TaskName'" -ForegroundColor Green
Write-Host "  Schedule:    Every 1 hour"
Write-Host "  Python:      $PythonPath"
Write-Host "  Script:      $ScriptPath"
Write-Host "  Working dir: $RepoRoot"
Write-Host ""
Write-Host "To run immediately: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To test without Slack: & `"$PythonPath`" `"$ScriptPath`" --dry-run"
