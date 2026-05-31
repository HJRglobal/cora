# setup-drive-sweep-task.ps1
# Registers a Windows Task Scheduler task that runs the Drive sweep every night
# at 3:30 AM Arizona time (MST = UTC-7, no DST).
#
# Run once from an elevated PowerShell prompt:
#   Set-ExecutionPolicy RemoteSigned -Scope Process
#   .\deployment\setup-drive-sweep-task.ps1
#
# Prerequisites:
#   1. GOOGLE_SERVICE_ACCOUNT_JSON in .env points to a valid service account key.
#   2. In Google Admin (admin.google.com) -> Security -> API Controls ->
#      Domain-wide Delegation -> edit the Cora SA entry -> scope list must include:
#        https://www.googleapis.com/auth/drive.readonly
#   3. ANTHROPIC_API_KEY is set in .env.
#   4. pdfplumber is installed: uv sync (picks up the new dep from pyproject.toml).
#
# To remove the task:
#   Unregister-ScheduledTask -TaskName "Cora - Drive Sweep" -Confirm:$false
#
# To run immediately (for testing):
#   Start-ScheduledTask -TaskName "Cora - Drive Sweep"
#
# To do a dry-run from the command line:
#   C:\Users\Harri\code\cora\.venv\Scripts\python.exe scripts\run_drive_sweep.py --dry-run
#
# To backfill a single account:
#   C:\Users\Harri\code\cora\.venv\Scripts\python.exe scripts\run_drive_sweep.py --only-email harrison@hjrglobal.com --backfill

$TaskName   = "Cora - Drive Sweep"
$RepoRoot   = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $RepoRoot "scripts\run_drive_sweep.py"
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

# Action: run via venv python (full absolute path -- Task Scheduler has no user PATH)
$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$ScriptPath`" --with-slack" `
    -WorkingDirectory $RepoRoot

# Trigger: daily at 3:30 AM (Arizona MST = UTC-7; adjust if DST ever applies)
$Trigger = New-ScheduledTaskTrigger `
    -Daily `
    -At "03:30"

# Settings: no execution time limit (full-corpus backfill can exceed 2h across 18 accounts)
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries:$false

# Run as the current interactive user (has access to .env and SA key)
$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force `
    -ErrorAction Stop | Out-Null

Write-Host ""
Write-Host "Task registered: '$TaskName'" -ForegroundColor Green
Write-Host "  Schedule:     Daily at 3:30 AM"
Write-Host "  Python:       $PythonPath"
Write-Host "  Script:       $ScriptPath"
Write-Host "  Working dir:  $RepoRoot"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Confirm drive.readonly DWD scope in admin.google.com (one-time)"
Write-Host "  2. uv sync  (picks up pdfplumber)"
Write-Host "  3. Dry-run test:"
Write-Host "       $PythonPath `"$ScriptPath`" --dry-run --only-email harrison@hjrglobal.com"
Write-Host "  4. To run immediately: Start-ScheduledTask -TaskName '$TaskName'"
