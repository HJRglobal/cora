# Setup Windows Scheduled Task: Cora weekly finance SOP adherence check (A1-A3).
#
# Deterministic, read-only file checks. Emits a facts block consumed by:
#   - scripts/run_finance_close_pack.py   (Mon 09:00 AZ, close-prep notes)
#   - the Cowork weekly finance review    (Mon 13:00 AZ, Section 1)
#
# Checks:
#   A1  cash-sheet freshness -- the REAL live Standing ACTUALS sheet, <=7d old
#       (deliberately NOT the SOP's _LIVE-named set, which was never migrated)
#   A2  Clover export        -- RETIRED; one static lane_retired fact, no alarms
#   A3  monthly filing presence + per-entity bank-statement freshness
#
# Outputs:
#   data\state\finance-adherence-facts.json
#   G:\My Drive\HJR-Founder-OS\01-HJR-Global\accounting\finance-adherence-facts.md
#
# Schedule: Monday 08:15 AZ. The facts block must exist BEFORE both consumers run
# (09:00 close pack, 13:00 weekly review), so both read facts computed that morning.
# Slot check (2026-08-04): 08:15 is free -- the Fireflies coverage monitor is 08:10
# and the finance weekly recap is 07:30.
#
# NOTE: --post-summary is deliberately OMITTED from the registered action. The
# summary line is finance-safe (no dollar figure by construction) but the facts
# already reach #hjrg-finance inside the 09:00 close pack, so posting here too
# would double up. Add the flag only if the review task needs an earlier signal.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-finance-adherence-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-finance-adherence' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-finance-adherence"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_finance_adherence_check.py"
$HourMin    = "08:15"

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

# D-005: absolute .venv python + absolute script path + WorkingDirectory = repo root.
$Action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument "scripts\run_finance_adherence_check.py" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $HourMin

# All reads are drive_io-bounded, so the job cannot hang on a G: blip. 20 minutes
# is a generous backstop for ~16 checks over a few hundred bounded stats.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Weekly finance SOP adherence check (A1-A3): cash-sheet freshness, Clover lane retired, monthly filing presence, per-entity bank-statement freshness. Read-only; deterministic facts block for the close pack and the weekly finance review. No model call." `
    | Out-Null

Write-Host "  Registered: $TaskName (Monday $HourMin)" -ForegroundColor Green

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "State:    $($task.State)"
Write-Host "Next run: $($info.NextRunTime)"
Write-Host ""
Write-Host "Preview any time (non-elevated is fine, writes nothing):" -ForegroundColor Cyan
Write-Host "  .venv\Scripts\python.exe scripts\run_finance_adherence_check.py --dry-run"
