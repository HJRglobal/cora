# setup-fireflies-coverage-task.ps1
# Registers the Cora Fireflies DWD coverage monitor as a Windows scheduled task.
# Runs weekly Monday 8:00 AM -- DMs Harrison a digest of who is COVERED /
# MEMBER_NO_RECORDINGS / NOT_A_MEMBER in Fireflies, AND nudges each uncovered
# teammate (7-day throttle per user).
#
# ARMED FOR NUDGE 2026-06-08 (after Harrison reviewed the first digest). The
# next scheduled run will DM ~14 teammates. To revert to digest-only, change
# the argument back to "--digest-only" and re-run this script.
#
# Run once from elevated PowerShell:
#   cd C:\Users\Harri\code\cora
#   .\deployment\setup-fireflies-coverage-task.ps1

$TaskName   = "cowork-cora-fireflies-coverage"
$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonPath = "$RepoRoot\.venv\Scripts\python.exe"
$ScriptPath = "$RepoRoot\scripts\run_fireflies_coverage.py"
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

# NUDGE armed 2026-06-08 (Harrison reviewed the first digest). Revert to "--digest-only" to disarm.
$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$ScriptPath`" --nudge" `
    -WorkingDirectory $RepoRoot

# Weekly, Monday 8:00 AM
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday `
    -At "08:00AM"

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
    -Description "Cora Fireflies DWD coverage monitor - weekly digest to Harrison + nudge uncovered teammates (7d throttle)" `
    | Out-Null

Write-Host ""
Write-Host "Task registered: $TaskName"
Write-Host "  Schedule : Weekly, Monday 8:00 AM"
Write-Host "  Argument : --nudge  (digest to Harrison + DMs each uncovered teammate, 7d throttle)"
Write-Host "  Python   : $PythonPath"
Write-Host "  Script   : $ScriptPath"
Write-Host "  Logs     : $LogDir\fireflies-coverage-YYYY-MM-DD.log"
Write-Host ""
Write-Host "To run immediately for a smoke test:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
