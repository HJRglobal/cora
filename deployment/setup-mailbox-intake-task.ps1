# Setup Windows Scheduled Task: "Cora - Mailbox Intake Sweep" (Code #13 Rider 1
# S-A, cq-8d16f1a557e5). Runs scripts/run_mailbox_intake_sweep.py --apply once a
# day at 06:10 AZ: for every roster row in data/maps/monitored-email-accounts.yaml
# carrying intake_route: knowledge_review (today: cora@hjrglobal.com) it reads new
# Gmail messages via DWD, skips automated senders / Calendar + Fireflies subject
# shapes / non-roster senders IN CODE, and hands each roster-human message to
# info_intake.ingest -- the SAME chokepoint as #info-for-cora -- so it lands as
# ONE PENDING knowledge-review proposal for the 07:00 review DM. Propose-only;
# never autowrite; PHI + LEX-content refused at ingest and again at apply.
#
# WHY 06:10: after the 06:05 #info-for-cora sweep (the ruled 06:20 was moved -- see NOTE)
# and before the 07:00 knowledge review, so a note emailed overnight rides the
# same morning's review DM. NOTE (VERIFY-FIRST 2026-09-19): 06:20 is ALSO the slot
# of cowork-cora-inventory-state-sync (data/maps/scheduled-task-state.yaml). Two
# tasks on one clock time is below the weekly health metric's >2 alarm, but the
# B1 stagger doctrine prefers unique minutes; 06:10 was verified FREE across
# deployment/*.ps1 on 2026-09-19, so the task fires at 06:10 (B1 unique minutes).
# WHY 30 min: one small mailbox, a bounded message cap (100), no model call.
#
# WINDOWLESS by construction (D-266..D-269) via the shared run_hidden helper.
# ASCII-only per D-016. D-005: absolute .venv python. RunLevel Limited.
#
# REGISTRATION IS HARRISON'S HAND: run this from elevated PowerShell after the
# branch is FF-merged. Registering a task = re-run
# deployment\pin-scheduled-task-models.ps1 -Apply afterwards.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-mailbox-intake-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'Cora - Mailbox Intake Sweep' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "Cora - Mailbox Intake Sweep"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_mailbox_intake_sweep.py"
$FireAt     = "06:10"

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

# Windowless action via the shared helper.
. "$PSScriptRoot\_task-action.ps1"
$action = New-WrappedTaskAction -TaskName $TaskName `
    -Execute $PythonExe `
    -Argument "`"$ScriptPath`" --apply" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Daily -At $FireAt

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "cora@ mailbox -> knowledge-review intake sweep (Code #13 Rider 1 S-A). Reads new mail to roster rows with intake_route: knowledge_review via DWD, skips automated/non-roster senders in code, and files each roster-human note as ONE pending knowledge-review proposal through info_intake.ingest (PHI + LEX refused at ingest and at apply). Propose-only, never autowrite. Watermark data/state/mailbox-intake-watermark.json; log logs/mailbox-intake-sweep-<date>.log." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

$after = Get-ScheduledTask -TaskName $TaskName
Write-Host "  Registered: $TaskName (daily $FireAt AZ, limit 30m)" -ForegroundColor Green
Write-Host ""
Write-Host "Dry-run first (safe, reads the mailbox, writes nothing):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath'"
Write-Host "Then re-pin task models:" -ForegroundColor Cyan
Write-Host "  .\deployment\pin-scheduled-task-models.ps1 -Apply"
Write-Host "Watch log:" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  $RepoRoot\logs\tasks\Cora-Mailbox-Intake-Sweep-$today.log"
Write-Host "  $RepoRoot\logs\mailbox-intake-sweep-$today.log"
