# Setup Windows Scheduled Task: Cora AI-Visibility weekly scan, Monday 10:15am AZ.
#
# Runs the frozen prompt basket across the grounded AI-search engines + the
# Otterly Google-AI-Overviews slice, scores F3 Energy / Pure / Mood 0-100, and
# posts the weekly score card to #f3-ai-visibility. Mon 10:15 AZ is outside the
# crowded 03:00-09:00 sync window and does not collide with the Mon 10:30 finance
# receipt digest. The script self-bounds on wall-clock (--time-budget-min, default
# 100); the task ExecutionTimeLimit (2h) is only the backstop.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-ai-visibility-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-ai-visibility-scan' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-ai-visibility-scan"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_ai_visibility_scan.py"
$HourMin    = "10:15"

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
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $HourMin

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
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Cora AI-Visibility weekly scan: score F3 Energy/Pure/Mood 0-100 across AI search engines + Google AI Overviews, post the card to #f3-ai-visibility" `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (runs weekly Monday at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
Write-Host ""
Write-Host "Dry-run test (safe, zero API calls):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath' --dry-run"
Write-Host ""
Write-Host "Watch log:" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  $RepoRoot\logs\ai-visibility-scan-$today.log"
