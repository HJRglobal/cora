"""Per-user email/Drive historical access control (Tiers 1 + 2).

Spec of record:
  G:\\My Drive\\HJR-Founder-OS\\_shared\\projects\\cora\\design\\
  2026-06-09_fndr_per-user-email-drive-access-spec.md

Two-tier model:

Tier 1 — Institutional knowledge (ALL users, entity-scoped) — ALLOWED.
  gmail / drive_sweep KB chunks may inform any answer, but before a chunk
  owned by someone OTHER than the asker is passed as LLM context, its
  identifying header/metadata is STRIPPED (From / To / Subject / Date /
  author / message_id / deep_link / thread_id). The factual body survives;
  the specific email/file can no longer be reproduced or attributed.

Tier 2 — Specific retrieval ("pull up / show me / find the email") — LOCKED.
  - DM with Cora ONLY (channel ask -> redirect to DM).
  - Returns only chunks owned by the asker (mailbox email + aliases).
  - Harrison-override via data/maps/historical-access-allowlist.yaml
    (default: Harrison only — add Slack IDs there, no code change).
  - Non-owner requesting an internal teammate's specifics -> explicit refusal
    (no existence leak).
  - FAIL-CLOSED: unmapped Slack identity gets no Tier-2 retrieval.

Why code-level instead of prompt-only (doctrine D-034): the model routes to
context/tools before applying scope rules, so a prompt rule alone surfaces
private mail before any refusal fires. This module is deterministic and is
wired BEFORE any Claude API call (top of app._dispatch_qa), the same pattern
as cross_entity_guard.py and sibling_guard.py.

Scope note: a Tier-2 grant deliberately does NOT consult user_access topic
blocks — the grant is scoped to the asker's OWN mailbox, which they may
always see (Harrison directive in the spec). Entity/PHI/sibling guards are
unaffected and still run.

This module is PURE (yaml + re + stdlib) so context_loader can import it
without dragging Google/Anthropic deps. Identity comes from
data/maps/monitored-email-accounts.yaml (mailbox owner registry) plus
data/maps/user-aliases.yaml (name variants).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from .phi_guard import is_phi_risk

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ACCOUNTS_PATH = _REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml"
_ALIASES_PATH = _REPO_ROOT / "data" / "maps" / "user-aliases.yaml"
_ALLOWLIST_PATH = _REPO_ROOT / "data" / "maps" / "historical-access-allowlist.yaml"
_AUDIT_LOG_PATH = _REPO_ROOT / "logs" / "historical-access-audit.jsonl"

# KB sources this access-control layer governs. Everything else (asana,
# fireflies, static_md, notion, slack, drive_asset) is org-level content and
# passes through untouched.
PERSONAL_SOURCES: frozenset[str] = frozenset({"gmail", "drive_sweep"})

# Pseudo-owners that are ORG-SHARED by definition — never stripped. The
# founders-os Drive sweep ingests the shared HJR-Founder-OS folder under this
# synthetic mailbox; it is not anyone's personal Drive.
ORG_SHARED_OWNERS: frozenset[str] = frozenset({"founders_os@hjrglobal.com"})

# 60s TTL cache that NEVER caches an empty result — same doctrine as
# user_access.py (an lru_cache populated during a startup race once pinned
# every user to wrong permissions until restart).
_CACHE_TTL_S = 60


# ────────────────────────────────────────────────────────────────────────────
# Identity index
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _IdentityIndex:
    by_slack: dict[str, frozenset[str]]        # slack_user_id -> owned emails
    name_to_emails: dict[str, frozenset[str]]  # normalized name -> emails
    name_to_label: dict[str, str]              # normalized name -> display name


_identity_cache: tuple[float, _IdentityIndex] | None = None
_allowlist_cache: tuple[float, frozenset[str]] | None = None


def _norm_name(name: str) -> str:
    """Lowercase, strip parenthetical suffixes like 'Harrison Rogers (F3E)'."""
    return re.sub(r"\s*\(.*?\)\s*$", "", name or "").strip().lower()


def _build_identity_index() -> _IdentityIndex:
    by_slack: dict[str, set[str]] = {}
    name_to_emails: dict[str, set[str]] = {}
    name_to_label: dict[str, str] = {}
    ambiguous_names: set[str] = set()
    name_owner: dict[str, str] = {}  # normalized name -> canonical full name

    try:
        raw = yaml.safe_load(_ACCOUNTS_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.error("historical_access: failed to load %s: %s", _ACCOUNTS_PATH, exc)
        raw = {}

    for acct in raw.get("accounts", []) or []:
        email = (acct.get("email") or "").strip().lower()
        if not email:
            continue
        emails = {email}
        for alias in acct.get("known_aliases") or []:
            emails.add(str(alias).strip().lower())

        slack_id = (acct.get("slack_user_id") or "").strip()
        if slack_id:
            by_slack.setdefault(slack_id, set()).update(emails)

        full = _norm_name(acct.get("name", ""))
        if not full:
            continue
        canonical_label = re.sub(r"\s*\(.*?\)\s*$", "", acct.get("name", "")).strip()
        # Index full name + first name. A first name shared by two DIFFERENT
        # people becomes ambiguous and is dropped (fail-closed on identity).
        for key in {full, full.split()[0]}:
            prior = name_owner.get(key)
            if prior is not None and prior != full:
                ambiguous_names.add(key)
                continue
            name_owner[key] = full
            name_to_emails.setdefault(key, set()).update(emails)
            name_to_label.setdefault(key, canonical_label)

    # Fold in user-aliases.yaml variants (e.g. "Sean" -> Shaun Hawkins).
    try:
        alias_raw = yaml.safe_load(_ALIASES_PATH.read_text(encoding="utf-8")) or {}
        for canonical, variants in (alias_raw.get("aliases") or {}).items():
            canon_key = _norm_name(canonical)
            emails = name_to_emails.get(canon_key)
            if not emails:
                continue
            for variant in variants or []:
                key = _norm_name(str(variant))
                prior = name_owner.get(key)
                if prior is not None and prior != canon_key:
                    ambiguous_names.add(key)
                    continue
                name_owner[key] = canon_key
                name_to_emails.setdefault(key, set()).update(emails)
                name_to_label.setdefault(key, name_to_label.get(canon_key, canonical))
    except Exception as exc:  # noqa: BLE001
        log.warning("historical_access: alias load failed (non-fatal): %s", exc)

    for key in ambiguous_names:
        name_to_emails.pop(key, None)
        name_to_label.pop(key, None)

    return _IdentityIndex(
        by_slack={k: frozenset(v) for k, v in by_slack.items()},
        name_to_emails={k: frozenset(v) for k, v in name_to_emails.items()},
        name_to_label=name_to_label,
    )


def _get_identity_index() -> _IdentityIndex:
    global _identity_cache
    now = time.monotonic()
    if _identity_cache is not None and now - _identity_cache[0] < _CACHE_TTL_S:
        return _identity_cache[1]
    idx = _build_identity_index()
    # Never cache an empty index — a startup-race empty read self-heals on the
    # next call instead of pinning everyone to fail-closed for the TTL.
    if idx.by_slack:
        _identity_cache = (now, idx)
    return idx


def owned_emails(slack_user_id: str) -> frozenset[str]:
    """All mailbox addresses (incl. aliases) owned by this Slack user.

    Empty frozenset when the identity can't be resolved — callers MUST treat
    that as fail-closed for Tier-2.
    """
    if not slack_user_id:
        return frozenset()
    return _get_identity_index().by_slack.get(slack_user_id, frozenset())


def is_unrestricted(slack_user_id: str) -> bool:
    """True if this Slack user may retrieve from ANY account (Harrison override).

    Backed by data/maps/historical-access-allowlist.yaml. Fail-closed: a
    missing/unreadable file allows nobody.
    """
    global _allowlist_cache
    if not slack_user_id:
        return False
    now = time.monotonic()
    if _allowlist_cache is not None and now - _allowlist_cache[0] < _CACHE_TTL_S:
        return slack_user_id in _allowlist_cache[1]
    ids: frozenset[str] = frozenset()
    try:
        raw = yaml.safe_load(_ALLOWLIST_PATH.read_text(encoding="utf-8")) or {}
        ids = frozenset(str(u).strip() for u in (raw.get("unrestricted") or []) if u)
    except Exception as exc:  # noqa: BLE001
        log.error("historical_access: allowlist load failed (fail-closed): %s", exc)
    if ids:
        _allowlist_cache = (now, ids)
    return slack_user_id in ids


def resolve_person(name: str) -> tuple[str, frozenset[str]] | None:
    """Resolve a teammate name/alias to (display_label, mailbox emails)."""
    key = _norm_name(name)
    idx = _get_identity_index()
    emails = idx.name_to_emails.get(key)
    if not emails:
        return None
    return idx.name_to_label.get(key, name.strip()), emails


def invalidate_cache() -> None:
    """Test hook — drop the TTL caches."""
    global _identity_cache, _allowlist_cache
    _identity_cache = None
    _allowlist_cache = None


# ────────────────────────────────────────────────────────────────────────────
# Retrieval-intent + target-person detection (deterministic, precision-biased)
# ────────────────────────────────────────────────────────────────────────────

_RETRIEVE_VERBS = (
    r"(?:show|pull(?:\s+up)?|find|fetch|get|grab|list|retrieve|forward|"
    r"send|search|look\s+up|dig\s+up)"
)
# Email-ish nouns. Deliberately does NOT include bare "file"/"document"/
# "invoice" — those stay on the existing KB DOCUMENT_QUERY path (D-013).
_MAIL_NOUNS = r"(?:e-?mails?|inbox|mailbox|gmail|mail|email\s+threads?|correspondence)"
_DRIVE_NOUNS = (
    r"(?:google\s+drive|drive\s+(?:files?|docs?|documents?|history)|"
    r"(?:files?|docs?|documents?)\s+(?:from|in|on)\s+"
    r"(?:my|his|her|their|[a-z]+'s)\s+drive)"
)

_INTENT_RES: tuple[re.Pattern, ...] = (
    re.compile(rf"\b{_RETRIEVE_VERBS}\b[^.?!\n]{{0,60}}?\b{_MAIL_NOUNS}\b", re.IGNORECASE),
    re.compile(rf"\b{_RETRIEVE_VERBS}\b[^.?!\n]{{0,60}}?\b{_DRIVE_NOUNS}\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+did\s+\w+(?:\s+\w+)?\s+e-?mail\b", re.IGNORECASE),
    re.compile(rf"\b{_MAIL_NOUNS}\b[^.?!\n]{{0,40}}\bfrom\s+my\s+(?:inbox|mailbox|account)\b", re.IGNORECASE),
)

# Possessive / explicit-mailbox reference to a person:
#   "Hannah's emails", "in Justin's inbox", "Shaun's drive files"
_POSSESSIVE_RE = re.compile(
    r"\b([a-z]+(?:\s+[a-z]+)?)'s\s+"
    r"(?:e-?mails?|inbox|mailbox|g?mail|drive|files?|docs?|documents?|"
    r"receipts?|invoices?|statements?|history)",
    re.IGNORECASE,
)
# "receipts from Hannah", "invoices from Justin Moran" — only meaningful on
# the cross-mailbox finance path (include_from=True). On the personal path,
# "emails from Justin" means mail FROM Justin sitting in the ASKER's own
# mailbox, which is theirs to see.
_FROM_PERSON_RE = re.compile(
    r"\b(?:receipts?|invoices?|statements?|bills?|e-?mails?|files?)\s+"
    r"(?:from|of)\s+([a-z]+(?:\s+[a-z]+)?)\b",
    re.IGNORECASE,
)


def detect_retrieval_intent(text: str) -> bool:
    """True when the message is an explicit retrieve/list/quote/forward of
    email or Drive items (vs a general question)."""
    if not text:
        return False
    return any(p.search(text) for p in _INTENT_RES)


def detect_target_person(
    text: str, asker_emails: frozenset[str] = frozenset(), include_from: bool = False
) -> tuple[str, frozenset[str]] | None:
    """Detect a reference to an internal teammate's mailbox in `text`.

    Returns (display_label, emails) for the FIRST internal-roster match whose
    mailboxes are not the asker's own, or None. Names that don't resolve to
    the internal roster are ignored (an external contact's name is a sender
    filter inside the asker's own mailbox, not a mailbox reference).
    """
    if not text:
        return None
    # Each pattern pairs with a fallback strategy for its greedy two-word
    # capture: in "pull up Hannah's emails" the possessive capture grabs
    # "up Hannah" (name is the LAST token); in "receipts from Hannah for May"
    # the from-capture grabs "Hannah for" (name is the FIRST token).
    patterns: list[tuple[re.Pattern, int]] = [(_POSSESSIVE_RE, -1)]
    if include_from:
        patterns.append((_FROM_PERSON_RE, 0))
    for pat, fallback_token in patterns:
        for m in pat.finditer(text):
            captured = m.group(1)
            candidates = [captured]
            tokens = captured.split()
            if len(tokens) > 1:
                candidates.append(tokens[fallback_token])
            for cand in candidates:
                resolved = resolve_person(cand)
                if resolved is None:
                    continue
                label, emails = resolved
                if asker_emails and emails <= asker_emails:
                    break  # their own mailbox referenced by name — not a target
                return label, emails
    return None


# ────────────────────────────────────────────────────────────────────────────
# Tier 1 — header strip for non-owner chunks
# ────────────────────────────────────────────────────────────────────────────

# Identifying header lines injected by gmail_threaded_sweep at chunk start.
_HEADER_LINE_RE = re.compile(
    r"^(?:From|To|Cc|Bcc|Subject|Date|Attachments):[^\n]*\n?", re.IGNORECASE | re.MULTILINE
)

_STRIPPED_TITLE = {
    "gmail": "(internal email — details withheld)",
    "drive_sweep": "(internal document — details withheld)",
}


def chunk_owner_email(result: Any) -> str | None:
    """Owner mailbox for a gmail/drive_sweep SearchResult, or None.

    Primary: metadata.user_email (present on 100% of both sources, verified
    2026-06-10). Fallback for gmail: parse the 'gmail:{email}:{id}' source_id.
    """
    meta = getattr(result, "metadata", None)
    if isinstance(meta, dict):
        owner = str(meta.get("user_email") or "").strip().lower()
        if owner:
            return owner
    if result.source == "gmail":
        parts = (result.source_id or "").split(":")
        if len(parts) >= 3 and "@" in parts[1]:
            return parts[1].strip().lower()
    return None


def strip_result(result: Any) -> Any:
    """Return a de-identified copy of a personal chunk (Tier-1 strip).

    Removes: title (Subject), author (From), deep_link, date, metadata
    (message_id / thread_id / drive_link / owner), and the From/To/Subject/
    Date/Attachments header lines inside the content. The factual body
    survives so the institutional knowledge still informs answers.
    """
    content = _HEADER_LINE_RE.sub("", result.content or "").strip()
    return replace(
        result,
        title=_STRIPPED_TITLE.get(result.source, "(internal item — details withheld)"),
        content=content,
        deep_link="",
        date_modified=None,
        author="",
        metadata=None,
    )


def apply_tier1(
    results: list, asker_emails: frozenset[str], unrestricted: bool
) -> tuple[list, bool]:
    """Strip non-owned personal chunks; pass everything else through.

    Returns (processed_results, contains_unstripped_personal). The flag is
    True when any gmail/drive_sweep chunk went through UNSTRIPPED (asker owns
    it, or asker is unrestricted) — callers must NOT store such a response in
    the shared semantic cache (another user could replay it).
    """
    out: list = []
    unstripped_personal = False
    for r in results:
        if r.source not in PERSONAL_SOURCES:
            out.append(r)
            continue
        owner = chunk_owner_email(r)
        if owner in ORG_SHARED_OWNERS:
            out.append(r)
            continue
        if unrestricted or (owner is not None and owner in asker_emails):
            out.append(r)
            unstripped_personal = True
        else:
            # Unknown owner (None) is stripped too — fail-closed.
            out.append(strip_result(r))
    return out, unstripped_personal


TIER1_SYNTHESIS_RULE = (
    "Email/Drive knowledge rule: use facts from organizational email and Drive "
    "context to inform your answer, but never quote, attribute, or list a "
    "specific individual's email or file unless the asker owns it or the "
    "context explicitly marks it as retrieved for them. Context items labeled "
    "'details withheld' must stay unattributed — no sender, subject, date, or "
    "link may be guessed or reconstructed."
)


# ────────────────────────────────────────────────────────────────────────────
# Tier 2 — decision + formatting
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AccessDecision:
    """Outcome of the pre-LLM historical-access check.

    action:
      "pass"    — not a Tier-2 request; continue the normal pipeline.
      "respond" — post `message` as the COMPLETE reply and stop (redirect or
                  refusal). Never add to it; never call the LLM.
      "grant"   — explicit retrieval authorized for `owner_emails`
                  (None = any mailbox; finance path only).
    """
    action: str
    message: str = ""
    owner_emails: frozenset[str] | None = None
    mode: str = ""           # "personal" | "finance"
    target_label: str = ""   # whose mailbox ("your", "Hannah Grant", ...)


PASS = AccessDecision(action="pass")

_REDIRECT_TO_DM = (
    "I can pull your own email and Drive files for you in a DM — message me "
    "directly and I'll take care of it."
)
_REFUSE_OTHER = (
    "I can only retrieve email and Drive history from your own accounts. "
    "Anyone else's mailbox is private — that access runs through Harrison."
)
_REFUSE_UNMAPPED = (
    "I can't link your Slack account to a monitored mailbox, so I can't pull "
    "email or Drive history for you. Ask Harrison to have your mailbox added."
)


def check_tier2(slack_user_id: str, is_dm: bool, text: str) -> AccessDecision:
    """Deterministic Tier-2 gate. Call BEFORE any Claude API call (D-034)."""
    if not detect_retrieval_intent(text):
        return PASS

    if not is_dm:
        return AccessDecision(action="respond", message=_REDIRECT_TO_DM)

    unrestricted = is_unrestricted(slack_user_id)
    own = owned_emails(slack_user_id)
    target = detect_target_person(text, asker_emails=own)

    if target is not None:
        label, emails = target
        if not unrestricted:
            return AccessDecision(action="respond", message=_REFUSE_OTHER)
        return AccessDecision(
            action="grant", owner_emails=emails, mode="personal", target_label=label,
        )

    # Own-mailbox retrieval — fail-closed when identity can't be resolved.
    if not own:
        return AccessDecision(action="respond", message=_REFUSE_UNMAPPED)
    return AccessDecision(
        action="grant", owner_emails=own, mode="personal", target_label="your",
    )


def drop_phi(results: list) -> list:
    """Defensive PHI filter for retrieval grants — LEX client PHI never rides
    a Tier-2/finance grant even if an ingest-time guard missed it."""
    return [
        r for r in results
        if not is_phi_risk(f"{r.title or ''}\n{(r.content or '')[:1200]}")
    ]


def format_owned_chunks(results: list, target_label: str) -> str:
    """Render owner-authorized chunks (FULL headers/links) as LLM context."""
    if not results:
        return (
            "# Retrieved mailbox items\n\n"
            f"No matching items were found in {target_label} mailbox or Drive "
            "history for this request. Say so plainly — do not invent items."
        )
    lines = [
        "# Retrieved mailbox items (explicit retrieval — owner-authorized)",
        "",
        f"(The asker is authorized to see these items from {target_label} "
        "mailbox/Drive history in full. Present the relevant ones as a short "
        "list — sender, subject, date — quoting content where useful. Only "
        "include links that appear below; never fabricate one.)",
        "",
    ]
    for i, r in enumerate(results, 1):
        owner = chunk_owner_email(r) or "unknown"
        date_str = ""
        if r.date_modified:
            try:
                import datetime as _dt
                date_str = _dt.date.fromtimestamp(r.date_modified).isoformat()
            except (OSError, ValueError, OverflowError):
                pass
        head = f"## [{i}] {r.title or r.source_id} | {date_str} | mailbox: {owner}"
        if getattr(r, "author", ""):
            head += f" | from: {r.author}"
        if r.deep_link:
            head += f" | {r.deep_link}"
        lines.extend([head, "", (r.content or "").strip(), ""])
    return "\n".join(lines)


def audit(
    requester: str,
    query: str,
    mode: str,
    owner_emails: frozenset[str] | None,
    items: list[str],
    channel: str = "",
) -> None:
    """Append a retrieval-grant record to logs/historical-access-audit.jsonl."""
    try:
        _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": int(time.time()),
            "requester": requester,
            "channel": channel,
            "mode": mode,
            "query": (query or "")[:500],
            "owner_emails": sorted(owner_emails) if owner_emails else "ANY",
            "items": items[:50],
        }
        with _AUDIT_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as exc:  # noqa: BLE001 — audit failure must not break replies
        log.error("historical_access: audit write failed: %s", exc)
