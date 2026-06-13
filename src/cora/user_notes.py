"""Personal user notes — Org Synthesis Phase 5, deliverable 1 (policy layer).

Spec of record:
  G:\\My Drive\\HJR-Founder-OS\\_shared\\projects\\cora\\design\\
  2026-06-10_fndr_org-synthesis-spec.md  (Phase 5 section, design locked 2026-06-11)

What a personal note is: any teammate teaching Cora a fact ("Cora, remember X")
that becomes instantly retrievable BY THE OWNER ONLY. Notes live in the main KB
under source="user_note" with metadata.owner_slack — a thin ADDITIVE layer
searched alongside the entity partition + FNDR co-scan, never a replacement
for it and never a sharding of the KB by user.

Blast-radius-1 enforcement is SQL-layer, not prompt-layer (D-034):
  - store.search() excludes source='user_note' in both vector paths, so every
    existing consumer (Q&A retrieval, sweeps, digests, reconciliation,
    friction/strategy mining) excludes notes by construction.
  - store.search_user_notes() is the only retrieval path and filters on
    metadata.owner_slack == asker (unrestricted = the D-043 allowlist, i.e.
    Harrison — the caller verifies via historical_access.is_unrestricted).
  - Answers built on personal notes set kb_meta["unstripped_personal"]=True so
    the existing D-043 invariant keeps them out of the shared semantic cache.

This module holds the POLICY around that storage: the PHI save-decision
matrix, save/conflict-check orchestration, and the labeled context block that
presents a note as the asker's own note, never org-canon.

D-011 is untouched: personal notes are the user's own data, NOT canonical
memory. Promotion to org-wide knowledge (share_requested=true) ships in
deliverable 2 as a Harrison-gated knowledge-review proposal.
"""

from __future__ import annotations

import datetime
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from . import lex_phi_access
from .phi_guard import is_lex_billing_status_phi, is_phi_risk

log = logging.getLogger(__name__)

USER_NOTE_SOURCE = "user_note"

# Overlay retrieval: same relevance threshold as the main KB scan
# (context_loader._KB_MAX_DISTANCE) so a note neither out-competes nor
# under-performs canonical chunks on the same metric.
NOTE_OVERLAY_K = 3

# Save-time conflict probe: a canonical chunk this close to the new note text
# is similar enough to be the same topic — flag it in the save confirmation.
# Tighter than the 1.30 retrieval threshold (we want "probably the same fact",
# not "vaguely related"). Never blocks the save.
CONFLICT_DISTANCE = 1.05

_LEX_PREFIX = "LEX"

PHI_REFUSAL = (
    "I can't save that note — it looks like it contains client-level health "
    "information (PHI). Client health data lives in the EHR, not in Cora's "
    "notes. If you need this captured, raise it with Harrison directly."
)


def _is_lex_scope(entity: str) -> bool:
    return bool(entity) and entity.upper().startswith(_LEX_PREFIX)


@dataclass(frozen=True)
class SaveDecision:
    """Outcome of the save-time PHI/scoping check.

    allowed=False → `reason` is the COMPLETE refusal for the tool to return.
    allowed=True  → save under `entity` (+ `sub_entity` for LEX sub-scopes).
    """
    allowed: bool
    entity: str = ""
    sub_entity: str | None = None
    reason: str = ""


def resolve_save_scope(
    note_text: str, channel_entity: str, owner_slack: str, is_dm: bool
) -> SaveDecision:
    """PHI save-decision matrix (deterministic, runs before any write).

    - Non-PHI note: saved under the channel's entity verbatim (DMs arrive as
      their routed entity, usually FNDR). Note entity values are only ever
      consulted by search_user_notes' entity_scope filter — the notes
      partition is invisible to the main entity search by construction.
    - PHI-flagged note: allowed ONLY for a LEX PHI custodian inside LEX scope
      (LEX/LEX-* channel) or in a DM — the lex_phi_access.phi_allowed posture.
      A custodian's DM note is FORCED into LEX scope (same rule as session
      capture: PHI always lands in the LEX-scoped store). Everyone/everywhere
      else: refuse the save with the standard PHI posture.
    """
    entity = (channel_entity or "FNDR").strip() or "FNDR"
    sub_entity = entity if entity.upper().startswith("LEX-") else None

    text = note_text or ""
    phi = is_phi_risk(text)
    # In LEX scope (LEX/LEX-*) or a DM, the base clinical/identifier patterns
    # are not enough: a named individual's billing / authorization /
    # eligibility / client-status is PHI even with no clinical keyword (the
    # 2026-06-12 "Bob Smith's billing authorization is pending" miss). The
    # augmentation is scoped here so ordinary business notes about a named
    # buyer's authorization in a non-LEX channel are not over-flagged.
    if not phi and (_is_lex_scope(entity) or is_dm):
        phi = is_lex_billing_status_phi(text)

    if not phi:
        return SaveDecision(allowed=True, entity=entity, sub_entity=sub_entity)

    if not lex_phi_access.phi_allowed(owner_slack, entity, is_dm=is_dm):
        return SaveDecision(allowed=False, reason=PHI_REFUSAL)

    # Custodian in LEX scope or DM — PHI notes always live in the LEX store.
    if not _is_lex_scope(entity):
        entity, sub_entity = "LEX", None
    return SaveDecision(allowed=True, entity=entity, sub_entity=sub_entity)


def new_note_id(owner_slack: str) -> str:
    return f"note:{owner_slack}:{uuid.uuid4().hex[:10]}"


def save_note(
    kb: Any,
    *,
    note_text: str,
    owner_slack: str,
    owner_email: str,
    entity: str,
    sub_entity: str | None = None,
    share_requested: bool = False,
    channel_name: str = "",
) -> str:
    """Upsert one personal note. Returns the note_id (source_id).

    Caller is responsible for the staged-write confirmation gate and for
    resolve_save_scope() — this function just persists.
    """
    from .knowledge_base.store import Document  # lazy: keep import surface light

    note_id = new_note_id(owner_slack)
    now = int(time.time())
    kb.upsert_documents([
        Document(
            source=USER_NOTE_SOURCE,
            source_id=note_id,
            entity=entity,
            sub_entity=sub_entity,
            content=note_text,
            date_created=now,
            date_modified=now,
            author=owner_slack,
            title=f"Personal note ({datetime.date.fromtimestamp(now).isoformat()})",
            metadata={
                "owner_slack": owner_slack,
                "owner_email": owner_email,
                "share_requested": bool(share_requested),
                "channel_name": channel_name,
                "created_ts": now,
            },
        )
    ])
    log.info(
        "user_note SAVED owner=%s entity=%s sub=%s share_requested=%s id=%s chars=%d",
        owner_slack, entity, sub_entity, share_requested, note_id, len(note_text),
    )
    return note_id


def conflict_excerpt(
    kb: Any, note_text: str, entity: str, query_vec: list[float] | None = None
) -> str | None:
    """Save-time conflict check: probe the CANONICAL KB (search() already
    excludes user_note chunks) for a high-similarity candidate in the same
    entity scope. Returns a short excerpt to surface in the save confirmation,
    or None. Failures return None — the check must never block a save."""
    try:
        results = kb.search(
            note_text,
            entity=entity if not entity.upper().startswith("LEX-") else "LEX",
            k=3,
            query_vec=query_vec,
        )
    except Exception as exc:  # noqa: BLE001 — advisory check only
        log.warning("user_note conflict check failed (non-blocking): %s", exc)
        return None
    for r in results:
        if r.distance <= CONFLICT_DISTANCE:
            excerpt = (r.content or "").strip().replace("\n", " ")
            if len(excerpt) > 220:
                excerpt = excerpt[:220] + "..."
            label = r.title or r.source
            return f"{label}: {excerpt}"
    return None


NOTES_SYNTHESIS_RULE = (
    "Personal-note rule: context items labeled PERSONAL NOTE are the asker's "
    "own saved notes — present them as their note (\"from your note on "
    "<date>\"), never as organizational fact or canon. Never reveal, confirm, "
    "or use one person's personal note when answering anyone else."
)


def format_notes_overlay(results: list, asker_slack: str) -> str:
    """Render the asker's matching personal notes as a labeled context block.

    Notes owned by someone else only ever reach this point for an
    unrestricted asker (Harrison, D-043 allowlist) — those carry an explicit
    founder-override label naming the owner.
    """
    if not results:
        return ""
    lines = [
        "# Asker's personal notes (matched to this question)",
        "",
        "(These are PERSONAL NOTES, retrievable only by their owner. " + NOTES_SYNTHESIS_RULE + ")",
        "",
    ]
    for r in results:
        meta = r.metadata or {}
        owner = str(meta.get("owner_slack") or "").strip()
        ts = meta.get("created_ts") or r.date_modified
        date_str = ""
        if ts:
            try:
                date_str = datetime.date.fromtimestamp(int(ts)).isoformat()
            except (OSError, ValueError, OverflowError, TypeError):
                pass
        if owner and owner != asker_slack:
            header = (
                f"## PERSONAL NOTE saved by <@{owner}> on {date_str or 'unknown date'} "
                "(visible to you via founder override — not org-canon, and not yours)"
            )
        else:
            header = (
                f"## ASKER'S PERSONAL NOTE from {date_str or 'unknown date'} "
                "— present as their own note, not org-canon"
            )
        lines.extend([header, "", (r.content or "").strip(), ""])
    return "\n".join(lines)
