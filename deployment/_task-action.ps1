# _task-action.ps1 -- SHARED helper: build a windowless scheduled-task action.
#
# Dot-source it, never run it:
#   . "$PSScriptRoot\_task-action.ps1"
#   $Action = New-WrappedTaskAction -TaskName $TaskName -Execute $Python `
#       -Argument $Script -WorkingDirectory $RepoRoot
#
# WHY: a plain New-ScheduledTaskAction pointing at python.exe / cmd.exe /
# powershell registers a CONSOLE action, and every fire flashes a window that
# steals focus on the founder's daily-driver host. Routing the action through
# deployment\run_hidden.py under pythonw.exe removes the window for the whole
# descendant tree. See run_hidden.py for the mechanism and the measurements.
#
# This file is the SINGLE definition of the wrapping, used by BOTH:
#   * deployment\rewrap-tasks-hidden.ps1  (rewraps the already-registered estate)
#   * deployment\setup-*-task.ps1         (registers new/replacement tasks)
# so a task gets the SAME logs\tasks\<slug>.log name whichever path created it.
# Duplicating the slug rule in two places would silently split one task's log
# in two.
#
# ASCII-only per D-016 (PowerShell 5.1 reads UTF-8 as Windows-1252).

$script:CoraRepoRoot = "C:\Users\Harri\code\cora"
$script:CoraPythonW  = Join-Path $script:CoraRepoRoot ".venv\Scripts\pythonw.exe"
$script:CoraLauncher = Join-Path $script:CoraRepoRoot "deployment\run_hidden.py"

function Get-CoraPythonW { return $script:CoraPythonW }
function Get-CoraLauncher { return $script:CoraLauncher }

function Get-TaskSlug {
    # Task name -> a filename-safe log slug. "Cora - Daily Synthesis (F3E)"
    # becomes "Cora-Daily-Synthesis-F3E". Runs of unsafe characters collapse to
    # a single dash so log names stay readable, and the result can never contain
    # the launcher's " -- " sentinel (it has no spaces at all).
    param([Parameter(Mandatory = $true)][string]$Name)
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
    # Build the launcher argument string for an original (Execute, Arguments).
    #
    # The original command is appended VERBATIM after the " -- " sentinel:
    # run_hidden.py recovers the child command line from the raw process command
    # line (GetCommandLineW), so the original quoting survives byte-for-byte.
    # That is what makes the `cmd.exe /c cd /d "<dir>" & "<exe>" "<script>"`
    # actions safe to wrap -- tokenizing and re-quoting them would not round-trip.
    param(
        [Parameter(Mandatory = $true)][string]$Slug,
        [Parameter(Mandatory = $true)][string]$Execute,
        [string]$Arguments
    )
    $child = Get-QuotedPath $Execute
    if (-not [string]::IsNullOrEmpty($Arguments)) {
        $child = $child + " " + $Arguments
    }
    return (Get-QuotedPath $script:CoraLauncher) + " --name " + $Slug + " -- " + $child
}

function New-WrappedTaskAction {
    <#
    .SYNOPSIS
        Drop-in replacement for New-ScheduledTaskAction that registers a
        WINDOWLESS action.
    .DESCRIPTION
        Takes the same -Execute / -Argument / -WorkingDirectory you would have
        passed to New-ScheduledTaskAction, plus the -TaskName (needed for the
        log slug), and returns a task action that runs the same command through
        deployment\run_hidden.py under pythonw.exe.

        Falls back to an UNWRAPPED action -- with a warning -- if pythonw.exe or
        the launcher is missing. A setup script must still be able to register a
        working task on a host where the venv has not been built yet; a visible
        window is a nuisance, an unregistered task is an outage.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Execute,
        [string]$Argument,
        [string]$WorkingDirectory
    )

    if ((-not (Test-Path $script:CoraPythonW)) -or (-not (Test-Path $script:CoraLauncher))) {
        Write-Warning ("run_hidden wrapping SKIPPED for '" + $TaskName + "': missing " +
            $script:CoraPythonW + " or " + $script:CoraLauncher +
            ". Registering a plain console action (it will flash a window). " +
            "Re-run deployment\rewrap-tasks-hidden.ps1 -Apply once the venv exists.")
        if ([string]::IsNullOrEmpty($WorkingDirectory)) {
            return New-ScheduledTaskAction -Execute $Execute -Argument $Argument
        }
        return New-ScheduledTaskAction -Execute $Execute -Argument $Argument -WorkingDirectory $WorkingDirectory
    }

    $slug = Get-TaskSlug $TaskName
    $wrapped = Get-WrappedArguments -Slug $slug -Execute $Execute -Arguments $Argument

    if ([string]::IsNullOrEmpty($WorkingDirectory)) {
        return New-ScheduledTaskAction -Execute $script:CoraPythonW -Argument $wrapped
    }
    return New-ScheduledTaskAction -Execute $script:CoraPythonW -Argument $wrapped -WorkingDirectory $WorkingDirectory
}

function Set-WrappedTaskAction {
    <#
    .SYNOPSIS
        Rewrap an ALREADY-REGISTERED task's action in place.
    .DESCRIPTION
        For the setup scripts that register via schtasks.exe /Create rather than
        Register-ScheduledTask. Call it immediately AFTER the /Create.

        WHY NOT WRAP THE /TR STRING (measured 2026-09-02): schtasks rejects a
        /TR value longer than 261 characters --
            ERROR: Value for '/TR' option cannot be more than 261 character(s).
        The wrapper prefix alone (pythonw.exe + run_hidden.py + --name <slug> +
        " -- ") is ~113 characters before the original command, so wrapping a
        realistic command at the /TR layer either fails outright or sits a few
        characters from failing. Set-ScheduledTask goes through the COM/CIM
        task API, which has no such limit -- this is the same call
        rewrap-tasks-hidden.ps1 uses for the whole estate.

        Idempotent: a task whose action is already wrapped is left alone.
        Returns $true if the task is wrapped when the function returns.
    #>
    param([Parameter(Mandatory = $true)][string]$TaskName)

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Warning ("Set-WrappedTaskAction: no task named '" + $TaskName + "' -- not wrapped.")
        return $false
    }
    if ($task.Actions.Count -ne 1) {
        Write-Warning ("Set-WrappedTaskAction: '" + $TaskName + "' has " +
            $task.Actions.Count + " actions; wrap it by hand.")
        return $false
    }

    $exec = $task.Actions[0].Execute
    $curArgs = $task.Actions[0].Arguments
    if ($exec.ToLower().EndsWith("pythonw.exe") -and
        (-not [string]::IsNullOrEmpty($curArgs)) -and
        $curArgs.ToLower().Contains("run_hidden.py")) {
        return $true  # already wrapped
    }
    if ((-not (Test-Path $script:CoraPythonW)) -or (-not (Test-Path $script:CoraLauncher))) {
        Write-Warning ("Set-WrappedTaskAction: pythonw.exe or run_hidden.py missing -- '" +
            $TaskName + "' left as a console action (it will flash a window).")
        return $false
    }

    $slug = Get-TaskSlug $TaskName
    $wrapped = Get-WrappedArguments -Slug $slug -Execute $exec -Arguments $curArgs
    $wd = $task.Actions[0].WorkingDirectory
    try {
        # -Action, NOT -InputObject. Measured 2026-09-02: pushing the whole task
        # object back with -InputObject fails with 0x80070057 "The parameter is
        # incorrect" on ANY task registered by schtasks.exe /Create -- which is
        # how the monthly- and minute-cadence Cora tasks are registered, because
        # PowerShell has no monthly trigger cmdlet. Sending only the action works
        # on both registration styles and leaves the triggers untouched.
        if ([string]::IsNullOrEmpty($wd)) {
            $newAction = New-ScheduledTaskAction -Execute $script:CoraPythonW -Argument $wrapped
        } else {
            $newAction = New-ScheduledTaskAction -Execute $script:CoraPythonW -Argument $wrapped -WorkingDirectory $wd
        }
        Set-ScheduledTask -TaskName $TaskName -Action $newAction | Out-Null
    } catch {
        Write-Warning ("Set-WrappedTaskAction failed for '" + $TaskName + "': " +
            $_.Exception.Message + " (elevation may be required for a /RL HIGHEST task)")
        return $false
    }
    $after = Get-ScheduledTask -TaskName $TaskName
    if ($after.Actions[0].Arguments -ne $wrapped) {
        Write-Warning ("Set-WrappedTaskAction: read-back mismatch for '" + $TaskName + "'.")
        return $false
    }
    Write-Host ("  windowless: '" + $TaskName + "' now runs via run_hidden.py (log: logs\tasks\" + $slug + "-<date>.log)")
    return $true
}
