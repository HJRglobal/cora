"""Code #13 slice 5 (cq-7a724ee43964): a captured meeting's recap, offered to the
ORGANIZER as one propose-only card, DM'd to INTERNAL attendees only on their tap.

WHAT THIS IS. After Fireflies finishes a transcript, the 15-minute meeting-ask
poll (`scripts/run_meeting_ask_capture.py`) already reads it. This module adds a
second, independent product of that same read: ONE card to the meeting's
organizer that says "I can DM this recap to these N internal attendees" and
carries two buttons. Nothing is sent until the organizer taps *Share recap*; the
tap is the human act (R2 ruling 2026-09-08: T0 organizer-confirm, propose-only).

THE THREE POPULATION RULES ARE THE SAFETY ARGUMENT, and each is enforced in code
rather than described on the card:

  * INTERNAL means "resolves to a Slack workspace member through the roster".
    An attendee email becomes a recipient ONLY when `data/maps/slack-to-asana.yaml`
    (via `fireflies_connector._load_email_to_slack`, aliases included) maps it to
    a Slack id AND that id is not flagged `external` in org-roles. A domain is
    never sufficient on its own: the scout found three divergent "internal
    domain" frozensets in this repo, none of which yields a Slack id, and the
    live roster carries a mapped staffer under a personal-mail alias -- so a
    domain belt would guess wrong in BOTH directions. The roster is the identity.
  * EXTERNAL attendees never receive anything under any path. They are not
    recipients, their addresses are not written to the card, the store, or the
    ledger, and a tap cannot widen the set (the recipient list is frozen into
    the record at card time, before any human sees it).
  * LEX meetings go to LEX PHI custodians ONLY (D-145): recipients are the
    roster-mapped attendees INTERSECTED with `lex_phi_access._load_custodian_ids()`
    (fail-closed frozenset). An EMPTY custodian set means NO card is built at
    all -- not a card with zero recipients, nothing pending, nothing logged.
    A non-custodian LEX organizer is not shown the recap either: the card routes
    to Harrison (a custodian and the founder). The LEX body additionally runs
    through the existing PHI scrub (`fireflies_action_extractor._scrub_lex_text`).

WHY THE STORE IS AN APPEND-ONLY EVENT LOG. The script mints cards and the always-on
bot resolves taps: two DIFFERENT processes writing one file. A rewritten JSON blob
under a `threading.Lock` is process-local and loses updates across that boundary
(reproduced with two interpreters in `f3e_blog.publish_cards`, whose shape this
copies). Here a writer only ever appends a line; `_fold` replays events into
state and refuses to move a row OUT of a terminal state, so a late `carded` event
cannot resurrect a shared or dismissed card. Tap-vs-tap races are all inside the
bot process and are serialised by `_LOCK` in `claim_for_tap`.

WHY THE F-23 CONFIRM SURFACE IS NOT REUSED. `confirm_cards` stashes live in a
process-local dict with a 600-second TTL, minted inside a bot conversation turn.
A card posted by the script-side runner has no such turn, so an F-23 stash cannot
back it. What IS kept is F-23's SEMANTICS: requester-only authority (the card's
addressee is the only actor who may tap), an atomic claim so a double tap cannot
double-send, and honest terminal states.

IDEMPOTENCY IS PER RECIPIENT, NOT PER CARD. A fan-out of three DMs can succeed on
two and fail on one. If the whole card were then marked FAILED-and-retryable, the
retry would DM the first two again. So every successful DM appends the recipient
to the record's `sent_to`, a retry skips those, and the resolved card narrates
exactly what the ledger shows ("Shared with 2 of 3; 1 failed") -- the S2' honesty
rail: a card may only claim what a ledger row proves.

THE RECAP TEXT IS FIREFLIES' OWN SUMMARY, READ DETERMINISTICALLY -- no model call.
Fields are the ones the ingest query already fetches live (`short_summary`,
`overview`, `gist`, `action_items`). Fireflies exposes NO "decisions" field, so
none is invented. Action items are presented as what they are: an AI draft, a
lead and not a record (the D-054 class). When every summary field is empty the
card says so and offers no button.

D-011 IS UNTOUCHED BY CONSTRUCTION. Authority here answers "is this card addressed
to you", decided in `claim_for_tap`, not "may you approve org canon"
(`review_lanes.can_approve`, Harrison-only). The affordance footer is REGISTERED
in `knowledge_review._CARD_AFFORDANCE_LINES` so a resolved card cannot keep
advertising a dead button (the C4 contract).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable

log = logging.getLogger(__name__)

_LOCK = Lock()
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: The founder. Fallback addressee when the organizer is external, unmapped, or
#: the capture identity itself; and the only non-custodian who may hold a LEX card
#: (he is a custodian). Same fixed id as `lex_phi_access._FOUNDER_ID`.
HARRISON_ID = "U0B2RM2JYJ1"

#: Addresses that appear as "attendees" or "organizer" but are not people to DM.
#: `cora@hjrglobal.com` is the capture seat (the COPY path makes it the organizer
#: of every externally organised meeting -- rollout doc 2026-09-19 sec.4), and the
#: two Fireflies addresses are the notetaker bot under its old and new names.
CAPTURE_IDENTITIES: frozenset[str] = frozenset({
    "cora@hjrglobal.com",
    "notetaker@fireflies.ai",
    "fred@fireflies.ai",
})

#: A recap loses its value fast, so the card ages out in a week (the meeting-ask
#: sibling uses 14 days because an ask is a request, not a digest). Expiry is
#: reported AT THE TAP -- `claim_for_tap` stamps EXPIRED and tells the tapper --
#: there is no background sweep because an untapped card costs nothing.
TTL_DAYS = 7

STATE_PENDING = "PENDING"
#: Held between `claim_for_tap` and the fan-out result. NOT terminal.
STATE_CLAIMED = "CLAIMED"
STATE_SHARED = "SHARED"
STATE_DISMISSED = "DISMISSED"
STATE_EXPIRED = "EXPIRED"
#: Fireflies produced no summary. The card says so and offers nothing; terminal so
#: the next poll cannot card the same silence again.
STATE_NO_SUMMARY = "NO_SUMMARY"
_TERMINAL = frozenset({STATE_SHARED, STATE_DISMISSED, STATE_EXPIRED, STATE_NO_SUMMARY})

#: The card's live-affordance sentence. REGISTERED in
#: knowledge_review._CARD_AFFORDANCE_LINES (see module docstring). Names only the
#: buttons -- this surface has no reaction handler, so it advertises no emoji.
AFFORDANCE_LINE = "Tap *Share recap* or *Not this one* below."

ACTION_SHARE = "meeting_recap_share"
ACTION_DISMISS = "meeting_recap_dismiss"

#: Ledger event names. Every row is a fact about ONE card or ONE recipient.
EVENT_CARDED = "carded"
EVENT_NO_SUMMARY = "no_summary"
EVENT_SENT = "sent"
EVENT_SEND_FAILED = "send_failed"
EVENT_SHARED = "shared"
EVENT_DISMISSED = "dismissed"
EVENT_EXPIRED = "expired"

_MAX_RECAP_CHARS = 1800
_MAX_ACTION_ITEMS_CHARS = 900
_MAX_TITLE_CHARS = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Paths (resolved PER CALL -- a module constant reading os.environ is the
# cq-06f4797db4f1 class: frozen at bot start and it defeats test isolation) ──

def pending_path() -> Path:
    """The card store: an APPEND-ONLY event log (see module docstring)."""
    return Path(os.environ.get(
        "MEETING_RECAP_PENDING_PATH",
        str(_REPO_ROOT / "data" / "state" / "meeting-recap-pending.jsonl"),
    ))


def ledger_path() -> Path:
    """One row per card event and per recipient send. Carries Slack ids only --
    never an email address -- so an external attendee cannot appear here even
    by accident."""
    return Path(os.environ.get(
        "MEETING_RECAP_LEDGER_PATH",
        str(_REPO_ROOT / "logs" / "meeting-recap-ledger.jsonl"),
    ))


# ── Recap text (deterministic read of Fireflies' own summary) ────────────────

def _as_text(value: Any) -> str:
    """Fireflies summary fields arrive as a string OR a list of strings."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v).strip() for v in value if str(v or "").strip())
    return str(value).strip()


def extract_recap(transcript: dict | None) -> dict:
    """{overview, action_items} from the transcript's `summary` block, or empty
    strings. Pure. The overview is the first non-empty of short_summary /
    overview / gist (the ingest's own precedence, `fireflies_connector`
    summary rendering). No field is synthesised: Fireflies has no "decisions"
    field, so none is claimed."""
    summary = (transcript or {}).get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    overview = ""
    for key in ("short_summary", "overview", "gist"):
        overview = _as_text(summary.get(key))
        if overview:
            break
    action_items = _as_text(summary.get("action_items"))
    return {
        "overview": overview[:_MAX_RECAP_CHARS],
        "action_items": action_items[:_MAX_ACTION_ITEMS_CHARS],
    }


def recap_is_empty(recap: dict | None) -> bool:
    r = recap or {}
    return not (str(r.get("overview") or "").strip()
                or str(r.get("action_items") or "").strip())


def scrub_lex(text: str) -> str:
    """The existing LEX PHI scrub, fail-safe. Applied to every LEX recap body even
    though recipients are custodians: defence in depth on an egress surface."""
    if not text:
        return ""
    try:
        from .connectors import fireflies_action_extractor as fae  # noqa: PLC0415
        return fae._scrub_lex_text(text)
    except Exception:  # noqa: BLE001 -- an unavailable scrubber must not leak
        log.warning("meeting_recap: LEX scrub unavailable -- body withheld", exc_info=True)
        return "[recap withheld: PHI scrub unavailable]"


def phi_flagged(text: str) -> bool:
    """Does a NON-LEX recap body trip the broad PHI screen? Fail CLOSED: an
    unavailable screen reads as flagged. (LEX bodies are scrubbed instead --
    `is_any_phi` trips on program vocabulary by construction there.)"""
    try:
        from . import phi_guard  # noqa: PLC0415
        return bool(phi_guard.is_any_phi(text or ""))
    except Exception:  # noqa: BLE001
        log.warning("meeting_recap: PHI screen unavailable -- treating as flagged",
                    exc_info=True)
        return True


# ── Population: who may receive, who holds the card ──────────────────────────

def _attendee_emails(transcript: dict | None) -> list[str]:
    """Lowercased, deduped attendee + participant emails, in transcript order.
    Capture identities (Cora's seat, the Fireflies bot) are dropped here so they
    can never be counted as attendees anywhere downstream."""
    t = transcript or {}
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        e = str(raw or "").strip().lower()
        if not e or "@" not in e or e in seen or e in CAPTURE_IDENTITIES:
            return
        seen.add(e)
        out.append(e)

    for a in (t.get("meeting_attendees") or []):
        if isinstance(a, dict):
            _add(a.get("email"))
    for p in (t.get("participants") or []):
        if isinstance(p, str):
            _add(p)
    return out


def _is_external_role(slack_id: str) -> bool:
    """org-roles `external: true` (guest / outside consultant). A lookup failure
    reads as external -- fail closed on the only belt behind the roster."""
    try:
        from . import org_roles  # noqa: PLC0415
        rec = org_roles.get_role(slack_id)
    except Exception:  # noqa: BLE001
        log.warning("meeting_recap: org_roles unavailable -- treating %s as external",
                    slack_id, exc_info=True)
        return True
    return bool(rec is not None and getattr(rec, "external", False))


def resolve_recipients(
    transcript: dict | None,
    *,
    email_to_slack: dict[str, str] | None,
    exclude_ids: set[str] | frozenset[str] = frozenset(),
    is_lex: bool = False,
    custodian_ids: frozenset[str] | None = None,
) -> dict:
    """The INTERNAL attendees who would receive the recap, as Slack ids.

    Returns {"recipients": [slack_id...], "attendees": N, "unmapped": M,
             "lex_filtered": K}. PURE apart from the org-roles lookup.

    An attendee becomes a recipient only when ALL of these hold:
      1. the roster maps their email to a Slack id (mapping REQUIRED -- a domain
         is never sufficient, see module docstring);
      2. that id is not org-roles `external`;
      3. that id is not in `exclude_ids` (the card's addressee already holds it);
      4. for a LEX meeting, that id is a PHI custodian.

    Counts of the people who were NOT admitted are returned as numbers only.
    No email address leaves this function.
    """
    lookup = {str(k).strip().lower(): str(v).strip()
              for k, v in (email_to_slack or {}).items() if k and v}
    emails = _attendee_emails(transcript)
    recipients: list[str] = []
    unmapped = 0
    lex_filtered = 0
    seen: set[str] = set()
    for email in emails:
        sid = lookup.get(email, "")
        if not sid or _is_external_role(sid):
            unmapped += 1
            continue
        if sid in seen or sid in exclude_ids:
            continue
        if is_lex and (custodian_ids is None or sid not in custodian_ids):
            lex_filtered += 1
            continue
        seen.add(sid)
        recipients.append(sid)
    return {"recipients": recipients, "attendees": len(emails),
            "unmapped": unmapped, "lex_filtered": lex_filtered}


def resolve_addressee(
    transcript: dict | None,
    *,
    email_to_slack: dict[str, str] | None,
    is_lex: bool = False,
    custodian_ids: frozenset[str] | None = None,
) -> tuple[str, str]:
    """(slack_id, routing_reason) for the person who holds the card.

    The organizer (then host) when they resolve to an internal, non-external
    Slack member -- and, for a LEX meeting, a custodian. Otherwise HARRISON,
    with the reason stated so the card can say why it came to him. The capture
    identity as organizer (the COPY path for externally organised meetings)
    counts as external.
    """
    lookup = {str(k).strip().lower(): str(v).strip()
              for k, v in (email_to_slack or {}).items() if k and v}
    t = transcript or {}
    owner = ""
    for key in ("organizer_email", "host_email"):
        owner = str(t.get(key) or "").strip().lower()
        if owner:
            break
    if not owner or owner in CAPTURE_IDENTITIES:
        return HARRISON_ID, ("the organizer is outside the workspace, so this came "
                             "to you as the founder")
    sid = lookup.get(owner, "")
    if not sid or _is_external_role(sid):
        return HARRISON_ID, ("the organizer is outside the workspace, so this came "
                             "to you as the founder")
    if is_lex and (custodian_ids is None or sid not in custodian_ids):
        return HARRISON_ID, ("this is a Lexington meeting and its organizer is not a "
                             "PHI custodian, so it came to you")
    return sid, ""


# ── Durable store (append-only events) ───────────────────────────────────────

def _append_event(recap_id: str, event: str, fields: dict) -> None:
    """The ONLY write path for card state. One line, one open(): whole-line
    appends interleave safely across the two writer processes."""
    row = {"recap_id": str(recap_id), "event": event, "at": _now_iso()}
    row.update(fields)
    p = pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _fold() -> dict:
    """Replay the event log into {recap_id: record}. A malformed line is skipped
    and counted, never read as an empty store. Terminal is sticky."""
    p = pending_path()
    if not p.exists():
        return {}
    out: dict = {}
    bad = 0
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.error("meeting_recap: card event log unreadable (%s)", exc)
        return {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            key = str(row["recap_id"])
        except Exception:  # noqa: BLE001
            bad += 1
            continue
        cur = out.get(key)
        if cur is None:
            out[key] = {k: v for k, v in row.items() if k != "event"}
            continue
        if cur.get("state") in _TERMINAL and row.get("state") not in _TERMINAL:
            row = {k: v for k, v in row.items() if k not in ("state", "event")}
        cur.update({k: v for k, v in row.items() if k != "event"})
    if bad:
        log.error("meeting_recap: card event log has %d unreadable line(s)", bad)
    return out


def get_record(recap_id: str) -> dict | None:
    rec = _fold().get(str(recap_id or ""))
    return dict(rec) if isinstance(rec, dict) else None


def already_carded(recap_id: str) -> bool:
    """ANY state -- a dismissed or summary-less meeting must never come back on
    the next poll, or the push becomes a nag."""
    return str(recap_id or "") in _fold()


def _ledger(event: str, rec: dict, **extra) -> None:
    """Append-only audit row. Fail-soft but loud. SLACK IDS ONLY: the row carries
    the card key, the addressee id and (per send) the recipient id. It never
    carries an email address, so no attendee -- internal or external -- is
    identified here by anything but a workspace id."""
    try:
        p = ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": int(time.time()),
            "at": _now_iso(),
            "event": event,
            "recap_id": rec.get("recap_id"),
            "entity": rec.get("entity"),
            "is_lex": bool(rec.get("is_lex")),
            "addressee_id": rec.get("addressee_id"),
        }
        row.update(extra)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.error("meeting_recap: ledger write FAILED for %s: %s", rec.get("recap_id"), exc)


def build_record(
    *,
    transcript: dict,
    entity: str,
    is_lex: bool,
    recap: dict,
    recipients: list[str],
    attendee_count: int,
    addressee_id: str,
    routing_reason: str,
    attribution_unreliable: bool,
    meeting_date: str,
) -> dict:
    """The card record as it will be persisted. Recipient ids are FROZEN here,
    before any human sees the card, so a tap cannot widen the set. Contains no
    email address."""
    title = str(transcript.get("title") or "(untitled meeting)").strip()[:_MAX_TITLE_CHARS]
    overview = str(recap.get("overview") or "")
    action_items = str(recap.get("action_items") or "")
    if is_lex:
        overview = scrub_lex(overview)
        action_items = scrub_lex(action_items)
    return {
        "recap_id": str(transcript.get("id") or ""),
        "transcript_id": str(transcript.get("id") or ""),
        "meeting_title": title,
        "meeting_date": str(meeting_date or ""),
        "entity": str(entity or ""),
        "is_lex": bool(is_lex),
        "overview": overview,
        "action_items": action_items,
        "transcript_url": str(transcript.get("transcript_url") or "").strip(),
        "recipients": list(recipients),
        "attendee_count": int(attendee_count),
        "addressee_id": str(addressee_id or ""),
        "routing_reason": str(routing_reason or ""),
        "attribution_unreliable": bool(attribution_unreliable),
        "sent_to": [],
        "state": STATE_PENDING if not recap_is_empty(recap) else STATE_NO_SUMMARY,
    }


def record_card(rec: dict, *, dm_channel_id: str, card_message_ts: str) -> dict:
    """Persist a freshly posted card (PENDING, or NO_SUMMARY when it offered
    nothing). Keyed on the recap id so the dedup survives a post that returned
    no ts."""
    row = dict(rec)
    row["dm_channel_id"] = str(dm_channel_id or "")
    row["card_message_ts"] = str(card_message_ts or "")
    row["carded_at"] = _now_iso()
    _append_event(row["recap_id"], EVENT_CARDED, row)
    _ledger(EVENT_NO_SUMMARY if row["state"] == STATE_NO_SUMMARY else EVENT_CARDED,
            row, recipient_count=len(row.get("recipients") or []),
            attendee_count=row.get("attendee_count"))
    return row


def expired(rec: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        return (now - datetime.fromisoformat(rec.get("carded_at", ""))) > timedelta(days=TTL_DAYS)
    except Exception:  # noqa: BLE001 -- an unparseable stamp reads as expired
        return True


def _set_state(recap_id: str, state: str, **fields) -> None:
    payload = {"state": state}
    payload.update(fields)
    _append_event(recap_id, "state", payload)


def claim_for_tap(recap_id: str, actor_id: str, *,
                  now: datetime | None = None) -> tuple[dict | None, str]:
    """CLAIM this card for one tap. (record, refusal).

    The record comes back ONLY when this tap should act, and by then the row is
    already CLAIMED, so a second tap cannot also act. Atomic under `_LOCK` --
    both taps arrive in the bot process. THE ADDRESSEE IS THE AUTHORITY; an
    empty stored addressee is a refusal, not a wildcard.
    """
    actor = str(actor_id or "").strip()
    with _LOCK:
        rec = get_record(recap_id)
        if rec is None:
            return None, ("I can't find this card any more -- it may predate a "
                          "restart. Nothing was sent.")
        state = str(rec.get("state") or "")
        if state == STATE_NO_SUMMARY:
            return None, "There was no recap to share on this one -- nothing to do."
        if state in _TERMINAL:
            return None, f"Already handled ({state.lower()}) -- nothing more to do."
        if state == STATE_CLAIMED:
            return None, "I'm sending that one already -- give me a second."
        if expired(rec, now):
            _set_state(rec["recap_id"], STATE_EXPIRED, resolved_at=_now_iso())
            _ledger(EVENT_EXPIRED, rec)
            return None, (f"This card aged out after {TTL_DAYS} days, so I didn't "
                          "send anything. Ask me for the meeting's summary if you "
                          "still want it.")
        addressee = str(rec.get("addressee_id") or "")
        if not addressee or (actor and actor != addressee):
            return None, ("This card was addressed to someone else, so I've left "
                          "it alone.")
        if not actor:
            return None, "I couldn't tell who tapped that, so I've left it alone."
        _set_state(rec["recap_id"], STATE_CLAIMED, claimed_at=_now_iso())
        rec["state"] = STATE_CLAIMED
        return rec, ""


def mark_dismissed(rec: dict) -> None:
    _set_state(rec["recap_id"], STATE_DISMISSED, resolved_at=_now_iso())
    _ledger(EVENT_DISMISSED, rec)


def pending_records() -> list[dict]:
    return [dict(r) for r in _fold().values()
            if isinstance(r, dict) and r.get("state") == STATE_PENDING]


# ── Rendering ────────────────────────────────────────────────────────────────

def _sanitize(text: str) -> str:
    """Block Kit section bodies BYPASS the class-level WebClient egress patch
    (D-168), so every block is sanitized at construction. Fail-soft to the raw
    text only if the sanitizer itself is missing -- which the import smoke
    would catch first."""
    try:
        from .slack_egress import sanitize_text  # noqa: PLC0415
        return sanitize_text(text)
    except Exception:  # noqa: BLE001
        log.error("meeting_recap: sanitize_text unavailable", exc_info=True)
        return text


def _recap_body(rec: dict) -> str:
    """The recap itself, as both the organizer preview and the recipient DM carry
    it. States what it is -- Fireflies' AI summary -- and never claims more."""
    lines: list[str] = []
    overview = str(rec.get("overview") or "").strip()
    items = str(rec.get("action_items") or "").strip()
    if overview:
        lines.append(overview)
    if items:
        lines.append("")
        lines.append("*Action items (Fireflies' AI draft - a lead, not a record):*")
        lines.append(items)
    url = str(rec.get("transcript_url") or "").strip()
    if url:
        lines.append("")
        lines.append(f"<{url}|Full transcript in Fireflies>")
    if rec.get("attribution_unreliable"):
        lines.append("")
        lines.append("_Speaker attribution on this transcript looked unreliable, so "
                     "who said what may be wrong._")
    return "\n".join(lines)


def build_card_text(rec: dict) -> str:
    """The organizer's card. Names the recipients it WOULD DM (as mentions --
    never an address), states that externals get nothing, previews the recap,
    says nothing has been sent, and ends with the REGISTERED affordance line
    only when there is something to share. No em-dashes (D-109)."""
    title = str(rec.get("meeting_title") or "(untitled meeting)")
    date = str(rec.get("meeting_date") or "unknown date")
    recipients = [str(r) for r in (rec.get("recipients") or []) if r]
    lines = [f"*Meeting recap ready to share* - {title} ({date})"]

    if str(rec.get("state") or "") == STATE_NO_SUMMARY:
        lines.append("Fireflies produced no summary for this meeting, so there is "
                     "nothing for me to share. No buttons on this one.")
        reason = str(rec.get("routing_reason") or "")
        if reason:
            lines.append(f"_Routing: {reason}._")
        return "\n".join(lines)

    if rec.get("is_lex"):
        lines.append("Lexington meeting: only PHI custodians who attended are "
                     "eligible recipients, and the text below has been through "
                     "the PHI scrub.")
    if recipients:
        who = ", ".join(f"<@{r}>" for r in recipients)
        lines.append(f"*Share recap* DMs this to {len(recipients)} internal "
                     f"attendee(s): {who}.")
    else:
        lines.append("No internal attendee to DM on this one.")
    attendees = int(rec.get("attendee_count") or 0)
    outside = max(attendees - len(recipients) - 1, 0)
    if outside:
        lines.append(f"{outside} other attendee(s) are outside the workspace or "
                     "unmapped and will receive nothing from me.")
    lines.append("")
    body = _recap_body(rec)
    if body:
        lines.append(body)
    reason = str(rec.get("routing_reason") or "")
    if reason:
        lines.append("")
        lines.append(f"_Routing: {reason}._")
    lines.append("")
    lines.append("_Nothing has been sent. Sharing is your tap, and only you can tap it._")
    if recipients:
        lines.append(AFFORDANCE_LINE)
    return "\n".join(lines)


def build_card_blocks(rec: dict) -> tuple[str, list[dict]]:
    """(fallback_text, blocks). Sanitized at construction (D-168), chunked, and
    an actions block ONLY when there is a recipient to share with."""
    from . import confirm_cards  # noqa: PLC0415
    text = _sanitize(build_card_text(rec))
    blocks = confirm_cards.chunk_mrkdwn_sections(text)
    recipients = [r for r in (rec.get("recipients") or []) if r]
    if recipients and str(rec.get("state") or "") != STATE_NO_SUMMARY:
        blocks.append({
            "type": "actions",
            "block_id": (f"cora_meeting_recap_actions_{rec.get('recap_id', '')}")[:255],
            "elements": [
                {"type": "button", "style": "primary", "action_id": ACTION_SHARE,
                 "text": {"type": "plain_text", "text": "Share recap"},
                 "value": str(rec.get("recap_id") or "")},
                {"type": "button", "action_id": ACTION_DISMISS,
                 "text": {"type": "plain_text", "text": "Not this one"},
                 "value": str(rec.get("recap_id") or "")},
            ],
        })
    fallback = _sanitize(f"Meeting recap ready to share: {rec.get('meeting_title') or ''}")
    return fallback, blocks


def build_dm_text(rec: dict) -> str:
    """The recipient's DM. Says who shared it (a mention, never an address) and
    carries the same body the organizer previewed."""
    title = str(rec.get("meeting_title") or "(untitled meeting)")
    date = str(rec.get("meeting_date") or "unknown date")
    sharer = str(rec.get("addressee_id") or "")
    lines = [f"*Meeting recap* - {title} ({date})"]
    if sharer:
        lines.append(f"Shared with you by <@{sharer}> because you were an attendee.")
    lines.append("")
    body = _recap_body(rec)
    if body:
        lines.append(body)
    lines.append("")
    lines.append("_This is Fireflies' AI summary of the meeting, forwarded on the "
                 "organizer's tap. Treat it as a lead, not a record._")
    return "\n".join(lines)


def outcome_text(rec: dict, *, sent: int, failed: int, dismissed: bool = False) -> str:
    """The resolved card's outcome line -- narrates ONLY what the ledger shows."""
    if dismissed:
        return ":x: Dropped - I sent nothing."
    total = len(rec.get("recipients") or [])
    if failed == 0 and sent >= total:
        return f":white_check_mark: Shared with {sent} of {total} internal attendee(s)."
    return (f":warning: Shared with {sent} of {total}; {failed} DM(s) failed. "
            "The buttons above still work to retry the rest.")


# ── Fan-out (the human act's consequence) ────────────────────────────────────

def process_share(rec: dict, client) -> tuple[int, int, bool]:
    """DM the recap to every recipient not already sent. (sent, failed, resolved).

    PER-RECIPIENT IDEMPOTENCY: each success is appended to `sent_to` before the
    next send begins, so a retry after a partial failure reaches only the
    remainder. The record's `recipients` list is the frozen population -- this
    function cannot add to it. On any failure the row returns to PENDING (retry
    keeps its buttons); when every recipient has been reached it is SHARED.
    """
    recap_id = str(rec.get("recap_id") or "")
    recipients = [str(r) for r in (rec.get("recipients") or []) if r]
    sent_to = {str(s) for s in (rec.get("sent_to") or []) if s}
    text = _sanitize(build_dm_text(rec))
    try:
        from . import confirm_cards  # noqa: PLC0415
        blocks = confirm_cards.chunk_mrkdwn_sections(text)
    except Exception:  # noqa: BLE001
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    failed = 0
    for sid in recipients:
        if sid in sent_to:
            continue
        try:
            opened = client.conversations_open(users=[sid])
            channel = ((opened.get("channel") or {}).get("id") if isinstance(opened, dict)
                       else "") or ""
            if not channel:
                raise RuntimeError("conversations_open returned no channel id")
            resp = client.chat_postMessage(channel=channel, text=text, blocks=blocks,
                                           unfurl_links=False, unfurl_media=False)
            sent_to.add(sid)
            _append_event(recap_id, EVENT_SENT, {"sent_to": sorted(sent_to)})
            _ledger(EVENT_SENT, rec, recipient_id=sid,
                    dm_message_ts=str((resp or {}).get("ts") or "") if isinstance(resp, dict) else "")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            _ledger(EVENT_SEND_FAILED, rec, recipient_id=sid, error=str(exc)[:200])
            log.warning("meeting_recap: DM to %s failed: %s", sid, exc)

    sent_now = len(sent_to & set(recipients))
    if failed == 0 and sent_now >= len(recipients):
        _set_state(recap_id, STATE_SHARED, sent_to=sorted(sent_to), resolved_at=_now_iso())
        _ledger(EVENT_SHARED, rec, sent_count=sent_now, recipient_count=len(recipients))
        return sent_now, failed, True
    _set_state(recap_id, STATE_PENDING, sent_to=sorted(sent_to))
    return sent_now, failed, False


# ── Runner-side entry (script-side; the bot never calls this) ────────────────

def prepare_card(
    transcript: dict,
    *,
    entity: str,
    email_to_slack: dict[str, str] | None,
    attribution_unreliable: bool,
    meeting_date: str,
    custodian_loader: Callable[[], frozenset[str]] | None = None,
) -> tuple[dict | None, str]:
    """Decide whether this transcript gets a recap card and build its record.
    (record, skip_reason). PURE apart from roster/org-roles/custodian reads.

    Skips (no card, nothing persisted): already carded; LEX with an empty
    custodian set; no internal recipient AND a non-empty recap (a card with
    nobody to share with is noise); a non-LEX body that trips the PHI screen.
    A recap-less meeting still gets its "nothing to share" card once.
    """
    recap_id = str(transcript.get("id") or "")
    if not recap_id:
        return None, "no transcript id"
    if already_carded(recap_id):
        return None, "already carded"

    is_lex = str(entity or "").upper().startswith("LEX")
    custodians: frozenset[str] | None = None
    if is_lex:
        loader = custodian_loader
        if loader is None:
            from . import lex_phi_access  # noqa: PLC0415
            loader = lex_phi_access._load_custodian_ids
        try:
            custodians = frozenset(loader())
        except Exception:  # noqa: BLE001 -- fail closed
            custodians = frozenset()
        if not custodians:
            return None, "LEX meeting with no PHI custodians configured -- no card"

    recap = extract_recap(transcript)
    if not is_lex and not recap_is_empty(recap):
        if phi_flagged(f"{recap['overview']}\n{recap['action_items']}"):
            return None, "recap body tripped the PHI screen -- not carded"

    addressee, reason = resolve_addressee(
        transcript, email_to_slack=email_to_slack, is_lex=is_lex, custodian_ids=custodians)
    pop = resolve_recipients(
        transcript, email_to_slack=email_to_slack, exclude_ids={addressee},
        is_lex=is_lex, custodian_ids=custodians)
    if not pop["recipients"] and not recap_is_empty(recap):
        return None, "no internal attendee to share with -- no card"

    rec = build_record(
        transcript=transcript, entity=entity, is_lex=is_lex, recap=recap,
        recipients=pop["recipients"], attendee_count=pop["attendees"],
        addressee_id=addressee, routing_reason=reason,
        attribution_unreliable=attribution_unreliable, meeting_date=meeting_date,
    )
    return rec, ""


def post_card(client, rec: dict) -> bool:
    """DM the organizer card and record it. Returns False WITHOUT recording on
    failure, so the next poll retries rather than losing the meeting."""
    fallback, blocks = build_card_blocks(rec)
    try:
        opened = client.conversations_open(users=[rec["addressee_id"]])
        channel = ((opened.get("channel") or {}).get("id") if isinstance(opened, dict)
                   else "") or ""
        if not channel:
            raise RuntimeError("conversations_open returned no channel id")
        resp = client.chat_postMessage(channel=channel, text=fallback, blocks=blocks,
                                       unfurl_links=False, unfurl_media=False)
        record_card(rec, dm_channel_id=channel,
                    card_message_ts=str((resp or {}).get("ts") or "") if isinstance(resp, dict) else "")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("meeting_recap: organizer card to %s failed: %s", rec.get("addressee_id"), exc)
        return False
