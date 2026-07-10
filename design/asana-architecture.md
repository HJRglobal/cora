# Asana Architecture — how Cora maps work into Asana (Phase 1.10)

**Purpose.** The canonical reference for how Cora's task creation (Fireflies
meeting capture, `react-to-task`, etc.) maps onto the HJR Global
Asana workspace. Harrison #4 ("Asana is messy; I need a Goals / Portfolio /
Project / Task framework"). This doc is the human-readable model; the machine
sources of truth are the two YAML maps in `data/maps/` (below).

**Workspace:** HJR Global, GID `682743441507584`.

---

## 1. The Asana object model, and how Cora uses each level

| Level | What it is | How Cora uses it |
|---|---|---|
| **Goals** | Org/team OKR objects (a separate Asana resource from tasks). | Read-only context. Cora does NOT create or close Goals. Goal objects never reach the capture task feed — `get_user_tasks` hits `/tasks?assignee=`, which returns tasks only. (Stale 2025 "goal"-named *tasks* are filtered out of the morning brief by the 1.7 staleness filter, not here.) |
| **Portfolios** | Roll-ups of projects for a cross-entity view; also where shared custom fields (Entity/Status/Priority) are defined. | Read-only. The "FNDR portfolio fields" are the canonical Entity/Status/Priority custom fields (see §5). |
| **Projects** | The unit Cora writes into. Every captured task MUST land in a real project (§4). | Routing target. Naming `[ENTITY-CODE] Function — Description`; Harrison owns every project (governance standard, below). |
| **Tasks** | The work item Cora creates. | Created with name + assignee + project + (best-effort) custom fields. |
| **Subtasks** | Children of a task. | Cora does not create subtasks today; captured items are top-level tasks. |

**Governance standard (canonical, founder-level):**
`00-Founder/asana-governance-standard-2026-06-08.md` — naming convention,
Harrison-as-owner-on-every-project, the team taxonomy, and the 2026-06-08
restructure. This doc defers to it for org-level policy and only describes the
Cora routing layer.

---

## 2. Entity → Team → Project taxonomy

One Asana **team per entity**; projects are entity-prefixed and named
`[ENTITY-CODE] Function — Description`. Each entity has a **catch-all** project
("`[ENTITY] Operations — General`") that is the single drop target when no
function-specific project matches.

Entity codes + catch-alls are enumerated in `data/maps/asana-project-map.yaml`
(`entities.<CODE>.catch_all_gid`). Current set: F3E, HJRG, FNDR (shares the HJRG
catch-all), LEX + LEX-LLC / LEX-LLA / LEX-LBHS, OSN, HJRP + HJRP-RR, POD /
HJRPROD, F3C, UFL (paused). LEX team GIDs and the 2026-06-08 new Harrison-owned
function projects are recorded in that map and in `meeting-capture-projects.yaml`.

**Do not hard-code project GIDs elsewhere.** GIDs are stable across renames; the
YAML maps are the only place they live.

---

## 3. Routing resolution order (how a task finds its project)

`src/cora/tools/project_resolver.resolve_project()` applies, in priority order
(first match wins) — defined by `data/maps/asana-project-map.yaml`:

1. **`blocked_projects`** — Cora NEVER creates here (the 6 Harrison-Private
   projects: Kids' Schedules, Family Travel, Personal Tasks, etc.).
2. **`assignee_rules`** — task assigned to a person → their home project
   (e.g. Tommy → `[F3E] Sales Pipeline — Tommy`).
3. **`meeting_title_rules`** — Fireflies meeting title matches a pattern.
4. **`brand_rules`** (F3E only) — Pure / Mood / Energy keyword → brand-specific
   event/social project.
5. **`keyword_rules`** — keyword in the task title/notes.
6. **`catch_all_gid`** — fallback when nothing else matches.

`meeting-capture-projects.yaml` (`projects:`) holds the per-entity catch-all GID
the capture pipeline falls back to; it is cross-checked to equal each entity's
`catch_all_gid` in the project map.

---

## 4. Capture safety rails (what makes a task eligible to be created)

The Fireflies capture pipeline (`fireflies_action_extractor.py`) enforces, in
code (these are the 1.5 + D-052 guards — keep them; they are deliberately strict):

- **Hard project-required guard (1.5):** if the resolver returns no project for
  a task, the task is **dropped, not created** — this eliminates the My-Tasks
  orphan class for every entity. (An unassigned-but-projected task is still a
  valid backlog item; assignee-required is a deliberate FUTURE tightening — see
  G-1.5 in the rebuild execution log, do not "fix" it.)
- **LEX scoping (D-052):** LEX tasks route ONLY into validated LEX-scoped
  projects (`_resolve_lex_project` → allowlist → LEX catch-all → else skip,
  never created outside LEX); LEX digests post only to LEX channels; task text
  is PHI-scrubbed; **LEX-LBHS excluded by default** (42 CFR Part 2). Clinical
  LEX meetings are skipped before routing.
- **UFL paused:** all UFL routing goes to `[UFL] Strategic Planning`
  (monitor-only); no active UFL work execution.
- **Noise filters (1.5):** FYIs / status updates / Cora's own stock phrases are
  not proposed as tasks.

---

## 5. Required custom fields — and the at-creation-tagging prerequisite

Captured tasks should carry three FNDR-portfolio custom fields. GIDs
(`meeting-capture-projects.yaml` → `custom_fields`):

| Field | Field GID | At-creation value |
|---|---|---|
| Status | `1214566926973275` | Not Started (`1214566926973276`) |
| Priority | `1204547177535963` | Medium (`1204547177535965`) |
| Entity | `1214487026542596` | per-entity enum option (one option GID per entity code) |

`fireflies_action_extractor._capture_custom_fields()` builds these and
`asana_client.set_task_custom_fields()` applies them **best-effort, after
creation** (project-scoped; a field not attached to the task's project is
skipped, never fatal — but note the PUT sends all fields together, so a *wrong*
option GID can drop the whole set for that task).

**⚠️ Verify-first finding (2026-06-16, Phase 1.10):** custom-field tagging at
creation is **currently inert** — the Entity/Status/Priority fields are **NOT
attached** to the new (2026-06-06/08) capture-target projects. Spot-checked via
the Asana API: `[F3E] Operations — General` (`1215470928454227`),
`[F3E] Sales — Wholesale & Retail Pipeline` (`1215477882565190`), and
`[HJRG] Operations — General` (`1215470834914137`) all return
`custom_field_settings: []`. So `set_task_custom_fields` skips every field on
these projects today; even Status/Priority are not actually being stamped. This
is why `meeting-capture-projects.yaml` → `entity_options` is intentionally left
**empty** — filling it would change nothing until the fields are attached.

**To enable Entity/Status/Priority tagging at creation (Asana-admin, Harrison):**
1. Attach the FNDR-portfolio Entity / Status / Priority custom fields to the
   capture-target projects (the per-entity catch-alls + the function-specific
   projects, or to the portfolios those projects belong to).
2. From a project that now has the Entity field, read its enum options
   (`asana_get_project` with `opt_fields=custom_field_settings.custom_field.enum_options.{gid,name}`)
   and record each entity code → option GID.
3. Fill `entity_options` in `meeting-capture-projects.yaml` with those GIDs.
   No bot restart needed — the capture script reloads the YAML each run; the
   change is additive + fail-safe.

Until step 1 is done, captured tasks land in the right project (§3/§4) but carry
no custom-field tags. Routing correctness does NOT depend on the fields.

---

## 6. Overdue-task nudge lane (D-045a + WS10 + WS12)

Distinct from CAPTURE (§3-§5, which CREATES tasks): the nudge lane COMMENTS on
already-overdue tasks to prod the assignee. There is exactly **one** @-mention
nudge engine.

- **Sole owner:** the daily scheduled task `Cora - Asana Hygiene Nudges` (6:30am AZ,
  `scripts/run_asana_hygiene_nudges.py`). The weekly Cowork `hygiene-asana` Step
  4.6b nudge-firing is disabled and **Make 4768887 is permanently deactivated**
  (D-045a). **Never** re-activate Make 4768887 or build a parallel nudge automation.
- **Shared throttle:** both this job and the weekly closure sweep read+append the
  ONE ledger `closure-nudges-throttle.jsonl` via `cora.nudge_ledger`
  (`recently_nudged`, 14-day cross-system window; D-031 ≤1 comment/task/7d).
- **Closed-task guard:** before any comment, `nudge_ledger.closed_task_guard()`
  re-fetches live completion and writes a permanent `already_closed` exclusion
  (shared by both lanes) so a task that closed between listing and firing is never
  nudged (fails open on API error). Skipped in `--dry-run`.
- **Eligibility:** task has a `due_on`, is ≥14 days overdue, is not a Visibility-CPA
  task, is not an Asana system reminder (filtered at the `get_user_tasks` source via
  `cora.asana_filters.is_system_noise_task` — WS12), has no recent KB signal, and
  is past the throttle window.
- **Importance budget (WS10):** each user's eligible tasks are sorted **Tier-0 first**
  (`_importance_tier` — compliance / financial-deadline / LEX-revalidation / P0 /
  urgent). Tier-0 nudges BYPASS the Tier-1 caps (`MAX_TOTAL` 25 / `MAX_PER_USER` 5),
  bounded by `MAX_TIER0` (15), so a critical overdue task is never starved on a
  high-volume day. A nudge-eligible task cut by a cap is logged to
  `data/state/hygiene-deferred.jsonl` — **informational only**; recovery is automatic
  (the next run re-sorts Tier-0 first and re-evaluates), NOT a recovery queue.

---

## 7. Sources of truth

| File | Holds |
|---|---|
| `data/maps/asana-project-map.yaml` | The full routing taxonomy: catch-alls, assignee / meeting-title / brand / keyword rules, blocked projects, per-entity project GIDs. |
| `data/maps/meeting-capture-projects.yaml` | Per-entity capture catch-all GIDs + the custom-field GIDs/options + `field_target_projects` (extra projects to receive the Entity/Status/Priority fields). |
| `00-Founder/asana-governance-standard-2026-06-08.md` | Org-level naming, ownership, team taxonomy, the 2026-06-08 restructure. |
| `src/cora/tools/project_resolver.py` | The resolver that applies the map. |
| `src/cora/connectors/fireflies_action_extractor.py` | The capture pipeline + safety rails (§4). |
| `src/cora/asana_filters.py` | Shared system-reminder filter (applied at the `get_user_tasks` source). |
| `scripts/run_asana_hygiene_nudges.py` | The sole overdue-task nudge engine (§6). |
| `scripts/attach_capture_custom_fields.py` | Idempotently attaches Entity/Status/Priority to capture-target + `field_target_projects` (§5). |
