# Cora Operations Runbook

## Operating Cora

**Start:**
```powershell
Start-ScheduledTask -TaskName "cowork-cora-service"
```

**Stop (graceful):**
```powershell
Stop-ScheduledTask -TaskName "cowork-cora-service"
```

**Stop (hard kill — use when Stop-ScheduledTask leaves zombie processes):**
```powershell
Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "cora.exe" -or ($_.Name -eq "python.exe" -and $_.CommandLine -like "*cora*") } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
```

> **WARNING — `schtasks /End` does NOT kill the Python process.** It only signals the task wrapper. Always use `Stop-ScheduledTask` + `Get-Process python* | Stop-Process -Force` as shown above. After stopping, confirm with `Get-Process python*` — if any python processes remain, kill them explicitly before restarting.

> **WARNING — Task Scheduler `State: Ready` is unreliable as a liveness check.** A task can show "Ready" while no Python process is actually running (e.g. after a crash that exhausted the restart retries). The only reliable liveness confirmation is a `heartbeat alive` line in the current log within the last 2 minutes. If the task shows Ready but there is no recent heartbeat, Cora is down — restart the task manually.

**Verify she's alive (single instance):**
```powershell
Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "cora.exe" } | Select-Object ProcessId, CreationDate
```
Or check the log for a single `heartbeat alive` sequence. Multiple interleaved uptime values = multiple instances running (use hard kill above).

**Invite to a new channel:** `/invite @Cora` in the Slack channel (manual Slack action — no code change needed).

**Update channel routing:** Edit `design/channel-routing.yaml`, commit, then restart the task:
```powershell
Stop-ScheduledTask -TaskName "cowork-cora-service"
Start-ScheduledTask -TaskName "cowork-cora-service"
```

**Update a system prompt:** Edit `design/system-prompts/{entity}.md`, commit, then restart the task. Prompts have no TTL cache — a restart is required for changes to load.

---

## Logs

**Location:** `C:\Users\Harri\code\cora\logs\cora-YYYY-MM-DD.log`

**Format:** ISO timestamps, thread name in brackets, module name, message.

**Key patterns to grep:**

| Pattern | Meaning |
|---|---|
| `Cora Socket Mode connecting` | Bot startup |
| `heartbeat alive` | Liveness pulse (every 60s) |
| `app_mention routed` | Incoming @-mention received |
| `responded entity=` | Reply posted to Slack |
| `rate_limited` | Request hit user/channel cap |
| `ClaudeClientError` | Anthropic API failure |
| `WebSocket CLOSE received` / `WebSocket error` | Socket Mode disconnect |
| `Restarting in` | In-process auto-restart firing |
| `SocketModeHandler raised` | Unexpected exception with stack trace |

---

## Failure Modes and Recovery

| Failure | How handled |
|---|---|
| Transient WebSocket disconnect | In-process restart loop catches it — back up within seconds |
| Uncaught Python exception | In-process loop catches; if loop itself fails, non-zero exit triggers Task Scheduler RestartOnFailure (within 1 min) |
| Process crash (OOM, segfault) | Non-zero exit -> Task Scheduler restart within 1 min |
| Reboot / logon | AtLogOn trigger fires automatically |
| **Manual kill via Stop-Process or Task Manager** | **NOT auto-restarted.** Windows Task Scheduler treats manual termination (result -1 / 0xFFFFFFFF) as user-initiated stop, not a failure. To bring Cora back after a manual kill: `Start-ScheduledTask -TaskName "cowork-cora-service"`. To permanently disable: run `deployment\remove-windows-task.ps1`. |

---

## Rotating Tokens

All token rotations require a task restart for new values to load from `.env`. After updating `.env`, run:

```powershell
Stop-ScheduledTask -TaskName "cowork-cora-service"
# Wait for python processes to fully exit:
Get-Process python* -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3
Start-ScheduledTask -TaskName "cowork-cora-service"
```

Each token can be rotated independently. **You don't need to rotate all four at once** unless you suspect compromise of multiple. For full disaster-recovery scenarios (new machine), see `deployment/bootstrap-new-machine.md`.

### Token 1: Anthropic API key (`ANTHROPIC_API_KEY`)

**Where:** https://console.anthropic.com

1. Sign in.
2. Left sidebar -> **API Keys**.
3. Find the `cora-phase-1` (or whatever you named it) key. Three-dot menu next to it -> **Delete** or **Revoke**. Confirm.
4. **Create Key** -> name it `cora-production` (or reuse the old name) -> **Create**.
5. **Copy the key immediately** (starts with `sk-ant-***`). Anthropic shows it only once.
6. Open `.env` in Notepad: `notepad C:\Users\Harri\code\cora\.env`
7. Replace the value after `ANTHROPIC_API_KEY=` with the new key. Save.
8. Restart task (see top of section).

**Verify:** check `Get-ScheduledTaskInfo -TaskName "cowork-cora-service"` shows recent run, and `@Cora ping` in Slack returns a reply.

### Token 2: Slack Signing Secret (`SLACK_SIGNING_SECRET`)

Note: under Socket Mode, signing secret is technically unused. We rotate it anyway for future HTTPS migration.

**Where:** https://api.slack.com/apps -> click **Cora** app

1. Left sidebar -> **Basic Information** -> scroll to **App Credentials**.
2. Next to **Signing Secret**, click **Regenerate** (or **Show** if you trust the existing one and just need to copy it).
3. Confirm the warning if prompted. Copy the new value.
4. Update `SLACK_SIGNING_SECRET=` in `.env`. Save.
5. Restart task.

### Token 3: Slack App-Level Token (`SLACK_APP_TOKEN`, the `xapp-` one)

**Where:** https://api.slack.com/apps -> click **Cora** app

1. Left sidebar -> **Basic Information** -> scroll to **App-Level Tokens**.
2. Click the existing `cora-socket` token entry -> **Delete** -> confirm.
3. Back at **App-Level Tokens** -> click **Generate Token and Scopes**.
4. Name: `cora-socket` -> click **Add Scope** -> select `connections:write` -> click **Generate**.
5. Copy the new `xapp-1-...` value.
6. Update `SLACK_APP_TOKEN=` in `.env`. Save.
7. Restart task.

**Note:** Socket Mode breaks immediately when this token is revoked, so the bot will be offline for the few seconds between revoke and task restart. Expected.

### Token 4: Slack Bot User OAuth Token (`SLACK_BOT_TOKEN`, the `xoxb-` one)

**Where:** https://api.slack.com/apps -> click **Cora** app

1. Left sidebar -> **OAuth & Permissions**.
2. Scroll to find **Revoke Tokens** (or **Revoke Token** singular, depending on Slack UI version). Click it. Confirm.
3. After revoke, navigate to **Install App** -> **Reinstall to HJR Global Workspace** -> approve in the OAuth dialog.
4. After install, you'll see the new **Bot User OAuth Token** at the top of OAuth & Permissions. Copy `xoxb-...` value.
5. Update `SLACK_BOT_TOKEN=` in `.env`. Save.
6. Restart task.

**Note:** This is the only rotation that briefly affects Cora's installation state in the workspace. The bot app stays installed; the OAuth token changes. Channel membership is preserved across token rotation.

### After any rotation

Smoke test in #cora-build:

```
@Cora ping after token rotation
```

Expect a threaded reply within 5-10 seconds. If no reply, check logs for auth errors:

```powershell
Get-Content "C:\Users\Harri\code\cora\logs\cora-$(Get-Date -Format yyyy-MM-dd).log" -Tail 30
```

Most likely failure: token mis-pasted into `.env` (missing leading prefix, trailing whitespace, line break in middle). Re-check and retry.

---

## Updating Channel Routing

`design/channel-routing.yaml` is the source of truth.

Rules:
- First match wins (top-down evaluation)
- Fallback is FNDR (catch-all `*` pattern at bottom — do not remove it)
- Pattern syntax is fnmatch glob (e.g. `f3e-*` matches `f3e-leadership`, `f3e-ops`, etc.)

After editing: commit + push to GitHub, then restart the scheduled task.

---

## Startup Diagnosis

When Cora is unresponsive and the cause is unknown, run this 4-step sequence in order:

**Step 1 — Tail the log:**
```powershell
cd C:\Users\Harri\code\cora
Get-Content "logs\cora-$(Get-Date -Format yyyy-MM-dd).log" -Tail 30
```
> **Log-naming edge case:** The log file is named by the date Cora *started*, not today's date. If Cora started yesterday and ran past midnight, today's log file will not exist. Check the previous day's file: `Get-Content "logs\cora-$((Get-Date).AddDays(-1).ToString('yyyy-MM-dd')).log" -Tail 30`

**Step 2 — Pattern match in the log:**
```powershell
Select-String -Path "logs\cora-$(Get-Date -Format yyyy-MM-dd).log" -Pattern "heartbeat alive|ERROR|CRITICAL|AuthenticationError|Restarting in" | Select-Object -Last 20
```
Look for: recent `heartbeat alive` (alive), absence of heartbeat (dead), `AuthenticationError` (bad token), repeated `Restarting in` (crash loop).

**Step 3 — Process check:**
```powershell
Get-Process python* -ErrorAction SilentlyContinue | Select-Object Id, CPU, StartTime, MainWindowTitle
```
No output = no Python process running = Cora is down. Multiple entries = possible duplicate instance (use hard kill above, then restart once).

**Step 4 — Manual terminal start (last resort to see live output):**
```powershell
cd C:\Users\Harri\code\cora
uv run python -m cora.main
```
Run this in a terminal to see startup errors that may not make it into the log (e.g. import failures, config validation errors at boot). Kill with Ctrl+C when done, then restart via Task Scheduler.

---

## Troubleshooting

**Cora not responding to @-mentions:**
Check `Get-Process python*` — is the bot running? Check the latest log for recent `heartbeat alive` entries. If no heartbeat in the last 2 minutes, the bot is in a bad state — restart the task.

**Cora replies are generic / not entity-aware:**
Check the log for the `app_mention routed` line for that mention. Verify the channel name matches a YAML pattern (e.g. `#f3e-leadership` should route to `F3E`). If the channel is new, add a pattern to `channel-routing.yaml` and restart.

**Cora refuses everything in an entity channel:**
The cross-entity scope rule in the entity's system prompt may be firing too broadly. Reproduce the question in `#cora-build` (FNDR catch-all) to confirm the model can answer it at all. If it can answer there but not in the entity channel, the entity prompt's cross-entity section needs softening.

**Bot starts then dies within seconds:**
Check the log for config validation errors or an `AuthenticationError`. Most likely cause: a token in `.env` is malformed or expired.

**Log shows `rate_limited`:**
A user hit the per-user (10/hr) or the channel hit the per-channel (50/hr) cap. This is normal during stress tests and load bursts. Caps reset automatically after 60 minutes — no action needed.

---

## Knowledge Gaps review workflow

Cora appends a `[CORA_KNOWLEDGE_GAP: ...]` marker to responses when her context was too thin to answer confidently. The marker is stripped before posting to Slack. Gaps are logged to `logs/knowledge-gaps.jsonl` (one JSON line per gap).

**Recommended path (since 2026-05-19):** open a Cowork chat and say "review today's gaps" — Cowork drives the ritual conversationally and writes decisions directly. Full playbook: `G:\My Drive\HJR-Founder-OS\_shared\projects\cora\playbooks\gap-review-ritual.md`.

**Legacy path (manual Notepad edit):** open the digest in Notepad, fill in `Your answer` blocks with SKIP / answer / ROUTE, save, run `uv run python scripts/ingest_digest_answers.py --digest <path>`. Still works; the Cowork ritual is just faster for non-trivial reviews.

**When to run:** Nightly, or any time you want to review what Cora has been uncertain about.

**How to run:**

```powershell
# Default: last 24 hours
uv run python scripts/generate_knowledge_gaps_digest.py

# Specific date window
uv run python scripts/generate_knowledge_gaps_digest.py --since 2026-05-18

# All gaps from all time
uv run python scripts/generate_knowledge_gaps_digest.py --all

# Dry-run: print to terminal, don't write to Drive
uv run python scripts/generate_knowledge_gaps_digest.py --dry-run
```

**Where the digest lands:**
`G:\My Drive\HJR-Founder-OS\_shared\projects\cora\knowledge-gaps\YYYY-MM-DD-digest.md`

**How to review the digest:**

Each gap entry has a **Your answer** block. Three actions:

1. **SKIP** — gap is trivial or one-off. Marked resolved, no feedback to Cora.
2. **Write the answer** — fill in the real context. This text is the source of truth and will be manually copied into `design/known-answers/{entity}.md` files when ready (see Phase 2 note below).
3. **ROUTE: ask [person/system]** — future questions of this type should go to a person or tool. Write the routing note so you remember.

Leave the block empty to defer the gap to the next digest run.

**Phase 2 note:** Automated ingestion of your written answers back into Cora's context is deferred to Phase 2. For now, answers you write in the digest are the source of truth — copy them manually into `design/known-answers/{entity}.md` files when you're ready to feed them to Cora. The digest builder reads `knowledge-gaps.jsonl` each time from scratch, so un-ingested gaps will reappear in future digests until you SKIP or answer them.

## .env Recovery (byte corruption)

**Cause:** PowerShell 5.1's `Add-Content` and some text-writing cmdlets inject Windows-1252 characters (e.g. byte `0x97`, the Windows-1252 em dash) when the file or terminal encoding is not explicitly UTF-8. The corrupted byte is invisible in most editors but causes token parse failures at Cora startup (`AuthenticationError` or config validation error).

**Symptoms:** Cora starts then dies immediately; log shows `AuthenticationError` or `Config validation failed`; token looks correct when you open `.env` in Notepad but doesn't work.

**Manual fix:**
1. Open `.env` in Notepad (not VS Code or PowerShell ISE — Notepad shows raw bytes most reliably):
   ```powershell
   notepad C:\Users\Harri\code\cora\.env
   ```
2. Find the corrupted line. Position the cursor at the start of the value and use the right-arrow key to step through each character. Any position where the cursor skips two steps for one keypress is a hidden non-ASCII byte.
3. Delete the invisible character(s). Retype the value from scratch if unsure.
4. Save as: **File → Save As → Encoding: UTF-8** (NOT "UTF-8 with BOM"). Overwrite the existing `.env`.

**Byte-level verification (confirms no corruption):**
```powershell
$raw = [System.IO.File]::ReadAllBytes("C:\Users\Harri\code\cora\.env")
$nonAscii = $raw | Where-Object { $_ -gt 127 }
if ($nonAscii) { Write-Host "NON-ASCII BYTES FOUND: $nonAscii" } else { Write-Host "Clean — all ASCII" }
```

**Prevention:**
- Always edit `.env` in Notepad or a proper UTF-8 editor, never via PowerShell `Add-Content` / `Set-Content` without `-Encoding UTF8`
- If scripting `.env` updates, always use: `Set-Content -Encoding UTF8 -Path ".env" -Value $content`

**PowerShell .NET CurrentDirectory warning:** `[System.IO.File]` and similar .NET methods resolve relative paths against the *process launch directory*, not the current `$PWD`. Always use absolute paths (e.g. `C:\Users\Harri\code\cora\.env`) when calling .NET file APIs. `cd` does not affect .NET path resolution.

---

## Escalation

- Anthropic API issues: https://status.anthropic.com + Anthropic support
- Slack API issues: https://status.slack.com
- Code bugs / system issues: Harrison (this is his build)
