# Setup Windows Scheduled Task: Cora knowledge-review Mon-Fri 7am AZ.
#
# Reads cora-proposed-memory-updates.jsonl for PENDING items, processes any
# Harrison reactions from cora-reply-log.jsonl, and DMs Harrison a batch
# summary of pending items for 👍/👎 approval.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-knowledge-review-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-knowledge-review' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot  = "C:\Users\Harri\code\cora"
$UvExe     = "C:\Users\Harri\AppData\Local\Programs\Python\Python312\Scripts\uv.exe"
$TaskName  = "cowork-cora-knowledge-review"
$Script    = "scripts\run_knowledge_review.py"
$HourMin   = "07:00"

# Fallback: try resolving uv from PATH
if (-not (Test-Path $UvExe)) {
    $uvFromPath = (Get-Command uv -ErrorAction SilentlyContinue).Source
    if ($uvFromPath) {
        $UvExe = $uvFromPath
        Write-Host "Resolved uv from PATH: $UvExe"
    } else {
        Write-Error "uv.exe not found at $UvExe or in PATH. Install uv or adjust the script."
        exit 1
    }
}

Write-Host "Setting up scheduled task: $TaskName" -ForegroundColor Cyan

# Remove existing task if present
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Removing existing task..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$scriptPath = Join-Path $RepoRoot $Script
$cmdArgs    = "/c cd /d `"$RepoRoot`" `& `"$UvExe`" run python `"$scriptPath`""
$action     = New-ScheduledTaskAction -Execute "cmd.exe" -WorkingDirectory $RepoRoot -Argument $cmdArgs

# Mon-Fri only at 7:00 AM local time (machine is set to AZ timezone)
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
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
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Cora knowledge-review: process Harrison reactions + DM pending updates for approval" `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName  (runs Mon-Fri at $HourMin AZ)" -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State, NextRunTime"
Write-Host ""
Write-Host "Force a test run:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "Watch log:" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  $RepoRoot\logs\knowledge-review-$today.log"
