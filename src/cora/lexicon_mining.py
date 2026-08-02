"""Lexicon mining (Lexicon Flywheel S5): two lanes feeding the 7am review rail.

Lane A (high precision, chokepoint-first): aggregate the resolver's own
telemetry (logs/lexicon-resolutions.jsonl). A candidate is a MISSED query later
resolved in-session -- a ``miss`` row followed by an ``exact`` row from the same
(user, channel, entity) within REPHRASE_WINDOW_SECONDS -- optionally strengthened
by the F-23 confirm linkage (a ``resolution_confirmed`` row for the same user +
canonical shortly after). Proposal-eligible at LANE_A_MIN_EVENTS pairs from
LANE_A_MIN_USERS distinct humans in LANE_A_WINDOW_DAYS. A proposal carries a
contributor_id (the most recent confirming teammate -- Tier 0 REACHABLE under
the unchanged classify_tier rules) ONLY at the stricter TIER0_* thresholds AND
with at least one write-confirmed pair; below that the contributor is empty and
the item lands on Harrison (Tier 2) by construction.

Lane B (weekly swept-Slack corpus pass, friction-mining sibling): 14d window,
SQL guards REUSED from friction_mining.query_chunks (source='slack' only,
bot_authored excluded, LEX/LEX-* excluded at the SQL layer, per-chunk PHI drop).
Detector: recurring 2-4 token quoted / Capitalized-run / acronym-shaped n-grams
(>= LANE_B_MIN_USES uses from >= LANE_B_MIN_HUMANS distinct speakers), not
already a lexicon surface, co-occurring in-chunk with a canonical from the
entity's canonical inventory (lexicon registry incl. SKU titles + store names,
known-answers headings, asana-project-map names; HubSpot deal names deliberately
OUT). Type + canonical_name come DETERMINISTICALLY from the co-occurring
canonical's registry entry; Haiku only drafts the human-readable card
description, FAIL-CLOSED (API/parse error -> propose nothing). Candidates with
no confident canonical are ledger-only (no ask-cards in v1).

Both lanes: is_any_phi fail-closed on every candidate text; max
MAX_PROPOSALS_PER_RUN proposals/run (lane A first, then lane B by evidence);
fingerprint ledger written AT PROPOSAL TIME (exact + fuzzy >= 0.85, the
friction pattern) so a candidate never re-proposes regardless of outcome.

Standalone script module: imports NO bot-process modules (subprocess-tested).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Optional

from . import lexicon
from .lexicon import norm_term

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Lane A thresholds (module constants, tunable)
LANE_A_WINDOW_DAYS = 30
LANE_A_MIN_EVENTS = 2
LANE_A_MIN_USERS = 2
TIER0_MIN_EVENTS = 3
TIER0_MIN_USERS = 2
REPHRASE_WINDOW_SECONDS = 900
CONFIRM_LINK_WINDOW_SECONDS = 3600

# Lane B thresholds
LANE_B_WINDOW_DAYS = 14
LANE_B_MIN_USES = 5
LANE_B_MIN_HUMANS = 3
CLUSTER_SIM = 0.82

MAX_PROPOSALS_PER_RUN = 5
FUZZY_DEDUP_RATIO = 0.85

_HAIKU_MODEL = "claude-haiku-4-5"

_UPDATE_TYPE = "lexicon"

# Acronym-shaped tokens that are ordinary business English, never shorthand.
# (UTC/GMT + tz codes: swept chunks carry timestamps -- the 2026-08-01 live
# dry-run's top "candidate" was UTC at 245 uses.)
_ACRONYM_STOPLIST = frozenset({
    "OK", "USA", "ASAP", "FYI", "EOD", "EOW", "CEO", "CFO", "COO", "CTO", "PDF",
    "API", "USD", "AM", "PM", "ET", "PT", "MST", "EST", "PST", "LLC", "INC",
    "TBD", "IMO", "IIRC", "DM", "CC", "PS", "AND", "THE", "NOT", "ALL", "NEW",
    "LOL", "WFH", "OOO", "PTO", "HR", "IT", "PO", "ID", "UI", "UX", "QA",
    "UTC", "GMT", "EDT", "PDT", "MDT", "CST", "CDT", "ISO", "URL", "HTTP", "WWW",
    "COM", "EOM", "ETA", "ROI", "KPI", "PNL", "AR", "AP", "DTC", "SKU", "AI",
    "KB", "CRM", "POS", "SOP", "LLM", "JSON", "YAML", "GID", "TS",
    "AZ", "UT", "CA", "TX", "NV",  # state codes (ubiquitous in addresses)
})

# Entity/portfolio codes are vocabulary, not shorthand candidates (the live
# dry-run proposed "OSN" as a term).
_ENTITY_CODE_NORMS = frozenset({
    "f3e", "f3c", "osn", "lex", "bdm", "ufl", "hjrg", "hjrp", "hjrprod", "fndr",
    "pod", "osngw", "osngm", "osngf", "osnvv", "llc", "lla", "lbhs", "lts",
})


def _fingerprints_path() -> Path:
    return Path(os.environ.get("LEXICON_FINGERPRINTS_PATH")
                or _REPO_ROOT / "data" / "state" / "lexicon-fingerprints.jsonl")


def _candidates_path() -> Path:
    return Path(os.environ.get("LEXICON_CANDIDATES_PATH")
                or _REPO_ROOT / "data" / "state" / "lexicon-candidates.jsonl")


@dataclass
class LexCandidate:
    term: str                 # display form of the shorthand
    entity: str
    canonical: str
    canonical_name: str
    type: str
    lane: str                 # "resolver" (lane A) | "mined" (lane B)
    events: int = 0
    users: set = field(default_factory=set)
    contributor_id: str = ""  # non-empty ONLY at Tier-0 thresholds (lane A)
    evidence: str = ""

    @property
    def fingerprint(self) -> str:
        key = f"{norm_term(self.term)}|{self.canonical}"
        return f"lex:{hashlib.md5(key.encode('utf-8')).hexdigest()[:12]}"


def _phi_clean(*texts: str) -> bool:
    """True when every text passes is_any_phi. Screen failure = NOT clean."""
    try:
        from .phi_guard import is_any_phi
        return not any(is_any_phi(t) for t in texts if t)
    except Exception:  # noqa: BLE001 -- fail closed
        return False


def _registry_entry_for(canonical: str, entity: str) -> Optional[lexicon.LexEntry]:
    """Find the registry entry carrying this canonical, in entity scope."""
    try:
        for e in lexicon._entries_for(entity, None):
            if e.canonical == canonical:
                return e
    except Exception:  # noqa: BLE001
        pass
    return None


# ── Lane A ───────────────────────────────────────────────────────────────────


def _read_telemetry(window_days: int) -> list[dict[str, Any]]:
    path = lexicon._log_path()
    if not path.exists():
        return []
    cutoff = time.time() - window_days * 86400
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if row.get("ts", 0) >= cutoff:
                    rows.append(row)
    except Exception as exc:  # noqa: BLE001
        log.warning("lexicon_mining: telemetry read failed: %s", exc)
    return rows


def mine_lane_a(*, window_days: int = LANE_A_WINDOW_DAYS) -> list[LexCandidate]:
    """Rephrase-to-hit pairs from the resolver telemetry, aggregated per
    (normalized missed query, canonical)."""
    rows = sorted(_read_telemetry(window_days), key=lambda r: r.get("ts", 0))
    resolves = [r for r in rows if r.get("event", "resolve") == "resolve"]
    confirms = [r for r in rows if r.get("event") == "resolution_confirmed"]

    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    misses = [r for r in resolves if r.get("status") in ("miss", "suggestion")]
    exacts = [r for r in resolves if r.get("status") == "exact"]
    for miss in misses:
        display = (miss.get("query_display") or "").strip()
        if not display or display == "[withheld]":
            continue  # a withheld (LEX PHI-screened) miss can never be proposed
        for hit in exacts:
            if hit.get("ts", 0) <= miss.get("ts", 0):
                continue
            if hit.get("ts", 0) - miss.get("ts", 0) > REPHRASE_WINDOW_SECONDS:
                continue
            if (hit.get("user"), hit.get("channel"), hit.get("entity")) != \
                    (miss.get("user"), miss.get("channel"), miss.get("entity")):
                continue
            if hit.get("query_hash") == miss.get("query_hash"):
                continue  # same query re-asked, not a rephrase mapping
            canonical = hit.get("canonical") or ""
            if not canonical:
                continue
            key = (norm_term(display), canonical)
            agg = pairs.setdefault(key, {
                "term": display, "canonical": canonical,
                "entity": (miss.get("entity") or "").upper(),
                "events": 0, "users": set(), "confirmed_users": [],
            })
            agg["events"] += 1
            if miss.get("user"):
                agg["users"].add(miss["user"])
            # F-23 confirm linkage: the same user write-confirmed this canonical
            # shortly after the rephrase hit.
            for c in confirms:
                if (c.get("user") == hit.get("user")
                        and c.get("canonical") == canonical
                        and 0 <= c.get("ts", 0) - hit.get("ts", 0)
                        <= CONFIRM_LINK_WINDOW_SECONDS):
                    agg["confirmed_users"].append(str(c.get("user")))
                    break
            break  # first qualifying hit after the miss claims the pair

    out: list[LexCandidate] = []
    for (_norm_q, canonical), agg in pairs.items():
        if agg["events"] < LANE_A_MIN_EVENTS or len(agg["users"]) < LANE_A_MIN_USERS:
            continue
        entry = _registry_entry_for(canonical, agg["entity"])
        if entry is None:
            continue  # cannot type an unknown canonical
        if not _phi_clean(agg["term"], entry.canonical_name):
            continue
        contributor = ""
        if (agg["events"] >= TIER0_MIN_EVENTS
                and len(agg["users"]) >= TIER0_MIN_USERS
                and agg["confirmed_users"]):
            contributor = agg["confirmed_users"][-1]
        out.append(LexCandidate(
            term=agg["term"], entity=agg["entity"] or entry.entity,
            canonical=canonical, canonical_name=entry.canonical_name,
            type=entry.type, lane="resolver", events=agg["events"],
            users=agg["users"], contributor_id=contributor,
            evidence=(f"{agg['events']} rephrase-to-hit events from "
                      f"{len(agg['users'])} users, {window_days}d"),
        ))
    out.sort(key=lambda c: (-c.events, -len(c.users), c.term))
    return out


# ── Lane B ───────────────────────────────────────────────────────────────────


_QUOTED_RE = re.compile(r"[\"“‘']([^\"”’'\n]{3,60})[\"”’']")
# Same-line only ([ \t], never \n): a run must not stitch the tail of one
# message to the next line's speaker name.
_CAPRUN_RE = re.compile(r"\b([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){1,3})\b")
_ACRONYM_RE = re.compile(r"\b([A-Z]{2,5})\b")
_SPEAKER_RE = re.compile(r"^\s*\[?([A-Z][\w .'-]{1,40}?)\]?\s*:", re.MULTILINE)


# A candidate and its anchor must sit within this many chars of each other in
# the chunk. Whole-chunk co-occurrence paired everything with everything on
# long daily-synthesis chunks (2026-08-01 live dry-run: "Lexington Services ->
# BLUE-CHIP-BEVERAGE" at 41 uses).
PROXIMITY_CHARS = 200


# A Capitalized-run ending in an org/address suffix is a proper name fragment
# ("Lexington Services", "Gilbert Rd"), never teachable shorthand.
_NAMELIKE_TAIL = frozenset({
    "services", "service", "llc", "inc", "corp", "co", "group", "rd", "road",
    "st", "street", "ave", "avenue", "blvd", "dr", "drive", "ln", "lane",
})


def _extract_ngram_occurrences(content: str) -> list[tuple[str, int]]:
    """(span, char_position) for quoted / Capitalized-run / acronym-shaped
    2-4 token spans."""
    out: list[tuple[str, int]] = []
    for m in _QUOTED_RE.finditer(content):
        span = m.group(1).strip()
        if 2 <= len(span.split()) <= 4:
            out.append((span, m.start()))
    for m in _CAPRUN_RE.finditer(content):
        span = m.group(1).strip()
        if span.split()[-1].lower() in _NAMELIKE_TAIL:
            continue
        out.append((span, m.start()))
    for m in _ACRONYM_RE.finditer(content):
        tok = m.group(1)
        if tok not in _ACRONYM_STOPLIST:
            out.append((tok, m.start()))
    return [(s, p) for s, p in out if norm_term(s)]


def _speakers(content: str) -> set[str]:
    return {m.group(1).strip().lower() for m in _SPEAKER_RE.finditer(content)}


def _canonical_inventory(entity: str) -> dict[str, lexicon.LexEntry]:
    """Canonical surfaces a lane-B term may map to: the entity's lexicon
    registry (seeds + SKU titles + store names). Keyed by lowercase surface.

    Anchor quality rules (2026-08-01 live dry-run findings): a SHORT canonical
    code ("HJRG") appears in every channel reference, turning the whole corpus
    into a co-occurrence -- so canonical codes anchor only when SKU-like
    (len >= 6 with a digit or hyphen). Multi-word term/alias surfaces anchor
    too ("blue chip" near "BCB" is the strongest real signal); single-word
    surfaces never do."""
    inv: dict[str, lexicon.LexEntry] = {}
    try:
        for e in lexicon._entries_for(entity, None):
            if e.type == "person":
                continue  # people are never lane-B canonicals
            inv[e.canonical_name.lower()] = e
            canon = e.canonical
            if len(canon) >= 6 and (any(c.isdigit() for c in canon) or "-" in canon):
                inv[canon.lower()] = e
            for surface in (e.term, *e.aliases):
                if len(surface.split()) >= 2:
                    inv.setdefault(surface.lower(), e)
    except Exception:  # noqa: BLE001
        pass
    return inv


def mine_lane_b(
    *,
    window_days: int = LANE_B_WINDOW_DAYS,
    embed_fn: Callable | None = None,
    chunks: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[LexCandidate], list[dict[str, Any]]]:
    """(candidates_with_canonical, ledger_only_rows). ``chunks`` is injectable
    for tests; live runs reuse friction_mining.query_chunks (same SQL guards:
    slack-only, bot_authored excluded, LEX excluded at the SQL layer)."""
    if chunks is None:
        from .friction_mining import query_chunks
        chunks = query_chunks(lookback_days=window_days, sources=("slack",))

    # term -> {uses, humans, entities, co-occurring canonical counts}
    stats: dict[str, dict[str, Any]] = {}
    for ch in chunks:
        entity = (ch.get("entity") or "").upper()
        if entity.startswith("LEX"):
            continue  # belt: lane B never mines LEX (SQL already excludes)
        content = ch.get("content") or ""
        speakers = _speakers(content) or {f"chunk:{ch.get('source_id', '')}"}
        speaker_norms = {norm_term(s) for s in speakers}
        inv = _canonical_inventory(entity)
        lowered = content.lower()
        # Anchor occurrences with positions (proximity-windowed co-occurrence).
        anchor_pos: dict[str, list[int]] = {}
        for surface in inv:
            start = 0
            while True:
                i = lowered.find(surface, start)
                if i == -1:
                    break
                anchor_pos.setdefault(surface, []).append(i)
                start = i + 1
        occurrences = _extract_ngram_occurrences(content)
        seen_this_chunk: set[str] = set()
        for span, pos in occurrences:
            n = norm_term(span)
            # A speaker's own name is never shorthand (Capitalized-run spans
            # would otherwise turn every transcript name into a candidate),
            # and neither is an entity/portfolio code.
            if n in speaker_norms or n in _ENTITY_CODE_NORMS:
                continue
            # Not already a lexicon surface, not itself a canonical surface.
            try:
                if lexicon.resolve(span, entity).status in ("exact", "ambiguous"):
                    continue
            except Exception:  # noqa: BLE001
                continue
            if span.lower() in inv:
                continue
            st = stats.setdefault(f"{entity}|{n}", {
                "term": span, "entity": entity, "uses": 0, "humans": set(),
                "canon": {},
            })
            if n not in seen_this_chunk:
                seen_this_chunk.add(n)
                st["uses"] += 1        # chunk-level use counting (anti-noise)
                st["humans"] |= speakers
            for surface, positions in anchor_pos.items():
                if any(abs(pos - a) <= PROXIMITY_CHARS for a in positions):
                    st["canon"][surface] = st["canon"].get(surface, 0) + 1

    eligible = [st for st in stats.values()
                if st["uses"] >= LANE_B_MIN_USES
                and len(st["humans"]) >= LANE_B_MIN_HUMANS
                and _phi_clean(st["term"])]

    # Near-dup grouping (cos >= CLUSTER_SIM, the friction idiom): keep each
    # cluster's highest-use representative. No embeddings -> exact-norm dedup
    # only (fail-soft).
    if len(eligible) > 1:
        try:
            from .friction_mining import _safe_embed, greedy_cluster
            vecs = _safe_embed([st["term"] for st in eligible], embed_fn)
            if vecs:
                keep: list[dict[str, Any]] = []
                for cluster in greedy_cluster(vecs, CLUSTER_SIM):
                    members = [eligible[i] for i in cluster]
                    members.sort(key=lambda s: -s["uses"])
                    top = members[0]
                    for extra in members[1:]:
                        top["uses"] += extra["uses"]
                        top["humans"] |= extra["humans"]
                    keep.append(top)
                eligible = keep
        except Exception as exc:  # noqa: BLE001
            log.warning("lexicon_mining: near-dup grouping skipped (%s)", exc)

    candidates: list[LexCandidate] = []
    ledger_only: list[dict[str, Any]] = []
    for st in eligible:
        if st["canon"]:
            surface = max(st["canon"], key=st["canon"].get)
            entry = _canonical_inventory(st["entity"]).get(surface)
            if entry is not None and _phi_clean(entry.canonical_name):
                candidates.append(LexCandidate(
                    term=st["term"], entity=st["entity"], canonical=entry.canonical,
                    canonical_name=entry.canonical_name, type=entry.type,
                    lane="mined", events=st["uses"], users=st["humans"],
                    evidence=(f"{st['uses']} uses by {len(st['humans'])} humans, "
                              f"{window_days}d; co-occurs with {entry.canonical}"),
                ))
                continue
        ledger_only.append({
            "term": st["term"], "entity": st["entity"], "uses": st["uses"],
            "humans": len(st["humans"]), "reason": "no_confident_canonical",
        })
    candidates.sort(key=lambda c: (-c.events, -len(c.users), c.term))
    return candidates, ledger_only


# ── Haiku card description (fail-closed) ─────────────────────────────────────


def draft_description(cand: LexCandidate) -> str | None:
    """Human-readable card description. FAIL-CLOSED: None on any API/parse
    error or missing key -> the candidate is NOT proposed this run."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("lexicon_mining: ANTHROPIC_API_KEY not set -- skipping draft")
        return None
    prompt = (
        "You are drafting a one-line review card for a company-lexicon proposal.\n"
        f"Shorthand: \"{cand.term}\" (entity {cand.entity})\n"
        f"Proposed meaning: {cand.canonical_name} (canonical {cand.canonical}, "
        f"type {cand.type})\nEvidence: {cand.evidence}\n"
        "Reply with JSON only: {\"description\": \"<one sentence for the "
        "reviewer, plain factual, no hype>\"}"
    )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=_HAIKU_MODEL, max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            from .llm_usage import log_usage
            log_usage(response, caller="lexicon_mining", model=_HAIKU_MODEL)
        except Exception:  # noqa: BLE001
            pass
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.split("\n") if not l.startswith("```")).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        desc = str(json.loads(raw[start:end + 1]).get("description") or "").strip()
        if not desc or not _phi_clean(desc):
            return None
        return desc[:300]
    except Exception as exc:  # noqa: BLE001 -- fail-closed by design
        log.warning("lexicon_mining: Haiku draft failed for %s: %s",
                    cand.fingerprint, exc)
        return None


# ── Fingerprints + proposals ─────────────────────────────────────────────────


def load_ledger() -> list[dict[str, Any]]:
    path = _fingerprints_path()
    if not path.exists():
        return []
    out = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    except Exception as exc:  # noqa: BLE001
        log.warning("lexicon_mining: ledger read failed: %s", exc)
    return out


def is_already_proposed(cand: LexCandidate, ledger: list[dict[str, Any]]) -> bool:
    """Exact fingerprint OR same-entity fuzzy paraphrase (>= FUZZY_DEDUP_RATIO)."""
    rep = norm_term(cand.term)
    for entry in ledger:
        if entry.get("fingerprint") == cand.fingerprint:
            return True
        if entry.get("entity") == cand.entity:
            prior = norm_term(str(entry.get("term") or ""))
            if prior and SequenceMatcher(None, rep, prior).ratio() >= FUZZY_DEDUP_RATIO:
                return True
    return False


def record_proposal(cand: LexCandidate, update_id: str) -> None:
    path = _fingerprints_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "fingerprint": cand.fingerprint,
            "term": cand.term[:200],
            "entity": cand.entity,
            "canonical": cand.canonical,
            "lane": cand.lane,
            "update_id": update_id,
            "proposed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False) + "\n")


def record_candidates(rows: list[dict[str, Any]]) -> None:
    """Ledger-only lane-B candidates (no confident canonical). Fail-soft."""
    if not rows:
        return
    try:
        path = _candidates_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({**r, "seen_at": stamp}, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.warning("lexicon_mining: candidates ledger write failed: %s", exc)


def propose_lexicon(cand: LexCandidate, description: str) -> str:
    """Queue one lexicon proposal for the 7am review rail. Returns update_id."""
    from .knowledge_review import propose_update
    update_id = f"lexicon-{cand.fingerprint.split(':')[-1]}"
    propose_update(
        update_id=update_id,
        update_type=_UPDATE_TYPE,
        description=description,
        payload={
            "term": cand.term,
            "type": cand.type,
            "entity": cand.entity,
            "canonical": cand.canonical,
            "canonical_name": cand.canonical_name,
            "lane": cand.lane,
            "contributor_id": cand.contributor_id,
            "evidence": cand.evidence,
            "fingerprint": cand.fingerprint,
        },
        source_evidence=cand.evidence,
        confidence="HIGH" if cand.lane == "resolver" else "MED",
    )
    return update_id


def run_mining(
    *,
    dry_run: bool = True,
    lane: str = "both",
    max_proposals: int = MAX_PROPOSALS_PER_RUN,
    embed_fn: Callable | None = None,
) -> dict[str, Any]:
    """One mining pass. dry_run writes NOTHING (no proposals, no ledgers).
    With writes enabled: at CORA_LEXICON=off the run is FULLY INERT -- it
    short-circuits before any lane work (no corpus scan, no embedding spend,
    no ledger writes; D-051 remediation F14); at 'resolve' the candidates
    ledger persists but PROPOSALS stay gated on 'full' (the rollout brake).
    An explicit human dry-run still works at any level (writes nothing)."""
    if not dry_run and lexicon.lexicon_level() == "off":
        return {
            "lane_a_candidates": 0, "lane_b_candidates": 0,
            "lane_b_ledger_only": 0, "already_proposed": 0,
            "capped_at": max_proposals, "proposed": 0, "dry_run": dry_run,
            "lexicon_level": "off", "candidates": [],
            "note": "CORA_LEXICON=off -- fully inert, nothing scanned or written",
        }
    lane_a = mine_lane_a() if lane in ("a", "both") else []
    lane_b: list[LexCandidate] = []
    ledger_only: list[dict[str, Any]] = []
    if lane in ("b", "both"):
        lane_b, ledger_only = mine_lane_b(embed_fn=embed_fn)

    ledger = load_ledger()
    fresh = [c for c in lane_a + lane_b if not is_already_proposed(c, ledger)]
    to_propose = fresh[:max_proposals]

    summary: dict[str, Any] = {
        "lane_a_candidates": len(lane_a),
        "lane_b_candidates": len(lane_b),
        "lane_b_ledger_only": len(ledger_only),
        "already_proposed": len(lane_a) + len(lane_b) - len(fresh),
        "capped_at": max_proposals,
        "proposed": 0,
        "dry_run": dry_run,
        "lexicon_level": lexicon.lexicon_level(),
        "candidates": [
            {"term": c.term, "entity": c.entity, "canonical": c.canonical,
             "type": c.type, "lane": c.lane, "events": c.events,
             "users": len(c.users), "contributor": bool(c.contributor_id),
             "evidence": c.evidence}
            for c in to_propose
        ],
    }
    if dry_run:
        return summary

    record_candidates(ledger_only)
    if lexicon.lexicon_level() != "full":
        summary["note"] = "CORA_LEXICON below 'full' -- candidates-ledger-only mode"
        return summary
    for cand in to_propose:
        desc = draft_description(cand)
        if desc is None:
            continue  # fail-closed: no draft, no proposal
        update_id = propose_lexicon(cand, desc)
        record_proposal(cand, update_id)
        summary["proposed"] += 1
    return summary
