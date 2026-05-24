# Cora — Lex Sub-Entity Routing: Claude Code Build Brief

_Created: 2026-05-23 by Cowork scaffolding session._  
_Incident context: On 2026-05-22, Cora disclosed LLA equity information in an LLC channel. Root cause: single "LEX" entity with no sub-entity discrimination. This brief implements the 3-part fix._

---

## What Cowork already built (do NOT recreate)

1. **4 new system prompt files** (already on disk, ready to use):
   - `design/system-prompts/llc.md` — Lexington LLC (Shaun Hawkins, DDD/HCBS/DTA ops)
   - `design/system-prompts/lts.md` — Lexington Therapies (Justin Gilmore, therapy services)
   - `design/system-prompts/lbhs.md` — LBHS (Jared Harker, behavioral health + COPA-private)
   - `design/system-prompts/lla.md` — Lex Life Academy (Sandy Patel, school/clinic programs)

2. **`design/channel-routing.yaml` patched** — 8 new routes added before the `lex` / `lex-*` block:
   - `llc` → `LEX-LLC`, `llc-*` → `LEX-LLC`
   - `lts` → `LEX-LTS`, `lts-*` → `LEX-LTS`
   - `lbhs` → `LEX-LBHS`, `lbhs-*` → `LEX-LBHS`
   - `lla` → `LEX-LLA`, `lla-*` → `LEX-LLA`

3. **`design/system-prompts/lex.md` updated** — Now explicitly GM-level prompt. Corrected "three sub-entities" → "four." Added sub-entity redirect guidance.

---

## What Claude Code needs to implement (3 parts)

### Part 1 — Register new entity codes in `prompt_loader.py`

File: `src/cora/prompt_loader.py`  
Dict: `_ENTITY_FILES`

Add 4 entries:

```python
_ENTITY_FILES: dict[str, str] = {
    "F3E":     "f3e.md",
    "LEX":     "lex.md",
    "LEX-LLC": "llc.md",    # ADD
    "LEX-LTS": "lts.md",    # ADD
    "LEX-LBHS":"lbhs.md",   # ADD
    "LEX-LLA": "lla.md",    # ADD
    "OSN":     "osn.md",
    "BDM":     "bdm.md",
    "FNDR":    "fndr.md",
    "HJRG":    "fndr.md",
}
```

No other changes to `prompt_loader.py` needed — the existing fallback-to-FNDR logic handles unknown codes gracefully, and the `load_prompt()` + `_ENTITY_FILES` lookup already works.

---

### Part 2 — KB chunk sub-entity tagging

Context: Phase 3 KB nightly sync ingests Fireflies transcripts and Asana tasks. Currently all Lex chunks share `entity = "LEX"`. After this change, chunks should be tagged with the appropriate sub-entity when it can be determined from the source.

File: `src/cora/knowledge_base/` — find the sync scripts for Fireflies and Asana ingestion.

**Changes needed:**

For **Asana chunks**: If a task belongs to the LLC Asana team (gid `1209152915815732`), tag `sub_entity = "LEX-LLC"`. For LLA team (gid `1209152923740446`), tag `LEX-LLA`. For LBHS team (gid `1209152923740451`), tag `LEX-LBHS`. For LTS — no dedicated gid confirmed yet; tag `LEX-LTS` if the project name contains "LTS" or "Therapies."

For **Fireflies chunks**: Tag based on meeting title / participants. Meetings with Justin Gilmore → `LEX-LTS`. Meetings with Jared Harker → `LEX-LBHS`. Meetings with Sandy Patel → `LEX-LLA`. Meetings with Shaun + no other sub-entity manager → `LEX-LLC`. Cross-sub-entity meetings → `LEX` (GM level).

**Query-time filter**: When Cora's entity is `LEX-LLC`, KB retrieval should filter to chunks where `sub_entity IN ("LEX-LLC", "LEX")` — i.e., LLC-specific chunks + GM-level chunks, but NOT LLA/LBHS/LTS chunks.

Schema change needed: add `sub_entity TEXT` column to the KB chunks table (nullable; NULL = not sub-entity-discriminated, treat as visible to all LEX variants).

---

### Part 3 — Tests

Add to `tests/test_entity_router.py`:
```python
# Sub-entity routing
("llc",           "LEX-LLC"),
("llc-operations","LEX-LLC"),
("llc-finance",   "LEX-LLC"),
("lts",           "LEX-LTS"),
("lts-operations","LEX-LTS"),
("lbhs",          "LEX-LBHS"),
("lbhs-finance",  "LEX-LBHS"),
("lla",           "LEX-LLA"),
("lla-leadership","LEX-LLA"),
# GM-level still routes to LEX
("lex",           "LEX"),
("lex-leadership","LEX"),
("lex-finance",   "LEX"),
("lex-cora-build","LEX"),
```

Add to `tests/test_prompt_loader.py`:
```python
# New sub-entity codes load correct files
for code, filename in [
    ("LEX-LLC", "llc.md"),
    ("LEX-LTS", "lts.md"),
    ("LEX-LBHS","lbhs.md"),
    ("LEX-LLA", "lla.md"),
]:
    prompt = load_prompt(code)
    assert len(prompt) > 100   # file loaded, not empty fallback
    assert "FNDR" not in prompt[:50]  # not the fallback
```

---

## Commit message suggestion

```
feat(lex): sub-entity routing + 4 new entity prompts (LEX-LLC/LTS/LBHS/LLA)

Fixes the 2026-05-22 LLA equity disclosure incident (root cause: single LEX
entity with no sub-entity discrimination). Three-part fix:

1. channel-routing.yaml: 8 new patterns route llc-*/lts-*/lbhs-*/lla-*
   to their respective sub-entity codes before the lex-* catch.
2. prompt_loader.py: register LEX-LLC/LEX-LTS/LEX-LBHS/LEX-LLA in
   _ENTITY_FILES; each maps to a new design/system-prompts/*.md file.
3. knowledge_base: add sub_entity column to chunks; tag at ingest time
   by Asana team gid (LLC/LLA/LBHS) or Fireflies participant; filter
   at query time so llc-* channels never surface lbhs/lla/lts chunks.

Prompt highlights:
- llc.md: Shaun Hawkins manager, DDD/HCBS/DTA, CT Corp UCC lien watch
- lts.md: Justin Gilmore manager, 2026-06-30 DDD revalidation deadline
- lbhs.md: Jared Harker manager, COPA-private, extra PHI sensitivity
- lla.md: Sandy Patel manager, Maryvale, quarterly tuition cash swings
- lex.md updated to explicit GM-level prompt (4 sub-entities now named)
```

---

## Restart after merging

```
schtasks /End /TN cowork-cora-service && schtasks /Run /TN cowork-cora-service
```

The `_ROUTES` and `_ENTITY_FILES` caches are loaded at startup — restart is required for the new routing + prompts to activate.

---

## Harrison actions post-deploy

1. Create the sub-entity Slack channels (#llc-*, #lts-*, #lbhs-*, #lla-*) as needed
2. Invite Cora to those channels
3. Smoke test: @Cora in #llc-operations → should answer with LLC context, refuse if asked about LLA equity
4. Smoke test: @Cora in #lbhs-operations → should answer with LBHS context, refuse COPA questions
