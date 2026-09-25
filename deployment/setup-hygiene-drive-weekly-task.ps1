# Setup Windows Scheduled Task: cowork-cora-hygiene-drive-weekly (Code #15 R8,
# cq-592baba613f1). Runs scripts/run_hygiene_drive_weekly.py --apply once a week:
# a NO-HASH, read-only inventory of the whole Founder-OS tree through the VENDORED
# folder-audit PS1 (deployment\hygiene\folder-audit-inventory.ps1, sha256-pinned),
# then a week-over-week report at
#   G:\My Drive\HJR-Founder-OS\_shared\hygiene-pending-moves\YYYY-MM-DD_fndr_hygiene-findings.md
# plus a desktop.ini DELETE manifest and a full-list sidecar in
#   C:\Users\Harri\Downloads\hjr-folder-audit
# and a keep-4 rotation of the runner's OWN inventory stamps there.
#
# WHEN: Saturday 02:40 AZ. A unique minute (no other task claims 02:40; checked
# against deployment\manifest\task-estate.md 2026-09-24), before the 03:00 syncs and
# far ahead of the ~08:30 Cowork hygiene-drive read that publishes the Notion page.
#
# WINDOWLESS by construction (D-266..D-269): registered through the shared
# run_hidden helper; the runner also spawns powershell with CREATE_NO_WINDOW.
# ASCII-only per D-016. D-005: absolute .venv python. Script-side only: no restart.
#
# HARRISON REGISTERS THIS (never a Code session). Dry-run first, from a normal
# (non-elevated) PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\.venv\Scripts\python.exe scripts\run_hygiene_drive_weekly.py
# then register from elevated PowerShell:
#     .\deployment\setup-hygiene-drive-weekly-task.ps1
# and regenerate the DR task-estate manifest afterwards (the Monday digest shows
# the new task as drift until then):
#     .\.venv\Scripts\python.exe scripts\generate_task_estate_manifest.py --update-docs
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-hygiene-drive-weekly' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-hygiene-drive-weekly"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_hygiene_drive_weekly.py"
$Inventory  = "C:\Users\Harri\code\cora\deployment\hygiene\folder-audit-inventory.ps1"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at $PythonExe. Check the venv."
    exit 1
}
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found at $ScriptPath."
    exit 1
}
if (-not (Test-Path $Inventory)) {
    Write-Error "Vendored inventory PS1 not found at $Inventory."
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

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At "02:40"

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Weekly Founder-OS Drive hygiene: NO-HASH read-only inventory (vendored folder-audit PS1) + week-over-week report into _shared\hygiene-pending-moves. Deterministic, no LLM, no egress. T0 instrument." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

$after = Get-ScheduledTask -TaskName $TaskName
Write-Host "  Registered: $TaskName ($($after.Triggers.Count) trigger: Saturday 02:40 AZ)" -ForegroundColor Green
if ($after.Triggers.Count -ne 1) {
    Write-Host "  WARNING: expected 1 trigger, found $($after.Triggers.Count)" -ForegroundColor Red
}
Write-Host ""
Write-Host "Dry-run (safe, writes nothing):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath'"
Write-Host "Watch log:" -ForegroundColor Cyan
Write-Host "  $RepoRoot\logs\tasks\cowork-cora-hygiene-drive-weekly-<YYYY-MM-DD>.log"
