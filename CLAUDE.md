# Cora — Code Session Context

This file is the authoritative startup read for every Code session.
Read this first, then check `decisions.md` for the full decision log.
TOM entries are newest-first. Do not edit past TOM entries.

---

## TOP OF MIND (TOM)

### [BUG FIX] Closed-task nudge guard -- Hannah's daily nudges on a year-closed task -- 2026-06-11 (SHIPPED, D-045)

Hannah (#info-for-cora 6/11): daily nudges on "Jimmy Bar - Potential Activation", completed
2025-06-03. **Offender = Make.com scenario 4768887**, NOT a Cora-side job: its Make filter had
the completed check in a separate OR group -- `(overdue 14d+) OR (incomplete)` -- so completed
overdue tasks passed; daily cadence, no throttle/ledger, posted as Harrison (Hannah hit as
follower). Three-layer fix:

1. **Make 4768887 fixed in place**: single AND group + cadence daily -> WEEKLY (next exec
   6/18) per the 1 comment/task/7d doctrine.
2. **Cora chokepoint**: `nudge_ledger.closed_task_guard()` -- fire-time live completion
   re-check via new `asana_client.get_task_completion`; completed -> skip + ledger row
   `reason="already_closed"` (PERMANENT when completed_at >48h old); wired into
   run_asana_hygiene_nudges before every post; skip rows carry last_nudged_at so the weekly
   sweep's lockout inherits them for free. Fetch errors fail open.
3. **Weekly hygiene-asana SKILL.md patched** (OneDrive): permanent already_closed rows never
   re-commented + live completion re-check before any comment.

**WARNING -- OWNERSHIP DRIFT FOR HARRISON**: the scheduled task "Cora - Asana Hygiene Nudges"
is ENABLED and firing daily (6:30am, ledger rows 6/08-6/11) DESPITE the 6/05 memory saying it
was disabled in favor of the Make scenario -- both have been nudging. Pick one owner: the
Cora job is the smarter one (all users, ledger, KB-signal check, throttles); the Make
scenario covers only Harrison's tasks with none of that. Recommend deactivating Make 4768887.
Hannah replied-to in #info-for-cora. Full record: D-045.

### [LEX + KB] DDD manuals + EVV docs LIVE via recurring dump-folder sync -- 2026-06-11 (SHIPPED, D-046; closes Asana 1215643646634974)

Shaun's live-in caregiver EVV question now answerable: **83 files / 3,978 chunks** ingested
(was 20 files one-shot). The 6/04 "DDD Policies" SHORTCUT (never indexed) held the DDD
Complete Provider (410 chunks) / Operations (531) / Medical (1,087) / Behavior Supports /
Eligibility manuals + 57 EVV docs incl. EVV_Live-InCaregiverFAQ.pdf. Old script's 80-page
PDF cap had truncated even the one manual it did ingest.

- **`scripts/run_lex_dump_folder_sync.py`** (replaces ingest_dump_folder.py): recursive +
  shortcut-following, watermark-incremental, full PDF parse, >60MB skip-with-note. Task
  **"Cora - LEX Dump Folder Sync"** daily 4:45am AZ REGISTERED (non-elevated OK).
- **Tagging**: DDD Policies tree -> GM-level (sub_entity NULL + `metadata.lex_gm_level=True`
  -- new store Step 0 opt-out so auto-detection can't scatter manual chunks); dump root
  stays LEX-LLC; client-record-looking filenames forced LEX-LLC even in the tree. Legacy
  one-shot `{fid}:chunkN` rows cleaned (0 remain).
- **PHI guard**: per-chunk is_phi_risk audit logged + stored in metadata.phi_risk_chunks
  (policy manuals trip program keywords by construction -- see D-046 posture).
- **Gmail related check PASSED**: Lex mailboxes enabled in monitored-email-accounts.yaml;
  KB already holds `gmail | LEX | "EVV Paper Timesheet Attestation"` (mentions DDD-2101A).
- **RESOLVED same evening (D-046a, Harrison directive)**: DDD tree re-tagged GM-level ->
  **LEX-LLC**. Sync script now tags everything in the dump folder LEX-LLC (policy-tree
  detection kept as metadata provenance only; client-record filename rule removed as dead
  code); **2,840 existing chunks re-tagged in place** (SQL on knowledge_chunks, no
  re-embedding); smoke test re-run in #llc-leadership PASSED + manuals-indexed note relayed
  there for Shaun's team. Manuals visible in #llc-* AND GM #lex-* channels; not in
  LTS/LBHS/LLA (accepted). The `lex_gm_level` store opt-out stays as a generic mechanism,
  unused by this script.

### [ORG SYNTHESIS] Phase 2 deliverable 2: briefing rework -- role-briefing-config.yaml RETIRED -- 2026-06-11 (SHIPPED; DIGEST REVIEW PENDING)

Full suite **3,875 passed / 41 skipped** (+11). Ship:
`deployment\ship-briefing-rework-2026-06-11.ps1` (optional `-Restart` from elevated PS).
Full entry: decisions.md 2026-06-11 (D-044 item 5 executed).

1. **`run_daily_briefing.py` now reads `data/maps/org-roles.yaml`** (org_roles.py, 60s TTL).
   `role-briefing-config.yaml` is DELETED -- the locked D-044 item 5 consolidation point. A
   retirement regression test fails the suite if the file or a source reference reappears.
   The registry IS the roster: externals (Jason) + registry-only (Tessa) excluded from
   delivery; unknown/unmapped users skipped fail-closed by construction.
2. **Content mirrors the plate composite, REUSING the tool_dispatch section builders** (no
   fork): role + lanes, entity-scoped tasks capped 10, today/tomorrow calendar, pipeline for
   owners (LEX never), stalled decisions Harrison-only, + the 25h recent-activity KB scan.
   Old `extra_data` fetchers (hubspot_f3e/hubspot_all/financial/deal_aging) retired with the
   config; no financial figures by plate doctrine (Cash Flow Pulse covers cash).
3. **ROLLOUT (Harrison-locked, refined same day -> `ed6c212`): review-driven per-user
   enablement.** Default mode sends Harrison ONE DM PER USER ("WOULD-BE BRIEFING -- name");
   his reactions are FUNCTIONAL: each run reads reactions on outstanding review messages
   (Harrison's only, D-011 pattern) -- :+1: enables that user's real delivery from that run
   on, :-1: drops them from review. State: `data/state/briefing-delivery.json`. The
   registered task (7:30am AZ weekdays, action unchanged) starts in pure review mode until
   the first thumbs-up. `--send-users` = force-deliver all (rarely needed);
   `--digest-only` = force review for everyone; `--dry-run`, `--user <filter>`.
4. **Shared-builder fix:** `_plate_asana_section` canonicalizes sub-entity -> parent before
   the task filter (LEX-LLC/OSNGW fell through ENTITY_PROJECT_PREFIXES UNFILTERED -- matters
   since the 6/11 registry move of Shaun/Jen/Jeff/Aaron to LEX-LLC). Plate-tool side needs
   the next restart; briefing side live at next task fire.
5. **d1 exit-gate nits FIXED (follow-up commit, folded into d2 per the spec tracker):**
   (a) `format_tasks_for_llm` no longer tells non-founders to "ask in a #fndr-* channel"
   (empty-case + scope footer now just state the out-of-scope count); (b) tasks render
   due-date-first (stable sort, `sort_tasks_due_first`) and the plate's 10-item cap selects
   due-dated work first -- Shaun's long no-due-date list had crowded out dated tasks and cut
   mid-link; long lists (>10) also carry a terse-render instruction. NOTE: asana_client.py
   was staged in ISOLATION (hash-object/update-index) because a concurrent session holds
   uncommitted nudge-guard WIP in the same file -- their working-tree changes were never
   committed here. +7 tests.

**⏰ Open:** (1) Harrison reviews the per-user briefing DMs at the next 7:30am fire and
:+1:'s the ones to enable -- enablement is automatic at the following run, no other action
needed; (2) next clean restart activates the sub-entity plate fix + the pending LLC posting
targets + five-custodian prompt language (558e768/c7bce7a); (3) **Aaron Ferrucci's Asana
account is NOT yet visible via the API** (no workspace-user or Lexington-team entry --
invite likely pending his acceptance); add his slack-to-asana row the moment his GID
appears (live-reload, no restart).

### [ORG SYNTHESIS] Plate round 3: role-header determinism + section caps + reply-formatter shells -- 2026-06-11 (RESTART PENDING)

Exit-gate round-2 findings (Cowork): Harrison PASS, Tommy PASS incl. negative test, Shaun
PARTIAL, Matt not yet run. Fixes (full suite **3,858 passed / 41 skipped**, +31): **restart via
`deployment\ship-plate-round3-2026-06-11.ps1 -Restart` (elevated PS), then finish the smoke.**

1. **Role header dropped for non-Harrison askers (2/2 live):** the tool DID emit it, but as an
   unlabeled preamble the model treated it as metadata and skipped it in narration. Now a labeled
   `YOUR ROLE` section + an explicit REPLY FORMAT instruction ("START your reply... EVERY asker
   gets their role line") in the tool output AND all 17 entity prompts. Tests pin the labeled
   section first for a non-Harrison asker.
2. **Long-plate truncation (25 tasks / 23 deals -> reply ended in a malformed half-link, i.e.
   max-token cutoff):** plate sections now cap at `_PLATE_MAX_ITEMS=10` with a "first 10 of N --
   say 'show me my tasks/deals' for the full list" note. Standalone tools remain the full view.
3. **reply_formatter "()" shells:** the bare-URL redaction left "( )" / "[label]()" artifacts
   when a redacted URL sat inside parens or a markdown link. URL regex now excludes ')' + new 8b
   cleanup pass (empty md-links keep their label; empty paren/bracket pairs dropped). 5 tests.

### [ORG SYNTHESIS + HOTFIX] Plate-tool live-crash fixes: truncated asana_client restored + plate hardening + calendar scope + router + kill filter -- 2026-06-11 (SHIPPED + LIVE, commit a5f4d4f)

Fixes for the 00:51 AZ live smoke crash (Cowork bug report). Full suite **3,827 passed / 41
skipped** (+14). **Restarted 2026-06-11 01:17 AZ via `ship-plate-fixes-2026-06-11.ps1 -Restart`
(corrected kill filter) -- old instance killed cleanly (its heartbeat counter died at 2341s),
single new instance confirmed** (one "Cora starting up", one monotonic heartbeat sequence; the
script's "2 instances" warning was a FALSE POSITIVE from a wrong threshold -- one healthy bot is
a 3-process chain, see doctrine #5; verification corrected in both ship PS1s).

1. **ROOT CAUSE -- `asana_client.py` was TRUNCATED ON MAIN since `d5f2e6f` (2026-06-03):** the
   Feature-14 commit cut the file mid-loop inside `format_tasks_for_llm`; the function fell off
   the end and returned **None for every NON-EMPTY task list**. `asana_get_my_tasks` has been
   silently returning None for a week (models narrated "no tasks"); the plate tool surfaced it
   as a TypeError. Tail restored byte-identical from `f0e5de3` (last complete blob); regression
   tests added (no test had ever covered the non-empty formatting path).
2. **Plate composition hardened:** `_safe_plate_section` wrapper -- any section that raises or
   returns None degrades to a stub line; helpers also coerce formatter regressions. The
   "every section fail-soft" promise now holds at BOTH layers.
3. **Calendar reads fixed (no admin action needed):** probed the live DWD grant -- `events.list`
   works ONLY under `calendar.events` (already granted); `calendar.freebusy` 403s it and
   `calendar.readonly` is NOT actually granted (contradicts the 6/06 audit note). freebusy.query
   conversely works ONLY under `calendar.freebusy`. `get_user_events` now builds with the events
   scope; freebusy path unchanged. Verified live: harrison@ returned 7 events. This also
   un-breaks the standalone `calendar_get_my_events` tool (silently 403ing since the 6/03 scope
   change).
4. **Model router:** plate queries now FORCE SONNET (multi-source composite; Haiku misnarrated a
   degraded result as "no open tasks"). "what's on my plate" had literally been a Haiku hint.
5. **RESTART KILL FILTER CORRECTED (doctrine #5 rewritten):** live service command lines contain
   `\Scripts\cora.exe`, NOT `cora.main` -- the old filter matched NOTHING, which is why restarts
   stacked instances (6/10 23:26 + 6/11 00:37). Both ship PS1s now kill on either pattern +
   verify exactly one instance after start.
6. Semantic-cache flush: checked -- tonight's bad replies were never cached (tool-bearing replies
   skip the cache store); `scripts/flush_plate_cache.py` kept as a targeted-flush utility.

**After restart, re-run the deliverable-1 exit gate** (Harrison / Matt / Tommy / LEX user smoke;
Cowork re-fires the Harrison leg on signal).

### [ORG SYNTHESIS] Phase 2 deliverable 1: whats_on_my_plate tool -- 2026-06-11 (SHIPPED + LIVE, commit f9cf11b)

Repo HEAD: `ab8db8b` on `origin/main` | full suite **3,813 passed / 41 skipped** (+47) |
**Cora restarted 2026-06-11 07:37 UTC (00:37 AZ)** via the ship PS1 `-Restart` from elevated
PS -- clean startup (Socket Mode attempt #1, prewarm 14/14, KB warmed 7.4s), heartbeat
advancing, tool exposure verified in loaded code (all entities incl. sub-entities, 25s tier).

First Phase 2 deliverable per the org-synthesis spec (decided with Harrison 2026-06-10:
pull-not-push ships before the briefing rework). On-demand composite plate view, all in
`tool_dispatch.py`:
- **`whats_on_my_plate`** -- any teammate asks "what's on my plate" in any channel/DM and
  gets THEIR role-scoped picture: role + lanes from org-roles.yaml (D-044), open Asana
  tasks (channel entity-scope reused from asana_get_my_tasks), today + tomorrow calendar,
  HubSpot deals when they own a pipeline (data-driven: presence in slack-to-hubspot.yaml),
  and stalled P0/P1 decisions for Harrison only. Each section fail-soft.
- **Scope rules:** own plate ONLY -- optional `person` param is Harrison-only (everyone
  else politely refused; asana_get_user_tasks stays the peer-visible path). Unknown user
  (no org-roles entry) = graceful fail-closed no-data response. External consultants
  (Jason Dorfman) = role scope only, zero internal task/CRM/calendar pulls. LEX scope
  (incl. sub-entities) never gets a HubSpot section (Tier-1 doctrine). NO financial
  figures pulled anywhere (channel-tier guardrail respected by construction).
- **ADVISORY data only** -- org_roles never expands access (D-044); user_access / sibling /
  cross_entity / phi / historical_access guards all run unchanged, pre-LLM.
- **Wiring:** TOOL_DEFINITIONS + _TOOL_FUNCTIONS + _GLOBAL_CORE_TOOLS (exposed to every
  entity channel + DMs) + heavy 25s timeout tier. All 17 entity prompts gained a mandatory
  "## What's on my plate" tool-call section. `asana_get_my_tasks` description re-pointed
  (it previously claimed the literal phrase "what's on my plate" as its trigger).
- 47 tests `tests/test_whats_on_my_plate.py` (wiring/exposure, registry scoping,
  unknown-user refusal, Harrison override, external limits, LEX HubSpot exclusion,
  fail-soft sections, prompt coverage).

**Exit gate (Phase 2 deliverable 1) -- the one open item:** live smoke for 3-4 roles --
Harrison (FNDR/DM, expect STALLED DECISIONS section), Matt (#osn-leadership), Tommy
(#f3e-sales, expect DEAL PIPELINE), one LEX user in #llc/#lex-leadership (expect NO
pipeline section). **Next Phase 2 deliverable:** briefing rework -- `run_daily_briefing.py`
reads org-roles.yaml; that is the consolidation point where role-briefing-config.yaml
retires (do NOT touch it before then).

### [ORG SYNTHESIS] Phase 1: org role registry + role-aware context -- 2026-06-10 (SHIPPED + LIVE, commit 8d153b6)

Repo HEAD: `8d153b6` on `origin/main` | full suite **3,762 passed / 41 skipped** | Cora restarted
2026-06-10 ~23:27 AZ, heartbeat confirmed advancing | role injection LIVE.

New program (Harrison-directed 2026-06-10): full organizational synthesis -- Cora as the
role-scoped individual resource for EVERY user (as she is for Harrison), plus a founder-level
strategy/oversight layer for Harrison. Spec of record:
`_shared/projects/cora/design/2026-06-10_fndr_org-synthesis-spec.md` (4 phases + tracker).

**Phase 1 BUILT this session (Cowork sandbox; commit via
`deployment\ship-org-roles-2026-06-10.ps1` from elevated PS):**
- `data/maps/org-roles.yaml` -- canonical org role registry, 19 people: role, primary entity,
  additional entities, lanes, manager, routing notes, external flag. Consolidated from founder
  CLAUDE.md delegates + role-briefing-config + slack-to-asana + monitored-email-accounts.
  `role-briefing-config.yaml` deliberately untouched (drives the live Tier-1 briefing task;
  consolidation point is Phase 2).
- `src/cora/org_roles.py` -- loader, 60s TTL live-reload (edit YAML, no restart -- the
  lex-phi-custodians pattern). FAIL-CLOSED: unknown asker = no role block = exact pre-Phase-1
  behavior. Parse errors keep the last good registry.
- `app.py` -- `_dispatch_qa` runtime context now carries a terse role block for every known
  asker (role, lanes, cautions: Daniel executor-removed, Jason external-consultant, Matt
  disambiguation, etc.). Covers mentions / thread follow-ups / /cora-ask / DMs.
- **ADVISORY-ONLY invariant (load-bearing):** the registry never grants access; every injected
  block carries an explicit "does NOT expand entity access" rule; user_access / sibling /
  cross_entity / phi / historical_access (D-043) all run unchanged.
- 28 tests `tests/test_org_roles.py` (28/28 in sandbox; full host suite runs in the ship PS1),
  incl. roster-drift guards: every slack-to-asana user + PHI custodian + finance-allowlisted
  user MUST have a registry entry.
- **D-044 LOCKED** (Harrison-approved 2026-06-10 after full roster review; full entry in
  decisions.md): org-roles.yaml is the canonical role registry; advisory-only; fail-closed;
  roster changes through Harrison; new per-user features read it instead of growing new
  per-user maps. Roster review shipped as `721970e` (Jerry = Staff Accountant under Justin;
  Tessa added as first registry-only entry; suite 3,766 passed / 41 skipped).

**Ship steps:** run the PS1 (import smoke -> full pytest -> commit/push -> optional restart).
**Restart REQUIRED** for the injection to go live (app.py is in the bot process). The PS1's
kill filter matches `cora.main` ONLY, so a restart will NOT touch the 18mo gmail backfill if
it is still running. Smoke test: a non-Harrison teammate asks @Cora anything -- reply should
reflect their role; guest/unknown users unchanged.
**Open for Harrison:** confirm Jerry Reick's title; Tessa registry entry y/n; Phase 2 first
deliverable (briefing rework vs on-demand "what's on my plate" tool).

### [SECURITY + FINANCE] Per-user email/Drive access control + finance receipt tier SHIPPED -- 2026-06-10 (D-043)

Built ahead of the 18-month gmail backfill (Harrison re-decided the gate order 2026-06-10:
enforcement ships FIRST so the historical mass lands into a guarded, tag-at-ingest pipeline).
Spec of record: `_shared/projects/cora/design/2026-06-09_fndr_per-user-email-drive-access-spec.md`.
Supersedes the [NEXT UP] entry below. Full doctrine: decisions.md D-043.

**Shipped (all deterministic, pre-LLM, D-034 pattern):**
- **Tier 1** -- `historical_access.apply_tier1` wired in `context_loader._try_kb_retrieve`:
  gmail/drive_sweep chunks owned by someone other than the asker are header-stripped
  (From/To/Subject/Date/author/message_id/deep_link/thread_id) before entering LLM context; the
  factual body survives as institutional knowledge. `founders_os@hjrglobal.com` chunks =
  org-shared, exempt. Unknown owner or unknown asker = stripped (fail-closed). Synthesis rule
  injected into every runtime context.
- **Tier 2** -- DM-only explicit retrieval ("pull up / show me / find the email"), own-mailbox-only
  (aliases incl.), Harrison override via `data/maps/historical-access-allowlist.yaml`,
  teammate-mailbox requests refused without existence leak, unmapped identity fail-closed. New
  `store.search_owned` (exact owner-scoped scan, no entity/recency filter) + a plain-DM branch in
  `app.handle_message_event` + the gate at the TOP of `_dispatch_qa` covering all 4 entry points
  (mention / thread follow-up / /cora-ask / DM).
- **Tier 2-Finance** -- Justin `U0B3AEJCYGP` / Eric `U0B3PRZMBCN` / Jerry `U0B4L7886PJ`
  (`data/maps/finance-receipt-allowlist.yaml`) may pull financial_document-tagged chunks from ANY
  mailbox, ONLY in #hjr-finance `C0BAK65N4TA`; non-financial retrieval refused; every pull audited
  to `logs/finance-access-audit.jsonl`. Ingest tagging at `store.upsert_documents` Step 0b
  (`finance_doc_classifier`, precision-biased, >=2 independent signals). Auto-file to the
  "Receipts & Invoices Inbox" Drive folder `1I7zWcCIAOx7zdzIXcxx6WTLk1K40eizj` (SA write verified
  live; Cora bot + all 3 allowlisted users confirmed in the channel). Weekly digest
  `scripts/run_finance_receipt_digest.py`, task `cowork-cora-finance-receipt-digest` (Mon 10:30
  AZ; register via `deployment\setup-finance-receipt-digest-task.ps1` from elevated PS).
- **Cache-leak closure:** responses built on UNSTRIPPED personal chunks (grants, owner's own mail,
  unrestricted asker) never enter the shared semantic cache; the grant path also skips cache
  lookup and withholds the static portfolio context (`static_text=""` -- a DM asker may not be
  entity-authorized for the founder brief).

**Sequencing (plan of record):**
1. Build + tests + restart -- THIS session.
2. **Tonight: relaunch the 18-month gmail backfill** from elevated PS (manual -- the scheduled
   task's 3h limit would kill it): `.venv\Scripts\python.exe scripts\gmail_threaded_sweep.py
   --force-since-days 550`. The 6/10 00:14 attempt died at 03:29 in the machine outage mid-FIRST
   account; ~60-65 percent of the work remains (harrison@hjrglobal Dec'24-Jun'25 gap + all 28
   other accounts). Expect ~8-12h, resumable, lands pre-tagged + pre-guarded.
3. **After the backfill completes:** run `scripts\backfill_financial_document_tags.py` (idempotent
   catch-up tagger; safe to also run once before), then the 5 live smoke tests (build prompt /
   D-043).

### [SUPERSEDED 2026-06-10 -- see entry above] Per-user email/Drive access control -- spec ready, build after the 18mo gmail backfill
Spec: `G:\My Drive\HJR-Founder-OS\_shared\projects\cora\design\2026-06-09_fndr_per-user-email-drive-access-spec.md` (also KB-ingested via static_md). Two-tier rule: (1) institutional knowledge stays usable as context for everyone via header-stripped chunks; (2) specific email/Drive retrieval is DM-only + owner's-own-mailbox-only, Harrison-override (allowlist), fail-closed. Code-level guard + new DM retrieval path + tests (D-034 pattern). Gate: build after `--force-since-days 550` gmail backfill lands + Drive owner-tagging confirmed.

### [GMAIL/KB + BACKUP/DR] Gmail sweep coverage fix + DR backup hardening -- 2026-06-09 (commits 02813dd, d2ac2a7, ec5af47, 5b3967f)

Repo HEAD: `5b3967f` on `origin/main` | full suite **3,628 passed / 41 skipped** | Cora restarted, heartbeat fresh | gmail catch-up CONFIRMED working in prod.

**Problem found (audit):** the nightly Gmail KB sweep (`scripts/gmail_threaded_sweep.py`,
task `cowork-cora-kb-sync-gmail`) had silently stalled since **2026-05-28**. Read-vs-unread
was never the issue (`after:{ts}` covers both). Root causes: (1) the task's 1h
ExecutionTimeLimit killed the run ~14/28 accounts in every night (always mid jason@f3energy);
(2) the watermark was flushed only once at end-of-run, so since the run never finished it
NEVER persisted -> early accounts re-scanned a growing backlog nightly and the entire
Lexington + UFL mailbox sets were never reached; (3) `uv run` in the task (D-005 violation);
(4) 5-6 DWD-eligible Slack users had no mailbox entry at all.

**Shipped (02813dd):** per-account ATOMIC watermark (resumable across kills), STALE-FIRST
ordering (oldest/never-swept mailboxes first), CAP-AWARE watermark (`_next_watermark` -- on
cap-hit advance only to newest-processed, never silently skip backlog), `_upsert_with_retry`
(backs off on transient KB locks), `--max-threads`/`--accounts` CLI; +6 DWD mailboxes (Eric,
Daniel, Jake, Micah, Elena, tommy@hjrglobal); `setup-kb-sync-tasks.ps1` -> .venv python + gmail
3h limit. **VERIFIED working:** `gmail-thread-watermarks.json` went 12 stuck@5-28 -> 27 accounts
@ 6/08-6/09, incl. the previously-dark Lexington inboxes. **Demi Bagby personal mail deliberately
EXCLUDED** (Harrison). `busy_timeout=30000` added to `schema.connect` (was the DB-lock-crash fix;
landed via a concurrent session's commit).

**DR backup hardened (ec5af47 + 5b3967f):** `backup_logs.py` now also bundles `.env` + the SA
JSON into ONE Fernet-encrypted blob (`secrets-YYYY-MM-DD.enc`, key from CORA_BACKUP_PASSPHRASE
via PBKDF2; SKIPS rather than ever writing plaintext if unset), online-backs-up the small
feature DBs, and VERIFIES the KB landed offsite (exit non-zero if not). `restore_secrets.py`
is the decrypt companion. `setup-backup-task.ps1` -> .venv python + 60m limit (10m was killing
the multi-GB KB online backup), 1:00pm trigger preserved.

**Drive-recall fixes (d2ac2a7):** oversized-sheet Sheets-API fallback, 250k-token embed
batching, deterministic entity override (another session's work; committed here when its ship
hit the lock).

**OPEN (Harrison action -- DR not yet ACTIVE):** (1) set `CORA_BACKUP_PASSPHRASE` (password
manager + persistent User env var); (2) re-run `deployment\setup-backup-task.ps1`; (3) one
`backup_logs.py` run showing `Offsite verify: PASS`. Until then the encrypted-secrets backup is
built+tested but dormant -- secrets remain the one thing a machine loss would cost.

**Doctrines locked this session: D-038 (gmail sweep resumability), D-039 (KB busy_timeout),
D-040 (DR backup completeness), D-041 (shared-tree git ops + virtiofs/sandbox reliability).**

### [HYGIENE] Monthly log + ledger compaction job -- section 10.5 -- 2026-06-09 (commit 3fe3a38)

Repo HEAD: `3fe3a38` on `origin/main` | task `Cora - Log Compaction` registered (monthly, day 1 @ 14:00 AZ, Ready, Next Run 7/1) | no Cora restart needed (standalone task).

Dated log files are the unbounded grower (~23 task families x 1 file/day forever; 45MB / 23 days).
`scripts/compact_logs.py` gzips dated logs older than 30d into `logs/archive/` + deletes originals,
purges archives >365d. Ledger trim is **size-gated** (only touches a `*.jsonl` once it exceeds 5MB;
keeps last 90d by `ts` + any undatable line -- fail-safe, never breaks a ~14d throttle lookback;
no-op today since all ledgers are <1MB). **No SQLite VACUUM** in the routine job (bot holds state
DBs open; cora_kb.db too heavy -- big reclaims are manual via `reclaim_kb_space.py`).

Registered non-elevated (user-level file hygiene needs no admin; uses `schtasks /Create /SC MONTHLY`
since `New-ScheduledTaskTrigger` has no monthly trigger). Validated: dry-run clean at 30d; a real
21d run archived 6 old logs to valid `.gz`. The weekly health metric (10.4) already alarms if
logs+ledgers exceed 300MB, so this job is the actuator behind that alarm.

---

### [INFRA/PERF] Slimmed the wholesale founder CLAUDE.md inject -- section 10.3 -- 2026-06-09 (commit b2254fe)

Repo HEAD: `b2254fe` on `origin/main` | full suite **3,580 passed / 41 skipped** | Cora restarted 2026-06-09 11:45 AZ, healthy (heartbeat fresh, KB warmed 4.1s). **LIVE.**

The founder CLAUDE.md is ~32.5K tok but ~93% (~30.3K) is the dynamic "Current State of the World"
section (TOM, workstreams, recent decisions) that changes daily AND is already chunked into the KB
(source=static_md) + co-scanned on every non-LEX query (include_fndr=True). Inlining it wholesale
into every entity's context was pure redundancy.

**Now (`context_loader._load_static_context` + `_slim_founder`):** aggregators **FNDR/HJRG** (which
ask portfolio-wide questions) keep the FULL founder brief inlined; **every other entity** gets only
the ~2.2K static head (everything before `# Current State of the World`) + a note that current-state
is retrieval-served. LEX sub-entities still get no founder context (firewall unchanged). **Marker
absent -> full inject** (a founder-doc restructure can never silently drop context -- the split keys
on the literal heading `# Current State of the World`, so keep that heading in the founder doc).

**Measured static-context drop (the win):** F3E 41.5K->11.2K, OSN 39.6K->9.3K, LEX 39.1K->8.9K,
BDM 30K->3.4K tok (~73% off the high-traffic entity channels); FNDR/HJRG unchanged at ~33K.
**Synergy with the caching split (10.1):** the volatile TOM no longer rides in those entities'
cached system block, so Harrison's daily TOM edits stop invalidating their cache -- it now stays
warm across days, not just the 5-min window. +8 tests.

**Quality note / what to watch:** non-FNDR channels now rely on KB retrieval for portfolio
current-state. The FNDR co-scan already surfaced those chunks, so this should be transparent, but if
an entity-channel answer ever misses cross-portfolio context it used to have, that's the signal --
the fix is to ensure the fact is in the entity's `known-answers/{entity}.md` (always-injected) or to
add HJRG/FNDR to `_FOUNDER_FULL_ENTITIES`.

---

### [INFRA] Dropped legacy float vec0 table (knowledge_vec) -- section 10.6 -- 2026-06-08 (commits fccf028, 40a0403, 65c38a1)

Repo HEAD: `65c38a1` on `origin/main` | full suite **3,574 passed / 41 skipped** | Cora restarted, healthy.

**DONE (the goal): `knowledge_vec` is dropped.** The store no longer reads/writes it (commit
`fccf028`): `upsert_documents` writes only `knowledge_vec_bin` + `knowledge_vec_f32`;
`_search_float` repointed from the knowledge_vec vec0 KNN to an exact brute-force
`vec_distance_l2` scan over `knowledge_vec_f32` (the fallback survives the drop -- f32 holds the
same float vectors, no rebuild path needed); schema stops creating knowledge_vec on fresh DBs.
The DROP ran via `scripts/drop_legacy_float_vec.py` (`40a0403`); verified gone, no orphan float
shadow tables, Cora healthy on the new code (binary fast path 100% recall unchanged).

**✅ DISK RECLAIM DONE 2026-06-08: 2.91 GB freed (6.11 -> 2.98 GB).** Harrison ran
`scripts\reclaim_kb_space.py` from elevated PS after killing the stuck procs; Cora restarted clean
(stable heartbeats). The stuck 02:51 AM elevated pair is gone -- watch for it reappearing (it points
to a recurring early-morning KB-sync/gmail-sweep hang worth fixing separately). Rollback backup
`data\cora_kb.db.bak-2026-06-08` (pre-drop, 5.69GB) can be deleted after a few stable days.

_How it played out (historical):_
- The in-place DROP+VACUUM did **not** shrink the file: a WAL-mode VACUUM writes compacted pages
  to the WAL, not the main file. `cora_kb.db` is still 5.69GB (+ a large WAL). `VACUUM INTO`
  proved the true compacted size is **3.20GB (reclaims 2.9GB)** and round-trips the vec0 binary
  search cleanly (VERIFY_OK).
- **Blocker: a stuck 02:51 AM elevated python pair (PIDs 21040 parent / 1776 child) has held
  cora_kb.db all day** -- 14h+, not serving (it is NOT the live Cora; the live Cora is a separate
  healthy pair). A non-elevated session CANNOT kill them ("Access is denied") -- they run elevated.
  This is why every in-place truncating VACUUM hit "database is locked". **Worth investigating what
  that stuck process is** (likely a hung kb-sync / gmail-sweep from the 02:30-03:00 window).
- **To finish the reclaim (elevated PS, when convenient -- 293GB free, zero disk pressure):**
  ```
  cd C:\Users\Harri\code\cora
  schtasks /End /TN cowork-cora-service
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.ExecutablePath -like '*cora*' } | Stop-Process -Force
  .venv\Scripts\python.exe scripts\reclaim_kb_space.py        # truncating VACUUM (commit 65c38a1)
  schtasks /Run /TN cowork-cora-service
  ```
  This also clears the stuck procs. Rollback if ever needed: `data\cora_kb.db.bak-2026-06-08`
  (pre-drop, 5.69GB) -- safe to delete after a few days of stable running.

**DOCTRINE LOCKED:** (1) in-place VACUUM in WAL mode does NOT truncate the main db file -- use
`VACUUM INTO` (+ swap) or `journal_mode=DELETE` around VACUUM (needs exclusive access).
(2) Elevated processes (the service runs `-RunLevel Highest`) are invisible to a non-elevated
`Get-Process .Path`/CommandLine match AND unkillable from a non-elevated session -- destructive KB
ops that need exclusive access must run from elevated PS. (3) The PowerShell sandbox analyzer
false-positives when `Remove-Item` and `schtasks /Change` share one command -- keep file ops and
scheduler ops in separate commands.

---

### [INFRA/PERF] Per-entity tool exposure + weekly self-watching health metrics -- 2026-06-08 (commits 691c6c1, f184267)

Repo HEAD: `f184267` on `origin/main` | full suite **3,574 passed / 41 skipped** | import smoke clean.
Game-plan payoff-items 2 + 4. **Committed + pushed + LIVE** (Cora restarted 2026-06-08 17:30 AZ,
heartbeat fresh; weekly task registered by Harrison, smoke post to #cora-health = True).

**Per-entity tool exposure (commit `691c6c1`):** all 48 tool schemas (~14.5K tok) shipped on every
Claude call regardless of channel. Now `tool_dispatch.tools_for_entity(entity, cross_entity)` offers
only the channel-entity's tools + a global core (asana/gmail/calendar/cashflow/decisions/slack_dm).
`claude_client._build_cached_tools(entity, cross_entity)` filters in TOOL_DEFINITIONS order (stable
per-entity cache key); app.py passes `cross_entity_tools=is_founder`. Aggregators (FNDR/HJRG) and the
founder-from-any-channel get the full set, so Harrison's cross-entity questions + the FNDR-only
dashboards stay reachable. Sub-entities resolve to parent (OSNGF->OSN, LEX-LLC->LEX, HJRP-1337->HJRP).
Lean entities (F3C/HJRPROD) now ship ~11 core tools, not 48; LEX excludes HubSpot (Tier-1 doctrine).
**NOT a security boundary** -- cross_entity_guard + per-tool runtime guardrails remain that layer;
the map errs inclusive. Mapping validated vs the entity system prompts. +18 tests
(`tests/test_tool_exposure.py`).

**Weekly self-watching health metrics (commit `f184267`, game-plan section 10.4):** `cora_health_report.py`
gained `--slack`: builds the Phase-0 metrics, runs the section-5 threshold checks (FNDR co-scan >60%,
KB >750K chunks, logs+ledgers >300MB, >2 tasks colliding on one clock time in 03:00-09:00), and posts a
compact mrkdwn digest (alarms first) to Slack. `deployment/setup-weekly-health-metrics-task.ps1` registers
`Cora - Weekly Health Metrics`, Monday 09:30 AZ, `--slack --channel cora-health --log-days 7` (offline
char/4, no API key). **Smoke-tested live: posted to #cora-health = True** (and one earlier test landed in
#hjrg-leadership before the channel was pinned). First live alarm already firing: up to 3 cora tasks share
a clock time in the 03:00-09:00 window (4:00 AM cluster) -- a future staggering cleanup.

**✅ Both activation steps DONE 2026-06-08:**
1. ✅ Weekly task registered (Harrison, elevated PS): `Cora - Weekly Health Metrics`, next run 6/15 09:30,
   smoke post to #cora-health = True. First alarm already firing: up to 4 tasks share a clock time in the
   03:00-09:00 window -> **acted on**: 3 heavy jobs staggered out (Drive Sweep 03:30->06:00,
   founders-os-sweep 04:15->06:30, backup 04:30->13:00; commit `1a88f10`) -- pending re-register of those
   3 tasks (elevated PS). (FYI: weekly task Logon Mode = "Interactive only" -- fires only while the host is
   logged in; fine for the always-on desktop, but won't run unattended if logged out.)
2. ✅ Cora restarted 17:30 AZ -- per-entity tool exposure live. Confirmed tool counts in loaded code:
   FNDR 47 (full) / F3E 41 / OSN 22 / LEX 19 / HJRP 19 / UFL 14 / F3C 11 / HJRPROD 11; founder-from-OSN
   channel = 47 (cross-entity preserved). The 15h-old WIP (schema busy_timeout PRAGMA + embeddings token-batch
   cap; drive_sweep not bot-loaded) was assessed safe and went live with this restart.

_Note: cache_read/input median still 0.0 in the billing parse is EXPECTED, not a bug -- the caching split
works (probe: 0 -> 68,706 on a warm repeat) but at current low volume real traffic rarely has 2 mentions in
one entity within the 5-min window before a CLAUDE.md edit/restart invalidates block 2. The win lands on bursts._

---

### [INFRA/PERF] Phase 0 baseline + caching split (static CLAUDE.md under the cache) -- 2026-06-08 (commits e7f8cd7, 37f2780)

Repo HEAD: `37f2780` on `origin/main` | **3,556 passed / 41 skipped** | import smoke clean |
Cora restarted 2026-06-08 22:29 UTC, `heartbeat alive uptime_s=120`, KB prewarm 5.2s, no errors.
Implements Phase 0 + payoff-item 1 of
`_shared/projects/cora/design/2026-06-08_fndr_cora-scaling-memory-game-plan.md`.

**Deliverable A -- `scripts/cora_health_report.py` (commit `e7f8cd7`):** repeatable Phase 0 /
weekly health metric. Six sections (KB corpus by entity+source, static-context tokens/entity,
tool-block size, billing parsed from logs/cora-*.log usage lines, state sizes, scheduled-task
03:00-09:00 overlaps). Offline char/4 by default; `--count-tokens` for the Anthropic endpoint;
`--json` for the ritual. No restart needed (new script + read-only).

**MEASURED BASELINE (2026-06-08 -- reconciles game-plan section 5):**
- KB **224,771 chunks. FNDR co-scan share 18.8%** (42,320) -- NOT the ~56% the mirror estimated.
  **LEX is the largest partition (81,882 = 36%)**, so LEX is the co-scan ceiling to watch, not
  FNDR. Both well under the 60% act-threshold.
- Static context/entity: F3E ~38.2K tok, OSN ~36.3K, LEX ~35.8K, BDM/HJRG/FNDR ~30K. LEX
  sub-entities ~1K (founder-firewalled). This is the uncached mass the split moves.
- Tools: 46 / ~14.5K tok. cora_kb.db 5.3GB (float table not yet dropped); logs/ 34MB; JSONL
  ledgers all small (no compaction needed yet).
- Billing (5 logs, low volume): median input 37,589, **cache_read/input = 0.0** (BEFORE baseline).
- Bench fast path (fresh): LEX p50 2.32s / FNDR 0.92s / F3E 1.47s / OSN 1.00s, recall@10 100%;
  float fallback ~8-9.8s. LEX p50 highest (largest partition) -- under the 3s warm threshold.
- **24 cora tasks land in the 03:00-09:00 AZ window** (e.g. 3:30 drive-sweep + kb-sync-fireflies
  overlap; 4:00 four tasks). Staggering is a future cleanup, not in scope.

**Deliverable B -- caching split (commit `37f2780`):** the entity prompt + tools were cached, but
the founder CLAUDE.md (~40K tok) + entity CLAUDE.md + known-answers + dynamic snapshots rode in
the UNCACHED context arg, re-billed every mention (twice on tool turns). Now a 3-block system
array: block 1 (prompt) + block 2 (static portfolio context) both `cache_control: ephemeral`;
block 3 (runtime + per-query KB chunks) uncached. `context_loader.load_context_parts()` returns
(static, kb) separately; `load_context()` is a byte-identical wrapper (no-query path still returns
the cached static object). `claude_client._build_cached_system()` gained optional `static_context`
(2-block back-compat when falsy); `generate_response[_streaming]` gained `cached_context=`; app.py
passes static_text there and runtime+kb_text as the uncached block. Retrieval path / distance
threshold / LEX sub-entity firewall untouched. +13 tests (`tests/test_caching_split.py`).

**AFTER -- empirically validated live (probe: same entity, 2 identical calls):** block shape = 3,
cache_control on blocks 1+2 only. Cold: input=342, cache_create=68,706, cache_read=0. Warm repeat:
input=**3**, **cache_read=68,706** (full prefix served from cache). The ~40K static mass + tools
now cache-read on a warm repeat instead of re-billing.

**Honest tradeoff:** block 2 invalidates whenever CLAUDE.md is edited (TOM ~daily) or on restart;
the win lands on the common case -- 2+ mentions in one entity within the ~5-min ephemeral window
(aligns with context_loader's 300s static TTL). The 2-breakpoint design keeps block-1 (prompt)
caching even when block-2 changes.

**Smoke test for Harrison:** two identical `@Cora` mentions in one entity channel within 5 min;
the 2nd's `claude usage` log line should show cache_read ~= entity_prompt + CLAUDE.md tokens and
input drop to the query remainder. Re-run the baseline any time:
`.venv\Scripts\python.exe scripts\cora_health_report.py`.

**Next sessions (game-plan section 10):** 2) per-entity tool exposure; 3) slim the wholesale
CLAUDE.md inject; 4) FNDR/Slack archive tier + schedule cora_health_report.py weekly; 6) drop the
float32 KB table.

_Note: code (commits e7f8cd7 + 37f2780) is committed + pushed + LIVE. This TOM entry is NOT yet
committed -- CLAUDE.md already carried concurrent uncommitted TOM entries from other sessions
(Fireflies DWD coverage, gmail/drive sweep); leaving it uncommitted avoids entangling the sessions.
All these TOM entries ride in the working tree until the next host cascade/ship commits CLAUDE.md._

---

### [FNDR] Fireflies DWD coverage monitor -- 2026-06-08 (commit 172d064; task NOT yet registered)

Repo HEAD: `172d064` on local main | **3,469 passed / 41 skipped** (+28 mine) | import smoke clean.
TOM entry written but CLAUDE.md NOT committed this session (it carried concurrent gmail/drive
sweep edits; commit was 4 Fireflies paths only -- this TOM lands via the next cascade/gmail ship).

**Problem:** Fireflies was only capturing Harrison's meetings. The org-wide invite wave (founder TOM
0i, 16 invites 2026-06-03) was stalling silently -- no automated signal when a teammate never
accepts or never connects their calendar. DWD does NOT help (Fireflies is third-party SaaS; DWD
grants Cora's service account access, not Fireflies). Needs durable assurance, so: a weekly monitor.

**Shipped (4 files, +1,224 lines):**
- `connectors/fireflies_connector.py`: `list_team_members()` (admin `users` GraphQL query --
  confirmed live, full field set) + `has_recent_host_meeting(email, days=30)` recency probe.
  **Schema correction:** the plan doc's `organizers: [String]` arg was REJECTED by the live schema;
  correct arg is a single `organizer_email: String`. `num_transcripts` returns null (not 0) for
  never-recorded members -> normalized to 0.
- `connectors/fireflies_coverage.py` (NEW, pure/no-network classifier): `load_dwd_humans()` reads
  `monitored-email-accounts.yaml`, drops shared inboxes (payables/receipts/service), collapses
  cross-domain aliases via union-find over {slack_user_id, shared-email, normalized-name} (the
  normalized-name edge is what collapses Alex's slack-less/alias-less UFL-legacy entry). `classify()`
  -> **3 statuses: COVERED / MEMBER_NO_RECORDINGS / NOT_A_MEMBER.** `MEMBER_NO_CALENDAR` was DROPPED
  -- `integrations` provably does NOT carry calendar state (Harrison has 566 transcripts, no calendar
  in his integrations list), so it would be dead/undetectable.
- **CORRECTNESS LOCK:** membership is authoritative. The organizer probe reflects "someone with a
  connected calendar attended", NOT "this person's calendar is connected" (Larry's 5/5 meeting was
  captured only because Harrison was in the room, yet Larry is not a member). The probe ONLY refines
  people who are ALREADY members; it never promotes a NOT_A_MEMBER to COVERED.
- `scripts/run_fireflies_coverage.py` (NEW): `--dry-run` / `--digest-only` / `--nudge` / `--days N`.
  Digest = DM to Harrison `U0B2RM2JYJ1`. Nudges (only with `--nudge`, status-branched copy) throttled
  7d/user via new `data/fireflies_coverage.db` (`coverage_nudge_log`). Fail-closed: if `users` errors,
  digest still sends with a "could not enumerate" note (no crash, no nudges).
- `tests/test_fireflies_coverage.py`: 28 tests (roster collapse incl. Alex-by-name, all 3 statuses,
  alias/case-insensitive match, recency refinement, correctness lock, 7d throttle on/off, fail-closed).

**Rollout (LOCKED by Harrison):** digest-FIRST. Task ships as `--digest-only`; flip to `--nudge` only
after Harrison eyeballs the first weekly digest. Task `cowork-cora-fireflies-coverage`, **Weekly Mon
08:00 AZ** -- **NOT yet registered** (CP-3 gate pending in the relay before
`deployment\setup-fireflies-coverage-task.ps1` runs from elevated PS).

**Live gap as of 2026-06-08 (dry-run, of 20 DWD humans):** 2 COVERED (Harrison 566; Shaun 1 -- Shaun
flipped from member-no-recordings to COVERED between CP-1 and the dry-run, i.e. the monitor caught a
real live state change), 6 MEMBER_NO_RECORDINGS (Elena, Eric, Hannah, Jeff, Jen, Micah), 12
NOT_A_MEMBER (Alex, Alina, Brett, Daniel, Gaelan, Jake, Jason, Justin, Larry, Matt, Sophia, Tommy).

**Jason kept** (`jason@f3energy.com` -- identity-TBD F3E mailbox, genuinely not covered, no slack_user_id
so never auto-nudged). **Future option (not built):** to exclude any mailbox from coverage, add a
roster-level `coverage_monitor: false` flag in `monitored-email-accounts.yaml` that the loader respects
-- keep the monitor 100% roster-driven, no hardcoded names.

Plan: `_notes/2026-06-08_fndr_fireflies-coverage-monitor-build-plan.md`. Relay:
`_notes/2026-06-08_fireflies-coverage-RELAY.md`.

**Update (CP-5, 2026-06-08, commit `e39b7a2`):** Harrison reviewed the first digest, offboarded 4 departed
users (Brett, Gaelan [both f3e+ufl entries], Jason, Sophia) -> `enabled: false` in
`monitored-email-accounts.yaml` (runtime-effective immediately; YAML not committed -- mixed with the
gmail/drive-sweep sessions' edits, rides their ship). Roster 20 -> 16; NOT_A_MEMBER 12 -> 8. Task arg flipped
`--digest-only` -> `--nudge` + re-registered; **next auto-run Mon 2026-06-15 08:00 AZ** will DM Harrison the
digest AND nudge 14 uncovered teammates (8 "accept invite" + 6 "connect calendar", 7d throttle). **NOT fired
manually** -- the first live nudge batch is irreversible comms and awaits Harrison's explicit "fire now" (or
the 6/15 auto-run carries it).

### [DRIVE/KB] Drive+Sheets sweep recall fixes -- 2026-06-08 (STAGED, host ship pending)

Repo HEAD at session start: `be4d4d6` on `main`. **STAGED in Cowork sandbox; NOT
committed** -- host ship via `deployment\ship-drive-sweep-recall-2026-06-08.ps1`
(elevated PS). Commits THIS session's files ONLY -- does NOT touch the separately
staged Gmail sweep work below.

**Context:** the multi-user Drive+Sheets sweep (`Cora - Drive Sweep`, 3:30am AZ,
`run_drive_sweep.py`) has run nightly since 5/28 and works (Hannah 2,650 / Justin
5,691 chunks). But its own logs (`drive-sweep-*.log`) showed three recall gaps,
all on Sheets/large files, plus an entity-tagging gap:
1. **Large Google Sheets 403 on Drive export** (`exportSizeLimitExceeded`, 17 hits
   5/28-6/07) -> the biggest, most data-rich sheets dropped entirely.
2. **Large files 400 at embed time** (OpenAI 300K-token request limit, 10 hits,
   e.g. Rita Tracking.xlsx) -> whole file lost.
3. **Sheet extraction truncated to first 200 rows/tab** -> later rows unrecallable.
4. **Haiku entity misclassification** (OSN P&L -> LEX; HJRP invoice -> LEX-LLC) ->
   pollutes entity-scoped recall + cross-entity-surfacing risk.

**Fix (staged, 3 src + 3 tests + 1 ship script):**
- `src/cora/knowledge_base/embeddings.py`: `embed_texts` now batches by BOTH count
  (100) AND a 250K-token budget (`MAX_BATCH_TOKENS`); a single oversized input
  becomes its own batch (never dropped). Fixes gap 2 globally (all connectors).
- `src/cora/connectors/drive_sweep.py`: on export failure, fall back to the Sheets
  API values reader per tab (`_extract_sheet_via_api`, no export ceiling) via new
  `_build_sheets_service` (DWD) + `_build_sa_sheets_service_direct`; row cap 200 ->
  `_MAX_SHEET_ROWS=5000`; `sheets_service` threaded through `sweep_user` +
  `sweep_founders_os`. Gaps 1+3.
- `src/cora/connectors/drive_entity_detect.py` (NEW): deterministic HJR
  naming-convention entity override applied after Haiku in `sweep_user` ONLY
  (`sweep_founders_os` is already folder-path-deterministic). Conservative: only
  fires on an exact code token in the first 2 naming positions. Gap 4.
- Tests: `test_embeddings_batching.py`, `test_drive_entity_detect.py`,
  `test_drive_sweep_sheets.py`.

**PREREQ for the oversized-sheet fallback:** add scope
`https://www.googleapis.com/auth/spreadsheets.readonly` to the Cora SA DWD grant
(admin.google.com -> Security -> API Controls -> Domain-wide Delegation). Code
degrades gracefully without it (oversized sheets stay dropped, same as today) --
safe to ship either way, but the re-backfill only recovers them once granted.

**Ship steps (in the PS1):** import smoke -> full pytest -> commit/push (this
session's files only) -> optional Cora restart -> optional one-time all-time
re-backfill `run_drive_sweep.py --backfill --freshness-days 36500` (recovers
previously-dropped sheets/large files, full rows, corrected entity tags;
resumable + idempotent). **COST:** `--backfill` re-embeds the corpus -> real
OpenAI spend; narrow `--freshness-days` to limit it.
**Restart:** NOT required for the fix to take effect in the nightly sweeps (each
sweep is a fresh process and picks up new code automatically). Recommended for the
live bot via the PS1's doctrine-#5 sequence so runtime KB upserts use it too.
**Verify:** `drive-sweep-<date>.log` shows `COMPLETE` + any "recovered oversized
sheet" lines (if oversized sheets exist + scope granted).

**Sandbox note:** full pytest + import smoke NOT runnable in this Cowork sandbox
(no `openai`/`googleapiclient`); validated via py_compile + standalone batching
algorithm check + `drive_entity_detect` unit run. Host PS1 runs the authoritative
pytest before commit. Stale-virtiofs truncated-read artifact reappeared on
host-edited files (`embeddings.py`, `monitored-email-accounts.yaml` looked
corrupt/truncated in sandbox `python3` but are intact on host) -- trust the Read
tool / `git show HEAD:` for those files, not sandbox bash.

### [GMAIL/KB] Gmail sweep was broken since 5/28 -- coverage + resumability fix -- 2026-06-08 (STAGED, host ship pending)

Repo HEAD at session start: `be4d4d6` on `main` (846faf3 KB fast-path + 2 doc commits on top).
**STAGED in Cowork sandbox; NOT yet committed** -- host ship via
`deployment\ship-gmail-sweep-coverage-2026-06-08.ps1` (elevated PS). No cowork-cora-service
restart required (the sweep is an independent scheduled task, not the bot process).

**Problem (audit finding):** the nightly Gmail KB sweep (`scripts/gmail_threaded_sweep.py`,
task `cowork-cora-kb-sync-gmail` 2:30am AZ) has been **silently failing since 2026-05-28**.
Read-vs-unread was never the issue -- the query is `after:{ts}` (covers read AND unread, all
mail minus spam/trash). The real bugs:
1. **Task `-ExecutionTimeLimit 1h`** (setup-kb-sync-tasks.ps1) kills the run every night ~3:30am,
   **14 of 28 accounts in -- always mid `jason@f3energy.com`.** "sweep complete" never logged.
2. **Watermark persisted only once at end-of-run** -> since the run never finishes,
   `data/cache/gmail-thread-watermarks.json` hasn't been written since **5/28 09:43**. The first
   ~7 accounts re-scan a growing backlog from 5/28 every night (harrison 284->427 threads; hannah +
   justin pinned at the 500 cap), re-embedding the same mail nightly (wasted OpenAI spend).
3. **14 mailboxes never reached at all**: the entire Lexington set (payables/receipts/harrison/
   justin/jeff/shaun/jen) + entire UFL set (harrison/hannah/alex/sophia/gaelan) + gaelan/brett@f3e.
4. **`uv run` in the task action** -> D-005 violation + venv-lock deadlock risk vs the live service.
5. **Roster gap**: 5 DWD-eligible Slack users had NO mailbox entry -- Eric Canku, Daniel Sion,
   Jake Lichtman, Micah Kessler, Elena Meirndorf.

**Fix (staged, 4 files + 1 ship script):**
- `scripts/gmail_threaded_sweep.py`: (a) **incremental atomic watermark** -- persisted after EACH
  account, so a mid-run kill still advances completed accounts and the next run resumes;
  (b) **stale-first ordering** (`_order_accounts`) -- oldest/never-swept mailboxes processed first,
  so no account is starved behind the time wall; (c) **cap-aware watermark** (`_next_watermark`) --
  on cap-hit, advance only to newest-processed (not sync_start) so older backlog is not silently
  dropped; (d) CLI `--max-threads` + `--accounts` for targeted/deep backfill.
- `data/maps/monitored-email-accounts.yaml`: +6 DWD-eligible mailboxes (Eric, Daniel, Jake,
  Micah, Elena, + tommy@hjrglobal.com per Harrison 2026-06-08), thread_sweep only
  (attachment_filer/drive_sweep off). Now 34 thread_sweep accounts. Demi Bagby's personal
  mailbox deliberately EXCLUDED per Harrison 2026-06-08 (do not add).
- `deployment/setup-kb-sync-tasks.ps1`: `uv run` -> `.venv\Scripts\python.exe` (D-005); Gmail
  task limit 1h -> **3h**; ASCII-only rewrite (D-016, file had em-dashes).
- `tests/test_gmail_threaded_sweep.py`: +8 tests (`_order_accounts`, `_next_watermark`).

**Host ship steps (in the PS1):** import smoke -> full pytest -> git add/commit/push ->
re-register kb-sync tasks (`setup-kb-sync-tasks.ps1`) -> optional one-time deep backfill
`.venv\Scripts\python.exe scripts\gmail_threaded_sweep.py --fallback-days 400 --max-threads 2000`
(drains the 5/28 backlog + the 14 dark accounts; resumable, safe to re-run). Even without the
manual backfill, the next 2:30am run self-heals stale-first.
**Verify after ship:** `logs/kb-sync-gmail-<date>.log` shows a "sweep complete" line and
`gmail-thread-watermarks.json` mtime is fresh with ~33 advancing entries.
**Resolved 2026-06-08 (Harrison):** Demi Bagby's personal mailbox stays EXCLUDED;
`tommy@hjrglobal.com` ADDED to the sweep. No open roster questions remain.

### [INFRA] KB vector-search fast path (binary quantization) -- 2026-06-07 (commit 8d3c3d0, ship 846faf3)

Repo HEAD: `846faf3` on `origin/main` | **3,398 passed / 41 skipped** | **MIGRATION RAN -- fast path
ARMED** (262,441 vectors backfilled; Cora restarted via `ship-kb-binary-index-2026-06-07.ps1`).

**MEASURED on live data (bench, LEX, 12 queries, on-disk):** float p50 **7,749ms** / p95 7,928ms ->
fast p50 **1,082ms** / p95 1,137ms = **~7.2x faster, recall@10 100%**. Original cold case was ~31s.

**DB note (benign):** `knowledge_vec`=262,441 but `knowledge_chunks`=223,799 -> ~38,642 orphan
vectors (chunk row deleted, vector left behind) pre-existed. Harmless: re-rank JOINs
knowledge_chunks so orphans drop on BOTH paths (recall 100%). ~230 MB dead weight -- cleanup later
alongside the `knowledge_vec` drop.

**Problem:** KB vector search was ~31s cold (75% of total mention latency). Root cause: brute-force
float KNN (`embedding MATCH`) scanning the entire ~1.4 GB vec0 float index over 223,799 chunks
(24x growth since Phase 3 shipped at 9,380). Aggravator: `context_loader` opened a fresh
`KnowledgeBase` per request, so the startup prewarm warmed a connection nothing used and
"Knowledge Base schema initialized" logged on every request (read as a cold start).

**Shipped (sandbox session 2026-06-07):**
- **Binary-quantized fast path** (`store.py` + `schema.py`): new `knowledge_vec_bin` (vec0
  `bit[1536]`, 1/32 the bytes ~= 43 MB, with an `entity` metadata column for in-scan pre-filtering)
  + `knowledge_vec_f32` (plain btree blob table for true O(log n) PK re-rank reads). `search()`
  does a coarse hamming scan (entity pre-filtered, `coarse_k=1000`) then exact float32 re-rank via
  `vec_distance_l2` -- same metric as the old float path, so the `_KB_MAX_DISTANCE=1.30` threshold
  is unchanged. Float path kept verbatim as the fallback; gated by the `kb_bin_index_ready`
  checkpoint so deploy is decoupled from the data migration. `upsert` keeps bin+f32 in sync.
  **sub_entity strict filter is unchanged** (security invariant, applied at re-rank vs knowledge_chunks).
- **Shared KB instance** (`context_loader.py` + `main.py`): one lock-serialized `KnowledgeBase`
  reused by all request threads AND the prewarm. Acceptance met: no schema-init log per request.
- **Migration** `scripts/migrate_kb_binary_index.py`: idempotent, resumable backfill from existing
  float vectors (NO re-embedding); heartbeat guard; arms the fast path only when bin==f32==knowledge_vec.
- **Bench** `scripts/bench_kb_search.py`: float vs fast p50/p95 + recall@10 guard (>=0.80).
- **+11 tests** (`tests/test_kb_binary_search.py`): fast==float equivalence, entity isolation,
  sub_entity strict invariant, recency, bin/f32 sync.

**Recall sized empirically:** sweep on worst-case (random-gaussian) vectors -> `coarse_k=1000`
gives ~98% recall@10; real clustered embeddings recall higher. Re-rank of 1000 candidates adds
~2-3ms (probe-measured), negligible vs the multi-second budget.

**Latency:** scan dropped 1.4 GB -> ~50 MB. Measured fast p50 ~1.1s (target was warm < 3s) at 262K
vectors; bench `scripts/bench_kb_search.py` reproduces float-vs-fast + recall@10 any time.

**Ship sequence (DONE 2026-06-07):** `ship-kb-binary-index-2026-06-07.ps1` ran -- push -> stop Cora
(WMI command-line kill after the `Get-Process .Path` match failed twice; doctrine confirmed) ->
backup -> migrate (262,441 rows, ~9 min @ ~450/s) -> bench -> restart. **Post-restart verify still
worth eyeballing:** first `#llc` mention logs `latency_ms` low (~1s) with NO "schema initialized"
line; `kb-prewarm: vector index warmed in <1s`.

**Follow-ups:** (1) after ~1 week stable, drop `knowledge_vec` (fallback only now) -> reclaims
~1.4 GB and also clears the 38K orphan vectors. (2) `ship-kb...ps1` Step 1 hardened mid-run to the
CIM/CommandLine kill -- reuse that pattern, not `Get-Process .Path`, for future stop-Cora scripts.

_Note: this was a selective commit -- the `gap_autofill` work (`app.py`, `run_knowledge_review.py`)
was committed separately by its own cascade (54b1ef2); never touched here._

### [FNDR] Knowledge-gap autofill from Slack conversations -- 2026-06-07 (commit 54b1ef2)

Repo HEAD: `54b1ef2` on `origin/main` | **3,360 passed / 41 skipped** | task `cowork-cora-gap-autofill`
registered (daily 6:00am AZ) | Cora restarted 03:17 UTC, heartbeat confirmed | 8 files, +1,563 lines.
Note: `cascade-push-gap-autofill-2026-06-07.ps1` is gitignored (one-shot, not committed).

**Problem:** 41 gaps in `logs/knowledge-gaps.jsonl`, only 1 ever resolved via the manual digest flow.

**Shipped (Cowork session 2026-06-07) -- two-stage autofill, both stages Harrison-gated (D-011 intact):**
- **Stage 1 MINE** -- `src/cora/gap_autofill.py`: per open gap, entity-scoped KB search restricted to
  swept Slack conversation chunks (`source="slack"`, distance <= 1.30, PHI-filtered), Haiku drafts an
  answer (FAIL-CLOSED: API/parse error proposes nothing), proposal lands in the existing 7am
  knowledge-review DM queue as new `update_type="known_answer"`.
- **Stage 2 ASK** -- gaps with no evidence after 72h get ONE escalation DM to the entity domain owner
  (`data/maps/gap-domain-owners.yaml`; LEX* + PHI gaps NEVER escalate; max 3 asks/run). Reply captured
  in app.py DM path (threaded replies always; top-level only when not an OSN shift command), routed
  through the same Harrison gate. Decline phrases ("no idea", "not my area") leave the gap open.
- **Executor** -- on Harrison thumbs-up, `run_knowledge_review.py` calls `gap_autofill.apply_known_answer`:
  appends to `design/known-answers/{entity}.md` "## Known facts" (same format as the digest flow,
  loaded into Cora's per-entity context) + records `.resolved-gaps.jsonl` + posts to #hjrg-leadership.
- **New scheduled task** -- `cowork-cora-gap-autofill` daily 6:00am AZ (after 2am Slack sync, before
  7am knowledge-review): `scripts/run_gap_autofill.py` (--dry-run / --max-gaps / --no-escalate).
  Register: `deployment\setup-gap-autofill-task.ps1` (elevated PS).
- **Tests:** `tests/test_gap_autofill.py` -- 54 tests (loading, evidence filtering, fail-closed drafting,
  escalation eligibility, ask lifecycle, executor, wiring assertions). Host suite 3,360 passed / 41
  skipped + import smoke clean at commit time.
- **State files:** `data/state/gap_autofill_state.json` (per-gap) + `data/state/gap_ask_pending.json` (asks).

**✅ Restart DONE 2026-06-08 03:17 UTC** (app.py DM reply capture live). First scheduled fire: 6:00am AZ.
**Smoke test queued:** `.venv\Scripts\python.exe scripts\run_gap_autofill.py --dry-run` then check the
7am knowledge-review DM the next weekday morning for any `known_answer` proposals.
**Doctrine note:** mid-session `git stash/pop` on the Cowork mount caused stale-size truncated reads of
app.py/CLAUDE.md (sandbox saw old st_size with new pages -- looked like file corruption). Recovery:
restore from `git show HEAD:file`, re-apply edits with sandbox-side writes. Avoid stash on the mount.

---

### [LEX + KB] Ingest-time LEX sub-entity tagging (Part 2) -- 2026-06-07 (commit 2e0c2a4)

Repo HEAD: `2e0c2a4` on local main (NOT yet pushed -- see pending host run below).
Prior 3 commits (`eaf25da`/`9f7fa66`/`bcb997e`, Asana YAML governance fixes from a
concurrent session) were already at HEAD when this session started.

**Problem:** The 5/31 sub-entity backfill was a one-shot script. Nightly syncs kept
writing LEX chunks with sub_entity=NULL -- by 6/07 the KB (223,799 chunks) held 52,916
NULL LEX chunks, 5,906 of them with UNAMBIGUOUS sub-entity signals (drive_sweep 41.7K +
gmail 7.4K NULLs dominated). Strict filter kept them OUT of sub-entity channels, so this
was a retrieval-coverage gap, NOT a leak -- sub-entity channels were blind to ~5,900
chunks they should see.

**Fix (commit `2e0c2a4`, 4 files):**
- New `src/cora/knowledge_base/lex_sub_entity.py` -- shared detection module (locked
  exactly-one-match keyword rule from the 5/31 ship, patterns unchanged).
- `store.upsert_documents` Step 0: LEX docs arriving with sub_entity=None get detected +
  tagged at the choke point -- covers ALL connectors permanently. Explicit connector tags
  never overridden; ambiguous / general-LEX stays NULL (GM-level) by design.
- `scripts/backfill_lex_sub_entity.py` refactored to import the shared module (now the
  catch-up sweep only; backward-compatible aliases keep old tests green).
- 14 new tests: `tests/test_lex_sub_entity_ingest.py`.

**Test state:** Session ran in Cowork Linux sandbox (host .venv not executable here).
Full-suite sandbox parity run: 3,292 passed / 41 skipped / 14 failed -- ALL 14 failures
reproduced identically on pristine HEAD (11 sqlite-vec vec0 KNN env errors + 3 gsheets
cache env errors). Zero regressions from this change. Import smoke clean through all
cora modules (stops only at app.py's live Slack call, which needs host network).

**✅ HOST RUN COMPLETE 2026-06-08 03:06 UTC** (via `rescue-git-and-ship-lex-tagging-2026-06-07.ps1`):
host suite **3,360 passed / 41 skipped**, import smoke OK, pushed `bcb997e..297ea43`,
catch-up backfill APPLIED (5,906 tagged: 4,515 LLC / 762 LTS / 434 LBHS / 195 LLA),
Cora restarted, heartbeat fresh. ⏳ Remaining: Slack smoke test `@Cora what's the
revalidation status?` in an #lts-* or #llc-* channel.

**🚨 GIT PACK CORRUPTION INCIDENT (same session, RESOLVED):** git auto-maintenance ran
during the Cowork sandbox session and rewrote packs over the virtiofs mount -- pack
`c44d7e5a` came out corrupt (object de2e7c04 hash mismatch). Recovery: fresh bare clone
from origin, pack transplant, corrupt pack quarantined to `.git-corrupt-backup`, dead
`refs/stash` dropped (only casualty -- local stash, contents unknowable, nothing pushed
or committed lost). fsck clean. **DOCTRINE LOCKED: `gc.auto=0` + `maintenance.auto=false`
set in this repo's local config -- sandbox sessions must NEVER auto-repack over virtiofs.
Do not unset these.** Cleanup when satisfied: remove `.git-corrupt-backup` + `C:\Users\Harri\code\cora-rescue.git`.

**Side findings:** (1) Working tree has a concurrent session's uncommitted gap_autofill
work (`src/cora/gap_autofill.py` + script + tests, modified app.py + run_knowledge_review.py)
-- deliberately NOT committed here. (2) 5 KB chunks carry sub_entity='LEX-LCI' which is not
a LEX sub-entity (LCI is HJRP's) -- worth a look in a future hygiene pass. (3) Sandbox
doctrine: virtiofs caches go stale when host file tools edit a file the sandbox already
read -- converge by rewriting from the sandbox side; never git-commit a file edited
host-side this session without verifying the sandbox view parses + tail is intact.

---

### [SECURITY + INFRA] Cross-entity firewall closed + team training manual — 2026-06-06 (9076b42→3748203)

**`cross_entity_guard.py` — deterministic pre-LLM cross-entity keyword interceptor**
- 8 entity keyword dicts: F3E / LEX / OSN / UFL / BDM / HJRP / HJRPROD / F3C
- FNDR + HJRG = pass-through aggregators (not blockable — they are the portfolio layer)
- PAIRED_ENTITIES = {F3E↔F3C} — brand + nonprofit pairing is intentional per doctrine
- Keywords removed from F3E to prevent OSN false positives: "energy drink", "shopify", "dtc"
- Wired at 2 sites in `app.py` BEFORE any Claude API call fires
- 16 tests: `tests/test_cross_entity_guard.py` + `tests/test_cross_entity_firewall.py`

**`hjrprod.md` typo fix (commit 9076b42):** "F3C (F3 Cannabis)" → "F3C (F3 Community — the nonprofit arm)"

**Team training manual written + saved to Drive:**
`G:\My Drive\HJR-Founder-OS\_shared\projects\cora\2026-06-06_fndr_cora-team-training-manual.md`
10 sections: capabilities, channel scoping, guardrails, automated messages schedule, meeting
action capture, knowledge approval map, troubleshooting, quick reference card.

**Verified live:**
- 3,290 tests passing (main @ `3748203`)
- 50 active tasks (confirmed 2026-06-06 -- Tier 3 registrations complete)
- Smoke test: `@Cora pipeline summary` in `#f3e-leadership` (confirm after next restart)

**Pre-distribution checks (do before distributing manual to team):**
1. Verify calendar DWD propagation: test `@Cora schedule a 15-min call`
2. Check Fireflies invite acceptance: app.fireflies.ai/settings/team/members-and-groups
3. Post reminder in #all-hjr-global

**Asana hygiene blocked:** 9 project renames + 3 archives require Tessa Miller to grant Harrison
project-admin access first. Chrome Agent prompt at:
`C:\Users\Harri\code\cora\_notes\2026-06-06_chrome-agent-asana-renames-archives.md`

**DOCTRINE LOCKED (D-034):** Prompt-only enforcement insufficient for hard security requirements.
Code-level interception required at the earliest possible intercept point in app.py before LLM is
called. Pattern: guard module + wire before any Claude API call. Same doctrine as sibling_guard.py
(2026-05-24), now generalized to portfolio-wide cross-entity firewall.

---

### [INFRA] Hygiene root fixes — D-031 Code delivery -- 2026-06-06 (commits 6429aa3, 1122214, 8991289, 2020f91, 8381b6f, d2c6929)

Repo HEAD: `2020f91` on `origin/main` | **3,269 passed / 41 skipped** | 45 active tools | Cora restarted, heartbeat confirmed (uptime_s 60→120).

Three code-level anti-recurrence fixes for the issues the 2026-06-06 hygiene-asana sweep surfaced (D-031). All on origin/main; test suite is now order-independent.

**Fix 1 — Meeting Action Capture: atomic watermark + lockfile + creation-time dedup (commit `6429aa3`)**
- Root cause of 6/4 "13 WCF Review" double-fire: even after 1d17912, the watermark (incl. transcript-ID ledger) was flushed only once at end-of-run. A crash after task creation → next run reprocessed the same meeting.
- Three new layers added:
  1. **Per-meeting atomic persistence**: watermark flushed immediately after each transcript is processed (no wait-until-end-of-run).
  2. **Process lockfile** at `data/state/meeting_action_capture.lock` — stale if >2h, auto-clears. Concurrent instance exits immediately.
  3. **Creation-time dedup guard** `find_recent_duplicate_task` — skips creating an action item if an identical OPEN task already exists from the last 7 days.

**Fix 2 — Nudge ledger unified (commit `1122214`)**
- Problem: daily Tier-3 Asana Hygiene Nudge + weekly hygiene-asana closure sweep were both commenting on the same stale tasks.
- New `src/cora/nudge_ledger.py` reads AND appends the EXISTING `closure-nudges-throttle.jsonl` (the same file the weekly sweep uses for its 7-day lockout) — bidirectional, zero SKILL change required.
- Daily nudge now skips any task nudged by EITHER system within 14 days.
- **Doctrine locked**: max 1 automated comment of any kind per task per 7 days.

**Fix 3 — Captured tasks routed into projects (commits `8991289` mechanism + `2020f91` live)**
- Problem: Fireflies-captured action items created with NO project → untaggable orphans cluttering My Tasks.
- New `data/maps/meeting-capture-projects.yaml` maps each entity → its catch-all Asana project. All GIDs sourced from asana-project-map.yaml and cross-checked (catch_all_gid per entity). BDM intentionally excluded (empty). LEX* entries populated but inert — PHI guardrail skips all LEX meetings before routing ever runs.
- Captured tasks now: routed into project + `Status=Not Started` + `Priority=Medium` stamped at creation.
- **⬜ Open**: Entity custom-field option GIDs not yet supplied → entity tagging still OFF. Field GIDs when ready:
  - Entity field: `1214487026542596`
  - Status: `1214566926973275` / Not Started: `1214566926973276`
  - Priority: `1204547177535963` / Medium: `1204547177535965`

**Additional fixes same session:**
- `8381b6f` — **KB-signal guard repaired**: `_has_kb_signal` queried table `"chunks"` but the live KB table is `"knowledge_chunks"` → guard had NEVER fired since it shipped. Also switched recency column `ingested_at` → `date_modified` (full KB re-ingest resets `ingested_at` to today, making the window a no-op). Validated via dry-run: `skipped_signal` 0 → 65.
- `d2c6929` — **Test isolation**: autouse conftest fixture now resets HubSpot portal-guard global state + nudge-ledger path per test. Full suite is order-independent.
- `2020f91` — **Clover stripped from `inventory-thresholds.yaml`**: OSN/Clover item-level block removed; only f3e thresholds remain. **Closes open TOM follow-up "confirm OSN/Clover leg stripped from inventory-thresholds.yaml" ✅**
- `2500668` (concurrent session) — D-029 HubSpot runtime portal guard (see HubSpot entry above).
- `b2eada1` (concurrent session) — startup KB-vector prewarm perf.

**⏳ Verification pending**: next real Fireflies capture will confirm Fix 3 routing end-to-end (background watch armed).

**Pre-existing, fail-soft, not yet addressed:**
- `_has_kb_signal` does unindexed LIKE scan over ~222K KB chunks (FTS index would speed it up).
- Confirm `inventory-alerts` loader reads `inventory-thresholds.yaml` as UTF-8 (⚠️ emoji in file can crash a default cp1252 read on Windows).

---

### [F3E] Make.com Fighter Scenario Deployment + IG Account Issue -- 2026-06-05 (Cowork session)

**Google Sheet restructured: "MMA Lab x F3 Fighters Tracker"**
- Tabs are now monthly: "June 2026", "July 2026", etc. (not platform-named)
- Column layout: A=Fighter Name, B=Handle, C=Hard Post (date), D=Story 1 (date),
  E=Story 2 (date), F=All 3 Complete? (formula -- NEVER write to col F)
- Row limit: fighter rows stop at ~58. Rows 60-65 are formula summary rows.

**Make.com template scenarios updated (both already active):**
- 4769310 [F3E] Fighter Tag Tracker TEMPLATE -- watches for @f3energy caption tags
- 4769305 [F3E] Fighter Post Tracker TEMPLATE -- watches for #DrinkF3/#F3Energy hashtags
- Both: dynamic tab `{{formatDate(now; "MMMM YYYY")}}`, corrected column mapping,
  range limited to A1:Z58, Slack connection corrected to 4792065

**Fighter scenario deployment: 91/114 active, 23 pending, 25 invalid**

Active (66 scenarios, fighters 1-45 both Tag+Post + 1 Tag-only):
- All polls every 15 minutes, writing to current month's tab in the Google Sheet
- Slack alerts post to #f3-athletes (C0B6GT3117Y), tagging Alex (U0B3VGWJTMJ)

Pending (23 scenarios -- Make.com daily create limit hit ~11 PM UTC 2026-06-05):
- Abdul Kamara Post + fighters 47-57 Tag + Post
- Limit resets ~24h from first batch. Say "finish the last 12 fighters" next session.

Invalid (25 scenarios -- auto-stopped by Make.com, isinvalid: true, errors: 1):
Root cause: `WatchPublicUserMedia` uses Instagram Business Discovery API, which
ONLY works with Business or Creator accounts. Personal accounts return
"Invalid user id (110, OAuthException)".

Fighters with Personal IG accounts (need to switch to Business/Creator -- free, 2 min):
  dorathedesstoryerrr  -- Leslie Hernandez
  ericmcconicojr       -- Eric McConico
  livioriberiomma      -- Livio Riberio
  jennawilliam_cpt     -- Jenna Williams
  gavin_leath          -- Gavin Leath
  deku.mma             -- Miguel Francisco
  oli_marie_           -- Olivia Hendrickson
  shanechristie_       -- Shane Christie
  rileyhelt            -- Riley Helt
  el_zb                -- Zeke Breuninger
  besninxha            -- Besnik Ghashi
  josh_cruz13          -- Josh Cruz
  reborn2fight33       -- Abdul Kamara (Tag only -- Post not yet created)

Fix: Alex tells each fighter: "Switch IG to Creator or Business account (Settings >
Account type and tools). Takes 2 min, free, doesn't affect your profile."
Once switched, re-activate their scenarios.

**Meta (Instagram API) setup:**
- Chrome agent prompt ready: deployment/meta-setup-chrome-agent-prompt.md
- F3 Energy IG User ID 17841448560031091 confirmed in Make.com connection
- INSTAGRAM_F3E_ACCESS_TOKEN must be added to .env (see META_SETUP_GUIDE.md)
- Cora influencer scan: 7 AM + 7 PM daily (updated from every 2h)

---

### [INCIDENT] Meeting Action Capture infinite loop -- 2026-06-05 evening (commit 1d17912)

**Incident:** `Cora - Meeting Action Capture` (hourly task) stuck in infinite loop on an OSN
meeting transcript, 6pm–11pm AZ. Same transcript reprocessed every hour → 54 duplicate
Asana tasks (all assigned Matt Petrovich) + #osn-leadership flooded with identical posts.
All 54 duplicates bulk-deleted via Chrome Agent.

**Root cause:** `fireflies_action_extractor.py` watermark only advanced when
`latest_ts > since_ts`. This OSN transcript's `meeting_ts` exactly equaled the watermark
value, so `latest_ts` never exceeded `since_ts`, the watermark never updated, and the
transcript was reprocessed every hour forever.

**Fix (commit `1d17912`):**
- `_read_watermark()` now returns `(timestamp, processed_ids: set[str])` instead of just a timestamp.
- After processing any transcript, its Fireflies ID is added to `processed_ids`.
- `_write_watermark()` always persists both timestamp AND the ID set (no longer conditional).
- Processing loop skips any transcript whose Fireflies ID is already in `processed_ids`.
- Dedup is now ID-based, not timestamp-based.

**New watermark format** (`data/state/meeting_action_watermark.json`):
`{"last_processed_ts": 1780686300, "processed_ids": ["id1", "id2", ...]}`

**D-030 locked** (see ACTIVE DECISIONS below): ID-based dedup is required for all Fireflies
watermarks. Timestamp-only is insufficient because meeting_ts reflects the meeting DATE,
not ingestion date.

**Status:** Task re-enabled, running clean. Fix committed + pushed. Cora restarted ✅

---

### [INFRA] Clover retired + Phase 3 tool audit -- 2026-06-05 (commits 4231ae7, d6e2133)

Repo HEAD: `d6e2133` on `origin/main` | 3,132 tests | 45 active tools | 37 Ready tasks | 10 Disabled

**`4231ae7` — Clover fully retired from Cora (853 lines removed)**
- OSN is moving to QBO as sole financial source. Clover added noise and was never accurate enough.
- 3 tools removed: `osn_sales_pulse`, `osn_inventory_status`, `osn_customer_trends`
- Clover test file removed. OSN system prompts updated to use `qbo_get_profit_loss` + `financial_get_cashflow`.
- Make.com Clover scenarios deleted from the org.
- Decision permanent: do NOT rebuild Clover integration (D-027 locked).

**`d6e2133` — Phase 3 tool audit + per-tool timeouts**
- 2 dead tools removed: `financial_notify_gap` (internal side-effect, never user-visible) +
  `lex_staff_pulse` (stub that was never built)
- Per-tool timeout tiers added (replaces global 25s for everything):
  - 8s fast tier: tools that hit only local SQLite/cache
  - 15s default tier: single external API call
  - 25s heavy tier: multi-step, image generation, meeting parsing
- Health check `_EXPECTED_DISABLED` updated with all 9 intentionally-disabled tasks
  (health check now runs clean, 0 false CRITICAL alerts)
- Tool count: 50 → 45 active
- D-028 + D-029 locked (see ACTIVE DECISIONS)

**Asana cleanup (same session)**
- 6 broken "Slack Feed - Task Completed" rules deleted across projects:
  HJRG Q1 Goals, F3E Weekly Meeting, OSN Recon Pilot, UFL Sponsor Pipeline, F3E 2026 Planning + 1 more.
  Leftover dead rules from the original Asana-Slack integration. 14 of 20 projects already clean.

**Make.com: 9th HJR scenario now active. Slack connection corrected.**
- All 9 HJR scenarios active (see KEY IDS for full list)
- Old wrong connection (ID 4791943, f3-energy.slack.com) deleted from Make.com
- **CORRECTION from prior TOM entry**: correct Slack connection is ID `4792065` (NOT `4791951`)
  Both point to hjr-global.slack.com + U0B2RM2JYJ1, but `4792065` is the current active Bot token.

**System health snapshot (2026-06-05 22:00 UTC):**
- Cora: Running, heartbeat every 60s ✅
- Tasks: 37 Ready / 10 Disabled (intentional) / 1 Running ✅
- KB: 218,922 chunks (grew from 159K) ✅
- QBO: 11 entities, 100 days remaining ✅
- Make.com: 9 HJR scenarios active, 0 errors ✅
- Tools: 45 active ✅
- Tests: 3,132 passed, 0 failed ✅

---

### [MAKE.COM] Phase 2 migration — 8 mechanical tasks moved out of Cora -- 2026-06-04/05

8 Make.com scenarios built covering all mechanical automation that doesn't require Cora's
intelligence. 7 Cora scheduled tasks disabled; task count: 47 → 40 active (**50 active tasks confirmed 2026-06-06** -- Tier 3 registrations complete + cross-entity firewall added).

| Make.com ID | Scenario | Cora task → status |
|---|---|---|
| 4768886 | Deal Task Sync — Proposal → Asana | Cora - Deal Task Sync → DISABLED |
| 4768887 | Asana Hygiene Nudges | Cora - Asana Hygiene Nudges → DISABLED |
| 4769070 | Clover Daily Store Summary → #osn-leadership | Cora - Clover Daily Summary → DISABLED |
| 4769072 | OSN Weekly Metrics → Matt DM | Cora - OSN Metrics Digest → DISABLED |
| 4769073 | Slack Channel Health Monitor | Cora - Channel Health Monitor → DISABLED |
| 4769075 | HubSpot Deal Stage Monitor | Cora - HubSpot Deal Monitor → DISABLED |
| 4769088 | Shopify Inventory Alerts → #f3e-leadership | (new — no prior Cora task) |
| 4769089 | Shopify DTC Daily Summary → #f3e-leadership | Cora - Shopify DTC Summary → ⚠️ STILL NEEDS DISABLE |

**⚠️ Pending action:** `schtasks /Change /TN "Cora - Shopify DTC Summary" /Disable` from elevated PS.
All 8 scenarios are currently INACTIVE — Chrome Agent activation run is next (test with
"Run once" per scenario, then activate toggle).

Make.com connections confirmed (corrects prior research session — old Slack connection was wrong workspace):
  Slack:    ID 4791951 (hjr-global.slack.com, Harrison U0B2RM2JYJ1 ✓)
  Shopify:  ID 4791971 (F3 Energy, OAuth, f3energy.com)
  HubSpot:  ID 4784191
  Asana:    ID 3829949
  OLD (delete): ID 4791943 (f3-energy.slack.com, wrong workspace)

Infrastructure: Data structure 281656 + data store 86993 (HJR Deal Stage Tracker) for
HubSpot deal-stage change detection.

Shopify OAuth note: Standard Make.com Shopify connection uses OAuth (subdomain only). The
shpat_ access token is for the Custom App connection type, not needed for the OAuth flow.

---

### [INFRA] Phase 1 performance -- 2026-06-04 (commit e922e74)

Three fixes that cut Cora's response time ~50%:

1. **Triple embedding eliminated**: Query was being embedded 3x per request (cache lookup +
   context_loader + store.search). Pre-computed once in app.py and passed through.
   Saves 200–400ms + 2 redundant OpenAI API calls per mention.

2. **Channel name cache**: `_resolve_channel_name()` was hitting Slack API on every mention.
   Added 30-min TTL cache (`_CHANNEL_NAME_CACHE`). Saves 300–500ms per mention after first hit.

3. **Context pre-warm at startup**: All 14 entity CLAUDE.md files now loaded into the 5-min
   TTL cache at boot via a background daemon thread. Eliminates cold-cache penalty on first
   request per entity (up to 2s saved on first post-restart mention per entity).

---

### [INFRA] Cashflow blank-pulse fix -- 2026-06-04 (commit bef32d5)

**Problem:** 3:30 PM Cross-Entity Cash Pulse DM showed `--` / `??` for all 9 entities yet
reported "9 entities fetched, 0 unavailable, 0 flagged." Looked like a success; was silent failure.

**Root cause:** `gsheets_financials.py` finds balance rows by case-insensitive substring match
against fixed label frozensets. Code matched generic labels like `"ending balance"` and
`"beginning balance"`, but the Standing ACTUALS tabs actually label them:
  - Opening: `BEGINNING Cash/CC - Book Balance`
  - Closing:  `Ending Cash/CC Book Balance`
Neither is a substring of the old patterns → both returned `None`. Week label + entity rows
parse independently so the fetch appeared successful while values silently came back blank.

**Fix:** Added real labels to the frozensets. Closing match is `ending cash/cc book balance`
(no dash) so it skips the decoy row `Total Liquidity - ENDING Cash/CC - Book Balance-S/B ZERO`
(value 0, appears later in the tab). +3 regression tests including decoy guard.

**Verified live (all 9 entities returning real balances):**
Portfolio $1,347,657 · OSN $77,629 · LEX $99,807 · HJRP $35,088 · HJRG $12,007 ·
BDM $2,507 · UFL $2,244 · F3E $1,680 · HJR Productions $0

**Current state:** main @ `bef32d5` | 3,169 tests passing | service restarted + corrected pulse re-sent

**Doctrine:** Sheet row-label renames silently break `gsheets_financials` (returns None, not error).
When cash values go blank portfolio-wide, dump the tab's column-0 labels and compare to frozensets.
Canonical labels locked as of 2026-06-04 (see memory/decisions.md entry).
Justin/Hayden: renaming rows in Standing ACTUALS can break the connector — flag row-label changes.

---

### [RESEARCH] HubSpot + Asana + Make.com ground-truth audit -- 2026-06-04

Chrome Agent + direct API research confirmed several facts that differ from prior assumptions.
Full entries in KEY IDS section below. Critical corrections for code:

**HubSpot corrections:**
- UFL/OSN/BDM pipeline ID = string `"default"` (already correct in code -- confirm no numeric ref exists)
- HubSpot Workflows NOT available on Sales Hub Starter. Make.com is the only deal automation layer.
- Matt Petrovich (owner 83346026) is DEACTIVATED. Do not assign new deals; valid for historical queries.
- Active deal entity property = `f3_entity` (95 uses). Legacy `"entity"` field has 0 uses -- ignore entirely.
- F3E Proposal stage: 22 active deals, ~$399,740. (Signal: pipeline is healthy, not stale.)
- Hannah Grant owner ID: 165179973 (invite pending -- add to slack-to-hubspot.yaml when she accepts).

**Asana corrections:**
- LTS team does NOT exist as a separate Asana team. Prior notes were wrong.
  Lex sub-teams: LLC (1209152915815732) / LLA (1209152923740446) / LBHS (1209152923740451).
- Entity custom field internal name: `f3_entity` (22 options). New options vs. prior:
  LEX-DDS, FF, HJR-PB, CHK, CHB.
- "Slack Feed - Task Completed" rule is broken/paused workspace-wide. Cleanup needed (low priority).
- Total workspace users: 69.
- Harrison: 100+ overdue tasks (oldest Jan 2025). Asana hygiene nudge feature is validated.
- Full Asana team + project GID table now in KEY IDS section (locked).

**Make.com warning:**
- Slack connection user harrison205 (U06H8N2TTEC) does NOT match Cora's Harrison ID (U0B2RM2JYJ1).
  Verify correct workspace before wiring any Make.com → Slack scenarios.
- 3 of 8 new HJR scenarios are active (F3E Instagram only); 8 new scenarios inactive pending activation.
- Make.com + Cora = the ONLY automation layers across the entire HJR portfolio.

---

### [INFRA] QBO financial routing fixed -- 2026-06-04 (commits 7f4e243, 5929878, c34e852)

**Root cause:** All 14 entity system prompts were routing ALL financial questions to
`financial_get_cashflow` (Google Sheets weekly forecast), which cannot answer P&L/revenue
questions. Shaun asked "Q1 LLC revenue" in #lex-finance and got UNKNOWN_RESPONSE.
QBO tokens were valid the entire time (100 days remaining) -- the prompts just never
told Claude to use them. Secondary bug: `financial_get_close_pack` had "Q1" in its
trigger examples, causing it to match before QBO could be tried.

**What was fixed (all three commits, now live):**
- `7f4e243` 14 entity system prompts updated with QBO-first financial routing table:
  - P&L / revenue / expenses / quarterly  --> `qbo_get_profit_loss`
  - Balance sheet                          --> `qbo_get_balance_sheet`
  - AR aging                               --> `qbo_get_ar_aging`
  - AP aging                               --> `qbo_get_ap_aging`
  - Transactions                           --> `qbo_get_recent_transactions`
  - Weekly cash flow forecast / close packs --> gsheets (supplemental only)
- `5929878` Tool descriptions patched:
  - `qbo_get_profit_loss` now explicitly marked "PRIMARY TOOL FOR ALL REVENUE, P&L,
    AND INCOME QUESTIONS" with Q1/quarterly examples + pre-resolved date ranges
    (Q1 = '2026-01-01 to 2026-03-31')
  - `financial_get_close_pack` now marked "fallback only" for archived Drive report
    files; explicitly excludes Q1/quarterly queries
- `c34e852` D-026 locked (see ACTIVE DECISIONS below)

**Current state:** main @ `c34e852` | 3,162 tests passing | 11 QBO entities provisioned

**Provisioned QBO entities (all valid, 100 days remaining):**
BDM, F3E, HJRG, HJRP, HRLLC, LEX, OSN, OSNGF, OSNGM, OSNGW, OSNVV

**Needs live smoke test (DO FIRST in next session):**
Send `@Cora what was Q1 LLC revenue?` in `#lex-finance` -- should call
`qbo_get_profit_loss` with `period: "2026-01-01 to 2026-03-31"` and return real
QBO P&L data, not UNKNOWN_RESPONSE.

---

### [INFRA+HJRP] Cora bug-fix + hjrp_lease_status batch -- 2026-06-04 (commits 286d9f0, cedfe49, 02717a3)

Repo HEAD: `e922e74` on `origin/main` | 3,166 tests passing | Cora restarted, stable.

Note: concurrent session also landed `7f4e243` + `5929878` + `c34e852` (QBO-first routing, see TOM above)
and `e922e74` (perf: Phase 1 embedding/context prewarm) on the shared desktop. Both are on main.

**`286d9f0` fix(health-check): `nightly_health_check.py` stale expected-state**
- `_EXPECTED_DISABLED` was listing tasks that are no longer disabled → false CRITICAL
  alerts firing every night on 4 tasks.
- Fixed: now expects `asana-email-sync` / `hubspot-email-sync` / `proactive-gaps`
  Disabled and `qbo-token-refresh` Ready (re-enabled 2026-06-04, see TOM above).
- Dry-run result: 0 CRITICAL after fix.

**`cedfe49` fix(decision-capture): Haiku verification gate kills digest noise**
- Root problem: 7am knowledge-review digest was full of backchannel chatter and
  near-duplicate non-decisions (11 raw → 2 → 0 after fix).
- Stage 1 hardened heuristic: strip `[Speaker]` prefix, 5-word floor, down-weight
  `we will`/`confirmed` to weight 1, normalized-fingerprint dedup.
- Stage 2 new Claude Haiku gate (`claude-haiku-4-5`): keeps only real decisions with
  clean summaries; robust JSON extraction; fail-open; records rejects.
- System prompt updated with guidance.

**`02717a3` feat(hjrp): `hjrp_lease_status` tool shipped**
- TIER_1-gated: `#hjrp-finance` + `#hjrp-leadership` only. Refuses in all other channels.
- Reads `data/maps/hjrp-leases.yaml` (sourced from hjrp.md tenant tables -- update both
  together when lease state changes).
- Returns: renewal countdown per lease, the Oct 2026 cluster (4 leases expiring 10/31:
  HJR Global + LLC Admin + LLC-DTA + LLC-DTT = ~$23,835/mo at risk), upcoming vacancy
  (Vine & Branches 6/30 → 7/1 vacant), and broker contacts.
- 36 tests.

**Smoke test needed:**
- `#hjrp-leadership` → `@Cora what's our lease renewal status?`
  Expect: Oct 2026 cluster surfaced + Vine & Branches vacancy + broker contacts.
- A TIER_3 HJRP channel → same question should refuse (TIER_1 gate).

---

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
| cowork-cora-influencer-scan | 7 AM + 7 PM daily | scripts/run_influencer_scan.py |
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

Deliverable credit rule (D-025):
- A post counts ONLY if the fighter tags @f3energy in the photo/video (media tag)
- Hashtag-only posts (#DrinkF3 etc.) do NOT qualify -- cannot auto-attribute poster
- Tagged-media scan auto-proposes matches; hashtag scan logs for awareness only
- Fighter contracts + Alex's onboarding language should reflect this requirement

Deferred:
- Recurring deliverable templates (auto-generate next month)
- TikTok monitoring (scaffold in tiktok_monitor.py, pending API approval)
- Bulk HubSpot->influencer import
- Update #f3-sales detection message to also use 👍/👎 pattern
- Update scanner: suppress auto-match proposals for hashtag-only detections (D-025)

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

HubSpot (portal 246351746)
  F3E Retail pipeline:      2313722582
  UFL/OSN/BDM pipeline:     "default"  <- string literal, NOT numeric
  Deal entity property:     f3_entity  <- 95 uses; legacy "entity" field = 0 uses, IGNORE
  Matt Petrovich owner ID:  83346026   <- DEACTIVATED; valid for historical only, no new assignments
  Hannah Grant owner ID:    165179973  <- pending invite (as of 2026-06-04)
  NOTE: Workflows NOT available on Sales Hub Starter. Make.com is the sole deal automation layer.

Asana (workspace 682743441507584)
  Teams:
    HJRG:      1211723492575901
    F3E:       1209079638382203
    OSN:       1209426556623911
    UFL:       1209152923740455
    BDM:       1211265649994430
    HJRP:      1209152923740487
    HJRPROD:   1209152923740471
    LLC Team:  1209152915815732   <- Lex sub-team (LTS team does NOT exist -- see note below)
    LLA Team:  1209152923740446
    LBHS Team: 1209152923740451
  NOTE: There is NO LTS Asana team. Lex sub-teams are LLC/LLA/LBHS only.
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
   After Start-ScheduledTask + sleep, verify single instance. **Healthy shape
   (verified live 2026-06-11, all 3 created the same second):** `cora.exe`
   launcher -> `.venv\Scripts\python.exe` (venv REDIRECTOR, not the bot) -> base
   `Python312\python.exe` (the actual bot). So the query returns 3 rows for ONE
   instance: 1 cora.exe + 2 python.exe. More than that = stacked; confirm via the
   log (a single "Cora starting up" + one monotonic heartbeat sequence). FYI the
   service task action is `uv.exe run cora` (pre-D-005 legacy; flagged 2026-06-11,
   unchanged -- migrating it to .venv\Scripts\python.exe -m needs a Harrison call).

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
| D-028 | Per-tool timeout tiers: 8s fast (local DB), 15s default (single API), 25s heavy (multi-step/image/meeting). |
| D-029 | Cora = intelligence + conversation. Make.com = mechanical automation. Rule-based, threshold-based, or straight data-push tasks belong in Make.com. Cora only where natural language or context is needed. |
| D-030 | Meeting Action Capture watermark must track transcript IDs, not just timestamps. Timestamp-only watermarks fail when meeting_ts equals the watermark value. ID-set dedup is required. Watermark format: `{"last_processed_ts": N, "processed_ids": [...]}`. |
