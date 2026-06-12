# ship-personal-notes-2026-06-11.ps1
# Org Synthesis Phase 5, deliverable 1 -- personal notes (write + read).
#   - store.py: source='user_note' EXCLUDED from general search at the SQL
#     layer (both vector paths); search_user_notes / list_user_notes /
#     delete_user_note (owner-filtered in SQL)
#   - user_notes.py: PHI save matrix, conflict check, labeled overlay
#   - context_loader/app.py: owner-notes overlay + cache-skip (D-043 reuse)
#   - tool_dispatch.py: cora_remember / cora_my_notes / cora_forget_note
#     (staged-write, global core)
#   - all 17 entity prompts: "## Personal notes" section
#
# Run from PowerShell in C:\Users\Harri\code\cora.
# Pass -Restart from ELEVATED PS (REQUIRED to activate: store, context_loader,
# app, tool_dispatch and prompts are all bot-process code).

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
git add src/cora/knowledge_base/store.py src/cora/user_notes.py src/cora/context_loader.py src/cora/app.py src/cora/tools/tool_dispatch.py
git add tests/test_user_notes.py
git add deployment/ship-personal-notes-2026-06-11.ps1 CLAUDE.md decisions.md
git add design/system-prompts/bdm.md design/system-prompts/f3c.md design/system-prompts/f3e.md design/system-prompts/fndr.md design/system-prompts/hjrp.md design/system-prompts/hjrprod.md design/system-prompts/lbhs.md design/system-prompts/lex.md design/system-prompts/lla.md design/system-prompts/llc.md design/system-prompts/lts.md design/system-prompts/osn.md design/system-prompts/osngf.md design/system-prompts/osngm.md design/system-prompts/osngw.md design/system-prompts/osnvv.md design/system-prompts/ufl.md
git commit -m "feat(notes): Org Synthesis Phase 5 d1 -- personal notes, owner-only at the SQL layer

Any teammate can teach Cora a personal note ('Cora, remember X') that is
retrievable BY THE OWNER ONLY. Blast-radius-1 enforced in code, never
prompts (D-034): general store.search excludes source='user_note' in both
vector paths, so every existing consumer (Q&A retrieval, sweeps, digests,
reconciliation, friction/strategy mining) excludes notes by construction;
the only retrieval path is the new search_user_notes, owner-filtered in
SQL (unrestricted = the D-043 allowlist, i.e. Harrison).

Write path: staged-write tools cora_remember / cora_my_notes /
cora_forget_note (global core, all entities + DMs). PHI save matrix:
PHI-flagged notes save only for a LEX custodian in LEX scope or DM
(forced into the LEX store); everyone else refused. Save-time conflict
check probes the canonical KB and appends a heads-up without blocking.
share_requested is captured as metadata for deliverable 2 (promotion is
Harrison-gated -- D-011 untouched).

Read path: context_loader co-retrieves the asker's own notes alongside
the entity + FNDR scan (channel asks see channel-scope notes only; a
LEX-scoped note never surfaces in a non-LEX channel; DMs see all owned
notes). Notes enter LLM context under an explicit PERSONAL NOTE label
('present as their own note, not org-canon') and set
kb_meta unstripped_personal so the response never enters the shared
semantic cache (D-043 invariant reused).

All 17 entity prompts gain '## Personal notes' (Cora now ACCEPTS
knowledge instead of refusing: save privately, org-wide sharing needs
Harrison's review).

+52 tests (adversarial owner-exclusion incl. identical-query and the
'Harrison approved my raise' pin, Harrison override, staged gates, PHI
matrix, LEX containment, cache-skip, list/delete owner-only, wiring)."
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
    Write-Host "Personal notes stay dormant until restart. From ELEVATED PS:"
    Write-Host "  .\deployment\ship-personal-notes-2026-06-11.ps1 -Restart"
}

Write-Host "=== Done ==="
Write-Host "Live smoke after restart:"
Write-Host "  1. Harrison DM: 'Cora, remember the Tucson stove vendor is Apex Appliance'"
Write-Host "     -> preview -> yes -> saved; then 'who is the Tucson stove vendor?' -> note,"
Write-Host "     labeled as your note. Have a teammate ask the same question -> NO note."
Write-Host "  2. One teammate save + retrieve in their entity channel."
Write-Host "  3. LEX channel, non-custodian: 'remember <client name>'s med schedule changed'"
Write-Host "     -> PHI refusal."
