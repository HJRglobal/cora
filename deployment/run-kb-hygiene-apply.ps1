# run-kb-hygiene-apply.ps1
# The Cora-STOPPED apply wrapper for a LARGE marked KB-hygiene sweep (the case the
# monthly task ESCALATES instead of auto-applying). One command: stop watchdog ->
# stop Cora -> back up cora_kb.db -> apply (--allow-large) -> optional reclaim ->
# restart Cora -> re-enable watchdog. Mirrors the D-086 apply runbook + restart-cora.
#
# ASCII-only (D-016). Absolute .venv python (D-005). Run from ELEVATED PowerShell
# (the service runs -RunLevel Highest; D-036).
#   .\deployment\run-kb-hygiene-apply.ps1            # apply large sweep (no VACUUM)
#   .\deployment\run-kb-hygiene-apply.ps1 -Reclaim   # also VACUUM/reclaim after
param(
    [switch]$Reclaim
)

$ErrorActionPreference = "Stop"
$Repo = "C:\Users\Harri\code\cora"
Set-Location $Repo
$Python = "$Repo\.venv\Scripts\python.exe"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Apply requires elevated PowerShell (Cora stop + exclusive DB access; D-036)."
    exit 1
}

Write-Host "=== Import smoke (never mutate into a broken import) ==="
& $Python -c "from src.cora.app import app"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Import smoke FAILED -- aborting; nothing stopped, nothing mutated."
    exit 1
}
Write-Host "Import smoke OK"

# 1) Disable the watchdog FIRST -- a multi-minute apply exceeds the heartbeat
#    staleness threshold and the watchdog would otherwise restart Cora mid-apply.
Write-Host "=== Disabling cora-watchdog for the apply window ==="
schtasks /End /TN "cora-watchdog" 2>$null | Out-Null
schtasks /Change /TN "cora-watchdog" /Disable 2>$null | Out-Null

try {
    # 2) Stop Cora (doctrine-5 kill filter).
    Write-Host "=== Stopping Cora ==="
    Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe'" |
        Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" -or $_.CommandLine -like "*cora.main*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep 3
    $leftover = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe'" |
        Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" -or $_.CommandLine -like "*cora.main*" })
    if ($leftover.Count -gt 0) {
        Write-Warning ("Still " + $leftover.Count + " bot process(es) alive -- aborting the apply (db still held).")
        $leftover | ForEach-Object { Write-Warning ("  PID " + $_.ProcessId + " " + $_.Name) }
        exit 1
    }

    # 3) Back up cora_kb.db (the purge is only reversible from a backup).
    $stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
    $bak = "$Repo\data\cora_kb.db.bak-$stamp"
    Write-Host "=== Backing up cora_kb.db -> $bak ==="
    Copy-Item "$Repo\data\cora_kb.db" $bak
    $wal = "$Repo\data\cora_kb.db-wal"
    if (Test-Path $wal) { Copy-Item $wal "$bak-wal" }

    # 4) Apply the large marked sweep (Cora is stopped; --allow-large bypasses the
    #    auto-apply escalation ceiling). --slack confirms the outcome to Harrison.
    Write-Host "=== Applying (kb_hygiene_sweep --marked --apply --allow-large) ==="
    & $Python "$Repo\scripts\kb_hygiene_sweep.py" --marked --apply --allow-large --slack
    $applyRc = $LASTEXITCODE
    Write-Host ("kb_hygiene_sweep exit: " + $applyRc)

    # 5) Optional reclaim (truncating VACUUM) while we still hold exclusive access.
    if ($Reclaim) {
        Write-Host "=== Reclaiming disk (reclaim_kb_space.py) ==="
        & $Python "$Repo\scripts\reclaim_kb_space.py"
    }
}
finally {
    # 6) Restart Cora (re-runs import smoke + verifies the single-instance shape),
    #    THEN re-enable the watchdog -- always, even if the apply above failed.
    Write-Host "=== Restarting Cora ==="
    & "$Repo\deployment\restart-cora.ps1"

    Write-Host "=== Re-enabling cora-watchdog ==="
    schtasks /Change /TN "cora-watchdog" /Enable 2>$null | Out-Null
}

Write-Host "=== KB-hygiene apply complete. Rollback if needed: restore data\cora_kb.db from the .bak, or --revert the manifest for files. ==="
