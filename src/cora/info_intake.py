"""#info-for-cora contribution intake -- the shared, event-independent chokepoint.

WHY THIS MODULE EXISTS (verify-first finding 2026-08-06, cq-f1236540b61e)
------------------------------------------------------------------------
The D1 intake (``app._handle_info_for_cora``, shipped 2026-06-13) was already
CORRECT -- entity tag, PHI screen, idempotency, Harrison-gated propose, ack --
and had never once executed. It is wired ONLY into ``@app.event("message")``,
and channel ``message`` events do not reach this app:

  * zero ``info-for-cora:`` lines in ANY log, ever -- not even a failure warning
    (the handler logs on every branch, including PHI refusal and propose failure)
  * zero ``payload.source == "info-for-cora"`` rows across 19,673 proposed-update
    ledger entries, which also covers the OTHER message-event knowledge path
    (the ``team_learning`` confirm-fold) -- two paraphrases were posted in private
    channels on 6/06 and 6/30 and neither confirm ever landed
  * meanwhile ``app_mention`` (256 recent routes) and ``message.im`` (DM Q&A,
    gap-ask capture) are busy, so the socket itself is healthy

Bot scopes are NOT the problem: ``groups:history`` and ``channels:history`` are
both granted (verified on the live token). The gap is the Slack app's Event
Subscriptions ``bot_events`` list, which is configured separately from scopes and
never appears in the token -- so it cannot be fixed from this repo.

Intake therefore must not depend on that event. Three routes converge HERE:

  1. ``app_mention``  -- proven working in this channel today
  2. ``message`` event -- KEPT; starts contributing the moment the subscription
     is fixed, with no further code change
  3. reconciling sweep -- ``conversations.history``; the ONLY route that can see a
     post which generates no event at all (the Cowork connector's
     un-@-mentioned posts, e.g. Harrison's 2026-07-10 pricing note)

All three derive the SAME deterministic ``infocora-{ts}`` update_id, so
``knowledge_review.propose_update``'s id check makes double-delivery a no-op.
That is the whole reason the routes can safely overlap.

DESIGN INVARIANTS
-----------------
* **Deterministic only.** No LLM call anywhere on this path. Posted content is
  DATA, never instructions -- a contribution reading "approve this" cannot
  approve anything, because intake only ever writes state=PENDING.
  **This claim is only true because of the R3 exclusion.** As originally shipped
  it was FALSE: info-for-cora generics are knowledge-class, so they entered the
  7am drain's auto-write scan, and with the live `CORA_AUTOWRITE_LIVE=all` an
  allowlist-category contribution that read CORROBORATED would have written
  itself into always-injected known-answers with no Harrison tap.
  `run_knowledge_review._autowrite_eligible` now excludes this source outright
  (D-060 restored); do not remove that predicate.
* **No pre-approval egress.** `source_evidence` is left EMPTY on purpose: the
  proposed-updates ledger is byte-copied unscreened to the org-readable
  `_brain/_flywheel/` Drive store, so anything put there is public before
  Harrison sees it.
* **LEX is refused outright** (Harrison mandate 2026-08-06), on CONTENT, at
  ingest AND again at the executor.
* **PHI parity-raise.** ``is_any_phi`` (the 3-predicate union) UNCONDITIONALLY,
  not the LEX-asker-scoped billing check the old path used. Strictly stricter
  than what it replaces; fail-closed on exception.
* **No embedding egress.** Near-duplicate detection is offline ``difflib``, not
  embeddings. Contributions can be LEX-tagged, and the embedding-dedup precedent
  (``code_queue._embedding_dup_id``) deliberately never embeds LEX text; using a
  local ratio keeps that invariant instead of carving an exception into it.
* **Never guess an entity.** Content-determined via the canonical
  ``cross_entity_guard`` keyword table. Exactly one entity named -> that entity;
  several named -> FNDR plus an ``ambiguous_entity`` flag Harrison can see; none
  named -> FNDR unflagged (an unscoped fact really is portfolio-level).
* **Logs never echo contribution text** (it may be PHI). Ids and outcomes only.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (cross_entity_guard, entity_router, knowledge_review,
               phi_guard, qa_scaffolding)
from .known_answers_map import ENTITY_FILES

log = logging.getLogger(__name__)

# The #info-for-cora channel (private since 2026-05-28).
CHANNEL_ID = "C0B5BNP6YKY"
CHANNEL_NAME = "info-for-cora"
SOURCE = "info-for-cora"

# ── Outcomes ────────────────────────────────────────────────────────────────
QUEUED = "queued"
DUPLICATE = "duplicate"
SUPERSEDES = "supersedes"          # queued AND flagged as contradicting canon
QUARANTINED = "quarantined"
PHI_REFUSED = "phi_refused"
LEX_REFUSED = "lex_refused"
NOT_A_CONTRIBUTION = "not_a_contribution"
SKIPPED = "skipped"
ERROR = "error"

# Outcomes that mean "nothing was stored".
NON_STORING = frozenset({DUPLICATE, QUARANTINED, PHI_REFUSED, LEX_REFUSED,
                         NOT_A_CONTRIBUTION, SKIPPED, ERROR})

# ── Blanket LEX skip (Harrison mandate, 2026-08-06) ─────────────────────────
# The first cut allowed non-PHI LEX contributions through on the reasoning that
# this path has no embeddings and no LLM call and that known-answers/lex.md is fed
# by exactly this executor. Harrison RULED against that: LEX-origin content must
# never enter this intake at all. The Cowork 4-lens D-051 fan-out also found the
# deviation unsafe for a second reason -- resolve_entity COLLAPSES a multi-entity
# hit to ("FNDR", True), discarding LEX membership, so a message naming both LEX
# and F3E would have filed as an ordinary FNDR fact (finding A-3).
#
# Keyed on CONTENT, not channel: #info-for-cora routes to FNDR, so a channel test
# would never fire. Belted with a token regex of the decision_inbox._LEX_TOKEN_RE
# class for bare/compound tokens the entity-keyword table misses.
_LEX_TOKEN_RE = re.compile(
    r"\blex(?:[-_][a-z0-9]+)*\b|\blexington[a-z]*\b|\b(?:lbhs|lts|lla)\b",
    re.IGNORECASE)

# ── Connector footer ────────────────────────────────────────────────────────
# The Cowork connector appends a literal "*Sent using* <@U...>" footer at the END
# of the message. Anchored to end-of-string and REQUIRES the mention token --
# a bare unanchored "sent using .*$" would eat legitimate mid-sentence prose
# ("invoices sent using the old template") and collide distinct facts. Same shape
# as code_queue._SENT_USING_RE and gap_autofill's QA scaffolding note (D-051
# defect B, 2026-08-02). NOT DOTALL: a multi-line contribution keeps its body.
_SENT_USING_RE = re.compile(
    r"\n?\s*\*?\s*sent using\s*\*?\s*<@[^>]+>\s*$", re.IGNORECASE)

# ── [QA] quarantine (D-104) ─────────────────────────────────────────────────
# Delegated to qa_scaffolding so this surface, the gap log, the code queue and the
# decision inbox all honour ONE definition of the marker.

# ── Durable-knowledge screen ────────────────────────────────────────────────
# MEASURED against the real channel (38 messages, 2026-04..08): the plain
# statement-vs-question split queued 17 items of which exactly ONE -- Harrison's
# F3 Pure pricing note -- was durable organizational knowledge. The other 16 were
# Asana task feedback aimed at Cora ("This task should have been assigned to
# Harrison"), conversational replies, and a Slack system message. Queueing those
# would flood Harrison's review queue and reproduce the known too-loose-eligibility
# defect class (cq-5c6ff15610bd) on a brand-new surface.
#
# So a statement must ALSO look like a durable fact. These screens are heuristic
# and therefore NON-PERMANENT: a screened message is simply not queued this pass,
# it is never marked resolved, so a regex false positive costs one skipped post
# rather than burying a contribution (the gap_autofill PERMANENT_INELIGIBLE
# doctrine applied to intake).

NOT_DURABLE_TASK_FEEDBACK = "feedback about a specific work item, not a fact"
NOT_DURABLE_ADDRESSED_TO_CORA = "feedback about Cora's own behaviour, not a fact"
NOT_DURABLE_SYSTEM = "Slack system/channel-management message"
NOT_DURABLE_INTERROGATIVE = "contains a question -- conversational, not a stated fact"
NOT_DURABLE_TOO_THIN = "too short to be a durable fact"

# A specific work item plus corrective/evaluative language. BOTH halves are
# required: "Tessa is coordinating leasing" (durable) must survive, while "This
# task should have been assigned to Tessa" (feedback) must not.
# A DEMONSTRATIVE reference to a work item ("this Asana task", "that task") or an
# Asana URL. Sufficient on its own: in this channel a demonstrative task reference
# is always feedback about that item, never a durable fact. The optional ASANA
# between determiner and noun is load-bearing -- without it the first live dry-run
# let two "This Asana task ..." critiques through as facts.
#
# "the task" is deliberately EXCLUDED from the determiner set: "The task of
# reconciling AR now belongs to Jerry" is ordinary prose stating a durable fact,
# whereas "this/that/these/those task" always points at a specific item in context.
_TASK_REF_RE = re.compile(
    r"\bapp\.asana\.com\b|\b(?:this|that|these|those)\s+(?:asana\s+)?tasks?\b",
    re.IGNORECASE)

# A durable fact needs some substance. "invite" (a two-word command to Cora) was
# queued as a "fact" by the first live dry-run.
_MIN_FACT_WORDS = 6

# Second person aimed at Cora's own actions ("You assigned me the same tasks
# three different times"). "Core" is the recurring live misspelling of Cora.
_ADDRESSED_TO_CORA_RE = re.compile(
    r"^\s*(?:you|cora|core)\b[^.?!]{0,80}?"
    r"\b(?:assigned|created|posted|sent|keep|nudg\w*|duplicat\w*)\b",
    re.IGNORECASE)

# Slack channel-management system prose. These usually carry a subtype (the sweep
# drops every subtyped message), but "made this channel private" arrived with
# none, so the text form is screened too.
_SYSTEM_PROSE_RE = re.compile(
    r"^\s*(?:made this channel\b|set the channel\b|renamed the channel\b"
    r"|archived this channel\b|joined the channel\b|left the channel\b"
    r"|pinned a message\b|added an integration\b)",
    re.IGNORECASE)


def durable_contribution_reason(text: str) -> str:
    """Empty string when `text` reads as a durable organizational fact, otherwise
    the reason it does not. Conservative by design -- see the block comment above."""
    t = strip_leading_mentions(text)
    if _SYSTEM_PROSE_RE.search(t):
        return NOT_DURABLE_SYSTEM
    if _ADDRESSED_TO_CORA_RE.search(t):
        return NOT_DURABLE_ADDRESSED_TO_CORA
    if _TASK_REF_RE.search(t):
        return NOT_DURABLE_TASK_FEEDBACK
    if len(t.split()) < _MIN_FACT_WORDS:
        return NOT_DURABLE_TOO_THIN
    # A statement carrying a question anywhere is a conversational turn. Accepted
    # residual: a genuine fact appended to a process question (the 6/17 Square POS
    # note) is skipped. Stating it without the question logs it.
    if "?" in t:
        return NOT_DURABLE_INTERROGATIVE
    return ""


# Near-duplicate threshold for the supersession flag. Deliberately high: below
# this, two facts about the same subject are usually genuinely different facts,
# and a false "supersedes" costs Harrison a wrong retraction.
SUPERSEDE_SIM = 0.82

_PROVENANCE_LINE_RE = re.compile(r"^\s*\*\*\[\d{4}-\d{2}-\d{2}\]")

# Leading Slack mention tokens ("<@UCORA> Tessa is staying on ..."). The app
# routes strip Cora's own leading mention before calling in, but the sweep sees
# RAW history text, so strip here too -- otherwise the stored fact begins with a
# raw user id and the ^-anchored durable screens never match.
_LEADING_MENTIONS_RE = re.compile(r"^(?:\s*<[@#!][^>]*>)+\s*")


def strip_leading_mentions(text: str) -> str:
    return _LEADING_MENTIONS_RE.sub("", text or "")


@dataclass(frozen=True)
class IntakeResult:
    """What intake decided. The caller owns all Slack I/O (post `ack` if non-empty)."""
    outcome: str
    ack: str = ""
    update_id: str = ""
    entity: str = "FNDR"
    ambiguous_entity: bool = False
    is_connector: bool = False
    supersedes: str = ""
    detail: str = ""

    @property
    def stored(self) -> bool:
        return self.outcome not in NON_STORING


# ── Pure helpers (all independently testable) ───────────────────────────────
def strip_connector_footer(text: str) -> tuple[str, bool]:
    """Return (text without the trailing Cowork footer, was_connector_post).

    Connector posts arrive as ORDINARY user messages -- verified on the wire
    2026-08-06: Harrison's 7/10 note carries user=U0B2RM2JYJ1, app_id=A08SF47R6P4,
    and NO bot_id and NO subtype. So the footer, not a bot flag, is the only
    reliable connector signal, and the bot_id guard never had to change.
    """
    raw = text or ""
    stripped = _SENT_USING_RE.sub("", raw)
    return stripped.strip(), stripped != raw


def is_qa_quarantined(text: str) -> bool:
    """True when the message carries the literal [QA] prefix (D-104 smoke traffic)."""
    return qa_scaffolding.is_qa_message(text)


def intake_update_id(ts: str) -> str:
    """The deterministic id every route derives. Matches the original D1 scheme so
    an item queued by one route is a no-op for the other two."""
    return f"infocora-{ts}"


# C9 (cq-dacabcc2e47e): a contribution that NAMES A CHANNEL is naming an entity.
#
# The live case: Hannah posted "Skylar has authorization to make inventory
# adjustments in the f3-hq-inventory-adjustments channel" to #info-for-cora. That
# is an F3E fact and the refusal it was fixing happened in an F3E channel -- but
# cross_entity_guard's F3E keywords are brand words ("f3 energy", "f3e", "f3 pure")
# and none of them appears in "f3-hq-inventory-adjustments", so it resolved FNDR
# and was written to fndr.md. Injection IS entity-scoped -- ONE .get(entity), ONE
# file -- so it can never load in the F3E channel it was meant to fix. (It IS
# reachable there through the FNDR KB co-scan, so the fact is not lost; it is
# simply not in the always-injected block where a scope refusal would see it.)
#
# entity_router already resolves channel names correctly and is the canonical map.
# Gated on is_mapped() so the trailing "*" catch-all cannot turn ordinary
# hyphenated prose ("a well-known random-thing issue") into an entity claim.
_CHANNEL_TOKEN_RE = re.compile(
    r"<#C[A-Z0-9]+\|([a-z0-9][a-z0-9._-]{2,})>"      # <#C123|f3-hq-inventory>
    r"|(#)([a-z0-9]+(?:-[a-z0-9]+){1,})\b"            # #f3-hq-inventory-adjustments
    r"|\b([a-z0-9]+(?:-[a-z0-9]+){1,})\b"            # f3-hq-inventory-adjustments
)
# The intake surface itself names no entity -- every contribution mentions it.
_NON_ENTITY_CHANNEL_NAMES = frozenset({"info-for-cora", "cora-build", "cora-health"})

# D-051: a BARE hyphenated token is only a channel reference when the text says
# so. Without this, "we agreed at the llc-level that this is fine" matched the
# 'llc-*' route, resolved LEX, and was HARD-REFUSED by the blanket LEX skip -- a
# benign non-LEX contribution killed by ordinary English. The '#' prefix and the
# literal word "channel" are how people actually write a reference, and the live
# case ("...in the f3-hq-inventory-adjustments channel") carries the latter.
_CHANNEL_WORD_RE = re.compile(r"\bchannels?\b", re.IGNORECASE)
_CHANNEL_WORD_WINDOW = 40


def _collapse_family(entity: str) -> str:
    """Sub-entity -> family. A channel token and a keyword hit that name the same
    business must be ONE hit, not an ambiguous two -- "#llc-finance" routes to
    LEX-LLC while the LEX keyword detector says LEX, and left uncollapsed that
    pair would resolve to ("FNDR", ambiguous) and file the fact nowhere useful."""
    ent = (entity or "").strip().upper()
    if ent.startswith("LEX-"):
        return "LEX"
    for parent in ("OSN", "HJRP", "F3E"):
        if ent.startswith(parent + "-") or (ent.startswith(parent) and ent != parent
                                            and ent not in ENTITY_FILES):
            return parent
    return ent


def channel_token_entities(text: str) -> set[str]:
    """Entities named by a CHANNEL REFERENCE in *text*. "" on any failure."""
    out: set[str] = set()
    try:
        body = str(text or "")
        for m in _CHANNEL_TOKEN_RE.finditer(body):
            # groups: 1 = <#C..|name>, 2 = the literal '#', 3 = the token after
            # '#', 4 = a bare token.
            labelled, hashed = m.group(1), m.group(2)
            tok = (labelled or m.group(3) or m.group(4) or "").strip().lower()
            if not tok or tok in _NON_ENTITY_CHANNEL_NAMES:
                continue
            if not entity_router.is_mapped(tok):
                continue      # only a REAL channel pattern counts
            # A bare token needs corroboration; "<#C…|x>" and "#x" are explicit.
            if not labelled and not hashed:
                lo = max(0, m.start() - _CHANNEL_WORD_WINDOW)
                hi = min(len(body), m.end() + _CHANNEL_WORD_WINDOW)
                if not _CHANNEL_WORD_RE.search(body[lo:hi]):
                    continue
            ent = (entity_router.route(tok) or "").upper()
            # FNDR is the DEFAULT, and utility channels (drive-shares,
            # asana-feed, fireflies-recaps, hjrg-*) all route there. Adding it as
            # a "hit" turned an otherwise-unambiguous contribution into an
            # ambiguous one -- "F3 Pure pricing per the drive-shares channel"
            # resolved ("FNDR", ambiguous) instead of F3E. A channel that names
            # no business entity should contribute nothing (D-051).
            if ent and ent != "FNDR":
                out.add(ent)
    except Exception:  # noqa: BLE001 -- tagging must never break intake
        log.warning("info_intake: channel-token detection failed", exc_info=True)
    return out


def resolve_entity(text: str) -> tuple[str, bool]:
    """(entity, ambiguous). Exactly one entity named in the CONTENT -> that entity.
    Two or more named -> FNDR and ambiguous=True (we cannot pick, so we flag).
    NONE named -> FNDR and ambiguous=False: an unscoped fact is genuinely
    portfolio-level, so FNDR is the correct tag rather than a guess to flag.

    Content-based, not author-based, on purpose: #info-for-cora is a cross-entity
    intake surface. Harrison's primary entity is FNDR but his F3 Pure pricing note
    is an F3E fact and belongs in known-answers/f3e.md where F3E channels load it.
    Never guesses: an ambiguous contribution stays FNDR and is flagged for Harrison
    rather than silently filed under a business entity.
    """
    try:
        hits = set(cross_entity_guard.detect_entities(text or ""))
    except Exception:  # noqa: BLE001 -- tagging must never break intake
        log.warning("info_intake: entity detection failed; defaulting FNDR", exc_info=True)
        return "FNDR", True
    # A named channel is an entity claim (C9). Sub-entity codes collapse to their
    # family first, so "#llc-finance" and "LEX" are ONE hit, not an ambiguous two.
    hits |= {_collapse_family(e) for e in channel_token_entities(text)}
    hits = {_collapse_family(e) for e in hits}
    if len(hits) == 1:
        return next(iter(hits)), False
    return "FNDR", bool(hits)


# ── R5a: D-123-class scrub of externally-authored contribution text ─────────
# An approved contribution is written verbatim into known-answers/{entity}.md --
# ALWAYS-INJECTED context -- and the same raw text renders on Harrison's DM card.
# The egress boundary deliberately PRESERVES `<...>` (the sanctioned citation
# form), so a contributor could make Cora carry a live `<!channel>` broadcast or a
# labelled attacker link into both surfaces. Neutralize before interpolation,
# never after (the D-123 chokepoint pattern).
#
# Unlike inventory_state.scrub this does NOT strip vendor/platform names or cap at
# 80: a fact about Shopify pricing IS the knowledge. Only live Slack behaviour is
# removed; the readable content survives.
_SCRUB_BROADCAST_RE = re.compile(r"<!([^>|]*)(?:\|[^>]*)?>")
_SCRUB_LINK_LABELLED_RE = re.compile(r"<(?:https?://|mailto:)[^>|]+\|([^>]*)>")
_SCRUB_LINK_BARE_RE = re.compile(r"<(?:https?://|mailto:)[^>]+>")
_SCRUB_USER_RE = re.compile(r"<@[UW][A-Z0-9]+(?:\|[^>]*)?>")
_SCRUB_CHANNEL_REF_RE = re.compile(r"<#C[A-Z0-9]+(?:\|([^>]*))?>")

# R5b: the card used to render clean[:240] while the WRITE was unbounded, so a long
# contribution's tail was approved sight-unseen. Card renders up to the Slack block
# cap; the stored text is bounded and the truncation is disclosed on the card.
CARD_TEXT_CAP = 2900
STORED_TEXT_CAP = 1500


def scrub_contribution(text: str) -> str:
    """Neutralize live Slack behaviour in externally-authored contribution text."""
    t = str(text or "")
    t = _SCRUB_BROADCAST_RE.sub(r"[\1]", t)          # <!channel> -> [channel]
    t = _SCRUB_LINK_LABELLED_RE.sub(r"\1 [link removed]", t)
    t = _SCRUB_LINK_BARE_RE.sub("[link removed]", t)
    t = _SCRUB_USER_RE.sub("[@user]", t)
    t = _SCRUB_CHANNEL_REF_RE.sub(lambda m: f"#{m.group(1)}" if m.group(1) else "[#channel]", t)
    return t


def is_lex_content(text: str) -> bool:
    """True when `text` carries ANY LEX signal. Raises on a detector failure so the
    caller can fail CLOSED -- see ingest().

    Deliberately evaluated on the RAW keyword hits, BEFORE resolve_entity's
    multi-entity collapse to ("FNDR", True): that collapse discards LEX membership,
    so a message naming both LEX and F3E would otherwise read as an ordinary
    ambiguous FNDR fact.
    """
    hits = set(cross_entity_guard.detect_entities(text or ""))
    # C9: the channel-token union must be consumed HERE TOO. Without it a
    # contribution naming "#llc-finance" would newly resolve to LEX in
    # resolve_entity while sailing past the blanket LEX skip -- i.e. widening the
    # tagger would have widened what gets FILED. Fail-closed stays fail-closed.
    hits |= channel_token_entities(text)
    if any((h or "").upper().startswith("LEX") for h in hits):
        return True
    return bool(_LEX_TOKEN_RE.search(text or ""))


def normalize_fact(text: str) -> str:
    """Comparison key for duplicate detection.

    DELIBERATELY MINIMAL. The false-merge lesson (DOTALL over-strip, 2026-07-28)
    is that aggressive normalization collapses distinct facts. Two facts differing
    only by a number, a URL, or a mention ARE different facts, so those all
    survive here; only the connector footer, whitespace, case, and one trailing
    period are removed.
    """
    body, _ = strip_connector_footer(text or "")
    body = re.sub(r"\s+", " ", body).strip().lower()
    return body.rstrip(".").strip()


def known_answer_facts(entity: str, *, known_answers_dir: Path | None = None) -> list[str]:
    """The stored fact lines for an entity's known-answers file.

    Skips headers, blank lines, and the bold provenance stamps that
    gap_autofill.apply_contributed_note writes above each fact, so comparison
    runs against the FACT text only. Fail-soft: unreadable file -> []."""
    try:
        base = known_answers_dir or _default_known_answers_dir()
        path = Path(base) / ENTITY_FILES.get((entity or "FNDR").upper(), "fndr.md")
        if not path.exists():
            return []
        out: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or _PROVENANCE_LINE_RE.match(line):
                continue
            out.append(s)
        return out
    except Exception:  # noqa: BLE001
        log.warning("info_intake: known-answers read failed for %s", entity, exc_info=True)
        return []


def _default_known_answers_dir() -> Path:
    import os
    return Path(os.environ.get("KNOWN_ANSWERS_DIR")
                or Path(__file__).resolve().parents[2] / "design" / "known-answers")


def find_pending_duplicate(norm: str, pending: list[dict[str, Any]]) -> str | None:
    """update_id of a PENDING #info-for-cora item carrying the same normalized
    fact, or None. Only PENDING items count -- a dismissed contribution that
    someone deliberately re-posts should reach Harrison again."""
    if not norm:
        return None
    for rec in pending or []:
        payload = rec.get("payload") or {}
        if payload.get("source") != SOURCE:
            continue
        if (rec.get("state") or "PENDING").upper() != "PENDING":
            continue
        if normalize_fact(payload.get("text") or "") == norm:
            return str(rec.get("update_id") or "")
    return None


def classify_against_canon(norm: str, facts: list[str]) -> tuple[str, str]:
    """Compare a normalized contribution against stored canon.

    Returns (verdict, matched_fact) where verdict is one of:
      "duplicate"   -- already canon verbatim; nothing to review
      "supersedes"  -- close enough to be about the same subject but NOT identical,
                       so it likely REVISES canon. Surfaced to Harrison as a
                       proposed supersession; never an automatic overwrite.
      ""            -- new fact
    """
    if not norm:
        return "", ""
    best_ratio, best_fact = 0.0, ""
    for fact in facts:
        nf = normalize_fact(fact)
        if not nf:
            continue
        if nf == norm:
            return "duplicate", fact
        ratio = difflib.SequenceMatcher(None, norm, nf).ratio()
        if ratio > best_ratio:
            best_ratio, best_fact = ratio, fact
    if best_ratio >= SUPERSEDE_SIM:
        return "supersedes", best_fact
    return "", ""


def permalink(channel_id: str, ts: str) -> str:
    """Slack archive permalink. Best-effort: empty when ts is missing rather than
    rendering a bare broken URI (the empty-provenance defect, cq-89fdad5f0f86)."""
    if not ts or not channel_id:
        return ""
    return f"https://hjr-global.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


def looks_like_question(text: str) -> bool:
    """#info-for-cora is used for BOTH questions and contributions (every one of
    Hannah's 2026-05/06 posts was a question). Only statements are contributions;
    questions keep their existing Q&A behaviour and are never queued as facts.

    Lazy import: reuses gap_autofill's reviewed classifier rather than growing a
    second one that can drift. Fail-safe -> treat as a question (skip intake)
    rather than queue a question as a fact."""
    try:
        from . import gap_autofill
        return bool(gap_autofill.looks_like_question(text or ""))
    except Exception:  # noqa: BLE001
        log.warning("info_intake: question classifier failed; skipping intake",
                    exc_info=True)
        return True


# ── The chokepoint ──────────────────────────────────────────────────────────
def ingest(
    *,
    text: str,
    author_id: str,
    author_name: str = "",
    ts: str,
    route: str,
    channel_id: str = CHANNEL_ID,
    channel_name: str = CHANNEL_NAME,
    known_answers_dir: Path | None = None,
    dry_run: bool = False,
) -> IntakeResult:
    """Run one #info-for-cora contribution through the full intake pipeline.

    Slack-free by construction: the caller posts `result.ack` if it is non-empty.
    Never raises -- intake must never break the bot or the sweep.
    """
    try:
        clean, is_connector = strip_connector_footer(text)
        clean = strip_leading_mentions(clean)
        if not clean or not author_id or not ts:
            return IntakeResult(SKIPPED, detail="empty text, author, or ts")

        # [QA] BEFORE anything durable. Log-only, never queued, on every route.
        if is_qa_quarantined(clean):
            log.info("info_intake: [QA] quarantined route=%s ts=%s (not queued)", route, ts)
            return IntakeResult(
                QUARANTINED, is_connector=is_connector,
                ack="[QA] noted -- quarantined as test traffic, not logged as knowledge.",
                detail="[QA] prefix")

        # A question is not a contribution.
        if looks_like_question(clean):
            return IntakeResult(NOT_A_CONTRIBUTION, is_connector=is_connector,
                                detail="reads as a question")

        # PHI is screened on EVERY statement, BEFORE the durable-knowledge screen.
        # Order matters: if "not durable" ran first, a PHI-bearing statement that
        # also looks like task feedback would fall through to the caller as
        # NOT_A_CONTRIBUTION -- and on the @mention route that means Q&A -- instead
        # of drawing the explicit refusal the D1 path always gave it. Questions are
        # checked before PHI on purpose: they are never stored here and must keep
        # reaching the normal Q&A guards (lex_phi_access / user_access), which own
        # the PHI decision for a QUESTION.
        # PHI: 3-predicate union, unconditional, fail-closed.
        try:
            if phi_guard.is_any_phi(clean):
                # LEX-61: record WHICH predicate fired (never the text, D-082)
                # so a false positive is tunable instead of unreproducible.
                log.info("info_intake: PHI-flagged contribution refused route=%s "
                         "user=%s predicates=%s len=%d",
                         route, author_id,
                         ",".join(phi_guard.which_predicates(clean)) or "none",
                         len(clean or ""))
                return IntakeResult(
                    PHI_REFUSED, is_connector=is_connector,
                    ack=("Thanks, but that reads like client / PHI information -- I can't "
                         "capture that here. Client data belongs in the EHR, not in "
                         "Cora's memory."),
                    detail="PHI")
        except Exception:  # noqa: BLE001 -- fail closed: drop rather than risk PHI
            log.warning("info_intake: PHI check failed; dropping", exc_info=True)
            return IntakeResult(ERROR, detail="PHI check failed (dropped fail-closed)")

        # Blanket LEX skip (Harrison mandate 2026-08-06). Placed immediately after
        # the PHI check and BEFORE the durable screen, mirroring the PHI-before-noise
        # rationale: a LEX statement that also looks like noise must still draw the
        # explicit refusal rather than falling through to the caller (which on the
        # @mention route means Q&A). One chokepoint covers all three routes.
        #
        # FAIL CLOSED: a detector exception must refuse, never fall through to
        # resolve_entity's ("FNDR", True) default.
        try:
            lex_hit = is_lex_content(clean)
        except Exception:  # noqa: BLE001 -- fail closed: refuse rather than risk LEX
            log.warning("info_intake: LEX detection failed; refusing fail-closed",
                        exc_info=True)
            lex_hit = True
        if lex_hit:
            log.info("info_intake: LEX content refused route=%s ts=%s", route, ts)
            return IntakeResult(
                LEX_REFUSED, is_connector=is_connector,
                ack=("Lexington items aren't captured through this channel; nothing "
                     "was saved. Share LEX process facts in the LEX channels (client "
                     "data belongs in the EHR)."),
                detail="LEX content")

        # A statement that is not durable organizational knowledge is not a fact.
        not_durable = durable_contribution_reason(clean)
        if not_durable:
            log.info("info_intake: not durable route=%s ts=%s (%s)", route, ts, not_durable)
            return IntakeResult(NOT_A_CONTRIBUTION, is_connector=is_connector,
                                detail=not_durable)

        update_id = intake_update_id(ts)
        norm = normalize_fact(clean)
        entity, ambiguous = resolve_entity(clean)

        # Cross-route idempotency + same-fact dedup against the live queue.
        try:
            pending = knowledge_review.load_proposed_updates()
        except Exception:  # noqa: BLE001
            log.warning("info_intake: pending load failed; continuing", exc_info=True)
            pending = []
        if any(str(r.get("update_id") or "") == update_id for r in pending):
            return IntakeResult(DUPLICATE, update_id=update_id, entity=entity,
                                is_connector=is_connector,
                                detail="same message already queued")
        dup_id = find_pending_duplicate(norm, pending)
        if dup_id:
            return IntakeResult(
                DUPLICATE, update_id=dup_id, entity=entity, is_connector=is_connector,
                ack="Already in the review queue -- I won't queue it twice.",
                detail=f"same fact pending as {dup_id}")

        # Compare against canon already written to known-answers.
        verdict, matched = classify_against_canon(
            norm, known_answer_facts(entity, known_answers_dir=known_answers_dir))
        if verdict == "duplicate":
            return IntakeResult(
                DUPLICATE, update_id=update_id, entity=entity, is_connector=is_connector,
                ack="I already have that one -- no need to review it again.",
                detail="already in known-answers")

        # R5a: neutralize live Slack behaviour BEFORE the text is interpolated into
        # either surface -- the durable known-answers write and the DM card both
        # consume what is built here.
        safe = scrub_contribution(clean)
        # R5b: bound the STORED text and disclose the bound on the card, so the
        # approver can never approve a tail they were not shown.
        stored_text = safe[:STORED_TEXT_CAP]
        truncated = len(safe) > STORED_TEXT_CAP

        label = f"#info-for-cora from {author_name or author_id} ({entity})"
        if ambiguous:
            label += " [entity ambiguous -- filed FNDR]"
        if verdict == "supersedes":
            label += f" [MAY SUPERSEDE existing fact: {scrub_contribution(matched)[:120]}]"
        if truncated:
            label += (f" [stored text truncated to {STORED_TEXT_CAP} chars; "
                      f"full post at the permalink]")
        link = permalink(channel_id, ts)
        description = f"{label}: {safe[:CARD_TEXT_CAP]}"
        if link:
            description = f"{description}\n{link}"

        payload = {
            "text": stored_text,
            "author_id": author_id,
            "author_name": author_name or author_id,
            "entity": entity,
            "ambiguous_entity": ambiguous,
            "channel": channel_name,
            "channel_id": channel_id,
            "source": SOURCE,
            "message_ts": ts,
            "permalink": link,
            "intake_route": route,
            "connector_relayed": is_connector,
            "stored_text_truncated": truncated,
        }
        if verdict == "supersedes":
            # Scrubbed too: this excerpt comes from known-answers, which is itself
            # fed by contributions, and it rides the same mirrored ledger + card.
            payload["supersedes_candidate"] = scrub_contribution(matched)

        if dry_run:
            return IntakeResult(
                SUPERSEDES if verdict == "supersedes" else QUEUED,
                update_id=update_id, entity=entity, ambiguous_entity=ambiguous,
                is_connector=is_connector, supersedes=matched if verdict else "",
                detail="dry-run (not written)")

        try:
            appended = knowledge_review.propose_update(
                update_id=update_id,
                update_type=knowledge_review.UPDATE_TYPE_GENERIC,
                description=description,
                payload=payload,
                # R2 (fan-out Lens A-1, HIGH): source_evidence is persisted into
                # data/cora-proposed-memory-updates.jsonl, which is the FIRST entry in
                # drive_materializer._FLYWHEEL_LEDGERS and is byte-copied UNSCREENED to
                # the org-readable _brain/_flywheel/ Drive store. Passing the raw
                # contribution here egressed it org-wide BEFORE Harrison ever approved
                # it -- the same class the pipeline bundle fixed one module over on
                # 8/6 (gap_autofill's fix was likewise source_evidence=""). The review
                # card renders description, which already carries clean[:240], so
                # nothing is lost from the approval surface.
                source_evidence="",
                confidence="MED",
            )
        except Exception:  # noqa: BLE001 -- intake must never break the bot
            log.warning("info_intake: propose_update failed route=%s", route, exc_info=True)
            return IntakeResult(ERROR, entity=entity, detail="propose_update failed")

        # propose_update returns False when this id was ALREADY in the ledger. That
        # is the concurrent-delivery race: two routes can both pass the pending-load
        # check above before either writes, and propose_update resolves it under its
        # own lock. Honouring the return value is what keeps the loser SILENT --
        # otherwise one contribution would draw two "logged for review" acks the
        # moment the message-event subscription starts firing alongside @mention.
        if appended is False:
            log.info("info_intake: id already queued (race) route=%s id=%s", route, update_id)
            return IntakeResult(DUPLICATE, update_id=update_id, entity=entity,
                                is_connector=is_connector,
                                detail="already queued by a concurrent route")

        log.info("info_intake: queued route=%s user=%s entity=%s id=%s supersedes=%s",
                 route, author_id, entity, update_id, bool(verdict))
        ack = ("Got it -- logged for Harrison's review. It won't become shared org "
               "knowledge until he approves it.")
        if verdict == "supersedes":
            ack = ("Got it -- logged for Harrison's review. Heads up: this looks like it "
                   "revises something I already have, so I've flagged it as an update "
                   "rather than a new fact.")
        elif ambiguous:
            ack = ("Got it -- logged for Harrison's review. I couldn't pin it to one "
                   "entity, so it's filed portfolio-wide for him to retag.")
        return IntakeResult(
            SUPERSEDES if verdict == "supersedes" else QUEUED, ack=ack,
            update_id=update_id, entity=entity, ambiguous_entity=ambiguous,
            is_connector=is_connector, supersedes=matched if verdict else "")
    except Exception:  # noqa: BLE001 -- absolute backstop
        log.error("info_intake: unexpected failure route=%s", route, exc_info=True)
        return IntakeResult(ERROR, detail="unexpected failure")
