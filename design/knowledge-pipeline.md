# Cora knowledge pipeline — the one boundary doc

_WS17-B (2026-06-20). The single source of truth for how a fact becomes durable
Cora knowledge: one Harrison-gated promotion queue, operational nudges routed to
domain owners, one knowledge write target, the legacy digest retired._

This replaces the implicit, drifted design where the same job had two UIs, three
copies of the entity→file map, and a drain that surfaced <5 items/week on Mondays
while producers appended ~17k/day. See `memory/decisions.md` D-059.

---

## The one queue

`data/cora-proposed-memory-updates.jsonl` is the **single promotion ledger**. Every
producer proposes into it via `knowledge_review.propose_update` (idempotent on
`update_id` — a re-run/backfill can't re-flood). The 7am Mon–Fri drain
(`scripts/run_knowledge_review.py`) processes it.

### Producers (all → `propose_update`)
| Producer | Task | Emits |
|---|---|---|
| `reconciliation_engine` | `run_reconciliation.py` (daily 5:30am) | asana_task / task_close / hubspot_note / decision_capture (capped 8/pass) |
| `drive_extractor` | daily 4am | generic (Drive "person" facts), + asana/hubspot/decision (capped 50/run) |
| `gap_autofill` | daily 6am | **known_answer** (mined from Slack, or a domain-owner's DM reply) |
| `friction_mining` | weekly Sun 5:30pm | **efficiency** (max 5/run) |
| `#info-for-cora` intake (`app.py`) | live | **generic** w/ `payload.source="info-for-cora"` (a human-fed fact) |

### The drain splits the queue (WS17-B items 3 + 4)
- **Knowledge** → DM Harrison **every run** (no Monday gate): `known_answer`,
  `efficiency`, and `#info-for-cora` generics. Rides the 👍 gate (D-011).
- **Operational nudges** → DM the entity's **domain owner**
  (`data/maps/gap-domain-owners.yaml`) as a decision-SUPPORT suggestion, then
  mark the item `DISMISSED` (`reason=routed_to_owner:<id>`). The owner acts in the
  native tool — **Cora never auto-executes** (asana_task/task_close/hubspot_note/
  decision_capture are people/money/tool actions; decision-SUPPORT, not -MAKER).
  Routing is per-owner + per-run capped and **floor-gated** (only items proposed
  after the WS17-B floor route, so the pre-existing backlog is never freshly DM'd
  to a teammate). **LEX entities are never routed (PHI).**

### The gate (D-011, unchanged)
Harrison's 👍 promotes — **every** knowledge item, no exception. WS17-C (D-060)
RETIRED the prior auto-approve carve-out (HIGH-confidence machine-mined
`known_answer` items used to write WITHOUT a 👍); they now ride the daily DM like
everything else. Canonical writes (`decisions.md`) and all external actions still
require a 👍.

### Cora's read (WS17-C)
Every KNOWLEDGE proposal DM now carries a short, advisory **"Cora's read"**
(`src/cora/coras_read.py`): she retrieves across her own sources (entity-scoped,
PHI-filtered, `user_note`/`team_note` excluded) + checks the entity's existing
known-answers, and classifies the claim — CORROBORATED / CONFLICTS / ADDS-CONTEXT /
NET-NEW + a one-line note — so the 👍 review is low-effort. It is **decision-SUPPORT
only**: fail-soft (any error → no read, the DM still sends), PHI-scrubbed,
source-opaque, entity-scoped, and it NEVER approves or writes anything. Computed at
send time (`run_knowledge_review._attach_coras_read`), stashed on the in-memory
update, never persisted.

---

## The one knowledge write target

Approved knowledge writes to **`design/known-answers/{entity}.md`** — the per-entity
file `context_loader` loads into Cora's system context. There is now **one** entity→
file map: `src/cora/known_answers_map.py` (`ENTITY_FILES`), imported by:
- `gap_autofill` (write: `apply_known_answer`, `apply_contributed_note`),
- `context_loader` (read: `_KNOWN_ANSWERS_PATHS`, built from the map),
- `scripts/ingest_digest_answers.py` (legacy write).

A test (`tests/test_known_answers_map.py`) asserts every entity that can be written
resolves to a file Cora reads — closing the bug where HJRP/UFL/F3C/HJRPROD answers
were written to files never read. LEX sub-entities (LLC/LLA/LBHS/LTS) all write to
`lex.md`, surfaced at the LEX (GM) level only, never inside a sibling sub-entity
channel.

---

## Retired: the legacy manual gap digest

`scripts/generate_knowledge_gaps_digest.py` + `scripts/ingest_digest_answers.py`
(task `cowork-cora-digest`, daily 5am) were a parallel, hand-edited path to the
SAME `known-answers/*.md` files (2 of 44 gaps ever resolved that way). They print a
DEPRECATED banner and share the canonical map. **`cowork-cora-digest` is Disabled on
the host** (WS17-B) and WS17-C records it in `data/maps/scheduled-task-state.yaml`
so the nightly health check stops warning "unexpectedly Disabled" every day. The
automated `gap_autofill` loop is the supported owner. (The "Cowork-native
cora-knowledge-review founder-memory loop" older notes mention is a Cowork SKILL /
Drive artifact, not a repo task.)

**`cowork-cora-gap-digest` (weekly Mon 8am, `scripts/post_gap_digest_slack.py`) —
KEPT (WS17-C decision).** It is a SEPARATE task: a channel-visible rollup of the OPEN
gaps in `logs/knowledge-gaps.jsonl` posted to `#hjrg-leadership` (a "what Cora still
can't answer" view), distinct in audience and function from the daily Harrison-only
knowledge-review APPROVAL DM. It shares the gaps source but is not the approval loop,
so it is not strictly redundant — left enabled, flagged as a low-priority retirement
candidate: if the leadership-channel rollup goes unused, disable it and add it to
`scheduled-task-state.yaml`.

---

## Ledger hygiene

`load_proposed_updates` / `resolve_update` formerly read+rewrote the entire
17.7k-line file per op. WS17-B (1) bulk-dismisses the operational dead-end backlog
(`scripts/triage_proposed_updates.py`, Harrison-gated) and (2) rotates resolved/
dismissed rows older than the retention window into
`cora-proposed-memory-updates.archive.jsonl`, keeping the live file to PENDING +
recently-resolved. Idempotency reads live ∪ archive ids so a rotated-out item is
never re-proposed. All rewrites are atomic (tmp + replace).

---

## System 2 — team knowledge contributions (FOLDED, WS17-C / D-060)

`team_learning.py` lets a teammate contribute knowledge via `note:`/`remember:`, a
correction-reply, or a 📚 bookmark. **As of WS17-C it no longer runs a parallel
queue.** The old path (an approval card in `#cora-kq-{entity}` → a per-entity
approver ✅ → an ingested KB chunk with `source="team_note"`) is RETIRED. There were
**0 `team_note` chunks ever ingested** (verified), so nothing migrated.

The fold: on the author's "yes" (the kept paraphrase-confirm loop), the confirmed
contribution — and a 📚 bookmark — calls `knowledge_review.propose_update` with
`update_type="generic"`, `payload.source="info-for-cora"`, an entity tag, and
`payload.kind` (note/correction/bookmark). On Harrison's 👍 it executes the existing
`#info-for-cora` branch → `gap_autofill.apply_contributed_note` →
`known-answers/{entity}.md`. A teammate-taught fact and a mined/told fact now share
ONE gate and ONE store. `apply_contributed_note`'s attribution is source-aware (e.g.
"Team note from #f3e-leadership by …" vs "via #info-for-cora").

**KEPT** in `team_learning.py`: the author paraphrase-confirm loop,
`screen_contribution` (scope/injection), `is_authorized_contributor` (who may
submit), `parse_note` / `is_correction` / `is_confirmation`. **Added**: a PHI intake
gate on the note + bookmark paths (`is_phi_risk` + `is_clinical_phi`, plus LEX
`is_lex_billing_status_phi`) — `screen_contribution` never checked PHI and the note
hits Haiku for paraphrasing; `apply_contributed_note`'s three-predicate write gate
stays the backstop. **Sole gate:** only Harrison approves; the 9 per-entity approvers
no longer write to the KB (decision-SUPPORT / sole-authority, D-034). The retired
approver path's `#cora-kb-log` audit + decision-pinning side-effects are dropped (a
known-answers line is not a decision to pin).

---

## Future intakes route here

New knowledge sources — including **Phase-5-d2 personal-notes promotion**
(`share_requested` notes a user asks to make org-wide) — must enter through THIS
queue (`propose_update` → Harrison 👍 → `known-answers`/canonical), not a new
side path. d2 is out of scope for WS17-B; the queue is designed so it can route
through later without a new pipeline.
