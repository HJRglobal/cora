"""Daily Personalized Knowledge Check -- pilot v1 (design 2026-08-11, APPROVED).

One personalized DM per weekday to each person in a locked 13-person roster,
grounded in a REAL gap in Cora's knowledge of their role. Their free-text answer
is restated back to them on a one-tap card, and only what they confirm is
written.

    ASK -> CAPTURE -> CONFIRM-BACK -> PROMOTE

WHY THIS IS NOT A QUIZ: staff are the knowledge SOURCE. Every question traces to
something Cora demonstrably does not know -- either a KPI the Instrumentation
ledger marks "attested" (Tier 1, data/maps/knowledge-check-roster.yaml) or a real
unresolved kb_miss scoped to their domain (Tier 2). There is deliberately NO
Tier 3: when a person has neither, they are SKIPPED. A model-invented question is
the exact failure class this design rejects, so no code path here can produce one.

THE THREE HARD RULES
  1. Never manufacture a question. Selection reads a fixed bank or a real logged
     gap; there is no generative path. `skipped_no_gap` is a correct outcome.
  2. Never write what the person did not confirm. CAPTURE stages; only a Confirm
     (or an Edit, whose text becomes the fact) promotes.
  3. Never silently overwrite canon. A promote whose key collides with a LIVE
     known-answers entry carrying different content is detected DETERMINISTICALLY
     here (never by model judgment) and routed to the decisions lane per D-128.

STATE IS AN APPEND-ONLY EVENT LOG. `data/state/knowledge-check-events.jsonl` is
written by TWO processes -- the scheduled runner (ASK) and the always-on bot
(CAPTURE / CONFIRM / PROMOTE). A read-modify-write JSON blob would let whichever
wrote last clobber the other, so every actor only ever APPENDS one line and the
effective state is a fold. Consequence of D-096's lesson ("fold is last-write-
wins, so a late event RESURRECTS a terminal row"): the fold here makes terminal
states STICKY -- once a cycle reaches PROMOTED/HELD/SKIPPED/EXPIRED/FAILED, no
later event can move it. Events after a terminal state are still recorded for
audit; they just cannot change the outcome.

IDEMPOTENT SENDS. The (person, date) ledger is checked BEFORE every send, and a
RESERVATION is appended BEFORE the Slack call. A cycle in ANY state -- including
terminal -- counts as "already handled today", so a re-run, a double-fired task,
or a mid-roster crash can never produce a second DM to the same person on the
same day. The deliberate trade: a crash between reserve and send costs that
person one day's question rather than risking a duplicate. `reserved_never_sent`
is surfaced in the run report so that loss is visible, never silent.

LEX POSTURE -- READ BEFORE CHANGING. Harrison's explicit, informed decision
(2026-08-11, after the PHI-egress scope was named and escalated) is that LEX
participants are treated IDENTICALLY to every other entity in this build: no PHI
scrub, no PHI gating, on the questions asked or the ANSWERS STORED. That posture
lives in exactly one place -- PHI_GATE_ANSWERS below -- so reversing it is one
flip. Be precise about the scope of that claim: Tier-2 QUESTION SELECTION
additionally inherits gap_autofill.should_escalate's own PHI/LEX screens (see the
Tier-2 section), a separate pre-existing policy this flag does not control. Those
screens guard re-broadcasting a THIRD PARTY's logged text, which is not what
Harrison's decision was about, so they are deliberately left standing.

WHERE A CONFIRMED ANSWER ACTUALLY GOES (the real blast radius, enumerated here
because the decision record named only the first two): design/known-answers/
{entity}.md -- which in production resolves to the G: Drive mount, so it is also
CLOUD-SYNCED -- plus the vector KB that ingests those curated files, plus the
Airtable Training Log (third-party SaaS), plus the append-only event log. All but
the event log are behind the flip; the event log copy is written at CAPTURE, so
phi_blocked runs there too.

This module deliberately does NOT reuse gap_autofill.apply_known_answer: that
function's PHI refusal is load-bearing for the mining path, and adding a bypass
parameter would weaken a shared writer for every one of its callers.
Escalation record: 00-Founder/projects/build-personalized-daily-knowledge-check/
_notes/2026-08-11_fndr_ESCALATION-lex-phi-egress-scope.md (RESOLVED).

CORA_KNOWLEDGE_CHECK=off|dry|on -- off (default) is the full kill switch.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Arizona never observes DST, so a fixed offset is exact (same idiom as
# delegated_work._AZ_TZ / decision_inbox._az_date).
_AZ_TZ = timezone(timedelta(hours=-7))

# ── Tunables ────────────────────────────────────────────────────────────────

# An item is re-asked at most once per this many days. Answering does NOT
# consume it (spec section 3.1): a Tier-1 answer is a dated status snapshot, so
# the same KPI is a legitimately fresh question a week later.
ITEM_COOLDOWN_DAYS = 7

# How many consecutive re-asks an UNANSWERED item gets before it falls back to
# the full cooldown (kickoff step 2.4 asks for a gentle re-ask; this bounds
# "gentle" so a person who never engages is not asked the same thing daily
# forever). Counted in MISSES: the 1st and 2nd no-response re-ask, the 3rd does
# not.
MAX_UNANSWERED_REASKS = 2

# D-089 operational TTL. A Tier-1 answer describes point-in-time status ("3 PCI
# notices still open"), which is stale within the week -- it is written with an
# explicit expiry so it can never harden into permanent canon. Tier-2 answers
# fill a real durable knowledge gap and carry NO TTL.
ANSWER_TTL_DAYS = 7

# A confirmed answer is appended to an ALWAYS-INJECTED known-answers file and is
# served back through retrieval, so its length is bounded at the door.
MAX_ANSWER_CHARS = 1500

# Per-person send window: sends are staggered across this many minutes so 13
# DMs do not land on one timestamp (spec section 4.1).
STAGGER_MINUTES = 45

STATE_ASKED = "ASKED"
STATE_CAPTURED = "CAPTURED"
STATE_PROMOTED = "PROMOTED"
STATE_HELD = "HELD"            # key collision -> decisions lane; nothing written
STATE_SKIPPED = "SKIPPED"      # the person tapped Skip
STATE_EXPIRED = "EXPIRED"      # no response, or no confirm, by end of day
STATE_FAILED = "FAILED"        # the ask itself could not be delivered
STATE_RESERVED = "RESERVED"    # reservation appended, Slack call not yet returned

# Sticky by construction (see the module docstring's D-096 note). Anything that
# is a real outcome for the day belongs here; only RESERVED/ASKED/CAPTURED are
# still in flight.
TERMINAL_STATES = frozenset(
    {STATE_PROMOTED, STATE_HELD, STATE_SKIPPED, STATE_EXPIRED, STATE_FAILED})

# States that count as "this person has already been handled today" for the
# send ledger. RESERVED is included ON PURPOSE -- that is the whole point of
# reserving before the Slack call.
HANDLED_STATES = TERMINAL_STATES | {STATE_RESERVED, STATE_ASKED, STATE_CAPTURED}

# ── LEX / PHI posture -- ONE switch, per Harrison's 2026-08-11 decision ──────
#
# False (current, decided): answers are written verbatim -- no PHI scrub, no PHI
#   refusal -- for EVERY entity including LEX. This is a deliberate deviation
#   from every other LEX-touching path in this codebase (D-042 / D-059), made by
#   Harrison after the new-egress concern was explicitly named and escalated.
# True: re-arms the same three predicates gap_autofill.apply_known_answer uses at
#   its irreversible write, entity-agnostically.
#
# Flipping this is the ONLY change needed to restore the wall -- there is no
# second copy of this policy anywhere in the module, and a test pins that.
PHI_GATE_ANSWERS = False


def mode() -> str:
    """off | dry | on. Fail-closed to 'off' on anything unrecognized -- same
    idiom as CORA_CONFIRM_BUTTONS / CORA_SEND_LIVE / CORA_LEXICON."""
    v = (os.environ.get("CORA_KNOWLEDGE_CHECK", "off") or "off").strip().lower()
    return v if v in ("off", "dry", "on") else "off"


def enabled() -> bool:
    """True when the capability may touch Slack or write anything at all."""
    return mode() in ("dry", "on")


def live() -> bool:
    """True only in full live mode (sends DMs, performs writes)."""
    return mode() == "on"


# ── Paths ───────────────────────────────────────────────────────────────────

def _roster_path() -> Path:
    return _REPO_ROOT / "data" / "maps" / "knowledge-check-roster.yaml"


def _events_path() -> Path:
    return _REPO_ROOT / "data" / "state" / "knowledge-check-events.jsonl"


def _known_answers_dir() -> Path:
    # Same env override gap_autofill uses, so tests point both writers at one
    # temp dir and the two can be exercised against the same files.
    return Path(os.environ.get("KNOWN_ANSWERS_DIR")
                or _REPO_ROOT / "design" / "known-answers")


# ── Time helpers ────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def az_date(dt: datetime | None = None) -> str:
    """The Arizona calendar date. The whole cadence (one question per person per
    weekday, expiry at end of day) is expressed in AZ local time, not UTC -- a
    UTC date would roll over at 5pm local and split one working day in two."""
    return (dt or _now()).astimezone(_AZ_TZ).date().isoformat()


def is_weekday(dt: datetime | None = None) -> bool:
    return (dt or _now()).astimezone(_AZ_TZ).weekday() < 5


def _days_between(a_date: str, b_date: str) -> int | None:
    """Whole days from a_date to b_date (both YYYY-MM-DD), or None if unparseable."""
    try:
        a = datetime.strptime(a_date, "%Y-%m-%d").date()
        b = datetime.strptime(b_date, "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return None
    return (b - a).days


# ── Roster ──────────────────────────────────────────────────────────────────

_ROSTER_LOCK = Lock()
_ROSTER_CACHE: dict[str, Any] = {"mtime": None, "data": None}


def load_roster(*, force: bool = False) -> list[dict[str, Any]]:
    """The pilot roster, mtime-cached (edit the YAML, no restart needed).

    FAIL-CLOSED on a parse error: keeps the last good roster if there is one, and
    otherwise returns EMPTY -- a broken roster file must never fall back to
    "DM everyone", and an empty roster simply sends nothing.
    """
    path = _roster_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    with _ROSTER_LOCK:
        if not force and _ROSTER_CACHE["data"] is not None and _ROSTER_CACHE["mtime"] == mtime:
            return list(_ROSTER_CACHE["data"])
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = raw.get("roster") or []
        if not isinstance(entries, list):
            raise ValueError("roster: expected a list")
        cleaned: list[dict[str, Any]] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            slack_id = str(e.get("slack_id", "") or "").strip()
            if not slack_id:
                continue
            items = []
            for it in (e.get("items") or []):
                if not isinstance(it, dict):
                    continue
                key = str(it.get("key", "") or "").strip()
                question = re.sub(r"\s+", " ", str(it.get("question", "") or "")).strip()
                if not key or not question or it.get("retired"):
                    continue
                items.append({"key": key, "question": question,
                              "kpi": str(it.get("kpi", "") or "").strip()})
            cleaned.append({
                "slack_id": slack_id,
                "name": str(e.get("name", "") or "").strip(),
                "entity": str(e.get("entity", "FNDR") or "FNDR").strip().upper(),
                "dogfood_only": bool(e.get("dogfood_only")),
                "items": items,
            })
    except Exception:  # noqa: BLE001 -- fail-closed
        log.error("knowledge_check: roster load failed -- keeping last good", exc_info=True)
        with _ROSTER_LOCK:
            return list(_ROSTER_CACHE["data"] or [])
    with _ROSTER_LOCK:
        _ROSTER_CACHE["mtime"] = mtime
        _ROSTER_CACHE["data"] = cleaned
    return list(cleaned)


def pilot_roster() -> list[dict[str, Any]]:
    """The 13 who receive real questions (dogfood entries excluded)."""
    return [p for p in load_roster() if not p.get("dogfood_only")]


def roster_member(slack_id: str) -> dict[str, Any] | None:
    for p in load_roster():
        if p["slack_id"] == slack_id:
            return p
    return None


def validate_roster() -> list[str]:
    """Structural problems with the roster, as human-readable strings.

    Used by the runner (refuses to send on a hard problem) and by the test that
    guards against roster drift. Empty list means clean.
    """
    problems: list[str] = []
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    # (entity, normalized question) -> owner. Collision detection keys on the
    # QUESTION TEXT, and several people share one known-answers file (Matt and
    # Micah are both OSN; Shaun, Jen and Aaron all write to lex.md). Two roster
    # items with the same question in one entity would therefore turn two valid
    # independent observations into a permanent "dispute", with the second
    # person's answer discarded every week. That is a data-only foot-gun -- no
    # code change, no restart -- so the guard belongs here.
    seen_questions: dict[tuple[str, str], str] = {}
    try:
        from . import org_roles
        registry = {r.slack_id for r in (org_roles.all_roles() or [])}
    except Exception:  # noqa: BLE001 -- registry unavailable is not a roster fault
        registry = set()
    for p in load_roster():
        sid = p["slack_id"]
        if sid in seen_ids:
            problems.append(f"duplicate slack_id {sid}")
        seen_ids.add(sid)
        if registry and sid not in registry:
            problems.append(f"{p['name'] or sid}: slack_id not in org-roles.yaml")
        if p["entity"].startswith("BDM"):
            problems.append(f"{p['name']}: BDM is out of scope for this pilot")
        for it in p["items"]:
            if it["key"] in seen_keys:
                problems.append(f"duplicate item key {it['key']}")
            seen_keys.add(it["key"])
            from .known_answers_map import file_for
            qkey = (file_for(p["entity"]), normalize_answer(it["question"]).lower())
            owner = seen_questions.get(qkey)
            if owner and owner != sid:
                problems.append(
                    f"{p['name']} and {owner} share an identical question in "
                    f"{qkey[0]} ({it['key']}) -- their answers would collide")
            seen_questions.setdefault(qkey, sid)
    return problems


# ── Append-only event log ───────────────────────────────────────────────────

_APPEND_LOCK = Lock()


def append_event(event: str, **fields: Any) -> dict[str, Any] | None:
    """Append ONE event line. Never raises (a logging failure must not break a
    DM handler or abort a run mid-roster).

    Append-mode single-line writes are what make this safe across the runner and
    the bot process simultaneously -- there is no read-modify-write anywhere in
    this module's state path, so there is nothing for a concurrent writer to
    clobber.
    """
    row = {"ts": _now_iso(), "event": event}
    row.update({k: v for k, v in fields.items() if v is not None})
    try:
        path = _events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _APPEND_LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
    except Exception:  # noqa: BLE001
        # Returns None on failure so a caller whose next step is IRREVERSIBLE can
        # refuse to take it. The reservation is the case that matters: sending a
        # DM whose ledger row never landed means the next run sees the person as
        # unhandled and DMs them again (D-051 lens-1 HIGH).
        log.error("knowledge_check: event append failed (%s)", event, exc_info=True)
        return None
    return row


# fold_state() runs on the DM hot path -- has_live_cycle fires for EVERY DM from
# every user, roster or not, and a single captured reply folds several times over.
# _read_events parses the whole append-only log from byte 0, and json.loads does
# not release the GIL, so an unmemoized fold burns the bot's shared thread
# (heartbeat, Socket Mode acks, every other user's turn) once per DM and grows
# forever. Memoized on the file's (size, mtime_ns): any append changes both, so a
# stale fold is impossible, and a same-process append invalidates it immediately.
_FOLD_LOCK = Lock()
_FOLD_CACHE: dict[str, Any] = {"key": None, "value": None}


def _events_fingerprint(path: Path) -> tuple[str, int, int] | None:
    """(path, size, mtime_ns). The PATH is part of the key on purpose: keying on
    (size, mtime_ns) alone lets two DIFFERENT event logs written in the same
    filesystem tick with the same length collide and serve each other's fold.
    Benign in production (one path) but a real cross-contamination hazard, and it
    silently broke test isolation the moment it was introduced -- which is how it
    was caught."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_size, st.st_mtime_ns)


def reset_caches_for_tests() -> None:
    with _FOLD_LOCK:
        _FOLD_CACHE["key"] = None
        _FOLD_CACHE["value"] = None


def _read_events() -> list[dict[str, Any]]:
    path = _events_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001 -- a torn line must not kill the fold
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except Exception:  # noqa: BLE001
        log.error("knowledge_check: event read failed", exc_info=True)
    return out


# event name -> the state it moves a cycle into.
_EVENT_STATES: dict[str, str] = {
    "reserved": STATE_RESERVED,
    "asked": STATE_ASKED,
    "ask_failed": STATE_FAILED,
    "captured": STATE_CAPTURED,
    "recaptured": STATE_CAPTURED,
    "promoted": STATE_PROMOTED,
    "held_collision": STATE_HELD,
    "skipped_by_user": STATE_SKIPPED,
    "expired": STATE_EXPIRED,
}


def fold_state() -> dict[str, Any]:
    """Fold the event log into effective state (memoized on the file fingerprint).

    Returns:
      {
        "cycles":     {cycle_id: {...}},
        "by_day":     {(user, date): cycle_id},          # send ledger
        "last_asked": {(user, item_key): date},          # cooldown
        "last_cycle": {(user, item_key): cycle},         # most recent, for re-ask
        "no_gap":     {(user, date): True},              # skipped_no_gap ledger
      }

    TERMINAL STICKINESS is the load-bearing property here (D-096): once a cycle
    is terminal, a later event -- a duplicated line, an out-of-order append from
    the other process, a replayed handler -- records in the log but CANNOT move
    the state back to live. Without this, appending any non-terminal event to a
    finished cycle would resurrect it and it could be answered/promoted twice.
    """
    path = _events_path()
    fp = _events_fingerprint(path)
    with _FOLD_LOCK:
        if fp is not None and _FOLD_CACHE["key"] == fp:
            return _FOLD_CACHE["value"]

    cycles: dict[str, dict[str, Any]] = {}
    by_day: dict[tuple[str, str], str] = {}
    last_asked: dict[tuple[str, str], str] = {}
    last_cycle: dict[tuple[str, str], dict[str, Any]] = {}
    _streaks: dict[tuple[str, str], int] = {}
    no_gap: dict[tuple[str, str], bool] = {}

    for row in _read_events():
        ev = str(row.get("event", "") or "")
        user = str(row.get("user", "") or "")
        date = str(row.get("date", "") or "")

        if ev == "skipped_no_gap":
            if user and date:
                no_gap[(user, date)] = True
            continue

        cycle_id = str(row.get("cycle_id", "") or "")
        if not cycle_id:
            continue

        cyc = cycles.get(cycle_id)
        if cyc is None:
            cyc = {
                "cycle_id": cycle_id,
                "user": user,
                "date": date,
                "entity": str(row.get("entity", "") or ""),
                "tier": row.get("tier"),
                "item_key": str(row.get("item_key", "") or ""),
                "question": str(row.get("question", "") or ""),
                "kpi": str(row.get("kpi", "") or ""),
                "gap_ts": str(row.get("gap_ts", "") or ""),
                "state": STATE_RESERVED,
                "answer": "",
                "message_ts": "",
                "channel": "",
                "created_ts": row.get("ts"),
                "edited": False,
                "delivered": False,
                "awaiting_reword": False,
                "card_ts": "",
            }
            cycles[cycle_id] = cyc

        # `delivered` is tracked OUTSIDE the terminal guard: it records whether
        # the DM ever actually reached the person, which stays true no matter
        # how the cycle ends. The cooldown keys on it, so a reservation that
        # crashed before the send -- or a send that failed -- can never burn a
        # week of that KPI's rotation for a question nobody ever saw.
        if ev == "asked":
            cyc["delivered"] = True
        if row.get("card_ts"):
            cyc["card_ts"] = row["card_ts"]

        # Terminal is sticky: record nothing further onto the state.
        if cyc["state"] in TERMINAL_STATES:
            continue

        new_state = _EVENT_STATES.get(ev)
        if new_state:
            cyc["state"] = new_state
        if ev == "expired":
            cyc["end_reason"] = str(row.get("reason", "") or "")
        if ev == "recaptured":
            cyc["edited"] = True
        # An edit tap parks the cycle until fresh prose arrives, so a bare "ok"
        # acknowledging the reword instruction cannot promote the very answer
        # the person just rejected (D-051 lens-2 MEDIUM).
        if ev == "editing":
            cyc["awaiting_reword"] = True
        elif ev in ("captured", "recaptured"):
            cyc["awaiting_reword"] = False
        for fld in ("answer", "message_ts", "channel", "question", "entity",
                    "item_key", "gap_ts", "tier", "kpi"):
            if row.get(fld) not in (None, ""):
                cyc[fld] = row[fld]
        cyc["last_ts"] = row.get("ts")

    for cid, cyc in cycles.items():
        u, d, k = cyc.get("user"), cyc.get("date"), cyc.get("item_key")
        if not (u and d):
            continue
        by_day[(u, d)] = cid
        if not k:
            continue
        # Only a DELIVERED ask counts toward the cooldown (see `delivered` above).
        if cyc.get("delivered"):
            prev = last_asked.get((u, k))
            if prev is None or d > prev:
                last_asked[(u, k)] = d
        prev_cyc = last_cycle.get((u, k))
        if prev_cyc is None or d > str(prev_cyc.get("date", "")):
            last_cycle[(u, k)] = cyc
        # Consecutive-unanswered streak per item: any cycle that got as far as an
        # ANSWER resets it, so a person who engages is never penalised.
        streak_key = (u, k)
        if cyc.get("delivered"):
            if cyc.get("state") == STATE_EXPIRED and cyc.get("end_reason") == "no_response":
                _streaks[streak_key] = _streaks.get(streak_key, 0) + 1
            elif cyc.get("answer"):
                _streaks[streak_key] = 0

    for key, n in _streaks.items():
        if key in last_cycle:
            last_cycle[key]["unanswered_streak"] = n
    out = {"cycles": cycles, "by_day": by_day, "last_asked": last_asked,
           "last_cycle": last_cycle, "no_gap": no_gap}
    with _FOLD_LOCK:
        if fp is not None:
            _FOLD_CACHE["key"] = fp
            _FOLD_CACHE["value"] = out
    return out


def handled_today(state: dict[str, Any], user: str, date: str) -> bool:
    """True when this person already has a question (or a recorded skip) for this
    AZ date. THE idempotency check -- consulted before every send."""
    if state["no_gap"].get((user, date)):
        return True
    cid = state["by_day"].get((user, date))
    if not cid:
        return False
    return state["cycles"][cid].get("state") in HANDLED_STATES


def live_cycle_for(state: dict[str, Any], user: str,
                   today: str | None = None) -> dict[str, Any] | None:
    """The one in-flight cycle for a person, if any.

    "In flight" means ASKED (awaiting an answer) or CAPTURED (awaiting a confirm
    tap), AND asked TODAY.

    THE DATE FILTER IS LOAD-BEARING, not tidiness (D-051 lens-2 HIGH). Expiry
    used to be purely runner-side, which meant a cycle was only really dead once
    the next weekday run swept it. A Friday question therefore stayed live all
    weekend, and ANY declarative DM on Saturday -- "the Tucson inspection is
    Tuesday" -- was captured as its answer, producing a wrong Q/A pair in an
    always-injected file. The same held, unbounded, whenever the scheduled task
    simply did not fire. Enforcing it at READ time makes "expires end of day"
    true regardless of whether anything ran; expire_stale_cycles still exists to
    record the outcome for the participation metrics.
    """
    today = today or az_date()
    best: dict[str, Any] | None = None
    for cyc in state["cycles"].values():
        if cyc.get("user") != user:
            continue
        if cyc.get("state") not in (STATE_ASKED, STATE_CAPTURED):
            continue
        if str(cyc.get("date", "")) != today:
            continue
        if best is None or str(cyc.get("date", "")) > str(best.get("date", "")):
            best = cyc
    return best


def get_cycle(state: dict[str, Any], cycle_id: str) -> dict[str, Any] | None:
    return state["cycles"].get(cycle_id)


def expire_stale_cycles(state: dict[str, Any], today: str | None = None) -> list[dict[str, Any]]:
    """Close out anything still in flight from a PREVIOUS day.

    Spec 4: an unanswered question expires end-of-day rather than stacking into
    tomorrow. Two distinct reasons, kept apart because they mean different things
    for the pilot's metrics: `no_response` (asked, never answered -- a
    participation signal) vs `no_confirm` (answered, never confirmed -- a
    friction signal about the card, not about the person's engagement).

    Also expires a RESERVED cycle whose DM never went out, so a crash between
    reserve and send does not block that person forever. Returns the rows it
    wrote so the runner can report them.
    """
    today = today or az_date()
    out: list[dict[str, Any]] = []
    for cyc in state["cycles"].values():
        if cyc.get("state") not in (STATE_ASKED, STATE_CAPTURED, STATE_RESERVED):
            continue
        if str(cyc.get("date", "")) >= today:
            continue
        reason = {STATE_ASKED: "no_response",
                  STATE_CAPTURED: "no_confirm",
                  STATE_RESERVED: "reserved_never_sent"}[cyc["state"]]
        row = append_event("expired", cycle_id=cyc["cycle_id"],
                           user=cyc.get("user"), date=cyc.get("date"),
                           reason=reason)
        if row is not None:
            out.append(row)
    return out


# ── Tier-1 selection ────────────────────────────────────────────────────────

def new_cycle_id() -> str:
    """Opaque, unguessable handle. It is also the confirm-card button value, so
    it must not be enumerable -- same shape as gap_autofill's `gapask-<hex>`."""
    return f"kchk-{secrets.token_hex(8)}"


def select_tier1(person: dict[str, Any], state: dict[str, Any],
                 today: str | None = None) -> dict[str, Any] | None:
    """The person's least-recently-asked eligible item, or None.

    ROTATION, NOT CONSUMPTION: eligibility is purely the cooldown. An item that
    was answered and promoted is eligible again once ITEM_COOLDOWN_DAYS have
    passed, because a status snapshot goes stale. Never-asked items sort first,
    then oldest-asked; ties break on the roster's own order so rotation is
    deterministic and reviewable.
    """
    today = today or az_date()
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for idx, item in enumerate(person.get("items") or []):
        asked_on = state["last_asked"].get((person["slack_id"], item["key"]))
        if asked_on is None:
            candidates.append((0, idx, item))
            continue
        age = _days_between(asked_on, today)
        if age is None:
            # Unparseable date -- treat as recently asked (fail-closed: do not
            # re-ask something we cannot prove is off cooldown).
            continue
        if age < ITEM_COOLDOWN_DAYS:
            # Kickoff step 2.4: an UNANSWERED question gets a gentle re-ask the
            # next weekday rather than sitting out the full cooldown -- the
            # cooldown exists to stop re-asking something we already have an
            # answer for, which is not this case. Bounded by
            # MAX_UNANSWERED_REASKS so somebody who never engages is not asked
            # the same thing every single day indefinitely.
            last = state.get("last_cycle", {}).get((person["slack_id"], item["key"]))
            if (last and age >= 1
                    and last.get("state") == STATE_EXPIRED
                    and last.get("end_reason") == "no_response"
                    and int(last.get("unanswered_streak", 0)) <= MAX_UNANSWERED_REASKS):
                candidates.append((0, idx, item))
            continue
        candidates.append((1, -age, item))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


# ── Answer hygiene ──────────────────────────────────────────────────────────

def normalize_answer(text: str) -> str:
    """Collapse an answer to a single clean line for storage and comparison."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def scrub_answer(text: str) -> str:
    """Neutralize LIVE SLACK BEHAVIOUR in an answer before it is stored.

    This is NOT a PHI scrub and is unaffected by the LEX decision -- it is the
    prompt-injection / broadcast belt (D-123 class). A confirmed answer lands in
    an ALWAYS-INJECTED known-answers file and is served back to the model through
    retrieval, so a staff answer is untrusted input on a durable surface: an
    embedded `<!channel>` would broadcast on every future render, and a labelled
    link is the classic instruction-smuggling shape. Reuses the exact function
    the #info-for-cora intake already applies at the same boundary, so the two
    durable-write paths cannot drift.
    """
    t = normalize_answer(text)
    try:
        from .info_intake import scrub_contribution
        t = scrub_contribution(t)
    except Exception:  # noqa: BLE001 -- belt, never a blocker
        log.warning("knowledge_check: answer scrub failed -- storing normalized text",
                    exc_info=True)
    if len(t) > MAX_ANSWER_CHARS:
        t = t[:MAX_ANSWER_CHARS].rstrip() + " ... (truncated)"
    return t


# Same edge-quantifier rule as _AFFIRMATIVE_RE below: normalized input only, so
# no `\s*` bookends and no O(n^2) whitespace backtracking.
_NON_ANSWER_RE = re.compile(
    r"^(no idea|not my area|don'?t know|dunno|no clue|n/?a|nothing|skip)$",
    re.IGNORECASE,
)


def is_non_answer(text: str) -> bool:
    """True for a reply that declines rather than answers. Treated as a skip --
    never staged as a fact."""
    return bool(_NON_ANSWER_RE.match(normalize_answer(text)))


# ── Tier-2 selection: a REAL organic gap in this person's domain ────────────
#
# Tier 2 PREEMPTS Tier 1 (spec section 3.2): a live gap beats a rotating
# checklist item.
#
# ELIGIBILITY IS gap_autofill's OWN TWO SCREENS, UNION-ED -- deliberately not a
# new set of rules:
#
#   mine_eligibility()  -- is this a real missing FACT? (screens out capability
#                          asks, QA/test scaffolding, retired processes,
#                          point-in-time state, already-routed, and disputed
#                          exchanges). This is the "99.3% test-fixture pollution"
#                          lesson the kickoff names.
#   should_escalate()   -- may this gap's TEXT be re-broadcast to a named human?
#                          Tier 2 is an escalation in every respect that matters:
#                          it quotes a question somebody else's session produced
#                          to a third party. So the protections that exist for
#                          that act apply unchanged -- DM-origin gaps stay
#                          private, finance-restricted gaps are not quoted to a
#                          possibly financials-blocked reader (D-064), and the
#                          3-predicate PHI union applies. Harrison's 2026-08-11
#                          LEX decision governs THIS BUILD's own questions and
#                          the storage of answers; it was not a decision to
#                          re-broadcast arbitrary third-party logged content, so
#                          those controls are left standing. (The LEX lane here
#                          follows the existing CORA_GAP_ESCALATION_LEX flag,
#                          exactly as the domain-owner lane does.)
#
# DELIBERATE DEVIATION FROM THE KICKOFF WORDING, verified against live code:
# the kickoff says Tier 2 sources "a real unresolved kb_miss/unknown_response"
# gap. should_escalate() EXCLUDES kb_miss, with a reviewed reason -- it is a
# retrieval-side detector that fires even when Cora answered the question
# correctly from static context, so asking a human to supply an answer she
# already gave is pure noise (gap_autofill.py, adversarial review MEDIUM).
# Honoring the literal wording would re-introduce exactly the garbage-question
# class the same kickoff paragraph asks us to screen out. unknown_response and
# llm_sentinel gaps -- which reflect an actually failed answer -- remain
# eligible, so Tier 2 still fires; it just fires on real misses.

# Cap per run so one noisy day cannot turn the whole roster into gap triage.
MAX_TIER2_PER_RUN = 5


def _entity_matches(person_entity: str, gap_entity: str) -> bool:
    """May a gap tagged `gap_entity` be asked of someone scoped to `person_entity`?

    Exact match, plus a sub-entity holder may receive their PARENT's gaps
    (a LEX-LLC director sees GM-level LEX questions). Never the reverse, and
    NEVER a sibling sub-entity -- LEX-LLA content must not reach a LEX-LLC
    director, which is the same firewall known_answers_map documents on the
    read side.
    """
    p = (person_entity or "").strip().upper()
    g = (gap_entity or "").strip().upper()
    if not p or not g:
        return False
    if p == g:
        return True
    return "-" in p and p.split("-", 1)[0] == g


def tier2_eligible(gap: dict[str, Any]) -> tuple[bool, str]:
    """(eligible, reason) -- both of gap_autofill's screens must pass.

    FAIL-CLOSED: any error means ineligible. Reasons are fixed strings, never
    raw gap text, so they are safe to log and aggregate.
    """
    try:
        from . import gap_autofill as ga
        ok, why = ga.mine_eligibility(gap)
        if not ok:
            return False, why or "mine-ineligible"
        if not ga.should_escalate(gap):
            return False, "not eligible to be re-broadcast to a person"
        return True, ""
    except Exception:  # noqa: BLE001 -- fail closed
        log.warning("knowledge_check: tier-2 screen errored -- fail-closed",
                    exc_info=True)
        return False, "eligibility screen errored"


def select_tier2(person: dict[str, Any],
                 open_gaps: list[dict[str, Any]] | None = None,
                 claimed: set[str] | None = None) -> dict[str, Any] | None:
    """The oldest eligible open gap in this person's domain, or None.

    `claimed` lets one run hold in-memory claims so two people in the same
    entity (Matt and Micah both scope to OSN) are never asked the same question
    in a single pass -- the durable half of that guarantee is claim_gap().
    """
    try:
        from . import gap_autofill as ga
        gaps = open_gaps if open_gaps is not None else ga.load_open_gaps()
    except Exception:  # noqa: BLE001
        log.warning("knowledge_check: could not load open gaps", exc_info=True)
        return None
    claimed = claimed if claimed is not None else set()
    candidates = []
    for gap in gaps:
        ts = str(gap.get("ts", "") or "")
        if not ts or ts in claimed:
            continue
        if not _entity_matches(person.get("entity", ""), str(gap.get("entity", "") or "")):
            continue
        ok, _why = tier2_eligible(gap)
        if not ok:
            continue
        candidates.append(gap)
    if not candidates:
        return None
    # Oldest first: a gap that has survived mining and aging is the one most
    # worth a human's attention.
    candidates.sort(key=lambda g: str(g.get("ts", "")))
    return candidates[0]


def claim_gap(gap_ts: str, cycle_id: str) -> bool:
    """Durably claim a gap so nothing else asks it. Returns True if claimed.

    Writes into gap_autofill's OWN state ledger rather than a parallel one:
    ga.load_open_gaps() excludes any gap ts present there, so a single write
    removes the gap from the Harrison-facing digest flow, from gap_autofill's
    domain-owner escalation, AND from future knowledge-check runs. Reusing the
    existing ledger is what makes the dedup the kickoff asks for actually hold --
    a separate claim file would leave the two flows blind to each other.

    Re-reads state immediately before writing (the same load-modify-save shape
    run_gap_autofill.py uses per gap) to keep the cross-process window small;
    this task is scheduled AFTER the 6:00am gap-autofill run precisely so the
    two are not writing concurrently.
    """
    if not gap_ts:
        return False
    try:
        from . import gap_autofill as ga
        # RE-READ IMMEDIATELY BEFORE WRITING, then merge only our own key
        # (D-051 lens-1 MEDIUM). The scheduling argument below covers the 06:00
        # gap-autofill SCRIPT, but the always-on BOT writes this same ledger
        # too, whenever a teammate answers a gap-ask DM, at any hour.
        # save_state replaces the whole file, so writing back a copy read 45
        # minutes earlier erased that teammate's claim and reopened a gap they
        # had already answered. Merging into a freshly-read dict cannot drop a
        # key we never saw; the residual (both writers landing between this read
        # and the write) loses OUR OWN claim, which is the benign direction.
        fresh = ga.load_state()
        if gap_ts in fresh:
            return False  # somebody already claimed it
        fresh[gap_ts] = {"state": "asked", "via": "knowledge_check",
                         "cycle_id": cycle_id, "at": _now_iso()}
        ga.save_state(fresh)
        return True
    except Exception:  # noqa: BLE001 -- a failed claim must not break the run
        log.warning("knowledge_check: gap claim failed for %s", gap_ts, exc_info=True)
        return False


def select_question(person: dict[str, Any], state: dict[str, Any],
                    *, open_gaps: list[dict[str, Any]] | None = None,
                    claimed: set[str] | None = None,
                    today: str | None = None) -> dict[str, Any] | None:
    """The one question for this person today, or None to SKIP them.

    Tier 2 preempts Tier 1. Returning None is a first-class outcome -- there is
    no Tier 3 and no generative fallback, so a person with an exhausted pool and
    no live gap is skipped rather than asked something manufactured.
    """
    gap = select_tier2(person, open_gaps=open_gaps, claimed=claimed)
    if gap is not None:
        # A Tier-2 question is a VERBATIM Slack message body written by whoever
        # asked it -- third-party text, unlike a roster question. It is relayed
        # in a DM from Cora AND written into an always-injected file, so it goes
        # through exactly the same door as an answer: whitespace-collapsed,
        # structure-neutralized (safe_line) and Slack-neutralized (scrub_answer,
        # which rewrites `<url|label>`, `<!channel>` and `<@user>`). Without this
        # a crafted gap question could forge a provenance block in known-answers
        # or get Cora to DM 13 people an attacker-chosen labelled link.
        return {
            "tier": 2,
            "item_key": f"gap:{gap.get('ts', '')}",
            "gap_ts": str(gap.get("ts", "") or ""),
            "question": safe_line(scrub_answer(str(gap.get("question", "") or ""))),
            "kpi": "open knowledge gap",
        }
    item = select_tier1(person, state, today=today)
    if item is not None:
        return {
            "tier": 1,
            "item_key": item["key"],
            "gap_ts": "",
            "question": item["question"],
            "kpi": item.get("kpi", ""),
        }
    return None


# ── Slack surfaces ──────────────────────────────────────────────────────────
#
# ADDITIVE BUTTONS (the standing rule from the confirm-buttons work): buttons
# only ever ADD a one-tap route to something the typed path already does. The
# answer itself is free prose and no button can carry it -- there is no
# answer-via-button here and there must never be one. What is enumerable is
# "not today", so that is the only button on the ask.
ACTION_SKIP_TODAY = "cora_kc_skip_today"
ACTION_CONFIRM_ANSWER = "cora_kc_confirm"
ACTION_EDIT_ANSWER = "cora_kc_edit"
ACTION_SKIP_ANSWER = "cora_kc_skip_answer"


def _first_name(name: str) -> str:
    return (name or "").strip().split(" ")[0] or "there"


def _sanitize(text: str) -> str:
    """Block Kit bodies bypass the class-level WebClient egress patch, which only
    covers `text=` (D-168) -- so every body built here is sanitized at
    construction, the same way gap_autofill.build_ask_blocks does it."""
    try:
        from .slack_egress import sanitize_text
        return sanitize_text(text or "")
    except Exception:  # noqa: BLE001 -- sanitizer is a belt, never a blocker
        return text or ""


def ask_text(question: str, name: str = "") -> str:
    """The ask body. One question, plain language, no preamble (spec 4.1).

    The second line is deliberate disclosure, not decoration: it tells the person
    up front that nothing is saved until they see it, which is the promise the
    confirm-back step exists to keep.
    """
    return (f"{_first_name(name)} -- {str(question or '').strip()}\n\n"
            "_Just reply here. I'll show you what I captured before anything is saved._")


def build_ask_blocks(question: str, cycle_id: str, name: str = "") -> list[dict]:
    from . import confirm_cards as _cc
    return [
        *_cc.chunk_mrkdwn_sections(_sanitize(ask_text(question, name))),
        {"type": "actions",
         "block_id": f"cora_kc_ask_{cycle_id}"[:255],
         "elements": [
             {"type": "button", "action_id": ACTION_SKIP_TODAY,
              "text": {"type": "plain_text", "text": "Skip today"},
              "value": cycle_id},
         ]},
    ]


def confirm_text(answer: str, question: str = "") -> str:
    """The confirm-back body: the person's OWN words restated as the fact that
    would be stored. Cora invents nothing here -- that is the whole point."""
    body = "Here's what I got -- save it?\n\n"
    if question:
        body += f"> {str(question).strip()[:300]}\n\n"
    return body + f"*{str(answer or '').strip()}*"


def answer_fingerprint(answer: str) -> str:
    """Short, stable digest of the answer a card is DISPLAYING.

    Not a secret and not an authorization token -- the opaque cycle_id remains
    the only thing that authorizes anything. This binds a card to the exact text
    printed on it, which the cycle_id alone cannot do (D-051 lens-1 MEDIUM):
    "Let me reword" deliberately leaves the old card live, so a reworded answer
    produces a SECOND card carrying the same cycle_id. Tapping "Save it" on the
    older card wrote the NEWER answer while the card still displayed the old one,
    and the outcome line was appended to that stale text -- the person would
    reasonably believe they had saved what they were looking at. That breaks the
    hard rule "never write what the person did not confirm" through an
    advertised flow, with no race required.
    """
    import hashlib
    return hashlib.sha256(normalize_answer(answer).encode("utf-8")).hexdigest()[:10]


def build_confirm_blocks(answer: str, cycle_id: str, question: str = "") -> list[dict]:
    from . import confirm_cards as _cc
    value = f"{cycle_id}:{answer_fingerprint(answer)}"
    return [
        *_cc.chunk_mrkdwn_sections(_sanitize(confirm_text(answer, question))),
        {"type": "actions",
         "block_id": f"cora_kc_confirm_{cycle_id}"[:255],
         "elements": [
             {"type": "button", "action_id": ACTION_CONFIRM_ANSWER, "style": "primary",
              "text": {"type": "plain_text", "text": "Save it"},
              "value": value},
             {"type": "button", "action_id": ACTION_EDIT_ANSWER,
              "text": {"type": "plain_text", "text": "Let me reword"},
              "value": value},
             {"type": "button", "action_id": ACTION_SKIP_ANSWER,
              "text": {"type": "plain_text", "text": "Skip"},
              "value": value},
         ]},
    ]


def split_tap_value(value: str) -> tuple[str, str]:
    """(cycle_id, fingerprint) from a button value. Fingerprint is "" when absent
    (the ask card's Skip-today button, which has no answer to bind to)."""
    raw = str(value or "")
    cycle_id, _, fp = raw.partition(":")
    return cycle_id, fp


def open_dm(client: Any, slack_id: str) -> str:
    """Resolve a person's DM channel id, or "" if unreachable. Never raises."""
    try:
        resp = client.conversations_open(users=slack_id)
        return ((resp or {}).get("channel") or {}).get("id", "") or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("knowledge_check: conversations_open failed for %s: %s",
                    slack_id, exc)
        return ""


# ── CAPTURE ─────────────────────────────────────────────────────────────────

def match_live_cycle(user_id: str, thread_ts: str | None,
                     *, allow_toplevel: bool = True) -> dict[str, Any] | None:
    """The cycle this DM is answering, if any. Mirrors gap_autofill.match_pending_ask.

    A reply typed in the ask's OWN thread is unambiguous intent and always
    matches. A top-level DM matches only when the caller has already ruled out
    every competing intent (staged write, shift command, remember/forget, a
    fresh question, a live gap ask) -- see the ordering note in
    app.handle_message_event.
    """
    state = fold_state()
    cyc = live_cycle_for(state, user_id)
    if cyc is None:
        return None
    if thread_ts:
        # The ask's ts OR the confirm card's ts. The flow encourages a top-level
        # answer, so the confirm card is usually posted top-level too -- and a
        # user replying in THAT card's thread is unambiguously answering it.
        return cyc if thread_ts in (cyc.get("message_ts"), cyc.get("card_ts")) else None
    return cyc if allow_toplevel else None


def register_card_ts(cycle_id: str, card_ts: str) -> None:
    """Record where a confirm card landed, so a reply typed in the CARD's own
    thread matches as well as one typed in the ask's thread."""
    if cycle_id and card_ts:
        append_event("card_posted", cycle_id=cycle_id, card_ts=card_ts)


def has_live_cycle(user_id: str) -> bool:
    """Cheap predicate for the DM router's ambiguity check."""
    try:
        return live_cycle_for(fold_state(), user_id) is not None
    except Exception:  # noqa: BLE001 -- routing must never break on a state read
        log.warning("knowledge_check: has_live_cycle failed", exc_info=True)
        return False


# One process owns every button tap and every DM capture (the always-on bot), so
# an in-process lock is a real claim here. Each claim re-folds the log INSIDE the
# lock and refuses if the cycle already reached a terminal state, which is what
# makes double-taps and tap-racing-typed-reply idempotent rather than a double
# write.
_CLAIM_LOCK = Lock()


def record_answer(cycle_id: str, user_id: str, text: str) -> tuple[str, str]:
    """Stage a typed answer against a live cycle. Returns (outcome, reply_text).

    Outcomes: captured | recaptured | declined | empty | not_live | not_authorized.

    NOTHING IS WRITTEN HERE. This only stages -- the confirm card is the gate,
    which is why a mis-capture costs the person one Skip tap rather than putting
    a wrong fact into an always-injected knowledge file.
    """
    with _CLAIM_LOCK:
        state = fold_state()
        cyc = get_cycle(state, cycle_id)
        if cyc is None or cyc.get("state") not in (STATE_ASKED, STATE_CAPTURED):
            return "not_live", ""
        if cyc.get("user") != user_id:
            return "not_authorized", ""
        answer = scrub_answer(text)
        if not answer:
            return "empty", ""
        # PHI gate at CAPTURE, not only at the write (D-051 lens-4 MEDIUM). The
        # answer is durably persisted into the append-only event log the moment
        # it is staged, so gating only in promote() meant an ARMED gate still let
        # PHI reach durable state -- and because a refused promote leaves the
        # cycle CAPTURED for a reword, each retry appended another plaintext
        # copy. Under the decided posture this is a no-op (PHI_GATE_ANSWERS is
        # False); it exists so the reversal is actually complete.
        blocked, reason = phi_blocked(answer)
        if blocked:
            append_event("skipped_by_user", cycle_id=cycle_id, user=user_id,
                         date=cyc.get("date"), reason="phi_refused")
            return "declined", reason
        if is_non_answer(answer):
            append_event("skipped_by_user", cycle_id=cycle_id, user=user_id,
                         date=cyc.get("date"), reason="declined_in_reply")
            return "declined", ("No problem -- nothing saved. I'll ask something "
                                "else next time.")
        first = cyc.get("state") == STATE_ASKED
        append_event("captured" if first else "recaptured", cycle_id=cycle_id,
                     user=user_id, date=cyc.get("date"), answer=answer)
        return ("captured" if first else "recaptured"), answer


# ── CONFIRM / EDIT / SKIP taps ──────────────────────────────────────────────

def _authorize_tap(cycle_id: str, actor_id: str,
                   allowed_states: tuple[str, ...],
                   fingerprint: str = "") -> tuple[str, dict[str, Any] | None]:
    """Shared resolve+authorize. Returns (outcome, cycle) where outcome is "ok"
    or a terminal explanation.

    ADDRESSEE-ONLY, checked BEFORE any state is touched: only the person a
    question was asked of may confirm, reword or skip it. These cards live in a
    1:1 DM so in practice nobody else can tap -- but a confirm writes a fact
    attributed to that person, and letting a forged payload trigger that would
    put words in their mouth.
    """
    if not cycle_id or not actor_id:
        return "orphaned", None
    state = fold_state()
    cyc = get_cycle(state, cycle_id)
    if cyc is None:
        return "orphaned", None
    if cyc.get("user") != actor_id:
        return "not_authorized", None
    if cyc.get("state") in TERMINAL_STATES:
        return "already_handled", cyc
    if cyc.get("state") not in allowed_states:
        return "not_live", cyc
    # The card must be acting on the text it is DISPLAYING. A tap carrying a
    # fingerprint that no longer matches the staged answer is a stale card (the
    # user reworded, then scrolled up and tapped the older one) -- refuse rather
    # than write something they were not looking at.
    if fingerprint and fingerprint != answer_fingerprint(cyc.get("answer", "")):
        return "superseded", cyc
    return "ok", cyc


def process_skip_today_tap(cycle_id: str, actor_id: str) -> tuple[str, str]:
    """"Skip today" on the ASK card. Records participation, writes nothing."""
    with _CLAIM_LOCK:
        outcome, cyc = _authorize_tap(cycle_id, actor_id, (STATE_ASKED, STATE_CAPTURED))
        if outcome != "ok":
            return outcome, _tap_message(outcome)
        append_event("skipped_by_user", cycle_id=cycle_id, user=actor_id,
                     date=cyc.get("date"), reason="skip_today")
    return "skipped", "No problem -- skipped for today."


def process_skip_answer_tap(cycle_id: str, actor_id: str,
                            fingerprint: str = "") -> tuple[str, str]:
    """"Skip" on the CONFIRM card: the staged answer is discarded unwritten."""
    with _CLAIM_LOCK:
        outcome, cyc = _authorize_tap(cycle_id, actor_id, (STATE_CAPTURED,),
                                      fingerprint)
        if outcome != "ok":
            return outcome, _tap_message(outcome)
        append_event("skipped_by_user", cycle_id=cycle_id, user=actor_id,
                     date=cyc.get("date"), reason="skip_at_confirm")
    return "skipped", "Got it -- nothing saved."


def process_edit_tap(cycle_id: str, actor_id: str,
                     fingerprint: str = "") -> tuple[str, str]:
    """"Let me reword": stay in CAPTURED and wait for a fresh typed answer.

    Deliberately does NOT open a modal. The whole capture surface is free prose
    in a DM; a second input mechanism would be a parallel path with its own
    failure modes for no benefit. The next DM replaces the staged answer.
    """
    with _CLAIM_LOCK:
        outcome, cyc = _authorize_tap(cycle_id, actor_id, (STATE_CAPTURED,),
                                      fingerprint)
        if outcome != "ok":
            return outcome, _tap_message(outcome)
        # PARK the cycle until fresh prose arrives. Without this, a user who taps
        # "Let me reword", reads the instruction below and types "ok" as an
        # acknowledgement hits the CAPTURED + is_affirmative branch and promotes
        # the very answer they just rejected -- irreversibly, from their side
        # (D-051 lens-2 MEDIUM).
        append_event("editing", cycle_id=cycle_id, user=actor_id,
                     date=cyc.get("date"))
    return "editing", ("Sure -- send it again however you'd like it worded, and "
                       "I'll show you the new version.")


# A confirm reply must be a WHOLE-MESSAGE affirmation. Anchored deliberately:
# "yes, but actually it's 4 open" is a REWORD, not a confirm, and treating it as
# a confirm would save the wrong number.
#
# NO `\s*` AT THE EDGES -- these run on normalize_answer()'d input, which has
# already collapsed and stripped whitespace. The obvious spelling,
# `^\s*(alt)\s*[.!]?\s*$`, is a ReDoS: two `\s*` separated by an optional
# character give O(n^2) backtracking on a long whitespace run that fails to
# match, and this matches RAW Slack message text where an attacker (or an
# accident) controls the length. Normalizing first makes the edge quantifiers
# unnecessary rather than merely careful.
_AFFIRMATIVE_RE = re.compile(
    r"^(yes|yep|yeah|yup|yes please|confirm|confirmed|correct|that'?s right|"
    r"right|save|save it|looks good|lgtm|ok|okay|perfect)[.!]?$",
    re.IGNORECASE)
_NEGATIVE_RE = re.compile(
    r"^(no|nope|nah|don'?t|do not|cancel|discard|scratch that|never ?mind)[.!]?$",
    re.IGNORECASE)


def is_affirmative(text: str) -> bool:
    return bool(_AFFIRMATIVE_RE.match(normalize_answer(text)))


def is_negative(text: str) -> bool:
    return bool(_NEGATIVE_RE.match(normalize_answer(text)))


def handle_dm_reply(cycle_id: str, user_id: str, text: str) -> tuple[str, str, bool]:
    """Route one DM reply against a live cycle. Returns (outcome, reply, post_card).

    THE TYPED PATH IS FIRST-CLASS, not a fallback. Buttons are additive (and can
    be switched off with CORA_CONFIRM_BUTTONS), so a flow that could only be
    completed by tapping would strand every staged answer unwritten the moment
    that flag flips. A whole-message "yes" confirms; a whole-message "no" skips;
    anything else is the answer, or a reword of it.
    """
    state = fold_state()
    cyc = get_cycle(state, cycle_id)
    if cyc is None:
        return "not_live", "", False
    # `awaiting_reword` suppresses the affirmative/negative branch until fresh
    # prose lands, so an "ok" acknowledging the reword prompt is treated as the
    # start of the new answer rather than as a confirm of the rejected one.
    if cyc.get("state") == STATE_CAPTURED and not cyc.get("awaiting_reword"):
        if is_affirmative(text):
            outcome, msg = process_confirm_tap(cycle_id, user_id)
            return outcome, msg, False
        if is_negative(text):
            outcome, msg = process_skip_answer_tap(cycle_id, user_id)
            return outcome, msg, False
    outcome, answer = record_answer(cycle_id, user_id, text)
    if outcome in ("captured", "recaptured"):
        return outcome, answer, True
    return outcome, answer, False


def _tap_message(outcome: str) -> str:
    return {
        "orphaned": "I don't have a record of that question anymore.",
        "not_authorized": "That question was for someone else.",
        "already_handled": "That one's already handled.",
        "not_live": "That question isn't waiting on anything right now.",
        "superseded": ("That card is showing an older version of your answer -- "
                       "use the most recent one."),
    }.get(outcome, "Something went wrong there.")


# ── PROMOTE ─────────────────────────────────────────────────────────────────
#
# A confirmed answer is appended to design/known-answers/{entity}.md -- the same
# always-injected store gap answers use -- and mirrored to the Org Remodel
# Tracker Training Log.
#
# THE ENTRY IS MACHINE-SWEEPABLE ON PURPOSE. A Tier-1 answer is a dated STATUS
# SNAPSHOT ("3 PCI notices still open"), which is wrong within the week. Writing
# it with only a human-readable "expires" note would leave stale status
# accumulating forever in a file injected into every reply -- exactly the
# KB-staleness problem D-087 exists for. So each block opens with a marker
# comment carrying its expiry, and expire_stale_answers() removes its own
# entries once they lapse. The TTL is real, not decorative.
#
# Tier-2 answers fill a genuine durable knowledge gap and are written with NO
# expiry.

_KC_MARKER_RE = re.compile(
    r"^<!--\s*kc-entry\s+cycle=(?P<cycle>[\w.-]+)\s+expires=(?P<expires>[\w-]+)\s*-->$")
_Q_LINE_RE = re.compile(r"^Q: (?P<q>.*)$")
_A_LINE_RE = re.compile(r"^A: (?P<a>.*)$")


def _entity_file(entity: str) -> Path:
    from .known_answers_map import file_for
    return _known_answers_dir() / file_for(entity)


def _expiry_for(tier: int, asked_date: str) -> str:
    """Tier-1 status snapshots expire; Tier-2 durable answers do not.

    Keyed on the date the question was ASKED, not the date it was confirmed
    (D-051 lens-3 MEDIUM). The cooldown is measured from the ask, so keying the
    expiry off the confirm made a cross-midnight tap (answered 08-11, tapped
    00:05 on 08-12) expire one day AFTER its own re-ask date -- the old entry
    was still live when the fresh answer arrived, so the new reading was
    discarded as a "dispute" and a false D-128 card went to Harrison. Self-
    healing on the following cycle, which made it fire every OTHER week for a
    habitual late-night responder. Anchoring both to the ask date removes the
    drift entirely.
    """
    if tier != 1:
        return "never"
    try:
        base = datetime.strptime(asked_date, "%Y-%m-%d")
    except Exception:  # noqa: BLE001 -- an unparseable date must not lose the TTL
        base = datetime.strptime(az_date(), "%Y-%m-%d")
    return (base + timedelta(days=ANSWER_TTL_DAYS)).date().isoformat()


def _is_expired(expires: str, today: str) -> bool:
    if not expires or expires == "never":
        return False
    return expires <= today


def safe_line(text: str, limit: int = 400) -> str:
    """Force `text` into ONE line that cannot forge this file's own structure.

    Everything written into a known-answers file is third-party-authored: an
    answer is typed by a teammate, and a Tier-2 question is a verbatim Slack
    message body from the gap log (NOT whitespace-collapsed at source, unlike a
    roster question). Three concrete structural attacks, all closed here:

      * a NEWLINE splits one logical field across lines, so _scan_entries records
        only the first fragment while detect_collision compares the whole string.
        They can never match, which renders that entry permanently INVISIBLE to
        every future collision check -- silently disabling hard rule 3 for it.
      * a leading '#' becomes a fake '## ' section boundary, which moves
        _append_entry's insertion point and can strand later entries outside
        '## Known facts'.
      * a forged '<!-- kc-entry ... expires=<past> -->' line makes the TTL sweep
        delete from there to the next blank -- truncating a legitimate entry it
        did not write.
    """
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    t = t.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
    t = re.sub(r"^#+\s*", "", t)
    if len(t) > limit:
        t = t[:limit].rstrip() + " ..."
    return t


def build_entry_lines(cycle: dict[str, Any], answer: str, today: str) -> list[str]:
    """The exact block written to known-answers.

    Attribution is mandatory (spec section 2): every promoted item names who
    contributed it and how, so a reader can always tell an attested human report
    from something Cora derived.
    """
    tier = int(cycle.get("tier") or 1)
    expires = _expiry_for(tier, str(cycle.get("date") or today))
    person = roster_member(str(cycle.get("user", ""))) or {}
    name = person.get("name") or str(cycle.get("user", ""))
    label = cycle.get("kpi") or ("open knowledge gap" if tier == 2 else "status")
    kind = ("status snapshot, expires " + expires) if tier == 1 else "contributed answer"
    # safe_line on BOTH fields, defensively, even though select_question already
    # cleans the Tier-2 question: this is the last gate before an always-injected
    # file, and a future producer must not be able to reintroduce the forgery.
    return [
        f"<!-- kc-entry cycle={cycle.get('cycle_id', '')} expires={expires} -->",
        f"**[{today}] {safe_line(name, 80)} -- {safe_line(label, 80)}** "
        f"_(daily knowledge check -- {kind})_",
        f"Q: {safe_line(cycle.get('question', ''))}",
        f"A: {safe_line(answer, MAX_ANSWER_CHARS)}",
        f"Author: {safe_line(name, 80)} -- contributed via daily knowledge check",
        "",
    ]


def _scan_entries(text: str, today: str) -> list[dict[str, Any]]:
    """Every Q/A pair in a known-answers file, with its live/expired status.

    A pair inside a LAPSED kc-entry is treated as absent: it is about to be swept
    and must not be mistaken for canon that a fresh answer contradicts. Pairs
    with no marker (gap-autofill's own writes) are durable and always live.
    """
    out: list[dict[str, Any]] = []
    cur_expires = "never"
    cur_cycle = ""
    pending_q: str | None = None
    for line in (text or "").split("\n"):
        m = _KC_MARKER_RE.match(line.strip())
        if m:
            cur_expires, cur_cycle = m.group("expires"), m.group("cycle")
            pending_q = None
            continue
        if not line.strip():
            # A blank line ends a block, so the next pair is outside any marker.
            cur_expires, cur_cycle = "never", ""
            pending_q = None
            continue
        mq = _Q_LINE_RE.match(line)
        if mq:
            pending_q = mq.group("q").strip()
            continue
        ma = _A_LINE_RE.match(line)
        if ma and pending_q is not None:
            out.append({"question": pending_q, "answer": ma.group("a").strip(),
                        "expires": cur_expires, "cycle_id": cur_cycle,
                        "expired": _is_expired(cur_expires, today)})
            pending_q = None
    return out


# Distinguishes "the scan failed" from "an entry exists and differs".
_COLLISION_READ_ERROR = "__read_error__"


def detect_collision(entity: str, question: str, answer: str,
                     today: str | None = None) -> tuple[bool, str]:
    """DETERMINISTIC key-collision check. Returns (collides, existing_answer).

    The key is the QUESTION text. A Tier-1 question is a fixed template, so the
    same KPI asked next week produces the same key -- which is precisely why the
    check ignores lapsed entries: a fresh snapshot superseding an expired one is
    NOT a contradiction. What IS a contradiction is a LIVE entry, from this flow
    or from gap-autofill's mining, whose answer differs.

    This is code, never model judgment (spec section 4.5 / doctrine 1jjj): a
    model asked "does this contradict?" is exactly the unreliable step the
    dispute rule exists to remove.
    """
    today = today or az_date()
    try:
        path = _entity_file(entity)
        if not path.exists():
            return False, ""
        target_q = str(question or "").strip()
        new_a = normalize_answer(answer).lower().rstrip(".")
        for e in _scan_entries(path.read_text(encoding="utf-8"), today):
            if e["expired"] or e["question"] != target_q:
                continue
            if e["answer"].lower().rstrip(".") != new_a:
                return True, e["answer"]
        return False, ""
    except Exception:  # noqa: BLE001 -- fail CLOSED: treat an unreadable file as
        # a possible collision rather than risk silently overwriting canon.
        # A SENTINEL, not "" -- the first cut passed the empty string straight
        # into the decision card, which then asserted a contradiction and showed
        # nothing to contradict ("On file: "), with no hint that the scan had
        # failed rather than found something (D-051 lens-4 LOW).
        log.error("knowledge_check: collision scan failed for %s -- holding",
                  entity, exc_info=True)
        return True, _COLLISION_READ_ERROR


def expire_stale_answers(today: str | None = None,
                         dry_run: bool = False) -> dict[str, int]:
    """Sweep lapsed knowledge-check entries out of every known-answers file.

    Removes ONLY blocks this module wrote (they carry the kc-entry marker) and
    ONLY once their expiry has passed. Everything else in the file -- gap-autofill
    answers, routing rules, hand-written facts -- is untouched by construction.
    """
    today = today or az_date()
    from .known_answers_map import ENTITY_FILES
    removed: dict[str, int] = {}
    for filename in sorted(set(ENTITY_FILES.values())):
        path = _known_answers_dir() / filename
        if not path.exists():
            continue
        # PER-FILE guard around read AND write (D-051 lens-3 MEDIUM-HIGH). These
        # files live on the G: Drive mount in production, where a stalled mount
        # raises WinError 21/53/67. The first cut wrapped only the read, so one
        # unwritable file propagated out of the sweep, out of the runner, and
        # killed the whole morning's DMs -- and because files are swept in sorted
        # order, every later entity went unswept too.
        try:
            with _known_answers_lock():
                n = _sweep_one_file(path, today, dry_run)
        except Exception:  # noqa: BLE001 -- one bad file must not end the sweep
            log.warning("knowledge_check: sweep failed for %s -- continuing",
                        filename, exc_info=True)
            continue
        if n:
            removed[filename] = n
    return removed


def _sweep_one_file(path: Path, today: str, dry_run: bool) -> int:
    lines = path.read_text(encoding="utf-8").split("\n")
    out: list[str] = []
    i = 0
    count = 0
    while i < len(lines):
        m = _KC_MARKER_RE.match(lines[i].strip())
        if m and _is_expired(m.group("expires"), today):
            count += 1
            i += 1
            # Terminator is the block's trailing blank OR the next block's
            # marker OR a section header, whichever comes first. Trusting the
            # blank alone assumed every field is single-line; a field that ever
            # contains a blank line (or a truncated legacy entry) would leave the
            # tail behind as an UNMARKED, undated fragment that no later sweep
            # could ever remove -- permanently resident in an always-injected
            # file. safe_line() now prevents such an entry being written, but the
            # sweep must also be able to clean any that already exist.
            while i < len(lines):
                s = lines[i].strip()
                if not s:
                    i += 1
                    break
                if _KC_MARKER_RE.match(s) or s.startswith("## "):
                    break
                i += 1
            continue
        out.append(lines[i])
        i += 1
    if count and not dry_run:
        # Each entry is written as blank + block + blank and the sweep takes
        # block + trailing blank, so without this the leading blanks pile up and
        # the file grows whitespace over months of write/expire cycles. Scoped to
        # runs of 3+ so it only removes the blanks the sweep itself just created
        # and leaves ordinary one-blank-line formatting alone.
        collapsed: list[str] = []
        run = 0
        for ln in out:
            if not ln.strip():
                run += 1
                if run >= 3:
                    continue
            else:
                run = 0
            collapsed.append(ln)
        _atomic_write_text(path, "\n".join(collapsed))
    return count


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic replace via a PROCESS-UNIQUE temp file.

    The obvious `path.with_suffix(".tmp")` is a FIXED name shared by every
    writer of these files (this module, gap_autofill, info_intake). Two
    processes writing `lex.md.tmp` interleave their bytes and whichever calls
    replace() second publishes a TORN file as that entity's always-injected
    known-answers (D-051 lens-3 HIGH). A unique name makes the temp private, so
    replace() is the only shared step and it is atomic.
    """
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# ── Cross-process lock for known-answers read-modify-write ──────────────────
#
# The module docstring's "no read-modify-write anywhere in this module's state
# path" is true of the EVENT LOG and false of the artifact this feature exists
# to produce. Two processes mutate known-answers: the 08:05 runner (the TTL
# sweep) and the always-on bot (a promote). _CLAIM_LOCK is a threading.Lock and
# does not cross processes, so a sweep that read the file before a promote
# appended would write its stale copy back and DESTROY a confirmed answer --
# reproduced in review, with the cycle left terminally PROMOTED, the person
# thanked, and the fact simply absent. Morning taps on the previous day's cards
# are exactly the traffic the 08:05 run overlaps.
#
# O_CREAT|O_EXCL is atomic on Windows and POSIX alike. Fail-OPEN on a timeout:
# the lock reduces a real race, but blocking a person's confirmed answer because
# a lock file was left behind would be a worse failure than the race it prevents.
_KA_LOCK_STALE_S = 60.0
_KA_LOCK_TIMEOUT_S = 5.0


def _ka_lock_path() -> Path:
    return _known_answers_dir() / ".knowledge-check-write.lock"


class _known_answers_lock:
    """Best-effort cross-process mutex around a known-answers read-modify-write."""

    def __init__(self) -> None:
        self._held = False

    def __enter__(self) -> "_known_answers_lock":
        path = _ka_lock_path()
        deadline = time.monotonic() + _KA_LOCK_TIMEOUT_S
        while True:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self._held = True
                return self
            except FileExistsError:
                try:  # reclaim a lock orphaned by a killed process
                    if time.time() - path.stat().st_mtime > _KA_LOCK_STALE_S:
                        path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
            except OSError:
                return self  # unwritable dir -- proceed unlocked rather than block
            if time.monotonic() > deadline:
                log.warning("knowledge_check: known-answers lock timed out -- "
                            "proceeding unlocked")
                return self
            time.sleep(0.05)

    def __exit__(self, *_exc: Any) -> None:
        if self._held:
            try:
                _ka_lock_path().unlink(missing_ok=True)
            except OSError:
                pass


def _append_entry(entity: str, entry_lines: list[str], cycle_id: str = "") -> Path:
    """Append under '## Known facts', matching gap_autofill._append_to_section's
    insertion semantics so both writers produce identically-shaped files.

    IDEMPOTENT on cycle_id (D-051 lens-3 MEDIUM -- the B6 window, re-opened).
    append_event swallows its own write failures, so a `promoted` append that
    fails leaves the cycle CAPTURED while the person has already been told
    "Saved". Their retry re-enters promote with an IDENTICAL answer, which is
    correctly not a collision, and the fact block is written twice.
    gap_autofill.apply_known_answer closes exactly this window with a
    resolved-ledger short-circuit plus a content check; this is the same guard,
    keyed on the marker that is already unique per cycle.
    """
    path = _entity_file(entity)
    path.parent.mkdir(parents=True, exist_ok=True)
    if cycle_id and path.exists():
        try:
            if f"cycle={cycle_id} " in path.read_text(encoding="utf-8"):
                log.info("knowledge_check: entry for %s already present -- skipping",
                         cycle_id)
                return path
        except Exception:  # noqa: BLE001 -- an unreadable file falls through to append
            pass
    header = "## Known facts"
    if not path.exists():
        _atomic_write_text(path, f"# Known Answers\n\n## Routing rules\n\n{header}\n")
    lines = path.read_text(encoding="utf-8").rstrip("\n").split("\n")
    insert_at = len(lines)
    in_section = False
    for i, line in enumerate(lines):
        if line == header:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            insert_at = i
            break
    if not in_section:
        lines += ["", header]
        insert_at = len(lines)
    lines = lines[:insert_at] + [""] + entry_lines + lines[insert_at:]
    _atomic_write_text(path, "\n".join(lines) + "\n")
    return path


def route_collision_to_decisions(cycle: dict[str, Any], answer: str,
                                 existing: str) -> str | None:
    """D-128: a contradiction is a CALL, not a fact -- never silently overwrite.

    Returns the update_id, or None when the decisions lane declined it. A LEX
    collision is expected to be declined here: decision_inbox.screen_decision
    excludes LEX content from the decisions inbox, which is a surface Harrison's
    2026-08-11 knowledge-check decision did not cover, so it is left standing.
    The collision is still HELD (nothing written) and surfaced in the run report,
    which is the outcome that actually matters -- canon is not overwritten either
    way.
    """
    entity = str(cycle.get("entity", "FNDR") or "FNDR").strip().upper()
    cycle_id = str(cycle.get("cycle_id", ""))
    person = roster_member(str(cycle.get("user", ""))) or {}
    name = person.get("name") or str(cycle.get("user", ""))
    update_id = f"kc-dispute-{cycle_id}"
    candidate = {
        "update_id": update_id,
        "description": f"[{entity}] Conflicting answer from {name}: "
                       f"{str(cycle.get('question', ''))[:120]}",
        "payload": {
            "entity": entity,
            "decision_text": (
                f"UNRESOLVED (routed by the daily knowledge check under D-128, not a "
                f"mined fact): {name} answered a question that already has a different "
                f"live answer on file.\n"
                f"Asked: {str(cycle.get('question', ''))[:400]}\n"
                + (f"On file: COULD NOT BE READ -- the scan failed, so this may "
                   f"not be a real conflict; the answer was held rather than "
                   f"risk overwriting canon.\n"
                   if existing == _COLLISION_READ_ERROR
                   else f"On file: {existing[:400]}\n") +
                f"New answer: {answer[:400]}\n"
                f"Which is authoritative is a call, not a fact -- so nothing was "
                f"written to known-answers."),
            "source": "knowledge_check_d128",
            "cycle_id": cycle_id,
        },
        "source_evidence": "",
    }
    try:
        from .decision_inbox import screen_decision
        excluded, why = screen_decision(candidate)
    except Exception:  # noqa: BLE001 -- fail closed
        log.warning("knowledge_check: decision screen errored -- not routing",
                    exc_info=True)
        return None
    if excluded:
        log.info("knowledge_check: collision for %s not routed (screen: %s)",
                 cycle_id, why)
        return None
    try:
        from .knowledge_review import UPDATE_TYPE_DECISION, propose_update
        propose_update(update_id=update_id, update_type=UPDATE_TYPE_DECISION,
                       description=candidate["description"],
                       payload=candidate["payload"], source_evidence="",
                       confidence="MED")
    except Exception:  # noqa: BLE001
        log.warning("knowledge_check: collision route failed for %s", cycle_id,
                    exc_info=True)
        return None
    return update_id


def promote(cycle: dict[str, Any], today: str | None = None) -> tuple[str, str]:
    """Write a confirmed answer. Returns (outcome, human_message).

    Outcomes: promoted | held | refused | empty.

    ORDER IS THE CONTRACT:
      1. PHI gate (a no-op under the decided posture -- see PHI_GATE_ANSWERS).
      2. Collision -> decisions lane, write NOTHING. Canon is never overwritten.
      3. known-answers write. THIS IS THE ONE THAT MATTERS.

    STRICTLY LOCAL, NO NETWORK. The Airtable Training Log mirror is deliberately
    NOT done here: this runs under _CLAIM_LOCK, and an HTTP call with retries
    could hold that lock for tens of seconds -- stalling every other person's
    capture and every other button tap in the whole bot process. The caller
    fires the mirror after releasing the lock (see process_confirm_tap), which
    also keeps the ordering the kickoff asks for: known-answers is the write that
    matters, the mirror is best-effort and can never fail the promote.
    """
    today = today or az_date()
    # `dry` must mean NO WRITES, everywhere (D-051, three lenses). The runner
    # half honoured it; the bot half gated on enabled(), which is true for dry --
    # so `on -> dry` (the natural "pause it, something looks wrong" move) left
    # every outstanding card still able to write to an always-injected file and
    # fire the Airtable mirror, in a mode .env.example documents as write-free.
    # Gated HERE rather than only at the app layer so every caller inherits it.
    if not live():
        return "refused", ("The knowledge check isn't in live mode right now, so "
                           "I haven't saved anything.")
    answer = normalize_answer(cycle.get("answer", ""))
    if not answer:
        return "empty", "There was nothing staged to save."
    entity = str(cycle.get("entity", "FNDR") or "FNDR").strip().upper()
    question = str(cycle.get("question", "")).strip()

    blocked, reason = phi_blocked(f"{question}\n{answer}")
    if blocked:
        return "refused", reason

    collides, existing = detect_collision(entity, question, answer, today=today)
    if collides:
        route_collision_to_decisions(cycle, answer, existing)
        if existing == _COLLISION_READ_ERROR:
            return "held", ("I couldn't read what I already have on file, so I've "
                            "held that rather than risk overwriting something.")
        return "held", ("Thanks -- that differs from what I already have on file, "
                        "so I've flagged it for Harrison instead of overwriting it.")

    try:
        with _known_answers_lock():
            path = _append_entry(entity, build_entry_lines(cycle, answer, today),
                                 cycle_id=str(cycle.get("cycle_id", "")))
    except Exception:  # noqa: BLE001
        log.error("knowledge_check: known-answers write failed for %s",
                  cycle.get("cycle_id"), exc_info=True)
        return "refused", "Something went wrong saving that -- nothing was written."

    log.info("knowledge_check: promoted %s -> %s", cycle.get("cycle_id"), path.name)
    return "promoted", "Saved -- thanks, that's genuinely useful."


def _mirror_to_training_log(cycle: dict[str, Any], answer: str,
                            today: str) -> tuple[bool, str]:
    try:
        from .connectors import airtable_training_log as atl
        person = roster_member(str(cycle.get("user", ""))) or {}
        name = person.get("name") or str(cycle.get("user", ""))
        return atl.log_knowledge_check(
            session=f"Knowledge check -- {cycle.get('kpi') or 'question'}",
            person=name, date=today,
            outcome=f"Q: {str(cycle.get('question', '')).strip()}\nA: {answer}")
    except Exception as exc:  # noqa: BLE001 -- best-effort, never raises upward
        return False, str(exc)


def process_confirm_tap(cycle_id: str, actor_id: str,
                        fingerprint: str = "") -> tuple[str, str]:
    """"Save it" on the confirm card: claim, then write. Exactly once.

    The whole claim-and-write runs under _CLAIM_LOCK and re-folds the log inside
    it, so a double-tap (or a tap racing a typed reply) can reach the write at
    most once -- the loser reads 'already handled', which is an idempotent ack
    rather than a false 'nothing happened'.
    """
    with _CLAIM_LOCK:
        outcome, cyc = _authorize_tap(cycle_id, actor_id, (STATE_CAPTURED,),
                                      fingerprint)
        if outcome != "ok":
            return outcome, _tap_message(outcome)
        result, message = promote(cyc)
        if result == "promoted":
            append_event("promoted", cycle_id=cycle_id, user=actor_id,
                         date=cyc.get("date"), entity=cyc.get("entity"),
                         tier=cyc.get("tier"), item_key=cyc.get("item_key"))
        elif result == "held":
            append_event("held_collision", cycle_id=cycle_id, user=actor_id,
                         date=cyc.get("date"))
        # 'refused'/'empty' leave the cycle CAPTURED so the person can reword;
        # end-of-day expiry closes it if they do not.

    # OUTSIDE the lock: this is an HTTP call with retries, and holding the claim
    # lock across it would stall every other capture and tap in the process. The
    # cycle is already durably PROMOTED, so a mirror failure is a logged
    # discrepancy against a completed write -- never a lost answer.
    if result == "promoted":
        ok, detail = _mirror_to_training_log(
            cyc, normalize_answer(cyc.get("answer", "")), az_date())
        if not ok:
            log.warning("knowledge_check: Training Log mirror failed for %s (%s) -- "
                        "known-answers write stands", cycle_id, detail)
    return result, message


# ── Participation reporting (feeds Hannah's weekly training-readiness audit) ─

def participation_stats(days: int = 30, today: str | None = None) -> dict[str, Any]:
    """Per-person and org-wide participation over the trailing window.

    Deliberately NOT a score. The pilot's purpose is gap-fill, not a compliance
    quiz (spec section 5), so what is counted is asked / answered / confirmed /
    skipped -- never right-or-wrong.

    `pool_exhausted` (a `skipped_no_gap` day) is counted SEPARATELY from
    `no_response`, because for Tommy/Justin/Hannah/Jerry a high skip count is the
    documented consequence of already being fully system-read (spec 3.4), not
    disengagement. Collapsing the two would make four people look like they were
    ignoring their DMs.
    """
    today = today or az_date()
    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days)).date().isoformat()
    state = fold_state()
    per: dict[str, dict[str, Any]] = {}

    def _row(user: str) -> dict[str, Any]:
        p = roster_member(user)
        return per.setdefault(user, {
            "name": (p or {}).get("name", user), "entity": (p or {}).get("entity", ""),
            "asked": 0, "answered": 0, "confirmed": 0, "user_skipped": 0,
            "no_response": 0, "no_confirm": 0, "pool_exhausted": 0,
            "held_collision": 0, "failed": 0, "reserved_never_sent": 0,
        })

    for (user, date), _ in state["no_gap"].items():
        if date >= cutoff:
            _row(user)["pool_exhausted"] += 1

    for cyc in state["cycles"].values():
        if str(cyc.get("date", "")) < cutoff:
            continue
        r = _row(str(cyc.get("user", "")))
        st = cyc.get("state")
        if st == STATE_RESERVED:
            r["reserved_never_sent"] += 1
            continue
        if st == STATE_FAILED:
            r["failed"] += 1
            continue
        r["asked"] += 1
        if cyc.get("answer"):
            r["answered"] += 1
        if st == STATE_PROMOTED:
            r["confirmed"] += 1
        elif st == STATE_SKIPPED:
            r["user_skipped"] += 1
        elif st == STATE_HELD:
            r["held_collision"] += 1
        elif st == STATE_EXPIRED:
            if cyc.get("answer"):
                r["no_confirm"] += 1
            else:
                r["no_response"] += 1

    totals = {k: sum(v.get(k, 0) for v in per.values())
              for k in ("asked", "answered", "confirmed", "user_skipped",
                        "no_response", "no_confirm", "pool_exhausted",
                        "held_collision", "failed", "reserved_never_sent")}
    return {"window_days": days, "since": cutoff, "people": per, "totals": totals}


def participation_report(days: int = 30, today: str | None = None) -> list[str]:
    """The stats as printable lines (runner `--report`, and the weekly audit)."""
    s = participation_stats(days=days, today=today)
    t = s["totals"]
    lines = [f"Knowledge check -- last {days}d (since {s['since']})",
             f"  asked {t['asked']} | answered {t['answered']} | "
             f"confirmed {t['confirmed']} | skipped-by-user {t['user_skipped']}",
             f"  no-response {t['no_response']} | no-confirm {t['no_confirm']} | "
             f"pool-exhausted {t['pool_exhausted']} | held {t['held_collision']}"]
    if t["failed"] or t["reserved_never_sent"]:
        # Anomalies, not participation -- surfaced so a silent delivery loss is
        # visible rather than reading as somebody ignoring their DMs.
        lines.append(f"  ANOMALIES: send-failed {t['failed']} | "
                     f"reserved-never-sent {t['reserved_never_sent']}")
    for user, r in sorted(s["people"].items(), key=lambda kv: kv[1]["name"]):
        lines.append(
            f"  {r['name']:<22} [{r['entity']:<8}] asked {r['asked']:>2} "
            f"answered {r['answered']:>2} confirmed {r['confirmed']:>2} "
            f"pool-exhausted {r['pool_exhausted']:>2}")
    return lines


def phi_blocked(text: str) -> tuple[bool, str]:
    """The PHI gate, governed entirely by PHI_GATE_ANSWERS.

    Returns (blocked, reason). With the flag False -- the decided posture -- this
    is a no-op for EVERY entity, which is the whole point: LEX is treated
    identically to everyone else, per Harrison 2026-08-11. See the module
    docstring before changing this.
    """
    if not PHI_GATE_ANSWERS:
        return False, ""
    try:
        from .phi_guard import (is_clinical_phi, is_lex_billing_status_phi,
                                is_phi_risk)
        blob = str(text or "")
        if is_phi_risk(blob) or is_clinical_phi(blob) or is_lex_billing_status_phi(blob):
            return True, "answer looks like PHI -- not persisted"
    except Exception:  # noqa: BLE001 -- if the gate is ARMED it fails CLOSED
        log.error("knowledge_check: PHI screen errored -- refusing (fail-closed)",
                  exc_info=True)
        return True, "PHI screen errored -- not persisted"
    return False, ""
