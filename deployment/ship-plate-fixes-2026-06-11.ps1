# ship-plate-fixes-2026-06-11.ps1
# Hotfix ship for the whats_on_my_plate 00:51 AZ live crash (Cowork bug report):
#   1. asana_client.py tail RESTORED (truncated on main since d5f2e6f 2026-06-03;
#      format_tasks_for_llm returned None for every non-empty task list)
#   2. plate composition hardened (_safe_plate_section; sections degrade, never crash)
#   3. calendar reads fixed (events.list needs calendar.events scope; probed live)
#   4. model router: plate queries force Sonnet
#   5. ship PS1 restart kill filter corrected (cora.exe pattern, instance verify)
#
# Run from PowerShell in C:\Users\Harri\code\cora.
# Commit/push runs non-elevated; pass -Restart from ELEVATED PS to restart
# (service runs -RunLevel Highest; D-036: non-elevated kills nothing).

param(
    [switch]$Restart
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
& .venv\Scripts\python.exe -c "from src.cora.app import app"
if ($LASTEXITCODE -ne 0) { Write-Error "Import smoke FAILED - aborting."; exit 1 }
Write-Host "Import smoke OK"

Write-Host "=== Step 2: full pytest suite ==="
& .venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Error "Test suite FAILED - aborting. Nothing committed."; exit 1 }
Write-Host "Full suite green"

Write-Host "=== Step 3: commit this session's files only (explicit paths, D-041) ==="
git add src/cora/tools/asana_client.py src/cora/tools/tool_dispatch.py src/cora/tools/calendar_client.py src/cora/model_router.py
git add tests/test_whats_on_my_plate.py tests/test_model_router.py
git add deployment/ship-whats-on-my-plate-2026-06-11.ps1 deployment/ship-plate-fixes-2026-06-11.ps1 scripts/flush_plate_cache.py CLAUDE.md
git commit -m "fix(plate): restore truncated asana_client + harden plate sections + calendar read scope + Sonnet routing

ROOT CAUSE of the 2026-06-11 00:51 AZ live crash: asana_client.py was
TRUNCATED ON MAIN since d5f2e6f (2026-06-03) - the file ended mid-loop
inside format_tasks_for_llm, so the function returned None for every
NON-EMPTY task list. asana_get_my_tasks silently returned None for a
week; whats_on_my_plate surfaced it as a TypeError on str concat.
Tail restored byte-identical from f0e5de3 (last complete blob) +
regression tests for the never-covered non-empty formatting path.

Hardening: _safe_plate_section wrapper - any plate section that raises
or returns None degrades to a stub line; the asana helper also coerces
formatter regressions (fail-soft now holds at both layers).

Calendar reads: probed the live DWD grant - events.list works ONLY
under calendar.events (granted); calendar.freebusy 403s it and
calendar.readonly is NOT in the grant. get_user_events now builds with
the events scope (freebusy path unchanged). Verified live: 7 events.
Also un-breaks the standalone calendar_get_my_events tool.

Model router: plate queries force Sonnet (multi-source composite;
Haiku narrated a degraded tool result as 'no open tasks'). The phrase
had literally been listed as a Haiku hint.

Ship PS1 restart kill filter corrected: live service command lines
contain \Scripts\cora.exe, NOT cora.main - the old filter matched
nothing and stacked instances. Both ship scripts now kill on either
pattern and verify exactly one instance after start (doctrine #5
rewritten in CLAUDE.md)."
if ($LASTEXITCODE -ne 0) {
    $staged = git diff --cached --name-only
    if ($staged) { Write-Error "Commit FAILED with staged changes."; exit 1 }
    Write-Host "Nothing to commit (already shipped) - continuing."
}

Write-Host "=== Step 4: verify HEAD advanced, then push ==="
git log --oneline -1
git push origin main
if ($LASTEXITCODE -ne 0) { Write-Error "Push FAILED - commit is local only."; exit 1 }

if ($Restart) {
    Write-Host "=== Step 5: restart Cora (CORRECTED kill filter - cora.exe pattern) ==="
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "Restart requires elevated PS (service runs -RunLevel Highest; D-036). Re-run with -Restart from an elevated prompt."
        exit 1
    }
    Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
    # Live service command lines contain \Scripts\cora.exe (console-script
    # wrapper), NOT cora.main. Kill BOTH patterns + the cora.exe launcher so
    # stacked instances from earlier bad restarts collapse too.
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe'" |
        Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" -or $_.CommandLine -like "*cora.main*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep 3
    $leftover = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe'" |
        Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" -or $_.CommandLine -like "*cora.main*" })
    if ($leftover.Count -gt 0) {
        Write-Warning ("Still " + $leftover.Count + " bot process(es) alive after kill - investigate before starting:")
        $leftover | ForEach-Object { Write-Warning ("  PID " + $_.ProcessId + " " + $_.Name) }
        exit 1
    }
    Start-ScheduledTask -TaskName "cowork-cora-service"
    Start-Sleep 90
    Write-Host "Heartbeat:"
    Get-Content "data\health\heartbeat.txt"
    Write-Host "Verify the timestamp above is fresh (within ~60s)."
    $instances = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" })
    Write-Host ("Bot python instances running: " + $instances.Count + " (must be exactly 1)")
    if ($instances.Count -ne 1) { Write-Warning "INSTANCE COUNT WRONG - investigate before walking away." }
} else {
    Write-Host "=== Step 5 SKIPPED: no -Restart switch ==="
    Write-Host "Fixes stay dormant until restart. From ELEVATED PS:"
    Write-Host "  .\deployment\ship-plate-fixes-2026-06-11.ps1 -Restart"
}

Write-Host "=== Done ==="
Write-Host "After restart: re-run the deliverable-1 exit gate smoke (Harrison via"
Write-Host "Cowork re-fire, Matt in #osn-leadership, Tommy in #f3e-sales, LEX user"
Write-Host "in #llc). Also sanity-check 'show me my tasks' - asana_get_my_tasks had"
Write-Host "been silently broken since 6/03."
