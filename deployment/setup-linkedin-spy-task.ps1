# setup-linkedin-spy-task.ps1
# Registers a Windows Task Scheduler task that runs the F3 LinkedIn Spy
# every Monday at 8:00 AM.
#
# Run once from PowerShell (no elevation needed -- runs as current user):
#   Set-ExecutionPolicy RemoteSigned -Scope Process
#   .\deployment\setup-linkedin-spy-task.ps1
#
# Prerequisites:
#   1. APOLLO_API_KEY is set in .env.
#   2. ANTHROPIC_API_KEY and SLACK_BOT_TOKEN are set in .env.
#   3. LINKEDIN_SPY_CHANNEL is set in .env (default: f3e-sales).
#
# To remove the task:
#   Unregister-ScheduledTask -TaskName "Cora - LinkedIn Spy" -Confirm:$false
#
# To run immediately (for testing):
#   Start-ScheduledTask -TaskName "Cora - LinkedIn Spy"

$TaskName = "Cora - LinkedIn Spy"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $RepoRoot "scripts\run_linkedin_spy.py"
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
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

# Action: run the spy script via the venv python (full path -- Task Scheduler has no user PATH)
$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $RepoRoot

# Trigger: every Monday at 8:00 AM
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday `
    -At "08:00"

# Settings: run if missed (laptop was off Monday morning); stop if > 10 minutes
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries:$false

# Run as the current user (who has access to .env and Apollo key)
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
Write-Host "  Schedule:    Every Monday at 08:00"
Write-Host "  Python:      $PythonPath"
Write-Host "  Script:      $ScriptPath"
Write-Host "  Working dir: $RepoRoot"
Write-Host ""
Write-Host "To run immediately: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To test manually:   uv run python scripts\run_linkedin_spy.py"
