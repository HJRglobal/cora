# ship-org-roles-2026-06-10.ps1
# Host ship for Org Synthesis Phase 1: org role registry + role-aware context.
# Run from elevated PowerShell in C:\Users\Harri\code\cora
#
# Ships ONLY this session's files:
#   data/maps/org-roles.yaml
#   src/cora/org_roles.py
#   src/cora/app.py            (role-block injection in _dispatch_qa)
#   tests/test_org_roles.py
#   deployment/ship-org-roles-2026-06-10.ps1
#   CLAUDE.md                  (TOM entry)
#
# Sequence: import smoke -> full pytest -> commit -> push -> restart Cora.
# Restart IS required (app.py changed in the live bot process).

$ErrorActionPreference = "Stop"
Set-Location "C:\Users\Harri\code\cora"

Write-Host "=== Step 0: clear stale git index.lock (left by the Cowork sandbox) ==="
# The 2026-06-10 Cowork session's read-only git status created a zero-byte
# .git\index.lock over virtiofs and could not unlink it (D-041 artifact).
# Safe to remove ONLY if no git process is running and the file is 0 bytes.
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

Write-Host "=== Step 3: commit this session's files only ==="
git add data/maps/org-roles.yaml src/cora/org_roles.py src/cora/app.py tests/test_org_roles.py deployment/ship-org-roles-2026-06-10.ps1 CLAUDE.md
git commit -m "feat(org): Org Synthesis Phase 1 - role registry + role-aware context

Canonical org role registry (data/maps/org-roles.yaml, 19 people: role,
entity, lanes, manager, routing notes) + org_roles.py loader (60s TTL
live-reload, fail-closed for unknown users, parse errors keep last good
registry). app._dispatch_qa injects a terse role block into the runtime
context for every known asker (mentions, thread follow-ups, /cora-ask,
DMs) so replies are tailored to the asker's position, entity, and role.

ADVISORY ONLY: the registry never grants access. Every injected block
carries an explicit no-expansion rule; user_access / sibling_guard /
cross_entity_guard / phi_guard / historical_access (D-043) all run
unchanged. Unknown askers get exactly the prior behavior.

Spec of record: _shared/projects/cora/design/2026-06-10_fndr_org-synthesis-spec.md
Phase 2 (per-user proactive assistant), Phase 3 (efficiency mining pass),
Phase 4 (founder strategy layer) are specced, not built.

28 tests (tests/test_org_roles.py) incl. roster-drift guards covering
slack-to-asana, PHI custodians, and the finance allowlist."
if ($LASTEXITCODE -ne 0) { Write-Error "Commit FAILED."; exit 1 }

Write-Host "=== Step 4: push ==="
git push origin main
if ($LASTEXITCODE -ne 0) { Write-Error "Push FAILED - commit is local only."; exit 1 }

Write-Host "=== Step 5: restart Cora (doctrine 5 sequence, CIM kill) ==="
$confirm = Read-Host "Restart Cora now? The 18mo gmail backfill may still be running - it is a SEPARATE process and is NOT affected by restarting the bot. (y/n)"
if ($confirm -eq "y") {
    Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*cora.main*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep 3
    Start-ScheduledTask -TaskName "cowork-cora-service"
    Start-Sleep 90
    Write-Host "Heartbeat:"
    Get-Content "data\health\heartbeat.txt"
    Write-Host "Verify the timestamp above is fresh (within ~60s)."
} else {
    Write-Host "Restart SKIPPED - role injection stays dormant until the next clean restart."
}

Write-Host "=== Done ==="
Write-Host "Smoke test: have a non-Harrison teammate (e.g. Matt in #osn-leadership)"
Write-Host "ask @Cora a question - the reply should reflect their role/lane without"
Write-Host "any change to entity scoping. Unknown/guest users behave exactly as before."
