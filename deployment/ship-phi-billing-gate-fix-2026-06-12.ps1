# ship-phi-billing-gate-fix-2026-06-12.ps1
# Org Synthesis Phase 5 d1 -- PHI save-gate fix.
#
# 2026-06-12 live miss (#llc-finance): Justin (NOT a PHI custodian) said
# "Cora, remember Bob Smith's billing authorization is pending" and
# cora_remember STAGED the save instead of refusing. Root cause: the base
# clinical/identifier PHI patterns carry no billing/authorization keyword, so a
# named individual's administrative status slipped through the save classifier.
#
# Fix (all bot-process code -> restart REQUIRED to activate):
#   - phi_guard.py: new is_lex_billing_status_phi() (opt-in; does NOT change
#     global is_phi_risk, so session_capture/reconciliation are unchanged)
#   - user_notes.py: resolve_save_scope applies the augmentation in LEX scope
#     or a DM
#   - tool_dispatch.py: PHI gate runs BEFORE the confirm gate (refusal fires at
#     the preview stage, never staged) + PHI nudge in the cora_remember desc
#   - tests: +19 (bug-string regression, preview-stage refusal, true-pos/neg,
#     custodian-allowed, outside-LEX-not-flagged, finance channel-scope pin,
#     owner-exclusion adversarial pin)
#
# Run from PowerShell in C:\Users\Harri\code\cora.
# Pass -Restart from ELEVATED PS (REQUIRED to activate -- D-036).

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
        Write-Error "index.lock present and either non-empty ($lockSize bytes) or a git process is running - investigate."
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
git add src/cora/phi_guard.py src/cora/user_notes.py src/cora/tools/tool_dispatch.py
git add tests/test_user_notes.py tests/test_finance_receipts.py
git add deployment/ship-phi-billing-gate-fix-2026-06-12.ps1 CLAUDE.md decisions.md
git commit -m "fix(notes): PHI save gate catches billing/authorization tied to a named individual in LEX scope

2026-06-12 live miss (#llc-finance): Justin (NOT a PHI custodian) said
'Cora, remember Bob Smith's billing authorization is pending' and
cora_remember STAGED the save instead of refusing. Root cause: the base
clinical/identifier PHI patterns carry no billing/authorization keyword,
so a named individual's administrative status slipped through the save
classifier; resolve_save_scope took the non-PHI branch and never
consulted the custodian gate.

phi_guard.is_lex_billing_status_phi(): an administrative term (billing /
authorization / eligibility / coverage / claims / units / placement) tied
to a specific person (possessive proper name OR care-recipient noun), or
explicit client-status phrasing. Opt-in -- NOT folded into is_phi_risk, so
session_capture / reconciliation PHI routing is unchanged. Applied by
user_notes.resolve_save_scope only in LEX scope or a DM (outside LEX a
named buyer's authorization is ordinary business, not PHI).

tool_dispatch._tool_cora_remember now runs the PHI/scope gate BEFORE the
staged-write confirm gate, so a refused save is rejected on the first tool
call -- never staged as a 'Saving to YOUR notes...' preview. cora_remember
description gains a PHI nudge so the model doesn't self-preview a
PHI-shaped LEX note. Blast-radius-1 held throughout (nothing persisted on
the live miss; owner-only retrieval unchanged). D-011/D-044 untouched.

+19 tests (bug-string regression, preview-stage refusal, true-pos/neg,
custodian-allowed, outside-LEX-not-flagged, finance channel-scope pin,
owner-exclusion adversarial identical-query pin)."
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
    Write-Host "The PHI gate fix stays dormant until restart. From ELEVATED PS:"
    Write-Host "  .\deployment\ship-phi-billing-gate-fix-2026-06-12.ps1 -Restart"
}

Write-Host "=== Done. Live re-test plan after restart ==="
Write-Host "1. #llc-finance, Justin (non-custodian): 'Cora, remember Bob Smith's billing"
Write-Host "   authorization is pending' -> clean PHI refusal, NO staged preview, nothing saved."
Write-Host "2. #some-non-finance channel, Justin: 'pull the invoices from Wildpack' -> no finance"
Write-Host "   retrieval (normal Tier-2 applies); inside #hjr-finance a non-allowlisted user is refused."
Write-Host "3. A teammate (e.g. Hannah) DM: save a personal note + retrieve it -> works, owner-only."
Write-Host "4. Tommy: ask 'who is the Tucson stove vendor?' after Harrison saved it -> NO note (cross-user negative)."
