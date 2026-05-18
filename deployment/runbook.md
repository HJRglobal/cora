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

**Stop (hard kill):**
```powershell
Get-Process python* | Stop-Process -Force
```

**Verify she's alive:**
```powershell
Get-Process python*
```
Or check the log file for a `heartbeat alive` entry within the last 2 minutes.

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

All token rotations require a task restart for new values to load from `.env`.

**Anthropic API key:**
1. console.anthropic.com -> API Keys -> revoke old, create new
2. Update `ANTHROPIC_API_KEY` in `.env`
3. Restart task

**Slack tokens (xoxb / xapp / signing secret):**
1. api.slack.com/apps -> Cora app -> revoke + reinstall to workspace
2. Update `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET` in `.env`
3. Restart task

---

## Updating Channel Routing

`design/channel-routing.yaml` is the source of truth.

Rules:
- First match wins (top-down evaluation)
- Fallback is FNDR (catch-all `*` pattern at bottom — do not remove it)
- Pattern syntax is fnmatch glob (e.g. `f3e-*` matches `f3e-leadership`, `f3e-ops`, etc.)

After editing: commit + push to GitHub, then restart the scheduled task.

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

## Escalation

- Anthropic API issues: https://status.anthropic.com + Anthropic support
- Slack API issues: https://status.slack.com
- Code bugs / system issues: Harrison (this is his build)
