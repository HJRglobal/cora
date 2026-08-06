# Setup Windows Scheduled Task: weekly 13-week cashflow QBO ACTUALS
# (13WCF shadow ledger, S2 / M2).
#
# Read-only pull of each provisioned QBO realm's BANK-CASH activity for two
# windows, banked beside the M1 forecast snapshots.
#
#   PRELIMINARY  the week that just ended. QBO is structurally INCOMPLETE for it
#                at this hour -- the bank feed has not downloaded Friday through
#                Sunday. Stamped posted-through per realm; NEVER used for
#                comparison or forecast-accuracy math.
#   FINALIZED    a re-pull of the week before that, now matured in QBO. It
#                supersedes its own preliminary file. Comparison and accuracy
#                consumers bind here and nowhere else.
#
# Outputs:
#   data\state\cashflow-ledger\actuals\YYYY-MM-DD_prelim-actuals.json
#   data\state\cashflow-ledger\actuals\YYYY-MM-DD_final-actuals.json
#   G:\My Drive\HJR-Founder-OS\01-HJR-Global\accounting\cashflow-ledger\actuals\
#
# THE CASH PERIMETER: a cash event is a transaction touching a BANK account. A
# card purchase is not one; the bank-to-card PAYMENT is. Card spend is excluded
# by construction and the map loader refuses to point a credit-card liability
# account at an expense category.
#
# Nothing here writes to the sheet, ever (A5 lock). HR LLC (personal books) and
# the cash-less OSN shell are excluded at COLLECTION by the map loader.
#
# Schedule: weekly MONDAY 06:25 AZ -- 10 minutes after the S1 forecast snapshot,
# whose banked week grid tells this job what "week ending" means. If S1 has never
# run, this job REFUSES rather than assuming a Friday week.
#
# SLOT CHECK against the LIVE registry (verified 2026-08-06): 06:25 is unused.
# Neighbours are cowork-cora-cashflow-forecast-snapshot 06:15,
# cowork-cora-inventory-state-sync 06:20 and cowork-cora-gap-autofill 06:10. This
# job is heavier than S1 (about 10 read-only QBO calls per realm per window) and
# ran roughly 4 minutes end-to-end over 9 realms in the 8/06 live dry-run, so it
# may still be running at 06:30 -- the neighbours are independent scripts with no
# shared lock or DB, so overlap is harmless. It sits inside the 03:00-09:00 window
# the weekly health metric watches but is unique at its minute, so max-concurrent
# stays 1 against a threshold of >2 -- no new alarm.
#
# Exit codes: 0 = every realm read cleanly; 1 = at least one realm went UNKNOWN or
# a tie-out failed (the windows are still written); 2 = total failure or a refused
# write, previous files left in place. Task Scheduler surfaces these as Last Result.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-cashflow-actuals-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-cashflow-actuals' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-cashflow-actuals"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_cashflow_actuals.py"
$HourMin    = "06:25"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at $PythonExe. Check the venv."
    exit 1
}
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found at $ScriptPath."
    exit 1
}

Write-Host "Setting up scheduled task: $TaskName" -ForegroundColor Cyan

# Guard the slot: refuse to stack onto a minute another Cora task already owns.
# The M1 build found the design's stated slot already taken, so this is checked
# against the LIVE registry rather than trusted from a document.
$collisions = @()
foreach ($t in (Get-ScheduledTask | Where-Object { $_.TaskName -like "*cora*" -and $_.TaskName -ne $TaskName })) {
    $nextRun = (Get-ScheduledTaskInfo -TaskName $t.TaskName -ErrorAction SilentlyContinue).NextRunTime
    if ($nextRun -and $nextRun.ToString("HH:mm") -eq $HourMin) {
        $collisions += $t.TaskName
    }
}
if ($collisions.Count -gt 0) {
    Write-Error "Slot $HourMin is already used by: $($collisions -join ', '). Pick a free minute and update `$HourMin."
    exit 1
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Removing existing task..." -ForegroundColor Yellow
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# D-005: absolute .venv python + relative script path + WorkingDirectory = repo root.
$Action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument "scripts\run_cashflow_actuals.py" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $HourMin

# About 10 read-only QBO calls per realm per window across 9 realms; roughly 4
# minutes observed end-to-end. 30 minutes bounds a hung read without killing a
# slow-but-working run. StartWhenAvailable matters: if the host is asleep at 06:25
# the run fires late rather than being skipped, and a late pull is still correct --
# unlike the forecast snapshot, these figures do not decay.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Weekly 13WCF QBO actuals (shadow ledger S2). Monday 06:25 AZ, after the S1 forecast snapshot. Pulls a PRELIMINARY just-ended week plus a FINALIZED re-pull of the week before, per QBO realm, bank-accounts-only cash perimeter. Read-only, deterministic, no model call. Never writes to the sheet. HR LLC and the OSN shell excluded at collection. Comparison and accuracy consumers bind to the FINALIZED window only." `
    | Out-Null

Write-Host "  Registered: $TaskName (weekly Monday $HourMin)" -ForegroundColor Green

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "State:    $($task.State)"
Write-Host "Next run: $($info.NextRunTime)"
Write-Host ""
Write-Host "Preview any time (non-elevated is fine, writes nothing):" -ForegroundColor Cyan
Write-Host "  .venv\Scripts\python.exe scripts\run_cashflow_actuals.py --dry-run"
Write-Host ""
Write-Host "BEFORE THE FIGURES MEAN ANYTHING -- Justin confirms the entity map." -ForegroundColor Yellow
Write-Host "Until then every realm renders UNCONFIRMED and feeds no comparison." -ForegroundColor Yellow
Write-Host "Populate the category map from live activity first:" -ForegroundColor Yellow
Write-Host "  .venv\Scripts\python.exe scripts\run_cashflow_actuals.py --discover"
Write-Host "  .venv\Scripts\python.exe scripts\run_cashflow_actuals.py --discover --apply"
