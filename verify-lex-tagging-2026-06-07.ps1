# verify-lex-tagging-2026-06-07.ps1
# Host-side verification + activation for commit 2e0c2a4 (LEX sub-entity KB tagging Part 2).
# Run from an ELEVATED PowerShell. ASCII-only per D-016.
#
# What this does, in order:
#   1. git fsck ground-truth check (sandbox FUSE view reported a pack warning -- verify on real disk)
#   2. Import smoke test (doctrine 6)
#   3. Full pytest suite (doctrine: before push)
#   4. Push to origin/main
#   5. Stop Cora (doctrine 5 restart sequence)
#   6. Catch-up backfill: tag the ~5,906 pre-existing NULL LEX chunks (service stopped = no DB contention)
#   7. Start Cora + heartbeat check

$ErrorActionPreference = "Stop"
$repo = "C:\Users\Harri\code\cora"
$py = "$repo\.venv\Scripts\python.exe"

Set-Location $repo

Write-Host "=== Step 1: git fsck (ground truth on host disk) ==="
git fsck --no-dangling 2>&1 | Select-Object -First 10
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: git fsck reported issues. Inspect before pushing." -ForegroundColor Yellow
    Write-Host "If pack corruption is real: git fetch origin; git reset --soft origin/main; re-commit."
    $answer = Read-Host "Continue anyway? (y/n)"
    if ($answer -ne "y") { exit 1 }
}

Write-Host "=== Step 2: import smoke test ==="
& $py -c "from src.cora.app import app"
if ($LASTEXITCODE -ne 0) { Write-Host "IMPORT SMOKE TEST FAILED -- aborting." -ForegroundColor Red; exit 1 }
Write-Host "Import OK"

Write-Host "=== Step 3: full pytest suite ==="
& $py -m pytest tests/ -q
if ($LASTEXITCODE -ne 0) { Write-Host "PYTEST FAILED -- aborting before push." -ForegroundColor Red; exit 1 }

Write-Host "=== Step 4: commit docs (host-side -- sandbox FUSE view of CLAUDE.md was stale) + push ==="
git add CLAUDE.md verify-lex-tagging-2026-06-07.ps1
git commit -m "docs: TOM entry + host verification script for LEX sub-entity tagging Part 2 (2e0c2a4)"
git push origin main
if ($LASTEXITCODE -ne 0) { Write-Host "PUSH FAILED -- aborting." -ForegroundColor Red; exit 1 }

Write-Host "=== Step 5: stop Cora (doctrine 5) ==="
Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*cora*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

Write-Host "=== Step 6: catch-up backfill (one-time, ~5,906 chunks) ==="
& $py scripts\backfill_lex_sub_entity.py --dry-run
$answer = Read-Host "Dry-run above. Apply for real? (y/n)"
if ($answer -eq "y") {
    & $py scripts\backfill_lex_sub_entity.py
} else {
    Write-Host "Backfill skipped -- new chunks still tagged at ingest; re-run script anytime."
}

Write-Host "=== Step 7: start Cora + heartbeat ==="
Start-ScheduledTask -TaskName "cowork-cora-service"
Start-Sleep -Seconds 70
Get-Content "$repo\data\health\heartbeat.txt" | Select-Object -First 3
Write-Host "Done. Confirm heartbeat timestamp above is fresh (less than 70s old)."
Write-Host "Smoke test in Slack: @Cora what's the revalidation status? (in #lts or #llc channel)"
