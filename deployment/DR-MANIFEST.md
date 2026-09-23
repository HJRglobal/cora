# Cora DR MANIFEST — bare-metal rebuild of the bot estate

**Status:** BASELINE ONLY. This manifest was probed on the OFFICE host (the machine it
describes) on 2026-09-23; that proves the manifest matches reality, **not** that a restore
works. The first real restore drill is VM step 2 (charter D1: stand-up as sandbox + warm
standby) — the migration validates the manifest. Nobody should read "manifest built" as
"restore proven".

**What this covers:** the *bot estate* — the always-on Python service (`python.exe -m
cora.main`), the ~96 Windows Task Scheduler tasks (`cowork-cora-*`, `Cora - *`,
`cora-watchdog`), the Google Drive for Desktop `G:` mount, healthchecks.io, the KB, the
encrypted secrets bundle. **Not covered:** the *Cowork estate* (scheduled tasks inside the
Claude desktop app) — it stays on the office machine (charter D4, ruled 2026-09-01), see
D21. The Founder OS on Drive, Slack, Anthropic billing and every SaaS identity are
cloud-side and survive a local loss (see `bootstrap-new-machine.md` "What's NOT covered").

**Rules this file obeys:** secret VALUES never appear here — key NAMES only (charter D3;
`scripts/secrets_scan.py` gates every commit). LEX partitions and folders are named as
identifiers only (D-145). Nothing here authorizes a provisioning action, a spend, a
credential move or a canon write.

**Companion files:**
- `deployment/manifest/task-estate.json` / `.md` — the scheduled estate, derived from the
  live registry by `scripts/generate_task_estate_manifest.py` (M1). Referenced, not duplicated.
- `deployment/manifest/dr-manifest.json` — the machine-readable twin of the table below,
  written by `scripts/dr_manifest_probes.py --update-docs`.
- `deployment/bootstrap-new-machine.md` — the step-by-step rebuild (Phases 0–7); this
  manifest is its checklist and its proof.
- `deployment/kb-rebuild.md` — KB restore path (b).
- `deployment/runbook.md` — operating procedures, identity inventory, rotations.

---

## 1. How to read the table

Each item carries **source of truth · restore step · probe result on this host · owner ·
RTO**. Statuses: `PASS` (the probe found the item as described), `FAIL` (it did not — fix
the host or the manifest), `MANUAL` (cannot be probed read-only; the owner does the step by
hand), `UNMEASURED` (an RTO nobody has timed — step 2 measures it), `INFO` (a cross-reference,
nothing to restore). Owners: **Harrison** (his hand, elevated where noted), **Justin** (the
Sept cost checkpoint + the RTO sign-off), **Cora-script** (a repo script does it; a human
starts it).

Regenerate the block with:

```powershell
.venv\Scripts\python.exe scripts\dr_manifest_probes.py --update-docs
```

(from a worktree: add `--live-root C:\Users\Harri\code\cora`). Hand edits inside the
markers are overwritten.

## 2. Probe baseline (generated — do not hand-edit)

<!-- BEGIN GENERATED: dr-probe-baseline -->
_Probe baseline 2026-09-23 AZ on `H-REMOTE` by `scripts/dr_manifest_probes.py` (live root `C:\Users\Harri\code\cora`): **PASS 19 / FAIL 0 / MANUAL 2 / UNMEASURED 1 / INFO 2** of 24 items. Machine-readable twin: `deployment/manifest/dr-manifest.json`. Regenerate with `--update-docs`; do not hand-edit._

| # | Item | Source of truth | Restore step | Probe (this host) | Owner | RTO |
|---|---|---|---|---|---|---|
| D01 | Repo + branch (`main`) + remote | GitHub HJRglobal/cora; local `C:\Users\Harri\code\cora` | `git clone <origin> C:\Users\Harri\code\cora`; checkout `main` at the recorded hash | PASS -- live checkout HEAD e46bd61 on claude/deposco-push-write-path-2026-09-22; origin set | Harrison | ~5 min |
| D02 | Dependency lock (`uv.lock` incl. `mcp`) | `pyproject.toml` + `uv.lock` in the repo | `uv sync` (dev: `uv sync --group dev`); the MCP server deps ride the lock since M2 | PASS -- uv.lock present; mcp locked | Harrison | ~10 min |
| D03 | Python 3.12 + `.venv` (+ `pythonw.exe`) | `.venv\Scripts\python.exe` (uv-managed venv at the repo root) | install Python 3.12 + uv (bootstrap Phase 1), `uv sync`; every task action hardcodes `.venv\Scripts\python.exe`/`pythonw.exe` | PASS -- Python 3.12.10; pythonw.exe present | Harrison | ~15 min |
| D04 | `.env` SCHEMA (key NAMES only, never values) | `.env.example` (every key read under src/, guarded by tests/test_env_example_coverage.py); live `.env` at the repo root (gitignored) | restore the live `.env` from the encrypted secrets bundle (D05); NEVER retype values from a doc; check for duplicate keys before the first start (2026-06-11 HEALTH_PING_URL incident) | PASS -- 73 keys live; 7 active example keys missing in live (HEALTH_BIND, HEALTH_PING_INTERVAL_S, HEALTH_PORT, POLAR_CLIENT_ID, POLAR_CLIENT_SECRET, POLAR_VIEW_ID); 23 live-only keys (documented as commented in .env.example or undocumented); no duplicate keys | Harrison | ~15 min |
| D05 | Encrypted secrets bundle (`secrets-YYYY-MM-DD.enc` = `.env` + the Google SA JSON) | daily `backup_logs.py` -> Drive `backups/<date>/secrets-*.enc`; passphrase `CORA_BACKUP_PASSPHRASE` in the password manager (+ User-scope env var on the old host) | fetch the passphrase from the password manager FIRST; `restore_secrets.py <secrets-*.enc>` -> `.env` + `cora-calendar-sa.json` | PASS -- newest 2026-09-22/secrets-2026-09-22.enc (27420 B, 12.7 h old); restore_secrets.py present; passphrase env var visible to this process | Harrison | ~10 min (after the passphrase) |
| D06 | WDAC / code-integrity posture | Windows Device Guard policy on the host (policy files under `C:\Windows\System32\CodeIntegrity`); service action `python.exe -m cora.main` (the console-script `cora.exe` is BLOCKED) | on a new host: either reproduce the policy (export the .cip set) or run without WDAC; either way register the service as `-m cora.main`, never `cora.exe` | PASS -- code-integrity policy enforced (kernel 2, user-mode 2); service runs `python.exe -m cora.main`; cora.exe present in the venv but blocked by policy -- never the action | Harrison | MANUAL |
| D07 | Service task `cowork-cora-service` (always-on bot) | `deployment/setup-windows-task.ps1` (AtLogon, RestartOnFailure 999 x PT1M, windowless via run_hidden) | run the setup script (elevated), then `deployment\restart-cora.ps1`; proof = a NEW pid row in `logs/cora-instances.jsonl` | PASS -- state Running; child `C:\Users\Harri\code\cora\.venv\Scripts\python.exe -m cora.main`; windowless True; trigger at logon; restart 999 x PT1M; run-as Interactive/Limited | Cora-script (Harrison runs) | ~5 min |
| D08 | Live bot health (heartbeat + instances ledger) | `data/health/heartbeat.txt` (60 s) + `logs/cora-instances.jsonl` | start the service task; heartbeat must advance within 2 min | PASS -- heartbeat 5 s old; pid 5384 started 2026-09-20T05:44:15Z | Cora-script | ~2 min |
| D09 | `cora-watchdog` (5-min heartbeat watchdog, RunLevel Highest) + `restart-cora.ps1` | `deployment/setup-cora-watchdog-task.ps1` + `deployment/cora-watchdog.ps1` | run the setup script (elevated); verify a tick line in `logs/tasks/cora-watchdog-<date>.log` | PASS -- Ready; every PT5M (from 2026-07-16T10:27); run level Highest; restart-cora.ps1 present; last result 0 | Cora-script (Harrison runs) | ~5 min |
| D10 | healthchecks.io dead-man ping (`HEALTH_PING_URL`) | the ping URL is a SECRET-shaped `.env` key (restored with D05); the check itself lives in the healthchecks.io account | after D05 + D07 the bot pings every 5 min; confirm the check flips UP in the dashboard (manual read) | PASS -- HEALTH_PING_URL set (key present; value never read); 'ping failed' lines in the last two bot logs: 0; the healthchecks.io dashboard state is a manual read | Harrison | ~5 min |
| D11 | Google Drive for Desktop + the `G:` mount (Founder OS) | Drive for Desktop signed in as harrison@hjrglobal.com, mounted at `G:`; `G:\My Drive\HJR-Founder-OS` | install Drive for Desktop, sign in, set the mount letter to G:, wait for the tree to stream; the bot degrades 14/14 entity contexts without it (9/9 incident) | PASS -- G:\My Drive\HJR-Founder-OS mounted; GoogleDriveFS pid 12012 | Harrison | ~30 min + initial sync UNMEASURED |
| D12 | Pinned KB-excluded Drive folder ids | `src/cora/kb_exclusions.KB_EXCLUDED_FOLDER_IDS` (code; incl. the `_shared/projects/cora` parent pin, the Computers roots, the personal/finance/LEX pins) | nothing to restore -- they ship with the repo; re-verify the ids still resolve after any Drive restructure | PASS -- 10 pinned Drive folder ids in cora.kb_exclusions.KB_EXCLUDED_FOLDER_IDS (identifiers only; the doc lists them) | Cora-script | n/a |
| D13 | Scheduled estate (96 tasks) = `deployment/manifest/task-estate.json` | the live Task Scheduler registry, derived by `scripts/generate_task_estate_manifest.py` (M1) | bootstrap Phase 5: every `setup-*.ps1`, then `rewrap-tasks-hidden.ps1 -Apply`; tasks without a setup script re-register from the manifest JSON; `--diff-only` must print zero drift | PASS -- 96 live / 96 manifest; 0 task-estate-drift line(s) | Harrison (elevated) + Cora-script | ~30-45 min |
| D14 | Interactive-logon dependency of the whole estate | every task `LogonType=Interactive` (manifest column `run_as`) | DECISION: auto-logon on the host/VM or a stored-credential logon type; then the cold-boot drill | **MANUAL** -- 96 of 96 tasks run LogonType=Interactive -> nothing fires until a user signs in (the 2026-09-09 dark box, 00:34-06:18 AZ); 21 tasks have StartWhenAvailable=false. Decision owner Harrison: auto-logon on the host / VM, or re-register with a stored-credential logon type. Drill item: cold boot with NO user logged on -> does the estate come up? | Harrison | MANUAL |
| D15 | Backups landing folder + offsite verify | `backup_logs.py` daily 20:30 AZ -> `G:\...\_shared\projects\cora\backups\YYYY-MM-DD\` (logs, ledgers, feature DBs, snapshots, secrets); log line `Offsite verify: PASS` | nothing to restore -- this is the SOURCE for D05/D16; after a rebuild, confirm the first nightly run prints `Offsite verify: PASS` | PASS -- dated folders present: ['2026-09-22']; last backup log offsite verify: PASS (cowork-cora-backup-2026-09-23.log) | Cora-script | n/a |
| D16 | KB restore path (a): snapshot restore | the one-off `--include-kb` copy `backups/2026-09-08/cora_kb.db` (taken inside the 9/8 stop window) | copy the snapshot to `data/cora_kb.db` with Cora STOPPED; start Cora; the nightly syncs catch up the gap (watermarks are inside the DB) | PASS -- G:\My Drive\HJR-Founder-OS\_shared\projects\cora\backups\2026-09-08\cora_kb.db = 7,874,965,504 B (7.87 GB), taken 2026-09-09 00:59Z, 14 days stale; RTO(a) UNMEASURED (copy 7.87 GB from Drive + open + run the nightly syncs to catch up 14 days) | Harrison | UNMEASURED (copy ~7.9 GB + catch-up) |
| D17 | KB restore path (b): connector rebuild | `deployment/kb-rebuild.md` (fast path: nightly tasks repopulate; full path: the backfill scripts) + the door tasks listed in the probe | move the DB aside with Cora stopped; start Cora; let the nightly doors run (fast path) or run the backfills in one window (full path) | PASS -- kb-rebuild.md present; 14/14 doors registered; RTO(b) UNMEASURED -- only bound: ~20 calendar days via nightly windows (full re-ingest 2026-05-28..06-17, TOM 2026-06-17); no single-window measurement exists. Doors: cowork-cora-kb-sync-static: Ready last 2026-09-23T04:00:01 rc 0 \| cowork-cora-kb-sync-slack: Ready last 2026-09-23T02:00:01 rc 0 \| cowork-cora-kb-sync-gmail: Ready last 2026-09-23T02:30:01 rc 0 \| cowork-cora-kb-sync-asana: Ready last 2026-09-23T03:00:01 rc 0 \| cowork-cora-kb-sync-fireflies: Ready last 2026-09-23T03:30:01 rc 0 \| cowork-cora-kb-sync-notion: Ready last 2026-09-23T05:00:01 rc 0 \| cowork-cora-kb-sync-drive: Ready last 2026-09-23T04:30:01 rc 0 \| Cora - Drive Sweep: Ready last 2026-09-23T06:00:01 rc 0 \| Cora - Drive Materialization: Ready last 2026-09-23T05:45:01 rc 0 \| Cora - LEX Dump Folder Sync: Ready last 2026-09-23T04:45:01 rc 0 \| cowork-cora-founders-os-sweep: Ready last 2026-09-23T06:30:01 rc 0 \| cowork-cora-channel-sweep: Ready last 2026-09-23T08:40:01 rc 0 \| cowork-cora-session-capture: Ready last 2026-09-23T05:15:01 rc 0 \| cowork-cora-claude-mirror: Ready last 2026-09-23T03:45:01 rc 0 | Harrison + Cora-script | UNMEASURED |
| D18 | Live KB integrity | `data/cora_kb.db` (sqlite + sqlite-vec; WAL) | n/a -- the probe is the read-only sanity check after either restore path | PASS -- 8.15 GB on disk; 685,109 knowledge_chunks (read-only open) | Cora-script | n/a |
| D19 | Slack app (Socket Mode) -- token key names | api.slack.com app config; `.env` keys `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_SIGNING_SECRET` | restore from D05, or regenerate in the Slack app config (bootstrap Phase 3) and revoke the old tokens | PASS -- key names present: SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_SIGNING_SECRET; Socket Mode (no inbound URL to restore) | Harrison | ~15 min |
| D20 | Windowless launcher (`deployment/run_hidden.py` under `pythonw.exe`) | the repo + the venv | ships with D01/D03; `rewrap-tasks-hidden.ps1 -Apply` after registering tasks | PASS -- run_hidden.py present; 78/96 tasks wrapped windowless | Cora-script | ~2 min |
| D21 | Cowork estate (Claude desktop scheduled tasks) -- cross-reference | `%USERPROFILE%\OneDrive\Documents\Claude\Scheduled\*` + `C:\Users\Harri\code\pin-scheduled-task-models.ps1` (weekly task `cowork-model-pin-weekly`) | NOT migrated (charter D4): stays on the office machine; revisit at Phase 3 | INFO -- 111 Cowork task folders under C:\Users\Harri\OneDrive\Documents\Claude\Scheduled; pin task cowork-model-pin-weekly Ready; NOT migrated (charter D4) | Harrison | n/a |
| D22 | Task XML exports (`deployment/task-backups/<date>`) | local, gitignored exports from `rewrap-tasks-hidden.ps1` | convenience only; the committed record is the manifest JSON | INFO -- local XML exports: ['2026-09-02', '2026-09-03'] (gitignored -- the committed record is deployment/manifest/task-estate.json; an XML export is a convenience, not the source of truth) | Cora-script | n/a |
| D23 | Restore drill: cold boot with NO user logged on | this manifest (D14) + the VM at step 2 | power-cycle the VM, do NOT sign in, wait 15 min: does the service start, does the heartbeat advance, do the 02:00-06:10 tasks fire? | **MANUAL** -- runs on the VM at step 2; the office host cannot be rebooted for a drill without a stop window | Harrison | MANUAL (step 2) |
| D24 | Full bare-metal RTO | the sum of D01-D19 measured end to end | measure on the VM at step 2 (the migration IS the drill); record the wall-clock here | **UNMEASURED** -- no measurement exists; every component RTO above is an estimate or UNMEASURED until step 2 | Harrison + Justin | UNMEASURED |

**`.env` schema (key NAMES from `.env.example`; 57 active + 169 commented/optional; values live ONLY in the encrypted bundle):**

- active: `AIRTABLE_API_KEY`, `ANTHROPIC_API_KEY`, `ASANA_PAT`, `CORA_DRIVE_ROOT_FOLDER_ID`, `DEPOSCO_BU`, `DEPOSCO_TENANT`, `DRIVE_EXTRACTOR_PROPOSALS_ENABLED`, `EMAIL_FILING_LOOKBACK_HOURS`, `EMAIL_FILING_NOTIFY_CHANNEL`, `FIGHTER_TRACKER_SHEET_ID`, `FIREFLIES_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `GSHEETS_CASHFLOW_FILE_ID`, `HEALTH_BIND`, `HEALTH_PING_INTERVAL_S`, `HEALTH_PING_URL`, `HEALTH_PORT`, `HUBSPOT_PORTAL_ID`, `HUBSPOT_PRIVATE_APP_TOKEN`, `INFLUENCER_SCAN_NOTIFY_CHANNEL`, `INSTAGRAM_F3E_ACCESS_TOKEN`, `INSTAGRAM_F3E_USER_ID`, `INSTAGRAM_F3MOOD_ACCESS_TOKEN`, `INSTAGRAM_F3MOOD_USER_ID`, `INSTAGRAM_F3PURE_ACCESS_TOKEN`, `INSTAGRAM_F3PURE_USER_ID`, `KLAVIYO_API_KEY`, `MAKE_SALES_DECK_WEBHOOK_URL`, `META_APP_ID`, `META_APP_SECRET`, `NOTION_API_KEY`, `OPENAI_API_KEY`, `OSN_SCHEDULER_ADMIN_USER_IDS`, `OSN_SCHEDULER_APPROVAL_CHANNEL`, `OTTERLY_API_KEY`, `PERPLEXITY_API_KEY`, `PHOTOROOM_API_KEY`, `PHOTOROOM_BASE_URL`, `PHOTOROOM_OUTPUTS_DRIVE_FOLDER_ID`, `POLAR_API_KEY`, `POLAR_CLIENT_ID`, `POLAR_CLIENT_SECRET`, `POLAR_VIEW_ID`, `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_ENVIRONMENT`, `QBO_REDIRECT_URI`, `SECURITY_ALERT_CHANNEL`, `SHOPIFY_F3E_ACCESS_TOKEN`, `SHOPIFY_F3E_API_KEY`, `SHOPIFY_F3E_API_SECRET`, `SHOPIFY_F3E_STORE`, `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_USER_TOKEN`
- commented / optional: `AIRTABLE_WRITE_API_KEY`, `AI_VISIBILITY_CHANNEL`, `AI_VIS_CITATION_TIMEOUT`, `AI_VIS_CITATION_WORKERS`, `AI_VIS_CLAUDE_MODEL`, `AI_VIS_CONCURRENCY`, `AI_VIS_GEMINI_MODEL`, `AI_VIS_HTTP_TIMEOUT`, `AI_VIS_OPENAI_MODEL`, `AI_VIS_PERPLEXITY_MODEL`, `APOLLO_API_KEY`, `ASANA_PAT_CORA`, `BRAIN_PEOPLE_DIR`, `CASHFLOW_FLIP_GATE_ABS`, `CASHFLOW_FLIP_GATE_PCT`, `CASHFLOW_PACK_DEBUT_MIN_CONFIRMED`, `CASH_PULSE_ENABLED`, `CATCHUP_REPLY_PREFACE`, `CLAUDE_MAX_INPUT_TOKENS`, `CLAUDE_PROJECTS_ROOT`, `CLOSURE_NUDGE_LOG_PATH`, `COMPLETION_SWEEP_POST_ENABLED`, `CORA_AI_VISIBILITY_DB`, `CORA_ASANA_IDENTITY`, `CORA_AUTOWRITE_LIVE`, `CORA_BACKUP_PASSPHRASE`, `CORA_BATCH_CAPTURE`, `CORA_BATCH_CAPTURE_DEADLINE_S`, `CORA_BATCH_DISABLE`, `CORA_BATCH_SYNTHESIS`, `CORA_BATCH_SYNTHESIS_DEADLINE_S`, `CORA_CODE_QUEUE`, `CORA_CONFIRM_BUTTONS`, `CORA_DECISIONS_INBOX_LEDGER`, `CORA_DECISIONS_INBOX_PATH`, `CORA_DELEGATED_JOB_USD`, `CORA_DELEGATED_MODEL`, `CORA_DELEGATED_MONTHLY_USD`, `CORA_DELEGATED_ORG_DAILY`, `CORA_DELEGATED_USER_DAILY`, `CORA_DELEGATED_WORK`, `CORA_DELEGATED_WORK_LEX`, `CORA_DEPOSCO_WAREHOUSE_LINE`, `CORA_DISABLE_HUBSPOT_PORTAL_GUARD`, `CORA_DRIVE_IMPERSONATE`, `CORA_DRIVE_ROOT`, `CORA_EVAL_MODE`, `CORA_F3E_BLOG_CARDS_PATH`, `CORA_F3E_BLOG_LEDGER_PATH`, `CORA_F3E_BLOG_STATE_PATH`, `CORA_FOUNDER_SLACK_ID`, `CORA_GAP_DETECT_DAILY_CAP`, `CORA_GAP_ESCALATION_LEX`, `CORA_GRADUATED_SHADOW`, `CORA_GRADUATED_SHADOW_DIR`, `CORA_HUBSPOT_EMAIL_SYNC_DM_ENABLED`, `CORA_KB_DB_PATH`, `CORA_KB_MISS_SHADOW_FLOOR`, `CORA_KNOWLEDGE_CHECK`, `CORA_LADDER_REGISTRY_PATH`, `CORA_LEXICON`, `CORA_MCP_HTTP_CERT`, `CORA_MCP_HTTP_KEY`, `CORA_MCP_HTTP_PORT`, `CORA_MCP_HTTP_TOKEN`, `CORA_MECHANICAL_REVIEW`, `CORA_MEETING_CAPTURE_LEDGER`, `CORA_ONECORA_ENSURE`, `CORA_OPERATIONAL_TTL_DAYS`, `CORA_OPUS_MODEL`, `CORA_REVOPS_DB`, `CORA_SEND_LIVE`, `CORA_SENTINEL_ENFORCE`, `CORA_SESSIONS_CHANNEL`, `CORA_SNAPSHOT_DIR`, `CORA_SNAPSHOT_INTERVAL_SECS`, `CORA_SNAPSHOT_MIRROR_DIR`, `CORA_SONNET_MODEL`, `CORA_WEB_FETCH_MAX_USES`, `CORA_WEB_KB_MISS_DISTANCE`, `CORA_WEB_SEARCH_DAILY_CAP`, `CORA_WEB_SEARCH_MAX_USES`, `CORA_WEB_TOOLS`, `CORA_WEB_TOOLS_LEX`, `COWORK_SESSIONS_ROOT`, `DECISION_ALERT_STATE_PATH`, `DECISION_CAPTURE_POST_ENABLED`, `DECISION_FACT_FP_PATH`, `DEPOSCO_PROD_PASS`, `DEPOSCO_PROD_USER`, `DEPOSCO_UA_PASS`, `DEPOSCO_UA_USER`, `DRIVE_EXTRACTOR_MAX_PROPOSALS_PER_RUN`, `DRIVE_IO_BACKOFF_SECONDS`, `DRIVE_IO_BREAKER_SECONDS`, `DRIVE_IO_MONITOR_INTERVAL_SECS`, `DRIVE_IO_MOUNT_ANCHOR`, `DRIVE_IO_RETRY_SECONDS`, `DRIVE_IO_TIMEOUT_SECONDS`, `DYNAMIC_ANSWERS_DIR`, `EFFICIENCY_BACKLOG_PATH`, `EMAIL_FILING_RUN_BUDGET_SECONDS`, `F3E_INVENTORY_FILE_ID`, `FILER_CONTENT_LEDGER_PATH`, `FILER_CONTENT_TTL_DAYS`, `FILER_MESSAGE_LEDGER_PATH`, `FILER_MESSAGE_TTL_DAYS`, `FINANCE_BANK_TXN_STALE_DAYS`, `FINANCE_CLOSE_NARRATE`, `FINANCE_DIGEST_FALLBACK_CHANNEL`, `FINANCE_INTERCOMPANY_DELTA_ABS`, `FIREFLIES_API_TOKEN`, `FIREFLIES_TOKEN`, `FLYWHEEL_MIRROR_DIR`, `FOUNDER_OS_ROOT`, `FRICTION_KB_DB_PATH`, `FRICTION_LEDGER_PATH`, `GAP_ASK_PENDING_PATH`, `GAP_AUTOFILL_SOURCES`, `GAP_AUTOFILL_STATE_PATH`, `GAP_DETECTION_STATE_PATH`, `GAP_DOMAIN_OWNERS_PATH`, `GOLDEN_SET_AUTO_PATH`, `GSHEETS_CASHFLOW_SHEET_NAME`, `HARRISON_SLACK_USER_ID`, `HEALTH_REPORT_CHANNEL`, `KB_DECISION_LOG_PATH`, `KNOWLEDGE_CHECK_AIRTABLE_MAP`, `KNOWLEDGE_GAPS_LOG_PATH`, `KNOWN_ANSWERS_DIR`, `LEXICON_CANDIDATES_PATH`, `LEXICON_DIR`, `LEXICON_FINGERPRINTS_PATH`, `LEXICON_RESOLUTIONS_PATH`, `LEXICON_ROSTER_PATH`, `LEXICON_SKU_ALIASES_PATH`, `LEXICON_USER_ALIASES_PATH`, `LOCALAPPDATA`, `LOG_LEVEL`, `MAILBOX_INTAKE_WATERMARK_PATH`, `MATERIALIZATION_WATERMARK_PATH`, `MATERIALIZER_KB_DB_PATH`, `MEETING_ASK_STATE_PATH`, `MEETING_RECAP_LEDGER_PATH`, `MEETING_RECAP_PENDING_PATH`, `MISSED_CATCHUP_LEDGER_PATH`, `NIGHTLY_CATCHUP_LEDGER_PATH`, `NOTION_EXTRA_DB_IDS`, `OTTERLY_AIO_ENGINE`, `OTTERLY_BASE_URL`, `OTTERLY_HTTP_TIMEOUT`, `PHOTOROOM_RATE_LIMIT_PER_MIN`, `PHOTOROOM_USE_SANDBOX`, `PHOTOROOM_WEEKLY_BUDGET_USD`, `POLAR_API_BASE_URL`, `POLAR_MCP_URL`, `POLAR_OAUTH_URL`, `QBO_TOKEN_LOCK_TIMEOUT_SEC`, `REPEAT_SIGNAL_LEDGER_PATH`, `RESOLVED_GAPS_PATH`, `STRATEGY_ASANA_MAP_PATH`, `STRATEGY_DECISIONS_PATH`, `STRATEGY_HEARTBEAT_PATH`, `STRATEGY_KB_DB_PATH`, `STRATEGY_MEMO_DIR`, `STRATEGY_SNAPSHOT_DIR`, `SWEPT_DIR`, `SYNTHESIS_SNAPSHOT_DIR`, `TASK_RUNS_LEDGER_PATH`
<!-- END GENERATED: dr-probe-baseline -->

## 3. The KB — two restore paths, side by side

The KB (`data/cora_kb.db`, ~8 GB, ~685K chunks on 2026-09-23) is **not backed up nightly by
design**: `backup_logs.py` skips it (~6–8 GB/day of Drive quota for a regenerable file).
Both paths below are real; neither has a measured RTO.

| | (a) Snapshot restore | (b) Connector rebuild |
|---|---|---|
| Source | the one-off `--include-kb` copy taken inside the 2026-09-08 stop window: `G:\My Drive\HJR-Founder-OS\_shared\projects\cora\backups\2026-09-08\cora_kb.db` (7,874,965,504 B) | the connector sync/backfill scripts against the source systems + the static markdown tree — `deployment/kb-rebuild.md` |
| Staleness | frozen at 2026-09-08 17:59 AZ; the nightly syncs catch up the gap after start (watermarks live inside the DB) | none — rebuilt from live sources |
| Steps | Cora STOPPED (stop window, watchdog parked) → copy the snapshot to `data/cora_kb.db` (remove `-wal`/`-shm`) → start Cora → let the nightly doors run | Cora STOPPED → move the DB aside → start Cora → **fast path:** the nightly doors repopulate over ~1–3 nights; **full path:** run the backfills in one window (kb-rebuild.md "Full path") |
| Doors (per-door last fire is in the probe row D17) | n/a | `kb-sync-static/slack/gmail/asana/fireflies/notion/drive`, `Drive Sweep`, `Drive Materialization`, `LEX Dump Folder Sync`, `founders-os-sweep`, `channel-sweep`, `session-capture`, `claude-mirror` |
| RTO | **UNMEASURED** — copy ~7.9 GB from the Drive mirror + open + catch-up; nobody has timed it | **UNMEASURED** — the only bound is historical: the whole corpus was re-ingested 2026-05-28..06-17 through the nightly windows (~20 calendar days); no single-window measurement exists |
| Owner | Harrison (stop window) | Harrison (stop window) + Cora-script (the doors) |
| Refresh the snapshot | inside any stop window: `.venv\Scripts\python.exe scripts\backup_logs.py --include-kb` (10–20 min; the offsite-verify line must say the KB landed) | n/a |

A fresh snapshot is worth taking at every stop window the estate has anyway; a stale
snapshot plus the nightly catch-up is still the faster path on paper, but **paper is all
we have until step 2 times both**.

## 4. Restore drill — the ordered probe list step 2 runs on the VM

Run in this order; every step is one row above (probe id in brackets). Record wall-clock
per step — those numbers become the RTO column.

1. Clone the repo at the recorded `main` hash; `uv sync` [D01, D02, D03].
2. Fetch `CORA_BACKUP_PASSPHRASE` from the password manager; `restore_secrets.py` the newest
   `secrets-*.enc` → `.env` + the Google SA JSON [D05]; check the `.env` for duplicate keys
   [D04]; confirm the three Slack key names are present [D19].
3. Install Google Drive for Desktop, sign in, mount `G:`, wait for `HJR-Founder-OS` to
   stream [D11]. Nothing below works without it (the bot degrades every entity context).
4. Decide the logon posture [D14] — auto-logon or a stored-credential logon type. **Without
   this the VM is the office box again: nothing fires after a reboot until someone signs in.**
5. Register the service + the watchdog (elevated) [D07, D09]; start; a NEW pid in
   `logs/cora-instances.jsonl` + a fresh heartbeat are the proof [D08]; `rewrap-tasks-hidden.ps1
   -Apply` [D20].
6. Register the full scheduled estate per bootstrap Phase 5; `generate_task_estate_manifest.py
   --diff-only` must print zero drift [D13].
7. KB: choose (a) or (b) [D16/D17] — **time it**; sanity-check with the read-only probe [D18].
8. healthchecks.io: confirm the check flips UP [D10]; confirm the first nightly backup prints
   `Offsite verify: PASS` into the VM's own dated folder [D15].
9. **Cold-boot drill** [D23]: power-cycle, do NOT sign in, wait 15 min — service up? heartbeat
   advancing? did the 02:00–06:10 tasks fire? This is the 9/9 incident replayed on purpose.
10. Record the end-to-end wall-clock [D24]; Justin signs off the RTO at the cost checkpoint.

Sandbox scope at step 2 is PHI-free by exclusion (see the scoping packet): no LEX KB
partition, no LEX Drive folders, no Gmail/Fireflies/Calendar credentials, no production
Slack token. Path (b) restricted to non-LEX, non-personal doors — or no KB at all for a
CI-only start.

## 5. Pinned Drive folder ids (identifiers only; `cora.kb_exclusions.KB_EXCLUDED_FOLDER_IDS`)

These ship with the repo and need no restore; they are listed so a Drive restructure is
checked against them. Labels are the code comments; no content is described.

| id | label (from kb_exclusions.py) |
|---|---|
| `1INi4fLXG23xao-d_yf56Wrbrah54pIBB` | 00-Founder/insurance/oneamerica (PERSONAL) |
| `1BZI6v5pmpgrt7G2dPsAib3u3S-HqB7ZP` | 02-F3-Energy/projects/capital-raise (HIGHLY CONFIDENTIAL) |
| `1NPBNBfx3MMjqQM_WnmL6jOJSaRAQf752` | 00-Founder/travel-points (PERSONAL) |
| `1HEHpMWgkJkHmV1wfWIiT5OhBI0p5p2P-` | Downloads/OneAmerica-Handoff dup (PERSONAL) |
| `112C7ljGRI5VO_ic66fVGQk4kf6IC40HQ` | 08-Lexington-Services/projects/copa-bhrf (LEX NDA; identifier only) |
| `1aDnmz3oY7QZxsH7mv7_ZDu7cUyDWLhy7` | 01-HJR-Global/accounting/cashflow-ledger (13WCF mirror) |
| (the remaining pins) | the `_shared/projects/cora` parent pin (D-057) and the Computers-backup roots + personal transfer tree (ingest-integrity I3, cq-a1aaee9f46e0) — read the frozenset in `src/cora/kb_exclusions.py`; the probe row D12 counts them (10 on 2026-09-23) |

## 6. Cowork estate — cross-reference (NOT migrated)

111 scheduled tasks live inside the Claude desktop app
(`%USERPROFILE%\OneDrive\Documents\Claude\Scheduled\<task-id>\SKILL.md`, 2026-09-23 count),
pinned to their models weekly by `C:\Users\Harri\code\pin-scheduled-task-models.ps1`
(Task Scheduler task `cowork-model-pin-weekly`, Sat 07:10). **Charter D4 (2026-09-01): the
bot estate migrates at cutover; the Cowork estate stays on the office machine; revisit at
Phase 3.** The mirror (`scripts/mirror_claude_workspace.py`) is the one reader of that
estate and this manifest reuses it (M1's Cowork cross-reference); the run-marker contract
for those tasks is `deployment/cowork-run-marker-footer.md`.

## 7. Known gaps this baseline records (not fixes)

- **Interactive logon** [D14]: 96/96 tasks fire only while a user is signed in; a Windows
  Update reboot at 00:34 AZ on 2026-09-09 left the estate dark until 06:18. Auto-logon or a
  logon-type change is Harrison's decision; the nightly catch-up lane (Code #13) recovers
  the ingest set the morning after but not the service itself.
- **Run markers** cover 4 of 96 tasks (measured; widening is `cq-06045f418bd2`, gated on the
  C13 ruling) — Task Scheduler's LastRunTime is the evidence for the other 92.
- **9 tasks have no `setup-*.ps1`** (`cowork-cora-health-check`, `-completion-sweep`,
  `-decision-capture`, `-feedback-health` enabled; `-asana-email-sync`, `-gap-digest`,
  `-hubspot-email-sync`, `-monthly-deliverables`, `-proactive-gaps` disabled) — re-register
  them from the manifest JSON.
- **Both KB RTOs UNMEASURED**; the encrypted-bundle path depends on a passphrase that lives
  only in the password manager (by design).

## 8. Change log

- 2026-09-23 — first version (DR/VM step 1, slice M2; kickoff 2026-09-08). Baseline probed
  on the office host: see the generated block. Supersedes the "3 tasks" bootstrap phase.
