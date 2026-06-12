# Registers the "Cora - LEX Dump Folder Sync" scheduled task.
# Daily 4:45 AM AZ -- after the 4:00 static_md sync, before the 5:00 Notion sync.
# Recurring sweep of the Shaun x Jen Lexington Dump Folder (incl. the DDD
# Policies shortcut tree) into the LEX KB. Watermark-incremental; idempotent.
#
# Run from the cora repo root. Direct .venv interpreter per doctrine D-005
# (never `uv run` -- it deadlocks against the running Cora service venv lock).

$ErrorActionPreference = "Stop"

$RepoRoot = "C:\Users\Harri\code\cora"
$TaskName = "Cora - LEX Dump Folder Sync"
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Script = Join-Path $RepoRoot "scripts\run_lex_dump_folder_sync.py"

if (-not (Test-Path $PythonExe)) { throw "venv python not found: $PythonExe" }
if (-not (Test-Path $Script)) { throw "script not found: $Script" }

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$Script`"" -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At "4:45AM"
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null

Write-Host "Registered task: $TaskName (daily 4:45 AM)"
schtasks /Query /TN $TaskName /FO LIST | Select-String "TaskName|Status|Next Run Time"
