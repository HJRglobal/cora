# setup-qbo-token-refresh-task.ps1
# Re-registers the QBO token refresh scheduled task using absolute .venv python path.
# Replaces the old uv-based registration which deadlocks against the Cora service.
#
# Run from ELEVATED PowerShell:
#   cd C:\Users\Harri\code\cora
#   .\deployment\setup-qbo-token-refresh-task.ps1

$RepoRoot = "C:\Users\Harri\code\cora"
$Python   = "$RepoRoot\.venv\Scripts\python.exe"
$Script   = "$RepoRoot\scripts\qbo_oauth_flow.py"
$TaskName = "cowork-cora-qbo-token-refresh"

$Action   = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$Script`" --refresh-all" `
    -WorkingDirectory $RepoRoot

$Trigger  = New-ScheduledTaskTrigger -Daily -At "02:00AM"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop } catch {}

$task = Register-ScheduledTask `
    -TaskName $TaskName `
    -Action   $Action `
    -Trigger  $Trigger `
    -Settings $Settings `
    -RunLevel Highest `
    -Force

Write-Host "Registered: $($task.TaskName)  State: $($task.State)"
Write-Host "Execute:    $($task.Actions[0].Execute)"
Write-Host "Arguments:  $($task.Actions[0].Arguments)"