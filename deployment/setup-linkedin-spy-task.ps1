# setup-linkedin-spy-task.ps1
<<<<<<< HEAD
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
=======
# Registers the F3 LinkedIn Spy as a Windows scheduled task.
# Runs every Monday at 7:00 AM so Tommy Anderson sees a fresh prospect
# report at the start of each work week in #f3e-sales.
#
# Run once from PowerShell (no admin required):
#   cd C:\Users\Harri\code\cora
#   .\deployment\setup-linkedin-spy-task.ps1
#
# Prerequisites:
#   1. APOLLO_API_KEY set in .env (Apollo Professional — trial expires June 10, 2026)
#   2. ANTHROPIC_API_KEY and SLACK_BOT_TOKEN already in .env (powering Cora)
#   3. LINKEDIN_SPY_CHANNEL set in .env (default: f3e-sales)
#   4. uv sync run at least once so .venv exists
#
# To remove the task:
#   .\deployment\remove-linkedin-spy-task.ps1
#
# To run immediately (smoke test):
#   Start-ScheduledTask -TaskName "Cora - LinkedIn Spy"

$TaskName  = "Cora - LinkedIn Spy"
$RepoRoot  = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $RepoRoot "scripts\run_linkedin_spy.py"
$PythonPath = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$LogDir     = Join-Path $RepoRoot "logs"

# Ensure log directory exists
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
    Write-Host "Created log directory: $LogDir"
}
>>>>>>> cedf5b7 (Add Windows Task Scheduler scripts for LinkedIn Spy weekly task)

# Verify script and interpreter exist before registering
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found: $ScriptPath"
<<<<<<< HEAD
    exit 1
}
if (-not (Test-Path $PythonPath)) {
    Write-Error "Venv python not found: $PythonPath  (run 'uv sync' first)"
    exit 1
}

# Remove existing task if present
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
=======
    Write-Error "Run 'git pull' to fetch the latest code, then re-run this script."
    exit 1
}
if (-not (Test-Path $PythonPath)) {
    Write-Error "Venv python not found: $PythonPath"
    Write-Error "Run 'uv sync' first to create the virtual environment."
    exit 1
}

# Remove existing task if present (clean reinstall)
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
>>>>>>> cedf5b7 (Add Windows Task Scheduler scripts for LinkedIn Spy weekly task)
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

<<<<<<< HEAD
# Action: run the spy script via the venv python (full path -- Task Scheduler has no user PATH)
=======
# Action: full venv python path — Task Scheduler runs with a minimal environment
# that does not include the user PATH, so uv / global python are not accessible.
>>>>>>> cedf5b7 (Add Windows Task Scheduler scripts for LinkedIn Spy weekly task)
$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $RepoRoot

<<<<<<< HEAD
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
=======
# Trigger: every Monday at 07:00 AM
# Start date is next Monday so the task is ready immediately.
$NextMonday = (Get-Date).Date
while ($NextMonday.DayOfWeek -ne [System.DayOfWeek]::Monday) {
    $NextMonday = $NextMonday.AddDays(1)
}
$TriggerTime = $NextMonday.AddHours(7)

$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 1 `
    -DaysOfWeek Monday `
    -At $TriggerTime

# Settings: allow up to 5 minutes (Apollo + Claude well within that); start if missed
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -DontStopIfGoingOnBatteries:$false

# Run as the current interactive user (who has .env access)
>>>>>>> cedf5b7 (Add Windows Task Scheduler scripts for LinkedIn Spy weekly task)
$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

<<<<<<< HEAD
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
=======
Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $Action `
    -Trigger     $Trigger `
    -Settings    $Settings `
    -Principal   $Principal `
    -Description "F3 LinkedIn Spy — weekly retail buyer & executive prospect scanner. Posts top 10 new prospects to #f3e-sales every Monday morning." `
    -Force `
    -ErrorAction Stop | Out-Null

Write-Host ""
Write-Host "Task registered: '$TaskName'" -ForegroundColor Green
Write-Host "  Schedule :  Every Monday at 7:00 AM"
Write-Host "  First run:  $TriggerTime"
Write-Host "  Python   :  $PythonPath"
Write-Host "  Script   :  $ScriptPath"
Write-Host "  Logs     :  $LogDir\linkedin-spy-YYYY-MM-DD.log"
Write-Host ""
Write-Host "To run immediately (posts to #f3e-sales):"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "To check last run:"
Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName' | Select LastRunTime, LastTaskResult"
Write-Host ""
Write-Host "To expand prospect pool: edit data\maps\linkedin-spy-search-config.yaml"
Write-Host "  Increase max_pages (currently 3 = 300 candidates/run) for deeper weekly pulls."
Write-Host ""
Write-Host "NOTE: Apollo trial expires June 10, 2026. Upgrade to Professional (~`$99/mo)"
Write-Host "      before then to keep the scanner running without interruption."
>>>>>>> cedf5b7 (Add Windows Task Scheduler scripts for LinkedIn Spy weekly task)
