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
_SCOPE_PATH   = _REPO_ROOT / "data" / "maps" / "hubspot-email-sync-scope.yaml"
_STATE_PATH   = _REPO_ROOT / "data" / "hubspot-email-sync-state.json"
_PENDING_PATH = _REPO_ROOT / "data" / "hubspot-email-sync-pending.json"
_SKIPPED_PATH = _REPO_ROOT / "data" / "hubspot-email-sync-skipped.json"
_DEFAULT_LOOKBACK = 7 * 24 * 3600  # 7 days on first run
_PORTAL_ID = "246351746"

# Email domains treated as "internal" — threads purely between these are skipped
_INTERNAL_DOMAINS: frozenset[str] = frozenset({
    "hjrglobal.com",
    "f3energy.com",
    "bigd.media",
    "onestopmedia.com",
    "lexingtonservices.com",
    "unitedfightleague.com",
})


def _dm_prompts_enabled() -> bool:
    """Ambiguous-match DM prompts are OFF by default (2026-06-16, audit N6).

    The hourly "confirm attachment / no active deals" DMs were relentless and
    memory-less. No prompt fires unless CORA_HUBSPOT_EMAIL_SYNC_DM_ENABLED=true.
    Full Alex+Tommy + exactly-one-active-deal scoping lands in Phase 1.8.
    """
    return os.environ.get("CORA_HUBSPOT_EMAIL_SYNC_DM_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


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


def _load_scope() -> set[str]:
    """Lowercase set of mailboxes the email sync is allowed to scan.

    Phase 1.8 (N6 / Harrison #8): association is scoped to Alex + Tommy only.
    Missing/empty config => empty set => NO mailbox is scanned (fail-closed off;
    the feature was over-broad, so the safe default is "scan nobody").
    """
    try:
        data = yaml.safe_load(_SCOPE_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("email_sync: could not read scope %s: %s", _SCOPE_PATH, exc)
        return set()
    return {str(e).strip().lower() for e in (data.get("monitored_emails") or []) if str(e).strip()}


# ── Skip ledger (durable "never re-ask a thread") ────────────────────────────
# A thread we deliberately did NOT associate (no active deal) or DM'd once is
# recorded here so it is never re-evaluated. Logged threads are NOT recorded --
# the per-message watermark check keeps logging new messages on an active
# deal's thread correctly.

def _load_skipped() -> dict[str, Any]:
    if _SKIPPED_PATH.exists():
        try:
            return json.loads(_SKIPPED_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _is_thread_skipped(thread_id: str, skipped: dict[str, Any]) -> bool:
    return thread_id in skipped


def _mark_thread_skipped(thread_id: str, reason: str, skipped: dict[str, Any]) -> None:
    skipped[thread_id] = {"reason": reason, "ts": int(time.time())}


def _save_skipped(skipped: dict[str, Any]) -> None:
    _SKIPPED_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SKIPPED_PATH.write_text(json.dumps(skipped, indent=2), encoding="utf-8")


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
    _dm_user_with_ts(slack_user_id, text)


def _dm_user_with_ts(slack_user_id: str, text: str) -> str | None:
    """Send a Slack DM and return the message_ts (needed to track reactions)."""
    from slack_sdk import WebClient  # type: ignore[import]
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        log.warning("SLACK_BOT_TOKEN not set — cannot DM %s", slack_user_id)
        return None
    client = WebClient(token=bot_token)
    try:
        dm = client.conversations_open(users=[slack_user_id])
        channel = dm["channel"]["id"]
        resp = client.chat_postMessage(channel=channel, text=text,
                                       unfurl_links=False, unfurl_media=False)
        return resp.get("ts")
    except Exception as exc:
        log.warning("DM failed to %s: %s", slack_user_id, exc)
        return None


# ── Pending reaction state ────────────────────────────────────────────────────
# When an ambiguous match is found, we DM each candidate deal as a separate
# message and store what to do if Harrison reacts 👍 to it.
# Key: Slack message_ts of the Cora DM. Value: everything needed to log.

def _load_pending() -> dict:
    if _PENDING_PATH.exists():
        try:
            return json.loads(_PENDING_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_pending(pending: dict) -> None:
    _PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PENDING_PATH.write_text(json.dumps(pending, indent=2), encoding="utf-8")


def _store_pending_reaction(
    message_ts: str,
    thread_id: str,
    owner_email: str,
    owner_id: str,
    contact_id: str,
    contact_name: str,
    deal_ids: list,
    messages: list,
) -> None:
    pending = _load_pending()
    # Compact message storage — keep only what log_email_engagement needs
    compact_msgs = [
        {
            "sender": m.get("sender", ""),
            "recipients": m.get("recipients", ""),
            "subject": m.get("subject", ""),
            "body_text": (m.get("body_text") or "")[:800],
            "date_ts": m.get("date_ts", 0),
        }
        for m in messages
    ]
    pending[message_ts] = {
        "thread_id": thread_id,
        "owner_email": owner_email,
        "owner_id": owner_id,
        "contact_id": contact_id,
        "contact_name": contact_name,
        "deal_ids": deal_ids,
        "messages": compact_msgs,
    }
    _save_pending(pending)


def get_pending_reaction(message_ts: str) -> dict | None:
    """Return the stored pending-reaction entry for this Slack message_ts, or None."""
    return _load_pending().get(message_ts)


def resolve_pending_reaction(message_ts: str, approved: bool) -> bool:
    """Execute or discard a pending email-sync reaction.

    If approved=True: calls log_email_engagement for all stored messages.
    Either way: removes the entry from the pending file.
    Returns True if the entry was found and processed.
    """
    from cora.tools.hubspot_client import HubSpotClientError, log_email_engagement

    pending = _load_pending()
    entry = pending.pop(message_ts, None)
    if not entry:
        return False

    _save_pending(pending)

    if not approved:
        log.info("email_sync: pending reaction dismissed for thread=%s", entry.get("thread_id"))
        return True

    owner_email = entry["owner_email"]
    owner_id    = entry["owner_id"]
    contact_id  = entry["contact_id"]
    deal_ids    = entry["deal_ids"]
    messages    = entry["messages"]

    logged = 0
    for msg in messages:
        sender_addrs = _extract_addresses(msg.get("sender", ""))
        from_email   = sender_addrs[0] if sender_addrs else owner_email
        to_addrs     = _extract_addresses(msg.get("recipients", ""))
        direction    = "OUTBOUND" if from_email.lower() == owner_email.lower() else "INBOUND"
        ts_ms        = msg.get("date_ts", 0) * 1000

        try:
            log_email_engagement(
                from_email=from_email,
                to_emails=to_addrs,
                subject=msg.get("subject", ""),
                body_text=msg.get("body_text", ""),
                timestamp_ms=ts_ms,
                direction=direction,
                owner_id=owner_id,
                contact_ids=[contact_id],
                deal_ids=deal_ids[:3],
            )
            logged += 1
        except HubSpotClientError as exc:
            log.warning("email_sync: reaction log failed: %s", exc)

    log.info(
        "email_sync: reaction approved — logged %d message(s) thread=%s contact=%s deals=%s",
        logged, entry.get("thread_id"), contact_id, deal_ids,
    )
    return True


# ── Core sync logic ─────────────────────────────────────────────────────────────

def sync_user(
    user: dict[str, str],
    state: dict[str, Any],
    dry_run: bool = False,
    skipped: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Sync Gmail → HubSpot for one user. Returns stats dict."""
    from cora.connectors.gmail_reader import (
        GmailReaderError,
        get_full_thread_text,
        list_threads_since,
    )
    from cora.tools.hubspot_client import (
        HubSpotClientError,
        get_open_deal_ids_for_contact,
        log_email_engagement,
        search_contact_by_email,
    )

    if skipped is None:
        skipped = {}

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

    # List new threads (watermark-based dedup — no visible Gmail labels)
    try:
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

        # Advance the watermark for EVERY thread we see (even skipped ones) so a
        # skipped thread does not re-list every run.
        for msg in messages:
            if msg.get("date_ts", 0) > new_last_ts:
                new_last_ts = msg["date_ts"]

        # Never re-evaluate a thread we already decided not to associate (N6:
        # the "Needed Items- Rogers" thread was re-prompted ~10x). Logged
        # threads are NOT recorded -- the per-message check below keeps logging
        # new messages on an active deal's thread.
        if _is_thread_skipped(thread_id, skipped):
            continue

        # Find external participants
        external = _external_participants(messages, owner_email)
        if not external:
            continue  # purely internal thread

        # Look up each external participant in HubSpot — collect ALL matches
        matched_contacts: list[tuple[str, str]] = []  # [(contact_id, contact_name), ...]
        for ext_email in external:
            contact = search_contact_by_email(ext_email)
            if not contact:
                continue
            cid = str(contact.get("id", ""))
            if not cid:
                continue
            cprops = contact.get("properties") or {}
            cname = (
                f"{cprops.get('firstname','')} {cprops.get('lastname','')}".strip()
                or ext_email
            )
            if not any(c[0] == cid for c in matched_contacts):
                matched_contacts.append((cid, cname))

        if not matched_contacts:
            continue  # no known HubSpot contacts in this thread

        # Phase 1.8 (N6 / Harrison #8): associate ONLY with a contact on an
        # ACTIVE deal. A transient HubSpot failure must NOT look like "no active
        # deal", so on error we retry next run WITHOUT recording a skip.
        try:
            active: list[tuple[str, str, list[str]]] = []  # (cid, name, open_deal_ids)
            for cid, cname in matched_contacts:
                open_ids = get_open_deal_ids_for_contact(cid)
                if open_ids:
                    active.append((cid, cname, open_ids))
        except HubSpotClientError as exc:
            log.warning("[%s] open-deal lookup failed (transient) thread=%s: %s",
                        display_name, thread_id, exc)
            continue

        if not active:
            # No matched contact is on an active deal -> never prompt, never log
            # "to contact only"; remember so the thread is never re-asked.
            _mark_thread_skipped(thread_id, "no_active_deal", skipped)
            stats["skipped"] += 1
            continue

        if len(active) > 1:
            # Genuine ambiguity among ACTIVE deals -> DM only if explicitly
            # enabled (default OFF, audit N6); record either way so it is asked
            # at most once, never re-prompted.
            if slack_uid and not dry_run and _dm_prompts_enabled():
                subject = messages[-1].get("subject", "") or "(no subject)"
                for cid, cname, open_ids in active[:3]:  # cap at 3 options
                    deal_url = (f"https://app.hubspot.com/contacts/"
                                f"{_PORTAL_ID}/deal/{open_ids[0]}")
                    dm_text = (
                        f":email: *Ambiguous email match — confirm attachment*\n\n"
                        f"*Subject:* {subject}\n"
                        f"*Contact:* {cname}\n"
                        f"*Deal:* <{deal_url}|Open in HubSpot>\n\n"
                        f"👍 attach this thread  ·  👎 skip"
                    )
                    msg_ts = _dm_user_with_ts(slack_uid, dm_text)
                    if msg_ts:
                        _store_pending_reaction(
                            message_ts=msg_ts,
                            thread_id=thread_id,
                            owner_email=owner_email,
                            owner_id=owner_id,
                            contact_id=cid,
                            contact_name=cname,
                            deal_ids=open_ids,
                            messages=messages,
                        )
                        stats["dm_sent"] += 1
            _mark_thread_skipped(thread_id, "ambiguous_active", skipped)
            stats["skipped"] += 1
            continue

        # Exactly one contact on an active deal -> log the thread to that deal.
        matched_contact_id, matched_contact_name, deal_ids = active[0]

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
            # No Gmail label applied — dedup handled invisibly via watermark

        time.sleep(0.1)

    # Save watermark
    if new_last_ts > since_ts:
        state[user_key] = {"last_synced_ts": new_last_ts}

    return stats


def run_sync(dry_run: bool = False) -> None:
    """Run the Gmail→HubSpot sync for the scoped users only.

    Phase 1.8 (N6 / Harrison #8): scoped to the mailboxes in
    hubspot-email-sync-scope.yaml (Alex + Tommy). Other mailboxes are not
    scanned at all; an empty/missing scope file scans nobody (fail-closed off).
    """
    users = _load_users()
    if not users:
        log.warning("No users found in %s", _HUBSPOT_MAP_PATH)
        return

    scope = _load_scope()
    if not scope:
        log.warning("email_sync: scope is empty (%s) — scanning nobody", _SCOPE_PATH)
        return
    scoped_users = [u for u in users if str(u.get("hubspot_email", "")).strip().lower() in scope]
    skipped_count = len(users) - len(scoped_users)
    log.info("email_sync: %d/%d users in scope (%d out of scope)",
             len(scoped_users), len(users), skipped_count)
    if not scoped_users:
        log.warning("email_sync: no scoped users matched the map — nothing to do")
        return

    state = _load_state()
    skipped = _load_skipped()
    total_logged = 0
    total_threads = 0

    for user in scoped_users:
        display = user.get("display_name", user.get("hubspot_email", "?"))
        log.info("--- Syncing %s ---", display)
        try:
            stats = sync_user(user, state, dry_run=dry_run, skipped=skipped)
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
        _save_skipped(skipped)

    log.info("Sync complete: %d threads scanned, %d logged to HubSpot", total_threads, total_logged)
