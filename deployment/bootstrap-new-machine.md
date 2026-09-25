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

Verify the bot can start with the new secrets (the SAME form the service task runs -- never the
console-script `cora.exe`, which the host's WDAC code-integrity policy blocks, DR-MANIFEST D06):

```powershell
.venv\Scripts\python.exe -m cora.main
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

## Phase 5 — Register the FULL scheduled estate (~30-45 min)

**This phase used to register 3 tasks. The live estate is ~96 (measured 2026-09-22:
service + watchdog + the KB ingest bundle + finance/synthesis/capture lanes).** A rebuild
that stops at 3 has no KB ingestion, no watchdog, no finance estate and no catch-up lane.
The estate is now DERIVED, not hand-listed: `scripts/generate_task_estate_manifest.py`
reads the live Task Scheduler registry and writes `deployment/manifest/task-estate.json`
(+ `.md`); the block below is regenerated from it (`--update-docs`). Hand edits inside the
markers are overwritten on the next regeneration -- fix the generator or the estate.

Every task runs as the interactive user (`LogonType=Interactive`), so the estate fires ONLY
while that user is signed in -- a cold boot to the sign-in screen runs nothing (the 9/9
incident, `cq-fb50c9e6c911`). Auto-logon (or a change of logon type) is a Harrison
decision recorded in `deployment/DR-MANIFEST.md`, not something this phase sets.

### Scheduled estate (generated -- do not hand-edit)

<!-- BEGIN GENERATED: scheduled-estate -->
_Generated 2026-09-25 by `scripts/generate_task_estate_manifest.py --update-docs` from the live registry: **100 tasks** (81 enabled). Source of truth: `deployment/manifest/task-estate.json`. Do not hand-list tasks here -- regenerate._

**Register the estate FROM THE MANIFEST** (from an ELEVATED PowerShell at the repo root; host time zone must be `US Mountain Standard Time` first -- `Set-TimeZone`):
1. `.\deployment\register-estate-from-manifest.ps1` (dry-run plan) then `.\deployment\register-estate-from-manifest.ps1 -Apply` -- registers every task below from `deployment/manifest/tasks/<slug>.xml` (full-fidelity Task Scheduler exports: triggers, windowless action, settings, run level, and `Enabled=false` for the 19 intent-disabled tasks). The `setup-*.ps1` scripts are NOT run on a rebuild: several carry drifted clocks or would re-enable disabled tasks; they create NEW tasks only.
2. Restore `.env` (Phase 4) BEFORE starting anything; then `Start-ScheduledTask -TaskName cowork-cora-service` (the service otherwise starts at the next logon).
3. Verify: `Get-ScheduledTask | Where-Object { $_.TaskName -like 'cowork-cora-*' -or $_.TaskName -like 'Cora - *' -or $_.TaskName -eq 'cora-watchdog' } | Measure-Object` -> expect **100**, then `.venv\Scripts\python.exe scripts\generate_task_estate_manifest.py --diff-only` -> expect ZERO `task-estate-drift` lines (cron / enabled / action / principal / settings / time zone are all compared).
4. The Cowork estate (Claude desktop scheduled tasks) is NOT registered by any of this: it stays on the office machine (charter D4); its weekly pin task is `cowork-model-pin-weekly`.

| Task | Trigger | XML (restore form) | Created by (setup script, informational) | Run level / logon | SWA | Intent |
|---|---|---|---|---|---|---|
| `Cora - Asana Hygiene Nudges` | daily 06:40 | `tasks/Cora-Asana-Hygiene-Nudges.xml` | `setup-asana-hygiene-nudges-task.ps1` | Highest / Interactive | yes | UNROWED |
| `Cora - Cash Flow Pulse` | daily 15:30 | `tasks/Cora-Cash-Flow-Pulse.xml` | `setup-cashflow-pulse-task.ps1` | Highest / Interactive | yes | disabled |
| `Cora - Cash Snapshot` | daily 06:45 | `tasks/Cora-Cash-Snapshot.xml` | `setup-cashflow-snapshot-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Channel Health Monitor` | weekly Sun 04:15 | `tasks/Cora-Channel-Health-Monitor.xml` | `setup-channel-health-monitor-task.ps1` | Highest / Interactive | yes | UNROWED |
| `Cora - Daily Briefing` | weekly Mon,Tue,Wed,Thu,Fri 07:30 | `tasks/Cora-Daily-Briefing.xml` | `setup-daily-briefing-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Daily Synthesis (BDM)` | daily 06:52 | `tasks/Cora-Daily-Synthesis-BDM.xml` | `setup-daily-synthesis-bdm-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Daily Synthesis (F3C)` | daily 06:58 | `tasks/Cora-Daily-Synthesis-F3C.xml` | `setup-daily-synthesis-f3c-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Daily Synthesis (F3E)` | daily 06:33 | `tasks/Cora-Daily-Synthesis-F3E.xml` | `setup-daily-synthesis-f3e-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Daily Synthesis (HJRP)` | daily 06:35 | `tasks/Cora-Daily-Synthesis-HJRP.xml` | `setup-daily-synthesis-hjrp-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Daily Synthesis (HJRPROD)` | daily 06:56 | `tasks/Cora-Daily-Synthesis-HJRPROD.xml` | `setup-daily-synthesis-hjrprod-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Daily Synthesis (LEX)` | daily 06:39 | `tasks/Cora-Daily-Synthesis-LEX.xml` | `setup-daily-synthesis-lex-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Daily Synthesis (OSN)` | daily 06:37 | `tasks/Cora-Daily-Synthesis-OSN.xml` | `setup-daily-synthesis-osn-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Daily Synthesis (Portfolio)` | daily 06:31 | `tasks/Cora-Daily-Synthesis-Portfolio.xml` | `setup-daily-synthesis-portfolio-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Daily Synthesis (UFL)` | daily 06:54 | `tasks/Cora-Daily-Synthesis-UFL.xml` | `setup-daily-synthesis-ufl-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Deal Aging Alerts` | daily 15:00 | `tasks/Cora-Deal-Aging-Alerts.xml` | `setup-deal-aging-alerts-task.ps1` | Limited / Interactive | yes | disabled |
| `Cora - Drive Materialization` | daily 05:45 | `tasks/Cora-Drive-Materialization.xml` | `setup-drive-materialization-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Drive Sweep` | daily 06:00 | `tasks/Cora-Drive-Sweep.xml` | `setup-drive-sweep-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Due Date Escalation` | daily 14:00 | `tasks/Cora-Due-Date-Escalation.xml` | `setup-due-date-escalation-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Email Attachment Filer` | every PT4H (from 2026-05-27T22:00) | `tasks/Cora-Email-Attachment-Filer.xml` | `setup-attachment-filer-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Expected Invoice Check` | monthly day 9 09:38 | `tasks/Cora-Expected-Invoice-Check.xml` | `setup-monthly-finance-report-tasks.ps1` | Limited / Interactive | **no** | UNROWED |
| `Cora - F3E Blog Pipeline` | weekly Mon 08:50 | `tasks/Cora-F3E-Blog-Pipeline.xml` | `setup-f3e-blog-pipeline-task.ps1` | Limited / Interactive | **no** | UNROWED |
| `Cora - F3E Daily Ecom Brief` | daily 07:10 | `tasks/Cora-F3E-Daily-Ecom-Brief.xml` | `setup-f3e-ecom-brief-task.ps1` | Limited / Interactive | yes | disabled |
| `Cora - False Deflection Watch` | weekly Mon 08:00 | `tasks/Cora-False-Deflection-Watch.xml` | `setup-false-deflection-watch-task.ps1` | Highest / Interactive | yes | UNROWED |
| `Cora - Friction Mining` | weekly Sun 17:30 | `tasks/Cora-Friction-Mining.xml` | `setup-friction-mining-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - HubSpot Deal Monitor` | every PT1H (from 2026-06-03T16:00) | `tasks/Cora-HubSpot-Deal-Monitor.xml` | `setup-hubspot-deal-monitor-task.ps1` | Limited / Interactive | yes | disabled |
| `Cora - Inventory Alerts` | daily 16:00 | `tasks/Cora-Inventory-Alerts.xml` | `setup-inventory-alerts-task.ps1` | Limited / Interactive | yes | disabled |
| `Cora - KB Evals` | weekly Mon 09:05 | `tasks/Cora-KB-Evals.xml` | `setup-kb-evals-task.ps1` | Limited / Interactive | yes | enabled |
| `Cora - Klaviyo Billing Audit` | monthly day 9 09:53 | `tasks/Cora-Klaviyo-Billing-Audit.xml` | `setup-monthly-finance-report-tasks.ps1` | Limited / Interactive | **no** | UNROWED |
| `Cora - Knowledge Check` | weekly Mon,Tue,Wed,Thu,Fri 08:05 | `tasks/Cora-Knowledge-Check.xml` | `setup-knowledge-check-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - LEX Dump Folder Sync` | daily 04:45 | `tasks/Cora-LEX-Dump-Folder-Sync.xml` | `setup-lex-dump-folder-sync-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - LEX Swept PHI Check` | daily 07:06 | `tasks/Cora-LEX-Swept-PHI-Check.xml` | `setup-lex-swept-phi-check-task.ps1` | Limited / Interactive | yes | enabled |
| `Cora - Log Compaction` | monthly day 1 14:00 | `tasks/Cora-Log-Compaction.xml` | `setup-compaction-task.ps1` | Limited / Interactive | **no** | UNROWED |
| `Cora - Meeting Action Capture` | every PT1H (from 2026-06-05T11:00) | `tasks/Cora-Meeting-Action-Capture.xml` | `setup-meeting-action-capture-task.ps1` | Limited / Interactive | yes | disabled |
| `Cora - Meeting Ask Capture` | every PT15M for PT13H stop-at-end (daily 07:08) | `tasks/Cora-Meeting-Ask-Capture.xml` | `setup-meeting-ask-capture-task.ps1` | Limited / Interactive | **no** | enabled |
| `Cora - Missed Nightly Catch-Up` | daily 08:30 | `tasks/Cora-Missed-Nightly-Catch-Up.xml` | `setup-missed-nightly-catchup-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - OSN Metrics Digest` | weekly Mon 15:00 | `tasks/Cora-OSN-Metrics-Digest.xml` | `setup-osn-metrics-digest-task.ps1` | Highest / Interactive | yes | UNROWED |
| `Cora - QBO Monthly Reports` | monthly day 2 07:45 | `tasks/Cora-QBO-Monthly-Reports.xml` | `setup-qbo-monthly-reports-task.ps1` | Limited / Interactive | **no** | UNROWED |
| `Cora - QBO Token Monitor` | daily 06:50 | `tasks/Cora-QBO-Token-Monitor.xml` | `setup-qbo-token-monitor-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Revops Sweep` | daily 10:15 | `tasks/Cora-Revops-Sweep.xml` | `setup-revops-sweep-task.ps1` | Limited / Interactive | **no** | UNROWED |
| `Cora - Shopify DTC Summary` | daily 15:00 | `tasks/Cora-Shopify-DTC-Summary.xml` | `setup-shopify-dtc-summary-task.ps1` | Limited / Interactive | yes | disabled |
| `Cora - Strategy Memo` | weekly Sun 18:30 | `tasks/Cora-Strategy-Memo.xml` | `setup-strategy-memo-task.ps1` | Limited / Interactive | yes | UNROWED |
| `Cora - Weekly Health Metrics` | weekly Mon 09:30 | `tasks/Cora-Weekly-Health-Metrics.xml` | `setup-weekly-health-metrics-task.ps1` | Highest / Interactive | yes | UNROWED |
| `Cora - Weekly Pipeline Digest` | weekly Mon 15:00 | `tasks/Cora-Weekly-Pipeline-Digest.xml` | `setup-pipeline-digest-task.ps1` | Limited / Interactive | yes | disabled |
| `cora-watchdog` | every PT5M (from 2026-07-16T10:27) | `tasks/cora-watchdog.xml` | `setup-cora-watchdog-task.ps1` | Highest / Interactive | yes | UNROWED |
| `cowork-cora-ai-visibility-scan` | weekly Mon 10:15 | `tasks/cowork-cora-ai-visibility-scan.xml` | `setup-ai-visibility-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-asana-email-sync` | every PT1H (from 2026-06-01T00:10) | `tasks/cowork-cora-asana-email-sync.xml` | (none -- manifest XML only) | Limited / Interactive | **no** | disabled |
| `cowork-cora-autowrite-digest` | weekly Mon 11:00 | `tasks/cowork-cora-autowrite-digest.xml` | `setup-autowrite-digest-task.ps1` | Limited / Interactive | **no** | UNROWED |
| `cowork-cora-backup` | daily 20:30 | `tasks/cowork-cora-backup.xml` | `setup-backup-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-bank-snapshot` | daily 07:05 | `tasks/cowork-cora-bank-snapshot.xml` | `setup-qbo-bank-snapshot-task.ps1` | Limited / Interactive | yes | enabled |
| `cowork-cora-cashflow-actuals` | weekly Mon 06:25 | `tasks/cowork-cora-cashflow-actuals.xml` | `setup-cashflow-actuals-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-cashflow-forecast-snapshot` | weekly Mon 06:15 | `tasks/cowork-cora-cashflow-forecast-snapshot.xml` | `setup-cashflow-forecast-snapshot-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-channel-sweep` | daily 08:40 | `tasks/cowork-cora-channel-sweep.xml` | `setup-channel-sweep-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-claude-mirror` | daily 03:45 + daily 12:15 | `tasks/cowork-cora-claude-mirror.xml` | `setup-claude-mirror-task.ps1` | Limited / Interactive | yes | enabled |
| `cowork-cora-completion-sweep` | daily 14:00 | `tasks/cowork-cora-completion-sweep.xml` | (none -- manifest XML only) | Limited / Interactive | **no** | UNROWED |
| `cowork-cora-decision-capture` | daily 07:15 | `tasks/cowork-cora-decision-capture.xml` | (none -- manifest XML only) | Limited / Interactive | **no** | UNROWED |
| `cowork-cora-delegated-work` | every PT15M for P3650D stop-at-end (from 2026-08-01T00:00) | `tasks/cowork-cora-delegated-work.xml` | `setup-delegated-work-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-deposco-inventory-sync` | daily 06:22 | `tasks/cowork-cora-deposco-inventory-sync.xml` | `setup-deposco-sync-tasks.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-deposco-lot-ledger` | daily 07:45 | `tasks/cowork-cora-deposco-lot-ledger.xml` | `setup-deposco-sync-tasks.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-digest` | daily 05:20 | `tasks/cowork-cora-digest.xml` | `setup-digest-task.ps1` | Limited / Interactive | yes | disabled |
| `cowork-cora-drive-extractor` | daily 04:05 + at logon | `tasks/cowork-cora-drive-extractor.xml` | `setup-drive-extractor-task.ps1` | Highest / ServiceAccount | yes | UNROWED |
| `cowork-cora-feedback-health` | weekly Mon 08:30 | `tasks/cowork-cora-feedback-health.xml` | (none -- manifest XML only) | Limited / Interactive | **no** | UNROWED |
| `cowork-cora-finance-adherence` | weekly Mon 08:15 | `tasks/cowork-cora-finance-adherence.xml` | `setup-finance-adherence-task.ps1` | Limited / Interactive | yes | enabled |
| `cowork-cora-finance-close-pack` | weekly Mon 09:00 | `tasks/cowork-cora-finance-close-pack.xml` | `setup-finance-close-pack-task.ps1` | Limited / Interactive | yes | enabled |
| `cowork-cora-finance-receipt-digest` | weekly Mon 10:30 | `tasks/cowork-cora-finance-receipt-digest.xml` | `setup-finance-receipt-digest-task.ps1` | Limited / Interactive | **no** | UNROWED |
| `cowork-cora-finance-weekly` | weekly Mon 14:30 | `tasks/cowork-cora-finance-weekly.xml` | `setup-finance-weekly-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-fireflies-coverage` | weekly Mon 08:10 | `tasks/cowork-cora-fireflies-coverage.xml` | `setup-fireflies-coverage-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-founders-os-sweep` | daily 06:30 | `tasks/cowork-cora-founders-os-sweep.xml` | `setup-founders-os-sweep-task.ps1` | Highest / Interactive | yes | UNROWED |
| `cowork-cora-gap-autofill` | daily 06:10 | `tasks/cowork-cora-gap-autofill.xml` | `setup-gap-autofill-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-gap-digest` | weekly Mon 08:00 | `tasks/cowork-cora-gap-digest.xml` | (none -- manifest XML only) | Limited / Interactive | **no** | disabled |
| `cowork-cora-health-check` | daily 08:45 | `tasks/cowork-cora-health-check.xml` | (none -- manifest XML only) | Limited / Interactive | **no** | UNROWED |
| `cowork-cora-hubspot-email-sync` | every PT1H (from 2026-05-31T23:23) | `tasks/cowork-cora-hubspot-email-sync.xml` | (none -- manifest XML only) | Limited / Interactive | **no** | disabled |
| `cowork-cora-hygiene-drive-weekly` | weekly Sat 02:40 | `tasks/cowork-cora-hygiene-drive-weekly.xml` | `setup-hygiene-drive-weekly-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-influencer-digest` | weekly Mon 08:20 | `tasks/cowork-cora-influencer-digest.xml` | `setup-influencer-digest-task.ps1` | Limited / Interactive | yes | disabled |
| `cowork-cora-influencer-overdue-alerts` | daily 09:10 | `tasks/cowork-cora-influencer-overdue-alerts.xml` | `setup-influencer-overdue-alerts-task.ps1` | Limited / Interactive | yes | disabled |
| `cowork-cora-influencer-scan` | every PT2H (from 2026-05-27T22:00) | `tasks/cowork-cora-influencer-scan.xml` | `setup-influencer-scan-task.ps1` | Limited / Interactive | yes | disabled |
| `cowork-cora-info-for-cora-sweep` | daily 06:05 | `tasks/cowork-cora-info-for-cora-sweep.xml` | `setup-info-for-cora-sweep-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-inventory-state-sync` | daily 06:20 | `tasks/cowork-cora-inventory-state-sync.xml` | `setup-inventory-state-sync-task.ps1` | Limited / Interactive | yes | enabled |
| `cowork-cora-kb-hygiene` | monthly day 1 15:00 | `tasks/cowork-cora-kb-hygiene.xml` | `setup-kb-hygiene-task.ps1` | Limited / Interactive | **no** | UNROWED |
| `cowork-cora-kb-sync-asana` | daily 03:00 | `tasks/cowork-cora-kb-sync-asana.xml` | `setup-kb-sync-tasks.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-kb-sync-drive` | daily 04:30 | `tasks/cowork-cora-kb-sync-drive.xml` | `setup-kb-sync-tasks.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-kb-sync-fireflies` | daily 03:30 | `tasks/cowork-cora-kb-sync-fireflies.xml` | `setup-kb-sync-tasks.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-kb-sync-gmail` | daily 02:30 | `tasks/cowork-cora-kb-sync-gmail.xml` | `setup-kb-sync-tasks.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-kb-sync-notion` | daily 05:00 | `tasks/cowork-cora-kb-sync-notion.xml` | `setup-kb-sync-tasks.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-kb-sync-slack` | daily 02:00 | `tasks/cowork-cora-kb-sync-slack.xml` | `setup-kb-sync-tasks.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-kb-sync-static` | daily 04:00 + daily 12:20 | `tasks/cowork-cora-kb-sync-static.xml` | `setup-kb-sync-tasks.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-knowledge-check-report` | weekly Mon 07:20 | `tasks/cowork-cora-knowledge-check-report.xml` | `setup-knowledge-check-report-task.ps1` | Limited / Interactive | **no** | UNROWED |
| `cowork-cora-knowledge-review` | weekly Mon,Tue,Wed,Thu,Fri 07:00 | `tasks/cowork-cora-knowledge-review.xml` | `setup-knowledge-review-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-lexicon-mining` | weekly Sun 17:50 | `tasks/cowork-cora-lexicon-mining.xml` | `setup-lexicon-mining-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-meeting-capture-audit` | daily 07:22 | `tasks/cowork-cora-meeting-capture-audit.xml` | `setup-meeting-capture-audit-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-meeting-capture-ensure` | every PT15M for PT14H stop-at-end (daily 06:07) | `tasks/cowork-cora-meeting-capture-ensure.xml` | `setup-meeting-capture-ensure-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-monthly-deliverables` | monthly day 1 09:00 | `tasks/cowork-cora-monthly-deliverables.xml` | (none -- manifest XML only) | Limited / Interactive | **no** | disabled |
| `cowork-cora-person-dossier-refresh` | weekly Sun 16:30 | `tasks/cowork-cora-person-dossier-refresh.xml` | `setup-person-dossier-refresh-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-pm-adoption-digest` | weekly Mon 08:20 | `tasks/cowork-cora-pm-adoption-digest.xml` | `setup-pm-adoption-digest-task.ps1` | Limited / Interactive | **no** | UNROWED |
| `cowork-cora-proactive-gaps` | daily 06:00 | `tasks/cowork-cora-proactive-gaps.xml` | (none -- manifest XML only) | Limited / Interactive | **no** | disabled |
| `cowork-cora-project-channel-sync` | daily 16:00 | `tasks/cowork-cora-project-channel-sync.xml` | `setup-project-channel-sync-task.ps1` | Limited / Interactive | yes | disabled |
| `cowork-cora-qbo-token-refresh` | daily 02:00 | `tasks/cowork-cora-qbo-token-refresh.xml` | `setup-qbo-token-refresh-task.ps1` | Highest / Interactive | yes | UNROWED |
| `cowork-cora-reconciliation` | daily 05:30 | `tasks/cowork-cora-reconciliation.xml` | `setup-kb-sync-tasks.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-security-monitor` | every PT15M (from 2026-05-27T22:33) | `tasks/cowork-cora-security-monitor.xml` | `setup-security-monitor-task.ps1` | Limited / Interactive | yes | UNROWED |
| `cowork-cora-service` | at logon | `tasks/cowork-cora-service.xml` | `setup-windows-task.ps1` | Limited / Interactive | yes | running |
| `cowork-cora-session-capture` | daily 05:15 + daily 12:30 | `tasks/cowork-cora-session-capture.xml` | `setup-session-capture-task.ps1` | Limited / Interactive | yes | UNROWED |
<!-- END GENERATED: scheduled-estate -->

### Verify the estate

```powershell
Get-ScheduledTask | Where-Object { $_.TaskName -like "cowork-cora-*" -or $_.TaskName -like "Cora - *" -or $_.TaskName -eq "cora-watchdog" } | Measure-Object
.venv\Scripts\python.exe scripts\generate_task_estate_manifest.py --diff-only
```

The count must match the manifest's `count`; `--diff-only` must print zero
`task-estate-drift` lines. Then run `C:\Users\Harri\code\pin-scheduled-task-models.ps1 -Apply`
ONLY if any Cowork (Claude desktop) task was registered -- none are, by this phase.

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

### Confirm the estate is live (the proofs `deployment/DR-MANIFEST.md` defines)

```powershell
# D08: heartbeat advancing (< 2 min old) and a NEW pid row in the instances ledger
Get-Item "C:\Users\Harri\code\cora\data\health\heartbeat.txt" | Select-Object LastWriteTime
Get-Content "C:\Users\Harri\code\cora\logs\cora-instances.jsonl" -Tail 1
# D13: the registry matches the committed manifest (zero drift)
.venv\Scripts\python.exe scripts\generate_task_estate_manifest.py --diff-only
# the whole probe table in one go
.venv\Scripts\python.exe scripts\dr_manifest_probes.py
```

(`cowork-cora-digest` is intent-DISABLED -- it never fires; do not wait for a digest file.)

### Tomorrow morning

After 08:45 AZ, confirm the nightly ingest set fired and the health check ran:

```powershell
Get-ChildItem "C:\Users\Harri\code\cora\logs\tasks\" -Filter "cowork-cora-kb-sync-*-$(Get-Date -Format yyyy-MM-dd).log"
Get-ChildItem "C:\Users\Harri\code\cora\logs\tasks\" -Filter "cowork-cora-health-check-$(Get-Date -Format yyyy-MM-dd).log"
```

Both should list files (one per kb-sync door + the 08:45 health check). The Monday digest in
#cora-health carries the `Task estate: N live / N manifest | drift none` line.

---

## What if something goes wrong?

| Symptom | Most likely cause | Fix |
|---|---|---|
| `python.exe -m cora.main` exits silently | `.env` has `REPLACE_ME_*` still or wrong prefix | Re-check .env values |
| `.venv\Scripts\cora.exe` refuses to launch / "blocked by policy" | WDAC code-integrity policy blocks the console script (DR-MANIFEST D06) | Never use `cora.exe`; the action is `python.exe -m cora.main` (the manifest XML already carries it) |
| `register-estate-from-manifest.ps1` prints WARNING about the time zone | the new host's zone differs from the manifest's `host_time_zone` | `Set-TimeZone "<manifest host_time_zone>"` then re-run; clock triggers are host-local |
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
- **Every non-Slack/Anthropic identity and credential (Google SA + DWD, cora@hjrglobal.com, Asana PATs, Fireflies, HubSpot, QBO, ...)** -- see the Identity inventory in `deployment/runbook.md` (sections "Identity inventory", "Rotation: Asana PAT (Cora)", "Provisioning: cora@hjrglobal.com", "Provisioning: Google service account + DWD"); restored from the encrypted secrets bundle (`restore_secrets.py`), never regenerated here -- PREREQUISITE: the bundle decrypts ONLY with `CORA_BACKUP_PASSPHRASE`, which lives in the password manager (and as a User-scope env var on the old host), NOT in `.env` and NOT in the bundle; fetch it from the password manager FIRST or nothing below restores.

---

## Sanity check questions to ask before declaring bootstrap complete

- [ ] The Cora-named task count equals the manifest `count` (`Get-ScheduledTask | Where-Object { $_.TaskName -like "cowork-cora-*" -or $_.TaskName -like "Cora - *" -or $_.TaskName -eq "cora-watchdog" } | Measure-Object`) and `generate_task_estate_manifest.py --diff-only` prints ZERO `task-estate-drift` lines
- [ ] `scripts\dr_manifest_probes.py` shows FAIL = 0 (MANUAL / UNMEASURED rows are expected and listed)
- [ ] `cowork-cora-service` is `Running` and `logs\cora-instances.jsonl` carries a NEW pid row from this host
- [ ] `@Cora ping` in `#cora-build` produces a threaded reply within 10 seconds
- [ ] `data\health\heartbeat.txt` is under 2 minutes old
- [ ] Anthropic console shows the new API key is the only active one (old one revoked if relevant)
- [ ] Slack app config shows the new tokens are the only active ones (old ones revoked if relevant)
- [ ] `.env` is NOT showing as modified or untracked in `git status` (gitignored correctly)

If all 8 pass, the bot estate is back online. **The KB and the full RTO are separate: see
`deployment/DR-MANIFEST.md` §3 (two restore paths, both UNMEASURED until the VM drill).**
