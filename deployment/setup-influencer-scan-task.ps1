# setup-influencer-scan-task.ps1
# Registers the Cora influencer scan as a Windows scheduled task.
# Runs twice daily: 7:00 AM and 7:00 PM.
# Detects new tagged posts on F3 brand Instagram accounts and posts
# Slack alerts to the influencer ops channel for Alex to confirm.
#
# Run once from PowerShell (as Administrator or normal user -- no admin needed):
#   cd C:\Users\Harri\code\cora
#   .\deployment\setup-influencer-scan-task.ps1
#
# Prerequisites:
#   1. Complete META_SETUP_GUIDE.md -- add IG tokens to .env
#   2. Confirm brand handles in data/maps/brand-social-accounts.yaml
#   3. Set INFLUENCER_SCAN_NOTIFY_CHANNEL in .env (default: f3-sales)

$TaskName   = "cowork-cora-influencer-scan"
$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonPath = "$RepoRoot\.venv\Scripts\python.exe"
$ScriptPath = "$RepoRoot\scripts\run_influencer_scan.py"
$LogDir     = "$RepoRoot\logs"

# Verify script + interpreter exist before registering (Task Scheduler has NO user PATH;
# absolute paths to both are required to avoid silent ERROR_FILE_NOT_FOUND 0x80070002 on every tick)
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found: $ScriptPath"
    exit 1
}
if (-not (Test-Path $PythonPath)) {
    Write-Error "Venv python not found: $PythonPath  (run 'uv sync' first)"
    exit 1
}

# Ensure log directory exists
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
    Write-Host "Created log directory: $LogDir"
}

# Remove existing task if present (clean reinstall)
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

$Action  = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $RepoRoot

# Two daily triggers: 7:00 AM and 7:00 PM
$Trigger7AM = New-ScheduledTaskTrigger -Daily -At "07:00AM"
$Trigger7PM = New-ScheduledTaskTrigger -Daily -At "07:00PM"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName   $TaskName `
    -Action     $Action `
    -Trigger    @($Trigger7AM, $Trigger7PM) `
    -Settings   $Settings `
    -Description "Cora influencer scan - polls F3 brand IG accounts at 7am and 7pm for athlete post detections" `
    | Out-Null

Write-Host ""
Write-Host "Task registered: $TaskName"
Write-Host "  Schedule : Daily at 7:00 AM and 7:00 PM"
Write-Host "  Python   : $PythonPath"
Write-Host "  Script   : $ScriptPath"
Write-Host "  Logs     : $LogDir\influencer-scan-YYYY-MM-DD.log"
Write-Host ""
Write-Host "To run immediately for a smoke test:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "To check last run status:"
Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName' | Select LastRunTime, LastTaskResult"
Write-Host ""
Write-Host "NOTE: Scanning will silently skip any brand accounts whose .env tokens are not"
Write-Host "      populated yet. Complete META_SETUP_GUIDE.md first to activate each account."
