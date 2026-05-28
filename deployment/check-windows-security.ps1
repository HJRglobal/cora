# check-windows-security.ps1
#
# Read-only security posture check for the Cora host machine.
# Prints a checklist of what is and isn't configured correctly.
# Does NOT change any system settings - safe to run anytime.
#
# Run as Administrator for the most complete results
# (some checks - BitLocker, audit policy - need elevation):
#   powershell -ExecutionPolicy Bypass -File "C:\Users\Harri\code\cora\deployment\check-windows-security.ps1"

$ErrorActionPreference = "SilentlyContinue"

$PASS = "PASS"
$WARN = "WARN"
$FAIL = "FAIL"
$INFO = "INFO"

function Show-Result($status, $label, $detail = "") {
    $color = switch ($status) {
        "PASS" { "Green"  }
        "WARN" { "Yellow" }
        "FAIL" { "Red"    }
        default { "Cyan"  }
    }
    $line = "  [$status] $label"
    if ($detail) { $line += " - $detail" }
    Write-Host $line -ForegroundColor $color
}

Write-Host ""
Write-Host "=== Cora Host Security Posture Check ===" -ForegroundColor Cyan
Write-Host "  Run as:  $env:USERNAME on $(hostname)"
Write-Host "  Date:    $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host ""

# ------------------------------------------------------------------
# 1. Windows Defender / Antivirus
# ------------------------------------------------------------------
Write-Host "[ Windows Defender ]"
try {
    $mpPref = Get-MpPreference -ErrorAction Stop
    $mpStatus = Get-MpComputerStatus -ErrorAction Stop

    if ($mpStatus.RealTimeProtectionEnabled) {
        Show-Result $PASS "Real-time protection enabled"
    } else {
        Show-Result $FAIL "Real-time protection DISABLED" "Enable in Windows Security > Virus & threat protection"
    }

    if ($mpStatus.AntivirusEnabled) {
        Show-Result $PASS "Antivirus definitions active (updated $($mpStatus.AntivirusSignatureLastUpdated.ToString('yyyy-MM-dd')))"
    } else {
        Show-Result $WARN "Antivirus definitions may be stale"
    }
} catch {
    Show-Result $WARN "Could not query Windows Defender status"
}

Write-Host ""

# ------------------------------------------------------------------
# 2. Windows Update
# ------------------------------------------------------------------
Write-Host "[ Windows Update ]"
try {
    $wu = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update" -ErrorAction Stop
    $auOptions = $wu.AUOptions
    if ($auOptions -ge 3) {
        Show-Result $PASS "Automatic updates enabled (AUOptions=$auOptions)"
    } else {
        Show-Result $WARN "Automatic updates may be disabled (AUOptions=$auOptions)" "Enable in Settings > Windows Update > Advanced options"
    }
} catch {
    Show-Result $INFO "Could not read Windows Update registry key"
}

Write-Host ""

# ------------------------------------------------------------------
# 3. BitLocker drive encryption
# ------------------------------------------------------------------
Write-Host "[ BitLocker ]"
try {
    $blStatus = Get-BitLockerVolume -MountPoint "C:" -ErrorAction Stop
    if ($blStatus.ProtectionStatus -eq "On") {
        Show-Result $PASS "C: drive encrypted with BitLocker"
    } else {
        Show-Result $FAIL "C: drive is NOT encrypted" "Run: manage-bde -on C: -RecoveryPassword (requires Admin)"
    }
} catch {
    Show-Result $WARN "BitLocker status unavailable (may need elevation or Pro/Enterprise edition)"
    Show-Result $INFO "To check manually: manage-bde -status C:"
}

Write-Host ""

# ------------------------------------------------------------------
# 4. Windows Firewall
# ------------------------------------------------------------------
Write-Host "[ Windows Firewall ]"
$profiles = @("Domain", "Private", "Public")
foreach ($p in $profiles) {
    $fw = Get-NetFirewallProfile -Name $p -ErrorAction SilentlyContinue
    if ($fw -and $fw.Enabled) {
        Show-Result $PASS "$p profile firewall enabled"
    } else {
        Show-Result $FAIL "$p profile firewall DISABLED" "Enable in: netsh advfirewall set ${p}profile state on"
    }
}

Write-Host ""

# ------------------------------------------------------------------
# 5. Remote Desktop
# ------------------------------------------------------------------
Write-Host "[ Remote Desktop ]"
$rdp = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server" -ErrorAction SilentlyContinue
if ($rdp -and $rdp.fDenyTSConnections -eq 1) {
    Show-Result $PASS "Remote Desktop disabled (not needed for Cora)"
} else {
    Show-Result $WARN "Remote Desktop may be enabled" "Disable in Settings > System > Remote Desktop, or set fDenyTSConnections=1"
}

Write-Host ""

# ------------------------------------------------------------------
# 6. SMBv1 (legacy, vulnerable protocol)
# ------------------------------------------------------------------
Write-Host "[ SMBv1 ]"
$smb1 = Get-SmbServerConfiguration -ErrorAction SilentlyContinue
if ($smb1 -and -not $smb1.EnableSMB1Protocol) {
    Show-Result $PASS "SMBv1 disabled"
} else {
    Show-Result $WARN "SMBv1 may be enabled (EternalBlue attack surface)" "Disable: Set-SmbServerConfiguration -EnableSMB1Protocol `$false -Force"
}

Write-Host ""

# ------------------------------------------------------------------
# 7. Windows Audit Policy (failed logins logged to Security Event Log)
# ------------------------------------------------------------------
Write-Host "[ Audit Policy ]"
try {
    $audit = auditpol /get /category:"Logon/Logoff" /r 2>&1
    if ($audit -match "Failure") {
        Show-Result $PASS "Logon/Logoff failure auditing enabled"
    } else {
        Show-Result $WARN "Logon failure auditing not confirmed" "Enable: auditpol /set /subcategory:`"Logon`" /failure:enable"
    }
} catch {
    Show-Result $INFO "Could not read audit policy (may need elevation)"
}

Write-Host ""

# ------------------------------------------------------------------
# 8. .env file permissions
# ------------------------------------------------------------------
Write-Host "[ .env File Permissions ]"
$REPO_DIR = "C:\Users\Harri\code\cora"
$envPath  = "$REPO_DIR\.env"
if (Test-Path $envPath) {
    $acl   = Get-Acl $envPath -ErrorAction SilentlyContinue
    $users = $acl.Access | Where-Object { $_.IdentityReference -notmatch $env:USERNAME -and $_.IdentityReference -notmatch "SYSTEM" -and $_.IdentityReference -notmatch "Administrators" }
    if ($users) {
        Show-Result $WARN ".env has unexpected access entries" ($users | ForEach-Object { $_.IdentityReference } | Join-String -Separator ", ")
    } else {
        Show-Result $PASS ".env access looks appropriate"
    }
} else {
    Show-Result $WARN ".env not found at $envPath"
}

Write-Host ""

# ------------------------------------------------------------------
# 9. API key budget alerts (manual reminder)
# ------------------------------------------------------------------
Write-Host "[ API Budget Alerts (manual check) ]"
Show-Result $INFO "Anthropic - check at: https://console.anthropic.com > Settings > Billing"
Show-Result $INFO "OpenAI    - check at: https://platform.openai.com/usage/limits"
Show-Result $INFO "Set a monthly hard cap on both to limit blast radius if a key is stolen"

Write-Host ""

# ------------------------------------------------------------------
# 10. Scheduled tasks running as Cora
# ------------------------------------------------------------------
Write-Host "[ Cora Scheduled Tasks ]"
$tasks = Get-ScheduledTask | Where-Object { $_.TaskName -like "cowork-cora-*" }
foreach ($t in $tasks) {
    $info = Get-ScheduledTaskInfo -TaskName $t.TaskName -ErrorAction SilentlyContinue
    $lastResult = if ($info) { $info.LastTaskResult } else { "unknown" }
    if ($t.State -eq "Running" -or $t.State -eq "Ready") {
        Show-Result $PASS "$($t.TaskName) - $($t.State) (last result: $lastResult)"
    } else {
        Show-Result $WARN "$($t.TaskName) - $($t.State)"
    }
}

Write-Host ""
Write-Host "=== Check complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "FAIL items are security gaps that should be fixed."
Write-Host "WARN items are worth reviewing."
Write-Host "PASS items are correctly configured."
Write-Host ""
