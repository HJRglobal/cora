# setup-attachment-filer-task.ps1
# Registers a Windows Task Scheduler task that runs the email attachment filer
# every 4 hours.
#
# Run once from an elevated PowerShell prompt:
#   Set-ExecutionPolicy RemoteSigned -Scope Process
#   .\deployment\setup-attachment-filer-task.ps1
#
# Prerequisites:
#   1. GOOGLE_SERVICE_ACCOUNT_JSON in .env points to a valid service account key.
#   2. The service account has gmail.modify DWD scope in Google Admin for every
#      account listed in data/maps/monitored-email-accounts.yaml.
#   3. ANTHROPIC_API_KEY and SLACK_BOT_TOKEN are set in .env.
#
# To remove the task:
#   .\deployment\remove-attachment-filer-task.ps1
#
# To run immediately (for testing):
#   Start-ScheduledTask -TaskName "Cora - Email Attachment Filer"

$TaskName = "Cora - Email Attachment Filer"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$ScriptPath = Join-Path $RepoRoot "scripts\run_attachment_filer.py"
$PythonPath = Join-Path $RepoRoot ".venv\Scripts\python.exe"

# Verify script and interpreter exist before registering
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found: $ScriptPath"
    exit 1
}
if (-not (Test-Path $PythonPath)) {
    Write-Error "Venv python not found: $PythonPath  (run 'uv sync' first)"
    exit 1
}

# Remove existing task if present
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

# Action: run the filer script via the venv python (full path -- Task Scheduler has no user PATH)
$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$ScriptPath`"" `
    -WorkingDirectory $RepoRoot

# Trigger: every 4 hours starting at the next :00 mark
$Trigger = New-ScheduledTaskTrigger `
    -RepetitionInterval (New-TimeSpan -Hours 4) `
    -Once `
    -At (Get-Date -Minute 0 -Second 0).AddHours(1)

# Settings: don't start if on battery; hard kill at 20 min.
# The script self-bounds at ~13 min (EMAIL_FILING_RUN_BUDGET_SECONDS=780) and
# persists per-account progress as it goes, so it exits cleanly well before this
# limit. The 20-min limit is only a backstop + gives headroom for catch-up runs.
# (Previously 15 min: with a frozen watermark the run re-scanned ~2.5 weeks of
# mail and was killed BEFORE the end-of-run state save, so dedup never persisted.
# Per-account incremental saves + the self-bound fix that; see filer_ledger.py.)
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries:$false

# Run as the current user (who has access to .env and service account key)
$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

$result = Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force `
    -ErrorAction Stop 2>&1

Write-Host ""
Write-Host "Task registered: '$TaskName'" -ForegroundColor Green
Write-Host "  Schedule:   Every 4 hours"
Write-Host "  Python:     $PythonPath"
Write-Host "  Script:     $ScriptPath"
Write-Host "  Working dir: $RepoRoot"
Write-Host ""
Write-Host "To run immediately: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To test without filing: uv run python scripts\run_attachment_filer.py --dry-run"
