"""The push card: staged, not written, until a human taps.

Card copy per the design proposal SS4.1. D-109: no em-dashes anywhere in this
module's OUTWARD text -- every separator below is a middot (`·`) or a
plain hyphen, never an em-dash, and `tests/test_deposco_orders_cards.py`
pins that against the same character the Class-B guard checks for.

Every string here is read by a HUMAN (Harrison) and by no model -- there is
no sentinel to strip and nothing a model-facing directive would do here, the
same posture `f3e_blog.publish_cards` documents for its own card copy.
"""

from __future__ import annotations

from .. import confirm_cards, slack_egress
from . import pending

ACTION_PUSH_CONFIRM = "cora_deposco_push_confirm"
ACTION_PUSH_DISMISS = "cora_deposco_push_dismiss"

_ENV_HOST = {"prod": "api.deposco.com", "ua": "sandboxapi.deposco.com"}

#: The design's own approver-tier target for the "N of 10" framing. Standing
#: eligibility is a SEPARATE decision (pending.approver_tier); this constant
#: only shapes the card's wording.
CLEAN_GATE_TARGET = 10


def _order_and_lines(entry: dict) -> tuple[dict, list[dict]]:
    order = (entry.get("payload") or {}).get("order", [{}])[0]
    lines = (order.get("orderLines") or {}).get("orderLine", [])
    return order, lines


def _preflight_summary(entry: dict) -> str:
    preflight = entry.get("preflight") or {}
    checks = {c["name"]: c for c in preflight.get("checks", [])}

    def mark(name: str, ok_word: str, fail_word: str) -> str:
        check = checks.get(name)
        if check is None:
            return "not run"
        return ok_word if check.get("passed") else f"{fail_word} - {check.get('detail', '')}"

    number_state = mark("number_miss", "not found", "ALREADY EXISTS")
    ref_state = mark("reference_miss", "not found", "ALREADY REFERENCED")
    items_state = mark("items_exist", "all exist", "MISSING")
    atp_state = mark("atp_sufficient", "ok", "SHORT")
    ship_via_state = mark("ship_via_pinned", "ok", "UNCONFIRMED")

    checked_at = preflight.get("checked_at") or ""
    return (
        f"Pre-flight  number: {number_state} · reference: {ref_state} · "
        f"items {items_state} · ATP {atp_state} · shipVia {ship_via_state} "
        f"· checked {checked_at}"
    )


def build_push_card(entry: dict) -> tuple[str, list[dict]]:
    """(fallback_text, blocks) for a SUPERVISED push card, per design SS4.1."""
    channel = entry.get("channel", "")
    order, lines = _order_and_lines(entry)
    clean_count = pending.consecutive_clean_count(channel)

    line_texts = []
    for i, line in enumerate(lines, start=1):
        line_texts.append(
            f"  {i} {line.get('itemNumber', ''):<16s} {line.get('orderPackQuantity', '')} "
            f"@ ${line.get('unitPrice', '')}"
        )
    total_qty = sum(int(float(l.get("orderPackQuantity", 0) or 0)) for l in lines)

    header = (
        f"DEPOSCO PUSH - STAGED, NOT WRITTEN · channel {channel.upper()} · "
        f"supervised push {clean_count + 1} of {CLEAN_GATE_TARGET} ({clean_count} clean so far)"
    )
    body_lines = [
        header,
        f"Order   {order.get('number', '')} · {order.get('type', '')} · "
        f"orderSource {order.get('orderSource', '')} · env PROD "
        f"({_ENV_HOST.get('prod', '')})",
        f"Refs    {order.get('otherReferenceNumber', '')} · freight "
        f"{(order.get('freight') or {}).get('termsType', '')} · shipVia "
        f"{order.get('shipVia', '')} · planned ship {order.get('plannedShipDate', 'not set')}",
        f"Lines   {len(lines)} · {total_qty} twelve-packs · ${order.get('orderTotal', '0.00')}",
        *line_texts,
        _preflight_summary(entry),
        f"Spec    {entry.get('spec_path') or '(none recorded)'} · sha256 "
        f"{entry.get('payload_hash', '')[:12]}",
        "MANUAL LANE IS OFF FOR THIS ORDER. No API cancel exists: after push, "
        "changes = call Nimbl.",
    ]
    body = slack_egress.sanitize_text("\n".join(body_lines))
    blocks = confirm_cards.chunk_mrkdwn_sections(body)
    blocks.append({
        "type": "actions",
        "block_id": (f"cora_deposco_push_actions_{entry.get('id', '')}")[:255],
        "elements": [
            {"type": "button", "action_id": ACTION_PUSH_CONFIRM, "style": "danger",
             "text": {"type": "plain_text", "text": "Push to Deposco PROD"},
             "value": entry.get("id", "")},
            {"type": "button", "action_id": ACTION_PUSH_DISMISS,
             "text": {"type": "plain_text", "text": "Dismiss"},
             "value": entry.get("id", "")},
        ],
    })
    fallback = f"Deposco push staged for {order.get('number', '')} (channel {channel})"
    return fallback, blocks


_OUTCOME_HEADLINE = {
    pending.STATE_CONFIRMED: "CONFIRMED - order created and read back clean.",
    pending.STATE_FAILED: "FAILED - nothing was created. Spec needs correction.",
    pending.STATE_UNKNOWN: (
        "UNKNOWN - check esm.deposco.com before anything else. This entry is "
        "locked; it will never auto-retry."
    ),
    pending.STATE_ANOMALY_UPDATED: (
        "ANOMALY: an EXISTING order was silently updated (a 200, not a 201). "
        "Call Nimbl now."
    ),
    pending.STATE_MISMATCH: (
        "MISMATCH: the order Deposco shows does not match what was sent. "
        "Call Nimbl now."
    ),
    pending.STATE_DISMISSED: "Dismissed. Nothing was sent to Deposco.",
}


def terminal_card_blocks(orig_blocks: list[dict], entry: dict) -> list[dict]:
    """Rewrite a tapped card so its own body no longer contradicts the
    outcome -- same reasoning as `publish_cards.terminal_card_blocks`: the
    first two lines here are STATE CLAIMS the tap has just falsified."""
    state = entry.get("state", "")
    message = _OUTCOME_HEADLINE.get(state, f"Resolved: {state}")
    kept: list[dict] = []
    for block in orig_blocks or []:
        if block.get("type") != "section":
            continue
        text = ((block.get("text") or {}).get("text") or "")
        lines = [
            ln for ln in text.split("\n")
            if not ln.startswith("DEPOSCO PUSH - STAGED")
            and not ln.startswith("MANUAL LANE IS OFF")
        ]
        body = "\n".join(lines).strip()
        if body:
            kept.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
    kept.insert(0, {"type": "section", "text": {
        "type": "mrkdwn", "text": slack_egress.sanitize_text(message)}})
    return kept
