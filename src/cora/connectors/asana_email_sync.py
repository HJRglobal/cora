"""Gmail → Asana email sync connector.

For each user in slack-to-asana.yaml, scans their Gmail inbox for threads
involving people referenced in open Asana tasks (by name or email mention
in the task name or notes). Matching threads are attached as comments on
the relevant Asana task.

Matching logic:
  1. Collect external participant names + emails from each Gmail thread
  2. Search Asana workspace for open tasks whose name or notes contain
     any of those names or the company/domain of the email address
  3. If one task matches clearly → add a comment with thread summary
  4. If multiple tasks match → add comment to the most recently modified one
     and note the ambiguity
  5. Dedup: data/asana-email-sync-state.json tracks (thread_id, task_gid)
     pairs already commented so the same thread is never double-posted

"External" = not in _INTERNAL_DOMAINS; internal-only threads are skipped.

Called by: scripts/run_asana_email_sync.py (scheduled hourly via Task Scheduler).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ASANA_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
_STATE_PATH = _REPO_ROOT / "data" / "asana-email-sync-state.json"
_DEFAULT_LOOKBACK = 7 * 24 * 3600  # 7 days on first run

# Same internal domains as hubspot_email_sync — kept in sync
_INTERNAL_DOMAINS: frozenset[str] = frozenset({
    "hjrglobal.com",
    "f3energy.com",
    "bigd.media",
    "onestopmedia.com",
    "lexingtonservices.com",
    "unitedfightleague.com",
})


# ── State management ────────────────────────────────────────────────────────

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


def _already_commented(state: dict, thread_id: str, task_gid: str) -> bool:
    """Return True if this (thread, task) pair was already commented."""
    return task_gid in state.get("commented", {}).get(thread_id, [])


def _mark_commented(state: dict, thread_id: str, task_gid: str) -> None:
    state.setdefault("commented", {}).setdefault(thread_id, [])
    if task_gid not in state["commented"][thread_id]:
        state["commented"][thread_id].append(task_gid)


# ── User map loading ────────────────────────────────────────────────────────

def _load_users() -> list[dict[str, str]]:
    """Load slack-to-asana.yaml. Returns users with email + asana_user_gid."""
    try:
        data = yaml.safe_load(_ASANA_MAP_PATH.read_text(encoding="utf-8"))
        users = data.get("users", []) or []
        # Only return users with both an email and a GID
        return [
            u for u in users
            if (u.get("asana_email") or u.get("email")) and u.get("asana_user_gid")
        ]
    except Exception as exc:
        log.error("Failed to load %s: %s", _ASANA_MAP_PATH, exc)
        return []


# ── Email helpers ───────────────────────────────────────────────────────────

import email.utils as _eu


def _extract_addresses(raw: str) -> list[str]:
    if not raw:
        return []
    return [
        addr.lower().strip()
        for _, addr in _eu.getaddresses([raw])
        if addr and "@" in addr
    ]


def _is_internal(addr: str) -> bool:
    return addr.split("@")[-1].lower() in _INTERNAL_DOMAINS


def _name_from_email(addr: str) -> str:
    """Best-effort display name from an email address."""
    local = addr.split("@")[0]
    return local.replace(".", " ").replace("_", " ").replace("-", " ").title()


def _domain_company(addr: str) -> str:
    """Return company-like name from email domain (strip TLD)."""
    domain = addr.split("@")[-1].lower()
    parts = domain.split(".")
    return parts[-2] if len(parts) >= 2 else domain


# ── Asana search + comment ──────────────────────────────────────────────────

def _search_asana_tasks(keywords: list[str], user_gid: str) -> list[dict]:
    """Search Asana for open tasks matching any of the keywords.

    Uses Asana's typeahead search (text search on task name) per keyword,
    then deduplicates results. Capped to 5 results per keyword, 20 total.
    """
    import httpx
    from cora.tools.asana_client import AsanaClientError, _BASE, _WORKSPACE_GID, _pat

    headers = {"Authorization": f"Bearer {_pat()}"}
    found: dict[str, dict] = {}  # gid → task

    for kw in keywords[:5]:  # cap to 5 keywords per thread
        if len(kw) < 4:
            continue
        try:
            with httpx.Client(timeout=10) as c:
                r = c.get(
                    f"{_BASE}/workspaces/{_WORKSPACE_GID}/typeahead",
                    params={
                        "resource_type": "task",
                        "query": kw,
                        "count": 5,
                        "opt_fields": "name,gid,permalink_url,assignee.name,completed,modified_at",
                    },
                    headers=headers,
                )
            if r.status_code != 200:
                continue
            for task in r.json().get("data", []) or []:
                gid = task.get("gid", "")
                if gid and not task.get("completed"):
                    found[gid] = task
        except Exception as exc:
            log.debug("Asana typeahead failed for %r: %s", kw, exc)

        time.sleep(0.15)

    # Sort by modified_at descending (most recently touched first)
    tasks = list(found.values())
    tasks.sort(key=lambda t: t.get("modified_at", ""), reverse=True)
    return tasks[:20]


def _add_task_comment(task_gid: str, comment: str) -> bool:
    """Add a comment (story) to an Asana task. Returns True on success."""
    import httpx
    from cora.tools.asana_client import _BASE, _pat

    headers = {
        "Authorization": f"Bearer {_pat()}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(
                f"{_BASE}/tasks/{task_gid}/stories",
                json={"data": {"text": comment}},
                headers=headers,
            )
        if r.status_code in (200, 201):
            log.info("Asana: commented on task %s", task_gid)
            return True
        log.warning("Asana comment failed task=%s status=%d", task_gid, r.status_code)
        return False
    except Exception as exc:
        log.warning("Asana comment error task=%s: %s", task_gid, exc)
        return False


def _build_comment(messages: list[dict], matched_name: str) -> str:
    """Build the Asana comment text from a Gmail thread."""
    first = messages[0] if messages else {}
    last = messages[-1] if messages else {}

    subject = last.get("subject") or first.get("subject") or "(no subject)"
    date_str = last.get("date_str", "")
    participants = ", ".join(
        {m.get("sender", "").split("<")[-1].rstrip(">").strip()
         for m in messages if m.get("sender")}
    )[:200]
    msg_count = len(messages)

    # Short excerpt from the latest message body
    latest_body = (last.get("body_text") or "").strip()
    excerpt = latest_body[:300].replace("\n", " ").strip()
    if len(latest_body) > 300:
        excerpt += "…"

    lines = [
        f"📧 Email thread synced by Cora",
        f"Subject: {subject}",
        f"Participants: {participants}",
        f"Messages: {msg_count}  |  Latest: {date_str}",
    ]
    if excerpt:
        lines.append(f"\nLatest message excerpt:\n{excerpt}")

    return "\n".join(lines)


# ── Core sync logic ─────────────────────────────────────────────────────────

def sync_user(
    user: dict[str, str],
    state: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, int]:
    """Sync Gmail → Asana for one user. Returns stats dict."""
    from cora.connectors.gmail_reader import (
        GmailReaderError,
        get_full_thread_text,
        list_threads_since,
    )

    owner_email: str = user.get("asana_email") or user.get("email", "")
    asana_gid: str = user.get("asana_user_gid", "")
    display_name: str = user.get("display_name", owner_email)

    if not owner_email or not asana_gid:
        return {"threads": 0, "commented": 0, "skipped": 0}

    user_key = owner_email.lower()
    user_state = state.get(user_key, {})
    since_ts = user_state.get("last_synced_ts",
                              int(time.time()) - _DEFAULT_LOOKBACK)

    stats = {"threads": 0, "commented": 0, "skipped": 0}

    try:
        thread_ids = list_threads_since(
            owner_email, since_ts=since_ts, max_results=50
        )
    except GmailReaderError as exc:
        log.warning("[%s] Gmail threads.list failed: %s", display_name, exc)
        return stats

    stats["threads"] = len(thread_ids)
    log.info("[%s] %d threads to check (since %d)", display_name,
             len(thread_ids), since_ts)

    new_last_ts = since_ts

    for thread_id in thread_ids:
        try:
            messages = get_full_thread_text(owner_email, thread_id)
        except GmailReaderError:
            continue

        if not messages:
            continue

        # Update watermark
        for msg in messages:
            if msg.get("date_ts", 0) > new_last_ts:
                new_last_ts = msg["date_ts"]

        # Collect external participants
        external_emails: set[str] = set()
        for msg in messages:
            for raw in (msg.get("sender", ""), msg.get("recipients", "")):
                for addr in _extract_addresses(raw):
                    if addr != owner_email.lower() and not _is_internal(addr):
                        external_emails.add(addr)

        if not external_emails:
            continue  # purely internal thread

        # Build keyword list from names + company domains
        keywords: list[str] = []
        for addr in external_emails:
            keywords.append(_name_from_email(addr))
            keywords.append(_domain_company(addr))

        # Deduplicate and filter short/common keywords
        _SKIP_WORDS = {"gmail", "yahoo", "hotmail", "outlook", "info", "hello",
                       "support", "contact", "admin", "mail", "sales", "noreply"}
        keywords = list({
            kw for kw in keywords
            if len(kw) >= 4 and kw.lower() not in _SKIP_WORDS
        })

        if not keywords:
            stats["skipped"] += 1
            continue

        # Search Asana for matching tasks
        matched_tasks = _search_asana_tasks(keywords, asana_gid)
        if not matched_tasks:
            stats["skipped"] += 1
            continue

        # Comment on the top match (most recently modified)
        top_task = matched_tasks[0]
        task_gid = top_task.get("gid", "")
        task_name = top_task.get("name", "(no name)")
        task_url = top_task.get("permalink_url", "")

        if _already_commented(state, thread_id, task_gid):
            stats["skipped"] += 1
            continue

        comment = _build_comment(messages, matched_name=task_name)

        if len(matched_tasks) > 1:
            other_names = ", ".join(
                t.get("name", "?") for t in matched_tasks[1:3]
            )
            comment += (
                f"\n\n(Also matched: {other_names} — "
                f"commented on most recent task only)"
            )

        if dry_run:
            log.info(
                "  [DRY] Would comment on task %r (%s): %s",
                task_name, task_gid, comment[:80],
            )
            stats["commented"] += 1
            continue

        success = _add_task_comment(task_gid, comment)
        if success:
            _mark_commented(state, thread_id, task_gid)
            stats["commented"] += 1
            log.info(
                "[%s] Commented on task %r (%s) for thread %s",
                display_name, task_name, task_gid, thread_id,
            )

        time.sleep(0.2)

    # Save watermark for this user
    if new_last_ts > since_ts:
        state[user_key] = {"last_synced_ts": new_last_ts}

    return stats


def run_sync(dry_run: bool = False) -> None:
    """Run full Gmail→Asana sync for all users in slack-to-asana.yaml."""
    users = _load_users()
    if not users:
        log.warning("No users with email+asana_gid found in %s", _ASANA_MAP_PATH)
        return

    state = _load_state()
    total_commented = 0
    total_threads = 0

    for user in users:
        display = user.get("display_name", user.get("email", "?"))
        log.info("--- Syncing %s ---", display)
        try:
            stats = sync_user(user, state, dry_run=dry_run)
        except Exception as exc:
            log.error("[%s] Unexpected error: %s", display, exc, exc_info=True)
            continue

        total_threads += stats["threads"]
        total_commented += stats["commented"]
        log.info(
            "  threads=%d  commented=%d  skipped=%d",
            stats["threads"], stats["commented"], stats["skipped"],
        )
        time.sleep(0.5)

    if not dry_run:
        _save_state(state)

    log.info(
        "Sync complete: %d threads scanned, %d Asana comments added",
        total_threads, total_commented,
    )
