# Setup Windows Scheduled Task: Cora lexicon mining, weekly Sunday 17:50 AZ.
#
# Lexicon Flywheel S5 -- lane A aggregates the resolver's own telemetry
# (logs/lexicon-resolutions.jsonl); lane B mines the swept Slack corpus
# (14 days, LEX excluded at the SQL layer, bot-authored chunks excluded) and
# queues at most 5 proposals into the Harrison-gated 7am knowledge-review DM
# (D-011). Sunday-evening proposals ride Monday morning's review run.
#
# Slot check (2026-08-01): Sunday 17:50 is free -- Friction Mining is Sun
# 17:30 (1h limit), Strategy Memo is Sun 18:30; unique clock minute, outside
# the 03:00-09:00 stagger window.
#
# NOTE: the script runs with --write, but proposals are additionally gated on
# CORA_LEXICON=full in .env; at 'resolve' the run is candidates-ledger-only.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-lexicon-mining-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-lexicon-mining' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-lexicon-mining"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_lexicon_mining.py"
$HourMin    = "17:50"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at $PythonExe. Check the venv."
    exit 1
}
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found at $ScriptPath."
    exit 1
}

Write-Host "Setting up scheduled task: $TaskName" -ForegroundColor Cyan

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Removing existing task..." -ForegroundColor Yellow
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# D-005: absolute .venv python + absolute script path + WorkingDirectory
$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$ScriptPath`" --write" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At $HourMin

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Cora lexicon mining: weekly shorthand pass over resolver telemetry + swept Slack; proposals Harrison-gated via knowledge review (Lexicon Flywheel S5)" `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (weekly Sunday at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
Write-Host ""
Write-Host "Dry-run test (safe, writes nothing):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath'"
Write-Host ""
Write-Host "Watch log:" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  $RepoRoot\logs\lexicon-mining-$today.log"
