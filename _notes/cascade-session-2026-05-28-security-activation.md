# Cascade — 2026-05-28 Security Activation (continuation)

_Continuation of `cascade-session-2026-05-28-security.md`. Branch merged to main at commit `32a9d62`._

---

## 1. decisions.md APPEND (paste under `## 2026-05 decisions` in Drive)

```markdown
### 2026-05-28 [INFRA] — Security Infrastructure Activated on Desktop (Stage 1 complete minus Defender)

- **Activation results**: Security monitor deployed and running. BitLocker (C: 100% encrypted, TPM), Windows Firewall (all 3 profiles), RDP disabled, logon failure auditing all confirmed PASS. Smoke test clean: no issues found.
- **One FAIL remaining**: Windows Defender real-time protection was disabled — Asana task created (due 2026-05-29).
- **Task errors noted**: `cowork-cora-influencer-scan` last result 2147942402 (access denied — investigate separately); `cowork-cora-qbo-token-refresh` last result 1 (QBO token expired, expected if QBO inactive).
- **PS1 encoding rule (new doctrine)**: All PowerShell scripts in this repo must use ASCII-only characters. Em-dash (U+2014, UTF-8 bytes E2 80 94) silently corrupts to `â€"` when PowerShell reads UTF-8 files as Windows-1252 (default on US systems). Any future PS1 file with non-ASCII characters will break silently the same way. Fixed in commit `896de67`.
- **PS 5.1 trigger pattern (new doctrine)**: `New-ScheduledTaskTrigger -AtLogOn` does not support direct `.RepetitionInterval` property assignment in PowerShell 5.1. Use `-Once -RepetitionInterval (New-TimeSpan -Minutes N)` + `-StartWhenAvailable` in settings instead. This matches all other Cora task registrations. Fixed in commit `45a3419`.
- **SECURITY_ALERT_CHANNEL**: Updated in `.env` to `cora-security` (private Slack channel). Security monitor restarted to pick up new value.
- **Email Attachment Filer task**: Re-registered from elevated PowerShell with correct uv path (`C:\Users\Harri\.local\bin\uv.exe`).
- **Asana task cleanup**: Marked "Deploy security monitor" and "Create #cora-security channel" complete. Created new tasks: Defender FAIL (due 2026-05-29), Slack app reinstall (due 2026-05-31).
- **Source**: Claude Code session 2026-05-28 continuation, commits `896de67`, `45a3419`, `32a9d62` (merge to main).
```

---

## 2. Cora CLAUDE.md TOM UPDATE (paste as NEW top entry, above the previous security entry)

```markdown
## ✅ [INFRA] Security Infrastructure Activated — 2026-05-28 (commit `32a9d62`, on main)

**What's confirmed live on the desktop:**
- `cowork-cora-security-monitor`: running every 15 min, alerts → `#cora-security`, smoke test clean
- BitLocker C: 100% encrypted, Protection On, TPM + Numerical Password
- Windows Firewall: Domain / Private / Public — all enabled
- Remote Desktop: disabled
- Logon failure auditing: Success and Failure enabled
- Email Attachment Filer task: re-registered with correct uv path (elevation required)

**One FAIL remaining:** Windows Defender real-time protection disabled — Asana task due 2026-05-29

**New PS1 doctrines (apply to all future PowerShell scripts):**
- ASCII-only: no em-dashes, arrows, or any non-ASCII Unicode — they corrupt on Windows-1252 systems
- Scheduled task trigger: use `-Once -RepetitionInterval (New-TimeSpan -Minutes N)` not property assignment

**Pending browser/Slack actions (not yet done):**
- Slack app reinstall for new OAuth scopes (due 2026-05-31)
- 2FA: Anthropic, Slack, GitHub, Google, Intuit, HubSpot (due 2026-05-31)
- Spending caps: Anthropic $200/mo, OpenAI $50/mo (due 2026-05-31)

**14-day infrastructure review:** June 11, 2026 10am AZ
```

---

## 3. Asana Task Status

| Task | Action | Due |
|---|---|---|
| Deploy security monitor | ✅ Marked complete | — |
| Create #cora-security + set env var | ✅ Marked complete | — |
| Fix Windows Defender real-time protection | 🆕 Created | 2026-05-29 |
| Reinstall Cora Slack app for new OAuth scopes | 🆕 Created | 2026-05-31 |
| Enable 2FA + set spending caps | Still open | 2026-05-31 |
| Cora Infrastructure Review (Stage 1→4) | Still open | 2026-06-11 |

---

## 4. New Doctrines

- **PS1 ASCII-only rule**: No em-dashes, right-arrows, or any non-ASCII Unicode in PowerShell scripts committed to this repo. Windows PowerShell 5.1 (default on Windows 10/11) reads files as Windows-1252 unless a BOM is present. UTF-8 non-ASCII bytes silently corrupt.
- **Scheduled task trigger pattern**: Use `-Once -RepetitionInterval (New-TimeSpan -Minutes N)` + `StartWhenAvailable` in settings. Never assign `.RepetitionInterval` / `.RepetitionDuration` as properties on a trigger object — not supported in PS 5.1.
- **Elevated re-registration required**: Any Task Scheduler task that runs uv must be re-registered from an elevated prompt after uv path changes or PATH updates. Non-elevated registration silently uses a different PATH context.

---

## 5. Guardrails Flagged

| Issue | Status |
|---|---|
| `cowork-cora-influencer-scan` last result 2147942402 (access denied) | **OPEN** — investigate when influencer scan is next needed; likely a credential or path issue |
| `cowork-cora-qbo-token-refresh` last result 1 (exit code 1) | **ACCEPTABLE** — QBO token expired; expected if QBO is not actively used. Rotate when QBO integration goes live. |
| Windows Defender real-time protection disabled | **OPEN** — Asana task due 2026-05-29. Top priority before next Code session. |
| Slack app not reinstalled for new scopes | **OPEN** — Asana task due 2026-05-31. Required for ambient awareness features (reactions:read, im:history). |
