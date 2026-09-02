# restart-cora.ps1
# Generic, reusable CLEAN RESTART of the Cora service to activate whatever
# bot-loaded code is already committed at HEAD. It does NOT commit, push, or run
# the full suite (use a ship-*.ps1 for a code-change ship) -- it import-smokes,
# then kills + restarts with the doctrine-5 kill filter and verifies a single
# healthy 3-process instance.
#
# Run from an ELEVATED PowerShell (the service runs -RunLevel Highest; D-036).
#   .\deployment\restart-cora.ps1
param()

$ErrorActionPreference = "Stop"
Set-Location "C:\Users\Harri\code\cora"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Restart requires elevated PowerShell (service runs -RunLevel Highest; D-036)."
    exit 1
}

Write-Host "=== Import smoke (never restart into a broken import) ==="
& .venv\Scripts\python.exe -c "from src.cora.app import app"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Import smoke FAILED -- NOT restarting; the live instance is left untouched."
    exit 1
}
Write-Host "Import smoke OK"

Write-Host "=== Stopping Cora (doctrine-5 kill filter: \Scripts\cora.exe + cora.main) ==="
Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe'" |
    Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" -or $_.CommandLine -like "*cora.main*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# THE WINDOWLESS LAUNCHER MUST BE KILLED TOO (2026-09-02).
#
# Once cowork-cora-service is rewrapped, the task runs
#   pythonw.exe run_hidden.py -- python.exe -m cora.main
# The doctrine-5 filter above is Name='python.exe' OR 'cora.exe', so it does
# NOT match the pythonw launcher. Doctrine 5 also records that
# Stop-ScheduledTask alone does not reliably kill the task's process. Leaving
# the launcher alive is not cosmetic: it still HOLDS the task's running
# instance, and the service is MultipleInstances=IgnoreNew (verified live), so
# the Start-ScheduledTask below would be silently rejected and Cora would stay
# DOWN -- while the $leftover guard, whose whole job is to refuse to start on
# top of a live instance, reported zero because it cannot see pythonw either.
#
# Killing the launcher is also the cleanest possible stop: run_hidden puts the
# child in a kill-on-close job object, so the child dies with it.
$launcherFilter = { $_.CommandLine -like "*run_hidden.py*" -and ($_.CommandLine -like "*cora.main*" -or $_.CommandLine -like "*\Scripts\cora.exe*") }
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object $launcherFilter |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Start-Sleep 3
$leftover = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe'" |
    Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" -or $_.CommandLine -like "*cora.main*" })
$leftover += @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object $launcherFilter)
if ($leftover.Count -gt 0) {
    Write-Warning ("Still " + $leftover.Count + " bot process(es) alive after kill -- investigate before starting:")
    $leftover | ForEach-Object { Write-Warning ("  PID " + $_.ProcessId + " " + $_.Name) }
    exit 1
}

Write-Host "=== Starting Cora ==="
$restartBeganUtc = (Get-Date).ToUniversalTime()
Start-ScheduledTask -TaskName "cowork-cora-service"
Start-Sleep 90
Write-Host "Heartbeat:"
Get-Content "data\health\heartbeat.txt" -TotalCount 1
Write-Host "Verify the timestamp above is fresh (within ~60s of now, UTC)."

# PROCESS SHAPE (corrected 2026-08-19, cq-0d163e5f9c22). The old check counted
# python.exe whose command line contains "\Scripts\cora.exe" plus cora.exe
# processes, and declared "1 cora.exe + 2 python.exe" healthy. Neither exists on
# this host: the live service action is `.venv\Scripts\python.exe -m cora.main`
# (verified live 8/19 via schtasks), so the chain is venv-redirector python ->
# base python, 2 matches, and ZERO cora.exe. The check therefore printed
# "0 cora.exe + 0 python" and warned on EVERY restart -- a false alarm loud
# enough to mask a real stacked instance. Count what the KILL FILTER matches
# instead, so the counter and the kill can never disagree: 2 = the -m cora.main
# chain, 3 = a console-script (cora.exe) launch, both ONE instance.
$bot = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe'" |
    Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" -or $_.CommandLine -like "*cora.main*" })
Write-Host ("Bot processes matching the kill filter: " + $bot.Count + " (ONE healthy instance = 2 for '-m cora.main', 3 via the cora.exe launcher)")
$bot | ForEach-Object { Write-Host ("  PID " + $_.ProcessId + " " + $_.Name) }

# WINDOWLESS SERVICE (2026-09-02): once cowork-cora-service is rewrapped by
# deployment\rewrap-tasks-hidden.ps1, the task launches
# pythonw.exe run_hidden.py -- python.exe -m cora.main
# so a THIRD process sits above the chain. The kill filter above is unaffected
# and deliberately unchanged: it matches on Name='python.exe' OR 'cora.exe',
# and the launcher is pythonw.exe, so the launcher is neither killed nor
# counted -- while the real bot child IS (its command line still carries
# "cora.main"). Killing the child makes the launcher's wait() return and it
# exits with the child's code, which is what Task Scheduler reports.
# Surfaced here only so a reader who sees an extra pid in Task Manager knows
# what it is.
$launchers = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object $launcherFilter)
$launchers | ForEach-Object { Write-Host ("  (windowless launcher, not counted above) PID " + $_.ProcessId + " pythonw.exe") }

# Whether the service is WRAPPED is a property of the task DEFINITION, not of
# which processes happen to be alive. Reading it from process presence reported
# "not rewrapped yet" whenever a wrapped service had failed to come up -- i.e.
# exactly when someone is debugging -- which is the same false-report class the
# process-shape comment above was rewritten to eliminate.
$svcAction = (Get-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue).Actions[0]
if ($null -eq $svcAction) {
    Write-Host "  (could not read the cowork-cora-service definition)"
} elseif ($svcAction.Execute.ToLower().EndsWith("pythonw.exe") -and $svcAction.Arguments -like "*run_hidden.py*") {
    if ($launchers.Count -eq 0) {
        Write-Host "  service action IS windowless, but NO launcher process is alive -- the service did not come up."
    }
} else {
    Write-Host "  (service action is NOT wrapped -- it still runs as a console action and flashes a window;"
    Write-Host "   wrap it with: .\deployment\rewrap-tasks-hidden.ps1 -Apply -Only cowork-cora-service)"
}
if ($bot.Count -eq 0) {
    Write-Warning "NO bot process is running -- the service did not come up. Check today's log and the task's Last Result."
} elseif ($bot.Count -gt 3) {
    Write-Warning ("STACKED INSTANCES LIKELY (" + $bot.Count + " matching processes) -- confirm via logs\cora-instances.jsonl (one start row) before leaving it.")
}

# Restart verification (cq-7915a8647cff): a fresh start row in the instance ledger
# is the only direct proof a NEW process came up. Absent ledger = a pre-2026-08-19
# build is still live, which is itself worth saying out loud.
$ledger = "logs\cora-instances.jsonl"
if (Test-Path $ledger) {
    $lastStart = $null
    foreach ($line in @(Get-Content $ledger -Tail 40)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try { $row = $line | ConvertFrom-Json } catch { continue }
        if ($row.event -eq "start") { $lastStart = $row }
    }
    if ($null -eq $lastStart) {
        Write-Warning "Instance ledger has no start row -- cannot verify a fresh instance."
    } else {
        $startUtc = [datetimeoffset]::Parse($lastStart.ts).UtcDateTime
        if ($startUtc -ge $restartBeganUtc) {
            Write-Host ("Restart VERIFIED: fresh instance pid " + $lastStart.pid + " started " + $lastStart.ts)
        } else {
            Write-Warning ("NO fresh start row since this restart began (newest is pid " + $lastStart.pid + " at " + $lastStart.ts + ") -- the old instance may still be the live one.")
        }
    }
} else {
    Write-Host "No instance ledger yet (logs\cora-instances.jsonl) -- expected until the first restart onto the 2026-08-19 build."
}
Write-Host "=== Restart complete -- activated whatever bot-loaded code is at HEAD. ==="
