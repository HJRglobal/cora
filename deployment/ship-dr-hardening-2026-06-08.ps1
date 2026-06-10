# Ship the DR backup hardening (2026-06-08):
#   - backup_logs.py: encrypted secrets (.env + SA JSON), feature-DB backup, offsite verify
#   - restore_secrets.py: decrypt companion
#   - setup-backup-task.ps1: venv python (D-005) + 60-min limit
#   - tests/test_backup_logs.py
#
# Scoped commit (explicit paths only -- never git add -A, to avoid sweeping other
# sessions' work in this shared tree). Clears a stale index.lock, pytest-gates,
# pushes, and verifies HEAD advanced.
#
# ASCII-only (D-016). Run from ELEVATED PowerShell:
#     cd C:\Users\Harri\code\cora
#     .\deployment\ship-dr-hardening-2026-06-08.ps1

$ErrorActionPreference = "Stop"
$RepoRoot  = "C:\Users\Harri\code\cora"
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
Set-Location $RepoRoot

function Abort($m) { Write-Host "ABORT: $m" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $PythonExe)) { Abort "venv python not found at $PythonExe" }

Write-Host "== Step 1: clear stale lock if present ==" -ForegroundColor Cyan
if (Get-Process git -ErrorAction SilentlyContinue) { Abort "a git process is running; let it finish then re-run." }
$lock = Join-Path $RepoRoot ".git\index.lock"
if (Test-Path $lock) { Remove-Item $lock -Force; Write-Host "  removed stale index.lock" -ForegroundColor Yellow }

Write-Host "== Step 2: import smoke + tests ==" -ForegroundColor Cyan
& $PythonExe -c "from src.cora.app import app; print('import smoke OK')"
if ($LASTEXITCODE -ne 0) { Abort "import smoke failed." }
& $PythonExe -m pytest tests/test_backup_logs.py -q
if ($LASTEXITCODE -ne 0) { Abort "backup tests failed -- nothing committed." }

$headBefore = (git rev-parse HEAD).Trim()

Write-Host "== Step 3: scoped commit (explicit paths only) ==" -ForegroundColor Cyan
git add scripts/backup_logs.py `
        scripts/restore_secrets.py `
        deployment/setup-backup-task.ps1 `
        deployment/ship-dr-hardening-2026-06-08.ps1 `
        tests/test_backup_logs.py
git commit -m "feat(backup): DR hardening -- encrypted secrets + feature DBs + offsite verify; venv python + 60m limit

- backup_logs.py: bundle .env + SA JSON into one Fernet-encrypted blob
  (secrets-YYYY-MM-DD.enc, key from CORA_BACKUP_PASSPHRASE via PBKDF2; skipped if
  unset, never writes plaintext); online-backup the small feature DBs; verify the
  KB backup actually landed offsite and exit non-zero if not.
- restore_secrets.py: decrypt companion (restores .env + SA JSON to original paths).
- setup-backup-task.ps1: uv run -> .venv python (D-005); 10m -> 60m ExecutionTimeLimit
  (multi-GB KB online backup was getting killed at 10m). 1:00pm trigger preserved.
- tests/test_backup_logs.py: encrypt/decrypt round-trip, passphrase gating, offsite
  verify, feature-DB exclusion of cora_kb.db."
if ($LASTEXITCODE -ne 0) { Abort "commit failed (see error above)." }

Write-Host "== Step 4: push + verify HEAD advanced ==" -ForegroundColor Cyan
git push
if ($LASTEXITCODE -ne 0) { Abort "push failed. If non-fast-forward: git pull --rebase then git push." }
$headAfter = (git rev-parse HEAD).Trim()
if ($headAfter -eq $headBefore) { Abort "HEAD did not advance -- commit did not land." }

Write-Host ""
Write-Host "SUCCESS. HEAD $headBefore -> $headAfter" -ForegroundColor Green
git --no-pager log --oneline -3
Write-Host ""
Write-Host "=== ACTIVATION (do these to make it fully live) ===" -ForegroundColor Cyan
Write-Host "1. Pick a strong passphrase, store it in your password manager, and set it as a"
Write-Host "   PERSISTENT user env var (so the scheduled task sees it):"
Write-Host '     [Environment]::SetEnvironmentVariable("CORA_BACKUP_PASSPHRASE","<your-passphrase>","User")'
Write-Host "   (then sign out/in, or it is picked up by new processes)."
Write-Host "2. Re-register the backup task so it uses venv python + the 60-min limit:"
Write-Host "     .\deployment\setup-backup-task.ps1"
Write-Host "3. Test a real backup now (writes today's dated folder to Drive):"
Write-Host '     $env:CORA_BACKUP_PASSPHRASE="<your-passphrase>"   # for this shell'
Write-Host "     .venv\Scripts\python.exe scripts\backup_logs.py"
Write-Host "   Confirm a secrets-*.enc appears in the Drive backups\<today>\ folder and the"
Write-Host "   run ends with 'Offsite verify: PASS'."
