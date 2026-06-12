# ship-strategy-memo-2026-06-11.ps1
# Org Synthesis Phase 4: founder strategy layer (the final phase).
#   1. src/cora/strategy_memo.py -- weekly portfolio synthesis memo:
#      deterministic fail-soft gather (cash / pipeline / stalled decisions /
#      deadline radar / efficiency findings / KB momentum / health), snapshot
#      + week-over-week deltas, Sonnet synthesis FAIL-CLOSED (fallback =
#      factual rollup), Harrison-only DM + memo file under
#      00-Founder/_strategy-memos/ (static_md ingests it).
#   2. scripts/run_strategy_memo.py -- runner (--dry-run / --no-synth).
#   3. deployment/setup-strategy-memo-task.ps1 -- weekly Sunday 18:30 AZ
#      (one hour after Cora - Friction Mining; slot verified free 2026-06-11).
#
# NO Cora restart needed -- standalone scheduled script; the module never
# imports bot-process code (D-047 invariant, subprocess-tested).
#
# Run from PowerShell in C:\Users\Harri\code\cora.
# Pass -RegisterTask to also register the scheduled task (elevated PS).

param(
    [switch]$RegisterTask
)

$ErrorActionPreference = "Stop"
Set-Location "C:\Users\Harri\code\cora"

Write-Host "=== Step 0: clear stale git index.lock if present (D-041) ==="
$lockPath = ".git\index.lock"
if (Test-Path $lockPath) {
    $gitProcs = Get-Process git -ErrorAction SilentlyContinue
    $lockSize = (Get-Item $lockPath).Length
    if (-not $gitProcs -and $lockSize -eq 0) {
        Remove-Item $lockPath -Force
        Write-Host "Stale zero-byte index.lock removed."
    } else {
        Write-Error "index.lock present and either non-empty ($lockSize bytes) or a git process is running - investigate before shipping."
        exit 1
    }
}

Write-Host "=== Step 1: import smoke test ==="
& .venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'src'); import cora.strategy_memo; sys.path.insert(0, 'scripts'); import run_strategy_memo; bad = [m for m in ('cora.app', 'cora.tool_dispatch', 'cora.claude_client') if m in sys.modules]; assert not bad, f'bot-process modules imported: {bad}'"
if ($LASTEXITCODE -ne 0) { Write-Error "Import smoke FAILED - aborting."; exit 1 }
Write-Host "Import smoke OK (no bot-process modules pulled)"

Write-Host "=== Step 2: full pytest suite ==="
& .venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Error "Test suite FAILED - aborting. Nothing committed."; exit 1 }
Write-Host "Full suite green"

Write-Host "=== Step 3: commit this session's files only (explicit paths, D-041) ==="
git add src/cora/strategy_memo.py
git add scripts/run_strategy_memo.py
git add tests/test_strategy_memo.py
git add deployment/setup-strategy-memo-task.ps1
git add deployment/ship-strategy-memo-2026-06-11.ps1
git add CLAUDE.md decisions.md
git commit -m "feat(strategy): Org Synthesis Phase 4 -- weekly founder strategy memo (Harrison-only, Sonnet fail-closed)

Weekly Sunday 18:30 AZ standalone script. Fail-soft gather (cash via
Standing ACTUALS, HubSpot pipelines, stalled P0/P1 decisions, 14d Asana
deadline radar, efficiency backlog + pending friction findings, KB
momentum, heartbeat), snapshots to data/state/strategy-memo-snapshots/
for real week-over-week deltas + streaks, Sonnet synthesis FAIL-CLOSED
(factual-rollup fallback), DM to Harrison ONLY + memo file under
00-Founder/_strategy-memos/ (static_md ingest). LEX aggregate-only,
PHI/Visibility excluded, advisory-only (D-011), no bot-process imports
(D-047 invariant). Completes the Org Synthesis program."
if ($LASTEXITCODE -ne 0) { Write-Error "Commit FAILED."; exit 1 }

Write-Host "=== Step 4: push ==="
git push origin main
if ($LASTEXITCODE -ne 0) { Write-Error "Push FAILED."; exit 1 }
git log --oneline -1

if ($RegisterTask) {
    Write-Host "=== Step 5: register scheduled task ==="
    & .\deployment\setup-strategy-memo-task.ps1
} else {
    Write-Host "=== Step 5 (skipped): register the task with ==="
    Write-Host "  .\deployment\setup-strategy-memo-task.ps1   (elevated PS)"
}

Write-Host ""
Write-Host "Done. Rollout gate: review a dry run before the first scheduled fire:" -ForegroundColor Cyan
Write-Host "  .venv\Scripts\python.exe scripts\run_strategy_memo.py --dry-run"
