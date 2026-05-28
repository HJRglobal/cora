# setup-security-monitor-task.ps1
#
# Registers the Cora security monitor as a Windows Task Scheduler task.
# The task runs every 15 minutes and posts Slack alerts on suspicious activity.
#
# Usage (run from any directory, as the current user - no elevation needed):
#   powershell -ExecutionPolicy Bypass -File "C:\Users\Harri\code\cora\deployment\setup-security-monitor-task.ps1"
#
# To remove the task:
#   deployment\remove-security-monitor-task.ps1

$ErrorActionPreference = "Stop"

$TASK_NAME = "cowork-cora-security-monitor"
$REPO_DIR  = "C:\Users\Harri\code\cora"
$SCRIPT    = "$REPO_DIR\scripts\security_monitor.py"

Write-Host ""
Write-Host "=== Cora Security Monitor Setup ==="
Write-Host ""

# ------------------------------------------------------------------
# [1/5] Pre-flight: repo directory
# ------------------------------------------------------------------
Write-Host "[1/5] Checking repo directory..."
if (-not (Test-Path $REPO_DIR -PathType Container)) {
    Write-Host "  ERROR: Repo not found at $REPO_DIR" -ForegroundColor Red
    exit 1
}
Write-Host "  OK  $REPO_DIR"

# ------------------------------------------------------------------
# [2/5] Pre-flight: .env and script
# ------------------------------------------------------------------
Write-Host "[2/5] Checking prerequisites..."
if (-not (Test-Path "$REPO_DIR\.env" -PathType Leaf)) {
    Write-Host "  ERROR: .env not found - copy .env.example and fill in tokens first." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $SCRIPT -PathType Leaf)) {
    Write-Host "  ERROR: security_monitor.py not found at $SCRIPT" -ForegroundColor Red
    exit 1
}
Write-Host "  OK  .env present"
Write-Host "  OK  security_monitor.py present"

# ------------------------------------------------------------------
# [3/5] Locate uv.exe
# ------------------------------------------------------------------
Write-Host "[3/5] Locating uv.exe..."
$uvExe = $null
$candidates = @(
    "C:\Users\Harri\.local\bin\uv.exe",
    "$env:LOCALAPPDATA\uv\bin\uv.exe",
    "$env:LOCALAPPDATA\Programs\uv\uv.exe"
)
foreach ($c in $candidates) {
    if (Test-Path $c -PathType Leaf) { $uvExe = $c; break }
}
if (-not $uvExe) {
    try { $uvExe = (Get-Command uv -ErrorAction Stop).Source } catch {}
}
if (-not $uvExe) {
    Write-Host "  ERROR: uv.exe not found. Install uv first: https://docs.astral.sh/uv/" -ForegroundColor Red
    exit 1
}
Write-Host "  OK  $uvExe"

# ------------------------------------------------------------------
# [4/5] Register task (idempotent - remove then re-add)
# ------------------------------------------------------------------
Write-Host "[4/5] Registering scheduled task '$TASK_NAME'..."

$existing = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Found existing task - removing before re-registration."
    Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $uvExe `
    -Argument "run python `"$SCRIPT`"" `
    -WorkingDirectory $REPO_DIR

# Fire at logon, then repeat every 15 minutes indefinitely
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.RepetitionInterval = [TimeSpan]::FromMinutes(15)
$trigger.RepetitionDuration = [TimeSpan]::MaxValue
$trigger.StopAtDurationEnd  = $false

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances   IgnoreNew `
    -RestartCount        3 `
    -RestartInterval     (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit  (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal `
    -UserId   $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName    $TASK_NAME `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -Principal   $principal `
    -Description "Cora security monitor - scans logs and file integrity every 15 minutes, Slack-alerts on anomalies." `
    | Out-Null

Write-Host "  OK  Task registered."

# ------------------------------------------------------------------
# [5/5] Initialize integrity baseline and start task
# ------------------------------------------------------------------
Write-Host "[5/5] Initializing file-integrity baseline..."
& $uvExe run python "$SCRIPT" --init
if ($LASTEXITCODE -ne 0) {
    Write-Host "  WARNING: Baseline init returned exit code $LASTEXITCODE - check manually." -ForegroundColor Yellow
} else {
    Write-Host "  OK  Baseline recorded in data\security\file_hashes.json"
}

# Verify and start
$task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "  ERROR: Task not found after registration." -ForegroundColor Red
    exit 1
}
$info = Get-ScheduledTaskInfo -TaskName $TASK_NAME
Write-Host "  OK  State      : $($task.State)"
Write-Host "  OK  Last result: $($info.LastTaskResult)"

Start-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
Write-Host "  OK  Task started."

Write-Host ""
Write-Host "=== Setup complete ==="
Write-Host ""
Write-Host "The security monitor runs every 15 minutes."
Write-Host "Alerts post to the channel in SECURITY_ALERT_CHANNEL (.env), default: #cora-build."
Write-Host ""
Write-Host "To test immediately:"
Write-Host "  uv run python scripts\security_monitor.py --dry-run"
Write-Host ""
Write-Host "To remove: powershell -ExecutionPolicy Bypass -File deployment\remove-security-monitor-task.ps1"
Write-Host ""
