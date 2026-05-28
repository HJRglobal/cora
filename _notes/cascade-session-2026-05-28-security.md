# Cascade — 2026-05-28 Infrastructure Security Session

_Paste blocks ready for Drive files. Branch: `claude/cora-infrastructure-security-JAQQQ`._

---

## 1. decisions.md APPEND (paste into Drive decisions.md under `## 2026-05 decisions`)

```markdown
### 2026-05-28 [INFRA] — Cora Infrastructure Security System BUILT (pending Harrison activation)

- **What was built**: Full security monitoring layer for Cora's Windows desktop deployment, plus a 4-stage infrastructure growth roadmap.
- **Security monitor** (`scripts/security_monitor.py`): Runs every 15 min via Task Scheduler. Scans Cora log files for auth failures, restart loops, API error spikes, and repeated 403s. Checks SHA-256 file integrity of `.env`, `app.py`, `claude_client.py`, `config.py`, pre-commit hook, and the main deployment task script. Queries Windows Event Log for failed login attempts (Win32 only). Sends Slack alert to `SECURITY_ALERT_CHANNEL` with 1-hour per-event dedup suppression. Alert history at `data/security/alert_history.json`; integrity baseline at `data/security/file_hashes.json` (both gitignored).
- **Windows hardening check** (`deployment/check-windows-security.ps1`): Read-only posture audit covering Defender, BitLocker, Firewall (all 3 profiles), Remote Desktop, SMBv1, audit policy, .env file permissions, API budget alert reminders, and scheduled task health. Run as Administrator.
- **Task Scheduler setup** (`deployment/setup-security-monitor-task.ps1`): Registers `cowork-cora-security-monitor` task — fires at logon + repeats every 15 min indefinitely. Also runs `--init` to seed the integrity baseline.
- **Growth roadmap** (`SECURITY.md`): 4-stage path — Stage 1 (desktop, now), Stage 2 (Cloudflare Tunnel + secrets vault + S3 backups, ~$20-40/mo), Stage 3 (Docker + EC2 + CI/CD, ~$50-120/mo), Stage 4 (Kubernetes + managed vector DB, ~$200-500/mo). Stage transition triggers are explicitly defined. Full incident response playbook and monthly security checklist included.
- **Pre-commit hook extended**: Added Instagram Graph API token patterns (`IGQV*`, `EAAG*`) to the secret scanner.
- **New env var**: `SECURITY_ALERT_CHANNEL` — controls where security alerts post (default: `cora-build`; recommended: `cora-security` private channel).
- **`httpx` used** (not `requests`) — already in dependencies; no pyproject.toml change needed.
- **Guardrail fixed**: `.credentials/` and `data/security/` both gitignored; integrity baseline never committed.
- **June 11 review scheduled**: Asana task + Google Calendar event (10am AZ) to review stage transition readiness.
- **Harrison activation steps (in order)**:
  1. `git fetch origin && git checkout claude/cora-infrastructure-security-JAQQQ` (or merge to main)
  2. Create `#cora-security` private Slack channel; add `SECURITY_ALERT_CHANNEL=cora-security` to `.env`
  3. `powershell -ExecutionPolicy Bypass -File deployment\setup-security-monitor-task.ps1` (registers task + seeds baseline)
  4. `powershell -ExecutionPolicy Bypass -File deployment\check-windows-security.ps1` (as Admin — fix all FAILs)
  5. Enable 2FA on: Anthropic, Slack, GitHub, Google, Intuit (QBO), HubSpot
  6. Set hard spending caps: Anthropic $200/mo, OpenAI $50/mo
  7. Smoke test: `uv run python scripts\security_monitor.py --dry-run`
- **Source**: Claude Code session 2026-05-28, branch `claude/cora-infrastructure-security-JAQQQ`, commit `783669c`.
```

---

## 2. Cora CLAUDE.md TOM UPDATE (paste as the NEW top section, above the current top ✅ entry)

```markdown
## ✅ [INFRA] Security Monitor + Growth Roadmap BUILT — 2026-05-28 (commit `783669c`)

**What shipped:**
- `scripts/security_monitor.py` — 15-min security scan: log analysis + file integrity + Windows failed-login check + Slack alert with 1h dedup
- `deployment/setup-security-monitor-task.ps1` — registers `cowork-cora-security-monitor` Task Scheduler job + seeds file-integrity baseline
- `deployment/remove-security-monitor-task.ps1` — clean teardown
- `deployment/check-windows-security.ps1` — read-only Windows security posture audit (Defender, BitLocker, Firewall, RDP, SMBv1, audit policy, .env perms)
- `SECURITY.md` — 4-stage infrastructure growth path + threat model + incident response playbook + monthly checklist
- Pre-commit hook: added Instagram token patterns (`IGQV*`/`EAAG*`)
- `.env.example`: documented `SECURITY_ALERT_CHANNEL`
- `.gitignore`: added `data/security/` exclusion

**New scheduled task:** `cowork-cora-security-monitor` (every 15 min, AtLogOn trigger) — NOT yet deployed on desktop

**New Slack channel needed:** `#cora-security` (private, Harrison + Cora only) — NOT yet created

**New env var:** `SECURITY_ALERT_CHANNEL=cora-security`

**Harrison activation (5 Asana tasks created, due 2026-05-30/31):**
1. ⬜ Deploy security monitor (run `setup-security-monitor-task.ps1`)
2. ⬜ Run Windows security check + fix all FAILs
3. ⬜ Enable 2FA on all API providers + set spending caps
4. ⬜ Create `#cora-security` channel + set env var
5. ⬜ One-time token rotation (see runbook.md)

**14-day review:** June 11, 2026 10am AZ — Asana task + calendar event set. Goal: decide Stage 1→2 transition.

**Guardrail flagged (see below):** `requests` not in deps → fixed to `httpx`.
```

---

## 3. New Scheduled Task Surface

| Task name | Trigger | What it does |
|---|---|---|
| `cowork-cora-security-monitor` | AtLogOn + every 15 min | Scans logs, checks file integrity, Windows failed logins, Slack-alerts on anomalies |

Existing tasks (unchanged):
- `cowork-cora-service` — Cora bot (continuous)
- `cowork-cora-digest` — daily knowledge gap digest (5am AZ)
- `cowork-cora-backup` — daily log backup (4:30am AZ)
- `cowork-cora-kb-sync-notion` — nightly Notion KB sync
- `cowork-cora-kb-sync-slack` — nightly Slack KB sync (Component 1, ambient awareness)
- `cowork-cora-kb-sync-gmail` — nightly Gmail sweep (Component 2)
- `cowork-cora-knowledge-review` — Mon-Fri 7am Harrison DM with 👍/👎 proposals
- `cowork-cora-reconciliation` — 5:30am reconciliation engine (Component 3)

---

## 4. New Slack Channels

| Channel | Type | Purpose |
|---|---|---|
| `#cora-security` | Private (to create) | Security monitor alerts — Harrison + Cora only |

---

## 5. New Doctrines

- **Security-first before expansion**: BitLocker + 2FA + spending caps + security monitor must be active before moving to Stage 2.
- **4-stage infrastructure growth path**: Triggers are condition-based, not time-based. Stage 1→2 trigger: machine goes offline while Harrison is away. Stage 2→3 trigger: real-time financial data + 3+ entities + remote deploy needed.
- **File integrity monitoring**: Any change to `.env`, `app.py`, `claude_client.py`, `config.py`, or the pre-commit hook fires a HIGH alert. Legitimate changes must re-initialize the baseline with `--init`.
- **Alert dedup doctrine**: Same security event suppressed for 1 hour. Reset by deleting `data/security/alert_history.json`.
- **Instagram tokens now guarded**: `IGQV*` and `EAAG*` prefixes blocked in pre-commit alongside existing patterns.

---

## 6. Guardrails Flagged

| Issue | Status |
|---|---|
| `requests` used in security_monitor.py but not in dependencies | **FIXED** — swapped to `httpx` (already in deps) |
| `data/security/` not in `.gitignore` | **FIXED** — added to `.gitignore` |
| Instagram API tokens (`IGQV*`/`EAAG*`) not in pre-commit hook | **FIXED** — added |
| `.credentials/` directory contains unencrypted private keys + QBO tokens | **OPEN** — mitigated by BitLocker (Step 4 of activation). Long-term fix: move to AWS Secrets Manager at Stage 3. |
| QBO OAuth redirect uses localhost HTTP — no HTTPS | **OPEN / ACCEPTABLE** — Intuit specifically allows http://localhost for dev convenience. Document in runbook that localhost is intentional per Intuit docs. |
| Security monitor itself has no self-watchdog (if the Task dies silently, no alert fires) | **OPEN** — Stage 2 fix: add UptimeRobot external monitor + `/health` endpoint. |
