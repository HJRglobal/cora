# rewrap-tasks-hidden.ps1
#
# Rewraps every Cora scheduled task action to run through
# deployment\run_hidden.py under pythonw.exe, so no task fire ever allocates a
# console window on the founder's daily-driver host. See run_hidden.py for the
# mechanism (pythonw = no console at all; CREATE_NO_WINDOW on the direct child
# = a windowless console the whole descendant tree inherits).
#
# VERIFIED 2026-09-02 via a temporary probe task on this host:
#   unwrapped action -> child GetConsoleWindow() = 8979760, IsWindowVisible = 1
#   wrapped action   -> child GetConsoleWindow() = 0
#   Task Scheduler still reports the CHILD's exit code as Last Result
#   (probe child exited 7 -> LastTaskResult 7), including through the
#   cmd.exe /c and powershell -File action classes.
#
# WHAT IT CHANGES:  the task's ACTION only (Execute / Arguments), plus an
#   explicit per-task Priority override (see $PriorityOverrides). Triggers,
#   principal, RunLevel and every other setting are carried through untouched
#   -- the whole point of this bundle is that cadence does not change.
#
# WHAT IT READS:  the LIVE task definitions (Get-ScheduledTask). It never reads
#   the setup-*.ps1 scripts, which have drifted from live state (the service
#   task's registered action is `python.exe -m cora.main` while
#   setup-windows-task.ps1 still says .venv\Scripts\cora.exe).
#
# USAGE
#   Dry run (default, safe, no elevation needed):
#     .\deployment\rewrap-tasks-hidden.ps1
#   Self-test the transformation logic only (no task access at all):
#     .\deployment\rewrap-tasks-hidden.ps1 -SelfTest
#   Apply to one task (staged rollout -- do this first):
#     .\deployment\rewrap-tasks-hidden.ps1 -Apply -Only cowork-cora-security-monitor
#   Apply to the whole estate except the service and watchdog:
#     .\deployment\rewrap-tasks-hidden.ps1 -Apply
#   The service and watchdog are HELD BACK from a bulk apply and can only be
#   rewrapped by naming them explicitly:
#     .\deployment\rewrap-tasks-hidden.ps1 -Apply -Only cora-watchdog
#     .\deployment\rewrap-tasks-hidden.ps1 -Apply -Only cowork-cora-service
#
# ROLLBACK: every task's XML is exported to
#   deployment\task-backups\<yyyy-MM-dd>\<task>.xml BEFORE it is modified.
#   From an elevated PowerShell:
#     Register-ScheduledTask -Xml (Get-Content <backup>.xml -Raw) -TaskName <name> -Force
#
# ASCII-only per D-016 (PowerShell 5.1 reads UTF-8 as Windows-1252).

param(
    [switch]$Apply,
    [string[]]$Only,
    [switch]$SelfTest
)

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonW    = Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"
$Launcher   = Join-Path $RepoRoot "deployment\run_hidden.py"
$BackupRoot = Join-Path $RepoRoot "deployment\task-backups"

# Held back from a bulk -Apply. Both are recovery-critical: the service IS Cora,
# and the watchdog is what restarts her. Rewrapping either needs its own
# deliberate run plus the standard restart verification, so a bulk apply must
# never touch them by accident.
$HeldBack = @("cowork-cora-service", "cora-watchdog")

# Per-task Priority overrides, applied only with -Apply.
#
# NOTE (measured 2026-09-02): all 94 Cora tasks are ALREADY registered at
# Priority 7. Task Scheduler maps 7 AND 8 to BELOW_NORMAL_PRIORITY_CLASS, so
# the "set everything to 8" idea in the original plan buys exactly nothing and
# is deliberately NOT done. Only 9/10 reach IDLE_PRIORITY_CLASS, so the backup
# job -- the one task that streams a ~5.7 GB KB copy while Harrison works --
# is the single entry that gets a real change (7 -> 9).
$PriorityOverrides = @{
    "cowork-cora-backup" = 9
}

# ---------------------------------------------------------------------------
# Pure transformation helpers (exercised by -SelfTest, no task access)
# ---------------------------------------------------------------------------

function Get-TaskSlug {
    # Task name -> a filename-safe log slug. "Cora - Daily Synthesis (F3E)"
    # becomes "Cora-Daily-Synthesis-F3E". Runs of unsafe characters collapse to
    # a single dash so log names stay readable, and the result can never
    # contain the launcher's " -- " sentinel (it has no spaces at all).
    param([string]$Name)
    $s = [regex]::Replace($Name, '[^A-Za-z0-9._-]+', '-')
    $s = [regex]::Replace($s, '-{2,}', '-')
    $s = $s.Trim('-', '.', ' ')
    if ([string]::IsNullOrEmpty($s)) { $s = "task" }
    if ($s.Length -gt 80) { $s = $s.Substring(0, 80) }
    return $s
}

function Get-QuotedPath {
    # Quote only when needed. CreateProcess resolves a bare token against PATH,
    # which is what the `cmd` and `powershell` actions rely on, so quoting them
    # unconditionally would be a behaviour change.
    param([string]$Path)
    if ([string]::IsNullOrEmpty($Path)) { return $Path }
    if ($Path.Contains(" ")) { return '"' + $Path + '"' }
    return $Path
}

function Get-WrappedArguments {
    # Build the wrapped Arguments string. The original Execute and Arguments are
    # appended VERBATIM after the " -- " sentinel: run_hidden.py recovers the
    # child command line from the raw process command line (GetCommandLineW),
    # so the original quoting survives byte-for-byte. That is what makes the
    # `cmd.exe /c cd /d "<dir>" & "<exe>" "<script>"` actions safe to wrap --
    # tokenizing and re-quoting them would not round-trip.
    param(
        [string]$Slug,
        [string]$Execute,
        [string]$Arguments
    )
    $child = Get-QuotedPath $Execute
    if (-not [string]::IsNullOrEmpty($Arguments)) {
        $child = $child + " " + $Arguments
    }
    return (Get-QuotedPath $Launcher) + " --name " + $Slug + " -- " + $child
}

function Test-ActionIsWrapped {
    # Idempotency probe: an action already routed through the launcher.
    param([string]$Execute, [string]$Arguments)
    if ([string]::IsNullOrEmpty($Execute)) { return $false }
    $isPythonW = $Execute.ToLower().EndsWith("pythonw.exe")
    $hasLauncher = $false
    if (-not [string]::IsNullOrEmpty($Arguments)) {
        $hasLauncher = $Arguments.ToLower().Contains("run_hidden.py")
    }
    return ($isPythonW -and $hasLauncher)
}

# ---------------------------------------------------------------------------
# Self-test: table-driven over every action shape live in the estate
# ---------------------------------------------------------------------------

function Invoke-SelfTest {
    $failures = New-Object System.Collections.ArrayList
    function Assert-Eq($label, $expected, $actual) {
        if ($expected -ne $actual) {
            [void]$failures.Add("FAIL " + $label + "`n  expected: " + $expected + "`n  actual:   " + $actual)
        } else {
            Write-Host ("  ok  " + $label)
        }
    }

    Write-Host "=== slug sanitisation ==="
    Assert-Eq "slug spaces+dash" "Cora-Drive-Sweep" (Get-TaskSlug "Cora - Drive Sweep")
    Assert-Eq "slug parens" "Cora-Daily-Synthesis-F3E" (Get-TaskSlug "Cora - Daily Synthesis (F3E)")
    Assert-Eq "slug already clean" "cowork-cora-backup" (Get-TaskSlug "cowork-cora-backup")
    Assert-Eq "slug traversal" "evil" (Get-TaskSlug "..\..\evil")
    Assert-Eq "slug empty" "task" (Get-TaskSlug "")
    # A slug can never reintroduce the launcher's sentinel.
    Assert-Eq "slug has no sentinel" $false ((Get-TaskSlug "a -- b").Contains(" -- "))

    $L = Get-QuotedPath $Launcher

    Write-Host "=== class A: python.exe + bare absolute script, WD=repo ==="
    Assert-Eq "A" ($L + " --name Cora-Asana-Hygiene-Nudges -- C:\Users\Harri\code\cora\.venv\Scripts\python.exe C:\Users\Harri\code\cora\scripts\run_asana_hygiene_nudges.py") `
        (Get-WrappedArguments "Cora-Asana-Hygiene-Nudges" "C:\Users\Harri\code\cora\.venv\Scripts\python.exe" "C:\Users\Harri\code\cora\scripts\run_asana_hygiene_nudges.py")

    Write-Host "=== class B: python.exe + quoted absolute script + args ==="
    Assert-Eq "B" ($L + " --name Cora-Daily-Briefing -- C:\Users\Harri\code\cora\.venv\Scripts\python.exe `"C:\Users\Harri\code\cora\scripts\run_daily_briefing.py`" --time-budget-min 18") `
        (Get-WrappedArguments "Cora-Daily-Briefing" "C:\Users\Harri\code\cora\.venv\Scripts\python.exe" "`"C:\Users\Harri\code\cora\scripts\run_daily_briefing.py`" --time-budget-min 18")

    Write-Host "=== class C: python.exe + repo-relative script + args ==="
    Assert-Eq "C" ($L + " --name Cora-KB-Evals -- C:\Users\Harri\code\cora\.venv\Scripts\python.exe scripts\run_kb_evals.py --slack --channel cora-health") `
        (Get-WrappedArguments "Cora-KB-Evals" "C:\Users\Harri\code\cora\.venv\Scripts\python.exe" "scripts\run_kb_evals.py --slack --channel cora-health")

    Write-Host "=== class D/E: python.exe -m cora.main (the service) ==="
    Assert-Eq "E" ($L + " --name cowork-cora-service -- C:\Users\Harri\code\cora\.venv\Scripts\python.exe -m cora.main") `
        (Get-WrappedArguments "cowork-cora-service" "C:\Users\Harri\code\cora\.venv\Scripts\python.exe" "-m cora.main")

    Write-Host "=== class F: cmd.exe /c cd /d <dir> & <exe> <script> (quoting must survive verbatim) ==="
    $fArgs = "/c cd /d `"C:\Users\Harri\code\cora`" & `"C:\Users\Harri\code\cora\.venv\Scripts\python.exe`" `"C:\Users\Harri\code\cora\scripts\incremental_sync_asana.py`""
    Assert-Eq "F cmd.exe" ($L + " --name cowork-cora-kb-sync-asana -- cmd.exe " + $fArgs) `
        (Get-WrappedArguments "cowork-cora-kb-sync-asana" "cmd.exe" $fArgs)
    # The bare `cmd` variant (Expected Invoice Check / Klaviyo) must stay bare so
    # CreateProcess still resolves it against PATH.
    $f2 = "/c cd /d C:\Users\Harri\code\cora && C:\Users\Harri\code\cora\.venv\Scripts\python.exe C:\Users\Harri\code\cora\scripts\run_klaviyo_billing_audit.py --post"
    Assert-Eq "F bare cmd" ($L + " --name Cora-Klaviyo-Billing-Audit -- cmd " + $f2) `
        (Get-WrappedArguments "Cora-Klaviyo-Billing-Audit" "cmd" $f2)

    Write-Host "=== class G: powershell -File (the watchdog) ==="
    Assert-Eq "G" ($L + " --name cora-watchdog -- powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Harri\code\cora\deployment\cora-watchdog.ps1") `
        (Get-WrappedArguments "cora-watchdog" "powershell" "-NoProfile -ExecutionPolicy Bypass -File C:\Users\Harri\code\cora\deployment\cora-watchdog.ps1")

    Write-Host "=== an exe path containing a space gets quoted ==="
    Assert-Eq "spaced exe" ($L + " --name x -- `"C:\Program Files\py\python.exe`" a.py") `
        (Get-WrappedArguments "x" "C:\Program Files\py\python.exe" "a.py")

    Write-Host "=== idempotency ==="
    $wrapped = Get-WrappedArguments "Cora-Drive-Sweep" "C:\Users\Harri\code\cora\.venv\Scripts\python.exe" "scripts\run_drive_sweep.py"
    Assert-Eq "wrapped detected" $true (Test-ActionIsWrapped $PythonW $wrapped)
    Assert-Eq "unwrapped python not detected" $false (Test-ActionIsWrapped "C:\Users\Harri\code\cora\.venv\Scripts\python.exe" "scripts\run_drive_sweep.py")
    Assert-Eq "unwrapped cmd not detected" $false (Test-ActionIsWrapped "cmd.exe" "/c cd /d C:\x & y.exe")
    Assert-Eq "pythonw without launcher not detected" $false (Test-ActionIsWrapped $PythonW "some_other.py")
    # The sentinel must be findable at a stable position: exactly one " -- "
    # before the child command begins.
    Assert-Eq "sentinel present once before child" 1 ([regex]::Matches($wrapped.Substring(0, $wrapped.IndexOf(" -- ") + 4), " -- ").Count)

    Write-Host ""
    if ($failures.Count -gt 0) {
        foreach ($f in $failures) { Write-Host $f }
        Write-Host ("SELF-TEST FAILED: " + $failures.Count + " assertion(s).")
        return 1
    }
    Write-Host "SELF-TEST PASSED."
    return 0
}

if ($SelfTest) {
    exit (Invoke-SelfTest)
}

# ---------------------------------------------------------------------------
# Live estate work
# ---------------------------------------------------------------------------

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PythonW)) {
    Write-Host ("ABORT: pythonw.exe not found at " + $PythonW)
    exit 1
}
if (-not (Test-Path $Launcher)) {
    Write-Host ("ABORT: launcher not found at " + $Launcher)
    exit 1
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$isElevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($Apply -and -not $isElevated) {
    # 9 tasks run -RunLevel Highest (watchdog, founders-os-sweep, qbo-token-refresh,
    # asana-hygiene-nudges, channel-health-monitor, false-deflection-watch,
    # osn-metrics-digest, weekly-health-metrics, cash-flow-pulse). Set-ScheduledTask
    # against those needs elevation, and a partial apply is worse than none.
    Write-Host "ABORT: -Apply requires an ELEVATED PowerShell (9 Cora tasks run -RunLevel Highest; D-036)."
    Write-Host "Re-run from an elevated prompt:"
    Write-Host "  cd C:\Users\Harri\code\cora"
    if ($Only) {
        Write-Host ("  .\deployment\rewrap-tasks-hidden.ps1 -Apply -Only " + ($Only -join ","))
    } else {
        Write-Host "  .\deployment\rewrap-tasks-hidden.ps1 -Apply"
    }
    exit 1
}

$all = @(Get-ScheduledTask | Where-Object {
    $_.TaskName -like "Cora - *" -or $_.TaskName -like "cowork-cora-*" -or $_.TaskName -eq "cora-watchdog"
} | Sort-Object TaskName)

if ($Only) {
    $targets = @($all | Where-Object { $Only -contains $_.TaskName })
    $missing = @($Only | Where-Object { $t = $_; -not ($all | Where-Object { $_.TaskName -eq $t }) })
    foreach ($m in $missing) { Write-Host ("WARNING: -Only named an unknown task: " + $m) }
} else {
    $targets = @($all | Where-Object { $HeldBack -notcontains $_.TaskName })
    $skipped = @($all | Where-Object { $HeldBack -contains $_.TaskName })
    foreach ($s in $skipped) {
        Write-Host ("HELD BACK (name it with -Only to rewrap): " + $s.TaskName)
    }
}

$mode = "DRY RUN"
if ($Apply) { $mode = "APPLY" }
Write-Host ""
Write-Host ("=== rewrap-tasks-hidden [" + $mode + "] " + $targets.Count + " of " + $all.Count + " Cora tasks ===")
Write-Host ""

$backupDir = Join-Path $BackupRoot (Get-Date -Format "yyyy-MM-dd")
if ($Apply) {
    if (-not (Test-Path $backupDir)) {
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    }
    Write-Host ("XML backups: " + $backupDir)
    Write-Host ""
}

$changed = 0
$alreadyOk = 0
$failed = 0

foreach ($task in $targets) {
    $name = $task.TaskName
    if ($task.Actions.Count -ne 1) {
        # Measured 2026-09-02: every Cora task has exactly one action. A
        # multi-action task would need each action wrapped and is not something
        # this script should guess at.
        Write-Host ("SKIP  " + $name + " -- has " + $task.Actions.Count + " actions (expected 1); rewrap by hand")
        $failed++
        continue
    }

    $action = $task.Actions[0]
    $oldExec = $action.Execute
    $oldArgs = $action.Arguments
    $wd = $action.WorkingDirectory

    if (Test-ActionIsWrapped $oldExec $oldArgs) {
        Write-Host ("OK    " + $name + " -- already wrapped")
        $alreadyOk++
        continue
    }

    $slug = Get-TaskSlug $name
    $newArgs = Get-WrappedArguments $slug $oldExec $oldArgs

    $wantPriority = $null
    if ($PriorityOverrides.ContainsKey($name)) {
        $wantPriority = $PriorityOverrides[$name]
    }

    Write-Host ("WRAP  " + $name)
    Write-Host ("        before: " + $oldExec + " " + $oldArgs)
    Write-Host ("        after : " + $PythonW + " " + $newArgs)
    Write-Host ("        wd    : [" + $wd + "] (preserved)  triggers: " + $task.Triggers.Count + "  runlevel: " + $task.Principal.RunLevel)
    if ($null -ne $wantPriority) {
        Write-Host ("        priority: " + $task.Settings.Priority + " -> " + $wantPriority + " (IDLE class; keeps the KB copy off Harrison's foreground)")
    }

    if (-not $Apply) { $changed++; continue }

    # Back the XML up BEFORE touching anything -- this is the rollback path.
    $safeName = [regex]::Replace($name, '[\\/:*?"<>|]', '_')
    $backupPath = Join-Path $backupDir ($safeName + ".xml")
    try {
        Export-ScheduledTask -TaskName $name | Set-Content -Path $backupPath -Encoding UTF8
    } catch {
        Write-Host ("        ERROR: XML export failed (" + $_.Exception.Message + ") -- NOT modifying this task")
        $failed++
        continue
    }
    if (-not (Test-Path $backupPath)) {
        Write-Host "        ERROR: XML backup missing after export -- NOT modifying this task"
        $failed++
        continue
    }

    $triggerCountBefore = $task.Triggers.Count
    $runLevelBefore = [string]$task.Principal.RunLevel

    try {
        # Mutate the live definition object and push it back whole, so triggers,
        # principal and every untouched setting are carried through by
        # construction rather than rebuilt (and silently defaulted).
        $task.Actions[0].Execute = $PythonW
        $task.Actions[0].Arguments = $newArgs
        if ($null -ne $wantPriority) {
            $task.Settings.Priority = $wantPriority
        }
        Set-ScheduledTask -InputObject $task | Out-Null
    } catch {
        Write-Host ("        ERROR: Set-ScheduledTask failed (" + $_.Exception.Message + ")")
        Write-Host ("        rollback: Register-ScheduledTask -Xml (Get-Content '" + $backupPath + "' -Raw) -TaskName '" + $name + "' -Force")
        $failed++
        continue
    }

    # Read back and assert the ONLY differences are the ones we intended.
    $after = Get-ScheduledTask -TaskName $name
    $problems = New-Object System.Collections.ArrayList
    if ($after.Actions[0].Execute -ne $PythonW) { [void]$problems.Add("Execute not applied") }
    if ($after.Actions[0].Arguments -ne $newArgs) { [void]$problems.Add("Arguments not applied") }
    if ($after.Actions[0].WorkingDirectory -ne $wd) {
        [void]$problems.Add("WorkingDirectory changed: [" + $wd + "] -> [" + $after.Actions[0].WorkingDirectory + "]")
    }
    if ($after.Triggers.Count -ne $triggerCountBefore) {
        [void]$problems.Add("trigger count changed: " + $triggerCountBefore + " -> " + $after.Triggers.Count)
    }
    if ([string]$after.Principal.RunLevel -ne $runLevelBefore) {
        [void]$problems.Add("RunLevel changed: " + $runLevelBefore + " -> " + $after.Principal.RunLevel)
    }
    if ($null -ne $wantPriority -and $after.Settings.Priority -ne $wantPriority) {
        [void]$problems.Add("Priority not applied: still " + $after.Settings.Priority)
    }

    if ($problems.Count -gt 0) {
        foreach ($p in $problems) { Write-Host ("        VERIFY FAILED: " + $p) }
        Write-Host ("        rollback: Register-ScheduledTask -Xml (Get-Content '" + $backupPath + "' -Raw) -TaskName '" + $name + "' -Force")
        $failed++
        continue
    }

    Write-Host "        applied + verified"
    $changed++
}

Write-Host ""
Write-Host ("=== " + $mode + " summary: " + $changed + " to wrap / " + $alreadyOk + " already wrapped / " + $failed + " failed ===")
if (-not $Apply) {
    Write-Host "Nothing was changed. Re-run with -Apply from an ELEVATED PowerShell to apply."
    Write-Host "Stage it: -Apply -Only cowork-cora-security-monitor   (fires every 15 min, so it proves itself fast)"
}
if ($failed -gt 0) { exit 1 }
exit 0
