"""Orchestration: stage an order, deliver its card, and resolve a tap.

Design of record: SONNET-HANDOFF steps 4 and 7. DETERMINISTIC throughout --
no LLM touches any part of the order path (Fork 1). The actual Slack
`@app.action` wiring lives in `app.py`, mirroring `f3e_blog/publish_cards.py`'s
thin-wrapper split: `app.py` extracts actor/channel/message_ts from the Slack
payload and the `CORA_EVAL_MODE` early return; every correctness promise
(authority, exactly-once claim, live re-check, classification) lives here.

APPROVER ALLOWLIST IS PINNED IN CODE, PER TIER:
  * supervised: `HARRISON_ID` only -- the exact constant
    `f3e_blog/publish_cards.py` uses. This is the ONLY reachable tier in this
    build: no channel is ever in `CORA_DEPOSCO_STANDING_CHANNELS` by default,
    and this session's guardrail forbids setting it.
  * standing: {Harrison, Alex, Eric}, author != approver (two-person rule),
    Harrison FYI-only. Wired and tested so the code is READY, but inert until
    Harrison sets the env flag AND the October S2' external-write gate clears
    (2026-09-09 ruling item 3) -- neither happens in this session.

RE-PREFLIGHT LIVE AT TAP. The staged preflight can be minutes to hours old;
drift between staging and tap -- the order now exists, an item vanished, ATP
fell short -- must never push on stale evidence. A drifted entry is
RE-STAGED (`pending.release_claim`), never pushed.

TTL: 8 hours (a stale ATP is the risk named in the design). Past it, the
entry is dismissed rather than claimed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime

from ..connectors import deposco_client as dc
from ..connectors import deposco_push as dpush
from . import cards, payload as payload_mod, pending, preflight, readback
from . import spec as spec_mod

log = logging.getLogger(__name__)

# Same fixed id as user_access._HARRISON_ID / f3e_blog.publish_cards.HARRISON_ID.
HARRISON_ID = os.environ.get("HARRISON_SLACK_USER_ID", "U0B2RM2JYJ1")
#: Alex Cordova (F3E ops) and Eric Canku, per CLAUDE.md KEY IDS / the finance
#: allowlist. Standing-tier constants, inert until the conditions above hold.
ALEX_ID = "U0B3VGWJTMJ"
ERIC_ID = "U0B3PRZMBCN"

SUPERVISED_APPROVERS = frozenset({HARRISON_ID})
STANDING_APPROVERS = frozenset({HARRISON_ID, ALEX_ID, ERIC_ID})
_NAME_TO_SLACK_ID = {"harrison": HARRISON_ID, "alex": ALEX_ID, "eric": ERIC_ID}

TTL_SECONDS = 8 * 3600


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


@dataclass
class StageOutcome:
    ok: bool
    entry: dict | None = None
    card_fallback: str = ""
    card_blocks: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    blocked_reason: str = ""


def stage_order(
    order_spec: spec_mod.OrderSpec, *, env: str, spec_raw: dict, spec_path: str = "",
    client: dc.DeposcoClient | None = None,
) -> StageOutcome:
    """Validate-then-preflight-then-persist. `order_spec` must already have
    passed `spec.validate_spec` -- this function does not re-validate the
    human-authored shape, only the LIVE checks and the channel gate."""
    blocked, reason = pending.channel_stage_blocked(order_spec.channel)
    if blocked:
        return StageOutcome(ok=False, blocked_reason=reason)

    active_client = client or dc.DeposcoClient(env=env)
    result = preflight.run_preflight(order_spec, active_client)
    if not result.passed:
        return StageOutcome(ok=False, errors=[c.detail for c in result.failed_checks()])

    payload = payload_mod.build_payload(order_spec)
    number = payload_mod.build_order_number(order_spec)
    entry = pending.stage_entry(
        channel=order_spec.channel, number=number, payload=payload, env=env,
        preflight_result=result, spec_raw=spec_raw, spec_path=spec_path,
        authored_by=order_spec.authored_by,
    )
    fallback, blocks = cards.build_push_card(entry)
    return StageOutcome(ok=True, entry=entry, card_fallback=fallback, card_blocks=blocks)


def _default_client_factory():
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return None
    from slack_sdk import WebClient  # noqa: PLC0415
    return WebClient(token=token)


def deliver_card(entry: dict, fallback: str, blocks: list, *, client_factory=None) -> dict:
    """DM the card to the entry's approver-tier target (supervised: Harrison
    only). Fail-soft, same posture as `publish_cards.stage_card`: the entry
    is already persisted, so a Slack failure leaves it recorded-but-
    undelivered rather than lost."""
    factory = client_factory or _default_client_factory
    client = factory()
    if client is None:
        log.warning("deposco_orders: no Slack token -- entry %s recorded but not sent",
                   entry.get("id"))
        return entry
    target = HARRISON_ID  # supervised only, reachable in this build
    try:
        dm = client.conversations_open(users=[target])
        channel_id = dm["channel"]["id"]
        posted = client.chat_postMessage(
            channel=channel_id, text=fallback, blocks=blocks,
            unfurl_links=False, unfurl_media=False,
        )
        updated = pending.record_delivery(
            entry["id"], dm_channel_id=channel_id, dm_message_ts=posted.get("ts", ""),
        )
        return updated or entry
    except Exception as exc:  # noqa: BLE001
        log.error("deposco_orders: card DM FAILED for %s: %s", entry.get("id"), exc)
        return entry


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


def _actor_matches_author(actor_id: str, authored_by: str) -> bool:
    return _NAME_TO_SLACK_ID.get((authored_by or "").strip().lower()) == actor_id


def _is_authorized(actor_id: str, channel: str, authored_by: str) -> tuple[bool, str]:
    tier = pending.approver_tier(channel)
    if tier == "standing":
        if actor_id not in STANDING_APPROVERS:
            return False, "not_in_standing_set"
        if _actor_matches_author(actor_id, authored_by):
            return False, "author_cannot_approve_own_spec"
        return True, ""
    if actor_id != HARRISON_ID:
        return False, "not_harrison"
    return True, ""


def _age_seconds(entry: dict) -> float | None:
    """None means "cannot determine age" (absent or malformed `created_at`),
    which SKIPS the TTL check rather than crashing the tap -- the TTL is a
    belt-and-suspenders control on top of the live re-preflight this function
    gates, which independently re-checks ATP/existence regardless of age."""
    created_at = entry.get("created_at")
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(created_at)
        return (datetime.now(created.tzinfo) - created).total_seconds()
    except (TypeError, ValueError):
        return None


def _already_handled_message(entry: dict) -> str:
    state = entry.get("state")
    if state == pending.STATE_CLAIMED:
        return "I'm in the middle of pushing that one right now. Give it a few seconds."
    if state in pending.TERMINAL_STATES:
        return f"That order is already resolved ({state})."
    return "That order is already handled; nothing changed just now."


def _spec_from_entry(entry: dict) -> spec_mod.OrderSpec:
    result = spec_mod.validate_spec(entry.get("spec_raw") or {})
    if not result.ok:
        raise RuntimeError(f"stored spec no longer validates: {result.errors}")
    return result.spec


# ---------------------------------------------------------------------------
# The tap
# ---------------------------------------------------------------------------


def process_dismiss_tap(pending_id: str, actor_id: str) -> tuple[str, str]:
    if not pending_id:
        return "orphaned", "I don't have a record of that order anymore."
    entry = pending.get_entry(pending_id)
    if entry is None:
        return "orphaned", "I don't have a record of that order anymore."
    ok, _ = _is_authorized(actor_id, entry.get("channel", ""), entry.get("authored_by", ""))
    if not ok:
        return "not_authorized", "Only this channel's approver can act on a Deposco order."
    if entry.get("state") != pending.STATE_STAGED:
        return "already_handled", _already_handled_message(entry)
    dismissed = pending.dismiss(pending_id, actor_id)
    if dismissed is None:
        return "already_handled", "Someone already acted on this one."
    return "dismissed", (
        "Dismissed. Nothing was sent to Deposco. Correct the spec and re-stage if needed."
    )


def process_push_tap(pending_id: str, actor_id: str) -> tuple[str, str]:
    """Apply a Push tap. Returns (outcome, message-for-the-human).

    Outcomes: confirmed | failed | unknown | anomaly_updated | mismatch |
              not_authorized | orphaned | already_handled | drifted_restaged |
              stale_dismissed | error

    Ordering: lookup, then authority (a channel's tier lives on the entry, so
    unlike a single-global-approver lane this cannot check authority before
    lookup) -- then state, then TTL, then the exactly-once claim, then a LIVE
    preflight re-check, then the push, then classification.
    """
    if not pending_id:
        return "orphaned", "I don't have a record of that order anymore."

    entry = pending.get_entry(pending_id)
    if entry is None:
        return "orphaned", "I don't have a record of that order anymore."

    ok, reason = _is_authorized(actor_id, entry.get("channel", ""), entry.get("authored_by", ""))
    if not ok:
        if reason == "author_cannot_approve_own_spec":
            return "not_authorized", (
                "You authored this spec -- the two-person rule means someone "
                "else has to approve it."
            )
        return "not_authorized", "Only this channel's approver can push a Deposco order."

    if entry.get("state") != pending.STATE_STAGED:
        return "already_handled", _already_handled_message(entry)

    age = _age_seconds(entry)
    if age is not None and age > TTL_SECONDS:
        pending.dismiss(pending_id, actor_id, reason=f"stale (age {int(age)}s > {TTL_SECONDS}s TTL)")
        return "stale_dismissed", (
            "This staged order aged out (over 8 hours) -- its ATP figure is no "
            "longer trustworthy. Re-stage it from the spec."
        )

    claimed = pending.claim_for_push(pending_id, actor_id)
    if claimed is None:
        return "already_handled", "Someone already acted on this one."

    try:
        env = claimed.get("env", "prod")
        read_client = dc.DeposcoClient(env=env)
        order_spec = _spec_from_entry(claimed)
        fresh = preflight.run_preflight(order_spec, read_client)
    except Exception as exc:  # noqa: BLE001 -- must not push blind on a recheck failure
        pending.release_claim(pending_id, f"preflight re-check errored: {exc.__class__.__name__}")
        return "error", "I could not re-check this order before pushing, so nothing was sent."

    if not fresh.passed:
        detail = "; ".join(c.detail for c in fresh.failed_checks())
        pending.release_claim(pending_id, detail)
        return "drifted_restaged", (
            f"Something changed since this was staged ({detail}). Nothing was "
            f"pushed -- re-stage it."
        )

    push_client = dpush.DeposcoPushClient(env=env)
    try:
        outcome = push_client.push_order(
            claimed["payload"], channel=claimed["channel"], claimed_pending_id=pending_id,
        )
    except dpush.DeposcoPushRefused as exc:
        pending.release_claim(pending_id, str(exc))
        return "error", f"Push refused: {exc}"
    except dc.DeposcoAuthError:
        pending.release_claim(pending_id, "auth error on push")
        return "error", "Credentials were rejected -- nothing was sent. Check the Deposco creds."

    return _classify_and_resolve(pending_id, claimed, outcome, read_client)


def _classify_and_resolve(
    pending_id: str, entry: dict, outcome: dpush.PushOutcome, read_client: dc.DeposcoClient,
) -> tuple[str, str]:
    channel = entry["channel"]
    payload = entry["payload"]
    number = payload["order"][0]["number"]
    actor = entry.get("claimed_by", "")

    if outcome.blank_after_retries or outcome.network_exhausted:
        pending.resolve(pending_id, pending.STATE_UNKNOWN, push_status=outcome.status)
        pending.append_ledger_row(channel=channel, number=number, event=pending.STATE_UNKNOWN,
                                  clean=False, actor=actor)
        return "unknown", (
            f"UNKNOWN: the push for {number} did not get a clear answer from Deposco. "
            f"Check esm.deposco.com before anything else. This entry is locked -- "
            f"it will never auto-retry."
        )

    # Deposco wraps every /orders response in a multistatus envelope (outer
    # HTTP status observed 207 for both a create and a duplicate, live
    # 2026-09-23) -- the real per-order result is in the body text, never the
    # outer status code. See PushOutcome's docstring for the full finding.
    if outcome.updated:
        pending.resolve(pending_id, pending.STATE_ANOMALY_UPDATED, push_status=outcome.status)
        pending.append_ledger_row(channel=channel, number=number,
                                  event=pending.STATE_ANOMALY_UPDATED, clean=False, actor=actor)
        return "anomaly_updated", (
            f"ANOMALY: Deposco says an EXISTING order {number} was silently "
            f"UPDATED, not created. Call Nimbl now."
        )

    if outcome.created:
        rb = readback.read_back_with_retries(read_client, payload)
        if not rb.found:
            pending.resolve(pending_id, pending.STATE_UNKNOWN, push_status=201)
            pending.append_ledger_row(channel=channel, number=number, event=pending.STATE_UNKNOWN,
                                      clean=False, actor=actor)
            return "unknown", (
                f"UNKNOWN: Deposco said 201 Created for {number} but I cannot find it "
                f"on read-back. Check esm.deposco.com before anything else. This "
                f"entry is locked -- it will never auto-retry."
            )
        if rb.clean:
            pending.resolve(pending_id, pending.STATE_CONFIRMED, push_status=201)
            pending.append_ledger_row(channel=channel, number=number,
                                      event=pending.STATE_CONFIRMED, clean=True, actor=actor)
            return "confirmed", f"CONFIRMED: {number} created and read back clean."
        pending.resolve(pending_id, pending.STATE_MISMATCH, push_status=201,
                        mismatches=rb.mismatches)
        pending.append_ledger_row(channel=channel, number=number, event=pending.STATE_MISMATCH,
                                  clean=False, actor=actor)
        return "mismatch", (
            f"MISMATCH: {number} was created but does not match what was sent "
            f"({'; '.join(rb.mismatches)}). Call Nimbl now."
        )

    pending.resolve(pending_id, pending.STATE_FAILED, push_status=outcome.status,
                    detail=(outcome.text or "")[:300])
    pending.append_ledger_row(channel=channel, number=number, event=pending.STATE_FAILED,
                              clean=False, actor=actor)
    return "failed", (
        f"FAILED: Deposco rejected {number} (HTTP {outcome.status}). Nothing was "
        f"created. Correct the spec and re-stage."
    )
