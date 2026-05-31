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
