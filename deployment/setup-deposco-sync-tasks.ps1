# Setup Windows Scheduled Tasks: Deposco V1 Phase-1 read-only warehouse feed.
#
# DO NOT RUN THIS UNTIL THE PHASE-1 GATE CLEARS. Two preconditions, both real:
#
#   1. DEPOSCO_PROD_USER and DEPOSCO_PROD_PASS are in C:\Users\Harri\code\cora\.env.
#      Without them every run exits 2 immediately -- it cannot even build a client.
#   2. The figures have reconciled against the warehouse UI AND the manual weekly
#      inventory Sheet on two consecutive weekly checks (the design's own gate).
#
# Until then, run the pre-flight by hand instead -- it writes nothing:
#     .venv\Scripts\python.exe scripts\run_deposco_smoke.py --env prod
#     .venv\Scripts\python.exe scripts\run_deposco_inventory_sync.py --dry-run
#
# Both tasks are READ-ONLY against Deposco. The client they use is GET-only by
# construction -- it has no method that can mutate anything in the warehouse
# system -- so a scheduled run cannot move stock or push an order.
#
# TASK 1  cowork-cora-deposco-inventory-sync   daily 06:22 AZ
#   Writes exactly ONE file, one writer one file (D-102):
#     G:\My Drive\HJR-Founder-OS\02-F3-Energy\inventory-state\f3e-inventory-deposco.json
#   Slot: 06:22 is unused on the live registry (2026-08-14). It sits just after its
#   sibling cowork-cora-inventory-state-sync (06:20) and, critically, BEFORE the
#   F3E daily synthesis at 06:33 -- the consumer of this file. A 07:xx producer
#   would have left every synthesis reading a ~24h-old file. Unique within the
#   03:00-09:00 window, so the weekly health metric gains no collision alarm.
#   Exit codes: 0 = every known SKU read; 1 = partial (written, gaps named);
#               2 = failure, nothing written, previous file left with its own stamp.
#
# TASK 2  cowork-cora-deposco-lot-ledger       daily 07:45 AZ
#   Writes data\state\deposco-lot-ledger.json (local, gitignored, backed up by
#   backup_logs.py). Deliberately OUTSIDE the crowded 06:00-07:30 band: nothing
#   consumes the ledger on a schedule, so it has no reason to compete for that
#   window. 07:45 verified free.
#   Exit codes: 0 = clean; 1 = written with reconciliation flags; 2 = failure.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-deposco-sync-tasks.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-deposco-inventory-sync' -Confirm:$false
#     Unregister-ScheduledTask -TaskName 'cowork-cora-deposco-lot-ledger' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot  = "C:\Users\Harri\code\cora"
$PythonExe = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at $PythonExe. Check the venv."
    exit 1
}

# Pre-flight: refuse to register tasks that provably cannot work. Reads only the
# KEY NAMES out of .env -- no value is read, printed, or logged.
$EnvPath = Join-Path $RepoRoot ".env"
if (Test-Path $EnvPath) {
    $keys = Get-Content $EnvPath | Where-Object { $_ -match "^\s*DEPOSCO_PROD_(USER|PASS)\s*=\s*\S" }
    if ($keys.Count -lt 2) {
        Write-Host ""
        Write-Host "REFUSING TO REGISTER: DEPOSCO_PROD_USER / DEPOSCO_PROD_PASS are not both" -ForegroundColor Red
        Write-Host "set in .env. Every scheduled run would exit 2 without doing anything." -ForegroundColor Red
        Write-Host "Add them, then re-run this script." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Error "No .env at $EnvPath."
    exit 1
}

$Tasks = @(
    @{
        Name   = "cowork-cora-deposco-inventory-sync"
        Script = "scripts\run_deposco_inventory_sync.py"
        At     = "06:22"
        Limit  = 20
        Desc   = "Daily F3E warehouse (3PL) inventory sync: per-SKU on-hand / ATP / on-PO / in-transit per facility, written to the cross-channel inventory store. READ-ONLY, deterministic, no model call. One writer, one file. A failed or empty read writes nothing and leaves the previous file with its own timestamp."
    },
    @{
        Name   = "cowork-cora-deposco-lot-ledger"
        Script = "scripts\run_deposco_lot_ledger.py"
        At     = "07:45"
        Limit  = 20
        Desc   = "Daily F3E lot ledger: per-lot receipts with expiry, tied out against warehouse on-hand as two independent computations. READ-ONLY. Discrepancies are flagged, never absorbed. Note: Deposco V1 exposes lot on receipts only, so per-lot depletion is not computed."
    }
)

foreach ($t in $Tasks) {
    Write-Host "Setting up scheduled task: $($t.Name)" -ForegroundColor Cyan

    $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  Removing existing task..." -ForegroundColor Yellow
        try { Stop-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue } catch {}
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
    }

    # D-005: absolute .venv python + relative script path + WorkingDirectory = repo root.
    $Action = New-ScheduledTaskAction -Execute $PythonExe `
        -Argument $t.Script `
        -WorkingDirectory $RepoRoot

    $Trigger = New-ScheduledTaskTrigger -Daily -At $t.At

    # Paced reads (~0.6s between requests) over a paginated endpoint plus one
    # bounded write. 20 minutes is a generous backstop; the script self-bounds.
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes $t.Limit)

    Register-ScheduledTask -TaskName $t.Name `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description $t.Desc `
        | Out-Null

    $info = Get-ScheduledTaskInfo -TaskName $t.Name
    Write-Host "  Registered: $($t.Name) (daily $($t.At)); next run $($info.NextRunTime)" -ForegroundColor Green
}

Write-Host ""
Write-Host "Both tasks registered. Preview any time (writes nothing):" -ForegroundColor Cyan
Write-Host "  .venv\Scripts\python.exe scripts\run_deposco_inventory_sync.py --dry-run"
Write-Host "  .venv\Scripts\python.exe scripts\run_deposco_lot_ledger.py --dry-run"
Write-Host ""
Write-Host "Reminder: the F3E synthesis line and the f3e_warehouse_inventory tool stay" -ForegroundColor Yellow
Write-Host "DARK until CORA_DEPOSCO_WAREHOUSE_LINE is set. Registering these tasks only" -ForegroundColor Yellow
Write-Host "starts producing the data; it does not surface it to anyone." -ForegroundColor Yellow
