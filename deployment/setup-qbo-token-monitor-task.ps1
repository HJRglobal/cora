# setup-qbo-token-monitor-task.ps1  (Phase 3.3 / F-18, STAGED -- optional)
# Registers a daily read-only QBO token-validity monitor that DMs Harrison if any
# of the ~11 realms has an EXPIRED/INVALID/WARN/STALE refresh token, so finance
# answers for that entity don't silently fail. The monitor makes NO Intuit calls
# (reads the local token store only); the separate 02:00 refresh task is what
# actually rotates tokens.
#
# Runs at LIMITED privilege (least-privilege -- the monitor only reads the local
# token store + posts HTTPS; it does NOT need RunLevel Highest), so it can be
# registered from a NORMAL (non-elevated) PowerShell:
#   cd C:\Users\Harri\code\cora
#   .\deployment\setup-qbo-token-monitor-task.ps1
#
# NOTE: if you register this, add a line
#       - Cora - QBO Token Monitor
#   under the enabled tasks in data/maps/scheduled-task-state.yaml so the nightly
#   health check does not flag it as drift. 06:50 AM is chosen to run after the
#   02:00 refresh + 05:30 reconciliation and before the 07:30 morning brief;
#   confirm it does not collide with another task's minute (de-collide doctrine).

$RepoRoot = "C:\Users\Harri\code\cora"
$Python   = "$RepoRoot\.venv\Scripts\python.exe"
$Script   = "$RepoRoot\scripts\qbo_token_status.py"
$TaskName = "Cora - QBO Token Monitor"

$Action   = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$Script`" --alert" `
    -WorkingDirectory $RepoRoot

$Trigger  = New-ScheduledTaskTrigger -Daily -At "06:50AM"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop } catch {}

$task = Register-ScheduledTask `
    -TaskName $TaskName `
    -Action   $Action `
    -Trigger  $Trigger `
    -Settings $Settings `
    -Force

Write-Host "Registered: $($task.TaskName)  State: $($task.State)"
Write-Host "Execute:    $($task.Actions[0].Execute)"
Write-Host "Arguments:  $($task.Actions[0].Arguments)"
