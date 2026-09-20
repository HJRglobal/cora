"""Repeat-signal escalation (Code #13 slice 9b; D-302 ruled 2026-09-10, D-310 from
the 2026-09-19 audit section 3).

THE CLASS THIS RETIRES. A monitor that finds the same fact wrong every fire and
says so on the same surface every time is not escalating -- it is repeating, and a
repeated alarm trains its reader to ignore it. The audit's live instance: the
decision-gate check recorded its OWN post as a "delivery", which silenced the
alarm for 8 days, after which it fired again -- an 8-9 day cadence with no human
in the loop and no change of surface. The expected-invoice check is the other
instance: Google Ads has been MISSING every month since the check shipped, on the
same channel line, to no owner.

THE CONTRACT. Every monitor that emits to a human surface registers a
`signal_key` (task + subject + entity). A signal that repeats on CONSECUTIVE
FIRES with no acknowledgement escalates by CHANGING surface/owner, never by
repeating:

  1st fire  -> the lane's normal surface (the caller emits as today)
  2nd       -> the owner + ONE line in Harrison's daily briefing (tier2_signals)
  3rd       -> ONE propose-only decision card (Accept / Dismiss on the existing
               decision_capture card; the options are TEXT: keep with owner /
               retire the signal / re-scope it) and SUPPRESSION at the original
               surface until acknowledged
  ack/clear -> reset; the next fire is tier 1 again

"Consecutive" counts DISTINCT fire_ids (a period for the monthly invoice check, a
date for the nightly gate check). Re-firing with a fire_id already recorded is
idempotent -- same tier, no new row -- so a re-run inside one fire never escalates.

ACKS. (a) Harrison's Accept OR Dismiss tap on the tier-3 card
(knowledge_review.process_decision_tap reads payload.signal_key and calls ack());
(b) a caller-observed human ack (a threaded reply on a decision alert) via ack();
(c) the underlying fact clearing via clear(); (d) the tier-3 card reaching a
terminal state by ANY other path (emoji-reaction executor, render-time LEX/PHI
dismiss, self-heal, hand edit) -- fire() re-reads the card on every suppressed
fire and records an implicit ack (via=card-resolved-out-of-band) when it is
absent or no longer PENDING, then fires tier 1. Each is a ledger event.

SUPPRESSION IS TIED TO A CARD. If the tier-3 card cannot be minted (the
LEX/PHI decision screen excluded it, or the proposal row is not PENDING so no
tap can ever arrive), nothing is suppressed: the caller keeps its normal surface
(pass-through). A suppression with nothing to acknowledge would be the exact
silence this module exists to end -- the ladder row's demotion trigger names it.
The tie holds for the LIFE of the suppression, not only at mint (see ack (d)).

CALLERS RECORD A FIRE ONLY AFTER ITS SURFACE DELIVERED. A fire that reached no
human is not an unacknowledged alarm (D-051 EF-4): a consumer computes the
outcome with dry_run=True, delivers on its normal surface, and records the fire
only when that surface actually sent -- except at tier 3, where the card mint
IS the surface and the fire is recorded regardless of Slack. Three Slack-down
months must never climb the ladder and mute the first delivery that works.

STORAGE. ONE append-only ledger, logs/repeat-signals.jsonl (same shape and
reasons as run_marker: append-only, no read-modify-write, multi-process safe,
under logs/ so compact_logs trims it). State is FOLDED from the ledger at read
time -- the ledger is the state. Write failure is loud: REPEAT_SIGNAL_WRITE_FAILING
is matched by nightly_health_check._CRITICAL_LOG_PATTERNS.

NO LLM CALLS. Deterministic by construction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_LEDGER = _REPO_ROOT / "logs" / "repeat-signals.jsonl"

#: Emitted on a ledger write failure (matched by the nightly health check).
WRITE_FAIL_TOKEN = "REPEAT_SIGNAL_WRITE_FAILING"

#: Arizona: fixed UTC-7, no DST (the repo's convention for "which day").
_AZ = timezone(timedelta(hours=-7))

TIER_NORMAL = 1
TIER_OWNER_BRIEFING = 2
TIER_CARD = 3

EVENT_FIRE = "fire"
EVENT_SUPPRESSED = "suppressed"
EVENT_ACK = "ack"
EVENT_CLEAR = "clear"

#: Harrison -- the fail-closed owner when a caller cannot resolve one.
HARRISON_SLACK_ID = "U0B2RM2JYJ1"


def ledger_path() -> Path:
    """Resolved PER CALL (never snapshotted at import) so tests redirect it."""
    return Path(os.environ.get("REPEAT_SIGNAL_LEDGER_PATH", "") or _DEFAULT_LEDGER)


def make_key(task: str, subject: str, entity: str) -> str:
    """signal_key = task|subject|entity. Exact, not fuzzy: a changed subject IS a
    new signal (it must never be suppressed by the old one)."""
    return "|".join(str(x or "").strip() for x in (task, subject, entity))


@dataclass
class Outcome:
    tier: int
    consecutive: int
    suppressed: bool
    card_update_id: str | None
    #: False when the fire_id was already recorded (idempotent re-fire) or on a
    #: dry run -- no ledger row was written.
    recorded: bool = True


@dataclass
class _State:
    consecutive: int = 0
    fire_ids: set[str] = field(default_factory=set)
    suppressed: bool = False
    card_update_id: str | None = None
    #: How many ack/clear resets this key has seen -- discriminates the card id
    #: across cycles (see _card_update_id).
    cycle: int = 0
    last_ts: str = ""
    subject: str = ""
    entity: str = ""
    owner_slack_id: str = ""
    owner_name: str = ""
    normal_surface: str = ""


# ── ledger I/O ───────────────────────────────────────────────────────────────

def _append(row: dict[str, Any]) -> bool:
    row = dict(row)
    row.setdefault("ts", datetime.now(timezone.utc).isoformat())
    path = ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 -- observability must never raise
        log.error("%s key=%s event=%s path=%s err=%s", WRITE_FAIL_TOKEN,
                  row.get("signal_key"), row.get("event"), path, exc)
        return False


def read_rows(path: Path | None = None) -> list[dict[str, Any]]:
    """Every ledger row in append order. Malformed lines are skipped."""
    p = Path(path) if path else ledger_path()
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(row, dict) and row.get("signal_key"):
                    rows.append(row)
    except Exception:  # noqa: BLE001
        return rows
    return rows


def _fold(rows: list[dict[str, Any]]) -> dict[str, _State]:
    """The ledger IS the state: fold every row, in order, per signal_key."""
    states: dict[str, _State] = {}
    for row in rows:
        key = str(row.get("signal_key") or "")
        st = states.setdefault(key, _State())
        event = str(row.get("event") or "")
        st.last_ts = str(row.get("ts") or st.last_ts)
        if event in (EVENT_ACK, EVENT_CLEAR):
            st.consecutive = 0
            st.fire_ids = set()
            st.suppressed = False
            st.card_update_id = None
            st.cycle += 1
            continue
        fid = str(row.get("fire_id") or "")
        if fid:
            st.fire_ids.add(fid)
        if event == EVENT_FIRE:
            try:
                st.consecutive = int(row.get("consecutive") or st.consecutive + 1)
            except (TypeError, ValueError):
                st.consecutive += 1
            st.suppressed = bool(row.get("suppressed"))
            st.card_update_id = row.get("card_update_id") or st.card_update_id
            st.subject = str(row.get("subject") or st.subject)
            st.entity = str(row.get("entity") or st.entity)
            st.owner_slack_id = str(row.get("owner_slack_id") or st.owner_slack_id)
            st.owner_name = str(row.get("owner_name") or st.owner_name)
            st.normal_surface = str(row.get("normal_surface") or st.normal_surface)
        elif event == EVENT_SUPPRESSED:
            st.suppressed = True
    return states


def _state(signal_key: str) -> _State:
    return _fold(read_rows()).get(signal_key, _State())


def state(signal_key: str) -> dict[str, Any]:
    """Read-only view of one signal's folded state (tests, reports)."""
    st = _state(signal_key)
    return {
        "consecutive": st.consecutive,
        "tier": min(st.consecutive, TIER_CARD) if st.consecutive else 0,
        "suppressed": st.suppressed,
        "card_update_id": st.card_update_id,
        "fire_ids": sorted(st.fire_ids),
        "cycle": st.cycle,
    }


def active_keys(prefix: str = "") -> list[str]:
    """Signal keys with a live (un-reset) count, optionally filtered by prefix.
    A caller uses this to CLEAR signals whose underlying fact has gone away."""
    return sorted(
        k for k, st in _fold(read_rows()).items()
        if (st.consecutive > 0 or st.suppressed) and k.startswith(prefix)
    )


# ── the tier-3 card ──────────────────────────────────────────────────────────

def _card_update_id(signal_key: str, cycle: int) -> str:
    """Deterministic: 'rs-' + sha256(signal_key)[:12], so re-fires inside one
    cycle are idempotent (propose_update short-circuits on update_id). A LATER
    cycle (after an ack reset) gets a '-<cycle>' suffix: without it the second
    cycle's proposal would collide with the first, already-resolved row,
    propose_update would return False, no card would render, and the signal
    would be suppressed with nothing to tap -- silence forever."""
    base = "rs-" + hashlib.sha256(signal_key.encode("utf-8")).hexdigest()[:12]
    return base if cycle <= 0 else f"{base}-{cycle}"


def _mint_card(signal_key: str, st: _State, *, subject: str, entity: str,
               owner_slack_id: str, owner_name: str) -> str | None:
    """Mint the ONE propose-only decision card, exactly like
    gap_autofill.route_disputed_to_decision_lane: decision_inbox.screen_decision
    first (LEX/PHI fail-closed), then knowledge_review.propose_update with
    UPDATE_TYPE_DECISION, confidence MED, the three options as TEXT. The
    existing Accept-to-inbox / Dismiss card IS the card; either tap acks.
    Returns the update_id when a PENDING card exists for this cycle, else None
    (and the caller must NOT suppress)."""
    who = owner_name or owner_slack_id or "unassigned"
    tag = f"[{entity}] " if entity else ""
    description = (
        f"{tag}Repeat signal -- 3rd consecutive fire with no acknowledgement: "
        f"{subject}. Options: keep with owner {who} / retire the signal / "
        f"re-scope it. Accept or Dismiss both acknowledge; the alarm at its "
        f"original surface is suppressed until you do."
    )
    candidate = {
        "description": description,
        "payload": {
            "entity": entity,
            "signal_key": signal_key,
            "source": "repeat_signal",
            "subject": subject,
            "owner_slack_id": owner_slack_id,
            "owner_name": owner_name,
            "options": [f"keep with owner {who}", "retire the signal", "re-scope it"],
            "consecutive": st.consecutive + 1,
        },
        "source_evidence": "",
    }
    try:
        from .decision_inbox import screen_decision
        excluded, why = screen_decision(candidate)
    except Exception:  # noqa: BLE001 -- fail closed
        log.warning("repeat_signal: decision screen errored for %s -- no card", signal_key,
                    exc_info=True)
        return None
    if excluded:
        log.warning("repeat_signal: tier-3 card for %s withheld by the decision screen "
                    "(%s) -- no suppression, the normal surface keeps alarming",
                    signal_key, why)
        return None
    update_id = _card_update_id(signal_key, st.cycle)
    try:
        from .knowledge_review import UPDATE_TYPE_DECISION, _find_update, propose_update
        proposed = propose_update(
            update_id=update_id,
            update_type=UPDATE_TYPE_DECISION,
            description=description,
            payload=candidate["payload"],
            source_evidence="",
            confidence="MED",
        )
        if not proposed:
            existing = _find_update(update_id)
            if existing is None or existing.get("state") != "PENDING":
                log.warning("repeat_signal: card %s already resolved/absent -- no tap can "
                            "arrive, so no suppression", update_id)
                return None
    except Exception:  # noqa: BLE001 -- never break the caller's lane
        log.warning("repeat_signal: card mint failed for %s", signal_key, exc_info=True)
        return None
    return update_id


def _card_still_pending(update_id: str | None) -> bool | None:
    """Is the tier-3 card behind a suppression still PENDING?

    True = a tap can still arrive (keep suppressing); False = the card is absent
    or terminal (implicit ack); None = could not read the proposal file, so the
    caller must NOT change state on it (see the suppressed branch of fire()).
    A suppression with no update_id at all is a card that never existed -> False.
    """
    if not update_id:
        return False
    try:
        from .knowledge_review import _find_update
        row = _find_update(str(update_id))
    except Exception:  # noqa: BLE001 -- unverifiable is not the same as resolved
        log.warning("repeat_signal: could not verify card %s -- suppression kept",
                    update_id, exc_info=True)
        return None
    return bool(row is not None and row.get("state") == "PENDING")


# ── the API ──────────────────────────────────────────────────────────────────

def fire(signal_key: str, *, fire_id: str, subject: str, entity: str,
         owner_slack_id: str, normal_surface: str, owner_name: str = "",
         dry_run: bool = False) -> Outcome:
    """Record one fire of `signal_key` and return where it stands.

    `fire_id` is the DISTINCT identity of this fire (period / date). Re-firing
    with a recorded fire_id is idempotent. `dry_run=True` computes the outcome
    without writing a row or minting a card (a dry run is a claim about every
    write site -- this module has two).
    """
    key = str(signal_key or "").strip()
    fid = str(fire_id or "").strip()
    st = _state(key)
    if not key or not fid:
        log.warning("repeat_signal: fire ignored -- empty key or fire_id (%r, %r)", key, fid)
        return Outcome(tier=0, consecutive=st.consecutive, suppressed=False,
                       card_update_id=None, recorded=False)

    if fid in st.fire_ids:
        return Outcome(tier=min(st.consecutive, TIER_CARD) or TIER_NORMAL,
                       consecutive=st.consecutive, suppressed=st.suppressed,
                       card_update_id=st.card_update_id, recorded=False)

    if st.suppressed:
        # SUPPRESSION IS TIED TO A CARD -- and the tie is re-checked on EVERY
        # suppressed fire, not only at mint time (D-051 EF-2). The card can reach
        # a terminal state without passing through process_decision_tap: the
        # emoji-reaction executor, the render-time LEX/PHI dismiss, the Step-0
        # self-heal, a hand edit. None of those knew about the signal, so the
        # ledger still said "suppressed" while nothing was left to tap -- the
        # exact silence this module exists to end. A card that is absent or no
        # longer PENDING is an implicit ack: record it, reset, and fall through
        # to a fresh tier-1 fire. A card that CANNOT be read (transient import /
        # I/O trouble) keeps the suppression: un-suppressing on an error would
        # start a new cycle and mint a SECOND card beside a live one, which is
        # the ladder row's demotion trigger.
        pending = _card_still_pending(st.card_update_id)
        if pending is False:
            if not dry_run:
                _append({"event": EVENT_ACK, "signal_key": key,
                         "via": "card-resolved-out-of-band",
                         "card_update_id": st.card_update_id})
            log.info("repeat_signal: card %s for %s resolved out-of-band -- suppression "
                     "lifted, next fire is tier 1", st.card_update_id, key)
            st = _State(cycle=st.cycle + 1, subject=st.subject, entity=st.entity,
                        owner_slack_id=st.owner_slack_id, owner_name=st.owner_name,
                        normal_surface=st.normal_surface)
        else:
            if not dry_run:
                _append({"event": EVENT_SUPPRESSED, "signal_key": key, "fire_id": fid,
                         "subject": str(subject or "")[:300], "entity": str(entity or ""),
                         "card_update_id": st.card_update_id})
            return Outcome(tier=TIER_CARD, consecutive=st.consecutive + 1, suppressed=True,
                           card_update_id=st.card_update_id, recorded=not dry_run)

    consecutive = st.consecutive + 1
    tier = min(consecutive, TIER_CARD)
    card_id: str | None = None
    suppressed = False
    if tier == TIER_CARD:
        if dry_run:
            card_id = _card_update_id(key, st.cycle)
            suppressed = True
        else:
            card_id = _mint_card(key, st, subject=str(subject or ""), entity=str(entity or ""),
                                 owner_slack_id=str(owner_slack_id or ""),
                                 owner_name=str(owner_name or ""))
            suppressed = card_id is not None
    if dry_run:
        return Outcome(tier=tier, consecutive=consecutive, suppressed=suppressed,
                       card_update_id=card_id, recorded=False)
    ok = _append({
        "event": EVENT_FIRE, "signal_key": key, "fire_id": fid,
        "tier": tier, "consecutive": consecutive, "suppressed": suppressed,
        "card_update_id": card_id, "subject": str(subject or "")[:300],
        "entity": str(entity or ""), "owner_slack_id": str(owner_slack_id or ""),
        "owner_name": str(owner_name or "")[:80],
        "normal_surface": str(normal_surface or "")[:80],
    })
    return Outcome(tier=tier, consecutive=consecutive, suppressed=suppressed,
                   card_update_id=card_id, recorded=ok)


def ack(signal_key: str, *, via: str) -> bool:
    """A human acknowledged the signal: reset the count and lift suppression.
    Returns False (and writes nothing) when the signal has no live state -- a
    reset of nothing is not an event."""
    key = str(signal_key or "").strip()
    st = _state(key)
    if not key or not (st.consecutive or st.suppressed):
        return False
    return _append({"event": EVENT_ACK, "signal_key": key, "via": str(via or "")[:80],
                    "card_update_id": st.card_update_id})


def clear(signal_key: str, *, why: str) -> bool:
    """The underlying fact cleared (invoice filed, gate met/closed): reset the
    count and lift suppression. Same no-live-state rule as ack()."""
    key = str(signal_key or "").strip()
    st = _state(key)
    if not key or not (st.consecutive or st.suppressed):
        return False
    return _append({"event": EVENT_CLEAR, "signal_key": key, "why": str(why or "")[:200],
                    "card_update_id": st.card_update_id})


# ── the briefing read (tier 2) ───────────────────────────────────────────────

def _az_date(ts: str) -> date | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_AZ).date()


#: The briefing window over the tier-2 fire row. THREE days, not one (D-051 EF-5):
#: "Cora - Daily Briefing" fires Monday-Friday only
#: (deployment/setup-daily-briefing-task.ps1: -Weekly -DaysOfWeek Monday..Friday),
#: so a tier-2 fire on a Friday or Saturday has no briefing the next day -- with a
#: 1-day window the line never rendered and the ladder went tier 1 -> tier 3 with
#: the tier-2 surface silently skipped (the invoice check's day-9 fire IS a Friday
#: in Oct 2026, Apr 2027 and Jul 2027). Friday -> Monday is three days.
TIER2_BRIEFING_LOOKBACK_DAYS = 3


def tier2_signals(today: date | None = None, *,
                  lookback_days: int = TIER2_BRIEFING_LOOKBACK_DAYS) -> list[dict[str, Any]]:
    """Signals at tier 2 for Harrison's briefing line: their tier-2 FIRE row is
    stamped (Arizona date) within [today - lookback_days, today] AND the signal
    still stands at tier 2 (not yet fired again, not acked/cleared).

    The window is wider than "today" on purpose: the 07:30 briefing runs BEFORE
    the 08:45 gate check and the 09:38 invoice check, so a strictly same-day
    read would never see either consumer's tier-2 fire; and the briefing is
    Mon-Fri only, so a Friday/Saturday fire must survive to Monday
    (TIER2_BRIEFING_LOOKBACK_DAYS). The "still at tier 2" requirement is what
    keeps the ladder honest inside the window: the next fire moves the signal
    to tier 3 (a different surface) and the line drops; an ack/clear drops it too.
    Within the window the line may render on up to three consecutive weekday
    briefings -- bounded, and by date, never forever.
    """
    today = today or datetime.now(_AZ).date()
    rows = read_rows()
    states = _fold(rows)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in reversed(rows):
        if row.get("event") != EVENT_FIRE or int(row.get("tier") or 0) != TIER_OWNER_BRIEFING:
            continue
        key = str(row.get("signal_key") or "")
        if key in seen:
            continue
        st = states.get(key)
        if st is None or st.consecutive != TIER_OWNER_BRIEFING or st.suppressed:
            continue
        day = _az_date(str(row.get("ts") or ""))
        if day is None or not (today - timedelta(days=lookback_days) <= day <= today):
            continue
        seen.add(key)
        out.append({
            "signal_key": key,
            "subject": str(row.get("subject") or ""),
            "entity": str(row.get("entity") or ""),
            "owner_slack_id": str(row.get("owner_slack_id") or ""),
            "owner_name": str(row.get("owner_name") or ""),
            "normal_surface": str(row.get("normal_surface") or ""),
            "fire_id": str(row.get("fire_id") or ""),
        })
    out.sort(key=lambda r: r["signal_key"])
    return out


def format_briefing_lines(signals: list[dict[str, Any]]) -> list[str]:
    """The deterministic tier-2 lines appended to HARRISON's briefing, after the
    LLM-synthesized body (never inside it -- the model has dropped labeled
    sections before)."""
    lines: list[str] = []
    for s in signals:
        owner = s.get("owner_name") or s.get("owner_slack_id") or "unassigned"
        lines.append(f"Repeat signal (2nd consecutive fire): {s.get('subject')} -- owner {owner}")
    return lines
