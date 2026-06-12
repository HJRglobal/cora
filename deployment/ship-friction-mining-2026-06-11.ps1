# Ship: Org Synthesis Phase 3 -- efficiency mining pass (friction_mining)
#
# Steps: import smoke -> full pytest -> commit/push (THIS session's files only)
#        -> register weekly task (Sunday 17:30 AZ).
#
# NO Cora restart needed: friction_mining is a standalone scheduled script and
# the run_knowledge_review.py executor change is also script-side. No bot-process
# file (app.py / tool_dispatch.py / claude_client.py) is touched.
#
# Run from PowerShell (elevated recommended for task registration):
#     cd C:\Users\Harri\code\cora
#     .\deployment\ship-friction-mining-2026-06-11.ps1
#
# Optional: -SkipTask to skip scheduled-task registration.

param(
    [switch]$SkipTask
)

$ErrorActionPreference = "Stop"
$RepoRoot  = "C:\Users\Harri\code\cora"
$PythonExe = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"

Set-Location $RepoRoot

Write-Host "=== Step 1: import smoke ===" -ForegroundColor Cyan
& $PythonExe -c "import sys; sys.path.insert(0, r'C:\Users\Harri\code\cora\src'); import cora.friction_mining; bad=[m for m in ('cora.app','cora.tool_dispatch','cora.claude_client') if m in sys.modules]; assert not bad, f'bot modules pulled: {bad}'; print('import smoke OK (no bot-process modules)')"
if ($LASTEXITCODE -ne 0) { Write-Error "Import smoke FAILED -- aborting."; exit 1 }

Write-Host "=== Step 2: full pytest ===" -ForegroundColor Cyan
& $PythonExe -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Error "Pytest FAILED -- aborting. Nothing committed."; exit 1 }

Write-Host "=== Step 3: commit + push (this session's files only) ===" -ForegroundColor Cyan
git add `
    src/cora/friction_mining.py `
    scripts/run_friction_mining.py `
    scripts/run_knowledge_review.py `
    deployment/setup-friction-mining-task.ps1 `
    deployment/ship-friction-mining-2026-06-11.ps1 `
    tests/test_friction_mining.py
git commit -m "feat(friction): Org Synthesis Phase 3 -- weekly efficiency mining pass (Harrison-gated, D-011/D-029/D-030)"
if ($LASTEXITCODE -ne 0) { Write-Error "Commit failed -- aborting."; exit 1 }
git push origin main
if ($LASTEXITCODE -ne 0) { Write-Error "Push failed -- commit is local only."; exit 1 }

if (-not $SkipTask) {
    Write-Host "=== Step 4: register weekly task ===" -ForegroundColor Cyan
    & "$RepoRoot\deployment\setup-friction-mining-task.ps1"
} else {
    Write-Host "=== Step 4: SKIPPED (register later via setup-friction-mining-task.ps1) ===" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== Ship complete ===" -ForegroundColor Green
Write-Host "No Cora restart needed (standalone script-side change)."
Write-Host "Rollout gate: review the --dry-run findings before the first live run:"
Write-Host "  & '$PythonExe' '$RepoRoot\scripts\run_friction_mining.py' --dry-run"
