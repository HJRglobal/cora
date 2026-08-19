# fix-watchdog-task-settings-2026-08-19.ps1   (run from an ELEVATED PowerShell)
#
# STAGED FOR HARRISON -- a Code session does not write the scheduler.
#
# WHY (cq-7915a8647cff, verified live 2026-08-19 13:17):
#   cora-watchdog was in State=Running with NO powershell process behind it, and
#   Get-ScheduledTaskInfo reported LastTaskResult 2147946720 = 0x80070420
#   ALREADY_RUNNING. Registered by schtasks /Create it inherited the defaults
#   MultipleInstances=IgnoreNew and ExecutionTimeLimit=PT72H, which together mean:
#   one stuck instance silently REJECTS every 5-minute trigger for up to three
#   days, logging nothing. That is the mechanism behind the "blind windows with
#   zero watchdog lines" in the 8/18 forensics -- and it was live again today.
#
#   Also fixed here: DisallowStartIfOnBatteries=True (a second silent-skip path)
#   and StartWhenAvailable=False (missed runs are never caught up).
#
# WHAT THIS DOES -- scheduler settings only. It does not touch the script, the
# trigger, the principal (already RunLevel=Highest, verified 8/19) or the action.
#   1. Clears any stuck instance:      schtasks /End /TN cora-watchdog
#   2. MultipleInstances  IgnoreNew -> StopExisting  (a fresh tick wins)
#   3. ExecutionTimeLimit PT72H     -> PT10M         (worst case is ~4 min:
#      restart-cora.ps1's 90s settle + the watchdog's 90s verify wait)
#   4. DisallowStartIfOnBatteries/StopIfGoingOnBatteries -> False
#   5. StartWhenAvailable -> True
#   6. Prints the resulting settings so the change is verifiable in one screen.
#
# Optional, separate, NOT done here (it is a machine-wide logging change):
#   wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true
#   -- Task Scheduler history is currently DISABLED on this host, which is why the
#      8/18 request to "review Task Scheduler history for the blind windows" could
#      not be answered: Get-WinEvent returns no events at all. The watchdog's own
#      hourly tick line (shipped 8/19) is the durable substitute and does not
#      depend on this being on.

$ErrorActionPreference = "Stop"
$TaskName = "cora-watchdog"

Write-Host "=== BEFORE ==="
$before = Get-ScheduledTask -TaskName $TaskName
$before.Settings | Format-List MultipleInstances, ExecutionTimeLimit, StartWhenAvailable, DisallowStartIfOnBatteries, StopIfGoingOnBatteries
Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime, LastTaskResult, NextRunTime
Write-Host ("State: " + $before.State)

Write-Host ""
Write-Host "=== Clearing any stuck instance (State=Running with no process blocks every trigger) ==="
schtasks /End /TN $TaskName
Write-Host ("schtasks /End exit: " + $LASTEXITCODE + "  (1 or a 'not running' message here is fine)")

Write-Host ""
Write-Host "=== Applying settings ==="
$task = Get-ScheduledTask -TaskName $TaskName
$task.Settings.MultipleInstances = "StopExisting"
$task.Settings.ExecutionTimeLimit = "PT10M"
$task.Settings.StartWhenAvailable = $true
$task.Settings.DisallowStartIfOnBatteries = $false
$task.Settings.StopIfGoingOnBatteries = $false
Set-ScheduledTask -InputObject $task | Out-Null

Write-Host ""
Write-Host "=== AFTER ==="
$after = Get-ScheduledTask -TaskName $TaskName
$after.Settings | Format-List MultipleInstances, ExecutionTimeLimit, StartWhenAvailable, DisallowStartIfOnBatteries, StopIfGoingOnBatteries
$after.Principal | Format-List UserId, RunLevel
Write-Host ("State: " + $after.State)

Write-Host ""
Write-Host "Then confirm the watchdog is actually running again -- a tick line should"
Write-Host "appear within the hour (the tick is throttled to one per hour when healthy):"
Write-Host ("  Get-Content C:\Users\Harri\code\cora\logs\watchdog-" + (Get-Date -Format "yyyy-MM-dd") + ".jsonl -Tail 5")
Write-Host "Or force one immediately:"
Write-Host "  schtasks /Run /TN cora-watchdog"
Write-Host ""
Write-Host "The nightly health check now CRITICALs if the newest watchdog line is >3h old,"
Write-Host "so a repeat of this failure mode reports itself instead of going dark."
