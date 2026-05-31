"""Gmail → HubSpot email sync connector.

For each user in slack-to-hubspot.yaml, scans their Gmail inbox for threads
involving known HubSpot contacts and auto-logs matching emails as HubSpot
email engagements.

Matching logic:
  1. Collect all external participant emails from the Gmail thread
  2. Look up each external email in HubSpot contacts
  3. If exactly one contact matches and has associated deals → auto-log, apply Cora-HubSpot label
  4. If ambiguous (multiple contacts, or contact has no deals) → DM the owner for clarification
  5. If no contact found → skip silently

"External" = not in _INTERNAL_DOMAINS; internal-to-internal emails are ignored.

State persistence: data/hubspot-email-sync-state.json stores last_synced_ts per user
email, so each run only processes new threads.

Called by: scripts/run_hubspot_email_sync.py (scheduled hourly via Task Scheduler).
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HUBSPOT_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-hubspot.yaml"
_STATE_PATH = _REPO_ROOT / "data" / "hubspot-email-sync-state.json"
_DEFAULT_LOOKBACK = 7 * 24 * 3600  # 7 days on first run

# Email domains treated as "internal" — threads purely between these are skipped
_INTERNAL_DOMAINS: frozenset[str] = frozenset({
    "hjrglobal.com",
    "f3energy.com",
    "bigd.media",
    "onestopmedia.com",
})


# ── State management ────────────────────────────────────────────────────────────

def _load_state() -> dict[str, Any]:
    if _STATE_PATH.exists():
        try:
            return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict[str, Any]) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ── User map loading ────────────────────────────────────────────────────────────

def _load_users() -> list[dict[str, str]]:
    """Load slack-to-hubspot.yaml. Returns list of user dicts."""
    try:
        data = yaml.safe_load(_HUBSPOT_MAP_PATH.read_text(encoding="utf-8"))
        return data.get("users", []) or []
    except Exception as exc:
        log.error("Failed to load %s: %s", _HUBSPOT_MAP_PATH, exc)
        return []


# ── Email address helpers ───────────────────────────────────────────────────────

def _extract_addresses(raw: str) -> list[str]:
    """Parse RFC 2822 address list → list of lowercase email strings."""
    if not raw:
        return []
    addrs = []
    for name, addr in email.utils.getaddresses([raw]):
        if addr and "@" in addr:
            addrs.append(addr.lower().strip())
    return addrs


def _is_internal(addr: str) -> bool:
    domain = addr.split("@")[-1].lower()
    return domain in _INTERNAL_DOMAINS


def _external_participants(messages: list[dict[str, Any]], owner_email: str) -> list[str]:
    """Return deduplicated external email addresses from all messages in a thread."""
    seen: set[str] = set()
    for msg in messages:
        for raw_field in (msg.get("sender", ""), msg.get("recipients", "")):
            for addr in _extract_addresses(raw_field):
                if addr != owner_email.lower() and not _is_internal(addr):
                    seen.add(addr)
    return list(seen)


# ── DM helpers ─────────────────────────────────────────────────────────────────

def _dm_user(slack_user_id: str, text: str) -> None:
    """Send a Slack DM to slack_user_id via bot token."""
    from slack_sdk import WebClient  # type: ignore[import]
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        log.warning("SLACK_BOT_TOKEN not set — cannot DM %s", slack_user_id)
        return
    client = WebClient(token=bot_token)
    try:
        dm = client.conversations_open(users=[slack_user_id])
        channel = dm["channel"]["id"]
        client.chat_postMessage(channel=channel, text=text)
    except Exception as exc:
        log.warning("DM failed to %s: %s", slack_user_id, exc)


# ── Core sync logic ─────────────────────────────────────────────────────────────

def sync_user(
    user: dict[str, str],
    state: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, int]:
    """Sync Gmail → HubSpot for one user. Returns stats dict."""
    from cora.connectors.gmail_reader import (
        GmailReaderError,
        apply_label,
        ensure_hubspot_label,
        get_full_thread_text,
        list_threads_since,
    )
    from cora.tools.hubspot_client import (
        HubSpotClientError,
        get_contact_deal_ids,
        log_email_engagement,
        search_contact_by_email,
    )

    owner_email: str = user.get("hubspot_email", "")
    owner_id: str    = user.get("hubspot_owner_id", "")
    slack_uid: str   = user.get("slack_user_id", "")
    display_name: str = user.get("display_name", owner_email)

    if not owner_email:
        return {"threads": 0, "logged": 0, "skipped": 0, "dm_sent": 0}

    user_key = owner_email.lower()
    user_state = state.get(user_key, {})
    import time as _time
    since_ts = user_state.get("last_synced_ts", int(_time.time()) - _DEFAULT_LOOKBACK)

    stats = {"threads": 0, "logged": 0, "skipped": 0, "dm_sent": 0}

    # Get Gmail label ID for idempotency
    try:
        label_id = ensure_hubspot_label(owner_email)
    except GmailReaderError as exc:
        log.warning("[%s] Cannot ensure HubSpot label: %s", display_name, exc)
        return stats

    # List new threads
    try:
        # Exclude already-synced threads via Gmail label filter
        thread_ids = list_threads_since(
            owner_email,
            since_ts=since_ts,
            max_results=100,
        )
    except GmailReaderError as exc:
        log.warning("[%s] Gmail threads.list failed: %s", display_name, exc)
        return stats

    stats["threads"] = len(thread_ids)
    log.info("[%s] %d new threads to process (since %d)", display_name, len(thread_ids), since_ts)

    new_last_ts = since_ts

    for thread_id in thread_ids:
        try:
            messages = get_full_thread_text(owner_email, thread_id)
        except GmailReaderError as exc:
            log.debug("[%s] Thread %s fetch failed: %s", display_name, thread_id, exc)
            continue

        if not messages:
            continue

        # Skip if already labeled
        latest_labels = messages[-1].get("label_ids", [])
        if any("Cora-HubSpot" in str(lbl) for lbl in latest_labels):
            stats["skipped"] += 1
            continue

        # Find external participants
        external = _external_participants(messages, owner_email)
        if not external:
            # Purely internal thread — skip
            continue

        # Update watermark to latest message in thread
        for msg in messages:
            if msg.get("date_ts", 0) > new_last_ts:
                new_last_ts = msg["date_ts"]

        # Look up each external participant in HubSpot
        matched_contact_id: str | None = None
        matched_contact_name: str = ""
        deal_ids: list[str] = []
        ambiguous = False

        for ext_email in external:
            contact = search_contact_by_email(ext_email)
            if not contact:
                continue
            cid = str(contact.get("id", ""))
            if not cid:
                continue
            cprops = contact.get("properties") or {}
            cname = f"{cprops.get('firstname','')} {cprops.get('lastname','')}".strip() or ext_email

            if matched_contact_id and matched_contact_id != cid:
                ambiguous = True
                break
            matched_contact_id = cid
            matched_contact_name = cname

        if not matched_contact_id:
            # No known HubSpot contact in this thread
            continue

        if ambiguous:
            # Multiple distinct HubSpot contacts — ask owner which deal to associate
            if slack_uid and not dry_run:
                subject = messages[-1].get("subject", "(no subject)")
                _dm_user(
                    slack_uid,
                    f"Hey {display_name.split()[0]}! I found a Gmail thread with multiple HubSpot "
                    f"contacts I could associate it with:\n*Subject:* {subject}\n"
                    f"Which HubSpot deal or contact should I attach this email thread to? "
                    f"(Reply with the deal name or 'skip' to ignore.)"
                )
                stats["dm_sent"] += 1
            stats["skipped"] += 1
            continue

        # Get associated deals
        deal_ids = get_contact_deal_ids(matched_contact_id)

        if not deal_ids:
            # Contact has no deals — still log to contact, just no deal association
            pass

        # Log each message in the thread as a HubSpot email engagement
        logged_count = 0
        for msg in messages:
            if msg.get("date_ts", 0) <= since_ts and not dry_run:
                # Already within previous sync window
                continue

            sender_addrs = _extract_addresses(msg.get("sender", ""))
            from_email = sender_addrs[0] if sender_addrs else owner_email
            to_addrs = _extract_addresses(msg.get("recipients", ""))

            direction = "OUTBOUND" if from_email.lower() == owner_email.lower() else "INBOUND"

            ts_ms = msg.get("date_ts", 0) * 1000

            if dry_run:
                log.info(
                    "  [DRY] Would log email: %r → %s  subject=%r  contact=%s  deals=%s",
                    from_email, to_addrs, msg.get("subject"), matched_contact_name, deal_ids[:2],
                )
                logged_count += 1
                continue

            try:
                log_email_engagement(
                    from_email=from_email,
                    to_emails=to_addrs,
                    subject=msg.get("subject", ""),
                    body_text=msg.get("body_text", ""),
                    timestamp_ms=ts_ms,
                    direction=direction,
                    owner_id=owner_id,
                    contact_ids=[matched_contact_id],
                    deal_ids=deal_ids[:3],  # cap to 3 deals
                )
                logged_count += 1
            except HubSpotClientError as exc:
                log.warning("[%s] log_email_engagement failed: %s", display_name, exc)

        if logged_count > 0:
            stats["logged"] += 1
            # Apply idempotency label to the first (oldest) message in thread
            if not dry_run:
                try:
                    apply_label(owner_email, messages[0]["message_id"], label_id)
                except GmailReaderError as exc:
                    log.debug("[%s] apply_label failed: %s", display_name, exc)

        time.sleep(0.1)

    # Save watermark
    if new_last_ts > since_ts:
        state[user_key] = {"last_synced_ts": new_last_ts}

    return stats


def run_sync(dry_run: bool = False) -> None:
    """Run full Gmail→HubSpot sync for all users in slack-to-hubspot.yaml."""
    users = _load_users()
    if not users:
        log.warning("No users found in %s", _HUBSPOT_MAP_PATH)
        return

    state = _load_state()
    total_logged = 0
    total_threads = 0

    for user in users:
        display = user.get("display_name", user.get("hubspot_email", "?"))
        log.info("--- Syncing %s ---", display)
        try:
            stats = sync_user(user, state, dry_run=dry_run)
        except Exception as exc:
            log.error("[%s] Unexpected error: %s", display, exc)
            continue

        total_threads += stats["threads"]
        total_logged  += stats["logged"]
        log.info(
            "  threads=%d  logged=%d  skipped=%d  dm_sent=%d",
            stats["threads"], stats["logged"], stats["skipped"], stats["dm_sent"],
        )
        time.sleep(0.5)

    if not dry_run:
        _save_state(state)

    log.info("Sync complete: %d threads scanned, %d logged to HubSpot", total_threads, total_logged)
