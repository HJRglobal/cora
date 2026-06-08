# rescue-git-and-ship-lex-tagging-2026-06-07.ps1
# REPLACES verify-lex-tagging-2026-06-07.ps1 (which aborted at Step 1 -- git stderr
# under ErrorActionPreference=Stop, plus the pack corruption is real).
#
# Root cause: git auto-maintenance ran during the 6/07 Cowork sandbox session and
# rewrote packs over the virtiofs mount. pack-c44d7e5a is corrupt (object de2e7c04
# fails its hash check). origin/main (bcb997e) holds every pushed object; commit
# 2e0c2a4 and its tree/blobs are loose files, unaffected.
#
# What this does:
#   1. Backup the corrupt pack, clean git tmp litter
#   2. Fresh bare clone from origin -> transplant its packs in
#   3. Quarantine the corrupt pack + multi-pack-index, verify the other 6/08 pack
#   4. git fsck ground truth + disable auto-gc/maintenance in this repo (anti-recurrence)
#   5. Import smoke + full pytest
#   6. Commit docs + push to origin/main
#   7. Stop Cora -> catch-up backfill (~5,906 chunks) -> start Cora -> heartbeat
#
# Run from an ELEVATED PowerShell. ASCII-only per D-016.

$ErrorActionPreference = "Continue"
$repo = "C:\Users\Harri\code\cora"
$py = "$repo\.venv\Scripts\python.exe"
$rescue = "C:\Users\Harri\code\cora-rescue.git"
$backup = "$repo\.git-corrupt-backup"
$badpack = "pack-c44d7e5a5cd167ba14d1e9e1e4ce0fb846a4d665"
$otherpack = "pack-28f3daa761fe9d01a59791a0e07d41ebe18fcd63"

Set-Location $repo

Write-Host "=== Step 1: backup corrupt pack + clean tmp litter ==="
New-Item -ItemType Directory -Force -Path $backup | Out-Null
Copy-Item ".git\objects\pack\$badpack.*" $backup -Force
Get-ChildItem ".git\objects" -Recurse -Filter "tmp_obj_*" | Remove-Item -Force
Remove-Item ".git\objects\de\tmp_test_file" -Force -ErrorAction SilentlyContinue
Write-Host "Backed up to $backup"

Write-Host "=== Step 2: fresh bare clone from origin ==="
if (Test-Path $rescue) { Remove-Item $rescue -Recurse -Force }
git clone --bare https://github.com/HJRglobal/cora.git $rescue
if ($LASTEXITCODE -ne 0) { Write-Host "CLONE FAILED -- aborting. Nothing was changed in the repo." -ForegroundColor Red; exit 1 }
Copy-Item "$rescue\objects\pack\*" "$repo\.git\objects\pack\" -Force
Write-Host "Rescue packs transplanted."

Write-Host "=== Step 3: quarantine corrupt pack + multi-pack-index ==="
Move-Item ".git\objects\pack\$badpack.pack" $backup -Force
Move-Item ".git\objects\pack\$badpack.idx" $backup -Force -ErrorAction SilentlyContinue
Move-Item ".git\objects\pack\$badpack.rev" $backup -Force -ErrorAction SilentlyContinue
Remove-Item ".git\objects\pack\multi-pack-index" -Force -ErrorAction SilentlyContinue
Write-Host "Verifying the other 6/08 pack ($otherpack)..."
git verify-pack ".git\objects\pack\$otherpack.idx" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Other 6/08 pack is ALSO bad -- quarantining it (origin packs cover its contents)." -ForegroundColor Yellow
    Move-Item ".git\objects\pack\$otherpack.pack" $backup -Force
    Move-Item ".git\objects\pack\$otherpack.idx" $backup -Force -ErrorAction SilentlyContinue
    Move-Item ".git\objects\pack\$otherpack.rev" $backup -Force -ErrorAction SilentlyContinue
} else {
    Write-Host "Other 6/08 pack verifies clean -- keeping it."
}

Write-Host "=== Step 4: fsck ground truth + disable auto-gc (anti-recurrence) ==="
$fsck = git fsck --no-dangling 2>&1
$fsck | Select-Object -First 15
if ($LASTEXITCODE -ne 0) {
    Write-Host "FSCK STILL FAILING after transplant -- stop here and show Cowork the output above." -ForegroundColor Red
    exit 1
}
Write-Host "fsck clean."
git config gc.auto 0
git config maintenance.auto false
Write-Host "Local repo config: gc.auto=0, maintenance.auto=false (sandbox sessions must never auto-repack over virtiofs)."
# sanity: both heads readable
git log --oneline -2 main
git log --oneline -1 "refs/heads/claude/cora-code-review-fixes-Zjg1S" 2>&1 | Select-Object -First 1

Write-Host "=== Step 5: import smoke + full pytest ==="
& $py -c "from src.cora.app import app"
if ($LASTEXITCODE -ne 0) { Write-Host "IMPORT SMOKE TEST FAILED -- aborting." -ForegroundColor Red; exit 1 }
Write-Host "Import OK"
& $py -m pytest tests/ -q
if ($LASTEXITCODE -ne 0) { Write-Host "PYTEST FAILED -- aborting before push." -ForegroundColor Red; exit 1 }

Write-Host "=== Step 6: commit docs + push ==="
git add CLAUDE.md verify-lex-tagging-2026-06-07.ps1 rescue-git-and-ship-lex-tagging-2026-06-07.ps1
git commit -m "docs: TOM entry + host scripts for LEX sub-entity tagging Part 2 (2e0c2a4); git pack rescue"
git push origin main
if ($LASTEXITCODE -ne 0) { Write-Host "PUSH FAILED -- aborting." -ForegroundColor Red; exit 1 }

Write-Host "=== Step 7: stop Cora -> backfill -> start Cora ==="
Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*cora*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
& $py scripts\backfill_lex_sub_entity.py --dry-run
$answer = Read-Host "Dry-run above. Apply for real? (y/n)"
if ($answer -eq "y") { & $py scripts\backfill_lex_sub_entity.py }
Start-ScheduledTask -TaskName "cowork-cora-service"
Start-Sleep -Seconds 70
Get-Content "$repo\data\health\heartbeat.txt" | Select-Object -First 3
Write-Host "Done. Confirm heartbeat timestamp above is fresh."
Write-Host "Cleanup when satisfied: remove $backup and $rescue"
Write-Host "Slack smoke test: @Cora what's the revalidation status? (in an #lts or #llc channel)"
