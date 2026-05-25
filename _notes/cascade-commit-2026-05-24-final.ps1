# cascade-commit-2026-05-24-final.ps1
# Final cascade for all uncommitted Cora work as of 2026-05-24 evening.
# Covers Groups A (LEX sub-entity siloing), B (F3E/OSN tools + app.py guard fix),
# C (infra, config, test restore, deployment scripts).
#
# Run from PowerShell in ANY directory -- script cd's to repo root.
# Execute: .\cascade-commit-2026-05-24-final.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = 'C:\Users\Harri\code\cora'
Set-Location $repoRoot

# -------------------------------------------------------------
# 0. PRE-FLIGHT
# -------------------------------------------------------------
$lockFile = Join-Path $repoRoot '.git\index.lock'
if (Test-Path $lockFile) {
    Write-Host '[PRE-FLIGHT] Removing stale .git/index.lock ...'
    Remove-Item $lockFile -Force
}

function Commit-WithMessage {
    param([string]$Message)
    $msgFile = Join-Path $env:TEMP 'cora_commit_msg.txt'
    Set-Content -Path $msgFile -Value $Message -Encoding UTF8
    git commit -F $msgFile
}

# -------------------------------------------------------------
# GROUP A -- LEX sub-entity siloing
# system prompts + channel routing + context_loader + KB store
# -------------------------------------------------------------
Write-Host ''
Write-Host '======================================================'
Write-Host ' GROUP A -- LEX sub-entity siloing + routing + KB'
Write-Host '======================================================'

$groupA = @(
    'design/system-prompts/llc.md',
    'design/system-prompts/lts.md',
    'design/system-prompts/lbhs.md',
    'design/system-prompts/lla.md',
    'design/channel-routing.yaml',
    'src/cora/context_loader.py',
    'src/cora/knowledge_base/store.py'
)

foreach ($f in $groupA) {
    if (Test-Path (Join-Path $repoRoot $f)) {
        Write-Host "  git add $f"
        git add $f
    } else {
        Write-Host "  [SKIP] not found: $f"
    }
}

# Check if sibling_guard.py is untracked (may have been in a prior commit)
$sgStatus = git status --porcelain -- 'src/cora/sibling_guard.py' 2>$null
if ($sgStatus -and $sgStatus.Substring(0,2).Trim() -in @('?', '??', 'A')) {
    Write-Host '  git add src/cora/sibling_guard.py  [new file]'
    git add 'src/cora/sibling_guard.py'
} elseif ($sgStatus -and $sgStatus.Substring(0,2).Trim() -eq 'M') {
    Write-Host '  git add src/cora/sibling_guard.py  [modified]'
    git add 'src/cora/sibling_guard.py'
} else {
    Write-Host '  [sibling_guard.py already committed or clean -- skipping]'
}

Commit-WithMessage '[LEX] Sub-entity siloing: system prompts (llc/lts/lbhs/lla), channel routing, context_loader firewall, KB strict sub_entity filter'

# -------------------------------------------------------------
# GROUP B -- F3E/OSN tools + app.py sibling guard wire-up
# -------------------------------------------------------------
Write-Host ''
Write-Host '======================================================'
Write-Host ' GROUP B -- F3E/OSN tools + app.py sibling guard'
Write-Host '======================================================'

$groupBRequired = @(
    'src/cora/app.py',
    'src/cora/tools/tool_dispatch.py',
    'src/cora/tools/hubspot_client.py'
)

foreach ($f in $groupBRequired) {
    if (Test-Path (Join-Path $repoRoot $f)) {
        Write-Host "  git add $f"
        git add $f
    } else {
        Write-Host "  [SKIP] not found: $f"
    }
}

$groupBOptional = @(
    'src/cora/tools/inventory_client.py',
    'src/cora/connectors/clover_client.py',
    'tests/test_f3e_inventory_pulse.py',
    'tests/test_f3e_hubspot_pipeline_summary.py',
    'tests/test_osn_clover_tools.py'
)

foreach ($f in $groupBOptional) {
    if (Test-Path (Join-Path $repoRoot $f)) {
        Write-Host "  git add $f  [optional -- found]"
        git add $f
    } else {
        Write-Host "  [OPTIONAL SKIP] not found: $f"
    }
}

Commit-WithMessage '[F3E/OSN/APP] app.py sibling guard wire-up; F3E inventory + HubSpot tools; OSN Clover connector; tool_dispatch updates'

# -------------------------------------------------------------
# GROUP C -- Infrastructure + system prompts (BDM/OSN/FNDR) +
#            deployment scripts + test restore + config
# -------------------------------------------------------------
Write-Host ''
Write-Host '======================================================'
Write-Host ' GROUP C -- Infra, BDM/OSN/FNDR prompts, test restore'
Write-Host '======================================================'

# C-1: System prompts not in Group A
$groupCPrompts = @(
    'design/system-prompts/bdm.md',
    'design/system-prompts/osn.md',
    'design/system-prompts/fndr.md'
)

foreach ($f in $groupCPrompts) {
    if (Test-Path (Join-Path $repoRoot $f)) {
        Write-Host "  git add $f"
        git add $f
    } else {
        Write-Host "  [SKIP] not found: $f"
    }
}

# C-2: Deployment scripts
$groupCDeploy = @(
    'deployment/setup-kb-sync-tasks.ps1',
    'deployment/remove-kb-sync-tasks.ps1'
)

foreach ($f in $groupCDeploy) {
    if (Test-Path (Join-Path $repoRoot $f)) {
        Write-Host "  git add $f"
        git add $f
    } else {
        Write-Host "  [SKIP] not found: $f"
    }
}

# C-3: Config + lock
$groupCConfig = @(
    'pyproject.toml',
    'uv.lock',
    '.gitignore',
    'data/maps/slack-to-asana.yaml'
)

foreach ($f in $groupCConfig) {
    if (Test-Path (Join-Path $repoRoot $f)) {
        Write-Host "  git add $f"
        git add $f
    } else {
        Write-Host "  [SKIP] not found: $f"
    }
}

# C-4: Restore deleted-then-recreated test files
Write-Host ''
Write-Host '  [TEST RESTORE] Scanning deleted+untracked test files ...'

$statusLines = git status --porcelain 2>$null
$testFiles = @{}

foreach ($line in $statusLines) {
    if ($line.Length -lt 3) { continue }
    $xy   = $line.Substring(0, 2)
    $path = $line.Substring(3).Trim()
    if ($path -match ' -> ') { $path = $path -split ' -> ' | Select-Object -Last 1 }
    if ($path -like 'tests/test_*.py') {
        $testFiles[$path] = $true
    }
}

foreach ($t in $testFiles.Keys) {
    if (Test-Path (Join-Path $repoRoot $t)) {
        Write-Host "  git add $t  [restoring]"
        git add $t
    }
}

# C-5: Specific named tests
$groupCTests = @(
    'tests/test_rate_limiter.py',
    'tests/test_slack_update_throttle.py',
    'tests/test_supervisor_authorization.py',
    'tests/test_parallel_tool_dispatch.py',
    'tests/test_prompt_loader.py',
    'tests/test_prompt_loader_voice.py',
    'tests/test_fndr_contracts_dashboard.py'
)

foreach ($f in $groupCTests) {
    if (Test-Path (Join-Path $repoRoot $f)) {
        Write-Host "  git add $f"
        git add $f
    }
}

Commit-WithMessage '[INFRA] BDM/OSN/FNDR prompts, deployment scripts, pyproject/uv.lock, .gitignore, slack-to-asana, restore test files'

# -------------------------------------------------------------
# PUSH
# -------------------------------------------------------------
Write-Host ''
Write-Host '======================================================'
Write-Host ' PUSH'
Write-Host '======================================================'
git push origin main

Write-Host ''
Write-Host '======================================================'
Write-Host ' DONE -- recent commits'
Write-Host '======================================================'
git log --oneline -6

Write-Host ''
Write-Host 'NEXT: restart Cora, then run Test 6 in #llc:'
Write-Host '  @Cora What is the LLA cap table and ownership structure?'
Write-Host 'Expected: 1-sentence redirect, no thinking bubble, no Claude API call'
