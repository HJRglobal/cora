# restagger-morning-tasks-2026-06-13.ps1
# -----------------------------------------------------------------------------
# Purpose: de-collide Cora scheduled tasks that share a clock time in the
# 03:00-09:00 AZ window (the weekly-health "stagger them" alarm). Each task
# below is nudged a few minutes so no two enabled Cora tasks fire on the same
# minute, while preserving every documented anchor + dependency order
# (KB syncs -> session-capture/digest -> reconciliation 05:30 -> gap-autofill
# -> knowledge-review 07:00 -> briefing 07:30).
#
# SAFETY:
#   - Only the trigger START TIME is changed; recurrence type (daily/weekly),
#     Settings (ExecutionTimeLimit), Principal (run level), and Actions are all
#     preserved -- we mutate StartBoundary in place and Set only -Trigger.
#   - Idempotent: re-running matches each task's OLD time before moving, so a
#     second run is a no-op (prints "no matching <old> trigger").
#   - Run from an ELEVATED PowerShell (tasks were registered elevated).
#
# ASCII-only per repo doctrine (no em-dashes / smart quotes).
# Verify-only run: pass -WhatIfList to print the plan without changing anything.
# -----------------------------------------------------------------------------
param([switch]$WhatIfList)

$moves = @(
  @{ Name = 'Cora - Channel Health Monitor';  Old = '04:00'; New = '04:15' },
  @{ Name = 'cowork-cora-session-capture';    Old = '05:00'; New = '05:15' },
  @{ Name = 'cowork-cora-digest';             Old = '05:00'; New = '05:20' },
  @{ Name = 'cowork-cora-gap-autofill';       Old = '06:00'; New = '06:10' },
  @{ Name = 'Cora - Asana Hygiene Nudges';    Old = '06:30'; New = '06:40' },
  @{ Name = 'cowork-cora-decision-capture';   Old = '07:00'; New = '07:15' },
  @{ Name = 'cowork-cora-fireflies-coverage'; Old = '08:00'; New = '08:10' },
  @{ Name = 'cowork-cora-influencer-digest';  Old = '08:00'; New = '08:20' },
  @{ Name = 'cowork-cora-channel-sweep';      Old = '08:30'; New = '08:40' },
  # Added 2026-06-13 (second pass): the first run cleared 7 of 9 but left two.
  # drive-extractor was not in the initial enumeration (surfaced once Channel
  # Health Monitor moved off 04:00); influencer-overdue was omitted from the
  # first move list. Re-running is safe -- already-moved tasks no-op (WARN).
  @{ Name = 'cowork-cora-drive-extractor';            Old = '04:00'; New = '04:05' },
  @{ Name = 'cowork-cora-influencer-overdue-alerts';  Old = '09:00'; New = '09:10' }
)

if ($WhatIfList) {
  Write-Host "PLAN (no changes will be made):"
  foreach ($m in $moves) { Write-Host ("  {0,-34} {1} -> {2}" -f $m.Name, $m.Old, $m.New) }
  Write-Host "Re-run without -WhatIfList from an elevated PowerShell to apply."
  return
}

foreach ($m in $moves) {
  $t = Get-ScheduledTask -TaskName $m.Name -ErrorAction SilentlyContinue
  if (-not $t) { Write-Host ("SKIP not-found: " + $m.Name); continue }
  $changed = $false
  foreach ($trg in $t.Triggers) {
    $sb = $trg.StartBoundary
    if ($sb -and $sb.Contains('T')) {
      $parts  = $sb -split 'T', 2
      $date   = $parts[0]
      $rest   = $parts[1]
      $offset = ''
      if ($rest -match '([+-]\d{2}:\d{2})$') { $offset = $matches[1] }
      $oldhhmm = $rest.Substring(0, 5)
      if ($oldhhmm -eq $m.Old) {
        $trg.StartBoundary = $date + 'T' + $m.New + ':00' + $offset
        $changed = $true
      }
    }
  }
  if (-not $changed) {
    Write-Host ("WARN no matching " + $m.Old + " trigger on " + $m.Name + " -- left unchanged")
    continue
  }
  Set-ScheduledTask -TaskName $m.Name -Trigger $t.Triggers | Out-Null
  $after = (Get-ScheduledTask -TaskName $m.Name).Triggers | ForEach-Object {
    if ($_.StartBoundary -and $_.StartBoundary.Contains('T')) { (($_.StartBoundary -split 'T', 2)[1]).Substring(0, 5) }
  }
  Write-Host ("MOVED " + $m.Name + " : " + $m.Old + " -> " + $m.New + "  (now " + ($after -join ',') + ")")
}

Write-Host ""
Write-Host "=== enabled Cora tasks firing 03:00-09:00 after re-stagger (sorted) ==="
$rows = @()
Get-ScheduledTask | Where-Object { $_.TaskName -match 'cora' -and $_.State -ne 'Disabled' } | ForEach-Object {
  $n = $_.TaskName
  foreach ($trg in $_.Triggers) {
    $sb = $trg.StartBoundary
    if ($sb -and $sb.Contains('T')) {
      $hhmm = (($sb -split 'T', 2)[1]).Substring(0, 5)
      if ($hhmm -ge '03:00' -and $hhmm -le '09:00') { $rows += ("{0}  {1}" -f $hhmm, $n) }
    }
  }
}
$rows | Sort-Object | ForEach-Object { Write-Host $_ }
$dupes = ($rows | ForEach-Object { ($_ -split '  ')[0] } | Group-Object | Where-Object { $_.Count -gt 1 })
if ($dupes) { Write-Host ("COLLISIONS REMAIN: " + (($dupes | ForEach-Object { $_.Name }) -join ', ')) }
else { Write-Host "OK: no two enabled Cora tasks share a clock time in 03:00-09:00." }
