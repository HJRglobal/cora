# Registers the "Cora - F3E Daily Ecom Brief" scheduled task.
# Posts the daily F3 Energy ecom + ops brief to #f3-ops-cockpit at 07:10 AZ.
#
# 07:10 is a UNIQUE morning minute (07:00 = knowledge-review, 07:15 = decision-capture,
# 07:30 = daily briefing) -- the 2026-06-13 de-collision doctrine requires one task
# per clock minute in the 03:00-09:00 window so a slow run never cascades into the next.
#
# Run from an elevated PowerShell in C:\Users\Harri\code\cora.
# ASCII-only (D-016); absolute interpreter + script paths (D-053).

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonExe  = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ScriptPath = Join-Path $RepoRoot "scripts\run_f3e_ecom_brief.py"
$TaskName   = "Cora - F3E Daily Ecom Brief"

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$ScriptPath`"" -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At "07:10"
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -StartWhenAvailable -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 5)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Cora: compose + post the F3 Energy daily ecom + ops brief to #f3-ops-cockpit (07:10 AZ). Script-side; reads Shopify/Polar/HubSpot/Asana." -Force | Out-Null

Write-Host "Registered task: $TaskName (daily 07:10 AZ)"
Write-Host "Smoke first:  $PythonExe `"$ScriptPath`" --channel C0B4B0URRQS"
Write-Host "Dry-run:      $PythonExe `"$ScriptPath`" --dry-run"
