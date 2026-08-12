# Setup Windows Scheduled Task: Cora daily personalized knowledge check, Mon-Fri 08:05 AZ.
#
# Sends ONE grounded, personalized question to each person in the pilot roster
# (data\maps\knowledge-check-roster.yaml). The CAPTURE / CONFIRM / PROMOTE half
# runs in the always-on bot process, not here.
#
# RUN ORDER MATTERS. 08:05 is chosen to sit AFTER cowork-cora-gap-autofill
# (06:00) and after the 07:00 knowledge-review DM batch. Tier-2 questions are
# claimed out of gap_autofill's own state ledger, so letting those jobs take
# their picks first keeps the flows from asking one gap of two people, and keeps
# two writers off that ledger at the same time. It is also outside the crowded
# 03:00-07:30 window, so it does not collide with the morning task stagger.
#
# The run staggers its sends across ~45 minutes so 13 DMs do not land on one
# timestamp, which is why ExecutionTimeLimit is 2 hours and not the usual 1.
#
# NOTHING SENDS until CORA_KNOWLEDGE_CHECK is set to 'on' in .env. Registering
# this task early is safe: with the flag off (the default) the run logs and exits.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-knowledge-check-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'Cora - Knowledge Check' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "Cora - Knowledge Check"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_knowledge_check.py"
$HourMin    = "08:05"

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

# Weekdays only. The script also refuses to run at a weekend, so the two agree.
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At $HourMin

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Cora daily knowledge check: one grounded personalized question per weekday to the pilot roster (gated on CORA_KNOWLEDGE_CHECK)" `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (Mon-Fri at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
Write-Host ""
Write-Host "Pre-flight, in order (all safe -- no DMs, no writes):" -ForegroundColor Cyan
Write-Host "  1. Dry-run the plan:"
Write-Host "     & '$PythonExe' '$ScriptPath' --dry-run --no-stagger"
Write-Host "  2. Confirm all 13 have a working DM path:"
Write-Host "     & '$PythonExe' '$ScriptPath' --check-reachability"
Write-Host "  3. Dogfood the full loop on Harrison only (SENDS one DM to Harrison):"
Write-Host "     Set CORA_KNOWLEDGE_CHECK=on in .env, restart Cora, then:"
Write-Host "     & '$PythonExe' '$ScriptPath' --dogfood --no-stagger"
Write-Host ""
Write-Host "Participation report (feeds Hannah's Monday audit):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath' --report"
Write-Host ""
Write-Host "Watch log:" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  $RepoRoot\logs\knowledge-check-$today.log"
