# setup-cora-watchdog-task.ps1  (run from an ELEVATED PowerShell)
# Registers "cora-watchdog" to run every 5 minutes at highest privileges.
# Uses schtasks.exe /SC MINUTE (rock-solid for minute cadence). /F force-creates
# (overwrites if it already exists) -- so NO pre-check/delete is needed (a /Query on a
# nonexistent task writes to stderr, which would halt an ErrorActionPreference=Stop run).
# NOTE: ErrorActionPreference is intentionally NOT "Stop" here -- schtasks writes status
# to stderr on some paths and we don't want native stderr to terminate the script.

$TaskName = "cora-watchdog"
$Script   = "C:\Users\Harri\code\cora\deployment\cora-watchdog.ps1"
# Script path has no spaces, so no inner quoting is needed (avoids schtasks quote-escaping pain).
$Run      = "powershell -NoProfile -ExecutionPolicy Bypass -File $Script"

# /SC MINUTE /MO 5 = every 5 min. /RL HIGHEST = elevated (needed by restart-cora.ps1).
# No /RU -> runs as the creating (interactive) user with no stored password, when logged on
# (matches the always-on, logged-in desktop). /F = create-or-overwrite (idempotent).
schtasks /Create /TN $TaskName /TR $Run /SC MINUTE /MO 5 /RL HIGHEST /F
$rc = $LASTEXITCODE

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
