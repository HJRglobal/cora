# setup-meeting-action-capture-task.ps1
# Registers a Windows Task Scheduler task that runs the meeting action capture
# every 1 hour. Fetches new Fireflies transcripts, parses action items, creates
# Asana tasks, and posts Slack digests.
#
# Run once from an elevated PowerShell prompt:
#   Set-ExecutionPolicy RemoteSigned -Scope Process
#   .\deployment\setup-meeting-action-capture-task.ps1
#
# Prerequisites:
#   1. FIREFLIES_API_KEY in .env
#   2. ANTHROPIC_API_KEY in .env
#   3. ASANA_PAT in .env
#   4. SLACK_BOT_TOKEN in .env
#
# To remove the task:
#   Unregister-ScheduledTask -TaskName "Cora - Meeting Action Capture" -Confirm:$false
#
# To run immediately (for testing):
#   Start-ScheduledTask -TaskName "Cora - Meeting Action Capture"
#
# To test without creating tasks:
#   & "$RepoRoot\.venv\Scripts\python.exe" "$RepoRoot\scripts\run_meeting_action_capture.py" --dry-run

$TaskName = "Cora - Meeting Action Capture"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $RepoRoot "scripts\run_meeting_action_capture.py"
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
Write-Host "To test without tasks: & `"$PythonPath`" `"$ScriptPath`" --dry-run"
