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
scrub, no PHI gating, on questions or answers. That posture lives in exactly one
place -- PHI_GATE_ANSWERS below -- so it is one flip to reverse if legal comes
back. This module deliberately does NOT reuse gap_autofill.apply_known_answer:
that function's PHI refusal is load-bearing for the mining path, and adding a
bypass parameter would weaken a shared writer for every one of its callers.
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
    return _REPO_ROOT / "design" / "known-answers"


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


def _parse_iso(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


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
    return problems


# ── Append-only event log ───────────────────────────────────────────────────

_APPEND_LOCK = Lock()


def append_event(event: str, **fields: Any) -> dict[str, Any]:
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
        log.error("knowledge_check: event append failed (%s)", event, exc_info=True)
    return row


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
    """Fold the event log into effective state.

    Returns:
      {
        "cycles":     {cycle_id: {...}},
        "by_day":     {(user, date): cycle_id},          # send ledger
        "last_asked": {(user, item_key): date},          # cooldown
        "no_gap":     {(user, date): True},              # skipped_no_gap ledger
      }

    TERMINAL STICKINESS is the load-bearing property here (D-096): once a cycle
    is terminal, a later event -- a duplicated line, an out-of-order append from
    the other process, a replayed handler -- records in the log but CANNOT move
    the state back to live. Without this, appending any non-terminal event to a
    finished cycle would resurrect it and it could be answered/promoted twice.
    """
    cycles: dict[str, dict[str, Any]] = {}
    by_day: dict[tuple[str, str], str] = {}
    last_asked: dict[tuple[str, str], str] = {}
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
                "gap_ts": str(row.get("gap_ts", "") or ""),
                "state": STATE_RESERVED,
                "answer": "",
                "message_ts": "",
                "channel": "",
                "created_ts": row.get("ts"),
                "edited": False,
            }
            cycles[cycle_id] = cyc

        # Terminal is sticky: record nothing further onto the state.
        if cyc["state"] in TERMINAL_STATES:
            continue

        new_state = _EVENT_STATES.get(ev)
        if new_state:
            cyc["state"] = new_state
        if ev == "recaptured":
            cyc["edited"] = True
        for fld in ("answer", "message_ts", "channel", "question", "entity",
                    "item_key", "gap_ts", "tier"):
            if row.get(fld) not in (None, ""):
                cyc[fld] = row[fld]
        cyc["last_ts"] = row.get("ts")

    for cid, cyc in cycles.items():
        u, d, k = cyc.get("user"), cyc.get("date"), cyc.get("item_key")
        if u and d:
            by_day[(u, d)] = cid
            if k:
                prev = last_asked.get((u, k))
                if prev is None or d > prev:
                    last_asked[(u, k)] = d

    return {"cycles": cycles, "by_day": by_day,
            "last_asked": last_asked, "no_gap": no_gap}


def handled_today(state: dict[str, Any], user: str, date: str) -> bool:
    """True when this person already has a question (or a recorded skip) for this
    AZ date. THE idempotency check -- consulted before every send."""
    if state["no_gap"].get((user, date)):
        return True
    cid = state["by_day"].get((user, date))
    if not cid:
        return False
    return state["cycles"][cid].get("state") in HANDLED_STATES


def live_cycle_for(state: dict[str, Any], user: str) -> dict[str, Any] | None:
    """The one in-flight cycle for a person, if any.

    "In flight" means ASKED (awaiting an answer) or CAPTURED (awaiting a confirm
    tap). Terminal cycles are invisible here, so a person who already confirmed
    today cannot have their next unrelated DM swallowed as an answer.
    """
    best: dict[str, Any] | None = None
    for cyc in state["cycles"].values():
        if cyc.get("user") != user:
            continue
        if cyc.get("state") not in (STATE_ASKED, STATE_CAPTURED):
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
        out.append(append_event("expired", cycle_id=cyc["cycle_id"],
                                user=cyc.get("user"), date=cyc.get("date"),
                                reason=reason))
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


_NON_ANSWER_RE = re.compile(
    r"^\s*(no idea|not my area|don'?t know|dunno|no clue|n/?a|nothing|skip)\s*$",
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
        state = ga.load_state()
        if gap_ts in state:
            return False  # somebody already claimed it
        state[gap_ts] = {
            "state": "asked",
            "via": "knowledge_check",
            "cycle_id": cycle_id,
            "at": _now_iso(),
        }
        ga.save_state(state)
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
        return {
            "tier": 2,
            "item_key": f"gap:{gap.get('ts', '')}",
            "gap_ts": str(gap.get("ts", "") or ""),
            "question": str(gap.get("question", "") or "")[:400],
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


def build_confirm_blocks(answer: str, cycle_id: str, question: str = "") -> list[dict]:
    from . import confirm_cards as _cc
    return [
        *_cc.chunk_mrkdwn_sections(_sanitize(confirm_text(answer, question))),
        {"type": "actions",
         "block_id": f"cora_kc_confirm_{cycle_id}"[:255],
         "elements": [
             {"type": "button", "action_id": ACTION_CONFIRM_ANSWER, "style": "primary",
              "text": {"type": "plain_text", "text": "Save it"},
              "value": cycle_id},
             {"type": "button", "action_id": ACTION_EDIT_ANSWER,
              "text": {"type": "plain_text", "text": "Let me reword"},
              "value": cycle_id},
             {"type": "button", "action_id": ACTION_SKIP_ANSWER,
              "text": {"type": "plain_text", "text": "Skip"},
              "value": cycle_id},
         ]},
    ]


def terminal_blocks(text: str) -> list[dict]:
    from . import confirm_cards as _cc
    return _cc.chunk_mrkdwn_sections(_sanitize(text))


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
        return cyc if cyc.get("message_ts") == thread_ts else None
    return cyc if allow_toplevel else None


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
                   allowed_states: tuple[str, ...]) -> tuple[str, dict[str, Any] | None]:
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


def process_skip_answer_tap(cycle_id: str, actor_id: str) -> tuple[str, str]:
    """"Skip" on the CONFIRM card: the staged answer is discarded unwritten."""
    with _CLAIM_LOCK:
        outcome, cyc = _authorize_tap(cycle_id, actor_id, (STATE_CAPTURED,))
        if outcome != "ok":
            return outcome, _tap_message(outcome)
        append_event("skipped_by_user", cycle_id=cycle_id, user=actor_id,
                     date=cyc.get("date"), reason="skip_at_confirm")
    return "skipped", "Got it -- nothing saved."


def process_edit_tap(cycle_id: str, actor_id: str) -> tuple[str, str]:
    """"Let me reword": stay in CAPTURED and wait for a fresh typed answer.

    Deliberately does NOT open a modal. The whole capture surface is free prose
    in a DM; a second input mechanism would be a parallel path with its own
    failure modes for no benefit. The next DM replaces the staged answer.
    """
    outcome, _cyc = _authorize_tap(cycle_id, actor_id, (STATE_CAPTURED,))
    if outcome != "ok":
        return outcome, _tap_message(outcome)
    return "editing", ("Sure -- send it again however you'd like it worded, and "
                       "I'll show you the new version.")


def _tap_message(outcome: str) -> str:
    return {
        "orphaned": "I don't have a record of that question anymore.",
        "not_authorized": "That question was for someone else.",
        "already_handled": "That one's already handled.",
        "not_live": "That question isn't waiting on anything right now.",
    }.get(outcome, "Something went wrong there.")


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
