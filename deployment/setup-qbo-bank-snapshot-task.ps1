# Setup Windows Scheduled Task: Cora daily QBO bank snapshot (A5 S1).
#
# Read-only sweep of all 11 provisioned QBO realms for ACTIVE Bank / Credit Card
# account balances plus the newest posted bank-side transaction date.
#
# Outputs:
#   data\state\qbo-bank-latest.json
#   G:\My Drive\HJR-Founder-OS\01-HJR-Global\accounting\live-snapshots\qbo-bank-latest.json
#
# Consumers:
#   scripts\run_finance_close_pack.py   (Mon 09:00 AZ, "QBO bank and books freshness")
#
# BASIS WARNING (D-116): balances here are ACCOUNT REGISTER figures from the QBO
# query API. They are a DIFFERENT measure from the BalanceSheet-report figures the
# close pack's cash section reads, and were verified on 2026-08-04 to differ
# materially -- including opposite signs on the same account (BDM "Big D Media
# Chase": register +11,758.94 vs report -8,483.22). A gap between the two is NOT
# a reconciliation break.
#
# Schedule: daily 07:05 AZ.
# Slot check against the LIVE registry (2026-08-04): 07:05 is unused. Neighbours are
# cowork-cora-knowledge-review 07:00 and "Cora - LEX Swept PHI Check" 07:06. 07:05
# is inside the 03:00-09:00 window the weekly health metric watches, but it is
# unique there, so max-concurrent stays 1 against a threshold of >2 -- no new alarm.
#
# Exit codes: 0 = all realms clean; 1 = at least one realm errored (snapshot still
# written, those realms marked UNKNOWN); 2 = total failure, previous snapshot left
# in place. Task Scheduler surfaces these as Last Result.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-qbo-bank-snapshot-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-bank-snapshot' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-bank-snapshot"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_qbo_bank_snapshot.py"
$HourMin    = "07:05"

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

# D-005: absolute .venv python + relative script path + WorkingDirectory = repo root.
$Action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument "scripts\run_qbo_bank_snapshot.py" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -Daily -At $HourMin

# ~55 QBO reads (accounts + 5 freshness queries x 11 realms), each a sub-second
# HTTPS GET. 20 minutes is a generous backstop that still bounds a hung realm.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Daily QBO bank snapshot: ACTIVE Bank/Credit Card register balances plus newest posted bank-side txn date across all provisioned realms. Read-only, deterministic, no model call. Feeds the Monday close pack. Balances are ACCOUNT REGISTER figures, not BalanceSheet-report figures." `
    | Out-Null

Write-Host "  Registered: $TaskName (daily $HourMin)" -ForegroundColor Green

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "State:    $($task.State)"
Write-Host "Next run: $($info.NextRunTime)"
Write-Host ""
Write-Host "Preview any time (non-elevated is fine, writes nothing):" -ForegroundColor Cyan
Write-Host "  .venv\Scripts\python.exe scripts\run_qbo_bank_snapshot.py --dry-run"
