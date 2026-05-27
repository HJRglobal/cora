# Cora Infrastructure Security & Growth Plan

## What We're Protecting

| Asset | Location | Risk if Compromised |
|---|---|---|
| API keys (Slack, Anthropic, etc.) | `.env` on the desktop | Attacker can impersonate Cora, rack up API bills, read all messages |
| Google Service Account key | `.credentials/cora-calendar-sa.json` | Read all calendar events |
| QBO OAuth tokens | `.credentials/qbo-tokens.json` | Read/write QuickBooks data |
| Instagram access tokens | `.env` | Read DMs, post as brand accounts |
| Source code & system prompts | GitHub (private repo) | Reveals Cora's logic and business context |
| Log files | `logs/` (local) | Contains conversation content |
| Knowledge base | `data/cora_kb.db` (local) | All ingested business context |

---

## Threat Model

**Most likely threats (ordered by probability):**

1. **Compromised API key** — a token gets leaked via a copy-paste mistake, a shoulder surf, or a third-party service breach. Attacker uses it to query Anthropic/Slack at your expense or extract data.
2. **Phishing / social engineering** — attacker gets you to reveal a token or approve an OAuth grant.
3. **Machine intrusion** — someone gets physical or remote access to the desktop.
4. **Third-party breach** — one of the 10+ connected services (Slack, HubSpot, QBO, etc.) gets hacked and your OAuth token is exposed.
5. **Git accident** — a secret gets committed and pushed to GitHub even momentarily.

**Lower-probability threats:**
- Ransomware (Windows Defender + patching + backups mitigate)
- Supply-chain attack via a Python dependency (uv.lock pins versions; update regularly)
- Denial-of-service against the desktop (rate limiter in code already handles Slack floods)

---

## Immediate Security Actions

These are the actions to take **today**. The security monitor script handles detection and alerting automatically once deployed.

### Step 1 — Deploy the Security Monitor (new, in this repo)

```powershell
powershell -ExecutionPolicy Bypass -File deployment\setup-security-monitor-task.ps1
```

This registers a Windows Task Scheduler job that runs every 15 minutes. It will:
- Scan Cora's log files for authentication failures, restart loops, and API errors
- Detect if any monitored source files changed unexpectedly (`.env`, `app.py`, etc.)
- Check Windows Event Log for failed login attempts
- Post a Slack alert to `#cora-build` (or `SECURITY_ALERT_CHANNEL`) when anything is found
- Suppress duplicate alerts for the same event for 1 hour to avoid notification fatigue

Test it immediately after setup:
```powershell
uv run python scripts\security_monitor.py --dry-run
```

### Step 2 — Run the Windows Security Posture Check

```powershell
# Run as Administrator for full results
powershell -ExecutionPolicy Bypass -File deployment\check-windows-security.ps1
```

Fix any **FAIL** items in the output. The most important ones:
- **BitLocker**: encrypts the drive so the `.env` file can't be read if someone steals or clones the disk
- **Windows Firewall**: all three profiles should be ON
- **Remote Desktop**: should be OFF (Cora doesn't need it)

### Step 3 — Enable 2FA on Every API Provider

Go through each one and turn on two-factor auth:
- [Anthropic Console](https://console.anthropic.com) → Account settings
- [Slack](https://slack.com/account/settings) → Two-factor authentication
- [GitHub](https://github.com/settings/security) → Two-factor authentication
- [Google](https://myaccount.google.com/security) → 2-Step Verification
- [Intuit (QBO)](https://accounts.intuit.com/index.html) → Security
- [HubSpot](https://app.hubspot.com/profile-preferences/security) → Two-factor authentication

### Step 4 — Set API Budget Caps

Even if a key is stolen, a hard spending cap limits the blast radius:
- **Anthropic**: console.anthropic.com → Settings → Billing → Usage Limits → hard cap $200/mo
- **OpenAI**: platform.openai.com/usage/limits → hard cap $50/mo

### Step 5 — Create a Private `#cora-security` Slack Channel

```
# In Slack: create a private channel called cora-security
# Then add to .env:
SECURITY_ALERT_CHANNEL=cora-security
```

Restart the scheduled task after updating `.env`:
```powershell
Stop-ScheduledTask  -TaskName "cowork-cora-security-monitor"
Start-ScheduledTask -TaskName "cowork-cora-security-monitor"
```

### Step 6 — Rotate All Tokens Now (one-time hygiene)

If you've never done a full rotation, do it once now to establish a clean baseline.
Follow the rotation guide in `deployment/runbook.md` → "Rotating Tokens".

---

## Infrastructure Growth Path

### Stage 1 — Secure the Desktop *(you are here)*

**When:** Now through ~6 months  
**Capacity:** Low-moderate Slack usage, single machine is fine  
**Cost:** $0 additional

What's already in place:
- `.env` is gitignored and never committed
- Pre-commit hook blocks API keys from being committed
- Windows Task Scheduler auto-restarts Cora on crash
- In-process restart loop for WebSocket disconnects

What to add (use the steps above):
- Security monitor (this PR)
- BitLocker encryption
- Windows Firewall rules
- 2FA on all API providers
- API spending caps

**Trigger to move to Stage 2:** You go on a trip and can't reach the desktop machine — or the desktop has a hardware failure and you lose more than a few hours.

---

### Stage 2 — Add a Cloud Safety Net

**When:** When the desktop going down for >4 hours would hurt the business  
**Capacity:** Same as Stage 1, but with 99%+ uptime  
**Cost:** ~$20–40/month

Actions:
1. **Cloudflare Tunnel** (free) — gives Cora a stable public HTTPS endpoint without opening any firewall ports. Required for QBO Production OAuth redirect URI and any future webhook endpoints.
   - Install `cloudflared` on the desktop
   - `cloudflared tunnel create cora-oauth`
   - Map `cora-oauth.hjrglobal.com` → `http://localhost:8766`

2. **Secrets in 1Password Teams or AWS Secrets Manager** — instead of `.env` living only on one disk, store secrets in a vault that survives hardware failure and lets you rotate without touching the disk.
   - 1Password Teams: ~$20/mo, simpler UX
   - AWS Secrets Manager: ~$0.40/secret/month at your scale (effectively free)

3. **Offsite backup** — run `scripts/backup_logs.py` on a schedule to push logs and the KB database to Google Drive or AWS S3.

4. **External uptime monitor** (free tier) — [UptimeRobot](https://uptimerobot.com) can ping Cora's health endpoint every 5 minutes and SMS/email you if it's down.
   - Add a `/health` HTTP endpoint to the bot (10 lines of code)

**Trigger to move to Stage 3:** Cora is handling real-time financial data, multiple entities are depending on her simultaneously, or you want to deploy without being physically at the machine.

---

### Stage 3 — Move to the Cloud

**When:** Cora is business-critical infrastructure with >3 active entities  
**Capacity:** Hundreds of interactions/day, multiple concurrent users  
**Cost:** ~$50–120/month

Architecture:
```
GitHub Actions (CI/CD)
        ↓
AWS EC2 t3.small or DigitalOcean Droplet ($12–20/mo)
  └── Docker container: cora
  └── AWS Secrets Manager (or 1Password) → replaces .env
  └── CloudWatch Logs (or Datadog free tier) → replaces local log files
  └── AWS S3 → log backups and KB snapshots
        ↓
Cloudflare (edge, DDoS protection, TLS termination)
```

Specific steps when you reach this stage:
1. **Dockerize Cora** — `Dockerfile` + `docker-compose.yml` so the runtime is reproducible
2. **Move secrets to AWS Secrets Manager** — `boto3` fetches them at startup, `.env` is gone
3. **GitHub Actions deployment** — push to `main` → CI runs tests → deploys to the EC2 instance
4. **Enable AWS CloudTrail** — audit log of every API call to your AWS account
5. **VPC + Security Groups** — allow only port 443 outbound; no inbound ports needed (Socket Mode is outbound-only)

Security upgrades that come for free with cloud:
- Automated OS patching (AWS SSM Patch Manager)
- No physical machine to steal
- IAM roles replace local credentials for AWS services
- CloudTrail gives you an immutable audit log

**Trigger to move to Stage 4:** You're running multiple independent Cora instances (one per brand/entity), or the knowledge base is so large it needs a managed vector database.

---

### Stage 4 — Scale & Resilience *(future)*

**When:** Enterprise scale, multiple independent deployments  
**Capacity:** Thousands of interactions/day  
**Cost:** $200–500/month

- Kubernetes (EKS or GKE) — scale horizontally, roll deployments with zero downtime
- Managed vector database (Pinecone or Weaviate Cloud) — replaces SQLite KB
- Multi-region (US-East + US-West) — survive an AWS availability zone failure
- Zero-trust networking (Cloudflare Access) — every service authenticates to every other service
- SOC2 readiness — if you ever need to share Cora's infrastructure with enterprise clients

---

## Incident Response Playbook

### "I think a token was leaked"

1. **Don't panic.** Revoke first, investigate after.
2. Identify which token (check what was exposed — email? screenshot? git commit?)
3. Immediately rotate that specific token (see `deployment/runbook.md` → "Rotating Tokens")
4. Check the API provider's usage dashboard for anomalous calls
5. Check Anthropic console → API Keys → "Last used" timestamp vs. what you expected
6. If you see usage you didn't generate: contact the API provider's security team

### "Security monitor sent a HIGH alert"

1. Open the log: `Get-Content "C:\Users\Harri\code\cora\logs\cora-$(Get-Date -Format yyyy-MM-dd).log" -Tail 100`
2. Look for the pattern the monitor flagged
3. If it's a false positive (e.g., a legitimate API error spike during a test), clear the alert history:
   ```powershell
   Remove-Item "C:\Users\Harri\code\cora\data\security\alert_history.json"
   ```
4. If it's real, follow the token rotation or incident response steps above

### "File integrity alert — [filename] changed unexpectedly"

1. Run `git diff HEAD [filename]` to see what changed
2. If you recognize the change: re-initialize the baseline: `uv run python scripts\security_monitor.py --init`
3. If you don't recognize the change: treat as potential intrusion — check Windows Event Log for recent logins, check git log for unauthorized commits

### "Cora is offline and the machine isn't responding"

Before Stage 3 (cloud migration), your options are:
1. Remote into the machine via Windows Remote Desktop (if it's enabled and you're on the same network)
2. Physically go to the office and restart the machine / the scheduled task
3. If you expect extended downtime, consider spinning up a temporary fallback on a laptop with `uv run cora`

After Stage 3: SSH into the EC2 instance from anywhere; auto-healing handles most failures.

---

## Security Checklist (run monthly)

- [ ] `deployment\check-windows-security.ps1` — all FAILs resolved
- [ ] `uv run python scripts\security_monitor.py --dry-run` — no HIGH issues
- [ ] Anthropic console: no unexpected API key usage, budget alerts still configured
- [ ] All API providers: 2FA enabled, no stale OAuth grants
- [ ] GitHub: no unexpected collaborators on `HJRglobal/cora`
- [ ] `.env` is in `.gitignore` and NOT tracked: `git ls-files .env` → empty output
- [ ] `Get-ScheduledTask | Where-Object { $_.TaskName -like "cowork-cora-*" }` — all tasks Ready/Running
- [ ] Python dependencies: run `uv lock --upgrade` quarterly to get patched versions
