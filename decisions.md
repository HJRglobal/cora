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

**SUPERSEDED (2026-07-03, audit W3-07):** the single 25s wrap became a per-tool
tier scheme. The current scheme is SIX tiers — 8 / 12 / 15 / 20 / 25 seconds plus
per-tool overrides (e.g. `cora_person_dossier` = 60s) — and the SOURCE OF TRUTH is
`_TOOL_TIMEOUTS` in `tool_dispatch.py` (default when unlisted = `_DEFAULT_TOOL_TIMEOUT`
= 15s). The older "8s fast / 15s default / 25s heavy" three-tier note (D-028 doctrine
row in the repo CLAUDE.md) is stale; read `_TOOL_TIMEOUTS` directly when tuning.

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

## D-033 -- (reserved -- numbering gap; decision-log namespacing) (2026-06-06)

**Decision:** No standalone cora-repo decision was recorded under D-033; the
sequence skipped from D-032 to D-034. This stub closes the phantom-number gap
(audit F-14) so future readers do not hunt for a missing entry.

**Namespacing (LOCKED 2026-06-16):** this repo's decision log and the Founder OS
decision log run two INDEPENDENT, colliding D-### sequences. Going forward, cite
repo decisions as **CORA-###** and founder decisions as **FNDR-###**; historical
numbers are NOT renumbered. Note that "D-033" in the FOUNDER log is a DIFFERENT
decision (the Drive large-file 3MB->15MB spill-file read pattern) and does not
apply here.

---

## D-034 -- Cross-entity firewall: deterministic pre-LLM keyword interception (code beats prompt; SQL-layer blast radius) (2026-06-06)

**Context:** Entity-scoped channels must never surface another entity's data. A
prompt-only instruction ("don't answer cross-entity") drifts: the model routes to
a tool and returns data before applying the scope check. This is the doctrine
cited 6x across the repo (it had no defining header until this backfill, F-14).

**Decision:** `src/cora/cross_entity_guard.py` -- a deterministic keyword
interceptor wired at two sites in `app.py` BEFORE any Claude API call (the mention
handler and the thread-follow-up path). Eight entity keyword dicts
(F3E/LEX/OSN/UFL/BDM/HJRP/HJRPROD/F3C); FNDR + HJRG are pass-through aggregators;
PAIRED_ENTITIES = {F3E<->F3C} (brand + nonprofit pairing is intentional). A
cross-entity question in an entity channel gets a complete refusal string,
pre-LLM, with zero tool calls and zero data. Keywords "energy drink"/"shopify"/
"dtc" were removed from F3E to avoid OSN false positives. Commits 9076b42 ->
3748203; tests `test_cross_entity_guard.py` + `test_cross_entity_firewall.py`.

**Doctrine (LOCKED, cited widely):** Prompt-only enforcement is insufficient for a
HARD requirement (security, privacy, blast-radius). Enforce in CODE at the
earliest intercept point before any LLM call. Same lesson as `sibling_guard.py`
(2026-05-24) and the personal-notes SQL-layer exclusion (D-049): the load-bearing
control lives at the data/boundary layer, never in the prompt.

**Reason:** Verified live -- an F3E question in #osn-leadership redirected in ~17s
with no tool call and no data. Immune to model drift.

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

---

## D-050 -- PHI save classifier: a named individual's billing/authorization/client-status is PHI in LEX scope even with no clinical keyword (2026-06-12)

**Context:** Live miss in `#llc-finance` (2026-06-12). Justin Moran (NOT a PHI
custodian) said "Cora, remember Bob Smith's billing authorization is pending."
`cora_remember` STAGED the save ("I'll save that... does that look right?")
instead of issuing the PHI refusal. Two faults: (a) `phi_guard.is_phi_risk`
returned False -- the base patterns key on CLINICAL / IDENTIFIER keywords
(diagnosis, care plan, client name, SSN, medication, AHCCCS), and "billing
authorization" tied to a named individual carries none of them, so
`user_notes.resolve_save_scope` took the non-PHI branch and never consulted the
custodian gate; (b) the gate ran AFTER the `confirmed` staged-write gate, so a
refusal could only fire post-confirm. Blast-radius-1 held -- nothing was ever
persisted (staged only, owner-only) -- but the refusal didn't fire.

**Decision (doctrine):**

1. **A personal name + billing / authorization / eligibility / coverage /
   claims / units / placement / client-status phrasing IS PHI in LEX scope,
   even with zero clinical keywords.** Tying an administrative term to a
   specific person reveals that the person is a Lexington care recipient --
   itself PHI. New `phi_guard.is_lex_billing_status_phi(text)`: admin-term +
   (possessive proper name OR care-recipient noun), or explicit client-status
   proximity phrasing.
2. **The augmentation is OPT-IN and scoped -- NOT folded into `is_phi_risk`.**
   Outside LEX, "authorization"/"billing" tied to a name is ordinary business
   (a retail buyer's PO authorization, a vendor's billing). `is_phi_risk` is
   shared by `session_capture` and `reconciliation_engine`; broadening it
   globally would over-quarantine. `user_notes.resolve_save_scope` applies the
   augmentation only when `_is_lex_scope(entity)` OR `is_dm` (a DM is
   LEX-eligible scope and would otherwise be a PHI-into-FNDR-store path). The
   base `is_phi_risk` stays the module-local name so existing monkeypatch tests
   are unaffected.
3. **The PHI/scope gate runs BEFORE the staged-write confirm gate.**
   `_tool_cora_remember` calls `resolve_save_scope` first, so a refused save is
   rejected on the FIRST tool call -- never staged as a "Saving to YOUR
   notes..." preview, never confirmed. The `cora_remember` description also
   carries a PHI nudge so the model doesn't self-preview a PHI-shaped LEX note.
4. **Fail-safe toward refusal in the most-regulated entity.** In LEX scope a
   benign false positive (a non-custodian's "client status changed") is refused
   with a graceful "raise it with Harrison" message -- acceptable. A custodian's
   PHI note still saves, forced into the LEX store.

**Why this is the same doctrine as D-034, applied to classifier sensitivity:**
hard PHI/security behavior must be deterministic and code-layer, and the
classifier must cover the administrative-but-PHI class, not just clinical
keywords. +19 tests including the exact bug-string regression, preview-stage
refusal, true-positive/negative sets, custodian-allowed, outside-LEX-not-flagged,
the finance channel-scope pin, and an owner-exclusion adversarial identical-query
pin. D-011 / D-044 untouched -- notes remain the user's own advisory data.

## D-051 -- 6/13 sweep: meeting-capture grounding, reply-formatter lists/code, #info-for-cora intake, dismiss-gate fix (2026-06-13)

**Context:** The 2026-06-13 sweep audit produced four changes -- B1 (scheduler
stagger), B3 (fireflies action-item grounding), B4 (reply_formatter), D1
(#info-for-cora intake). A 37-agent adversarial Workflow review of the diff
(7 finder angles -> dedup -> 1-vote verify) caught 6 real bugs the green
4132-test suite did NOT. Shipped a4c32cd (+ restart helper 3bc2788, TOM b4d0f5d);
full suite 4144 passed / 41 skipped; B4+D1 restarted live 2026-06-13 ~14:09 AZ
via the new deployment/restart-cora.ps1.

**Decisions (doctrine):**

1. **Roster-ground meeting-capture assignees by VALIDATION, never canonicalize.**
   _ground_and_filter_items keeps the PARSED assignee name when it confidently
   matches an org-roles person, and nulls it (unassigned) otherwise. Do NOT
   substitute the canonical org-roles name: downstream _resolve_assignee_gid
   matches against Fireflies attendee displayNames, and a legal name ("Jennifer
   Mortensen") will not substring-match a nickname displayName ("Jen Mortensen")
   -> silently orphaned task. Off-roster -> unassigned is the safe default;
   mis-assignment is the failure to avoid.

2. **Name matching must be ANCHORED -- no unbounded substring.** _match_roster_name
   uses exact-full / exact-first-name(unambiguous) / first-name-prefix>=3(unique)
   / fuzzy-0.88(unambiguous). The old unanchored `n in full_name` mapped short
   off-roster tokens to whoever contained them: "Lex"->Alex Cordova, "Ann"->Hannah
   Grant, "Al"->first-alphabetical, "Mort"->Jennifer Mortensen. Same family as
   D-034: a guard that fires confidently on a non-confident match is worse than
   no guard.

3. **Booleans from an LLM may arrive as strings or 0/1 -- normalize, never
   identity-compare.** `is_actionable is False` missed JSON `"false"` and `0`.
   Use a normalizer (False / 0 / "false"/"no"/"none"/"0" -> not actionable;
   missing/None -> actionable). Applies to any LLM-emitted boolean flag.

4. **#info-for-cora is a Harrison-gated knowledge intake, never an auto-write.**
   Channel messages route into knowledge_review.propose_update as a GENERIC
   pending item (surfaces in the 7am review DM; on thumbs-up the GENERIC executor
   posts to #hjrg-leadership -- NO canonical-memory auto-write; D-011 intact). PHI
   refused at intake: is_phi_risk always, plus is_lex_billing_status_phi (D-050)
   ONLY for a LEX-entity asker (scoped so a non-LEX business fact about a named
   buyer's PO authorization is not over-refused). Entity = asker's org-roles
   primary (FNDR fallback). Bot/edit/non-string messages ignored.

5. **knowledge_review Step 0 must NOT auto-dismiss a never-DM'd PENDING entry.**
   The 48h auto-dismiss is now gated on dm_message_ts (extracted, tested
   _auto_dismiss_stale_pending): only entries Harrison was actually shown and left
   unreacted for 48h are dismissed. A proposal created at an arbitrary time (an
   #info-for-cora note Friday evening, next weekday review Monday 7am, >48h later)
   was being dismissed BEFORE it was ever DM'd -- silent loss. Any source that
   proposes PENDING-without-DM (gap_autofill, friction, this intake) relies on it.

6. **reply_formatter also flattens markdown LIST markers + `inline code` / ```
   fences``` (line-anchored, so mid-line " - " and hyphenated words survive); the
   280-char cap stays LOG-ONLY (never hard-truncate -- truncation drops real
   answer text). Non-string assignee coerced to None on the empty-roster path.**

7. **deployment/restart-cora.ps1** is the reusable clean restart: import-smoke
   gate (never restart into a broken import) + doctrine-5 kill filter
   (\Scripts\cora.exe / cora.main) + single 3-process-instance verify. Activates
   bot-loaded code at HEAD without a ship script's commit/pytest logic.

8. **Scheduler: no two enabled Cora tasks share a clock minute in 03:00-09:00 AZ**
   (the weekly-health stagger alarm). restagger-morning-tasks-2026-06-13.ps1
   changes trigger START TIME only (preserves recurrence / Settings / Principal),
   idempotent. Surfaced a stray cowork-cora-drive-extractor at 04:00 (moved to
   04:05) -- possible stale sibling of Cora - Drive Sweep (06:00); open question.

9. **.env hygiene (reinforces D-022 + the dead-man-ping incident):** a stray
   `Klaviyo: pk_...` line (accidental paste; unused -- the only klaviyo reference
   in code is klaviyo.com in an email skip-list) tripped python-dotenv ("could not
   parse statement") on every startup; removed byte-safe (UTF-8 no-BOM). Every
   .env line is KEY=VALUE -- no label:value pastes, no BOM; watch the import-smoke
   for a dotenv parse warning after any .env edit.

**Process note:** the adversarial diff review found 6 confirmed bugs a green suite
missed (substring mis-assign, canonicalization GID regression, string-`false`
is_actionable, missing LEX-PHI gate at intake, never-DM'd auto-dismiss,
non-string-assignee crash). Lock: review significant diffs adversarially before
committing.

---

## D-052 · LEX meetings flow through Meeting Action Capture (scoped) + Fireflies ingest dedup (2026-06-14)

**Context:** Two changes to the Fireflies pipeline, bundled (they share a
restart). (1) Per Harrison directive (2026-06-14), Lexington OPERATIONAL
meetings should produce Asana action items instead of being skipped wholesale.
(2) Org-wide Fireflies rollout means several attendees' notetakers capture the
SAME meeting, ingesting near-identical transcripts as separate KB rows
(observed 6/13: Voyager/Copa x2, F3 Amazon Weekly x2 on 6/10).

**Decision (WI1 — LEX capture relaxed, SCOPED to the capture pipeline only):**
The blanket `entity == "LEX"` skip in `fireflies_action_extractor.run_action_capture`
is replaced by a scope check. This change touches the Fireflies meeting ->
action-item -> Asana pipeline ONLY — NOT Slack Q&A PHI behavior, the
reconciliation engine, the LEX PHI custodian gate, or any other surface.
Hard rails enforced in code:
  1. LEX-derived tasks route ONLY into LEX-scoped Asana projects. New
     `_resolve_lex_project()` resolves via the (entity-scoped) smart resolver,
     VALIDATES the result against `_known_lex_project_gids()` (union of LEX*
     entries in meeting-capture-projects.yaml + every project GID under any LEX*
     entity in asana-project-map.yaml), then falls back to the LEX catch-all.
     Returns None only if no LEX project exists at all -> the task is SKIPPED
     (never created outside LEX scope).
  2. A LEX digest posts ONLY to a LEX channel. `_ENTITY_CHANNEL` gained LEX /
     LEX-LLC / LEX-LLA / LEX-LTS (channel IDs from entity-channels.yaml; LLC ->
     #llc-leadership, the rest -> #lex-leadership GM). `_LEX_CHANNEL_ALLOWLIST`
     (built from those entries) is a hard check before any LEX post.
  3. cross_entity_guard + sibling_guard untouched + still enforced elsewhere;
     the capture pipeline never routes a LEX task to a non-LEX project/channel.
  4. Task title + notes are PHI-scrubbed (`phi_guard.scrub_lex_phi`, keeping
     staff names from org-roles); LEX notes omit the raw action-item dump
     entirely (minimum-necessary). Fail-safe: a scrubber exception keeps the
     task but truncates + flags it "[review for PHI]" rather than dropping it.
  5. ENTITY TOGGLE — LEX-LBHS is EXCLUDED by default (42 CFR Part 2; a BAA does
     NOT waive Part 2). Scope lives in `data/maps/meeting-capture-lex-scope.yaml`
     (enabled + included/excluded sub-entities); excluded always wins; one-line
     change to flip LBHS on later. A clinically-titled LEX meeting is STILL
     skipped (existing `_is_phi_meeting` guard, kept as belt-and-suspenders).
Scope is FAIL-SAFE OFF: an unreadable scope config disables LEX capture (reverts
to the old skip-all behavior). Sub-entity is resolved from Fireflies attendees
via `_tag_fireflies_sub_entity` (untagged GM-level -> "LEX").

**Decision (WI2 — Fireflies ingest dedup):** `backfill()` now fetches the full
window, then collapses duplicate-meeting transcripts before chunk/embed, keyed
on `(meeting_link, start_time)` within +/-5 min (fallback when no meeting_link:
normalized_title + participant-email set + start window). The most-complete
copy (sentence count, then summary length, then title length; smallest id on
tie) is kept; the rest are dropped. `meeting_link` was added to the transcripts
GraphQL query. A persistent ledger (`data/state/fireflies-dedup-ledger.json`)
records which ids collapsed into which canonical: re-running sync drops any
previously-collapsed id immediately (never resurrects it). Recurring meetings
that reuse one link are kept separate by the start-time window.

**Basis:** Lexington BAA confirmed in place (Emily + legal 2026-06-09); Harrison
is sole authority on PHI access posture (founder doctrine 2026-05-21).

**Activation:** Both changes are script-side; each scheduled task spawns a fresh
process that imports the current on-disk source, so no bot restart is required.
WI1 runs in the "Cora - Meeting Action Capture" scheduled task — verified
2026-06-14 to be **ENABLED** and firing hourly (last run 17:00 AZ, result 0),
which CONTRADICTS the founder docs that still call it "disabled" (stale —
flagged to Harrison). WI1 therefore goes LIVE at the next hourly fire (~18:00 AZ
2026-06-14): LEX operational meetings begin producing scoped, PHI-scrubbed Asana
tasks. WI2 runs in `cowork-cora-kb-sync-fireflies` (3:30am AZ daily); it
activates at the next fire. Bot restart was HELD: a concurrent Code session's
uncommitted attachment-filer WIP (drive_connector.py / attachment_filer.py /
run_attachment_filer.py / filer_ledger.py) is on disk, and a restart would
activate it; my commit (c2c4088) was scoped to my own files only.

**Tests:** `tests/test_phi_scrubber.py`, `tests/test_lex_meeting_capture.py`,
`tests/test_fireflies_dedup.py`, plus updated `tests/test_meeting_action_capture.py`.
Full suite 4,200 passed / 41 skipped.

---

## D-053 · Email attachment auto-filer is crash-safe + content-aware idempotent (2026-06-14)

**Context:** A 2026-06-14 Drive-hygiene cleanup found the auto-filer creating
many byte-identical duplicates (the single OSN Amended Personal Guarantee PDF
had been filed 6× across `legal/` and `contracts/`, under 4 different names).

**Root cause (the prompt's "no ledger / bad naming" diagnosis was wrong):**
The filer ALREADY had a message-id dedup dict (`data/cache/filed-message-ids.json`)
and `upload_file()` ALREADY deduped by exact name. Both failed because:
1. The state dict + per-account watermark were saved ONLY at the very end of
   `run_filer()`. The Task Scheduler job's 15-min `ExecutionTimeLimit` SIGKILLed
   the process before that save — every run, since 2026-05-28. So the watermark
   froze at May 28, every 4-hourly run re-scanned ~2.5 weeks × 12 inboxes
   (500-750 re-files/run), which guaranteed the next 15-min kill: a death
   spiral. `filed-message-ids.json` never even existed on disk. Evidence:
   `"Watermark advanced"` logged in-loop daily but `"Total filed"` (logged after
   `run_filer` returns) appeared 0× in any log; the 14:00 run died at exactly
   14:15:01.
2. The same document arrives via DISTINCT emails (an original Dropbox-Sign
   notice + a "Fwd:" of it) with different Message-IDs + Dates, classified
   independently by the LLM into different names/folders. Neither a message-id
   ledger nor a same-name Drive check can dedup that — only content (md5) can.

**Decision — idempotency is crash-safe + content-aware + persisted incrementally:**
- Two append-only JSONL ledgers in `data/state/` (`filer_ledger.py`), loaded once
  per run into memory and appended the instant something is filed → a kill loses
  at most the in-flight line:
  - **content ledger** keyed on **md5** (matches Drive's `md5Checksum`, so one
    value backs the local ledger, the in-folder Drive backstop, and `--reconcile`).
    Folder-agnostic: the same bytes are filed once regardless of email/name/folder.
  - **message ledger** keyed on `rfc_message_id` (falling back to `gmail:<id>`
    when the header is absent — the old code short-circuited on an empty rfc id
    and silently never deduped). Lets a re-scanned message skip re-classification.
- `upload_file()` gains a second dedup layer: same `content_md5` under a
  DIFFERENT name in the target folder → skip (was name-only).
- Watermark advances + is **saved per account, immediately** (not once at
  end-of-run) — this is what breaks the kill-before-save spiral. It advances
  whenever the listing succeeded; per-message file errors no longer freeze it
  (the ledgers make a re-scan safe), so one bad attachment can't pin a
  watermark forever again.
- `run_filer()` **self-bounds** at `EMAIL_FILING_RUN_BUDGET_SECONDS` (default
  780s/13min) and exits cleanly, persisting per-account progress; the next run
  resumes. Doctrine reaffirmed: script-side self-bounding is the real control;
  the Task Scheduler limit only backstops it. Task limit raised 15→20 min for
  catch-up headroom.
- `--reconcile` (read-only) seeds the content ledger from files already in Drive
  so the next run dedups against the canonical copy left after a manual cleanup,
  regardless of which folder the LLM would pick. Run once after deploy.

**Naming:** the date prefix already came from the email Date header (correct);
the LLM's description/folder variance is now non-load-bearing because md5 dedup
catches duplicates regardless of name. No separate classification cache needed.

**Gmail label:** NOT re-introduced. The prompt asked to apply a `Cora-Filed`
label, but commit 396d8e4 deliberately removed it for invisible JSON dedup. The
crash-safe JSON+md5 design fully fixes the bug without writing to anyone's
mailbox, so 396d8e4 stands.

**Activation:** Scheduled task spawns a fresh process per run → the fix went
live on disk immediately; NO bot restart needed (the filer is not the bot). Verified
live in the 18:00 AZ 2026-06-14 run: harrison's watermark advanced off May 28
and saved per-account; the OSN guarantee Fwd hit the new md5 backstop
(`Drive md5 dedup … skipping upload`) and the duplicate Dropbox-Sign email hit
the content ledger (`Content already filed … skipping`) — zero new dupes.
`--reconcile` then seeded 488 hashes (531 total). Remaining 11 accounts catch
their watermark up over the next few 4-hourly runs.

**Tests:** `tests/test_filer_ledger.py` (22) + `tests/test_attachment_filer.py`
(34): file-once-then-zero, same-content-different-message, md5 preseed skip,
deterministic date-from-header, dry-run writes nothing, per-account watermark
save, list-failed/budget-hit don't advance, reconcile seeds + dedups,
`upload_file` name/md5/no-match. Full suite 4,234 passed / 41 skipped.

**Follow-up (Harrison, optional):** re-run `deployment\setup-attachment-filer-task.ps1`
from elevated PowerShell to apply the 15→20-min ExecutionTimeLimit (the rest is
already live + self-healing).

---

## D-054 · Meeting action capture flips PUSH -> PULL (user-initiated, staged-write) (2026-06-18)

**Context:** A Track-A reliability item under the locked North Star
(`_shared/projects/cora/2026-06-18_fndr_cora-north-star-and-two-track-plan.md`).
Demi (#bdm-leadership) asked Cora to "take these 14 tasks out of Asana" and
"don't auto-capture our calls" -- the hourly "Cora - Meeting Action Capture"
task was auto-creating + auto-ASSIGNING Asana tasks from every meeting (a
decision-MAKER behavior). This embodies the North-Star invariant
**decision-SUPPORT, not decision-MAKER**. SUPERSEDES the lighter
`capture_mode: auto|opt-in|off` idea (triage Conv-2C / North-Star §5-A.4): no
entity should get silent auto-create; pull is the cleaner answer.

**Decision -- retire the push, add a PULL tool:**
- New global-core tool `meeting_action_items` (`src/cora/tools/meeting_actions.py`,
  registered in tool_dispatch). A meeting ATTENDEE asks Cora for a meeting's
  summary + the action items meant for THEM; Cora returns them (read-only
  preview); the user confirms which to create; Cora then creates ONLY those, as
  tasks assigned to the ASKER, via the staged-write `confirmed=true` gate.
- The hourly "Cora - Meeting Action Capture" task is DISABLED
  (`deployment/disable-meeting-action-capture-2026-06-18.ps1`; added to
  `scheduled-task-state.yaml` disabled list; the D-052 "it is ENABLED" pin +
  its nightly-health-check regression test are INVERTED). The extractor module
  + its YAML maps STAY -- they are the reuse source for the pull flow, not dead
  code. **This globally retires auto-create.**
- Fireflies KB ingest + recall (`cowork-cora-kb-sync-fireflies`) are UNTOUCHED:
  "recall any item from any meeting" stays the existing entity-scoped,
  PHI-guarded Cora Q&A path.

**Security model (the tool self-enforces -- entity-scoping is a perf hint, not a
boundary):**
- ATTENDEE GATE (primary): the resolution window is fetched by the asker's own
  email (`participant_email`), and preview + confirm + the transcript_id-direct
  path each re-verify attendee membership. A non-attendee gets nothing.
- CHANNEL/DM SCOPE GATE applied to EVERY candidate BEFORE a pick-list is built
  (`_visible_meetings`): LEX meetings only in a LEX channel / a LEX person's DM;
  a specific-entity meeting only in that entity's channel, a founder/HJRG
  channel, or any DM. An empty filtered list names nothing.
- LEX RAILS carried forward verbatim from D-052: a meeting is LEX if ANY of
  title-classifies-LEX / a NAMED LEX lead attends / an attendee email is on a
  Lexington DOMAIN (closes the generic-title-Jen/Aaron-meeting leak the
  name-only detector missed). LEX capture enabled + sub-entity in scope
  (LEX-LBHS / 42 CFR Part 2 EXCLUDED, most-restrictive-wins) + clinical title
  skipped + title/summary/items/due PHI-scrubbed + LEX-only project routing
  (None -> skip, never create outside LEX scope). cross_entity_guard +
  sibling_guard (pre-dispatch in app.py) unchanged.
- Asker creates only their OWN tasks (items owned by another on-roster person
  are excluded; off-roster-named + unowned are claimable). Confirmed creates are
  integrity-checked against the meeting's (scrubbed-for-LEX) action-item text so
  fabricated / cross-meeting text can't be persisted.

**is_dm wiring (found + worked around):** the QA tool loop threads
`channel_name` but NOT `channel_id` into `dispatch()`
(`_dispatch_tools_parallel`), so `_channel_id` is empty for QA-loop tools and
`is_dm` would be permanently False. Threading `channel_id` globally was REJECTED
-- it would also activate `financial_get_cashflow`'s dormant Slack file-upload
(it reads `_channel_id`) and `cora_remember`'s dormant DM-PHI path. So
`meeting_action_items` derives `is_dm` from the already-threaded
`_channel_name == "dm"` signal (set at app.py for DMs). The broader latent gap
(cora_remember DM-PHI + financial upload both dormant in the QA loop) is flagged
for Harrison, NOT fixed here (out of scope; would need its own review).

**Two-Cora future:** the LEX half of this tool RELOCATES to the isolated
BAA LEX-Cora at the North-Star split (same logic, different instance; it reuses
LEX rails that already live in this Cora -- adds no split cost). NOT throwaway.

**v1.1 (noted, not built):** a proactive post-meeting DM offer to each attendee
("here are your candidate items -- want me to create any?") to drive adoption
while preserving the confirm gate. Residual (shared with the retired push path):
a LEX-adjacent program meeting with NO Lexington-staff signal (no LEX title /
named lead / Lexington domain -- e.g. a probation budget class organized by a
non-LEX staffer) still classifies non-LEX; closing that needs content-based PHI
detection (a Track-B item).

**Process:** two adversarial diff reviews (D-051). Review 1 (4 lenses) found 2
HIGH confidentiality defects (pick-list bypassed the scope gate; LEX detection
was title-only) + a CRITICAL-tagged is_dm-wiring gap (fail-safe) + MEDIUMs --
all a green suite missed; all fixed. Review 2 (3 lenses, on the remediation)
confirmed the core fixes CLOSED + found 3 fail-safe residuals (the
email-domain LEX gap, a scrub-vs-raw match drop, an off-roster docstring
mismatch) -- all fixed. Doctrine reaffirmed: a pick-list / enumeration surface
needs the same scope gate as the single-item surface; LEX detection must use
participant DOMAIN + name, not title alone; a content-integrity check over
PHI-scrubbed text must compare like-for-like (scrub both sides).

**Activation:** BOT-LOADED (new tool in tool_dispatch) -> restart REQUIRED.
Deployment order (per review): run the disable `.ps1` FIRST (or in the same
window), THEN restart -- so the push task isn't still firing after the pull
tool goes live. Live smoke after restart: one non-LEX user pull + one LEX user
pull (correct PHI/scope). Full suite 4,749 passed / 42 skipped.

**Tests:** `tests/test_meeting_actions_pull.py` (helpers: scope/LEX gate,
classify w/ domain signal, dedup, attendee gate, item-match; preview:
pick-list scope filter, LEX/cross-entity not enumerated, DM signal, scrub;
confirm: staged-write, attendee/scope/LEX re-check, content-integrity,
LEX-only routing, LBHS exclusion, cap) + inverted
`tests/test_nightly_health_check.py` pin.

---

**Follow-up (`96cbdbe`, 2026-06-18) -- date/ordinal-aware resolution + relay grounding.**
A live LEX smoke (STEP-0 logs) showed the pull tool FIRED on all 3 pulls but never
resolved (no PREVIEW line): the preview path used `_match_query` (TITLE substring
only), so a date follow-up ("Lexington Progress June 18") didn't match the title and
the model then FABRICATED "the last meeting was June 4." Routing was fine -- the gap
was resolution + relay-grounding, not a missing tool call.
- **Fix A:** new `_extract_selectors` (dates: ISO / month+day / m/d / today-yesterday /
  "the 18th"; ordinals: "first/second/last", bare "2nd"=position) + `_resolve_meetings`,
  replacing the title-only match in the preview path. Prefers the FULL-query title
  match (so an in-title date like "Q2 6/30 Forecast" isn't hijacked), unions the
  selector interpretation, and returns a PICK-LIST when they disagree -- it NEVER
  silently resolves the wrong meeting, and a real-but-unmatched title is not replaced
  by a date/position guess. Explicit dates match the UTC-label day (== what the
  pick-list shows); relative dates match UTC or AZ; ordinals select within the
  displayed cap. AZ = fixed UTC-7 (ZoneInfo raises on this host).
- **Fix B (relay):** on a resolution miss WITH pullable meetings present, return the
  real scope-filtered visible pick-list so the model relays actual titles/dates
  instead of inventing one; the `meeting_action_items` tool description now forbids
  calendar/KB substitution or fabricating a meeting date for this query class.
- D-051 adversarial review (4 lenses) caught a HIGH wrong-meeting substitution + the
  dual-date / ordinal-cap / "2nd"-vs-"second" MEDIUMs -- all remediated.
  `test_meeting_actions_pull.py` -> 113 tests; follow-up suite green.
- Doctrine: title-substring resolution silently misses date/ordinal follow-ups -- a
  conversational resolver must parse dates + ordinals AND fall back to the real
  pick-list rather than let the model fabricate a meeting/date.
- Ship state: the CORE flip (70c8365) is already merged + restarted (live ~18:51 UTC
  2026-06-18); this follow-up needs its OWN merge + restart to go live (the live bot
  has the core flip but not the date/ordinal fix yet). Bundle the restart with the
  polar MCP-auth fix so the working tree carries both.

**Follow-up 2 (`b5da8ae` + `96ec591`, 2026-06-18) -- deterministic project-scoped dedup
(the smoke duplicate).** The 2026-06-18 live smoke confirmed the pull CREATED a duplicate
of a task the retired push had already made (Asana `1215849728768964` vs `1215697338246453`,
identical name, same `[F3E] Operations -- General` project), despite the 7-day dedup. Root
cause: `find_recent_duplicate_task` resolves existing tasks via Asana TYPEAHEAD, which is
fuzzy/prefix-oriented and unreliable for the long descriptive names action items carry -> it
returned None -> the create proceeded.
- **Fix:** before creating, a DETERMINISTIC exact-name check against the TARGET project's open
  tasks (`asana_client.get_project_tasks`, cached once per project per confirm-call) via a
  shared `_dedup_key` (collapse whitespace + truncate-to-`_MAX_TASK_LEN` + lowercase, applied to
  BOTH sides). The typeahead lookup stays a SECONDARY workspace-wide net. Both FAIL OPEN. A match
  is reported transparently ("already had an open task -- not duplicated"), never a silent skip.
- **A 3-lens D-051 review found a HIGH + 2 MEDIUM, all fixed (then a clean 2-lens re-review):**
  HIGH -- the retired push scrubbed-AFTER-truncating (a long LEX name could exceed 160) while the
  pull scrubs-then-truncates (<=160), so the stored names diverged and the exact match missed; the
  `_dedup_key` both-sides truncation closes the length/order divergence (residual: PHI straddling
  char 160 can still differ in content -- narrow, LEX-only, transient since the push is retired,
  fail-open). MEDIUM -- an Asana create error was misreported as "no project to land in" (now a
  separate `create_failed` bucket with honest wording). MEDIUM -- the project scan capped at 500
  open tasks with no ordering (now logs on cap-hit; best-effort, typeahead backstop). NIT -- the
  same item selected twice in one call now creates once (in-call `created_keys` guard).
- Doctrine: dedup by exact name must compare against an authoritative project task list, not Asana
  typeahead (unreliable for long names); when two code paths build a name in different
  scrub/truncate orders, normalize BOTH sides for comparison. Branch `claude/meeting-actions-dedup-fix`;
  bot-loaded -> needs its own merge + restart. Pull suite -> 120 tests; full suite 4,832 passed.

---

## D-055 - Track A P0 reliability/privacy block (WS1-WS4) (2026-06-19)

**Context:** First North-Star Track A (reliability) build off `main`@`23e4bf6`, in an
isolated worktree. Four workstreams from the 20-report synthesis P0 block.

**Decisions / what shipped (branch `claude/track-a-p0-reliability`, 6 commits, NOT merged):**
- **WS1** - Cora's own build/audit/forensic/code-prompt docs (under `_shared/projects/cora/`
  + cora-build session captures) were ingested as `static_md`/FNDR and RAG-narrated as a
  fabricated "diagnostic" (the Minute Press miss). New `kb_exclusions.py` shared predicate
  (folder + narrow filename rule) wired into `incremental_sync_static.py`; new read-only
  `cora_self_check` tool + prompt nudge routes status/diagnose queries to LIVE state, never
  the KB; gated purge script (live dry-run: 921 chunks/152 files + the fabricated note
  `0ca8e649`).
- **WS2** - ONE shared LEX detector `fireflies_connector.classify_lex_meeting` used by BOTH
  ingest + the capture pull tool. Signals: LEX email-domain, named lead, LEX title kw
  (word-boundary `\blex-`), self-sufficient CARE (hcbs/dta/day-treatment/anger-management) +
  DDD + clinical titles, and CORROBORATION-required ambiguous program titles + known-organizer
  + .gov. LEX program/client/DDD/clinical/LBHS are HARD-EXCLUDED from KB ingest (decided:
  exclude, not scrub); plain LEX ops still ingest LEX-scoped. Closes the "1st Budget Class"
  (probation, alina@hjrglobal.com organizer, maricopa.gov clients) leak. Gated purge (88 chunks).
- **WS3** - the general staged-write `asana_create_task` had a confirm gate but NO dedup /
  cross-entity validation / LEX scrub (the CREATE-bypass). Added `_plan_asana_create` (drop
  cross-entity project_gid; no-orphan -> entity catch-all; LEX channel -> PHI scrub + LEX-only
  project, fail-CLOSED on an unverified project) + exact-name dedup fail-OPEN. INVARIANT CLAMP:
  every adjustment SURFACED in the unconfirmed preview, never silent. FNDR/HJRG no-op.
- **WS4** - cross-entity vendor/contact fallback (the genuine Minute Press fix): when an
  entity-scoped search is empty AND the asker has cross-entity authority, search the wider
  portfolio (reusing per-entity `kb.search`), confidence-LABEL, EXCLUDE LEX for non-custodians
  (two layers). Replaces a confident "no record" with a labeled wider result.

**Doctrines:**
- Cora's own build/audit docs are NOT org knowledge - keep them out of the KB or RAG fabricates
  self-"diagnostics"; status queries must read LIVE state (heartbeat/KB-counts/watermarks).
- A LEX meeting classifier must split CARE/clinical-specific titles (self-sufficient) from
  business-AMBIGUOUS program titles (corroboration-required) - the budget-class root case is
  caught via organizer+gov / a Lexington-domain attendee, not the bare program title.
- A general staged-write tool that can be reached in any channel must carry the SAME
  entity-routing + PHI rails as the specialized path, fail-CLOSED for LEX, and SURFACE every
  routing/scrub adjustment at the confirm gate (decision-SUPPORT, not silent decision-MAKER).
- PHI fail-CLOSED; dedup fail-OPEN; a config loader must coerce malformed shapes to defaults so
  a hand-edit never crashes the nightly ingest.

**Process:** two adversarial D-051 reviews (14-agent, then 4-agent on the remediation) found 8 +
1 confirmed real findings the green suite hid - ALL fixed before any merge. Full suite 4,888
passed / 3 pre-existing env-failures / 42 skipped. Bot-loaded changes -> Harrison merges + ONE
coordinated restart. All KB purges (`purge_cora_internal_kb.py`, `purge_lex_program_kb.py`) are
dry-run-default + Harrison-gated.

## D-056 - Track A P1 reliability block (WS11 + WS-BACKUP + WS5) (2026-06-19)

**Context:** Second Track A build, off the just-merged `main`@`3d1ffc8`, isolated worktree
`cora-wt-track-a-p1` (branch `claude/track-a-p1`, 4 commits, NOT merged - Harrison merges after
the P0 restart). Three workstreams from the P1 block.

**Decisions / what shipped:**
- **WS11** (`shopify_client.py`) - the F3E inventory snapshot was internally contradictory:
  Shopify oversell returns NEGATIVE `inventory_quantity`, and multi-variant SKUs were counted as
  distinct SKUs, so a brand could read "all stocked, low" while units > 0. `get_inventory_status`
  now clamps qty to >= 0, dedups by variant `id`, and reports `unique_skus` vs `variants` +
  `total_units` with a consistency guard; `get_inventory_by_location` clamps `available` >= 0;
  `format_inventory_for_llm` stops mislabeling variants as SKUs. NOTE: the merch->beverage filter
  is on the separate unmerged `986b0eb` and overlaps `shopify_client.py` ADDITIVELY -> merge WS11
  FIRST, then `986b0eb`.
- **WS-BACKUP** (`backup_logs.py`) - the daily Drive backup was copying the ~6 GB regenerable
  `cora_kb.db` every run (the KB is rebuildable from source connectors). `cora_kb.db` is now
  EXCLUDED by default (`--include-kb` opt-in); `verify_offsite` reframed so the daily run still
  FAILS LOUD on a genuinely empty/broken backup but PASSES when the small DR set landed with the
  KB excluded; the small stateful set (feature DBs, encrypted secrets, jsonl, logs) is still
  backed up. New `deployment/kb-rebuild.md` documents the rebuild path with real script names.
- **WS5** (`asana_client.py` + `tool_dispatch.py`) - new conversational write tools:
  `asana_complete_task` (staged), `asana_delete_task` (confirm-gated, IRREVERSIBLE, warns
  permanent), and `follower_names` on create (resolve names -> `add_task_followers` after create).
  Both action tools resolve a task ONLY within the asker's OWN open tasks, on BOTH the gid and
  name paths (founder + FNDR/HJRG exempt). LEX-safe labels on output.

**Doctrines:**
- An inventory/quantity summary must be SELF-CONSISTENT: clamp provider oversell to >= 0 and
  separate the unique-SKU count from the variant/line count, or the rollup contradicts itself
  (all-zero while units > 0).
- Do NOT back up a large REGENERABLE artifact (the vector KB) on the daily DR run - back up only
  the small stateful set + secrets, and keep the loud-on-empty verify; document the rebuild path.
- A conversational write tool that can act on a task by raw gid MUST verify the gid is one of the
  ASKER's own open tasks - a shared-workspace PAT exposes every teammate's gid via
  `get_user_tasks`, so a confirm gate alone lets a non-founder complete or PERMANENTLY DELETE
  anyone's task. Ownership scoping belongs on the gid path too, not just the name path. (This was
  the P1 review's HIGH finding; the founder + FNDR/HJRG retain cross-entity authority by design.)
- A passing test can still be a coverage hole: an `assert A or B` where `B` is always true never
  exercises `A` (the tautological follower assertion). Pin both call arguments unconditionally and
  add the NON-privileged actor's negative case.

**Process:** one adversarial D-051 review (3-lens) over the P1 diff confirmed the gid-ownership
HIGH + two test gaps; all fixed on `8b4557d` before push. Full suite 4,909 passed / 3 pre-existing
env-failures / 42 skipped. Bot-loaded changes -> Harrison merges (WS11 then `986b0eb`) + the same
coordinated restart as P0. KB purges remain dry-run-default + Harrison-gated (handed off as an
exact elevated stop -> dry-run -> apply -> reclaim -> restart -> smoke sequence).

## D-057 - WS1-DRIVE: the self-diagnostic KB leak ran through drive_sweep, not static_md (2026-06-19)

**Context:** WS1 (D-055) excluded Cora's build/audit docs from the static_md path and purged
by source_id PATH. Post-merge verification of the LIVE KB showed the static_md path held ZERO
`_shared/projects/cora/` content (the P0 "921 + fabricated note" was a synthetic test DB,
never live), so the purge Harrison ran found 0. The ACTUAL leak: `drive_sweep` walks Harrison's
whole Google Drive (the Founder OS lives there) and ingests `cora-rebuild-execution-log.md`,
`cora-forensic-findings-report.md`, `cora-exec-summary.md` ("Forensic Audit Executive Summary"),
`CORA_IMPROVEMENT_BACKLOG.md`, the north-star plan, code-prompts, and raw `cora-*.log` files
under a Drive-FILE-ID source_id with the filename in `title` -- which no PATH rule can see.
~329-480 chunks of exactly the self-diagnostic material the fix exists to remove, live + re-ingesting.

**What shipped (branch `claude/track-a-drive-sweep-leak`, 4 commits, NOT merged):**
- `kb_exclusions.is_cora_internal_title()` matches the stored filename (the only signal on a
  Drive-copy source_id). Both edges anchored: keywords with `\b`, the `cora` token with a left
  lookbehind `(?<![a-z0-9])` (never a mid-word substring -- pecora/decora/mancora/incora spared).
  Underscores normalized `_`->`-` before matching so `\b` works across both separators
  (`CORA_IMPROVEMENT_BACKLOG` matches). A `cora-*.log` rule. A `_LEGIT_FAMILY_RE` NEGATIVE guard
  (reference|wishlist|mapping|f3-monitor-privacy) that spares those families EVEN with a soft
  keyword suffix, but NOT when a STRONG build keyword is also present.
- `drive_sweep` BOTH ingest loops guarded with `is_cora_internal_title(filename, broad=True)`
  (fail-safe to the WIDER exclusion at ingest: over-excluding Cora's own ops docs is harmless,
  under-excluding re-opens the leak). Stops re-ingestion on the next nightly sweep.
- `purge_cora_internal_kb.py` `target_drive_doc_copies()` scans `drive_sweep`+`drive_asset` by
  title OR source_id; `--scope targeted|broad`; writes a FULL file manifest to `logs/`.
  Live dry-run: targeted 387 / broad 496 chunks.

**Doctrines:**
- A path/folder-based KB exclusion is INCOMPLETE when the same docs are also swept from Drive
  under file-id source_ids -- match the stored TITLE (filename) too.
- An ingest guard should FAIL-SAFE to the wider exclusion (over-exclude is harmless; under-exclude
  re-opens the leak); a one-time DESTRUCTIVE purge should stay CONSERVATIVE by default + opt-in for
  the full clean + write an auditable manifest (the inline log caps at 40).
- `\b` is not a boundary at `_` (underscore is a word char) -- normalize `_`->`-` before anchoring,
  or underscore-named docs silently escape. And `\b`/`cora[-_]` has no LEFT boundary -- anchor the
  token edge with a lookbehind or "cora" matches mid-word (pecora/Cora-the-person -> over-delete).
- Over-deletion is the cardinal sin on a one-time destructive op: prefer a documented UNDER-match
  (space-named / keyword-first Cora docs; reversible at ingest, backstopped by cora_self_check+WS4)
  over widening matches in a way that risks deleting legit data.

**Process:** THREE adversarial D-051 reviews (8 + 6 + 5 agents) each confirmed a real HIGH the
green suite hid -- (1) targeted missing audit/review/sweep, (2) the `\b`-underscore under-match +
targeted ingest scope, (3) the missing cora-token left boundary -- all fixed before merge.
Validated against ALL 75 live drive_sweep cora-token titles (broad catches 69; spares only the 4
legit families + a fireflies note + space-named human notes). Full suite 4,949 passed / 3
pre-existing env-failures / 42 skipped. Bot-loaded? NO -- both surfaces are script-side (drive_sweep
is a scheduled task; the purge is a script). No bot restart needed. Harrison merges + re-runs the
purge (recommended `--scope broad`, dry-run manifest reviewed first).

## D-058 - Track A P1-tail + P2 block (WS6/7/8/9/10/12/13/15) (2026-06-20)

**Context:** Third Track A build off `main`@`de519b5` (pushed to origin first), isolated
worktree `cora-wt-remainder` (branch `claude/track-a-p1tail-p2`, 10 commits, NOT merged).
The P1-tail + P2 remainder from the 20-report synthesis. WS14 / WS16 / the is_dm-channel_id
gap are NO-CODE deliverables (verify-only / D-011 draft / PHI-review-defer) and are handed
off, not built. WS17-A/B are a later session.

**Decisions / what shipped:**
- **WS8** (`drive_connector.py` + `photoroom_client.py`) - PREVENTIVE Drive guardrails:
  `safe_drive_create` fails CLOSED if a create has no non-empty-string `parents` (no
  My-Drive-root write) or sets `permissions` inline (no anyone-with-link); wired at all 3
  create sites. A repo grep-guard test bans `anyoneWithLink` / `type:anyone` / a
  permissions-create call anywhere in src+scripts (the real public-share vector the body
  check can't see; patterns assembled from fragments so the test never matches itself).
- **WS9** (`attachment_filer.py` + `inventory_client.py` + `canonical-files.yaml`) -
  VERIFY-FIRST killed the "root-drop" premise (invalid classifications are SKIPPED, never
  root-written; D-053 dedup already shipped). Residuals only: a pinned canonical inventory
  fileId (deterministic, fail-OPEN to name-search) + the always-named-folder invariant
  (comment + tests). Doc-type map + forwarded-handling DEFERRED.
- **WS6** (`qbo_client.py` + `tool_dispatch.py`) - the conversational P&L now LABELS the
  basis QBO actually rendered (`Header.ReportBasis`), never fabricated, and passes an
  optional per-entity override `_ENTITY_PNL_BASIS` that ships EMPTY (INVARIANT CLAMP: never
  blanket-Accrual; LEX-LLC is cash, LBHS differs). No lex.md band-aid existed to retire
  (premise wrong). QBO stays READ-ONLY.
- **WS7** (`gsheets_financials.py` + `scripts/write_cashflow_snapshot.py` + setup ps1) - the
  daily-brief cash was DEAD; built a Cowork-readable surface (Harrison-locked: NOT a Cash
  Pulse re-enable). New `ending_cash_series` + `ending_cash_outlook(weeks)`; a standalone
  scheduled writer dumps a labeled, source-opaque JSON to `00-Founder/_cash-snapshot/` on
  the Drive mount; fail-SOFT (read OR write error leaves the prior snapshot + exits nonzero,
  no silent stale fallback).
- **WS13** (`fireflies_connector.py`) - multi-organizer dedup: cluster on EITHER link OR
  title+participants, but a title-only cross-link merge requires a TIGHT window
  (`_TITLE_MERGE_TOLERANCE_SEC` 180s) and cluster keys are the ANCHOR's only (no
  accumulation), so two genuinely-different same-title/same-attendee meetings can't merge
  and a borrowed link can't transitively bridge. Empty-recording guard unchanged.
- **WS12** (`asana_filters.py` + `asana_client.py` + reconciliation + nudge script) - one
  shared system-reminder filter applied at the `get_user_tasks` SOURCE (every caller gets
  clean lists), filtered PER PAGE so noise never consumes the `max_tasks` budget, curly-
  apostrophe-normalized, `name` force-included in `opt_fields`; reconciliation requests
  narrow fields (drops notes/projects/memberships it never reads).
- **WS10** (`run_asana_hygiene_nudges.py` + `attach_capture_custom_fields.py` +
  `asana-architecture.md`) - VERIFY-FIRST confirmed the nudge lane is already the sole owner
  (Make 4768887 deactivated, shared throttle, closed-task guard). Added Tier-0 importance
  (compliance/revalidat/p0/urgent/emoji; bare audit/deadline DROPPED) that bypasses the
  Tier-1 caps bounded by MAX_TIER0 + MAX_TIER0_PER_USER, with the cap-decision BEFORE the
  expensive kb-signal/closed-task-guard so a backlog day issues no unbounded Asana reads;
  cap-cut tasks logged to `hygiene-deferred.jsonl` (informational, auto-recovers next run).
  Custom-field attach extended to the 2026-06-08 projects + a `field_target_projects` list
  (apply stays Harrison-gated). The design doc gained the nudge-lane section.
- **WS15** (`user-aliases.yaml`) - Sara Fonseca alias (name resolution was failing).

**Doctrines:**
- A KB exclusion / source filter applied at a SHARED source helps every consumer at once -
  but verify no existing caller depends on the removed items (reconciliation reads only
  name/gid/permalink/assignee, so the narrow opt_fields drop nothing) and force-include the
  field the filter keys on (`name`) so a narrow caller can't fail it OPEN.
- A dedup/merge over a fuzzy identity must prefer UNDER-merge (a harmless duplicate) over
  OVER-merge (irreversible data loss): require a tight time window + anchor-only keys for a
  cross-link title match; never accumulate keys that let a later copy transitively bridge.
- A freshness/availability flag must fail CLOSED on indeterminate input (an unparseable week
  -> stale, not fresh), and two figures on the same surface must share one precedence
  (headline mirrors the outlook anchor) or they will disagree mid-week.
- A per-run cap that drops work needs an IMPORTANCE tier so a compliance/P0 task is never
  starved - but the tier regex must be high-signal (bare "audit"/"deadline" over-escalate),
  the bypass needs a per-user sub-cap, and the cap decision must run BEFORE any per-task API
  call or a backlog day fans out unbounded reads.
- Label the basis a financial source actually used (read it back), never force a default you
  can't verify per entity; an override map ships EMPTY and is a Harrison/Justin policy input.

**Process:** one 6-lens adversarial D-051 review found 3 MEDIUM (WS13 data-loss merge, WS10
unbounded-reads + over-escalation, WS7 fail-open freshness + headline disagreement) + LOWs
the green 5,021-suite hid; ALL fixed in one remediation commit; a 3-lens second-pass review
confirmed every finding CLOSED with ZERO new defects (SHIP x3). Full suite 5,035 passed / 42
skipped / 0 failed (with `GOOGLE_SERVICE_ACCOUNT_JSON` set; the 3 gsheets cache tests are
env-gated, green on the host). Bot-loaded? PARTIAL - WS6/WS12-source-filter/WS8-guard touch
bot-imported modules (activate at the next restart, restart-safe); WS7/WS9/WS10/WS13 are
script-side (their scheduled tasks import on-disk source). Harrison merges + ONE coordinated
restart; registers `setup-cashflow-snapshot-task.ps1`; populates `_ENTITY_PNL_BASIS` per
entity when ready. META: the green suite caught NONE of the 3 MEDIUMs - adversarial diff
review did, again.


## D-059 - WS17-B: the "gets smarter daily" knowledge pipeline (2026-06-21)

_Backfilled 2026-07-02 (hygiene session): shipped 2026-06-21 but never logged here -- the
log jumped D-058 -> D-061. Condensed from the founder-canon record (TOM 0vv) + the WS17-B
capture note; see `design/knowledge-pipeline.md` for the boundary doc._

**Context:** The North-Star Phase-1 knowledge loop was structurally dead: ONE flat
proposed-updates ledger held 17,952 PENDING but only 5 items had EVER been DM'd to
Harrison -- uncapped producers (a single drive run dumped ~17k), a drain capped at
<=5/run Mondays-only, ~80% dead-end types, and the actual learning stream
(`known_answer`) at ~11 lifetime. Merged `main`=`origin/main`=`3931049`; bot restarted
clean 20:28Z; suite 5,079.

**Decisions / what shipped:**
- Producers capped + `propose_update` idempotent (no re-flood on re-runs).
- Operational nudges route to DOMAIN OWNERS (floor-gated, LEX fail-closed,
  decision-SUPPORT) so Harrison's queue is knowledge + founder + ratify only.
- The knowledge drain decoupled from Monday/5-cap -> DMs DAILY.
- Approved `#info-for-cora`/generic items now WRITE `design/known-answers/{entity}.md`
  (previously a Slack-only dead-end); canonical `known_answers_map.py` closes the
  silent HJRP/UFL/F3C/HJRPROD non-learning bug (those files were written but never read).
- Ledger bounded: resolved rows rotate to an archive; live UNION archive backs
  idempotency. Legacy manual digest de-conflicted; `cowork-cora-digest` DISABLED.
- Boundary doc `design/knowledge-pipeline.md` (the single Harrison-gated promotion
  queue). D-034 invariants + the D-011 thumbs-up gate intact.
- Bulk-dismiss (bot down): 14,354 dead-end PENDING cleared (hubspot_note 9,855 +
  decision_capture 3,015 + generic 1,484); the 11 known_answer + 5 efficiency learning
  items kept; reversible `.bak`.

**Process / the headline:** an INDEPENDENT pre-merge adversarial pass (6 surfaces)
caught 1 real HIGH the green 5,079-suite AND the session's own two D-051 reviews
missed: the entity-agnostic known-answers WRITE gate screened billing/auth/client-name
PHI but NOT the clinical diagnosis/medication class (autism/ADHD/nonverbal/risperidone
all ALLOWED -- the detectors lived only in `scrub_lex_phi`, never called on the write
path). Fixed in `3931049`: `phi_guard.is_clinical_phi` wired into all 3 write gates,
deliberately NARROW (no name redaction; EXCLUDES wellness-overlap anxiety/depression so
F3 Mood positioning isn't over-refused); both directions verified (8/8 clinical caught,
7/7 legit pass).

**DOCTRINE:** (1) uncapped producers + a capped drain is structural starvation -- cap at
the source and let the drain run daily; (2) a knowledge WRITE path must call the PHI
detectors directly, never assume an upstream scrub covered it (the write gate and the
scrub are different surfaces); (3) the write map and the read map for per-entity
known-answers must be ONE canonical module or entities silently stop learning.


## D-060 - WS17-C: System-2 FOLD + auto-approve RETIRED + "Cora's read" enrichment (2026-06-22)

_Backfilled 2026-07-02 (hygiene session): shipped 2026-06-22 but never logged here.
Condensed from the founder-canon record (TOM 0zz) + the WS17-C capture note._

**Context:** Extends WS17-B (D-059). Executes the System-2 FOLD decision (Harrison
2026-06-21, `_shared/projects/cora/2026-06-21_fndr_cora-system2-fold-decision.md`).
Merged `main`=`origin/main`=`855b6eb` (the WS17-C commits + the pre-merge PHI-egress
fix); bot restarted clean; suite ~5,113.

**Decisions / what shipped:**
- **FOLD:** team contributions (`note:` / correction / bookmark reaction) now flow into
  the ONE Harrison-gated knowledge queue and write `known-answers/{entity}.md` via
  `apply_contributed_note` on thumbs-up. Retired: the `#cora-kq` approver card, ~15
  team_learning fns, the pending_contributions table, and the `team_note` KB write
  (0 such chunks ever existed). KEPT: the author paraphrase-confirm loop.
- **AUTO-APPROVE RETIRED:** every knowledge item (incl. HIGH machine-mined
  `known_answer`) now needs Harrison's thumbs-up; nothing writes without a reaction
  (D-011). Deliberately reverses the 2.4 HIGH-confidence auto-approve leg.
- **"Cora's read":** every knowledge DM carries a corroborated / conflicts /
  adds-context / net-new read + recommendation from Cora's own entity-scoped
  retrieval -- PHI-scrubbed, source-opaque, fail-soft, decision-SUPPORT, never
  persisted.
- `cowork-cora-digest` recorded expected-disabled in `scheduled-task-state.yaml`
  (kills the daily false WARN); `cowork-cora-gap-digest` KEPT as a retirement
  candidate; boundary doc `design/knowledge-pipeline.md` updated.

**Process / the headline (6th straight):** the independent pre-merge pass caught 1 real
HIGH the green ~5,113-suite AND the session's own 7-reviewer + 2-verifier passes
missed: WS17-C added a new LLM-EGRESS surface (Cora's read -> Anthropic; paraphrase ->
Haiku) where `is_lex_billing_status_phi` was ENTITY-gated while the write gate was
unconditional -- a folded note tagged with the AUTHOR's entity (e.g. Harrison=FNDR) but
carrying named LEX billing PHI would have reached the LLM verbatim. Fixed (`855b6eb`):
`is_lex_billing` made unconditional on the 4 LLM-egress screens; the 2 non-egress
intake screens left entity-gated (avoids over-refusing legit non-LEX billing facts).

**Validation:** WS17-B's first 7am fire came back clean the same morning -- no re-flood
(~65 new vs the old ~17k dump), 10 knowledge DMs (6 known_answer + 4 efficiency) vs 5
EVER pre-WS17-B, rotation archived 94 rows; owner-routing began at the next fire by
design (its floor initialized that morning).

**DOCTRINE:** (1) an LLM-EGRESS screen must be unconditional on the content class --
entity-gating belongs only on non-egress intake surfaces (the D-066 doctrine-2 "3
consecutive builds" count includes this instance); (2) the D-011 thumbs-up is the write
gate for EVERY knowledge item, machine-mined or team-contributed -- auto-approve on
canonical knowledge is retired; (3) fold parallel contribution queues into the one
gated pipeline rather than maintaining a second approver surface.


## D-061 - Per-person involvement dossier (North Star pillar 4): cora_person_dossier + weekly refresh (2026-06-30)

**Context:** Re-homes the per-person involvement / founder check-in capability from the
deprecated Tag Founder Bundle onto Cora, per
`_shared/projects/cora/2026-06-29_fndr_cora-per-person-layer-build-spec.md` (section 10
decisions LOCKED). Branch `claude/per-person-dossier` off `main`@`e0a77c6`; MERGED to main.

**Decisions / what shipped:**
- `src/cora/person_identity.py` - PersonIdentity resolver derived ONLY from the 5 maintained
  YAMLs (org-roles + slack-to-asana + slack-to-hubspot + user-aliases +
  monitored-email-accounts). NO new map (anti-drift). `lex_staff` on the PRIMARY entity (so a
  cross-entity controller like Justin is NOT misclassified - his incidental LEX activity is
  caught by the non-LEX clinical backstop); `external` (Jason); `exclude_personal_mailbox`
  (Demi - also structurally empty mailbox); `exclude_maricopa` (Alina). The two non-structural
  flags are a small in-code constant (LOCKED policy, not a parallel identity map).
- `src/cora/tools/person_dossier.py` - founder-or-self access gate (peer refused with NO target
  leak), DM-only surface gate (deterministic peer-wall, D-034), fail-soft multi-source pull
  (Gmail DWD per-mailbox / Fireflies deduped via meeting_actions helpers / Asana / HubSpot
  stage-label / Calendar this+next week; Drive PENDING v1), LEX PHI wall mirroring
  `drive_materializer._phi_wall` (scrub PRE-synthesis, drop on surviving clinical/named-billing;
  non-LEX clinical backstop), Sonnet synthesis (fail-soft), write-back replacing the dossier's
  "Recent involvements" section / preserving "Durable notes" / normalizing "by Tag" -> "by Cora".
- `tool_dispatch.py` - `cora_person_dossier` registered (global-core, founder-or-self handler via
  `_load_supervisor_hierarchy().founder_slack_id` || `_HARRISON_SLACK_ID`); timeout 25 -> 45 ->
  60s after the live smoke (`15d0f83`). `model_router.py` - check-in phrasings -> Sonnet.
- `scripts/run_person_dossier_refresh.py` + `deployment/setup-person-dossier-refresh-task.ps1`
  (task `cowork-cora-person-dossier-refresh`, Sun 16:30 AZ, self-bounded). `scripts/check_identity_map.py`
  roster-drift guard vs `_brain/reference/team-identity-map.md`. Tests: `test_person_identity.py`
  + `test_person_dossier.py` (35).

**Process:** A 3-agent adversarial D-051 review BEFORE the restart caught a real HIGH the green
5,179-suite + self-review both missed - `_gmail_block` scrubbed by `mailboxes[0]` only, so a
non-lex_staff target whose LEX mailbox isn't first (Justin) would leak that mailbox's PHI
subjects to the LLM + into the written dossier; fixed via PER-MAILBOX scrub. Plus a peer-wall MED
(no surface gate -> a check-in in a shared channel would post the summary to peers; fixed with the
DM-only gate) and a Fireflies LBHS-domain MED. Then the LIVE in-Slack DM smoke caught a latency
bug the suite couldn't: the 5-connector + internal-Sonnet pull ran ~38s SEQUENTIALLY and blew the
25s dispatch timeout (the model got "timed out" while the orphaned worker still wrote the dossier);
fixed by parallelizing the source pulls (pull 28s -> 6.9s; full build ~38s -> 31.9s) + the 45->60s
timeout. Suite 5184. Merged to main; bot restarted x2 (running the parallel code); in-Slack DM
smoke PASSED (917-char real summary, no timeout); gate smoke all-pass (peer-refuse-no-leak,
DM-only redirect, graceful); Tommy write-back verified. META: the green suite caught NEITHER the
PHI HIGH nor the latency bug - the adversarial pass and the live smoke did. DOCTRINES: a tool that
makes its OWN internal LLM call + multiple connector pulls must fan the pulls out concurrently AND
carry a timeout well above the single-API default; a dispatch timeout never kills the orphaned
worker thread (it finishes + writes anyway). Phase 2 deferred: Drive "recent files" source; the
self-serve "what's on my radar" overlay.


## D-062 - lex-swept-phi-check: daily defense-in-depth PHI re-scan over _brain/swept/ (2026-06-30)

**Context:** The North Star cited a "lex-swept-phi-check (daily 07:06)" net behind the drive
materializer's inline `_phi_wall` but it did NOT exist in the repo. Built per
`_shared/projects/cora/2026-06-29_fndr_cora-lex-swept-phi-check-spec.md` (recommendation: build
it). Branch `claude/lex-swept-phi-check` off `main`@`15d0f83`; MERGED to main@`ae859df`.
SCRIPT-SIDE - no bot restart.

**Decisions / what shipped:**
- `scripts/run_lex_swept_phi_check.py` - re-reads every written `_brain/swept/**/*.md` and re-runs
  the SAME PHI detectors the wall uses, IMPORTED from `phi_guard` + `drive_materializer` (no
  drift): `is_clinical_phi`, `is_lex_billing_status_phi`, `_LBHS_SIGNAL_RE`, `_LEX_CONTEXT_RE`,
  `_lex_staff_names`, + phi_guard's name regexes via `_has_unredacted_client_name`. On a hit:
  quarantine (rename IN PLACE to `{date}.QUARANTINED.md`) + alert Harrison (DM + #cora-health;
  entity/date/detector ONLY, NEVER the PHI text) + audit log. Clean run = heartbeat
  ("N files scanned, 0 PHI"). Fail-soft: read + tree-walk errors surfaced as UNVERIFIED (alerted),
  never silently passed. Quarantine-failure alert labels "file still LIVE" (not "dry-run").
- `deployment/setup-lex-swept-phi-check-task.ps1` - task "Cora - LEX Swept PHI Check", daily 07:06
  AZ (after the 05:45 materializer), runs `--all` so a >26h missed run leaves no gap. Recorded
  expected-enabled in `data/maps/scheduled-task-state.yaml` (documentary `enabled:` key;
  nightly_health_check reads only disabled/running, so inert + forward-compatible).
  `tests/test_lex_swept_phi_check.py` (19).

**Key design (reviewer-validated):**
- `scan_body` runs the detectors on the body AS-IS (NOT a re-scrubbed copy): a written swept file
  is already the wall's output, a regression file is raw; detectors-on-body are a strict SUPERSET
  of the wall's checks-on-scrubbed (scrubbing only REMOVES PHI) AND idempotent-safe on the
  placeholders. Do NOT reintroduce a `scrub_lex_phi` string-diff - scrub_lex_phi is NOT idempotent
  (it re-wraps `[medication redacted]` -> `[medication [medication redacted]]` via `_MED_CONTEXT_RE`).
- Quarantine = rename IN PLACE within `_brain/swept/` (stays KB-excluded: the exclusion keys on a
  path having BOTH `_brain` AND `swept` segments); `_brain/_quarantine/` was REJECTED (has `_brain`
  but not `swept` -> WOULD be re-ingested).
- LBHS check is ENTITY-AWARE (matches the wall): flag in the LEX branch; in non-LEX rely on clinical
  + named-billing-with-context, so a bare BUSINESS mention of LBHS/COPA/BHRF/Jared Harker in a
  holdco M&A digest is NOT false-quarantined.

**Process:** A 3-reviewer adversarial pass (false-negative/parity/containment, robustness,
leak/false-positive). Two reviewers INDEPENDENTLY caught a real HIGH the green 5,195-suite missed
(the non-idempotent scrub-diff would false-quarantine clean med-mentioning LEX digests daily ->
alert fatigue). A re-review (the 3rd reviewer's process crashed, re-run focused) caught a MED (the
unconditional LBHS flag false-quarantined holdco business-entity LBHS digests). Both fixed +
regression-tested; re-review confirmed no PHI leak, idempotency fixed, true superset preserved.
Suite 5203; dry-run over the live 13 swept files = 0 PHI. Merged + task registered (Ready, daily
07:06 AZ) + final dry-run clean. META: the green suite caught the scrub-diff HIGH ZERO times - two
independent adversarial reviewers did. DOCTRINE: an artifact-level PHI re-scan must reuse the
wall's exact detectors AND run them on the written body as-is (re-scrubbing is non-idempotent);
the in-place `.QUARANTINED.md` rename is the safe quarantine; an LBHS signal is PHI in LEX scope
but a business entity name in a holdco digest - gate it entity-aware or it false-fires on M&A.

## D-063 - Graduated-trust SHADOW-mode instrumentation: measure, never flip (2026-06-30)

**Context:** The North Star Pillar-B (item 1c / `00-Founder/tag-standup/2026-06-28_fndr_brain-learning-architecture.md`) wants the knowledge gate moved "off the low-stakes majority" via graduated trust (Tier 0 auto / Tier 1 owner / Tier 2 Harrison). But WS17-C (D-060) deliberately RETIRED auto-approve, and the founder TOM (item 1c) is explicit: do NOT flip - build shadow instrumentation first, review ~2 weeks, then Harrison decides. There was NO shadow data. Built per `_shared/projects/cora/2026-06-29_fndr_cora-graduated-trust-shadow-spec.md`. Branch `claude/graduated-trust-shadow` off `main`@`b2953c6`, pushed (awaiting `main` merge), HEAD `e07cc06`, suite 5245. SCRIPT-SIDE - no bot restart.

**Decisions / what shipped:**
- `src/cora/graduated_trust_shadow.py` (new): per knowledge proposal (known_answer/efficiency/generic) computes + PERSISTS what graduated trust WOULD have done, logging only. Deterministic (no-LLM) DENYLIST-FIRST category classifier (allowlist: operational/sop/ownership/contacts/logistics/addresses/product_inventory; denylist: money/contracts/legal/equity/comp/strategy; else "other"). `is_high_stakes` flags LEX entity / PHI (all 3 phi_guard predicates, fail-SAFE on error) / Maricopa / denylist-category / cross-entity. `classify_tier`: Tier 0 (would-auto-approve) iff CORROBORATED + allowlist + recognized teammate (org-roles) + not-high-stakes + not-CONFLICTS; Tier 1 (would-route-to-owner) iff allowlist + authorized owner (gap-domain-owners) + uncorroborated + not-high-stakes; Tier 2 (harrison) otherwise. Append-only daily `logs/graduated-trust-shadow-YYYY-MM-DD.jsonl` (`shadow_decision` + `shadow_reaction` records, joined by update_id). `build_report`/`format_report` = counts by tier, would-Tier-0 rate/week, would-Tier-0 FALSE-POSITIVE rate (Tier-0 item Harrison later thumbs-down'd). Kill switch `CORA_GRADUATED_SHADOW=0`. Imports NO bot-process module (phi_guard/org_roles/gap_autofill/cross_entity_guard lazy).
- `coras_read.py`: exposed the structured verdict via `build_coras_read_struct` -> frozen `CorasRead(verdict, note, line)`; `build_coras_read` is now a thin `.line` wrapper (DM byte-identical - "stop discarding the verdict as transient"). `_CACHE` stores `CorasRead`.
- `run_knowledge_review.py`: `_attach_coras_read` stashes `_coras_read_verdict`; shadow decisions logged for the DM batch (Step 2b, after the read attaches, before the send); shadow reactions appended in Step 1 (non-dry-run, for the resolved pairs); new `--report` mode (early return BEFORE the lock/drain/Slack). ACTS ON NOTHING - every item still PENDING -> DMs Harrison; auto-approve stays retired (D-060 / D-011 intact).

**ACT ON NOTHING (the load-bearing invariant):** the live approve/DM path is byte-identical with shadow on or off. `_coras_read_verdict` / shadow_* keys are never read by `format_single_item_dm` and never persisted to the proposed-updates ledger (`_patch_dm_ts` re-reads from disk). Both `record_shadow_*` are double-wrapped fail-soft and never raise into the drain. Tests pin it: `test_format_single_item_dm_ignores_shadow_fields` + `test_shadow_acts_on_nothing_byte_identical` (a would-Tier-0 item still stays PENDING + is still DM'd; shadow ON writes a log, OFF does not).

**Design choice (on the record):** FNDR/HJRG entity is NOT blanket-excluded from Tier-0 ELIGIBILITY - its high-stakes content is caught via the category denylist + PHI + the cross-entity TEXT scan, not by a blanket entity rule. Rationale: blanket-suppressing FNDR would gut the very signal the shadow exists to measure; since it acts on nothing, a mis-eligible FNDR item simply shows up in `--report` for Harrison to judge. Documented so the flip decision is informed, not surprised.

**Process (Standing Operating Loop):** import-smoke + full pytest green; a 6-lens INDEPENDENT adversarial diff review (PHI-egress / byte-identity / FP-accounting / spec-compliance = SHIP; tier-logic + fail-soft = SHIP_WITH_FIXES). All findings fixed + regression-tested: (MED) the spec's "NOT cross-entity" Tier-0 condition only fired off the structured `entities` list (set for efficiency only, which can't reach Tier 0) -> added a TEXT keyword cross-entity scan reusing `cross_entity_guard._ENTITY_DEFS` (paired F3E/F3C collapse to one), so a cross-cutting known_answer/generic is correctly Tier 2; (MED) `build_report` crashed on a valid-JSON-but-non-dict log line -> `isinstance(rec, dict)` guard; (LOW) money denylist missed spelled/per-unit/net-terms cues -> tightened; (LOW) regexes ran on uncapped text (O(n^2) email pattern) -> `_MAX_CLASSIFY_CHARS=2000` cap; (LOW) per-week rate over-extrapolated on <1wk -> PROVISIONAL qualifier. META: the green suite caught NONE of these; the independent review did.

**THE FLIP IS NOT IN THIS BUILD.** Flipping Tier-0 (and Tier-1 owner-routing-as-approval) live is a PARTIAL REVERSAL of WS17-C and touches D-011 - Harrison's explicit decision, made FROM the shadow data (run `python scripts/run_knowledge_review.py --report` after ~2 weeks; target ~0% would-Tier-0 false-positive rate), behind a transparent #brain-log, NOT from this code. DOCTRINE: never flip auto-approve from a *design* - flip from measured shadow data behind a near-zero-false-positive bar on Tier 0.

## D-064 - Sales over-deflection fix: precision-favoring financials/legal block (2026-06-30)

**Context:** Cora hard-refused Alex/Tommy's COMMERCIAL questions (deal value, PO amount, wholesale price, margin on an order, invoice paid-status) as "finance/legal." Root cause (review `_shared/projects/cora/2026-06-30_fndr_cora-over-deflection-review.md`): a deterministic PRE-LLM block in `user_access.check_access` on a flat substring list (`cost/spend/margin/invoice/order/PO/revenue/income...` via `any(p in msg_lower)`) that flagged any money word, mis-fired on substrings (`cost`->`Costco`, `income`->`incoming`), and gave no partial answer. Legal similarly tripped on routine `contract/agreement` + `terminate/penalty`. Branch `claude/sales-commercial-deflection-fix` off `main`@`f8f87d3`, pushed, HEAD `cd2d553`, suite 5410. **BOT-LOADED (user_access/app/prompt_loader/f3e.md) -> restart REQUIRED; gated on Harrison.**

**Decision / what shipped:**
- `_financials_is_blocked` rebuilt (v3, after 3 adversarial rounds) to a PRECISION-FAVORING model: block iff CANON (always-company terms: p&l/cash*/net income/ebitda/balance sheet/payroll/AR-AP/profitability/cogs/overhead/net worth/refinance/quickbooks) OR bare "financials" (unless specific-deal) OR (FINANCE_TERM {profit/revenue/margin/income/earnings/debt/finances + "make/bring-in money"/"in the black"/owe} AND COMPANY_SCOPE {we/our/company/entity-name/aggregate/category} AND NOT specific-deal). Word-bounded, ReDoS-clear.
- `legal` sensitivity dropped routine-commercial verbs (terminate/penalty/default/violation/enforce); `_legal_is_blocked` STRONG terms + genuinely-contentious sensitivity only.
- `check_access` gains `tier` param; financials block SUPPRESSED in TIER_1 (fail-safe: absent tier => restrictive); `app.py` passes channel tier at all 3 sites; DMs pinned TIER_3 structurally. cap_table/hr/legal/phi stay tier-blind.
- Prompt (`_UNIVERSAL_RULES` TIER_3 hard-stop + `f3e.md`): deflect off restricted-finance CONTENT not channel function; mixed-question answer-commercial + pointer; deflection copy names the restricted thing; tool-table TIER_3-governs note.
- R6: 105-case regression corpus (63 block / 42 pass across all 3 review rounds) + `scripts/run_false_deflection_watch.py` (weekly Mon 08:00 UTC -> #cora-health; buckets blocks by topic; flags financials/legal spikes for commercial roles only).

**DOCTRINE (the load-bearing lesson):** A keyword matcher CANNOT cleanly separate company-finance from commercial money-talk for bare/ambiguous terms (the split depends on proper-noun customers + quantifier semantics). Three adversarial rounds each found real defects in BOTH directions (leaks AND over-blocks) until a precision-favoring synthesis: **layer 1 (this deterministic block) blocks only on a clear COMPANY signal and lets ambiguous bare terms PASS; the prompt (layer 2) + the tool-level TIER_1 gate on QBO/cashflow (layer 3, untouched) backstop.** Do NOT over-claim layer 1 as "the hard guarantee" - it is defense-in-depth. Accepted residuals (documented in code + `TestBareTermResidualPasses`): bare scopeless finance terms ("what's the revenue"), rare money-verb idioms ("how much did we clear", "are we up/down"), "bottom line" discourse marker. Re-closing a residual is a conscious choice, never accidental. META (D-051 held 3x): the green suite caught ZERO of the real defects; the independent adversarial passes caught every one.

**Invariants preserved (verified live):** entity firewall (Alex->OSN blocked), PHI/LEX wall (phi_custodian relaxes only phi), true company-finance + cap_table gating (company P&L/cash/payroll/cap-table still deflect for a financials-blocked sales role in a non-TIER_1 channel). This NARROWS an over-broad heuristic; it opens NO firewall.

**Open (Harrison):** (1) merge to `main`; (2) restart to activate (`deployment/restart-cora.ps1`); (3) posture: re-add `legal` to Alex/Tommy (his R7 stopgap removed it; the tightened matcher makes it safe) + confirm R3 (TIER_1 financials pass-through for sales leads in #f3e-leadership); (4) register the watch (`deployment\setup-false-deflection-watch-task.ps1`, elevated PS, script-side). Harrison's `user-permissions.yaml` stopgap edit left uncommitted (his live posture; YAML live-reloads).

## D-065 - Fireflies coverage monitor seat-scope: population-scoping lives in the DATA (2026-07-01)

**Context:** The 2026-06-22 Fireflies Enterprise right-size removed 5 people (Micah/Elena/Eric/Jeff/Matt) and cancelled Jake's invite, but they correctly remain in `data/maps/monitored-email-accounts.yaml` as employees (Gmail/Drive KB ingestion). The weekly coverage monitor (`cowork-cora-fireflies-coverage`, armed `--nudge`, Mon 08:10 AZ after the B1 restagger) read the FULL DWD roster via `fireflies_coverage.load_dwd_humans()` and would DM the removed people wrong "accept your Fireflies invite" nudges. Decision context: stay on Enterprise + maximize it, HIPAA off (Harrison 2026-07-01, `00-Founder/2026-07-01_fndr_fireflies-enterprise-maximization-plan.md`). Branch `claude/fireflies-seat-scope` off `main`@`a8a93bb`, HEAD `cb9d44f`, pushed, awaiting merge. Suite 5492/42sk. SCRIPT-SIDE - no restart.

**Decision / what shipped:**
- `fireflies_seat: true` on exactly ONE YAML entry per current seat-holder (10: harrison/hannah/justin/alina/daniel @hjrglobal, larry@bigd.media, tommy@f3energy, alex@f3energy, shaun/jen @lexingtonservices); key documented in the YAML header.
- `load_dwd_humans()` seat-scope filter: mode detected across the whole file; a collapsed alias component is kept iff ANY member entry carries the flag (flag on tommy@f3energy keeps the Tommy human whose primary/rep is tommy@hjrglobal). Zero flags anywhere => full-roster behavior (backward-compatible). `load_dwd_humans` has exactly ONE consumer (this monitor); all other YAML readers use their own keys.
- Tests: TestSeatScope (6) incl. flag-via-collapsed-alias-entry, no-flags backward-compat, and a real-roster set-equality pin of the 10 names (update flags + pin TOGETHER when seats change).
- Live dry-run: exactly 10 humans - 4 COVERED, 3 MEMBER_NO_RECORDINGS (Hannah/Tommy older-recordings-none-in-30d, Jen), 3 NOT_A_MEMBER = the open invites (Alex/Justin/Daniel). None of the removed six.

**DOCTRINE:** Population-scoping for a people-facing nudge tool belongs in the DATA (a roster flag evaluated across the collapsed alias component), NOT in disabling the task or forking the roster file - one file can serve a broad ingestion audience and a narrow nudge audience simultaneously. Corollaries: (1) the interim "disarm the task" mitigation became moot because the monitor is script-side (working-tree-is-live) and the change landed before the next fire; (2) do NOT re-run `deployment/setup-fireflies-coverage-task.ps1` casually - it re-registers at 08:00 and silently undoes the 08:10 restagger (same class as the D-058 de-collision rule); (3) stale throttle rows for out-of-scope users are inert by construction (they can never re-enter the uncovered list).

**Process note:** the 4-agent adversarial review fleet died on a subagent session limit; a 4-lens INLINE review ran instead (correctness / blast-radius / failure-modes / tests-docs) - one stale module-docstring fix, no code defects. The concurrent flywheel session committed WS-1/WS-2 on the checked-out branch mid-build, so the scoped commit was cherry-picked as a TWIN (`6751fbc` -> `cb9d44f`) onto `main`@`a8a93bb` in a temp worktree so Harrison's merge takes ONLY this change; git dedupes the identical patch when flywheel later merges.

## D-066 - Flywheel reliability: deterministic gap detection + throughput telemetry + eval harness + extractor pause + ledger boundedness (2026-07-01)

**Context:** The North-Star "gets smarter daily" loop had silently flatlined for 2+ weeks (0 knowledge items DM'd to Harrison every weekday since ~6/23; knowledge-gaps.jsonl dry since 6/15 - 44 gaps EVER; the D-063 graduated-trust shadow had ZERO records because the stream it instruments was starved; PENDING grew 3,772->4,277/wk) and nothing alarmed. Root causes: (1) the ONLY gap intake was the LLM self-emitting a sentinel - prompt-only INSTRUMENTATION, the twin of the D-034 enforcement lesson; (2) the dominant producer (drive_extractor, 50/day + 1,980 backlog) fed exclusively the operational bucket WS17-B ruled dead-end; (3) no telemetry watched throughput. Spec of record: `_shared/projects/cora/2026-07-01_fndr_cora-flywheel-reliability-spec.md`. Branch `claude/flywheel-reliability` off `main`@`a8a93bb` (7 session commits `9ac5d08..968780a` + the fireflies twins), PUSHED, awaiting merge. Suite 5,420 -> 5,596 / 42 sk.

**Decisions / what shipped:**
- **WS-1 (bot-loaded):** `gap_detection.py` runs two deterministic detectors on every LLM Q&A response at the `_extract_and_log_gap` chokepoint - `kb_miss` (retrieval ran, 0 chunks passed the live gate, no note/fallback/tool/thread answer; signal exposed via kb_meta in `context_loader._try_kb_retrieve`, zero new embeds) and `unknown_response` (locked UNKNOWN_RESPONSE or short "I don't have that" shapes). Sentinel kept, tagged `llm_sentinel`. Controls: guard refusals structurally never reach the hook (all guards return pre-`_dispatch_qa`); LLM deflection shapes veto-matched (emphasis markers stripped first); LEX* fail-closed out; 3-predicate PHI union on the question; smalltalk skip; 7d (entity, normalized-question) dedup; 15/day cap (`CORA_GAP_DETECT_DAILY_CAP`) + persisted overflow counter, a cap hit does NOT burn the dedup key; one detection per thread root; eval traffic never logs. DM gaps carry `private_source`.
- **Gap lifecycle:** 30d TTL (`expire_stale_gaps`, start of the gap-autofill run) clears stale gaps from the Haiku mining spend. `should_escalate` hardened: kb_miss = mining-only (never an owner ask); DM/private gaps never escalate; 3-predicate PHI union; D-064 `_financials_is_blocked` screen (fail-CLOSED) so a TIER_1 finance question is never quoted to a financials-blocked owner.
- **WS-2 (script-side):** `flywheel_metrics.py` = ONE module computing the metrics + owning the thresholds for BOTH health surfaces (nightly `check_flywheel` + weekly report section [7] / alarms / Slack line): knowledge DMs 7d (WARN if 0), gap-log staleness (WARN >7d), autofill mined 7d, shadow records+days (the D-063 flip gauge), PENDING size (WARN >6,000) + 7d growth (WARN >+500) via a daily baseline, producer-vs-drain. Warn-only by design; day-1 WARNs are CORRECT (the starvation is real). CRITICAL if CORA_EVAL_MODE is ever present in the environment (a stray .env line would strip every tool from the bot - the HEALTH_PING_URL precedent class).
- **WS-3 (script-side + one inert bot-loaded gate):** golden-set eval harness - `data/evals/golden-set.yaml` (24 seeded cases: known-answers content, D-064 pass/block canon, entity-firewall + PHI probes; LEX = must-refuse ONLY, no client facts ever) + `scripts/run_kb_evals.py` (L1 = live `load_context_parts` retrieval/static presence + deterministic guard canon, no LLM; L2 `--answers` = full pipeline with tools disabled, capped). Isolation: `CORA_EVAL_MODE=1` set post-dotenv pre-cora-import; `tools_for_entity` offers [] + `dispatch()` refuses in eval mode; the runner never imports app/cache/ledger/slack_sdk (grep-guard test). Auto-growth: on Harrison's approval the executor appends an L1 case to `golden-set-auto.yaml` (id-idempotent, PHI re-screened, LEX-refused, fail-soft). A corpus load failure = red summary + exit 1, never a silent green. Weekly task "Cora - KB Evals" Mon 09:05 AZ staged (`deployment\setup-kb-evals-task.ps1`, Harrison registers, elevated). Live baseline: 21/21 L1+guard pass; both L2 refuse-canaries refused through the live pipeline.
- **WS-4 (script-side; Harrison RATIFIES at merge):** triage verdict = PAUSE the drive-extractor proposal loop. Evidence: 97% of ALL owner-routed DMs (68/70 ever; 58/60 last 7d, 35 to Harrison himself) and 93% of PENDING are drive facts; the 30-fact sample = invoices-as-deals, Cora's own build docs as decisions (the D-057 class), OSN employee POS PINs in person facts. Mechanism: call-time gate `DRIVE_EXTRACTOR_PROPOSALS_ENABLED` (code default enabled = behavior-preserving; Harrison adds `=0` to .env at merge; extraction/facts-DB untouched, watermarks held, reversible, no elevated PS). Cap check moved to loop top (cap=0 leaked 1/run; exactly-at-cap no longer holds the watermark pointlessly; dry-run now counts toward the cap). Ledger boundedness: operational items PENDING + never DM'd/routed after 14d expire as DISMISSED/`expired_unrouted` in the Step-0 atomic pass - a DELIBERATE spec'd exception to the D-051 never-dismiss-unseen rule, scoped strictly to the operational stream; knowledge items stay exempt. `knowledge_review._ids_in_file` now fails LOUD on non-missing read errors (post-expiry the archive becomes the SOLE re-proposal barrier for ~3,900 drive-fact ids; failing open = mass re-propose). Verdict doc: `_shared/projects/cora/2026-07-01_fndr_cora-drive-extractor-disposition.md`.
- **WS-5 (script-side):** `reply_formatter.normalize_slack_bold()` (**/__ -> * only; fence/inline-code/Slack-token protected, reverse-order placeholder restore, idempotent) wired into the daily-briefing composer, the strategy-memo Slack copy (the .md file keeps markdown), the finance-recap post, and the "Cora's read" DM line. Deliberately NOT in `slack_egress.sanitize_text` (the boundary stays safety-only per the 2026-06-17 review). Ecom brief + fireflies coverage audited: deterministic, no LLM, no change; person-dossier already rides format_reply.

**DOCTRINE:**
1. Prompt-only INSTRUMENTATION is insufficient for a load-bearing signal, exactly as prompt-only enforcement is for a hard requirement (D-034) - a flywheel input belongs in deterministic code at the chokepoint.
2. Any NEW machine-written surface that later egresses (here: the gap log -> Haiku mining, owner DMs, eval seeds) must screen text with the FULL 3-predicate PHI union (is_phi_risk + is_clinical_phi + is_lex_billing_status_phi) - is_phi_risk alone has now missed the clinical/admin classes on a new surface in three consecutive builds (WS17-B, WS17-C, this one); the independent pass caught it each time.
3. A throughput gauge lives in ONE module consumed by every reporting surface (the _EXPECTED_DISABLED two-copies drift class), and it must alarm on ABSENCE (0 items, stale file), not just on errors - this loop died silently for two weeks because only errors alarmed.
4. An eval harness must be able to FAIL loudly: a corpus parse error is a red run, never a silent shrink; a skipped case keeps its failing status; eval isolation denies tools at the OFFER (tools_for_entity), not just execution, with the env flag set AFTER load_dotenv(override=True) so .env cannot clobber it - and a health check watches for that flag leaking into the live environment.
5. Population-level producer noise is paused in the config plane (a call-time env gate that holds watermarks), never by deleting the producer or its DB - reversibility is what made "let Code decide, Harrison ratifies" safe.
6. Test hygiene: a script that sets an env var at IMPORT leaks it into the whole pytest session via collection, and monkeypatch.delenv-in-teardown RESTORES it (51 unrelated tests failed) - pop with plain os.environ at test-module import; and any executor-side write hook needs a conftest-level path redirect BEFORE its first test lands (a fixture fact briefly reached the real golden-set-auto.yaml).

**META (the D-051 streak extends):** the green 5,577-suite caught ZERO of the 9 confirmed defects; the 87-agent find+verify pass (8 lenses, 3 refutation votes per finding, ~14 findings refuted) caught the HIGH (the PHI union on the new egress surface) and every MEDIUM. Every session since 6/17 has repeated this pattern.

**Open (Harrison):** (1) merge `claude/flywheel-reliability` -> `main` (carries the fireflies twins `6751fbc`/`f9c865c`; git dedupes against `claude/fireflies-seat-scope`); (2) ratify the WS-4 pause -> add `DRIVE_EXTRACTOR_PROPOSALS_ENABLED=0` to `.env`; (3) ONE coordinated restart (bot-loaded: the app.py hook, context_loader kb_meta, gap_detection, the inert tool_dispatch gate); (4) register "Cora - KB Evals" (elevated PS) + reconcile scheduled-task-state.yaml; (5) spec 5 live smokes post-restart; (6) the graduated-trust flip review RE-DATES to ~2 weeks after the FIRST real shadow records (post-merge), not 7/14. Accepted residuals: kb_miss can log a statically-answered question (bounded by cap+dedup; mining-only, Harrison-gated); the thread-follow-up path still lacks check_access (pre-existing, flagged 6/18); OSN PINs remain in the facts DB (extraction-side; egress stopped by the pause).

---

## D-067 - Gap-detector calibration hotfix: unknown_response length-independence + kb_miss best_distance instrumentation (2026-07-02)

**Context:** Two post-D-066-restart live smokes (#cora-build, 2026-07-02 ~00:52-00:56 AZ) proved BOTH deterministic detectors were mis-calibrated - a safe-direction miss (no leak, pure under-detection). A 566-char "official policy on office plant watering?" reply (opened with the locked UNKNOWN phrase) and a 657-char "SOP for beekeeping on the office roof?" reply (opened "I don't have that context.") should each have fired `unknown_response`; NEITHER did. Roots verified in code: (1) `is_unknown_response`'s `len(reply) > _UNKNOWN_MAX_CHARS (=350)` guard ran at the TOP, before the prefix-anchored `_UNKNOWN_RES` regexes - but Cora's answer-first house style (the 2026-06-30 format standard) pads a genuine miss reply to 550-700 chars, so the archetypal miss was length-skipped. (2) `kb_miss` requires 0 chunks under the live 1.30 gate, empirically unreachable at ~560K chunks (even orthogonal vocabulary retrieves ~12 chunks <=1.08) - a dead backstop as shipped. Follow-up slice to D-066 WS-1. Branch `claude/gap-detector-calibration` off `main`@`fd76253`, PUSHED, awaiting merge. Suite 5,420-era -> 5,604 pass / 42 sk (the 13 red `test_drive_extractor.py` cases are the D-066 WS-4 pause landing with a stale test file - PRE-EXISTING on `main`, confirmed by stashing this diff and re-running; NOT introduced here - flag for a separate Code fix).

**Decisions / what shipped (bot-loaded except the tests):**
- **unknown_response widened (`gap_detection.py`):** the locked-phrase `startswith` and the prefix-anchored `_UNKNOWN_RES` regexes now run REGARDLESS of length (they anchor to the reply START, so they fire only when the reply BEGINS with an unknown/no-data shape, never on a mid-text quote). `_UNKNOWN_MAX_CHARS` is retained ONLY on the anywhere-in-reply containment path (a long reply may QUOTE the locked phrase mid-text). The determiner set `(that|this|any|it)` is unchanged - a long helpful answer opening "I don't have THE exact figure, but ..." (determiner OUTSIDE the set) correctly still does NOT fire.
- **kb_miss calibration DATA, not a gate change:** `context_loader._try_kb_retrieve` now sets `kb_meta["kb_best_distance"]` (min distance across ALL returned chunks, regardless of the 1.30 gate) + `kb_meta["kb_chunks_returned"]`; `gap_detection` passes both to `knowledge_gaps.log_gap` (recorded only when not None - pre-existing/non-KB records stay clean) and into the decision log line; the app.py sentinel path passes them too. The `kb_miss` PREDICATE is byte-for-byte unchanged (still 0 relevant hits). A week of these best_distance values lets kb_miss be recalibrated to a distance FLOOR WITH Harrison rather than a guess - deliberately deferred. `flywheel_metrics` gained a doc-note that `kb_miss=0` is EXPECTED-for-now, not a defect.
- **Two review fixes (SHIP_WITH_FIXES; both CONFIRMED MED, both caught by the independent pass, NOT the green suite):**
  - **(MED-1) tool "not found" relays:** the widened matcher would have logged `meeting_actions.py:1376/1382` ("I couldn't find a meeting matching ...") and `person_dossier.py:686` ("I don't have any reachable work-involvement signals for {name} ...") as `unknown_response` gaps - a tool RESULT is not a knowledge gap, and the dossier relay carries a PERSON NAME into the egress-bound log. Fix: gate the `unknown_response` branch on `not gen_meta.get("used_tools")` (mirroring `kb_miss`), with an exception for the exact locked finance UNKNOWN phrase (the finance connector's genuine data-gap signal - `test_unknown_wins_even_with_tools` pins it).
  - **(MED-2) >400-char deflection bypass:** `is_deflection` caps at `_DEFLECTION_MAX_CHARS`=400 but the widened prefix path is length-independent, so a >400-char refusal that OPENS unknown-shaped AND carries a deflection phrase (e.g. "I don't have visibility into that ... that's company financials, ask in #f3e-finance") escaped the veto. Fix: re-assert the deflection veto length-INDEPENDENTLY inside the unknown prefix branch (safe - no `_DEFLECTION_RES` redirect phrase legitimately BEGINS with an unknown shape).
- **Tests:** `test_gap_detection.py` gains the two smoke shapes (length-independence pinned), the mid-text false-positive guard, the determiner-set guard, the deflection-collision + tool-refusal classes; new `test_kb_meta_calibration.py` pins the calibration data's ORIGIN in `context_loader` (min-not-first, set-even-when-0-pass-the-gate, None-when-empty, 4dp rounding).

**DOCTRINE:**
1. A response-shape detector's guards must be ordered by INTENT, not convenience: a length cap that protects an anywhere-in-string containment match must NOT also gate the prefix-anchored openers - the two have opposite false-positive profiles (a padded miss reply vs a mid-text quote).
2. When one detector widens (unknown length-independence) it can newly collide with a sibling guard whose OWN cap is now asymmetric (`is_deflection`'s 400 vs the removed 350) - re-assert the sibling veto in the widened path rather than raising the sibling's cap (raising it would false-veto long legit answers that merely reference a channel - the exact case `test_long_answer_containing_channel_hint_not_vetoed` pins).
3. A "no data / not found" relay emitted BY A TOOL is a tool result, not a knowledge gap - gate any deterministic gap detector on `used_tools`, with a narrow allowlist for the tool whose UNKNOWN output IS the signal (the finance connector).
4. Ship calibration DATA before a calibration GATE: expose the measurement (best_distance) on the record for a week, then set the threshold WITH Harrison - never guess a distance floor blind.

**META (the D-051 streak extends again):** the green 5,599-suite (with my new tests already in it) caught ZERO of the 2 confirmed MED regressions; a 4-lens independent adversarial pass (R1 PHI/leak SHIP, R2 correctness, R3 deflection-collision, R4 scope) caught both, plus 2 correctly-downgraded LOW residuals.

**Open (Harrison):** (1) merge `claude/gap-detector-calibration` -> `main`; (2) ONE restart (bot-loaded: `gap_detection`, `context_loader` kb_meta, the app.py sentinel line); no .env change, no task registration. (3) Acceptance fixture: re-ask one smoke with FRESH wording (7d dedup blocks identical re-asks - e.g. "who maintains the office plants?") and confirm a `detector: "unknown_response"` record in `logs/knowledge-gaps.jsonl` carrying `best_distance`. Accepted residuals (flag-only, bounded by 7d dedup + 15/day cap, over-log not leak): a qualified-affirmative opener "I don't have any concerns, ship it" can over-log (dropping `any`/`it` from `_UNKNOWN_RES[0]` would lose real "I don't have any record/information of X" misses - not worth it); a blocked-topic (non-PHI, non-LEX) question can enter the gap log, but every egress consumer re-defends (escalation re-screens LEX+3-predicate-PHI+`_financials_is_blocked` fail-closed; mining Harrison-gated; MED-2 shrank this window). SEPARATE (not this branch): the 13 `test_drive_extractor.py` reds are the D-066 WS-4 pause on a stale test file - needs its own Code fix.

## D-068 - Thread-follow-up access parity: Path 2 runs check_access; dead app.py cluster removed (2026-07-02)

**Context:** Hygiene Session 2 (spec `_shared/projects/cora/2026-07-02_fndr_cora-hygiene-cleanup-spec.md` section 2). `handle_message_event` Path 2 (active-thread follow-up) ran rate-limit + sibling_guard + cross_entity_guard but SKIPPED `user_access.check_access` -- the ONLY Q&A surface without it (mention ~:872, /cora-ask ~:757, DM Q&A ~:1242 all had it; `_dispatch_qa` has no internal check). Consequence: on an in-thread follow-up the entity-authorization refusal, D-064 finance-content deflection, and PHI topic block were bypassed (entity firewall still held via sibling/cross; live financial data stayed gated at the tool layer -- the pre-LLM layer was the gap). Flagged 6/18 + R1a; cascade section 15e residual. Branch `claude/hygiene-appfix` off `main`@`6ef9153` (Session 1 merged first), 4 commits, suite 5,617 -> **5,626 / 42 sk**. **BOT-LOADED (app.py) -> ONE restart after merge.**

**Decisions / what shipped:**
- `6a60e1b` -- Path 2 access block mirrors handle_mention + /cora-ask EXACTLY: same params (`phi_custodian` via `lex_phi_access.phi_allowed` with is_dm from the channel-id prefix; `tier` via `channel_classifier.tier_label(classify_function(channel_name))`), same ordering (check_access -> sibling -> cross), refusal posted in-thread. PURE PATH PARITY -- user_access / channel_classifier / lex_phi_access / D-064 logic untouched. `tests/test_thread_followup_access.py` (9): mocked ordering/param-pin legs + REAL-check_access integration legs (company-finance follow-up from a financials-blocked sales role deflected with no entity-code leak; commercial deal-scoped follow-up passes; unknown user fails closed).
- `ef57804` -- dead-cluster removal, each symbol verified at zero external call sites: app.py's `_DECISION_RE` copy + `_is_decision_content` + `_ENTITY_CHANNELS_CACHE` / `_load_entity_channels` / `_entity_leadership_channel` / `_entity_finance_channel`. VERIFY-FIRST: the cluster was silently BROKEN, not just dead -- app.py has no module-level `Path` import, so the loader raised NameError inside its bare try/except and permanently cached `{}` (the accessors could only ever return None). `entity-channels.yaml` KEPT as reference data with a no-runtime-readers header (the spec's "read elsewhere" premise was stale -- fireflies_action_extractor uses its own hardcoded `_ENTITY_CHANNEL` map). reconciliation_engine's own `_DECISION_RE` is live, untouched.
- `f347e9a` -- D-051 remediation: unfurl_links/unfurl_media parity on the refusal post; the confirm-echo residual pinned in tests + a code comment; unused test constant removed; stale `meeting-capture-lex-scope.yaml` comment corrected (digest channels come from the hardcoded extractor map, not the yaml).

**Adversarial review (D-051 -- the streak holds):** 33-agent fleet (6 find lenses -> 3 refutation votes per finding): 5 SHIP / 1 SHIP_WITH_FIXES; 2 confirmed (1 MED + 1 LOW), ~7 refuted by the panel. THE MED: a staged-write CONFIRM reply that echoes a blocked-topic phrase from the preview ("yes, the DDD revalidation one" -> phi; "yes, create the task to hire the merchandiser" -> hr) is now refused on Path 2 where it previously slipped through -- verified live against the real roster. **ACCEPTED BY DESIGN, not exempted**: it is exact parity with the mention path (the same sentence @mentioned blocks on main today) and a confirmation-shaped exemption would be a smuggling hole; a bare "yes"/"confirm" recovers and completes the staged write. The green suite caught neither confirmed finding.

**DOCTRINE:**
1. Every user-text path into `_dispatch_qa` / an LLM call runs the SAME gate stack (check_access -> sibling -> cross) with identical params. A "follow-up" surface is a full Q&A surface -- when adding one, grep the existing surfaces' guard blocks and mirror them wholesale, then pin the params in a test against the mention path.
2. Never exempt confirmation-shaped text from an access gate -- a blocked user can phrase any question as a confirmation. Document + test-pin the echo-refusal residual AND the recovery path (a bare confirm word must keep passing) so neither silently changes.
3. A cluster behind a broad try/except can be dead AND broken at once (a missing import swallowed as a logged warning for months). When verifying "is this called?", also verify "did it ever work?" -- a provably never-working path has no behavioral dependents, which changes the removal-risk calculus.
4. When a data file's only consumers disappear, say so IN the file (a reference-only header) -- otherwise the next auditor reads the orphan as a wiring bug and "fixes" it.

## D-069 - Test-coverage foundation: guard-parity integration suite + AST coverage guardrail (audit slice 03) (2026-07-03)

**Context:** Forensic audit v2 (`_shared/projects/cora/2026-07-02_fndr_cora-full-audit-report.md` + `-backlog.md`) CRITICAL W7-01 + 7 siblings. The suite exercised the preserve-list invariants (cross_entity / sibling / clinical_phi / slack_egress / lex_phi_retrieval_scrub / user_access_refusal) only in MODULE ISOLATION; of the four Q&A egress surfaces, only Path 2 (D-068's `test_thread_followup_access.py`) was driven end-to-end through the real guard chain -- so the exact D-068 defect class (a gate present in a module but skipped by a handler before egress) was structurally invisible for handle_mention, /cora-ask, and DM Q&A. Branch `claude/audit-slice-03-test-coverage` off `main`@`aa82136`. TEST-ONLY -- no production code touched, no restart. Suite 5,626/42-skipped -> **5,731 / 0-skipped**.

**Decisions / what shipped (all test files):**
- W7-01 (CRITICAL) `tests/test_surface_guard_parity.py`: parametrized module driving ALL FOUR surfaces (handle_mention, handle_cora_ask, _handle_dm_qa, handle_message_event Path 2) end-to-end through the REAL user_access.check_access -> sibling_guard -> cross_entity_guard chain (mock only _dispatch_qa + infra). Mocked-guard layer pins gate PRESENCE + ORDERING; real-guard layer pins behavioral invariants (D-064 company-finance deflection with no entity-code leak, cross-entity redirect, unknown-user fail-closed on channel surfaces, authorized commercial pass). Modeled on test_thread_followup_access.py.
- W7-06 (MED) same file: AST guardrail over app.py -- every _dispatch_qa site must have a preceding check_access (or be on a documented `_UNGATED_ALLOWLIST`), every gated surface must have a driver (COVERED_SURFACES), backstopped by a PINNED dispatch-site inventory (multiset of (func, entity-literal)) + a 1:1 ungated<->allowlist count.
- W3-03 (MED) `tests/test_tool_exposure.py`: TOOL_DEFINITIONS == _TOOL_FUNCTIONS, every target callable, no duplicate names, _TOOL_TIMEOUTS subset of _TOOL_FUNCTIONS.
- W3-06 (MED) `tests/test_untested_tool_wrappers.py`: the 4 zero-coverage tools -- slack_send_dm (LEX block / confirmed gate / unmapped / mapped+confirmed send), financial_get_pulse + financial_get_close_pack (finance-channel gate; close_pack period-required; pins the current divergent W3-04 gate), f3_create_sales_deck (delegation).
- W7-02 (HIGH) `tests/test_notion_connector.py`: import `_DB_ID` -> `_CONTRACTS_DB_ID` (module never defined `_DB_ID`) + narrowed the Layer-B skip guard `except (ImportError, SyntaxError)` -> `except ModuleNotFoundError`, so symbol drift hard-fails collection instead of masquerading as an env skip. 41 Layer-B tests now RUN.
- W7-04 (LOW) `tests/test_lex_tools.py`: deleted the orphaned `TestGetStaffPulse` (lex_staff_pulse removed from dispatch in D-058; also the 2 slowest tests).
- W7-03 (MED) `tests/test_slack_egress.py`: replaced the permanently-skipping (aiohttp-absent) async-guard positive test with a sys.modules-stub test that injects a fake `slack_sdk.web.async_client.AsyncWebClient` and asserts `_guard_async_webclient` makes construction raise -- runs unconditionally.
- W7-05 (LOW) `tests/conftest.py` + deleted `tests/conftest_calendar_patch.py`: removed the ~150-line `_patch_calendar_client_scheduler` injection. Verified inert-on-host (shipped `cora.tools.calendar_client` exports `_round_up_to_slot`/`find_next_available_slot`/`format_slot_proposal_for_llm`/`get_free_busy`, so the guard early-returns) and the shim was imported by nobody -- removes a conftest-fork false-green hazard.

**Verification -- mutation sweep (D-051 non-theater proof), all git-reverted:** remove check_access on EACH of the 4 surfaces -> that surface's parity tests + W7-06 RED; desync the tool maps -> W3-03 RED; break slack_send_dm's LEX block or confirmed gate -> W3-06 RED; break the async guard -> W7-03 RED; reintroduce `_DB_ID` -> notion collection HARD-FAILS (not skip). Review-fix guards: a new/duplicate ungated dispatch -> pinned inventory + ungated-count RED; drop a refusal's thread_ts -> routing RED; wrong entity to phi_allowed -> phi positional pin RED.

**Adversarial review (D-051 -- streak holds):** 17-agent fleet (6 find lenses -> per-finding refutation verify), 0 refuted / 11 confirmed-or-partial -- ALL against the TESTS' future-robustness, ZERO against production behavior. Folded in: (HIGH cluster) `_is_gated` is line-positional not control-flow-dominance and the allowlist keyed on (func,entity) alone -> a future new/duplicate ungated dispatch in handle_message_event could slip past -> closed with the pinned dispatch-site inventory + ungated-count (both mutation-verified). (LOW) refusal-routing assertions (channel/thread_ts were never checked); the `"F3E" not in text` leak check was dead on the financials-deflect path -> moved the real no-entity-leak assertion to the unknown-user entity-authorization branch where a leak actually surfaces; phi_allowed positional-arg pin; a self-explaining roster-precondition test so a future user-permissions.yaml drift fails with a clear message not a misleading "dispatch called". (Accepted, fails-safe:) `_enclosing_func` over-flags if a dispatch moves into a nested closure -- documented; the inventory pin is the robust net.

**DOCTRINE:**
1. An invariant tested only in module isolation does NOT protect the egress surface that must CALL it -- drive each real handler end-to-end through the real guard chain (mock only the LLM/egress), so a handler that forgets/reorders a gate fails RED. Structural cure for the "green suite misses the real defect" streak.
2. A test is theater until a mutation proves it RED -- for every new invariant test, break the guarded behavior git-revertably against the real source and confirm the failure; "green under mutation" means it asserts nothing (or the mutation missed its target -- verify placement, e.g. a `replace(..,1)` can hit an earlier occurrence inside a mocked-out callee).
3. An AST "gate precedes dispatch" check by line-number is a coarse proxy, not control-flow dominance; back it with a PINNED call-site inventory so any new/moved/duplicated egress site trips a conscious re-verification, and anchor by-design allowlist exceptions to a COUNT (not a bare (func,entity) key) so a second exception can't hide behind the first.
4. A skip guard must skip only on a genuine environment failure (ModuleNotFoundError) -- swallowing a name-level ImportError/SyntaxError lets real code/test drift hide behind an "environment" skip reason.
5. Before deleting test infra, prove it inert on the real host (the shipped module already provides what the fork injected) AND that nothing imports it -- a stale conftest fork of production logic is a false-green hazard, not a safety net.

**Open (Harrison / Cowork cascade):** branch `claude/audit-slice-03-test-coverage` committed (fix `39a409f` + this docs commit), NOT pushed/merged pending Harrison. Merge to `main` (test-only, no restart). Remaining audit slices are separate: W1/W9 god-file splits (app.py / tool_dispatch.py), W2-01/W6-01/W6-06 PHI backstops, W3-01/W3-02/W3-04 tool-timeout + gate fixes, MK-01 deal-sync drop-both, .env.example regen (W9-01), doc-sprawl archives (W11-02/W8-02).

---

## D-070 - Audit Slice A: config/DR + hygiene sweep (16 findings) (2026-07-03)

Branch `claude/audit-slice-01-config-dr-hygiene` off local main `c9d13cc` (D-069 merged; note origin/main was still `aa82136` = D-068, i.e. the D-069 merge was local-only/unpushed when this slice branched). NOT pushed/merged pending Harrison. Suite 5,740 green. NO restart (doc/config + one bot-loaded but behavior-neutral guard). VERIFY-FIRST corrected the audit in several places.

**SHIPPED in-repo:**
- **W9-01 (HIGH)** `.env.example` regenerated from the actual env-read set (39 -> 108 documented keys), grouped by domain, each with code-default + one-line purpose. The RESERVED drive-extractor pause is pinned **`DRIVE_EXTRACTOR_PROPOSALS_ENABLED=0` ACTIVE** (code default is `"1"`=ENABLED) so a rebuild-from-example reproduces the paused state -- the silent-reversal fix. Durable guard: new `tests/test_env_example_coverage.py` fails if any `os.environ`/`getenv`/config-wrapper key read under `src/` is undocumented (3 extraction patterns: direct call, `*_ENV="KEY"` constant indirection, config.py `get("KEY")` wrapper; full-text scan so multi-line reads are caught; commented `# KEY=` counts as documented). Pins the pause + the dead-key removal. **DID NOT** flip the code default of the pause flag (RESERVED -- documenting the value in .env.example is sufficient and honors "do not change it").
- **W9-04** removed the dead `LINKEDIN_SPY_CHANNEL` (Apollo->Make; read nowhere). APOLLO_API_KEY + META_APP_* kept under a labeled "legacy/not-read-by-current-code" section (flagged, not deleted -- out of slice scope).
- **W3-07** the stale D-028 3-tier timeout doctrine (8/15/25) updated to the real 6-tier scheme (8/12/15/20/25 + 60s dossier; default 15) in three places: the `_TOOL_TIMEOUTS` header comment (declared source of truth), repo `CLAUDE.md` D-028 row, and a SUPERSEDED note on D-014. No code change.
- **W4-03 / W8-03** removed the stale `Cora - Clover Daily Summary` from `scheduled-task-state.yaml` disabled: block (task removed from host, like LinkedIn Spy); kept `cowork-clover-daily-pull` (still present-but-Disabled). Updated the pinning test (`test_nightly_health_check.py`) to assert-absent.
- **W4-04** `setup-drive-extractor-task.ps1` trigger `04:00`->`04:05` (matches the B1 de-collision map) + a warning header (re-registering must keep 04:05; the pause lives in .env, not the task). ASCII-only.
- **W11-01** `cora-constitution.md` refreshed: bullet threshold 4+ -> 3+ (matches `_UNIVERSAL_RULES`); added D-032 Slack-native format, D-064 finance-precision, D-068 thread parity; declared the machine files the source of truth; changelog rows.
- **W11-03** created the 3 missing repo `design/known-answers/{f3c,hjrprod,ufl}.md` stubs the entity map references (fallback store completeness).
- **W6-05 (entity-firewall strengthening)** `drive_sweep._ingest_file` now rejects a non-canonical post-split entity (a Haiku-hallucinated off-menu code like the audited `F3`) and falls back to the file owner's canonical `entity_default` instead of minting a novel entity; `_CANONICAL_ENTITIES = frozenset(drive_materializer.ENTITY_CODES)` (single source of truth). Tests + a prompt-vs-canon anti-drift test.
- **W6-04 / W6-05 (KB writes -- GATED, NOT run)** `scripts/fix_kb_hygiene.py` (`--dry-run` DEFAULT, read-only; `--apply` Harrison-gated): re-tag the single `entity='F3'` chunk (`04061ff8-...`, an OSN Val Vista receipt) -> `OSN` (aborts unless exactly 1 F3 chunk + content marker); refresh the cosmetic `checkpoint_state kb_bin_index_ready` count 262441 -> live 605,982 (only `ready` is load-bearing; count is unread). Dry-run verified.

**FLAGGED (Cowork / Harrison actions, NOT forced in-repo):** W11-02/W8-02 Drive doc-sprawl archive (the executed 2026-06-16 rebuild set + 2026-06-17 phase-3 prompts -> `_shared/projects/cora/_archive/`; cross-ref grep done -- moving requires updating live references, see the cascade report); W6-02 the 3.44 GB `backups/2026-06-07/cora_kb.db.bak` disk-delete (`.gitignore` already covers `/backups/` -- so only the host delete remains); W4-05 the 4 off-window same-minute task collisions (02:00/14:00/16:00/22:00 AZ; document-as-accepted, the 02:00 kb-sync-slack+qbo-token-refresh double is the one worth restaggering); W4-04 broader sweep (~6 setup-*.ps1 `-At` times still pre-B1; exact B1 map in the cascade report); W11-04 the 140 KB repo-root `CLAUDE.md` build-log trim (note-only).

**Adversarial review (D-051 -- streak holds):** 5-agent fleet (security/preserve-list, W9-01 env coverage, drive_sweep guard, KB script/registry/setup, doc accuracy). 0 findings against PRODUCTION behavior; every real finding was TEST/DOC-quality -- exactly the "green suite misses it" pattern. Fixed pre-push: (1) the drive_sweep reconciliation test was TAUTOLOGICAL (`_CANONICAL_ENTITIES` IS `frozenset(ENTITY_CODES)`) -> added a real test that the classifier PROMPT's allowed codes collapse to the guard set (which itself fired on my own regex bug -- `[A-Z]{2,}` dropped the digit codes F3E/F3C -- proving it bites); (2) the env-coverage extractor was line-by-line so multi-line reads (config.py `get(\n "QBO_REDIRECT_URI")`) slipped -> switched to full-text scan; (3) `OSN_SCHEDULER_ADMIN_USER_IDS` mislabeled `# default` when the code default is `""` -> relabeled operational; (4) W6-04 count-semantics clarifying comment.

**DOCTRINE:**
1. A one-time regen is not a fix -- pair every "regenerate X from the code" cleanup with a guardrail TEST that fails on future drift (here: src env-reads must be a subset of `.env.example`). The extractor must model how the code ACTUALLY reads env (direct call + `*_ENV` constant indirection + the config.py wrapper) and scan full-text, or it silently under-covers.
2. A RESERVED pause is safest fixed by pinning its ratified value in the template that a rebuild copies (`=0`), NOT by relying on the live `.env` alone -- but do NOT change the code default when told the flag is RESERVED; the template pin closes the DR hazard without touching runtime.
3. Verify a "cosmetic" claim before writing to a live store: confirm the field is unread (grep every consumer) -- `kb_bin_index_ready.count` is written by the migration but read by nobody; only `ready` gates the fast path.
4. An anti-drift reconciliation test must compare the two things that can ACTUALLY diverge (the classifier prompt's allowed codes vs the guard set), not a tautology (`X == X` where both are the same import).
5. A config-hygiene fix to a scheduled-task registry can require a test edit -- `test_nightly_health_check.py` pinned the stale Clover entry; removing the yaml line without the test would have gone RED. Grep tests before editing a data/config file.

## D-071 - Audit Slice B: scheduled-task reliability (W4-01 self-budget+resumable checkpoint, W4-02 fail-loud delivery, W4-07 LastResult probe) (2026-07-04)

Branch `claude/audit-slice-02-task-reliability` off `main`=`bb44652` (D-070 merged; note the slice prompt's "base aa82136" line was stale). Pushed, NOT merged (Harrison). Suite **5,778 green** (+31 tests). NO restart -- all three fixes are script-side (each scheduled run spawns a fresh process from on-disk source; the bot is untouched). Opus 4.8 @ xhigh. VERIFY-FIRST corrected the audit twice (below).

**W4-01 -- founders-os-sweep SIGKILLed every run (`src/cora/connectors/drive_sweep.py`, `scripts/ingest_founders_os.py`):** the task's PT2H `ExecutionTimeLimit` SIGKILLed the process mid-LEX-ingest EVERY run (LastResult `267014`=`0x00041306` SCHED_S_TASK_TERMINATED; the audit's `0x0004130E` hex was a typo). `_sweep_folder_tree` accepted `checkpoint_key` but never used it. Fix: `--time-budget-min` (default **105**, 15-min margin under PT2H; 0/neg = unlimited; `--backfill` unbounded) so the sweep stops CLEANLY (no SIGKILL-mid-`commit()` corruption) + a **resumable per-subtree checkpoint** (`{completed_folder_ids, tree_done}`) so a subtree bigger than one window (LEX) chips away across runs + **neediest-first entity ordering** (never-completed first, then stalest watermark). The watermark advances -- and checkpoints clear -- ONLY when EVERY subtree of an entity returns True (fully walked); a budget-cut subtree returns False -> watermark untouched, checkpoints persisted. The root tree gets `skip_folder_ids` = the matched sub-entity folder IDs so it never re-tags a sub-entity file as the parent entity under a fresh-`seen_file_ids` resume (firewall preserved).

**W4-02 -- finance-receipt-digest silently drops (`src/cora/finance_receipts.py`, `scripts/run_finance_receipt_digest.py`):** the digest files docs then fails the Slack post to an ARCHIVED `#hjr-finance` (verified LIVE still archived 2026-07-04). The runner already exited 1; what was missing was a HUMAN alert. Added `alert_delivery_failure` -- a **metadata-only** notice (count + reason + fix, NO vendor/amount/subject lines -- finance firewall preserved) to a live ops fallback (`FINANCE_DIGEST_FALLBACK_CHANNEL` -> `HEALTH_REPORT_CHANNEL` -> `hjrg-leadership`) + kept the nonzero exit. The channel un-archive/repoint is a Harrison host action (repoint moves the `check_request` cross-mailbox retrieval-power boundary too -- deliberately NOT in the diff).

**W4-07 -- health check ignored LastResult (`scripts/nightly_health_check.py`):** `check_scheduled_tasks` classified STATE only, so a "Ready but failing every run" task never alarmed (this is why W4-01/W4-02 persisted). Added `check_task_last_results` + pure `_classify_task_last_results` (left the tested State classifier untouched): WARN (never critical) on a nonzero, non-benign LastResult for an ENABLED task; skip Disabled (idle by design); benign codes `{0,267008,267009,267011}`; signal-OK name allow-list `{Cora - QBO Token Monitor (exit 1 = a real token finding it already DM'd; freshness-monitored by check_qbo_monitor), cowork-cora-health-check (self-referential)}`. Live dry-run of the new probe: **62 tasks -> exactly 2 WARN (the two real failures), 0 false positives, 12 Disabled skipped.**

**VERIFY-FIRST corrections to the audit:** (1) "LEX runs last" is imprecise -- the top-folder order is Drive-arbitrary (today OSN->HJRPROD->LEX); LEX is just the first large/un-watermarked entity and starves everything after it. (2) UNDERSTATED harm: live `sync_state` showed only 3 of 10 entities ever got a watermark (F3E frozen 2026-06-04, HJRPROD + OSN today) -- **7 entities (FNDR/HJRG/HJRP/BDM/LEX/F3C/UFL) have NEVER completed a founders_os sweep.**

**Adversarial D-051 fleet (5 lenses -> per-finding skeptic; streak holds):** 4 raised, 2 CONFIRMED, 2 refuted. The green 5,773 suite missed both confirmed. **CONFIRMED-1 (MEDIUM, fixed):** a file dropped into an already-`completed` subtree BETWEEN runs of a multi-run sweep was skipped on the completing run AND then fell permanently below the advanced incremental watermark -> silent KB loss. Fix: advance the watermark on completion to the ORIGINAL sweep-start (an eagerly-pinned `founders_os_startmark_*` checkpoint), NOT the completing run's clock, so mid-sweep drops stay above the next cutoff (re-enumerated; idempotent upsert). **CONFIRMED-2 (MEDIUM, fixed):** no test pinned that `sweep_founders_os` threads `deadline_monotonic` into `_sweep_folder_tree` -- dropping the kwarg would reinstate the SIGKILL green. Added `test_deadline_threaded_into_all_tree_calls` (+ 2 start-marker tests + a mid-page-cut test). Refuted: a by-construction finance-firewall assertion; a coverage gap on correct code.

**DOCTRINE:**
1. A resumable-sweep watermark must record the time the (multi-run) sweep BEGAN, not the run that finished it -- else any source item created between an early run's folder-completion and the sweep's completion is skipped-then-buried under the advanced watermark and silently lost forever. Pin the sweep-start eagerly (survives even a hard SIGKILL) and advance the watermark to it.
2. `ExecutionTimeLimit` SIGKILLs the process (can corrupt a mid-`commit()` SQLite write); the real control is a script-side self-budget that exits CLEANLY a margin under the limit + a checkpoint. (Extends the D-055 briefing / D-053 filer SIGKILL-doctrine class to the founders-os sweep.)
3. A watermark that advances only on all-subtrees-complete is corruption-proof by construction -- but ONLY if the completing run doesn't skip un-ingested work; the completed-folder skip is a perf optimization that MUST be paired with the sweep-start watermark (doctrine 1) or it turns "resumable" into "lossy."
4. A health-check LastResult probe must (a) skip Disabled tasks (idle by design -> no false alarm) and (b) allow-list tasks documented to exit nonzero as a SIGNAL (the QBO monitor's exit 1), or it either false-alarms on the fleet or, if the allow-list is too broad, masks a real failure. WARN (not critical) is the right severity so it never flips its own exit code into a self-referential loop.
5. Threading a budget/deadline through N call sites needs a test that BINDS the threading (assert the kwarg is passed), not just tests of the leaf that receives it -- else a refactor drops the kwarg and the leaf tests stay green while the feature dies (the D-069 "green suite misses the wiring defect" class).

## D-072 - Audit Slice D: PHI store-side defense-in-depth (W6-01 ingest deny-list + purge, W2-01 live-retrieval content backstop, W6-06 eval/monitoring) (2026-07-05)

Branch `claude/audit-slice-04-phi-store-defense` off CURRENT `main`=`54b3360` (D-071/Slice B merged; the slice doc's "base aa82136" line was stale). Pushed, NOT merged (Harrison). Suite **5,848 passed / 1 skipped** (+~70 tests; the 1 skip is a pre-existing Drive-file env skip in test_person_identity, not new). Opus 4.8 @ xhigh. ONE bot restart to activate the W2-01 `context_loader` backstop (Harrison-gated, AFTER merge). The purge is built + dry-run-capable but NOT applied (Harrison-gated). This hardens the single-Cora posture while the BAA-for-cloud two-Cora split is negotiated (Track B); it does NOT replace that split.

**W6-01 -- LBHS/LTS PHI entered via gmail + drive_sweep (the deny-list was Slack-only) (`store.upsert_documents` Step 0a + `knowledge_base/lex_sub_entity.py` + `scripts/purge_lex_restricted_kb.py`):** the `lbhs*/lts*` deny-list is a Slack-channel-name filter; gmail/drive_sweep have no channel, so LBHS (42 CFR Part 2) + LTS (Provider-Type-15) content reached the KB via those sources (live: 1,135 LBHS + 665 LTS chunks; counts had GROWN since the audit -> the gap was live). Fix: `is_restricted_lex_ingest(source, sub_entity)` drops any gmail/drive_sweep doc whose RESOLVED sub_entity is LEX-LBHS/LEX-LTS at the ingest chokepoint (after Step-0 tagging, before chunk/embed), mirroring the Slack deny-list for the non-Slack sources. Purge script: dry-run default, CONSERVATIVE default scope (gmail+drive_sweep only), reversible per-run `.bak.jsonl` row-backup, batched, idempotent, heartbeat running-bot guard + `--force`.

**W2-01 -- live-retrieval PHI scrub was entity-tag-gated (`kb_entity=='LEX'`) with no content backstop for a MIS-TAGGED LEX-PHI chunk (`context_loader._withhold_non_lex_phi` + `phi_guard`):** a LEX-PHI chunk mis-tagged under a non-LEX entity (fireflies defaults to FNDR; include_fndr co-scans FNDR into every non-LEX channel) was served UNSCRUBBED, backstopped only by the prompt-only FNDR guardrail (a D-034 violation for a PHI invariant). Fix: a deterministic content backstop for non-custodians that WITHHOLDS a chunk whose content trips the PHI predicate, mirroring `drive_materializer._phi_wall`'s non-LEX branch. Applied on the main block AND the cross-entity fallback.

**W6-06 -- 184k GM-level (NULL) LEX chunks, retrieval-scrub is the sole store-side guard (monitoring only, NO purge):** added a golden-set clinical-PHI battery (`non_lex_phi_backstop` / `non_lex_phi_backstop_live` / `lex_billing_status` guard kinds, weekly deterministic eval) that goes RED if a PHI predicate silently stops firing (the D-059-class regression the green suite has missed), and re-verified the semantic-cache custodian exclusion (`cache_storable ... and not phi_custodian`; the lookup is user-agnostic so the store-side gate is the sole, load-bearing protection). Did NOT purge the GM NULL set (expected per the full-ingest directive D-046).

**Centralization:** one `phi_guard._LEX_PROGRAM_CONTEXT_RE` / `is_lex_program_context`; removed the duplicate `_LEX_CONTEXT_RE` from `drive_materializer`, `person_dossier`, and `run_lex_swept_phi_check` (behavior-identical; the full suite caught the run_lex_swept_phi_check coupling; person_dossier's copy was byte-identical).

**Enshrining-test reconciliation (Harrison sign-off at merge):** `test_non_lex_retrieval_is_never_scrubbed` ENSHRINED the mis-tagged-chunk passthrough. W2-01 flips that posture by design (deterministic backstop replaces the prompt-only net). Rewrote to `test_non_lex_retrieval_clinical_phi_is_withheld` + added ordinary-prose / wellness / commercial-billing / custodian-bypass / fallback / fireflies-title tests. Flagged in-file for sign-off.

**MANDATORY adversarial D-051 review found 8 real issues the green suite missed; ALL fixed + re-gated:**
- **F1 (HIGH) title/deep_link leak:** the backstop vetted only `r.content`; a mis-tagged fireflies chunk whose PHI is the bare-client-name TITLE ("Jalen Alicea Intake Assessment", benign body) was served unscrubbed (apply_tier1 strips titles only for gmail/drive_sweep). Fix: `_citation_carries_phi` neutralizes a kept chunk's citation (title+deep_link) when the source is fireflies, the citation trips the live predicate, or `redact_cue_adjacent_names` alters it -- mirroring `_apply_lex_phi_scrub`'s citation neutralization, without over-stripping legit business titles.
- **F3+F8 (MEDIUM) live-path over-refusal:** the backstop applied `is_clinical_phi`'s bare med-name/dx-term matching to per-query retrieval, silently withholding legit OSN/F3E product copy (melatonin SKU, ADHD/PTSD/ASD framing, lithium). Fix: `non_lex_phi_backstop_trips_live` -- high-specificity clinical framing (DOB/ICD/diagnosed-with) trips unconditionally; bare dx-term/med-name trip ONLY with a co-present care-recipient/program cue. `is_clinical_phi` + the strict predicate (write gate + drive/dossier egress) LEFT UNCHANGED. (Caught + fixed my own bug mid-remediation: I'd initially added `_DOSE_RE`/`_MED_CONTEXT_RE` to the live unconditional group, which would have re-created the "200mg caffeine" over-refusal -- removed, since `is_clinical_phi` excludes them.)
- **F4 (MEDIUM) aggregate-finance over-refusal:** the billing/status leg fired on bare "Lexington member billing volume" (care-noun + admin + program, no individual), withholding ~1,347 legit FNDR/HJRG finance chunks co-scanned into non-LEX channels. Fix: the LIVE billing/status leg additionally requires `_reveals_individual_care_recipient` (care-noun+name OR non-staff possessive; staff roster excluded). `is_lex_billing_status_phi` UNCHANGED -> the D-050 write gate is unaffected.
- **F5 (MEDIUM) stale-chunk survival on re-ingest:** Step 0a dropped restricted docs BEFORE the replace-on-conflict delete keys were computed, so a now-restricted re-ingest left the doc's OLD chunks (incl. NULL-tagged, which the purge can never reach) in the KB. Fix: `_delete_chunks_for_keys` purges stale chunks for dropped docs' (source,source_id) -- even when ALL docs are dropped; the normal path seeds seen_keys with dropped_keys.
- **F6 (MEDIUM) purge missing running-bot guard:** added `_heartbeat_is_fresh` + `--force` + exit 3 (mirrors prune_kb_retention.py / migrate_kb_binary_index.py) so `--apply` never deletes rows out from under the live bot.
- **F2 (LOW) ingest drop was count-only:** added per-doc audit log (source_id/title[:80]/sub_entity). Kept the broad (org-tag) drop -- it mirrors the Slack deny-list; narrowing it to PHI-only (fix A) is deferred to Harrison ("is LBHS/LTS business deliberately kept?" -- the open W6-01 question).
- **F7 (LOW) purge review-listing suppressed on --all-sources:** the most-destructive path was the least transparent; now always lists non-default rows marked WILL PURGE.

**A focused D-051 RE-GATE of the remediation found 1 more CONFIRMED MEDIUM (fixed + re-verified):** the findings-3/8 relaxation OVER-SHOT — the live variant gated a bare med/dx term on a care-noun/program cue but ignored a co-present personal NAME, so "Jalen's risperidone" / "Marcus Johnson is autistic" (name + med/dx, no cue) leaked verbatim to a non-custodian. Fix: the bare med/dx leg now also trips on `_reveals_individual_care_recipient` (possessive or care-noun-governed non-staff name). ACCEPTED, DOCUMENTED residual: a bare full-name-subject / first name next to a med/dx TERM with no possessive/care-noun/program/DOB/ICD/diagnosed-with ("Marcus Johnson is autistic", "Kayla started clonidine") — closing it needs person-name detection that over-refuses legit OSN/F3E copy where med/dx terms co-occur with named stores/brands ("Sprouts carries melatonin", "natural Prozac alternative", "ADHD-style Focus stack"), the co-equal don't-over-refuse mandate. Not a regression (the live non-LEX path had NO backstop pre-slice); the STRICT predicate (Drive/dossier egress) + LEX-channel scrub + custodian gate + entity siloing + fireflies-first classify_lex_meeting remain the primary net; the two-Cora/BAA split (Track B) is the durable fix. Flagged for Harrison.

**Verify-first corrections to the audit:** (1) NOT the slice's "3-predicate union" -- `is_phi_risk` on all non-LEX content over-refuses generic words (assessment/patient/member id/medicaid); the actual `_phi_wall` mirror uses only `is_clinical_phi` + `is_lex_billing_status_phi`+cue (the task instruction confirmed this). (2) NO blanket domain-substring drop -- 1,414 gmail/drive chunks mention `lexingtonbhs.com` + 1,687 mention `lexingtontherapyservices.com`, mostly LBHS/LTS BUSINESS (loans/management-fees/PTO), not Part-2 clinical; a domain drop would nuke ~3,101 chunks. Scoped to content-resolved sub_entity; the residue is caught at egress (W2-01) + monitored (W6-06). (3) Purge conservative-by-default -- the 15 non-swept LBHS/LTS rows (a drive_asset "LBHS unpaid management fees" business file, GM #lex-leadership slack threads, LEX session-captures) are surfaced for review, NOT auto-purged.

**DOCTRINE:**
1. A PHI content-backstop on the LIVE per-query retrieval path must be a NARROWER, identifier-gated variant of the recall-biased write-gate/Drive-egress predicate -- a bare drug/diagnosis TERM with no care-recipient/program/name identifier is legitimate product copy (melatonin SKU, "ADHD-style focus"), not PHI; applying the recall-biased predicate to high-volume retrieval silently over-refuses. Split the predicate; keep the write gate + Drive egress strict.
2. PHI on a retrieved chunk lives on THREE surfaces -- content, title, and the pre-wrapped deep_link LABEL. A body-only scrub leaks a bare-client-name meeting TITLE; the citation surface must be neutralized too (apply_tier1 strips it only for gmail/drive_sweep). Mirror _apply_lex_phi_scrub on any egress that surfaces citations.
3. An ingest deny-list that drops a doc must ALSO delete that doc's STALE chunks by (source,source_id) -- else a now-restricted re-ingest buries old chunks (incl. NULL-tagged rows a sub_entity-keyed purge can never reach). Delete-by-key must run even when the whole batch is dropped.
4. Centralize a shared PHI-wall predicate in phi_guard (one regex, one decision) so the retrieval / Drive-egress / dossier / independent-scanner consumers cannot drift; the full suite catches a removed private copy only if a consumer imports it -- so a green suite is necessary but not sufficient on a PHI change (the D-051 review found 8 holes the 5,822-green suite missed).
5. A destructive KB-delete script must share the running-bot heartbeat guard (+ --force) with its siblings; a prose-only "STOP Cora" is not a control.
6. Re-gating a PHI-wall REMEDIATION is not optional. The fix for an over-refusal (relaxing a predicate on the live egress) is the SAME edit as opening a leak, viewed from opposite directions — and the remediation's own new tests missed the leak it introduced. A focused adversarial re-verification of the remediation caught it. On the PHI wall, review the fix, not just the original.
7. A live-egress PHI backstop trades over-refusal against leakage on a spectrum a regex cannot fully separate (person names vs org/brand names both being Title-case). Pick the identifier-gated middle (possessive/care-noun-governed names + care/program cues), then DOCUMENT the residual on both ends explicitly rather than pretend a regex closed it — the durable fix is the two-Cora/BAA split, not a tighter pattern.

## D-073 - Audit W6-01 Fix-A: narrow the LEX-LBHS/LTS ingest drop + purge from bare-tag to PHI-CONTENT, PER-CHUNK (2026-07-06)

Branch `claude/audit-w601-fixa-phi-content` off `main`=`47b7e61` (D-072/Slice D merged). Pushed, NOT merged (Harrison). Suite **5,871 passed**. Opus 4.8 @ xhigh. Follow-up to D-072's W6-01. Bot-loaded change (store.upsert_documents) -> activates on the next bot restart; the nightly gmail/drive sweeps pick up on-disk code at their next run (script-side). The purge is built + dry-run-verified but NOT applied (Harrison-gated).

**Context / decision.** D-072 (W6-01) added a BROAD ingest drop: `store.upsert_documents` dropped ANY gmail/drive_sweep doc whose resolved sub_entity is LEX-LBHS/LEX-LTS (bare tag). Harrison (2026-07-06): NARROW it to PHI-content, so LBHS/LTS **business** (payroll / fees / PTO / aggregate "client billing" — NOT patient records, NOT 42-CFR-Part-2) is KEPT + retrievable, while clinical/Part-2 + named-billing PHI is dropped and backstopped at retrieval (W2-01).

**Verify-first.** Of the 1,135 LEX-LBHS + 665 LEX-LTS tagged chunks, only **~19–42 carry PHI content**; ~1,758 are business. A pure STRICT predicate over-drops 20 business docs (invoices/fee-schedules/P&L/staff-spreadsheets). So the DROP moved to the PHI-content predicate, keyed PER-CHUNK.

**Implementation (reuses existing predicates — NO new detector).**
- `phi_guard.non_lex_phi_backstop_trips_individual(text, allowed_names)` — TAG-SCOPED variant: clinical FRAMING (DOB/ICD/diagnosed-with) unconditional; bare dx/med term requires a specific INDIVIDUAL (possessive / care-noun-governed non-staff name), NOT the program cue; named billing requires program cue + individual. (See F1 below for why the program-cue leg can't be used on tag-scoped content.)
- `lex_sub_entity.restricted_lex_phi_content_drop(source, sub_entity, title, content, allowed_names)` = scope gate (`is_restricted_lex_ingest`) AND `non_lex_phi_backstop_trips_individual(title+content)`. Single source of truth for ingest + purge.
- `store.upsert_documents` — RESTRUCTURED to PER-CHUNK: chunk first (Step 1), then drop only the chunks whose own title+content carries PHI (Step 1a). F5 stale-chunk replacement preserved via `seen_keys = kept-chunk docs + _phi_filtered_keys`. Staff roster loaded only when a restricted candidate is present; if the roster is empty/unavailable the drop is DEFERRED for the batch (keep; W2-01 guards).
- `scripts/purge_lex_restricted_kb.py` — targets PER-CHUNK PHI-content chunks (business kept); `phi_breakdown` shows total/PHI/business; `non_default_rows` flags PHI vs business; empty-roster warning; heartbeat guard + `--force` + dry-run default + reversible `.bak` preserved.
- Live dry-run: **19 PHI chunks targeted** (18 LBHS + 1 LTS drive_sweep); every business chunk kept (cash-flow spreadsheets, fee schedules, recruiting emails, aggregate billing, all gmail LBHS/LTS).

**Both directions verified:** business survives (no over-drop — incl. large mixed financial docs); localized Part-2 clinical + named-billing PHI still drops. STRICT predicate (write gate + Drive/dossier egress) + is_clinical_phi + is_lex_billing_status_phi + non_lex_phi_backstop_trips_live (W2-01) all UNCHANGED; the W2-01 retrieval backstop is the second layer.

**MANDATORY D-051 review — 3 confirmed findings, ALL fixed + re-gated:**
- **F1 (HIGH over-drop):** Fix-A initially reused the W2-01 LIVE predicate, but on content ALREADY tagged LBHS/LTS the Lexington/behavioral-health PROGRAM cue is present BY CONSTRUCTION (the tag keyword IS the cue — 66% of tagged chunks), so its "bare dx/med + cue" leg degenerated and over-dropped business docs mentioning a diagnosis in a school name / job title / fee schedule (3 real recruiting emails). Fixed: the tag-scoped `non_lex_phi_backstop_trips_individual` gates the bare dx/med leg on a specific INDIVIDUAL, not the program cue.
- **F2 (MED over-drop):** the staff-roster fail-soft-to-empty made staff possessives read as care recipients → over-dropped staff-billing business. Fixed: defer the ingest drop for a batch when the roster is empty/unavailable (keep; W2-01 guards); purge warns.
- **F3 (HIGH purge):** the purge evaluated per-chunk while the ingest dropped whole-doc — a multi-chunk Part-2 doc kept 12/13 chunks. The reviewer proposed making the purge WHOLE-DOC to match the ingest; **verify-first against the live corpus REJECTED that**: whole-doc grouping over-purges large mixed BUSINESS docs (a 131-chunk "Weekly Cash Flow Standing ACTUAL", P&L, tracking spreadsheets) whose joined 500KB trips the billing leg on billing-words + "Lexington" + a name in unrelated rows. Resolution: make BOTH the ingest and the purge PER-CHUNK (a chunk drops only on its own local PHI). Accepted residual: PHI split across chunk boundaries (a name in one chunk, a med in the next) is not caught at ingest/purge — the same accepted-residual class as D-072, guarded at retrieval by W2-01 + the strict Drive egress; the two-Cora/BAA split (Track B) is the durable fix.

**Focused re-gate of the per-chunk restructure — 1 more CONFIRMED HIGH (F4), fixed + re-verified:** the re-gate proved that ALL 19 chunks the (post-F1/F3) predicate targeted were BUSINESS, none patient PHI. They tripped the billing leg because `_reveals_individual_care_recipient` reads ANY unrostered possessive as a care recipient — bookkeeper AR sheets ("Rita Hill's"), vendor P&L lines ("Lowe's"/"Roper's"), "Employee's" M&A clauses, a CFO invoice ("Kuska's"), a Chase-wire "recipient Cust[omer]" — combined with finance billing vocab + the tag-implied program cue. Fix: the BILLING leg now requires a care-recipient-noun-GOVERNED name (`_names_governed_care_recipient` → `_CARE_RECIPIENT_NAME_RE`, "client John"), NOT a bare possessive; and the banking sense of "recipient" is excluded (a wire payee is not a care recipient). The dx/med leg keeps possessive detection ("Jalen's risperidone" is PHI regardless). Live re-verification: **targets 0 of the 1,800 tagged chunks (was 19), 0 clinical-framing chunks exist in the set** — the LBHS/LTS gmail/drive tagged corpus is entirely BUSINESS + non-individual clinical descriptors; the drop keeps all of it and the guard is ARMED for future localized PHI (framing / dx+individual / care-noun-governed billing).

**Accepted residuals (documented; all downstream-caught):** (a) a sentence-capitalized care-noun ("Client John's ...", `_CARE_RECIPIENT_NAME_RE` is case-sensitive on purpose so it won't read "Member Services" as a person) and (b) a bare-possessive named client with no care-noun ("Bob Smith's BHRF authorization") are NOT dropped at INGEST — but ARE caught at RETRIEVAL by the W2-01 LIVE variant (possessive-based, unchanged), the STRICT Drive/dossier egress, and the D-050 write gate. Administrative billing PHI (not 42-CFR-Part-2 clinical) handled by the layered defense; the two-Cora/BAA split (Track B) is the durable fix. (c) PHI split across chunk boundaries (per-chunk) — same class.

**DOCTRINE:**
1. A durable-store ingest deny-list for a regulated sub-entity should key on PHI CONTENT, not the bare entity tag, when the tag is over-inclusive (LBHS/BHRF fire on payroll and patient notes alike; ~1,758 of 1,800 tagged chunks are business). Scope on the tag; DROP on the content.
2. A tag-scoped PHI predicate must NOT reuse a cue that the tag itself implies. The W2-01 LIVE predicate treats a Lexington/Medicaid PROGRAM cue as an identifier — correct on the general retrieval path where the cue is RARE, but on content already tagged LBHS/LTS the cue is present by construction, so the leg degenerates and over-drops business. On tag-scoped paths, require a specific INDIVIDUAL, not the program cue.
3. Evaluate the PHI drop PER-CHUNK, not whole-doc: a whole-doc decision over a large mixed business document (cash flow / P&L / tracking) trips the billing leg on billing-words + a program cue + a name in UNRELATED rows across hundreds of KB, over-dropping critical business. Per-chunk drops a chunk only when a client name + billing/dx co-occur LOCALLY. The cost — PHI split across chunk boundaries, and a business doc's non-identifying clinical boilerplate — is accepted, guarded at retrieval (W2-01) + the strict Drive egress.
4. An ingest DROP is not an egress redactor: an empty/unavailable staff roster must fail toward KEEP (defer the drop; retrieval guards), NOT toward over-redact — the "empty = safe direction" framing is correct for the Drive/dossier redactors but WRONG for a permanent, un-backed ingest drop whose intent is to retain business.
5. Ingest, purge, and retrieval must share ONE predicate + ONE text-assembly so a chunk dropped at ingest is the same chunk the purge removes and the retrieval backstop withholds — verified by construction (restricted_lex_phi_content_drop == purge._is_phi == non_lex_phi_backstop_trips_individual over title+content).
6. On FINANCIAL business content the billing/status PHI leg is a false-positive machine: "billing"/"invoice"/"claims" are ordinary vocab and every ledger has possessives (a bookkeeper, a vendor, an "Employee's ..." clause, a wire "recipient"). Requiring the individual to be a care-recipient-noun-GOVERNED name ("client John"), NOT a bare possessive, is the discriminator — a possessive next to a MEDICATION stays PHI (that's the dx/med leg), but a possessive next to BILLING is business. Verify a "PHI-drop" against the live corpus by INSPECTING what it targets: the re-gate found the drop's 19 "PHI" targets were 19 business docs and 0 patient records — a green suite + a plausible predicate hid a fully-inverted outcome.
7. VERIFY-FIRST beats the reviewer's suggested fix when the fix is untested against real data: the D-051 review's finding-3 remedy (make the purge whole-doc to mirror the ingest) was correct in principle but, checked against the corpus, would have wholesale-purged the "Weekly Cash Flow Standing ACTUAL" + P&Ls; the right resolution was the opposite (per-chunk both). Trace the proposed fix through the live corpus before adopting it.
