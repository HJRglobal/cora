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
$UvPath = "uv"  # assumes uv is on PATH; replace with full path if needed

# Verify script exists before registering
if (-not (Test-Path $ScriptPath)) {
    Write-Error "Script not found: $ScriptPath"
    exit 1
}

# Remove existing task if present
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

# Action: run the filer script via uv
$Action = New-ScheduledTaskAction `
    -Execute $UvPath `
    -Argument "run python `"$ScriptPath`"" `
    -WorkingDirectory $RepoRoot

# Trigger: every 4 hours starting at the next :00 mark
$Trigger = New-ScheduledTaskTrigger `
    -RepetitionInterval (New-TimeSpan -Hours 4) `
    -Once `
    -At (Get-Date -Minute 0 -Second 0).AddHours(1)

# Settings: don't start if on battery; stop if it runs > 15 minutes
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries:$false

# Run as the current user (who has access to .env and service account key)
$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force

Write-Host ""
Write-Host "Task registered: '$TaskName'" -ForegroundColor Green
Write-Host "  Schedule:   Every 4 hours"
Write-Host "  Script:     $ScriptPath"
Write-Host "  Working dir: $RepoRoot"
Write-Host ""
Write-Host "To run immediately: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To test without filing: uv run python scripts\run_attachment_filer.py --dry-run"
