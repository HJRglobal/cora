"""Founder-only text intents of the dead-channel lane (Code #16 C1, A21) and the
deterministic replies they get. Every branch here returns BEFORE the model and
before code_queue's signal capture, so an archive request can never be narrated by
a zero-tool model turn ("Archived 12 channels" trips no rail: `archived` is not in
the ruled phantom-write lexicon) and can never re-seed a build ask (the 9/20
three-captures incident).

  * ``looks_like_archive_ask`` -- the start-anchored grammar: "archive the dead
    channels" (+ vocative, polite modal, determiner, a deadness adjective, "slack",
    a short tail). -> the scan.
  * ``looks_like_archive_attempt`` -- an ``archive`` verb + ``channel(s)`` or a
    ``<#C...>`` token that FAILED the grammar ("archive #old-promo", "archive the
    dead channels except #x"). -> a refusal naming the one thing that works.
  * ``looks_like_archive_status`` -- an interrogative + ``archiv*`` + channel / card /
    proposal. -> a store/ledger-backed status line, never a model turn, never a
    forced tool (lesson 83: a forced read is itself a tool_use).
  * ``looks_like_live_followup`` -- while a card is live in this DM, a typed "archive
    them / all / those / the list", or a bare yes / ok / go ahead / do it within 30
    minutes of the card. -> "typed replies don't act".

Every predicate first collapses whitespace and caps the input (a real ask is short),
so no pattern can backtrack on a 40k-space message (each is also timed in tests).
"""
from __future__ import annotations

import html
import re
import time
from typing import Any

MAX_CHARS = 300
FOLLOWUP_WINDOW_S = 30 * 60

_MENTION_TOKEN_RE = re.compile(r"<@[A-Za-z0-9_]{2,24}(?:\|[^>\n]{0,40})?>")
_CHANNEL_TOKEN_RE = re.compile(r"<#C[A-Z0-9]{6,24}(?:\|[^>\n]{0,80})?>")
_LIST_MARKER_RE = re.compile(r"\A[-*•>]{1,3} ")

_PREFIX = (r"(?:(?:hey|hi|ok|okay) )?(?:@?cora[,:]? )?"
           r"(?:(?:can|could|would|will) (?:you|u) (?:please )?|please |pls |go ahead and |"
           r"let's |lets )?")
_ASK_RE = re.compile(
    r"\A" + _PREFIX +
    r"archive (?:(?:all of the|all the|all|any|our|my|the) )?"
    r"(?:dead|inactive|stale|unused|quiet|old|abandoned|idle|silent) "
    r"(?:slack )?channels?"
    r"(?: (?:now|please|for me|in slack|thanks|thank you)){0,2}[?.!]*\Z")
_ATTEMPT_VERB_RE = re.compile(r"\A" + _PREFIX + r"archive\b")
_CHANNEL_WORD_RE = re.compile(r"\bchannels?\b")
_STATUS_START_RE = re.compile(
    r"\A(?:(?:hey|hi|ok|okay) )?(?:@?cora[,:]? )?"
    r"(?:did|do|does|has|have|had|were|was|is|are|which|what|what's|whats|how|when|why|"
    r"where|who|any|show me|list|status)\b")
_STATUS_ARCH_RE = re.compile(r"\barchiv")
_STATUS_OBJ_RE = re.compile(r"\b(?:channels?|card|proposal)\b")
_FOLLOWUP_IMPERATIVE_RE = re.compile(
    r"\A" + _PREFIX + r"archive (?:them|all|those|these|the list|all of them|the lot)"
    r"(?: (?:now|please))?[?.!]*\Z")
_BARE_YES_RE = re.compile(
    r"\A(?:yes|yep|yeah|y|ok|okay|go ahead|do it|go|sure|please do|yes please|confirm|"
    r"confirmed|approved|approve)[?.!]*\Z")


def normalize(text: str, *, bot_user_id: str | None = None) -> str:
    """Lowercase, entity-unescaped, Slack mention tokens removed, whitespace collapsed
    to single spaces, capped. Channel tokens survive (lowercased)."""
    t = html.unescape(str(text or "")[: MAX_CHARS * 4])
    t = _MENTION_TOKEN_RE.sub(" ", t)
    t = " ".join(t.split())
    t = _LIST_MARKER_RE.sub("", t)
    t = t.replace("`", "").replace("*", "").strip()
    return t.lower()[:MAX_CHARS]


def _ok(t: str) -> bool:
    return bool(t) and len(t) < MAX_CHARS


def looks_like_archive_ask(text: str) -> bool:
    t = normalize(text)
    return _ok(t) and bool(_ASK_RE.match(t))


def looks_like_archive_attempt(text: str) -> bool:
    raw = str(text or "")[: MAX_CHARS * 4]
    t = normalize(raw)
    if not _ok(t) or not _ATTEMPT_VERB_RE.match(t) or _ASK_RE.match(t):
        return False
    return bool(_CHANNEL_WORD_RE.search(t) or _CHANNEL_TOKEN_RE.search(raw))


def looks_like_archive_status(text: str) -> bool:
    raw = str(text or "")[: MAX_CHARS * 4]
    t = normalize(raw)
    if not _ok(t) or not _STATUS_START_RE.match(t) or not _STATUS_ARCH_RE.search(t):
        return False
    return bool(_STATUS_OBJ_RE.search(t) or _CHANNEL_TOKEN_RE.search(raw))


def followup_shape(text: str) -> str | None:
    """"imperative" ("archive them / all / those / the list") | "affirmative" (a bare
    yes / ok / go ahead / do it) | None. Pure text: the caller reads the store only
    when this is not None."""
    t = normalize(text)
    if not _ok(t):
        return None
    if _FOLLOWUP_IMPERATIVE_RE.match(t):
        return "imperative"
    if _BARE_YES_RE.match(t):
        return "affirmative"
    return None


def looks_like_live_followup(text: str, *, card_ts: float | None, now: float | None = None) -> bool:
    """Only meaningful while a proposal is live in this DM (the caller checks that and
    passes the newest card message's time). An imperative counts for the card's whole
    life; a bare affirmative only within 30 minutes of the card."""
    shape = followup_shape(text)
    if shape is None or card_ts is None:
        return False
    if shape == "imperative":
        return True
    now = time.time() if now is None else float(now)
    return 0 <= now - float(card_ts) <= FOLLOWUP_WINDOW_S


# ── replies (deterministic, code-authored, tier-truthful) ────────────────────
#: "five to ten minutes" is pacing ARITHMETIC, not a measurement (no live scan has run
#: from a Code session): ~100 member channels read at >= 2 s per history call, plus
#: phase 2 + pins for the inactive ones -> roughly 6-8 minutes.
ACK_REPLY = ("Scanning the channels I belong to for 90+ days without a person posting — "
             "metadata only; nothing will be archived by this scan. The proposal card will "
             "follow here in about five to ten minutes.")
CHANNEL_ACK_REPLY = ("Scanning now — the proposal card will arrive in your DM; nothing will be "
                     "archived by this scan.")
SCAN_RUNNING_REPLY = "A scan is already running; its card will arrive here."
SCAN_FAILED_REPLY = ("The dead-channel scan stopped before a card was built — nothing was "
                     "archived. Ask again.")
OFF_REPLY = "The dead-channel lane is switched off (CORA_CHANNEL_ARCHIVE=off). Nothing was scanned."
ATTEMPT_REPLY = ("I only run the full dead-channel scan — nothing was archived. Say 'archive "
                 "the dead channels' here; exceptions are the Keep buttons.")
CATCHUP_DRAFT = "This was a dead-channel request — ask again live; nothing was archived."
EVAL_NOOP = ""


def followup_reply() -> str:
    from . import gates, policy  # noqa: PLC0415
    tier = "T1" if (policy.acting_tier() == "T1" and gates.registry_allows_t1() is True) else "T0"
    if tier == "T0":
        return ("Typed replies don't act — only the card's buttons do. Lane at T0: nothing has "
                "been archived.")
    return ("Typed replies don't act — only the card's buttons do. Nothing is archived without "
            "a tap on the card.")


def status_reply(*, now: float | None = None) -> str:
    """A store/ledger-backed status line. Counts only -- never a channel name."""
    from . import gates, policy  # noqa: PLC0415
    from . import store as st  # noqa: PLC0415
    now = time.time() if now is None else float(now)
    acting_t1 = policy.acting_tier() == "T1" and gates.registry_allows_t1() is True
    lane = ("T1 (approve-then-act: a tap on the card posts a notice, then archives)" if acting_t1
            else "T0 (proposal cards only — nothing is archived at T0)")
    if policy.is_demoted():
        lane += "; DEMOTED — the nightly monitor found an archive it could not attribute to a tap"
    f = st.fold(now=now)
    ledger = st.read_ledger()
    if not f.ok or ledger is None:
        return (f"Dead-channel lane: {lane}. I couldn't read the proposal store or the archive "
                "ledger just now, so I can't give counts.")
    archived = sum(1 for r in ledger if r.get("event") == "outcome"
                   and str(r.get("outcome") or "").startswith("archived"))
    marked = kept = unknown = 0
    for p in f.proposals.values():
        for cid in p.rows_by_cid:
            s = p.state_of(cid)
            marked += s == st.AGREED
            kept += s == st.KEPT
            unknown += s == st.UNKNOWN
    last = f.latest()
    if last is None:
        card = "no proposal card yet"
    else:
        from .cards import _date  # noqa: PLC0415
        card = (f"last card {_date(last.created)} ({len(last.rows)} listed"
                + (", could not complete" if last.blind else "") + ")")
    return (f"Dead-channel lane: {lane}. {card[0].upper() + card[1:]}. Marked to archive {marked} · "
            f"kept {kept} · archived {archived} (ledger-backed) · outcome unknown {unknown}. "
            "Slack archives are reversible: anyone in the channel can unarchive it from the "
            "channel settings.")


def live_card_message_ts(dm_channel: str, *, now: float | None = None) -> set[str]:
    """Every page message ts of the LIVE proposal delivered in *dm_channel* (a threaded
    follow-up counts only inside one of these threads)."""
    from . import store as st  # noqa: PLC0415
    now = time.time() if now is None else float(now)
    try:
        p = st.fold(now=now).live_proposal(now)
    except Exception:  # noqa: BLE001
        return set()
    if p is None or (dm_channel and p.dm_channel() != dm_channel):
        return set()
    return {str(i.get("message_ts")) for i in p.pages.values() if i.get("message_ts")}


def live_card_ts(dm_channel: str, *, now: float | None = None) -> float | None:
    """The newest card message time of a LIVE proposal delivered in *dm_channel*."""
    from . import store as st  # noqa: PLC0415
    now = time.time() if now is None else float(now)
    try:
        f = st.fold(now=now)
    except Exception:  # noqa: BLE001
        return None
    p = f.live_proposal(now)
    if p is None or (dm_channel and p.dm_channel() != dm_channel):
        return None
    stamps = []
    for info in p.pages.values():
        try:
            stamps.append(float(info.get("message_ts") or 0))
        except (TypeError, ValueError):
            continue
    return max(stamps) if stamps else p.created
