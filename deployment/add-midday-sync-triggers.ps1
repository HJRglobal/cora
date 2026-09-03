# add-midday-sync-triggers.ps1 (S4, claude-workspace mirror)
#
# Adds ONE midday daily trigger to two existing KB-freshness tasks so same-day
# knowledge is retrievable by early afternoon (not only next morning):
#
#   cowork-cora-kb-sync-static      + 12:30 AZ  (2nd static ingest of the day)
#   cowork-cora-session-capture     + 12:20 AZ  (2nd Cowork/Code capture; keeps --with-kb)
#
# WHY NOT re-run the original setup scripts: re-registration is the registry-drop
# class (TOM 1nnnn) -- it rewrites the whole task and can strip cadence/principal.
# This script MUTATES IN PLACE: it exports the current task XML to a dated backup
# FIRST (the rollback point), appends the trigger via Set-ScheduledTask -Trigger,
# and reads back the trigger count. It changes NO existing trigger time, principal,
# RunLevel, action, or the --with-kb argument.
#
# IDEMPOTENT: a task that already has the midday trigger is left untouched.
# DRY-RUN BY DEFAULT: pass -Apply to write. ASCII-only per D-016.
#
# Slots verified free 2026-09-03 against live Get-ScheduledTask: nothing Cora fires
# 12:xx, and both slots are OUTSIDE the 03:00-09:00 collision window the health
# check watches. The midday static run (~78s) finishes long before the 20:30 backup.
#
# Run from elevated PowerShell (Set-ScheduledTask on a Limited-runlevel task):
#     cd C:\Users\Harri\code\cora
#     .\deployment\add-midday-sync-triggers.ps1            # dry-run
#     .\deployment\add-midday-sync-triggers.ps1 -Apply

param([switch]$Apply)

$ErrorActionPreference = "Stop"

$RepoRoot   = "C:\Users\Harri\code\cora"
$BackupRoot = Join-Path $RepoRoot "deployment\task-backups"
$Stamp      = Get-Date -Format "yyyy-MM-dd"
$BackupDir  = Join-Path $BackupRoot $Stamp

$Changes = @(
    @{ Name = "cowork-cora-kb-sync-static";  Add = "12:30" }
    @{ Name = "cowork-cora-session-capture"; Add = "12:20" }
)

if ($Apply) {
    Write-Host "APPLY MODE - tasks will be modified." -ForegroundColor Yellow
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
} else {
    Write-Host "DRY RUN - nothing will change. Re-run with -Apply." -ForegroundColor Cyan
}
Write-Host ""

foreach ($c in $Changes) {
    $name = $c.Name
    $add  = $c.Add
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Host ("SKIP not-found: " + $name) -ForegroundColor Yellow
        continue
    }

    # Existing trigger clock times (HH:mm) for idempotency + reporting.
    $times = @()
    foreach ($trg in $task.Triggers) {
        if ($trg.StartBoundary -and $trg.StartBoundary.Contains('T')) {
            $times += (($trg.StartBoundary -split 'T', 2)[1]).Substring(0, 5)
        }
    }

    if ($times -contains $add) {
        Write-Host ("OK already has $add : " + $name + "  (triggers: " + ($times -join ',') + ")") -ForegroundColor Green
        continue
    }

    Write-Host ("WILL ADD $add to " + $name + "  (current: " + ($times -join ',') + ")") -ForegroundColor Cyan
    if (-not $Apply) { continue }

    # Rollback point FIRST -- Export as UTF-16 (Export-ScheduledTask emits a
    # UTF-16 prolog; UTF-8+BOM makes the bytes disagree with the declaration and
    # schtasks /Create /XML refuses it -- measured, see rewrap-tasks-hidden.ps1).
    $safe = [regex]::Replace($name, '[\\/:*?"<>|]', '_')
    $backupPath = Join-Path $BackupDir ($safe + ".xml")
    Export-ScheduledTask -TaskName $name | Set-Content -Path $backupPath -Encoding Unicode
    $probe = New-Object System.Xml.XmlDocument
    $probe.Load($backupPath)
    if ($null -eq $probe.DocumentElement) {
        Write-Host ("  ERROR: XML backup at " + $backupPath + " not loadable -- NOT modifying " + $name) -ForegroundColor Red
        continue
    }

    # Append the trigger. Set-ScheduledTask -Trigger REPLACES the trigger set, so
    # we pass ALL existing triggers plus the new one (preserving each StartBoundary).
    $newTrigger = New-ScheduledTaskTrigger -Daily -At $add
    $allTriggers = @($task.Triggers) + $newTrigger
    Set-ScheduledTask -TaskName $name -Trigger $allTriggers | Out-Null

    $post = Get-ScheduledTask -TaskName $name
    $postTimes = @()
    foreach ($trg in $post.Triggers) {
        if ($trg.StartBoundary -and $trg.StartBoundary.Contains('T')) {
            $postTimes += (($trg.StartBoundary -split 'T', 2)[1]).Substring(0, 5)
        }
    }
    if ($post.Triggers.Count -eq ($task.Triggers.Count + 1) -and ($postTimes -contains $add)) {
        Write-Host ("  ADDED. " + $name + " now fires: " + ($postTimes -join ',') + "  (backup: " + $backupPath + ")") -ForegroundColor Green
    } else {
        Write-Host ("  WARNING: read-back mismatch on " + $name + " (triggers now " + $post.Triggers.Count + "). Restore from " + $backupPath + " if needed.") -ForegroundColor Red
    }
}

Write-Host ""
if ($Apply) {
    Write-Host "Done. Rollback: schtasks /Create /XML <backup> /TN <name> /F from $BackupDir" -ForegroundColor DarkGray
} else {
    Write-Host "Re-run with -Apply to write the trigger additions." -ForegroundColor Cyan
}
