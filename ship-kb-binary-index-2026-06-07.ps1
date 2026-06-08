# ship-kb-binary-index-2026-06-07.ps1
# Activates the KB binary-index fast path: backup -> migrate cora_kb.db ->
# bench -> restart Cora -> verify. Run from an ELEVATED PowerShell. ASCII-only.
#
# Background: KB vector search was ~31s cold (brute-force float scan of the
# ~1.4 GB index over 224K chunks). New code adds a binary-quantized coarse scan
# (~43 MB) + exact float32 re-rank. This script backfills the two new tables
# from existing vectors (NO re-embedding) and arms the fast path.
#
# The code (store.py / schema.py / context_loader.py / main.py) is already
# committed; until the migration runs, store.search() stays on the float
# fallback, so Cora is correct either way -- this just makes it fast.
#
# Does NOT modify git config (gc.auto/maintenance.auto stay as set by the
# 6/07 rescue). Does NOT touch the uncommitted gap_autofill working-tree files.

$ErrorActionPreference = "Continue"
$repo = "C:\Users\Harri\code\cora"
$py = "$repo\.venv\Scripts\python.exe"
$db = "$repo\data\cora_kb.db"
$stamp = Get-Date -Format "yyyy-MM-dd"
$backupDir = "$repo\backups\$stamp"

Set-Location $repo

Write-Host "=== Step 0: disk space + push the committed code ==="
$free = (Get-PSDrive C).Free / 1GB
Write-Host ("Free on C: {0:N1} GB (need ~8 GB headroom: 3.3 GB backup + ~1.4 GB f32 table)" -f $free)
if ($free -lt 8) { Write-Host "WARNING: low free space. Free up space before continuing." -ForegroundColor Yellow }
# The fast-path code commit is local (made in the sandbox). Push it now.
git push origin main
if ($LASTEXITCODE -ne 0) { Write-Host "git push failed (non-fatal for migration; retry later)." -ForegroundColor Yellow }

Write-Host "=== Step 1: stop Cora (service + orphan python) ==="
Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*cora*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 4

Write-Host "=== Step 2: backup cora_kb.db (Cora is stopped) ==="
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
Copy-Item $db "$backupDir\cora_kb.db.bak" -Force
foreach ($ext in @("-wal", "-shm")) {
    if (Test-Path "$db$ext") { Copy-Item "$db$ext" "$backupDir\cora_kb.db$ext.bak" -Force }
}
if (-not (Test-Path "$backupDir\cora_kb.db.bak")) { Write-Host "BACKUP FAILED -- aborting." -ForegroundColor Red; exit 1 }
Write-Host "Backed up to $backupDir"

Write-Host "=== Step 3: dry-run migration (counts only) ==="
& $py scripts\migrate_kb_binary_index.py --dry-run
if ($LASTEXITCODE -ne 0) { Write-Host "Dry-run failed -- aborting." -ForegroundColor Red; exit 1 }

$answer = Read-Host "Proceed with the real migration? (y/n)"
if ($answer -ne "y") { Write-Host "Aborted by user. Restarting Cora unchanged."; Start-ScheduledTask -TaskName "cowork-cora-service"; exit 0 }

Write-Host "=== Step 4: migrate (backfill bin + f32, arm fast path) ==="
& $py scripts\migrate_kb_binary_index.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "MIGRATION DID NOT COMPLETE (fast path not armed). Cora will use the float fallback (still correct). Re-run this script to finish. Restarting Cora..." -ForegroundColor Yellow
    Start-ScheduledTask -TaskName "cowork-cora-service"
    exit 1
}

Write-Host "=== Step 5: benchmark (latency + recall guard) ==="
& $py scripts\bench_kb_search.py
if ($LASTEXITCODE -ne 0) { Write-Host "RECALL GUARD FAILED. Investigate before relying on the fast path (it is armed but recall is below target)." -ForegroundColor Yellow }

Write-Host "=== Step 6: start Cora -> verify heartbeat ==="
Start-ScheduledTask -TaskName "cowork-cora-service"
Start-Sleep -Seconds 70
Get-Content "$repo\data\health\heartbeat.txt" | Select-Object -First 3

Write-Host ""
Write-Host "=== Done. Verification ==="
Write-Host "1. First @Cora mention in #llc after restart: tail logs\cora-$stamp.log --"
Write-Host "   - latency_ms should be < 10000 (cold), warm mentions < 5000"
Write-Host "   - there must be NO 'Knowledge Base schema initialized' line during a request"
Write-Host "   - look for 'kb-prewarm: vector index warmed in <1s'"
Write-Host "2. Slack smoke: '@Cora what's the revalidation status?' in #lts or #llc -> correct + fast"
Write-Host "3. Cleanup when satisfied: remove $backupDir (after a few days of stable operation)"
Write-Host "4. Future cleanup (separate session, after ~1 week stable): drop knowledge_vec to"
Write-Host "   reclaim ~1.4 GB -- it is now only the fallback; the fast path uses bin + f32."
