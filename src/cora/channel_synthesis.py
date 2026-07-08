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
