# Ship script: Gmail sweep coverage + resumability fix (2026-06-08)
#
# Staged in a Cowork sandbox session; this script does the authoritative host-side
# ship: import smoke -> full pytest -> commit/push -> re-register kb-sync tasks ->
# optional one-time deep backfill.
#
# NO cowork-cora-service restart is required: the Gmail sweep is an independent
# scheduled task (cowork-cora-kb-sync-gmail), not the bot process.
#
# ASCII-only per doctrine D-016. Run from ELEVATED PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\ship-gmail-sweep-coverage-2026-06-08.ps1

$ErrorActionPreference = "Stop"
$RepoRoot  = "C:\Users\Harri\code\cora"
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
Set-Location $RepoRoot

if (-not (Test-Path $PythonExe)) { Write-Error "venv python not found at $PythonExe"; exit 1 }

Write-Host "== Step 1: import smoke ==" -ForegroundColor Cyan
& $PythonExe -c "from src.cora.app import app; print('import smoke OK')"
if ($LASTEXITCODE -ne 0) { Write-Error "Import smoke FAILED -- aborting."; exit 1 }

Write-Host "== Step 2: YAML parse check ==" -ForegroundColor Cyan
& $PythonExe -c "import yaml; d=yaml.safe_load(open('data/maps/monitored-email-accounts.yaml',encoding='utf-8')); a=[x for x in d['accounts'] if x.get('enabled') and x.get('thread_sweep',True)]; print('thread_sweep accounts:', len(a))"
if ($LASTEXITCODE -ne 0) { Write-Error "YAML parse FAILED -- aborting."; exit 1 }

Write-Host "== Step 3: full pytest ==" -ForegroundColor Cyan
& $PythonExe -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Error "pytest FAILED -- aborting before commit."; exit 1 }

Write-Host "== Step 4: commit + push ==" -ForegroundColor Cyan
# Clear a stale index.lock (a crashed/concurrent git can leave a 0-byte lock that
# blocks add/commit). Only remove it if no git process is actually running.
$lock = Join-Path $RepoRoot ".git\index.lock"
if (Test-Path $lock) {
    $gitProcs = Get-Process git -ErrorAction SilentlyContinue
    if (-not $gitProcs) {
        Write-Host "  Removing stale .git\index.lock (no git process running)" -ForegroundColor Yellow
        Remove-Item $lock -Force
    } else {
        Write-Error "index.lock present AND a git process is running -- aborting. Investigate before retrying."
        exit 1
    }
}
git add scripts/gmail_threaded_sweep.py `
        src/cora/knowledge_base/schema.py `
        data/maps/monitored-email-accounts.yaml `
        deployment/setup-kb-sync-tasks.ps1 `
        deployment/ship-gmail-sweep-coverage-2026-06-08.ps1 `
        tests/test_gmail_threaded_sweep.py `
        CLAUDE.md
git commit -m "fix(gmail-sweep): resumable + stale-first + cap-aware; +6 DWD mailboxes; venv python; 3h limit

Gmail KB sweep had silently stalled since 2026-05-28: 1h Task Scheduler limit killed
the run 14/28 accounts in, watermarks never persisted (written only at end-of-run),
so early accounts re-scanned a growing backlog nightly and 14 mailboxes (all LEX + all
UFL + 2 F3E) were never reached. Read/unread was never the issue (after: covers both).

- gmail_threaded_sweep.py: per-account atomic watermark (resumable), stale-first
  ordering, cap-aware watermark (no silent backlog drop), --max-threads/--accounts.
- monitored-email-accounts.yaml: +6 DWD mailboxes (Eric, Daniel, Jake, Micah,
  Elena, tommy@hjrglobal.com), thread_sweep only. Demi excluded per Harrison.
- setup-kb-sync-tasks.ps1: uv run -> .venv python (D-005); gmail limit 1h -> 3h;
  ASCII-only (D-016).
- schema.connect: PRAGMA busy_timeout=30000 so a concurrent writer waits instead
  of crashing with 'database is locked' (fixes the backfill-vs-live-bot collision).
- gmail_threaded_sweep: _upsert_with_retry backs off on transient KB locks.
- tests: +8 (_order_accounts, _next_watermark)."
git push
if ($LASTEXITCODE -ne 0) { Write-Error "git push FAILED."; exit 1 }
Write-Host "Pushed. HEAD:" -ForegroundColor Green
git rev-parse --short HEAD

Write-Host "== Step 5: re-register kb-sync tasks (applies venv python + 3h gmail limit) ==" -ForegroundColor Cyan
& "$RepoRoot\deployment\setup-kb-sync-tasks.ps1"

Write-Host ""
Write-Host "Core ship complete. HEAD above; record it + test count in CLAUDE.md TOM." -ForegroundColor Green
Write-Host ""
Write-Host "== Step 6 (OPTIONAL, recommended): one-time deep backfill ==" -ForegroundColor Cyan
Write-Host "Drains the 5/28 backlog + the 14 previously-dark mailboxes now instead of" -ForegroundColor Yellow
Write-Host "over several nights. Resumable + idempotent; safe to re-run. May take 1-3h." -ForegroundColor Yellow
$ans = Read-Host "Run deep backfill now? (y/N)"
if ($ans -eq "y" -or $ans -eq "Y") {
    Write-Host "Running deep backfill (fallback 400d, max 2000 threads/account)..." -ForegroundColor Cyan
    & $PythonExe "$RepoRoot\scripts\gmail_threaded_sweep.py" --fallback-days 400 --max-threads 2000
    Write-Host "Backfill run finished (exit $LASTEXITCODE)." -ForegroundColor Green
} else {
    Write-Host "Skipped. The next 2:30am run will self-heal stale-first." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "== Verify ==" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  Select-String 'sweep complete' $RepoRoot\logs\kb-sync-gmail-$today.log"
Write-Host "  Get-Item $RepoRoot\data\cache\gmail-thread-watermarks.json | Select LastWriteTime"
Write-Host "  (expect a fresh mtime + ~33 advancing entries)"
