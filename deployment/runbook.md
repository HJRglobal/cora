# Cora Operations Runbook

## Scheduled Tasks Registry

The registry is GENERATED from the live Task Scheduler by
`scripts/generate_task_estate_manifest.py --update-docs` (DR/VM step 1, M1). The
machine-readable manifest is `deployment/manifest/task-estate.json`; full columns
(run-as, StartWhenAvailable, last result, run marker, log path, intent, setup script)
are in `deployment/manifest/task-estate.md`. The Monday digest
(`cora_health_report.py --slack`) diffs the live registry against the committed
manifest and prints `task-estate-drift:` lines. Do not hand-edit inside the markers.

<!-- BEGIN GENERATED: task-registry -->
_Generated 2026-09-24 by `scripts/generate_task_estate_manifest.py --update-docs` from the live registry (99 tasks, 80 enabled). Full columns: `deployment/manifest/task-estate.md`. Do not hand-edit._

| Task Name | Schedule | Script | State | Run level | Notes |
|---|---|---|---|---|---|
| `Cora - Asana Hygiene Nudges` | daily 06:40 | `scripts/run_asana_hygiene_nudges.py` | Ready | Highest |  |
| `Cora - Cash Flow Pulse` | daily 15:30 | `scripts/run_cashflow_pulse.py` | Disabled | Highest | intent: disabled; console action (not run_hidden-wrapped) |
| `Cora - Cash Snapshot` | daily 06:45 | `scripts/write_cashflow_snapshot.py` | Ready | Limited |  |
| `Cora - Channel Health Monitor` | weekly Sun 04:15 | `scripts/run_channel_health_monitor.py` | Ready | Highest |  |
| `Cora - Daily Briefing` | weekly Mon,Tue,Wed,Thu,Fri 07:30 | `scripts/run_daily_briefing.py` | Ready | Limited |  |
| `Cora - Daily Synthesis (BDM)` | daily 06:52 | `scripts/run_entity_synthesis.py` | Ready | Limited |  |
| `Cora - Daily Synthesis (F3C)` | daily 06:58 | `scripts/run_entity_synthesis.py` | Ready | Limited |  |
| `Cora - Daily Synthesis (F3E)` | daily 06:33 | `scripts/run_entity_synthesis.py` | Ready | Limited |  |
| `Cora - Daily Synthesis (HJRP)` | daily 06:35 | `scripts/run_entity_synthesis.py` | Ready | Limited |  |
| `Cora - Daily Synthesis (HJRPROD)` | daily 06:56 | `scripts/run_entity_synthesis.py` | Ready | Limited |  |
| `Cora - Daily Synthesis (LEX)` | daily 06:39 | `scripts/run_entity_synthesis.py` | Ready | Limited |  |
| `Cora - Daily Synthesis (OSN)` | daily 06:37 | `scripts/run_entity_synthesis.py` | Ready | Limited |  |
| `Cora - Daily Synthesis (Portfolio)` | daily 06:31 | `scripts/run_portfolio_synthesis.py` | Ready | Limited |  |
| `Cora - Daily Synthesis (UFL)` | daily 06:54 | `scripts/run_entity_synthesis.py` | Ready | Limited |  |
| `Cora - Deal Aging Alerts` | daily 15:00 | `scripts/run_deal_aging_alerts.py` | Disabled | Limited | intent: disabled; console action (not run_hidden-wrapped) |
| `Cora - Drive Materialization` | daily 05:45 | `scripts/run_drive_materialization.py` | Ready | Limited |  |
| `Cora - Drive Sweep` | daily 06:00 | `scripts/run_drive_sweep.py` | Ready | Limited |  |
| `Cora - Due Date Escalation` | daily 14:00 | `scripts/run_due_date_escalation.py` | Ready | Limited |  |
| `Cora - Email Attachment Filer` | every PT4H (from 2026-05-27T22:00) | `scripts/run_attachment_filer.py` | Ready | Limited |  |
| `Cora - Expected Invoice Check` | monthly day 9 09:38 | `scripts/run_expected_invoice_check.py` | Ready | Limited | SWA=false |
| `Cora - F3E Blog Pipeline` | weekly Mon 08:50 | `scripts/run_f3e_blog_pipeline.py` | Ready | Limited | SWA=false |
| `Cora - F3E Daily Ecom Brief` | daily 07:10 | `scripts/run_f3e_ecom_brief.py` | Disabled | Limited | intent: disabled; console action (not run_hidden-wrapped) |
| `Cora - False Deflection Watch` | weekly Mon 08:00 | `scripts/run_false_deflection_watch.py` | Ready | Highest |  |
| `Cora - Friction Mining` | weekly Sun 17:30 | `scripts/run_friction_mining.py` | Ready | Limited |  |
| `Cora - HubSpot Deal Monitor` | every PT1H (from 2026-06-03T16:00) | `scripts/run_hubspot_deal_monitor.py` | Disabled | Limited | intent: disabled; console action (not run_hidden-wrapped) |
| `Cora - Inventory Alerts` | daily 16:00 | `scripts/run_inventory_alerts.py` | Disabled | Limited | intent: disabled; console action (not run_hidden-wrapped) |
| `Cora - KB Evals` | weekly Mon 09:05 | `scripts/run_kb_evals.py` | Ready | Limited |  |
| `Cora - Klaviyo Billing Audit` | monthly day 9 09:53 | `scripts/run_klaviyo_billing_audit.py` | Ready | Limited | SWA=false |
| `Cora - Knowledge Check` | weekly Mon,Tue,Wed,Thu,Fri 08:05 | `scripts/run_knowledge_check.py` | Ready | Limited |  |
| `Cora - LEX Dump Folder Sync` | daily 04:45 | `scripts/run_lex_dump_folder_sync.py` | Ready | Limited |  |
| `Cora - LEX Swept PHI Check` | daily 07:06 | `scripts/run_lex_swept_phi_check.py` | Ready | Limited |  |
| `Cora - Log Compaction` | monthly day 1 14:00 | `scripts/compact_logs.py` | Ready | Limited | SWA=false |
| `Cora - Meeting Action Capture` | every PT1H (from 2026-06-05T11:00) | `scripts/run_meeting_action_capture.py` | Disabled | Limited | intent: disabled; console action (not run_hidden-wrapped) |
| `Cora - Meeting Ask Capture` | every PT15M for PT13H stop-at-end (daily 07:08) | `scripts/run_meeting_ask_capture.py` | Ready | Limited | SWA=false |
| `Cora - Missed Nightly Catch-Up` | daily 08:30 | `scripts/check_missed_nightly.py` | Ready | Limited |  |
| `Cora - OSN Metrics Digest` | weekly Mon 15:00 | `scripts/run_osn_metrics_digest.py` | Ready | Highest |  |
| `Cora - QBO Monthly Reports` | monthly day 2 07:45 | `scripts/run_qbo_monthly_reports.py` | Ready | Limited | SWA=false |
| `Cora - QBO Token Monitor` | daily 06:50 | `scripts/qbo_token_status.py` | Ready | Limited |  |
| `Cora - Revops Sweep` | daily 10:15 | `scripts/run_revops_sweep.py` | Ready | Limited | SWA=false |
| `Cora - Shopify DTC Summary` | daily 15:00 | `scripts/run_shopify_dtc_summary.py` | Disabled | Limited | intent: disabled; console action (not run_hidden-wrapped) |
| `Cora - Strategy Memo` | weekly Sun 18:30 | `scripts/run_strategy_memo.py` | Ready | Limited |  |
| `Cora - Weekly Health Metrics` | weekly Mon 09:30 | `scripts/cora_health_report.py` | Ready | Highest |  |
| `Cora - Weekly Pipeline Digest` | weekly Mon 15:00 | `scripts/run_pipeline_digest.py` | Disabled | Limited | intent: disabled; console action (not run_hidden-wrapped) |
| `cora-watchdog` | every PT5M (from 2026-07-16T10:27) | `deployment/cora-watchdog.ps1` | Ready | Highest |  |
| `cowork-cora-ai-visibility-scan` | weekly Mon 10:15 | `scripts/run_ai_visibility_scan.py` | Ready | Limited |  |
| `cowork-cora-asana-email-sync` | every PT1H (from 2026-06-01T00:10) | `scripts/run_asana_email_sync.py` | Disabled | Limited | intent: disabled; SWA=false; console action (not run_hidden-wrapped) |
| `cowork-cora-autowrite-digest` | weekly Mon 11:00 | `scripts/run_autowrite_digest.py` | Ready | Limited | SWA=false |
| `cowork-cora-backup` | daily 20:30 | `scripts/backup_logs.py` | Ready | Limited |  |
| `cowork-cora-bank-snapshot` | daily 07:05 | `scripts/run_qbo_bank_snapshot.py` | Ready | Limited |  |
| `cowork-cora-cashflow-actuals` | weekly Mon 06:25 | `scripts/run_cashflow_actuals.py` | Ready | Limited |  |
| `cowork-cora-cashflow-forecast-snapshot` | weekly Mon 06:15 | `scripts/run_cashflow_forecast_snapshot.py` | Ready | Limited |  |
| `cowork-cora-channel-sweep` | daily 08:40 | `scripts/run_channel_sweep.py` | Ready | Limited |  |
| `cowork-cora-claude-mirror` | daily 03:45 + daily 12:15 | `scripts/mirror_claude_workspace.py` | Ready | Limited |  |
| `cowork-cora-completion-sweep` | daily 14:00 | `scripts/run_completion_sweep.py` | Ready | Limited | SWA=false |
| `cowork-cora-decision-capture` | daily 07:15 | `scripts/capture_decisions.py` | Ready | Limited | SWA=false |
| `cowork-cora-delegated-work` | every PT15M for P3650D stop-at-end (from 2026-08-01T00:00) | `scripts/run_delegated_work_runner.py` | Ready | Limited |  |
| `cowork-cora-deposco-inventory-sync` | daily 06:22 | `scripts/run_deposco_inventory_sync.py` | Ready | Limited |  |
| `cowork-cora-deposco-lot-ledger` | daily 07:45 | `scripts/run_deposco_lot_ledger.py` | Ready | Limited |  |
| `cowork-cora-digest` | daily 05:20 | `scripts/generate_knowledge_gaps_digest.py` | Disabled | Limited | intent: disabled; console action (not run_hidden-wrapped) |
| `cowork-cora-drive-extractor` | daily 04:05 + at logon | `scripts/run_drive_extractor.py` | Disabled | Highest | DRIFT: host DISABLED, not in the disabled list; console action (not run_hidden-wrapped) |
| `cowork-cora-feedback-health` | weekly Mon 08:30 | `scripts/run_feedback_health_report.py` | Ready | Limited | SWA=false |
| `cowork-cora-finance-adherence` | weekly Mon 08:15 | `scripts/run_finance_adherence_check.py` | Ready | Limited |  |
| `cowork-cora-finance-close-pack` | weekly Mon 09:00 | `scripts/run_finance_close_pack.py` | Ready | Limited |  |
| `cowork-cora-finance-receipt-digest` | weekly Mon 10:30 | `scripts/run_finance_receipt_digest.py` | Ready | Limited | SWA=false |
| `cowork-cora-finance-weekly` | weekly Mon 14:30 | `scripts/run_finance_weekly_recap.py` | Ready | Limited |  |
| `cowork-cora-fireflies-coverage` | weekly Mon 08:10 | `scripts/run_fireflies_coverage.py` | Ready | Limited |  |
| `cowork-cora-founders-os-sweep` | daily 06:30 | `scripts/ingest_founders_os.py` | Ready | Highest |  |
| `cowork-cora-gap-autofill` | daily 06:10 | `scripts/run_gap_autofill.py` | Ready | Limited |  |
| `cowork-cora-gap-digest` | weekly Mon 08:00 | `scripts/post_gap_digest_slack.py` | Disabled | Limited | intent: disabled; SWA=false; console action (not run_hidden-wrapped) |
| `cowork-cora-health-check` | daily 08:45 | `scripts/nightly_health_check.py` | Ready | Limited | SWA=false |
| `cowork-cora-hubspot-email-sync` | every PT1H (from 2026-05-31T23:23) | `scripts/run_hubspot_email_sync.py` | Disabled | Limited | intent: disabled; SWA=false; console action (not run_hidden-wrapped) |
| `cowork-cora-influencer-digest` | weekly Mon 08:20 | `scripts/run_influencer_digest.py` | Disabled | Limited | intent: disabled; console action (not run_hidden-wrapped) |
| `cowork-cora-influencer-overdue-alerts` | daily 09:10 | `scripts/run_influencer_overdue_alerts.py` | Disabled | Limited | intent: disabled; console action (not run_hidden-wrapped) |
| `cowork-cora-influencer-scan` | every PT2H (from 2026-05-27T22:00) | `scripts/run_influencer_scan.py` | Disabled | Limited | intent: disabled; console action (not run_hidden-wrapped) |
| `cowork-cora-info-for-cora-sweep` | daily 06:05 | `scripts/run_info_for_cora_sweep.py` | Ready | Limited |  |
| `cowork-cora-inventory-state-sync` | daily 06:20 | `scripts/run_inventory_state_sync.py` | Ready | Limited |  |
| `cowork-cora-kb-hygiene` | monthly day 1 15:00 | `scripts/kb_hygiene_sweep.py` | Ready | Limited | SWA=false |
| `cowork-cora-kb-sync-asana` | daily 03:00 | `scripts/incremental_sync_asana.py` | Ready | Limited |  |
| `cowork-cora-kb-sync-drive` | daily 04:30 | `scripts/incremental_sync_drive.py` | Ready | Limited |  |
| `cowork-cora-kb-sync-fireflies` | daily 03:30 | `scripts/incremental_sync_fireflies.py` | Ready | Limited |  |
| `cowork-cora-kb-sync-gmail` | daily 02:30 | `scripts/gmail_threaded_sweep.py` | Ready | Limited |  |
| `cowork-cora-kb-sync-notion` | daily 05:00 | `scripts/incremental_sync_notion.py` | Ready | Limited |  |
| `cowork-cora-kb-sync-slack` | daily 02:00 | `scripts/incremental_sync_slack.py` | Ready | Limited |  |
| `cowork-cora-kb-sync-static` | daily 04:00 + daily 12:20 | `scripts/incremental_sync_static.py` | Ready | Limited |  |
| `cowork-cora-knowledge-check-report` | weekly Mon 07:20 | `scripts/run_knowledge_check_report.py` | Ready | Limited | SWA=false |
| `cowork-cora-knowledge-review` | weekly Mon,Tue,Wed,Thu,Fri 07:00 | `scripts/run_knowledge_review.py` | Ready | Limited |  |
| `cowork-cora-lexicon-mining` | weekly Sun 17:50 | `scripts/run_lexicon_mining.py` | Ready | Limited |  |
| `cowork-cora-meeting-capture-audit` | daily 07:22 | `scripts/run_meeting_capture_audit.py` | Ready | Limited |  |
| `cowork-cora-meeting-capture-ensure` | every PT15M for PT14H stop-at-end (daily 06:07) | `scripts/run_meeting_capture_ensure.py` | Ready | Limited |  |
| `cowork-cora-monthly-deliverables` | monthly day 1 09:00 | `scripts/generate_monthly_deliverables.py` | Disabled | Limited | intent: disabled; SWA=false; console action (not run_hidden-wrapped) |
| `cowork-cora-person-dossier-refresh` | weekly Sun 16:30 | `scripts/run_person_dossier_refresh.py` | Ready | Limited |  |
| `cowork-cora-pm-adoption-digest` | weekly Mon 08:20 | `scripts/run_pm_adoption_digest.py` | Ready | Limited | SWA=false |
| `cowork-cora-proactive-gaps` | daily 06:00 | `scripts/run_proactive_gaps.py` | Disabled | Limited | intent: disabled; SWA=false; console action (not run_hidden-wrapped) |
| `cowork-cora-project-channel-sync` | daily 16:00 | `scripts/run_project_channel_sync.py` | Disabled | Limited | intent: disabled; console action (not run_hidden-wrapped) |
| `cowork-cora-qbo-token-refresh` | daily 02:00 | `scripts/qbo_oauth_flow.py` | Ready | Highest |  |
| `cowork-cora-reconciliation` | daily 05:30 | `scripts/run_reconciliation.py` | Ready | Limited |  |
| `cowork-cora-security-monitor` | every PT15M (from 2026-05-27T22:33) | `scripts/security_monitor.py` | Ready | Limited |  |
| `cowork-cora-service` | at logon | `cora.main` | Running | Limited |  |
| `cowork-cora-session-capture` | daily 05:15 + daily 12:30 | `scripts/run_session_capture.py` | Ready | Limited |  |
<!-- END GENERATED: task-registry -->
**LinkedIn Spy — RETIRED (Phase 1.8, gate G-C / D-027).** The Python Apollo
writer (`run_linkedin_spy.py` + `apollo_client.py` + `linkedin_spy_client.py` +
`linkedin-spy-search-config.yaml`) was removed; **Make scenario 4769263 is the
sole owner** (no Python double-write). To unregister the leftover (disabled)
host task:
```powershell
.\deployment\remove-linkedin-spy-task.ps1
```

### Pending registration: `cowork-cora-hygiene-drive-weekly` (Code #15 R8, cq-592baba613f1)

Not in the generated table above until Harrison registers it and the manifest
is regenerated. **Weekly Sat 02:40 AZ** -> `scripts/run_hygiene_drive_weekly.py --apply`
(windowless, Interactive/Limited, 1 h limit). Script-side only: no restart.

- **What it does:** runs the VENDORED folder-audit inventory
  (`deployment/hygiene/folder-audit-inventory.ps1`, a byte-identical copy of the
  Drive original, sha256 `339d456d...` pinned in the runner and marked `-text` in
  `.gitattributes`) with `-MaxHashMB 0` -- a NO-HASH, read-only walk (~90 s) -- then
  writes `_shared\hygiene-pending-moves\YYYY-MM-DD_fndr_hygiene-findings.md`
  (week-over-week counts, NEW vs standing offenders, capped lists) plus, in
  `Downloads\hjr-folder-audit`, `hygiene-findings-full-YYYY-MM-DD.csv` (full
  non-LEX list) and `manifest-desktopini-YYYY-MM-DD.csv` (DELETE rows, non-LEX,
  apply.ps1 schema; trend only -- Drive for Desktop recreates desktop.ini).
- **Disclosure:** the report folder is static_md-ingested, so `08-Lexington-Services`,
  any path elsewhere naming LEX (a sub-entity / program code, COPA, a
  cross_entity_guard LEX keyword -- codes also read with CamelCase and a glued
  all-caps code as word breaks: `LexOps`, `LEXreport`, `COPAdiligence`,
  `TheLexington` -- or a LEX person from the detector's lead lists or the org_roles
  roster, first-last or last-first joined by any separators or none, or first
  initial + last: `JaneDoe`, `doe_jane`, `JDoe`, `J.Doe`, `J. Doe`) and every
  KB-pinned container (incl. `_archive` + `00-Founder\personal-finances`) are
  COUNTS ONLY, and listed names pass `phi_guard.is_any_phi`. Still LISTED (known
  residuals, 0 of the 4,681 listable items on the 9/21 baseline): a lead's last
  name alone, a middle name or initial between the two names, both names as
  initials, dotted / dashed
  code letters (`L.B.H.S.`, `L-B-H-S`), an all-caps code glued to another
  all-caps word (`LEXHR`), and payer words (`EVV`, `AHCCCS`, `Medicaid`).
- **Safety:** never mkdirs the report folder; never overwrites a same-named file
  without the generator marker; a walk that fails the floor / has walk errors /
  wrote no summary / cannot read its prior's CSVs writes an INCOMPLETE WALK report,
  records the stamp `incomplete` in the ledger, and exits 1 (never a clean report).
  Keep-4 rotation deletes only stamps the runner itself recorded
  (`data/state/hygiene-drive-stamps.jsonl`), never `20260921-1948`, hand or subtree
  runs, `manifest-*`, `_applied-*` or `_dryrun-*`.
- **The floor (high-water + cumulative anchor):** files AND folders must be >= 80%
  of the LARGEST of the newest four eligible runs (runner runs that passed the
  floor; the `20260921-1948` baseline counts only while it is one of those four),
  never of a run older than the newest re-baseline, AND >= 80% of the cumulative
  ANCHOR -- the newest re-baselined stamp's counts (kept in the stamp ledger), or
  the `20260921-1948` baseline's while nothing was ever re-baselined -- RAISED to
  the largest clean walk since that anchor, so the anchor follows growth (D-051 r4). It is
  anchored on those, not on last week, so a partial walk cannot ratchet the floor
  down -- neither at once nor 20% per four weeks (D-051 R3 rb2#r3-0: the four-run
  window alone let 50% of the tree vanish over ~13 CLEAN weeks); a run that fails
  it is never eligible, so ANY cumulative shrink bigger than 20% (a partition
  moved out of the tree, a purge, or several small ones) fails every week until
  the operator re-baselines. A `--from-stamp` rebuild of a week OLDER than a
  re-baseline keeps that week's own floor (the re-baseline governs only the weeks
  from its stamp on).
- **Re-baseline (operator, after checking the shrink is real):** the INCOMPLETE
  report prints the exact commands whenever the walk itself was sound (COMPLETE,
  full root, 0 walk errors, CSV counts match). Dry-run first, then record it, then
  rebuild that week's report:
  `.\.venv\Scripts\python.exe scripts\run_hygiene_drive_weekly.py --rebaseline <stamp>`
  then the same with `--apply`, then `--from-stamp <stamp> --apply` (reads
  UNVERIFIED -- nothing is compared that week). It appends a `rebaselined` row to the
  stamp ledger; from then on `<stamp>` is the prior and the cumulative anchor, and
  no older run (the baseline included) is a prior or part of the high-water for any
  later week. The baseline's files stay on disk
  (the RIDER B proposer reads them). Refused (exit 3, writes nothing) for a stamp the
  runner did not record, a rotated one, one with its own walk failures, or one older
  than an earlier re-baseline. If the mount was degraded instead, just re-run
  `--apply` once it is healthy.
- **Exit codes:** 0 ok; 1 incomplete walk; 2 G: unavailable; 3 refused (PS1 hash /
  config / a refused re-baseline); 4 PS1 failed; 5 stamp detection / collision; 6
  report write refused.
- **Notion:** the `[Hygiene] Drive` page stays Cowork-side -- the rewritten Cowork
  `hygiene-drive` task publishes this `.md` (keeps the orchestrator's 8-page count).
- **Dry-run (writes nothing):** `.\.venv\Scripts\python.exe scripts\run_hygiene_drive_weekly.py`
- **Register (elevated):** `.\deployment\setup-hygiene-drive-weekly-task.ps1`, then
  `.\.venv\Scripts\python.exe scripts\generate_task_estate_manifest.py --update-docs`.

---

## External Health Check

Cora writes a UTC timestamp to `data/health/heartbeat.txt` every 60 seconds.
If this file is older than 3 minutes, the process is stalled or dead.

```powershell
Get-Content "data\health\heartbeat.txt" -TotalCount 1
(Get-Date).ToUniversalTime() - [datetime]::Parse((Get-Content "data\health\heartbeat.txt" -TotalCount 1).Trim())
```

`heartbeat.txt` is contractually **one bare ISO-8601 UTC timestamp** — ten parsers
depend on that (cora-watchdog.ps1, health_endpoint, strategy_memo,
nightly_health_check, four KB-maintenance heartbeat guards, restart-cora.ps1, this
runbook). Never add a second line; new facts go in the two files below.

**Who wrote it** (added 2026-08-19, cq-7915a8647cff):

```powershell
Get-Content "data\health\instance.json"          # live pid, started_at, uptime_s,
                                                 # heartbeat_write_failures
Get-Content "logs\cora-instances.jsonl" -Tail 5  # append-only start/stop ledger
```

A restart is proven by a **new pid in a new `start` row**, not by an exit code. Do
not try to prove it from the log: `TimedRotatingFileHandler` pins the live log file
to the process's START date and moves each completed day to
`cora-<startdate>.log.<thatday>`, so a `cora-*.log` glob cannot see the startup line
of any instance older than one midnight.

If `heartbeat.txt` is stale but the log shows heartbeats flowing, look for
`HEARTBEAT_FILE_WRITE_FAILING` — the write is failing and the log says why (the
nightly health check treats that token as critical).

**Watchdog silence is now meaningful.** `logs/watchdog-<date>.jsonl` carries a
`tick` row at least hourly whenever the watchdog runs and is happy, so no lines =
the task is not running. The usual cause is a stuck `State=Running` instance with
no process behind it, which silently rejects every trigger (`LastTaskResult`
0x80070420 ALREADY_RUNNING): clear it with `schtasks /End /TN cora-watchdog`, and
if the task settings were never hardened run
`deployment\fix-watchdog-task-settings-2026-08-19.ps1` from elevated PS. The
nightly health check CRITICALs on a watchdog line older than 3h.


---

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

### Windowless tasks: the `pythonw.exe` launcher (2026-09-02)

Every Cora scheduled task action is wrapped as:

```
pythonw.exe deployment\run_hidden.py --name <slug> -- <original exe> <original args>
```

`pythonw.exe` is GUI-subsystem (Windows gives it no console at all) and it
spawns the real command with `CREATE_NO_WINDOW`, so no task fire flashes a
console window on the founder's desktop. Consequences for anyone at a prompt
during an incident:

- **There is one extra pid per running task**, a `pythonw.exe` whose command
  line contains `run_hidden.py`. It is the launcher, not a second instance.
  Doctrine 5's kill filter (`Name='python.exe' OR Name='cora.exe'`) does not
  match it, so the "one healthy instance = 2 matching processes" count still
  means what it says.
- **`restart-cora.ps1` kills the launcher explicitly.** It must: the launcher
  holds the task's running instance, and the service is
  `MultipleInstances=IgnoreNew`, so leaving it alive makes the next
  `Start-ScheduledTask` a silent no-op and Cora stays down.
- **Each task's stdout/stderr now lands in `logs\tasks\<slug>-<date>.log`** —
  output Task Scheduler used to discard. That is the first place to look when a
  task's Last Result is nonzero.
- **Last Result `224`-`227` (`0xE0`-`0xE3`) are the LAUNCHER's own failures**,
  not the script's: 224 malformed wrapped action, 225 `logs\tasks` unusable,
  226 could not start the child, 227 internal error (see
  `logs\tasks\_launcher-errors.log`). The nightly health check names these, and
  its `Windowless launcher` check reports any task that is not wrapped.
- The child runs in a kill-on-close **job object**, so a task killed at its
  `ExecutionTimeLimit` takes its whole process tree with it instead of
  orphaning the worker.

Re-wrap the estate (idempotent, dry-run by default) with:

```powershell
.\deployment\rewrap-tasks-hidden.ps1              # dry run
.\deployment\rewrap-tasks-hidden.ps1 -Apply       # ELEVATED; skips disabled tasks
```

**IMPORTANT — `schtasks /End` or `Stop-ScheduledTask` does NOT kill the Python process.** The task scheduler record changes state but the underlying `python.exe` keeps running. After any stop command, always kill orphan processes before restarting (and note the `pythonw.exe` launcher above — `restart-cora.ps1` is the maintained path and handles both):
```powershell
# Step 1: signal the task scheduler
Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
# Step 2: kill the actual Python process
Get-Process python* -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*cora*" } | Stop-Process -Force
# Fallback if the above misses it:
taskkill /F /IM python.exe /T
# Step 3: wait a moment, then start
Start-Sleep -Seconds 3
Start-ScheduledTask -TaskName "cowork-cora-service"
```

**Verify she's alive (single instance):**
```powershell
Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "cora.exe" } | Select-Object ProcessId, CreationDate
```
Or check the log for a single `heartbeat alive` sequence. Multiple interleaved uptime values = multiple instances running (use hard kill above).

> ⚠️ **Do NOT trust scheduled-task State to confirm Cora is alive.** Task Scheduler shows "Ready" both when idle and when crashed-and-not-restarted. The only reliable signal is a fresh `heartbeat alive` line in the log within the last 60 seconds.

**Invite to a new channel:** `/invite @Cora` in the Slack channel (manual Slack action — no code change needed).

**Update channel routing:** Edit `design/channel-routing.yaml`, commit, then restart the task:
```powershell
Stop-ScheduledTask -TaskName "cowork-cora-service"
Start-ScheduledTask -TaskName "cowork-cora-service"
```

**Update a system prompt:** Edit `design/system-prompts/{entity}.md`, commit, then restart the task. Prompts have no TTL cache — a restart is required for changes to load.

---

## Allowlist migration -- the ONE stop window (Code #13 slice 8)

D-303 (ruled 2026-09-10): the flat per-user Drive sweep of harrison@hjrglobal.com
runs in ALLOWLIST-BY-FOLDER mode (`drive_sweep_mode: allowlist` +
`drive_sweep_allowlist` on the PRIMARY row in `data/maps/monitored-email-accounts.yaml`;
v1 = the HJR-Founder-OS root only). The mode is SCRIPT-SIDE: `Cora - Drive Sweep`
reads the working tree at its next fire, so no restart is needed for the mode
itself. The migration -- purging the out-of-allowlist rows the denylist era
already ingested -- is what needs the ONE stop window below, because the KB
reclaim needs exclusive access to `data/cora_kb.db`.

**NOTE -- the "Stop (hard kill)" block under "Operating Cora" above is STALE and
is NOT the amended stop window.** It filters on `cora.exe` (no such process since
the service action became `-m cora.main`, see doctrine 5), it does not park the
watchdog or DISABLE the service task, and it does not kill the `pythonw.exe`
`run_hidden.py` launcher. Use the block in this section for any window that needs
Cora down for more than a moment. (The stale block is left in place deliberately;
this section supersedes it for stop windows.)

### Before the window -- NORMAL PowerShell, Cora running (read-only)

1. Build the manifest (read-only: SELECT on the KB + `files.get` under DWD as the
   account; writes only under `--out-dir`):
   ```powershell
   .venv\Scripts\python.exe scripts\drive_sweep_allowlist_manifest.py --db data\cora_kb.db --out-dir logs\drive-sweep-allowlist
   ```
   Re-runs are cheap: the folder cache persists at
   `logs\drive-sweep-allowlist\drive-sweep-allowlist-folder-cache.json` (override with `--cache`).
   **After RENAMING or MOVING a folder in Drive, delete that JSON (or pass
   `--cache <new path>`) before re-running:** a cached folder is never re-read, so
   the re-run would still render its OLD name and OLD parents (D-051 r3 docs#r3-0).
   A purge folder the manifest REFUSED is dropped from the cache automatically, so
   renaming just that folder is picked up either way; a move never is.
   `--limit N` previews the N largest files; `--account` defaults to harrison@hjrglobal.com.
2. EYEBALL the manifest (`logs\drive-sweep-allowlist\drive-sweep-allowlist-manifest-<date>.txt`):
   - `CANDIDATES TO ADD TO THE ALLOWLIST`: every out-of-tree TOP-LEVEL folder that
     holds KB rows. The kickoff rule: a tree-adjacent BUSINESS folder is ADDED to
     `drive_sweep_allowlist` in the yaml (the manifest prints the yaml line), never
     purged. Re-run step 1 after editing the yaml; the folder moves to IN-allowlist.
   - `UNRESOLVED / STALE KB ROWS` (404 / trashed / API error): NEVER in a purge set
     here; they are stale rows for the monthly kb-hygiene sweep.
   - `PURGE LINES`: one ready-to-run `purge_cora_internal_kb.py --folder-id <id>
     --expect-leaf '<name>' --impersonate harrison@hjrglobal.com` line per purgeable
     folder (the leaf a PowerShell single-quoted literal, apostrophes doubled, plus the
     step-4e `$kids` row to paste; an unsafe name prints `# REFUSED` instead -- Code #15
     C13-02), through the UNCHANGED positive-leaf gate (one folder per --apply,
     --expect-leaf must equal the resolved leaf, chain depth >= 3, complete
     enumeration, reviewed dry-run manifest). The gate REFUSES depth-2 folders
     (a folder directly under My Drive) and cannot reach loose files directly
     under My Drive -- the manifest flags both `unreachable by the gate -- move in
     Drive first` (the 9/9 precedent: pin the parent, purge per CHILD). Move those
     files/folders under a sub-folder in Drive, delete the folder-cache JSON (step 1;
     or pass `--cache <new path>`), re-run step 1, and the lines appear.
3. Run every PURGE LINE WITHOUT `--apply` in normal PS (Cora running is fine --
   dry-run). Each writes its own reviewed manifest at
   `logs\purge-cora-internal-folder-<id>.txt`; eyeball the file list and totals.
   Note the expected `Deleted:` totals per folder for step 4e.

### The window -- ELEVATED PowerShell (amended 4c -> 4f, copied from the 9/9 _notes runbook)

The block below is the AMENDED stop window from
`_shared/projects/cora/_notes/2026-09-09_fndr_RUNBOOK-post-smoke-remaining-items.md`
(the 4c/4f text as executed 2026-09-10), transliterated to ASCII (D-016) and with
the #12-specific purge list replaced by the manifest's PURGE LINES. Steps 4a/4b/4g
of that note (the #12 merge, its 7.5 reconcile, its pin script) do not apply here.

```powershell
# 4c. Park the watchdog AND the service task, then stop Cora.
#     AMENDED 2026-09-10 (Harrison, ask H RULED): the service task is DISABLED for the window, not just stopped.
#     `Stop-ScheduledTask` alone does not hold Cora down -- on 9/8 and again on 9/10 (pid 7048 at 08:45:06, ~11 min after
#     the stop) the task re-launched Cora mid-window and held the DB against reclaim. Disable first, re-enable at 4f.
Disable-ScheduledTask -TaskName "cora-watchdog"
Disable-ScheduledTask -TaskName "cowork-cora-service"
Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe'" |
    Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" -or $_.CommandLine -like "*cora.main*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*run_hidden.py*" -and ($_.CommandLine -like "*cora.main*" -or $_.CommandLine -like "*\Scripts\cora.exe*") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 3
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*cora.main*" -or $_.CommandLine -like "*\Scripts\cora.exe*" } |
    Select-Object ProcessId, Name
# Must print NOTHING. If a row prints, STOP.

# 4c-wait. The heartbeat wait (>= 300 s): Cora is only proven DOWN when the heartbeat file has NOT advanced for a full
#     watchdog period. Record it, wait, compare -- identical = down. If it moved, something relaunched Cora: go back to 4c.
$hb0 = (Get-Item data\health\heartbeat.txt).LastWriteTime
Start-Sleep 310
(Get-Item data\health\heartbeat.txt).LastWriteTime -eq $hb0
# Must print True.

# 4d. OPTIONAL full KB snapshot (10-20 min) if the last one is older than you like.
# .venv\Scripts\python.exe scripts\backup_logs.py --include-kb

# 4e. The purges: the per-folder --apply loop, one folder per --apply (a depth-2 parent is refused by the depth floor;
#     the PIN is on the parent, the PURGE is per child). Populate $kids from the manifest's PURGE LINES section (step 2):
#     paste each "runbook 4e $kids row (paste as printed)" line EXACTLY as printed -- the name is already a PowerShell
#     single-quoted literal with every apostrophe doubled (Code #15 C13-02: "Harrison's ..." names broke the old
#     hand-typed name='...' row). A "# REFUSED: folder <id>" line means the name cannot pass through PS 5.1 intact
#     (a double quote, a trailing backslash, a control character, an empty name, or ANY non-ASCII character --
#     including a typographic/curly apostrophe, which macOS/iOS autocorrect types in place of a straight one, or an
#     accented letter; D-051 r1 rb-pins#0 -- a plain ASCII apostrophe is fine, it is doubled): rename it in Drive,
#     delete logs\drive-sweep-allowlist\drive-sweep-allowlist-folder-cache.json (or pass --cache <new path>), and
#     re-run step 1 -- a cached folder is never re-read, so a re-run on a stale cache still renders the old name
#     (D-051 r3 docs#r3-0). Such a folder gets NO PURGE line until it is renamed, and the rename is a Drive connector
#     write -- Harrison's to make (plain ASCII, e.g. a straight apostrophe), never a Code session's. Each apply
#     REFUSES (nothing deleted) if its step-3 dry-run manifest is missing, does not cover every selected file (files
#     added since -> re-run step 3 first, or add --accept-delta only if you accept them), or --expect-leaf mismatches.
#     A folder whose dry-run showed 0 chunks deletes nothing.
$env:PYTHONIOENCODING = 'utf-8'   # silences the cosmetic middle-dot logging error seen on 9/10
$kids = @(
  @{id='<folder id>'; name='<leaf>'}   # <- replace with the manifest's printed $kids rows, verbatim
  # ... one row per PURGE LINE
)
foreach ($k in $kids) {
  "=== APPLY $($k.name) ==="
  .venv\Scripts\python.exe scripts\purge_cora_internal_kb.py --folder-id $k.id --impersonate harrison@hjrglobal.com --expect-leaf $k.name --apply 2>&1 |
    Select-String -Pattern "Deleted:|Applied|REFUSED|ERROR"
}
# Each folder with chunks: "Selected-rows intent written" -> "Deleted: {...}" -> "Applied record written" -> "Applied folder record written".
# Expect each Deleted total to equal that folder's step-3 dry-run count across each of the vec-cascade tables.
# If files landed in a folder since its dry-run the apply REFUSES it -> re-run its step-3 dry-run, eyeball, re-apply.

# 4f. Reclaim + re-enable the service task + THE ONE RESTART, then watchdog on.
#     AMENDED 2026-09-10 (ask H RULED): Enable-ScheduledTask for cowork-cora-service goes BEFORE restart-cora.ps1 -- Cora runs as
#     that task's instance (cora_health lists it "Running"), and a disabled task cannot be started. Reclaim runs while it is still disabled.
.venv\Scripts\python.exe scripts\reclaim_kb_space.py
Enable-ScheduledTask -TaskName "cowork-cora-service"
.\deployment\restart-cora.ps1
Enable-ScheduledTask -TaskName "cora-watchdog"
Get-Content logs\cora-instances.jsonl -Tail 1
# Proof of LIVE = a NEW pid in the instances tail (not the pid Cora had before 4c). A restart script's exit code is
# never the proof; the instances ledger is (doctrine 5).
```

### After the window

- The next `Cora - Drive Sweep` fire (06:00 AZ) logs `harrison@hjrglobal.com done -- mode=allowlist ...
  skipped_outside_allowlist=N` and run_sweep's `COMPLETE -- ... skipped_outside_allowlist=N`; the
  `Drive sweep DONE` line and the `--with-slack` summary carry the same counters.
- Re-run step 1: the OUTSIDE buckets you purged should now be empty; anything that
  re-appears was re-ingested by a DENYLIST account that can also see it (cross-user
  dedup deliberately does not let an allowlist skip poison other accounts) -- that is
  the other account's row to decide, not a sweep bug.
- Harrison's ALIAS rows (harrison@f3energy.com, harrison@lexingtonservices.com) are
  the SAME physical Drive as the primary and are NOT such an account: run_sweep
  collapses them into the primary's sweep (`alias collapse -- sweeping
  harrison@hjrglobal.com once (skipping alias rows: ...)` on the sweep log) and both
  rows carry `drive_sweep: false` (D-051 Code #13 review AD-1). If an outside file
  ever re-appears under `user_email harrison@f3energy.com` or `@lexingtonservices.com`,
  that IS a sweep bug -- the collapse or the flag regressed.
- `cora_self_inventory` in a founder channel lists the mode + allowlisted folder
  under `DRIVE SWEEP MODES`.

---

## Code #15 combined KB purge (S1 + RIDER B) — stop window

ONE dry-run INTENT and ONE apply carry every Code #15 KB change
(`scripts\purge_kb_code15_2026-09.py`): lane `s1_tokens` (S1, REDACT API-token shapes in
place) and the RIDER B DELETE lanes `rb2_personal_finances` (the pinned personal store,
expected 0), `rb3_static_old_paths` (kb-purge-rows.csv old static_md paths, existence-gated),
`rb3_archived_nonmd` (`_archive\dedup-2026-09`, drive_sweep rows only; drive_asset kept),
`rb4_desktop_ini` (expected 0) and `rb8_ufl_equity` (three UFL file ids, drive_sweep rows
only). Every record carries ids and counts only (D-082). The DELETE union is LARGE
(rb3_archived_nonmd alone is thousands of chunks), so the apply REFUSES unless Cora is
proven stopped -- it runs inside the stop window below. The ingest-side pins/belts of the
same bundle are live at their tasks' next fire (static sync 04:00, flat sweep 06:00, tree
walk 06:30); the bot-side part rides the bundle's ONE restart (4f).

### Before the window -- NORMAL PowerShell, from the PRIMARY checkout AFTER the merge (read-only)

1. The dry-run (opens the KB `mode=ro`; Drive via the direct SA, `files.get` / `files.list`
   only; writes ONLY `logs\kb-purge-code15-INTENT-<stamp>.json` + `.txt` and the id-only
   folder manifests under `logs\kb-purge-code15-folders-<stamp>\`):
   ```powershell
   .venv\Scripts\python.exe scripts\purge_kb_code15_2026-09.py --csv "C:\Users\Harri\Downloads\hjr-folder-audit\kb-purge-rows.csv"
   ```
   Defaults: `--db data\cora_kb.db`, `--out-dir logs`, `--founder-root "G:\My Drive\HJR-Founder-OS"`,
   `--hash-store data\state\static-md-content-hashes.json` (read, never written).
2. Harrison reviews the INTENT (`.txt`) -- the intent, not just the totals:
   - every lane's action / chunks / files / holds / STOPS, the `union` (`is_large`) and the residuals;
   - `rb3_static_old_paths`: the ARCHIVE row whose old path is still live is HELD (its move is deferred
     to folder-audit batch 2 -- a follow-up purge after it lands); the applied MOVE rows PURGE;
   - `rb3_archived_nonmd`: `sources_selected` = drive_sweep, `kept_by_source` = the drive_asset cards,
     LEX rows HELD, `chain_depth` 3; the whole-`_archive` scope and drive_asset deletion are NOT in it
     (open rulings);
   - `rb8_ufl_equity`: 25 / 25 / 10 per id and the `reingest_analysis` (flat sweep: NO; tree walk: LATENT);
   - `rb2_personal_finances` / `rb4_desktop_ini`: 0 = a recorded, applied no-op;
   - a lane with a STOP cannot be applied: fix the cause and re-run the dry-run, or deselect it at the
     apply with `--lanes`. A LEX release (`--release-lex <lane>`) is a ruling: re-run the dry-run with
     it, and the apply must repeat it.

### The window -- ELEVATED PowerShell (4c -> 4f, as in the allowlist section above)

```powershell
# 4c. Park the watchdog AND DISABLE the service task, then stop Cora (the amended 4c; Stop alone relaunches).
Disable-ScheduledTask -TaskName "cora-watchdog"
Disable-ScheduledTask -TaskName "cowork-cora-service"
Stop-ScheduledTask -TaskName "cowork-cora-service" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe'" |
    Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" -or $_.CommandLine -like "*cora.main*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*run_hidden.py*" -and ($_.CommandLine -like "*cora.main*" -or $_.CommandLine -like "*\Scripts\cora.exe*") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 3
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*cora.main*" -or $_.CommandLine -like "*\Scripts\cora.exe*" } |
    Select-Object ProcessId, Name
# Must print NOTHING. If a row prints, STOP.

# 4c-wait. Cora is DOWN only when the heartbeat has NOT advanced for >= 300 s.
$hb0 = (Get-Item data\health\heartbeat.txt).LastWriteTime
Start-Sleep 310
(Get-Item data\health\heartbeat.txt).LastWriteTime -eq $hb0
# Must print True. (The apply checks it again: the heartbeat must EXIST and be > 300 s old by its
# content stamp AND its mtime -- a missing or unparseable heartbeat REFUSES.)

# Backup. The KB delete is reversible ONLY from this copy (the run-kb-hygiene-apply.ps1 pattern: .db + -wal).
$bakstamp = Get-Date -Format "yyyyMMdd-HHmmss"
$bak = "data\cora_kb.db.bak-code15-$bakstamp"
Copy-Item data\cora_kb.db $bak
if (Test-Path data\cora_kb.db-wal) { Copy-Item data\cora_kb.db-wal "$bak-wal" }
Get-Item "$bak*" | Select-Object Name, Length

# Apply the REVIEWED INTENT with the SAME CSV (the apply re-checks its sha256 and re-runs the existence
# gate, re-runs the positive-leaf gate per folder lane and the UFL gate, and refuses drift -- every
# refusal happens BEFORE the first write). Add --lanes a,b for a subset; repeat any --release-lex.
# The folder lanes re-walk their Drive folders read-only first (~1-2 min each on 2026-09-24).
$env:PYTHONIOENCODING = 'utf-8'
.venv\Scripts\python.exe scripts\purge_kb_code15_2026-09.py --apply --manifest logs\kb-purge-code15-INTENT-<stamp>.json --csv "C:\Users\Harri\Downloads\hjr-folder-audit\kb-purge-rows.csv"
# Success writes logs\kb-purge-code15-APPLIED-<stamp>.json: its item_record is the per-lane
# APPLIED / APPLIED (no-op) / NOT APPLIED with held-why and stopped-why -- the record the folder audit
# reads before batch 2. outcome._delete_union.remaining_after must be 0.

# 4f. Reclaim (only if the freed space matters; it needs exclusive access), re-enable the service task
#     BEFORE the restart (a disabled task cannot be started), THE ONE RESTART, then the watchdog.
.venv\Scripts\python.exe scripts\reclaim_kb_space.py
Enable-ScheduledTask -TaskName "cowork-cora-service"
.\deployment\restart-cora.ps1
Enable-ScheduledTask -TaskName "cora-watchdog"
Get-Content logs\cora-instances.jsonl -Tail 1
# Proof of LIVE = a NEW pid in the instances tail (not the pid Cora had before 4c) -- never the script's exit code.
Get-Content data\state\egress-rails-armed.json
# first_armed_at must still read 2026-09-10T08:45:06 (the rails' clock survives the restart);
# last_armed_at and pid move to the new process.
```

Rollback: stop Cora again (4c + 4c-wait), then restore `data\cora_kb.db` (and `-wal`) from the
`.bak-code15-<stamp>` copy, and 4f.

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

## Startup Diagnosis

If Cora appears to start but shows no heartbeat, or fails silently, run this 4-step sequence:

**Step 1 — Check today's log:**
```powershell
cd C:\Users\Harri\code\cora
Get-Content "logs\cora-$(Get-Date -Format yyyy-MM-dd).log" -Tail 30
```
If empty or missing: log files are named by the date the process STARTED, not today's date. Check yesterday's log:
```powershell
Get-Content "logs\cora-$((Get-Date).AddDays(-1).ToString('yyyy-MM-dd')).log" -Tail 30
```

**Step 2 — Look for heartbeat or error:**
- `heartbeat alive` every 60s = Cora is running normally
- `UnicodeDecodeError` or `load_dotenv` crash = `.env` byte corruption (see `.env` Recovery below)
- `ImportError` or `ModuleNotFoundError` = wrong Python / outside venv (use `.venv\Scripts\python.exe` directly)
- `AuthenticationError` or `unauthorized_client` = token expired or malformed in `.env`
- No output at all = process died immediately; check for `SocketModeHandler raised` + traceback

**Step 3 — Confirm process is actually running:**
```powershell
Get-Process python* | Where-Object { $_.Path -like "*cora*" }
```

**Step 4 — Manual start for diagnosis (bypasses Task Scheduler):**
```powershell
cd C:\Users\Harri\code\cora
.\.venv\Scripts\python.exe -m cora
```
This surfaces errors directly in the terminal instead of log files.

---

## .env Recovery (byte corruption)

**Symptom:** Cora crashes on startup with `UnicodeDecodeError` or `load_dotenv` traceback mentioning `.env`.

**Cause:** PowerShell 5.1 writes files as Windows-1252 by default. If any script wrote to `.env` using PowerShell string methods (e.g., `[System.IO.File]::WriteAllText` without explicit UTF-8 encoding), it may have injected byte `0x97` (em dash in cp1252) or other multi-byte characters.

**Fix:**
1. Open `.env` in Notepad (File > Open > `C:\Users\Harri\code\cora\.env`)
2. Search (Ctrl+H) for `--` preceded by unusual whitespace, or look for any `—` (em dash) characters
3. Delete the corrupted character(s)
4. Save As > encoding = UTF-8 (NOT "UTF-8 with BOM")
5. Restart Cora (with orphan kill — see above)

**Verify fix:**
```powershell
cd C:\Users\Harri\code\cora
# Check for the specific bad byte:
$bytes = [System.IO.File]::ReadAllBytes("C:\Users\Harri\code\cora\.env")
($bytes | Where-Object { $_ -eq 0x97 }).Count
# Should return 0
```

**Prevention:** Never write to `.env` using PowerShell string interpolation. Always use Notepad or a UTF-8-aware editor. If scripting `.env` changes, use:
```powershell
[System.IO.File]::WriteAllText("C:\Users\Harri\code\cora\.env", $content, [System.Text.Encoding]::UTF8)
```

> ⚠️ **PowerShell .NET CurrentDirectory ≠ $PWD**: `[System.IO.File]` methods use `Environment.CurrentDirectory` (set at process launch), not the directory you `cd`'d to. A stray `[System.IO.File]::WriteAllText('.env', ...)` (relative path) will write to your home directory, not the cora repo. Always use absolute paths in .NET file operations.

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

## Identity inventory

Built 2026-09-19 (Code #13 RIDER 1, slice S-C; canon D-307 / D-308, ruled 2026-09-11).
This is the inventory D-308 names: every system Cora touches, WHO she is there,
which `.env` KEY holds the credential (key NAMES only -- a value never appears in
this file), how her actions appear to staff, whether the identity is admin-level,
and the CODE rail that bounds it.

**Doctrine (D-308):** admin-level only where the platform's API model requires it
for a locked lane (Fireflies precedent), credential inside the code seam,
Harrison-provisioned, listed here. Cora holds NO admin-level and NO human-style
account in Google Workspace, Slack, Asana or HubSpot (D-307). Never a UI/browser
admin, never a human-shaped seat, never for testing convenience.

**How to read the table.** "present"/"ABSENT" = whether the live `.env` carried a
`KEY=` line on 2026-09-19, checked by count only
(`Select-String .env -Pattern "^KEY="`), never by value. Drift guard: a key in
`.env.example` (active or `# `-commented) whose NAME ends in `_TOKEN`, `_KEY`,
`_SECRET`, `_PAT`, `_PASS`, `_PASSWORD`, `_PASSPHRASE`, `_JSON`, `_URL` or `_DSN`
and has no row in this table fails `tests/test_identity_docs.py` -- add the row when
you add the key. The guard keeps a short NAMED allowlist of public vendor ENDPOINT
keys that carry no secret (`POLAR_API_BASE_URL`, `POLAR_MCP_URL`, `POLAR_OAUTH_URL`,
`PHOTOROOM_BASE_URL`, `OTTERLY_BASE_URL`); a `*_URL` whose VALUE is the secret
(`HEALTH_PING_URL`, any webhook URL) is a credential and gets a row. A key that is a
credential but has none of those suffixes is invisible to the guard: add the row
AND a suffix, never rely on the guard alone.

| System | Identity (who Cora is there) | Credential KEY NAME (.env) | Appears as | Admin? | Rail (code) |
|---|---|---|---|---|---|
| Slack | Bot user of the "Cora" app, Socket Mode | `SLACK_BOT_TOKEN` (xoxb, present), `SLACK_APP_TOKEN` (xapp, present), `SLACK_SIGNING_SECRET` (present) | "Cora" (bot) | no -- 15 scopes, no `admin.*`, no `as_user` | `slack_egress.sanitize_text` (class-level WebClient patch + `tests/test_no_raw_slack_post.py`); staged-write gate; `data/maps/send-trust.yaml` approver ids |
| Slack (user token) | Would be Harrison's owner-level token | `SLACK_USER_TOKEN` (xoxp) -- documented (`.env.example` lines 31-33), **ABSENT from the live `.env`** (count 0; R5 closed 2026-09-11) | Harrison | owner-level if it existed | Script-only: `scripts/archive_sprawl_channels.py` reads it and HARD-FAILS without it (no bot-token fallback, ruled 2026-09-19 4b.4) -- in BOTH modes, dry-run included, before any Slack client is built, so nothing is read or archived. The script therefore cannot run until Harrison deliberately provisions the key. Do NOT add the key anywhere. |
| Slack (founder id) | The single approver / founder identity in code | `HARRISON_SLACK_USER_ID`, `CORA_FOUNDER_SLACK_ID` (both optional overrides, ABSENT; code default `U0B2RM2JYJ1`) | n/a | n/a | `tool_dispatch.py` `_FOUNDER_SLACK_ID` / `_HARRISON_SLACK_ID`, `user_access.py`, `review_lanes.py`, `send-trust.yaml`. Repo `CLAUDE.md` KEY IDS still lists `U02P3D6AT2C` -- UNDER VERIFICATION by `scripts/probe_slack_user_ids.py` (read-only `users.info` on both ids). |
| Anthropic | API key (model vendor; no identity surface) | `ANTHROPIC_API_KEY` (present) | n/a | n/a | `claude_client` token-budget guard (D-084); model never holds a credential |
| OpenAI | API key (KB embeddings only) | `OPENAI_API_KEY` (present) | n/a | n/a | embeddings batching cap (`knowledge_base/embeddings.py`) |
| Google Workspace (service account) | SA `cora-calendar@cora-calendar-readonly.iam.gserviceaccount.com` impersonating roster mailboxes via domain-wide delegation | `GOOGLE_SERVICE_ACCOUNT_JSON` (PATH to the key file under gitignored `.credentials/`; present). The DWD client id is the `client_id` field INSIDE that file -- never copied into a doc. `CORA_DRIVE_IMPERSONATE` (ABSENT; code default `harrison@hjrglobal.com`) | the impersonated user (a draft lands in that user's Drafts; reads run as that user) | no admin ROLE; the granted scope STRINGS are the guarantee (see Provisioning: Google service account + DWD) | scope strings matched character-for-character; `gmail.send` + `spreadsheets` write WITHHELD; Tier-1 header strip (`historical_access`); D-145 at ingest |
| Google Workspace (cora@hjrglobal.com) | Workspace USER: Tier-0 intake mailbox (S-A), Fireflies capture identity, Cora-voice send mailbox (R4, drafts only today) | none -- no password or per-user credential in `.env` or the repo; reached only through the SA + DWD like every roster mailbox | cora@hjrglobal.com | no admin role (D-307) | ONE mail consumer: the intake sweep (`scripts/run_mailbox_intake_sweep.py`, propose-only). Every roster reader that selects mailboxes by `enabled` + `dwd_eligible` alone -- the weekly finance-receipt digest (`finance_receipts._digest_accounts`, which FILES receipt-shaped attachments to Drive) first among them -- skips `intake_route` rows BY CODE; the chunk-ingesting consumers (thread sweep / attachment filer / drive sweep) are off via the row's false flags. Send lock is code-only (`CORA_SEND_LIVE`), not a scope boundary; see Provisioning: cora@hjrglobal.com |
| Fireflies | "Cora Global" seat on cora@hjrglobal.com (bot named "Cora NoteTaker") | `FIREFLIES_API_KEY` (present); fallback names `FIREFLIES_API_TOKEN`, `FIREFLIES_TOKEN` (ABSENT) | Cora Global / Cora NoteTaker | **YES -- one of two admin-level lanes** (the other is the Meet join audit read, next row). The platform's API model requires it: the workspace-wide `users` query (`fireflies_connector.list_team_members`, admin-only, verified 2026-06-08) and org-wide capture. This is the D-308 precedent, not a template. | credential inside the code seam; `capture_identity` in `data/maps/meeting-capture-roster.yaml`; LEX recaps -> custodians only (`meeting_recap`); coverage nudges roster-scoped (`fireflies_seat` flag) |
| Google Workspace (Meet join audit read) | The Cora SA impersonating a SUPER-ADMIN human subject (default harrison@hjrglobal.com; override `CORA_REPORTS_IMPERSONATE`; NEVER cora@ -- refused in code per D-307) for ONE call: Reports API `activities.list` `applicationName=meet` `eventName=call_ended` (Code #14 R14-8, ruled 2026-09-19 ask 9.6; D-324) | `GOOGLE_SERVICE_ACCOUNT_JSON` (the same key file as the SA row, its OWN credential object with `admin.reports.audit.readonly` alone); `CORA_REPORTS_IMPERSONATE` (ABSENT = harrison@) | n/a (a read; nothing is written anywhere, as anyone) | **YES -- admin-scoped READ, the second D-308 lane.** BLAST RADIUS: `admin.reports.audit.readonly` against a super-admin subject can read EVERY Workspace audit log (login, drive, admin, token, gmail ...), not only Meet; the narrowing to Meet is CODE (the constant `_APPLICATION = "meet"` + an AST source-pin test in `tests/test_meet_audit.py`), not the scope | `connectors/meet_audit.py` drops participant emails / names / IPs / locations at parse (only two in-memory counting keys leave the parse: a 12-hex endpoint hash, and a 12-hex PER-READ HMAC of the email identity to count distinct PEOPLE, keyed by random bytes minted and dropped inside each read; neither hash is ever stored, logged or printed -- the probe prints counts only); the audit ledger's Meet fields carry a state, a reason class, counts and event ids only; after the grant, `scripts/probe_meet_audit.py` (read-only) verifies the parser: its "parser keys not observed" line (the VERIFY-AT-BUILD names, `identifier_type` included) and its "people check" (0 identified people with >0 human endpoints = the `identifier_type` value is not what the parser expects, so every multi-endpoint meeting fails closed to "cannot tell"); DARK until Harrison grants the scope (the lane self-detects `unauthorized_client` = dark:scope, accessNotConfigured = dark:api, not-authorized = dark:subject); `nightly_health_check.check_meet_join_audit` WARNs on error / partial / contradiction |
| Asana | TODAY: Harrison's personal PAT ("Cora Email Watch") -- every Cora write is attributed to Harrison. AFTER the S-B flip: cora@hjrglobal.com MEMBER seat (13 named teams, never Harrison Private) | `ASANA_PAT` (Harrison's; present), `ASANA_PAT_CORA` (Cora's; present since 2026-09-11), selector `CORA_ASANA_IDENTITY` (ABSENT = `harrison`; the key lands with S-B) | Harrison today; Cora after the flip | today: inherits Harrison's privileges (a human credential in Cora's keyring); after: Member, never admin (R2) | staged-write gate on every Asana write tool; `pm_metrics` ledger = the Cora-side attribution ground truth; see Rotation: Asana PAT (Cora) |
| HubSpot | Private app on portal 246351746 | `HUBSPOT_PRIVATE_APP_TOKEN` (present), `HUBSPOT_PORTAL_ID` | the private app; every deal carries a HUMAN `hubspot_owner_id` | no seat, no admin (R7) | portal guard (D-029); staged-write gate; no Cora owner id exists |
| QuickBooks Online | Intuit OAuth app authorized by Harrison's Intuit login; per-realm refresh tokens in `.credentials/qbo-tokens.json` (gitignored via `.credentials/`; `qbo_oauth._TOKEN_FILE`) | `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_REDIRECT_URI`, `QBO_ENVIRONMENT` (all present) | the authorizing Intuit user | company-admin-authorized grant; code is READ-ONLY (`qbo_client.py` has 0 POST sites) | D-026; finance tool gate + egress (D-074); daily `qbo_token_status --alert` monitor |
| Shopify | F3E custom app (offline access token) | `SHOPIFY_F3E_STORE`, `SHOPIFY_F3E_ACCESS_TOKEN`, `SHOPIFY_F3E_API_KEY`, `SHOPIFY_F3E_API_SECRET` (all present) | the app | app-scoped, no human seat | inventory staged-write with floor-guarded delta; identity bound SERVER-SIDE, never an LLM echo |
| Deposco | Integration user, V1 GET-only | `DEPOSCO_PROD_USER` / `DEPOSCO_PROD_PASS` (present), `DEPOSCO_UA_USER` / `DEPOSCO_UA_PASS` (present), `DEPOSCO_TENANT`, `DEPOSCO_BU` | the integration user | no | write-impossibility invariant (`deposco_client` exposes `_get` only) |
| Klaviyo | Private API key | `KLAVIYO_API_KEY` (present) | n/a | no | read-only audit figures |
| Instagram / Meta | Long-lived user access tokens for the F3E / F3MOOD / F3PURE IG Business accounts; Meta app | `INSTAGRAM_F3E_ACCESS_TOKEN`, `INSTAGRAM_F3MOOD_ACCESS_TOKEN`, `INSTAGRAM_F3PURE_ACCESS_TOKEN` (+ the `*_USER_ID` ids), `META_APP_ID`, `META_APP_SECRET` (all present) | the brand account | account-scoped | read-only monitoring (influencer scan); credit rule D-025 |
| Notion | Internal integration token | `NOTION_API_KEY` (present) | the integration | no | read lane (press sweep, KB sync); `notion_connector.py` also carries 2 POST/PATCH sites -- NOT audited in this inventory |
| Airtable | API key (read) + a separate WRITE key | `AIRTABLE_API_KEY` (present), `AIRTABLE_WRITE_API_KEY` (ABSENT = the org-tracker write lane is dark) | the token owner | no | `airtable_org_tracker` refuses to write without the WRITE key |
| MCP local-HTTP bridge (SHELVED) | Loopback bridge for Claude Code; the plugin won | `CORA_MCP_HTTP_TOKEN`, `CORA_MCP_HTTP_CERT`, `CORA_MCP_HTTP_KEY`, `CORA_MCP_HTTP_PORT` (all documented, unused) | n/a | n/a | 127.0.0.1 bind + Host allowlist; mode=ro |
| healthchecks.io (dead-man heartbeat) | Outbound ping URL; the UUID INSIDE the URL is the credential (anyone holding it can keep the check green) | `HEALTH_PING_URL` (present; exactly ONE line -- the 2026-06-11 incident was a second, placeholder line that dotenv took as the value), `HEALTH_PING_INTERVAL_S` (interval only) | n/a | no | `health_endpoint` ping loop: off when blank; skips the ping while the heartbeat is stale so a wedged bot still trips the external alert |
| DR secrets bundle (`backup_logs.py` / `restore_secrets.py`) | Passphrase that encrypts `.env` + the SA JSON into `secrets-YYYY-MM-DD.enc`; the ONLY way back into every other row after a machine loss | `CORA_BACKUP_PASSPHRASE` -- deliberately NOT in `.env` (count 0) and only `# `-commented in `.env.example`: `.env` is INSIDE the bundle, so the passphrase must live OUTSIDE what it encrypts. Held as a persistent User-scope Windows env var on the host + the canonical copy in the password manager (2026-06-09 DR hardening, D-040). | n/a | n/a | `backup_logs.py` SKIPS the secrets step when the key is unset (never writes plaintext); `restore_secrets.py` reads the env var, else prompts. A new machine cannot restore any credential in this table without it -- see the Bootstrap cross-reference below |
| Other API keys (no identity surface) | vendor keys | `POLAR_API_KEY`, `PHOTOROOM_API_KEY`, `MAKE_SALES_DECK_WEBHOOK_URL`, `PERPLEXITY_API_KEY`, `GEMINI_API_KEY`, `OTTERLY_API_KEY` (present); `POLAR_CLIENT_ID` / `POLAR_CLIENT_SECRET` (**ABSENT** -- count 0 each, re-counted 2026-09-24, Code #15 rider R1-09); `APOLLO_API_KEY` (LEGACY -- not read by current code) | n/a | n/a | per-connector caps and fail-soft |

Bootstrap cross-reference: `deployment/bootstrap-new-machine.md` regenerates ONLY
the 4 Slack/Anthropic secrets; every other row above is a Harrison-provisioned
credential that a new machine restores from the encrypted secrets bundle
(`backup_logs.py` / `restore_secrets.py`), not by regeneration. PREREQUISITE: that
restore needs `CORA_BACKUP_PASSPHRASE` (the DR secrets bundle row) from the password
manager -- it is not in `.env`, not in the bundle and not on the destroyed machine.
Without it the bundle is opaque and every row above must be re-provisioned by hand.

---

## Rotation: Asana PAT (Cora)

Two Asana tokens exist; the bot and the scripts load exactly ONE of them, chosen by
`CORA_ASANA_IDENTITY` (slice S-B of Code #13 RIDER 1 introduces the selector;
until it lands, `ASANA_PAT` is the only token read):

- `ASANA_PAT` -- Harrison's personal token ("Cora Email Watch"). Every Cora write
  made with it is attributed to Harrison in Asana.
- `ASANA_PAT_CORA` -- the token of the cora@hjrglobal.com MEMBER seat (13 named
  teams, never Harrison Private). Present in `.env` since 2026-09-11. The first
  token issued for the seat was exposed in a chat and DELETED in Asana; the one in
  `.env` is the re-issued token. Never paste a PAT into a chat, a doc or a commit.
- `CORA_ASANA_IDENTITY` -- `harrison` (default when absent = today's behaviour) or
  `cora`. `cora` with `ASANA_PAT_CORA` missing is a HARD FAIL at first use, never a
  silent fallback to Harrison's token.

The token is read via `os.environ` at call time in three seams
(`asana_client`, `asana_connector`, `lex_client`) plus four scripts; `load_dotenv`
populates the environment once at process start, so a `.env` change is BOT-LOADED:
the bot picks it up only at the next restart, scripts at their next fire.

### The flip (Harrison's hand, after reading the visibility diff)

1. Run the READ-ONLY diff (S-B): `.venv\Scripts\python.exe scripts\asana_visibility_diff.py`
   -- per-query counts under both tokens and the set difference of task/project
   gids (names only). Any project Cora would LOSE is a line for Harrison to add the
   cora@ seat to in Asana, never a code workaround. A lost catch-all
   `Operations -- General` project is a STOP.
2. Post the announce line in #info-for-cora: "Heads up: Cora's Asana tasks and
   comments now show as Cora, not Harrison. Nothing else changes."
3. Edit `.env`: set `CORA_ASANA_IDENTITY=cora`. Verify exactly ONE line:
   `Select-String .env -Pattern "^CORA_ASANA_IDENTITY="` (dotenv takes the LAST
   duplicate -- the 2026-06-11 HEALTH_PING_URL incident).
4. Restart from ELEVATED PowerShell: `deployment\restart-cora.ps1`. Proof of the
   restart = a NEW pid row in `logs/cora-instances.jsonl`, never the script's exit
   code.
5. Smoke: a `users/me` probe under the active token returns the cora@ user, not
   Harrison -- the nightly health check's "Asana API" line
   (`nightly_health_check.check_api_connectivity`; by hand:
   `.venv\Scripts\python.exe scripts\nightly_health_check.py --dry-run --verbose`)
   runs exactly that probe through `asana_identity.resolve_pat()` and classifies
   the answer
   (`asana_identity.classify_users_me`: CRITICAL if the email is not the cora@
   seat under `CORA_ASANA_IDENTITY=cora`). (A hygiene-nudge `--dry-run` is NOT a
   smoke for this: it posts no comment and prints no author or identity.)

### Rotating `ASANA_PAT_CORA` itself

1. Sign in to Asana AS cora@hjrglobal.com (Harrison, via the Google account
   switcher -- see Provisioning: cora@hjrglobal.com). Open
   https://app.asana.com/0/my-apps -> Personal access tokens -> Create new token.
2. Paste the value into `.env` after `ASANA_PAT_CORA=` (Notepad; one line; no
   trailing whitespace). Verify exactly one `^ASANA_PAT_CORA=` line.
3. Restart (step 4 above). Smoke (step 5 above).
4. ONLY THEN revoke the old token in the same Asana page.

### 14-day retention of Harrison's PAT, then removal

Harrison's `ASANA_PAT` stays in `.env` (loaded only while
`CORA_ASANA_IDENTITY=harrison`) for 14 clean days after the flip -- the rollback
path. On day 14 Harrison removes the `ASANA_PAT=` line from `.env` and revokes the
"Cora Email Watch" token in Asana. Two guards before removal:

- `scripts/nightly_health_check.py` is identity-aware since S-B (same rider): it
  no longer reads `ASANA_PAT` directly -- it asks `cora.asana_identity` for the
  ACTIVE identity's key, so after the flip it requires `ASANA_PAT_CORA` and stays
  green when Harrison's key is removed. Before the flip (selector absent) it still
  requires `ASANA_PAT`: do NOT remove the key while the selector is unset, or the
  health check alarms every night.
- Queue row `cq-40baab26d7f3` (Asana identity flip + Harrison PAT retirement) closes
  at the removal, via `process_queue_action`, never by hand-editing the ledger.

Removal record (a runbook line, not a code step -- fill in by hand):

    ASANA_PAT (Harrison, "Cora Email Watch") removed from .env and revoked in Asana on: ____-__-__

---

## Provisioning: cora@hjrglobal.com

cora@hjrglobal.com is a Google Workspace USER with NO admin role (D-307). It exists
for three roles, each bounded by a code rail; it is never operated as a human-style
Cora account.

1. **Tier-0 intake mailbox (slice S-A).** A row in
   `data/maps/monitored-email-accounts.yaml`: `entity_default: FNDR` (system
   mailbox), `fireflies_seat: false`, `thread_sweep: false`,
   `attachment_filer: false`, `drive_sweep: false`, and the S-A key
   `intake_route: knowledge_review`. Inbound mail becomes PROPOSALS in the
   Harrison-gated knowledge-review queue (D-143 / D-144: pending is not published;
   approval verified at the consumer), LEX/PHI content refused at ingest and again
   at apply (D-145), never autowritten (`CORA_AUTOWRITE_LIVE` tiers do not apply --
   Tier-0 by construction). Same code path as #info-for-cora (`info_intake.ingest`),
   never a second copy. **cora@'s mail has exactly ONE consumer**: that intake sweep
   (`scripts/run_mailbox_intake_sweep.py`, task "Cora - Mailbox Intake Sweep"). The
   false ingest flags switch off the chunk-ingesting consumers (thread sweep,
   attachment filer, drive sweep), but the weekly finance-receipt digest
   (`finance_receipts._digest_accounts`, task `cowork-cora-finance-receipt-digest`,
   Mon 10:30 AZ) selects mailboxes by `enabled` + `dwd_eligible` alone and would
   otherwise FILE a receipt-shaped attachment forwarded to cora@ into the Receipts &
   Invoices Inbox Drive folder and list it in #hjr-finance -- a second, WRITING
   consumer. It skips `intake_route` rows by code (Code #13 RIDER 1 D-051
   remediation), so an invoice emailed to cora@ as a knowledge note is a pending
   proposal and nothing else. Any future roster reader keyed on `enabled` +
   `dwd_eligible` must skip `intake_route` rows the same way; the roster comment and
   the `.env.example` cora@ block state the ONE-consumer rule for that reason.
2. **Fireflies capture identity.** `data/maps/meeting-capture-roster.yaml`
   `capture_identity: "cora@hjrglobal.com"` -- the ONE Fireflies seat ("Cora
   Global", ACTIVE + ADMIN since 2026-08-28; bot "Cora NoteTaker") whose connected
   calendar auto-joins meetings. The DWD roster row keeps `fireflies_seat: false`:
   the seat is tracked in the capture roster and is never double-flagged, so
   `load_dwd_humans()` (the weekly coverage monitor) never nudges cora@.
3. **Cora-voice send mailbox (R4).** Today T0: Cora drafts into cora@'s Drafts and a
   human taps Send. T1 (send-trust ladder with mailbox = cora@) waits for the
   October external-WRITE gate. **The send lock is CODE, not a scope boundary.** The
   DWD grant is already send-capable: `gmail.modify` and `gmail.compose` (both
   requested by code) and the granted `https://mail.google.com/` each authorize
   `users.messages.send`, so withholding `gmail.send` is hygiene, not the control.
   What keeps cora@ -- and every mailbox -- from sending is `CORA_SEND_LIVE`
   (default off; env off beats approval) gating the ONE send call site in the
   codebase, `src/cora/revops/sender.py` `_gmail_send_raw` (CI guard
   `tests/test_no_raw_gmail_send.py`), whose mailbox must also sit in the playbook
   allowlist AND the code-level `V1_MAILBOX_UNIVERSE` (`harrison@hjrglobal.com`
   only today -- cora@ is not in it). Opening T1 for cora@ is a code change to
   that universe plus the flag, reviewed under D-051; no Admin-console change
   makes or unmakes it.

**Human operator = Harrison, via the Google account switcher.** Two Workspace
accounts live in one browser profile, so any Chrome-driven session that touches
Gmail / Calendar / Drive reads the ACTIVE account before acting.
**NO Gmail delegates on cora@** (amended 2026-09-11); the Admin-console "allow
mail delegation" toggle is NOT needed and stays off.

**Access path.** Cora reaches cora@ exactly as she reaches every roster mailbox:
the service account impersonates it under domain-wide delegation. No password and
no per-user credential for cora@ exists in `.env`, the repo or the secrets bundle.

**Staged READ-ONLY smoke (Harrison's say-so before it runs; ids only in output):**

- Gmail: `gmail_reader._build_service("cora@hjrglobal.com")` ->
  `users().messages().list(userId="me", maxResults=1)` -- scope `gmail.modify`.
- Calendar: `calendar_client._build_service("cora@hjrglobal.com", write=True)` ->
  `events().list(calendarId="primary", maxResults=1)` -- scope `calendar.events`.
  The `write=False` build uses `calendar.freebusy`, under which `events.list`
  returns 403 (2026-06-11 finding) -- a spurious "DWD error" if you use it.

**Checklist.** Workspace user exists, no admin role | DWD roster row present with
the flags above | `capture_identity` matches | no Gmail delegates | no Slack seat
(R5) | no HubSpot seat (R7) | Asana: the MEMBER seat only (R2).

---

## Provisioning: Google service account + DWD

**Service account:** `cora-calendar@cora-calendar-readonly.iam.gserviceaccount.com`
(identity, not a credential; recorded in `.env.example` and decisions.md
2026-08-26). The key file lives under the gitignored `.credentials/` folder and
`GOOGLE_SERVICE_ACCOUNT_JSON` holds its PATH. The OAuth client id that the
domain-wide delegation grant is keyed on is the `client_id` field INSIDE that JSON:
read it from the file when you need it, never copy it into a doc, ticket or chat.

**Where the grant lives:** admin.google.com -> Security -> Access and data control
-> API controls -> Manage Domain Wide Delegation -> the SA's client id -> the scope
list. The grant covers the domains listed in the header of
`data/maps/monitored-email-accounts.yaml` (hjrglobal.com, f3energy.com,
lexingtonservices.com, unitedfightleague.com, bigd.media); personal Gmail is never
eligible.

**The character-for-character rule (decisions.md, 2026-08-26 entry, filed with
D-245 / D-246):** Google matches the REQUESTED scope string against the GRANTED list
literally. A broader granted scope does NOT satisfy a request for a narrower one:
the SA held `gmail.modify` and `https://mail.google.com/` and the draft path still
failed `unauthorized_client` because it requested `gmail.compose`, which was absent.
When a code path adds a scope, add that EXACT string to the grant.

**Scopes the CODE requests** (each builder re-verified 2026-09-24, Code #15 rider
R1-11, and cited by SYMBOL -- the scope constant or the credential builder that
passes `scopes=` -- never by line number, and never a docstring, comment or error
f-string that merely MENTIONS a scope. Keep this list in step with
`grep -rn "googleapis.com/auth/" src/ scripts/`; note that grep, like the
`tests/test_identity_docs.py` drift guard, also counts those prose mentions, so a
hit is a candidate, not a builder):

| Scope (suffix of `https://www.googleapis.com/auth/`) | Builder(s) | Lane |
|---|---|---|
| `gmail.modify` | `src/cora/connectors/gmail_reader.py` `_GMAIL_SCOPES` | thread sweep, attachment filer, intake |
| `gmail.compose` | `src/cora/tools/gmail_client.py` `_SCOPES` | draft lane; the scope itself permits `messages.send` -- draft-only is enforced by CODE (`CORA_SEND_LIVE`, see Provisioning: cora@hjrglobal.com item 3), not by the absence of `gmail.send` (NOT requested and NOT granted) |
| `calendar.events` | `src/cora/tools/calendar_client.py` `_SCOPES_WRITE` (selected when the credential builder is called with `write=True`) | `events.list` + event create |
| `calendar.freebusy` | `src/cora/tools/calendar_client.py` `_SCOPES_READ` | `freebusy.query` only (`events.list` 403s under it) |
| `drive.readonly` | `src/cora/connectors/drive_sweep.py` `_DRIVE_SCOPE` (used by `_build_drive_service` via DWD and by `_build_sa_drive_service_direct` on a direct SA credential; `scripts/run_drive_sweep.py` reaches it through `drive_sweep.run_sweep`); `scripts/run_lex_dump_folder_sync.py` `_build_drive_service` (inline `scopes=`) | Drive sweeps |
| `drive` (full) | `src/cora/connectors/drive_connector.py` `_DRIVE_SCOPES` (used by its `_build_drive_service`; `scripts/backfill_drive_assets.py` reaches it through `drive_connector.backfill`, so it requests FULL `drive` -- the `drive.readonly` line in its docstring is prose, not a request); `scripts/run_retroactive_hashtag_scan.py` `_get_sheets_service` (inline `scopes`) | attachment filer upload, finance-receipt filing, Drive asset backfill |
| `spreadsheets.readonly` | `src/cora/connectors/drive_sweep.py` `_SHEETS_SCOPE`, `src/cora/tools/fighter_tracker_client.py` `_SCOPES` (DWD); `src/cora/connectors/gsheets_financials.py` (`_DRIVE_SCOPES`) requests it on a DIRECT service-account credential (no impersonation), NOT via DWD | oversized-sheet fallback, fighter roster; cash sheet reads (direct SA) |
| `drive.metadata.readonly` | `src/cora/connectors/gsheets_financials.py` (`_DRIVE_META_SCOPES`) | DIRECT service-account credential (no impersonation) -- NOT a DWD scope, needs NO Admin-console grant, never paste it into the DWD list; cashflow sheet modifiedTime (the "as of" label) only (Code #14 S5) |
| `admin.reports.audit.readonly` | `src/cora/connectors/meet_audit.py` (`REPORTS_SCOPE`, its own credential) | Meet join audit (Code #14 R14-8): Reports `activities.list` `applicationName=meet` `eventName=call_ended` ONLY, admin subject (default harrison@, `CORA_REPORTS_IMPERSONATE`, never cora@), READ-ONLY; the second D-308 admin-level lane. NOT YET GRANTED -- ships dark until Harrison adds it (the lane self-detects `unauthorized_client`); after granting, run the read-only `scripts/probe_meet_audit.py` (the Identity inventory's Meet join audit row says what its lines must show) |

**Granted but not requested by code** (as recorded 2026-08-26 on Harrison's "more
capability" ruling): `gmail.readonly`, `https://mail.google.com/`, `calendar`
(full), `documents`, `admin.directory.user.readonly`. Reconcile against the Admin
console before pruning any of them -- this document is not the console.

**Editing the grant: the Admin-console edit REPLACES the whole list.** The SA's
Domain Wide Delegation entry holds ONE comma-separated scope string, and saving the
edit replaces every scope the entry held with exactly what is in the field (the
1ssssss lock). Typing only the new scope REVOKES all the others, and every DWD lane
(gmail thread sweep, attachment filer, calendar ensure lane, drive sweeps, the draft
lane) then fails `unauthorized_client`. Never edit the list in place:

1. Read the console's CURRENT list first (the path under "Where the grant lives")
   and diff it against the recorded grant below. If they differ, STOP: the console is
   the truth. Rebuild the paste line from the console read-back plus the one new
   string, and correct this section. (If the proposed tightening of the entry to the
   7 code-requested strings has run, the console will NOT match this section.)
2. Paste the WHOLE line below -- never a single scope -- and save.
3. Read the console back and confirm it shows exactly these strings, no more, no
   fewer.

Recorded grant (12 scopes): the seven granted code-requested DWD scopes in the table
(every row except the direct-SA `drive.metadata.readonly` and the not-yet-granted
`admin.reports.audit.readonly`) plus the five granted-but-not-requested ones -- the
2026-08-26 grant, confirmed verbatim by the
live console read-backs of 2026-09-19 ~19:05 AZ and 2026-09-20 ~16:21 AZ
(`00-Founder/projects/set-up-cora-calendar-meeting-notetaker/2026-09-19_fndr_fireflies-dwd-settings-audit.md`:
one remaining entry, 12 scopes, "leave-at-12"). `drive.metadata.readonly` is NOT in
it and never goes in it (a direct-SA credential, see the table).

Source reconciliation: the 2026-09-19 fireflies rollout doc says the SA "holds
.../auth/calendar" and that the domain-wide roster slice "needs"
`admin.directory.user.readonly`. That doc was written BEFORE that evening's console
read-back, which recorded `admin.directory.user.readonly` (and `calendar`) as already
granted; this section trusts the dated console read-back over a planning doc's
statement of need. The asymmetry makes that the safe side either way: pasting a scope
that is already granted changes nothing, while omitting a live one revokes it.

**Paste-ready FULL list** = the recorded grant + `admin.reports.audit.readonly`
(Code #14 R14-8, the Meet join audit read; NOT YET GRANTED). Diff first (step 1):

```text
https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/gmail.readonly,https://mail.google.com/,https://www.googleapis.com/auth/gmail.compose,https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/calendar.events,https://www.googleapis.com/auth/calendar.freebusy,https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/spreadsheets.readonly,https://www.googleapis.com/auth/documents,https://www.googleapis.com/auth/admin.directory.user.readonly,https://www.googleapis.com/auth/admin.reports.audit.readonly
```

**Deliberately WITHHELD:** `gmail.send` (a posture change gated on the October
external-WRITE seam, R4) and `spreadsheets` (write) -- "Cora cannot write the
Standing ACTUALS sheet" is guaranteed by the SCOPE today; granting write would
downgrade that guarantee to policy. The two differ in kind: withholding
`spreadsheets` IS the write lock, but withholding `gmail.send` is NOT the send
lock -- `gmail.modify`, `gmail.compose` and the granted `https://mail.google.com/`
already authorize `messages.send`. The send lock is code-only (`CORA_SEND_LIVE` +
the single call site in `revops/sender.py`); keep `gmail.send` out as hygiene, but
never cite its absence as the guarantee. The 13WCF worksheet writes use direct SA auth on
directly-shared files, outside DWD entirely.

**Impersonation default:** `CORA_DRIVE_IMPERSONATE` (default `harrison@hjrglobal.com`
in `drive_connector.py:52`); per-mailbox sweeps pass the roster email explicitly.
`gsheets_financials.py` also defines that default, but only its dead delegated
path (`_build_delegated_creds`) reads it and no caller reaches that path: the cash
sheet is read with direct service-account credentials.
The Meet join audit read (`meet_audit.py`) resolves its OWN admin
subject per call from `CORA_REPORTS_IMPERSONATE` (default `harrison@hjrglobal.com`) and
hard-refuses cora@hjrglobal.com (D-307).

**Rotating the SA key:** Google Cloud console -> IAM & Admin -> Service accounts ->
the SA -> Keys -> Add key (JSON) -> save the file under `.credentials/` -> point
`GOOGLE_SERVICE_ACCOUNT_JSON` at the new path -> restart (elevated
`deployment\restart-cora.ps1`; new pid in `logs/cora-instances.jsonl`) -> run one
nightly sweep or a read-only DWD smoke -> delete the OLD key in the console. The DWD
grant is keyed on the client id, not the key, so no Admin-console change is needed.
The encrypted secrets bundle (`backup_logs.py`) picks up the new file at its next
run; verify `Offsite verify: PASS` afterwards.

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
A user hit the per-user (30/hr, `rate_limiter._USER_LIMIT`) or the channel hit the per-channel (50/hr, `_CHANNEL_LIMIT`) cap -- the numbers live in `src/cora/rate_limiter.py`; the Slack refusal copy is derived from the same constant (DR/VM step-1 M3 fixed a "10/hour" copy drift). This is normal during stress tests and load bursts. Caps reset automatically after 60 minutes — no action needed.

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
