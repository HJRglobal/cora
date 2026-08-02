# Registers "Cora - Revops Sweep" -- daily revenue-ops cadence sweep (R1/R3).
#
# REPORT-ONLY posture: the task action carries NO --mode argument, so the sweep
# advances ledger states and writes logs\revops-sweep-<date>.log but sends no
# DMs, drafts nothing, and stages no cards. This is the B2 parallel-run rule
# (design 2026-08-01 section 4): fndr-reply-watch stays the human-facing
# surface until 5 consecutive matching days, then flip this task's argument to
# "--mode stage" and disable fndr-reply-watch (both reversible).
#
# Schedule: daily 10:15 AZ -- outside the 03:00-09:00 stagger window; no other
# cora task holds 10:15 (verified against the registry at build time).
#
# Run from PowerShell (non-elevated is fine):
#   .\deployment\setup-revops-sweep-task.ps1
# Remove:
#   Unregister-ScheduledTask -TaskName "Cora - Revops Sweep" -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$TaskName = "Cora - Revops Sweep"
$ScriptPath = Join-Path $RepoRoot "scripts\run_revops_sweep.py"
$HourMin = "10:15"

if (-not (Test-Path $PythonExe)) { throw "python not found: $PythonExe" }
if (-not (Test-Path $ScriptPath)) { throw "script not found: $ScriptPath" }

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$ScriptPath`"" -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $HourMin
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null

Write-Host "Registered '$TaskName' daily at $HourMin (REPORT-ONLY: no --mode argument)."
Write-Host "Verify:   schtasks /Query /TN `"$TaskName`" /V /FO LIST"
Write-Host "Dry run:  $PythonExe `"$ScriptPath`" --dry-run"
Write-Host "Log:      $RepoRoot\logs\revops-sweep-<date>.log"
Write-Host ""
Write-Host "AFTER the 5-clean-day verifier passes (Harrison): re-register with"
Write-Host "  -Argument '`"$ScriptPath`" --mode stage'  and disable fndr-reply-watch."
