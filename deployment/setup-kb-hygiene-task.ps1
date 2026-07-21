# Setup the monthly KB-hygiene sweep task (KB-staleness LOOP).
# Runs the 1st of each month at 15:00 local (AZ) -- a quiet hour OUTSIDE the
# 02:00-06:30 AZ KB-sync window. ASCII-only (D-016). Absolute .venv python (D-005).
#
# What the task does each month:
#   --marked --apply   auto-archive+purge every KB-STATUS: SUPERSEDED banner'd .md.
#                      SMALL sweeps apply live (WAL DELETE, no VACUUM, no Cora stop);
#                      a LARGE sweep ESCALATES (does not auto-apply) and DMs Harrison
#                      to run deployment\run-kb-hygiene-apply.ps1 (Cora stopped).
#   --proactive        PROPOSE-ONLY staleness candidates (never moves/purges).
#   --gc               retention: hard-delete _archive files older than 180d
#                      (no-op until this loop's own archives age ~6 months).
#   --slack            DM the composed report to Harrison.
#
# This is user-level file+WAL work -- no elevation, no -RL HIGHEST (matches the
# log-compaction task). Uses schtasks /Create (New-ScheduledTaskTrigger has no
# monthly trigger). Run from PowerShell (elevated only if schtasks reports denied).
#
# >>> FIRST-RUN GATE (do this BEFORE the first scheduled fire):
#   .venv\Scripts\python.exe scripts\kb_hygiene_sweep.py --marked --proactive
# review the dry-run manifest + candidate report, THEN let the monthly task run.
# (The marked tier only ever touches EXPLICITLY banner'd files, so it is a no-op
# until you start stamping the KB-STATUS: SUPERSEDED banner.)

$TaskName = "cowork-cora-kb-hygiene"
$Python   = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$Script   = "C:\Users\Harri\code\cora\scripts\kb_hygiene_sweep.py"

$tr = '"{0}" "{1}" --marked --apply --proactive --gc --slack' -f $Python, $Script
schtasks /Create /TN $TaskName /TR $tr /SC MONTHLY /D 1 /ST 15:00 /F

Write-Host "Registered: $TaskName (monthly, day 1 at 15:00 AZ)"
Write-Host "Action: --marked --apply --proactive --gc --slack"
Write-Host ""
Write-Host "FIRST-RUN GATE -- run + review a dry-run before the first scheduled fire:"
Write-Host "  $Python $Script --marked --proactive"
