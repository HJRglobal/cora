# rewrap-tasks-hidden.ps1
#
# Rewraps every Cora scheduled task action to run through
# deployment\run_hidden.py under pythonw.exe, so no task fire ever allocates a
# console window on the founder's daily-driver host. See run_hidden.py for the
# mechanism (pythonw = no console at all; CREATE_NO_WINDOW on the direct child
# = a windowless console the whole descendant tree inherits).
#
# VERIFIED 2026-09-02 via temporary probe tasks on this host:
#   unwrapped action -> child GetConsoleWindow() = 8979760, IsWindowVisible = 1
#   wrapped action   -> child GetConsoleWindow() = 0
#   Task Scheduler still reports the CHILD's exit code as Last Result (probe
#   child exited 7 -> LastTaskResult 7), including through the cmd.exe /c and
#   powershell -File action classes.
#
# WHAT IT CHANGES:  the task's ACTION only (Execute / Arguments), plus an
#   explicit per-task Priority override (see $PriorityOverrides). Triggers,
#   principal, RunLevel and every other setting are carried through untouched
#   -- the whole point of this bundle is that cadence does not change -- and
#   the read-back VERIFIES that per task rather than trusting it.
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
#   Apply to every ENABLED task except the service and watchdog:
#     .\deployment\rewrap-tasks-hidden.ps1 -Apply
#   Include the 18 deliberately-disabled tasks as well:
#     .\deployment\rewrap-tasks-hidden.ps1 -Apply -IncludeDisabled
#   The service and watchdog are HELD BACK from a bulk apply and can only be
#   rewrapped by naming them explicitly:
#     .\deployment\rewrap-tasks-hidden.ps1 -Apply -Only cora-watchdog
#     .\deployment\rewrap-tasks-hidden.ps1 -Apply -Only cowork-cora-service
#
# ROLLBACK: every task's XML is exported to
#   deployment\task-backups\<yyyy-MM-dd>\<task>.xml BEFORE it is modified.
#   From an elevated PowerShell, either of:
#     Register-ScheduledTask -Xml (Get-Content <backup>.xml -Raw) -TaskName <name> -Force
#     schtasks /Create /TN <name> /XML <backup>.xml /F
#   (The file is written as UTF-16 to match the declaration Export-ScheduledTask
#   emits, so both routes work -- see the export step for why that matters.)
#
# ASCII-only per D-016 (PowerShell 5.1 reads UTF-8 as Windows-1252).

param(
    [switch]$Apply,
    [string[]]$Only,
    [switch]$SelfTest,
    [switch]$IncludeDisabled
)

# The wrapping itself (slug rule, quoting rule, argument assembly) lives in
# _task-action.ps1 and is SHARED with every deployment\setup-*-task.ps1, so a
# task gets the same logs\tasks\<slug>.log name whichever path registered it.
# Duplicating the slug rule would silently split a task's log in two.
. "$PSScriptRoot\_task-action.ps1"

$RepoRoot   = "C:\Users\Harri\code\cora"
$PythonW    = Get-CoraPythonW
$Launcher   = Get-CoraLauncher
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
# is deliberately NOT done. Only 9/10 reach IDLE_PRIORITY_CLASS.
#
# The backup job is the single entry that gets a real change. Its live action is
# `python.exe scripts\backup_logs.py` with NO --include-kb, so the daily 1pm run
# copies logs and the small feature DBs, not the ~5.7 GB KB (that is the manual
# DR one-off). Priority 9 is still the right setting for a Drive-bound copy job
# on the founder's foreground machine -- it is just a smaller win than the
# original plan assumed.
$PriorityOverrides = @{
    "cowork-cora-backup" = 9
}

# ---------------------------------------------------------------------------
# Pure helpers (exercised by -SelfTest; the shared ones come from
# _task-action.ps1, where positional calls bind to -Slug -Execute -Arguments)
# ---------------------------------------------------------------------------

function Test-ActionIsWrapped {
    # Shape probe: is this action routed through the launcher AT ALL?
    param([string]$Execute, [string]$Arguments)
    if ([string]::IsNullOrEmpty($Execute)) { return $false }
    $isPythonW = $Execute.ToLower().EndsWith("pythonw.exe")
    $hasLauncher = $false
    if (-not [string]::IsNullOrEmpty($Arguments)) {
        $hasLauncher = $Arguments.ToLower().Contains("run_hidden.py")
    }
    return ($isPythonW -and $hasLauncher)
}

function Get-OriginalCommandLine {
    # The command the task is really trying to run, whether or not it is
    # currently wrapped. For a wrapped action that is everything after the
    # launcher's " -- " sentinel; for a bare action it is exe + args.
    #
    # This is what makes the script SELF-HEALING rather than merely idempotent:
    # a task wrapped against an OLD launcher path (repo moved, venv rebuilt) or
    # an OLD slug (task renamed) can be re-derived and re-wrapped correctly,
    # instead of being reported "already wrapped" while every fire fails with
    # 0x80070002.
    param([string]$Execute, [string]$Arguments)
    if (Test-ActionIsWrapped $Execute $Arguments) {
        $i = $Arguments.IndexOf(" -- ")
        if ($i -ge 0) { return $Arguments.Substring($i + 4).Trim() }
    }
    $cmd = Get-QuotedPath $Execute
    if (-not [string]::IsNullOrEmpty($Arguments)) { $cmd = $cmd + " " + $Arguments }
    return $cmd
}

function Get-ActionFingerprint {
    # Everything about a task that this script must NOT change. Compared
    # before/after so a silent normalisation is caught per task.
    #
    # Trigger COUNT is not enough: 11 Cora tasks carry their cadence in
    # Trigger.Repetition (Email Attachment Filer PT4H, security-monitor PT15M,
    # watchdog PT5M, ...). A dropped repetition leaves the count at 1 and turns
    # a 15-minute task into a once-daily one, so the interval, duration and
    # start boundary are part of the fingerprint.
    param($Task)
    $trig = @()
    foreach ($t in $Task.Triggers) {
        $ri = ""
        $rd = ""
        $rs = ""
        if ($null -ne $t.Repetition) {
            $ri = [string]$t.Repetition.Interval
            $rd = [string]$t.Repetition.Duration
            $rs = [string]$t.Repetition.StopAtDurationEnd
        }
        $trig += ($t.CimClass.CimClassName + "|" + [string]$t.StartBoundary + "|" +
                  [string]$t.EndBoundary + "|" + [string]$t.Enabled + "|" +
                  $ri + "|" + $rd + "|" + $rs)
    }
    return [pscustomobject]@{
        Triggers          = ($trig -join " ;; ")
        TriggerCount      = $Task.Triggers.Count
        RunLevel          = [string]$Task.Principal.RunLevel
        UserId            = [string]$Task.Principal.UserId
        LogonType         = [string]$Task.Principal.LogonType
        Enabled           = [string]$Task.Settings.Enabled
        ExecutionTimeLimit = [string]$Task.Settings.ExecutionTimeLimit
        MultipleInstances = [string]$Task.Settings.MultipleInstances
        Compatibility     = [string]$Task.Settings.Compatibility
    }
}

function Compare-Fingerprint {
    # Returns a list of "field: before -> after" strings for every difference.
    param($Before, $After)
    $diffs = New-Object System.Collections.ArrayList
    foreach ($prop in $Before.PSObject.Properties.Name) {
        $b = [string]$Before.$prop
        $a = [string]$After.$prop
        if ($b -ne $a) { [void]$diffs.Add($prop + ": [" + $b + "] -> [" + $a + "]") }
    }
    return $diffs
}

function Select-TargetTasks {
    # Target selection, as a pure function over {Name, State} rows so -SelfTest
    # can cover it. Returns @{ Targets; HeldBack; Disabled; Missing }.
    param(
        $Rows,
        [string[]]$OnlyNames,
        [switch]$WithDisabled
    )
    $targets = New-Object System.Collections.ArrayList
    $held = New-Object System.Collections.ArrayList
    $disabled = New-Object System.Collections.ArrayList
    $missing = New-Object System.Collections.ArrayList

    if ($OnlyNames) {
        foreach ($n in $OnlyNames) {
            $row = $Rows | Where-Object { $_.Name -eq $n }
            if ($row) { [void]$targets.Add($row.Name) } else { [void]$missing.Add($n) }
        }
        # -Only is an explicit, deliberate act: it overrides both the held-back
        # list and the disabled skip. That is the ONLY way to rewrap the
        # service or the watchdog.
        return @{ Targets = $targets; HeldBack = $held; Disabled = $disabled; Missing = $missing }
    }

    foreach ($row in $Rows) {
        if ($HeldBack -contains $row.Name) { [void]$held.Add($row.Name); continue }
        if ((-not $WithDisabled) -and ($row.State -eq "Disabled")) {
            # A disabled task never fires, so it never flashes a window:
            # wrapping it buys nothing and spends blast radius. 18 of the 94 are
            # disabled by design (several because the work moved to Make.com).
            [void]$disabled.Add($row.Name); continue
        }
        [void]$targets.Add($row.Name)
    }
    return @{ Targets = $targets; HeldBack = $held; Disabled = $disabled; Missing = $missing }
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

    # A THROWN error must FAIL the self-test, not print and let it report
    # PASSED. Measured 2026-09-02: with the default Continue, the
    # `Get-TaskSlug ""` assertion threw a ParameterArgumentValidationError
    # before Assert-Eq was ever reached, the error printed, and the script still
    # said "SELF-TEST PASSED" and exited 0 -- so the only automated gate on the
    # transformation logic was unsound for any breakage that throws.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Stop"
    try {
        Write-Host "=== slug sanitisation ==="
        Assert-Eq "slug spaces+dash" "Cora-Drive-Sweep" (Get-TaskSlug "Cora - Drive Sweep")
        Assert-Eq "slug parens" "Cora-Daily-Synthesis-F3E" (Get-TaskSlug "Cora - Daily Synthesis (F3E)")
        Assert-Eq "slug already clean" "cowork-cora-backup" (Get-TaskSlug "cowork-cora-backup")
        Assert-Eq "slug traversal" "evil" (Get-TaskSlug "..\..\evil")
        Assert-Eq "slug empty" "task" (Get-TaskSlug "")
        Assert-Eq "slug dots only" "task" (Get-TaskSlug "...")
        Assert-Eq "slug has no sentinel" $false ((Get-TaskSlug "a -- b").Contains(" -- "))
        # Cap after trim, re-trim after cap -- must match run_hidden.sanitize_slug.
        Assert-Eq "slug caps at 80" 80 ((Get-TaskSlug ("x" * 200)).Length)
        Assert-Eq "slug no trailing dash after cap" $false ((Get-TaskSlug (("y" * 79) + " zzz")).EndsWith("-"))

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

        Write-Host "=== class E: python.exe -m cora.main (the service) ==="
        Assert-Eq "E" ($L + " --name cowork-cora-service -- C:\Users\Harri\code\cora\.venv\Scripts\python.exe -m cora.main") `
            (Get-WrappedArguments "cowork-cora-service" "C:\Users\Harri\code\cora\.venv\Scripts\python.exe" "-m cora.main")

        Write-Host "=== class F: cmd.exe /c cd /d <dir> & <exe> <script> (quoting must survive verbatim) ==="
        $fArgs = "/c cd /d `"C:\Users\Harri\code\cora`" & `"C:\Users\Harri\code\cora\.venv\Scripts\python.exe`" `"C:\Users\Harri\code\cora\scripts\incremental_sync_asana.py`""
        Assert-Eq "F cmd.exe" ($L + " --name cowork-cora-kb-sync-asana -- cmd.exe " + $fArgs) `
            (Get-WrappedArguments "cowork-cora-kb-sync-asana" "cmd.exe" $fArgs)
        $f2 = "/c cd /d C:\Users\Harri\code\cora && C:\Users\Harri\code\cora\.venv\Scripts\python.exe C:\Users\Harri\code\cora\scripts\run_klaviyo_billing_audit.py --post"
        Assert-Eq "F bare cmd" ($L + " --name Cora-Klaviyo-Billing-Audit -- cmd " + $f2) `
            (Get-WrappedArguments "Cora-Klaviyo-Billing-Audit" "cmd" $f2)

        Write-Host "=== class G: powershell -File (the watchdog) ==="
        Assert-Eq "G" ($L + " --name cora-watchdog -- powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\Harri\code\cora\deployment\cora-watchdog.ps1") `
            (Get-WrappedArguments "cora-watchdog" "powershell" "-NoProfile -ExecutionPolicy Bypass -File C:\Users\Harri\code\cora\deployment\cora-watchdog.ps1")

        Write-Host "=== an exe path containing a space gets quoted ==="
        Assert-Eq "spaced exe" ($L + " --name x -- `"C:\Program Files\py\python.exe`" a.py") `
            (Get-WrappedArguments "x" "C:\Program Files\py\python.exe" "a.py")

        Write-Host "=== wrap detection ==="
        $wrapped = Get-WrappedArguments "Cora-Drive-Sweep" "C:\Users\Harri\code\cora\.venv\Scripts\python.exe" "scripts\run_drive_sweep.py"
        Assert-Eq "wrapped detected" $true (Test-ActionIsWrapped $PythonW $wrapped)
        Assert-Eq "unwrapped python not detected" $false (Test-ActionIsWrapped "C:\Users\Harri\code\cora\.venv\Scripts\python.exe" "scripts\run_drive_sweep.py")
        Assert-Eq "unwrapped cmd not detected" $false (Test-ActionIsWrapped "cmd.exe" "/c cd /d C:\x & y.exe")
        Assert-Eq "pythonw without launcher not detected" $false (Test-ActionIsWrapped $PythonW "some_other.py")
        Assert-Eq "empty execute not detected" $false (Test-ActionIsWrapped "" "")

        Write-Host "=== original-command recovery (self-healing) ==="
        Assert-Eq "original from bare action" "C:\py\python.exe a.py --x" (Get-OriginalCommandLine "C:\py\python.exe" "a.py --x")
        Assert-Eq "original from bare action, no args" "C:\py\python.exe" (Get-OriginalCommandLine "C:\py\python.exe" "")
        Assert-Eq "original from wrapped action" "C:\Users\Harri\code\cora\.venv\Scripts\python.exe scripts\run_drive_sweep.py" (Get-OriginalCommandLine $PythonW $wrapped)
        # A wrapped action whose own command contains " -- " must keep it: only
        # the FIRST sentinel is the launcher's.
        $withDash = Get-WrappedArguments "t" "C:\py\python.exe" "run.py -- --passthrough 1"
        Assert-Eq "original keeps a later double-dash" "C:\py\python.exe run.py -- --passthrough 1" (Get-OriginalCommandLine $PythonW $withDash)
        # Re-wrapping is a fixed point: wrap(original(wrapped)) == wrapped.
        $again = (Get-QuotedPath $Launcher) + " --name Cora-Drive-Sweep -- " + (Get-OriginalCommandLine $PythonW $wrapped)
        Assert-Eq "rewrap is a fixed point" $wrapped $again
        # A STALE wrap (old launcher path) still yields the right original, so
        # it can be re-wrapped rather than reported "already wrapped".
        $stale = "C:\OLD\repo\deployment\run_hidden.py --name Cora-Drive-Sweep -- C:\py\python.exe a.py"
        Assert-Eq "original from a stale wrap" "C:\py\python.exe a.py" (Get-OriginalCommandLine "C:\OLD\repo\.venv\Scripts\pythonw.exe" $stale)

        Write-Host "=== target selection ==="
        $rows = @(
            [pscustomobject]@{ Name = "Cora - A"; State = "Ready" },
            [pscustomobject]@{ Name = "Cora - B"; State = "Disabled" },
            [pscustomobject]@{ Name = "cowork-cora-service"; State = "Running" },
            [pscustomobject]@{ Name = "cora-watchdog"; State = "Ready" }
        )
        $sel = Select-TargetTasks -Rows $rows
        Assert-Eq "bulk skips held-back + disabled" "Cora - A" ($sel.Targets -join ",")
        Assert-Eq "bulk reports held-back" "cowork-cora-service,cora-watchdog" ($sel.HeldBack -join ",")
        Assert-Eq "bulk reports disabled" "Cora - B" ($sel.Disabled -join ",")
        $selD = Select-TargetTasks -Rows $rows -WithDisabled
        Assert-Eq "-IncludeDisabled adds them" "Cora - A,Cora - B" ($selD.Targets -join ",")
        $selO = Select-TargetTasks -Rows $rows -OnlyNames @("cowork-cora-service")
        Assert-Eq "-Only overrides held-back" "cowork-cora-service" ($selO.Targets -join ",")
        $selOD = Select-TargetTasks -Rows $rows -OnlyNames @("Cora - B")
        Assert-Eq "-Only overrides the disabled skip" "Cora - B" ($selOD.Targets -join ",")
        $selM = Select-TargetTasks -Rows $rows -OnlyNames @("Cora - typo")
        Assert-Eq "-Only records a miss" "Cora - typo" ($selM.Missing -join ",")
        Assert-Eq "-Only miss selects nothing" 0 $selM.Targets.Count

        Write-Host "=== fingerprint comparison ==="
        $fpA = [pscustomobject]@{ Triggers = "T|x|||PT15M||"; RunLevel = "Limited" }
        $fpB = [pscustomobject]@{ Triggers = "T|x|||PT15M||"; RunLevel = "Limited" }
        Assert-Eq "identical fingerprints compare clean" 0 (Compare-Fingerprint $fpA $fpB).Count
        $fpC = [pscustomobject]@{ Triggers = "T|x|||||"; RunLevel = "Limited" }
        Assert-Eq "a dropped repetition is caught" 1 (Compare-Fingerprint $fpA $fpC).Count
        $fpD = [pscustomobject]@{ Triggers = "T|x|||PT15M||"; RunLevel = "Highest" }
        Assert-Eq "a changed RunLevel is caught" 1 (Compare-Fingerprint $fpA $fpD).Count
    } catch {
        [void]$failures.Add("FAIL (threw) " + $_.Exception.Message + "`n  at " + $_.InvocationInfo.PositionMessage)
    } finally {
        $ErrorActionPreference = $prevEap
    }

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

$rows = @($all | ForEach-Object { [pscustomobject]@{ Name = $_.TaskName; State = [string]$_.State } })
$sel = Select-TargetTasks -Rows $rows -OnlyNames $Only -WithDisabled:$IncludeDisabled

foreach ($m in $sel.Missing) { Write-Host ("WARNING: -Only named an unknown task: " + $m) }
foreach ($h in $sel.HeldBack) { Write-Host ("HELD BACK (name it with -Only to rewrap): " + $h) }
if ($sel.Disabled.Count -gt 0) {
    Write-Host ("SKIPPED " + $sel.Disabled.Count + " DISABLED task(s) -- they never fire, so they never flash a window. Use -IncludeDisabled to wrap them anyway:")
    Write-Host ("  " + ($sel.Disabled -join ", "))
}

if ($Only -and $sel.Targets.Count -eq 0) {
    # Exit non-zero so a typo'd -Only cannot look like a successful staged
    # rollout to a human skimming, or to any caller checking the exit code.
    Write-Host "ABORT: -Only matched no task; nothing to do."
    exit 1
}

$targets = @($all | Where-Object { $sel.Targets -contains $_.TaskName } | Sort-Object TaskName)

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

    $slug = Get-TaskSlug $name
    $original = Get-OriginalCommandLine $oldExec $oldArgs
    $newArgs = (Get-QuotedPath $Launcher) + " --name " + $slug + " -- " + $original

    $wantPriority = $null
    if ($PriorityOverrides.ContainsKey($name)) {
        $wantPriority = $PriorityOverrides[$name]
    }
    $priorityNeeded = ($null -ne $wantPriority) -and ($task.Settings.Priority -ne $wantPriority)

    $actionCurrent = (($oldExec -eq $PythonW) -and ($oldArgs -eq $newArgs))

    if ($actionCurrent -and -not $priorityNeeded) {
        Write-Host ("OK    " + $name + " -- already wrapped")
        $alreadyOk++
        continue
    }

    if ($actionCurrent) {
        # The action is right but a Priority override is still outstanding. This
        # path exists because the priority used to be unreachable once a task
        # was wrapped: re-running setup-backup-task.ps1 registers a WRAPPED
        # action at Priority 7, and every later -Apply then said "already
        # wrapped" and left the override permanently unapplied.
        Write-Host ("PRIO  " + $name + " -- action already correct; priority " + $task.Settings.Priority + " -> " + $wantPriority)
    } elseif (Test-ActionIsWrapped $oldExec $oldArgs) {
        Write-Host ("REWRAP " + $name + " -- wrapped, but against a STALE launcher path or slug")
        Write-Host ("        before: " + $oldExec + " " + $oldArgs)
        Write-Host ("        after : " + $PythonW + " " + $newArgs)
    } else {
        Write-Host ("WRAP  " + $name)
        Write-Host ("        before: " + $oldExec + " " + $oldArgs)
        Write-Host ("        after : " + $PythonW + " " + $newArgs)
    }
    Write-Host ("        wd    : [" + $wd + "] (preserved)  triggers: " + $task.Triggers.Count +
                "  runlevel: " + $task.Principal.RunLevel + "  state: " + $task.State)
    if ($priorityNeeded) {
        Write-Host ("        priority: " + $task.Settings.Priority + " -> " + $wantPriority + " (IDLE class)")
    }

    if (-not $Apply) { $changed++; continue }

    # Back the XML up BEFORE touching anything -- this is the rollback path.
    #
    # -Encoding Unicode, NOT UTF8: Export-ScheduledTask emits a string whose
    # prolog says encoding="UTF-16". Written as UTF-8+BOM the declaration and
    # the bytes disagree, and XmlDocument.Load / `schtasks /Create /XML` both
    # refuse it with "There is no Unicode byte order mark" -- measured
    # 2026-09-02. The Get-Content -Raw rollback happened to still work, but the
    # reflexive alternative at recovery time did not.
    $safeName = [regex]::Replace($name, '[\\/:*?"<>|]', '_')
    $backupPath = Join-Path $backupDir ($safeName + ".xml")
    try {
        Export-ScheduledTask -TaskName $name | Set-Content -Path $backupPath -Encoding Unicode
    } catch {
        Write-Host ("        ERROR: XML export failed (" + $_.Exception.Message + ") -- NOT modifying this task")
        $failed++
        continue
    }
    # Existence is not validity: a truncated or empty file (full disk, AV lock)
    # would pass Test-Path and leave a useless rollback point.
    try {
        $probe = New-Object System.Xml.XmlDocument
        $probe.Load($backupPath)
        if ($null -eq $probe.DocumentElement) { throw "no document element" }
    } catch {
        Write-Host ("        ERROR: XML backup at " + $backupPath + " is not loadable (" + $_.Exception.Message + ") -- NOT modifying this task")
        $failed++
        continue
    }

    $fpBefore = Get-ActionFingerprint $task

    try {
        # Send ONLY the action, via -TaskName -Action.
        #
        # Measured 2026-09-02: pushing the whole task object back with
        # -InputObject fails with 0x80070057 "The parameter is incorrect" on any
        # task registered by schtasks.exe /Create -- 15 of these 94 carry
        # Compatibility=Vista, that registration's signature (Expected Invoice
        # Check, Klaviyo Billing Audit, Log Compaction, QBO Monthly Reports,
        # autowrite-digest, completion-sweep, decision-capture, feedback-health,
        # gap-digest, health-check, kb-hygiene, asana-email-sync,
        # hubspot-email-sync, monthly-deliverables, proactive-gaps), because
        # PowerShell has no monthly trigger cmdlet. -Action works on both
        # registration styles and preserves Trigger.Repetition (measured); the
        # fingerprint read-back below proves it per task rather than trusting it.
        if ([string]::IsNullOrEmpty($wd)) {
            $newAction = New-ScheduledTaskAction -Execute $PythonW -Argument $newArgs
        } else {
            $newAction = New-ScheduledTaskAction -Execute $PythonW -Argument $newArgs -WorkingDirectory $wd
        }
        Set-ScheduledTask -TaskName $name -Action $newAction | Out-Null
    } catch {
        Write-Host ("        ERROR: the ACTION was not applied (" + $_.Exception.Message + ")")
        Write-Host ("        the task is UNCHANGED; rollback is not needed, but the backup is at " + $backupPath)
        $failed++
        continue
    }

    if ($priorityNeeded) {
        # Priority lives in Settings, so it needs its own call -- and its own
        # try, so a settings failure is not reported as "the action failed" and
        # does not send the operator to roll back a wrap that succeeded.
        try {
            $settings = $task.Settings
            $settings.Priority = $wantPriority
            Set-ScheduledTask -TaskName $name -Settings $settings | Out-Null
        } catch {
            Write-Host ("        WARNING: the action WAS applied but Priority was not (" + $_.Exception.Message + ")")
            Write-Host ("        re-run this script to retry just the priority; no rollback is needed.")
        }
    }

    # Read back and assert the ONLY differences are the ones we intended.
    $after = Get-ScheduledTask -TaskName $name
    $problems = New-Object System.Collections.ArrayList
    if ($after.Actions[0].Execute -ne $PythonW) { [void]$problems.Add("Execute not applied") }
    if ($after.Actions[0].Arguments -ne $newArgs) { [void]$problems.Add("Arguments not applied") }
    if ($after.Actions[0].WorkingDirectory -ne $wd) {
        [void]$problems.Add("WorkingDirectory changed: [" + $wd + "] -> [" + $after.Actions[0].WorkingDirectory + "]")
    }
    foreach ($d in (Compare-Fingerprint $fpBefore (Get-ActionFingerprint $after))) {
        [void]$problems.Add("CADENCE/SETTINGS CHANGED -- " + $d)
    }
    if ($priorityNeeded -and $after.Settings.Priority -ne $wantPriority) {
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
Write-Host ("=== " + $mode + " summary: " + $changed + " to change / " + $alreadyOk + " already correct / " + $failed + " failed ===")
if (-not $Apply) {
    Write-Host "Nothing was changed. Re-run with -Apply from an ELEVATED PowerShell to apply."
    Write-Host "Stage it: -Apply -Only cowork-cora-security-monitor   (fires every 15 min, so it proves itself fast)"
}
if ($failed -gt 0) { exit 1 }
exit 0
