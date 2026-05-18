# setup-windows-task.ps1
#
# Registers cowork-cora-service as a Windows Task Scheduler task.
# The task launches "uv run cora" at logon and auto-restarts on failure.
#
# Usage (run from any directory, as the current user - no elevation needed):
#   powershell -ExecutionPolicy Bypass -File "C:\Users\Harri\code\cora\deployment\setup-windows-task.ps1"
#
# To remove the task:
#   deployment\remove-windows-task.ps1

$ErrorActionPreference = "Stop"

$TASK_NAME    = "cowork-cora-service"
$REPO_DIR     = "C:\Users\Harri\code\cora"
$ENV_FILE     = "$REPO_DIR\.env"

Write-Host ""
Write-Host "=== Cora Task Scheduler Setup ==="
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
# [2/5] Pre-flight: .env file
# ------------------------------------------------------------------
Write-Host "[2/5] Checking .env file..."
if (-not (Test-Path $ENV_FILE -PathType Leaf)) {
    Write-Host "  ERROR: .env not found at $ENV_FILE" -ForegroundColor Red
    Write-Host "         Copy .env.example and fill in your tokens before running this script."
    exit 1
}
Write-Host "  OK  $ENV_FILE"

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
# [4/5] Build and register the task (idempotent - remove then re-add)
# ------------------------------------------------------------------
Write-Host "[4/5] Registering scheduled task '$TASK_NAME'..."

# Remove existing task silently if present
$existing = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Found existing task - removing before re-registration."
    Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $uvExe `
    -Argument "run cora" `
    -WorkingDirectory $REPO_DIR

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TASK_NAME `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "  OK  Task registered."

# ------------------------------------------------------------------
# [5/5] Verify registration
# ------------------------------------------------------------------
Write-Host "[5/5] Verifying registration..."
$task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "  ERROR: Task was not found after registration." -ForegroundColor Red
    exit 1
}
$info = Get-ScheduledTaskInfo -TaskName $TASK_NAME
Write-Host "  OK  State      : $($task.State)"
Write-Host "  OK  Last result: $($info.LastTaskResult)"

Write-Host ""
Write-Host "=== Setup complete ==="
Write-Host ""
Write-Host "The task will start automatically at next logon."
Write-Host "To start it NOW without rebooting:"
Write-Host "  Start-ScheduledTask -TaskName '$TASK_NAME'"
Write-Host ""
Write-Host "IMPORTANT: If Cora is already running as a foreground process,"
Write-Host "stop it first, then start the task - do not run both at once."
Write-Host ""
