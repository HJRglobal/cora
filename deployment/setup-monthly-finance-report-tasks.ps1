# Setup Windows Scheduled Tasks: the two monthly READ-ONLY report lanes from
# session 7 (C13 cq-015b3bc779e9, C14 cq-118f8bbf842e).
#
#   Cora - Expected Invoice Check   day 4, 09:38 AZ  -> #hjrg-finance
#   Cora - Klaviyo Billing Audit    day 4, 09:53 AZ  -> #founder-operations
#
# BOTH ARE READ-ONLY. The invoice check reads a YAML expectation list and the
# attachment filer's own content ledger; the Klaviyo audit reads segments through
# a client that has exactly one GET request primitive (Ops Dept OS v1 makes both
# contact-list deletion AND billing cleanup unauthorized, so the client is
# greppably write-free and a test enforces it). Neither creates, changes,
# suppresses or deletes anything.
#
# DAY 4, not day 1: the invoice check asks "is last month's invoice FILED?", and
# the attachment filer needs a few days to see and file an invoice that vendors
# send on the 1st or 2nd. Asking on the 1st would report a false MISSING every
# month, which is exactly how a monthly report trains its reader to ignore it.
#
# 09:38 / 09:53 are OUTSIDE the 03:00-09:00 window the weekly health metric
# watches, and collide with no existing cora task clock time (checked against the
# live registry 2026-08-25).
#
# schtasks /Create /SC MONTHLY is used rather than New-ScheduledTaskTrigger --
# PowerShell has no monthly trigger cmdlet (same reason as the Log Compaction
# task). It registers non-elevated: user-level report jobs need no admin.
#
# THE KLAVIYO AUDIT SHIPS DARK. There is no KLAVIYO_API_KEY in .env, so its
# profile figures will report UNAVAILABLE (never zero) and only the seat section
# -- which comes from the org-roles klaviyo_seat flag, not the API -- will carry
# content. Registering it now means it starts reporting the moment the credential
# lands. NOTE for the first credentialed run: the pinned API `revision` header has
# never been exercised against the live API; an unknown revision surfaces as an
# HTTP error naming the revision in the log.
#
# ASCII-only (D-016): PowerShell 5.1 reads UTF-8 as Windows-1252.
#
# Run from elevated PowerShell (or not -- these do not need admin):
#     cd C:\Users\Harri\code\cora
#     .\deployment\setup-monthly-finance-report-tasks.ps1
#
# INSPECT FIRST. Both are dry-run by default and post nothing without --post:
#     .venv\Scripts\python.exe scripts\run_expected_invoice_check.py
#     .venv\Scripts\python.exe scripts\run_klaviyo_billing_audit.py
#
# To remove:
#     schtasks /Delete /TN "Cora - Expected Invoice Check" /F
#     schtasks /Delete /TN "Cora - Klaviyo Billing Audit" /F

$ErrorActionPreference = "Stop"

$RepoRoot  = "C:\Users\Harri\code\cora"
$PythonExe = "C:\Users\Harri\code\cora\.venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at $PythonExe. Check the venv."
    exit 1
}

$jobs = @(
    @{
        Name   = "Cora - Expected Invoice Check"
        Script = "scripts\run_expected_invoice_check.py"
        Time   = "09:38"
        Desc   = "C13: monthly custody check -- are the expected recurring vendor invoices FILED for the last closed month? Reads the filer ledger, posts to #hjrg-finance. Read-only."
    },
    @{
        Name   = "Cora - Klaviyo Billing Audit"
        Script = "scripts\run_klaviyo_billing_audit.py"
        Time   = "09:53"
        Desc   = "C14: monthly read-only Klaviyo billing and seat audit -- derived charge basis, never-engaged candidates, seat roster from canon. Posts to #founder-operations. Zero Klaviyo writes."
    }
)

foreach ($job in $jobs) {
    $scriptFull = Join-Path $RepoRoot $job.Script
    if (-not (Test-Path $scriptFull)) {
        Write-Error "Script not found at $scriptFull."
        exit 1
    }

    Write-Host "Setting up scheduled task: $($job.Name)" -ForegroundColor Cyan

    # --post is REQUIRED in the task action: both scripts are dry-run by default,
    # so a task registered without it would run monthly and deliver nothing --
    # the silent-no-op class this repo has shipped more than once.
    $cmd = "cmd /c cd /d `"$RepoRoot`" && `"$PythonExe`" `"$scriptFull`" --post"

    # FIRST-RUN ABORT, observed live 2026-08-25: this used to be
    #     schtasks /Delete /TN $job.Name /F 2>$null | Out-Null
    # A first-ever run has no task to delete, so schtasks writes "ERROR: The
    # system cannot find the file specified." to stderr -- and in PS 5.1 ANY
    # native-command stderr write becomes a TERMINATING NativeCommandError while
    # $ErrorActionPreference = "Stop". `2>$null` does not prevent that. The script
    # died on the first job and registered NEITHER task, while printing enough
    # output to look like it had started working.
    #
    # The pre-delete was redundant to begin with: /Create /F below overwrites an
    # existing task by definition. Kept only as a cmdlet-based guard, because
    # cmdlets raise catchable errors instead of writing to a native stderr stream.
    if (Get-ScheduledTask -TaskName $job.Name -ErrorAction SilentlyContinue) {
        try {
            Unregister-ScheduledTask -TaskName $job.Name -Confirm:$false | Out-Null
        } catch {
            Write-Host "  (existing task not removed, /F will overwrite: $($_.Exception.Message))" -ForegroundColor Yellow
        }
    }

    schtasks /Create `
        /TN $job.Name `
        /TR $cmd `
        /SC MONTHLY `
        /D 4 `
        /ST $job.Time `
        /RL LIMITED `
        /F | Out-Null

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to register $($job.Name) (exit $LASTEXITCODE)."
        exit 1
    }

    # schtasks has no description flag; set it through the ScheduledTask object.
    try {
        Set-ScheduledTask -TaskName $job.Name -Description $job.Desc | Out-Null
    } catch {
        Write-Host "  (description not set: $($_.Exception.Message))" -ForegroundColor Yellow
    }

    Write-Host "  Registered: $($job.Name)  (day 4 monthly at $($job.Time) AZ)" -ForegroundColor Green
}

Write-Host ""
Write-Host "Verify with:" -ForegroundColor Cyan
Write-Host "  schtasks /query /tn 'Cora - Expected Invoice Check' /v /fo list | Select-String 'Status|Next Run|Task To Run'"
Write-Host "  schtasks /query /tn 'Cora - Klaviyo Billing Audit' /v /fo list | Select-String 'Status|Next Run|Task To Run'"
