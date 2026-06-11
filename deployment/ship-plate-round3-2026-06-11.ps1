# ship-plate-round3-2026-06-11.ps1
# Round-3 plate fixes from the exit-gate smoke (Cowork findings):
#   1. Role header determinism: labeled YOUR ROLE section + role-first REPLY
#      FORMAT directive in tool output and all 17 entity prompts (models were
#      dropping the role line for non-Harrison askers)
#   2. Plate sections cap at 10 items + "first 10 of N" note (long plates hit
#      max-token truncation -> malformed trailing link)
#   3. reply_formatter: clean "( )" / "[label]()" redaction shells
#
# Run from PowerShell in C:\Users\Harri\code\cora.
# Pass -Restart from ELEVATED PS (required: tool_dispatch + prompts +
# reply_formatter are bot-loaded).

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
git add src/cora/tools/tool_dispatch.py src/cora/reply_formatter.py
git add tests/test_whats_on_my_plate.py tests/test_reply_formatter.py
git add deployment/ship-plate-round3-2026-06-11.ps1 CLAUDE.md
git add design/system-prompts/bdm.md design/system-prompts/f3c.md design/system-prompts/f3e.md design/system-prompts/fndr.md design/system-prompts/hjrp.md design/system-prompts/hjrprod.md design/system-prompts/lbhs.md design/system-prompts/lex.md design/system-prompts/lla.md design/system-prompts/llc.md design/system-prompts/lts.md design/system-prompts/osn.md design/system-prompts/osngf.md design/system-prompts/osngm.md design/system-prompts/osngw.md design/system-prompts/osnvv.md design/system-prompts/ufl.md
git commit -m "fix(plate): role-header determinism + 10-item section caps + reply-formatter redaction shells

Exit-gate round-2 findings (live 2026-06-11): (1) models dropped the
role/lanes line for 2/2 non-Harrison askers - the tool emitted it as an
unlabeled preamble and narration treated it as metadata. Now a labeled
YOUR ROLE section + explicit role-first REPLY FORMAT directive in the
tool output and all 17 entity prompts. (2) 25-task/23-deal plates hit
max-token truncation ending in a malformed half-link - sections now cap
at 10 items with a 'first 10 of N' note; standalone tools keep the full
view. (3) reply_formatter left '( )' / '[label]()' shells when redacting
bare doc URLs inside parens/markdown links - URL regex excludes ')' and
a new cleanup pass drops empty shells, keeping md-link labels.

+31 tests (role-first pinning, caps, prompt directive coverage,
redaction shells)."
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
    Write-Host "Fixes stay dormant until restart. From ELEVATED PS:"
    Write-Host "  .\deployment\ship-plate-round3-2026-06-11.ps1 -Restart"
}

Write-Host "=== Done ==="
Write-Host "After restart: finish the exit-gate smoke - re-check Tommy or Shaun's"
Write-Host "plate for the role line at the TOP of the reply, run the Matt leg, and"
Write-Host "confirm long plates end cleanly with a 'first 10 of N' note."
