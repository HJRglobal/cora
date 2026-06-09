# Setup Windows Scheduled Tasks for KB incremental sync (Phase 3E + Phase 4 Layer 2 + Notion).
#
# Registers the nightly KB-freshness tasks:
#   cowork-cora-kb-sync-slack        - 2:00 AM AZ daily
#   cowork-cora-kb-sync-gmail        - 2:30 AM AZ daily  (3h limit, see note)
#   cowork-cora-kb-sync-asana        - 3:00 AM AZ daily
#   cowork-cora-kb-sync-fireflies    - 3:30 AM AZ daily
#   cowork-cora-kb-sync-static       - 4:00 AM AZ daily
#   cowork-cora-kb-sync-drive        - 4:30 AM AZ daily (Phase 4 Layer 2)
#   cowork-cora-kb-sync-notion       - 5:00 AM AZ daily (Contracts and Renewals Registry)
#   cowork-cora-reconciliation       - 5:30 AM AZ daily
#
# Each task runs the repo .venv python directly (NOT uv) per doctrine D-005 and the
# venv-lock deadlock lesson (uv run contends with the live cowork-cora-service for the
# venv lock). Output is redirected to logs/kb-sync-<source>-YYYY-MM-DD.log.
#
# ASCII-only file per doctrine D-016 (PowerShell 5.1 reads UTF-8 as Windows-1252).
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-kb-sync-tasks.ps1
#
# To remove the tasks later:
#     .\deployment\remove-kb-sync-tasks.ps1

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Repo venv python not found at $PythonExe. Create the venv first (uv venv / uv sync)."
    exit 1
}

# Define the sync tasks. TimeLimitHours defaults to 1; the Gmail sweep gets 3 because
# it impersonates ~30 mailboxes via DWD and the one-time historical catch-up is large.
# The sweep is resumable (per-account watermark), so even a short window makes progress,
# but 3h lets the catch-up finish in a couple of nights instead of weeks.
$Tasks = @(
    @{
        Name          = "cowork-cora-kb-sync-slack"
        Script        = "scripts\incremental_sync_slack.py"
        HourMin       = "02:00"
        TimeLimitHours = 1
        Description   = "Cora KB daily incremental sync - Slack channel history (all channels Cora is a member of)"
    },
    @{
        Name          = "cowork-cora-kb-sync-gmail"
        Script        = "scripts\gmail_threaded_sweep.py"
        HourMin       = "02:30"
        TimeLimitHours = 3
        Description   = "Cora KB daily incremental sync - Gmail full thread text (multi-user DWD sweep, read+unread)"
    },
    @{
        Name          = "cowork-cora-kb-sync-asana"
        Script        = "scripts\incremental_sync_asana.py"
        HourMin       = "03:00"
        TimeLimitHours = 1
        Description   = "Cora KB daily incremental sync - Asana tasks + comments + project descriptions"
    },
    @{
        Name          = "cowork-cora-kb-sync-fireflies"
        Script        = "scripts\incremental_sync_fireflies.py"
        HourMin       = "03:30"
        TimeLimitHours = 1
        Description   = "Cora KB daily incremental sync - Fireflies meeting transcripts"
    },
    @{
        Name          = "cowork-cora-kb-sync-static"
        Script        = "scripts\incremental_sync_static.py"
        HourMin       = "04:00"
        TimeLimitHours = 1
        Description   = "Cora KB daily incremental sync - Founder OS static markdown (CLAUDE.md, decisions.md, etc.)"
    },
    @{
        Name          = "cowork-cora-kb-sync-drive"
        Script        = "scripts\incremental_sync_drive.py"
        HourMin       = "04:30"
        TimeLimitHours = 1
        Description   = "Cora KB daily incremental sync - Google Drive deliverable files via Drive API DWD"
    },
    @{
        Name          = "cowork-cora-kb-sync-notion"
        Script        = "scripts\incremental_sync_notion.py"
        HourMin       = "05:00"
        TimeLimitHours = 1
        Description   = "Cora KB daily incremental sync - Notion Contracts and Renewals Registry"
    },
    @{
        Name          = "cowork-cora-reconciliation"
        Script        = "scripts\run_reconciliation.py"
        HourMin       = "05:30"
        TimeLimitHours = 1
        Description   = "Cora cross-source reconciliation - detects gaps across Slack/Gmail/Asana/HubSpot, proposes to Harrison for approval"
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
        try { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue } catch {}
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }

    # Build the action: invoke the repo venv python via cmd.exe wrapper so output
    # redirection works. cd to repo root, run the script, redirect to log file.
    $scriptPath = Join-Path $RepoRoot $task.Script
    $cmdArgs = "/c cd /d `"$RepoRoot`" `& `"$PythonExe`" `"$scriptPath`""
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -WorkingDirectory $RepoRoot -Argument $cmdArgs

    # Trigger: daily at the specified time
    $trigger = New-ScheduledTaskTrigger -Daily -At $task.HourMin

    # Run as current user while logged in (InteractiveToken - no admin required to register,
    # consistent with cowork-cora-service which uses the same logon type).
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited

    # Settings: don't run if on batteries, allow start-on-demand, retry once on failure.
    $limitHours = if ($task.TimeLimitHours) { $task.TimeLimitHours } else { 1 }
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 1 `
        -RestartInterval (New-TimeSpan -Minutes 5) `
        -ExecutionTimeLimit (New-TimeSpan -Hours $limitHours)

    Register-ScheduledTask `
        -TaskName $taskName `
        -Description $task.Description `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings | Out-Null

    Write-Host "  Registered: $taskName  (daily $($task.HourMin) AZ, limit $limitHours h)" -ForegroundColor Green
}

Write-Host ""
Write-Host "All 8 KB sync tasks registered (venv python, ASCII-only)." -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName 'cowork-cora-kb-sync-*' | Format-Table TaskName, State, NextRunTime"
Write-Host ""
Write-Host "Force a test run for any task:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName 'cowork-cora-kb-sync-gmail'"
Write-Host ""
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "Watch logs at:" -ForegroundColor Cyan
Write-Host "  $RepoRoot\logs\kb-sync-*-$today.log"
