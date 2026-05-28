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
