# Setup Windows Scheduled Task: Cora daily F3E inventory-state sync (A5 Part 2).
#
# Read-only against the DTC store. Writes exactly ONE file in the cross-channel
# inventory store -- one writer, one file (D-102), so no two processes ever write
# the same file:
#
#   G:\My Drive\HJR-Founder-OS\02-F3-Energy\inventory-state\f3e-inventory-shopify.json
#
# The other two store files belong to Cowork tasks (the Mon+Thu channel sweep and
# the daily manual-count transcription) and are NOT touched here.
#
# Covers four locations, each labelled with what it actually is:
#   office      81567023424   1337 S Gilbert Rd -- manually managed
#   dtc_3pl    110064533824   Nimbl -- real-time 3PL sync, the canonical DTC number
#   unis        98823012672   UNIS (Cotton) -- fed by a WEEKLY upstream batch
#   tiktok_fbt 111242608960   marketplace-managed MIRROR; can drift from Seller Center
# All four verified live 2026-08-04.
#
# Consumers: the f3e_channel_inventory Cora tool, the F3E daily synthesis
# cross-channel line, and the F3E ecom brief.
#
# Schedule: daily 06:20 AZ -- BEFORE its consumers, which is the whole point.
#
# The design suggested ~07:10, and a first cut used 07:20. Both are WRONG: the F3E
# daily synthesis (the live consumer of this file) fires at 06:33, so a 07:xx
# producer would have left every synthesis reading a ~24h-old file, and the very
# first one reading no file at all. 06:20 puts the producer ahead of the consumer.
#
# Slot check against the LIVE registry (2026-08-05): 06:20 is unused. Neighbours are
# cowork-cora-gap-autofill 06:10 and cowork-cora-founders-os-sweep 06:30. Unique
# within the 03:00-09:00 window, so the weekly health metric gains no alarm.
# (07:10 is separately held by the Disabled "Cora - F3E Daily Ecom Brief" -- reusing
# it would have created a latent collision if that task is ever re-enabled.)
#
# Exit codes: 0 = every location read; 1 = at least one location failed (file still
# written, those channels UNKNOWN); 2 = total failure, previous file left in place.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-inventory-state-sync-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-inventory-state-sync' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-inventory-state-sync"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_inventory_state_sync.py"
$HourMin    = "06:20"

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
# Windowless action: route the command through run_hidden.py under pythonw.exe
. "$PSScriptRoot\_task-action.ps1"
$Action = New-WrappedTaskAction -TaskName $TaskName -Execute $PythonExe `
    -Argument "scripts\run_inventory_state_sync.py" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -Daily -At $HourMin

# Four paginated inventory reads plus one bounded Drive write. 15 minutes is a
# generous backstop; drive_io already timeout-bounds the mount write.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Daily F3E inventory-state sync: per-SKU units at office / DTC 3PL / UNIS / TikTok FBT mirror, written to the cross-channel inventory store. Read-only, deterministic, no model call. One writer, one file." `
    | Out-Null

Write-Host "  Registered: $TaskName (daily $HourMin)" -ForegroundColor Green

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "State:    $($task.State)"
Write-Host "Next run: $($info.NextRunTime)"
Write-Host ""
Write-Host "Preview any time (non-elevated is fine, writes nothing):" -ForegroundColor Cyan
Write-Host "  .venv\Scripts\python.exe scripts\run_inventory_state_sync.py --dry-run"
