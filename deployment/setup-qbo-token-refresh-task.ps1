# Setup Windows Scheduled Task for daily QBO access-token refresh.
#
# Registers ONE task that runs nightly at 2:00 AM AZ to refresh every
# provisioned QBO entity's access token, BEFORE the KB sync tasks at
# 3:00 / 3:30 / 4:00 AM AZ.
#
#   cowork-cora-qbo-token-refresh   - 2:00 AM AZ daily
#
# Why this exists: Intuit QBO refresh tokens are valid for 100 days, but
# only as long as they're USED periodically. Each successful refresh
# yields a new refresh token (rolling expiry). If a refresh token sits
# unused for 100 days, the entity must be manually re-OAuthed. This task
# guarantees daily use of every entity's refresh token, keeping the
# 100-day clock effectively reset every night.
#
# The task invokes:
#     uv run python scripts/qbo_oauth_flow.py --refresh-all
# which iterates every entity in .credentials/qbo-tokens.json and forces
# a refresh-token rotation per entity. Output redirected to a dated log
# file in logs/ for diagnostics.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-qbo-token-refresh-task.ps1
#
# Verify:
#     Get-ScheduledTask -TaskName 'cowork-cora-qbo-token-refresh' | Format-List TaskName, State, NextRunTime
#
# Force a test run (to verify wiring before the first 2 AM fire):
#     Start-ScheduledTask -TaskName 'cowork-cora-qbo-token-refresh'
#     # Then check the log file in logs/
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-qbo-token-refresh' -Confirm:$false

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

# Ensure logs directory exists
$LogDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    Write-Host "Created log directory: $LogDir"
}

$TaskName    = "cowork-cora-qbo-token-refresh"
$Description = "Cora daily QBO access-token refresh for all provisioned entities. Runs at 2:00 AM AZ before the KB sync tasks. Keeps Intuit refresh tokens rotated within the 100-day window."
$HourMin     = "02:00"

# Remove existing task if present (idempotent re-registration)
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task: $TaskName" -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Build the action. Log to dated file so each night's refresh has its own log.
# The %DATE% expansion in cmd.exe gets the current date in MM/DD/YYYY, which has
# slashes that break paths - so we use PowerShell's stable yyyy-MM-dd format at
# task-fire time via a small inline date wrangle. Simpler: just use a static log
# filename pattern with date appended by cmd at runtime using PowerShell.
#
# Pattern matches setup-kb-sync-tasks.ps1 exactly:
#   cmd /c cd /d <repo> & <uv> run python <script>
# but with --refresh-all flag and an output redirection to a dated log.

$LogFilePattern = "logs\qbo-token-refresh-$((Get-Date -Format 'yyyy-MM-dd')).log"
# NOTE: The log filename above is resolved at REGISTRATION time, not fire time.
# So the log filename will be the date you ran this script, not the date the task
# fires. To get a fresh-per-night log, see the alternative redirection below.
# We use cmd's dynamic date instead so each fire gets a fresh dated log:
$cmdLogPath = "logs\qbo-token-refresh-%date:~10,4%-%date:~4,2%-%date:~7,2%.log"

$cmdArgs = "/c cd /d `"$RepoRoot`" `& `"$UvExe`" run python `"scripts\qbo_oauth_flow.py`" --refresh-all >> `"$cmdLogPath`" 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -WorkingDirectory $RepoRoot -Argument $cmdArgs

# Trigger: daily at 02:00 local (machine is AZ)
$trigger = New-ScheduledTaskTrigger -Daily -At $HourMin

# Principal: current user, Interactive logon, RunLevel Limited. Token refresh
# only reads/writes .credentials/qbo-tokens.json - no elevated privileges
# needed. Limited lets us register without an elevated shell.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Settings: tolerate sleeping/battery state, allow start-when-available so a
# missed 2 AM fire gets caught when the machine comes back. Cap execution at
# 10 minutes (a refresh of 5-9 entities should take ~10 seconds; 10 min is
# a generous timeout that prevents a stuck task from accumulating).
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description $Description `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host ""
Write-Host "[OK] Registered scheduled task: $TaskName" -ForegroundColor Green
Write-Host "     Runs daily at $HourMin (local time, AZ)" -ForegroundColor Green
Write-Host "     Log path pattern: $RepoRoot\logs\qbo-token-refresh-YYYY-MM-DD.log" -ForegroundColor Green
Write-Host ""
Write-Host "Recommended next step: force a one-time test run to verify wiring:" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'" -ForegroundColor White
Write-Host ""
Write-Host "Then verify the log contents (should show 5 entities refreshed [OK]):" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  Get-Content `"$RepoRoot\logs\qbo-token-refresh-$today.log`"" -ForegroundColor White
