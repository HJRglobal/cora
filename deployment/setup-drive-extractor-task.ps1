# Setup Drive Extractor scheduled task (cowork-cora-drive-extractor)
# Run from ELEVATED Admin PowerShell in C:\Users\Harri\code\cora
#
# Schedule: Daily 4:05 AM AZ + AtLogOn
# Action:   .venv\Scripts\python.exe scripts\run_drive_extractor.py --propose
#
# Runs AFTER the nightly drive_sweep task (3:30 AM) and BEFORE the reconciliation
# task (5:30 AM), so facts are extracted and proposals are queued before Cora's
# morning reconciliation sweep.
#
# ============================ WARNING (audit W4-04) ============================
# The LIVE trigger is 04:05, NOT 04:00. The morning window (03:00-09:00 AZ) was
# de-collided on 2026-06-13 (B1, restagger-morning-tasks-2026-06-13.ps1) so no two
# tasks share a clock minute. This script sets 04:05 to MATCH the live trigger --
# an earlier 04:00 value here would RE-COLLIDE the window if the task is ever
# re-registered. Keep this at 04:05 (or re-run the B1 restagger afterward).
#
# The live task is ALSO env-PAUSED via DRIVE_EXTRACTOR_PROPOSALS_ENABLED=0 in .env
# (RESERVED, ratified D-066). Re-registering the task does NOT touch that flag --
# but if you re-register, confirm .env still carries the pause before relying on it.
# ==============================================================================

$TaskName   = "cowork-cora-drive-extractor"
$RepoRoot   = "C:\Users\Harri\code\cora"
$Python     = "$RepoRoot\.venv\Scripts\python.exe"
$Script     = "$RepoRoot\scripts\run_drive_extractor.py"
$LogDir     = "$RepoRoot\logs"

# Ensure log directory exists
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Write-Host "Registering task: $TaskName" -ForegroundColor Cyan

# Remove existing task if present
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Build action -- absolute paths, no PATH dependency (Task Scheduler doctrine)
$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "scripts\run_drive_extractor.py --propose" `
    -WorkingDirectory $RepoRoot

# Daily at 4:05 AM AZ (MST/no DST = UTC-7). 04:05 = the de-collided B1 slot; do NOT
# revert to 04:00 (would re-collide the morning window -- see WARNING header above).
$TriggerDaily = New-ScheduledTaskTrigger -Daily -At "04:05AM"

# Also fire at logon so it catches up if machine was off at 4am
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn

# Settings: 90-min execution limit, run whether logged on or not
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 90) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false `
    -MultipleInstances IgnoreNew

# Principal: SYSTEM account so it runs even when user is not logged in
$Principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

$Task = New-ScheduledTask `
    -Action $Action `
    -Trigger @($TriggerDaily, $TriggerLogon) `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Extracts structured facts from nightly Drive sweep chunks and queues proposals for Harrison review"

Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null

Write-Host "Task registered." -ForegroundColor Green
Write-Host ""
Write-Host "To test immediately (dry-run):" -ForegroundColor Yellow
Write-Host "  $Python scripts\run_drive_extractor.py --dry-run --propose"
Write-Host ""
Write-Host "To run a 30-day backfill:" -ForegroundColor Yellow
Write-Host "  $Python scripts\run_drive_extractor.py --backfill --propose --lookback-days 30"
Write-Host ""
Write-Host "Task schedule: daily 4:05 AM AZ + AtLogOn" -ForegroundColor Cyan
Write-Host "Runs AFTER drive_sweep (3:30 AM), BEFORE reconciliation (5:30 AM)" -ForegroundColor Cyan
Write-Host "NOTE: live trigger is 04:05 (B1 de-collision). Pause flag DRIVE_EXTRACTOR_PROPOSALS_ENABLED=0 lives in .env (RESERVED)." -ForegroundColor Yellow
