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
Start-Sleep 3
$leftover = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe'" |
    Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" -or $_.CommandLine -like "*cora.main*" })
if ($leftover.Count -gt 0) {
    Write-Warning ("Still " + $leftover.Count + " bot process(es) alive after kill -- investigate before starting:")
    $leftover | ForEach-Object { Write-Warning ("  PID " + $_.ProcessId + " " + $_.Name) }
    exit 1
}

Write-Host "=== Starting Cora ==="
Start-ScheduledTask -TaskName "cowork-cora-service"
Start-Sleep 90
Write-Host "Heartbeat:"
Get-Content "data\health\heartbeat.txt"
Write-Host "Verify the timestamp above is fresh (within ~60s of now, UTC)."

# Healthy single instance = cora.exe launcher -> venv python redirector ->
# base python = 1 cora.exe + 2 python.exe matches (doctrine 5).
$pys = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" })
$launchers = @(Get-CimInstance Win32_Process -Filter "Name='cora.exe'")
Write-Host ("Bot processes: " + $launchers.Count + " cora.exe + " + $pys.Count + " python (healthy single instance = 1 + 2)")
if ($pys.Count -ne 2 -or $launchers.Count -ne 1) {
    Write-Warning "PROCESS SHAPE UNEXPECTED (stacked or partial instance?) -- check the log for one 'Cora starting up' + a single monotonic heartbeat sequence."
}
Write-Host "=== Restart complete -- activated whatever bot-loaded code is at HEAD. ==="
