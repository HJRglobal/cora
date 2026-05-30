# deploy-updates.ps1
#
# Pull the latest Cora code, sync dependencies, and restart the service.
# Run this whenever a new batch of features lands on the branch you're tracking.
#
# Usage (from an elevated or normal PowerShell prompt):
#
#   cd C:\Users\Harri\code\cora
#   .\deployment\deploy-updates.ps1
#
# What this script does:
#   1. Verifies the repo and uv.exe are present
#   2. Stops the running Cora service task (if running)
#   3. Pulls the latest code from origin (current branch)
#   4. Runs uv sync to install any new/updated dependencies
#   5. Restarts the Cora service task
#   6. Tails the log to confirm a clean start
#
# What is included in this deployment batch (2026-05-30):
#   - Per-user Gmail inbox tool
#   - Email completion signals wired into fndr_completion_candidates
#   - Calendar multi-slot scheduling: up to 3 time options presented to user
#   - Google Meet link auto-included in every booked calendar event
#   - Completion-candidates timeout fix (was hanging on 13K-signal KB)
#   - OSN shift scheduling system + 40-employee seed data
#   - cora-kq-* channel routing (all 14 KQ channels now route correctly)

$ErrorActionPreference = "Stop"

$RepoRoot = "C:\Users\Harri\code\cora"
$TaskName = "cowork-cora-service"

Write-Host ""
Write-Host "=== Cora Deploy: $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" -ForegroundColor Cyan
Write-Host ""

# ------------------------------------------------------------------
# [1/6] Pre-flight checks
# ------------------------------------------------------------------
Write-Host "[1/6] Pre-flight checks..." -ForegroundColor White

if (-not (Test-Path $RepoRoot -PathType Container)) {
    Write-Error "Repo not found at $RepoRoot"
    exit 1
}
Write-Host "  OK  Repo: $RepoRoot"

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
if (-not $uvExe) {
    Write-Error "uv.exe not found. Install uv first: https://docs.astral.sh/uv/"
    exit 1
}
Write-Host "  OK  uv: $uvExe"

# ------------------------------------------------------------------
# [2/6] Stop the running service (if active)
# ------------------------------------------------------------------
Write-Host "[2/6] Stopping Cora service task..." -ForegroundColor White

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task -and $task.State -eq "Running") {
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    Write-Host "  OK  Task stopped."
} elseif ($task) {
    Write-Host "  OK  Task was not running (state: $($task.State))."
} else {
    Write-Host "  --  Task '$TaskName' not found - skipping stop."
}

# Also kill any lingering python processes running cora (belt-and-suspenders)
$coraPids = Get-Process python* -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowTitle -eq "" } |
    Select-Object -ExpandProperty Id
if ($coraPids) {
    Write-Host "  Killing $($coraPids.Count) lingering python process(es)..." -ForegroundColor Yellow
    $coraPids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

# ------------------------------------------------------------------
# [3/6] Pull latest code
# ------------------------------------------------------------------
Write-Host "[3/6] Pulling latest code..." -ForegroundColor White

# Git writes progress to stderr even on success. Temporarily relax
# ErrorActionPreference so those lines don't abort the script, then
# check $LASTEXITCODE to catch real failures.
Push-Location $RepoRoot
try {
    $branch = (& git rev-parse --abbrev-ref HEAD 2>&1)
    Write-Host "  Branch: $branch"

    $savedEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    & git fetch origin 2>&1 | ForEach-Object { Write-Host "  $_" }

    & git pull origin $branch 2>&1 | ForEach-Object { Write-Host "  $_" }
    $pullExit = $LASTEXITCODE

    $ErrorActionPreference = $savedEAP

    if ($pullExit -ne 0) {
        Write-Error "git pull exited with code $pullExit - resolve any conflicts before proceeding."
        exit $pullExit
    }

    $commitHash = (& git rev-parse --short HEAD 2>&1)
    Write-Host "  OK  At commit: $commitHash"
} finally {
    Pop-Location
}

# ------------------------------------------------------------------
# [4/6] Sync dependencies
# ------------------------------------------------------------------
Write-Host "[4/6] Syncing dependencies (uv sync)..." -ForegroundColor White

Push-Location $RepoRoot
try {
    $savedEAP2 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $uvExe sync 2>&1 | ForEach-Object { Write-Host "  $_" }
    $syncExit = $LASTEXITCODE
    $ErrorActionPreference = $savedEAP2
    if ($syncExit -ne 0) {
        Write-Error "uv sync exited with code $syncExit"
        exit $syncExit
    }
    Write-Host "  OK  Dependencies synced."
} finally {
    Pop-Location
}

# ------------------------------------------------------------------
# [5/6] Restart service
# ------------------------------------------------------------------
Write-Host "[5/6] Starting Cora service task..." -ForegroundColor White

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 6

    $state = (Get-ScheduledTask -TaskName $TaskName).State
    if ($state -eq "Running") {
        Write-Host "  OK  Task is Running." -ForegroundColor Green
    } else {
        Write-Host "  WARN  Task state: $state (expected Running)" -ForegroundColor Yellow
    }
} else {
    Write-Host "  --  Task '$TaskName' not registered. Start Cora manually:" -ForegroundColor Yellow
    Write-Host "        uv run cora"
}

# ------------------------------------------------------------------
# [6/6] Smoke-check log
# ------------------------------------------------------------------
Write-Host "[6/6] Checking startup log..." -ForegroundColor White

$today   = Get-Date -Format "yyyy-MM-dd"
$logPath = "$RepoRoot\logs\cora-$today.log"
$logGlob = "$RepoRoot\logs\cora-*.log"

Start-Sleep -Seconds 4

if (Test-Path $logPath) {
    $tail = Get-Content $logPath -Tail 8
    foreach ($line in $tail) {
        Write-Host "  LOG  $line"
    }
} else {
    $recent = Get-ChildItem $logGlob -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime -Descending |
              Select-Object -First 1
    if ($recent) {
        Write-Host "  (log at $($recent.FullName))"
        $tail = Get-Content $recent.FullName -Tail 8
        foreach ($line in $tail) {
            Write-Host "  LOG  $line"
        }
    } else {
        Write-Host "  --  No log file found yet at $logPath"
        Write-Host "      Check again in a few seconds:"
        Write-Host "      Get-Content '$logPath' -Tail 20"
    }
}

Write-Host ""
Write-Host "=== Deploy complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Quick checks:" -ForegroundColor Cyan
Write-Host "  Get-Process python*"
Write-Host "  Get-Content '$logPath' -Tail 20"
Write-Host ""
Write-Host "Slack smoke test in #cora-build:" -ForegroundColor Cyan
Write-Host "  Mention Cora and say: ping"
Write-Host ""
Write-Host "New features in this build:" -ForegroundColor Cyan
Write-Host "  Gmail inbox tool    - ask Cora to read your inbox"
Write-Host "  Calendar scheduling - ask Cora to schedule a meeting (3 slot options + Meet link)"
Write-Host "  KQ channels         - cora-kq-osn/f3e/lex/bdm etc. now route to correct entity"
