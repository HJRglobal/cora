# Setup the weekly auto-write digest task (autonomy update 7B: oversight-after-the-fact).
# DMs Harrison every Monday at 11:00 AZ: what Cora auto-learned this week (Tier
# 0/1), one-tap Revert per item, week-over-week counts. This is the validation
# surface for the graduated-trust flip -- watch these counts.
#
# ASCII-only (D-016). Absolute .venv python (D-005). User-level (read + DM only).
# schtasks /SC WEEKLY (New-ScheduledTaskTrigger weekly works too, but schtasks is
# consistent with the other Cora task setups). Run from PowerShell.
#
# NOTE: the digest itself only reports; auto-writing is gated separately by the
# CORA_AUTOWRITE_LIVE env var (default OFF). Enable auto-write by setting that in
# .env to 'tier0' (corroborated only) or 'all' (Tier 0 + Tier 1), then restart.

$TaskName = "cowork-cora-autowrite-digest"
$Python   = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"
$Script   = "C:\Users\Harri\code\cora\scripts\run_autowrite_digest.py"

$tr = '"{0}" "{1}"' -f $Python, $Script
schtasks /Create /TN $TaskName /TR $tr /SC WEEKLY /D MON /ST 11:00 /F

Write-Host "Registered: $TaskName (weekly, Monday 11:00 AZ)"
Write-Host "Smoke test now:"
Write-Host "  $Python $Script --dry-run --force"
