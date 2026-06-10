# Recover the uncommitted backlog that the stale .git\index.lock has been blocking.
#
# Context: multiple sessions' work piled up uncommitted because a stale index.lock
# silently failed every commit. All of it is ALREADY running live (Cora imports from
# the working tree), so the real safety gate is "does the full test suite pass."
#
# This script:
#   0. Aborts if a git process is running (so we never fight a live git op).
#   1. Clears a stale index.lock and excludes junk (.fuse_hidden*, backups, corrupt-pack).
#   2. Runs import smoke + the FULL test suite on the current tree. Aborts if red --
#      nothing is committed and your work stays safely in the working tree.
#   3. Commits in two logical commits (gmail; then a catch-up for everything else)
#      and PUSHES after each (push to origin = the durable backup).
#   4. Verifies HEAD actually advanced (the check the ship scripts were missing).
#
# It does NOT restart Cora -- that is a separate, explicit step printed at the end.
# ASCII-only (D-016). Run from ELEVATED PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\recover-backlog-2026-06-08.ps1

$ErrorActionPreference = "Stop"
$RepoRoot  = "C:\Users\Harri\code\cora"
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
Set-Location $RepoRoot

function Abort($msg) { Write-Host "ABORT: $msg" -ForegroundColor Red; exit 1 }

if (-not (Test-Path $PythonExe)) { Abort "venv python not found at $PythonExe" }

Write-Host "== Step 0: guards ==" -ForegroundColor Cyan
if (Get-Process git -ErrorAction SilentlyContinue) {
    Abort "a git process is running. Let it finish, then re-run."
}
# Warn (do not block) if a KB sweep is still writing -- committing is git-only and safe,
# but a quiet tree is cleaner.
$sweeps = Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
          Where-Object { $_.CommandLine -and ($_.CommandLine -like '*drive_sweep*' -or $_.CommandLine -like '*gmail_threaded_sweep*') }
if ($sweeps) {
    Write-Host "  NOTE: a KB sweep still appears to be running. Safe to proceed (git does not touch the KB)," -ForegroundColor Yellow
    Write-Host "        but you may prefer to wait for it to finish. Continuing in 5s (Ctrl+C to stop)..." -ForegroundColor Yellow
    Start-Sleep 5
}

Write-Host "== Step 1: clear stale lock + exclude junk ==" -ForegroundColor Cyan
$lock = Join-Path $RepoRoot ".git\index.lock"
if (Test-Path $lock) { Remove-Item $lock -Force; Write-Host "  removed stale .git\index.lock" -ForegroundColor Yellow }

$excludeFile = Join-Path $RepoRoot ".git\info\exclude"
$excludePatterns = @("data/.fuse_hidden*", ".git-corrupt-backup/", "backups/")
$existing = if (Test-Path $excludeFile) { Get-Content $excludeFile } else { @() }
foreach ($p in $excludePatterns) {
    if ($existing -notcontains $p) { Add-Content -Path $excludeFile -Value $p }
}
Write-Host "  junk patterns excluded from git add" -ForegroundColor Green

Write-Host "== Step 2: safety gate (import smoke + full pytest on the live tree) ==" -ForegroundColor Cyan
& $PythonExe -c "from src.cora.app import app; print('import smoke OK')"
if ($LASTEXITCODE -ne 0) { Abort "import smoke failed. Work stays uncommitted in the tree. Fix the import, then re-run." }
& $PythonExe -m pytest -q
if ($LASTEXITCODE -ne 0) { Abort "pytest failed. Nothing committed -- your work is safe in the working tree. Fix tests, then re-run." }
Write-Host "  GREEN -- the full live tree is coherent and safe to commit." -ForegroundColor Green

$headBefore = (git rev-parse HEAD).Trim()

Write-Host "== Step 3a: commit gmail-sweep coverage ==" -ForegroundColor Cyan
git add scripts/gmail_threaded_sweep.py `
        src/cora/knowledge_base/schema.py `
        data/maps/monitored-email-accounts.yaml `
        deployment/setup-kb-sync-tasks.ps1 `
        deployment/ship-gmail-sweep-coverage-2026-06-08.ps1 `
        tests/test_gmail_threaded_sweep.py
git commit -m "fix(gmail-sweep): resumable + stale-first + cap-aware; +6 DWD mailboxes; busy_timeout; venv python; 3h limit"
if ($LASTEXITCODE -ne 0) { Abort "gmail commit failed (see error above)." }

Write-Host "== Step 3b: catch-up commit (drive recall, prompt-caching split, fireflies coverage/health, CLAUDE.md, state) ==" -ForegroundColor Cyan
git add -A
# If nothing remains staged, that's fine -- skip the commit.
$staged = (git diff --cached --name-only)
if ($staged) {
    git commit -m "chore(backlog): commit work blocked by stale index.lock since ~9:05 UTC 2026-06-08

Catch-up for changes already live in the working tree (Cora imports from it):
- drive-sweep recall fixes (drive_sweep.py, embeddings.py, drive_entity_detect.py + tests)
- prompt-caching split (app.py, context_loader.py load_context_parts, claude_client.py
  3-block cached system, test_caching_split.py)
- fireflies coverage + health report (fireflies_coverage.py, run_fireflies_coverage.py,
  cora_health_report.py, run_retroactive_hashtag_scan.py + tests)
- CLAUDE.md TOM entries; runtime state files
All validated together by the full pytest run gating this commit."
    if ($LASTEXITCODE -ne 0) { Abort "catch-up commit failed (see error above)." }
} else {
    Write-Host "  nothing left to commit after the gmail commit." -ForegroundColor Yellow
}

Write-Host "== Step 4: push + verify HEAD advanced ==" -ForegroundColor Cyan
git push
if ($LASTEXITCODE -ne 0) { Abort "git push failed." }
$headAfter = (git rev-parse HEAD).Trim()
if ($headAfter -eq $headBefore) { Abort "HEAD did not advance ($headBefore) -- commits did not land. Investigate." }

Write-Host ""
Write-Host "SUCCESS. HEAD moved $headBefore -> $headAfter" -ForegroundColor Green
git --no-pager log --oneline -4
Write-Host ""
Write-Host "Working tree status (should be clean except live runtime files that re-dirty):" -ForegroundColor Cyan
git status -s
Write-Host ""
Write-Host "NEXT: restart Cora to load busy_timeout + the prompt-caching split:" -ForegroundColor Cyan
Write-Host '  Stop-ScheduledTask -TaskName "cowork-cora-service"'
Write-Host '  Get-CimInstance Win32_Process -Filter "name=''python.exe''" | Where-Object { $_.CommandLine -like ''*cora*'' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }'
Write-Host '  Start-Sleep 3'
Write-Host '  Start-ScheduledTask -TaskName "cowork-cora-service"'
Write-Host '  Get-Content .\data\health\heartbeat.txt   # confirm fresh timestamp'
