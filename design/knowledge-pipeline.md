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
Harrison's 👍 promotes. The only carve-out is the pre-existing **auto-approve**:
HIGH-confidence machine-mined `known_answer` (NOT teammate-DM, NOT canonical),
floored to exclude the legacy backlog. Canonical writes (`decisions.md`) and all
external actions always require a 👍.

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
SAME `known-answers/*.md` files (2 of 44 gaps ever resolved that way). They now
print a DEPRECATED banner and share the canonical map. **Action for Harrison:**
disable the `cowork-cora-digest` scheduled task — the automated `gap_autofill` loop
is the supported owner. (The separate "Cowork-native cora-knowledge-review
founder-memory loop" the older notes mention is a Cowork SKILL / Drive artifact,
not a repo task — there is exactly one repo task running the live System-1 loop.)

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

## System 2 — team knowledge contributions (status + recommendation)

A **separate** system (`team_learning.py`) lets a teammate contribute knowledge via
`note:`/`remember:`, a correction-reply, or a 📚 bookmark → an approval card in
`#cora-kq-{entity}` → on ✅, an ingested KB chunk with `source="team_note"` (broadly
retrievable in Q&A). It is fully wired but has **0 contributions ever ingested**
(2 submitted, both declined). WS17-B removed a dead duplicate bookmark handler
(`app.py`) but did NOT change System 2's behavior.

**Why it's idle (diagnosis):** discovery friction (you must know the `note:`
syntax), every registered user is an *approver* (no plain contributors), and the
14 `#cora-kq-*` queue channels must exist or cards silently fall back to
`#hjrg-leadership`.

**Recommendation for Harrison (a structural call — NOT made here):** **fold** team
contributions into System 1's knowledge write target so there is genuinely ONE
knowledge store. Concretely: an approved team contribution would write to
`known-answers/{entity}.md` (the runtime-loaded context) via the shared writer,
the same path `#info-for-cora` now uses — instead of a divergent `team_note` KB
chunk. This unifies "a teammate taught Cora a fact" and "Cora mined/was-told a
fact" onto one gate and one store. Trade-off to weigh: `team_note` chunks are
retrieved via KB semantic search (good for long-tail recall), while
`known-answers` is always-loaded per-entity context (good for high-value, frequently
needed facts) — folding changes *how* a contribution surfaces. **Alternative if
kept separate:** verify the 14 `#cora-kq-*` channels exist, register plain-
contributor users, and surface the `note:` syntax in onboarding. Either way, this
is Harrison's decision; this build only flags it.

---

## Future intakes route here

New knowledge sources — including **Phase-5-d2 personal-notes promotion**
(`share_requested` notes a user asks to make org-wide) — must enter through THIS
queue (`propose_update` → Harrison 👍 → `known-answers`/canonical), not a new
side path. d2 is out of scope for WS17-B; the queue is designed so it can route
through later without a new pipeline.
