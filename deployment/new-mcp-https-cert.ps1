# new-mcp-https-cert.ps1
#
# Generates a self-signed, LOOPBACK-ONLY TLS leaf certificate for Cora's MCP
# local-HTTP bridge (scripts\run_mcp_server_http.py), so a client UI that
# refuses a plain http:// URL can reach the bridge over
# https://127.0.0.1:<port>/mcp instead.
#
# Kickoff of record: _notes\2026-07-30_fndr_cora-code-prompt-mcp-http-bridge.md
# (TLS fallback follow-up, same day -- the GO/NO-GO smoke found Cowork's
# Add-connector UI rejects a plain http:// URL on scheme alone: "URL must
# start with 'https'"; localhost itself was NOT blocked). This script does
# NOT change the bridge's trust boundary -- TLS here satisfies a client's
# URL-scheme requirement, not a new network exposure. The bind stays the
# hard-coded 127.0.0.1 literal in run_mcp_server_http.py (unchanged by this
# script); the Host-header allowlist and the optional bearer token are
# unaffected either.
#
# WHY A LOOPBACK-SAN LEAF IN THE USER'S TRUSTED ROOT STORE IS SAFE:
# Installing a self-signed certificate into Cert:\CurrentUser\Root normally
# raises alarm because a trusted ROOT can vouch for (sign) certificates for
# ANY hostname -- a malicious root CA in that store could issue a trusted
# cert for, say, a bank's domain, and a browser would accept it. This
# certificate is NOT a CA: it is generated as a LEAF (Basic Constraints
# CA=false, path length 0 -- it has no signing authority over any other
# certificate) and its Subject Alternative Name (SAN) list is hard-coded to
# EXACTLY 127.0.0.1 / localhost / ::1. TLS hostname validation checks the SAN
# of the presented leaf certificate against the hostname the client actually
# requested; since this leaf's SAN can only ever match a loopback hostname,
# it cannot be presented as (or accepted for) any DIFFERENT real-world
# hostname/domain, whether or not it sits in the Root store. Trusting it in
# Root only removes the "untrusted self-signed certificate" warning for THIS
# EXACT loopback identity -- it grants no signing authority over anything
# else and cannot be used to impersonate a different hostname.
#
# RESIDUAL RISK (inherent to putting ANY self-signed leaf in Root, not unique
# to this script): if key.pem were ever exfiltrated -- despite the ACL
# lockdown in steps 4/6 below -- it could be used to stand up a rogue TLS
# listener presenting THIS SAME leaf for 127.0.0.1/localhost to some OTHER
# local process on this account that also chains to Cert:\CurrentUser\Root
# and doesn't pin this specific certificate. That is a property of the
# Root-trust mechanism itself (this leaf is trusted as its own anchor for its
# SAN identities, account-wide, not scoped to just this one bridge process),
# not something unique to how this script builds the cert. Keep key.pem's
# ACL restriction intact; do not copy the key elsewhere.
#
# Requires openssl.exe (used ONCE, to convert the exported PFX into the PEM
# cert+key pair Python's ssl module / uvicorn need -- Windows PowerShell
# 5.1's .NET Framework has no built-in PKCS8 PEM private-key export). Looks
# on PATH first, then common Git for Windows locations (Git for Windows
# ships openssl.exe and is already required for this repo's git workflow).
#
# Usage (run from any directory, current user - no elevation needed):
#   powershell -ExecutionPolicy Bypass -File "C:\Users\Harri\code\cora\deployment\new-mcp-https-cert.ps1"
#
# Output: data\state\mcp-tls\cert.pem + data\state\mcp-tls\key.pem (gitignored;
# see .gitignore). Then add to .env:
#   CORA_MCP_HTTP_CERT=data\state\mcp-tls\cert.pem
#   CORA_MCP_HTTP_KEY=data\state\mcp-tls\key.pem
#
# Idempotent: re-running removes any prior cert with the same Subject from
# both Cert:\CurrentUser\My and Cert:\CurrentUser\Root before generating a
# fresh one, and overwrites the PEM output files.

$ErrorActionPreference = "Stop"

$REPO_DIR = "C:\Users\Harri\code\cora"
$OUT_DIR  = Join-Path $REPO_DIR "data\state\mcp-tls"
$CERT_PEM = Join-Path $OUT_DIR "cert.pem"
$KEY_PEM  = Join-Path $OUT_DIR "key.pem"
$SUBJECT  = "CN=cora-mcp-bridge-loopback"
$CurrentUserTrustee = "$($env:USERDOMAIN)\$($env:USERNAME)"

Write-Host ""
Write-Host "=== Cora MCP HTTP Bridge - Self-Signed Loopback TLS Cert ==="
Write-Host ""

# ------------------------------------------------------------------
# [1/7] Locate openssl.exe (one-time PFX -> PEM key conversion)
# ------------------------------------------------------------------
Write-Host "[1/7] Locating openssl.exe..."
$OpenSSL = $null
$onPath = Get-Command openssl -ErrorAction SilentlyContinue
if ($onPath) { $OpenSSL = $onPath.Source }
if (-not $OpenSSL) {
    foreach ($candidate in @(
        "C:\Program Files\Git\usr\bin\openssl.exe",
        "C:\Program Files\Git\mingw64\bin\openssl.exe",
        "C:\Program Files (x86)\Git\usr\bin\openssl.exe"
    )) {
        if (Test-Path $candidate -PathType Leaf) { $OpenSSL = $candidate; break }
    }
}
if (-not $OpenSSL) {
    Write-Host "  ERROR: openssl.exe not found (checked PATH + common Git for Windows" -ForegroundColor Red
    Write-Host "         locations). Install Git for Windows (ships openssl.exe) and re-run." -ForegroundColor Red
    exit 1
}
Write-Host "  OK  $OpenSSL"

# ------------------------------------------------------------------
# [2/7] Remove any prior cert with the same Subject (idempotent re-run)
# ------------------------------------------------------------------
Write-Host "[2/7] Clearing any prior cert with Subject '$SUBJECT'..."
Get-ChildItem Cert:\CurrentUser\My -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq $SUBJECT } |
    ForEach-Object {
        Write-Host "  Removing prior cert $($_.Thumbprint) from Cert:\CurrentUser\My"
        Remove-Item -Path $_.PSPath -DeleteKey -Force
    }
# Removing from Root via Remove-Item can throw "operation is on user root
# store and UI is not allowed" in a non-interactive session (Windows normally
# shows a UI confirmation to delete a trusted root; some automation contexts
# can't display it -- reproduced empirically running this script headlessly).
# `certutil -delstore` deletes non-interactively without hitting that check,
# so try it as a fallback. Even if BOTH fail, this is not fatal to re-running
# the script: a leftover old entry has no private key left anywhere (it was
# just deleted from My above), so it is inert clutter, never a live/usable
# credential, and its SAN is the same fixed loopback-only set as the new cert
# this script is about to install.
Get-ChildItem Cert:\CurrentUser\Root -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq $SUBJECT } |
    ForEach-Object {
        $thumb = $_.Thumbprint
        try {
            Remove-Item -Path $_.PSPath -Force -ErrorAction Stop
            Write-Host "  Removed prior cert $thumb from Cert:\CurrentUser\Root"
        } catch {
            certutil -delstore -user Root $thumb | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  Removed prior cert $thumb from Cert:\CurrentUser\Root (via certutil)"
            } else {
                Write-Host "  NOTE: could not remove prior cert $thumb from" -ForegroundColor Yellow
                Write-Host "        Cert:\CurrentUser\Root (Remove-Item and certutil both failed)." -ForegroundColor Yellow
                Write-Host "        Its private key is already gone (harmless, inert entry)." -ForegroundColor Yellow
                Write-Host "        Remove manually via certmgr.msc if you want it gone." -ForegroundColor Yellow
            }
        }
    }

# ------------------------------------------------------------------
# [3/7] Generate the leaf certificate. CA=false + path length 0 (Basic
# Constraints), serverAuth EKU, and a SAN built EXPLICITLY as IP-address
# entries for 127.0.0.1/::1 (New-SelfSignedCertificate's -DnsName parameter
# emits IP-looking strings as DNS-type SAN entries, not IP-address-type --
# verified empirically; a strict TLS client validating an IP-literal
# connection checks IP-address SAN entries, so a DNS-type "127.0.0.1" SAN
# would not satisfy it. The manual -TextExtension below is the only way to
# get real IP Address SAN entries out of this cmdlet.)
# ------------------------------------------------------------------
Write-Host "[3/7] Generating self-signed loopback leaf certificate..."
$cert = New-SelfSignedCertificate `
    -Subject $SUBJECT `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -KeyExportPolicy Exportable `
    -KeyAlgorithm RSA `
    -KeyLength 2048 `
    -KeyUsage DigitalSignature, KeyEncipherment `
    -TextExtension @(
        "2.5.29.19={critical}{text}ca=0&pathlength=0",
        "2.5.29.37={text}1.3.6.1.5.5.7.3.1",
        "2.5.29.17={text}DNS=localhost&IPAddress=127.0.0.1&IPAddress=::1"
    ) `
    -NotAfter (Get-Date).AddYears(2)
Write-Host "  OK  Thumbprint $($cert.Thumbprint)"
$sanEntry = $cert.Extensions | Where-Object { $_.Oid.FriendlyName -eq "Subject Alternative Name" }
Write-Host "  SAN: $($sanEntry.Format($false))"

function Assert-IcaclsOk($LastCommandDescription) {
    # icacls is an external .exe -- $ErrorActionPreference = "Stop" governs
    # PowerShell's own error stream only, NOT a nonzero exit code from a
    # native tool. Check $LASTEXITCODE explicitly after every icacls call so
    # a silent ACL failure can never be reported as "OK" (D-051-light finding).
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: icacls failed ($LastCommandDescription), exit code $LASTEXITCODE." -ForegroundColor Red
        exit 1
    }
}

# ------------------------------------------------------------------
# [4/7] Lock down the OUTPUT DIRECTORY's ACL BEFORE anything is written into
# it (D-051-light finding: locking down key.pem only AFTER openssl writes it
# leaves a TOCTOU window where the unencrypted key briefly carries whatever
# ACL data\state\ inherited). New files created under a restricted directory
# inherit its ACEs at creation time -- so cert.pem, key.pem, and the temp PFX
# are all born with the restricted ACL, never a wider one.
# ------------------------------------------------------------------
Write-Host "[4/7] Creating + locking down $OUT_DIR before writing any key material..."
New-Item -ItemType Directory -Force -Path $OUT_DIR | Out-Null

# Re-run idempotency: a PRIOR run's step 6 (below) locks key.pem to
# read-only for the current user -- which then blocks THIS run's openssl
# from overwriting it (verified empirically: a plain re-run failed with
# "Permission denied" on key.pem). Relax any pre-existing output files back
# to full control for the current user before proceeding; the fresh,
# rotated key gets the same read-only lockdown re-applied at the end
# regardless of this file's starting ACL.
foreach ($existing in @($CERT_PEM, $KEY_PEM)) {
    if (Test-Path $existing -PathType Leaf) {
        icacls $existing /grant:r "${CurrentUserTrustee}:(F)" | Out-Null
        Assert-IcaclsOk "relax pre-existing $existing for overwrite"
    }
}

icacls $OUT_DIR /inheritance:r | Out-Null
Assert-IcaclsOk "break inheritance on $OUT_DIR"
icacls $OUT_DIR /grant:r "${CurrentUserTrustee}:(OI)(CI)F" | Out-Null
Assert-IcaclsOk "grant $CurrentUserTrustee on $OUT_DIR"
icacls $OUT_DIR /grant:r "SYSTEM:(OI)(CI)F" | Out-Null
Assert-IcaclsOk "grant SYSTEM on $OUT_DIR"
Write-Host "  OK  $OUT_DIR restricted to $CurrentUserTrustee + SYSTEM (new files inherit this)."

# ------------------------------------------------------------------
# [5/7] Export cert+key to a temp PFX (now inheriting the restricted ACL),
# convert to PEM via openssl, then delete the PFX (fewer copies of the
# private key at rest)
# ------------------------------------------------------------------
Write-Host "[5/7] Exporting + converting to PEM ($OUT_DIR)..."
$tmpPfx = Join-Path $OUT_DIR "_tmp.pfx"
# Throwaway password: held only in-process, to protect the PFX in transit to
# openssl. The PEM key openssl writes is unencrypted (the bridge's
# CORA_MCP_HTTP_KEY does not carry a password -- uvicorn's ssl_keyfile
# parameter isn't wired to one); the directory ACL above (and the tighter
# per-file ACL below) is the real protection for the key at rest, not this
# password. A GUID passed via `-passin pass:` on the command line is a
# standard, accepted exposure for a local one-shot admin script (briefly
# visible to a concurrent local process enumerator; not persisted anywhere).
$pwPlain = [System.Guid]::NewGuid().ToString("N")
$pwSecure = ConvertTo-SecureString -String $pwPlain -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath $tmpPfx -Password $pwSecure | Out-Null

& $OpenSSL pkcs12 -in $tmpPfx -clcerts -nokeys -out $CERT_PEM -passin "pass:$pwPlain" 2>$null
& $OpenSSL pkcs12 -in $tmpPfx -nocerts -nodes -out $KEY_PEM -passin "pass:$pwPlain" 2>$null
Remove-Item $tmpPfx -Force

if (-not (Test-Path $CERT_PEM -PathType Leaf) -or -not (Test-Path $KEY_PEM -PathType Leaf)) {
    Write-Host "  ERROR: openssl conversion did not produce both PEM files." -ForegroundColor Red
    exit 1
}
Write-Host "  OK  $CERT_PEM"
Write-Host "  OK  $KEY_PEM"

# ------------------------------------------------------------------
# [6/7] Further tighten key.pem specifically to READ-ONLY for the current
# user (the directory grant above is Full Control, needed so this script
# itself can create/overwrite files on a re-run; the key file itself only
# ever needs to be read).
# ------------------------------------------------------------------
Write-Host "[6/7] Restricting key.pem to read-only for $CurrentUserTrustee..."
icacls $KEY_PEM /inheritance:r | Out-Null
Assert-IcaclsOk "break inheritance on $KEY_PEM"
icacls $KEY_PEM /grant:r "${CurrentUserTrustee}:(R)" | Out-Null
Assert-IcaclsOk "grant $CurrentUserTrustee read-only on $KEY_PEM"
icacls $KEY_PEM /grant:r "SYSTEM:(F)" | Out-Null
Assert-IcaclsOk "grant SYSTEM on $KEY_PEM"
Write-Host "  OK  ACL restricted."

# ------------------------------------------------------------------
# [7/7] Trust the leaf in CurrentUser\Root (removes the "untrusted
# self-signed certificate" warning for THIS loopback identity ONLY -- see
# the header comment above for why this grants no broader trust)
# ------------------------------------------------------------------
Write-Host "[7/7] Installing the cert into Cert:\CurrentUser\Root (loopback trust only)..."
$rootStore = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", "CurrentUser")
$rootStore.Open("ReadWrite")
$rootStore.Add($cert)
$rootStore.Close()
Write-Host "  OK  Trusted for 127.0.0.1 / localhost / ::1 only."

Write-Host ""
Write-Host "=== Done ==="
Write-Host ""
Write-Host "Add to .env:"
Write-Host "  CORA_MCP_HTTP_CERT=data\state\mcp-tls\cert.pem"
Write-Host "  CORA_MCP_HTTP_KEY=data\state\mcp-tls\key.pem"
Write-Host ""
Write-Host "Then run the bridge and point the client at https://127.0.0.1:8791/mcp"
Write-Host "(or your configured CORA_MCP_HTTP_PORT)."
Write-Host ""
