# Cora — Code Session Context

This file is the authoritative startup read for every Code session.
Read this first, then check `decisions.md` for the full decision log.
The TOP OF MIND block below is a hand-maintained POINTER table (one row per merged bundle,
newest last) -- a Code session that lands a bundle adds/refreshes ITS row in its docs slice;
the pre-2026-09-23 narrative TOM lives in git history (`git show 1e93c99:CLAUDE.md`).

---

## STANDING OPERATING LOOP -- standing doctrine; read first; applies to EVERY Cora Code session + cascade

This is the canonical cadence for all Cora build/fix work. It is standing doctrine, not session-specific. Each new chunk of work runs these 8 steps in order:

| # | Step | Who | Gate / rule |
|---|------|-----|-------------|
| 1 | **Branch** off `main` | Code session | `git checkout main` -> `git pull` -> `git checkout -b claude/<name>`. Never build on `main`. |
| 2 | **Build test-gated** | Code session | Import-smoke (`from src.cora.app import app`) + full pytest on EVERY slice. Never commit on red. One commit per verified slice (`git commit -F <tempfile>`; no PS here-strings -- BOM). VERIFY-FIRST: reconcile every plan/audit claim against live code before building. |
| 3 | **Adversarial review** | Code session | Required before any bot restart or irreversible/high-stakes change (D-051). Parallel multi-agent diff review; fix confirmed defects, re-gate. A green suite is NOT sufficient alone. |
| 4 | **Push the feature branch** | Code session | `git push -u origin claude/<name>`. NEVER push/commit to `main` from a Code session. |
| 5 | **SAVE** | Code session | Update the execution log + Drive design docs; update the Code-agent memory (`project_*.md` + `MEMORY.md`); write a dated capture note to `00-Founder/_session-captures/YYYY-MM/`. |
| 6 | **CASCADE** | Code preps -> Cowork executes (thumbs-up-gated) | Code drafts the canonical entries into the cascade report (new dated section: founder `CLAUDE.md` TOM entry + `memory/decisions.md` entries). Cowork PROMOTES them one at a time on Harrison's thumbs-up (D-011). UPDATE/supersede stale TOM entries -- never duplicate. Code sessions must NOT write founder `CLAUDE.md`/`decisions.md` directly. |
| 7 | **MERGE** | Harrison | After review: `git fetch . claude/<name>:main` then `git push origin main` (PS 5.1 -- two lines, no `&&`). Fast-forward pointer move. Code stages; Harrison runs. |
| 7.5 | **RECONCILE the queue** | Code stages -> executed at/after merge | Transition the code-queue seeds the merged branch closes: staged one-shot script (dry-run default) calling `code_queue.process_queue_action(ACTION_MARK_SHIPPED, cq_id, HARRISON_ID)`; never hand-edit the jsonl/backlog. Without this the queue reads stale-positive -- sessions and the Monday menu re-offer shipped work (2026-07-31 incident: 14 D-095 seeds still PROPOSED post-merge). |
| 8 | **RESTART** | Harrison | ONLY if bot-loaded modules changed. `deployment\restart-cora.ps1` (elevated), after the merge, then live smoke. Script-side changes need NO restart (working-tree-is-live). |

**Decision rules:**
- **Script-side vs bot-loaded -> restart?** A scheduled-task script activates at its next fire from the working tree (no restart). A module the always-on bot imports activates only at the next restart. Know which before promising "live."
- **STOP and ask Harrison before:** a bot restart, any connector write (Slack/HubSpot/Asana/Drive/QBO), elevated/irreversible PowerShell, or any KB write/delete. Stage those (`--dry-run` / vetted script); Harrison executes.
- **Canonical memory is thumbs-up-gated (D-011).** Founder `CLAUDE.md` TOM + `memory/decisions.md` change only with Harrison's explicit thumbs-up, via Cowork -- never from a Code session.
- **Cascade ordering:** step 6 may run AFTER step 7 (merge) so the cascade entry can cite the merged hash. The loop is closed once step 6 is done; restart (8) is N/A when no bot-loaded code changed.
- **The Full Cascade Prompt** (the reusable text Harrison pastes into Cowork to run step 6) lives in the cascade report appendix at `_shared/projects/cora/*COWORK-CASCADE-REPORT.md`.

---

## TOP OF MIND (TOM)

**This block is a POINTER, not a second canon (D-087).** Regenerated 2026-09-23 by the DR/VM
step-1 Code session from the cascade reports since 8/30; the previous 1,690-line narrative TOM
(45 entries, 2026-05-28..08-01, every one of them since merged/restarted) is in git history
(`git show 1e93c99:CLAUDE.md`) and in the reports it pointed at. Canon = founder `CLAUDE.md` +
`memory/decisions.md` (Harrison-gated, D-011); build history = `_shared/projects/cora/*CASCADE-REPORT.md`.

| Merged | Bundle (branch) | What landed | Report |
|---|---|---|---|
| 8/30 | Code #11 integrity rails (`claude/integrity-rails-2026-08-30`) | sentinel seam on 13 write tools, test/prod isolation, run-marker contract, uptime parser; S7 reverted | `2026-08-30_fndr_cora-integrity-rails-CASCADE-REPORT.md` |
| 9/2 | windowless tasks (`claude/windowless-scheduled-tasks`, tip `faf2296`) | `pythonw` + `run_hidden.py` launcher, `_task-action.ps1` shared by every setup script, estate rewrapped, task stdout in `logs/tasks/`; D-266..268 | `2026-09-02_fndr_cora-windowless-tasks-CASCADE-REPORT.md` |
| 9/3 | claude-workspace mirror (`9f257e6`) | two-zone D-057 mirror of skills/Cowork tasks/agent memory into the Founder OS; parent `_shared/projects/cora` folder pinned + purged; twice-daily sync | `2026-09-03_fndr_cora-claude-workspace-mirror-CASCADE-REPORT.md` |
| 9/8 | ingest-integrity bundle (`8682ff2`) | banking redaction at every chunk renderer, Leak #2 closed, Computers roots pinned + purged, `cora_self_inventory`, flat sweep leaves Founder-OS markdown to static_md | `2026-09-08_fndr_cora-ingest-integrity-bundle-CASCADE-REPORT.md` |
| 9/10 | Code #12 queue metabolism (`bb601c0`) | founder-DM queue verbs, phantom-write rail (observe), evidence floor, C7 bundle-linkage gate on SHIPPED, Monday menu; D-300/D-301 | `2026-09-09_fndr_cora-code-12-queue-metabolism-CASCADE-REPORT.md` |
| 9/19 | Code #13 honesty rail (`41cee38`) | third egress rail, nightly catch-up task (08:30), One-Cora RSVP/recap, `data/ladder-registry.yaml`, Drive allowlist-by-folder (D-303), repeat-signal escalation (D-302/D-310) | `2026-09-19_fndr_cora-code-13-honesty-rail-CASCADE-REPORT.md` |
| 9/19 | RIDER 1 identity (`907b2e2`) | cora@ system mailbox as knowledge-review intake, Asana identity resolver `CORA_ASANA_IDENTITY`, identity runbook sections; `f65b7fc` ruled roster rows on top | `2026-09-19_cora_CASCADE-REPORT-identity-least-privilege.md` |
| PUSHED | DR/VM step 1 (`claude/dr-manifest-vm-step1-2026-09-22`) | task-estate manifest from the LIVE registry (96 tasks) + drift line in the Monday digest; `deployment/DR-MANIFEST.md` + probe baseline; secrets-scan CI gate; `uv.lock` mcp closure; VM step-1 scoping packet + T0 card; this TOM | `2026-09-23_fndr_cora-dr-manifest-vm-step1-CASCADE-REPORT.md` |
| PUSHED | Code #14 bug bundle (`claude/code-14-bug-bundle`, on `afcca60`) | health-check `--dry-run` at every write site; founder-only start-anchored verb refusal; queue-status questions FORCE `cora_queue_status`; phantom rail = completion grammar + denial recall + an on-disk hit ledger; tool-bearing replies never cached (D-043 leak); cashflow `as_of`; card badge/id fixes; rail-2 legacy union wired; LEX RSVP include; sticky-ack window; run-marker grace; Meet join audit DARK; COPA no-record carve-out + audit breach-on-any-copy; uv `exclude-newer` pinned; needs ONE restart | `2026-09-23_fndr_cora-code-14-bug-bundle-CASCADE-REPORT.md` |

**Live state pointers (read these, never this file, for current numbers):** the bot's pid and
restarts = `logs/cora-instances.jsonl` (doctrine 5); the scheduled estate = `deployment/manifest/task-estate.md`
(the `## SCHEDULED TASKS` table further down this file is a 2026-06-03 hand list kept for history --
it is NOT the registry); rebuild = `deployment/DR-MANIFEST.md` + `deployment/bootstrap-new-machine.md`;
queue = `cora_code_queue` (MCP) / `code-session-backlog.md`; ladder = `data/ladder-registry.yaml`;
health = the 08:45 nightly check + the Monday `cora_health_report.py --slack` digest.

**Open across those bundles (owner Harrison unless noted):** rail-2 loosening ESCALATION (Code #13
slice 6, SHIP NO); `CORA_SENTINEL_ENFORCE` flip waits on the 9/25 clean-week read; register the
06:11 mailbox-intake task + the Asana identity flip/restart + day-14 PAT removal (RIDER 1); 17
mirror quarantines to decide; the interactive-logon posture of the whole estate (DR-MANIFEST D14).

## KEY IDS AND CONSTANTS

```
HJR Slack workspace
  Harrison (founder):       U02P3D6AT2C
  NOTE 2026-09-19 (Code #13 RIDER 1 S-C): the id above is UNDER VERIFICATION -- code uniformly uses U0B2RM2JYJ1 (tool_dispatch.py _FOUNDER_SLACK_ID l.1106 / HARRISON_SLACK_USER_ID default l.7040 / _HARRISON_SLACK_ID l.8205; user_access.py:39; review_lanes.py:63; send-trust.yaml:25) and the live queue ledger records Harrison's taps under U0B2RM2JYJ1. The staged read-only probe scripts/probe_slack_user_ids.py (users.info on BOTH ids) decides; strike the loser with a dated note. Do not edit the id until then.
  Alex Cordova (F3E ops):   U0B3VGWJTMJ
  #f3-athletes channel:     C0B6GT3117Y
  #f3e-sales channel:       (name: f3e-sales)

HubSpot (portal 246351746)
  F3E Retail pipeline:      2313722582
  UFL/OSN/BDM pipeline:     "default"  <- string literal, NOT numeric
  Deal entity property:     f3_entity  <- 95 uses; legacy "entity" field = 0 uses, IGNORE
  Matt Petrovich owner ID:  83346026   <- DEACTIVATED; valid for historical only, no new assignments
  Hannah Grant owner ID:    165179973  <- pending invite (as of 2026-06-04)
  NOTE: Workflows NOT available on Sales Hub Starter. Make.com is the sole deal automation layer.

Asana (workspace 682743441507584)
  CANONICAL team/project registry: _shared/playbooks/asana-architecture.md
  (the founder CLAUDE.md "Layer 3 -- Asana" points there; this block is a quick ref).
  Teams (15):
    HJRG:              1211723492575901   <- also home of [FNDR] + [HJRG] projects
    F3E:               1209079638382203
    F3 Community:      1209152923740479   <- [F3C]
    OSN:               1209426556623911
    UFL Team:          1209152923740455
    BDM:               1211265649994430
    HJRP:              1209152923740487
    HJRPROD:           1209152923740471
    Lexington Services: 1215480830642799  <- LEX parent team ([LEX] projects)
    LLC Team:          1209152915815732   <- [LEX-LLC]
    LLA Team:          1209152923740446   <- [LEX-LLA]
    LBHS Team:         1209152923740451   <- [LEX-LBHS]
    LTS Team:          1215480830642802   <- [LEX-LTS] (exists since ~2026-06)
    Harrison Private:  1209086911828214   <- exempt from governance
  NOTE (2026-07-27, Asana Standard v1): the 4 Lex sub-teams are LLC/LLA/LBHS/LTS.
  Older "no LTS team" notes were STALE -- LTS Team exists (1215480830642802).
  Key projects:
    [HJRG] Q1 Goals - HAT:            1212816399207681
    [F3E] Sales Pipeline — Tommy:     1214824237490027
    [F3E] Pure Launch:                1214878916621796
    [OSN] Inventory Reconciliation:   1214516618188085
    [OSN] MMH Priorities:             1212754436098216
    [LEX-LLC] DDD Contract Expansion: 1212752629798314
    [LEX-LLC] LBHS COPA Diligence:    1214873835768214
    [HJRP-RR] Launch:                 1215070431336670
    [HJRP-RR] Operations:             1215070431026838
    [POD] Episode Pipeline:           1214487014690541
    [POD] Guest Pipeline:             1214487014638100
  Catch-alls (Cora meeting-capture targets, one per operating entity;
  2026-07-27 Asana Standard v1 created/repointed UFL/HJRP/F3C/LEX-LTS):
    [F3E] Operations — General:       1215470928454227
    [OSN] Operations — General:       1215470834881074
    [HJRG] Operations — General:      1215470834914137   <- FNDR + HJRG share this
    [UFL] Operations — General:       1216928707369575
    [HJRP] Operations — General:      1216928758714643
    [F3C] Operations — General:       1216928758905250
    [POD] Operations — General:       1215470944131060
    [LEX-LLC] Operations — General:   1215470944114390   <- LEX GM-level shares this
    [LEX-LLA] Operations — General:   1215470928477598
    [LEX-LTS] Operations — General:   1216928755480116
    (BDM + LEX-LBHS intentionally excluded from meeting capture -- canon / D-052)
  Entity custom field: internal name = f3_entity (22 options; includes LEX-DDS, FF, HJR-PB, CHK, CHB)
  Total workspace users: 69
  Harrison open tasks: 100+ overdue (oldest Jan 2025). Hygiene nudge validated.
  Broken Asana rule: "Slack Feed - Task Completed" is paused workspace-wide (disconnected Slack integration). Not causing harm but should be cleaned up.

Make.com
  Slack connection:     ID 4792065, workspace hjr-global.slack.com, Harrison U0B2RM2JYJ1 ✓ ACTIVE BOT TOKEN
  OLD entries deleted:  ID 4791943 (f3-energy.slack.com -- DELETED) + ID 4791951 (superseded by 4792065)
  Shopify connection:   ID 4791971 (F3 Energy, OAuth, f3energy.com)
  HubSpot connection:   ID 4784191
  Asana connection:     ID 3829949
  Operations:           9,420 / 120,000 used (8%). Reset: June 24.
  Active HJR scenarios (9 core + 66 fighter trackers):
    4769263  [F3E] Apollo LinkedIn Spy — HubSpot Leads + Tommy DM
    4768887  [HJR] Asana Hygiene Nudges — Overdue Task Comments
    4768886  [HJR] Deal Task Sync — Proposal Stage to Asana
    4769075  [HJR] HubSpot Deal Stage Monitor
    4769089  Shopify DTC Daily Summary → #f3e-leadership
    4769088  Shopify Inventory Alerts → #f3e-leadership
    4769073  Slack Channel Health Monitor
    4769072  OSN Weekly Metrics → Matt DM
    4398938/43/17  F3E Instagram trackers (3 legacy scenarios)
  Fighter tracker scenarios (IDs 4770810-4770927 range):
    66 active (fighters 1-45, Tag + Post each)
    25 invalid -- Personal IG accounts, see TOM above for fighter list
    23 pending -- rate limit, create next session (fighters 46 Post + 47-57 Tag+Post)
    Templates: 4769310 (Tag), 4769305 (Post) -- both active, updated column mapping
    Sheet: "MMA Lab x F3 Fighters Tracker" | spreadsheetId 1tPpsdUrvXaYq7Cz77L5yYwEC6plptO_xcGY3JncPK28
  Data stores:          3 -- hjr_youtube_comment_ids (YouTube dedup) + HJR Deal Stage Tracker (86993, struct 281656) + F3 Fighter Roster (87002, struct 281678)
  NOTE: Make.com + Cora are the ONLY automation layers across the entire portfolio.

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

5. **Restart sequence** -- Stop-ScheduledTask -> CIM kill -> Start-Sleep 3 ->
   Start-ScheduledTask -> VERIFY exactly one instance. Stop alone does NOT kill
   python.exe. **KILL FILTER (corrected 2026-06-11): the service launches via the
   console-script wrapper, so live command lines contain `\Scripts\cora.exe` --
   NOT `cora.main`. A `*cora.main*` filter matches NOTHING and stacks a second
   instance (happened 6/10 23:26 + 6/11 00:37).** Canonical kill, from ELEVATED
   PS (service runs -RunLevel Highest; non-elevated sees no cmdline, kills nothing):
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='cora.exe'" |
     Where-Object { $_.CommandLine -like "*\Scripts\cora.exe*" -or $_.CommandLine -like "*cora.main*" } |
     ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
   ```
   After Start-ScheduledTask + sleep, verify single instance. **HEALTHY SHAPE
   CORRECTED 2026-08-19 (cq-0d163e5f9c22, verified live):** the service task action
   is `.venv\Scripts\python.exe -m cora.main` (`schtasks /query /tn
   cowork-cora-service /v`, 8/19) -- there is **NO `cora.exe` process at all**. One
   healthy instance = **2 python.exe** matching the filter (`.venv\Scripts\python.exe
   -m cora.main` redirector -> base `Python312\python.exe -m cora.main`). The kill
   filter still works (it matches on `cora.main`), but the old "1 cora.exe + 2
   python.exe" shape check counted processes the filter never matches, printed
   "0 + 0" and warned on EVERY restart -- loud enough to mask a real stacking. The
   2026-06-11/06-16 `cora.exe` observations are STALE, as is the older "uv.exe run
   cora" note. Count what the kill filter matches: 2 = `-m cora.main`, 3 = a
   console-script launch, both ONE instance; >3 = stacked. **Definitive check is
   `logs/cora-instances.jsonl`** -- one `start` row per real process (pid + ts), so
   a restart is proven by a NEW pid, never by a restart script's exit code. The log
   line alone cannot prove it: `TimedRotatingFileHandler` pins the live file to the
   process START date and moves each finished day to `cora-<startdate>.log.<thatday>`,
   so an instance's startup line is invisible to a `cora-*.log` glob after its first
   midnight (this is what made the 8/18 forensics conclude, wrongly, that four
   watchdog restarts had never restarted anything).
   **Service RunLevel is `Limited`, not Highest** (verified live 8/19) -- the
   watchdog task is the one at `Highest`. Elevation is still required for
   restart-cora.ps1 because Stop-Process against another session's tree needs it.

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

11. **KB-staleness hygiene + supersede-in-place (D-087)** -- Update canonical
    doctrine IN PLACE (edit the file; the nightly static sync's replace-on-conflict
    auto-cleans its chunks). Create-new only for dated captures/one-offs. Stamp a
    superseded standalone `.md` with `<!-- KB-STATUS: SUPERSEDED <date> by <ref> --
    <why> -->` (first line) so the monthly `cowork-cora-kb-hygiene` sweep
    archives+purges it: SMALL sweeps auto-apply LIVE (WAL DELETE, no VACUUM, no Cora
    stop), a LARGE sweep (>500 chunks or >100 files) ESCALATES to the Cora-stopped
    `deployment\run-kb-hygiene-apply.ps1`. `--proactive` detectors are PROPOSE-ONLY.
    Append-only logs (`decisions.md`) are EXEMPT -- never archive them. Move/purge
    core is shared in `src/cora/kb_archive.py`; the sweep reuses it.

12. **Autonomous knowledge writes are reversible, not ungated (D-087, D-011 relaxed)**
    -- `CORA_AUTOWRITE_LIVE` (default OFF = every item still DMs Harrison) lets
    graduated-trust Tier-0/1 knowledge auto-write. This is NOT the retired WS17-C
    silent auto-approve (D-060): every auto-write is AUDITED (`logs/cora-autowrite-audit.jsonl`)
    and one-tap REVERTIBLE in the weekly `cowork-cora-autowrite-digest`, and Tier-2
    (money/contracts/legal/equity/comp/PHI/LEX/cross-entity/conflicts-with-canon)
    stays Harrison-gated by the classifier + an independent fail-closed
    is_high_stakes belt. Ship a "flip a classifier live" change default-OFF behind
    an operator flag with the digest AS the validation; keep the flip in the
    CONSUMER, not the pure shadow module.

---

## SCHEDULED TASKS (full registry as of 2026-06-03)

| Task name | Schedule | Script |
|---|---|---|
| cowork-cora-service | AtLogon + RestartOnFailure | cora.main (bot process) |
| cowork-cora-channel-sweep | Daily 01:30 AZ | run_channel_sweep.py |
| cowork-cora-knowledge-review | Mon-Fri 07:00 AZ | run_knowledge_review.py |
| cowork-cora-daily-briefing | Daily (see PS1) | run_daily_briefing.py |
| cowork-cora-backup | Daily 04:30 AZ | backup_logs.py |
| cowork-cora-influencer-scan | 7 AM + 7 PM daily | run_influencer_scan.py |
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
| D-025 | Credit requires @f3energy media tag -- hashtag-only posts do not qualify |
| D-026 | QBO is primary financial source for all accounting questions (P&L, balance sheet, AR/AP, transactions). GSheets = supplemental only (weekly cash flow forecast + filed close packs). |
| D-027 | Clover retired permanently from OSN stack (2026-06-05). OSN uses QBO as sole financial source. Do NOT rebuild Clover integration. |
| D-028 | Per-tool timeout tiers (6-tier as of 2026-07-03, audit W3-07): 8s fast (local DB) / 12s normal (single API) / 15s default (finance+QBO) / 20s heavy (uploads/DM/drafts) / 25s heaviest (image/deck/meeting/composite) + per-tool overrides (cora_person_dossier=60s). SOURCE OF TRUTH = `_TOOL_TIMEOUTS` in tool_dispatch.py; unlisted default = 15s. |
| D-029 | Cora = intelligence + conversation. Make.com = mechanical automation. Rule-based, threshold-based, or straight data-push tasks belong in Make.com. Cora only where natural language or context is needed. |
| D-030 | Meeting Action Capture watermark must track transcript IDs, not just timestamps. Timestamp-only watermarks fail when meeting_ts equals the watermark value. ID-set dedup is required. Watermark format: `{"last_processed_ts": N, "processed_ids": [...]}`. |
