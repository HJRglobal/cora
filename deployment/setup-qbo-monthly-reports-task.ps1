# Register "Cora - QBO Monthly Reports" (cq-96adf03bcda3).
#
# Monthly on the 2nd at 07:15 AZ: pull the prior month's P&L + Balance Sheet for
# every provisioned QBO realm and write naming-convention .xlsx files into
# 01-HJR-Global\accounting\monthly-reports\{filing-month}\ on G:.
#
# WHY THE 2ND, NOT THE 1ST: the job must run AFTER the month closes. The archive's
# existing 2026-05 files were exported on 2026-05-22 -- mid-month -- so their
# figures are an open-month snapshot (HJRG management fees read 2,000 there vs
# 79,580 once May actually closed). Firing on the 2nd, with --month defaulting to
# the last completed month, is what makes the output a closed-month statement.
# It also lands before the standing 13WCF / month-close meeting cadence.
#
# ASCII-only (D-016). Absolute .venv python, never uv (D-005).
# Run from PowerShell (elevated only if schtasks /Create reports access denied).
#
# schtasks /Create ONLY. The first version chased it with a Set-ScheduledTask
# -Action call citing 'the log-compaction pattern' -- a D-051 review found that
# precedent does not exist anywhere in deployment/ (setup-compaction-task.ps1,
# the actual monthly task, uses schtasks alone), and whether Set-ScheduledTask
# preserves a schtasks-created monthly day-2 trigger was never verified.
# Dropped rather than shipped on an untested assumption: one command now owns
# both the trigger and the action.

# Windowless action: route the command through run_hidden.py under pythonw.exe
. "$PSScriptRoot\_task-action.ps1"

$ErrorActionPreference = "Stop"

$TaskName = "Cora - QBO Monthly Reports"
$RepoRoot = "C:\Users\Harri\code\cora"
$Python   = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Script   = Join-Path $RepoRoot "scripts\run_qbo_monthly_reports.py"

if (-not (Test-Path $Python)) { throw "python not found: $Python" }
if (-not (Test-Path $Script)) { throw "script not found: $Script" }

# --apply is REQUIRED for the task to write anything: the script is dry-run by
# default, so a mis-registered task reports instead of writing.
$tr = "`"$Python`" `"$Script`" --apply"

schtasks /Create /TN $TaskName /TR $tr /SC MONTHLY /D 2 /ST 07:15 /F
# schtasks /TR caps at 261 chars, too short for the wrapper -- wrap the
# registered action instead (COM/CIM, no length limit).
Set-WrappedTaskAction -TaskName $TaskName | Out-Null

Write-Host ""
Write-Host "Registered: $TaskName (monthly, day 2, 07:15 AZ)"
Write-Host ""
Write-Host "Verify (Next Run must NOT read N/A -- that would mean no trigger):"
Write-Host "  schtasks /query /tn `"$TaskName`" /v /fo LIST | Select-String 'Task To Run','Next Run'"
Write-Host ""
Write-Host "Dry-run by hand first (writes nothing):"
Write-Host "  .venv\Scripts\python.exe scripts\run_qbo_monthly_reports.py --month 2026-07"
Write-Host ""
Write-Host "Backfill the 2026-07 gap Justin's loop left (writes files):"
Write-Host "  .venv\Scripts\python.exe scripts\run_qbo_monthly_reports.py --month 2026-07 --apply"
