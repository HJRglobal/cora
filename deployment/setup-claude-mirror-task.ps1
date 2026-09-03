# Setup Windows Scheduled Task: cowork-cora-claude-mirror (S4, claude-workspace
# mirror). Runs scripts/mirror_claude_workspace.py --apply twice daily, keeping the
# out-of-tree Claude knowledge (skills, Cowork task definitions, agent memory)
# mirrored into the Founder OS so the existing static_md sweep ingests it.
#
# Two daily triggers:
#   03:45 AZ  -- ahead of the 04:00 static sweep, so the morning ingest is fresh
#   12:15 AZ  -- the midday freshness pass (finishes long before the 20:30 backup)
#
# WINDOWLESS by construction (D-266..D-269): registered through the shared
# run_hidden helper so no console window flashes on the founder's daily-driver host.
# ASCII-only per D-016. D-005: absolute .venv python.
#
# FIRST APPLY IS HARRISON-RUN (S6): review the dry-run's quarantine + WARNs before
# the first --apply. Register this task AFTER that review -- once registered it
# --apply's on schedule (idempotent, manifest-guarded, reversible via --revert).
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-claude-mirror-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-claude-mirror' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-claude-mirror"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\mirror_claude_workspace.py"

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

# Two daily triggers.
$trigger1 = New-ScheduledTaskTrigger -Daily -At "03:45"
$trigger2 = New-ScheduledTaskTrigger -Daily -At "12:15"

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
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Claude-workspace mirror: copy out-of-tree Claude knowledge (skills, Cowork task defs, agent memory) into the Founder OS for static_md ingest. Deterministic, no LLM, no egress. T0 lane." `
    -Action $action `
    -Trigger @($trigger1, $trigger2) `
    -Principal $principal `
    -Settings $settings | Out-Null

$after = Get-ScheduledTask -TaskName $TaskName
Write-Host "  Registered: $TaskName ($($after.Triggers.Count) triggers: 03:45 + 12:15 AZ)" -ForegroundColor Green
if ($after.Triggers.Count -ne 2) {
    Write-Host "  WARNING: expected 2 triggers, found $($after.Triggers.Count)" -ForegroundColor Red
}
Write-Host ""
Write-Host "Dry-run first (safe, writes nothing):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath'"
Write-Host "Watch log:" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  $RepoRoot\logs\tasks\cowork-cora-claude-mirror-$today.log"
