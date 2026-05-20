# Setup Windows Scheduled Tasks for KB incremental sync (Phase 3E).
#
# Registers three tasks that run nightly to keep the KB fresh:
#   cowork-cora-kb-sync-asana       — 3:00 AM AZ daily
#   cowork-cora-kb-sync-fireflies   — 3:30 AM AZ daily
#   cowork-cora-kb-sync-static      — 4:00 AM AZ daily
#
# Each task runs `uv run python scripts/incremental_sync_<source>.py`, logging
# stdout+stderr to logs/kb-sync-<source>-YYYY-MM-DD.log inside the cora repo.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-kb-sync-tasks.ps1
#
# To remove the tasks later:
#     .\deployment\remove-kb-sync-tasks.ps1
#
# Note: tasks use $env:USERPROFILE-relative paths so they work on any Windows
# user account, but the bot lives at C:\Users\Harri\code\cora — adjust below
# if you move the repo.

$ErrorActionPreference = "Stop"

$RepoRoot = "C:\Users\Harri\code\cora"
$UvExe    = "C:\Users\Harri\AppData\Local\Programs\Python\Python312\Scripts\uv.exe"

# Fallback: try resolving uv from PATH if the hard-coded location is missing
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

# Define the three tasks
$Tasks = @(
    @{
        Name        = "cowork-cora-kb-sync-asana"
        Script      = "scripts\incremental_sync_asana.py"
        HourMin     = "03:00"
        Description = "Cora KB daily incremental sync - Asana tasks + comments + project descriptions"
    },
    @{
        Name        = "cowork-cora-kb-sync-fireflies"
        Script      = "scripts\incremental_sync_fireflies.py"
        HourMin     = "03:30"
        Description = "Cora KB daily incremental sync - Fireflies meeting transcripts"
    },
    @{
        Name        = "cowork-cora-kb-sync-static"
        Script      = "scripts\incremental_sync_static.py"
        HourMin     = "04:00"
        Description = "Cora KB daily incremental sync - Founder OS static markdown (CLAUDE.md, decisions.md, etc.)"
    }
)

foreach ($task in $Tasks) {
    $taskName = $task.Name
    Write-Host ""
    Write-Host "Setting up scheduled task: $taskName" -ForegroundColor Cyan

    # Remove any existing task with the same name
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  Removing existing task..." -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }

    # Build the action: invoke uv via cmd.exe wrapper so output redirection works
    # cd to repo root, run the script, redirect to log file
    $scriptPath = Join-Path $RepoRoot $task.Script
    $cmdArgs = "/c cd /d `"$RepoRoot`" `& `"$UvExe`" run python `"$scriptPath`""
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -WorkingDirectory $RepoRoot -Argument $cmdArgs

    # Trigger: daily at the specified time
    $trigger = New-ScheduledTaskTrigger -Daily -At $task.HourMin

    # Run as current user while logged in (InteractiveToken — no admin required to register,
    # consistent with cowork-cora-service which uses the same logon type).
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Highest

    # Settings: don't run if on batteries, allow start-on-demand, retry once on failure
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 1 `
        -RestartInterval (New-TimeSpan -Minutes 5) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1)

    Register-ScheduledTask `
        -TaskName $taskName `
        -Description $task.Description `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings | Out-Null

    Write-Host "  Registered: $taskName  (runs daily at $($task.HourMin) AZ)" -ForegroundColor Green
}

Write-Host ""
Write-Host "All 3 KB sync tasks registered." -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName 'cowork-cora-kb-sync-*' | Format-Table TaskName, State, NextRunTime"
Write-Host ""
Write-Host "Force a test run for any task:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName 'cowork-cora-kb-sync-asana'"
Write-Host ""
Write-Host "Watch logs at:" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  $RepoRoot\logs\kb-sync-*-$today.log"
