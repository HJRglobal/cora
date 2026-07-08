"""Daily leadership-channel synthesis -- the operational sibling of the weekly
Harrison-only strategy memo (strategy_memo.py).

Two DISTINCT products share the gather/snapshot/synthesis machinery:
  - strategy_memo.py  -> WEEKLY, Harrison-DM-only, blunt founder-strategy memo.
    Its "never post to a channel" invariant is unchanged and lives in its module.
  - channel_synthesis.py (this module) -> DAILY, team-appropriate operational
    syntheses posted to leadership channels (portfolio + 8 entities).

Standalone-script invariant (D-047): this module and its runners must NEVER
import app.py / tool_dispatch.py / claude_client.py, so no bot restart is ever
needed. It reuses strategy_memo's PUBLIC gather/snapshot/facts/delta helpers and
imports only pure modules (phi_guard, reply_formatter, slack_egress,
asana_filters) + connectors lazily.

Guardrails (preserve-list -- see the 2026-07-07 build spec):
  - Financial firewall: cash posts ONLY to a TIER_1 channel. A standalone script
    cannot resolve a channel's tier from its id (channel_classifier is name-based
    and #founder-operations even mis-classifies as unknown/TIER_3), so the
    explicit id allowlist below IS the tier gate, fail-closed.
  - Entity firewall (cross_entity_guard) -- no cross-entity bleed in an entity
    synthesis (post-synthesis assertion).
  - PHI / LEX wall (phi_guard) -- LEX aggregate-only; the LEX synthesis is the
    highest-stakes surface (its own gather + prompt + output backstop).
  - Source-opacity / egress (slack_egress) -- every send through the boundary;
    F3E source-opaque.
  - Visibility-CPA exclusion; D-011 (advisory only, no Asana/decisions/KB writes).
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

from . import strategy_memo as sm

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Channels + TIER_1 allowlist (the financial-firewall gate)
# ---------------------------------------------------------------------------
# Scope -> target channel id. ALL are TIER_1 (leadership/founder/build per
# channel_classifier.TIER_1_FUNCTIONS), verified private 2026-07-07. HJRG folds
# into the portfolio post (no separate HJRG channel synthesis).
SCOPE_CHANNELS: dict[str, str] = {
    "portfolio": "C0BCUBUDHAR",  # #founder-operations (holdco; covers HJRG)
    "f3e":       "C0B4KRQT3LY",  # #f3e-leadership
    "hjrp":      "C0B3A3W2A3H",  # #hjrp-leadership
    "osn":       "C0B3TCEF4KT",  # #osn-leadership
    "lex":       "C0B3A3U7WS3",  # #lex-leadership
    "bdm":       "C0B3PF5QK9C",  # #bdm-leadership
    "ufl":       "C0B3N5YG1SR",  # #ufl-leadership
    "hjrprod":   "C0BFCM2TV55",  # #hjrprod-leadership
    "f3c":       "C0BFCMB2JFR",  # #f3c-leadership
}

# #cora-build -- the dry-run/smoke target (also TIER_1: function "build").
SMOKE_CHANNEL = "C0B4B0URRQS"

# The financial firewall, defense-in-depth: cash may post ONLY to an id in this
# set. Any other channel id is refused (fail-closed) -- see deliver_to_channel.
_TIER1_CHANNEL_IDS: frozenset[str] = frozenset(SCOPE_CHANNELS.values()) | {SMOKE_CHANNEL}

_MAX_SLACK_CHARS = 39000  # mirror strategy_memo.deliver_to_harrison


def _assert_tier1(channel_id: str) -> bool:
    """True iff channel_id is an allowlisted TIER_1 synthesis target. This is the
    only sanctioned tier check on the standalone path (no runtime id->name map)."""
    return bool(channel_id) and channel_id in _TIER1_CHANNEL_IDS


# ---------------------------------------------------------------------------
# Delivery -- channel post (sibling to strategy_memo.deliver_to_harrison)
# ---------------------------------------------------------------------------

def deliver_to_channel(channel_id: str, body: str, *, today: date | None = None) -> bool:
    """Post a synthesis to *channel_id*. Fail-soft (log + return False, never raise).

    Financial firewall: refuses (posts NOTHING) if channel_id is not TIER_1
    allowlisted. Routes through the egress boundary -- explicit sanitize_text
    (belt-and-suspenders; the WebClient class patch also sanitizes the text=
    kwarg, but the explicit call is robust to a positional-arg refactor) plus
    normalize_slack_bold (Sonnet emits ** which Slack renders literally).

    Does NOT touch deliver_to_harrison or its hard-coded Harrison-only rule.
    """
    if not _assert_tier1(channel_id):
        log.error("channel_synthesis: refusing to post to non-TIER_1 channel %s "
                  "(financial firewall, fail-closed)", channel_id)
        return False
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("channel_synthesis: SLACK_BOT_TOKEN not set -- cannot post")
        return False
    if not body:
        log.error("channel_synthesis: empty body -- nothing to post to %s", channel_id)
        return False

    from slack_sdk import WebClient

    from .reply_formatter import normalize_slack_bold
    from .slack_egress import sanitize_text

    text = sanitize_text(normalize_slack_bold(body))[:_MAX_SLACK_CHARS]
    try:
        client = WebClient(token=token)
        client.chat_postMessage(channel=channel_id, text=text)
        log.info("channel_synthesis: posted to %s (%d chars)", channel_id, len(text))
        return True
    except Exception as exc:  # noqa: BLE001 -- fail-soft; never raise
        log.error("channel_synthesis: post to %s failed: %s", channel_id, exc)
        return False


def _today() -> date:
    return sm._today()


# ---------------------------------------------------------------------------
# Per-scope snapshots (day-over-day deltas; separate from the weekly memo)
# ---------------------------------------------------------------------------

def _synthesis_snapshot_root() -> Path:
    return Path(os.environ.get("SYNTHESIS_SNAPSHOT_DIR")
                or sm._REPO_ROOT / "data" / "state" / "synthesis-snapshots")


def _scope_snapshot_dir(scope: str) -> Path:
    """A distinct snapshot dir per scope (portfolio / f3e / osn / ...), so daily
    deltas never collide with the weekly memo's strategy-memo-snapshots."""
    return _synthesis_snapshot_root() / scope


# ---------------------------------------------------------------------------
# Synthesis (Sonnet, FAIL-CLOSED, output PHI backstop)
# ---------------------------------------------------------------------------

def _default_phi_check(text: str) -> bool:
    """Output backstop for the portfolio + non-LEX entity syntheses.

    Uses the NARROW is_clinical_phi (DOB / ICD-10 / 'diagnosed with X' / bare
    diagnosis terms / medication names). DELIBERATELY NOT the broad is_phi_risk:
    that over-trips on ordinary aggregate program vocab (AHCCCS / Medicaid /
    assessment / discharge / member id) that legitimately appears in a holdco
    operational post's Lexington aggregate line, and would false-drop it to the
    fallback every run. Clinical PHI (a leaked diagnosis / med) is the real
    hazard the backstop must catch; the gather + prompt layers are the primary
    guarantee that no client detail reaches the facts at all. LEX has its own
    stricter output gate (see synthesize_channel_entity)."""
    from .phi_guard import is_clinical_phi
    return is_clinical_phi(text)


def _synthesize(prompt_text: str, *, phi_check=None) -> str | None:
    """One Sonnet synthesis call. FAIL-CLOSED: None on missing key / API error /
    empty output / a positive PHI check -- the caller falls back to a deterministic
    factual rollup, never a hallucinated or PHI-bearing post."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning("channel_synthesis: ANTHROPIC_API_KEY not set -- no synthesis")
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=sm.SONNET_MODEL,
            max_tokens=sm._SYNTH_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt_text}],
        )
        text = (response.content[0].text or "").strip()
    except Exception as exc:  # noqa: BLE001 -- fail-closed by design
        log.warning("channel_synthesis: synthesis failed: %s", exc)
        return None
    if not text:
        return None
    checker = phi_check or _default_phi_check
    try:
        if checker(text):
            log.warning("channel_synthesis: synthesized output tripped PHI check "
                        "-- dropping to factual fallback")
            return None
    except Exception:  # noqa: BLE001 -- a broken checker must fail CLOSED
        log.exception("channel_synthesis: PHI check raised -- dropping to fallback")
        return None
    return text


_PORTFOLIO_PROMPT = """\
You are writing the DAILY portfolio operations briefing for the #founder-operations
channel of a multi-entity holding company (HJR Global is the holdco / shared-services
spine; the operating entities are F3 Energy, One Stop Nutrition, Lexington Services,
HJR Properties, Big D Media, United Fight League, HJR Productions, F3 Community).

Below is today's verified fact base, including day-over-day deltas. Write a concise,
operational briefing (roughly 200-320 words) with these sections, each on its own
line with a bold header:

*Portfolio pulse* -- 2-3 lines: the overall cash position and the single most
important movement of the day.
*Cash* -- the notable per-entity closing balances and day-over-day changes, plus any
multi-day decline streaks. Lexington is aggregate only.
*Pipeline* -- open deal posture and any stage movement or aging deals worth attention.
*Deadlines* -- what is due soon and what is overdue (counts plus the few that matter).
*Needs Harrison* -- the stalled P0/P1 decisions: the shortest possible list of what
needs a founder call. This is an operational status list, not strategic advice.

Hard rules:
- Use ONLY facts present in the fact base. Never invent numbers, deals, dates, or
  names. If a section's source was unavailable, say so in one short line.
- This is an OPERATIONAL status post for the team -- NOT founder strategy. Do NOT make
  business-restructuring recommendations or blunt strategic calls; those live in the
  private weekly memo. Report the state of the world and what needs a decision.
- Never include any client name, diagnosis, or client-level health information --
  Lexington data stays strictly aggregate.
- Advisory / operational only; nothing here executes automatically.
- Plain text, Slack-friendly. Use *single asterisks* for bold. No markdown tables.

FACT BASE:
{facts}
"""


def synthesize_channel_portfolio(facts_text: str) -> str | None:
    """Operational holdco rollup for #founder-operations (Sonnet, FAIL-CLOSED)."""
    return _synthesize(_PORTFOLIO_PROMPT.format(facts=facts_text))


# ---------------------------------------------------------------------------
# Orchestrator (per-scope; channel_synthesis analog of strategy_memo.run_memo)
# ---------------------------------------------------------------------------

def run_synthesis(
    scope: str,
    *,
    gather_fn,
    synth_fn,
    deliver_fn,
    deltas_fn=None,
    facts_fn=None,
    dry_run: bool = False,
    today: date | None = None,
    snapshot_dir: Path | None = None,
) -> dict:
    """One synthesis run for *scope*. dry_run: gather + synthesize but write/send
    NOTHING (no snapshot, no post) -- the rollout-gate review mode. Snapshots key
    per-scope so day-over-day deltas never collide across scopes or with the
    weekly memo. No Drive memo file is written (channel post + snapshot only)."""
    today = today or _today()
    deltas_fn = deltas_fn or sm.compute_deltas
    facts_fn = facts_fn or sm.build_facts_text
    snap_dir = snapshot_dir or _scope_snapshot_dir(scope)

    gathered = gather_fn()
    priors = sm.load_prior_snapshots(today=today, snapshot_dir=snap_dir)
    deltas = deltas_fn(gathered, priors)
    facts = facts_fn(gathered, deltas)

    body = synth_fn(facts)
    synthesized = body is not None
    if body is None:
        body = sm.fallback_memo(facts)

    delivered = False
    if not dry_run:
        sm.save_snapshot(gathered, today=today, snapshot_dir=snap_dir)
        delivered = deliver_fn(body)

    return {
        "scope": scope,
        "dry_run": dry_run,
        "date": today.isoformat(),
        "first_run": bool(deltas.get("first_run")),
        "synthesized": synthesized,
        "delivered": delivered,
        "facts": facts,
        "body": body,
    }


def run_portfolio(*, dry_run: bool = False, today: date | None = None,
                  channel: str | None = None) -> dict:
    """Portfolio synthesis -> #founder-operations (holdco; covers HJRG)."""
    today = today or _today()
    ch = channel or SCOPE_CHANNELS["portfolio"]
    return run_synthesis(
        "portfolio",
        gather_fn=lambda: sm.gather_all(today=today),
        synth_fn=synthesize_channel_portfolio,
        deliver_fn=lambda body: deliver_to_channel(ch, body, today=today),
        dry_run=dry_run,
        today=today,
    )
