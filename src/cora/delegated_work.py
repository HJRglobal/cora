"""Cora delegated work, Phase 1 -- job store + intake + quota/envelope + fold.

Design of record: ``_shared/projects/cora/2026-08-01_fndr_cora-delegated-work-phase1-design.md``
(LOCKED 2026-08-01; D-051 design review folded in). Build handoff:
``_notes/2026-08-01_fndr_cora-code-prompt-delegated-work-phase1.md``.

Teammates delegate bounded, artifact-producing jobs (research brief, spreadsheet
build, creator shortlist, document draft) via the ``cora_delegate_work`` staged
tool; a script-side runner (``scripts/run_delegated_work_runner.py`` +
``delegated_worker.py``) executes them asynchronously with the REQUESTER's scope
enforced in code; delivery is a guarded Slack summary + a Drive artifact.

Core invariant (design section 1): a delegated job can do nothing the requester
could not do by asking Cora in that channel. This module owns intake screening
(guard parity), the append-only job ledgers, quota + envelope enforcement, and
the Harrison HELD lane. Execution lives in ``delegated_worker`` (never imported
by the bot process).

SPLIT LEDGERS, SINGLE WRITER PER FILE (design section 6): the threading lock is
per-process and Windows ``open("a")`` appends are not atomic across processes,
so the BOT appends only to ``delegated-work.jsonl`` and the RUNNER appends only
to ``delegated-work-runner.jsonl``. The fold merges both by timestamp. Terminal
states never resurrect (the cq-dad80c0011c9 resurrect-trap class); the ONE
allowed post-terminal event is ``artifact_homed``, which mutates artifact
metadata only, never state.

Rollout: ``CORA_DELEGATED_WORK = off | log | live`` (default ``off``). NOTE
(the code_queue_level lesson, verbatim): the always-on bot loads ``.env`` ONCE
at startup, so editing the ``.env`` FILE does NOT change a running bot's value
-- freshly-spawned SCRIPTS (the runner) re-read it at import. To flip the BOT:
change the value AND restart. The operational kill switch is ``.env off`` + a
restart (``off`` without restart stops the runner but not bot-side intake).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import phi_guard
from .code_queue import HARRISON_ID  # single source -- never redeclared (design 8.8)

log = logging.getLogger("cora.delegated_work")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
ARCHETYPES: tuple[str, ...] = (
    "research_brief", "spreadsheet_build", "creator_shortlist", "doc_draft",
)
DELIVERABLES: tuple[str, ...] = ("md", "xlsx")

# States (design section 6). REQUESTED is a transient fold-internal state that is
# never observed externally: `requested` + (`queued`|`held`) append in ONE lock.
STATE_HELD = "HELD"
STATE_QUEUED = "QUEUED"
STATE_RUNNING = "RUNNING"
STATE_DELIVERED = "DELIVERED"
STATE_FAILED = "FAILED"
STATE_CANCELLED = "CANCELLED"
STATE_EXPIRED = "EXPIRED"
STATE_SIMULATED = "SIMULATED"
TERMINAL_STATES: frozenset[str] = frozenset({
    STATE_DELIVERED, STATE_FAILED, STATE_CANCELLED, STATE_EXPIRED, STATE_SIMULATED,
})
NON_TERMINAL_STATES: frozenset[str] = frozenset({
    "REQUESTED", STATE_HELD, STATE_QUEUED, STATE_RUNNING,
})

# Events, per writer (single-writer-per-file invariant).
BOT_EVENTS: frozenset[str] = frozenset({
    "requested", "held", "released", "queued", "cancelled",
    # DM-card bookkeeping (observability, bot-lane): the 5/day cap must be durable.
    "card_sent", "card_held",
})
RUNNER_EVENTS: frozenset[str] = frozenset({
    "started", "delivering", "delivered", "artifact_homed", "failed",
    "expired", "simulated",
    # Overflow-digest bookkeeping (runner-lane).
    "card_flushed",
})

BRIEF_MAX_CHARS = 4_000        # persisted-brief cap (bounds ledger line size)
TITLE_MAX_CHARS = 80
MAX_HELD_CARDS_PER_DAY = 5     # storm cap on Harrison HELD cards (code_queue pattern)
EXPIRE_HOURS = 48              # QUEUED jobs unclaimed this long expire (runner is sole expirer)

# Block Kit action ids (own namespace; app.py wrappers are I/O only).
ACTION_RELEASE = "delegated_work_release"
ACTION_DISMISS = "delegated_work_dismiss"

# Arizona is UTC-7 year-round (no DST) -- quota days + envelope months bucket
# in AZ local time (design section 10).
_AZ_TZ = timezone(timedelta(hours=-7))

# ─────────────────────────────────────────────────────────────────────────────
# Paths + locks (single writer per file; each process holds only its own lock)
# ─────────────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATE_DIR = _REPO_ROOT / "data" / "state"
_BOT_LEDGER = _STATE_DIR / "delegated-work.jsonl"
_RUNNER_LEDGER = _STATE_DIR / "delegated-work-runner.jsonl"
_STAGING_ROOT = _REPO_ROOT / "data" / "delegated-work"

_BOT_LOCK = threading.RLock()
_RUNNER_LOCK = threading.RLock()

# In-flight HELD-card reservations (guarded by _BOT_LOCK): counts cards whose
# send is between the cap-check and the persisted card_sent event (the
# code_queue._DM_RESERVE pattern -- concurrent confirms cannot breach the cap).
_CARD_RESERVE: dict[str, Any] = {"date": None, "n": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Rollout flag + env knobs
# ─────────────────────────────────────────────────────────────────────────────
def delegated_level() -> str:
    """CORA_DELEGATED_WORK: 'off' (default; intake refuses, runner claims
    nothing), 'log' (intake + ledger; runner claims + SIMULATED, no model calls,
    no delivery), or 'live'. Unrecognized -> 'off' (fail-closed whitelist).

    Read per-call from the PROCESS ENVIRONMENT. The always-on bot loads ``.env``
    ONCE at startup, so editing the ``.env`` FILE does NOT change a running
    bot's value -- confirmed live 2026-07-28 for the code-queue flag (scripts
    re-read it fresh; the bot did not). To flip the BOT: change the value AND
    restart. log->live is runner-visible with NO restart (the runner is a fresh
    process each fire); live->off requires ``.env off`` + a restart to also
    close bot-side intake (documented kill switch, design section 5)."""
    v = (os.environ.get("CORA_DELEGATED_WORK", "off") or "off").strip().lower()
    return v if v in ("off", "log", "live") else "off"


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def user_daily_quota() -> int:
    return _int_env("CORA_DELEGATED_USER_DAILY", 3)


def org_daily_quota() -> int:
    return _int_env("CORA_DELEGATED_ORG_DAILY", 10)


def job_usd_cap() -> float:
    return _float_env("CORA_DELEGATED_JOB_USD", 2.0)


def monthly_usd_cap() -> float:
    return _float_env("CORA_DELEGATED_MONTHLY_USD", 50.0)


# ─────────────────────────────────────────────────────────────────────────────
# Small utilities
# ─────────────────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _az_date(dt: datetime | None = None) -> str:
    return (dt or _now()).astimezone(_AZ_TZ).date().isoformat()


def _az_month(dt: datetime | None = None) -> str:
    return (dt or _now()).astimezone(_AZ_TZ).strftime("%Y-%m")


_MENTION_RE = re.compile(r"<[@#!][^>]*>")


def _normalize(text: str) -> str:
    t = _MENTION_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", t.strip().lower())


def brief_fingerprint(requester: str, brief: str) -> str:
    basis = f"{requester}:{_normalize(brief)}"
    return hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()  # noqa: S324 -- dedup, not security


def _derive_title(brief: str) -> str:
    first = (brief or "").strip().splitlines()[0] if (brief or "").strip() else ""
    first = re.sub(r"\s+", " ", first).strip()
    if len(first) > TITLE_MAX_CHARS:
        first = first[: TITLE_MAX_CHARS - 3].rstrip() + "..."
    return first or "(untitled)"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a jsonl ledger, skipping blank/torn/undecodable lines (a torn line
    from a crashed writer must never poison the fold -- design section 6)."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def append_bot_event(event: dict[str, Any]) -> None:
    """Append to the BOT ledger. Only the bot process may call this (single
    writer per file). Event type is validated against BOT_EVENTS."""
    et = str(event.get("event") or "")
    if et not in BOT_EVENTS:
        raise ValueError(f"not a bot-ledger event: {et!r}")
    with _BOT_LOCK:
        _BOT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with _BOT_LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def append_runner_event(event: dict[str, Any]) -> None:
    """Append to the RUNNER ledger. Only the runner process may call this."""
    et = str(event.get("event") or "")
    if et not in RUNNER_EVENTS:
        raise ValueError(f"not a runner-ledger event: {et!r}")
    with _RUNNER_LOCK:
        _RUNNER_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with _RUNNER_LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def staging_dir(job_id: str) -> Path:
    return _STAGING_ROOT / job_id


# ─────────────────────────────────────────────────────────────────────────────
# Fold (merge both ledgers by ts; terminal states never resurrect)
# ─────────────────────────────────────────────────────────────────────────────
_SPEC_FIELDS = (
    "job_id", "archetype", "title", "brief", "requester", "requester_name",
    "entity", "channel_id", "channel_name", "thread_ts", "deliverable",
    "fingerprint",
)


def _fold_jobs() -> dict[str, dict[str, Any]]:
    """Fold both ledgers into {job_id: record}. Events are merged by ``ts``
    (ISO strings sort chronologically; the sort is stable so same-ts events
    keep file order, bot file first). A state-changing event on a terminal
    record is IGNORED; ``artifact_homed`` is the one allowed post-terminal
    event and mutates artifact metadata only."""
    events = _read_jsonl(_BOT_LEDGER) + _read_jsonl(_RUNNER_LEDGER)
    events.sort(key=lambda e: str(e.get("ts") or ""))
    jobs: dict[str, dict[str, Any]] = {}
    for ev in events:
        et = str(ev.get("event") or "")
        jid = str(ev.get("job_id") or "")
        if not jid:
            continue
        if et == "requested":
            if jid in jobs:
                continue  # a duplicate requested for an existing id is ignored
            rec: dict[str, Any] = {k: ev.get(k, "") for k in _SPEC_FIELDS}
            rec["state"] = "REQUESTED"
            rec["requested_at"] = ev.get("ts", "")
            rec["cost"] = {}
            rec["artifact"] = {}
            rec["failure"] = {}
            rec["delivering"] = False
            jobs[jid] = rec
            continue
        rec = jobs.get(jid)
        if rec is None:
            continue
        terminal = rec.get("state") in TERMINAL_STATES
        if et == "artifact_homed":
            # Allowed even on DELIVERED -- artifact metadata only, never state.
            art = rec.setdefault("artifact", {})
            art["mis_homed"] = False
            if ev.get("target_path"):
                art["target_path"] = ev.get("target_path")
            rec["homed_at"] = ev.get("ts", "")
            continue
        if et == "card_sent":
            rec["card_channel_id"] = ev.get("card_channel_id", "")
            rec["card_message_ts"] = ev.get("card_message_ts", "")
            rec["card_sent_at"] = ev.get("ts", "")
            continue
        if et == "card_held":
            rec["card_held"] = True
            continue
        if et == "card_flushed":
            rec["card_held"] = False
            rec["card_flushed"] = True
            continue
        if terminal:
            continue  # terminal states never resurrect
        if et == "queued":
            rec["state"] = STATE_QUEUED
            rec["queued_at"] = ev.get("ts", "")
        elif et == "held":
            rec["state"] = STATE_HELD
            rec["held_at"] = ev.get("ts", "")
            rec["held_reason"] = ev.get("reason", "")
        elif et == "released":
            rec["state"] = STATE_QUEUED
            # The expiry clock runs from QUEUED entry -- a late release gets
            # its full 48h (design section 6).
            rec["queued_at"] = ev.get("ts", "")
            rec["released_at"] = ev.get("ts", "")
        elif et == "cancelled":
            rec["state"] = STATE_CANCELLED
            rec["cancelled_at"] = ev.get("ts", "")
            rec["cancelled_reason"] = ev.get("reason", "")
        elif et == "started":
            rec["state"] = STATE_RUNNING
            rec["started_at"] = ev.get("ts", "")
        elif et == "delivering":
            rec["delivering"] = True
            if isinstance(ev.get("artifact"), dict):
                rec.setdefault("artifact", {}).update(ev["artifact"])
            if isinstance(ev.get("cost"), dict):
                rec["cost"] = ev["cost"]
        elif et == "delivered":
            rec["state"] = STATE_DELIVERED
            rec["delivered_at"] = ev.get("ts", "")
            if isinstance(ev.get("cost"), dict):
                rec["cost"] = ev["cost"]
            if isinstance(ev.get("artifact"), dict):
                rec.setdefault("artifact", {}).update(ev["artifact"])
        elif et == "failed":
            rec["state"] = STATE_FAILED
            rec["failed_at"] = ev.get("ts", "")
            rec["failure"] = {
                "class": str(ev.get("failure_class") or "error"),
                "message": str(ev.get("message") or ""),
            }
            if isinstance(ev.get("cost"), dict):
                rec["cost"] = ev["cost"]
        elif et == "expired":
            rec["state"] = STATE_EXPIRED
            rec["expired_at"] = ev.get("ts", "")
        elif et == "simulated":
            rec["state"] = STATE_SIMULATED
            rec["simulated_at"] = ev.get("ts", "")
    return jobs


def load_jobs() -> list[dict[str, Any]]:
    """All jobs (folded), newest-requested first."""
    jobs = list(_fold_jobs().values())
    jobs.sort(key=lambda r: str(r.get("requested_at") or ""), reverse=True)
    return jobs


def get_job(job_id: str) -> dict[str, Any] | None:
    return _fold_jobs().get(job_id)


# ─────────────────────────────────────────────────────────────────────────────
# Quota + envelope (evaluated under _BOT_LOCK at confirm -- one critical section)
# ─────────────────────────────────────────────────────────────────────────────
def _requested_events() -> list[dict[str, Any]]:
    return [e for e in _read_jsonl(_BOT_LEDGER) if e.get("event") == "requested"]


def requested_today(user: str | None = None) -> int:
    """Count of ``requested`` events today (AZ), org-wide or per user. Quota
    basis is requested events REGARDLESS of later outcome -- a cancel does not
    refund the slot (design section 4)."""
    today = _az_date()
    n = 0
    for ev in _requested_events():
        ts = _parse_ts(ev.get("ts"))
        if ts is None or _az_date(ts) != today:
            continue
        if user is not None and str(ev.get("requester")) != user:
            continue
        n += 1
    return n


def quota_remaining(user: str) -> int:
    if user == HARRISON_ID:
        return user_daily_quota()  # founder exempt; display-only value
    return max(0, user_daily_quota() - requested_today(user))


def mtd_spend(jobs: dict[str, dict[str, Any]] | None = None) -> float:
    """Month-to-date estimated spend: sum of cost.est_usd across jobs whose
    ``requested`` timestamp falls in the current AZ month (design section 10)."""
    jobs = jobs if jobs is not None else _fold_jobs()
    month = _az_month()
    total = 0.0
    for rec in jobs.values():
        ts = _parse_ts(rec.get("requested_at"))
        if ts is None or _az_month(ts) != month:
            continue
        try:
            total += float((rec.get("cost") or {}).get("est_usd") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def open_job_count(jobs: dict[str, dict[str, Any]] | None = None) -> int:
    jobs = jobs if jobs is not None else _fold_jobs()
    return sum(1 for r in jobs.values() if r.get("state") in NON_TERMINAL_STATES)


def envelope_headroom(jobs: dict[str, dict[str, Any]] | None = None) -> float:
    """Monthly cap minus (MTD spend + committed reservation). The reservation is
    open-non-terminal-count x the per-job cap, so a burst of confirmed-but-unrun
    jobs cannot sail past the envelope (design review H-7)."""
    jobs = jobs if jobs is not None else _fold_jobs()
    reserved = open_job_count(jobs) * job_usd_cap()
    return monthly_usd_cap() - mtd_spend(jobs) - reserved


# ─────────────────────────────────────────────────────────────────────────────
# Intake screens (design section 3 order; ALL fail-closed)
# ─────────────────────────────────────────────────────────────────────────────
_LEX_REFUSAL = (
    "Delegated work isn't enabled for Lexington scope yet, so nothing was "
    "queued. What I *can* do right now: answer it directly here, or pull the "
    "policy/document from the knowledge base if it's already been dumped. "
    "Harrison can turn the Lexington lane on."
)

# LEX lane (2026-08-06 Harrison decision -- supersedes the D-102 v1
# "LEX excluded by construction" line). Only these two archetypes: they produce
# a document from policy + internal knowledge. spreadsheet_build and
# creator_shortlist have no LEX v1 case and both invite roster/client data into
# a structured artifact (a spreadsheet of "individuals" is the exact shape PHI
# takes at LEX), so they refuse with route-copy even when the lane is ON.
LEX_ALLOWED_ARCHETYPES = frozenset({"research_brief", "doc_draft"})

_LEX_ARCHETYPE_REFUSAL = (
    "For Lexington I can run a *research brief* or a *document draft* -- not "
    "{archetype}. Nothing was queued. If you need data assembled into a "
    "spreadsheet, ask me for the numbers here and I'll pull what I'm allowed "
    "to show; Harrison owns widening this."
)

_LEX_PHI_REFUSAL = (
    "That brief names a specific person in a care or billing context, so I "
    "can't run it as a background job. This isn't about YOUR access -- it's "
    "that the background worker retrieves as a non-custodian by design, so it "
    "could not read that person's records even if I queued the job. Nothing "
    "was queued. What works: ask the same question about the POLICY (\"what "
    "does DDD require for live-in caregiver respite?\") and I'll research that "
    "properly -- or ask me here in the channel, where your own access applies. "
    "Harrison owns any exception."
)


_LEX_CLINICAL_REFUSAL = (
    "That brief carries clinical or identifier detail (a diagnosis, medication, "
    "record identifier, or care documentation), so I can't run it as a "
    "background job -- the worker retrieves as a non-custodian by design and "
    "could not read those records anyway. Nothing was queued. What works: ask "
    "the POLICY version and I'll research that properly, or ask me here in the "
    "channel where your own access applies. Harrison owns any exception."
)


_LEX_BILLING_REFUSAL = (
    "That brief ties a named individual to their billing, authorization or "
    "eligibility status, which is protected information at Lexington even with "
    "no clinical detail in it. I can't run it as a background job -- the worker "
    "retrieves as a non-custodian by design. Nothing was queued. What works: "
    "ask about the RULE rather than the person (\"how are DDD respite units "
    "authorized?\"), or ask me here in the channel where your own access "
    "applies. Harrison owns any exception."
)

_GENERIC_PHI_REFUSAL = (
    "That brief looks like it contains protected client/health info, so I "
    "can't run it as a background job. Nothing was queued -- rephrase without "
    "client details if this was a false alarm."
)


def lex_delegated_enabled() -> bool:
    """CORA_DELEGATED_WORK_LEX: may LEX requesters/channels queue a job?

    Default OFF (unset/unrecognized -> off), independent of CORA_DELEGATED_WORK:
    the base flag must ALSO be on. MIXED activation surface -- intake
    (screen_request) runs in the always-on BOT, which snapshots ``.env`` at
    startup, so opening the lane needs the value change AND a restart; the
    RUNNER is a fresh process per fire and sees a change immediately. Flip both
    together and treat the bot as the slower half.
    """
    return (os.environ.get("CORA_DELEGATED_WORK_LEX", "") or "").strip().lower() in (
        "on", "1", "true", "yes",
    )


def _is_lex(code: str) -> bool:
    c = (code or "").strip().upper()
    return c == "LEX" or c.startswith("LEX-")


_staff_names_cache: set[str] | None = None


def _staff_names() -> set[str]:
    """Roster display names the LEX brief screen PRESERVES (colleagues, not
    clients). Same source + fail-toward-blocking posture as web_guard's."""
    global _staff_names_cache
    if _staff_names_cache is not None:
        return _staff_names_cache
    names: set[str] = set()
    try:
        import yaml
        path = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        names = {
            str(u.get("display_name", "")).strip()
            for u in (raw.get("users") or []) if u.get("display_name")
        }
    except Exception:  # noqa: BLE001 -- fail toward refusing, never crash intake
        log.warning("delegated_work: staff-name load failed -- LEX screen runs nameless",
                    exc_info=True)
    _staff_names_cache = names
    return _staff_names_cache


def screen_request(
    slack_user_id: str,
    entity: str,
    channel_name: str,
    archetype: str,
    brief: str,
    deliverable: str,
) -> str | None:
    """Run every deterministic intake screen in the design's exact order.
    Returns a user-facing refusal string, or None when the request may proceed
    to stash/preview. NOTHING here persists or stashes -- in particular the PHI
    screen runs before any caller stores the brief anywhere (design 3).

    Fail-closed throughout: an unresolvable requester, a guard exception, or a
    PHI-check error all refuse."""
    # 1. Feature flag.
    if delegated_level() == "off":
        return ("Delegated work is currently turned off -- nothing was queued. "
                "Harrison can enable it with CORA_DELEGATED_WORK.")

    # Shape validation (server-side; a model-invented archetype never executes).
    if archetype not in ARCHETYPES:
        return (f"Unknown job archetype {archetype!r}. Valid archetypes: "
                f"{', '.join(ARCHETYPES)}. Nothing was queued.")
    if deliverable not in DELIVERABLES:
        return (f"Unknown deliverable {deliverable!r}. Valid: md, xlsx. "
                "Nothing was queued.")
    brief = (brief or "").strip()
    if len(brief) < 12:
        return ("The brief is too short to run as a job -- give me a sentence or "
                "two describing what to research/build. Nothing was queued.")

    # 2. Requester eligibility (org-roles; fail-closed) + LEX exclusion on BOTH
    # the requester's primary entity AND the channel entity.
    try:
        from . import org_roles
        role = org_roles.get_role(slack_user_id)
    except Exception:  # noqa: BLE001 -- fail closed
        role = None
    if role is None:
        return ("I can't verify you in the org registry, so I can't take a "
                "delegated job from you. Nothing was queued.")
    if getattr(role, "external", False):
        return ("Delegated work is for internal teammates only -- nothing was "
                "queued.")
    # A job is LEX if EITHER side is LEX -- the requester's primary entity or
    # the channel it was asked in. Both legs kept (a LEX requester in a shared
    # channel, and a non-LEX requester in a LEX channel, are both LEX jobs).
    lex_job = _is_lex(getattr(role, "entity", "")) or _is_lex(entity)
    if lex_job:
        if not lex_delegated_enabled():
            return _LEX_REFUSAL
        if archetype not in LEX_ALLOWED_ARCHETYPES:
            return _LEX_ARCHETYPE_REFUSAL.format(
                archetype=f"a {archetype.replace('_', ' ')}")

    # 3. PHI -- BEFORE any stash/preview; fail-closed on error.
    #
    # A BRIEF is request-shaped text, so it takes the person-linked screen, not
    # the ingestion screen (cq-a24f9d2210fc, 2026-08-07): is_phi_risk carries
    # bare payer/programme names for filename/subject triage, and every real AZ
    # DDD policy brief says "AHCCCS". That single token refused three live
    # person-free briefs and blocked the lane's flagship use case. Clinical,
    # identifier, D-050 admin-PHI and the client-name detector all still apply;
    # only "names a programme" stopped counting as "names a person".
    #
    # The fired class is ROUTED INTO THE COPY. One template for every hit is how
    # a false claim shipped: all three refusals asserted "that brief names a
    # specific person" about briefs naming nobody. Never assert a detection that
    # did not fire (D-151/D-152).
    try:
        if lex_job and phi_guard.has_care_context_person_name(brief, _staff_names()):
            return _LEX_PHI_REFUSAL                       # a person WAS named
        # THREE branches, THREE messages. The first cut of this fix collapsed
        # them into one "clinical or identifier detail" template -- which is the
        # same defect it was written to close, just narrowed from one false
        # claim to two: an eligibility/authorization brief carries no diagnosis,
        # medication or record identifier, and was told it did (D-051 MED-4).
        if phi_guard.is_lex_billing_status_phi(brief):
            return _LEX_BILLING_REFUSAL if lex_job else _GENERIC_PHI_REFUSAL
        if (phi_guard.is_phi_risk_person_linked(brief)
                or phi_guard.is_clinical_phi(brief)):
            return _LEX_CLINICAL_REFUSAL if lex_job else (
                    "That brief looks like it contains protected client/health "
                    "info, so I can't run it as a background job. Nothing was "
                    "queued -- rephrase without client details if this was a "
                    "false alarm.")
    except Exception:  # noqa: BLE001 -- fail closed
        return ("I couldn't screen that brief for protected info (fail-closed), "
                "so nothing was queued. Try again in a moment.")

    # 4. Pre-LLM guard parity (design review H-2): user_access + sibling +
    # cross-entity run against the BRIEF exactly as app.py runs them for an
    # interactive mention. These are app.py chokepoints, NOT inside dispatch(),
    # so intake must run them explicitly or the core invariant is false.
    # R2 (Harrison ruling 2026-08-07): this screen asks "is this REQUESTER
    # authorized for this TOPIC" -- a question about the human. It was passing
    # phi_custodian=False, the WORKER's retrieval pin, which conflated two
    # different things and made the lane refuse its own use case: every LEX
    # requester carries `phi` in sensitive_topics_blocked, so "research the DDD
    # provider revalidation requirements" drew "Client-specific health info
    # stays in the EHR." Authorization at intake now reflects the requester's
    # REAL custodian status; the pin stays exactly where it belongs -- on WORKER
    # retrieval (delegated_worker.make_kb_search, design 8.3), which is what
    # actually bounds privilege. Content containment is unchanged and lives at
    # screen #3 above: a client-identifying brief still refuses fail-closed for
    # all five custodians.
    try:
        from . import (channel_classifier, cross_entity_guard, lex_phi_access,
                       sibling_guard, user_access)
        tier = channel_classifier.tier_label(
            entity, channel_classifier.classify_function(channel_name or ""))
        # Fail-closed: an unresolvable identity reads as non-custodian.
        try:
            requester_custodian = bool(slack_user_id) and lex_phi_access.phi_allowed(
                slack_user_id, entity, is_dm=False)
        except Exception:  # noqa: BLE001
            requester_custodian = False
        access_block = user_access.check_access(
            slack_user_id, entity, brief, phi_custodian=requester_custodian, tier=tier)
        if access_block:
            return access_block
        sibling_redirect = sibling_guard.check_redirect(entity, brief)
        if sibling_redirect:
            return sibling_redirect
        cross_redirect = cross_entity_guard.check_cross_entity(brief, entity)
        if cross_redirect:
            return cross_redirect
    except Exception:  # noqa: BLE001 -- fail closed
        log.exception("delegated_work: guard-parity screen errored (fail-closed)")
        return ("I couldn't run the access screens on that brief (fail-closed), "
                "so nothing was queued. Try again in a moment.")

    # 5. Dedup: an identical (normalized brief, same requester) job that is
    # NON-TERMINAL refuses, regardless of age; terminal re-asks are allowed.
    fp = brief_fingerprint(slack_user_id, brief)
    for rec in _fold_jobs().values():
        if (rec.get("fingerprint") == fp
                and rec.get("state") in NON_TERMINAL_STATES):
            return (f"You already have this exact job open ({rec.get('job_id')}, "
                    f"{rec.get('state')}). I'll deliver that one -- nothing new "
                    "was queued. Cancel it first if you want to re-run it.")

    # 6. Quota + envelope are evaluated at CONFIRM inside the ledger lock
    # (submit_job) -- one atomic check-and-append critical section.
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Submit (the confirm executor -- check-and-append under ONE lock)
# ─────────────────────────────────────────────────────────────────────────────
def submit_job(
    slack_user_id: str,
    entity: str,
    channel_id: str,
    channel_name: str,
    thread_ts: str | None,
    archetype: str,
    brief: str,
    deliverable: str,
    *,
    client_factory: Callable | None = None,
) -> tuple[dict[str, Any] | None, str, str]:
    """Persist a confirmed job. Returns (job_record, outcome, message) where
    outcome is 'queued' | 'held' | 'refused'.

    Re-runs the full deterministic screen chain (cheap, no network) so a stale
    stash that has become invalid since preview never executes, then evaluates
    quota + envelope and appends inside ONE lock (the code-queue v1.1 TOCTOU
    lesson: parallel previews in N channels cannot collectively breach caps)."""
    brief = (brief or "").strip()[:BRIEF_MAX_CHARS]
    refusal = screen_request(slack_user_id, entity, channel_name, archetype,
                             brief, deliverable)
    if refusal:
        return None, "refused", refusal

    requester_name = ""
    try:
        from . import org_roles
        role = org_roles.get_role(slack_user_id)
        requester_name = getattr(role, "name", "") or ""
    except Exception:  # noqa: BLE001
        pass

    job_id = "dw-" + uuid.uuid4().hex[:12]
    spec = {
        "job_id": job_id,
        "archetype": archetype,
        "title": _derive_title(brief),
        "brief": brief,
        "requester": slack_user_id,
        "requester_name": requester_name,
        "entity": (entity or "").strip().upper(),
        "channel_id": channel_id or "",
        "channel_name": channel_name or "",
        "thread_ts": thread_ts or "",
        "deliverable": deliverable,
        "fingerprint": brief_fingerprint(slack_user_id, brief),
    }

    with _BOT_LOCK:
        # Re-check dedup inside the lock (two parallel confirms of the same
        # brief: the second must see the first's requested/queued rows).
        jobs = _fold_jobs()
        for rec in jobs.values():
            if (rec.get("fingerprint") == spec["fingerprint"]
                    and rec.get("state") in NON_TERMINAL_STATES):
                return None, "refused", (
                    f"You already have this exact job open ({rec.get('job_id')}, "
                    f"{rec.get('state')}) -- nothing new was queued.")

        held_reason = ""
        if slack_user_id != HARRISON_ID:
            if requested_today(slack_user_id) >= user_daily_quota():
                held_reason = "user_quota"
            elif requested_today() >= org_daily_quota():
                held_reason = "org_quota"
        if not held_reason and envelope_headroom(jobs) < job_usd_cap():
            held_reason = "envelope"

        ts = _now_iso()
        append_bot_event({"event": "requested", "ts": ts, **spec})
        if held_reason:
            append_bot_event({"event": "held", "ts": _now_iso(),
                              "job_id": job_id, "reason": held_reason})
        else:
            append_bot_event({"event": "queued", "ts": _now_iso(),
                              "job_id": job_id})

    job = get_job(job_id) or {**spec, "state": STATE_HELD if held_reason else STATE_QUEUED}
    if held_reason:
        # Card send is network -- outside the lock (reservation-guarded cap).
        _send_held_card(job, held_reason, client_factory)
        reason_text = {
            "user_quota": f"you've hit today's per-person limit ({user_daily_quota()}/day)",
            "org_quota": f"the org has hit today's limit ({org_daily_quota()}/day)",
            "envelope": "this month's delegated-work budget is fully committed",
        }.get(held_reason, held_reason)
        return job, "held", (
            f"Held for Harrison's release ({job_id}): {reason_text}. It is NOT "
            "lost -- Harrison got a release/dismiss card and a released job runs "
            "with its full 48h window.")
    return job, "queued", (
        f"Queued ({job_id}). The runner picks jobs up about every 15 minutes; "
        "I'll deliver the result back to this thread.")


# ─────────────────────────────────────────────────────────────────────────────
# HELD lane -- Harrison release/dismiss card (5/day cap + overflow digest)
# ─────────────────────────────────────────────────────────────────────────────
def _default_client_factory() -> Any:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return None
    from slack_sdk import WebClient
    return WebClient(token=token)


def _cards_sent_today() -> int:
    today = _az_date()
    n = 0
    for ev in _read_jsonl(_BOT_LEDGER):
        if ev.get("event") != "card_sent":
            continue
        ts = _parse_ts(ev.get("ts"))
        if ts and _az_date(ts) == today:
            n += 1
    return n


def _reserve_card_slot() -> bool:
    with _BOT_LOCK:
        today = _az_date()
        if _CARD_RESERVE["date"] != today:
            _CARD_RESERVE["date"] = today
            _CARD_RESERVE["n"] = 0
        if _cards_sent_today() + _CARD_RESERVE["n"] >= MAX_HELD_CARDS_PER_DAY:
            return False
        _CARD_RESERVE["n"] += 1
        return True


def _release_card_slot() -> None:
    with _BOT_LOCK:
        _CARD_RESERVE["n"] = max(0, _CARD_RESERVE["n"] - 1)


def build_held_card(job: dict[str, Any], reason: str) -> tuple[str, list[dict[str, Any]]]:
    """(fallback_text, blocks) for one HELD-job release/dismiss card. The text
    (title = the brief's head) is sanitize_text-wrapped -- Block Kit bodies
    bypass the class-level WebClient egress patch, which only covers `text=`."""
    jid = str(job.get("job_id", ""))
    text = (
        f"*Delegated work -- HELD* ({reason})\n"
        f"`{jid}` {job.get('archetype', '?')} [{job.get('entity', '?')}] from "
        f"{job.get('requester_name') or job.get('requester', '?')} in "
        f"#{job.get('channel_name', '?')}\n"
        f"*{job.get('title', '(untitled)')}*"
    )
    try:
        from .slack_egress import sanitize_text
        text = sanitize_text(text)
    except Exception:  # noqa: BLE001 -- sanitizer is a belt, never a blocker
        pass
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
        {"type": "actions", "block_id": f"dw_actions_{jid}"[:255], "elements": [
            {"type": "button", "action_id": ACTION_RELEASE, "style": "primary",
             "text": {"type": "plain_text", "text": "▶️ Release"}, "value": jid},
            {"type": "button", "action_id": ACTION_DISMISS,
             "text": {"type": "plain_text", "text": "🗑️ Dismiss"}, "value": jid},
        ]},
    ]
    return text, blocks


def _append_card_held(jid: str) -> None:
    try:
        append_bot_event({"event": "card_held", "ts": _now_iso(), "job_id": jid})
    except Exception:  # noqa: BLE001
        log.warning("delegated_work: card_held append failed", exc_info=True)


def _send_held_card(job: dict[str, Any], reason: str,
                    client_factory: Callable | None) -> None:
    """DM Harrison one HELD card, respecting the daily cap. EVERY non-sent arm
    (over cap, no client, send failure) leaves a durable card_held marker so
    the runner's overflow digest surfaces the job later -- a HELD job with
    neither card_sent nor card_held would be invisible forever (HELD never
    expires; D-051, found by three lenses independently). Best-effort: a send
    failure never raises into the confirm ack."""
    jid = str(job.get("job_id", ""))
    if not _reserve_card_slot():
        _append_card_held(jid)
        log.info("delegated_work: HELD-card cap hit -- %s rides the overflow digest", jid)
        return
    try:
        client = (client_factory or _default_client_factory)()
        if client is None:
            _append_card_held(jid)
            return
        open_resp = client.conversations_open(users=[HARRISON_ID])
        dm_channel = open_resp["channel"]["id"]
        text, blocks = build_held_card(job, reason)
        resp = client.chat_postMessage(
            channel=dm_channel, text=text, blocks=blocks,
            unfurl_links=False, unfurl_media=False,
        )
        append_bot_event({
            "event": "card_sent", "ts": _now_iso(), "job_id": jid,
            "card_channel_id": dm_channel, "card_message_ts": resp.get("ts", ""),
        })
    except Exception:  # noqa: BLE001 -- best-effort; the durable marker keeps it visible
        log.warning("delegated_work: HELD card send failed (non-fatal)", exc_info=True)
        _append_card_held(jid)
    finally:
        _release_card_slot()


def held_jobs_awaiting_card() -> list[dict[str, Any]]:
    """HELD jobs whose card was held over-cap and not yet flushed (for the
    runner's overflow digest)."""
    return [r for r in load_jobs()
            if r.get("state") == STATE_HELD and r.get("card_held")
            and not r.get("card_flushed")]


# ─────────────────────────────────────────────────────────────────────────────
# Harrison card actions + requester cancel
# ─────────────────────────────────────────────────────────────────────────────
def process_job_action(action_id: str, dw_id: str, actor_id: str) -> tuple[str, str]:
    """Apply a HELD-card button action. Harrison-only; idempotent; all
    correctness here (the app.py wrapper is Slack I/O only) -- the
    process_queue_action shape."""
    if actor_id != HARRISON_ID:
        return "not_authorized", "Only Harrison can action delegated-work holds."
    rec = get_job(dw_id)
    if not rec:
        return "error", "That job no longer exists."
    state = str(rec.get("state") or "")

    if action_id == ACTION_RELEASE:
        if state == STATE_QUEUED:
            return "noop", "Already released -- it's queued for the next runner pass."
        if state in TERMINAL_STATES or state == STATE_RUNNING:
            return "noop", f"Job is {state} -- nothing to release."
        append_bot_event({"event": "released", "ts": _now_iso(), "job_id": dw_id})
        return "released", ("▶️ Released -- queued for the next runner pass "
                            "(full 48h window from now).")

    if action_id == ACTION_DISMISS:
        if state == STATE_CANCELLED:
            return "noop", "Already dismissed."
        if state in TERMINAL_STATES:
            return "noop", f"Job is {state} -- nothing to dismiss."
        if state == STATE_RUNNING:
            return "noop", "Job is already running -- v1 has no mid-run cancel."
        append_bot_event({"event": "cancelled", "ts": _now_iso(), "job_id": dw_id,
                          "reason": "harrison_dismiss"})
        return "dismissed", "🗑️ Dismissed -- the requester can re-ask later."

    return "error", f"Unknown action: {action_id}"


def cancel_job(dw_id: str, actor_id: str) -> tuple[str, str]:
    """Requester cancels their own QUEUED job; Harrison may cancel any QUEUED or
    HELD job. RUNNING cancellation is out of v1. Cancel does NOT refund the
    day's quota slot (quota basis = requested events)."""
    rec = get_job(dw_id)
    if not rec:
        return "error", f"No job {dw_id} found."
    state = str(rec.get("state") or "")
    if state in TERMINAL_STATES:
        return "noop", f"{dw_id} is already {state} -- nothing to cancel."
    if state == STATE_RUNNING:
        return "refused", (f"{dw_id} is already running -- v1 can't cancel a "
                           "running job. It finishes or fails on its own.")
    is_founder = actor_id == HARRISON_ID
    if not is_founder:
        if str(rec.get("requester")) != actor_id:
            # No existence/ownership leak beyond what `list` already shows.
            return "refused", "You can only cancel your own queued jobs."
        if state != STATE_QUEUED:
            return "refused", f"{dw_id} is {state} -- you can only cancel a QUEUED job."
    append_bot_event({"event": "cancelled", "ts": _now_iso(), "job_id": dw_id,
                      "reason": "harrison_cancel" if is_founder else "requester_cancel"})
    return "cancelled", (f"Cancelled {dw_id}. (Today's quota slot is not "
                         "refunded.)")


# ─────────────────────────────────────────────────────────────────────────────
# List view (cross-channel title suppression) + observability summary
# ─────────────────────────────────────────────────────────────────────────────
def render_job_list(user: str, channel_id: str) -> str:
    """The asker's own jobs, newest first (cap 10). Full titles render ONLY for
    jobs requested in the CURRENT channel -- a title authored in a higher-tier
    channel must not render in a lower-tier one (design section 3). An empty
    current-channel id suppresses every title (fail-closed)."""
    mine = [r for r in load_jobs() if str(r.get("requester")) == user]
    if not mine:
        return "You have no delegated jobs on record."
    lines = ["Your delegated jobs (newest first):"]
    for r in mine[:10]:
        jid = r.get("job_id", "?")
        base = f"- `{jid}` {r.get('archetype', '?')} [{r.get('entity', '?')}] -- {r.get('state', '?')}"
        if channel_id and str(r.get("channel_id") or "") == channel_id:
            base += f": {r.get('title', '')}"
        cost = (r.get("cost") or {}).get("est_usd")
        if cost:
            base += f" (~${float(cost):.2f})"
        lines.append(base)
    if len(mine) > 10:
        lines.append(f"(+{len(mine) - 10} older)")
    return "\n".join(lines)


def jobs_summary(limit: int = 15) -> dict[str, Any]:
    """Observability view for the MCP tool + session snapshot. Renders
    job_id/archetype/entity/state/cost + MTD spend ONLY -- never titles or
    briefs (briefs typed in private channels must not surface on org-readable
    mirrors; design section 9)."""
    jobs = load_jobs()
    by_state: dict[str, int] = {}
    for r in jobs:
        s = str(r.get("state") or "?")
        by_state[s] = by_state.get(s, 0) + 1
    recent = []
    for r in jobs[:limit]:
        cost = r.get("cost") or {}
        recent.append({
            "job_id": r.get("job_id", ""),
            "archetype": r.get("archetype", ""),
            "entity": r.get("entity", ""),
            "state": r.get("state", ""),
            "requested_at": r.get("requested_at", ""),
            "est_usd": round(float(cost.get("est_usd") or 0.0), 4),
        })
    return {
        "level": delegated_level(),
        "counts_by_state": by_state,
        "open_jobs": open_job_count(),
        "mtd_est_usd": round(mtd_spend(), 4),
        "monthly_cap_usd": monthly_usd_cap(),
        "recent": recent,
    }
