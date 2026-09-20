#!/usr/bin/env python3
"""cora@ mailbox -> knowledge-review INTAKE sweep (Code #13 Rider 1 S-A, cq-8d16f1a557e5).

WHY THIS SCRIPT EXISTS (VERIFY-FIRST, 2026-09-19)
-------------------------------------------------
The 8/18 ruling PROVISIONED cora@hjrglobal.com as an intake mailbox whose messages
"sweep into the knowledge-review queue as PROPOSALS". The kickoff assumed the
nightly gmail sweep could do that. It cannot: scripts/gmail_threaded_sweep.py has
exactly ONE write site -- kb.upsert_documents -- and never imports knowledge_review
or info_intake. A cora@ row with the sweep flags left at their defaults would have
been CHUNK-INGESTED (thread_sweep defaults TRUE), the opposite posture. So the
roster row sets all three sweep flags explicitly false and carries ONE new key,
``intake_route: knowledge_review``, whose only consumer is this script.

WHAT IT DOES
------------
For every roster row with ``intake_route == knowledge_review`` (today: cora@):
  1. list Gmail messages newer than the per-mailbox watermark (DWD, the existing
     gmail_reader builder, gmail.modify scope exactly as the nightly sweep);
  2. per message, CODE skip rules first -- automated-sender classes (fireflies.ai,
     calendar-notification / noreply / notifications / mailer-daemon localparts) and
     the Fireflies + Google Calendar subject shapes ("Invitation:", "Accepted:",
     "Declined:", "Updated invitation", "meeting recap/notes/summary"). Enforcement
     in CODE, never a prompt (the kickoff's "existing skip rules" were lines of the
     attachment filer's Claude prompt, which this path never runs);
  3. resolve the SENDER to a roster human (monitored-email-accounts email +
     known_aliases -> slack_user_id). A non-roster sender is skipped fail-closed,
     counted, never proposed; a message the mailbox sent itself is skipped too.
     The From: header alone is forgeable (RFC 5322), and this is a knowledge
     door keyed on identity, so the address resolves to a roster human ONLY when
     Gmail's own Authentication-Results header (authserv-id mx.google.com) says
     dmarc=pass, or spf=pass AND dkim=pass, with the authenticated domain equal
     to the From domain. A missing / unparseable header or any other verdict is
     skipped as ``unauthenticated_sender`` (counted, logged by reason + sender
     domain, never the body) -- D-051 A-intake-roster-5;
  4. hand the message to info_intake.ingest(route="mailbox") -- REUSED, never copied.
     ingest already carries the unconditional PHI screen, the blanket LEX-CONTENT
     refusal, the durable-fact screen, dedup, and payload.source="info-for-cora", so
     knowledge_review.is_knowledge_update classifies the proposal as knowledge and
     the R3 autowrite SOURCE exclusion applies unchanged (Tier-0 by construction:
     CORA_AUTOWRITE_LIVE never reaches this mailbox);
  5. advance the per-mailbox watermark (atomic write) -- ONLY with --apply.

ORDER + CAP (D-051 A-intake-roster-2): Gmail's messages.list returns NEWEST first.
The sweep lists the WHOLE window (paged, bounded by _MAX_LIST_PAGES), then
processes the OLDEST ``--max-messages`` of it in chronological order, so at the
cap the watermark = newest message actually processed and the newer remainder is
the next run's backlog -- WARNED by count, never silently skipped. (Processing in
API order and then advancing to "newest processed" -- the first cut -- skipped
every older unseen message forever the moment the cap was hit.)

NO size floor: a one-line human note is the primary use case (smoke 4).

DRY-RUN IS THE DEFAULT. ``--apply`` is the only mode that proposes or writes the
watermark. A --dry-run flag is a claim about EVERY write site (D-290): the test
suite proves a dry-run leaves the watermark untouched and never calls
knowledge_review.propose_update.

Usage:
    python scripts/run_mailbox_intake_sweep.py              # dry-run (default)
    python scripts/run_mailbox_intake_sweep.py --apply      # propose + advance watermark
    python scripts/run_mailbox_intake_sweep.py --mailbox cora@hjrglobal.com --max-messages 20

Environment: GOOGLE_SERVICE_ACCOUNT_JSON (DWD). Optional
MAILBOX_INTAKE_WATERMARK_PATH relocates the watermark (tests redirect it).
Standalone -- imports no bot-process module beyond the shared intake chokepoint,
so it activates from the working tree at its next fire with NO Cora restart.
"""

from __future__ import annotations

import argparse
import email.utils
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import info_intake  # noqa: E402

log = logging.getLogger("mailbox_intake_sweep")

ACCOUNTS_PATH = _REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml"
_DEFAULT_WATERMARK_PATH = _REPO_ROOT / "data" / "state" / "mailbox-intake-watermark.json"
_LOG_DIR = _REPO_ROOT / "logs"

#: The roster key + the one value this consumer honours. Any OTHER value on a row
#: is REFUSED (skipped + logged), never fallen open to "sweep it anyway".
INTAKE_ROUTE_KEY = "intake_route"
INTAKE_ROUTE_KNOWLEDGE_REVIEW = "knowledge_review"
#: payload.intake_route recorded on every proposal from this path (the Slack
#: routes are mention / message_event / sweep).
ROUTE = "mailbox"
CHANNEL_NAME = "cora@ mailbox"
TASK_NAME = "Cora - Mailbox Intake Sweep"

#: First run with no watermark: how far back to look. Bounded on purpose -- an
#: unbounded first pass would re-read the mailbox's whole history.
DEFAULT_BOOTSTRAP_DAYS = 7
#: Safety cap on messages PROCESSED (fetched + classified) per mailbox per run.
#: The whole window is LISTED (ids only, cheap) and the OLDEST cap of it is
#: processed, so the cap-aware watermark (D-038) -- advance only to the newest
#: message actually processed, never to "now" -- really does leave the newer
#: remainder for the next run instead of skipping the older one.
DEFAULT_MAX_MESSAGES = 100
#: Listing ceiling (pages of <=100 ids). Unreachable for one small intake
#: mailbox over a 7-day bootstrap; if it IS hit the listing is truncated at the
#: NEWEST ids, so the oldest are unknown and the watermark is FROZEN + WARNED
#: rather than advanced past mail never seen (narrow --bootstrap-days).
_MAX_LIST_PAGES = 50

# -- Sender authentication (D-051 A-intake-roster-5) ---------------------------
#: The authserv-id of the MTA that received the message for a Workspace mailbox.
#: Only an Authentication-Results header stamped by it is trusted; Gmail
#: prepends its own trace headers above any a sender supplied, so the FIRST such
#: header in payload order is Gmail's.
_TRUSTED_AUTHSERV_ID = "mx.google.com"
#: RFC 8601 pieces. Each regex is linear: one bounded character class per token,
#: no nested quantifiers (ReDoS class, D-051 lessons); growth-shape tested.
_AR_COMMENT_RE = re.compile(r"\([^()]*\)")
_AR_METHOD_RE = re.compile(r"^\s*([A-Za-z0-9-]+)\s*=\s*([A-Za-z0-9]+)")
_AR_PROP_RE = re.compile(
    r"\b(header\.from|header\.d|header\.i|smtp\.mailfrom)\s*=\s*\"?([^\s;\"]+)",
    re.IGNORECASE,
)

# -- CODE skip rules ----------------------------------------------------------
# Automated-sender LOCALPARTS. Anchored at both ends; the optional tail admits a
# plus/dot suffix ("noreply+abc", "notifications.bounces") without letting a
# human localpart that merely CONTAINS one of these words ("mynotifications",
# "notifications-team") in.
# Linear: one alternation, one optional tail, no nested quantifiers (ReDoS class,
# D-051 lessons).
_AUTOMATED_LOCALPART_RE = re.compile(
    r"^(?:no[-_.]?reply|do[-_]?not[-_]?reply|notifications?|mailer-daemon|postmaster"
    r"|calendar-notification|calendar|alerts?|bounces?)(?:[+.][A-Za-z0-9+._\-]*)?$",
    re.IGNORECASE,
)
# Automated-sender DOMAINS (exact or any subdomain).
_AUTOMATED_DOMAINS: frozenset[str] = frozenset({
    "fireflies.ai",
    "calendar-server.bounces.google.com",
})
# Subject shapes of Fireflies + Google Calendar traffic. Each alternative is a
# literal-led phrase; the leading "(Re|Fwd):" strip is a bounded, non-nested
# repeat that consumes at least three characters per iteration.
_REPLY_PREFIX_RE = re.compile(r"^(?:\s*(?:re|fwd?|fw)\s*:)*\s*", re.IGNORECASE)
_AUTOMATED_SUBJECT_RE = re.compile(
    r"^(?:invitation|updated invitation|accepted|declined|tentatively accepted"
    r"|canceled event|cancelled event|new event|reminder)\b"
    r"|\bmeeting (?:recap|notes|summary)\b"
    r"|\byour meeting recap\b"
    r"|\bfireflies\b",
    re.IGNORECASE,
)

#: Skip reasons (counted per run; the log line names the reason, never the body).
SKIP_AUTOMATED_SENDER = "automated_sender"
SKIP_AUTOMATED_SUBJECT = "automated_subject"
SKIP_SELF = "self_sent"
SKIP_NON_ROSTER = "non_roster_sender"
#: A roster address in From: that Gmail did NOT authenticate (no trusted
#: Authentication-Results, or a verdict other than dmarc=pass / spf+dkim pass
#: aligned to the From domain). Fail-closed: skipped, counted, never proposed.
SKIP_UNAUTHENTICATED = "unauthenticated_sender"
SKIP_EMPTY = "empty"


def _setup_logging() -> None:
    """Console + dated file (C7 doctrine: a windowless task's stdout dies with its
    window). Lazy -- importing this module writes nothing."""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers: list[logging.Handler] = [
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                _LOG_DIR / f"mailbox-intake-sweep-{datetime.now().strftime('%Y-%m-%d')}.log",
                encoding="utf-8"),
        ]
    except Exception:  # noqa: BLE001 -- a log file must never stop the sweep
        handlers = [logging.StreamHandler(sys.stdout)]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


# -- Watermark (per mailbox, atomic) ------------------------------------------
def watermark_path() -> Path:
    """Env-relocatable so the test suite can redirect it (conftest); default is
    data/state/mailbox-intake-watermark.json."""
    override = os.environ.get("MAILBOX_INTAKE_WATERMARK_PATH", "")
    return Path(override) if override else _DEFAULT_WATERMARK_PATH


def read_watermarks() -> dict[str, int]:
    """mailbox -> newest processed Gmail internalDate, in Unix SECONDS."""
    p = watermark_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 -- corrupt watermark -> bootstrap window
        log.warning("watermark unreadable at %s -- bootstrapping", p)
        return {}
    out: dict[str, int] = {}
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        try:
            out[str(k).lower()] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def write_watermarks(marks: dict[str, int]) -> None:
    """Atomic write so a kill mid-run cannot leave a truncated watermark (the
    gmail sweep's _save_watermarks shape)."""
    p = watermark_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(marks, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def next_watermark(processed: int, cap: int, newest_processed_ts: int, sync_start: int) -> int:
    """Cap-aware (D-038, the gmail sweep's _next_watermark): under the cap we
    drained the window -> sync_start; at the cap there is NEWER backlog we listed
    but did not process -> advance only to the newest message actually processed
    so the next run's ``after:`` re-lists exactly that remainder; nothing
    processed -> 0 (caller treats 0 as 'no change').

    This is only correct because sweep_mailbox processes the OLDEST ``cap``
    messages of the listed window (A-intake-roster-2). Fed the newest ``cap``
    (Gmail's native list order) the same arithmetic would skip every older
    unseen message forever."""
    if processed < cap:
        return sync_start
    if newest_processed_ts > 0:
        return newest_processed_ts
    return 0


# -- Roster -------------------------------------------------------------------
def _load_roster(path: Path | str | None = None) -> list[dict[str, Any]]:
    import yaml  # lazy: keep the module importable without the yaml dep at import
    p = Path(path) if path else ACCOUNTS_PATH
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [a for a in (data.get("accounts") or []) if isinstance(a, dict)]


def load_intake_mailboxes(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Roster rows this sweep serves: enabled AND intake_route == knowledge_review.
    A row carrying the key with any OTHER value is refused loudly (skipped +
    logged), never swept -- a typo must not fall open."""
    out: list[dict[str, Any]] = []
    for a in _load_roster(path):
        route = a.get(INTAKE_ROUTE_KEY)
        if route is None:
            continue
        if not a.get("enabled"):
            continue
        if str(route).strip().lower() != INTAKE_ROUTE_KNOWLEDGE_REVIEW:
            log.warning("roster row %s carries %s=%r -- REFUSED (only %r is served)",
                        a.get("email"), INTAKE_ROUTE_KEY, route,
                        INTAKE_ROUTE_KNOWLEDGE_REVIEW)
            continue
        if a.get("thread_sweep", True) or a.get("attachment_filer", True) \
                or a.get("drive_sweep", True):
            # Posture guard: an intake mailbox must never ALSO be chunk-ingested.
            # Refuse the row rather than sweep a mailbox the other consumers also
            # read -- the header documents the explicit-false requirement.
            log.warning("roster row %s is an intake mailbox but leaves a sweep flag "
                        "true/absent -- REFUSED until thread_sweep, attachment_filer "
                        "and drive_sweep are all explicitly false", a.get("email"))
            continue
        out.append(a)
    return out


def build_sender_index(path: Path | str | None = None) -> dict[str, tuple[str, str]]:
    """sender email (lowercase) -> (slack_user_id, display name) over every ENABLED
    roster human with a slack_user_id, via the row email + known_aliases. Rows
    without a slack_user_id (shared inboxes, the intake mailbox itself) are never
    senders -- ingest() requires a non-empty author_id and this path must resolve
    to a REAL roster human or skip."""
    index: dict[str, tuple[str, str]] = {}
    for a in _load_roster(path):
        if not a.get("enabled"):
            continue
        sid = str(a.get("slack_user_id") or "").strip()
        if not sid:
            continue
        name = str(a.get("name") or "").strip()
        name = re.sub(r"\s*\(.*?\)\s*$", "", name).strip() or sid
        for em in [a.get("email"), *(a.get("known_aliases") or [])]:
            em_l = str(em or "").strip().lower()
            if em_l and em_l not in index:
                index[em_l] = (sid, name)
    return index


# -- Message classification ---------------------------------------------------
def sender_email(from_header: str) -> str:
    """'Name <addr>' or bare 'addr' -> lowercase addr ('' when unparseable)."""
    _, addr = email.utils.parseaddr(from_header or "")
    return (addr or "").strip().lower()


def is_automated_sender(addr: str) -> bool:
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return False
    local, _, domain = addr.rpartition("@")
    if domain in _AUTOMATED_DOMAINS or any(
            domain.endswith("." + d) for d in _AUTOMATED_DOMAINS):
        return True
    return bool(_AUTOMATED_LOCALPART_RE.match(local))


def is_automated_subject(subject: str) -> bool:
    s = _REPLY_PREFIX_RE.sub("", (subject or "").strip(), count=1)
    return bool(_AUTOMATED_SUBJECT_RE.search(s))


def _domain_of(value: str) -> str:
    """'tommy@f3energy.com' / '@f3energy.com' / 'f3energy.com.' -> 'f3energy.com'."""
    v = (value or "").strip().strip('"').lower()
    if "@" in v:
        v = v.rpartition("@")[2]
    return v.rstrip(".")


def parse_authentication_results(values: list[str] | None) -> dict[str, dict[str, str]]:
    """The FIRST Authentication-Results header stamped by the trusted receiving
    MTA (_TRUSTED_AUTHSERV_ID), parsed per RFC 8601 into
    {method: {"result": ..., "<ptype.property>": ...}}. {} when no trusted header
    exists or it is unparseable. Headers claiming any other authserv-id are
    ignored: a sender can write its own Authentication-Results line, but it
    cannot make Gmail's MX stamp one above it."""
    for raw in values or []:
        text = _AR_COMMENT_RE.sub(" ", str(raw or ""))
        text = _AR_COMMENT_RE.sub(" ", text)  # one nesting level of CFWS, still linear
        parts = [p for p in text.split(";")]
        if not parts:
            continue
        authserv = parts[0].strip().split()
        if not authserv or authserv[0].lower() != _TRUSTED_AUTHSERV_ID:
            continue
        out: dict[str, dict[str, str]] = {}
        for seg in parts[1:]:
            m = _AR_METHOD_RE.match(seg)
            if not m:
                continue
            method, result = m.group(1).lower(), m.group(2).lower()
            props: dict[str, str] = {"result": result}
            for pm in _AR_PROP_RE.finditer(seg):
                props[pm.group(1).lower()] = pm.group(2)
            out.setdefault(method, props)   # first resinfo per method wins
        return out
    return {}


def sender_authenticated(auth_results: list[str] | None, from_addr: str) -> bool:
    """True ONLY when Gmail authenticated the From domain: dmarc=pass aligned to
    the From domain, or spf=pass AND dkim=pass each aligned to it. Anything else
    -- no trusted header, unparseable, fail/none/neutral/softfail/temperror,
    or a pass for a DIFFERENT domain -- is False (fail-closed)."""
    from_domain = _domain_of(from_addr)
    if not from_domain:
        return False
    parsed = parse_authentication_results(auth_results)
    if not parsed:
        return False
    dmarc = parsed.get("dmarc") or {}
    if dmarc.get("result") == "pass" and _domain_of(dmarc.get("header.from", "")) == from_domain:
        return True
    spf = parsed.get("spf") or {}
    dkim = parsed.get("dkim") or {}
    spf_ok = (spf.get("result") == "pass"
              and _domain_of(spf.get("smtp.mailfrom", "")) == from_domain)
    dkim_domain = _domain_of(dkim.get("header.i", "") or dkim.get("header.d", ""))
    dkim_ok = dkim.get("result") == "pass" and dkim_domain == from_domain
    return bool(spf_ok and dkim_ok)


def skip_reason(*, sender: str, subject: str, body: str, mailbox: str,
                sender_index: dict[str, tuple[str, str]],
                auth_results: list[str] | None = None) -> str:
    """'' when the message should reach ingest(); otherwise the SKIP_* reason.
    Order: automated sender -> automated subject -> self-sent -> non-roster ->
    UNAUTHENTICATED -> empty. Fail-closed: an unresolvable sender is skipped,
    never proposed, and a roster address Gmail did not authenticate for its own
    domain (``auth_results`` = the message's Authentication-Results headers in
    payload order; None/[] = no header) is skipped too -- the From: header alone
    never resolves an identity (A-intake-roster-5)."""
    addr = sender_email(sender)
    if is_automated_sender(addr):
        return SKIP_AUTOMATED_SENDER
    if is_automated_subject(subject):
        return SKIP_AUTOMATED_SUBJECT
    if addr and addr == (mailbox or "").strip().lower():
        return SKIP_SELF
    if addr not in sender_index:
        return SKIP_NON_ROSTER
    if not sender_authenticated(auth_results, addr):
        return SKIP_UNAUTHENTICATED
    if not (subject or "").strip() and not (body or "").strip():
        return SKIP_EMPTY
    return ""


def intake_text(subject: str, body: str) -> str:
    """subject + body, each stripped; a subject-only note is a valid one-liner."""
    subj = (subject or "").strip()
    if subj.lower() == "(no subject)":
        subj = ""
    parts = [p for p in (subj, (body or "").strip()) if p]
    return "\n".join(parts)


def intake_ts(internal_ms: int | str | None, message_id: str) -> str:
    """The ts handed to ingest(): the Gmail internalDate (epoch MILLISECONDS, as
    a digit string -- distinct from a Slack ts, which always carries a decimal
    part) or, when absent, the Gmail message id. Drives update_id
    (infocora-<ts>) and payload.message_ts."""
    try:
        ms = int(internal_ms or 0)
    except (TypeError, ValueError):
        ms = 0
    return str(ms) if ms > 0 else str(message_id or "")


# -- Gmail (read-only; the existing DWD builder) ------------------------------
def _service(mailbox: str):
    from cora.connectors.gmail_reader import _build_service
    return _build_service(mailbox)


def list_message_ids(service, since_ts: int,
                     max_pages: int = _MAX_LIST_PAGES) -> tuple[list[str], bool]:
    """messages.list with after:<since> over the WHOLE window (paged, 100/page,
    at most ``max_pages`` pages), in Gmail's native NEWEST-first order. Returns
    (ids, truncated): truncated=True means the ceiling cut the listing at its
    newest ids and the oldest are unknown. num_retries=2 mirrors the gmail
    sweep's getProfile call (a single transient 429/500 must not skip a mailbox
    for a night)."""
    ids: list[str] = []
    page_token: str | None = None
    pages = 0
    while True:
        kwargs: dict[str, Any] = {
            "userId": "me",
            "q": f"after:{int(since_ts)}",
            "maxResults": 100,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute(num_retries=2)
        pages += 1
        for m in resp.get("messages") or []:
            if m.get("id"):
                ids.append(m["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            return ids, False
        if pages >= max_pages:
            return ids, True


def fetch_message(service, message_id: str) -> dict[str, Any]:
    """Full message -> {message_id, sender, subject, body, internal_ms,
    auth_results}. ``auth_results`` = EVERY Authentication-Results header value
    in payload order (Gmail's own is first; a sender-supplied one may follow)."""
    from cora.connectors.gmail_reader import _extract_text_from_part
    msg = service.users().messages().get(
        userId="me", id=message_id, format="full").execute(num_retries=2)
    raw_headers = (msg.get("payload") or {}).get("headers", []) or []
    headers = {h["name"].lower(): h["value"] for h in raw_headers}
    auth_results = [h.get("value", "") for h in raw_headers
                    if str(h.get("name", "")).lower() == "authentication-results"]
    body = _extract_text_from_part(msg.get("payload") or {}).strip()
    return {
        "message_id": msg.get("id") or message_id,
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "body": body[:4000],
        "internal_ms": int(msg.get("internalDate") or 0),
        "auth_results": auth_results,
    }


# -- The sweep ----------------------------------------------------------------
def sweep_mailbox(
    row: dict[str, Any],
    *,
    sender_index: dict[str, tuple[str, str]],
    watermarks: dict[str, int],
    apply: bool,
    max_messages: int,
    bootstrap_days: int,
    service=None,
) -> dict[str, Any]:
    """One mailbox. Returns a summary dict (counts + the watermark decision).
    Writes NOTHING itself: the caller persists watermarks, and only with --apply."""
    mailbox = str(row.get("email") or "").strip().lower()
    sync_start = int(time.time())
    since = watermarks.get(mailbox) or (sync_start - bootstrap_days * 86400)
    summary: dict[str, Any] = {
        "mailbox": mailbox, "since": since, "listed": 0, "fetched": 0, "backlog": 0,
        "outcomes": {}, "skips": {}, "new_watermark": 0, "error": "",
    }
    try:
        svc = service or _service(mailbox)
        all_ids, truncated = list_message_ids(svc, since)
    except Exception as exc:  # noqa: BLE001 -- a dark mailbox skips, never crashes the run
        summary["error"] = f"list failed: {type(exc).__name__}"
        log.warning("%s: messages.list failed (%s) -- skipping this mailbox, "
                    "watermark untouched", mailbox, type(exc).__name__)
        return summary
    # OLDEST-first (A-intake-roster-2): Gmail lists newest first; reverse, then
    # take the oldest `max_messages`. The newer remainder is next run's backlog
    # -- its ids were listed, so the cap-hit is a counted WARNING, not a silent skip.
    ids = list(reversed(all_ids))[:max_messages]
    backlog = max(0, len(all_ids) - len(ids))
    summary["listed"] = len(all_ids)
    summary["fetched"] = len(ids)
    summary["backlog"] = backlog
    log.info("%s: %d message(s) since %d%s", mailbox, len(all_ids), since,
             "" if apply else " [DRY-RUN]")
    if backlog:
        log.warning("%s: CAP HIT -- processing the OLDEST %d of %d listed; %d newer "
                    "message(s) remain as BACKLOG for the next run (watermark advances "
                    "only to the newest processed, never to now)",
                    mailbox, len(ids), len(all_ids), backlog)
    if truncated:
        log.warning("%s: listing ceiling (%d pages) hit -- the window's OLDEST mail is "
                    "unknown, so the watermark is FROZEN this run; narrow "
                    "--bootstrap-days or raise the cap and re-run",
                    mailbox, _MAX_LIST_PAGES)

    newest_processed = 0
    # an ingest ERROR (or a truncated listing) freezes the watermark for the run
    frozen = bool(truncated)
    for mid in ids:
        try:
            m = fetch_message(svc, mid)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: messages.get %s failed (%s) -- freezing watermark",
                        mailbox, mid, type(exc).__name__)
            frozen = True
            continue
        ts_s = int(m["internal_ms"] // 1000) if m["internal_ms"] else 0
        reason = skip_reason(sender=m["sender"], subject=m["subject"], body=m["body"],
                             mailbox=mailbox, sender_index=sender_index,
                             auth_results=m.get("auth_results"))
        if reason:
            summary["skips"][reason] = summary["skips"].get(reason, 0) + 1
            if reason == SKIP_UNAUTHENTICATED:
                # Reason + sender DOMAIN only -- never the body, never the address
                # (a forged From is attacker-chosen text).
                log.warning("  id=%s -> skipped (%s) from-domain=%s", mid, reason,
                            _domain_of(sender_email(m["sender"])) or "?")
            else:
                log.info("  id=%s -> skipped (%s)", mid, reason)
            if not frozen and ts_s > newest_processed:
                newest_processed = ts_s
            continue
        author_id, author_name = sender_index[sender_email(m["sender"])]
        result = info_intake.ingest(
            text=intake_text(m["subject"], m["body"]),
            author_id=author_id,
            author_name=author_name,
            ts=intake_ts(m["internal_ms"], m["message_id"]),
            route=ROUTE,
            channel_id="",
            channel_name=CHANNEL_NAME,
            dry_run=not apply,
        )
        summary["outcomes"][result.outcome] = summary["outcomes"].get(result.outcome, 0) + 1
        log.info("  id=%s from=%s -> %s%s", mid, author_id, result.outcome,
                 f" (entity {result.entity})" if result.stored else "")
        if result.outcome == info_intake.ERROR:
            frozen = True
        elif not frozen and ts_s > newest_processed:
            newest_processed = ts_s

    if frozen:
        summary["new_watermark"] = 0
        log.warning("%s: watermark FROZEN this run (a message errored) -- the next "
                    "run re-reads from %d", mailbox, since)
    else:
        # processed = the WHOLE listed window; under the cap it was drained
        # (-> sync_start), at/over it only the oldest `cap` were processed
        # (-> newest processed, the backlog's lower bound).
        summary["new_watermark"] = next_watermark(
            len(all_ids), max_messages, newest_processed, sync_start)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="cora@ mailbox -> knowledge-review intake sweep.")
    ap.add_argument("--apply", action="store_true",
                    help="Propose + advance the watermark. Without it (default) the run "
                         "is a DRY-RUN: classify and report, write nothing.")
    ap.add_argument("--mailbox", default="",
                    help="Serve only this roster mailbox (must still carry intake_route).")
    ap.add_argument("--max-messages", type=int, default=DEFAULT_MAX_MESSAGES,
                    help=f"Cap per mailbox per run (default {DEFAULT_MAX_MESSAGES}).")
    ap.add_argument("--bootstrap-days", type=int, default=DEFAULT_BOOTSTRAP_DAYS,
                    help=f"First-run lookback when no watermark exists (default {DEFAULT_BOOTSTRAP_DAYS}).")
    args = ap.parse_args(argv)
    _setup_logging()

    if not os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        log.error("GOOGLE_SERVICE_ACCOUNT_JSON is not set -- cannot read the mailbox.")
        return 2

    rows = load_intake_mailboxes()
    if args.mailbox:
        want = args.mailbox.strip().lower()
        rows = [r for r in rows if str(r.get("email") or "").strip().lower() == want]
    if not rows:
        log.warning("no roster row carries %s=%s%s -- nothing to sweep", INTAKE_ROUTE_KEY,
                    INTAKE_ROUTE_KNOWLEDGE_REVIEW,
                    f" (filter {args.mailbox})" if args.mailbox else "")
        return 0

    sender_index = build_sender_index()
    watermarks = read_watermarks()
    started = time.time()
    exit_code = 0
    queued = 0
    new_marks = dict(watermarks)
    for row in rows:
        s = sweep_mailbox(row, sender_index=sender_index, watermarks=watermarks,
                          apply=args.apply, max_messages=args.max_messages,
                          bootstrap_days=args.bootstrap_days)
        if s["error"]:
            exit_code = 2
        queued += s["outcomes"].get(info_intake.QUEUED, 0) + s["outcomes"].get(
            info_intake.SUPERSEDES, 0)
        log.info("%s: listed=%d processed=%d backlog=%d outcomes=%s skips=%s watermark->%s",
                 s["mailbox"], s.get("listed", 0), s.get("fetched", 0), s.get("backlog", 0),
                 s["outcomes"] or "{}", s["skips"] or "{}",
                 s["new_watermark"] or "(unchanged)")
        if s["new_watermark"]:
            new_marks[s["mailbox"]] = s["new_watermark"]

    if not args.apply:
        log.info("DRY-RUN -- nothing proposed, watermark not written (would be %s)",
                 {k: v for k, v in new_marks.items() if watermarks.get(k) != v} or "unchanged")
        return exit_code

    if new_marks != watermarks:
        write_watermarks(new_marks)
        log.info("watermark written -> %s", watermark_path())
    try:
        from cora import run_marker
        run_marker.write(TASK_NAME, script=Path(__file__).name, ok=exit_code == 0,
                         outputs=queued, outcome="complete" if exit_code == 0 else "partial",
                         elapsed_s=time.time() - started)
    except Exception:  # noqa: BLE001 -- a marker must never fail the lane
        log.debug("run marker write failed", exc_info=True)
    log.info("sweep complete: %d proposal(s) queued", queued)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
