# Setup Windows Scheduled Task: Cora daily F3E synthesis, 06:33 AZ.
#
# Part of the daily leadership-channel synthesis (2026-07-07 build spec) -- the
# operational sibling of the weekly Harrison-only strategy memo. Posts the daily
# F3E synthesis to #f3e-leadership.
#
# Standalone script (D-047): the runner imports channel_synthesis / strategy_memo
# only, never app.py / tool_dispatch / claude_client -- NO bot restart is needed.
# The post routes through the egress boundary; the TIER_1 channel allowlist is the
# financial-firewall gate.
#
# Slot check (2026-07-07, verified against the live registry): 06:33 AZ is a free
# clock minute in the 03:00-09:00 morning window (occupied minutes: 06:00 / 06:10 /
# 06:30 / 06:40 / 06:45 / 06:50 / 07:00 / 07:06 / 07:10 / 07:15 / 07:30). The 9
# daily-synthesis tasks each use a distinct free minute (06:31/33/35/37/39/52/54/
# 56/58). During the parallel-run these sit just after Tag's ~6:17-6:53 fires.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-daily-synthesis-f3e-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'Cora - Daily Synthesis (F3E)' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "Cora - Daily Synthesis (F3E)"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_entity_synthesis.py"
$HourMin    = "06:33"

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

# D-005: absolute .venv python + absolute script path + WorkingDirectory
$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$ScriptPath`" --entity f3e" `
    -WorkingDirectory $RepoRoot

# -At is LOCAL (Arizona) time -- put the AZ minute in directly, no UTC conversion.
$trigger = New-ScheduledTaskTrigger -Daily -At $HourMin

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
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "F3 Energy operational synthesis (cash, retail pipeline, DTC/subs/paid/inventory/production ecom fold, deadlines) to #f3e-leadership; source-opaque" `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (daily at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
Write-Host ""
Write-Host "Dry-run test (safe, no post, no snapshot):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath' --entity f3e --dry-run"
