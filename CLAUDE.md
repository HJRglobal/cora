# Cora — Code Session Context

This file is the authoritative startup read for every Code session.
Read this first, then check `decisions.md` for the full decision log.
TOM entries are newest-first. Do not edit past TOM entries.

---

## TOP OF MIND (TOM)

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
