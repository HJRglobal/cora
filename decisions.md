# Cora — Architecture Decision Log

Decisions recorded here are permanent unless explicitly superseded. Each entry
captures the choice made, the alternative considered, and the reason. Append
new entries at the bottom; do not edit past entries.

---

## D-001 · Apollo.io over direct LinkedIn scraping (2026-05-28)

**Context:** F3 needed a way to scan LinkedIn weekly for retail buyers and
executives to build an outreach list for Tommy Anderson.

**Decision:** Use Apollo.io People Search API as the data source rather than
scraping LinkedIn directly.

**Alternatives considered:**
- Direct LinkedIn scraping (Selenium / requests) — rejected: violates LinkedIn
  ToS; hiQ v. LinkedIn litigation creates legal exposure.
- LinkedIn Sales Navigator API — rejected: heavily partner-gated; no usable
  API for automation at this stage.
- PhantomBuster — rejected: gray-area ToS, unreliable, and adds a third-party
  dependency with its own outage risk.

**Rationale:** Apollo holds a licensed B2B database and provides a clean REST
API. Search calls are free (no credits consumed); credits only spent on
email/phone reveals, which the scanner never triggers.

---

## D-002 · Apollo API: omit q_keywords from search payload (2026-05-28)

**Context:** Initial scanner config included `q_keywords: "retail grocery
natural foods energy drink functional beverage"` to bias results toward
relevant industries.

**Decision:** `q_keywords` is intentionally excluded from the Apollo API
payload. The field is documented in code with an explanation.

**Why:** Live testing on the Professional trial confirmed that combining
`q_keywords` with `person_titles` and `person_locations` returns 0 results.
Apollo phrase-matches `q_keywords` against profile bio content, not as a
relevance signal — it conflicts with the structured filters. Title targeting
alone (22 buyer/category-manager titles) plus the channel_fit YAML rules in
`data/maps/linkedin-spy-search-config.yaml` provide sufficient narrowing.

**Test result (2026-05-27):**
- titles + location only → 114,970 results ✅
- titles + location + q_keywords → 0 results ❌

---

## D-003 · Apollo endpoint: mixed_people/api_search (2026-05-28)

**Context:** Apollo deprecated `POST /v1/mixed_people/search`.

**Decision:** Use `POST https://api.apollo.io/v1/mixed_people/api_search`.

**Notes:**
- Auth: `X-Api-Key` header (not a body param).
- Response: `total_entries` is at the top level of the response dict, not
  nested inside a `pagination` object.
- Names (`first_name`, `last_name`) return `null` until a credit reveal is
  triggered. `title`, `organization.name`, and `linkedin_url` are always
  returned without credits.

---

## D-004 · LinkedIn spy: never trigger credit reveals (2026-05-28)

**Decision:** The weekly scanner surfaces title + company + LinkedIn URL only.
It never calls any Apollo endpoint that triggers a credit reveal (email,
phone). Tommy decides manually which prospects are worth revealing.

**Rationale:** 245 credits on the Professional trial; search calls are free
at 600/day. Burning credits automatically on every scan is wasteful and would
exhaust the trial budget within weeks. The weekly report is valuable without
names — buyers can be identified by title + company + LinkedIn URL.

**Review trigger:** Revisit if/when F3 upgrades to a paid Apollo plan with
higher monthly credit allocation and a clear policy for which prospects qualify
for reveal.

---

## D-005 · Task Scheduler: use .venv python, not uv (2026-05-28)

**Context:** Influencer scan (setup-influencer-scan-task.ps1) runs via `uv run
python`. LinkedIn spy Task Scheduler job was created using the full venv path.

**Decision:** All new scheduled tasks registered after 2026-05-27 use
`.venv\Scripts\python.exe` with a full absolute path, not `uv`.

**Rationale:** Windows Task Scheduler launches processes with a minimal
environment — the user's PATH is not inherited. `uv` is only on PATH in
interactive shells. Full venv path is reliable in both contexts.

**Note:** The influencer scan task (older pattern) still uses `uv`; it has
not been migrated because it works in practice on Harrison's machine where uv
is in the system PATH via its installer. New tasks should use the venv path.

---

## D-006 · LinkedIn spy Slack channel: #f3e-sales (2026-05-28)

**Decision:** Weekly LinkedIn prospect reports post to `#f3e-sales`.

**Background:** `#f3-sales` does not exist in the HJR Slack workspace. The
bot confirmed its membership in `#f3e-sales` during first-run testing on
2026-05-27. Config default and .env value both corrected.

---

## D-007 · LinkedIn spy schedule: Monday 8:00 AM (2026-05-28)

**Decision:** "Cora - LinkedIn Spy" Task Scheduler task fires every Monday at
08:00 local time on Harrison's machine.

**Rationale:** Monday morning delivery gives Tommy a fresh prospect queue at
the start of each work week before the day's outreach begins. The 8 AM time
was set during live testing and confirmed working (last result: 0, clean exit).

---

## D-008 · HubSpot migration: old portal 243870963 retired, new portal 246351746 (2026-05-30)

**Context:** Old HubSpot portal (243870963, free tier) hit limits and had
incorrect pipeline structure. Migrated to new Sales Hub Starter portal.

**Decision:** New canonical HubSpot portal ID is **246351746**. All code,
config, and URLs reference this ID. Old portal is being cancelled.

**Pipeline structure in new portal:**
- `PIPELINE_F3E_RETAIL` = `"2313722582"` — F3E retail deals
- `PIPELINE_UFL_OSN_BDM` = `"default"` — combined UFL / OSN / BDM pipeline
  (replaces the old separate "UFL Sponsorships" pipeline)

**Custom deal properties created in new portal:**
- `f3e_channel`, `f3e_geography`, `f3e_chain_type`, `f3e_distributor`

**Migration outcome:** 27 deals + 9 notes imported. Old portal data archived
in `data/hubspot-migration/`.

---

## D-009 · HubSpot combined UFL/OSN/BDM pipeline (2026-05-30)

**Context:** Old portal had a separate "UFL Sponsorships" pipeline. New portal
uses a single combined pipeline for UFL, OSN, and BDM deals.

**Decision:** All three entity types route to `PIPELINE_UFL_OSN_BDM = "default"`.
The `entity` custom property on each deal distinguishes UFL vs OSN vs BDM
within the pipeline.

**Alternatives considered:** Three separate pipelines — rejected because
HubSpot Starter limits total pipeline count and the deal volumes don't justify
the overhead.

---

## D-010 · Knowledge-review: one DM per proposed update, not a batch (2026-05-31)

**Context:** Original implementation sent all PENDING updates as a single
batched Slack DM. One 👍 on that message approved everything at once.

**Decision:** Each proposed update gets its own DM with its own 👍/👎.
Harrison approves or dismisses items individually. The `dm_message_ts` on each
entry correlates its reaction back to the specific update.

**Implementation:** `send_individual_dms()` in `knowledge_review.py`; 0.5s
delay between messages to stay within Slack rate limits.

---

## D-011 · Harrison-sole-authority doctrine for all memory writes (LOCKED 2026-05-21)

**Decision:** Cora **never** auto-writes to `decisions.md`, Asana, or HubSpot
without an explicit Harrison 👍 reaction on a knowledge-review DM.

**Locked:** This doctrine is non-negotiable and must not be relaxed without an
explicit new decision entry superseding this one.

**Enforcement points in code:**
- `_process_contribution_reaction` in `app.py`: checks `reactor_id == _FOUNDER_ID`
- `correlate_reactions_to_updates` in `knowledge_review.py`: only processes
  reactions where `reactor_id == HARRISON_SLACK_USER_ID`
- `run_knowledge_review.py`: prints `APPROVED:` lines to stdout for downstream
  executors; nothing executes without that signal

---

## D-012 · PHI guard centralized to phi_guard.py (2026-05-31)

**Context:** `drive_sweep.py` and `reconciliation_engine.py` both defined their
own `_PHI_PATTERNS` / `_PHI_RE` regex with overlapping but non-identical patterns.

**Decision:** Single source of truth at `src/cora/phi_guard.py`. Both modules
import `_PHI_PATTERNS` from there. The union of all patterns (drive_sweep +
reconciliation_engine + additions: patient, medicaid, ahcccs, npi, ssn) is the
canonical set.

---

## D-013 · DOCUMENT_QUERY intent to prevent invoice/file queries from hitting financial tool (2026-05-31)

**Context:** "Find the shipping invoice" was being classified as FINANCIAL and
routed to the financial tool instead of KB search, returning wrong results.

**Decision:** New `Intent.DOCUMENT_QUERY` in `intent_classifier.py` with k=15
and 15-minute TTL. Checked before SIMPLE in the classification chain.
COMPLEX k bumped from default-8 to 12.

---

## D-014 · Tool dispatch 25s hard timeout (2026-05-31)

**Decision:** Every tool call in `tool_dispatch.dispatch()` is wrapped in a
`ThreadPoolExecutor` with `future.result(timeout=25)`. Timed-out tools return
`"Tool timed out — please try again."` instead of hanging indefinitely.

**Rationale:** Slack's 3-second ack deadline means Cora's main loop must
remain responsive. Long-running tool calls (slow HubSpot API, Drive timeouts)
were silently blocking the event loop.

---

## D-015 · Rate limiter persisted to SQLite across restarts (2026-05-31)

**Context:** Original rate limiter used in-memory deques with `time.monotonic()`.
All rate limit windows reset on every process restart, allowing bypass.

**Decision:** `rate_limiter.py` persists hits to `data/rate_limiter.db`
(SQLite) using wall-clock `time.time()`. In-memory deque is the fast path;
SQLite is written on every allowed request. Falls back to in-memory-only if
the DB file cannot be opened.

---

## D-016 · PS1 files must use ASCII-only characters (2026-05-31)

**Context:** `setup-channel-sweep-task.ps1` originally used em dashes (U+2014)
in `Write-Host` strings. PowerShell 5.1 reads `.ps1` files as Windows-1252 by
default; U+2014 (UTF-8: E2 80 94) is misread as byte 0x94, which is a closing
double-quote in Windows-1252, causing string parse errors.

**Decision:** All `.ps1` files in this repo use ASCII-only characters. No em
dashes, curly quotes, box-drawing characters, or any codepoint > 127.

---

## D-017 · Org-wide channel sweep: Cora joins all public channels (2026-05-31)

**Decision:** Cora is a member of all 51+ public Slack channels (bootstrapped
via `scripts/bootstrap_channel_membership.py`). A nightly sweep
(`scripts/run_channel_sweep.py`, 01:30 AZ via `cowork-cora-channel-sweep` task)
scans recent messages for commitments, decisions, and cross-entity mentions.
New public channels are auto-joined via the `channel_created` Slack event.

**Excluded from sweep:** #general, #random, #announcements, #cora-build.

**Output:** `data/channel-sweep/sweep-YYYY-MM-DD.json` — per-user synthesis
written by Haiku; feeds Pass 6 of reconciliation.


---

## D-018 · Pass 4 semantic matching upgrade (2026-05-31)

**Context:** reconciliation_engine.py Pass 4 was using `SequenceMatcher.ratio()`
(threshold 0.35) to match completion-language sentences against open Asana task
names. Miss rate ~85% because natural language ("shipped samples to ADF") shares
almost no characters with Asana task titles ("[F3E] Tommy -- ADF sampling kit").

**Decisions:**
1. Fireflies added as a source alongside Slack + Gmail (meetings are highest-signal).
2. Semantic embedding (text-embedding-3-small, cosine_sim >= 0.72) replaces fuzzy
   string matching. Falls back to fuzzy if OPENAI_API_KEY is absent.
3. Task name prefixes ([ENTITY], "Name --") stripped via `_normalize_task_name()`
   before matching to avoid dilution.
4. New helpers: `_cosine_sim()`, `_embed_task_names()`, `_embed_sentence()`,
   `_semantic_best_match()`, `_confidence_from_sim()`.
5. `FIREFLIES_LOOKBACK_SECONDS = 48 * 3600` (separate from 25h default) so
   yesterday's meetings are always included even when reconciliation runs same-day.

---

## D-019 · Reconciliation all-user coverage fixes (2026-05-31)

**Context:** 5 bugs found that silently skipped users or blocked good matches.

**Decisions (all implemented, committed 407fd03 + 4ada0d2):**
1. `max_tasks` 50 → 200 per user (Harrison, Larry, Jake, Alex were hitting cap).
2. `MAX_GAPS_PER_PASS` 10 → 30 (with 327 tasks across 16 users, later-processed
   users were receiving 0 gaps per nightly run).
3. `FIREFLIES_LOOKBACK_SECONDS` separate from DEFAULT (see D-018).
4. Harrison Rogers included in stale-task DMs — all 16 users now receive.
5. `seen_tasks` set replaced with `best_per_task` dict — best-score-wins per task
   so a strong Fireflies signal can supersede a weak earlier Slack match.

**Validation:** Manual run 2026-05-31 20:31: 15 gaps proposed, 6 users DM'd
(Jake, Hannah, Larry, Harrison, Matt, Micah).

---

## D-020 · Reliability items — pending_confirm, KB checkpoint, Notion multi-DB, orphan-kill (2026-05-31)

**Decisions (committed 32b06a1):**
1. `team_learning.py`: `store/get/clear_pending_confirm()`, `kq_channel_for_entity()`,
   `paraphrase_note()`, `is_confirmation()` — all were called from app.py but
   undefined (live AttributeError on every paraphrase-confirm attempt). Now
   SQLite-backed with 24h TTL. `pending_paraphrase_confirms` table in `cora_kb.db`.
2. `checkpoint_state` table + `get/set/delete_checkpoint()` on `KnowledgeBase`.
   Drive sweep saves per-user page token after each page; resumes mid-user on
   restart rather than re-scanning from scratch.
3. Notion connector: `NOTION_EXTRA_DB_IDS` env var (comma-separated DB IDs).
   Extra DBs use generic page→text extraction. Only Contracts DB uses full schema.
4. All 15 setup scripts: `Stop-ScheduledTask` added before `Unregister-ScheduledTask`
   so any running instance is killed before re-registration.

---

## D-021 · Conftest env var doctrine (2026-05-31)

**Context:** Cowork sandbox pre-sets required env vars to empty string `""`.
`os.environ.setdefault()` does NOT overwrite empty strings (key already exists).
This caused `cora.config._load()` to raise "ANTHROPIC_API_KEY: missing" during
test collection, silently breaking ~30 tests per session.

**Decision LOCKED:** In `tests/conftest.py`, always use:
```python
os.environ["KEY"] = os.environ.get("KEY") or "fallback-test-value"
```
Never `os.environ.setdefault("KEY", "fallback")` for required config vars in CI/sandbox.

**Secondary doctrine:** Also pre-import `cora.config` at conftest module-load time
(before `pytest_configure`) to prevent `test_f3e_inventory_location.py`'s fake
`_Config` injection from corrupting the module cache for later tests.

---

## D-022 · Smart quote / encoding doctrine for PowerShell + Python source files (2026-05-31)

**Context:** `Add-Content` in PowerShell 5.1 converts `"` to smart quotes
(U+201C/U+201D) in appended content. Python source files with smart quote
string delimiters fail to parse with `SyntaxError: unterminated string literal`.

**Decision LOCKED:** Never use `Add-Content` to append Python source code.
Use `Write` (Cowork tool) or `Edit` (targeted Edit tool). If a file gets smart
quotes, fix with binary byte replacement:
```python
raw = raw.replace(b"\xe2\x80\x9c", b'"').replace(b"\xe2\x80\x9d", b'"')
```
Companion to D-016 (PS1 ASCII-only).

---

## D-023 · influencer_complete_deliverable: no staged-write gate (2026-06-03)

**Context:** All other Cora write tools require a preview block + confirmed=True
before executing. The new influencer_complete_deliverable tool marks a deliverable
done when Alex types "@Cora complete deliverable [ID]".

**Decision:** No confirm gate on this tool. Alex's explicit typed command (or a
thumbs-up reaction on a detection proposal) is unambiguous intent -- the extra
round-trip adds friction with no safety benefit for a one-shot close-out action.

**Contrast with other writes:** asana_create_task / gmail_create_draft / etc. have
ambiguous parameters (title, assignee, dates) where a preview catches mistakes.
"Complete #5" has no parameters to misread.

---

## D-024 · Influencer monthly deliverables: auto-generated 1st of month (2026-06-03)

**Context:** 57 F3 sponsored fighters each owe 3 deliverables/month (2 IG stories +
1 IG post, tagging @f3energy + #DrinkF3, due last day of month).

**Decision:** cowork-cora-monthly-deliverables Task Scheduler task fires at 9 AM
AZ on the 1st of every month, running scripts/generate_monthly_deliverables.py.
Creates 3 rows per active fighter in influencer_deliverables (campaign_month +
requirements columns added in schema migration). Posts confirmation to #f3-athletes.

**Source of truth:** SQLite (influencer_tracker.db) only. No HubSpot involvement.
Fighter roster seeded 2026-06-03 from Google Sheet
1oFmiSVbPMLOMdpjsUBOG_SGp00a9xzTrUCVNuyb0_kA via scripts/seed_fighters.py
(idempotent -- safe to re-run).

**Fighters excluded from roster (not seeded):**
- Malik Besseck -- no IG on file
- Jovan Ravago -- TikTok only (not yet monitored)
- Louie Lopez / Taquel Young -- dates in handle column, not handles
- Gym accounts (Betweenrounds, MMAelite, MMALAB) -- not individual fighters

---

## D-025 · Deliverable credit requires @f3energy tag, not hashtag alone (2026-06-03)

**Decision:** A fighter's Instagram post only counts as a completed deliverable
if they directly tag @f3energy in the photo or video (a media tag). Hashtag-only
posts (#DrinkF3, #F3Energy, etc.) do NOT qualify for credit.

**Rationale:** Media tags are verifiable by the Graph API -- the /{ig-user-id}/tags
endpoint returns only posts where the brand was tagged in the media object, and
it returns the poster's username so Cora can auto-identify the fighter. Hashtag
search does not return the poster's username (Meta privacy restriction), so credit
cannot be attributed automatically. Requiring a tag keeps the system auditable and
removes ambiguity about who posted.

**Operational impact:**
- Scanner auto-match proposals are only generated for tagged-media detections,
  not hashtag-only detections. Hashtag hits are logged to detection_log for
  reference but do not trigger a match proposal to Alex.
- Fighter contracts and onboarding language should reflect this: "must tag
  @f3energy in the photo or video to receive credit toward monthly deliverables."
- Cora's system prompt for #f3-athletes should communicate this rule to Alex
  when she surfaces deliverable status or detections.

**What still uses hashtags:** The hashtag scan (#DrinkF3, #F3Energy, #DrinkF3Energy)
stays active as a monitoring signal -- useful for brand awareness tracking and
catching posts Cora can flag for Alex to manually review -- but it does not
auto-complete deliverables.


---

## D-026 · QBO is the primary financial data source for all accounting questions (2026-06-04)

**Context:** Cora was returning UNKNOWN_RESPONSE to "Q1 LLC revenue" in #lex-finance.
Root cause: all entity system prompts had a single MANDATORY directive routing ALL financial
questions to `financial_get_cashflow` (Google Sheets weekly cash flow). That tool reads the
13-week rolling forecast spreadsheet -- it cannot answer P&L or revenue questions. QBO tokens
were fully valid (all 11 entities, 100 days remaining) but the prompts never directed Claude
to use QBO tools.

Secondary cause: `financial_get_close_pack` tool description included "balance sheet for Q1"
as a trigger example, causing Claude to match "Q1 LLC revenue" to the wrong tool. And
`qbo_get_profit_loss` had no quarterly examples and no signal that it handles date ranges.

**Decisions LOCKED:**

1. **QBO is the primary source for all accounting questions** -- P&L, revenue, income,
   expenses, quarterly results, balance sheet, AR aging, AP aging, recent transactions.
   `qbo_get_profit_loss` is called FIRST. QBO tokens refresh daily (task: cowork-cora-qbo-token-refresh, 2am AZ).

2. **Google Sheets is supplemental** -- for weekly cash flow forecast (13-week rolling),
   monthly close pack files Hayden/Justin file in Drive. Never the first call for P&L.

3. **Tool description hierarchy** -- `qbo_get_profit_loss` description now explicitly says
   "THIS IS THE PRIMARY TOOL FOR ALL REVENUE, P&L, AND INCOME QUESTIONS" and includes
   explicit Q1/quarterly examples with pre-resolved date ranges
   (Q1 = '2026-01-01 to 2026-03-31', etc.).

4. **`financial_get_close_pack` scoped to archived Drive files only** -- description now
   says "USE THIS ONLY AS A FALLBACK when qbo_get_profit_loss returns no data" and
   explicitly excludes Q1/quarterly queries.

5. **Financial routing table applied across all 14 entity prompts** (commit 7f4e243):
   lex.md, llc.md, lts.md, lbhs.md, lla.md, f3e.md, fndr.md, bdm.md,
   osn.md, osngf.md, osngm.md, osngw.md, osnvv.md, hjrp.md.

**Provisioned QBO entities (as of 2026-06-04):**
BDM, F3E, HJRG, HJRP, HRLLC, LEX, OSN, OSNGF, OSNGM, OSNGW, OSNVV
(11 entities, all refreshed today, 100 days remaining on tokens)


---

## D-027 · Make.com Apollo LinkedIn Spy scenario built (2026-06-05)

**What shipped:**
Make.com scenario ID `4769263` -- "[F3E] Apollo LinkedIn Spy -- HubSpot Leads + Tommy DM"
Runs weekly (every 7 days). Apollo key: `Dgjo_xURHLTiIonedhGPjw` (renewed June 2026, key stable).

**Pipeline:**
1. HTTP -> Apollo People Search API (25 retail buyers/run, US-based, 13 buyer titles)
2. BasicFeeder iterator -- one prospect at a time
3. Data Store dedup check (store ID 86999, structure ID 281670, "F3E Apollo Spy Dedup")
4. Filter: skip if already processed (exist = false passes through)
5. OpenAI GPT-3.5 -- brand fit score (Pure/Mood/Energy/All, 1-10) + 280-char LinkedIn note
6. JSON Parse AI response
7. HubSpot: Create or Update Contact (connection 4784191, portal 246351746)
8. HubSpot: Create Deal (F3E Retail pipeline 2313722582, Identify stage 3760235201, Tommy owner 162944825)
9. Data Store: Add Record -- marks LinkedIn URL as processed with contact + deal IDs
10. Array Aggregator -- collects all new leads this run
11. Slack DM to Tommy (U0B3RU5Q55G, connection 4791951, HJR Global workspace)

**Relationship to Python script:** Both the Python LinkedIn Spy (scripts/run_linkedin_spy.py,
cowork-cora-linkedin-spy Task Scheduler) and this Make scenario run the same Apollo query and
write to HubSpot. They use SEPARATE dedup stores (Python: SQLite data/linkedin_spy.db,
Make: data store 86999). Running both creates duplicate HubSpot contacts/deals. Disable one.
Harrison to decide -- can retire the Python task if Make ownership is preferred.

---

## D-028 · F3 Shopify Impulse theme build state locked (2026-06-04, via Cowork cascade)

**Theme:** Impulse ID 185801638208 -- UNPUBLISHED DRAFT on f3energy.myshopify.com
**Active live theme:** Reformation (ID 180110164288) -- DO NOT touch

**8 theme files written 2026-06-04:**
brand-routing-vars.liquid (Pure button #2D3436, font weight CSS vars),
font-face.liquid (Josefin Sans 100/300/600 added),
product.pure/mood/energy.json ($75 shipping),
header-group.json ($75 announcement bar),
footer-group.json (Shop/Company/Resources labels),
page.contact.json (F3 branded copy).

**4 decisions LOCKED:**
1. Pure H2 = Josefin Sans Light 300 (weight 200 does not exist in font)
2. Energy H2 = Josefin Sans Regular 400 (weight 500 does not exist in font)
3. Free shipping threshold = $75 everywhere (canonical)
4. Klaviyo popup SUBSCRIBE button = #2D3436 charcoal (not teal)
Josefin Sans ships 100/300/400/600/700 ONLY. No 200, no 500. Any prior spec referencing
those weights is superseded.

**BLOCKING PUBLISH -- Harrison uploads needed:**
- Favicon (32x32 + 180x180 + 192x192) via Shopify Admin > Themes > Impulse > Customize
- f3-logo-pure.png (1200x400 transparent PNG) via Shopify Admin > Content > Files
- f3-logo-mood.png (same spec)
- 8 hero photography slots (all 2880x1620px 16:9): homepage mood-hero, family-hero
  pure/mood/energy/family images, Pure + Mood + Energy collection heroes

**One manual Shopify step:** Rename nav menu "footer--company" -> "Company" in Admin.
**Klaviyo:** Update SUBSCRIBE button to #2D3436 in form builder (not a theme change).

**Verification gate before publish:**
Preview: https://f3energy.myshopify.com/?preview_theme_id=185801638208&brand=energy
DevTools: H1 = font-weight 100, H2 = font-weight 300, @font-face for 100/300/600 present.
If weights show as 400 despite CSS vars, font_modify returned nil -- escalate before publishing.

**11 draft pages to review** before publishing (ingredients-pure/mood/energy, 4 LPs,
about/faq/shipping-returns/contact-v2 -- each replaces an existing page).

**Canonical files:**
- Inventory: 02-F3-Energy/_shared/f3-website-brand-config-inventory-CANONICAL-2026-06-04.md
- Punch list v3: 02-F3-Energy/_shared/impulse-correction-punch-list-v3-2026-06-04.md

## D-029 · HubSpot runtime portal guard -- never operate on the wrong portal (2026-06-06)

**Context:** The 2026-05-31 portal migration (old 243870963 -> canonical 246351746,
see D-008) repointed Cora's code but NOT the Cowork HubSpot MCP connector. The drift
went unnoticed for ~6 days -- the 2026-06-06 hygiene-hubspot run silently audited the
dead portal. Founder doctrine D-030 (memory/decisions.md): any system-of-record
migration must sweep + repoint every consumer in the same session, and any agent
touching HubSpot must verify the portal before writing.

**Decision:** `hubspot_client._assert_portal()` is a hard runtime guard.
- On the first request per process it calls `GET /account-info/v3/details` and asserts
  the live token's portalId == `HUBSPOT_PORTAL_ID` env (if set) == canonical 246351746.
- Confirmed mismatch -> raise `HubSpotClientError` (callers degrade to Cora's standard
  graceful refusal). It must NEVER silently operate on the wrong portal.
- Inconclusive probe (network error / non-200) -> fail open, do not cache, retry next
  call; the real API call surfaces its own auth error.
- A non-canonical `HUBSPOT_PORTAL_ID` env value is refused deterministically (no
  network) -- catches a misconfigured repoint instantly.
- Wired through `_headers()` so every read/write path is covered; `log_email_engagement`
  (a `_token`-direct write) now uses `_headers()`.
- Verified once per process then cached (`_portal_verified`).

**Test harness:** conftest sets `CORA_DISABLE_HUBSPOT_PORTAL_GUARD=1` so the broad
suite (which mocks httpx with deal-search payloads, no portalId) does not trip a false
mismatch. `tests/test_hubspot_portal_guard.py` clears the flag and exercises the real
logic (match / mismatch / inconclusive / env-override / caching / `_headers`).

**Confirmed live (2026-06-06):** portal 246351746; F3E Retail pipeline 2313722582;
UFL Sponsorships = `default` pipeline ("UFL / OSN / BDM") -- no separate UFL pipeline
(Sales Hub Starter 2-pipeline cap).

**Deliberate non-actions:** state stores (`data/state/deal_task_sync_state.json`,
`data/hubspot_deal_snapshots.db`) already key on new-portal deal IDs and were NOT
flushed -- flushing would re-create ~22 duplicate Asana tasks (D-031). The existing
private-app token already authenticates on 246351746; rotation optional.

**Companion to D-008** (portal migration). Commit `2500668`.

## D-030 · Hygiene root fixes -- meeting-capture dedup, nudge unification, project routing (2026-06-06)

The 2026-06-06 hygiene-asana sweep surfaced three recurring hygiene failures. Symptoms
were cleaned in Asana; these are the code-level anti-recurrence fixes.

**Fix 1 -- Meeting Action Capture duplicate creation (commit `6429aa3`).** Builds on
`1d17912` (transcript-ID dedup) with three layers:
- Per-meeting *atomic* watermark persistence: `processed_ids` are marked AND written
  after each meeting, so a crash later in a run can't lose dedup state for finished
  meetings (the 6/4 double-fire root cause -- run died before the single end-of-run
  watermark write).
- Process lockfile (`data/state/meeting_action_capture.lock`, stale >2h) in the run
  script -- a concurrent instance exits immediately; skipped in `--dry-run`.
- Creation-time dedup guard `asana_client.find_recent_duplicate_task()` (typeahead +
  per-gid confirm, fail-open) -- skips creating an action item if an identical OPEN
  task was created in the last 7d. Catches the partial-crash case.

**Fix 2 -- two nudge systems unified on one ledger (commit `1122214`).** New
`src/cora/nudge_ledger.py` reads AND appends the EXISTING closure-nudges JSONL (the same
append-only file the weekly Cowork hygiene-asana sweep already uses for its 7d lockout).
Sharing that file gives a bidirectional guarantee with zero SKILL change. The daily
hygiene-nudge now skips any task nudged by EITHER system within 14d and records its own
fires. Path: `CLOSURE_NUDGE_LOG_PATH` (default = Drive closure JSONL). Doctrine: **max 1
automated comment of any kind per task per 7 days.**

**Fix 3 -- captured tasks routed into projects (commit `8991289`).** Fireflies-captured
action items were created with NO project -> untaggable orphans (Asana custom fields are
project-scoped). New `data/maps/meeting-capture-projects.yaml` maps entity -> project +
Entity/Status/Priority field GIDs. `run_action_capture` now creates tasks INTO the
entity's project and best-effort stamps the fields (`set_task_custom_fields`, never
fatal). Config-map design (vs auto-creating projects) keeps it repointable.
**ONE manual step remains to make this live:** fill `project_gid` (and optionally
`entity_options`) in the YAML; until then behavior is unchanged + an orphan warning is
logged.

**Test isolation (commit `d2c6929`).** Adding two new test files shifted collection order
and exposed a latent leak (test_hubspot_portal_guard's `_portal_verified` global / guard
env bleeding into test_hubspot_two_way). conftest autouse fixture now force-resets the
portal guard + isolates the nudge-ledger path after every test. Full suite order-
independent: **3232 passed, 41 skipped.**

**Discovered (out of scope, not fixed):** `run_asana_hygiene_nudges._has_kb_signal`
queries table `chunks` but the KB table is `knowledge_chunks` -> the KB-signal skip
silently never fires (fails soft). Worth a follow-up.

### Update (2026-06-06, later same day) -- follow-on commits

- **KB-signal bug fixed (commit `8381b6f`).** The "discovered" item above is now done:
  table `chunks` -> `knowledge_chunks`, and recency column `ingested_at` -> `date_modified`
  (after a full re-ingest every row's `ingested_at` is recent, which made the 30-day
  window a no-op). Validated via `--dry-run`: `skipped_signal` 0 -> 65; "no such table"
  warnings gone. +5 tests.

- **Fix 3 is now LIVE (commit `2020f91`).** `meeting-capture-projects.yaml` `project_gid`s
  populated for all entities except BDM (excluded), using per-entity catch-all
  "Operations -- General" GIDs from `asana-project-map.yaml` (produced by the concurrent
  Asana structure rebuild; every GID cross-checked == that entity's `catch_all_gid`).
  Captured tasks now route to projects + get Status=Not Started / Priority=Medium stamped.
  `entity_options` still blank -> Entity field tagging stays off until those option GIDs
  are supplied. LEX* entries populated but inert (PHI guardrail skips all LEX meetings
  before routing).

- **Clover stripped from `inventory-thresholds.yaml` (commit `2020f91`, D-027 follow-through).**
  Removed the `osn:` item-level block ("Clover inventory by item name"); only `f3e:`
  thresholds remain. grep `clover`/`osn` -> 0.

Suite at this point: 3269 passed, 41 skipped. Cora restarted, post-restart heartbeat
confirmed. Live end-to-end routing verification pending the next real capture (watch armed).

---

## D-031 (cora log) · Knowledge-gap autofill doctrines (2026-06-07, commit 54b1ef2)

_Note: numbering is this repo's own sequence -- distinct from the founder memory/decisions.md
D-031 (Asana hygiene remediation). Cross-log collisions are pre-existing (D-027+ differ too)._

**Context:** 41 gaps in logs/knowledge-gaps.jsonl, 1 ever resolved via the manual digest
flow. New two-stage autofill ships in src/cora/gap_autofill.py + scripts/run_gap_autofill.py
(task cowork-cora-gap-autofill, daily 6am AZ).

**Decisions LOCKED:**

1. **Both stages Harrison-gated** -- mined drafts AND teammate DM answers enter the existing
   knowledge-review queue as update_type="known_answer". Nothing writes to known-answers
   files without Harrison thumbs-up (extends D-011, no exceptions).

2. **Fail-closed drafting** -- a Haiku API error, JSON parse failure, or PHI-flagged answer
   proposes NOTHING (contrast with capture_decisions fail-open, which degrades to heuristics).
   Rationale: a wrong "known fact" poisons every future answer for that entity.

3. **Evidence source = swept Slack conversations only** (source="slack", distance <= 1.30,
   min 2 chunks). Override via GAP_AUTOFILL_SOURCES env if other sources earn trust later.

4. **Escalation rules** -- one DM ask per gap EVER; max 3 asks/run; 72h age gate; LEX* and
   PHI-flagged gaps NEVER escalate; owner map at data/maps/gap-domain-owners.yaml; decline
   phrases leave the gap open for the digest flow.

5. **Shared resolution ledger** -- autofill writes the same design/known-answers/{entity}.md
   "## Known facts" format and the same .resolved-gaps.jsonl as the manual digest flow, so
   the two flows can never double-resolve or fight.

6. **DM routing precedence** -- gap-ask reply capture runs BEFORE osn_shift_handler in
   app.py's DM path. Threaded replies to the ask message always win; top-level DMs are
   captured only when the user has exactly one live ask and the text is not a shift command.

---

## D-032 · Conversational replies pass through a deterministic formatter; tool outputs bypass (2026-06-08)

**Context:** A 2026-06-08 comms review of ~40 recent Cora messages found systematic
violations of the voice/style contract in `design/system-prompts/fndr.md` (em-dashes,
emojis, markdown bold/tables/headers/rules, filler prefaces) and source-opacity breaches
(bare `docs.google.com` / `app.asana.com` URLs and raw Asana GIDs pasted to users).
Prompt-only enforcement of the contract drifts.

**Decision:** Every CONVERSATIONAL reply is passed through
`src/cora/reply_formatter.py` `format_reply(text, *, is_tool_output=False)` immediately
before posting -- the same point in `app.py` where `WRITE_CONFIRMED` and the
`[CORA_KNOWLEDGE_GAP: ...]` marker are stripped. It flattens banned markdown, replaces
em/en-dashes with hyphens, strips emoji + `:shortcodes:`, redacts bare
docs.google/drive/asana/notion URLs and naked `gid`/16+-digit IDs (while PRESERVING
sanctioned `<url|label>` links and `<@mentions>`), and measures + logs the 280-char cap
WITHOUT truncating (truncation is worse than length; the cap is enforced primarily via
the prompt). TOOL OUTPUTS bypass the formatter entirely via `is_tool_output=True` --
financial pulses, decision queues, dashboard/pipeline summaries are presented exactly as
the tool returned them (per fndr.md "tool outputs are presented as-is").

**Alternative considered:** Tighten the system prompt only. Rejected -- same failure mode
as the cross-entity guard and sibling guard: prompt-only enforcement of a hard requirement
drifts; deterministic code-level interception at the latest point before posting is
required.

**Reason:** Mechanical, testable (`tests/test_reply_formatter.py`), and immune to model
drift. The formatter never changes financial / PHI / cross-entity guard behavior -- it
only shapes already-approved conversational text.

**Note:** the formatter MODULE shipped in commit `544bbe2`; the `app.py` wiring + Cora
restart were HELD pending a clean working tree (a concurrent session held `app.py`
uncommitted). Wiring + restart land in the next clean-window commit.

---

## D-035 -- WAL-mode in-place VACUUM does NOT shrink the database file (2026-06-09)

**Decision:** To reclaim disk from `cora_kb.db`, use `VACUUM INTO 'copy.db'` (then swap the
file in with Cora stopped) OR `journal_mode=DELETE; VACUUM; journal_mode=WAL`. A plain
in-place `VACUUM` while the connection is in WAL mode writes the compacted pages into the
WAL, leaving the MAIN file at its high-water mark -- it reports success but reclaims ~0.
Both reliable methods need EXCLUSIVE access (Cora + every KB holder stopped). `VACUUM INTO`
needs only a read lock and round-trips the vec0 tables cleanly (verified), so it is the
safest when the live service can't be fully stopped. Helper: `scripts/reclaim_kb_space.py`.

**Reason:** Discovered shipping section 10.6 -- the drop+in-place-VACUUM showed "reclaimed
0.00 GB"; `VACUUM INTO` proved the true compacted size (6.11->3.20 GB). Python sqlite3 also
wraps PRAGMAs in a transaction, so set `conn.isolation_level = None` (autocommit) before
`journal_mode=DELETE` or the mode switch self-locks with "database is locked".

## D-036 -- Elevated processes are invisible to / unkillable from a non-elevated shell (2026-06-09)

**Decision:** Any destructive KB op that needs exclusive access (DROP/VACUUM, file swap)
must run from ELEVATED PowerShell. `cowork-cora-service` runs `-RunLevel Highest`, so its
python (and any stuck child) has an unreadable `ExecutablePath`/`CommandLine` and returns
"Access is denied" to `Stop-Process` from a non-elevated session. Therefore: (a) do NOT
trust a "0 cora python" check that filters by `.Path`/CommandLine `-like '*cora*'` -- it
silently misses elevated procs; count ALL `python.exe` to confirm a clean stop; (b) kill by
PID via `... | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`, NOT by piping CIM
objects straight to `Stop-Process` (that binds by Name and fails).

**Reason:** A 14h-stuck elevated python pair held `cora_kb.db` and blocked every truncating
VACUUM with "database is locked"; the cause was invisible to non-elevated checks. Composes
with the existing WMI/CommandLine-kill doctrine (a CommandLine match still misses procs
whose CommandLine is unreadable due to privilege).

## D-037 -- PowerShell sandbox quirks: separate file-ops from scheduler-ops (2026-06-09)

**Decision:** Keep `Remove-Item`/`Move-Item` (file ops) and `schtasks` (scheduler ops) in
SEPARATE tool calls. The sandbox command analyzer false-positives when they share one
command -- it mis-binds `schtasks /Change` as a protected-path removal and blocks the whole
command before execution. Also: `$pid` is a read-only automatic variable (use `$procId`);
`[System.IO.File]::ReadAllBytes(rel)` uses .NET's CWD, not PowerShell `$PWD`, so pass
absolute paths.

**Reason:** Cost two blocked swap attempts during the section-10.6 reclaim before the cause
was clear.

## D-038 -- Gmail/KB sweeps must be resumable, stale-first, and cap-aware (2026-06-09)

**Decision:** Long multi-account sweeps (gmail_threaded_sweep.py and similar) must: (1) persist
their watermark AFTER EACH account, not once at end-of-run; (2) process accounts STALE-FIRST
(oldest/never-swept watermark first) so no account is starved behind a time limit; (3) be
CAP-AWARE -- when an account hits the per-run thread cap, advance its watermark only to the
newest message actually processed, never jump to "now" (that silently drops backlog); (4) ship
with an ExecutionTimeLimit that fits the workload (gmail = 3h). A bulk one-time backfill is a
separate `--max-threads`/`--fallback-days`/`--accounts` invocation.

**Reason:** The gmail sweep silently stalled 5-28 -> 6-08: a 1h task limit killed it ~14/28
accounts in every night, and the end-of-run-only watermark flush meant nothing ever persisted,
so early accounts re-scanned a growing backlog nightly while the entire Lexington + UFL mailbox
sets were never reached. Read-vs-unread was never the issue (`after:{ts}` covers both).

## D-039 -- KB SQLite needs PRAGMA busy_timeout (WAL alone is insufficient) (2026-06-09)

**Decision:** `schema.connect` sets `PRAGMA busy_timeout=30000`. WAL allows concurrent readers +
ONE writer, but two writers (e.g. the live bot ingesting while a manual backfill runs) still
contend; without a busy timeout the loser raises `OperationalError: database is locked` and
crashes mid-run. A heavy daytime backfill should run with the bot also on busy_timeout (i.e.
after a restart that picks up this change) so both wait politely.

**Reason:** The first gmail deep-backfill crashed on "database is locked" colliding with the
live service; busy_timeout fixed it.

## D-040 -- DR backup must include encrypted secrets + verify offsite (2026-06-09)

**Decision:** The daily backup (`backup_logs.py`) must (1) bundle `.env` + the Google SA JSON
into ONE Fernet-encrypted blob (key from `CORA_BACKUP_PASSPHRASE` via PBKDF2; SKIP rather than
ever write plaintext if the passphrase is unset; decrypt via `restore_secrets.py`); (2) online-
backup the small feature DBs; (3) VERIFY the KB actually landed at the offsite destination and
exit non-zero if not. Scheduled backup tasks use `.venv` python (D-005) and a time limit that
fits a multi-GB online backup (60m). The passphrase lives in Harrison's password manager + a
persistent User env var, NEVER in `.env` (the thing being backed up).

**Reason:** Code is on GitHub and the KB was already backed up, but `.env` + SA JSON (gitignored)
were backed up nowhere -- a machine loss would leave a rebuilt Cora unable to authenticate to
anything. The old backup also had a silent-failure mode (reported success with no offsite file).

## D-041 -- Shared working tree + sandbox reliability doctrines (2026-06-09)

**Decision:** When multiple Cowork/Code sessions share the one repo working tree: (1) commit
with EXPLICIT paths (`git add <files>`), NEVER `git add -A` -- it sweeps other sessions' in-flight
work into your commit (observed live: a busy_timeout edit landed in an unrelated session's
commit). (2) A stale `.git/index.lock` (0-byte, from a crashed/concurrent git) silently fails
every commit -- clear it (after confirming no git process runs) as the first step of any ship
script, and VERIFY HEAD actually advanced after committing (checking `git push` exit code is
not enough -- "Everything up-to-date" returns 0). (3) Do NOT bulk re-register scheduled tasks
with `-StartWhenAvailable` while past-due -- Windows fires the whole missed batch at once (a
thundering herd that collided three sweeps + the bot). (4) The Cowork Linux sandbox's virtiofs
view of host-edited files goes STALE/truncated -- for host files trust the Read tool and
`git show HEAD:<path>` (object store, stale-proof), NOT sandbox `cat`/`git diff`/`py_compile`.

**Reason:** All four cost real time during the 2026-06-09 multi-session commit storm; codifying
them prevents the next pile-up.

## D-042 -- Session capture: no Python transcript API + Path("") is truthy (2026-06-09)

**Decision:** (1) Cora's Python service has NO session-transcript API. The Universal Session
Capture spec named `session_info.list_sessions` / `read_transcript`, but those are MCP tools
available only to the Cowork/Code harness, not to the standalone service. The harvester
(`session_capture.py`) reads the on-disk Claude Code transcripts at `~/.claude/projects/**/*.jsonl`
directly (skip `*/subagents/*`; dedup by session id via `logs/session-captures.jsonl`; 30-min
settle window so live sessions aren't grabbed). Claude Desktop Cowork chats live in a SEPARATE,
undocumented `AppData\Roaming\Claude` store and are NOT reachable by the harvester -- they rely on
the session-end self-capture doctrine instead. (2) `Path("")` is TRUTHY (it equals `Path(".")`),
so `Path(os.environ.get("VAR", "")) or fallback` never reaches the fallback when the var is unset --
it silently resolves to the current directory. Guard on the string (`Path(s) if s else default`),
not on the Path object. PHI routing: if `phi_guard.is_phi_risk` fires on a transcript, the note is
forced into the LEX-scoped store regardless of the distilled entity, so client PHI is always gated
by `lex_phi_access` + the existing sibling / cross-entity guards.

**Reason:** Both were live traps in the 2026-06-09 build. The transcript-API assumption would have
made the feature unbuildable as specced; reading JSONL directly is the only workable path on this
machine. The `Path("")` truthiness bug made the first dry-run find zero transcripts (it pointed at
cwd) -- caught only because the dry-run reported "0 examined".


## Closeout -- 2026-06-10 weekly closeout: D-032 wiring live + single clean restart

**What activated (one restart, 10:59:45 AZ 2026-06-10):**
- `3dc0f2b` feat(comms): D-032 reply-formatter wiring -- generate_response /
  generate_response_streaming now report tool usage via a caller-owned `meta` dict;
  app._dispatch_qa (shared by @-mentions, thread follow-ups, /cora-ask) passes every
  conversational reply through reply_formatter.format_reply after gap extraction and
  before the semantic-cache store; tool-bearing replies bypass via is_tool_output=True.
- `06417e7` fix(nudges): _SYSTEM_NOISE_SKIP_TERMS skip for Asana "It's time to update
  your goal(s)" auto-reminders in the daily hygiene nudge (Cowork edit 2026-06-10,
  committed for durability; picked up by the next 06:30 run, no restart needed).
- `e8b2fac` docs(tom): committed the 2026-06-09 session's leftover [NEXT UP] per-user
  email/Drive access-spec TOM pointer so the tree was clean for the restart.
- Already-pushed dormant commits now confirmed live: fndr_press_pipeline_summary
  (b418bb4), f3e.md production-KB prompt + T3 escalation (8acbdf4), reply-formatter
  module + refusal-copy fix (544bbe2), Universal Session Capture + LEX PHI custodian
  gate (4a9c8c9 -- was already live since the 2026-06-09 restart).

**State:** HEAD `e8b2fac` on origin/main. Full suite 3,636 passed / 41 skipped.
Restart: orphan-kill via WMI CommandLine match (2 procs killed), single instance
relaunched 10:59:45, heartbeats steady from 11:00:48.

**Smoke results (live Slack, 11:07):**
1. Press pipeline (#hjrg-leadership): PASS -- totals + status breakdown + per-entity
   Published-vs-AfC; tool bypass kept rich output intact.
2. F3 production KB (#f3e-leadership): PASS -- "Pure R-ALA, not RS-ALA" + Run 1 context.
3. Reply formatter conversational (#hjrg-leadership): PASS -- no em-dashes / markdown
   bold / emoji in a no-tool reply; log confirms tool-using replies took the bypass.
4. PHI custodian gate: PASS at the code level -- custodian (Harrison) in #lex-leadership
   reached the LLM (no hard refusal); PHI ask in non-LEX channel did not surface client
   detail. Non-custodian refusal covered by tests/test_lex_phi_access.py (cannot be
   live-tested from Harrison's account).

**Flags for follow-up (no code change this session):**
- lex*.md system prompts still say "HIPAA compliance for Slack-with-Lex is UNVERIFIED
  as of 2026-05-24" -- stale vs the 2026-06-09 BAA confirmation, so Cora describes the
  custodian gate as inactive and behaviorally refuses PHI to custodians in LEX channels.
  Fail-closed (safe direction), but the prompt layer now lags the sanctioned model.
  Harrison to decide the prompt-language update.
- In the non-LEX PHI redirect, Cora emitted channel links with IDs not present in the
  channel registry (likely hallucinated <#id|name> tokens) -- the reply took the tool
  bypass so the formatter's GID redaction did not apply. Watch for recurrence.
- Untracked leftovers in the tree (not committed, unknown origin): .git-corrupt-backup/,
  backups/, deployment/recover-backlog-2026-06-08.ps1, scripts/run_retroactive_hashtag_scan.py.

**Addendum (same day, later session):** Both flags RESOLVED, Harrison-directed.
(1) Post-BAA authorized-custodian language shipped to all 5 LEX prompts (`0dad7c4`);
verified live in #lex-leadership -- Cora now correctly offers client-level PHI to the
four custodians in LEX scope at minimum-necessary, LBHS keeps the 42 CFR Part 2
heightened posture. (2) Hallucinated channel-link validator shipped (`4a03a1c`) --
_validate_channel_links verifies every <#Cxxx|name> token via conversations_info on
ALL replies including tool-bypass output. Also: working tree fully cleaned (`ff5e877`)
-- runtime churn files untracked + ignored, ending the perpetually-dirty-tree
condition. Second clean restart 12:07:55 AZ, heartbeat fresh. HEAD `4a03a1c`,
3,643 passed / 41 skipped.


## D-043 -- Per-user email/Drive access: two-tier code-level guard + finance content-type exception (2026-06-10)

**Decision:** Access to personal-source KB content (source gmail / drive_sweep) is governed by a
deterministic pre-LLM guard (D-034 pattern), NOT prompts:

1. **Tier 1 (institutional knowledge, everyone):** gmail/drive_sweep chunks still inform any
   answer, but a chunk owned by someone other than the asker is HEADER-STRIPPED before it enters
   LLM context (`historical_access.apply_tier1`, wired in `context_loader._try_kb_retrieve`):
   title/Subject, author/From, To/Cc/Date/Attachments header lines, deep_link, date, metadata
   (message_id/thread_id) all removed; the factual body survives. Owner identity =
   `metadata.user_email` (100% coverage verified on both sources 2026-06-10); unknown owner =
   stripped (fail-closed); `founders_os@hjrglobal.com` chunks are org-shared and exempt.
2. **Tier 2 (explicit retrieval):** "pull up / show me / find the email(s)" is DM-ONLY (channel ask
   gets a redirect), owner's-own-mailbox-only (aliases included), Harrison-override via
   `data/maps/historical-access-allowlist.yaml` (default Harrison; file-driven, 60s TTL,
   fail-closed), explicit refusal for an internal teammate's mailbox with no existence leak,
   FAIL-CLOSED for unmapped Slack identities. Implemented as `historical_access.check_tier2` +
   `store.search_owned` (exact scan over the owner-filtered subset -- recall-perfect, no
   coarse-index starvation) + a new plain-DM branch in app.handle_message_event.
3. **Tier 2-Finance (content-type permission, not a mailbox permission):**
   `data/maps/finance-receipt-allowlist.yaml` (Justin/Eric/Jerry by Slack ID) may retrieve
   `metadata.financial_document=true` chunks from ANY mailbox, ONLY inside #hjr-finance
   (`C0BAK65N4TA`); non-financial retrieval on that path is refused; every pull is audit-logged to
   `logs/finance-access-audit.jsonl`. Tagging is deterministic + precision-biased
   (`finance_doc_classifier`, >=2 independent signals) at the `store.upsert_documents` choke point
   (Step 0b) so the 18-month gmail backfill arrives pre-tagged;
   `scripts/backfill_financial_document_tags.py` is the idempotent catch-up for older chunks.
   Auto-file copies retrieved + weekly-digest-detected receipts into the "Receipts & Invoices
   Inbox" Drive folder (`1I7zWcCIAOx7zdzIXcxx6WTLk1K40eizj`; SA write verified); weekly task
   `cowork-cora-finance-receipt-digest` (Mon 10:30 AZ) posts the proactive digest with per-account
   atomic watermarks (D-038) + a dedup ledger.

**Companion invariants:**
- **Semantic-cache leak closed:** any response built on UNSTRIPPED personal chunks (a Tier-2 grant,
  an owner's own chunk, or an unrestricted asker) is NEVER stored in the shared semantic cache --
  a similar question from another user must not replay private mail. Grant-path requests also skip
  cache LOOKUP entirely.
- **Grant path withholds static portfolio context** (`static_text=""`): a DM asker may not be
  entity-authorized for the founder brief, and explicit mailbox retrieval doesn't need it.
- **PHI:** ingest guards already exclude LEX client PHI; grants additionally run
  `historical_access.drop_phi` (defensive phi_guard pattern filter). Sibling + cross-entity guards
  are untouched and still run.
- A Tier-2 grant deliberately does NOT consult user_access topic blocks -- the scope is the asker's
  OWN mailbox, which they may always see (spec directive).

**Reason:** Harrison's requirement (spec 2026-06-09): Cora absorbs organizational email/Drive
knowledge collectively, but specific individual emails go only to their owner (or Harrison), and
the finance team gets receipts/invoices -- a content-type carve-out -- without ever seeing the
private mail around them. Prompt-only enforcement is insufficient for hard privacy rules (D-034);
shipping the guard BEFORE the 18-month backfill lands means the historical mass arrives into an
already-guarded, tag-at-ingest pipeline.


## D-044 -- org-roles.yaml is the canonical org role registry: advisory-only, fail-closed (2026-06-10)

**Decision (Harrison-approved 2026-06-10 after full roster review):**

1. `data/maps/org-roles.yaml` is the CANONICAL registry of who each person is in the
   organization: role, primary entity, additional entities, responsibilities (lanes), manager,
   routing notes, external flag. Loaded by `src/cora/org_roles.py` (60s TTL live-reload -- edit
   the YAML, no restart).
2. **Advisory-only:** the registry NEVER expands access. It tailors tone, relevance, and
   proactive suggestions. Access control remains exclusively with the deterministic guards
   (user_access, sibling_guard, cross_entity_guard, phi_guard, historical_access D-043). Every
   injected role block carries an explicit no-expansion rule so prompt-layer behavior cannot
   silently drift from this contract.
3. **Fail-closed:** an unknown Slack user gets no role block -- exactly the pre-registry
   behavior. Parse errors keep the last good registry; a registry typo must never take Cora
   down or change access posture.
4. **Roster changes go through Harrison** (D-011 pattern). Registry-only entries (no slack_id,
   e.g. Tessa Miller) ride `all_roles()`/`roles_for_entity()` for roster-level features but can
   never trigger role-block injection.
5. **New per-user features read this registry** instead of growing new per-user YAML maps.
   Existing maps that drive live systems (slack-to-asana, role-briefing-config,
   lex-phi-custodians, finance-receipt-allowlist, gap-domain-owners) stay separate until their
   feature is reworked onto the registry (role-briefing-config consolidates in Org Synthesis
   Phase 2).

**Reason:** Org Synthesis program (spec
`_shared/projects/cora/design/2026-06-10_fndr_org-synthesis-spec.md`): Cora becomes the
role-scoped individual resource for every user and a portfolio-oversight layer for Harrison.
That requires one authoritative answer to "who is this person and what do they own" -- before
Phase 1 it was scattered across six maps and the founder brief. Keeping it advisory-only
preserves the D-034 doctrine that hard security boundaries live in code-level guards, never in
context or prompts. Shipped: `8d153b6` (registry + loader + injection, 3,762 tests) +
`721970e` (roster review: Jerry Reick = Staff Accountant under Justin; Tessa Miller added as
first registry-only entry; 3,766 tests).

---

## 2026-06-11 -- D-044 item 5 EXECUTED: briefing rework onto org-roles.yaml; role-briefing-config.yaml retired (Org Synthesis Phase 2 d2)

**What happened:** `run_daily_briefing.py` rewritten to read `data/maps/org-roles.yaml` via
`org_roles.py`. `data/maps/role-briefing-config.yaml` is DELETED -- this was the locked
consolidation point (D-044 item 5: existing per-user maps stay separate "until their feature
is reworked onto the registry"). Do NOT recreate the old config; the registry IS the briefing
roster. A regression test (`tests/test_per_role_briefing.py::TestOldConfigRetired`) fails the
suite if the file or any source reference to it reappears.

**Content contract:** per-user briefing content mirrors the `whats_on_my_plate` composite and
REUSES its section builders from `tool_dispatch` (`_plate_asana_section`,
`_plate_calendar_section`, `_plate_hubspot_section`, `_safe_plate_section`,
`_tool_fndr_open_decisions`) -- the logic is shared, not forked, so plate scoping/fail-soft
fixes apply to the briefing automatically. Sections: role + lanes, entity-scoped open tasks
(capped 10), today/tomorrow calendar, deal pipeline for owners (LEX scope never, Tier-1
doctrine), stalled decisions Harrison-only, plus the 25h recent-activity KB scan the briefing
has always carried. The old `extra_data` system (hubspot_f3e / hubspot_all / financial /
deal_aging) retired with the config -- the plate carries NO financial figures by doctrine
(Harrison's daily Cash Flow Pulse covers cash separately).

**Exclusions (fail-closed):** external consultants (`external: true`, e.g. Jason Dorfman) and
registry-only people (no slack_id, e.g. Tessa Miller) never receive delivery; anyone absent
from the registry is skipped by construction.

**ROLLOUT DOCTRINE (Harrison-locked 2026-06-11): digest-to-Harrison-first.** The script
DEFAULTS to digest mode -- ONE DM to Harrison containing every user's would-be briefing for
review. Per-user delivery requires the explicit `--send-users` flag and flips on only after
Harrison's explicit go (`setup-daily-briefing-task.ps1 -SendUsers`). Same pattern as the
Fireflies coverage rollout. No unsolicited DMs before review.

**Shared-builder fix (applies to the plate tool too):** `_plate_asana_section` now
canonicalizes sub-entities to their parent (`_SUBENTITY_PARENT`) before the task filter.
Previously a raw sub-entity (LEX-LLC, OSNGW, ...) fell through `ENTITY_PROJECT_PREFIXES`
UNFILTERED -- with the 6/11 registry move of Shaun/Jen/Jeff/Aaron to `entity: LEX-LLC`, their
plates and briefings would have shown unscoped task lists. A sub-entity scope must never be
wider than its parent's. Bot-loaded: the plate-tool side activates at the next restart; the
briefing side is live at the task's next fire (fresh process).

---

## D-045 -- Closed-task nudge guard: fire-time completion re-check + permanent ledger exclusion (2026-06-11)

**Context (Hannah report, #info-for-cora 2026-06-11):** Hannah received DAILY nudge comments
on an Asana task completed a year prior ("Jimmy Bar - Potential Activation", closed
2025-06-03, nudged every day 6/05-6/11). Root-cause investigation found THREE nudge sources,
none of which re-checked completion at fire time:

1. **Make.com scenario 4768887** ("[HJR] Asana Hygiene Nudges -- Overdue Task Comments",
   created 6/04) -- THE OFFENDER. Its filter had two condition GROUPS, which Make ORs:
   `(due_on < now-14d) OR (completed == false)` -- so any task overdue 14+ days passed even
   when completed. It listed tasks without a completed_since filter, ran DAILY with no
   throttle/ledger (violating the D-031 max 1 comment/task/7d doctrine), and posted via
   Harrison's Asana connection (which is why ledger forensics showed nothing). Hannah was hit
   as a follower on Harrison's tasks.
2. The daily Cora job (run_asana_hygiene_nudges.py) -- candidates are incomplete-only
   (completed_since=now) but nothing re-checked completion between listing and firing.
   ALSO: the scheduled task "Cora - Asana Hygiene Nudges" is ENABLED and firing daily,
   despite the 6/05 memory claiming it was disabled in favor of the Make scenario --
   Cora + Make were BOTH nudging (Harrison to pick one owner; see TOM).
3. The weekly hygiene-asana Cowork sweep -- throttle-ledger-aware but no fire-time
   completion re-check.

**Decision (Harrison-approved fix directive 2026-06-11):**

1. **Shared chokepoint guard in `nudge_ledger.py`:** `closed_task_guard(task_gid)` runs at
   fire time -- checks the ledger for a permanent exclusion first (no API call), then fetches
   live `completed`/`completed_at` via the new `asana_client.get_task_completion`. Completed ->
   skip + append a `reason="already_closed"` row. The exclusion is PERMANENT when completed_at
   is older than 48h (`CLOSED_PERMANENT_AFTER_HOURS`) or missing; a just-closed task gets a
   throttled row that re-evaluates later. Skip rows carry `last_nudged_at` deliberately so the
   weekly sweep's existing lockout window honors them with zero SKILL changes. Fetch errors
   fail OPEN (the nudge proceeds; a dead Asana API fails loudly at comment-post).
2. **Make scenario 4768887 fixed in place:** filter conditions moved into ONE AND group
   (`overdue 14d+ AND incomplete`) and cadence dropped daily -> weekly (604800s) to respect
   the 1 comment/task/7d doctrine. Next exec 2026-06-18.
3. **Weekly sweep SKILL.md patched** (OneDrive Scheduled/hygiene-asana): already_closed
   permanent rows are never re-commented; live completion re-checked before any comment.

**Doctrine locked:** Any automation that comments on Asana tasks MUST re-check the task's
live completed status immediately before posting, and MUST consult/append the shared
closure-nudges ledger. A task completed >48h ago is permanently excluded from all nudging.
Make filter conditions that must ALL hold belong in ONE condition group -- separate groups
are OR'd, and a wrong grouping turns a guard into a bypass.

---

## D-046 -- LEX Dump Folder: recurring recursive KB sync replaces the one-shot ingest (2026-06-11)

**Context (Shaun, #lex-leadership 2026-06-11; Asana 1215643646634974):** Cora could not
answer what DDD policy says about live-in caregivers' EVV responsibilities. The 2026-06-01
ingest of the Shaun x Jen Lexington Dump Folder was a ONE-SHOT script with a hardcoded
20-file list; the "DDD Policies" SHORTCUT added 6/04 (-> folder with the DDD Complete
Provider/Operations/Medical/Behavior Supports/Eligibility manuals + a 57-file EVV Documents
folder incl. EVV_Live-InCaregiverFAQ.pdf) was never picked up. The old script also capped
PDF parsing at 80 pages, silently truncating the Provider Manual.

**Decision (Harrison-approved 2026-06-11):**

1. **`scripts/run_lex_dump_folder_sync.py`** replaces `ingest_dump_folder.py`: recursive
   enumeration (follows folder shortcuts, depth-capped), watermark-incremental
   (sync_state source `lex_dump_folder`, --backfill to force), no PDF page cap (2000-page
   sanity bound), >60MB files skipped with a logged note. Scheduled task
   **"Cora - LEX Dump Folder Sync"** daily 4:45am AZ (registered, non-elevated OK).
2. **Tagging:** entity=LEX everywhere. Files inside the curated DDD Policies tree
   (published AHCCCS/DES policy docs) -> sub_entity=NULL (GM-level) with
   `metadata.lex_gm_level=True`; everything else keeps LEX-LLC (tightest). A filename that
   looks like a client record (progress report / assessment / intake form...) is forced to
   LEX-LLC even inside the policy tree -- fail-closed against drift.
3. **store.py Step 0 opt-out:** `metadata.lex_gm_level=True` blocks LEX sub-entity
   auto-detection. Published manuals mention HCBS/Day Program constantly; auto-detection
   would scatter a cross-sub-entity manual's chunks into single sub-entity scopes.
4. **PHI guard posture:** `phi_guard.is_phi_risk` runs per chunk; the count is logged and
   stored in `metadata.phi_risk_chunks`. For the curated policy tree it is an AUDIT signal,
   not a scope downgrade -- published manuals trip the program-keyword patterns
   (ahcccs/medicaid/assessment) on most chunks BY CONSTRUCTION because they are manuals
   ABOUT those topics. Keyword PHI detection cannot distinguish policy-about-PHI from
   actual PHI; the compensating controls are the LEX-LLC default outside the tree, the
   client-record filename rule inside it, and the unchanged response-layer guards
   (prompts + custodian gate + sibling/cross-entity guards).
5. **Known visibility tension (flagged, not resolved here):** GM-level (NULL) chunks are
   excluded from #llc-*/#lts-*/#lbhs-*/#lla-* by the locked strict filter -- and the LLC
   team (Shaun/Jen/Jeff/Aaron) left #lex-leadership on 6/11 per the LLC routing directive.
   The DDD manuals are therefore invisible in the channels that team now lives in.
   Harrison decides: published-policy carve-out in the strict filter, or re-tag the DDD
   tree to LEX-LLC.

**Reason:** Recurring coverage beats one-shot lists -- the folder is a living dump that
Shaun/Jen keep adding to (per the 2026-05-22 Cora x Lex direction). Backfill ingested the
full tree same-day (83 files; see TOM for chunk counts) so the live compliance question
(DES notices on live-in caregiver EVV date to Dec 2025) is answerable immediately.

---

## 2026-06-11 (addendum) -- Briefing rollout refined: review-driven per-user enablement via Harrison's reactions

**Harrison directive (2026-06-11, same day as the d2 ship):** a single combined digest DM
cannot be reviewed per user -- he wants ONE DM PER USER so he can thumbs the ones he wants
running. Shipped as `ed6c212` (suite 3,903 passed / 41 skipped); supersedes the
"single digest DM + -SendUsers full flip" mechanism in the entry above.

**Mechanism (self-contained in the scheduled task, no bot restart):** default mode sends
Harrison one review DM per user ("WOULD-BE BRIEFING -- name"). Each run STARTS by reading
reactions on outstanding review messages via the Slack reactions API -- ONLY Harrison's
reactions count (D-011 pattern). `:+1:` enables that user's real delivery from that run
on; `:-1:` declines (user dropped from review AND delivery; re-review by removing them
from the state file). State: `data/state/briefing-delivery.json` (enabled / declined /
pending_reviews; a newer review message replaces the older pending entry for the same user;
unanswered pendings expire after 30 days and the user simply reappears in review).
`--send-users` remains as a force-deliver-all override; `--digest-only` forces review
mode for everyone. No unsolicited DMs before a thumbs-up, ever.

**Aaron Ferrucci note:** Harrison reports an Asana account was assigned 2026-06-11, but the
API shows NO matching workspace user or Lexington-team member (likely invite pending his
acceptance). Fail-closed: no slack-to-asana row until his GID is visible; org-roles note
updated to say exactly that. His briefing/plate task section shows a fail-soft stub until
then.

---

## D-046a -- AMENDMENT: DDD Policies tree re-tagged GM-level -> LEX-LLC (2026-06-11 PM, Harrison)

**Supersedes D-046 item 2 (tagging) and resolves D-046 item 5 (visibility tension).**

Harrison directive same evening: re-tag the DDD Policies tree to LEX-LLC so the manuals are
visible in #llc-* channels, where the DDD policy consumers (Shaun/Jen/Jeff/Aaron) now live
per the 6/11 LLC routing directive. GM-level NULL tagging made the manuals invisible there
(strict sub-entity filter excludes NULL).

**Executed:**
1. `run_lex_dump_folder_sync.py` now tags EVERYTHING in the dump folder LEX-LLC, including
   the DDD Policies tree. The policy-tree detection survives as metadata provenance only
   (`metadata.policy_tree`); the client-record filename rule was removed (dead code once
   every path is LEX-LLC). Explicit sub_entity means store Step 0 auto-detection never fires.
2. **2,840 existing chunks re-tagged in place** (63 files, SQL UPDATE on knowledge_chunks --
   sub_entity lives only there, no re-embedding needed). Zero NULL dump-folder chunks remain.
3. Smoke test re-run in #llc-leadership (strict LEX-LLC scope) -- see TOM.

**Visibility after the change:** manuals visible in #llc-* (strict filter matches LEX-LLC)
AND all GM #lex-* channels (GM scope sees every LEX chunk). NOT visible in
#lts-*/#lbhs-*/#lla-* -- accepted; LTS is the only other DDD provider and can re-raise.

**Note:** the `lex_gm_level` store Step 0 opt-out (D-046 item 3) stays in the codebase --
it is a generic, tested mechanism for any future deliberately-GM-level LEX ingest; this
script simply no longer uses it.

---

## D-045a -- AMENDMENT: Cora is the sole owner of Asana hygiene nudges; Make 4768887 DEACTIVATED (2026-06-11 PM, Harrison)

**Resolves the D-045 ownership-drift item.** Harrison directive: deactivate Make scenario
4768887 and make the Cora daily job the default and only owner of overdue-task nudging.

**Executed:** scenario 4768887 deactivated via Make API (isActive=false, nextExec null;
blueprint retained with the corrected AND filter in case it is ever revived). The scheduled
task "Cora - Asana Hygiene Nudges" (daily 6:30am AZ) is confirmed Enabled/Ready and is now
the single nudge source alongside the weekly hygiene-asana closure sweep -- both share the
closure-nudges ledger and the D-045 closed-task guard.

**Doctrine:** overdue-task nudging lives in Cora (run_asana_hygiene_nudges.py). Do NOT
re-activate Make 4768887 or create parallel nudge automations -- the 6/05-6/11 period proved
that two unsynchronized sources double-comment and bypass the ledger. Any future nudge
behavior change goes into the Cora job.

## D-047 -- Org Synthesis Phase 3: weekly friction mining is proposal-only, ledger-deduped, LEX-excluded (2026-06-11)

**Decision:** Efficiency mining (`src/cora/friction_mining.py`, task "Cora - Friction Mining",
weekly Sunday 17:30 AZ) surfaces process-friction findings -- repeated questions, repeated
manual steps, stale handoffs, cross-entity duplication -- as proposals into the existing 7am
knowledge-review DM queue (`update_type="efficiency"`). Locked rules:

1. **Proposal-only (D-011):** nothing auto-executes. Harrison's thumbs-up routes the finding
   (via the run_knowledge_review.py executor) into `design/efficiency-backlog.md`
   (append-only); thumbs-down just resolves it.
2. **Fingerprints recorded at PROPOSAL time (D-030 pattern):** ledger
   `data/state/friction-fingerprints.jsonl` -- a finding never re-proposes regardless of
   outcome, including paraphrases (same-signal fuzzy >= 0.85). No dismissal hook needed.
3. **LEX is excluded ENTIRELY at the SQL layer** (entity/sub_entity NOT LIKE 'LEX%') --
   stronger than reconciliation passes 1-4. PHI-flagged content (is_phi_risk) is dropped for
   ALL entities; Visibility CPA mentions excluded.
4. **Haiku drafting FAIL-CLOSED** (gap_autofill pattern): any API/parse error, PHI in the
   draft, or a not-worth-proposing verdict proposes nothing.
5. **Caps:** max 5 proposals/run (highest confidence first), max 12 Haiku candidates/run,
   bounded embedding pools.
6. **org-roles is advisory routing context only** in the draft prompt (D-044) -- never an
   access expansion. Recommendation routing follows D-029: rule-based mechanical -> Make.com
   idea; language/context -> Cora tool idea; repeated questions -> known-answer/doc.
7. **Quoted-reply lines are never counted** ('>'-prefixed sentences are copies, not
   occurrences -- the first live dry-run counted one email line 134x via re-quotes before
   this rule).
8. **Standalone script-side stack:** friction_mining must never import bot-process modules
   (app/tool_dispatch/claude_client) -- regression-tested via a subprocess import check.
   Shipping changes here NEVER requires a Cora restart (the knowledge-review executor is
   also script-side).

**Why:** Phase 3 of the org-synthesis spec -- continuous learning at the entity level. The
reconciliation engine catches tracking gaps; this pass catches PROCESS gaps (the "should
this live at the holdco?" lens included). Routing through the existing Harrison gate keeps
one review surface and one approval doctrine.

Commits `a473a2d` (feature) + `5c84df5` (quote-skip), 3,951 tests. Rollout gate: Harrison
reviewed the 2026-06-11 live dry-run findings before the first scheduled fire (6/14).

## D-048 -- Org Synthesis Phase 4: weekly founder strategy memo is Harrison-only, fail-closed, advisory (2026-06-11)

**Decision:** The founder strategy layer (`src/cora/strategy_memo.py`, task
"Cora - Strategy Memo", weekly Sunday 18:30 AZ -- one hour after friction mining so the
memo sees that run's pending findings) produces a weekly portfolio synthesis memo.
Locked rules:

1. **Harrison-only distribution:** the memo is DM'd to `HARRISON_SLACK_ID` (hard-coded --
   no recipient parameter exists) and filed to
   `00-Founder/_strategy-memos/YYYY-MM/YYYY-MM-DD_fndr_weekly-strategy-memo.md` (nightly
   static_md sync ingests it, so Cora can be held to her own past recommendations).
   NEVER posted to any channel or any other user's DM -- source-level regression test
   pins exactly one Slack post site.
2. **Gather is deterministic and fail-soft PER SECTION:** cash (Standing ACTUALS via
   gsheets, the Cash Flow Pulse source), pipeline posture (HubSpot F3E Retail + default),
   stalled P0/P1 decisions (memory/decisions-pending.md), 14d Asana deadline radar,
   efficiency backlog + pending friction findings, 7d KB momentum counts, heartbeat
   one-liner. A dead source degrades to a stub line; it never kills the memo.
3. **Snapshots before synthesis:** every gather is written to
   `data/state/strategy-memo-snapshots/YYYY-MM-DD.json` (26 kept); deltas and multi-week
   streaks ("cash down N weeks straight", "decision unmoved N memos running") are computed
   from real snapshots. The first run says "first run -- no deltas yet" honestly.
4. **Synthesis is Sonnet (claude-sonnet-4-6), FAIL-CLOSED:** the one place quality beats
   cost. Any API error or PHI-flagged output falls back to a deterministic factual rollup
   with a "SYNTHESIS UNAVAILABLE" note -- never a hallucinated memo.
5. **LEX stays aggregate:** LEX cash/task counts may appear; LEX tasks are never itemized
   (counted in an aggregate-only bucket); is_phi_risk drops flagged content everywhere;
   Visibility CPA excluded; the synthesis prompt forbids client-level health information.
6. **Advisory only (D-011):** recommendations carry reasoning + trade-off and route per the
   holdco lens ("should this live at HJR Global?"); nothing auto-executes, no Asana or
   decisions.md writes.
7. **Standalone script-side stack (D-047 invariant):** strategy_memo never imports
   bot-process modules (app/tool_dispatch/claude_client) -- subprocess regression test.
   Shipping changes here NEVER requires a Cora restart.

**Why:** Phase 4 (final phase) of the org-synthesis spec -- the founder-level oversight
layer on top of Phases 1-3. Consumes Phase 3's approved efficiency-backlog entries in the
recommendations section, closing the loop from detection to strategy. Rollout gate:
Harrison reviews a --dry-run memo before the first scheduled fire.


## D-049 -- Org Synthesis Phase 5 d1: personal notes are owner-only at the SQL layer; notes are not canonical memory (2026-06-11)

**Decision:** Any teammate can teach Cora a personal note ("Cora, remember X") stored in
the main KB under `source="user_note"` + `metadata.owner_slack`. Locked rules:

1. **Blast-radius-1 is enforced in SQL, never prompts (D-034 pattern):** the general
   `store.search()` excludes `source='user_note'` in BOTH vector paths, so every consumer
   (Q&A retrieval, sweeps, digests, reconciliation, friction/strategy mining) excludes
   notes by construction with no per-caller opt-out. The ONLY retrieval path is
   `store.search_user_notes()`, which filters `metadata.owner_slack == asker` in the
   WHERE clause. `unrestricted=True` (the D-043 historical-access allowlist, i.e.
   Harrison) is the single exception, and callers must verify it via
   `historical_access.is_unrestricted`. `search_owned` refuses the user_note source.
2. **Notes are the user's own data, NOT canonical memory -- D-011 untouched.** Saving a
   note is not a canonical write; org-wide promotion is deliverable 2 and goes through
   Harrison's knowledge-review gate. Share intent today = private save +
   `share_requested=true` metadata + telling the user review is coming.
3. **PHI save matrix (deterministic, `user_notes.resolve_save_scope`):** PHI-flagged note
   text saves ONLY when `lex_phi_access.phi_allowed` passes (LEX custodian in LEX scope
   or DM); custodian DM saves are FORCED into the LEX store (session-capture rule);
   everyone else gets the standard PHI refusal and nothing is written.
4. **Channel containment on read:** channel asks retrieve only notes saved in that
   channel's entity scope (+FNDR, except LEX sub-entities which stay firewalled); a
   LEX-scoped note can never surface in a non-LEX channel reply. DMs see all owned notes.
5. **Labeling + cache-skip:** notes enter LLM context only under an explicit
   "ASKER'S PERSONAL NOTE from <date> -- present as their own note, not org-canon"
   header with a synthesis rule, and any response built on one sets
   `kb_meta["unstripped_personal"]=True` so it never enters the shared semantic cache
   (the D-043 invariant, reused and test-pinned).
6. **Save-time conflict check is advisory:** the canonical KB is probed at save
   (distance <= 1.05); a hit appends a heads-up to the confirmation but NEVER blocks the
   save -- the user may be righter than canon; the conflict rides the d2 drift sweep.
7. **Staged-write doctrine applies:** `cora_remember` and `cora_forget_note` refuse
   without confirmed=true; delete is owner-only and a non-owner delete is a no-op
   indistinguishable from a missing note (no existence leak).

**Why:** Phase 5 of the org-synthesis spec (Harrison-directed, design locked 2026-06-11):
the contribution flywheel -- each user can teach Cora directly with blast-radius-1 safety,
so Cora ACCEPTS knowledge instead of refusing it, without touching the Harrison-gated
canonical layer. NOT a sharding of the KB by user: question scope does not correlate with
user scope; notes are a thin additive overlay next to the entity partition + FNDR co-scan.
Shipped with 52 tests including the adversarial identical-query exclusion and the
"remember Harrison approved my raise" pin (saves fine, never surfaces to anyone else,
never presented as org-fact).
