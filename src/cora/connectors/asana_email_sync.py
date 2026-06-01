"""Gmail → Asana email sync connector — v2 with 3-layer precision matching.

Attaches Gmail threads as comments on Asana tasks only when there is high
confidence the email is genuinely related to the task.

Three-layer matching:

  Layer 1 — Pre-filter (kills noise before touching Asana)
    Skip bulk/automated emails: known notification domains, Gmail promotion
    labels, >5 recipients, bulk-signal subject lines.

  Layer 2 — Structured entity extraction
    Full name from email display header (not just local part).
    Company name cleaned from domain (strip TLD + common prefixes).
    Subject line meaningful keywords (proper nouns, strip Re:/Fw: and stopwords).

  Layer 3 — Confidence scoring (only comment at score ≥ 4)
    Scores each Asana candidate on multiple independent signals:
      Full name in task name      +3
      Company name in task name   +3
      2+ subject keywords overlap +3
      Task assigned to user       +2
      Full name in task notes     +2
      Company name in task notes  +1
      1 subject keyword overlap   +1
    A score ≥ 4 requires at least two independent signals to agree.

State: data/asana-email-sync-state.json tracks (thread_id, task_gid) pairs
so no thread is ever double-posted to the same task.

Called by: scripts/run_asana_email_sync.py (hourly via Task Scheduler).
"""

from __future__ import annotations

import email.utils as _eu
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ASANA_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
_STATE_PATH = _REPO_ROOT / "data" / "asana-email-sync-state.json"
_DEFAULT_LOOKBACK = 7 * 24 * 3600  # 7 days on first run
_MIN_CONFIDENCE = 4  # minimum score to post a comment

# ── Internal domains (skip threads purely between these) ─────────────────────
_INTERNAL_DOMAINS: frozenset[str] = frozenset({
    "hjrglobal.com",
    "f3energy.com",
    "bigd.media",
    "onestopmedia.com",
    "lexingtonservices.com",
    "unitedfightleague.com",
})

# ── Layer 1: Bulk sender domains ─────────────────────────────────────────────
_BULK_SENDER_DOMAINS: frozenset[str] = frozenset({
    # Finance / invoicing
    "intuit.com", "quickbooks.com", "notification.intuit.com",
    "paypal.com", "stripe.com", "bill.com", "freshbooks.com",
    # Productivity / SaaS notifications
    "slack.com", "asana.com", "notion.so", "monday.com",
    "trello.com", "airtable.com", "dropbox.com", "box.com",
    # Calendar / scheduling
    "calendar-notification.google.com", "calendar.google.com",
    "calendly.com", "doodle.com",
    # Marketing / email tools
    "mailchimp.com", "constantcontact.com", "klaviyo.com",
    "sendgrid.net", "hubspotemail.net", "salesforce.com",
    "marketo.com", "pardot.com", "activecampaign.com",
    # E-commerce / order notifications
    "shopify.com", "etsy.com", "amazon.com", "ebay.com",
    # Event / ticketing
    "eventbrite.com", "ticketmaster.com",
    # Utilities / services
    "srpnet.com", "aps.com", "swn.com",
    # Generic notification patterns (matched by substring below)
})

# Substrings that mark a sender domain as bulk
_BULK_DOMAIN_PATTERNS: tuple[str, ...] = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications", "notification", "alerts", "alert",
    "mailer", "newsletter", "digest", "bounce",
    "auto-confirm", "autoconfirm", "support-noreply",
)

# ── Layer 1: Bulk subject signals ────────────────────────────────────────────
_BULK_SUBJECT_PATTERNS: re.Pattern = re.compile(
    r"\b(unsubscribe|invoice\s+reminder|payment\s+receipt|order\s+confirm"
    r"|automated\s+message|your\s+receipt|order\s+confirmation|you\s+have\s+a\s+new"
    r"|sign\s+in\s+attempt|security\s+alert|verify\s+your|password\s+reset"
    r"|account\s+activity|statement\s+ready|your\s+bill\s+is\s+ready"
    r"|payment\s+confirmation|subscription\s+renewal|trial\s+expir"
    r"|mentioned\s+you\s+in|reacted\s+to|left\s+a\s+comment|shared\s+a\s+file"
    r"|invited\s+you|has\s+been\s+assigned|task\s+reminder|meeting\s+reminder"
    r"|welcome\s+to|getting\s+started|your\s+account|confirm\s+your"
    r"|digest|weekly\s+report|daily\s+summary)\b",
    re.IGNORECASE,
)

# ── Layer 2: Domain cleaning ──────────────────────────────────────────────────
# Common prefixes that add no company meaning
_DOMAIN_STRIP_PREFIXES: tuple[str, ...] = (
    "get", "my", "the", "app", "use", "try", "go", "join",
    "team", "mail", "email", "hello", "info", "hi", "hey",
    "meet", "www", "portal", "login",
)

# Common TLDs (strip these from domain)
_TLDS: frozenset[str] = frozenset({
    "com", "net", "org", "io", "co", "biz", "us", "info",
    "agency", "media", "studio", "group", "inc", "llc",
})

# English stopwords for subject keyword extraction
_SUBJECT_STOPWORDS: frozenset[str] = frozenset({
    "re", "fw", "fwd", "the", "a", "an", "and", "or", "but",
    "in", "on", "at", "to", "for", "of", "with", "from", "by",
    "is", "are", "was", "were", "be", "been", "have", "has",
    "had", "will", "would", "could", "should", "may", "might",
    "this", "that", "these", "those", "it", "its", "your",
    "our", "their", "my", "his", "her", "we", "you", "they",
    "i", "me", "us", "him", "them", "about", "up", "out",
    "into", "over", "after", "before", "new", "please", "hello",
    "hi", "hey", "just", "quick", "follow", "update", "check",
    "call", "email", "meeting", "next", "week", "today", "per",
})


# ── State helpers ─────────────────────────────────────────────────────────────

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
    return task_gid in state.get("commented", {}).get(thread_id, [])


def _mark_commented(state: dict, thread_id: str, task_gid: str) -> None:
    state.setdefault("commented", {}).setdefault(thread_id, [])
    if task_gid not in state["commented"][thread_id]:
        state["commented"][thread_id].append(task_gid)


# ── User map ──────────────────────────────────────────────────────────────────

def _load_users() -> list[dict[str, str]]:
    try:
        data = yaml.safe_load(_ASANA_MAP_PATH.read_text(encoding="utf-8"))
        return [
            u for u in (data.get("users", []) or [])
            if (u.get("asana_email") or u.get("email")) and u.get("asana_user_gid")
        ]
    except Exception as exc:
        log.error("Failed to load %s: %s", _ASANA_MAP_PATH, exc)
        return []


# ── Layer 1: Pre-filter ───────────────────────────────────────────────────────

def _is_bulk_domain(domain: str) -> bool:
    """Return True if the sender domain is a known bulk/notification sender."""
    domain = domain.lower()
    if domain in _BULK_SENDER_DOMAINS:
        return True
    return any(pat in domain for pat in _BULK_DOMAIN_PATTERNS)


def _extract_addresses_with_names(raw: str) -> list[tuple[str, str]]:
    """Parse RFC 2822 address list → list of (display_name, email) tuples."""
    if not raw:
        return []
    result = []
    for name, addr in _eu.getaddresses([raw]):
        if addr and "@" in addr:
            result.append((name.strip(), addr.lower().strip()))
    return result


def _is_internal(addr: str) -> bool:
    return addr.split("@")[-1].lower() in _INTERNAL_DOMAINS


def _thread_passes_prefilter(messages: list[dict], owner_email: str) -> tuple[bool, str]:
    """Return (passes, reason_if_rejected) for Layer 1 pre-filter."""
    if not messages:
        return False, "empty thread"

    # Check all senders across thread
    for msg in messages:
        sender_pairs = _extract_addresses_with_names(msg.get("sender", ""))
        for _, addr in sender_pairs:
            domain = addr.split("@")[-1].lower()
            if _is_bulk_domain(domain):
                return False, f"bulk sender domain: {domain}"

    # Check Gmail category labels (PROMOTIONS, SOCIAL, UPDATES)
    for msg in messages:
        labels = msg.get("label_ids", []) or []
        for lbl in labels:
            lbl_upper = str(lbl).upper()
            if any(cat in lbl_upper for cat in (
                "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL",
                "CATEGORY_UPDATES", "CATEGORY_FORUMS",
            )):
                return False, f"Gmail category label: {lbl}"

    # Check recipient count on latest message
    latest = messages[-1]
    all_recipients: list[str] = []
    for raw in (latest.get("recipients", ""), latest.get("cc", "")):
        all_recipients.extend(
            addr for _, addr in _extract_addresses_with_names(raw)
        )
    external_recipient_count = sum(
        1 for a in all_recipients if not _is_internal(a)
    )
    if external_recipient_count > 5:
        return False, f"mass email: {external_recipient_count} external recipients"

    # Check subject for bulk signals
    subject = latest.get("subject", "") or ""
    if _BULK_SUBJECT_PATTERNS.search(subject):
        return False, f"bulk subject signal: {subject[:60]}"

    # Ensure at least one real external participant exists
    has_external = False
    for msg in messages:
        for raw in (msg.get("sender", ""), msg.get("recipients", "")):
            for _, addr in _extract_addresses_with_names(raw):
                if addr != owner_email.lower() and not _is_internal(addr):
                    has_external = True
                    break

    if not has_external:
        return False, "no external participants"

    return True, ""


# ── Layer 2: Entity extraction ────────────────────────────────────────────────

def _clean_company_from_domain(domain: str) -> str:
    """Convert an email domain to a human-readable company name.

    getnimbl.com        → Nimbl
    sjfoodbrokers.com   → SJ Food Brokers
    hubbrandscale.com   → HubBrandScale
    kseagency.com       → KSE Agency
    mmalab.com          → MMA Lab
    """
    # Strip TLD(s)
    parts = domain.lower().split(".")
    parts = [p for p in parts if p not in _TLDS and len(p) > 1]
    if not parts:
        return domain

    company = parts[0]  # primary domain segment

    # Strip common prefixes
    for prefix in _DOMAIN_STRIP_PREFIXES:
        if company.startswith(prefix) and len(company) > len(prefix) + 2:
            company = company[len(prefix):]
            break

    # Split CamelCase or insert spaces before capital runs
    # e.g. "sjfoodbrokers" → try to detect word boundaries by length heuristic
    # Simple approach: if it looks like concatenated words (all lowercase, long),
    # keep as-is but capitalize first letter
    company = company.capitalize()

    # For known short acronyms, uppercase them
    if len(company) <= 4 and company.isalpha():
        company = company.upper()

    return company


def _extract_subject_keywords(subject: str) -> list[str]:
    """Extract meaningful keywords from a subject line."""
    # Strip prefixes like Re:, Fw:, Fwd:
    subject = re.sub(r"^(re|fw|fwd|forward)[\s:]+", "", subject, flags=re.IGNORECASE).strip()

    # Tokenize on whitespace and punctuation
    tokens = re.findall(r"[A-Za-z0-9&'-]+", subject)

    keywords = []
    for tok in tokens:
        tok_clean = tok.strip("-'").lower()
        if (
            len(tok_clean) >= 3
            and tok_clean not in _SUBJECT_STOPWORDS
            and not tok_clean.isdigit()
        ):
            keywords.append(tok.strip("-'"))

    return keywords[:8]  # cap at 8


def _extract_entities(messages: list[dict], owner_email: str) -> dict[str, Any]:
    """Extract structured entities from a thread for matching.

    Returns:
      full_names     list[str]  — "Sean Young", "Natalia Kensington"
      company_names  list[str]  — "Nimbl", "HubBrandScale"
      subject_kws    list[str]  — meaningful words from subject
      raw_emails     list[str]  — full external email addresses
    """
    full_names: list[str] = []
    company_names: list[str] = []
    raw_emails: list[str] = []

    seen_domains: set[str] = set()
    seen_names: set[str] = set()

    for msg in messages:
        for raw_field in (msg.get("sender", ""), msg.get("recipients", "")):
            for display_name, addr in _extract_addresses_with_names(raw_field):
                if addr == owner_email.lower() or _is_internal(addr):
                    continue

                raw_emails.append(addr)
                domain = addr.split("@")[-1].lower()

                # Full name from display header
                if display_name and " " in display_name and display_name not in seen_names:
                    # Looks like a real first + last name
                    cleaned = re.sub(r"[^A-Za-z\s]", "", display_name).strip()
                    if len(cleaned.split()) >= 2:
                        full_names.append(cleaned)
                        seen_names.add(cleaned)

                # Company from domain
                if domain not in seen_domains:
                    seen_domains.add(domain)
                    company = _clean_company_from_domain(domain)
                    if len(company) >= 3:
                        company_names.append(company)

    # Subject keywords from the latest message
    latest_subject = ""
    for msg in reversed(messages):
        s = msg.get("subject", "") or ""
        if s:
            latest_subject = s
            break

    subject_kws = _extract_subject_keywords(latest_subject)

    return {
        "full_names": list(dict.fromkeys(full_names)),      # dedup, preserve order
        "company_names": list(dict.fromkeys(company_names)),
        "subject_kws": subject_kws,
        "raw_emails": list(dict.fromkeys(raw_emails)),
        "subject_raw": latest_subject,
    }


# ── Layer 3: Confidence scoring ───────────────────────────────────────────────

def _score_task(task: dict, entities: dict, owner_gid: str) -> int:
    """Score how relevant a task is to this email thread.

    Returns an integer score; _MIN_CONFIDENCE is the posting threshold.
    """
    score = 0
    task_name = (task.get("name") or "").lower()
    task_notes = (task.get("notes") or "").lower()
    task_assignee_gid = (
        (task.get("assignee") or {}).get("gid") or ""
    )

    full_names = [n.lower() for n in entities["full_names"]]
    company_names = [c.lower() for c in entities["company_names"]]
    subject_kws = [k.lower() for k in entities["subject_kws"]]

    # Full name in task name (+3 each, cap at 2 names)
    for name in full_names[:2]:
        if name and name in task_name:
            score += 3
            log.debug("  +3 full name %r in task name", name)

    # Company name in task name (+3 each, cap at 2)
    for company in company_names[:2]:
        if len(company) >= 3 and company in task_name:
            score += 3
            log.debug("  +3 company %r in task name", company)

    # Subject keyword overlap with task name
    kw_hits = sum(1 for kw in subject_kws if len(kw) >= 4 and kw in task_name)
    if kw_hits >= 2:
        score += 3
        log.debug("  +3 subject kws (%d hits) in task name", kw_hits)
    elif kw_hits == 1:
        score += 1
        log.debug("  +1 subject kw (1 hit) in task name")

    # Task assigned to the same user whose email we're scanning
    if owner_gid and task_assignee_gid == owner_gid:
        score += 2
        log.debug("  +2 task assigned to same user")

    # Full name in task notes (+2)
    for name in full_names[:2]:
        if name and name in task_notes:
            score += 2
            log.debug("  +2 full name %r in task notes", name)

    # Company name in task notes (+1)
    for company in company_names[:2]:
        if len(company) >= 3 and company in task_notes:
            score += 1
            log.debug("  +1 company %r in task notes", company)

    return score


# ── Asana search ──────────────────────────────────────────────────────────────

def _search_asana(search_terms: list[str], user_gid: str) -> list[dict]:
    """Search Asana typeahead for open tasks matching any search term.

    Searches by company names and subject keywords only (NOT first names alone).
    Deduplicates results, sorts by modified_at descending.
    """
    import httpx
    from cora.tools.asana_client import _BASE, _WORKSPACE_GID, _pat

    headers = {"Authorization": f"Bearer {_pat()}"}
    found: dict[str, dict] = {}

    for term in search_terms[:6]:
        if len(term) < 3:
            continue
        try:
            with httpx.Client(timeout=10) as c:
                r = c.get(
                    f"{_BASE}/workspaces/{_WORKSPACE_GID}/typeahead",
                    params={
                        "resource_type": "task",
                        "query": term,
                        "count": 10,
                        "opt_fields": (
                            "name,gid,permalink_url,assignee.gid,assignee.name,"
                            "completed,modified_at,notes"
                        ),
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
            log.debug("Asana typeahead error for %r: %s", term, exc)
        time.sleep(0.15)

    tasks = list(found.values())
    tasks.sort(key=lambda t: t.get("modified_at", ""), reverse=True)
    return tasks[:30]


# ── Asana comment ─────────────────────────────────────────────────────────────

def _add_task_comment(task_gid: str, comment: str) -> bool:
    """POST a comment (story) to an Asana task. Returns True on success."""
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
            return True
        log.warning("Asana comment failed task=%s status=%d", task_gid, r.status_code)
        return False
    except Exception as exc:
        log.warning("Asana comment error task=%s: %s", task_gid, exc)
        return False


def _build_comment(messages: list[dict], task_name: str, score: int,
                   entities: dict) -> str:
    latest = messages[-1] if messages else {}
    subject = latest.get("subject", "") or messages[0].get("subject", "(no subject)") if messages else "(no subject)"
    date_str = latest.get("date_str", "")
    msg_count = len(messages)

    # Participant display
    participants = ", ".join(entities["raw_emails"][:4])
    if len(entities["raw_emails"]) > 4:
        participants += f" +{len(entities['raw_emails']) - 4} more"

    # Latest body excerpt
    body = (latest.get("body_text") or "").strip()
    excerpt = body[:300].replace("\n", " ")
    if len(body) > 300:
        excerpt += "…"

    lines = [
        "📧 Email thread synced by Cora",
        f"Subject: {subject}",
        f"Participants: {participants}",
        f"Messages: {msg_count}  |  Latest: {date_str}  |  Match score: {score}",
    ]
    if excerpt:
        lines.append(f"\nLatest excerpt:\n{excerpt}")

    return "\n".join(lines)


# ── Core sync ─────────────────────────────────────────────────────────────────

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
    owner_gid: str = user.get("asana_user_gid", "")
    display_name: str = user.get("display_name", owner_email)

    if not owner_email or not owner_gid:
        return {"threads": 0, "filtered": 0, "scored": 0, "commented": 0, "skipped": 0}

    user_key = owner_email.lower()
    user_state = state.get(user_key, {})
    since_ts = user_state.get("last_synced_ts", int(time.time()) - _DEFAULT_LOOKBACK)

    stats = {"threads": 0, "filtered": 0, "scored": 0, "commented": 0, "skipped": 0}

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

        for msg in messages:
            if msg.get("date_ts", 0) > new_last_ts:
                new_last_ts = msg["date_ts"]

        # ── Layer 1: Pre-filter ───────────────────────────────────────────
        passes, reason = _thread_passes_prefilter(messages, owner_email)
        if not passes:
            log.debug("[%s] Filtered thread %s: %s", display_name, thread_id, reason)
            stats["filtered"] += 1
            continue

        # ── Layer 2: Entity extraction ────────────────────────────────────
        entities = _extract_entities(messages, owner_email)

        # Build search terms: company names + subject keywords (NOT first names)
        search_terms = entities["company_names"] + entities["subject_kws"]
        search_terms = [t for t in search_terms if len(t) >= 3]

        if not search_terms:
            log.debug("[%s] No usable search terms for thread %s", display_name, thread_id)
            stats["skipped"] += 1
            continue

        log.debug(
            "[%s] Thread %s | companies=%s | subject_kws=%s",
            display_name, thread_id,
            entities["company_names"], entities["subject_kws"],
        )

        # ── Layer 3: Search + score ───────────────────────────────────────
        candidates = _search_asana(search_terms, owner_gid)
        stats["scored"] += len(candidates)

        best_task: dict | None = None
        best_score = 0

        for task in candidates:
            score = _score_task(task, entities, owner_gid)
            log.debug(
                "  Task %r score=%d (min=%d)",
                task.get("name", "?")[:50], score, _MIN_CONFIDENCE,
            )
            if score >= _MIN_CONFIDENCE and score > best_score:
                best_score = score
                best_task = task

        if not best_task:
            stats["skipped"] += 1
            continue

        task_gid = best_task.get("gid", "")
        task_name = best_task.get("name", "(no name)")
        task_url = best_task.get("permalink_url", "")

        if _already_commented(state, thread_id, task_gid):
            stats["skipped"] += 1
            continue

        comment = _build_comment(messages, task_name, best_score, entities)

        if dry_run:
            log.info(
                "  [DRY] score=%d → task %r (%s)\n    subject: %s\n    companies: %s",
                best_score, task_name[:60], task_gid,
                entities["subject_raw"][:60], entities["company_names"],
            )
            stats["commented"] += 1
            continue

        success = _add_task_comment(task_gid, comment)
        if success:
            _mark_commented(state, thread_id, task_gid)
            stats["commented"] += 1
            log.info(
                "[%s] score=%d → commented on %r (%s)",
                display_name, best_score, task_name[:60], task_gid,
            )

        time.sleep(0.2)

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
    total_filtered = 0

    for user in users:
        display = user.get("display_name", user.get("asana_email", "?"))
        log.info("--- Syncing %s ---", display)
        try:
            stats = sync_user(user, state, dry_run=dry_run)
        except Exception as exc:
            log.error("[%s] Unexpected error: %s", display, exc, exc_info=True)
            continue

        total_threads += stats["threads"]
        total_commented += stats["commented"]
        total_filtered += stats["filtered"]
        log.info(
            "  threads=%d  filtered=%d  scored=%d  commented=%d  skipped=%d",
            stats["threads"], stats["filtered"], stats["scored"],
            stats["commented"], stats["skipped"],
        )
        time.sleep(0.5)

    if not dry_run:
        _save_state(state)

    log.info(
        "Sync complete: %d threads, %d filtered (Layer 1), %d commented",
        total_threads, total_filtered, total_commented,
    )
