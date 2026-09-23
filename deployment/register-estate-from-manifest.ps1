# register-estate-from-manifest.ps1 -- register / update the WHOLE Cora scheduled estate
# from the committed manifest (DR/VM step 1, M1 + D-051 remediation).
#
# WHY THIS EXISTS (2026-09-23 review, HIGH x2): the old bootstrap said "run every
# setup-*.ps1 and expect zero drift". It cannot: the setup scripts carry drifted
# triggers (9 stale clocks), re-register the 18 intent-disabled tasks ENABLED, six of
# them register tasks that are NOT in the estate, setup-windows-task.ps1 still pointed
# at the WDAC-blocked cora.exe, and 9 tasks have no setup script at all. The manifest
# is the source of truth, so the manifest is the registration source:
#
#   deployment\manifest\tasks\<slug>.xml   one full-fidelity Task Scheduler XML per task
#                                          (Export-ScheduledTask output; triggers, action,
#                                          settings, Enabled, principal)  -- written by
#                                          scripts\generate_task_estate_manifest.py
#   deployment\manifest\task-estate.json   the human/diff view (name, slug, enabled, ...)
#
# WHAT IT DOES (per task, in manifest order): Register-ScheduledTask -Xml <file> -TaskName
# <name> -Force, with -User <current user> so the principal is THIS host's account (the XML
# carries the old host's SID); the XML's <Enabled>false</Enabled> keeps a disabled task
# disabled. Then Set-WrappedTaskAction (idempotent; the XML already carries the run_hidden
# wrapper, so this is a belt). Dry-run by default: prints the plan and touches nothing.
#
# It does NOT start any task (the service task starts at logon; start it by hand with
# Start-ScheduledTask after the .env is restored) and it never edits the manifest.
#
# Run from an ELEVATED PowerShell at the repo root (some tasks are RunLevel Highest):
#   .\deployment\register-estate-from-manifest.ps1                 # dry-run plan
#   .\deployment\register-estate-from-manifest.ps1 -Apply          # register everything
#   .\deployment\register-estate-from-manifest.ps1 -Apply -Only "cowork-cora-service","cora-watchdog"
#
# Afterwards:  .venv\Scripts\python.exe scripts\generate_task_estate_manifest.py --diff-only
#              -> expect ZERO task-estate-drift lines (settings/principal included).
#
# ASCII-only (D-016). Setup scripts (setup-*.ps1) remain the way to CREATE a new task;
# once it exists, regenerate the manifest and it is carried by this path from then on.

param(
    [switch]$Apply,
    [string[]]$Only = @(),
    [string]$ManifestDir = ""
)

$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_task-action.ps1"

# Default to the manifest that ships NEXT TO THIS SCRIPT (deployment\manifest) -- correct
# in the live checkout, a fresh clone at any path, and a git worktree. A hard-coded
# C:\Users\Harri\code\cora default would silently register ANOTHER checkout's estate.
if (-not $ManifestDir) { $ManifestDir = Join-Path $PSScriptRoot "manifest" }

$jsonPath = Join-Path $ManifestDir "task-estate.json"
$xmlDir   = Join-Path $ManifestDir "tasks"
if (-not (Test-Path $jsonPath)) { Write-Error "manifest not found: $jsonPath"; exit 1 }
if (-not (Test-Path $xmlDir))   { Write-Error "task XML folder not found: $xmlDir (regenerate the manifest)"; exit 1 }

$manifest = Get-Content $jsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
$tasks = @($manifest.tasks)
if ($Only.Count -gt 0) { $tasks = @($tasks | Where-Object { $Only -contains $_.name }) }

$user = "$env:USERDOMAIN\$env:USERNAME"
$mode = if ($Apply) { "APPLY" } else { "DRY-RUN" }
Write-Host ""
Write-Host "=== register-estate-from-manifest ($mode) : $($tasks.Count) task(s) from manifest generated $($manifest.generated_at_az) ==="
Write-Host "    principal user for every task: $user"
if ($manifest.host_time_zone) {
    $tz = (Get-TimeZone).Id
    if ($tz -ne $manifest.host_time_zone) {
        Write-Host "    WARNING: host time zone is '$tz' but the manifest was taken on '$($manifest.host_time_zone)' -- clock triggers will fire at the wrong local time. Set-TimeZone first." -ForegroundColor Yellow
    }
}
Write-Host ""

$registered = 0; $skipped = 0; $failed = 0; $missing = 0
foreach ($t in $tasks) {
    $slug = $t.log_slug
    $xml  = Join-Path $xmlDir "$slug.xml"
    $state = if ($t.enabled) { "enabled" } else { "DISABLED" }
    if (-not (Test-Path $xml)) {
        Write-Host ("  MISSING XML  {0}  ({1})  -> {2}" -f $t.name, $state, $xml) -ForegroundColor Red
        $missing++
        continue
    }
    if (-not $Apply) {
        Write-Host ("  [dry-run] would register  {0}  ({1}; {2}; {3}/{4})" -f $t.name, $state, $t.trigger_text, $t.run_as.run_level, $t.run_as.logon_type)
        continue
    }
    try {
        $xmlText = Get-Content $xml -Raw -Encoding UTF8
        Register-ScheduledTask -Xml $xmlText -TaskName $t.name -User $user -Force | Out-Null
        # Belt: the XML already carries the run_hidden wrapper; this is idempotent.
        [void](Set-WrappedTaskAction -TaskName $t.name)
        $live = Get-ScheduledTask -TaskName $t.name
        $liveState = [string]$live.State
        if (-not $t.enabled -and $liveState -ne "Disabled") {
            Disable-ScheduledTask -TaskName $t.name | Out-Null
            $liveState = "Disabled"
        }
        Write-Host ("  REGISTERED  {0}  ({1}; live state {2})" -f $t.name, $state, $liveState)
        $registered++
    } catch {
        Write-Host ("  FAILED  {0}  -> {1}" -f $t.name, $_.Exception.Message) -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
Write-Host ("=== done: registered={0} failed={1} missing-xml={2} (of {3}) ===" -f $registered, $failed, $missing, $tasks.Count)
if (-not $Apply) { Write-Host "    (dry-run; re-run with -Apply from an ELEVATED PowerShell to register)" }
if ($failed -gt 0 -or $missing -gt 0) { exit 1 }
exit 0
