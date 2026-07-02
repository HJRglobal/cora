# Register the weekly golden-set KB evals task (WS-3, flywheel-reliability).
# Run from ELEVATED PowerShell in C:\Users\Harri\code\cora
#
# Schedule: weekly Monday 09:05 AZ -- BEFORE "Cora - Weekly Health Metrics"
# (Mon 09:30) so the eval summary and health digest land together in
# #cora-health, and on a unique clock minute per the stagger doctrine
# (verify with the nightly collision check after registering; the on-disk
# restagger baseline is deployment\restagger-morning-tasks-2026-06-13.ps1).
#
# D-005: absolute .venv python path, never `uv run`.

$ErrorActionPreference = "Stop"

$TaskName   = "Cora - KB Evals"
$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ScriptPath = "scripts\run_kb_evals.py"
$TaskArgs   = "$ScriptPath --slack --channel cora-health"

if (-not (Test-Path $PythonExe)) {
    Write-Host "ERROR: $PythonExe not found -- run from the cora repo root." -ForegroundColor Red
    exit 1
}

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument $TaskArgs -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 9:05am
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Weekly golden-set KB evals (L1 retrieval + guard canon) -> #cora-health. WS-3 flywheel-reliability 2026-07-01." `
    -Force

Write-Host "Registered '$TaskName' (weekly Monday 9:05 AM AZ)." -ForegroundColor Green
Write-Host "Smoke test now with:" -ForegroundColor Yellow
Write-Host "  $PythonExe $ScriptPath" -ForegroundColor Yellow
Write-Host "NOTE: reconcile data\maps\scheduled-task-state.yaml (documentary 'enabled' section)" -ForegroundColor Yellow
Write-Host "and confirm no clock-minute collision in the next weekly health metrics run." -ForegroundColor Yellow
