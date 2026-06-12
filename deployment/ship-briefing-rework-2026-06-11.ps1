# ship-briefing-rework-2026-06-11.ps1
# Org Synthesis Phase 2, deliverable 2: briefing rework.
#   1. run_daily_briefing.py now reads data/maps/org-roles.yaml (org_roles.py);
#      role-briefing-config.yaml is RETIRED (D-044 item 5 consolidation point)
#   2. Briefing content mirrors the whats_on_my_plate composite and REUSES its
#      section builders from tool_dispatch (no forked logic)
#   3. Rollout doctrine: digest-to-Harrison-first -- script defaults to digest
#      mode (ONE review DM to Harrison); per-user delivery needs --send-users
#   4. Shared-builder fix: _plate_asana_section canonicalizes sub-entities to
#      their parent (LEX-LLC was falling through the task filter UNFILTERED)
#
# Run from PowerShell in C:\Users\Harri\code\cora.
# Pass -Restart from ELEVATED PS (optional for the briefing itself -- the
# scheduled task spawns a fresh process and picks up new code at its next
# fire -- but the tool_dispatch sub-entity fix is bot-loaded, and the next
# restart also activates the pending LLC posting targets + five-custodian
# prompt language from commits 558e768 + c7bce7a).

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
& .venv\Scripts\python.exe -c "from src.cora.app import app; import sys; sys.path.insert(0,'scripts'); import run_daily_briefing"
if ($LASTEXITCODE -ne 0) { Write-Error "Import smoke FAILED - aborting."; exit 1 }
Write-Host "Import smoke OK"

Write-Host "=== Step 2: full pytest suite ==="
& .venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Error "Test suite FAILED - aborting. Nothing committed."; exit 1 }
Write-Host "Full suite green"

Write-Host "=== Step 3: commit this session's files only (explicit paths, D-041) ==="
git add scripts/run_daily_briefing.py
git add tests/test_per_role_briefing.py
git add src/cora/tools/tool_dispatch.py
git add deployment/setup-daily-briefing-task.ps1
git add deployment/ship-briefing-rework-2026-06-11.ps1
git add data/maps/role-briefing-config.yaml
git add CLAUDE.md decisions.md
git commit -m "feat(briefing): org-roles-driven daily briefing -- role-briefing-config.yaml RETIRED (Org Synthesis Phase 2 d2)

run_daily_briefing.py now reads data/maps/org-roles.yaml via org_roles
(D-044 item 5 consolidation point -- the old per-user briefing config is
deleted). Briefing content mirrors the whats_on_my_plate composite and
REUSES its section builders from tool_dispatch: role + lanes, entity-
scoped tasks (capped 10), today/tomorrow calendar, pipeline for owners
(LEX never), stalled decisions Harrison-only. Externals (Jason) and
registry-only people (Tessa) are excluded from delivery; users absent
from the registry are skipped fail-closed by construction.

ROLLOUT DOCTRINE (locked): digest-to-Harrison-first. Default mode sends
Harrison ONE review DM with every user's would-be briefing; per-user
delivery requires the explicit --send-users flag (flip via
setup-daily-briefing-task.ps1 -SendUsers after Harrison's go).

Shared-builder fix: _plate_asana_section canonicalizes sub-entities to
their parent before the task filter -- LEX-LLC/OSNGF/... previously fell
through ENTITY_PROJECT_PREFIXES unfiltered (a sub-entity scope must
never be wider than its parent's). Applies to the plate tool too.

Tests rewritten registry-driven: roster parity, exclusions, digest/send
modes, retirement guard (source + repo file), chunking, sub-entity
scoping. Suite 3,875 passed / 41 skipped."
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
    Write-Host "=== Step 5: restart Cora (corrected kill filter, doctrine 5) ==="
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "Restart requires elevated PS (service runs -RunLevel Highest; D-036). Re-run with -Restart from an elevated prompt."
        exit 1
    }
    Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
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
    # Healthy single instance = cora.exe launcher -> venv python REDIRECTOR ->
    # base python = 1 cora.exe + 2 python.exe matches (doctrine 5).
    $pys = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" })
    $launchers = @(Get-CimInstance Win32_Process -Filter "Name='cora.exe'")
    Write-Host ("Bot processes: " + $launchers.Count + " cora.exe launcher(s) + " + $pys.Count + " python (healthy single instance = 1 + 2)")
    if ($pys.Count -ne 2 -or $launchers.Count -ne 1) {
        Write-Warning "PROCESS SHAPE UNEXPECTED (stacked or partial instance?) - check the log for a single 'Cora starting up' + one monotonic heartbeat sequence."
    }
} else {
    Write-Host "=== Step 5 SKIPPED: no -Restart switch ==="
    Write-Host "The briefing rework is live at the task's next 7:30am fire (fresh"
    Write-Host "process). The tool_dispatch sub-entity fix waits for the next bot"
    Write-Host "restart, which also activates the pending LLC routing items. From"
    Write-Host "ELEVATED PS:  .\deployment\ship-briefing-rework-2026-06-11.ps1 -Restart"
}

Write-Host "=== Done ==="
Write-Host "REVIEW STEP (review-driven enablement, ed6c212): tomorrow's 7:30am run"
Write-Host "(or Start-ScheduledTask -TaskName 'Cora - Daily Briefing' now) DMs"
Write-Host "Harrison ONE MESSAGE PER USER with that user's would-be briefing."
Write-Host "React :+1: on a user's message to enable their delivery (picked up"
Write-Host "automatically at the NEXT run); :-1: drops them from review. No"
Write-Host "re-registration needed; -SendUsers exists only as a force-all override."
