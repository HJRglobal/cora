# setup-hubspot-sync-task.ps1
#
# Registers a Windows Scheduled Task that runs Gmail → HubSpot email sync every hour.
#
# Usage (from an elevated PowerShell prompt):
#
#   cd C:\Users\Harri\code\cora
#   .\deployment\setup-hubspot-sync-task.ps1
#
# Prerequisites:
#   - Cora repo deployed at C:\Users\Harri\code\cora
#   - uv and dependencies synced (run deploy-updates.ps1 first)
#   - HUBSPOT_PRIVATE_APP_TOKEN set in .env (new account token)
#   - GOOGLE_SERVICE_ACCOUNT_JSON set in .env (DWD service account)
#   - SLACK_BOT_TOKEN set in .env (for clarification DMs)

$ErrorActionPreference = "Stop"

$RepoRoot = "C:\Users\Harri\code\cora"
$TaskName = "Cora - HubSpot Email Sync"
$PythonExe = "$RepoRoot\.venv\Scripts\python.exe"
$ScriptPath = "$RepoRoot\scripts\run_hubspot_email_sync.py"

Write-Host ""
Write-Host "=== Registering HubSpot Email Sync Task ===" -ForegroundColor Cyan
Write-Host ""

# Verify prerequisites
if (-not (Test-Path $RepoRoot -PathType Container)) {
    Write-Error "Repo not found at $RepoRoot — deploy first."
    exit 1
}
if (-not (Test-Path $PythonExe -PathType Leaf)) {
    Write-Error "Python not found at $PythonExe — run 'uv sync' first."
    exit 1
}
Write-Host "  OK  Repo: $RepoRoot"
Write-Host "  OK  Python: $PythonExe"

# Remove existing task if present
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "  Removing existing task '$TaskName'..." -ForegroundColor Yellow
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Build task components
$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument $ScriptPath `
    -WorkingDirectory $RepoRoot

# Run every hour, starting now
$trigger = New-ScheduledTaskTrigger `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -Once `
    -At (Get-Date)

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -RunOnlyIfNetworkAvailable

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Cora: sync Gmail threads to HubSpot email engagements (runs hourly)" | Out-Null

Write-Host "  OK  Task registered: '$TaskName'" -ForegroundColor Green
Write-Host ""

# Run it once immediately
Write-Host "Running sync immediately to verify setup..." -ForegroundColor White
Push-Location $RepoRoot
try {
    $savedEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $PythonExe $ScriptPath --dry-run 2>&1 | ForEach-Object { Write-Host "  $_" }
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $savedEAP
    if ($exitCode -ne 0) {
        Write-Host "  WARN  Dry run exited $exitCode — check output above" -ForegroundColor Yellow
    } else {
        Write-Host "  OK  Dry run passed." -ForegroundColor Green
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Quick checks:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'   # force an immediate run"
Write-Host "  Get-Content '$RepoRoot\logs\cora-*.log' -Tail 20"
Write-Host ""
Write-Host "To remove the task:" -ForegroundColor Cyan
Write-Host "  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
