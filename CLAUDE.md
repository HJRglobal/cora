# Cora — Code Session Context

This file is the authoritative startup read for every Code session.
Read this first, then check `decisions.md` for the full decision log.
TOM entries are newest-first. Do not edit past TOM entries.

---

## TOP OF MIND (TOM)

### [F3E] Influencer Tracking Phase 2 + Fighter Roster Live -- 2026-06-03 (commits ad9c94d, 8d9222d + this session)

**57 F3 sponsored fighters are now seeded and June deliverables are running.**

Fighter roster:
- Source: Google Sheet 1oFmiSVbPMLOMdpjsUBOG_SGp00a9xzTrUCVNuyb0_kA
- Seeded via scripts/seed_fighters.py (idempotent -- safe to re-run)
- Excluded: Malik Besseck (no IG), Jovan Ravago (TikTok only),
  Louie Lopez / Taquel Young (date in handle column), gym accounts

June 2026 deliverables: 186 rows, due 2026-06-30
- 2 IG stories + 1 IG post per active fighter
- Requirements: tag @f3energy + use #DrinkF3

Schema migration (backward-compatible, runs on every DB connection open):
- `campaign_month` + `requirements` columns added to influencer_deliverables

Hashtags now monitored on F3 Energy account: #F3Energy, #DrinkF3Energy, #DrinkF3
- data/maps/brand-social-accounts.yaml updated

Scheduled tasks (all registered, all smoke-tested):

| Task | Schedule | Script |
|---|---|---|
| cowork-cora-influencer-scan | Every 2h | scripts/run_influencer_scan.py |
| cowork-cora-monthly-deliverables | 1st of month, 9 AM AZ | scripts/generate_monthly_deliverables.py |
| cowork-cora-influencer-overdue-alerts | Daily 9 AM | scripts/run_influencer_overdue_alerts.py |
| cowork-cora-influencer-digest | Monday 8 AM | scripts/run_influencer_digest.py |

Phase 2 tools (built this Cowork session):
- `influencer_complete_deliverable` -- Alex types "@Cora complete deliverable 5",
  no confirm gate (D-023), auto-posts summary to #f3-athletes (C0B6GT3117Y)
- Weekly digest: Mondays, overdue / due-this-week / completed-since-Monday
- Instagram auto-match: scanner proposes match to open deliverable via 👍/👎
  reaction pattern; pending in data/influencer_pending_matches.json
- Overdue alerts: daily DM to Alex (U0B3VGWJTMJ), 72h throttle via
  overdue_alert_log table in influencer_tracker.db

No HubSpot involved -- tracking lives entirely in SQLite + Slack.

Deferred:
- Recurring deliverable templates (auto-generate next month)
- TikTok monitoring (scaffold in tiktok_monitor.py, pending API approval)
- Bulk HubSpot->influencer import
- Update #f3-sales detection message to also use 👍/👎 pattern

---

### [INFRA] Security Infrastructure Activated -- 2026-05-28 (commit 32a9d62)

Confirmed live: BitLocker (C: 100% encrypted), Windows Firewall (all 3 profiles),
RDP disabled, logon auditing on, cowork-cora-security-monitor every 15 min -> #cora-security.

Remaining: Windows Defender real-time protection was disabled (Asana task created).

---

## KEY IDS AND CONSTANTS

```
HJR Slack workspace
  Harrison (founder):       U02P3D6AT2C
  Alex Cordova (F3E ops):   U0B3VGWJTMJ
  #f3-athletes channel:     C0B6GT3117Y
  #f3e-sales channel:       (name: f3e-sales)

HubSpot
  Portal ID:                246351746
  F3E Retail pipeline:      2313722582
  UFL/OSN/BDM pipeline:     default

Instagram
  F3 Energy IG user ID:     17841448560031091
  Token env var:            INSTAGRAM_F3E_ACCESS_TOKEN

Google Sheet (fighter roster source):
  1oFmiSVbPMLOMdpjsUBOG_SGp00a9xzTrUCVNuyb0_kA
```

---

## REPO STRUCTURE (key paths)

```
src/cora/
  app.py                   -- Slack bolt app, all event handlers, reaction dispatch
  tools/
    tool_dispatch.py       -- Tool catalog + _TOOL_FUNCTIONS registry + dispatch()
    influencer_client.py   -- SQLite tracker (influencer_tracker.db), pending match store
    hubspot_client.py      -- HubSpot API (portal 246351746)
    financial_client.py    -- Google Sheets cash flow reader
    qbo_client.py          -- QuickBooks Online connector
    [others]               -- asana, calendar, gmail, notion, ads, lex, etc.
  connectors/
    instagram_monitor.py   -- IG Graph API polling
    hubspot_email_sync.py  -- Gmail->HubSpot thread sync
    [others]

scripts/
  run_influencer_scan.py         -- Every 2h, detects posts, proposes matches
  run_influencer_digest.py       -- Monday 8 AM compliance digest
  run_influencer_overdue_alerts.py  -- Daily 9 AM overdue DMs
  generate_monthly_deliverables.py  -- 1st of month, creates 186 deliverables
  seed_fighters.py               -- One-time seeder, idempotent
  run_channel_sweep.py           -- Nightly org-wide Slack sweep
  run_knowledge_review.py        -- Mon-Fri, sends Harrison pending KB DMs
  run_linkedin_spy.py            -- Monday 8 AM, Apollo.io prospect scan

data/
  influencer_tracker.db          -- SQLite: handles, deliverables, detections, alerts
  influencer_pending_matches.json -- Pending 👍/👎 auto-match proposals
  maps/
    slack-to-asana.yaml
    slack-to-hubspot.yaml
    user-aliases.yaml
    brand-social-accounts.yaml   -- IG accounts + hashtags to monitor
  health/heartbeat.txt           -- Liveness: updated every 60s

design/
  system-prompts/{entity}.md     -- Per-entity Slack system prompts
  channel-routing.yaml           -- channel name -> entity mapping
  cora-constitution.md           -- Core operating principles

deployment/
  runbook.md                     -- Task registry, ops procedures, failure modes
  setup-*.ps1                    -- One-per-task registration scripts
```

---

## DOCTRINES (apply to all new code)

1. **Staged-write gate** -- All write tools show preview + require confirmed=True
   before executing. Exception: `influencer_complete_deliverable` (D-023).

2. **load_dotenv** -- Always `load_dotenv(_REPO_ROOT / ".env", override=True)`.
   Never setdefault() for required config vars (D-021).

3. **Task Scheduler** -- Absolute `.venv\Scripts\python.exe` paths only.
   Never `uv` in scheduled tasks. WorkingDirectory = repo root.

4. **PS1 files** -- ASCII-only. No em-dashes, curly quotes, or any char > 127.
   PowerShell 5.1 reads UTF-8 as Windows-1252 by default (D-016).

5. **Restart sequence** -- Stop-ScheduledTask -> WMI/Get-Process kill ->
   Start-Sleep 3 -> Start-ScheduledTask. Stop alone does NOT kill python.exe.

6. **Import smoke test** -- Before every commit:
   `.venv\Scripts\python.exe -c "from src.cora.app import app"`

7. **Harrison-sole-authority** -- Cora never auto-writes to decisions.md, Asana,
   or HubSpot without Harrison 👍 on a knowledge-review DM (D-011, LOCKED).

8. **PHI guard** -- phi_guard.py is the single source of truth. Influencer
   feature has PHI guard OFF (no health data involved).

9. **No Add-Content for Python** -- Use Write/Edit tools only. Add-Content
   converts quotes to smart quotes, breaking syntax (D-022, LOCKED).

10. **Entity scoping** -- Tools filter by entity (F3E, OSN, LEX, etc.) from
    channel routing. FNDR channels see all entities (no filter).

---

## SCHEDULED TASKS (full registry as of 2026-06-03)

| Task name | Schedule | Script |
|---|---|---|
| cowork-cora-service | AtLogon + RestartOnFailure | cora.main (bot process) |
| cowork-cora-channel-sweep | Daily 01:30 AZ | run_channel_sweep.py |
| cowork-cora-knowledge-review | Mon-Fri 07:00 AZ | run_knowledge_review.py |
| cowork-cora-daily-briefing | Daily (see PS1) | run_daily_briefing.py |
| cowork-cora-backup | Daily 04:30 AZ | backup_logs.py |
| cowork-cora-influencer-scan | Every 2h | run_influencer_scan.py |
| cowork-cora-influencer-digest | Monday 08:00 AZ | run_influencer_digest.py |
| cowork-cora-influencer-overdue-alerts | Daily 09:00 AZ | run_influencer_overdue_alerts.py |
| cowork-cora-monthly-deliverables | 1st of month 09:00 AZ | generate_monthly_deliverables.py |
| cowork-cora-security-monitor | Every 15 min | run_security_monitor.py |
| cowork-cora-qbo-token-refresh | (see PS1) | qbo_token_refresh.py |
| Cora - Email Attachment Filer | Every 4h | run_attachment_filer.py |
| Cora - LinkedIn Spy | Monday 08:00 | run_linkedin_spy.py |

> Apollo trial expires 2026-06-10. Upgrade before 2026-06-07 at https://app.apollo.io/#/settings/billing

---

## ACTIVE DECISIONS (summary -- full entries in decisions.md)

| # | Decision |
|---|---|
| D-005 | Task Scheduler: .venv python, not uv |
| D-008 | HubSpot portal 246351746 (old 243870963 retired) |
| D-011 | Harrison-sole-authority for all memory writes (LOCKED) |
| D-016 | PS1 files: ASCII-only (LOCKED) |
| D-021 | conftest: os.environ["K"] = ... or "fallback", never setdefault (LOCKED) |
| D-022 | Never Add-Content for Python source (LOCKED) |
| D-023 | influencer_complete_deliverable: no staged-write gate |
| D-024 | Monthly deliverables auto-generated 1st of month, 57 fighters, 3 each |
