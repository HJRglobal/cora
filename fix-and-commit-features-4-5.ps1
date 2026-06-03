# fix-and-commit-features-4-5.ps1
# Run from an elevated PowerShell prompt in C:\Users\Harri\code\cora
# Clears git locks, runs tests, and commits Features #2, #4, and #5.
#
# Usage (from elevated PowerShell):
#   cd C:\Users\Harri\code\cora
#   Set-ExecutionPolicy RemoteSigned -Scope Process
#   .\fix-and-commit-features-4-5.ps1

Set-Location "C:\Users\Harri\code\cora"
$PythonPath = Join-Path $PWD ".venv\Scripts\python.exe"

# ---- Step 1: Kill any background git processes --------------------------------
Write-Host "Step 1: Kill any background git processes..." -ForegroundColor Cyan
Get-Process -Name "git" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

# ---- Step 2: Remove stale git lock files -------------------------------------
Write-Host "Step 2: Remove stale git lock files..." -ForegroundColor Cyan
@(
    ".git\index.lock",
    ".git\HEAD.lock",
    ".git\refs\heads\main.lock",
    ".git\objects\maintenance.lock"
) | ForEach-Object {
    if (Test-Path $_) {
        Remove-Item $_ -Force
        Write-Host "  Removed: $_"
    }
}

# ---- Step 3: Restore index from HEAD -----------------------------------------
Write-Host "Step 3: Restore index from HEAD..." -ForegroundColor Cyan
git read-tree HEAD
if ($LASTEXITCODE -ne 0) { Write-Error "git read-tree failed"; exit 1 }

# ---- Step 4: Run import smoke test -------------------------------------------
Write-Host "Step 4: Import smoke test..." -ForegroundColor Cyan
& $PythonPath -c "from src.cora.app import app; print('  Import OK')"
if ($LASTEXITCODE -ne 0) { Write-Error "Import smoke test FAILED -- aborting"; exit 1 }

# ---- Step 5: Run new tests only (fast gate before full suite) -----------------
Write-Host "Step 5: Running new feature tests..." -ForegroundColor Cyan
& $PythonPath -m pytest `
    tests/test_deal_aging_alerts.py `
    tests/test_hubspot_two_way.py `
    tests/test_per_role_briefing.py `
    tests/test_meeting_action_capture.py `
    tests/test_hubspot_deal_monitor.py `
    -v --tb=short 2>&1 | Tee-Object -Variable testOutput
if ($LASTEXITCODE -ne 0) {
    Write-Error "New feature tests FAILED -- aborting commits"
    exit 1
}
Write-Host "New feature tests passed." -ForegroundColor Green

# ---- Step 6: Full test suite (confirm nothing broken) ------------------------
Write-Host "Step 6: Full test suite..." -ForegroundColor Cyan
& $PythonPath -m pytest tests/ -x -q 2>&1 | Tee-Object -Variable fullOutput | Select-Object -Last 10
if ($LASTEXITCODE -ne 0) {
    Write-Error "Full test suite FAILED -- review output above before committing"
    exit 1
}
Write-Host "Full suite passed." -ForegroundColor Green

# ---- Step 7: Commit Feature #4 -----------------------------------------------
Write-Host "Step 7: Committing Feature #4..." -ForegroundColor Cyan
git add scripts/run_deal_aging_alerts.py
git add tests/test_deal_aging_alerts.py
git add deployment/setup-deal-aging-alerts-task.ps1
git commit -m "feat: Feature #4 -- Deal Aging Alerts to owner DM"
if ($LASTEXITCODE -ne 0) { Write-Error "Commit Feature #4 failed"; exit 1 }

# ---- Step 8: Commit Feature #5 -----------------------------------------------
Write-Host "Step 8: Committing Feature #5..." -ForegroundColor Cyan
git add src/cora/tools/hubspot_client.py
git add src/cora/tools/tool_dispatch.py
git add tests/test_hubspot_two_way.py
git commit -m "feat: Feature #5 -- Two-way HubSpot updates from Slack (update stage + add note)"
if ($LASTEXITCODE -ne 0) { Write-Error "Commit Feature #5 failed"; exit 1 }

# ---- Step 9: Commit Feature #2 -----------------------------------------------
Write-Host "Step 9: Committing Feature #2..." -ForegroundColor Cyan
git add scripts/run_daily_briefing.py
git add data/maps/role-briefing-config.yaml
git add tests/test_per_role_briefing.py
git commit -m "feat: Feature #2 -- Per-role entity briefings with HubSpot/financial/aging data"
if ($LASTEXITCODE -ne 0) { Write-Error "Commit Feature #2 failed"; exit 1 }

# ---- Done -------------------------------------------------------------------
Write-Host ""
Write-Host "All 3 features committed. Recent git log:" -ForegroundColor Green
git log --oneline -8
