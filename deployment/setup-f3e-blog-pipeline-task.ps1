# Setup Windows Scheduled Task: Cora F3E blog pipeline (cq-2577936d2809).
#
# WHAT IT DOES each Monday: reads the Learn editorial backlog, drafts the top
# QUEUED row against the post template and the two cleared fact sources, runs the
# deterministic claims preflight, stages the article UNPUBLISHED in Shopify, reads
# it back, marks the backlog row DRAFTED, appends the pipeline log, and DMs
# Harrison a one-tap publish card. It also sweeps the press tracker for a new
# Published flip and re-reads previously published posts to confirm they are
# still serving.
#
# WHAT IT CANNOT DO: publish. Harrison ruled 2026-08-26 that full-auto publishing
# is rejected and he is the sole publisher. The publish mutation is reachable only
# from the Slack confirm-tap handler, and the test suite pins that this script's
# import graph does not even name it.
#
# SCHEDULE: Monday 08:50 AZ. Chosen against the live registry, not guessed:
#   * The kickoff suggested 08:40, but 08:40 is already taken by
#     cowork-cora-channel-sweep. The weekly health metric alarms when two tasks
#     share a clock time inside 03:00-09:00, so 08:40 would have shipped a
#     standing false alarm.
#   * Taken minutes in the 07:00-09:00 band on the live host: :00 :05 :06 :08
#     :10 :15 :20 :30 :40 :45. 08:50 is free.
#   * It sits AFTER the 07:50 interim Cowork task (f3e-weekly-learn-draft-pipeline)
#     on purpose. While both run, the later one sees the earlier one's row already
#     marked DRAFTED and skips it, so the worst case is no double-staging.
#
# ExecutionTimeLimit is 30 minutes: an LLM draft plus a few API round-trips is a
# couple of minutes, so 30 is generous, and MultipleInstances defaults to
# IgnoreNew so a wedged run cannot stack with anything.
#
# ASCII-only (D-016): PowerShell 5.1 reads UTF-8 as Windows-1252.
#
# No pre-delete via schtasks (D-229): on a first run, "schtasks /Delete" writes to
# stderr for a task that does not exist yet, which PS 5.1 raises as a TERMINATING
# NativeCommandError under $ErrorActionPreference = "Stop" -- and 2>$null does not
# prevent it. Existence is guarded with Get-ScheduledTask, a cmdlet, whose errors
# are catchable.
#
# Run from elevated PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-f3e-blog-pipeline-task.ps1
#
# INSPECT FIRST -- the rollout gate. A dry run drafts and preflights but stages
# nothing, writes nothing to Shopify, and sends no card:
#     .venv\Scripts\python.exe scripts\run_f3e_blog_pipeline.py --dry-run
#
# To remove:
#     Unregister-ScheduledTask -TaskName 'Cora - F3E Blog Pipeline' -Confirm:$false

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$TaskName   = "Cora - F3E Blog Pipeline"
$ScriptPath = "C:\Users\Harri\code\cora\scripts\run_f3e_blog_pipeline.py"
$StartAt    = "08:50"

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

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $StartAt

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "cq-2577936d2809: weekly F3E blog lane. Drafts the top QUEUED Learn backlog row, runs the deterministic claims preflight, stages the article UNPUBLISHED, and DMs Harrison a one-tap publish card. Also sweeps the press tracker for new Published flips and re-reads previously published posts. Cannot publish: the publish mutation is reachable only from the confirm-tap handler." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Registered: $TaskName" -ForegroundColor Green
Write-Host "  Runs Mondays at $StartAt AZ." -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Format-Table TaskName, State"
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo | Format-List NextRunTime"
