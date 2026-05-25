# cascade-commit-2026-05-24.ps1
# Commits all pending uncommitted Cora work in 3 logical groups.
# Run from any directory -- script cd's to the repo root first.
# No em-dashes anywhere in this file (cp1252 safe).

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = 'C:\Users\Harri\code\cora'
Set-Location $repoRoot

# Pre-flight -- remove stale index.lock if present
$lockFile = Join-Path $repoRoot '.git\index.lock'
if (Test-Path $lockFile) {
    Write-Host '[PRE-FLIGHT] Removing stale .git/index.lock ...'
    Remove-Item $lockFile -Force
}

# Helper: write a commit message to a temp file and commit
function Invoke-CommitWithMessage {
    param([string]$Message)
    $msgFile = Join-Path $env:TEMP 'cora_commit_msg.txt'
    Set-Content -Path $msgFile -Value $Message -Encoding UTF8
    git commit -F $msgFile
}

Write-Host ''
Write-Host '======================================================'
Write-Host ' GROUP A -- Lex sub-entity siloing + routing + KB'
Write-Host '======================================================'

$groupAFiles = @(
    'design/system-prompts/llc.md',
    'design/system-prompts/lbhs.md',
    'design/system-prompts/lla.md',
    'design/system-prompts/lts.md',
    'design/system-prompts/bdm.md',
    'design/channel-routing.yaml',
    'src/cora/context_loader.py',
    'src/cora/knowledge_base/store.py'
)

foreach ($f in $groupAFiles) {
    if (Test-Path (Join-Path $repoRoot $f)) {
        Write-Host "  git add $f"
        git add $f
    } else {
        Write-Host "  [SKIP] not found: $f"
    }
}

Invoke-CommitWithMessage '[LEX] Sub-entity siloing -- system prompts (llc/lbhs/lla/lts), BDM prompt rework, channel routing, context_loader, KB store'

Write-Host ''
Write-Host '======================================================'
Write-Host ' GROUP B -- New F3E/OSN tools'
Write-Host '======================================================'

$groupBRequired = @(
    'src/cora/tools/inventory_client.py',
    'src/cora/tools/hubspot_client.py',
    'src/cora/app.py',
    'src/cora/tools/tool_dispatch.py',
    'tests/test_f3e_inventory_pulse.py',
    'tests/test_f3e_hubspot_pipeline_summary.py'
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
    'src/cora/connectors/clover_client.py',
    'tests/test_osn_clover_tools.py'
)

foreach ($f in $groupBOptional) {
    if (Test-Path (Join-Path $repoRoot $f)) {
        Write-Host "  git add $f  [optional - found]"
        git add $f
    } else {
        Write-Host "  [OPTIONAL SKIP] not found: $f"
    }
}

Invoke-CommitWithMessage '[F3E/OSN] New tools -- F3E inventory pulse (Cotton 3PL), F3E HubSpot pipeline summary, OSN Clover connector; tests for all three'

Write-Host ''
Write-Host '======================================================'
Write-Host ' GROUP C -- Infrastructure, config, restore mis-tracked tests'
Write-Host '======================================================'

# Step C-1: patch .gitignore
$gitignorePath = Join-Path $repoRoot '.gitignore'
$newPatterns = @(
    'data/*.db',
    'data/cache/*.json',
    'data/*_watermarks.json'
)

if (-not (Test-Path $gitignorePath)) {
    Write-Host '  [.gitignore] Creating new .gitignore ...'
    New-Item $gitignorePath -ItemType File -Force | Out-Null
}

$existingContent = Get-Content $gitignorePath -Raw -ErrorAction SilentlyContinue
if (-not $existingContent) { $existingContent = '' }

foreach ($pattern in $newPatterns) {
    $lines = $existingContent -split "`n" | ForEach-Object { $_.TrimEnd() }
    if ($lines -notcontains $pattern) {
        Add-Content -Path $gitignorePath -Value $pattern
        Write-Host "  [.gitignore] Added: $pattern"
    } else {
        Write-Host "  [.gitignore] Already present: $pattern"
    }
}

git add .gitignore

# Step C-2: restore any deleted+untracked test files
Write-Host ''
Write-Host '  [TEST RESTORE] Scanning for deleted+untracked test files ...'

$statusLines = git status --porcelain 2>$null
$allTestsToRestore = @()

foreach ($line in $statusLines) {
    if ($line.Length -lt 3) { continue }
    $xy   = $line.Substring(0, 2)
    $path = $line.Substring(3).Trim()
    if ($path -match ' -> ') { $path = $path -split ' -> ' | Select-Object -Last 1 }
    if (($xy -match 'D' -or $xy -match '\?') -and $path -like 'tests/test_*.py') {
        $allTestsToRestore += $path
    }
}

$allTestsToRestore = $allTestsToRestore | Sort-Object -Unique
foreach ($t in $allTestsToRestore) {
    if (Test-Path (Join-Path $repoRoot $t)) {
        Write-Host "  git add $t  [restoring]"
        git add $t
    } else {
        Write-Host "  [SKIP] missing on disk: $t"
    }
}

# Step C-3: named Group C files
$groupCNamed = @(
    'pyproject.toml',
    'uv.lock',
    'data/maps/slack-to-asana.yaml',
    'tests/test_rate_limiter.py',
    'tests/test_slack_update_throttle.py',
    'tests/test_supervisor_authorization.py'
)

foreach ($f in $groupCNamed) {
    if (Test-Path (Join-Path $repoRoot $f)) {
        Write-Host "  git add $f"
        git add $f
    } else {
        Write-Host "  [SKIP] not found: $f"
    }
}

Invoke-CommitWithMessage '[INFRA] pyproject/uv.lock, .gitignore (exclude runtime DBs/caches), Tessa removal from slack-to-asana, restore mis-tracked test files'

Write-Host ''
Write-Host '======================================================'
Write-Host ' PUSH -- git push origin main'
Write-Host '======================================================'
git push origin main

Write-Host ''
Write-Host '======================================================'
Write-Host ' SUMMARY -- recent commits'
Write-Host '======================================================'
git log --oneline -5
