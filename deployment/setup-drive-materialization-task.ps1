# Setup Windows Scheduled Task: Cora Drive materialization, daily 05:45 AZ.
#
# Distills the day's NEW swept KB chunks into Drive _brain/swept/{ENTITY}/YYYY-MM-DD.md
# so a Drive-reading frontend (Tag) answers from swept knowledge, not just curated facts.
# Reads the LOCAL cora_kb.db (metadata SELECT only -- no vector search, no rebuild, no
# connector re-fetch). LEX is PHI-walled (LBHS excluded, GM-level scrubbed, dropped if
# PHI survives the scrub). NOT bot-loaded -- runs as its own task; no Cora restart needed.
#
# Slot: 05:45 AZ daily. AFTER the kb-sync sweeps that produce the chunks it reads
# (gmail 02:30, asana 03:00, fireflies 03:30, drive 04:30, notion 05:00) and after
# reconciliation 05:30; BEFORE proactive-gaps 06:00 / gap-autofill 06:10 / knowledge
# review 07:00. 05:45 is a verified-free minute per the scheduler de-collide doctrine
# (B1 2026-06-13); confirm against the live schedule before changing -HourMin.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-drive-materialization-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'Cora - Drive Materialization' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "Cora - Drive Materialization"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_drive_materialization.py"
$HourMin    = "05:45"

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
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Cora Drive materialization: distill the day's swept KB chunks to Drive _brain/swept/{ENTITY}/YYYY-MM-DD.md for a Drive-reading frontend. Reads local KB only; LEX PHI-walled (LBHS excluded, GM-level scrubbed)." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (daily at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
Write-Host ""
Write-Host "Dry-run test (safe, writes nothing to Drive, no watermark advance):" -ForegroundColor Cyan
Write-Host "  & '$PythonExe' '$ScriptPath' --dry-run"
