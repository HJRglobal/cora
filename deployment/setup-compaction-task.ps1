# Setup the monthly log + ledger compaction task (game-plan section 10.5).
# Runs the 1st of each month at 14:00 local (AZ), after the 1PM backup so logs
# are backed up before they are archived. ASCII-only (D-016).
#
# The work is plain file hygiene (gzip old logs, trim oversized ledgers) -- it
# needs no elevation, so this registers a user-level task (no -RL HIGHEST).
# Uses schtasks /Create because New-ScheduledTaskTrigger has no monthly trigger.
# Run from PowerShell (elevated only if schtasks /Create reports access denied).

$TaskName = "Cora - Log Compaction"
$Python   = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$Script   = "C:\Users\Harri\code\cora\scripts\compact_logs.py"

$tr = '"{0}" "{1}"' -f $Python, $Script
schtasks /Create /TN $TaskName /TR $tr /SC MONTHLY /D 1 /ST 14:00 /F

Write-Host "Registered: $TaskName (monthly, day 1 at 14:00 AZ)"
Write-Host "Smoke test now:"
Write-Host "  $Python $Script --dry-run"
