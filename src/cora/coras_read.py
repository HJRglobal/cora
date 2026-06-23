"""Cora's read -- a short, advisory assessment appended to each KNOWLEDGE
proposal DM (WS17-C Part 3).

Before Harrison thumbs-up/down a knowledge proposal (a gap-autofill known_answer,
a #info-for-cora / folded team note, or an efficiency finding), Cora retrieves
across her own sources and classifies the proposed claim against what she already
knows:

    CORROBORATED  -- supporting evidence found
    CONFLICTS     -- contradicts existing knowledge (esp. an existing known-answer)
    ADDS-CONTEXT  -- related but not the same fact
    NET-NEW       -- no corroboration found

The verdict + a one-line note is appended to the DM so the review is low-effort.
This is decision-SUPPORT, never decision-MAKER: the read NEVER approves or writes
anything; the thumbs-up is always Harrison's, and the read is not persisted.

Guards (every one fail-soft -- any error returns "" and the DM still sends):
  * entity-scoped retrieval (kb.search(entity=...) + only the F3E<->F3C paired set);
  * PHI -- evidence chunks flagged is_phi_risk are dropped before the prompt; the
    rendered note is re-checked (is_phi_risk / is_lex_billing_status_phi /
    is_clinical_phi) and dropped on any hit; for LEX it is additionally scrubbed
    with the staff allowlist (fail-closed);
  * source-opaque -- the prompt forbids naming sources/files/links and the note is
    passed through reply_formatter.redact_links_and_ids; chunk titles/deep_links
    are never echoed;
  * fail-soft -- the whole helper is wrapped; a dead KB / missing API key / parse
    error returns "";
  * bounded -- run_knowledge_review only calls this for the <=10 knowledge items
    per run; a per-process cache dedups an identical claim within a run.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_KB_DB_PATH = Path(os.environ.get("CORA_KB_DB_PATH") or _REPO_ROOT / "data" / "cora_kb.db")

_MAX_DISTANCE = 1.30          # same KB cosine-distance ceiling as gap_autofill / context_loader
_SEARCH_K = 8                 # evidence chunks per entity
_PAIRED_K = 4                 # paired-entity (F3E<->F3C) chunks
_HAIKU_MODEL = "claude-haiku-4-5"
# Sources never used as corroboration: user_note is owner-private (SQL-excluded
# already); team_note is the RETIRED folded-contribution KB source (stale chunks
# may linger -- never let them surface as "evidence").
_EXCLUDED_SOURCES = frozenset({"user_note", "team_note"})

_VERDICTS = {
    "CORROBORATED": "✅",
    "CONFLICTS": "⚠️",
    "ADDS-CONTEXT": "➕",
    "NET-NEW": "🆕",
}

# Per-process cache so the same claim isn't classified twice in one run.
_CACHE: dict[tuple[str, str], str] = {}

_PROMPT = """\
You compare a PROPOSED fact against what an internal company assistant already
knows, for entity {entity}. Decide one verdict:
  CORROBORATED  - the retrieved context supports the proposed fact
  CONFLICTS     - it contradicts an EXISTING KNOWN FACT or the retrieved context
  ADDS-CONTEXT  - related to existing knowledge but not the same fact
  NET-NEW       - nothing corroborating found

PROPOSED FACT:
{claim}

EXISTING KNOWN FACTS (already in Cora's memory for this entity):
{prior}

RETRIEVED CONTEXT (excerpts from Cora's knowledge base):
{evidence}

Respond with ONLY a JSON object (no markdown fences, no prose):
{{"verdict": "CORROBORATED|CONFLICTS|ADDS-CONTEXT|NET-NEW",
  "note": "<=20 words, plain language"}}

Rules:
- Base the verdict ONLY on the text above; do not guess.
- The note must NOT name any source, file, sheet, channel, link, or person's
  client/patient details, and must contain NO diagnoses, medications, or PHI.
"""


def _claim_and_entity(update: dict[str, Any]) -> tuple[str, str]:
    payload = update.get("payload") or {}
    claim = (payload.get("text") or payload.get("answer")
             or update.get("description") or "").strip()
    entity = (payload.get("entity") or "FNDR").strip().upper()
    return claim, entity


def _entity_scope(entity: str) -> tuple[str, str | None]:
    """Map an entity code to (kb_entity, sub_entity) -- LEX-* collapses to LEX."""
    entity = (entity or "FNDR").strip().upper()
    if entity.startswith("LEX-"):
        return "LEX", entity
    return entity, None


def _paired_entities(kb_entity: str) -> set[str]:
    try:
        from .cross_entity_guard import PAIRED_ENTITIES
        return set(PAIRED_ENTITIES.get(kb_entity, set()))
    except Exception:  # noqa: BLE001
        return set()


def _retrieve_evidence(kb: Any, claim: str, entity: str) -> list[Any]:
    """Entity-scoped KB search, filtered to non-PHI, non-excluded-source chunks
    within the distance ceiling. Returns excerpt strings."""
    from .phi_guard import is_phi_risk, is_clinical_phi, is_lex_billing_status_phi

    kb_entity, sub_entity = _entity_scope(entity)
    hits: list[Any] = []
    try:
        hits.extend(kb.search(query=claim, entity=kb_entity, k=_SEARCH_K, sub_entity=sub_entity))
    except Exception as exc:  # noqa: BLE001
        log.warning("coras_read: KB search failed (%s)", exc)
        return []
    for paired in _paired_entities(kb_entity):
        try:
            hits.extend(kb.search(query=claim, entity=paired, k=_PAIRED_K))
        except Exception as exc:  # noqa: BLE001
            log.warning("coras_read: paired KB search failed (%s)", exc)
    out = []
    for r in hits:
        if getattr(r, "source", "") in _EXCLUDED_SOURCES:
            continue
        if getattr(r, "distance", 99.0) > _MAX_DISTANCE:
            continue
        content = getattr(r, "content", "") or ""
        # Drop PHI chunks before they reach the prompt -- is_clinical_phi catches
        # the diagnosis/medication class is_phi_risk misses (WS17-B), and the
        # administrative-billing class is dropped UNCONDITIONALLY (not just for
        # kb_entity==LEX): this evidence is sent to the LLM, so the input filter is
        # truly symmetric with the output _scrub (all three predicates, entity-agnostic).
        if (not content or is_phi_risk(content) or is_clinical_phi(content)
                or is_lex_billing_status_phi(content)):
            continue
        out.append(content[:600])
        if len(out) >= _SEARCH_K:
            break
    return out


def _read_prior(entity: str) -> str:
    """The entity's existing known-answers file (so the classifier can spot a CONFLICT)."""
    try:
        from .known_answers_map import file_for
        ka_dir = Path(os.environ.get("KNOWN_ANSWERS_DIR")
                      or _REPO_ROOT / "design" / "known-answers")
        path = ka_dir / file_for(entity)
        return path.read_text(encoding="utf-8")[:1500] if path.exists() else ""
    except Exception:  # noqa: BLE001
        return ""


def _classify(claim: str, prior: str, evidence: list[str]) -> dict | None:
    """Fail-CLOSED Haiku classify -- any error / missing key returns None."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    prompt = _PROMPT.format(
        entity="(internal)",
        claim=claim[:600],
        prior=prior or "(none on file)",
        evidence="\n\n".join(f"- {e}" for e in evidence) or "(no matching context found)",
    )
    try:
        import anthropic
        # Bounded so a slow/hung LLM call never delays or blocks the 7am DM run --
        # advisory enrichment must never gate the knowledge DM.
        client = anthropic.Anthropic(api_key=api_key, timeout=15.0, max_retries=1)
        resp = client.messages.create(
            model=_HAIKU_MODEL, max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.split("\n") if not l.startswith("```")).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        obj = json.loads(raw[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception as exc:  # noqa: BLE001 -- fail-closed by design
        log.warning("coras_read: Haiku classify failed (%s)", exc)
        return None


def _scrub(text: str, entity: str) -> str:
    """PHI-drop + source-opaque the rendered note. Returns "" if it looks like PHI."""
    from .phi_guard import is_phi_risk, is_lex_billing_status_phi, is_clinical_phi
    if is_phi_risk(text) or is_lex_billing_status_phi(text) or is_clinical_phi(text):
        return ""
    kb_entity, _ = _entity_scope(entity)
    if kb_entity == "LEX":
        try:
            from . import org_roles
            from .phi_guard import scrub_lex_phi
            staff = {r.name for r in org_roles.all_roles() if getattr(r, "name", "")}
            text = scrub_lex_phi(text, allowed_names=staff)
        except Exception:  # noqa: BLE001 -- fail-CLOSED: never emit unscrubbed LEX text
            return ""
    try:
        from .reply_formatter import redact_links_and_ids
        text = redact_links_and_ids(text)
    except Exception:  # noqa: BLE001
        return ""
    return text.strip()


def build_coras_read(update: dict[str, Any], *, kb: Any = None) -> str:
    """Return a one-line "Cora's read" for a knowledge proposal, or "" (fail-soft).

    Decision-SUPPORT only: this never writes or approves anything. kb is injected
    in tests; in production a KnowledgeBase is opened against the live KB and closed.
    """
    try:
        claim, entity = _claim_and_entity(update)
        if not claim:
            return ""
        # Defense-in-depth: never send a PHI claim to the LLM, even if an upstream
        # intake gate regressed. is_lex_billing_status_phi is UNCONDITIONAL (entity-
        # agnostic): a folded contribution carries the AUTHOR's entity (e.g. a custodian
        # like Harrison=FNDR), but the TEXT can be named-client LEX billing/authorization
        # PHI with no clinical keyword -- and claim[:600] is about to be sent to the
        # Anthropic API. Entity-gating this (the WS17-C oversight an independent pass
        # caught) would let that PHI egress to the LLM. Fail-closed; never cache PHI.
        from .phi_guard import is_phi_risk, is_clinical_phi, is_lex_billing_status_phi
        if (is_phi_risk(claim) or is_clinical_phi(claim)
                or is_lex_billing_status_phi(claim)):
            return ""
        cache_key = (entity, claim[:200])
        if cache_key in _CACHE:
            return _CACHE[cache_key]

        own_kb = False
        if kb is None:
            try:
                from .knowledge_base import KnowledgeBase
                kb = KnowledgeBase(_KB_DB_PATH, check_same_thread=False)
                own_kb = True
            except Exception as exc:  # noqa: BLE001
                log.warning("coras_read: KB open failed (%s)", exc)
                return ""
        try:
            evidence = _retrieve_evidence(kb, claim, entity)
            prior = _read_prior(entity)
        finally:
            if own_kb:
                try:
                    kb.close()
                except Exception:  # noqa: BLE001
                    pass

        verdict_obj = _classify(claim, prior, evidence)
        if not verdict_obj:
            return ""
        verdict = str(verdict_obj.get("verdict") or "").strip().upper()
        if verdict not in _VERDICTS:
            return ""
        note = _scrub(str(verdict_obj.get("note") or "").strip(), entity)
        emoji = _VERDICTS[verdict]
        # The verdict label is fixed/safe; only the model's free-text note is scrubbed.
        line = f"🧠 *Cora's read:* {emoji} {verdict}"
        if note:
            line += f": {note}"
        _CACHE[cache_key] = line
        return line
    except Exception as exc:  # noqa: BLE001 -- never block the DM
        log.warning("coras_read: build failed (%s)", exc)
        return ""
