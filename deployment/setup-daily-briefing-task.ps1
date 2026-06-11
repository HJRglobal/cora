# setup-daily-briefing-task.ps1
# Registers the Windows Task Scheduler task for the org-roles-driven daily
# briefing (Org Synthesis Phase 2, deliverable 2) at 7:30am AZ, weekdays.
#
# ROLLOUT DOCTRINE (locked 2026-06-11): digest-to-Harrison-first.
#   Default registration runs the script with NO flags -> digest mode:
#   Harrison gets ONE DM containing every user's would-be briefing; no
#   per-user DMs are sent. After Harrison's explicit go, re-register with
#   -SendUsers to flip on per-user delivery.
#
# Run once from an elevated PowerShell prompt:
#   Set-ExecutionPolicy RemoteSigned -Scope Process
#   .\deployment\setup-daily-briefing-task.ps1            # digest mode (default)
#   .\deployment\setup-daily-briefing-task.ps1 -SendUsers # per-user delivery
#
# Prerequisites:
#   1. ASANA_PAT, ANTHROPIC_API_KEY, and SLACK_BOT_TOKEN are set in .env.
#   2. data/maps/org-roles.yaml is the briefing roster (D-044). The old
#      role-briefing-config.yaml is RETIRED -- do not recreate it.
#
# To run immediately (for testing):
#   Start-ScheduledTask -TaskName "Cora - Daily Briefing"
#
# Smoke test (check last 10 lines of the audit log):
#   Get-Content "C:\Users\Harri\code\cora\logs\cora-daily-briefing.jsonl" -Tail 10

param(
    [switch]$SendUsers
)

$TaskName   = "Cora - Daily Briefing"
$RepoRoot   = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $RepoRoot "scripts\run_daily_briefing.py"
$PythonPath = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found: $ScriptPath"
    exit 1
}
if (-not (Test-Path $PythonPath)) {
    Write-Error "Venv python not found: $PythonPath  (run 'uv sync' first)"
    exit 1
}

# Remove existing task if present
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

# Action: run via venv python (Task Scheduler has no user PATH; D-005)
$ScriptArgs = "`"$ScriptPath`""
$Mode = "digest-only (review DM to Harrison)"
if ($SendUsers) {
    $ScriptArgs = "`"$ScriptPath`" --send-users"
    $Mode = "per-user delivery (--send-users)"
}

$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument $ScriptArgs `
    -WorkingDirectory $RepoRoot

# Trigger: 7:30am AZ, weekdays only (AZ is UTC-7 year-round, no DST)
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At "7:30am"

# Settings: stop if it runs > 10 minutes; start if missed while machine was off
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries:$false

# Run as the current user (has access to .env + venv)
$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force `
    -ErrorAction Stop | Out-Null

Write-Host ""
Write-Host "Task registered: '$TaskName'" -ForegroundColor Green
Write-Host "  Mode:        $Mode"
Write-Host "  Schedule:    Weekdays at 7:30am AZ"
Write-Host "  Python:      $PythonPath"
Write-Host "  Script:      $ScriptPath"
Write-Host "  Working dir: $RepoRoot"
Write-Host ""
Write-Host "To run immediately: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To review output:   Get-Content '$RepoRoot\logs\cora-daily-briefing.jsonl' -Tail 20"
