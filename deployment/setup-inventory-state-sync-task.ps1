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
# Schedule: daily 07:20 AZ.
# Slot check against the LIVE registry (2026-08-04): 07:20 is unused. NOTE 07:10 was
# the design's suggested minute but is held by "Cora - F3E Daily Ecom Brief" -- that
# task is currently Disabled (folded into the F3E daily synthesis 2026-07-07), and
# reusing its minute would create a latent collision if it is ever re-enabled.
# Neighbours: cowork-cora-decision-capture 07:15, "Cora - Daily Briefing" 07:30.
# Unique within the 03:00-09:00 window, so the weekly health metric gains no alarm.
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
$HourMin    = "07:20"

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
