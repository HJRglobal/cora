# Bootstrap Cora on a New Windows Machine

**Use this when:** the always-on desktop has been destroyed, replaced, or you're standing up a second machine to run Cora.

**You'll need before starting:**
- Internet access on the new machine
- Login credentials for Anthropic console (console.anthropic.com)
- Login credentials for Slack admin (api.slack.com/apps)
- Login credentials for GitHub (github.com/HJRglobal)
- Your existing Slack workspace (HJR Global) — Cora is already installed there, no need to re-create the app

**Time estimate:** 60-90 minutes if everything goes clean, 2 hours if you hit any winget snags.

---

## Phase 0 — Pre-flight (~5 min)

Open Windows PowerShell as your normal user (not admin). Verify the basics:

```powershell
$PSVersionTable.PSVersion
# Should show 5.1 or higher
```

Set execution policy so npm scripts and our PS1 files can run:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
# Confirm with Y when prompted
```

---

## Phase 1 — Install dependencies (~15-20 min)

### Node.js (needed for Claude Code)

```powershell
winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
```

### Git

```powershell
winget install -e --id Git.Git --accept-source-agreements --accept-package-agreements
```

### Python 3.12

```powershell
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
```

### Close and reopen PowerShell

PATH won't refresh until you do. **Close the window, open a new one.** Then verify:

```powershell
node --version    # Should print v20.x or v22.x
git --version     # Should print git version 2.x
python --version  # Should print Python 3.12.x
```

If any fail with "command not found," PATH is wrong. Check `$env:PATH -split ';'` and add the install dir manually if missing.

### Claude Code CLI

```powershell
npm install -g @anthropic-ai/claude-code
```

Then **close and reopen PowerShell again** (npm's global bin needs PATH refresh):

```powershell
claude --version
# Should print a version number
```

### uv (Python project manager)

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Installs to `C:\Users\<you>\.local\bin\uv.exe`. Add to PATH for this session:

```powershell
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
uv --version
# Should print uv 0.x.x
```

For permanent PATH (so Task Scheduler can find it):

```powershell
[Environment]::SetEnvironmentVariable("PATH", "$env:USERPROFILE\.local\bin;$env:PATH", "User")
```

Then reopen PowerShell once more.

---

## Phase 2 — Clone the repo (~5 min)

```powershell
mkdir C:\Users\Harri\code
cd C:\Users\Harri\code
git clone https://github.com/HJRglobal/cora.git
cd cora
```

Configure git user (one-time, replace with your info):

```powershell
git config user.email "harrison@hjrglobal.com"
git config user.name "Harrison Rogers"
```

Install the pre-commit hook (this isn't automatic on clone):

```powershell
git config core.hooksPath .githooks
```

Install Python dependencies:

```powershell
uv sync
```

Verify the package installs cleanly. Last line should be something like "Installed N packages."

---

## Phase 3 — Regenerate the 4 secrets (~15 min)

The destroyed machine had a `.env` with 4 live tokens. **Those tokens still work** in the Slack and Anthropic systems — unless you actively revoke them, they'll still authenticate. But if you can't account for where the old `.env` ended up (or the disk was compromised), regenerate to be safe.

### Secret 1 of 4: Anthropic API key

1. Go to **https://console.anthropic.com**
2. Sign in
3. Left sidebar → **API Keys**
4. **(Optional) Revoke the old `cora-phase-1` key** (if you suspect it was compromised). Three-dot menu next to the key → Revoke.
5. **Create Key** → name it `cora-production` (or `cora-phase-1` if you revoked) → **Create**
6. **Copy the key immediately** (starts with `sk-ant-***`) — shown only once.

Verify budget alerts still in place:

7. Settings → **Plans & Billing** → check **Usage Limits**
8. Confirm: $50 monthly warn, $200 monthly hard cap

### Secret 2 of 4: Slack Signing Secret

1. Go to **https://api.slack.com/apps**
2. Sign in. Click the **Cora** app.
3. Left sidebar → **Basic Information** → scroll to **App Credentials**
4. Next to **Signing Secret**, click **Regenerate** (or **Show** if you trust the existing one)
5. Confirm the warning. Copy the new secret.

### Secret 3 of 4: Slack App-Level Token (xapp-)

Still in your Cora Slack app config:

1. Left sidebar → **Basic Information** → scroll to **App-Level Tokens**
2. Find the existing `cora-socket` token → click into it → **Delete** (confirm).
3. Back at **App-Level Tokens** → **Generate Token and Scopes**
4. Name: `cora-socket` → **Add Scope** → select `connections:write` → **Generate**
5. Copy the new `xapp-1-...` value.

### Secret 4 of 4: Slack Bot User OAuth Token (xoxb-)

Still in your Cora Slack app config:

1. Left sidebar → **OAuth & Permissions**
2. Scroll to find **Revoke Tokens** (or skip if not concerned about old token leaking)
3. Click **Revoke** if you want to invalidate the old token.
4. After revoke, navigate to **Install App** → **Reinstall to HJR Global Workspace** → approve.
5. New **Bot User OAuth Token** appears at the top of OAuth & Permissions page. Copy `xoxb-...` value.

---

## Phase 4 — Create `.env` with the new secrets (~5 min)

```powershell
copy .env.example .env
notepad C:\Users\Harri\code\cora\.env
```

In Notepad, replace each `REPLACE_ME_*` placeholder with the actual value you just copied. The file should end up with:

```
SLACK_BOT_TOKEN=xoxb-NNNNNNNNN-NNNNNNNNN-XXXXXXX
SLACK_APP_TOKEN=xapp-1-AXXXXXXXXX-NNNNNNNNN-XXXXXX
SLACK_SIGNING_SECRET=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
ANTHROPIC_API_KEY=sk-ant-***your-key***
```

Save (Ctrl+S) and close.

**Critical:** `.env` is gitignored. NEVER commit it. NEVER paste these values into chat or any external system.

Verify the bot can start with the new secrets:

```powershell
uv run cora
```

Expect logs:
```
2026-XX-XXTHH:MM:SS INFO [MainThread] cora.main: Cora starting up...
2026-XX-XXTHH:MM:SS INFO [MainThread] cora.main: Cora Socket Mode connecting... (attempt #1)
Bolt app is running!
```

If you see config validation errors (wrong prefix, REPLACE_ME still present), fix the .env and retry.

Press Ctrl+C to stop the foreground bot. We'll start it via Task Scheduler in the next phase.

---

## Phase 5 — Register Windows Task Scheduler entries (~5 min)

Two scheduled tasks: the bot service (continuous) and the daily digest builder.

### Task 1 — Cora service (continuous, AtLogOn trigger)

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\Harri\code\cora\deployment\setup-windows-task.ps1"
```

Expect 5 OK lines ending with "Setup complete."

### Task 2 — Daily digest builder (5am AZ)

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\Harri\code\cora\deployment\setup-digest-task.ps1"
```

Expect 5 OK lines. `NextRunTime` will show tomorrow at 05:00.

### Task 3 — Daily log backup (4:30am AZ, fires before the digest)

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\Harri\code\cora\deployment\setup-backup-task.ps1"
```

Verify all three are registered:

```powershell
Get-ScheduledTask | Where-Object { $_.TaskName -like "cowork-cora-*" } | Format-Table TaskName, State
```

Should show three rows: `cowork-cora-service`, `cowork-cora-digest`, `cowork-cora-backup`.

---

## Phase 6 — Start Cora and smoke test (~5 min)

```powershell
Start-ScheduledTask -TaskName "cowork-cora-service"
Start-Sleep -Seconds 8
Get-Process python*
```

Expect at least one `python` process running.

### Test in Slack

Go to Slack `#cora-build` (channel ID `C0B4B0URRQS`). If Cora isn't a member already (she should be from the workspace install — verify with `/who`):

```
/invite @Cora
```

Then:

```
@Cora ping from new machine
```

Expect a threaded reply within 5-10 seconds.

---

## Phase 7 — Verify ongoing operations (~10 min)

### Check the log

```powershell
Get-Content "C:\Users\Harri\code\cora\logs\cora-$(Get-Date -Format yyyy-MM-dd).log" -Tail 20
```

Should show recent connecting / heartbeat / app_mention entries.

### Confirm the digest will fire

```powershell
Get-ScheduledTaskInfo -TaskName "cowork-cora-digest" | Format-List LastRunTime, LastTaskResult, NextRunTime
```

`NextRunTime` should show tomorrow at 05:00.

### Tomorrow morning

After 5am, check that the digest landed:

```powershell
ls "G:\My Drive\HJR-Founder-OS\_shared\projects\cora\knowledge-gaps\"
```

You should see today's digest file (or yesterday's, depending on timing).

---

## What if something goes wrong?

| Symptom | Most likely cause | Fix |
|---|---|---|
| `uv run cora` exits silently | `.env` has `REPLACE_ME_*` still or wrong prefix | Re-check .env values |
| `Bolt app is running!` never appears | `xapp-1-` token missing `connections:write` scope | Regenerate token with correct scope |
| Cora doesn't reply in Slack | Bot not invited to channel OR wrong workspace | `/invite @Cora`; verify Slack workspace is HJR Global |
| Setup script fails at "uv.exe not found" | uv not on PATH | Re-run uv install + reopen PowerShell |
| Task registered but bot never starts | Logon trigger needs user to be logged in | Use `Start-ScheduledTask` to start manually first |
| Setup script parse error | em-dash or other non-ASCII char in .ps1 | This shouldn't happen on a fresh clone — repo is ASCII-clean |
| Pre-commit hook blocks commit on real key | `.env` got staged accidentally | `git restore --staged .env` and verify `.env` is gitignored |

For deeper troubleshooting see `deployment/runbook.md`.

---

## What's NOT covered by this runbook

- **Decisions / context in `G:\My Drive\HJR-Founder-OS\`** — that's the Founder OS, separately backed up via Drive sync. Not Cora's responsibility.
- **Slack workspace itself** — cloud-hosted by Slack, survives any local machine destruction.
- **Anthropic billing / usage caps** — cloud-side, survives local destruction. Worth a quick check at console.anthropic.com after bootstrap to confirm budget alerts are still configured.
- **The full team's individual Slack usage** — Cora's reply behavior is restored; team channels and members are workspace-side.

---

## Sanity check questions to ask before declaring bootstrap complete

- [ ] `Get-ScheduledTask` shows all 3 cowork-cora-* tasks in `Ready` or `Running` state
- [ ] `Get-Process python*` shows a running python process (Cora is alive)
- [ ] `@Cora ping` in `#cora-build` produces a threaded reply within 10 seconds
- [ ] Most recent log file shows a heartbeat within the last 2 minutes
- [ ] Anthropic console shows the new API key is the only active one (old one revoked if relevant)
- [ ] Slack app config shows the new tokens are the only active ones (old ones revoked if relevant)
- [ ] `.env` is NOT showing as modified or untracked in `git status` (gitignored correctly)

If all 7 pass, Cora is fully back online.
