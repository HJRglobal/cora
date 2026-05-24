# cascade-commit-2026-05-24.ps1
# Commits all pending uncommitted Cora work in 3 logical groups.
# Run from any directory — script cd's to the repo root first.
# DO NOT run with -WhatIf; just execute: .\cascade-commit-2026-05-24.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = 'C:\Users\Harri\code\cora'
Set-Location $repoRoot

# ─────────────────────────────────────────────────────────────
# 0. PRE-FLIGHT — remove stale index.lock if present
# ─────────────────────────────────────────────────────────────
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

# Helper: show current git status for a file (returns '' if clean/untracked unknown)
function Get-FileStatus {
    param([string]$RelPath)
    $line = git status --porcelain -- $RelPath 2>$null | Select-Object -First 1
    if ($line) { return $line.Substring(0,2).Trim() }
    return ''
}

Write-Host ''
Write-Host '======================================================'
Write-Host ' GROUP A — Lex sub-entity siloing + routing + KB'
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

Invoke-CommitWithMessage '[LEX] Sub-entity siloing — system prompts (llc/lbhs/lla/lts), BDM prompt rework, channel routing, context_loader, KB store'

Write-Host ''
Write-Host '======================================================'
Write-Host ' GROUP B — New F3E/OSN tools'
Write-Host '======================================================'

# Core files (required)
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

# Optional files — add only if they exist
$groupBOptional = @(
    'src/cora/connectors/clover_client.py',
    'tests/test_osn_clover_tools.py'
)

foreach ($f in $groupBOptional) {
    if (Test-Path (Join-Path $repoRoot $f)) {
        Write-Host "  git add $f  [optional — found]"
        git add $f
    } else {
        Write-Host "  [OPTIONAL SKIP] not found: $f"
    }
}

Invoke-CommitWithMessage '[F3E/OSN] New tools — F3E inventory pulse (Cotton 3PL), F3E HubSpot pipeline summary, OSN Clover connector; tests for all three'

Write-Host ''
Write-Host '======================================================'
Write-Host ' GROUP C — Infrastructure, config, restore mis-tracked tests'
Write-Host '======================================================'

# ── Step C-1: patch .gitignore ────────────────────────────────
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

$patternsAdded = @()
foreach ($pattern in $newPatterns) {
    # Check line-by-line so partial substring matches don't fool us
    $lines = $existingContent -split "`n" | ForEach-Object { $_.TrimEnd() }
    if ($lines -notcontains $pattern) {
        Add-Content -Path $gitignorePath -Value $pattern
        $patternsAdded += $pattern
        Write-Host "  [.gitignore] Added: $pattern"
    } else {
        Write-Host "  [.gitignore] Already present: $pattern"
    }
}

git add .gitignore

# ── Step C-2: handle deleted-then-untracked test files ────────
# Files that appear as 'D' (staged deletion) AND also show as untracked '?'
# in the same git status output just need a plain `git add` to re-track them.
Write-Host ''
Write-Host '  [TEST RESTORE] Scanning for deleted+untracked test files ...'

$statusLines = git status --porcelain 2>$null
$deletedTests  = @()
$untrackedTests = @()

foreach ($line in $statusLines) {
    if ($line.Length -lt 3) { continue }
    $xy   = $line.Substring(0, 2)
    $path = $line.Substring(3).Trim()
    # Strip rename arrow if present (e.g. "old -> new")
    if ($path -match ' -> ') { $path = $path -split ' -> ' | Select-Object -Last 1 }

    if ($xy -match 'D' -and $path -like 'tests/test_*.py') {
        $deletedTests += $path
    }
    if ($xy -match '\?' -and $path -like 'tests/test_*.py') {
        $untrackedTests += $path
    }
}

# Restore any test file that is EITHER deleted in index OR untracked
$allTestsToRestore = ($deletedTests + $untrackedTests) | Sort-Object -Unique
foreach ($t in $allTestsToRestore) {
    if (Test-Path (Join-Path $repoRoot $t)) {
        Write-Host "  git add $t  [restoring]"
        git add $t
    } else {
        Write-Host "  [SKIP] missing on disk, cannot restore: $t"
    }
}

# ── Step C-3: explicitly add the named Group C files ──────────
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

# ─────────────────────────────────────────────────────────────
# PUSH
# ─────────────────────────────────────────────────────────────
Write-Host ''
Write-Host '======================================================'
Write-Host ' PUSH — git push origin main'
Write-Host '======================================================'
git push origin main

# ─────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────
Write-Host ''
Write-Host '======================================================'
Write-Host ' SUMMARY — recent commits'
Write-Host '======================================================'
git log --oneline -5
