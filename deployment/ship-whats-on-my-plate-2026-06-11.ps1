# ship-whats-on-my-plate-2026-06-11.ps1
# Host ship for Org Synthesis Phase 2, deliverable 1: whats_on_my_plate tool.
# Run from PowerShell in C:\Users\Harri\code\cora.
# Commit/push runs non-elevated; the restart step REQUIRES elevated PS
# (the service runs -RunLevel Highest; D-036: elevated procs are unkillable
# from a non-elevated session). Pass -Restart from elevated PS to restart.
#
# Ships ONLY this session's files:
#   src/cora/tools/tool_dispatch.py        (tool + wiring + timeout tier)
#   design/system-prompts/*.md             (17 prompts: mandatory tool-call section)
#   tests/test_whats_on_my_plate.py        (47 tests)
#   deployment/ship-whats-on-my-plate-2026-06-11.ps1
#   CLAUDE.md                              (TOM entry)
#
# Sequence: import smoke -> full pytest -> commit -> push -> restart (optional).
# Restart IS required for the tool to go live (tool_dispatch + prompts are
# loaded by the bot process).

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
git add src/cora/tools/tool_dispatch.py tests/test_whats_on_my_plate.py deployment/ship-whats-on-my-plate-2026-06-11.ps1 CLAUDE.md
git add design/system-prompts/bdm.md design/system-prompts/f3c.md design/system-prompts/f3e.md design/system-prompts/fndr.md design/system-prompts/hjrp.md design/system-prompts/hjrprod.md design/system-prompts/lbhs.md design/system-prompts/lex.md design/system-prompts/lla.md design/system-prompts/llc.md design/system-prompts/lts.md design/system-prompts/osn.md design/system-prompts/osngf.md design/system-prompts/osngm.md design/system-prompts/osngw.md design/system-prompts/osnvv.md design/system-prompts/ufl.md
git commit -m "feat(org): whats_on_my_plate tool - Org Synthesis Phase 2 deliverable 1

On-demand role-scoped composite plate view: role + lanes (org-roles
registry, D-044), open Asana tasks (channel entity-scoped), today +
tomorrow calendar, HubSpot deals for pipeline owners, and stalled
decisions for Harrison. Global core tool (every entity channel + DMs),
heavy 25s timeout tier.

Own-plate-only: the optional person parameter is Harrison-only; everyone
else is politely refused (asana_get_user_tasks stays the peer-visible
path). Unknown users (no org-roles entry) get a graceful fail-closed
no-data response. External consultants get role scope only - no internal
task/CRM/calendar pulls. LEX scope never gets a HubSpot section (Tier-1
doctrine). No financial figures are pulled (channel-tier guardrail
respected). ADVISORY data only - all deterministic guards run unchanged.

All 17 entity prompts gain a mandatory '## What's on my plate' tool-call
section; asana_get_my_tasks description re-pointed so the plate phrase
routes to the new tool.

47 tests (tests/test_whats_on_my_plate.py): wiring + exposure, registry
scoping, unknown-user refusal, Harrison override, external limits, LEX
HubSpot exclusion, fail-soft sections, prompt coverage."
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
    Write-Host "=== Step 5: restart Cora (kill filter matches cora.main ONLY) ==="
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "Restart requires elevated PS (service runs -RunLevel Highest; D-036). Re-run with -Restart from an elevated prompt."
        exit 1
    }
    # CORRECTED KILL FILTER (2026-06-11): the service launches via the console
    # script wrapper, so live command lines contain \Scripts\cora.exe -- NOT
    # cora.main. The old *cora.main* filter matched nothing and stacked instances.
    Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe'" |
        Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" -or $_.CommandLine -like "*cora.main*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep 3
    Start-ScheduledTask -TaskName "cowork-cora-service"
    Start-Sleep 90
    Write-Host "Heartbeat:"
    Get-Content "data\health\heartbeat.txt"
    Write-Host "Verify the timestamp above is fresh (within ~60s)."
    # Healthy single instance = cora.exe launcher -> venv python REDIRECTOR ->
    # base python = 1 cora.exe + 2 python.exe matches (verified 2026-06-11).
    $pys = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" })
    $launchers = @(Get-CimInstance Win32_Process -Filter "Name='cora.exe'")
    Write-Host ("Bot processes: " + $launchers.Count + " cora.exe launcher(s) + " + $pys.Count + " python (healthy single instance = 1 + 2)")
    if ($pys.Count -ne 2 -or $launchers.Count -ne 1) {
        Write-Warning "PROCESS SHAPE UNEXPECTED (stacked or partial instance?) - check the log for a single 'Cora starting up' + one monotonic heartbeat sequence."
    }
} else {
    Write-Host "=== Step 5 SKIPPED: no -Restart switch ==="
    Write-Host "The tool stays dormant until the next clean restart. From elevated PS:"
    Write-Host "  .\deployment\ship-whats-on-my-plate-2026-06-11.ps1 -Restart"
    Write-Host "(re-running is safe: pytest re-runs, commit no-ops if already shipped)"
}

Write-Host "=== Done ==="
Write-Host "Smoke test: '@Cora what's on my plate?' as Harrison (FNDR channel or DM),"
Write-Host "Matt in #osn-leadership, Tommy in #f3e-sales, and a LEX user in #llc or"
Write-Host "#lex-leadership (expect: tasks + calendar, NO deal pipeline section)."
