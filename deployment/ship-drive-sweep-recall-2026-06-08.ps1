# Ship script: Drive/Sheets sweep recall fixes (2026-06-08)
#
# Staged in a Cowork sandbox session; this script does the authoritative host-side
# ship: import smoke -> full pytest -> commit/push (this session's files ONLY) ->
# optional Cora restart -> optional one-time all-time re-backfill.
#
# What shipped (makes the existing nightly Drive+Sheets sweep recall complete):
#   1. embeddings.py    token-aware batching (stops large files 400ing at 300K tokens)
#   2. drive_sweep.py   Sheets API fallback when Drive export 403s
#                       (exportSizeLimitExceeded); row cap 200 -> 5000 per tab
#   3. drive_entity_detect.py  deterministic filename entity override (stops Haiku
#                       tagging OSN/HJRP files as LEX)
#
# IMPORTANT: this script commits ONLY this session's files. It does NOT touch the
# separately-staged Gmail sweep work (ship-gmail-sweep-coverage-2026-06-08.ps1).
#
# ASCII-only per doctrine D-016. Run from ELEVATED PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\ship-drive-sweep-recall-2026-06-08.ps1

$ErrorActionPreference = "Stop"
$RepoRoot  = "C:\Users\Harri\code\cora"
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
Set-Location $RepoRoot

if (-not (Test-Path $PythonExe)) { Write-Error "venv python not found at $PythonExe"; exit 1 }

Write-Host "== Step 0: PREREQ -- Sheets API DWD scope ==" -ForegroundColor Cyan
Write-Host "The oversized-sheet fallback uses the Sheets API. The Cora service account"
Write-Host "DWD grant must include this scope (one-time, in admin.google.com ->"
Write-Host "Security -> API Controls -> Domain-wide Delegation -> edit the Cora SA):"
Write-Host "    https://www.googleapis.com/auth/spreadsheets.readonly" -ForegroundColor Yellow
Write-Host "Without it the fallback degrades gracefully (oversized sheets stay dropped,"
Write-Host "same as today) -- the code ships safely either way, but the re-backfill will"
Write-Host "only recover oversized sheets once the scope is granted."
Write-Host ""

Write-Host "== Step 1: import smoke ==" -ForegroundColor Cyan
& $PythonExe -c "from src.cora.app import app; print('import smoke OK')"
if ($LASTEXITCODE -ne 0) { Write-Error "Import smoke FAILED -- aborting."; exit 1 }

Write-Host "== Step 2: full pytest ==" -ForegroundColor Cyan
& $PythonExe -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Error "pytest FAILED -- aborting before commit."; exit 1 }

Write-Host "== Step 3: commit + push (this session's files ONLY) ==" -ForegroundColor Cyan
git add src/cora/knowledge_base/embeddings.py `
        src/cora/connectors/drive_sweep.py `
        src/cora/connectors/drive_entity_detect.py `
        tests/test_embeddings_batching.py `
        tests/test_drive_entity_detect.py `
        tests/test_drive_sweep_sheets.py `
        deployment/ship-drive-sweep-recall-2026-06-08.ps1 `
        CLAUDE.md
git commit -m "feat(drive-sweep): complete Sheets recall -- API fallback, token batching, entity override

The nightly multi-user Drive+Sheets sweep ran fine but dropped its most data-rich
content. Logs (5/28-6/07) showed three recall gaps, all on Sheets/large files:

- Large Google Sheets 403d on Drive export (exportSizeLimitExceeded, 17 hits) and
  were dropped entirely. drive_sweep now falls back to the Sheets API values
  reader (no export size ceiling) per tab. Requires the SA DWD grant to add
  spreadsheets.readonly; degrades gracefully if absent.
- Large files 400d at embed time (OpenAI 300K-token request limit, 10 hits) and
  the whole file was lost. embeddings.embed_texts now batches by BOTH count and a
  250K-token budget; a single oversized input becomes its own batch (never dropped).
- Sheet extraction truncated to the first 200 rows/tab. Raised to 5000.

Also: drive_entity_detect.py adds a deterministic HJR-naming-convention entity
override applied after Haiku in sweep_user, so OSN/HJRP files stop being mis-tagged
LEX (observed 6/06-6/07). Founder-OS path unchanged (folder path already wins).

Tests: +3 files (token batching, filename detection, Sheets fallback + row cap)."
git push
if ($LASTEXITCODE -ne 0) { Write-Error "git push FAILED."; exit 1 }
Write-Host "Pushed. HEAD:" -ForegroundColor Green
git rev-parse --short HEAD

Write-Host ""
Write-Host "== Step 4 (recommended): restart Cora so the live bot uses token-aware batching ==" -ForegroundColor Cyan
Write-Host "The nightly sweep tasks pick up new code on their next run automatically, so a"
Write-Host "restart is not strictly required. Restart anyway so any runtime KB upsert the bot"
Write-Host "does also uses the new batching. Doctrine #5 sequence:"
$ans = Read-Host "Restart cowork-cora-service now? (y/N)"
if ($ans -eq "y" -or $ans -eq "Y") {
    Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*cora*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
    Start-ScheduledTask -TaskName "cowork-cora-service"
    Write-Host "Restarted. Confirm heartbeat:" -ForegroundColor Green
    Write-Host "  Get-Content $RepoRoot\data\health\heartbeat.txt"
} else {
    Write-Host "Skipped restart. Sweep tasks still pick up the fix on their next run." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "== Step 5 (the actual 'full sweep'): one-time all-time re-backfill ==" -ForegroundColor Cyan
Write-Host "Re-sweeps ALL drive_sweep accounts ignoring watermarks, so previously-dropped"
Write-Host "oversized sheets + large files are recovered, full rows ingested, and entity"
Write-Host "tags corrected. Resumable per-account via checkpoints; idempotent (upsert"
Write-Host "replace-on-conflict, no duplicate chunks)." -ForegroundColor Yellow
Write-Host "COST NOTE: --backfill re-embeds the whole corpus -> real OpenAI spend"
Write-Host "proportional to corpus size. The wide window (all-time) pulls files older than"
Write-Host "the 730-day default too. Narrow the window if you want to limit cost." -ForegroundColor Yellow
$days = Read-Host "Freshness window in days for the backfill (Enter=36500 all-time, or e.g. 1095 for 3y, or 's' to skip)"
if ($days -eq "s" -or $days -eq "S") {
    Write-Host "Skipped re-backfill. The nightly 3:30am sweep will pick up newly-modified" -ForegroundColor Yellow
    Write-Host "files with the fixes, but previously-dropped older sheets will NOT be"
    Write-Host "recovered until a --backfill is run."
} else {
    if ([string]::IsNullOrWhiteSpace($days)) { $days = "36500" }
    Write-Host "Running all-account re-backfill (freshness-days=$days, with Slack summary)..." -ForegroundColor Cyan
    & $PythonExe "$RepoRoot\scripts\run_drive_sweep.py" --backfill --freshness-days $days --with-slack
    Write-Host "Re-backfill finished (exit $LASTEXITCODE)." -ForegroundColor Green
}

Write-Host ""
Write-Host "== Verify ==" -ForegroundColor Cyan
$today = Get-Date -Format "yyyy-MM-dd"
Write-Host "  Select-String 'COMPLETE' $RepoRoot\logs\drive-sweep-$today.log | Select -Last 1"
Write-Host "  Select-String 'recovered oversized sheet' $RepoRoot\logs\drive-sweep-$today.log"
Write-Host "  (expect recovered-sheet lines if oversized sheets exist + the scope is granted)"
Write-Host "Record HEAD + test count in CLAUDE.md TOM."
