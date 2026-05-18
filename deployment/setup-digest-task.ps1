# setup-digest-task.ps1
#
# Registers cowork-cora-digest as a Windows Task Scheduler task.
# The task runs the Knowledge Gaps digest builder once daily at 5:00 AM (local time).
# This is a one-shot daily task -- not a persistent service.
#
# Usage (run from any directory, as the current user - no elevation needed):
#   powershell -ExecutionPolicy Bypass -File "C:\Users\Harri\code\cora\deployment\setup-digest-task.ps1"
#
# To remove the task:
#   deployment\remove-digest-task.ps1

$ErrorActionPreference = "Stop"

$TASK_NAME    = "cowork-cora-digest"
$REPO_DIR     = "C:\Users\Harri\code\cora"
$SCRIPT_PATH  = "$REPO_DIR\scripts\generate_knowledge_gaps_digest.py"

Write-Host ""
Write-Host "=== Cora Digest Task Setup ==="
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
# [2/5] Pre-flight: digest script
# ------------------------------------------------------------------
Write-Host "[2/5] Checking digest script..."
if (-not (Test-Path $SCRIPT_PATH -PathType Leaf)) {
    Write-Host "  ERROR: Script not found at $SCRIPT_PATH" -ForegroundColor Red
    Write-Host "         Pull the latest changes from GitHub before running this script."
    exit 1
}
Write-Host "  OK  $SCRIPT_PATH"

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

$existing = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Found existing task - removing before re-registration."
    Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $uvExe `
    -Argument "run python scripts/generate_knowledge_gaps_digest.py" `
    -WorkingDirectory $REPO_DIR

$trigger = New-ScheduledTaskTrigger -Daily -At "05:00"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -WakeToRun

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
Write-Host "  OK  NextRunTime: $($info.NextRunTime)"

Write-Host ""
Write-Host "=== Setup complete ==="
Write-Host ""
Write-Host "The digest will run automatically every day at 5:00 AM."
Write-Host "To run it manually right now:"
Write-Host "  Start-ScheduledTask -TaskName '$TASK_NAME'"
Write-Host "Or run the script directly:"
Write-Host "  uv run python scripts/generate_knowledge_gaps_digest.py"
Write-Host ""
