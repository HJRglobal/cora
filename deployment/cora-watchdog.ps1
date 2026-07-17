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
# Run elevated (restart-cora.ps1 requires admin). Use -DryRun to see the decision only.
#   powershell -NoProfile -ExecutionPolicy Bypass -File deployment\cora-watchdog.ps1 -DryRun
param(
    [int]$StaleMinutes = 6,
    [int]$CooldownMinutes = 15,
    [int]$MaxRestartsPerHour = 3,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$Root = "C:\Users\Harri\code\cora"
Set-Location $Root

$hbPath    = Join-Path $Root "data\health\heartbeat.txt"
$statePath = Join-Path $Root "data\health\watchdog-state.json"
$logPath   = Join-Path $Root ("logs\watchdog-" + (Get-Date -Format "yyyy-MM-dd") + ".jsonl")
$nowUtc    = (Get-Date).ToUniversalTime()

function Write-WLog($obj) {
    ($obj | ConvertTo-Json -Compress) | Add-Content -Path $logPath -Encoding utf8
}

# 1. Heartbeat present + parseable?
if (-not (Test-Path $hbPath)) {
    Write-WLog @{ ts = $nowUtc.ToString("o"); event = "no_heartbeat_file"; action = "ESCALATE_ALERT" }
    Write-Host "WATCHDOG: heartbeat file missing -- manual check needed"
    exit 0
}
$hbRaw = (Get-Content $hbPath -Raw).Trim()
try {
    $hbTime = [datetimeoffset]::Parse($hbRaw).UtcDateTime
} catch {
    Write-WLog @{ ts = $nowUtc.ToString("o"); event = "unparseable_heartbeat" }
    Write-Host ("WATCHDOG: could not parse heartbeat value")
    exit 0
}
$ageMin = [math]::Round((($nowUtc - $hbTime).TotalMinutes), 1)

# 2. Healthy -> quiet exit.
if ($ageMin -le $StaleMinutes) {
    Write-Host ("WATCHDOG: healthy (heartbeat " + $ageMin + " min old)")
    exit 0
}

# 3. Stale. Load state (last restart + restart times in the last hour).
$lastRestart = $null
$recent = @()
if (Test-Path $statePath) {
    try {
        $state = Get-Content $statePath -Raw | ConvertFrom-Json
        $lastRestart = $state.last_restart
        $hourAgo = $nowUtc.AddHours(-1)
        if ($state.restarts) {
            $recent = @(@($state.restarts) | Where-Object { [datetimeoffset]::Parse($_).UtcDateTime -gt $hourAgo })
        }
    } catch { }
}

# Cooldown: do not restart again within CooldownMinutes of the last restart.
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

# 4. Decide to restart.
if ($DryRun) {
    Write-Host ("WATCHDOG [DRYRUN]: WOULD restart Cora (heartbeat " + $ageMin + " min stale)")
    Write-WLog @{ ts = $nowUtc.ToString("o"); event = "would_restart_dryrun"; age_min = $ageMin }
    exit 0
}

Write-WLog @{ ts = $nowUtc.ToString("o"); event = "restart_begin"; age_min = $ageMin }
Write-Host ("WATCHDOG: heartbeat " + $ageMin + " min stale -> restarting Cora")
& (Join-Path $Root "deployment\restart-cora.ps1")
$rc = $LASTEXITCODE

# 5. Record restart in state.
$recent = @($recent) + @($nowUtc.ToString("o"))
$newState = @{ last_restart = $nowUtc.ToString("o"); restarts = $recent }
($newState | ConvertTo-Json -Compress) | Set-Content -Path $statePath -Encoding utf8
Write-WLog @{ ts = $nowUtc.ToString("o"); event = "restart_done"; age_min = $ageMin; restart_exit = $rc; restarts_last_hour = $recent.Count }
Write-Host ("WATCHDOG: restart complete (exit " + $rc + ")")
exit 0
