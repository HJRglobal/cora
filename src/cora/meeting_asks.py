"""S3 (cq-f52c6b691127): an explicit in-meeting ask to Cora becomes a PROPOSE-ONLY card.

WHAT THIS IS AND WHY IT IS A NARROW POPULATION. Someone in a recorded meeting says
"Cora, make a task to send Larry the deck." Today that sentence lands in the KB as
prose and nothing ever happens: the only route from a meeting to a task is the
`meeting_action_items` PULL tool, which a human has to invoke -- and measured over
the live logs it has been invoked ZERO times in the 16 days to 2026-08-25, with
only two real-user episodes in the entire corpus. The binding constraint is not
surface quality (the per-item confirm cards shipped 8/08 and are live-proven);
it is that nobody asks. So this module pushes -- but it pushes a PROPOSAL, and it
pushes only for sentences where a human explicitly addressed Cora out loud.

THAT NARROWNESS IS THE SAFETY ARGUMENT, not a nicety. D-054 retired the hourly
auto-create push in 2026-06-18 after "Demi's 14 unwanted tasks" -- a push keyed on
Fireflies' AI-generated `summary.action_items`, i.e. on every item the meeting AI
thought it saw. This is keyed on a human saying Cora's name followed by a request,
which is a hand-raise, not an inference. `detect_asks` is measured against the
live transcript corpus rather than reasoned about, and the count it returns there
is the number that has to stay small.

NOTHING HERE EXECUTES ANYTHING. `detect_asks` and the store are pure; the card
carries a Confirm the ADDRESSEE taps, and the tap is what acts. D-136 holds in the
strong form it asks for -- "Ground every promoted item in a quoted line with a
timestamp" -- because every proposal carries the verbatim sentence and its
`start_time` offset, and the card renders both. A proposal whose quoted line is
missing is not proposable and is dropped.

WHY THIS DOES NOT TOUCH `review_lanes.can_approve`. That function's guarantee is
that "there is no code path and no configuration by which a non-Harrison actor
reaches the judgment, decision or operational lanes -- that is what keeps D-011
intact." An S3 card is addressed to whoever spoke in the meeting, so routing it
through the shared proposed-updates ledger would have required widening exactly
that function, and the ledger's own drain would have eaten the rows anyway (an
unrecognised update_type falls to `operational_unsent`, gets a batched text-only
DM to the entity domain owner and is then DISMISSED `routed_to_owner:<id>`). So
this keeps its own durable store and its own action ids, and the authority
question it answers is a different one: not "may you approve org canon" but "is
this proposal addressed to you". D-011 is untouched by construction.

WHAT IT DOES REUSE, deliberately: `knowledge_review.terminal_card_blocks` for the
resolved-card edit, and `_CARD_AFFORDANCE_LINES`, which the affordance footer
below is REGISTERED in. That registration is load-bearing rather than tidy:
`strip_card_affordance` is driven by a closed tuple of literal strings and returns
its input unchanged for any footer it does not know, so an unregistered footer
means the resolved card silently keeps advertising a dead button -- which is the
exact defect C4 shipped to fix.

ATTRIBUTION IS NOT TRUSTED. A transcript records who made a sound, not who holds a
view (D-136), and the diarization canary is firing on 17% of ingested transcripts.
So `resolve_addressee` returns the meeting OWNER, never a named speaker, whenever
the transcript is flagged attribution-unreliable or the speaker cannot be matched
to exactly one attendee. Addressing a card to the wrong colleague is the failure
mode this guards, and the owner is always a defensible recipient because the
meeting is theirs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

_LOCK = Lock()
_DEFAULT_PATH = (Path(__file__).resolve().parents[2] / "data" / "state"
                 / "meeting-ask-pending.json")

#: A card left untapped this long stops being actionable. Longer than the
#: decision-alert TTL (7d) because a meeting ask is not a nag -- it is one card
#: about one sentence, and a week of PTO must not silently discard it. The expiry
#: is reported, never silent.
TTL_DAYS = 14

STATE_PENDING = "PENDING"
#: Held between `claim_for_tap` and the executor's result. NOT terminal: a failed
#: execution returns the row to PENDING so the tap can be retried, which is why
#: this is a distinct state rather than an early ACCEPTED.
STATE_CLAIMED = "CLAIMED"
STATE_ACCEPTED = "ACCEPTED"
STATE_DISMISSED = "DISMISSED"
STATE_EXPIRED = "EXPIRED"
_TERMINAL = frozenset({STATE_ACCEPTED, STATE_DISMISSED, STATE_EXPIRED})

#: The proposable kinds. `task` and `note` have executors; `other` deliberately
#: does not -- see KIND_OTHER below.
KIND_TASK = "task"
KIND_NOTE = "note"
KIND_OTHER = "other"

#: Per-meeting cap. The population is meant to be 0-2 asks per meeting; a
#: transcript that yields more than this is far more likely to be a detector
#: false-positive storm than a meeting in which six separate asks were spoken, and
#: the D-054 incident was precisely a push that sent more cards than a human
#: wanted. Overflow is REPORTED by `cap_overflow`, never silently dropped.
MAX_ASKS_PER_MEETING = 3

#: The card's live-affordance sentence. REGISTERED in
#: knowledge_review._CARD_AFFORDANCE_LINES -- see the module docstring.
AFFORDANCE_LINE = (
    "\U0001F44D Yes, do it · \U0001F44E No, drop it  (or tap a button below)"
)

ACTION_ACCEPT = "meeting_ask_accept"
ACTION_DISMISS = "meeting_ask_dismiss"


# ── Detection ────────────────────────────────────────────────────────────────
#
# Two predicates, and BOTH must agree: an address to Cora, and no Cora-as-subject
# construction. The veto exists because the single most common way Cora's name
# appears in a transcript is someone talking ABOUT her, and D-136's third failure
# mode makes that worse -- in a screen-shared session roughly half of one
# speaker's lines were him reading Cora's output aloud, so the transcript is
# literally accurate and only the attribution is wrong. A detector that fires on
# "Cora said she'd make a task" would mint a card for a task Cora had already been
# told about, addressed to whoever happened to be narrating.

#: Verbs that open a request whose product is a tracked item of work.
_TASK_VERBS = (
    "make", "create", "add", "open", "file", "put", "track", "set up",
    "setup", "remind", "assign", "queue",
)
#: Verbs that open a request whose product is a remembered fact.
#:
#: "log" lives HERE, not in the task list, and the head-noun rule is what makes
#: that safe: "Cora, log that the price moved to $25.15" is a fact and "Cora, log
#: a ticket for the Ellsworth repair" is work, and the object -- not the verb --
#: is what separates them. The first cut had "log" in the task list and a comment
#: claiming it was in both; it was in neither place correctly, and the fact case
#: came out as a proposed Asana task.
_NOTE_VERBS = ("note", "remember", "save", "capture", "record", "jot", "keep", "log")
#: Verbs whose product is COMPOSED OUTPUT -- a draft, a message, a document. These
#: are deliberately NOT executable here; see KIND_OTHER.
_OTHER_VERBS = (
    "draft", "write", "send", "email", "schedule", "book", "reply", "respond",
    "post", "share", "build", "pull", "find", "look up", "check", "summarize",
)

_ALL_VERBS = _TASK_VERBS + _NOTE_VERBS + _OTHER_VERBS
_VERB_ALT = "|".join(sorted((re.escape(v) for v in _ALL_VERBS), key=len, reverse=True))

#: The HEAD noun of the request -- the object the verb takes. Scoped to a
#: 3-word window after the verb, and NOT searched over the whole body.
#:
#: MEASURED AGAINST THE LIVE CORPUS, and the whole-body version was wrong in
#: both directions. "Cora, note to send Hannah a nudge on who can accomplish
#: these quick maintenance tasks" was classified TASK because the word "tasks"
#: appeared 12 words downstream, describing the WORK rather than naming what
#: Cora was asked to produce; and "Cora, make a note to get with Sarah" was
#: classified TASK because the verb "make" is in the task list even though the
#: speaker said the word "note" immediately after it. A request's kind is set by
#: the object the verb takes, so that is the only place worth looking. The window
#: is 3 words because real speech inserts modifiers -- "create a sauna task for
#: Sean" is a live example and needs two.
_HEAD_WINDOW = r"(?:[\w'-]+\s+){0,3}?"
_NOTE_HEAD_RE = re.compile(rf"^\W{{0,3}}{_HEAD_WINDOW}(?P<n>notes?)\b", re.IGNORECASE)
_TASK_HEAD_RE = re.compile(
    rf"^\W{{0,3}}{_HEAD_WINDOW}"
    r"(?P<n>tasks?|tickets?|to-?dos?|action\s+items?|follow[\s-]?ups?|reminders?|asana)\b",
    re.IGNORECASE,
)

#: Cora's ROLE being named, not an ask. She sits in these meetings as the
#: notetaker, so "Cora, note taker." and "Cora note taker" are a human reading
#: the participant list aloud. Measured: 2 of the 22 live detections were exactly
#: this, and both would have become cards.
_NOTETAKER_RE = re.compile(r"^\W{0,3}(?:tak(?:er|ing)|note\s?tak(?:er|ing))\b", re.IGNORECASE)

#: PHRASAL false positives -- a request verb whose next word turns it into
#: something that is not a request for a deliverable. Measured live: "Cora, make
#: sure that ..." was classified TASK, because "make" is a task verb and nothing
#: looked at the word after it. "make sure" is an exhortation, not an instruction
#: to create anything. Enumerated rather than generalised, so a real object is
#: never swallowed.
_PHRASAL_VETO_RE = re.compile(
    r"^\W{0,3}(?:sure|certain|note\s+of\s+that|do\s+that)\b", re.IGNORECASE)

#: Cora ADDRESSED. `cora` must sit at a clause boundary (start of the sentence, or
#: after terminal punctuation, or after a comma/conjunction opener) and be followed
#: -- within a short, bounded window -- by a request head. The window is a bounded
#: character class rather than `.*?` so there is no backtracking surface: this repo
#: has shipped five ReDoS regressions in patterns cleverer than they needed to be,
#: and this one runs over arbitrarily long meeting text.
_ADDRESS_RE = re.compile(
    r"(?:^|(?<=[.?!])|(?<=,\s)|(?<=^\s))"
    r"\s{0,4}(?:hey|ok|okay|alright|so|and|also|then)?\s{0,4}"
    r"\bcora\b[\s,:\-]{0,4}"
    r"(?:please\s{1,3})?"
    r"(?:(?:can|could|would|will)\s{1,3}you\s{1,3}(?:please\s{1,3})?)?"
    rf"(?P<verb>{_VERB_ALT})\b"
    r"(?P<rest>[^.?!]{0,300})",
    re.IGNORECASE,
)

#: Cora as SUBJECT -- the veto. Enumerated rather than "any verb", so a real
#: request never trips it: every entry here is a form in which Cora is the one
#: acting or being discussed, not the one being asked.
_SUBJECT_VETO_RE = re.compile(
    r"\bcora\b\s{1,3}(?:already\s{1,3}|just\s{1,3}|also\s{1,3})?"
    r"(?:said|says|say|posted|posts|sent|sends|told|tells|thinks|thought|"
    r"answered|replied|knows|knew|found|gave|generated|drafted|made|created|"
    r"will|would|should|could|can\'t|cannot|is|was|were|has|have|had|does|did|"
    r"doesn\'t|didn\'t|wasn\'t|isn\'t)\b",
    re.IGNORECASE,
)

#: A question ABOUT Cora ("did Cora get that?", "can Cora do this?") is not an
#: instruction TO Cora. Anchored to the sentence opening.
_ABOUT_QUESTION_RE = re.compile(
    r"^\s{0,4}(?:did|does|do|can|could|will|would|has|have|is|was|should)\s{1,3}cora\b",
    re.IGNORECASE,
)

#: Text that is not a proposable ask body even though the grammar matched.
_EMPTY_REST_RE = re.compile(r"^[\s,:;\-–—]*$")


def classify_kind(verb: str, rest: str) -> str:
    """Which kind of proposal this request is.

    The HEAD NOUN wins over the verb, and whichever head noun appears EARLIER
    wins over the other. "make a note" is a note even though "make" is a task
    verb; "log a task" is work even though "log" is a note verb. The verb only
    decides when the request takes no recognised object ("Cora, note to send
    Hannah a nudge" -> note).
    """
    v = (verb or "").strip().lower()
    body = rest or ""
    note_m = _NOTE_HEAD_RE.match(body)
    task_m = _TASK_HEAD_RE.match(body)
    if note_m and task_m:
        # Both present ("a note about the task"): the nearer object is the one
        # the verb actually takes.
        return KIND_NOTE if note_m.start("n") <= task_m.start("n") else KIND_TASK
    if note_m:
        return KIND_NOTE
    if task_m:
        return KIND_TASK
    if v in _NOTE_VERBS:
        return KIND_NOTE
    if v in _TASK_VERBS:
        return KIND_TASK
    return KIND_OTHER


#: Content words that carry no request on their own. A body made only of these
#: is not an ask -- it is a fragment.
_HOLLOW_BODY_WORDS = frozenset({
    # self-reference and pronouns
    "cora", "me", "us", "it", "that", "this", "there", "them", "him", "her",
    # discourse filler
    "please", "ok", "okay", "yeah", "yes", "no", "just", "actually", "really",
    # function words -- without these, "for Cora" survives as a body, because
    # "for" is a content word to a naive word count. Measured: "Cora, note for
    # Cora." produced a card. Real bodies keep their nouns and verbs, so
    # stripping function words costs them nothing: "for adding legal fees and
    # damages" -> {adding, legal, fees, damages}.
    "a", "an", "the", "for", "to", "of", "on", "in", "at", "and", "or", "with",
    "about", "from", "by", "is", "was", "be",
})


def _is_hollow(body: str) -> bool:
    """True when the request body carries nothing to act on.

    A WORD FLOOR ALONE WAS NOT ENOUGH, and a floor high enough to stop the junk
    would have stopped real asks -- the failure #6 shipped twice (a quality floor
    that rejected "$25.15" and "Net 30" as insubstantial). Measured here instead:
    the live junk is "note taker", "note for Cora" and "for Cora" -- bodies whose
    only content word names Cora or her role -- while the shortest legitimate live
    ask is "a catalog of our recurring monthly journal entries" (7 words). So the
    test is SUBSTANCE, not length: strip the hollow words and require that
    something is left.
    """
    words = [w for w in re.findall(r"[\w'-]+", str(body or "").lower())]
    if not words:
        return True
    return not [w for w in words if w not in _HOLLOW_BODY_WORDS]


def _clean_body(rest: str) -> str:
    """The request body, trimmed of the connective tissue a spoken request opens
    with. Never rewrites meaning -- only strips leading filler and whitespace."""
    body = re.sub(r"\s+", " ", str(rest or "")).strip(" ,:;-–—")
    body = re.sub(
        r"^(?:a|an|the|me\s+to|us\s+to|me|us|that|this|it|of)\b[\s,]*",
        "", body, flags=re.IGNORECASE,
    ).strip(" ,:;-")
    return body


def detect_asks(sentences: list[dict] | None) -> list[dict]:
    """Explicit Cora-directed asks in a transcript's sentences.

    Each returned ask carries the VERBATIM sentence and its `start_time`, which is
    what makes D-136's grounding requirement satisfiable at the card. A sentence
    with no usable text or no timestamp is skipped: an ungroundable proposal is
    not proposable.

    Pure -- no network, no store, no clock. Everything downstream of this is a
    decision about a list of dicts.
    """
    out: list[dict] = []
    for s in (sentences or []):
        if not isinstance(s, dict):
            continue
        text = str(s.get("text") or "").strip()
        if not text or "cora" not in text.lower():
            continue
        if _SUBJECT_VETO_RE.search(text) or _ABOUT_QUESTION_RE.search(text):
            continue
        match = _ADDRESS_RE.search(text)
        if not match:
            continue
        rest = match.group("rest") or ""
        if _EMPTY_REST_RE.match(rest):
            continue
        stripped_rest = rest.lstrip()
        if _NOTETAKER_RE.match(stripped_rest) or _PHRASAL_VETO_RE.match(stripped_rest):
            continue
        body = _clean_body(rest)
        if not body or _is_hollow(body):
            continue
        start = s.get("start_time")
        # `0.0` is a legitimate offset (an ask in the first second), so this
        # tests for PRESENCE, not truthiness.
        if not isinstance(start, (int, float)) or isinstance(start, bool):
            continue
        out.append({
            "kind": classify_kind(match.group("verb"), rest),
            "verb": (match.group("verb") or "").strip().lower(),
            "body": body[:300],
            "quoted_line": text[:600],
            "speaker": str(s.get("speaker_name") or "").strip(),
            "start_time": float(start),
            "sentence_index": s.get("index"),
        })
    return out


def cap_overflow(asks: list[dict]) -> tuple[list[dict], int]:
    """(the asks to card, how many were held back). The caller REPORTS the
    overflow -- a silently truncated push is how a cap reads as coverage."""
    items = list(asks or [])
    if len(items) <= MAX_ASKS_PER_MEETING:
        return items, 0
    return items[:MAX_ASKS_PER_MEETING], len(items) - MAX_ASKS_PER_MEETING


def format_offset(seconds: float | int | None) -> str:
    """`start_time` as the m:ss stamp D-136 asks for. Fireflies returns seconds
    here (unlike `Transcript.duration`, which is MINUTES -- the unit that was
    wrong everywhere until this session)."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "?"
    if total < 0:
        return "?"
    return f"{total // 60}:{total % 60:02d}"


# ── Addressee resolution ─────────────────────────────────────────────────────

def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z]+", " ", str(value or "").lower()).strip()


def match_speaker_to_attendee(speaker: str, attendees: list[dict] | None) -> str:
    """The attendee email for this speaker label, or '' when not confident.

    RESOLVES THROUGH THE ROSTER, NOT THROUGH `displayName`. The first cut matched
    `sentences[].speaker_name` against `meeting_attendees[].displayName`, which is
    dead on live data: measured across real transcripts, `displayName` is None for
    EVERY human attendee -- only the Fireflies notetaker bot carries one. So that
    matcher could never succeed in production and every card silently fell back to
    the meeting owner. Found by running the capture against 25 real meetings, not
    by a test; the fixtures had displayName populated because I wrote them.

    So: speaker name -> canonical roster name (reusing
    `fireflies_action_extractor._match_roster_name`, which was built for exactly
    this problem and carries the anti-substring fix -- no unanchored substring
    rule, because that one mapped "Lex" to "Alex Cordova" and "Ann" to "Hannah
    Grant") -> that person's email via the slack-to-asana map -> and only then a
    CHECK that the email is actually in this meeting's attendee list.

    THAT LAST CHECK IS THE POINT. Resolving a name through a global roster would
    otherwise happily address someone who was never in the room; requiring them to
    be an attendee keeps the old guarantee while making the match work at all.
    """
    target = str(speaker or "").strip()
    if not target:
        return ""
    attendee_emails = {
        str(a.get("email") or "").strip().lower()
        for a in (attendees or []) if isinstance(a, dict)
    }
    attendee_emails.discard("")
    if not attendee_emails:
        return ""
    try:
        from .connectors import fireflies_action_extractor as fae  # noqa: PLC0415
        canonical = fae._match_roster_name(target, fae._roster_names())
    except Exception:  # noqa: BLE001 -- an unavailable roster falls back to the owner
        log.warning("meeting_asks: roster match unavailable", exc_info=True)
        return ""
    if not canonical:
        return ""
    # Canonical roster name -> every email we know for that person, then keep only
    # the one that was actually in this meeting.
    for email in _roster_emails_for(canonical):
        if email.lower() in attendee_emails:
            return email
    return ""


def _roster_emails_for(canonical_name: str) -> list[str]:
    """Every email the slack-to-asana map knows for this canonical roster name.

    Same map `fireflies_connector._load_email_to_slack` reads, walked in the other
    direction. Aliases included, because the address a person appears under in a
    calendar invite is routinely not their primary.
    """
    out: list[str] = []
    try:
        import yaml  # noqa: PLC0415
        path = (Path(__file__).resolve().parents[2] / "data" / "maps"
                / "slack-to-asana.yaml")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return out
    users = raw.get("users") if isinstance(raw, dict) else None
    rows = users.values() if isinstance(users, dict) else (users or [])
    target = _norm_name(canonical_name)
    for row in rows:
        if not isinstance(row, dict):
            continue
        # `display_name` is the key this map actually uses (verified against the
        # live file); `name` is what I assumed and it does not exist there.
        if _norm_name(row.get("display_name") or row.get("name")) != target:
            continue
        primary = str(row.get("asana_email") or "").strip()
        if primary:
            out.append(primary)
        for alias in (row.get("email_aliases") or []):
            alias = str(alias or "").strip()
            if alias:
                out.append(alias)
    return out


def meeting_owner_email(transcript: dict | None) -> str:
    """The meeting's owner: organizer first, then host. The always-defensible
    recipient -- the meeting is theirs even when nothing else is knowable."""
    t = transcript or {}
    for key in ("organizer_email", "host_email"):
        value = str(t.get(key) or "").strip()
        if value:
            return value
    return ""


def resolve_addressee(
    ask: dict,
    transcript: dict | None,
    *,
    attribution_unreliable: bool,
    email_to_slack: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """(slack_id, email, why) for the person this card goes to.

    ORDER MATTERS AND THE UNRELIABLE CHECK IS FIRST. When the canary has flagged
    the transcript, the speaker labels are the thing under suspicion, so they are
    not consulted at all -- not even as a hint. 17% of ingested transcripts are
    flagged, so this is the common path, not an edge case.

    Returns ('', '', why) when nobody is addressable; the caller then proposes
    nothing, because a card with no recipient is not a proposal.
    """
    lookup = {str(k).strip().lower(): v for k, v in (email_to_slack or {}).items()}
    owner = meeting_owner_email(transcript)
    owner_sid = lookup.get(owner.lower(), "")

    if attribution_unreliable:
        return (owner_sid, owner,
                "attribution unreliable on this transcript -- addressed to the "
                "meeting owner rather than a named speaker")

    attendees = (transcript or {}).get("meeting_attendees") or []
    has_attendees = any(
        str(a.get("email") or "").strip()
        for a in attendees if isinstance(a, dict)
    )
    email = match_speaker_to_attendee(ask.get("speaker", ""), attendees)
    if not email:
        # SAY WHICH CASE FIRED. Measured on live data, the two fallbacks look
        # identical to a reader and mean different things: a personal recording
        # carries NO attendee list at all (nothing to match against), while a
        # calendar meeting can have a speaker who is not on its invite. Reporting
        # both as "did not match exactly one attendee" told the reader something
        # false about the first.
        reason = ("this recording has no attendee list, so I couldn't confirm who "
                  "spoke -- addressed to the meeting owner" if not has_attendees
                  else "the speaker isn't on this meeting's attendee list -- "
                       "addressed to the meeting owner")
        return (owner_sid, owner, reason)
    sid = lookup.get(email.lower(), "")
    if not sid:
        return (owner_sid, owner,
                "speaker has no Slack mapping -- addressed to the meeting owner")
    return (sid, email, "addressed to the speaker who made the ask")


# ── Durable store ────────────────────────────────────────────────────────────

def _path() -> Path:
    """Resolved PER CALL. A module-level constant reading os.environ is the
    cq-06f4797db4f1 class -- frozen at bot start, and it defeats test isolation."""
    return Path(os.environ.get("MEETING_ASK_STATE_PATH") or _DEFAULT_PATH)


def ask_key(transcript_id: str, quoted_line: str, start_time: float | int) -> str:
    """Stable id for one ask, so re-processing a transcript cannot double-card it.

    hashlib, never the builtin `hash()`: that is siphash-randomised per
    interpreter, so a per-process key would make every dedup a silent no-op --
    the C6 defect that filed one vendor quote six times.
    """
    basis = f"{transcript_id}|{_norm_name(quoted_line)}|{int(round(float(start_time or 0)))}"
    return hashlib.md5(basis.encode("utf-8")).hexdigest()[:16]


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 -- absent or malformed reads as empty
        return {}


def _save(data: dict) -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001
        log.warning("meeting_asks: state write failed", exc_info=True)


def already_carded(ask_id: str) -> bool:
    """True when this exact ask already has a record in ANY state.

    Any state, not just PENDING: a dismissed ask must never come back on the next
    poll, or the push becomes the nag D-054 retired.
    """
    return str(ask_id or "") in _load()


def record_card(*, ask_id: str, transcript_id: str, meeting_title: str,
                meeting_date: str, entity: str, kind: str, body: str,
                quoted_line: str, start_time: float, speaker: str,
                addressee_id: str, addressee_email: str, routing_reason: str,
                dm_channel_id: str, card_message_ts: str) -> dict:
    """Persist a PENDING card. Keyed on the ASK id (not the message ts) so the
    dedup survives a Slack post that succeeded without returning a ts."""
    rec = {
        "ask_id": str(ask_id),
        "transcript_id": str(transcript_id),
        "meeting_title": str(meeting_title)[:300],
        "meeting_date": str(meeting_date or ""),
        "entity": str(entity or ""),
        "kind": str(kind or ""),
        "body": str(body)[:300],
        "quoted_line": str(quoted_line)[:600],
        "start_time": float(start_time or 0),
        "speaker": str(speaker or ""),
        "addressee_id": str(addressee_id or ""),
        "addressee_email": str(addressee_email or ""),
        "routing_reason": str(routing_reason or ""),
        "dm_channel_id": str(dm_channel_id or ""),
        "card_message_ts": str(card_message_ts or ""),
        "carded_at": datetime.now(timezone.utc).isoformat(),
        "state": STATE_PENDING,
    }
    with _LOCK:
        data = _load()
        data[rec["ask_id"]] = rec
        _save(data)
    return rec


def find_by_message_ts(message_ts: str) -> dict | None:
    """The record whose card is this Slack message, or None."""
    ts = str(message_ts or "").strip()
    if not ts:
        return None
    for rec in _load().values():
        if isinstance(rec, dict) and str(rec.get("card_message_ts") or "") == ts:
            return rec
    return None


def expired(rec: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        return (now - datetime.fromisoformat(rec.get("carded_at", ""))) > timedelta(days=TTL_DAYS)
    except Exception:  # noqa: BLE001 -- an unparseable stamp reads as expired
        return True


def claim_for_tap(ask_id: str, actor_id: str, *, message_ts: str = "",
                  now: datetime | None = None) -> tuple[dict | None, str]:
    """CLAIM this card for one tap. (record, refusal).

    The record comes back ONLY when this tap should act, and by then the row has
    already been moved to CLAIMED, so a second tap cannot also act.

    KEYED ON THE ASK ID, WHICH THE BUTTON CARRIES IN ITS `value`. The first cut
    keyed on the card's Slack message ts, which is strictly worse for no benefit:
    it makes every tap depend on `body["message"]["ts"]` still equalling the ts
    that `chat_postMessage` returned when the card was posted, so a card that is
    ever edited, re-posted or threaded orphans every button on it -- while the
    authoritative id was sitting unused in the payload the whole time. The live,
    proven sibling (`gap_autofill.process_decline_tap`) keys on its own ask id for
    exactly this reason. `message_ts` is kept as a FALLBACK for a card posted
    before the value was carried.

    THE WHOLE CHECK-AND-CLAIM IS ATOMIC, under the same lock the writes take.
    Without that, two fast taps both read PENDING and both execute -- and for a
    task ask that means TWO Asana tasks from one card, which is the
    button-tap-race class this repo has already shipped twice
    (cq-883878e81274 cross-bound stashes, cq-056a3a4de2f7 the edit race).

    THE ADDRESSEE IS THE AUTHORITY, and it is decided here rather than by the
    caller. A card proposes an action about the tapper's OWN meeting ask, so being
    its addressee is the permission. That is a different question from
    `review_lanes.can_approve` (which governs org canon and stays Harrison-only),
    and keeping it in a different function is what leaves D-011 untouched.
    """
    actor = str(actor_id or "").strip()
    with _LOCK:
        data = _load()
        key = str(ask_id or "").strip()
        rec = data.get(key) if key else None
        if not isinstance(rec, dict) and message_ts:
            # Fallback for a card posted before the id rode in the button value.
            for candidate in data.values():
                if (isinstance(candidate, dict)
                        and str(candidate.get("card_message_ts") or "") == str(message_ts)):
                    rec, key = candidate, str(candidate.get("ask_id") or "")
                    break
        if not isinstance(rec, dict):
            return None, ("I can't find this card any more -- it may predate a "
                          "restart. Ask me for the meeting's action items and "
                          "I'll pull them live.")

        state = str(rec.get("state") or "")
        if state in _TERMINAL:
            return None, (f"Already handled ({state.lower()}) -- nothing more "
                          "to do.")
        if state == STATE_CLAIMED:
            return None, "I'm working on that one already -- give me a second."
        if expired(rec, now):
            rec["state"] = STATE_EXPIRED
            rec["resolved_at"] = datetime.now(timezone.utc).isoformat()
            data[key] = rec
            _save(data)
            return None, (f"This card aged out after {TTL_DAYS} days, so I didn't "
                          "act on it. Ask me for the meeting's action items if you "
                          "still want it.")
        # AUTHORITY. An EMPTY stored addressee is a REFUSAL, not a wildcard: the
        # sender never records one (it skips an unaddressable ask), so an empty
        # value means a corrupted or hand-edited row, and reading it as "anyone
        # may act" is the fail-OPEN direction on the only authority check there
        # is. The `review_lanes` docstring settled this same argument for the
        # entity field -- an unknown on an authority boundary is a no.
        addressee = str(rec.get("addressee_id") or "")
        if not addressee or (actor and actor != addressee):
            return None, ("This card was addressed to someone else, so I've left "
                          "it alone.")
        if not actor:
            return None, "I couldn't tell who tapped that, so I've left it alone."

        rec["state"] = STATE_CLAIMED
        rec["claimed_at"] = datetime.now(timezone.utc).isoformat()
        data[key] = rec
        _save(data)
        return dict(rec), ""


def mark_state(ask_id: str, state: str, *, outcome: str = "") -> bool:
    with _LOCK:
        data = _load()
        rec = data.get(str(ask_id or ""))
        if not isinstance(rec, dict):
            return False
        rec["state"] = state
        rec["resolved_at"] = datetime.now(timezone.utc).isoformat()
        if outcome:
            rec["outcome"] = str(outcome)[:400]
        data[str(ask_id)] = rec
        _save(data)
    return True


def pending_records() -> list[dict]:
    return [r for r in _load().values()
            if isinstance(r, dict) and r.get("state") == STATE_PENDING]


# ── Card rendering ───────────────────────────────────────────────────────────

def outcome_text(action: str, kind: str, *, success: bool = True,
                 detail: str = "") -> str:
    """The resolved card's outcome line. NAMES THE STORE the item landed in --
    the C4 rule, and the reason `outcome_text` exists in knowledge_review at all:
    one string for every type there had rendered "Saved to Cora's known-answers"
    over an item that went to the efficiency backlog.
    """
    if action == "DISMISSED":
        return ":x: Dropped -- I didn't create anything."
    if not success:
        return (":warning: I couldn't complete that -- nothing was created. "
                + (detail or "Details in #hjrg-leadership."))
    if kind == KIND_TASK:
        return (":white_check_mark: Created in Asana, assigned to you."
                + (f" {detail}" if detail else ""))
    if kind == KIND_NOTE:
        return (":white_check_mark: Saved to YOUR personal notes -- only you can "
                "retrieve it.")
    return ":white_check_mark: Noted."


def _kind_promise(kind: str) -> str:
    """What tapping Confirm ACTUALLY does, per kind.

    `other` gets no promise and no button, and saying so is the point: the C4
    review found a card whose header, footer and ack all claimed Cora carried out
    a HubSpot note she never writes. A draft/send request is a real ask, but it
    is an egress class this slice does not open, so the card hands it back
    instead of implying a capability.
    """
    if kind == KIND_TASK:
        return ("*Yes* creates ONE Asana task assigned to you, in the project this "
                "meeting's entity routes to. Nothing is assigned to anyone else.")
    if kind == KIND_NOTE:
        return ("*Yes* saves this to YOUR personal notes (only you can retrieve "
                "it). It does not become company canon.")
    return ("I can't act on this one from a card -- drafting and sending are not "
            "something I'll do off a meeting transcript. Bring it to me in a DM "
            "and I'll draft it there, with the usual preview before anything goes out.")


def build_card_text(rec: dict) -> str:
    """The card body. Quotes the line and stamps its offset (D-136), says who it
    was routed to and why, states exactly what Confirm does, and -- for an
    actionable kind -- ends with the REGISTERED affordance line."""
    kind = str(rec.get("kind") or "")
    title = str(rec.get("meeting_title") or "(untitled meeting)")
    date = str(rec.get("meeting_date") or "unknown date")
    offset = format_offset(rec.get("start_time"))
    quoted = str(rec.get("quoted_line") or "").strip()
    body = str(rec.get("body") or "").strip()

    lines = [
        f"*You asked me for something in a meeting* — {title} ({date})",
        f"> [{offset}] {quoted}",
        "",
        f"*What I'd do:* {body}" if body else "",
        _kind_promise(kind),
    ]
    reason = str(rec.get("routing_reason") or "")
    if reason and "the speaker who made the ask" not in reason:
        lines.append(f"_Routing: {reason}._")
    lines.append(
        "_I have not done anything yet. This is a proposal from the meeting "
        "transcript, which is a lead and not a record._"
    )
    if kind in (KIND_TASK, KIND_NOTE):
        lines.append(AFFORDANCE_LINE)
    return "\n".join(x for x in lines if x != "")
