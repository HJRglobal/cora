# Setup Windows Scheduled Task: weekly 13-week cashflow FORECAST snapshot
# (13WCF shadow ledger, S1 / M1).
#
# Read-only sweep of all 19 CF tabs of the Standing ACTUALS sheet, banking each
# tab's full week grid (forecast / actual / diff, basis-labelled) plus a
# structural roll-state stamp.
#
# Outputs:
#   data\state\cashflow-ledger\forecast-snapshots\YYYY-MM-DD_forecast.json
#   G:\My Drive\HJR-Founder-OS\01-HJR-Global\accounting\cashflow-ledger\
#       forecast-snapshots\YYYY-MM-DD_forecast.json
#
# WHY THIS JOB MATTERS MORE THAN MOST. The Standing ACTUALS sheet overwrites its
# FORECAST column in place once a week closes (D-121): 42 of 43 historical weeks
# had FORECAST equal to ACTUAL to sub-dollar rounding. The sheet therefore
# destroys its own forecast history, and a Monday that goes unsnapshotted is
# forecast history lost PERMANENTLY -- no later run recovers it. The nightly
# health check (08:45) asserts this job fired and WARNs from Tuesday if it did
# not.
#
# Nothing here writes to the sheet, ever (A5 lock). CF_HR LLC is excluded at
# COLLECTION -- Harrison's personal books, and the mirror lands in a folder
# Justin and Hayden work in.
#
# Schedule: weekly MONDAY 06:15 AZ -- before Justin's Monday refresh.
#
# SLOT CHECK against the LIVE registry (2026-08-05): the design named 06:10, but
# 06:10 is OCCUPIED by cowork-cora-gap-autofill. 06:15 is unused; neighbours are
# gap-autofill 06:10 and cowork-cora-inventory-state-sync 06:20. This job is
# light (19 Sheets reads, ~30s) so it clears well before 06:20. It sits inside
# the 03:00-09:00 window the weekly health metric watches but is unique at its
# minute, so max-concurrent stays 1 against a threshold of >2 -- no new alarm.
# (06:25, the M2 actuals slot, is also free as of this check.)
#
# A run that lands AFTER the refresh is not wasted: each tab is stamped
# post_refresh_suspect and excluded from forecast-accuracy math rather than
# silently averaged in as though it held a real forecast.
#
# Exit codes: 0 = every tab read cleanly; 1 = at least one tab unreadable
# (snapshot still written, those tabs listed under unreadable_tabs); 2 = total
# failure or a refused partial sweep, previous snapshot left in place. Task
# Scheduler surfaces these as Last Result.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-cashflow-forecast-snapshot-task.ps1
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'cowork-cora-cashflow-forecast-snapshot' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "cowork-cora-cashflow-forecast-snapshot"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_cashflow_forecast_snapshot.py"
$HourMin    = "06:15"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at $PythonExe. Check the venv."
    exit 1
}
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found at $ScriptPath."
    exit 1
}

Write-Host "Setting up scheduled task: $TaskName" -ForegroundColor Cyan

# Guard the slot: refuse to stack onto a minute another Cora task already owns.
$collisions = @()
foreach ($t in (Get-ScheduledTask | Where-Object { $_.TaskName -like "*cora*" -and $_.TaskName -ne $TaskName })) {
    $nextRun = (Get-ScheduledTaskInfo -TaskName $t.TaskName -ErrorAction SilentlyContinue).NextRunTime
    if ($nextRun -and $nextRun.ToString("HH:mm") -eq $HourMin) {
        $collisions += $t.TaskName
    }
}
if ($collisions.Count -gt 0) {
    Write-Error "Slot $HourMin is already used by: $($collisions -join ', '). Pick a free minute and update `$HourMin."
    exit 1
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Removing existing task..." -ForegroundColor Yellow
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# D-005: absolute .venv python + relative script path + WorkingDirectory = repo root.
$Action = New-ScheduledTaskAction -Execute $PythonExe `
    -Argument "scripts\run_cashflow_forecast_snapshot.py" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $HourMin

# 19 Sheets values.get calls, each a sub-second HTTPS GET (~30s observed
# end-to-end). 20 minutes is a generous backstop that still bounds a hung read.
# StartWhenAvailable matters here: if the host is asleep at 06:15 the run fires
# late rather than being skipped, and a late snapshot still banks the week.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Weekly 13-week cashflow FORECAST snapshot (13WCF shadow ledger S1). Reads all 19 CF tabs of the Standing ACTUALS sheet Monday 06:15 AZ, before the weekly refresh overwrites the forecast column in place (D-121). Read-only, deterministic, no model call. Never writes to the sheet. CF_HR LLC excluded at collection. A missed Monday is forecast history lost permanently." `
    | Out-Null

Write-Host "  Registered: $TaskName (weekly Monday $HourMin)" -ForegroundColor Green

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "State:    $($task.State)"
Write-Host "Next run: $($info.NextRunTime)"
Write-Host ""
Write-Host "Preview any time (non-elevated is fine, writes nothing):" -ForegroundColor Cyan
Write-Host "  .venv\Scripts\python.exe scripts\run_cashflow_forecast_snapshot.py --dry-run"
Write-Host ""
Write-Host "MANUAL FIRST RUN (do this at merge -- banks the current forecast today" -ForegroundColor Yellow
Write-Host "instead of waiting for Monday; every unsnapshotted week is lost forever):" -ForegroundColor Yellow
Write-Host "  .venv\Scripts\python.exe scripts\run_cashflow_forecast_snapshot.py"
