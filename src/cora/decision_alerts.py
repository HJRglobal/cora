"""C8 (cq-c3454e25f7cf): a stalled-decision alert you can answer in-thread.

THE 8/19 INCIDENT, reconstructed from live artifacts. The daily 14:00
`Cora - Due Date Escalation` pass 2 DM'd Harrison "Stalled P1 decision (untouched
>30d) -- AI Summit revenue: which entity books it (HJR Productions vs Harrison
Rogers LLC)?". He replied in-thread with the deciding word: "HJR productions".
The log records `dm_qa routed ... thread=True text=HJR productions` followed by
`thread_history: fetched 0 turns` -- 15 context-free characters to Haiku, which
answered with clarifying questions. The item stayed stalled until it was ruled by
hand five days later.

TWO INDEPENDENT DEFECTS PRODUCED THAT, both real:

  NO ALERT HAD AN IDENTITY. `_send_dm` threw away chat_postMessage's response and
  persisted only a throttle hash -- no ts, no channel, no topic. Nothing anywhere
  in the repo could recognise a reply as being TO a stalled-decision alert,
  because no alert was identifiable. (The same discard exists in
  channel_synthesis and strategy_memo.)

  ROUTING HAD NO CLAIMANT. The DM branch tries knowledge-check capture, gap-ask
  capture, Tier-2 retrieval, the shift scheduler, then plain Q&A. A decision-alert
  thread matched nothing, so it fell to Q&A.

This module supplies the missing identity and the matching half, deliberately as
a NARROWER copy of gap_autofill's ask lifecycle -- which already implements this
exact shape (mint an id, capture the post ts, persist PENDING, match a reply,
route to a gate).

THREADED-ONLY, ON PURPOSE. A match requires thread_ts == the alert's own ts. The
alert is always the thread parent, so a threaded match is unambiguous, and
refusing to claim top-level DMs keeps this out of the crowded greedy-capture
contest the DM branch already documents.

NOTHING HERE WRITES CANON. decisions-pending.md is read-only to all of Cora and
stays that way (D-011). The confirm path records the answer and stops re-alerting;
the durable edit remains Harrison's.
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

log = logging.getLogger(__name__)

_LOCK = Lock()
_DEFAULT_PATH = (Path(__file__).resolve().parents[2] / "data" / "state"
                 / "decision-alert-pending.json")

# Matches the pass-2 re-alert throttle: at most one live alert per topic, so a
# stale record can never shadow a fresh alert on the same decision.
TTL_DAYS = 7

STATE_PENDING = "PENDING"
STATE_ANSWERED = "ANSWERED"
STATE_DECLINED = "DECLINED"
STATE_REFUSED_PHI = "REFUSED_PHI"
_TERMINAL = frozenset({STATE_ANSWERED, STATE_DECLINED, STATE_REFUSED_PHI})

# Phrases that decline rather than decide. Mirrors gap_autofill's decline
# handling: "not my area" must leave the item open, not close it.
_DECLINE_RE = re.compile(
    r"^\s*(?:no idea|not sure|dunno|don'?t know|not my (?:area|call)|"
    r"still thinking|later|skip|hold off|park (?:it|this))\b",
    re.IGNORECASE,
)


def _path() -> Path:
    """Resolved PER CALL, never snapshotted at import -- a module-level constant
    reading os.environ is the cq-06f4797db4f1 class, and it silently defeats
    test isolation."""
    return Path(os.environ.get("DECISION_ALERT_STATE_PATH") or _DEFAULT_PATH)


def topic_key(topic: str) -> str:
    """Stable id for a decision topic. hashlib, never the builtin hash() -- that
    is siphash-randomized per interpreter and would give a different key every
    run (the C6 defect, in a module that would have inherited it)."""
    norm = re.sub(r"[^a-z0-9]+", " ", str(topic or "").lower()).strip()
    return hashlib.md5(norm.encode()).hexdigest()[:16]


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
        log.warning("decision_alerts: state write failed", exc_info=True)


def record_alert(*, topic: str, severity: str, entity: str, owner: str,
                 surfaced: str, dm_channel_id: str, alert_message_ts: str,
                 target_user_id: str) -> dict:
    """Persist a PENDING alert so a reply to it can be recognised. Returns the
    record (also returned on a write failure, so the caller never branches on
    bookkeeping)."""
    rec = {
        "topic_key": topic_key(topic),
        "topic": str(topic)[:400],
        "severity": str(severity or ""),
        "entity": str(entity or ""),
        "owner": str(owner or ""),
        "surfaced": str(surfaced or ""),
        "dm_channel_id": str(dm_channel_id or ""),
        "alert_message_ts": str(alert_message_ts or ""),
        "target_user_id": str(target_user_id or ""),
        "alerted_at": datetime.now(timezone.utc).isoformat(),
        "state": STATE_PENDING,
    }
    with _LOCK:
        data = _load()
        data[rec["alert_message_ts"] or rec["topic_key"]] = rec
        _save(data)
    return rec


def _expired(rec: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        return (now - datetime.fromisoformat(rec.get("alerted_at", ""))) > \
            timedelta(days=TTL_DAYS)
    except Exception:  # noqa: BLE001 -- an unparseable stamp reads as expired
        return True


def match_alert_reply(user_id: str, thread_ts: str,
                      now: datetime | None = None) -> dict | None:
    """The PENDING alert this threaded reply answers, or None.

    Requires an exact thread_ts match against the alert's own message ts, from
    the person the alert was sent to, still PENDING and inside the TTL.
    """
    ts = str(thread_ts or "").strip()
    if not ts or not user_id:
        return None
    try:
        rec = _load().get(ts)
        if not isinstance(rec, dict):
            return None
        if rec.get("state") != STATE_PENDING:
            return None
        if rec.get("target_user_id") and rec["target_user_id"] != user_id:
            return None
        if _expired(rec, now):
            return None
        return rec
    except Exception:  # noqa: BLE001 -- capture must never break a DM
        log.warning("decision_alerts: match failed", exc_info=True)
        return None


# A decline is essentially the WHOLE reply, not a prefix of one. "Skip the
# Gilbert store, book it to Warner", "Later this quarter, under HJRP" and "Park
# it under HJR Productions" are ANSWERS that happen to open with a decline word,
# and reading them as declines discarded the decision they carried (D-051). A
# word-count ceiling was not enough -- all three are short. What separates them
# is that a real decline has nothing AFTER the decline phrase.
# Two, not one: "park it for now" and "hold off for today" are declines whose
# trailing filler is not an answer. Every real answer measured carries more
# ("under HJR Productions" = 3, "this quarter, under HJRP" = 4).
_DECLINE_RESIDUE_WORDS = 2


def is_decline(text: str) -> bool:
    """"Not my area" leaves the decision OPEN. Only an actual answer closes it."""
    body = str(text or "").strip()
    m = _DECLINE_RE.match(body)
    if not m:
        return False
    residue = body[m.end():].strip(" .,;:!-").split()
    return len(residue) <= _DECLINE_RESIDUE_WORDS


def mark_state(alert_key: str, state: str, *, answer: str = "") -> bool:
    with _LOCK:
        data = _load()
        rec = data.get(alert_key)
        if not isinstance(rec, dict):
            return False
        rec["state"] = state
        rec["resolved_at"] = datetime.now(timezone.utc).isoformat()
        if answer:
            rec["answer"] = str(answer)[:600]
        data[alert_key] = rec
        _save(data)
    return True


def answered_topic_keys(now: datetime | None = None) -> set[str]:
    """Topics whose alert has been answered, so pass 2 stops re-alerting them.

    Load-bearing: `age_days` keys on the file's "Last touched" line, and an
    in-thread answer never touches that file, so without this the same decision
    re-alerts every 7 days until Harrison hand-edits it -- and the re-ask is the
    exact behaviour this slice exists to remove.
    """
    out: set[str] = set()
    for rec in _load().values():
        if isinstance(rec, dict) and rec.get("state") == STATE_ANSWERED:
            out.add(str(rec.get("topic_key") or ""))
    out.discard("")
    return out


def build_close_preview(rec: dict, answer: str) -> str:
    """The confirm card's text. Names the decision, the answer, and EXACTLY what
    Confirm does -- which is not "close the decision", because nothing in Cora
    may write decisions-pending.md."""
    topic = str(rec.get("topic") or "(unknown decision)")
    sev = str(rec.get("severity") or "")
    return (
        f"*Record your answer to this stalled {sev} decision?*\n"
        f"> {topic[:300]}\n\n"
        f"*Your answer:* {str(answer)[:300]}\n\n"
        f"*Tap Confirm below* -- typing \"yes\" will not do it (this card has no "
        f"tool behind it, so a typed confirm defers to the model and lands "
        f"nowhere).\n"
        f"Confirm records your answer against this decision and stops the daily "
        f"alert re-asking you. It does NOT edit `decisions-pending.md` -- that "
        f"stays your edit (D-011). I'll include the wording in the next cascade "
        f"proposal."
    )
