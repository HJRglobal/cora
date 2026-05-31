# setup-channel-sweep-task.ps1
#
# Registers the nightly org-wide channel sweep as a Windows Scheduled Task.
# Runs at 1:30am AZ (08:30 UTC) - after Slack KB sync (2am) but before
# reconciliation (5:30am), so sweep data is available for pass 6.
#
# Usage (from repo root):
#   .\deployment\setup-channel-sweep-task.ps1
#
# What it registers:
#   Task name : cowork-cora-channel-sweep
#   Schedule  : Daily at 01:30 AZ (America/Phoenix = UTC-7, no DST)
#   Action    : uv run python scripts/run_channel_sweep.py
#   Working dir: C:\Users\Harri\code\cora

$ErrorActionPreference = "Stop"

$RepoRoot  = "C:\Users\Harri\code\cora"
$TaskName  = "cowork-cora-channel-sweep"
$LogDir    = "$RepoRoot\logs"
$RunTime   = "08:30"   # UTC = 01:30 AZ (Phoenix, UTC-7, no DST)

# Locate uv
$uvExe = $null
$uvCandidates = @(
    "C:\Users\Harri\.local\bin\uv.exe",
    "$env:LOCALAPPDATA\uv\bin\uv.exe",
    "$env:LOCALAPPDATA\Programs\uv\uv.exe"
)
foreach ($c in $uvCandidates) {
    if (Test-Path $c -PathType Leaf) { $uvExe = $c; break }
}
if (-not $uvExe) {
    try { $uvExe = (Get-Command uv -ErrorAction Stop).Source } catch {}
}
if (-not $uvExe) { Write-Error "uv.exe not found."; exit 1 }

Write-Host "Registering task: $TaskName" -ForegroundColor Cyan

$Action  = New-ScheduledTaskAction `
    -Execute $uvExe `
    -Argument "run python scripts/run_channel_sweep.py" `
    -WorkingDirectory $RepoRoot

$Trigger = New-ScheduledTaskTrigger -Daily -At $RunTime

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "  Removed old task." -ForegroundColor Yellow
}

Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $Action `
    -Trigger   $Trigger `
    -Settings  $Settings `
    -Principal $Principal | Out-Null

Write-Host "  OK  Task registered: $TaskName" -ForegroundColor Green
Write-Host "  Schedule: daily at $RunTime UTC (01:30 AZ)" -ForegroundColor White

# --- Dry-run verification ---
Write-Host ""
Write-Host "Running dry-run verification..." -ForegroundColor Cyan

Push-Location $RepoRoot
try {
    $savedEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $uvExe run python scripts/run_channel_sweep.py --dry-run 2>&1 | ForEach-Object { Write-Host "  $_" }
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $savedEAP
    if ($exitCode -eq 0) {
        Write-Host "  OK  Dry run succeeded." -ForegroundColor Green
    } else {
        Write-Host "  WARN  Dry run exited $exitCode - check output above." -ForegroundColor Yellow
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "=== Channel Sweep task registered ===" -ForegroundColor Green
Write-Host ""
Write-Host "One-time setup: join all existing public channels now:" -ForegroundColor Cyan
Write-Host "  uv run python scripts/bootstrap_channel_membership.py"
Write-Host ""
Write-Host "Run manually anytime:" -ForegroundColor Cyan
Write-Host "  uv run python scripts/run_channel_sweep.py"
Write-Host "  uv run python scripts/run_channel_sweep.py --dry-run"
