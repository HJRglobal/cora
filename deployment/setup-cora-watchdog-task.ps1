# setup-cora-watchdog-task.ps1  (run from an ELEVATED PowerShell)
# Registers "cora-watchdog" to run every 5 minutes at highest privileges.
# Uses schtasks.exe /SC MINUTE (rock-solid for minute cadence). /F force-creates
# (overwrites if it already exists) -- so NO pre-check/delete is needed (a /Query on a
# nonexistent task writes to stderr, which would halt an ErrorActionPreference=Stop run).
# NOTE: ErrorActionPreference is intentionally NOT "Stop" here -- schtasks writes status
# to stderr on some paths and we don't want native stderr to terminate the script.

# Windowless action: route the command through run_hidden.py under pythonw.exe
. "$PSScriptRoot\_task-action.ps1"

$TaskName = "cora-watchdog"
$Script   = "C:\Users\Harri\code\cora\deployment\cora-watchdog.ps1"
# Script path has no spaces, so no inner quoting is needed (avoids schtasks quote-escaping pain).
$Run      = "powershell -NoProfile -ExecutionPolicy Bypass -File $Script"

# /SC MINUTE /MO 5 = every 5 min. /RL HIGHEST = elevated (needed by restart-cora.ps1).
# No /RU -> runs as the creating (interactive) user with no stored password, when logged on
# (matches the always-on, logged-in desktop). /F = create-or-overwrite (idempotent).
schtasks /Create /TN $TaskName /TR $Run /SC MINUTE /MO 5 /RL HIGHEST /F
$rc = $LASTEXITCODE

if ($rc -eq 0) {
    # schtasks /TR caps at 261 chars, too short for the wrapper -- wrap the
    # registered action instead (COM/CIM, no length limit).
    if (-not (Set-WrappedTaskAction -TaskName $TaskName)) {
        Write-Host "  WARNING: the task is registered but NOT windowless -- it will flash a console window at every fire."
        Write-Host ("  Fix with (ELEVATED): .\deployment\rewrap-tasks-hidden.ps1 -Apply -Only " + $TaskName)
    }
}



# schtasks /Create leaves the DEFAULTS MultipleInstances=IgnoreNew and
# ExecutionTimeLimit=PT72H, which is how this task went dark on 2026-08-19: it sat
# in State=Running with no process behind it, and every 5-minute trigger was
# silently rejected with 0x80070420 ALREADY_RUNNING -- for up to three days, with
# no log line (cq-7915a8647cff). StopExisting + a 10-minute limit make a stuck run
# self-clearing. Batteries/StartWhenAvailable close two more silent-skip paths.
# Worst-case real runtime is ~4 min (restart-cora.ps1's 90s settle + 90s verify).
if ($rc -eq 0) {
    try {
        $task = Get-ScheduledTask -TaskName $TaskName
        $task.Settings.MultipleInstances = "StopExisting"
        $task.Settings.ExecutionTimeLimit = "PT10M"
        $task.Settings.StartWhenAvailable = $true
        $task.Settings.DisallowStartIfOnBatteries = $false
        $task.Settings.StopIfGoingOnBatteries = $false
        Set-ScheduledTask -InputObject $task | Out-Null
        Write-Host "Applied settings: MultipleInstances=StopExisting, ExecutionTimeLimit=PT10M, StartWhenAvailable, no battery skip."
    } catch {
        Write-Host ("WARNING: could not apply hardened settings (" + $_.Exception.Message + ") -- run deployment\fix-watchdog-task-settings-2026-08-19.ps1")
    }
}

Write-Host ""
if ($rc -eq 0) {
    Write-Host "Registered cora-watchdog (every 5 min, highest privileges, runs while logged on)."
} else {
    Write-Host ("schtasks /Create returned exit " + $rc + " -- task NOT registered; see the error above.")
}
Write-Host ""
Write-Host "Verify:  schtasks /Query /TN cora-watchdog /V /FO LIST"
Write-Host ("Dry-run: powershell -NoProfile -ExecutionPolicy Bypass -File `"" + $Script + "`" -DryRun")
Write-Host ("Watch:   Get-Content C:\Users\Harri\code\cora\logs\watchdog-" + (Get-Date -Format "yyyy-MM-dd") + ".jsonl -Wait")
Write-Host ""
Write-Host "To also survive a reboot with NO logon: re-create with stored creds (add /RU + /RP),"
Write-Host "or set 'Run whether user is logged on or not' in Task Scheduler."
