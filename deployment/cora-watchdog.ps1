# cora-watchdog.ps1
# Heartbeat watchdog for the always-on Cora service.
# Runs on a schedule (every 5 min). If data/health/heartbeat.txt is stale beyond
# -StaleMinutes, it restarts Cora via restart-cora.ps1 -- with an anti-thrash
# cooldown AND an hourly cap, so a persistently-broken bot (e.g. G: Drive still down)
# is NOT thrash-restarted; instead it holds and logs an ESCALATE line for alerting.
#
# WHY: 2026-07-15 the G: Google Drive mount blipped (unmount->remount, ~30s) and Cora
# died with no error and no auto-recovery for ~9.5h. RestartOnFailure does NOT cover a
# HANG (no failure exit code). This watchdog is the auto-recovery for that class.
#
# 2026-08-19 HARDENING (cq-7915a8647cff). The 8/18 forensics could not tell
# "watchdog healthy" from "watchdog never ran": the healthy path logged NOTHING, so a
# 29h window with zero lines looked like proof of a broken stale-branch when it was
# equally consistent with a task that never fired. Three additions close that:
#   1. TICK LINE -- every run logs, at most once per -OkLogIntervalMinutes, that it
#      ran and what age it saw. Silence now means the task did not run, full stop.
#   2. ERROR LINE -- the whole body is wrapped, so an unhandled throw under
#      ErrorActionPreference=Stop leaves a watchdog_error row instead of nothing.
#   3. RESTART VERIFICATION -- restart_exit 0 is NOT proof of a restart. After the
#      restart script returns we wait (bounded) for the heartbeat to actually go
#      fresh, and (when present) for a NEW pid in logs/cora-instances.jsonl. Result:
#      restart_verified, or restart_unverified with ESCALATE_ALERT.
# Also: the elevation self-check. restart-cora.ps1 hard-requires admin, so a
# non-elevated watchdog can only ever log restart failures -- it now says so loudly
# instead of discovering it mid-incident.
#
# Run elevated (restart-cora.ps1 requires admin). Use -DryRun to see the decision only.
#   powershell -NoProfile -ExecutionPolicy Bypass -File deployment\cora-watchdog.ps1 -DryRun
param(
    [int]$StaleMinutes = 6,
    [int]$CooldownMinutes = 15,
    [int]$MaxRestartsPerHour = 3,
    [int]$OkLogIntervalMinutes = 60,
    [int]$VerifyWaitSeconds = 90,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$Root = "C:\Users\Harri\code\cora"
Set-Location $Root

$hbPath    = Join-Path $Root "data\health\heartbeat.txt"
$instPath  = Join-Path $Root "data\health\instance.json"
$ledgerPath= Join-Path $Root "logs\cora-instances.jsonl"
$statePath = Join-Path $Root "data\health\watchdog-state.json"
$logPath   = Join-Path $Root ("logs\watchdog-" + (Get-Date -Format "yyyy-MM-dd") + ".jsonl")
$nowUtc    = (Get-Date).ToUniversalTime()

function Write-WLog($obj) {
    try {
        ($obj | ConvertTo-Json -Compress) | Add-Content -Path $logPath -Encoding utf8
    } catch {
        # Logging must never be the thing that kills the watchdog.
    }
}

# Read the heartbeat sentinel. FIRST LINE only: heartbeat.txt is contractually a bare
# ISO-8601 UTC timestamp (ten parsers depend on that), but reading line 1 rather than
# the whole blob means a future extra line cannot blind the watchdog.
function Read-HeartbeatAge($path, $now) {
    if (-not (Test-Path $path)) { return $null }
    $raw = $null
    try { $raw = (Get-Content $path -TotalCount 1 -ErrorAction Stop) } catch { return $null }
    if ($null -eq $raw) { return $null }
    $raw = ([string]$raw).Trim()
    if ($raw -eq "") { return $null }
    try {
        $t = [datetimeoffset]::Parse($raw).UtcDateTime
    } catch {
        return $null
    }
    return [math]::Round((($now - $t).TotalMinutes), 1)
}

# Current instance pid, or $null when the sentinel is absent (a pre-2026-08-19 bot
# build, or a first run). Absence is never treated as a fault.
function Read-InstancePid($path) {
    if (-not (Test-Path $path)) { return $null }
    try {
        $j = Get-Content $path -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($null -ne $j.pid) { return [int]$j.pid }
    } catch { }
    return $null
}

function Read-LastStartPid($path) {
    if (-not (Test-Path $path)) { return $null }
    try {
        $lines = @(Get-Content $path -Tail 40 -ErrorAction Stop)
    } catch { return $null }
    $found = $null
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try {
            $row = $line | ConvertFrom-Json
        } catch { continue }
        if ($row.event -eq "start" -and $null -ne $row.pid) { $found = [int]$row.pid }
    }
    return $found
}

try {
    # 0. Elevation self-check. The kill in restart-cora.ps1 needs admin; without it
    #    every "restart" this watchdog performs is guaranteed to fail at the gate.
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    $isElevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    # 1. Heartbeat present + parseable?
    if (-not (Test-Path $hbPath)) {
        Write-WLog @{ ts = $nowUtc.ToString("o"); event = "no_heartbeat_file"; action = "ESCALATE_ALERT" }
        Write-Host "WATCHDOG: heartbeat file missing -- manual check needed"
        exit 0
    }
    $ageMin = Read-HeartbeatAge $hbPath $nowUtc
    if ($null -eq $ageMin) {
        Write-WLog @{ ts = $nowUtc.ToString("o"); event = "unparseable_heartbeat" }
        Write-Host ("WATCHDOG: could not parse heartbeat value")
        exit 0
    }

    # 2. Load state (last restart, restarts in the last hour, last tick log).
    $lastRestart = $null
    $lastOkLog = $null
    $recent = @()
    if (Test-Path $statePath) {
        try {
            $state = Get-Content $statePath -Raw | ConvertFrom-Json
            $lastRestart = $state.last_restart
            $lastOkLog = $state.last_tick_log
            $hourAgo = $nowUtc.AddHours(-1)
            if ($state.restarts) {
                $recent = @(@($state.restarts) | Where-Object { [datetimeoffset]::Parse($_).UtcDateTime -gt $hourAgo })
            }
        } catch { }
    }

    function Save-State($lastRestartVal, $restartsVal, $lastTickVal) {
        $obj = @{ last_restart = $lastRestartVal; restarts = @($restartsVal); last_tick_log = $lastTickVal }
        try {
            ($obj | ConvertTo-Json -Compress) | Set-Content -Path $statePath -Encoding utf8
        } catch { }
    }

    # 3. Healthy -> quiet exit, but leave a periodic TICK so that silence in this log
    #    means "the watchdog did not run", never "the watchdog saw nothing wrong".
    if ($ageMin -le $StaleMinutes) {
        $due = $true
        if ($lastOkLog) {
            try {
                $sinceTick = ($nowUtc - [datetimeoffset]::Parse($lastOkLog).UtcDateTime).TotalMinutes
                if ($sinceTick -lt $OkLogIntervalMinutes) { $due = $false }
            } catch { }
        }
        if ($due) {
            $tick = @{ ts = $nowUtc.ToString("o"); event = "tick"; age_min = $ageMin; healthy = $true; elevated = $isElevated }
            if (-not $isElevated) { $tick["action"] = "ESCALATE_ALERT"; $tick["note"] = "watchdog NOT elevated -- restart-cora.ps1 will refuse" }
            Write-WLog $tick
            Save-State $lastRestart $recent $nowUtc.ToString("o")
        }
        Write-Host ("WATCHDOG: healthy (heartbeat " + $ageMin + " min old)")
        exit 0
    }

    # 4. Stale. Cooldown: do not restart again within CooldownMinutes of the last one.
    if ($lastRestart) {
        $sinceLast = ($nowUtc - [datetimeoffset]::Parse($lastRestart).UtcDateTime).TotalMinutes
        if ($sinceLast -lt $CooldownMinutes) {
            Write-WLog @{ ts = $nowUtc.ToString("o"); event = "stale_in_cooldown"; age_min = $ageMin; since_last_restart_min = [math]::Round($sinceLast,1) }
            Write-Host ("WATCHDOG: stale (" + $ageMin + " min) but in cooldown; skipping")
            exit 0
        }
    }

    # Thrash guard: if already restarted MaxRestartsPerHour times this hour, hold + escalate.
    if ($recent.Count -ge $MaxRestartsPerHour) {
        Write-WLog @{ ts = $nowUtc.ToString("o"); event = "thrash_guard_hold"; age_min = $ageMin; restarts_last_hour = $recent.Count; action = "ESCALATE_ALERT" }
        Write-Host ("WATCHDOG: stale (" + $ageMin + " min) but already restarted " + $recent.Count + "x this hour -- NOT thrashing. Manual intervention (check G: Drive mount).")
        exit 0
    }

    # 5. Decide to restart.
    if ($DryRun) {
        Write-Host ("WATCHDOG [DRYRUN]: WOULD restart Cora (heartbeat " + $ageMin + " min stale)")
        Write-WLog @{ ts = $nowUtc.ToString("o"); event = "would_restart_dryrun"; age_min = $ageMin; elevated = $isElevated }
        exit 0
    }

    if (-not $isElevated) {
        # restart-cora.ps1 exits 1 at its own elevation gate, so this would be a
        # guaranteed-failed restart recorded as an attempt. Say the real thing.
        Write-WLog @{ ts = $nowUtc.ToString("o"); event = "restart_blocked_not_elevated"; age_min = $ageMin; action = "ESCALATE_ALERT" }
        Write-Host ("WATCHDOG: heartbeat " + $ageMin + " min stale but this watchdog is NOT elevated -- restart-cora.ps1 requires admin. Fix the task's Run-With-Highest-Privileges setting.")
        exit 0
    }

    $pidBefore = Read-InstancePid $instPath
    Write-WLog @{ ts = $nowUtc.ToString("o"); event = "restart_begin"; age_min = $ageMin; pid_before = $pidBefore }
    Write-Host ("WATCHDOG: heartbeat " + $ageMin + " min stale -> restarting Cora")

    # 6. RECORD THE ATTEMPT FIRST, and isolate the call.
    #
    # This ordering is the whole anti-thrash guarantee, and the previous version
    # lost it on exactly the failure it exists for (D-051 lens-4 HIGH, caught
    # before merge). restart-cora.ps1 sets ErrorActionPreference=Stop and gates on
    # Write-Error when the import smoke fails -- and under Stop, Write-Error is a
    # TERMINATING error that propagates straight out of the `&` call into the
    # outer catch, skipping the Save-State below. Measured: with a hung bot AND a
    # broken HEAD, neither the cooldown nor MaxRestartsPerHour ever saw the
    # attempt, so the task re-fired every 5 minutes forever -- each run killing
    # the bot and running a 2.5s import smoke -- while reporting only as a WARN.
    # That is precisely what the file header promises cannot happen.
    $recent = @($recent) + @($nowUtc.ToString("o"))
    Save-State $nowUtc.ToString("o") $recent $nowUtc.ToString("o")

    $rc = 1
    try {
        & (Join-Path $Root "deployment\restart-cora.ps1")
        $rc = $LASTEXITCODE
    } catch {
        Write-WLog @{ ts = (Get-Date).ToUniversalTime().ToString("o"); event = "restart_script_error"; error = ([string]$_.Exception.Message); action = "ESCALATE_ALERT" }
        Write-Host ("WATCHDOG: restart-cora.ps1 threw: " + $_.Exception.Message)
    }
    Write-WLog @{ ts = $nowUtc.ToString("o"); event = "restart_done"; age_min = $ageMin; restart_exit = $rc; restarts_last_hour = $recent.Count }
    Write-Host ("WATCHDOG: restart complete (exit " + $rc + ")")

    # 7. VERIFY. An exit code says the script finished, not that a bot came back.
    #    Primary evidence is a heartbeat that has actually gone fresh; the pid change
    #    is corroboration when the instance ledger exists.
    $deadline = (Get-Date).AddSeconds($VerifyWaitSeconds)
    $fresh = $false
    $verifyAge = $null
    while ((Get-Date) -lt $deadline) {
        $probeNow = (Get-Date).ToUniversalTime()
        $verifyAge = Read-HeartbeatAge $hbPath $probeNow
        if ($null -ne $verifyAge -and $verifyAge -le $StaleMinutes) { $fresh = $true; break }
        Start-Sleep -Seconds 10
    }
    $pidAfter = Read-InstancePid $instPath
    $startPid = Read-LastStartPid $ledgerPath
    $pidChanged = $null
    if ($null -ne $pidBefore -and $null -ne $pidAfter) { $pidChanged = ($pidAfter -ne $pidBefore) }

    if ($fresh) {
        Write-WLog @{ ts = (Get-Date).ToUniversalTime().ToString("o"); event = "restart_verified"; heartbeat_age_min = $verifyAge; pid_before = $pidBefore; pid_after = $pidAfter; last_start_pid = $startPid; pid_changed = $pidChanged }
        Write-Host "WATCHDOG: restart VERIFIED (heartbeat fresh)"
    } else {
        Write-WLog @{ ts = (Get-Date).ToUniversalTime().ToString("o"); event = "restart_unverified"; heartbeat_age_min = $verifyAge; pid_before = $pidBefore; pid_after = $pidAfter; last_start_pid = $startPid; restart_exit = $rc; action = "ESCALATE_ALERT" }
        Write-Host ("WATCHDOG: restart NOT verified -- heartbeat still stale after " + $VerifyWaitSeconds + "s. Manual check needed (exit code was " + $rc + ").")
    }
    exit 0
}
catch {
    # Without this, ErrorActionPreference=Stop turns any unexpected throw into a
    # completely silent run -- indistinguishable from the task never firing.
    Write-WLog @{ ts = (Get-Date).ToUniversalTime().ToString("o"); event = "watchdog_error"; error = ([string]$_.Exception.Message); action = "ESCALATE_ALERT" }
    Write-Host ("WATCHDOG ERROR: " + $_.Exception.Message)
    exit 0
}
