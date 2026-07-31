# setup-cora-mcp-http-task.ps1
#
# Registers cowork-cora-mcp-http as a Windows Task Scheduler task: the local-only
# streamable-HTTP bridge (scripts\run_mcp_server_http.py) for Cora's MCP tool
# surface, so the Cowork desktop app's Add-connector UI (remote-URL-only, cannot
# spawn a stdio child) can reach the same read-only(+1) surface Claude Code
# already gets over stdio via .mcp.json.
#
# Kickoff of record: _notes\2026-07-30_fndr_cora-code-prompt-mcp-http-bridge.md
# (Harrison "locked as recommended", 2026-07-30). Extends D-092.
#
# This is a SEPARATE process from the always-on Cora bot service
# (cowork-cora-service) -- registering or restarting it needs NO Cora bot
# restart. The bind is HARD-CODED loopback-only inside run_mcp_server_http.py;
# this script does not and cannot change that.
#
# GO/NO-GO: run this ONLY after the manual smoke in the kickoff note passes
# (Cowork connector round-trips a tool call against a foreground run of the
# script). Do not register speculatively.
#
# Usage (run from any directory, as the current user - no elevation needed):
#   powershell -ExecutionPolicy Bypass -File "C:\Users\Harri\code\cora\deployment\setup-cora-mcp-http-task.ps1"
#
# To remove the task:
#   Unregister-ScheduledTask -TaskName "cowork-cora-mcp-http" -Confirm:$false

$ErrorActionPreference = "Stop"

$TASK_NAME = "cowork-cora-mcp-http"
$REPO_DIR  = "C:\Users\Harri\code\cora"
$ENV_FILE  = "$REPO_DIR\.env"

Write-Host ""
Write-Host "=== Cora MCP HTTP Bridge - Task Scheduler Setup ==="
Write-Host ""

# ------------------------------------------------------------------
# [1/5] Pre-flight: repo directory
# ------------------------------------------------------------------
Write-Host "[1/5] Checking repo directory..."
if (-not (Test-Path $REPO_DIR -PathType Container)) {
    Write-Host "  ERROR: Repo not found at $REPO_DIR" -ForegroundColor Red
    exit 1
}
Write-Host "  OK  $REPO_DIR"

# ------------------------------------------------------------------
# [2/5] Pre-flight: .env file + CORA_MCP_HTTP_TOKEN presence check (advisory)
# ------------------------------------------------------------------
Write-Host "[2/5] Checking .env file..."
if (-not (Test-Path $ENV_FILE -PathType Leaf)) {
    Write-Host "  ERROR: .env not found at $ENV_FILE" -ForegroundColor Red
    exit 1
}
Write-Host "  OK  $ENV_FILE"
$hasToken = Select-String -Path $ENV_FILE -Pattern "^CORA_MCP_HTTP_TOKEN=" -Quiet -ErrorAction SilentlyContinue
if (-not $hasToken) {
    Write-Host "  NOTE: CORA_MCP_HTTP_TOKEN is not set in .env -- the bridge will run" -ForegroundColor Yellow
    Write-Host "        with loopback-bind-only as its sole gate (v1-accepted posture," -ForegroundColor Yellow
    Write-Host "        2026-07-30 locked fork #2). Set it if the Cowork connector UI" -ForegroundColor Yellow
    Write-Host "        supports a custom header." -ForegroundColor Yellow
}

# TLS is entirely .env-driven (run_mcp_server_http.py calls load_dotenv itself
# at process start, same as every other env var) -- this script does NOT need
# to thread CORA_MCP_HTTP_CERT/KEY through the scheduled-task action. It DOES
# pre-flight-check them here so a misconfigured pair is caught at registration
# time, not at the next silent restart.
$certLine = Select-String -Path $ENV_FILE -Pattern "^CORA_MCP_HTTP_CERT=(.+)$" -ErrorAction SilentlyContinue
$keyLine  = Select-String -Path $ENV_FILE -Pattern "^CORA_MCP_HTTP_KEY=(.+)$" -ErrorAction SilentlyContinue
$UseTls = $false
if ($certLine -and $keyLine) {
    $certPath = $certLine.Matches[0].Groups[1].Value.Trim()
    $keyPath  = $keyLine.Matches[0].Groups[1].Value.Trim()
    if (-not [System.IO.Path]::IsPathRooted($certPath)) { $certPath = Join-Path $REPO_DIR $certPath }
    if (-not [System.IO.Path]::IsPathRooted($keyPath))  { $keyPath  = Join-Path $REPO_DIR $keyPath }
    if ((Test-Path $certPath -PathType Leaf) -and (Test-Path $keyPath -PathType Leaf)) {
        Write-Host "  OK  TLS configured -- bridge will serve https:// ($certPath)"
        $UseTls = $true
    } else {
        Write-Host "  ERROR: CORA_MCP_HTTP_CERT/CORA_MCP_HTTP_KEY are set in .env but the" -ForegroundColor Red
        Write-Host "         referenced file(s) do not exist. Run deployment\new-mcp-https-cert.ps1" -ForegroundColor Red
        Write-Host "         first, or fix the paths in .env." -ForegroundColor Red
        exit 1
    }
} elseif ($certLine -or $keyLine) {
    Write-Host "  ERROR: only one of CORA_MCP_HTTP_CERT / CORA_MCP_HTTP_KEY is set in .env" -ForegroundColor Red
    Write-Host "         -- the bridge requires BOTH or NEITHER. Fix .env before registering." -ForegroundColor Red
    exit 1
} else {
    Write-Host "  NOTE: no TLS cert configured -- bridge will serve plain http://. Run" -ForegroundColor Yellow
    Write-Host "        deployment\new-mcp-https-cert.ps1 first if the client UI requires https." -ForegroundColor Yellow
}

# ------------------------------------------------------------------
# [3/5] Locate the venv python (absolute path - D-005: never "uv run" in a
# scheduled-task action)
# ------------------------------------------------------------------
Write-Host "[3/5] Locating .venv\Scripts\python.exe..."
$PythonExe = Join-Path $REPO_DIR ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe -PathType Leaf)) {
    Write-Host "  ERROR: $PythonExe not found. Run 'uv sync' in $REPO_DIR first." -ForegroundColor Red
    exit 1
}
Write-Host "  OK  $PythonExe"

$ScriptPath = Join-Path $REPO_DIR "scripts\run_mcp_server_http.py"
if (-not (Test-Path $ScriptPath -PathType Leaf)) {
    Write-Host "  ERROR: $ScriptPath not found." -ForegroundColor Red
    exit 1
}

# ------------------------------------------------------------------
# [4/5] Build and register the task (idempotent - remove then re-add)
# ------------------------------------------------------------------
Write-Host "[4/5] Registering scheduled task '$TASK_NAME'..."

$existing = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Found existing task - stopping and removing before re-registration."
    try { Stop-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue } catch {}
    Start-Sleep -Seconds 2
    Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "scripts\run_mcp_server_http.py" `
    -WorkingDirectory $REPO_DIR

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TASK_NAME `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "  OK  Task registered."

# ------------------------------------------------------------------
# [5/5] Verify registration
# ------------------------------------------------------------------
Write-Host "[5/5] Verifying registration..."
$task = Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "  ERROR: Task was not found after registration." -ForegroundColor Red
    exit 1
}
$info = Get-ScheduledTaskInfo -TaskName $TASK_NAME
Write-Host "  OK  State      : $($task.State)"
Write-Host "  OK  Last result: $($info.LastTaskResult)"

Write-Host ""
Write-Host "=== Setup complete ==="
Write-Host ""
Write-Host "The task will start automatically at next logon."
Write-Host "To start it NOW without logging off/on:"
Write-Host "  Start-ScheduledTask -TaskName '$TASK_NAME'"
Write-Host ""
$scheme = if ($UseTls) { "https" } else { "http" }
Write-Host "Bridge listens on ${scheme}://127.0.0.1:8791/mcp by default (CORA_MCP_HTTP_PORT"
Write-Host "in .env overrides the port; the bind host is hard-coded and cannot be"
Write-Host "overridden). Point the Cowork connector at that URL."
Write-Host ""
