# Claude Code Prompt: Lex Sub-Entity KB Tagging (Part 2 of 3)

_Copy this entire prompt into a Claude Code session in the `C:\Users\Harri\code\cora\` repo._

---

## Context

Cora uses a Phase 3 sqlite-vec knowledge base (`src/cora/knowledge_base/`) with nightly sync from Asana and Fireflies. All Lexington chunks currently share `entity = "LEX"`. This means when Cora is scoped to `LEX-LLC` (the LLC sub-entity channel), KB retrieval can surface LLA equity info, LBHS COPA details, or LTS therapy data — which is the root cause of the 2026-05-22 incident where Cora disclosed LLA equity information in an LLC channel.

This task implements sub-entity tagging and query-time filtering.

**Cowork already completed:**
- `design/channel-routing.yaml` — 8 new sub-entity routes added (`llc`, `llc-*`, `lts`, `lts-*`, `lbhs`, `lbhs-*`, `lla`, `lla-*`)
- `design/system-prompts/llc.md`, `lts.md`, `lbhs.md`, `lla.md` — 4 new prompt files
- `src/cora/prompt_loader.py` — `_ENTITY_FILES` dict updated with LEX-LLC/LEX-LTS/LEX-LBHS/LEX-LLA

**Your job (3 parts):**

---

## Part A — Schema: Add `sub_entity` column to KB chunks table

Find the SQLite schema initialization code in `src/cora/knowledge_base/`. Add a nullable `sub_entity TEXT` column to the chunks table.

- Column: `sub_entity TEXT` (nullable — NULL means "visible to all LEX variants")
- Add a migration guard: if the column already exists (e.g. from a prior run), skip silently
- Typical pattern: `ALTER TABLE chunks ADD COLUMN sub_entity TEXT;` inside a try/except or `PRAGMA table_info` check

---

## Part B — Ingest tagging at sync time

### B1 — Asana chunks

Find the Asana sync script (likely `src/cora/knowledge_base/sync_asana.py` or similar). When creating a chunk for an Asana task:

Tag `sub_entity` based on the task's team gid:

```python
ASANA_TEAM_SUB_ENTITY = {
    "1209152915815732": "LEX-LLC",   # Lexington LLC team
    "1209152923740446": "LEX-LLA",   # Lex Life Academy team
    "1209152923740451": "LEX-LBHS",  # Lexington Behavioral Health team
}

def _tag_asana_sub_entity(task: dict) -> str | None:
    """Return sub_entity code for a task, or None if indeterminate."""
    team_gid = task.get("memberships", [{}])[0].get("project", {}).get("team", {}).get("gid")
    # Fallback: check all memberships
    for membership in task.get("memberships", []):
        tgid = membership.get("project", {}).get("team", {}).get("gid", "")
        if tgid in ASANA_TEAM_SUB_ENTITY:
            return ASANA_TEAM_SUB_ENTITY[tgid]
    # LTS: no confirmed gid yet — tag by project name keyword
    project_name = task.get("memberships", [{}])[0].get("project", {}).get("name", "")
    if any(kw in project_name for kw in ["LTS", "Therapies", "Lexington Therapies"]):
        return "LEX-LTS"
    return None
```

Apply the tag when building each chunk dict. Only set `sub_entity` when the task's entity is already `LEX` (don't tag F3E tasks, etc.).

### B2 — Fireflies chunks

Find the Fireflies sync script (likely `src/cora/knowledge_base/sync_fireflies.py` or similar). When creating a chunk for a meeting transcript:

Tag `sub_entity` based on meeting participants:

```python
# Known sub-entity managers by email fragment or display name
FIREFLIES_PARTICIPANT_SUB_ENTITY = [
    (["justin.gilmore", "Justin Gilmore"], "LEX-LTS"),   # LTS manager
    (["jared.harker", "Jared Harker"],     "LEX-LBHS"),  # LBHS manager
    (["sandy.patel",  "Sandy Patel"],      "LEX-LLA"),   # LLA manager
]
# Shaun Hawkins → LEX-LLC only if NO other sub-entity manager is present
SHAUN_IDENTIFIERS = ["shaun.hawkins", "Shaun Hawkins"]

def _tag_fireflies_sub_entity(meeting: dict) -> str | None:
    """Tag meeting by sub-entity based on participants. Cross-entity → None (LEX GM)."""
    participants = meeting.get("participants", [])
    participant_text = " ".join(
        p.get("displayName", "") + " " + p.get("email", "") for p in participants
    ).lower()

    matched = []
    for identifiers, code in FIREFLIES_PARTICIPANT_SUB_ENTITY:
        if any(ident.lower() in participant_text for ident in identifiers):
            matched.append(code)

    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        return None  # cross-sub-entity meeting → LEX GM level, no sub_entity tag

    # Check for Shaun only (LLC)
    shaun_present = any(s.lower() in participant_text for s in SHAUN_IDENTIFIERS)
    if shaun_present:
        return "LEX-LLC"

    return None  # indeterminate → NULL, visible to all LEX variants
```

Only apply this tag when the meeting is already tagged `entity = "LEX"`.

---

## Part C — Query-time filter

Find where KB retrieval queries are built (likely `src/cora/knowledge_base/retrieval.py` or similar, or inline in `claude_client.py`). Add a sub-entity filter when the active entity is a LEX sub-entity.

Logic:

```python
LEX_SUB_ENTITY_VISIBILITY = {
    "LEX-LLC":  ("LEX-LLC", "LEX"),
    "LEX-LTS":  ("LEX-LTS", "LEX"),
    "LEX-LBHS": ("LEX-LBHS", "LEX"),
    "LEX-LLA":  ("LEX-LLA", "LEX"),
    # LEX (GM) sees all LEX chunks regardless of sub_entity
}

def build_sub_entity_filter(entity: str) -> str | None:
    """Return a SQL fragment to add to KB WHERE clause, or None for no filter."""
    visibility = LEX_SUB_ENTITY_VISIBILITY.get(entity)
    if visibility is None:
        return None  # non-LEX entity — no sub_entity filter needed
    # entity = "LEX" → no filter (sees everything)
    if entity == "LEX":
        return None
    # Sub-entity → filter to (sub_entity IS NULL OR sub_entity IN (...))
    quoted = ", ".join(f"'{v}'" for v in visibility)
    return f"(sub_entity IS NULL OR sub_entity IN ({quoted}))"
```

Add this filter to the vector similarity search query (the `SELECT ... FROM chunks WHERE entity = ? ...` or equivalent).

---

## Tests to add

File: `tests/test_kb_sub_entity.py` (new file)

```python
"""Tests for sub-entity KB tagging and query-time filtering."""

import pytest
from cora.knowledge_base.sync_asana import _tag_asana_sub_entity
from cora.knowledge_base.sync_fireflies import _tag_fireflies_sub_entity


def test_asana_llc_team_tags_correctly():
    task = {"memberships": [{"project": {"team": {"gid": "1209152915815732"}, "name": "LLC Project"}}]}
    assert _tag_asana_sub_entity(task) == "LEX-LLC"

def test_asana_lla_team_tags_correctly():
    task = {"memberships": [{"project": {"team": {"gid": "1209152923740446"}, "name": "LLA Project"}}]}
    assert _tag_asana_sub_entity(task) == "LEX-LLA"

def test_asana_lbhs_team_tags_correctly():
    task = {"memberships": [{"project": {"team": {"gid": "1209152923740451"}, "name": "LBHS Project"}}]}
    assert _tag_asana_sub_entity(task) == "LEX-LBHS"

def test_asana_lts_by_project_name():
    task = {"memberships": [{"project": {"team": {"gid": "9999999999"}, "name": "[LTS] Revalidation"}}]}
    assert _tag_asana_sub_entity(task) == "LEX-LTS"

def test_asana_unknown_team_returns_none():
    task = {"memberships": [{"project": {"team": {"gid": "0000000000"}, "name": "Generic Project"}}]}
    assert _tag_asana_sub_entity(task) is None

def test_fireflies_justin_gilmore_tags_lts():
    meeting = {"participants": [
        {"displayName": "Justin Gilmore", "email": "justin.gilmore@lexingtonservices.com"},
        {"displayName": "Harrison Rogers", "email": "harrison@hjrglobal.com"},
    ]}
    assert _tag_fireflies_sub_entity(meeting) == "LEX-LTS"

def test_fireflies_jared_harker_tags_lbhs():
    meeting = {"participants": [
        {"displayName": "Jared Harker", "email": "jared.harker@lexingtonservices.com"},
    ]}
    assert _tag_fireflies_sub_entity(meeting) == "LEX-LBHS"

def test_fireflies_sandy_patel_tags_lla():
    meeting = {"participants": [
        {"displayName": "Sandy Patel", "email": "sandy.patel@lexingtonservices.com"},
    ]}
    assert _tag_fireflies_sub_entity(meeting) == "LEX-LLA"

def test_fireflies_shaun_only_tags_llc():
    meeting = {"participants": [
        {"displayName": "Shaun Hawkins", "email": "shaun@lexingtonservices.com"},
        {"displayName": "Harrison Rogers", "email": "harrison@hjrglobal.com"},
    ]}
    assert _tag_fireflies_sub_entity(meeting) == "LEX-LLC"

def test_fireflies_cross_sub_entity_meeting_returns_none():
    """Meeting with multiple sub-entity managers → GM level, no tag."""
    meeting = {"participants": [
        {"displayName": "Justin Gilmore", "email": "justin.gilmore@lexingtonservices.com"},
        {"displayName": "Sandy Patel", "email": "sandy.patel@lexingtonservices.com"},
    ]}
    assert _tag_fireflies_sub_entity(meeting) is None

def test_fireflies_no_known_participant_returns_none():
    meeting = {"participants": [
        {"displayName": "External Consultant", "email": "consultant@example.com"},
    ]}
    assert _tag_fireflies_sub_entity(meeting) is None
```

---

## Commit message

```
feat(lex-kb): sub-entity tagging + query-time siloing for LEX sub-entities

Part 2 of 3 for the 2026-05-22 LLA equity disclosure fix.

- schema: add sub_entity TEXT (nullable) column to chunks table
- sync_asana: tag by Asana team gid (LLC/LLA/LBHS) or project name (LTS)
- sync_fireflies: tag by participant (Gilmore→LTS, Harker→LBHS,
  Patel→LLA, Shaun-only→LLC, cross-entity→NULL/GM)
- retrieval: sub_entity filter at query time so LEX-LLC channels
  only surface (sub_entity IS NULL OR sub_entity IN ('LEX-LLC','LEX'))
- tests: new test_kb_sub_entity.py covering tagging + filter logic
```

---

## Notes

- NULL `sub_entity` = chunk is visible to ALL LEX variants (safe default for unclassified legacy chunks)
- The nightly scheduled tasks will re-tag all new chunks going forward; existing chunks get NULL (visible to all) until manually re-tagged or a backfill migration runs
- LTS Asana team gid is unconfirmed — fallback to project name keyword works for now; update `ASANA_TEAM_SUB_ENTITY` when Justin Gilmore confirms
- After merging, restart Cora: `schtasks /End /TN cowork-cora-service && schtasks /Run /TN cowork-cora-service`
