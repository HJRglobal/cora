"""Cora code-session queue -- real-time capture of build-worthy signals.

Design of record: ``_shared/projects/cora/2026-07-27_fndr_cora-code-session-queueing-design.md``.
Build handoff: ``_notes/2026-07-27_fndr_cora-code-prompt-code-session-queue.md``.

The problem this closes: build-worthy signals (a tool crash blocking a teammate,
a "can Cora ...?" capability ask, a repeated thumbs-down, an explicit "@Cora queue
a code session ...") die in Slack unless Harrison personally notices them. Nothing
durable is emitted at the moment of the signal. This module emits it.

What it is / is NOT:
  * NOT canon (D-011): the queue never writes to Asana / HubSpot / decisions.md /
    known-answers, and nothing in it executes code. Approval only STAGES files.
  * Fail-soft on the hot path (guardrail #2): every capture entry point is wrapped
    so a capture failure can never raise into, delay, or alter a user-facing reply.
    Slow work (Slack DM, Haiku/Sonnet calls) runs off the calling thread.
  * PHI-safe sink (D-082 extension): a LEX-sourced item persists evidence as
    channel/ts POINTERS only (never message text); every candidate summary passes
    ``phi_guard.is_phi_risk`` FAIL-CLOSED (flagged -> the item is dropped).

Rollout: ``CORA_CODE_QUEUE = off | log | live`` (default ``off``, read per-call so a
flip needs no restart). ``off`` = fully inert; ``log`` = capture + ledger + backlog,
no DMs; ``live`` = + immediate DM cards to Harrison.

State model (append-only event ledger, state derived by fold):
  ``PROPOSED -> APPROVED -> STAGED -> SHIPPED`` with ``DISMISSED`` (fingerprint never
  re-proposes), ``SNOOZED`` (auto-resurface 14d), ``SUPERSEDED``, ``BLOCKED``.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from . import drive_io, phi_guard
from .model_router import MODEL_SONNET

log = logging.getLogger("cora.code_queue")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
HARRISON_ID = os.environ.get("HARRISON_SLACK_USER_ID", "U0B2RM2JYJ1")

_HAIKU_MODEL = "claude-haiku-4-5"
_SONNET_MODEL = MODEL_SONNET  # single source: model_router (CORA_SONNET_MODEL-overridable)

FUZZY_DEDUP_RATIO = 0.85          # same-signal paraphrase-dedup threshold (friction pattern)
DEDUP_EMBED_SIM = 0.82            # cosine sim for SEMANTIC (paraphrase) dedup (friction CLUSTER_SIM)
DEDUP_EMBED_WINDOW_DAYS = 14      # embedding dedup only considers OPEN items this recent
MAX_DM_PER_DAY = 5                # storm cap: new-item DM cards per day
SILENT_TIMEOUT_THRESHOLD = 3      # UC6: >= this many same-tool timeouts in the window -> candidate
SILENT_TIMEOUT_WINDOW_DAYS = 7
THUMBSDOWN_THRESHOLD = 2          # UC7: >= this many similar thumbs-downs in the window -> candidate
THUMBSDOWN_WINDOW_DAYS = 14
SNOOZE_DAYS = 14                  # "Later" suppresses this long
STALE_STAGED_DAYS = 14           # Monday menu resurfaces STAGED items older than this
EXPLICIT_THROTTLE_PER_DAY = 3    # per-user cap on the explicit tool

VALID_KINDS = ("bug", "feature", "config")
VALID_SEVERITIES = ("P0", "P1", "P2", "P3")
# The queue carries TWO severity vocabularies in live data (Slice 4 verify-first,
# 2026-08-05): the Haiku classifier emits the P0-P3 ladder above, while every
# `seed_item` caller (bug-hunt bundles, flywheel audits, the MCP seed tool) has
# been passing HIGH/MEDIUM/LOW -- and seed_item never validated against
# VALID_SEVERITIES, so both persisted. Rather than rewrite live rows, the two
# ladders are mapped onto one PRIORITY notion here; `is_priority_severity` is the
# single source of truth for "this item is P0/P1-class" (see its docstring for why
# a silent vocabulary split made the P1-at-approval rule unenforceable).
SEVERITY_ALIASES = {
    "CRITICAL": "P0", "URGENT": "P0",
    "HIGH": "P1",
    "MED": "P2", "MEDIUM": "P2", "NORMAL": "P2",
    "LOW": "P3", "MINOR": "P3",
}
# Everything an explicit re-rate (set_severity) will accept.
VALID_SEVERITY_INPUTS = tuple(VALID_SEVERITIES) + tuple(sorted(SEVERITY_ALIASES))
_PRIORITY_LADDER = frozenset({"P0", "P1"})
# Mirrors _scrub_evidence's own note[:200] cut and the fold's 10-entry cap, so
# append_evidence can REPORT a truncation/refusal instead of falsely succeeding.
_EVIDENCE_NOTE_MAX_CHARS = 200
_EVIDENCE_MAX_ENTRIES = 10


def canonical_severity(severity: str | None) -> str:
    """Map either live vocabulary onto the canonical P0-P3 ladder. An unknown or
    blank value returns "" (never a silent P3 -- callers decide)."""
    s = str(severity or "").strip().upper()
    if s in VALID_SEVERITIES:
        return s
    return SEVERITY_ALIASES.get(s, "")


def is_priority_severity(severity: str | None) -> bool:
    """True when this severity is P0/P1-class in EITHER vocabulary (so "HIGH"
    counts). The P1-at-approval rule and the nightly aging monitor both read this
    one predicate: keying either of them on a bare `in ("P0","P1")` string test is
    what let HIGH-severity items sit APPROVED-and-unstaged invisibly."""
    return canonical_severity(severity) in _PRIORITY_LADDER

# Statuses at which an item is still "live" -- a new similar signal should dedup INTO
# it (used by the embedding paraphrase layer). Terminal statuses are excluded so a
# dismissed/shipped item never absorbs a genuinely fresh ask.
_OPEN_STATUSES = frozenset({"PROPOSED", "APPROVED", "STAGED", "SNOOZED", "BLOCKED", "PARKED"})
# A PARKED row is open but INVISIBLE -- it leaves every menu section until its
# trigger -- so a deliberate re-ask must never dedup into it silently (D-051 lens B
# HIGH #2: one park tap could have silenced a recurring P0 ask until 2099). It is
# not a merge target: an explicit re-ask mints a fresh row, and a passive
# recurrence on a parked row is COUNTED as its trigger (see _parked_rows).
_MERGE_TARGET_STATUSES = _OPEN_STATUSES - {"PARKED"}
PARK_MAX_DAYS = 90            # every park has a review horizon; no 2099 parks (lens B)

# Block Kit action ids (own namespace; handled by app.py wrappers)
ACTION_APPROVE = "code_queue_approve"
ACTION_EDIT = "code_queue_edit"
ACTION_DISMISS = "code_queue_dismiss"
ACTION_LATER = "code_queue_later"
ACTION_STAGE = "code_queue_stage"           # stage a prompt (single item or bundle)
ACTION_MARK_SHIPPED = "code_queue_shipped"
ACTION_KEEP = "code_queue_keep"
VIEW_EDIT_SUBMIT = "code_queue_edit_submit"
# C3 (Code #12): park-with-trigger + dismiss-with-evidence (both open a modal).
ACTION_PARK = "code_queue_park"
VIEW_PARK_SUBMIT = "code_queue_park_submit"
ACTION_DISMISS_NOTE = "code_queue_dismiss_note"
VIEW_DISMISS_SUBMIT = "code_queue_dismiss_submit"
# A Keep tap buys STALE_STAGED_DAYS of silence; after this many the row must be
# parked with a trigger, shipped or dismissed (lock packet: "Keep-count cap").
KEEP_CAP = 2

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATE_DIR = _REPO_ROOT / "data" / "state"
_EVENT_LEDGER = _STATE_DIR / "code-session-queue.jsonl"
_FINGERPRINT_LEDGER = _STATE_DIR / "code-queue-fingerprints.jsonl"
_SIGNALS_LEDGER = _STATE_DIR / "code-queue-signals.jsonl"
_NOTES_DIR = _REPO_ROOT / "_notes"

# The REAL, canonical event-ledger path -- a separate constant from _EVENT_LEDGER
# (never monkeypatched) so the leak guard below can detect when _EVENT_LEDGER has
# been redirected (a test/sandbox isolating it to a tmp dir), independent of
# whatever _EVENT_LEDGER currently points at.
_DEFAULT_EVENT_LEDGER = _STATE_DIR / "code-session-queue.jsonl"

_LEDGER_LOCK = threading.RLock()

# In-flight DM-card reservations (guarded by _LEDGER_LOCK): counts cards whose send
# is between the cap-check and the persisted dm_sent event, so concurrent capture
# threads cannot each pass the 5/day check and collectively breach the cap (D-051).
_DM_RESERVE: dict[str, Any] = {"date": None, "n": 0}

# In-flight staging reservations (guarded by _LEDGER_LOCK): the id(s) whose kickoff
# prompt is being generated RIGHT NOW. The persisted `staged` event is only appended
# AFTER the (multi-second) Sonnet generate returns, so a second Stage tap in that
# window would otherwise pass the "already STAGED?" check and generate a SECOND prompt
# (day-one defect #3 -- a TOCTOU on the stage path). A concurrent tap that sees an id
# reserved here backs off with an "already staging" ack -- no second generate.
_STAGING_INFLIGHT: set[str] = set()


def _begin_staging(keys: list[str]) -> bool:
    """Atomically reserve staging for ``keys`` (item ids). Returns True if reserved
    (caller MUST later _end_staging), False if any key is already being staged."""
    with _LEDGER_LOCK:
        if any(k in _STAGING_INFLIGHT for k in keys):
            return False
        _STAGING_INFLIGHT.update(keys)
        return True


def _end_staging(keys: list[str]) -> None:
    with _LEDGER_LOCK:
        for k in keys:
            _STAGING_INFLIGHT.discard(k)

# Test hook: when True, _submit runs the worker INLINE (synchronous) so tests are
# deterministic. Production leaves it False -> capture work runs on a daemon thread.
_SYNC = False


def _founder_os_root() -> Path:
    """Founder-OS Drive root (mirrors channel_synthesis._founder_os_root)."""
    env = os.environ.get("FOUNDER_OS_ROOT", "").strip()
    return Path(env) if env else Path(r"G:\My Drive\HJR-Founder-OS")


def backlog_path() -> Path:
    """Generated backlog view -- KB-ingested (see kb_exclusions allowlist)."""
    return _founder_os_root() / "_shared" / "projects" / "cora" / "code-session-backlog.md"


def _real_backlog_path() -> Path:
    """The REAL, unredirected default backlog path -- ignores any FOUNDER_OS_ROOT
    override. Used only by the leak guard below to detect "the write target is
    STILL the live Founder-OS default" even in a process that happens to leave
    FOUNDER_OS_ROOT unset."""
    return Path(r"G:\My Drive\HJR-Founder-OS") / "_shared" / "projects" / "cora" / "code-session-backlog.md"


def _backlog_write_would_leak() -> bool:
    """True when _EVENT_LEDGER has been redirected away from its real, canonical
    path (signalling a test/sandbox context reading an ISOLATED ledger) WHILE the
    backlog write target is STILL the real, unredirected Founder-OS default --
    the exact mismatch that let a 1-item isolated test ledger render into the
    live G:\\...\\code-session-backlog.md on 2026-07-30 (item cq-96fdd1850605, a
    LEX-redacted TEST title that was never a real queue item). A properly
    isolated caller that also redirects FOUNDER_OS_ROOT (or mocks
    drive_io.write_text_atomic) is unaffected -- this only trips on the
    inconsistent HALF-isolated state, never on legitimate isolation.

    Fail-CLOSED on any path-resolution error (an unresolvable path counts as
    both "redirected" and "still the real default" -- i.e. refuse the write).
    """
    try:
        ledger_redirected = Path(_EVENT_LEDGER).resolve() != _DEFAULT_EVENT_LEDGER.resolve()
    except OSError:
        ledger_redirected = True
    if not ledger_redirected:
        return False
    try:
        target_is_real_default = backlog_path().resolve() == _real_backlog_path().resolve()
    except OSError:
        target_is_real_default = True
    return target_is_real_default


def founder_os_notes_dir() -> Path:
    """Canonical home for generated kickoff prompts: the Founder-OS ``_notes`` folder
    (NOT the repo ``_notes``). KB-excluded by filename (``cora-code-prompt`` -> the
    kb_exclusions rule), so living under the swept Drive tree leaks nothing."""
    return _founder_os_root() / "_shared" / "projects" / "cora" / "_notes"


# ─────────────────────────────────────────────────────────────────────────────
# Rollout flag
# ─────────────────────────────────────────────────────────────────────────────
def code_queue_level() -> str:
    """CORA_CODE_QUEUE: 'off' (default, fully inert), 'log' (capture + ledger +
    backlog, NO DMs), or 'live' (+ immediate DM cards). Unrecognized -> 'off'.

    Read per-call from the PROCESS ENVIRONMENT. NOTE (day-one defect: the docstring
    used to claim "a flip needs no restart" -- that is only true for freshly-spawned
    SCRIPTS, which re-run ``load_dotenv`` at import). The always-on bot loads ``.env``
    ONCE at startup, so editing the ``.env`` FILE does NOT change a running bot's value
    -- confirmed live 2026-07-28 (the 7am menu script read the flip fresh; the bot did
    not). To flip the BOT: change the value AND restart (or set it in the service
    environment). Runbook: CLAUDE.md 'RESTART' step. A per-call ``.env`` re-read was
    considered and rejected -- it would let a stale/edited ``.env`` silently override an
    operator's real-environment value and break the test contract (tests set os.environ
    directly); a restart is already required for the bot-loaded hooks anyway."""
    v = (os.environ.get("CORA_CODE_QUEUE", "off") or "off").strip().lower()
    return v if v in ("off", "log", "live") else "off"


# ─────────────────────────────────────────────────────────────────────────────
# Small utilities
# ─────────────────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# Slack/Cowork connector noise that must NOT defeat dedup. The Cowork connector
# appends a trailing "*Sent using* <@U...>" footer, and asks routinely carry channel
# / user mention tokens; two otherwise-identical asks differing only by these read as
# distinct under a naive normalize (day-one defect #1 -- the RepRally double-file).
_MENTION_RE = re.compile(r"<[@#!][^>]*>")
# Anchored to the ACTUAL Cowork footer shape: optional "*Sent using*" mrkdwn wrapping a
# trailing <@user> mention at END of message. NOT DOTALL and NOT a bare "sent using .*$"
# -- an unanchored greedy strip would eat legitimate mid-sentence content (e.g. "flag
# invoices sent using the old template") and collide distinct asks (D-051 defect B).
_SENT_USING_RE = re.compile(r"\n?\s*\*?\s*sent using\s*\*?\s*<@[^>]+>\s*$", re.IGNORECASE)


def _normalize(text: str) -> str:
    """Lowercase + whitespace-collapse AFTER stripping the Cowork "Sent using" footer
    and any Slack mention tokens, so connector noise can't split a dedup group."""
    t = _SENT_USING_RE.sub("", text or "")
    t = _MENTION_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t.strip().lower())


def _fingerprint(signal: str, representative: str) -> str:
    basis = f"{signal}:{_normalize(representative)}"
    return hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()  # noqa: S324 -- dedup, not security


def _class_key(title: str, subsystem: str) -> str | None:
    """Second dedup key over the CLASSIFIER OUTPUT (title + subsystem), so two
    paraphrased asks that the classifier collapses to the same (title, subsystem)
    dedup even across DIFFERENT signals. A hash -> PHI-safe (stores nothing raw).
    None when either field is empty (too coarse to key on)."""
    t = _normalize(title)
    s = _normalize(subsystem)
    if not t or not s:
        return None
    return "c:" + hashlib.sha1(f"{t}|{s}".encode("utf-8", "replace")).hexdigest()  # noqa: S324 -- dedup, not security


def _default_embed(texts: list[str]) -> list[list[float]]:
    """Lazy import so tests / offline runs don't require the openai dependency
    (mirrors friction_mining._default_embed)."""
    from cora.knowledge_base.embeddings import embed_texts
    return embed_texts(texts)


def _embedding_dup_id(signal: str, representative: str, entity: str,
                      *, embed_fn: Callable | None = None) -> str | None:
    """Semantic (paraphrase) dedup: cosine >= DEDUP_EMBED_SIM against OPEN items
    captured within DEDUP_EMBED_WINDOW_DAYS. Cross-signal. Returns the matched cq-id
    or None. FAIL-SOFT (any embedding error -> None). NEVER embeds LEX/PHI text (egress
    guard); LEX candidates carry no stored representative, so they are excluded on the
    candidate side too. Runs OUTSIDE the ledger lock (it makes a network call)."""
    if _is_phi_or_lex(representative, entity):
        return None
    rep = (representative or "").strip()
    if len(rep) < 8:
        return None
    cutoff = _now() - timedelta(days=DEDUP_EMBED_WINDOW_DAYS)
    cands: list[tuple[str, str]] = []
    for it in load_items():
        if it.get("status") not in _OPEN_STATUSES:
            continue
        ts = _parse_ts(it.get("ts"))
        if ts is None or ts < cutoff:
            continue
        cr = str(it.get("representative") or "").strip()
        if len(cr) < 8:
            continue  # LEX/PHI-redacted ("") or too-short candidate -- skip
        # Defense-in-depth (D-051 defect A): never embed a candidate whose stored rep is
        # LEX-sourced or PHI-tripping, even if a raw one slipped past the write-time
        # redaction (e.g. a legacy seed_item row). The write path (seed_item + _capture)
        # now redacts, but the read side must not TRUST that invariant for egress.
        if _is_phi_or_lex(cr, str(it.get("entity") or "")):
            continue
        cands.append((str(it.get("id") or ""), cr))
    if not cands:
        return None
    try:
        from .reconciliation_engine import _cosine_sim
        fn = embed_fn or _default_embed
        vecs = fn([rep] + [c[1] for c in cands])
    except Exception:  # noqa: BLE001 -- fail-soft: no embeddings, no semantic dedup
        return None
    if not vecs or len(vecs) != len(cands) + 1:
        return None
    q = vecs[0]
    best_id: str | None = None
    best = DEDUP_EMBED_SIM
    for (cid, _cr), v in zip(cands, vecs[1:]):
        try:
            s = _cosine_sim(q, v)
        except Exception:  # noqa: BLE001
            continue
        if s >= best:
            best, best_id = s, cid
    return best_id


def _is_phi_or_lex(text: str, entity: str) -> bool:
    """True if free text must NOT be persisted raw at rest (or embedded/egressed):
    LEX-sourced OR is_any_phi (fail-closed on error). D-082 extension; upgraded to
    the 3-predicate union (is_any_phi) 2026-07-30 PHI parity-raise -- this gate also
    decides what is safe to send to the OpenAI embedding API, so it must not miss
    named LEX billing/status text with no clinical keyword."""
    if str(entity or "").strip().upper().startswith("LEX"):
        return True
    try:
        return bool(phi_guard.is_any_phi(text))
    except Exception:  # noqa: BLE001 -- fail closed
        return True


def _phi_safe_key(text: str, entity: str) -> str:
    """A PHI-safe, DISCRIMINATING key for a counting ledger (e.g. thumbs-down
    signals): the raw text when safe (so fuzzy counting clusters similar items),
    else a content hash (exact-only counting; distinct texts stay distinct without
    storing any raw LEX/PHI content)."""
    if _is_phi_or_lex(text, entity):
        return "h:" + _fingerprint("phi", text)
    return (text or "")[:300]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LEDGER_LOCK:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# Fingerprint dedup ledger (friction pattern: exact OR same-signal fuzzy)
# ─────────────────────────────────────────────────────────────────────────────
def _append_fingerprint(fp: str, signal: str, representative: str, cq_id: str,
                        class_key: str | None = None) -> None:
    _append_jsonl(_FINGERPRINT_LEDGER, {
        "fingerprint": fp,
        "signal": signal,
        "representative": (representative or "")[:300],
        "class_key": class_key or "",
        "id": cq_id,
        "ts": _now_iso(),
    })


def find_fingerprint(signal: str, representative: str,
                     *, class_key: str | None = None) -> str | None:
    """Return the cq-id of a prior candidate matching this signal+text, else None.
    Deterministic (no network); three layers, first hit wins:
      1. exact fingerprint (same signal + normalized text),
      2. cross-signal identical classifier key (title + subsystem), when supplied,
      3. same-signal paraphrase (SequenceMatcher >= FUZZY_DEDUP_RATIO).
    Runs inside the ledger lock in ``_capture`` (the semantic/embedding layer runs
    OUTSIDE the lock -- see ``_embedding_dup_id``)."""
    fp = _fingerprint(signal, representative)
    rep = _normalize(representative)
    for entry in _read_jsonl(_FINGERPRINT_LEDGER):
        if entry.get("fingerprint") == fp:
            return entry.get("id")
        if class_key and entry.get("class_key") == class_key:
            return entry.get("id")
        if entry.get("signal") == signal:
            prior = _normalize(str(entry.get("representative") or ""))
            if prior and SequenceMatcher(None, rep, prior).ratio() >= FUZZY_DEDUP_RATIO:
                return entry.get("id")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Signals / counter store (threshold gating for UC6 timeouts + UC7 thumbs-downs)
# ─────────────────────────────────────────────────────────────────────────────
def _record_signal(signal: str, key: str, extra: dict[str, Any] | None = None) -> None:
    row = {"ts": _now_iso(), "signal": signal, "key": key}
    if extra:
        row.update(extra)
    _append_jsonl(_SIGNALS_LEDGER, row)


def _count_signals(signal: str, key: str, *, days: int, fuzzy: bool = False) -> int:
    cutoff = _now() - timedelta(days=days)
    n = 0
    for row in _read_jsonl(_SIGNALS_LEDGER):
        if row.get("signal") != signal:
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts < cutoff:
            continue
        rk = str(row.get("key") or "")
        if fuzzy:
            if rk == key or SequenceMatcher(None, _normalize(rk), _normalize(key)).ratio() >= FUZZY_DEDUP_RATIO:
                n += 1
        elif rk == key:
            n += 1
    return n


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Event ledger + state fold
# ─────────────────────────────────────────────────────────────────────────────
def _append_event(event: dict[str, Any]) -> None:
    _append_jsonl(_EVENT_LEDGER, event)


# Every event kind the reducer models. F4 (Code #12, audit F1/F4): an event of any
# OTHER kind used to fall through `items.get(...)` -> None and vanish without a
# trace -- a hand-written `"event": "cowork-biweekly-review"` row made a real,
# Harrison-approved ask invisible on every surface for 16 days. Unknown kinds and
# orphaned events (no `captured` for the id) are now LOUD here and COUNTED by
# ledger_integrity() for the nightly health check.
_KNOWN_EVENT_TYPES = frozenset({
    "captured", "recurrence", "approved", "dismissed", "snoozed", "staged", "shipped",
    "reconciled", "superseded", "blocked", "edited", "evidence", "kept", "parked",
    "dm_sent", "dm_held", "dm_flushed",
})
# Warn ONCE per (kind|id) per process: get_item folds on every read, and a WARNING
# per fold would be thousands of lines a day for one bad row.
_LEDGER_ANOMALY_WARNED: set[str] = set()


def _warn_ledger_anomaly(key: str, msg: str, *args: Any) -> None:
    with _LEDGER_LOCK:
        if key in _LEDGER_ANOMALY_WARNED:
            return
        _LEDGER_ANOMALY_WARNED.add(key)
    log.warning(msg, *args)


def _fold_items() -> dict[str, dict[str, Any]]:
    """Fold the append-only event ledger into {id: record}. Last-write-wins per
    field; process_queue_action enforces which transitions are legal to write.
    An unknown event kind or an orphaned event is skipped LOUDLY (F4), never
    silently -- see ledger_integrity() for the counted view."""
    items: dict[str, dict[str, Any]] = {}
    for ev in _read_jsonl(_EVENT_LEDGER):
        et = ev.get("event")
        if et not in _KNOWN_EVENT_TYPES:
            _warn_ledger_anomaly(
                f"unknown|{et}|{ev.get('id')}",
                "code_queue: UNKNOWN ledger event kind %r for %s -- the reducer cannot apply "
                "it and the row may be invisible on every surface (ledger integrity; the "
                "nightly health check counts these)", et, ev.get("id"))
            continue
        if et == "captured":
            rec = {k: v for k, v in ev.items() if k != "event"}
            rec.setdefault("count", 1)
            rec.setdefault("status", "PROPOSED")
            items[rec.get("id", "")] = rec
            continue
        rec = items.get(ev.get("id", ""))
        if not rec:
            _warn_ledger_anomaly(
                f"orphan|{ev.get('id')}",
                "code_queue: ORPHAN ledger event %r for %s -- no `captured` event precedes "
                "it, so the item folds to nothing (ledger integrity; the nightly health "
                "check counts these)", et, ev.get("id"))
            continue
        if et == "recurrence":
            rec["count"] = int(rec.get("count", 1)) + 1
            rec["last_seen"] = ev.get("ts")
            if rec.get("status") == "PARKED":
                # C3 / D-051 lens B: a new sighting of a PARKED row is its trigger --
                # the Monday menu promotes it to DUE (never a silent count bump).
                rec["parked_recurrences"] = int(rec.get("parked_recurrences") or 0) + 1
            ev_ev = ev.get("evidence")
            if ev_ev:
                ev_list = rec.get("evidence") or []
                if len(ev_list) < 10:
                    rec["evidence"] = ev_list + [ev_ev]
        elif et == "approved":
            rec["status"] = "APPROVED"
            # Slice 4: the aging monitor measures "APPROVED for >24h with no
            # kickoff" from the APPROVAL, not from capture -- an item captured
            # weeks ago and approved today must not flag immediately. Absent on
            # legacy rows (and on seed-at-APPROVED rows, which have no `approved`
            # event at all), where the monitor falls back to `ts`.
            rec["approved_at"] = ev.get("ts")
        elif et == "dismissed":
            rec["status"] = "DISMISSED"
            if ev.get("reason"):
                rec["dismiss_reason"] = str(ev.get("reason"))  # C3: dismiss-with-evidence
        elif et == "snoozed":
            rec["status"] = "SNOOZED"
            rec["snooze_until"] = ev.get("snooze_until")
        elif et == "staged":
            rec["status"] = "STAGED"
            rec["prompt_path"] = ev.get("prompt_path", rec.get("prompt_path", ""))
            rec["bundle_id"] = ev.get("bundle_id", rec.get("bundle_id", ""))
            rec["staged_at"] = ev.get("ts")
        elif et == "shipped":
            rec["status"] = "SHIPPED"
            # C7: the shipping bundle / branch / commit ride the event itself
            # (schema was exactly {event, ts, id} until 2026-09-09). Legacy rows
            # keep whatever staging bundle_id they carried; nothing is invented.
            for k in ("bundle_id", "branch", "commit"):
                if ev.get(k):
                    rec[k] = ev[k]
            rec["shipped_at"] = ev.get("ts")
        elif et == "reconciled":
            # A step-7.5 provenance record (bundle_id + branch + commit for one
            # transition), written by the 9/3 and 9/8 reconcile scripts before the
            # C7 gate put the fields on the `shipped` event itself. Status-neutral;
            # kept on the row so "which bundle shipped this" is answerable for those
            # rows too (ledger-replay doctrine: copy the reducer's DEFAULTS, never
            # drop what it merely did not model yet).
            recs = list(rec.get("reconciled") or [])
            if len(recs) < 10:
                recs.append({k: ev.get(k) for k in ("transition", "bundle_id", "branch", "commit", "ts")})
            else:
                # D-051 lens B LOW: the cap is COUNTED, never a silent drop.
                rec["reconciled_dropped"] = int(rec.get("reconciled_dropped") or 0) + 1
            rec["reconciled"] = recs
            for k in ("bundle_id", "branch", "commit"):
                if ev.get(k) and not rec.get(k):
                    rec[k] = ev[k]
        elif et == "superseded":
            rec["status"] = "SUPERSEDED"
            if ev.get("superseded_by"):
                rec["superseded_by"] = ev.get("superseded_by")
        elif et == "blocked":
            rec["status"] = "BLOCKED"
        elif et == "edited":
            if ev.get("title"):
                rec["title"] = ev["title"]
            if ev.get("summary"):
                rec["summary"] = ev["summary"]
            # Slice 0 (2026-08-05): a severity re-rate rides the SAME edited event
            # (last-write-wins on the field, like title/summary) so re-prioritizing
            # never needs a ledger hand-edit. set_severity validates the vocabulary.
            if ev.get("severity"):
                rec["severity"] = ev["severity"]
        elif et == "evidence":
            # Append-only extra evidence for an EXISTING occurrence -- deliberately
            # NOT a `recurrence` (which means "seen again" and bumps `count`).
            # Attaching a real-world example to an already-filed item must not
            # inflate the recurrence counter the Monday menu ranks on.
            note = ev.get("evidence")
            if note:
                ev_list = rec.get("evidence") or []
                if len(ev_list) < 10:
                    rec["evidence"] = ev_list + [note]
        elif et == "kept":
            rec["last_touch"] = ev.get("ts")
            # C3 (Code #12): a Keep is COUNTED. One tap used to buy 14 silent days
            # with no reason, no trigger and no cap (audit F6: 16 of 23 aged rows
            # suppressed by a single tap); the Monday menu renders the count and
            # caps Keeps at KEEP_CAP -- after that the row must be parked with a
            # trigger, shipped or dismissed.
            rec["keep_count"] = int(rec.get("keep_count") or 0) + 1
        elif et == "parked":
            # C3: park-with-trigger. A PARKED row leaves every menu section until
            # its date trigger passes (park_until) -- an event-parked row (no date)
            # stays listed count-only so it can never silently vanish.
            rec["status"] = "PARKED"
            rec["park_reason"] = str(ev.get("reason") or "")
            rec["park_until"] = str(ev.get("until") or "")
            rec["park_event"] = str(ev.get("trigger_event") or "")
            rec["park_horizon"] = bool(ev.get("review_horizon"))
            rec["parked_at"] = ev.get("ts")
            rec["parked_recurrences"] = 0
        elif et == "dm_sent":
            rec["dm_channel_id"] = ev.get("dm_channel_id", "")
            rec["dm_message_ts"] = ev.get("dm_message_ts", "")
            rec["dm_ts_at"] = ev.get("ts")
        elif et == "dm_held":
            rec["dm_held"] = True
        elif et == "dm_flushed":
            rec["dm_held"] = False
            rec["dm_flushed"] = True
    return items


def ledger_integrity(path: Path | None = None) -> dict[str, Any]:
    """F4 (Code #12): the folded-vs-raw reconciliation of the event ledger.

    {raw_ids, folded_ids, orphans: [{id, events, kinds}], unknown_event_types:
     {kind: n}, unknown_event_ids: [id]} -- an ORPHAN is an id that has events but
    no `captured` event (the 7/30 cleanup removed a captured row and left 7
    descendants; a hand-written row used an invented kind); an UNKNOWN kind is one
    the reducer does not model. Both are the population the fold drops. Reads the
    RAW file (never the fold) so the two can be compared. Raises FileNotFoundError
    on a MISSING ledger and counts every unparseable line (`unparseable`) -- blind
    must never read as clean (D-051 lens B MED #3: the first cut delegated to
    _read_jsonl, which returns [] for a missing file and skips torn lines, so a
    0-byte ledger reported '0 ids, all fold' and the health check said ok).
    """
    p = Path(path) if path is not None else Path(_EVENT_LEDGER)
    if not p.exists():
        raise FileNotFoundError(f"code-queue ledger missing: {p}")
    events: list[dict[str, Any]] = []
    unparseable = 0
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            unparseable += 1  # a torn line is a LOST event, and the fold skips it silently
    captured: set[str] = set()
    per_id: dict[str, dict[str, Any]] = {}
    unknown: dict[str, int] = {}
    unknown_ids: list[str] = []
    for ev in events:
        et = str(ev.get("event") or "")
        cid = str(ev.get("id") or "")
        if et not in _KNOWN_EVENT_TYPES:
            unknown[et] = unknown.get(et, 0) + 1
            if cid and cid not in unknown_ids:
                unknown_ids.append(cid)
        if et == "captured":
            captured.add(cid)
        if cid:
            slot = per_id.setdefault(cid, {"events": 0, "kinds": []})
            slot["events"] += 1
            if et not in slot["kinds"]:
                slot["kinds"].append(et)
    orphans = [{"id": cid, "events": slot["events"], "kinds": slot["kinds"]}
               for cid, slot in sorted(per_id.items()) if cid not in captured]
    return {
        "raw_ids": len(per_id),
        "folded_ids": len(captured),
        "orphans": orphans,
        "unknown_event_types": unknown,
        "unknown_event_ids": unknown_ids,
        "unparseable": unparseable,
    }


# F5 (Code #12, audit F5): the queue's one monitor watched APPROVED + P0/P1 + no
# kickoff -- a population every approval path empties synchronously, so it read
# "ok" permanently while 8 HIGH-class items rotted in PROPOSED, three of them a
# month old. This is the sibling gauge, aimed at the tier that actually regresses.
PROPOSED_PRIORITY_AGING_DAYS = 14


def proposed_priority_aging(days: int = PROPOSED_PRIORITY_AGING_DAYS) -> dict[str, Any]:
    """{aged_priority: [{id, severity, entity, age_days}], aged_total: n,
     proposed_total: n} -- PROPOSED rows that are P0/P1-class (either vocabulary)
    and older than `days`, oldest first, plus the size of the whole aged PROPOSED
    tier for context. Raises on a ledger read failure (blind != clean)."""
    items = load_items()
    proposed = [it for it in items if it.get("status") == "PROPOSED"]
    aged = [it for it in proposed if _age_days(it.get("ts")) >= max(0, int(days))]
    out = [{"id": it.get("id", ""), "severity": it.get("severity", ""),
            "entity": it.get("entity", ""), "age_days": _age_days(it.get("ts"))}
           for it in aged if is_priority_severity(it.get("severity"))]
    out.sort(key=lambda r: -r["age_days"])
    return {"aged_priority": out, "aged_total": len(aged), "proposed_total": len(proposed)}


def parked_aging() -> dict[str, Any]:
    """{due: [{id, severity, entity, park_until, parked_recurrences, days_parked}],
     parked_total: n} -- PARKED rows whose resume date has passed or that were
    re-asked since parking (either is the trigger): the population the Monday menu
    is the ONLY surface for, watched by no gauge until now (D-051 lens B HIGH #2).
    Raises on a ledger read failure (blind != clean)."""
    items = load_items()
    parked = [it for it in items if it.get("status") == "PARKED"]
    due, _waiting = _parked_rows(parked, _now())
    out = [{"id": it.get("id", ""), "severity": it.get("severity", ""),
            "entity": it.get("entity", ""), "park_until": str(it.get("park_until") or "")[:10],
            "parked_recurrences": int(it.get("parked_recurrences") or 0),
            "days_parked": _age_days(it.get("parked_at") or it.get("ts"))}
           for it in due]
    out.sort(key=lambda r: -r["days_parked"])
    return {"due": out, "parked_total": len(parked)}


_KNOWN_IDS_CACHE: dict[str, Any] = {"key": None, "ids": frozenset()}


def known_ids() -> frozenset[str]:
    """Every cq- id that appears in ANY event of the RAW ledger -- orphans (events
    with no ``captured``) included, because an id the ledger has ever seen is not
    FABRICATED whatever the fold makes of it. The reference set for the S2'
    fabricated-id screen (slack_egress.screen_phantom_write_claims, Code #12).
    Cached on (path, mtime, size) so a hot reply path never re-parses an unchanged
    file; an unreadable ledger returns the empty set and the caller treats that as
    "cannot check", never as "everything is fabricated"."""
    path = _EVENT_LEDGER
    try:
        st = Path(path).stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        # D-051 lens C F6: a MISSING ledger is "cannot check", never "every id is
        # fabricated" -- an empty reference set would redact every real id under
        # ENFORCE. None tells the screen to skip the family with a WARNING.
        return None  # type: ignore[return-value]
    except OSError:
        raise  # unreadable (not missing): the screen skips the family with a WARNING
    with _LEDGER_LOCK:
        if _KNOWN_IDS_CACHE.get("key") == key:
            return _KNOWN_IDS_CACHE["ids"]
    ids = frozenset(
        str(ev.get("id") or "").lower() for ev in _read_jsonl(Path(path)) if ev.get("id"))
    with _LEDGER_LOCK:
        _KNOWN_IDS_CACHE.update({"key": key, "ids": ids})
    return ids


def load_items() -> list[dict[str, Any]]:
    """All queue records (folded), newest-captured first.

    D-051 finding (2026-07-30 adversarial review, all 3 review lenses
    independently): the egress-side LEX redaction (_lex_safe_view) was wired
    into render_backlog_text ONLY -- build_item_card, generate_kickoff_prompt,
    process_queue_action, and stage_bundle all read via get_item/load_items and
    would render/egress a legacy (pre-parity-raise) raw-LEX-title record
    verbatim, including to the Anthropic API via generate_kickoff_prompt. Now
    centralized HERE (the read layer) so every consumer gets the redacted view
    by construction -- no consumer can forget to call it."""
    items = [_lex_safe_view(r) for r in _fold_items().values()]
    items.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    return items


def get_item(cq_id: str) -> dict[str, Any] | None:
    item = _fold_items().get(cq_id)
    return _lex_safe_view(item) if item is not None else None


def _dm_sent_today() -> int:
    today = _now().date()
    n = 0
    for ev in _read_jsonl(_EVENT_LEDGER):
        if ev.get("event") != "dm_sent":
            continue
        ts = _parse_ts(ev.get("ts"))
        if ts and ts.date() == today:
            n += 1
    return n


# ─────────────────────────────────────────────────────────────────────────────
# PHI sink + core capture
# ─────────────────────────────────────────────────────────────────────────────
def _representative(rec: dict[str, Any]) -> str:
    return str(rec.get("representative") or (rec.get("title", "") + " " + rec.get("summary", ""))).strip()


def _scrub_evidence(evidence: list[dict[str, Any]] | None, *, is_lex: bool) -> list[dict[str, Any]]:
    """LEX -> pointers only (channel_id + ts, never text). Any entity -> a note is
    dropped to a pointer if it itself trips is_any_phi (belt-and-braces; upgraded to
    the 3-predicate union 2026-07-30 PHI parity-raise)."""
    out: list[dict[str, Any]] = []
    for e in (evidence or [])[:5]:
        ptr = {"channel_id": str(e.get("channel_id", "") or ""), "ts": str(e.get("ts", "") or "")}
        note = str(e.get("note", "") or "")
        if note and not is_lex and not phi_guard.is_any_phi(note):
            ptr["note"] = note[:200]
        out.append(ptr)
    return out


# LEX build-ask redaction (D-051 PHI parity-raise, 2026-07-30 Wave-1 Fork 3b): the
# fixed placeholder used for a LEX-entity item's title/summary/fix_sketch wherever
# they would otherwise egress -- capture (write), render_backlog_text, and the MCP
# code_queue_view surface (which simply calls render_backlog_text, so one fix covers
# both). representative/evidence already got this treatment (D-082); title/summary/
# fix_sketch did not, and persisted RAW into code-session-queue.jsonl, exposed
# verbatim by render_backlog_text, build_item_card, and generate_kickoff_prompt.
# Applied unconditionally for ANY LEX entity, not just PHI-tripping text -- the LEX
# wall is fail-closed regardless of whether this particular ask happens to trip a
# PHI predicate (mirrors the existing representative/evidence blanket redaction).
_LEX_REDACTED_TITLE = "[LEX build ask -- details withheld]"

# D-051 2026-07-31 (session-snapshots review): the staged prompt FILENAME embeds
# _slug(title) -- title-DERIVED text that must not outlive the title redaction
# (pre-parity-raise LEX rows persisted raw titles, so their staged paths carry
# raw-title slugs forever). Replaced with a fixed placeholder that stays TRUTHY:
# the staging idempotency checks in process_queue_action key on prompt_path
# truthiness, so blanking it would re-stage (and double-generate) LEX items.
_LEX_REDACTED_PROMPT_PATH = "[LEX staged prompt -- path withheld]"


def _redact_lex_build_fields(rec: dict[str, Any]) -> None:
    """In-place: blank title/summary/fix_sketch for a LEX-entity record. Call BEFORE
    the record is ever persisted (only meaningful on the NEW-item capture path --
    a recurrence event never re-persists these fields)."""
    rec["title"] = _LEX_REDACTED_TITLE
    rec["summary"] = ""
    rec["fix_sketch"] = ""


def _lex_safe_view(it: dict[str, Any]) -> dict[str, Any]:
    """Egress-side re-check (D-051: never trust the write-side redaction alone) --
    used by render_backlog_text (and therefore the MCP code_queue_view surface,
    which calls it directly) so a legacy/pre-fix record or any future write-path
    regression still cannot surface raw LEX title/summary/fix_sketch text."""
    if str(it.get("entity", "")).strip().upper().startswith("LEX"):
        it = dict(it)
        it["title"] = _LEX_REDACTED_TITLE
        it["summary"] = ""
        it["fix_sketch"] = ""
        if it.get("prompt_path"):
            it["prompt_path"] = _LEX_REDACTED_PROMPT_PATH
        # C3 typed fields (park reason / event, dismissal evidence) are PHI-screened
        # at write time; the LEX blanket still applies at egress (D-051 lens A LOW #11).
        for k in ("park_reason", "park_event", "dismiss_reason"):
            if it.get(k):
                it[k] = "[LEX -- withheld]"
    return it


def _capture(rec: dict[str, Any], *, initial_status: str = "PROPOSED",
             client_factory: Callable | None = None,
             dm_held: bool = False) -> str | None:
    """Deduplicate, PHI-gate, persist, and (if live) DM a card. Returns the cq-id
    (existing on recurrence, new on first sighting) or None if dropped.

    dm_held: capture the item but suppress the immediate DM card (persist it
    dm_held so the overflow flush surfaces it on the next knowledge-review run).
    Used when a confirmed ask is over quota -- it must never vanish, but Harrison
    must not be stormed (1g). Only meaningful for a NEW item.

    Runs off the hot path (see the public capture_* entry points)."""
    entity = str(rec.get("entity") or "FNDR").strip().upper()
    is_lex = entity.startswith("LEX")

    # Summary PHI gate -- FAIL-CLOSED. is_any_phi error is treated as PHI (drop).
    # Covers every model-authored field that could carry PHI (title/summary/fix).
    # Upgraded from is_phi_risk alone to the 3-predicate union (2026-07-30 PHI
    # parity-raise): is_phi_risk alone missed named LEX billing/authorization/
    # eligibility text with no clinical keyword (is_lex_billing_status_phi) and bare
    # diagnosis/medication terms (is_clinical_phi) -- exactly the class a LEX
    # capability ask like "can you access Marcus's service hours?" falls into.
    summary_text = (f"{rec.get('title', '')} {rec.get('summary', '')} "
                    f"{rec.get('fix_sketch', '')}").strip()
    try:
        phi = phi_guard.is_any_phi(summary_text)
    except Exception:  # noqa: BLE001 -- fail closed
        phi = True
    if phi:
        log.info("code_queue: dropped PHI-flagged candidate (signal=%s entity=%s)",
                 rec.get("signal"), entity)
        return None

    # D-051 2026-07-31 (session-snapshots review follow-through): subsystem_guess
    # was the ONE model-authored field outside the PHI screen above, yet it
    # egresses via mixed-bundle prompt-path slugs (_affinity_key -> _bundle_theme
    # -> _slug), where the LEX prompt_path redaction cannot reach co-bundled
    # non-LEX items. A PHI-tripping value must never persist. Fail-closed blank
    # (the item survives; only the affinity hint is dropped).
    sub = str(rec.get("subsystem_guess") or "")
    if sub:
        try:
            if phi_guard.is_any_phi(sub):
                rec["subsystem_guess"] = ""
        except Exception:  # noqa: BLE001 -- fail closed
            rec["subsystem_guess"] = ""

    rec["evidence"] = _scrub_evidence(rec.get("evidence"), is_lex=is_lex)

    signal = str(rec.get("signal") or "unknown")
    representative = _representative(rec)
    rec["fingerprint"] = _fingerprint(signal, representative)
    class_key = _class_key(str(rec.get("title", "")), str(rec.get("subsystem_guess", "")))

    # PHI-safe persistence of the dedup basis: NEVER store raw LEX or PHI-tripping
    # text in the fingerprint ledger OR the event record. The hash (already
    # computed from the full text) still dedups exact repeats; only fuzzy dedup is
    # forgone for these. D-082 extension; is_any_phi 2026-07-30 parity-raise.
    try:
        rep_phi = phi_guard.is_any_phi(representative)
    except Exception:  # noqa: BLE001 -- fail closed
        rep_phi = True
    store_rep = "" if (is_lex or rep_phi) else representative
    if store_rep != representative:
        rec["representative"] = ""

    # Semantic (embedding) dedup runs OUTSIDE the lock -- it makes a network call and
    # must not hold the process-wide ledger lock. Only computed when the cheap
    # deterministic layers miss (a lock-free pre-read), so an exact/near-exact repeat
    # never pays an embedding round-trip. Re-validated inside the lock below.
    emb_id: str | None = None
    if find_fingerprint(signal, representative, class_key=class_key) is None:
        emb_id = _embedding_dup_id(signal, representative, entity)

    # Dedup decision + ledger writes are ONE atomic critical section (D-051 TOCTOU
    # fix): the deterministic find (read) and the fingerprint append must not
    # interleave, or two concurrent captures of the same signal both miss and both
    # mint a card. _LEDGER_LOCK is reentrant, so the nested _append_jsonl re-acquire
    # is fine. Network (embedding, DM) + backlog render stay OUTSIDE the lock.
    with _LEDGER_LOCK:
        existing_id = find_fingerprint(signal, representative, class_key=class_key)
        # A confirmed EXPLICIT human ask must NEVER silently merge into a CLOSED item
        # (finding-6 invariant: a confirmed ask never vanishes). find_fingerprint is
        # status-blind, so an explicit re-ask matching a DISMISSED/SHIPPED/SUPERSEDED
        # fingerprint would otherwise record a recurrence onto the terminal item and never
        # resurface. For the explicit signal only, ignore a terminal match and mint a fresh
        # (APPROVED/PROPOSED) item. Other signals keep the existing dedup-onto-any behavior.
        if existing_id and signal == "explicit":
            cand = get_item(existing_id)
            if not (cand and cand.get("status") in _MERGE_TARGET_STATUSES):
                existing_id = None
        if existing_id is None and emb_id:
            cand = get_item(emb_id)
            if cand and cand.get("status") in _MERGE_TARGET_STATUSES:
                existing_id = emb_id  # semantic paraphrase of a still-open item
        if existing_id:
            _append_event({
                "event": "recurrence", "ts": _now_iso(), "id": existing_id,
                "evidence": (rec.get("evidence") or [None])[0],
            })
            is_new = False
            result_id = existing_id
        else:
            result_id = "cq-" + uuid.uuid4().hex[:12]
            rec["id"] = result_id
            rec["ts"] = _now_iso()
            rec["status"] = initial_status
            rec.setdefault("count", 1)
            if is_lex:
                # D-051 PHI parity-raise (2026-07-30): redact BEFORE this record is
                # ever persisted -- title/summary/fix_sketch previously survived raw
                # (only representative/evidence were redacted). rec is the SAME
                # object used below for the DM card, so the card is covered too.
                _redact_lex_build_fields(rec)
            if dm_held:
                # Persist the hold on the captured event so the reducer folds it and
                # maybe_flush_overflow() delivers it on the next review run (never lost).
                rec["dm_held"] = True
            _append_event({"event": "captured", **rec})
            _append_fingerprint(rec["fingerprint"], signal, store_rep, result_id,
                                class_key=class_key)
            is_new = True

    _render_backlog_safe()
    if code_queue_level() == "live":
        if is_new and not dm_held:
            _send_new_item_card(rec, client_factory)
        elif not is_new:
            _thread_count_update(result_id, client_factory)
    return result_id


# ─────────────────────────────────────────────────────────────────────────────
# Off-hot-path dispatch (daemon thread by default; inline under _SYNC for tests)
# ─────────────────────────────────────────────────────────────────────────────
def _guarded_run(fn: Callable, *args: Any) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 -- capture worker must never surface
        log.warning("code_queue: worker error (non-fatal)", exc_info=True)


def _submit(fn: Callable, *args: Any) -> None:
    if _SYNC:
        _guarded_run(fn, *args)
    else:
        threading.Thread(target=_guarded_run, args=(fn, *args), daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# S1 -- tool-dispatch failures (crash immediate; timeout counter-gated per UC6)
# ─────────────────────────────────────────────────────────────────────────────
def capture_tool_failure(tool_name: str, entity: str, error_class: str,
                         channel_id: str, slack_user_id: str, is_timeout: bool,
                         *, client_factory: Callable | None = None,
                         thread_ts: str = "") -> None:
    """Hot-path entry (called from dispatch's timeout + crash arms). Fail-soft:
    NEVER raises, NEVER blocks the dispatch return. No message text is captured
    (evidence is a channel pointer only), so S1 is inherently PHI-safe.

    thread_ts (C1): the thread root of the turn that crashed, so the pointer is a
    PERMALINK (channel_id + ts) and the item passes the evidence floor."""
    try:
        if code_queue_level() == "off":
            return
        _submit(_process_tool_failure, tool_name, entity, error_class,
                channel_id, slack_user_id, is_timeout, client_factory,
                str(thread_ts or ""))
    except Exception:  # noqa: BLE001 -- belt-and-braces; capture may never affect the reply
        log.debug("code_queue.capture_tool_failure swallowed", exc_info=True)


def _process_tool_failure(tool_name: str, entity: str, error_class: str,
                          channel_id: str, slack_user_id: str, is_timeout: bool,
                          client_factory: Callable | None, thread_ts: str = "") -> None:
    tool_name = str(tool_name or "").strip() or "unknown_tool"
    _record_signal("tool_failure", tool_name, {"timeout": bool(is_timeout)})
    user_present = bool((slack_user_id or "").strip())

    if is_timeout:
        n = _count_signals("tool_failure", tool_name, days=SILENT_TIMEOUT_WINDOW_DAYS)
        if n < SILENT_TIMEOUT_THRESHOLD:
            return  # single/rare timeout: counter only (UC6)
        severity, title = "P2", f"`{tool_name}` repeatedly timing out"
        summary = (f"`{tool_name}` has timed out {n}x in {SILENT_TIMEOUT_WINDOW_DAYS}d "
                   f"-- likely silent degradation (UC6).")
    else:
        if not user_present:
            return  # conservative: crash with no user context is not carded
        severity, title = "P1", f"`{tool_name}` crashed"
        summary = f"`{tool_name}` raised {error_class or 'an unexpected error'} for a user in a live turn (UC1)."

    rec = {
        "kind": "bug", "severity": severity, "title": title, "summary": summary,
        "subsystem_guess": tool_name, "entity": entity, "signal": "tool_error",
        "representative": tool_name,  # invariant: same tool failing = same item
        # C1: the thread root is the PERMALINK half of the pointer; the note still
        # carries no message text (PHI-safe by construction).
        "evidence": [{"channel_id": channel_id, "ts": str(thread_ts or ""),
                      "note": "tool failure (no message text)"}],
        "reporter": slack_user_id,
    }
    _capture(rec, client_factory=client_factory)


# ─────────────────────────────────────────────────────────────────────────────
# S2 + S4 -- phrase signals + capability deflections (from _extract_and_log_gap)
# ─────────────────────────────────────────────────────────────────────────────
# Word-boundary phrase regex. Skips Cora-authored messages and >-quoted lines at
# the call site (friction-mining lesson). Matches are classifier candidates.
_PHRASE_RE = re.compile(
    r"\b(cora should|can cora|could cora|does cora|cora can't|cora cannot|"
    r"cora doesn't|feature request|would be great if cora|wish cora|"
    r"it'?s broken|is broken|doesn'?t work|not working|"
    r"there'?s a bug|it'?s a bug|that'?s a bug)\b",
    re.IGNORECASE,
)
# A response that reads like a capability deflection (Cora said she can't / has no tool).
_DEFLECTION_RE = re.compile(
    r"\b(i don'?t have (a|the|any)?\s*(tool|way|ability|access)|i can'?t (do|pull|access|fetch)|"
    r"i'?m not able to|i am not able to|i don'?t (currently )?have (that|the ability)|"
    r"no tool (for|to)|i cannot (pull|access|fetch|do that)|that'?s not something i can)\b",
    re.IGNORECASE,
)


def _strip_quoted(text: str) -> str:
    return "\n".join(ln for ln in (text or "").splitlines() if not ln.lstrip().startswith(">"))


def capture_message_signal(text: str, entity: str, channel_id: str, channel_name: str,
                           slack_user_id: str, response_text: str = "",
                           *, client_factory: Callable | None = None,
                           message_ts: str = "") -> None:
    """Hot-path entry (called post-reply from _extract_and_log_gap). Detects an
    S2 phrase in the user's message OR an S4 capability deflection in Cora's reply;
    a hit becomes a classifier candidate. Fail-soft, off-thread, dedup-before-model.

    message_ts (C1): the triggering message's / thread root's Slack ts, so the
    evidence row can become a PERMALINK (channel_id + ts) instead of the bare
    channel pointer every passive capture wrote before 2026-09-09 -- which is why
    the 9/7 auto-generated kickoffs all read "no Slack permalink"."""
    try:
        if code_queue_level() == "off":
            return
        # D-104 [QA] quarantine (2026-08-06): a smoke-test message must never mint a
        # capture card. Checked before phrase/deflection detection so neither signal
        # can carry it through.
        from . import qa_scaffolding
        if qa_scaffolding.is_qa_message(text):
            log.info("code_queue: [QA] smoke message -- signal not captured")
            return
        clean = _strip_quoted(text or "")
        phrase_hit = bool(_PHRASE_RE.search(clean))
        deflect_hit = bool(_DEFLECTION_RE.search(response_text or ""))
        if not (phrase_hit or deflect_hit):
            return
        signal = "phrase" if phrase_hit else "deflection"
        _submit(_process_message_signal, clean, entity, channel_id, channel_name,
                slack_user_id, signal, client_factory, str(message_ts or ""))
    except Exception:  # noqa: BLE001
        log.debug("code_queue.capture_message_signal swallowed", exc_info=True)


def _process_message_signal(text: str, entity: str, channel_id: str, channel_name: str,
                            slack_user_id: str, signal: str,
                            client_factory: Callable | None, message_ts: str = "") -> None:
    question = (text or "").strip()
    if len(question) < 8:
        return
    # Dedup BEFORE the Haiku call (cost + noise): a known fingerprint recurs.
    existing_id = find_fingerprint(signal, question)
    if existing_id:
        _append_event({"event": "recurrence", "ts": _now_iso(), "id": existing_id})
        _render_backlog_safe()
        if code_queue_level() == "live":
            _thread_count_update(existing_id, client_factory)
        return

    # PHI egress guard (fail-closed): never send LEX/PHI client text to the Haiku
    # classifier. Non-PHI LEX build-asks (e.g. "cora should add an LTS scheduler")
    # still pass; PHI-tripping ones are dropped before any model call. Upgraded to
    # the 3-predicate union (is_any_phi) 2026-07-30 PHI parity-raise.
    #
    # D-051: the C12 precision pass briefly repointed this to is_any_phi_request,
    # which is wrong and contradicts that function's own docstring -- this is
    # THIRD-PARTY EGRESS, where recall beats precision and over-refusing costs
    # nothing. The request-shaped union belongs only where a human's typed text is
    # REFUSED TO THEIR FACE (seed_item's title/summary gate, apply_edit), which is
    # where both live 8/24 false refusals actually happened. Persistence and
    # egress screens stay strict.
    try:
        if phi_guard.is_any_phi(question):
            log.info("code_queue: message signal dropped pre-classify (PHI)")
            return
    except Exception:  # noqa: BLE001 -- fail closed
        return

    verdict = classify_candidate(question, entity)
    if verdict is None:
        return  # fail-closed: API/parse error proposes nothing
    kind = verdict.get("kind")
    if kind == "noise":
        # Remember the fingerprint so identical noise never re-classifies. The
        # exact-dedup HASH is stored in the fingerprint field; the raw text is the
        # (fuzzy-only) representative -- redact it for LEX so no raw LEX message text
        # lands in the ledger (D-082). Exact dedup via the hash is unaffected.
        is_lex = str(entity or "").strip().upper().startswith("LEX")
        _append_fingerprint(_fingerprint(signal, question), signal,
                            "" if is_lex else question, "noise")
        return
    if kind == "knowledge":
        # Fork 3a (Wave-1 flywheel-conversion calibration): a capability ask
        # ("can you access X", "do you have access to X") is a missing TOOL, not
        # a missing FACT -- reroute it to the code-queue as a feature candidate
        # instead of the knowledge flywheel. D-051 adversarial review MEDIUM:
        # an earlier version ran this check BEFORE classify_candidate for every
        # signal (including S2 phrase / S4 deflection hits), which downgraded a
        # real bug report ("it's not working -- can you check why the Shopify
        # sync failed?") from Haiku's P1/P2 "bug" verdict to a P3 "feature"
        # verdict, since ordinary bug-report phrasing ("can you check X")
        # matches the capability-ask pattern too. Now the deterministic check
        # only ever OVERRIDES Haiku's OWN "knowledge" verdict -- it can never
        # downgrade a bug/feature/config classification Haiku already made.
        from . import knowledge_gaps
        if knowledge_gaps.is_capability_ask(question):
            capture_capability_ask(question, entity, channel_name, slack_user_id,
                                   client_factory=client_factory,
                                   channel_id=channel_id, message_ts=message_ts)
        else:
            _route_to_flywheel(question, entity, channel_name, slack_user_id)
        return

    rec = {
        "kind": kind if kind in VALID_KINDS else "feature",
        "severity": verdict.get("severity", "P3"),
        "title": verdict.get("summary", question)[:120],
        "summary": verdict.get("summary", "")[:200],
        "subsystem_guess": verdict.get("subsystem_guess", ""),
        "entity": entity, "signal": signal,
        "representative": question,
        # C1: ts is the PERMALINK half of the pointer (was always "" before 2026-09-09).
        "evidence": [{"channel_id": channel_id, "ts": str(message_ts or ""), "note": question[:400]}],
        "reporter": slack_user_id,
        "fix_sketch": verdict.get("fix_sketch", ""),
    }
    _capture(rec, client_factory=client_factory)


def _route_to_flywheel(question: str, entity: str, channel_name: str, user: str) -> None:
    """Knowledge-shaped finding -> the existing knowledge flywheel, never the queue.

    D-051 PHI PARITY-RAISE (2026-07-30, Wave-1 Fork 3b -- the load-bearing fix): this
    path previously called log_gap with ZERO PHI screening -- a LEX ask Haiku
    miscalled "knowledge" would persist RAW client text into knowledge-gaps.jsonl
    (confirmed live: the 7/30 LEX how-to-guide asks). Now fail-closed on two levels:
    LEX entities are skipped OUTRIGHT (mirrors gap_detection.maybe_log_gap's existing
    LEX-skip -- this path must never be a side-door around it), and any other
    entity's question is screened with the 3-predicate union (is_any_phi) before the
    write. knowledge_gaps.log_gap ALSO carries its own capability-ask intercept
    (Fork 3a) that fires before this function's write would land, so most capability
    asks reaching here never write at all -- this guard is the belt for the rest.
    """
    try:
        ent = (entity or "FNDR").strip().upper()
        if ent.startswith("LEX"):
            log.debug("code_queue: LEX entity -- flywheel route skipped (fail-closed)")
            return
        if phi_guard.is_any_phi(question):
            log.info("code_queue: flywheel route dropped (PHI-flagged question)")
            return
        from . import knowledge_gaps
        knowledge_gaps.log_gap(
            entity=entity, channel=channel_name, user=user or "",
            question=question, response_chars=0,
            gap="capability/knowledge ask routed from code-queue classifier",
            latency_ms=0, detector="code_queue_route",
        )
    except Exception:  # noqa: BLE001
        log.debug("code_queue: flywheel route failed (non-fatal)", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Fork 3a (Wave-1 flywheel-conversion calibration) -- capability-ask routing
# ─────────────────────────────────────────────────────────────────────────────
def capture_capability_ask(question: str, entity: str, channel: str, user: str | None,
                           *, client_factory: Callable | None = None,
                           channel_id: str = "", message_ts: str = "") -> None:
    """Route a deterministically-classified capability ask (knowledge_gaps.
    is_capability_ask) straight to the code-queue as a feature candidate -- never
    through Haiku (the deterministic verdict already decided kind) and never through
    the knowledge-gap log. Hot-path entry (called from knowledge_gaps.log_gap on all
    three of its callers, and directly from _process_message_signal before the Haiku
    call): fail-soft, off-thread. `channel` may be a channel NAME or ID depending on
    the caller -- stored only as a note string, never as a slack:// deep-link target,
    so an inconsistent shape can never render a broken link."""
    try:
        if code_queue_level() == "off":
            return
        question = (question or "").strip()
        if not question:
            return
        _submit(_process_capability_ask, question, entity, channel, user, client_factory,
                str(channel_id or ""), str(message_ts or ""))
    except Exception:  # noqa: BLE001
        log.debug("code_queue.capture_capability_ask swallowed", exc_info=True)


def _process_capability_ask(question: str, entity: str, channel: str, user: str | None,
                            client_factory: Callable | None,
                            channel_id: str = "", message_ts: str = "") -> None:
    # C1: a capability ask minted from a message now carries the message's
    # PERMALINK halves when the caller has them (channel_id + ts); the
    # knowledge_gaps.log_gap callers know only the channel NAME, so their rows
    # keep the name-only note and fall under the evidence floor until a human
    # attaches the thread -- honest, never a fabricated pointer. Passive captures
    # mint PROPOSED, never APPROVED: the founder fast-path exists ONLY in
    # queue_explicit (the deliberate tool call), so a founder's QUESTION can never
    # arrive at APPROVED through this route (cq-b6f2f4825ffb, pinned in tests).
    rec = {
        "kind": "feature", "severity": "P3",
        "title": question[:120], "summary": question[:200],
        "subsystem_guess": "", "entity": entity, "signal": "capability",
        "representative": question,
        "evidence": [{"channel_id": str(channel_id or ""), "ts": str(message_ts or ""),
                      "note": f"#{channel}" if channel else ""}],
        "reporter": user or "",
    }
    _capture(rec, client_factory=client_factory)


# ─────────────────────────────────────────────────────────────────────────────
# S3 -- repeated thumbs-down (from _handle_reaction)
# ─────────────────────────────────────────────────────────────────────────────
def capture_thumbsdown(channel_id: str, message_ts: str, entity: str, reactor: str,
                       *, client: Any = None, client_factory: Callable | None = None) -> None:
    """Hot-path entry (called from _handle_reaction's negative-reaction branch).
    Fingerprints the reacted reply; >= THUMBSDOWN_THRESHOLD similar in the window
    promotes to a candidate. Fail-soft, off-thread."""
    try:
        if code_queue_level() == "off":
            return
        _submit(_process_thumbsdown, channel_id, message_ts, entity, reactor,
                client, client_factory)
    except Exception:  # noqa: BLE001
        log.debug("code_queue.capture_thumbsdown swallowed", exc_info=True)


def _fetch_message_text(client: Any, channel_id: str, message_ts: str) -> str:
    if client is None or not channel_id or not message_ts:
        return ""
    try:
        resp = client.conversations_history(
            channel=channel_id, latest=message_ts, inclusive=True, limit=1,
        )
        msgs = resp.get("messages") or []
        return str(msgs[0].get("text", "")) if msgs else ""
    except Exception:  # noqa: BLE001
        return ""


def _process_thumbsdown(channel_id: str, message_ts: str, entity: str, reactor: str,
                        client: Any, client_factory: Callable | None) -> None:
    reply_text = _fetch_message_text(client, channel_id, message_ts)
    # Fingerprint basis: the reacted reply text if we could fetch it, else the ts
    # (so a lone thumbs-down we can't read still counts once, never dedups falsely).
    basis = reply_text.strip() or f"ts:{message_ts}"
    # PHI-safe signals key: NEVER persist raw LEX/PHI reply text at rest -- hash it
    # so counting still discriminates distinct replies without storing content
    # (D-082). Non-LEX/non-PHI keeps the raw text so fuzzy counting clusters.
    key = _phi_safe_key(basis, entity)
    _record_signal("thumbsdown", key)
    n = _count_signals("thumbsdown", key, days=THUMBSDOWN_WINDOW_DAYS, fuzzy=True)
    if n < THUMBSDOWN_THRESHOLD:
        return
    rec = {
        "kind": "bug", "severity": "P2",
        "title": "Repeated thumbs-down on similar replies",
        "summary": (f"{n} thumbs-downs on similar Cora replies in "
                    f"{THUMBSDOWN_WINDOW_DAYS}d -- the answer path may be wrong (UC7)."),
        "subsystem_guess": "qa_reply", "entity": entity, "signal": "thumbsdown",
        "representative": basis,
        "evidence": [{"channel_id": channel_id, "ts": message_ts,
                      "note": reply_text[:300] if reply_text else "thumbs-down (text unavailable)"}],
        "reporter": reactor,
    }
    _capture(rec, client_factory=client_factory)


# ─────────────────────────────────────────────────────────────────────────────
# S6 -- friction-mining spillover (Cora-tool builds; route == cora_tool)
# ─────────────────────────────────────────────────────────────────────────────
def register_from_efficiency(payload: dict[str, Any]) -> str | None:
    """Called from friction_mining.apply_efficiency when an approved efficiency item
    is a Cora-tool build (D-029 language-side, route == 'cora_tool'). Lands the item
    APPROVED (Harrison already approved the efficiency finding). Idempotent on
    fingerprint. Fail-soft -- never raises into the friction executor."""
    try:
        if code_queue_level() == "off":
            return None
        title = str(payload.get("title") or "").strip()
        if not title:
            return None
        rec = {
            "kind": "feature", "severity": "P3", "title": title[:120],
            "summary": str(payload.get("recommendation") or "")[:200],
            "subsystem_guess": str(payload.get("entity") or ""),
            "entity": str(payload.get("entity") or "FNDR"),
            "signal": "friction", "representative": title,
            "evidence": [{"channel_id": "", "ts": "",
                          "note": f"friction-mining (route=cora_tool): {payload.get('recommendation', '')[:200]}"}],
            "reporter": HARRISON_ID,
        }
        # APPROVED directly: appears in the Monday bundle menu.
        return _capture(rec, initial_status="APPROVED")
    except Exception:  # noqa: BLE001
        log.warning("code_queue.register_from_efficiency failed (non-fatal)", exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Classifier (Haiku, fail-closed) -- only S2/S4 candidates reach a model call
# ─────────────────────────────────────────────────────────────────────────────
_CLASSIFY_PROMPT = """\
You triage a single Slack message that may be reporting a bug, requesting a
feature, or asking about Cora's (an internal AI assistant) capabilities. Decide
what KIND of signal it is and, if it is build-worthy, summarize it.

ENTITY CONTEXT: {entity}
MESSAGE: {message}

Respond with ONLY a JSON object (no markdown fences, no prose):
{{"kind": "bug"/"feature"/"config"/"knowledge"/"noise",
  "severity": "P0"/"P1"/"P2"/"P3",
  "subsystem_guess": "short guess at the code area, or empty",
  "summary": "<= 200 chars, imperative, no client names or PHI",
  "fix_sketch": "1-2 sentence sketch of the fix, or empty"}}

Rules:
- "bug" = something is broken/wrong in Cora's behavior. "feature" = a new
  capability is being requested. "config" = a small setting/threshold/routing
  change (no Code session needed). "knowledge" = the person is really asking a
  factual question Cora should learn the answer to (route to the knowledge loop,
  NOT a code build). "noise" = smalltalk, a joke, unrelated, or not actionable.
- severity: P0 live wrong-behavior with business/PHI stakes; P1 a tool is broken
  and a teammate is blocked; P2 clear-demand feature or degradation; P3 a wish.
- Never include client names, diagnoses, or other PHI in the summary.
- Do not invent facts beyond the message.
"""


def classify_candidate(message: str, entity: str) -> dict[str, Any] | None:
    """Haiku triage. FAIL-CLOSED: None on any API/parse error (proposes nothing)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.debug("code_queue: ANTHROPIC_API_KEY unset -- skipping classification")
        return None
    prompt = _CLASSIFY_PROMPT.format(entity=entity or "FNDR", message=(message or "")[:800])
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=_HAIKU_MODEL, max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        from .llm_usage import log_usage
        log_usage(resp, caller="code_queue.classify", model=_HAIKU_MODEL)
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.split("\n") if not l.startswith("```")).strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return None
        verdict = json.loads(raw[start:end + 1])
    except Exception as exc:  # noqa: BLE001 -- fail-closed
        log.warning("code_queue: classification failed: %s", exc)
        return None
    if not isinstance(verdict, dict):
        return None
    kind = str(verdict.get("kind") or "").strip().lower()
    if kind not in ("bug", "feature", "config", "knowledge", "noise"):
        return None
    severity = str(verdict.get("severity") or "P3").strip().upper()
    if severity not in VALID_SEVERITIES:
        severity = "P3"
    summary = str(verdict.get("summary") or "").strip()
    # Belt-and-braces: a PHI-tripping summary is treated as noise (dropped).
    # is_any_phi 2026-07-30 parity-raise (3-predicate union).
    if summary and phi_guard.is_any_phi(summary):
        return {"kind": "noise"}
    return {
        "kind": kind, "severity": severity,
        "subsystem_guess": str(verdict.get("subsystem_guess") or "")[:80],
        "summary": summary[:200],
        "fix_sketch": str(verdict.get("fix_sketch") or "")[:400],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Backlog renderer (generated view; drive_io fail-soft; NEVER raises)
# ─────────────────────────────────────────────────────────────────────────────
_STATUS_ORDER = ["PROPOSED", "APPROVED", "STAGED", "BLOCKED", "SNOOZED", "PARKED",
                 "SHIPPED", "DISMISSED", "SUPERSEDED"]


# ── S3'(b) (cq-deca62a00719): per-item surface fields ────────────────────────
# The 9/2 audit's traceability finding in miniature: a reader of the backlog or a
# card could not tell whether an item HAD a kickoff, whether a card had ever been
# posted for it, or how to get it staged -- so a seeded item with no card was
# "tap Stage on the item" (unactionable) and a PROPOSED item's next step was
# unstated. Three fields, rendered identically on the backlog view (KB-ingested),
# the item card and the Monday menu rows.
def item_surface_fields(rec: dict[str, Any]) -> dict[str, Any]:
    """{kickoff: bool, card: bool, how_to_stage: str} for one folded record."""
    status = str(rec.get("status") or "PROPOSED").upper()
    kickoff = bool(rec.get("prompt_path"))
    card = bool(rec.get("dm_message_ts"))
    cid = str(rec.get("id") or "?")
    # The line is read on the KB-ingested backlog by anyone (and by Cora answering
    # anyone), so it names WHO types the verb (D-051 lens A LOW #12) and only offers
    # a card tap when a card exists (lens F MED #2 -- a seeded PROPOSED row has none).
    if status in _TERMINAL_STATUSES:
        how = f"closed ({status})"
    elif kickoff:
        how = "already staged -- the kickoff is on disk"
    elif status == "PROPOSED":
        how = ("approve first ("
               + ("tap Queue on its card, or " if card else "")
               + f"Harrison replies `approve {cid}` in a Cora DM"
               + ("" if card else " -- no card, a seeded item")
               + f"), then Harrison replies `stage {cid}`")
    else:
        how = f"Harrison replies `stage {cid}` in a Cora DM"
        how += (" or taps Stage prompt on its card" if card else
                " (no card -- a seeded item; it also closes at step 7.5 when a session ships it)")
    return {"kickoff": kickoff, "card": card, "how_to_stage": how}


def format_surface_line(rec: dict[str, Any], *, on_card: bool = False) -> str:
    """`kickoff: yes/no · card: yes/no · how-to-stage: ...` -- one line, ASCII-safe
    apart from the middots. on_card=True renders the card field as "this message"
    (the line is on the card itself, whose dm_message_ts is not yet known)."""
    f = item_surface_fields(rec)
    kickoff = "yes" if f["kickoff"] else "no"
    card = "this message" if on_card else ("yes" if f["card"] else "no")
    return f"kickoff: {kickoff} · card: {card} · how-to-stage: {f['how_to_stage']}"


def render_backlog_text(items: list[dict[str, Any]] | None = None) -> str:
    items = items if items is not None else load_items()
    lines = [
        "# Cora Code-Session Backlog",
        "",
        "<!-- GENERATED from data/state/code-session-queue.jsonl -- do NOT hand-edit. "
        "Regenerated on every status transition (code_queue.render_backlog). -->",
        f"_Last generated: {_now_iso()} | {len(items)} item(s)_",
        "",
    ]
    by_status: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_status.setdefault(str(it.get("status", "PROPOSED")), []).append(it)
    for status in _STATUS_ORDER:
        group = by_status.get(status)
        if not group:
            continue
        lines.append(f"## {status} ({len(group)})")
        lines.append("")
        for it in group:
            # Egress-side LEX redaction re-check (D-051: never trust the write-side
            # redaction alone) -- covers this renderer AND the MCP code_queue_view
            # surface, which calls render_backlog_text directly.
            it = _lex_safe_view(it)
            age = _age_days(it.get("ts"))
            cnt = int(it.get("count", 1))
            cnt_s = f" x{cnt}" if cnt > 1 else ""
            entity = it.get("entity", "?")
            lines.append(
                f"- `{it.get('severity', '?')}` **{it.get('kind', '?')}** "
                f"[{entity}] {it.get('title', '(untitled)')}{cnt_s} "
                f"-- {age}d old (`{it.get('id', '?')}`)"
            )
            lines.append(f"    - {format_surface_line(it)}")
            if it.get("prompt_path"):
                lines.append(f"    - prompt: `{it['prompt_path']}`")
        lines.append("")
    return "\n".join(lines)


def render_backlog(items: list[dict[str, Any]] | None = None) -> bool:
    """Write the backlog view to the Founder-OS Drive path. Fail-soft: a G: outage
    (DriveUnavailable) or any error returns False and NEVER raises (a button ack
    must not break on a Drive blip -- Rider B / drive_io doctrine).

    2026-07-30 incident guard: refuses the write outright when the event ledger
    is redirected (test/sandbox) but the backlog target is STILL the real,
    unredirected Founder-OS default -- see _backlog_write_would_leak()."""
    try:
        if _backlog_write_would_leak():
            log.warning(
                "code_queue: refusing to write the real Founder-OS backlog -- "
                "the event ledger is redirected but the backlog target is not "
                "(test/sandbox isolation mismatch; see the 2026-07-30 incident)"
            )
            return False
        text = render_backlog_text(items)
        drive_io.write_text_atomic(backlog_path(), text)
        return True
    except Exception as exc:  # noqa: BLE001 -- never raise on a Drive blip
        log.warning("code_queue: backlog render skipped (non-fatal): %s", exc)
        return False


def _render_backlog_safe() -> None:
    try:
        render_backlog()
    except Exception:  # noqa: BLE001 -- double belt-and-braces
        log.debug("code_queue: _render_backlog_safe swallowed", exc_info=True)


def _age_days(ts: Any) -> int:
    dt = _parse_ts(ts)
    if dt is None:
        return 0
    return max(0, (_now() - dt).days)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt generator (Sonnet; fail-soft -> deterministic skeleton)
# ─────────────────────────────────────────────────────────────────────────────
def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (s or "code-session")[:48]


def _id_suffix(items: list[dict[str, Any]]) -> str:
    """A short, deterministic id-derived suffix so two DIFFERENT items whose titles
    slugify identically get DISTINCT filenames (day-one defect #4: the env-flag pair
    clobbered each other). Derived from the sorted item ids (bundle-stable)."""
    ids = sorted(str(it.get("id") or "") for it in items if it.get("id"))
    basis = ",".join(ids) or "noid"
    return hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:6]  # noqa: S324 -- filename disambig, not security


def _write_prompt_file(body: str, fname: str) -> tuple[str | None, bool]:
    """Write a generated prompt. Primary target: the Founder-OS ``_notes`` folder via
    drive_io (mount-resilient). If G: is unavailable (DriveUnavailable) or the write
    otherwise fails, fail-soft to the repo ``_notes`` folder, log a WARNING, and flag
    the write ``mis_homed`` so the caller records it on the ledger event. Returns
    ``(path, mis_homed)``; ``(None, False)`` only if BOTH targets fail."""
    fos = founder_os_notes_dir() / fname
    try:
        drive_io.write_text_atomic(fos, body)
        return str(fos), False
    except Exception as exc:  # noqa: BLE001 -- DriveUnavailable or any write error
        log.warning("code_queue: prompt mis-homed to repo _notes (Founder-OS write failed: %s)", exc)
    try:
        _NOTES_DIR.mkdir(parents=True, exist_ok=True)
        rp = _NOTES_DIR / fname
        rp.write_text(body, encoding="utf-8")
        return str(rp), True
    except Exception as exc:  # noqa: BLE001
        log.warning("code_queue: prompt file write failed entirely: %s", exc)
        return None, False


# ─────────────────────────────────────────────────────────────────────────────
# C1 (Code #12, cq-b6f2f4825ffb): evidence provenance + the EVIDENCE FLOOR
# ─────────────────────────────────────────────────────────────────────────────
# 2026-09-07: the Monday menu's Stage buttons generated FOUR kickoffs whose
# "## 0. Evidence" read `evidence= field empty -- no Slack permalink, no repro`,
# one of them for Harrison's own QUESTION ("do you have access to all the Cowork
# Cascade knowledge now as well?", cq-651e6783994f). Root causes, measured on the
# ledger: (1) every passive capture path wrote evidence with ts="" -- a channel
# pointer that can never become a permalink -- and the capability route wrote no
# channel id at all; (2) nothing stood between "APPROVED" and "generate a kickoff"
# that asked whether the item carried any evidence.
#
# The floor: an item whose evidence is EMPTY does not auto-stage. Evidence is a
# Slack PERMALINK (channel_id AND ts) OR an EXPLICIT body (signal == "explicit":
# the cora_queue_code_session tool or a seed_item caller -- a human or a session
# wrote the request deliberately, and that text IS the evidence). A passive
# capture's raw note (deflection / capability / phrase / thumbs-down / tool
# failure) is NOT evidence on its own: it is the trigger text, and the 9/7
# fixture rows all carry one. The founder's typed `stage cq-<id>` (S1') is the
# deliberate override; every other path -- approve auto-stage, the Stage button,
# seed stage_now, a script -- re-cards for evidence instead.
_SLACK_WORKSPACE_URL = "https://hjr-global.slack.com"


def slack_permalink(channel_id: str, ts: str) -> str:
    """The canonical archive permalink, or "" when either half is missing."""
    ch = str(channel_id or "").strip()
    t = str(ts or "").strip()
    if not ch or not t:
        return ""
    return f"{_SLACK_WORKSPACE_URL}/archives/{ch}/p{t.replace('.', '')}"


def evidence_permalinks(rec: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for e in (rec.get("evidence") or []):
        if not isinstance(e, dict):
            continue
        link = slack_permalink(e.get("channel_id", ""), e.get("ts", ""))
        if link and link not in out:
            out.append(link)
    return out


def _is_seed_shaped(rec: dict[str, Any]) -> bool:
    """A seed_item row, by SHAPE rather than by label: no channel, no ts, and the
    evidence note is the summary's first 200 chars -- the exact row seed_item has
    always written. Live census 2026-09-09: 223 captured rows have this shape
    under NINE different signal labels (explicit, friction, tool_error, v2b_s5_*,
    kb_eval_*), so keying the seed body on the label alone would have floored
    every non-"explicit" seed. Rows seeded after this change also carry
    ``seeded: True``; the shape rule is what covers the legacy rows."""
    summary = str(rec.get("summary") or "").strip()
    title = str(rec.get("title") or "").strip()
    has_body = bool(summary) or bool(title and title != _LEX_REDACTED_TITLE)
    if rec.get("seeded") is True:
        # The flag says a seed wrote it; the floor still needs SOMETHING to build
        # from (D-051 lens A LOW #10) -- a LEX seed is redacted at rest, so for it
        # the permalink stays the only door, like every other LEX row.
        return has_body
    if not summary:
        return False
    if str(rec.get("reporter") or "") != HARRISON_ID:
        # Legacy seeds all carry seed_item's reporter; a passive capture never
        # does -- so a future channel-less capture cannot satisfy the shape
        # (D-051 lens B LOW: value-equality on note == summary[:200] alone was
        # satisfiable by a deflection capture with channel_id "").
        return False
    for e in (rec.get("evidence") or []):
        if not isinstance(e, dict):
            continue
        if str(e.get("channel_id") or "").strip() or str(e.get("ts") or "").strip():
            continue
        note = str(e.get("note") or "").strip()
        if note and note == summary[:200].strip():
            return True
    return False


def has_evidence(rec: dict[str, Any]) -> bool:
    """The evidence FLOOR predicate. True when the item carries a Slack permalink
    (an evidence row with BOTH channel_id and ts), OR an explicit body -- the
    cora_queue_code_session tool (signal == "explicit", the request text is the
    note) or a seed_item row (see _is_seed_shaped): a human or a session wrote the
    request deliberately, and that text IS the evidence. A passive capture's raw
    trigger text is NOT evidence on its own -- every 9/7 fixture row carries one.
    A LEX item's body is redacted at rest, so for LEX the permalink is the only
    door -- a LEX build ask with no pointer re-cards like any other."""
    if evidence_permalinks(rec):
        return True
    if _is_seed_shaped(rec):
        return True
    if str(rec.get("signal") or "").strip().lower() == "explicit":
        if str(rec.get("summary") or "").strip():
            return True
        for e in (rec.get("evidence") or []):
            if not isinstance(e, dict):
                continue
            if str(e.get("note") or "").strip():
                return True
            if str(e.get("channel_id") or "").strip():
                # A human TYPED this ask (cora_queue_code_session) in a known
                # channel; redaction of its body is Cora's own act, not missing
                # provenance (D-051 lens B MED #7 -- every LEX explicit ask was
                # floored because queue_explicit wrote ts="" and the LEX scrub
                # drops the note). The kickoff names the channel pointer.
                return True
    return False


def no_evidence_message(rec: dict[str, Any]) -> str:
    """The re-card text: what is missing, and the two honest ways forward."""
    cid = str(rec.get("id") or "?")
    return (f"NOT staged -- `{cid}` carries no evidence (no Slack permalink, no seed body). "
            f"Attach the thread it came from, or reply `stage {cid}` in your Cora DM to "
            f"stage it anyway (the deliberate override).")


def _evidence_block(items: list[dict[str, Any]], *, override: bool = False) -> list[str]:
    """`## 0. Evidence` lines populated from seed provenance: every permalink, the
    seed text (representative / summary / explicit notes), and the classifier
    fields (kind, severity, signal, subsystem, reporter, count, captured date).
    An item with nothing says so -- never a fabricated pointer. ``override`` is
    True only when the founder's typed `stage` verb bypassed the floor (D-051 lens
    B HIGH #1: the sentence used to CLAIM an override for every floored item)."""
    lines: list[str] = []
    for it in items:
        cid = it.get("id", "?")
        lines.append(f"- `{it.get('severity', '?')}` **{it.get('kind', '?')}** "
                     f"[{it.get('entity', '?')}] {it.get('title', '')} (`{cid}`)")
        cls = (f"signal={it.get('signal', '?')} · subsystem={it.get('subsystem_guess') or '-'} · "
               f"reporter={it.get('reporter') or '-'} · seen x{int(it.get('count') or 1)} · "
               f"captured {str(it.get('ts') or '')[:10] or '-'}")
        lines.append(f"    - classifier: {cls}")
        if it.get("summary"):
            lines.append(f"    - summary: {it['summary']}")
        rep = str(it.get("representative") or "").strip()
        if rep and rep != str(it.get("summary") or "").strip():
            lines.append(f"    - seed text: {rep[:400]}")
        links = evidence_permalinks(it)
        for link in links:
            lines.append(f"    - permalink: {link}")
        for e in (it.get("evidence") or [])[:5]:
            if not isinstance(e, dict):
                continue
            note = str(e.get("note") or "").strip()
            if note and note != rep:
                lines.append(f"    - note: {note[:400]}")
            ch = str(e.get("channel_id") or "").strip()
            if (ch and not str(e.get("ts") or "").strip()
                    and str(it.get("signal") or "").lower() == "explicit"):
                # Only for a human-TYPED ask: the channel it was typed in is a real
                # pointer (the LEX door, lens B MED #7). A passive capture's
                # channel-only row renders no pointer at all -- that half-pointer was
                # the cq-89fdad5f0f86 artifact and stays pinned out.
                lines.append(f"    - channel pointer: <slack://channel?id={ch}> (no message ts; "
                             "find the typed ask in that channel)")
        if not has_evidence(it):
            lines.append("    - EVIDENCE: none on the item (no Slack permalink, no seed body)"
                         + (" -- staged by the founder's typed override; attach the thread "
                            "before firing." if override else
                            " -- attach the thread before firing."))
    return lines


_PROMPT_SYS = """\
You write a paste-ready Code-session kickoff prompt for "Cora" (an internal
Slack AI-assistant codebase). Match this house skeleton EXACTLY:

- A one-line byline pinning Opus-tier + the STANDING OPERATING LOOP, and the
  literal banner "AUTO-GENERATED DRAFT -- VERIFY-FIRST everything", plus a
  suggested branch name `claude/<slug>`.
- Section 0: evidence -- copy the EVIDENCE block you are given VERBATIM (every
  permalink, the seed text, the classifier fields); if it says none exists,
  say so. Never invent a pointer.
- Section 1: deliverable slices (ONE per queued item when bundled).
- Section 2: guardrails to respect (reference Cora doctrine IDs where relevant:
  D-011 no-canon-write, staged-write gate, D-051 adversarial review, PHI D-082).
- Section 3: tests.
- Section 4: live acceptance (Harrison, after merge + restart).
- Section 5: notes incl. restart implications.

Be concise. Do NOT invent facts beyond the evidence. Output MARKDOWN only.
"""


def _deterministic_prompt(items: list[dict[str, Any]], slug: str, *, override: bool = False) -> str:
    today = _now().strftime("%Y-%m-%d")
    lines = [
        f"# Cora Code prompt -- {slug} ({today})",
        "",
        "_AUTO-GENERATED DRAFT -- VERIFY-FIRST everything. Opus-tier, xhigh; follow the "
        "STANDING OPERATING LOOP in repo CLAUDE.md. Branch: "
        f"`claude/{slug}` off `main`._",
        "",
        "## 0. Evidence",
        "",
    ]
    lines += _evidence_block(items, override=override)
    lines += [
        "",
        "## 1. Deliverables",
        "",
    ]
    for i, it in enumerate(items, 1):
        lines.append(f"- Slice {i}: {it.get('title', '')} -- {it.get('fix_sketch', '') or it.get('summary', '')}")
    lines += [
        "",
        "## 2. Guardrails",
        "- D-011: not canon; no Asana/HubSpot/decisions.md writes.",
        "- Staged-write gate for any new write tool; D-051 adversarial review before restart.",
        "- PHI (D-082): LEX evidence pointers-only; is_phi_risk fail-closed.",
        "",
        "## 3. Tests",
        "- Full suite green at every slice; import smoke before every commit.",
        "",
        "## 4. Live acceptance (Harrison)",
        "- Merge + restart if bot-loaded, then smoke each slice.",
        "",
        "## 5. Notes",
        "- Restart implications: assess which files are bot-loaded vs script-side.",
    ]
    return "\n".join(lines)


def generate_kickoff_prompt(items: list[dict[str, Any]], *, slug: str | None = None,
                            meta_out: dict[str, Any] | None = None,
                            override: bool = False) -> str | None:
    """Render a kickoff prompt for one item or a bundle and write it to the Founder-OS
    ``_notes`` folder (mount-resilient; fail-soft to the repo ``_notes``). Returns the
    written path (str) or None on total write failure. Model call is fail-soft: on any
    Sonnet error a deterministic skeleton is written instead of nothing.

    ``meta_out`` (optional): populated with ``{"mis_homed": bool}`` so the caller can
    stamp the ledger ``staged`` event when the prompt fell back to the repo ``_notes``
    (G: was unavailable)."""
    if not items:
        return None
    slug = slug or _slug(str(items[0].get("title", "")))
    today = _now().strftime("%Y-%m-%d")
    body: str | None = None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        # C1: the model is handed the SAME evidence block the deterministic
        # skeleton renders (permalinks, seed text, classifier fields) and told to
        # copy it verbatim -- so a generated kickoff can never again read
        # "evidence= field empty" for an item whose provenance the ledger holds,
        # and never invents a pointer for one whose provenance it does not.
        evidence = "\n".join(
            [f"- [{it.get('severity')}] {it.get('kind')} [{it.get('entity')}] "
             f"{it.get('title')} :: fix: {it.get('fix_sketch', '')} :: id={it.get('id')}"
             for it in items]
            + ["", "EVIDENCE block (copy verbatim into Section 0):"]
            + _evidence_block(items, override=override)
        )
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model=_SONNET_MODEL, max_tokens=2000, system=_PROMPT_SYS,
                thinking={"type": "disabled"},  # D-051: Sonnet 5 thinks by default + shares max_tokens
                messages=[{"role": "user", "content":
                           f"Slug: {slug}\nItems to cover:\n{evidence}"}],
            )
            from .llm_usage import log_usage
            log_usage(resp, caller="code_queue.kickoff")
            body = resp.content[0].text.strip()
        except Exception as exc:  # noqa: BLE001 -- fail-soft to the skeleton
            log.warning("code_queue: prompt generation failed, using skeleton: %s", exc)
            body = None
    if not body:
        body = _deterministic_prompt(items, slug, override=override)

    # Id-suffix the filename so two items with the same slug can never clobber each
    # other's prompt (day-one defect #4).
    fname = f"{today}_fndr_cora-code-prompt-{slug}-{_id_suffix(items)}.md"
    path, mis_homed = _write_prompt_file(body, fname)
    if meta_out is not None:
        meta_out["mis_homed"] = mis_homed
    return path


# ─────────────────────────────────────────────────────────────────────────────
# DM cards
# ─────────────────────────────────────────────────────────────────────────────
def _default_client_factory() -> Any:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return None
    from slack_sdk import WebClient
    return WebClient(token=token)


def _mrk(text: Any) -> str:
    """Escape the three mrkdwn control characters in a TITLE rendered on a Slack
    surface (D-051 lens F LOW #5): a captured title carrying `<` or `&` would
    otherwise render as a broken link/entity. Titles only -- summaries may carry
    sanctioned `<url|label>` links."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _safe_reason(it: dict[str, Any], key: str) -> str:
    """A typed C3 reason for a Slack surface: LEX blanket, then mrkdwn escape."""
    return _mrk(_lex_safe_view(it).get(key, ""))


def build_item_card(rec: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """(fallback_text, Block Kit blocks) for one item card. A PROPOSED item gets
    the four Queue / Edit / Dismiss / Later buttons; an already-APPROVED item
    (founder fast-path / friction spillover) gets Stage-prompt / Dismiss. value =
    cq-id."""
    cq_id = str(rec.get("id", ""))
    status = str(rec.get("status", "PROPOSED"))
    ev_lines = []
    for e in (rec.get("evidence") or [])[:3]:
        # cq-89fdad5f0f86: this was an OR, so an evidence row with a channel but
        # no ts rendered "<slack://channel?id=D0B4CTD3B09> ts ``" -- a provenance
        # line with no provenance in it. Every row minted by queue_explicit
        # carries ts="" by construction, so it fired on every explicit capture.
        ch = str(e.get("channel_id") or "").strip()
        ts = str(e.get("ts") or "").strip()
        if ch and ts:
            ev_lines.append(f"<slack://channel?id={ch}> ts `{ts}`")
    ev_txt = ("\n" + "\n".join(f"> {x}" for x in ev_lines)) if ev_lines else ""
    lead = "queued" if status == "APPROVED" else "new"
    text = (
        f"*Code-session queue* -- {lead} {rec.get('kind', '?')} `{rec.get('severity', '?')}` "
        f"[{rec.get('entity', '?')}]\n"
        f"*{_mrk(rec.get('title', '(untitled)'))}*\n"
        f"{rec.get('summary', '')}"
    )
    if rec.get("fix_sketch"):
        text += f"\n_Fix sketch:_ {rec['fix_sketch']}"
    text += ev_txt
    # S3'(b): the same three surface fields the backlog view carries.
    text += f"\n_{format_surface_line(rec, on_card=True)}_"
    if status == "APPROVED":
        elements = [
            {"type": "button", "action_id": ACTION_STAGE, "style": "primary",
             "text": {"type": "plain_text", "text": "📝 Stage prompt"}, "value": cq_id},
            {"type": "button", "action_id": ACTION_DISMISS,
             "text": {"type": "plain_text", "text": "🗑️ Dismiss"}, "value": cq_id},
        ]
    else:
        elements = [
            {"type": "button", "action_id": ACTION_APPROVE, "style": "primary",
             "text": {"type": "plain_text", "text": "✅ Queue"}, "value": cq_id},
            {"type": "button", "action_id": ACTION_EDIT,
             "text": {"type": "plain_text", "text": "✏️ Edit"}, "value": cq_id},
            {"type": "button", "action_id": ACTION_DISMISS,
             "text": {"type": "plain_text", "text": "🗑️ Dismiss"}, "value": cq_id},
            {"type": "button", "action_id": ACTION_LATER,
             "text": {"type": "plain_text", "text": "⏸ Later"}, "value": cq_id},
        ]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
        {"type": "actions", "block_id": f"cq_actions_{cq_id}"[:255], "elements": elements},
    ]
    return text, blocks


def _reserve_dm_slot() -> bool:
    """Atomically claim a DM-card slot for today if under the 5/day cap. Returns
    True if reserved (caller MUST later _release_dm_slot), False if over cap."""
    with _LEDGER_LOCK:
        today = _now().date().isoformat()
        if _DM_RESERVE["date"] != today:
            _DM_RESERVE["date"] = today
            _DM_RESERVE["n"] = 0
        if _dm_sent_today() + _DM_RESERVE["n"] >= MAX_DM_PER_DAY:
            return False
        _DM_RESERVE["n"] += 1
        return True


def _release_dm_slot() -> None:
    """Release an in-flight DM reservation (the send finished -- persisted or failed)."""
    with _LEDGER_LOCK:
        _DM_RESERVE["n"] = max(0, _DM_RESERVE["n"] - 1)


def _send_new_item_card(rec: dict[str, Any], client_factory: Callable | None) -> None:
    """Send one new-item DM card to Harrison, respecting the 5/day storm cap.
    Over cap -> mark dm_held (the overflow flush delivers a '+N more' card). The cap
    is reservation-guarded so concurrent captures can't collectively breach it."""
    if not _reserve_dm_slot():
        try:
            _append_event({"event": "dm_held", "ts": _now_iso(), "id": rec["id"]})
        except Exception:  # noqa: BLE001
            pass
        log.info("code_queue: DM cap hit -- holding item %s for overflow flush", rec["id"])
        return
    try:
        client = (client_factory or _default_client_factory)()
        if client is None:
            return
        open_resp = client.conversations_open(users=[HARRISON_ID])
        dm_channel = open_resp["channel"]["id"]
        text, blocks = build_item_card(rec)
        resp = client.chat_postMessage(
            channel=dm_channel, text=text, blocks=blocks,
            unfurl_links=False, unfurl_media=False,
        )
        _append_event({
            "event": "dm_sent", "ts": _now_iso(), "id": rec["id"],
            "dm_channel_id": dm_channel, "dm_message_ts": resp.get("ts", ""),
        })
    except Exception:  # noqa: BLE001 -- card delivery is best-effort
        log.warning("code_queue: new-item card send failed (non-fatal)", exc_info=True)
    finally:
        # Release the in-flight slot: on success the persisted dm_sent now covers it;
        # on failure the slot is freed so it isn't wasted for the rest of the day.
        _release_dm_slot()


def _thread_count_update(cq_id: str, client_factory: Callable | None) -> None:
    """Post a threaded '+1' onto the ORIGINAL card (never a new DM)."""
    try:
        rec = get_item(cq_id)
        if not rec or not rec.get("dm_channel_id") or not rec.get("dm_message_ts"):
            return
        client = (client_factory or _default_client_factory)()
        if client is None:
            return
        client.chat_postMessage(
            channel=rec["dm_channel_id"], thread_ts=rec["dm_message_ts"],
            text=f"🔁 Seen again -- now {int(rec.get('count', 1))}x.",
            unfurl_links=False, unfurl_media=False,
        )
    except Exception:  # noqa: BLE001
        log.debug("code_queue: thread-count update failed (non-fatal)", exc_info=True)


def maybe_flush_overflow(*, client_factory: Callable | None = None) -> int:
    """Deliver held (over-cap) items as ONE '+N more' summary DM. Called on every
    knowledge-review run (script-side; zero new scheduled tasks). Returns count
    flushed. No-op unless live."""
    if code_queue_level() != "live":
        return 0
    try:
        held = [it for it in load_items()
                if it.get("dm_held") and not it.get("dm_flushed")]
        if not held:
            return 0
        client = (client_factory or _default_client_factory)()
        if client is None:
            return 0
        open_resp = client.conversations_open(users=[HARRISON_ID])
        dm_channel = open_resp["channel"]["id"]
        lines = [f"*Code-session queue -- {len(held)} more captured (over yesterday's cap):*"]
        for it in held[:25]:
            lines.append(f"- `{it.get('severity', '?')}` [{it.get('entity', '?')}] "
                         f"{it.get('title', '')} (`{it.get('id', '?')}`)")
        lines.append("_Review in the generated backlog; ✅ from there to queue._")
        client.chat_postMessage(channel=dm_channel, text="\n".join(lines),
                                unfurl_links=False, unfurl_media=False)
        for it in held:
            _append_event({"event": "dm_flushed", "ts": _now_iso(), "id": it["id"]})
        return len(held)
    except Exception:  # noqa: BLE001
        log.warning("code_queue: overflow flush failed (non-fatal)", exc_info=True)
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Button-action correctness (Harrison-gated; idempotent; apply-then-record)
# ─────────────────────────────────────────────────────────────────────────────
def process_queue_action(action_id: str, cq_id: str, actor_id: str, *,
                         bundle_id: str = "", branch: str = "",
                         commit: str = "") -> tuple[str, str]:
    """Apply a card button action. Returns (outcome, message). Harrison-only
    (org-wide intake, founder-only approval per the locked decision). Idempotent.
    All correctness lives here; the app.py wrapper is only Slack I/O.

    bundle_id / branch / commit (Code #12 C7, the bundle-linkage HARD GATE ruled
    2026-09-02): the provenance a `shipped` event must carry. A step-7.5 reconcile
    script passes them; a Slack tap passes none and may ship only an item whose
    row already carries a bundle_id from staging. Without one, MARK_SHIPPED is
    REFUSED -- the 9/2 audit found 1 of 115 SHIPPED rows traceable to a bundle
    and the event schema exactly {event, ts, id}."""
    if actor_id != HARRISON_ID:
        return "not_authorized", "Only Harrison can action the code-session queue."
    rec = get_item(cq_id)
    if not rec:
        return "error", "That queue item no longer exists."
    status = str(rec.get("status", "PROPOSED"))

    if action_id == ACTION_APPROVE:
        # STAGED is already past approval (a P0/P1 auto-staged on the first approve);
        # re-approving must NOT re-run the Sonnet generator / append a second prompt.
        if status in ("APPROVED", "STAGED"):
            return "noop", "Already queued."
        if status in ("DISMISSED", "SHIPPED", "SUPERSEDED"):
            return "noop", f"Item is {status} -- not re-queuing."
        _append_event({"event": "approved", "ts": _now_iso(), "id": cq_id})
        _render_backlog_safe()
        msg = "✅ Queued (APPROVED)."
        # P0/P1-class gets a full kickoff prompt immediately (Slice 4). Delegated to
        # ensure_kickoff_staged so EVERY approval path shares one implementation and
        # one loud-failure contract.
        if is_priority_severity(rec.get("severity")):
            outcome, detail = ensure_kickoff_staged(cq_id, via="approve_auto")
            if outcome == "staged":
                msg = f"✅ Queued + prompt staged: `{detail}`"
            elif outcome == "inflight":
                # A benign concurrent stage (e.g. a Monday-menu bundle holding the
                # reservation) is NOT a failure -- reporting one would send Harrison
                # chasing a retry the in-flight winner is about to make unnecessary.
                msg = "✅ Queued (APPROVED) -- a prompt is already being generated."
            elif outcome == "no_evidence":
                # C1: the approve stands; the kickoff does NOT -- the ack IS the
                # re-card, naming what is missing and both honest ways forward.
                msg = f"✅ Queued (APPROVED). {detail}"
            elif outcome == "error":
                # LOUD, never silent: the approve still stands (the ledger event is
                # already written) but Harrison is told the kickoff did NOT generate,
                # so a P1 can never look fully handled when it is not.
                msg = (f"✅ Queued (APPROVED) -- but the kickoff prompt did NOT "
                       f"generate: {detail}\nUse Stage on the item to retry.")
        return "approved", msg

    if action_id == ACTION_DISMISS:
        if status == "DISMISSED":
            return "noop", "Already dismissed."
        _append_event({"event": "dismissed", "ts": _now_iso(), "id": cq_id})
        _render_backlog_safe()
        return "dismissed", "🗑️ Dismissed -- this fingerprint won't resurface."

    if action_id == ACTION_LATER:
        # D-051 lens B MED #5: Later had NO status guard -- a stale card button
        # snoozed a SHIPPED row back into the backlog and silently replaced a PARK
        # (reason + trigger gone from every surface). Terminal and PARKED refuse.
        if status in _TERMINAL_STATUSES:
            return "noop", f"Item is {status} -- not snoozing a terminal row."
        if status == "PARKED":
            return "noop", ("Item is PARKED (reason + trigger recorded) -- use Park again to "
                            "change the trigger, or Re-queue / Dismiss w/ note to end the park.")
        snooze_until = (_now() + timedelta(days=SNOOZE_DAYS)).isoformat()
        _append_event({"event": "snoozed", "ts": _now_iso(), "id": cq_id,
                       "snooze_until": snooze_until})
        _render_backlog_safe()
        return "snoozed", f"⏸ Snoozed {SNOOZE_DAYS}d -- resurfaces in the Monday menu."

    if action_id == ACTION_STAGE:
        # Shares ensure_kickoff_staged with the approve path (Slice 4) so there is
        # ONE generator implementation and one loud-failure contract. Its reservation
        # guard is what keeps a re-tap from a second Sonnet call / second `staged`
        # event (defect #3 TOCTOU class), and its terminal guard is what keeps a
        # stale Slack button from resurrecting a SHIPPED row (lens-4 HIGH).
        outcome, detail = ensure_kickoff_staged(cq_id, via="button")
        if outcome == "staged":
            return "staged", f"📝 Prompt staged: `{detail}`"
        if outcome == "noop":
            return "noop", (f"Already staged: `{detail}`" if detail.startswith(("/", "G:", "C:", "\\"))
                            else detail)
        if outcome == "inflight":
            return "noop", "Already staging -- I'll post the prompt path when it's ready."
        if outcome == "no_evidence":
            # C1: a Stage BUTTON is not the override -- the founder's typed verb is.
            return "no_evidence", detail
        return "error", f"Prompt generation failed -- nothing staged ({detail})."

    if action_id == ACTION_MARK_SHIPPED:
        # C7 HARD GATE (ruled 2026-09-02, audit F3): no bundle/branch reference, no
        # SHIPPED. Covers the seed-then-ship path (102 of 115 historical SHIPPED
        # rows never passed STAGED and carry nothing): those ship ONLY through a
        # reconcile script that names its bundle. A row staged inside a bundle
        # (stage_bundle) or alone (ensure_kickoff_staged -> "solo-<id>") carries
        # its staging bundle_id, so a Monday-menu tap on such a row still ships.
        # No back-fill of historical rows (ruled out of scope): the gate is forward.
        if status == "SHIPPED":
            return "noop", "Already shipped."
        bid = str(bundle_id or "").strip() or str(rec.get("bundle_id") or "").strip()
        if not bid:
            if rec.get("prompt_path") or status in ("STAGED", "PARKED", "SNOOZED"):
                # D-051 lens B MED #4: 18 of the 24 stale rows on the live menu were
                # staged before bundle linkage existed -- there is no script to run
                # for them. Name the one honest door such a row has.
                return "refused", (
                    f"Not marked shipped -- `{cq_id}` predates bundle linkage (no bundle/branch "
                    f"on the row). Reply `ship {cq_id} <bundle-or-branch>` in your Cora DM to "
                    "ship it with a stated reference, or run the shipping bundle's step-7.5 "
                    "reconcile script.")
            return "refused", (
                f"Not marked shipped -- `{cq_id}` carries no bundle/branch reference. "
                "Run this bundle's step-7.5 reconcile script (it supplies bundle_id + "
                "branch); a Slack tap can only ship an item that was staged inside a "
                "bundle.")
        ev: dict[str, Any] = {"event": "shipped", "ts": _now_iso(), "id": cq_id, "bundle_id": bid}
        if str(branch or "").strip():
            ev["branch"] = str(branch).strip()
        if str(commit or "").strip():
            ev["commit"] = str(commit).strip()
        _append_event(ev)
        _render_backlog_safe()
        ref = f"bundle `{bid}`" + (f", branch `{ev['branch']}`" if ev.get("branch") else "") \
            + (f", commit `{ev['commit']}`" if ev.get("commit") else "")
        return "shipped", f"🚢 Marked shipped ({ref})."

    if action_id == ACTION_KEEP:
        # C3: Keeps are counted and CAPPED. One tap used to buy 14 silent days with
        # no reason, no trigger and no ceiling (audit F6: 16 of 23 aged STAGED rows
        # suppressed by one tap each). At the cap the row must be parked WITH a
        # trigger, shipped, or dismissed -- a stale Keep button is a no-op.
        kc = int(rec.get("keep_count") or 0)
        if kc >= KEEP_CAP:
            return "noop", (f"Keep is capped at {KEEP_CAP} for this item (kept x{kc}) -- park it "
                            "with a trigger, ship it, or dismiss it.")
        _append_event({"event": "kept", "ts": _now_iso(), "id": cq_id})
        left = KEEP_CAP - (kc + 1)
        return "kept", (f"Kept (x{kc + 1}) -- staleness clock reset"
                        + (f"; {left} Keep left before the cap." if left else
                           "; that was the last Keep -- next time park it with a trigger, "
                           "ship it, or dismiss it."))

    return "error", f"Unknown action: {action_id}"


# ─────────────────────────────────────────────────────────────────────────────
# P0/P1-at-approval enforcement (Slice 4, pipeline-integrity bundle 2026-08-05)
# ─────────────────────────────────────────────────────────────────────────────
# Design (TOM 1fff) says a P0/P1 item gets a full kickoff prompt the moment it is
# approved. VERIFY-FIRST found three ways that rule was unenforceable:
#
#   1. The rule lived ONLY inside process_queue_action(ACTION_APPROVE), so an item
#      seeded directly at status="APPROVED" never triggered it. That is exactly what
#      happened to cq-f1236540b61e (P1, #info-for-cora intake): its `captured` event
#      carries "status": "APPROVED" -- no `approved` event exists at all -- so the
#      generator never ran, a Fable session hand-wrote the kickoff on 8/3, the ledger
#      was never told, and it read as approved-and-unstaged for a week.
#   2. The severity test was a bare `in ("P0","P1")` string check while every
#      seed_item caller passes HIGH/MEDIUM/LOW (and seed_item never validated), so a
#      HIGH item -- P1 in every meaningful sense -- was invisible to the rule.
#      is_priority_severity now reads across both ladders.
#   3. A generation FAILURE was silent: generate_kickoff_prompt returning falsy left
#      the plain "Queued (APPROVED)" message, so a P1 looked fully handled when no
#      prompt existed. Same for losing the _begin_staging reservation race.
#
# Fix: one shared ensure_kickoff_staged() with a LOUD failure contract, called by
# every interactive approval path, plus a nightly aging monitor
# (priority_items_missing_kickoff) so the "dropped P1 kickoff" class can never be
# invisible again even on a path nobody anticipated.
#
# seed_item deliberately does NOT auto-generate (see its stage_now parameter): it is
# documented as "no DM, no classifier", and making a data-migration helper fire a
# Sonnet call + a Drive write as a side effect would be a surprising,
# network-dependent change to every seeding script and the MCP seed tool. Instead it
# WARNS loudly and the nightly monitor catches the row within 24h.

# A `staged` event on a TERMINAL row RESURRECTS it -- the fold is last-write-wins,
# so `staged` unconditionally sets status=STAGED. This is the cq-dad80c0011c9 trap,
# already a live incident once. ACTION_APPROVE has always guarded the terminal
# triple; ACTION_STAGE never did, and record_staged/ensure_kickoff_staged shipped
# with the same hole in the first cut of this slice (D-051 lens-4 HIGH). The
# collision was concrete: step 7.5 marks this bundle's items SHIPPED at merge, and
# the new nightly WARN's own remediation text says "tap Stage on each" -- a Slack
# button is permanent, so a later tap on a now-SHIPPED row would spend Sonnet, write
# a new G: prompt, and flip SHIPPED -> STAGED back into the backlog.
_TERMINAL_STATUSES = frozenset({"SHIPPED", "DISMISSED", "SUPERSEDED"})


def ensure_kickoff_staged(cq_id: str, *, override_evidence_floor: bool = False,
                          via: str = "") -> tuple[str, str]:
    """Generate + ledger-record a kickoff prompt for one item. The single
    implementation every approval path shares.

    ``via`` (Code #13 slice 1, kickoff section 9 ask 7): the door this staging
    came through -- 'typed_verb' | 'button' | 'approve_auto' | 'seed' | 'script'
    -- recorded on the `staged` event beside `override: true` when the floor was
    bypassed. The 9/14 forensics could not tell a Stage-button stage from a typed
    override on the ledger (the event carried only {event, ts, id, prompt_path,
    bundle_id}); D-314 says verify on the LEDGER, so the ledger must say.

    Returns (outcome, detail):
      "staged"   -> detail is the prompt path
      "noop"     -> nothing to do (detail is the existing path, or why)
      "inflight" -> a concurrent attempt holds the reservation; it will finish
      "error"    -> detail is a short human reason; the CALLER must surface it

    override_evidence_floor: the C1 evidence floor's deliberate override -- set
    ONLY by stage_by_id (the founder's typed `stage cq-<id>` verb, Code #12 S1').
    Every other caller (approve auto-stage, the Stage button, seed stage_now,
    scripts) is subject to the floor once C1 lands.

    Reservation-guarded (defect #3 TOCTOU class) so a concurrent approve/stage can
    never double-generate. The race is its OWN outcome rather than an error string
    the callers string-match (D-051 lens-4/5 MEDIUM: comparing prose across two
    functions meant rewording the message would silently turn a benign race into a
    reported generation failure, and ACTION_APPROVE never recovered it at all --
    telling Harrison the kickoff failed while a Monday-menu bundle was about to
    produce it).
    """
    rec = get_item(cq_id)
    if not rec:
        return "error", "item no longer exists"
    if str(rec.get("status", "")).upper() in _TERMINAL_STATUSES:
        return "noop", f"item is {rec['status']} -- not staging a terminal row"
    # Key on prompt_path ALONE, not on status==STAGED (D-051 lens-4 MEDIUM):
    # ACTION_LATER has no status guard, so STAGED -> Later -> Approve left a row
    # SNOOZED WITH a prompt_path, failed the conjunction, and generated a SECOND
    # prompt + a second `staged` event -- orphaning the first G: file. prompt_path
    # truthiness is the real "a prompt already exists" key, and it is what
    # priority_items_missing_kickoff already used.
    if rec.get("prompt_path"):
        return "noop", str(rec["prompt_path"])
    # C1 EVIDENCE FLOOR (cq-b6f2f4825ffb): no permalink and no explicit body ->
    # nothing to build from, so nothing is generated. The caller re-cards with
    # the two honest ways forward (no_evidence_message). Only the founder's typed
    # `stage cq-<id>` (stage_by_id, S1') passes override_evidence_floor=True.
    if not override_evidence_floor and not has_evidence(rec):
        log.info("code_queue: evidence floor held %s (signal=%s) -- not auto-staging",
                 cq_id, rec.get("signal"))
        return "no_evidence", no_evidence_message(rec)
    if not _begin_staging([cq_id]):
        return "inflight", "another staging attempt is already in flight for this item"
    try:
        fresh = get_item(cq_id) or rec
        if fresh.get("prompt_path"):
            return "noop", str(fresh["prompt_path"])
        meta: dict[str, Any] = {}
        try:
            path = generate_kickoff_prompt([fresh], meta_out=meta, override=override_evidence_floor)
        except Exception as exc:  # noqa: BLE001 -- an approve must never crash on this
            log.exception("code_queue: kickoff generation crashed for %s", cq_id)
            return "error", f"generator crashed ({type(exc).__name__})"
        if not path:
            log.error("code_queue: kickoff generation returned no path for %s "
                      "(severity=%s) -- APPROVED but UNSTAGED",
                      cq_id, rec.get("severity"))
            return "error", "prompt generation produced no file"
        # C7: the single-item path names its bundle too ("solo-<id>"), so a later
        # Mark-shipped tap on this row has a reference and the gate can pass it.
        ev = {"event": "staged", "ts": _now_iso(), "id": cq_id, "prompt_path": path,
              "bundle_id": solo_bundle_id(cq_id)}
        if via:
            ev["via"] = str(via)
        if override_evidence_floor:
            ev["override"] = True   # the founder's typed verb bypassed the C1 floor
        if meta.get("mis_homed"):
            ev["mis_homed"] = True
        _append_event(ev)
        _render_backlog_safe()
        _dm_prompt_path(path)
        return "staged", path
    finally:
        _end_staging([cq_id])


def solo_bundle_id(cq_id: str) -> str:
    """The staging bundle_id of an item staged ALONE (C7): deterministic, so a
    re-stage or a rehome never mints a second identity for the same solo prompt."""
    return f"solo-{str(cq_id or '').strip().lower()}"


# How long an APPROVED priority item may sit without a kickoff before the nightly
# health check WARNs. 24h per the Slice-4 spec: long enough that an approve late in
# the evening is not flagged before the next morning's check.
PRIORITY_KICKOFF_GRACE_HOURS = 24


def priority_items_missing_kickoff(
    grace_hours: int = PRIORITY_KICKOFF_GRACE_HOURS,
) -> list[dict[str, Any]]:
    """APPROVED P0/P1-class items older than `grace_hours` with no `staged` event.

    The structural net behind ensure_kickoff_staged: whatever path approved the item
    -- card tap, Monday menu, a seeding script, or something not yet written -- a
    dropped priority kickoff becomes visible within a day. Most-aged first.

    RAISES on a ledger read failure (D-051 lens-5 HIGH). The first cut swallowed
    everything and returned [], which the health check rendered as
    "No APPROVED P0/P1 item is missing a kickoff prompt" -- empty-because-clean and
    empty-because-blind were indistinguishable on the one surface Harrison reads. A
    monitor built because an item sat unnoticed for a week must not have a false
    all-clear as its own failure mode. The caller wraps this and WARNs.
    """
    out: list[dict[str, Any]] = []
    cutoff = _now() - timedelta(hours=max(0, int(grace_hours)))
    for rec in load_items():
        if rec.get("status") != "APPROVED":
            continue
        if not is_priority_severity(rec.get("severity")):
            continue
        # prompt_path truthiness is not proof the ARTIFACT exists (lens-4 MEDIUM):
        # the originating incident was a MISSING prompt, and _write_prompt_file
        # fail-softs to the repo _notes dir while apply_prompt_rehome moves paths
        # around. Fail OPEN on a stat error -- an unreadable mount must not
        # manufacture offenders.
        pp = str(rec.get("prompt_path") or "")
        if pp:
            try:
                if Path(pp).exists():
                    continue
            except OSError:
                continue
        # Age from the APPROVAL, never from `last_touch` (lens-4/5 MEDIUM). The first
        # cut preferred last_touch, which the `kept` event writes and ACTION_KEEP does
        # not status-gate -- so tapping Keep on a stale card bought another 24h of
        # invisibility (re-tappable weekly, hiding a dropped P1 indefinitely), while a
        # last_touch OLDER than the approval flagged a fresh approve instantly with a
        # wildly inflated age. "APPROVED for >Nh with no kickoff" only ever meant the
        # approval clock. Seed-at-APPROVED rows have no `approved` event, so `ts`
        # (always set by seed_item) is the real fallback.
        stamp = _parse_ts(rec.get("approved_at")) or _parse_ts(rec.get("ts"))
        if stamp is not None and stamp > cutoff:
            continue
        out.append({
            "id": rec.get("id", ""),
            "severity": rec.get("severity", ""),
            "entity": rec.get("entity", ""),
            "prompt_path_missing": bool(pp),
            "age_hours": (round((_now() - stamp).total_seconds() / 3600.0, 1)
                          if stamp is not None else None),
        })
    # Unparseable-timestamp rows are the MOST broken, so they sort FIRST rather than
    # being the first hidden by the caller's top-N truncation (lens-5 LOW).
    out.sort(key=lambda r: (r["age_hours"] is not None,
                            -(r.get("age_hours") or 0.0)))
    return out


def _dm_prompt_path(path: str) -> None:
    """DM Harrison the staged prompt path (best-effort)."""
    try:
        if code_queue_level() != "live":
            return
        client = _default_client_factory()
        if client is None:
            return
        open_resp = client.conversations_open(users=[HARRISON_ID])
        client.chat_postMessage(
            channel=open_resp["channel"]["id"],
            text=f"📝 Code-session prompt staged (AUTO-GENERATED DRAFT -- verify before pasting):\n`{path}`",
            unfurl_links=False, unfurl_media=False,
        )
    except Exception:  # noqa: BLE001
        log.debug("code_queue: prompt-path DM failed (non-fatal)", exc_info=True)


# ── Edit modal ────────────────────────────────────────────────────────────────
def edit_modal_view(cq_id: str, dm_channel: str, dm_ts: str) -> dict[str, Any]:
    rec = get_item(cq_id) or {}
    meta = json.dumps({"cq_id": cq_id, "dm_channel": dm_channel, "dm_ts": dm_ts})
    return {
        "type": "modal",
        "callback_id": VIEW_EDIT_SUBMIT,
        "private_metadata": meta,
        "title": {"type": "plain_text", "text": "Edit queue item"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input", "block_id": "cq_title",
             "label": {"type": "plain_text", "text": "Title"},
             "element": {"type": "plain_text_input", "action_id": "v",
                         "initial_value": str(rec.get("title", ""))[:150]}},
            {"type": "input", "block_id": "cq_summary",
             "label": {"type": "plain_text", "text": "Summary"},
             "element": {"type": "plain_text_input", "action_id": "v", "multiline": True,
                         "initial_value": str(rec.get("summary", ""))[:1000]}},
        ],
    }


def apply_edit(cq_id: str, actor_id: str, title: str, summary: str) -> tuple[str, str]:
    if actor_id != HARRISON_ID:
        return "not_authorized", "Only Harrison can edit queue items."
    rec = get_item(cq_id)
    if not rec:
        return "error", "That queue item no longer exists."
    title = (title or "").strip()[:120]
    summary = (summary or "").strip()[:200]
    # PHI belt-and-braces on the edited text (fail-closed). REQUEST-shaped --
    # Harrison typing into the edit modal -- so it takes the precision-tuned
    # union (C12), same as seed_item's title/summary gate. The at-rest screens
    # that decide what gets WRITTEN into the KB-ingested backlog stay strict.
    try:
        if phi_guard.is_any_phi_request(f"{title} {summary}"):
            return "error", "Edit rejected -- text tripped the PHI guard."
    except Exception:  # noqa: BLE001
        return "error", "Edit rejected -- PHI check failed (fail-closed)."
    _append_event({"event": "edited", "ts": _now_iso(), "id": cq_id,
                   "title": title, "summary": summary})
    _render_backlog_safe()
    return "edited", "✏️ Updated."


def set_severity(cq_id: str, actor_id: str, severity: str) -> tuple[str, str]:
    """Re-rate an existing item's severity via the append-only ledger (Slice 0).

    Harrison-only, like every other mutation. Accepts either live vocabulary
    (VALID_SEVERITY_INPUTS) and stores the value AS GIVEN -- canonicalizing live
    rows in place would silently rewrite the priorities rendered in the backlog and
    the Monday menu. `is_priority_severity` is what reads across both ladders.
    """
    if actor_id != HARRISON_ID:
        return "not_authorized", "Only Harrison can action the code-session queue."
    rec = get_item(cq_id)
    if not rec:
        return "error", "That queue item no longer exists."
    sev = str(severity or "").strip().upper()
    if sev not in VALID_SEVERITY_INPUTS:
        return "error", (f"Unknown severity {severity!r} -- "
                         f"expected one of: {', '.join(VALID_SEVERITY_INPUTS)}.")
    if str(rec.get("severity", "")).strip().upper() == sev:
        return "noop", f"Already {sev}."
    _append_event({"event": "edited", "ts": _now_iso(), "id": cq_id, "severity": sev})
    _render_backlog_safe()
    return "edited", f"Severity set to {sev}."


def record_staged(cq_id: str, prompt_path: str, actor_id: str) -> tuple[str, str]:
    """Record a `staged` event for a kickoff prompt that was authored OUTSIDE the
    generator (a hand-written or another-session-written prompt file).

    Slice 0 verify-first: cq-f1236540b61e went APPROVED ~7/30 and a Fable session
    hand-wrote its kickoff file on 8/3, but nothing ever told the ledger -- so the
    item read as approved-and-unstaged for a week and the Monday menu kept
    re-offering it. Without this API the only way to close that loop was a ledger
    hand-edit. Idempotent; Harrison-only.
    """
    if actor_id != HARRISON_ID:
        return "not_authorized", "Only Harrison can action the code-session queue."
    rec = get_item(cq_id)
    if not rec:
        return "error", "That queue item no longer exists."
    path = str(prompt_path or "").strip()
    if not path:
        return "error", "A staged event needs a prompt path."
    # Terminal guard (D-051 lens-4 HIGH): a `staged` event on a SHIPPED/DISMISSED/
    # SUPERSEDED row RESURRECTS it via the last-write-wins fold -- the
    # cq-dad80c0011c9 trap. See _TERMINAL_STATUSES.
    if str(rec.get("status", "")).upper() in _TERMINAL_STATUSES:
        return "noop", f"Item is {rec['status']} -- not staging a terminal row."
    if rec.get("prompt_path"):
        return "noop", f"Already staged: `{rec['prompt_path']}`"
    _append_event({"event": "staged", "ts": _now_iso(), "id": cq_id,
                   "prompt_path": path, "authored": "external",
                   "bundle_id": solo_bundle_id(cq_id)})  # C7: single-item path names its bundle
    _render_backlog_safe()
    return "staged", f"📝 Prompt staged: `{path}`"


# ── C3 (Code #12): park-with-trigger + dismiss-with-evidence ──────────────────
_PARK_REASON_MAX = 200
_PARK_EVENT_MAX = 120
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_park(reason: str, until: str = "", trigger_event: str = "", *,
                  today=None) -> dict[str, str]:
    """{modal block_id: error} for a park (empty = valid). PURE -- shared by
    park_item and the app.py submit handler, so Slack can render the errors IN the
    modal (response_action="errors") instead of closing it on a silent refusal
    (D-051 lens F MED #3). Rules: a reason; a real YYYY-MM-DD date at most
    PARK_MAX_DAYS out (lens B HIGH #2: a 2099 park was accepted); a date or an event."""
    from datetime import date as _date
    errors: dict[str, str] = {}
    if not " ".join(str(reason or "").split()):
        errors["cq_park_reason"] = "A park needs a reason -- say why it waits."
    until_n = str(until or "").strip()
    if until_n and not _DATE_RE.match(until_n):
        errors["cq_park_until"] = "The resume date must be YYYY-MM-DD."
    elif until_n:
        try:
            d = _date.fromisoformat(until_n)
        except ValueError:
            errors["cq_park_until"] = "The resume date must be a real YYYY-MM-DD date."
        else:
            today = today or _now().date()
            if (d - today).days > PARK_MAX_DAYS:
                errors["cq_park_until"] = (
                    f"A park can wait at most {PARK_MAX_DAYS} days -- pick a nearer date, or "
                    f"park on an event (it gets a {PARK_MAX_DAYS}-day review horizon).")
    ev_n = " ".join(str(trigger_event or "").split())
    if not until_n and not ev_n:
        errors["cq_park_event"] = ("A park needs a trigger -- a resume date or the event that "
                                  "resumes it.")
    return errors


def validate_dismiss_note(note: str) -> dict[str, str]:
    """{modal block_id: error} for a dismiss-with-evidence (empty = valid). Pure;
    the PHI screen is included so the modal can say why (fail-closed on error)."""
    n = " ".join(str(note or "").split())
    if not n:
        return {"cq_dismiss_note": "A dismiss-with-evidence needs the evidence -- say why."}
    try:
        if phi_guard.is_any_phi_request(n):
            return {"cq_dismiss_note": "Rejected -- the text tripped the PHI guard."}
    except Exception:  # noqa: BLE001 -- fail closed
        return {"cq_dismiss_note": "Rejected -- PHI check failed (fail-closed)."}
    return {}


def park_item(cq_id: str, actor_id: str, reason: str, *, until: str = "",
              trigger_event: str = "") -> tuple[str, str]:
    """Park an open item WITH a trigger: a reason plus a resume DATE (YYYY-MM-DD,
    resurfaces on the Monday menu once it passes) and/or a resume EVENT (free
    text; listed count-only on every menu until a human acts). Replaces the
    reason-less, trigger-less Keep as the way to defer (audit F6). Harrison-only;
    terminal rows refused; the reason is PHI-screened like every other typed
    field that egresses to the backlog view."""
    if actor_id != HARRISON_ID:
        return "not_authorized", "Only Harrison can action the code-session queue."
    rec = get_item(cq_id)
    if not rec:
        return "error", "That queue item no longer exists."
    if str(rec.get("status", "")).upper() in _TERMINAL_STATUSES:
        return "noop", f"Item is {rec['status']} -- not parking a terminal row."
    errors = validate_park(reason, until, trigger_event)
    if errors:
        return "error", " ".join(errors.values())
    reason = " ".join(str(reason or "").split())[:_PARK_REASON_MAX]
    until = str(until or "").strip()
    trigger_event = " ".join(str(trigger_event or "").split())[:_PARK_EVENT_MAX]
    try:
        if phi_guard.is_any_phi_request(f"{reason} {trigger_event}"):
            return "error", "Park rejected -- the text tripped the PHI guard."
    except Exception:  # noqa: BLE001 -- fail closed
        return "error", "Park rejected -- PHI check failed (fail-closed)."
    horizon = False
    if not until:
        # An event-only park never became due (D-051 lens B HIGH #2) -- it now
        # carries a REVIEW HORIZON: it resurfaces at PARK_MAX_DAYS even if the
        # event has not fired, so no park is indefinite.
        until = (_now().date() + timedelta(days=PARK_MAX_DAYS)).isoformat()
        horizon = True
    _append_event({"event": "parked", "ts": _now_iso(), "id": cq_id, "reason": reason,
                   "until": until, "trigger_event": trigger_event, "review_horizon": horizon})
    _render_backlog_safe()
    trig = " / ".join(x for x in (
        (f"review by {until}" if horizon else f"until {until}"),
        f"on: {trigger_event}" if trigger_event else "") if x)
    return "parked", f"⏸ Parked ({trig}) -- {reason}" + (
        f" (a re-ask, or the {PARK_MAX_DAYS}-day review horizon, resurfaces it)" if horizon
        else " (a re-ask, or the date, resurfaces it)")


def dismiss_with_evidence(cq_id: str, actor_id: str, note: str, *,
                          channel_id: str = "", ts: str = "") -> tuple[str, str]:
    """Dismiss WITH a stated reason (the evidence-attached disposition class the 9/8
    lock named for the Appendix-A rows). The reason rides the `dismissed` event and
    folds to dismiss_reason. Harrison-only; PHI-screened; idempotent."""
    if actor_id != HARRISON_ID:
        return "not_authorized", "Only Harrison can action the code-session queue."
    rec = get_item(cq_id)
    if not rec:
        return "error", "That queue item no longer exists."
    if str(rec.get("status", "")).upper() == "DISMISSED":
        return "noop", "Already dismissed."
    errors = validate_dismiss_note(note)
    if errors:
        return "error", " ".join(errors.values())
    note = " ".join(str(note or "").split())[:_EVIDENCE_NOTE_MAX_CHARS]
    ev: dict[str, Any] = {"event": "dismissed", "ts": _now_iso(), "id": cq_id, "reason": note}
    if channel_id or ts:
        ev["evidence"] = {"channel_id": str(channel_id or ""), "ts": str(ts or "")}
    _append_event(ev)
    _render_backlog_safe()
    return "dismissed", f"🗑️ Dismissed with evidence -- {note}"


def _modal_meta(cq_id: str, dm_channel: str, dm_ts: str) -> str:
    return json.dumps({"cq_id": cq_id, "dm_channel": dm_channel, "dm_ts": dm_ts})


def park_modal_view(cq_id: str, dm_channel: str, dm_ts: str) -> dict[str, Any]:
    rec = get_item(cq_id) or {}
    return {
        "type": "modal",
        "callback_id": VIEW_PARK_SUBMIT,
        "private_metadata": _modal_meta(cq_id, dm_channel, dm_ts),
        "title": {"type": "plain_text", "text": "Park with a trigger"},
        "submit": {"type": "plain_text", "text": "Park"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"*{str(rec.get('title', ''))[:150]}* (`{cq_id}`)"}},
            {"type": "input", "block_id": "cq_park_reason",
             "label": {"type": "plain_text", "text": "Why does it wait?"},
             "element": {"type": "plain_text_input", "action_id": "v", "multiline": True,
                         "max_length": _PARK_REASON_MAX}},
            {"type": "input", "block_id": "cq_park_until", "optional": True,
             "label": {"type": "plain_text",
                       "text": f"Resume date (resurfaces on that Monday's menu; at most {PARK_MAX_DAYS} days out)"},
             "element": {"type": "datepicker", "action_id": "v"}},
            {"type": "input", "block_id": "cq_park_event", "optional": True,
             "label": {"type": "plain_text", "text": "...or the event that resumes it"},
             "element": {"type": "plain_text_input", "action_id": "v",
                         "max_length": _PARK_EVENT_MAX}},
        ],
    }


def dismiss_modal_view(cq_id: str, dm_channel: str, dm_ts: str) -> dict[str, Any]:
    rec = get_item(cq_id) or {}
    return {
        "type": "modal",
        "callback_id": VIEW_DISMISS_SUBMIT,
        "private_metadata": _modal_meta(cq_id, dm_channel, dm_ts),
        "title": {"type": "plain_text", "text": "Dismiss with evidence"},
        "submit": {"type": "plain_text", "text": "Dismiss"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"*{str(rec.get('title', ''))[:150]}* (`{cq_id}`)"}},
            {"type": "input", "block_id": "cq_dismiss_note",
             "label": {"type": "plain_text", "text": "Why is this closed? (the evidence)"},
             "element": {"type": "plain_text_input", "action_id": "v", "multiline": True,
                         "max_length": _EVIDENCE_NOTE_MAX_CHARS}},
        ],
    }


def append_evidence(cq_id: str, actor_id: str, note: str,
                    *, channel_id: str = "", ts: str = "") -> tuple[str, str]:
    """Attach one dated real-world example to an EXISTING item (Slice 0).

    Deliberately NOT a `recurrence` event: recurrence means "this signal fired
    again" and bumps `count`, which the Monday menu ranks on. Adding evidence to an
    already-filed item must not inflate that counter.

    PHI/LEX safety mirrors the capture + seed paths: the note is screened
    fail-closed with the 3-predicate union, and a LEX item's note is reduced to a
    pointer by _scrub_evidence (the note is persisted and egresses via the kickoff
    prompt + the KB-ingested backlog).
    """
    if actor_id != HARRISON_ID:
        return "not_authorized", "Only Harrison can action the code-session queue."
    rec = get_item(cq_id)
    if not rec:
        return "error", "That queue item no longer exists."
    note = str(note or "").strip()
    if not note:
        return "error", "Nothing to attach -- the evidence note is empty."
    # FALSE-SUCCESS FIXES (D-051 lens-4 MEDIUM). The first cut returned "Evidence
    # attached." for two writes that did not land as given:
    #   * _scrub_evidence truncates the note to 200 chars with no warning -- and this
    #     already cost live data: both notes the reconcile script attached to
    #     cq-5c6ff15610bd are stored cut mid-word at exactly 200 chars, losing the
    #     substance that followed. Truncation is now REPORTED.
    #   * the fold caps evidence at 10 entries and silently drops the 11th. Refuse.
    # Doctrine precedent: the Shopify inventory hotfix ("write-dead + false-success").
    truncated = len(note) > _EVIDENCE_NOTE_MAX_CHARS
    if len(rec.get("evidence") or []) >= _EVIDENCE_MAX_ENTRIES:
        return "error", (f"This item already carries {_EVIDENCE_MAX_ENTRIES} evidence "
                         f"entries -- the fold drops any more. Edit the summary instead.")
    # Dedup (lens-4 MEDIUM): the reconcile script's docstring promises idempotency,
    # but re-running it re-appended the same notes until the cap silently ate them.
    existing_notes = {str(e.get("note", "")).strip()
                      for e in (rec.get("evidence") or []) if e.get("note")}
    if note[:_EVIDENCE_NOTE_MAX_CHARS] in existing_notes:
        return "noop", "That evidence note is already attached."
    try:
        if phi_guard.is_any_phi(note):
            return "error", "Evidence rejected -- text tripped the PHI guard."
    except Exception:  # noqa: BLE001 -- fail closed
        return "error", "Evidence rejected -- PHI check failed (fail-closed)."
    is_lex = str(rec.get("entity") or "").strip().upper().startswith("LEX")
    scrubbed = _scrub_evidence(
        [{"channel_id": channel_id, "ts": ts, "note": note}], is_lex=is_lex)
    entry = scrubbed[0] if scrubbed else {}
    # A LEX item's note is reduced to a pointer; with no channel_id/ts to point AT,
    # nothing informative survives -- refuse rather than persist an empty stub.
    if not (entry.get("note") or entry.get("channel_id") or entry.get("ts")):
        return "error", ("Evidence rejected -- a LEX item's note is reduced to a "
                        "pointer, and no channel/ts pointer was supplied.")
    _append_event({"event": "evidence", "ts": _now_iso(), "id": cq_id,
                   "evidence": entry})
    _render_backlog_safe()
    if truncated:
        return "evidence", (f"Evidence attached, TRUNCATED to "
                            f"{_EVIDENCE_NOTE_MAX_CHARS} chars -- shorten the note if "
                            f"the tail mattered.")
    return "evidence", "Evidence attached."


# ─────────────────────────────────────────────────────────────────────────────
# Explicit tool backend (cora_queue_code_session) -- preview + confirm
# ─────────────────────────────────────────────────────────────────────────────
def _explicit_count_today(user: str) -> int:
    today = _now().date()
    n = 0
    for ev in _read_jsonl(_EVENT_LEDGER):
        if ev.get("event") != "captured" or ev.get("signal") != "explicit":
            continue
        if str(ev.get("reporter")) != user:
            continue
        ts = _parse_ts(ev.get("ts"))
        if ts and ts.date() == today:
            n += 1
    return n


# Live ids are 12 lowercase hex characters. Anchored on word boundaries so a
# longer hex blob cannot be mistaken for one.
_CQ_ID_RE = re.compile(r"\bcq-[0-9a-f]{12}\b", re.IGNORECASE)

# D-051: the first cut short-circuited on ANY resolvable id anywhere in the text,
# which would DISCARD a genuine build request that merely cites one -- "Cora
# should retry uploads the way cq-abc123def456 describes" would have been thrown
# away and acked as queued. A stage request has a VERB. Require one within a
# short window of the id, in either order, so a citation stays a capture.
_STAGE_VERB_RE = re.compile(
    r"\b(?:stage|staging|kickoff|kick[- ]?off|prompt|write\s+the\s+prompt|"
    r"generate\s+the\s+prompt|retrieve|pull\s+up)\b", re.IGNORECASE)
_STAGE_VERB_WINDOW = 60


def find_cq_id(text: str) -> str:
    """The first cq id named in *text*, lowercased, or "" -- resolved against the
    ledger so a typo'd or invented id never counts as a reference."""
    for m in _CQ_ID_RE.finditer(str(text or "")):
        cid = m.group(0).lower()
        if get_item(cid):
            return cid
    return ""


def find_stage_request(text: str) -> str:
    """The cq id this text asks to STAGE, or "".

    Requires a staging verb within _STAGE_VERB_WINDOW characters of the id, so a
    build request that merely CITES an existing item is still captured as new
    work rather than silently discarded.
    """
    body = str(text or "")
    for m in _CQ_ID_RE.finditer(body):
        cid = m.group(0).lower()
        if not get_item(cid):
            continue
        lo = max(0, m.start() - _STAGE_VERB_WINDOW)
        hi = min(len(body), m.end() + _STAGE_VERB_WINDOW)
        if _STAGE_VERB_RE.search(body[lo:hi]):
            return cid
    return ""


def stage_by_id(cq_id: str, actor_id: str) -> tuple[str, str]:
    """Generate the kickoff for an EXISTING item. Harrison-only.

    A thin wrapper over ensure_kickoff_staged, deliberately -- reusing it
    inherits the staging reservation, the terminal-status guard, the prompt_path
    idempotence and the loud-failure contract instead of re-deriving four
    behaviours that already exist and are tested.

    Why this exists: on 2026-08-24 15:38 a typed "stage a code-session prompt for
    cq-f52c6b691127" went to the cora_queue_code_session TOOL, whose backend
    queue_explicit MINTS UNCONDITIONALLY and never looks at the request text for
    an id. It created a junk P2 meta-item titled "Retrieve pending code-queue
    item cq-f52c6b691127 for staging" -- a queue entry whose entire content is a
    request to look at another queue entry -- and auto-staged a kickoff for it.
    Fingerprint dedup could not help: the key is (signal, representative) =
    ("explicit", the raw request text), so two differently-worded stage requests
    naming the SAME id produce two different junk items.
    """
    cid = str(cq_id or "").strip().lower()
    if actor_id != HARRISON_ID:
        return "not_authorized", "Only Harrison can stage a queue item."
    if not cid or not get_item(cid):
        return ("not_found",
                f"I can't find `{cid or '(no id)'}` in the queue. If you have the "
                f"card, tap its \u201cStage prompt\u201d button -- that is the same "
                f"path and it always resolves the right item.")
    return ensure_kickoff_staged(cid, override_evidence_floor=True, via="typed_verb")


# ─────────────────────────────────────────────────────────────────────────────
# Founder-DM queue verbs (Code #12 S1', cq-554184feb53b) -- pre-model, deterministic
# ─────────────────────────────────────────────────────────────────────────────
# 2026-09-03 06:49 AZ: Harrison's DM "@Cora Stage cq-621dfad586aa" was answered
# with the morning-briefing digest -- zero tool_use. The typed verb was honored
# ONLY inside queue_explicit (the cora_queue_code_session backend), i.e. only when
# the MODEL elected to call that tool; a queue verb in a founder DM had no
# deterministic route the way the F-23 confirm interceptor gives one to "yes" /
# "cancel" (the 1ff pattern). Four minutes later the model narrated "Done.
# Staging all three" with two invented ids (S2' in slack_egress guards that half).
#
# EXACT MATCH ONLY. A sentence that merely cites an id ("retry uploads the way
# cq-abc123def456 describes") is a build request, not a verb, and still goes to
# the model. The verb is case-insensitive ("Stage" is what was typed); the id is
# lowercased. Trailing punctuation is NOT tolerated -- the locked spec's regex is
# reproduced verbatim so the match surface stays exactly what was ruled.
_QUEUE_VERB_RE = re.compile(
    r"^\s*(stage|approve|dismiss)\s+(cq-[0-9a-f]{12})\s*$", re.IGNORECASE)


# `ship <id> <bundle-or-branch>` (D-051 lens B MED #4): the one honest door for a
# row staged before bundle linkage existed -- the C7 gate refuses a bare tap on it
# and no reconcile script names it. Exact match; the reference is passed through as
# the shipped event's bundle_id (and branch when it is path-shaped).
_SHIP_VERB_RE = re.compile(
    r"^\s*ship\s+(cq-[0-9a-f]{12})\s+([A-Za-z0-9][A-Za-z0-9._/+-]{2,79})\s*$", re.IGNORECASE)


def match_queue_verb(text: str) -> tuple[str, ...] | None:
    """(verb, cq_id) when *text* is EXACTLY a queue verb + id -- or ("ship", cq_id,
    reference) for the ship verb -- else None."""
    m = _QUEUE_VERB_RE.match(str(text or ""))
    if m:
        return m.group(1).lower(), m.group(2).lower()
    m = _SHIP_VERB_RE.match(str(text or ""))
    if m:
        return "ship", m.group(1).lower(), m.group(2)
    return None


# ── RIDER 2 (Code #13 section 10, cq-70d7b203f7ad): decorated / malformed verbs ──
# 2026-09-15 21:17:48 AZ: Harrison's DM `• <@U0B44MDGC5R> \`stage cq-a24f9d2210fc\``
# -- a bullet, a bot mention and backticks, i.e. the shape of a capture's paste-
# ready block -- did NOT match the grammar above (the DM mention strip only removes
# a LEADING token), fell through to dm_qa -> haiku, and the model replied "staged"
# with zero tool_use. The 9/10 09:35:53 / 09:36:42 phantoms were the same class
# (`ship cq-<STAGED id you are happy to close>` typed literally; `ship cq-cq-...`).
# Three incidents, one mechanism: a decorated or malformed queue verb reaches the
# model, which narrates a write.
#
# Two rails, both deterministic, both BEFORE the grammar's callers:
#   1. normalize_verb_text -- strips ONE leading list marker, Cora's OWN mention
#      token (never another user's: an unknown mention is ambiguity, and ambiguity
#      refuses), backticks / fences, HTML entities and surrounding whitespace, and
#      lower-cases the verb token only. Anything else (extra words, two ids, a
#      second line) still fails the grammar. The grammar itself is UNCHANGED
#      (1hhhhhhhhh: typed verb = deterministic, the ONLY evidence-floor override).
#   2. looks_like_queue_verb_attempt -- a verb token followed by anything cq-shaped
#      or a `<placeholder>` that FAILED the grammar is a PARSE FAILURE to refuse
#      from code (PARSE_REFUSED_REPLY), never a question for the model (D-316).
#      The refusal carries ZERO write-claim lexicon (test-pinned against S2').
_VERB_LIST_MARKER_RE = re.compile(r"^\s*(?:[•◦‣⁃·\-\*\+]|\d{1,2}[.)])\s+")
_VERB_CODE_TICKS_RE = re.compile(r"`{1,3}")
_QUEUE_VERBS: tuple[str, ...] = ("stage", "approve", "dismiss", "ship")
# `<@U..>` / `<#C..>` / `<!here>` / `<http...>` are Slack tokens, not placeholders --
# a verb followed by one of those is NOT a queue-verb attempt (a DM "dismiss <@U1>'s
# concern" is ordinary prose and stays with the model).
# ONE character class between the verb and the reference (D-051 Code #13 review,
# AD-3): the earlier `\s*[`'"]*\s*` -- three adjacent optional atoms over overlapping
# input -- backtracked QUADRATICALLY on a verb followed by whitespace ('stage' + 40k
# spaces + 'x' spun ~8.8 s on the bolt worker, ahead of the rate limiter, for ANY DM
# sender). The GRAMMAR regex behind match_queue_verb is untouched (D-281).
#
# START-ANCHORED (R14-2, ruled 2026-09-19 R-1; D-173 stolen turn). The first cut
# was an unanchored .search, so "did Harrison approve cq-1234567890ab yet?" --
# a QUESTION that mentions a verb before an id -- was refused from code instead
# of answered, and the branch ran for EVERY DM sender (the caller now gates on
# the founder). The anchor still tolerates the decorations an ambiguous paste
# carries, because ambiguity must keep REFUSING, never reach the model (the 9/15
# phantom class): a short run of non-word, non-`<` characters (a second list
# marker "- -", a blockquote "> ", a quote or paren), then at most ONE Slack
# mention token (a foreign user's, or Cora's own when auth.test could not resolve
# it -- normalize_verb_text leaves both in place), then an optional leading
# "Cora," vocative. Every atom is bounded or a single class that cannot overlap
# its neighbour (`<` is excluded from the prefix class, letters from both
# prefixes), and .match anchors the scan at position 0 (D-171: re-timed).
_VERB_ATTEMPT_RE = re.compile(
    r"\A[^\w<\n]{0,8}"
    r"(?:<@[A-Za-z0-9_]{2,24}(?:\|[^>\n]{0,40})?>[^\w<\n]{0,8})?"
    r"(?:cora[,:]?[ \t]{1,4})?"
    r"(stage|approve|dismiss|ship)\b[\s`'\"]*(?:cq-\S*|<(?![@#!]|https?:)[^>\n]{0,80}>)",
    re.IGNORECASE)
PARSE_REFUSED_REPLY = ("I see a queue verb but couldn't parse it. Nothing was changed. "
                       "Send exactly `stage cq-<12 hex>` on its own line.")


def normalize_verb_text(text: str, *, bot_user_id: str | None = None) -> str:
    """The verb-matching view of a DM: decorations a paste carries are removed; the
    words are not. Only Cora's OWN mention (``bot_user_id``) is stripped -- with no
    id known, a mention stays and the grammar fails (refusal, never execution)."""
    t = html.unescape(str(text or ""))
    t = _VERB_LIST_MARKER_RE.sub("", t, count=1)
    if isinstance(bot_user_id, str) and bot_user_id:
        t = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>\n]{{0,40}})?>", " ", t)
    t = _VERB_CODE_TICKS_RE.sub("", t)
    t = t.strip()
    m = re.match(r"(\S+)(.*)", t, re.DOTALL)
    if m and m.group(1).lower() in _QUEUE_VERBS:
        t = m.group(1).lower() + m.group(2)
    return t


def looks_like_queue_verb_attempt(text: str) -> str | None:
    """The verb when *text* STARTS with a queue verb (after bounded decoration, one
    mention token, an optional "Cora," vocative) followed by something cq-shaped or
    a `<placeholder>` -- a paste that failed the grammar; else None. A verb word in
    mid-sentence ("did Harrison approve cq-... yet?") is a question, not an attempt
    (R14-2). Callers run this ONLY after match_queue_verb returned None, and ONLY
    for the founder (app.handle_message_event gates on HARRISON_ID)."""
    m = _VERB_ATTEMPT_RE.match(str(text or ""))
    return m.group(1).lower() if m else None


def apply_queue_verb(verb: str, cq_id: str, actor_id: str, arg: str = "") -> tuple[str, str]:
    """Run one typed queue verb DIRECTLY against the queue. The reply is the
    queue's OWN outcome string, verbatim; the model is never consulted.

    Every guard is INHERITED, not re-derived: `stage` -> stage_by_id (Harrison-
    only, the not-found string, the _TERMINAL_STATUSES guard, prompt_path
    idempotence, the staging reservation -- and, deliberately, the C1
    evidence-floor OVERRIDE: a typed `stage` is the founder saying "stage it
    anyway"); `approve` / `dismiss` -> process_queue_action (Harrison-only,
    idempotent, terminal-row guards). A non-founder gets the tool's own
    not_authorized text; an unknown id gets its not-found text -- never a model
    reply in either case.
    """
    v = str(verb or "").strip().lower()
    cid = str(cq_id or "").strip().lower()
    if v == "stage":
        return stage_by_id(cid, actor_id)
    if v == "approve":
        return process_queue_action(ACTION_APPROVE, cid, actor_id)
    if v == "dismiss":
        return process_queue_action(ACTION_DISMISS, cid, actor_id)
    if v == "ship":
        ref = str(arg or "").strip()
        if not ref:
            return "error", "`ship <id> <bundle-or-branch>` needs the reference."
        return process_queue_action(ACTION_MARK_SHIPPED, cid, actor_id, bundle_id=ref,
                                    branch=ref if "/" in ref else "")
    return "error", f"Unknown queue verb: {verb!r}"


def queue_explicit(user: str, entity: str, channel_id: str, request: str,
                   is_founder: bool, *, message_ts: str = "") -> tuple[str | None, str]:
    """Backend for the explicit tool's confirmed call. Returns (cq_id, outcome).

    outcome is one of:
      "ok"      -- captured (APPROVED for founder, PROPOSED for a teammate) + carded
      "held"    -- captured PROPOSED but over the daily cap: no immediate card, rides
                   the overflow flush (a confirmed ask MUST NOT vanish -- 1g)
      "empty"   -- nothing to file (blank request)
      "dropped" -- captured nothing (PHI summary gate refused the request)

    Founder is THROTTLE-EXEMPT: he IS the approval gate, so his explicit files are
    never capped (1g). `is_founder` is derived by the caller from the real Slack
    event user id (never a model-supplied field). A teammate over
    EXPLICIT_THROTTLE_PER_DAY is still captured -- with dm_held so the confirmed ask
    is not lost -- and the caller voices an honest, structured over-quota message."""
    request = (request or "").strip()
    if not request:
        return None, "empty"
    # A request that NAMES an existing queue item is a reference to it, never a
    # new item. Minting here is how a typed stage request became a junk meta-item
    # on 2026-08-24. Founder-only, because staging is: for anyone else this
    # resolves and reports rather than acting.
    named = find_stage_request(request)
    if named:
        if is_founder:
            outcome, _detail = stage_by_id(named, user)
            return named, ("staged" if outcome == "staged" else f"resolved:{outcome}")
        return named, "resolved:not_authorized"
    held = (not is_founder) and _explicit_count_today(user) >= EXPLICIT_THROTTLE_PER_DAY
    rec = {
        "kind": "feature", "severity": "P2", "title": request[:120],
        "summary": request[:200], "subsystem_guess": "", "entity": entity,
        "signal": "explicit", "representative": request,
        "evidence": [{"channel_id": channel_id, "ts": str(message_ts or ""), "note": request[:400]}],
        "reporter": user,
    }
    cq_id = _capture(rec, initial_status="APPROVED" if is_founder else "PROPOSED",
                     dm_held=held)
    if cq_id is None:
        return None, "dropped"
    # Derive the outcome from what was actually persisted, not the pre-decision `held`
    # flag: dm_held is set ONLY on a NEW held item, never on a dedup-recurrence. So an
    # over-quota ask that paraphrase-merges into the asker's own still-open item reports
    # "ok" (it rides the existing card) rather than a false "will surface in the digest"
    # promise for an item that has no dm_held flag and will not be flushed.
    item = get_item(cq_id)
    outcome = "held" if (item and item.get("dm_held")) else "ok"
    return cq_id, outcome


# ─────────────────────────────────────────────────────────────────────────────
# Monday menu (rides run_knowledge_review; behind _is_digest_day; zero new tasks)
# ─────────────────────────────────────────────────────────────────────────────
def _effort(n: int) -> str:
    return "S" if n <= 2 else ("M" if n <= 4 else "L")


def _affinity_key(rec: dict[str, Any]) -> str:
    """Grouping key for a bundle: subsystem_guess (the true affinity), falling back to
    entity, then 'general'. Normalized so 'Shopify' and 'shopify' group together."""
    return _normalize(str(rec.get("subsystem_guess") or "")) or \
        _normalize(str(rec.get("entity") or "")) or "general"


def _bundle_theme(items: list[dict[str, Any]]) -> str:
    """The shared theme of a bundle (defect #5: the slug must come from the common
    theme, NOT item #1). Returns the most common affinity key among the items."""
    counts: dict[str, int] = {}
    for it in items:
        counts[_affinity_key(it)] = counts.get(_affinity_key(it), 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else "bundle"


_MAX_BUNDLE_ITEMS = 4  # defect #5: cap items per staged bundle


_MENU_MAX_ROWS = 12  # cap stage-able rows shown (bundles + singletons) -- Slack's
#                      50-block ceiling; overflow is NOTED, never silently dropped.


# C3 (Code #12; audit F2/F6/F8): the menu's four groups were APPROVED, APPROVED-
# config, STAGED >14d and expired SNOOZEs -- and NO PROPOSED branch, so the queue's
# largest tier (80 rows on 9/9, 44 aged >= 14d, 13 HIGH-class) had no recurring
# surface and no backstop (the Friday sweep keys on prompt FILES, which PROPOSED
# rows lack). Keep was a reason-less, trigger-less 14-day snooze (16 of 23 aged
# STAGED rows suppressed by one tap). And the card wrote no durable artifact of
# what it carried. All four are closed here:
#   * PROPOSED coverage: rows aged >= STALE_STAGED_DAYS and/or P0/P1-class,
#     priority first; the first rows are actionable (Queue / Park / Dismiss w/
#     note) under a hard block budget, the rest are LISTED -- never dropped;
#   * park-with-trigger (reason + resume date and/or event); a due park resurfaces,
#     an event-park stays listed count-only;
#   * Keep count rendered; at KEEP_CAP the row renders in a distinct state and
#     loses its Keep button;
#   * every send writes one row to _MENU_RUNS_LEDGER (what was carried, or why
#     nothing was) -- a task that fires and writes nothing == one that never fired.
_MENU_MAX_PROPOSED_ROWS = 8   # actionable PROPOSED rows (2 blocks each); the rest list as text
_MENU_BLOCK_BUDGET = 48       # Slack's ceiling is 50 blocks; 2 kept in reserve for the notes
_MENU_RUNS_LEDGER = _STATE_DIR / "code-queue-menu-runs.jsonl"


def _proposed_coverage(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PROPOSED rows aged >= STALE_STAGED_DAYS OR P0/P1-class (either vocabulary),
    priority first, then oldest first."""
    rows = [it for it in items if it.get("status") == "PROPOSED"
            and (_age_days(it.get("ts")) >= STALE_STAGED_DAYS
                 or is_priority_severity(it.get("severity")))]
    rows.sort(key=lambda it: (0 if is_priority_severity(it.get("severity")) else 1,
                              -_age_days(it.get("ts"))))
    return rows


def _parked_rows(items: list[dict[str, Any]], now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(due, waiting): a PARKED row whose resume date has passed -- or that was
    RE-ASKED since it was parked (a recurrence is the trigger, D-051 lens B HIGH
    #2) -- is DUE; the rest are WAITING and list count-only."""
    today = now.date().isoformat()
    due: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    for it in items:
        if it.get("status") != "PARKED":
            continue
        until = str(it.get("park_until") or "")[:10]
        reasked = int(it.get("parked_recurrences") or 0) > 0
        (due if ((until and until <= today) or reasked) else waiting).append(it)
    return due, waiting


def _park_trigger_text(it: dict[str, Any]) -> str:
    """'until <date>' / 'review by <date> / on: <event>' for one PARKED row."""
    until = str(it.get("park_until") or "")[:10]
    event = _safe_reason(it, "park_event")
    parts = []
    if until:
        parts.append(("review by " if it.get("park_horizon") else "until ") + until)
    if event:
        parts.append(f"on: {event}")
    return " / ".join(parts) or "(no trigger recorded)"


def _ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(r.get("id", "")) for r in rows]


def _proposed_row_line(it: dict[str, Any]) -> str:
    age = _age_days(it.get("ts"))
    return (f"• `{it.get('severity', '?')}` {it.get('kind', '?')} [{it.get('entity', '?')}] "
            f"{_mrk(it.get('title', ''))} (`{it.get('id', '?')}`) -- {age}d")


def _fit_plain(text: str, limit: int) -> str:
    """Whole-line truncation for a fallback `text` field (D-051 lens F LOW #6):
    the `[:2900]` slice cut the last line mid-word."""
    if len(text) <= limit:
        return text
    note = "\n_(fallback text truncated -- the blocks carry the full card)_"
    out: list[str] = []
    used = 0
    for ln in text.splitlines():
        if used + len(ln) + 1 > limit - len(note):
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out) + note


_LISTING_MAX_CHARS = 2600   # one section block (Slack cap 3000); leaves room for the overflow note


def _fit_listing(header: str, lines: list[str], overflow_note: Callable[[int], str]) -> tuple[str, int]:
    """Pack as many whole lines as fit under _LISTING_MAX_CHARS and report how many
    did. A `[:2900]` slice on a long listing cut the last lines (and the '+N more'
    note) mid-word on the live card -- a lossy truncation nothing counted. Whole
    lines only; the remainder is COUNTED in the note, never silently dropped."""
    body = header
    n = 0
    for ln in lines:
        candidate = body + "\n" + ln
        if len(candidate) > _LISTING_MAX_CHARS:
            break
        body = candidate
        n += 1
    rest = len(lines) - n
    if rest:
        body += "\n" + overflow_note(rest)
    return body, n


_MENU_MAX_STALE_ROWS = 8      # actionable stale / due-parked rows; the rest list as text


def _allocate_menu_slots(n_prio: int, n_approved_rows: int, n_stale: int, n_aged: int,
                         fixed_blocks: int) -> dict[str, int]:
    """Action-row slots (2 blocks each) under the block budget, in PRIORITY order:
    PROPOSED P0/P1-class rows first (the least-netted tier, audit F8), APPROVED rows
    (the menu's original purpose), stale STAGED / due-PARKED rows, then PROPOSED aged
    rows. Every remainder is LISTED as text (one block each) -- never dropped.

    Found on the live ledger 2026-09-09 (not by a fixture): 24 stale STAGED rows
    rendered first took 48 blocks, the card hit 54, and the trailing PROPOSED
    coverage -- the section C3 exists for -- was trimmed off entirely. Allocation
    before rendering is what makes the ceiling a budget instead of a guillotine.
    """
    listing_reserve = 3  # proposed-rest + stale-rest + approved-overflow notes, worst case
    avail = max(0, (_MENU_BLOCK_BUDGET - fixed_blocks - listing_reserve) // 2)
    out: dict[str, int] = {}
    take = min(n_prio, _MENU_MAX_PROPOSED_ROWS, avail)
    out["prio"], avail = take, avail - take
    take = min(n_approved_rows, _MENU_MAX_ROWS, avail)
    out["approved"], avail = take, avail - take
    take = min(n_stale, _MENU_MAX_STALE_ROWS, avail)
    out["stale"], avail = take, avail - take
    take = min(n_aged, max(0, _MENU_MAX_PROPOSED_ROWS - out["prio"]), avail)
    out["aged"], avail = take, avail - take
    return out


def build_weekly_menu(*, carried_out: dict[str, Any] | None = None) -> tuple[str, list[dict[str, Any]]] | None:
    """(text, blocks) for the Monday menu, or None if there is nothing to show.
    APPROVED items are grouped by AFFINITY (subsystem_guess -> entity) into bundles of
    at most _MAX_BUNDLE_ITEMS; a group of one is listed singly. There is NO kitchen-sink
    "other" bundle (defect #5). Plus config items, the PROPOSED coverage (C3), a
    staleness sweep (STAGED >14d and expired SNOOZEs, with Keep counts + the cap) and
    due/waiting PARKED rows. Action slots are ALLOCATED by priority under the Slack
    block budget before anything renders (_allocate_menu_slots); every remainder is
    listed. ``carried_out`` (optional dict) is filled with the ids carried per
    section -- the durable artifact maybe_send_weekly_menu records."""
    items = load_items()
    approved = [it for it in items if it.get("status") == "APPROVED" and it.get("kind") != "config"]
    config_items = [it for it in items if it.get("status") == "APPROVED" and it.get("kind") == "config"]

    now = _now()
    # last_touch (a "Keep" tap) takes precedence so Keeping resets the staleness clock.
    stale_staged = [it for it in items if it.get("status") == "STAGED"
                    and _age_days(it.get("last_touch") or it.get("staged_at") or it.get("ts")) >= STALE_STAGED_DAYS]
    expired_snoozed = [it for it in items if it.get("status") == "SNOOZED"
                       and (_parse_ts(it.get("snooze_until")) or now) <= now]
    proposed_cov = _proposed_coverage(items)
    prio_rows = [it for it in proposed_cov if is_priority_severity(it.get("severity"))]
    aged_rows = [it for it in proposed_cov if not is_priority_severity(it.get("severity"))]
    parked_due, parked_waiting = _parked_rows(items, now)

    carried: dict[str, Any] = {
        "approved": _ids(approved), "config": _ids(config_items),
        "stale_staged": _ids(stale_staged), "expired_snoozed": _ids(expired_snoozed),
        "parked_due": _ids(parked_due), "parked_waiting": _ids(parked_waiting),
        "stale_actionable": [], "stale_listed": [], "stale_overflow": 0,
        "proposed_actionable": [], "proposed_listed": [], "proposed_overflow": 0,
        "proposed_aged": sum(1 for it in proposed_cov if _age_days(it.get("ts")) >= STALE_STAGED_DAYS),
        "proposed_priority": len(prio_rows),
        "blocks": 0, "trimmed": 0,
    }

    if not (approved or config_items or stale_staged or expired_snoozed
            or proposed_cov or parked_due or parked_waiting):
        if carried_out is not None:
            carried_out.update(carried)
        return None

    def _btn(action_id: str, label: str, value: str, *, style: str | None = None) -> dict[str, Any]:
        b: dict[str, Any] = {"type": "button", "action_id": action_id,
                             "text": {"type": "plain_text", "text": label}, "value": value}
        if style:
            b["style"] = style
        return b

    # APPROVED -> affinity bundles (<= _MAX_BUNDLE_ITEMS each), singletons listed singly.
    approved_rows: list[list[dict[str, Any]]] = []
    if approved:
        groups: dict[str, list[dict[str, Any]]] = {}
        for it in approved:
            groups.setdefault(_affinity_key(it), []).append(it)
        # Deterministic order: largest group first, then key. Split each group into
        # chunks of <= _MAX_BUNDLE_ITEMS -- NO cross-affinity merge.
        for _key, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            for start in range(0, len(group), _MAX_BUNDLE_ITEMS):
                approved_rows.append(group[start:start + _MAX_BUNDLE_ITEMS])

    # Attention rows: due parks first (a trigger fired), then capped Keeps (they
    # need a decision, not another Keep), then the oldest stale rows, then expired snoozes.
    capped = [it for it in stale_staged if int(it.get("keep_count") or 0) >= KEEP_CAP]
    capped_ids = {str(it.get("id")) for it in capped}
    uncapped = sorted([it for it in stale_staged if str(it.get("id")) not in capped_ids],
                      key=lambda it: str(it.get("staged_at") or it.get("ts") or ""))
    attention_rows = parked_due + capped + uncapped + expired_snoozed

    fixed_blocks = 1 + (1 if config_items else 0) + (1 if parked_waiting else 0) \
        + (1 if proposed_cov else 0)
    slots = _allocate_menu_slots(len(prio_rows), len(approved_rows), len(attention_rows),
                                 len(aged_rows), fixed_blocks)

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn",
         "text": "*Cora code-session queue -- Monday menu*"}},
    ]
    text_lines = ["*Cora code-session queue -- Monday menu*"]

    if approved_rows:
        shown, overflow_n = approved_rows[:slots["approved"]], max(0, len(approved_rows) - slots["approved"])
        for chunk in shown:
            ids_csv = ",".join(str(g.get("id", "")) for g in chunk)
            if len(chunk) == 1:
                it = chunk[0]
                sline = (f"• [{it.get('entity', '?')}] {_mrk(it.get('title', ''))} (`{it.get('id', '?')}`)"
                         f"\n_{format_surface_line(it)}_")
                text_lines.append("- " + sline.splitlines()[0])
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": sline[:2900]}})
                blocks.append({
                    "type": "actions", "block_id": f"cq_single_{it.get('id', '')}"[:255],
                    "elements": [
                        _btn(ACTION_STAGE, "📝 Stage prompt", str(it.get("id", "")), style="primary"),
                        _btn(ACTION_PARK, "⏸ Park", str(it.get("id", ""))),
                        _btn(ACTION_DISMISS_NOTE, "🗑️ Dismiss w/ note", str(it.get("id", ""))),
                    ],
                })
            else:
                theme = _bundle_theme(chunk)
                eff = _effort(len(chunk))
                titles = "; ".join(_mrk(g.get("title", "")) for g in chunk[:6])
                bline = f"*{theme}* ({len(chunk)} items, ~{eff}): {titles}"
                text_lines.append("- " + bline)
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": bline[:2900]}})
                blocks.append({
                    "type": "actions", "block_id": f"cq_bundle_{_slug(theme)}_{_id_suffix(chunk)}"[:255],
                    "elements": [
                        _btn(ACTION_STAGE, "📝 Stage bundle", "bundle:" + ids_csv, style="primary"),
                    ],
                })
        if overflow_n:
            oline = (f"_+{overflow_n} more bundle(s) not shown -- review the generated "
                     f"backlog; nothing was dropped._")
            text_lines.append(oline)
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": oline}})

    if config_items:
        cline = "*No Code session needed (config):* " + "; ".join(
            _mrk(c.get("title", "")) for c in config_items[:8])
        text_lines.append(cline)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": cline[:2900]}})

    # PROPOSED coverage (C3) -- P0/P1-class rows first, then aged rows; the remainder listed.
    if proposed_cov:
        hline = (f"*PROPOSED coverage* -- {len(proposed_cov)} row(s): {carried['proposed_aged']} aged >= "
                 f"{STALE_STAGED_DAYS}d, {len(prio_rows)} P0/P1-class (nothing else surfaces these)")
        text_lines.append(hline)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": hline[:2900]}})
        actionable = prio_rows[:slots["prio"]] + aged_rows[:slots["aged"]]
        rest = prio_rows[slots["prio"]:] + aged_rows[slots["aged"]:]
        for it in actionable:
            cid = str(it.get("id", ""))
            sline = _proposed_row_line(it) + f"\n_{format_surface_line(it)}_"
            text_lines.append("- " + sline.splitlines()[0])
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": sline[:2900]}})
            blocks.append({"type": "actions", "block_id": f"cq_proposed_{cid}"[:255], "elements": [
                _btn(ACTION_APPROVE, "✅ Queue", cid, style="primary"),
                _btn(ACTION_PARK, "⏸ Park", cid),
                _btn(ACTION_DISMISS_NOTE, "🗑️ Dismiss w/ note", cid),
            ]})
        carried["proposed_actionable"] = _ids(actionable)
        if rest:
            rline, n_listed = _fit_listing(
                "*Also PROPOSED and aged / priority (approve with `approve <id>` in your Cora DM):*",
                [_proposed_row_line(it) for it in rest],
                lambda k: f"_+{k} more in the generated backlog -- nothing dropped_",
            )
            text_lines.append(rline.splitlines()[0])
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": rline}})
            carried["proposed_listed"] = _ids(rest[:n_listed])
            carried["proposed_overflow"] = len(rest) - n_listed

    # Attention sweep -- due parks, stale STAGED (with the Keep count and the cap),
    # expired snoozes; the allocated rows carry buttons, the rest are listed.
    act_rows, list_rows = attention_rows[:slots["stale"]], attention_rows[slots["stale"]:]
    for it in act_rows:
        cid = str(it.get("id", ""))
        kc = int(it.get("keep_count") or 0)
        if it.get("status") == "PARKED":
            nrec = int(it.get("parked_recurrences") or 0)
            trig = (f"re-asked x{nrec} since parked" if nrec
                    else str(it.get("park_until") or "")[:10])
            sline = (f"⏰ park trigger reached ({trig}): "
                     f"{_safe_reason(it, 'park_reason')} -- {_mrk(it.get('title', ''))} (`{cid}`)"
                     f"\n_{format_surface_line(it)}_")
            # No Mark-shipped button on a row with no bundle reference: the C7 gate
            # would refuse the tap (D-051 lens B MED #4 -- 4 of 8 live buttons did).
            elements = [_btn(ACTION_APPROVE, "✅ Re-queue", cid, style="primary")]
            if it.get("bundle_id"):
                elements.append(_btn(ACTION_MARK_SHIPPED, "🚢 Mark shipped", cid))
            elements += [_btn(ACTION_PARK, "⏸ Park again", cid),
                         _btn(ACTION_DISMISS_NOTE, "🗑️ Dismiss w/ note", cid)]
            block_id = f"cq_parked_{cid}"
        else:
            why = "STAGED >14d" if it.get("status") == "STAGED" else "snooze expired"
            capped_row = kc >= KEEP_CAP
            if capped_row:
                sline = (f"⛔ KEPT x{kc} (capped) -- {why}: {_mrk(it.get('title', ''))} (`{cid}`) -- "
                         f"park it with a trigger, ship it, or dismiss it")
            else:
                sline = f"⏳ {why}: {_mrk(it.get('title', ''))} (`{cid}`)" + (f" -- kept x{kc}" if kc else "")
            if not it.get("bundle_id"):
                sline += f" · to ship: `ship {cid} <bundle-or-branch>` in your Cora DM"
            sline += f"\n_{format_surface_line(it)}_"
            elements = [_btn(ACTION_MARK_SHIPPED, "🚢 Mark shipped", cid)] if it.get("bundle_id") else []
            if not capped_row:
                elements.append(_btn(ACTION_KEEP, "Keep", cid))
            elements += [_btn(ACTION_PARK, "⏸ Park", cid), _btn(ACTION_DISMISS_NOTE, "🗑️ Dismiss w/ note", cid)]
            block_id = f"cq_stale_{cid}"
        text_lines.append("- " + sline.splitlines()[0])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": sline[:2900]}})
        blocks.append({"type": "actions", "block_id": block_id[:255], "elements": elements})
    carried["stale_actionable"] = _ids(act_rows)
    if list_rows:
        ll = []
        for it in list_rows:
            kc = int(it.get("keep_count") or 0)
            tag = ("park trigger reached" if it.get("status") == "PARKED"
                   else ("KEPT x%d (capped)" % kc if kc >= KEEP_CAP
                         else ("STAGED >14d" if it.get("status") == "STAGED" else "snooze expired")))
            ll.append(f"• {tag}: {_mrk(it.get('title', ''))} (`{it.get('id', '?')}`)")
        lline, n_listed = _fit_listing(
            f"*Also waiting on a decision ({len(list_rows)}) -- `dismiss <id>` or "
            f"`ship <id> <bundle-or-branch>` in your Cora DM, or the bundle's step-7.5 script:*",
            ll, lambda k: f"_+{k} more in the generated backlog -- nothing dropped_")
        text_lines.append(lline.splitlines()[0])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": lline}})
        carried["stale_listed"] = _ids(list_rows[:n_listed])
        carried["stale_overflow"] = len(list_rows) - n_listed

    if parked_waiting:
        wl = [f"• {_mrk(it.get('title', ''))} (`{it.get('id', '?')}`) -- {_park_trigger_text(it)}"
              for it in parked_waiting]
        pline, _n = _fit_listing(
            f"*Parked ({len(parked_waiting)}), waiting on a trigger (a re-ask resurfaces any of them):*",
            wl, lambda k: f"_+{k} more parked rows in the backlog -- nothing dropped_")
        text_lines.append(pline.splitlines()[0])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": pline}})

    # Hard ceiling (Slack rejects > 50 blocks). By construction the allocator keeps us
    # under it, so a breach is a BUG: fail loud in the log, trim the tail rather than let
    # chat.postMessage reject the whole card, and RECORD the trim in the artifact.
    if len(blocks) > 50:
        log.error("code_queue: Monday menu built %d blocks (> 50) -- trimming; fix the allocator",
                  len(blocks))
        carried["trimmed"] = len(blocks) - 50
        blocks = blocks[:50]
    carried["blocks"] = len(blocks)
    if carried_out is not None:
        carried_out.update(carried)
    return "\n".join(text_lines), blocks


def record_menu_run(row: dict[str, Any]) -> None:
    """One durable row per Monday-menu fire (C3): what the card carried, or why
    nothing was sent. Fail-soft -- an artifact write must never break the send."""
    try:
        _append_jsonl(_MENU_RUNS_LEDGER, {"ts": _now_iso(), **row})
    except Exception:  # noqa: BLE001
        log.warning("code_queue: menu-run artifact write failed (non-fatal)", exc_info=True)


def maybe_send_weekly_menu(*, client_factory: Callable | None = None) -> bool:
    """Send the Monday menu DM. No-op unless live. Returns True if a DM was sent.
    Gating on the weekday is the CALLER's job (run_knowledge_review._is_digest_day).
    Every call writes ONE artifact row (C3): sent + what was carried, or the reason
    nothing went out -- a fire that writes nothing == a fire that never happened."""
    if code_queue_level() != "live":
        record_menu_run({"sent": False, "reason": "not_live"})
        return False
    carried: dict[str, Any] = {}
    try:
        built = build_weekly_menu(carried_out=carried)
        if built is None:
            record_menu_run({"sent": False, "reason": "nothing_to_show", **carried})
            return False
        text, blocks = built
        client = (client_factory or _default_client_factory)()
        if client is None:
            record_menu_run({"sent": False, "reason": "no_slack_client", **carried})
            return False
        open_resp = client.conversations_open(users=[HARRISON_ID])
        resp = client.chat_postMessage(
            channel=open_resp["channel"]["id"], text=_fit_plain(text, 2900), blocks=blocks,
            unfurl_links=False, unfurl_media=False,
        )
        record_menu_run({"sent": True, "message_ts": str((resp or {}).get("ts", "")), **carried})
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("code_queue: weekly menu send failed (non-fatal)", exc_info=True)
        record_menu_run({"sent": False, "reason": f"error: {type(exc).__name__}", **carried})
        return False


def stage_bundle(value: str, actor_id: str) -> tuple[str, str]:
    """Stage ONE combined kickoff prompt for a bundle (value = 'bundle:id1,id2,...').
    Harrison-only. Marks each item STAGED with a shared bundle_id."""
    if actor_id != HARRISON_ID:
        return "not_authorized", "Only Harrison can stage a bundle."
    raw = value[len("bundle:"):] if value.startswith("bundle:") else value
    ids = [x for x in raw.split(",") if x]
    recs = [r for r in (get_item(i) for i in ids) if r]
    if not recs:
        return "error", "No items to stage."
    # Idempotency (D-051): the Monday menu is one multi-item message, so its
    # "Stage bundle" button threads a reply (it is NOT consumed) and can be tapped
    # again. Only stage items still awaiting a prompt; a re-tap finds none pending
    # and is a no-op pointing at the existing prompt (no second Sonnet call).
    pending = [r for r in recs if r.get("status") in ("PROPOSED", "APPROVED")]
    if not pending:
        existing = next((r.get("prompt_path") for r in recs if r.get("prompt_path")), "")
        return "noop", (f"Bundle already staged: `{existing}`" if existing
                        else "Bundle already staged.")
    # Reservation guard (defect #3): a double-tap while the first tap is still
    # generating the (multi-second) Sonnet prompt must not double-generate.
    keys = [str(r["id"]) for r in pending]
    if not _begin_staging(keys):
        return "noop", "Bundle already staging -- I'll post the prompt path when it's ready."
    try:
        # Re-read under the reservation: another tap may have JUST finished staging.
        fresh = [get_item(k) for k in keys]
        still = [r for r in fresh if r and r.get("status") in ("PROPOSED", "APPROVED")]
        if not still:
            existing = next((r.get("prompt_path") for r in fresh if r and r.get("prompt_path")), "")
            return "noop", (f"Bundle already staged: `{existing}`" if existing
                            else "Bundle already staged.")
        # C1 EVIDENCE FLOOR (D-051 lens B HIGH #1): the bundle button is the SAME
        # Monday-menu surface as the 9/7 incident's singleton Stage button, and it
        # bypassed the floor entirely (register_from_efficiency lands APPROVED rows
        # with no pointer). Floored rows are refused by id; only the rest stage.
        floored = [r for r in still if not has_evidence(r)]
        still = [r for r in still if has_evidence(r)]
        floored_ids = ", ".join(f"`{r['id']}`" for r in floored)
        if not still:
            return "no_evidence", (
                f"NOT staged -- no item in this bundle carries evidence (no Slack permalink, "
                f"no seed body): {floored_ids}. Attach the threads, or stage each deliberately "
                "with `stage <id>` in your Cora DM (the override).")
        # Slug from the shared theme, NOT item #1 (defect #5).
        slug = _slug(_bundle_theme(still))
        meta: dict[str, Any] = {}
        path = generate_kickoff_prompt(still, slug=f"{slug}-bundle", meta_out=meta)
        if not path:
            return "error", "Prompt generation failed -- nothing staged."
        bundle_id = "bnd-" + uuid.uuid4().hex[:8]
        for r in still:
            ev = {"event": "staged", "ts": _now_iso(), "id": r["id"],
                  "prompt_path": path, "bundle_id": bundle_id}
            if meta.get("mis_homed"):
                ev["mis_homed"] = True
            _append_event(ev)
        _render_backlog_safe()
        _dm_prompt_path(path)
        return "staged", (f"📝 Bundle prompt staged ({len(still)} items): `{path}`"
                          + (f" -- {len(floored)} refused by the evidence floor (no permalink, "
                             f"no seed body): {floored_ids}" if floored else ""))
    finally:
        _end_staging(keys)


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance helpers -- used by the one-shot cleanup / re-home scripts (1f / 1d)
# ─────────────────────────────────────────────────────────────────────────────
def supersede_item(loser_id: str, winner_id: str) -> bool:
    """Merge ``loser_id`` INTO ``winner_id`` (a dedup miss caught after the fact):
    mark the loser SUPERSEDED (recording ``superseded_by``) and bump the winner's
    recurrence count so its card reflects the duplicate. Both ids must exist and the
    loser must not already be SUPERSEDED. Returns True iff a merge was written."""
    loser, winner = get_item(loser_id), get_item(winner_id)
    if not loser or not winner or loser_id == winner_id:
        return False
    if loser.get("status") == "SUPERSEDED":
        return False
    _append_event({"event": "recurrence", "ts": _now_iso(), "id": winner_id})
    _append_event({"event": "superseded", "ts": _now_iso(), "id": loser_id,
                   "superseded_by": winner_id})
    _render_backlog_safe()
    return True


def _latest_staged_prompt_paths() -> dict[str, str]:
    """Latest prompt_path per cq-id across all ``staged`` events (fold order)."""
    out: dict[str, str] = {}
    for ev in _read_jsonl(_EVENT_LEDGER):
        if ev.get("event") == "staged" and ev.get("prompt_path"):
            out[str(ev.get("id") or "")] = str(ev["prompt_path"])
    return out


def plan_prompt_rehome() -> list[dict[str, str]]:
    """Plan re-homing of prompt files that landed under the REPO ``_notes`` (day-one
    defect #4) to the Founder-OS ``_notes``. CONSERVATIVE (D-051 over-deletion guard):
    a file is included ONLY if it (a) is referenced by a ``staged`` event, (b) lives
    under the repo ``_notes`` dir, (c) has a ``cora-code-prompt`` basename, and (d)
    still exists -- OR (self-heal, 2026-07-31 defect) is already at the Founder-OS
    destination but the row's ledger pointer was never backfilled (the pre-group-by-src
    applier moved a SHARED file on its first row and FileNotFoundError'd rows 2..N);
    those get a ``backfill_only`` entry. Never globs or deletes blindly.

    Only rows whose CURRENT status is STAGED are planned: appending a ``staged``
    backfill event to a terminal row (SUPERSEDED/SHIPPED/DISMISSED) would resurrect
    it via the last-write-wins fold."""
    repo_notes = _NOTES_DIR.resolve()
    status_by_id = {it.get("id"): it.get("status") for it in load_items()}
    plan: list[dict[str, str]] = []
    for cq_id, p in _latest_staged_prompt_paths().items():
        if status_by_id.get(cq_id) != "STAGED":
            continue
        src = Path(p)
        try:
            parents = list(src.resolve().parents)
        except OSError:
            continue
        if repo_notes not in parents:
            continue  # already Founder-OS-homed (or elsewhere) -- leave it
        if "cora-code-prompt" not in src.name:
            continue
        dst = founder_os_notes_dir() / src.name
        if not src.exists():
            if dst.exists():
                plan.append({"id": cq_id, "src": str(src), "dst": str(dst),
                             "backfill_only": "1"})
            continue
        plan.append({"id": cq_id, "src": str(src), "dst": str(dst)})
    return plan


def apply_prompt_rehome(plan: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Execute a ``plan_prompt_rehome`` plan: copy src -> Founder-OS ``_notes`` (via
    drive_io), backfill the ledger prompt_path (a ``staged`` event, ``rehomed=True``),
    then delete the repo copy. Best-effort per item -- one failure never aborts the
    rest. Returns the per-item outcomes.

    GROUPED BY SOURCE FILE (2026-07-31 defect): a bundle stages ONE prompt file for
    N rows, so the file is moved exactly once and every row that references it gets
    its ledger backfill. The old per-row copy-then-unlink deleted the shared file on
    row 1 and FileNotFoundError'd rows 2..N, freezing their prompt_path at the
    deleted repo path (the 5 stale STAGED rows of bnd-1fbf2c12)."""
    done: list[dict[str, Any]] = []
    moved: set[str] = set()  # srcs already copied+unlinked this run
    for a in plan:
        src, dst, cq_id = Path(a["src"]), Path(a["dst"]), a["id"]
        try:
            if a.get("backfill_only") or str(src) in moved:
                # File already at dst (moved earlier this run, or by a prior
                # pre-fix run) -- only the ledger pointer needs fixing.
                if not dst.exists():
                    raise FileNotFoundError(f"destination missing: {dst}")
            else:
                body = src.read_text(encoding="utf-8", errors="replace")
                drive_io.write_text_atomic(dst, body)
            _append_event({"event": "staged", "ts": _now_iso(), "id": cq_id,
                           "prompt_path": str(dst), "rehomed": True})
            if str(src) not in moved and not a.get("backfill_only") and src.exists():
                src.unlink()
                moved.add(str(src))
            done.append({"id": cq_id, "src": str(src), "dst": str(dst), "ok": True})
        except Exception as exc:  # noqa: BLE001 -- best-effort per item
            log.warning("code_queue: rehome failed for %s: %s", cq_id, exc)
            done.append({"id": cq_id, "src": str(src), "dst": str(dst),
                         "ok": False, "error": str(exc)})
    _render_backlog_safe()
    return done


# ─────────────────────────────────────────────────────────────────────────────
# Seed (migration) helper -- used by scripts/seed_code_queue.py
# ─────────────────────────────────────────────────────────────────────────────
def seed_item(*, kind: str, severity: str, title: str, summary: str, entity: str,
              signal: str, status: str, subsystem_guess: str = "",
              stage_now: bool = False) -> str | None:
    """Directly seed a queue item (no DM, no classifier). Idempotent on fingerprint.
    Used only by the one-shot seed script. Returns the cq-id.

    Slice 4: seeding straight to status="APPROVED" is an approval path that bypasses
    process_queue_action entirely -- which is how cq-f1236540b61e (P1) went a week
    APPROVED-and-unstaged. This stays NON-generating by default (the function is
    documented "no DM, no classifier", and firing a Sonnet call + a Drive write as a
    side effect of a data-migration helper would surprise every seeding script and the
    MCP seed tool), but it can no longer be SILENT: a P0/P1-class APPROVED seed logs a
    WARNING naming the id, and code_queue.priority_items_missing_kickoff surfaces it in
    the nightly health check within PRIORITY_KICKOFF_GRACE_HOURS.

    Pass stage_now=True to generate the kickoff inline (a seeding script that intends
    the item to be built immediately).
    """
    rec = {
        "kind": kind, "severity": severity, "title": title, "summary": summary,
        "subsystem_guess": subsystem_guess or entity, "entity": entity, "signal": signal,
        "representative": title,
        "evidence": [{"channel_id": "", "ts": "", "note": summary[:200]}],
        "reporter": HARRISON_ID,
        "seeded": True,  # C1: a seed's body IS its evidence (has_evidence / _is_seed_shaped)
    }
    # Summary PHI gate -- FAIL-CLOSED, mirroring _capture (D-051): the seed persists
    # title/summary RAW (title is the dedup basis; it also renders into the KB-ingested
    # code-session-backlog.md and would egress). A PHI-tripping seed is refused outright,
    # never persisted -- same contract as the capture path (fixes the seed/ _capture
    # asymmetry). The 1h LEX-DDD seed is generic build text and passes cleanly.
    # is_any_phi 2026-07-30 parity-raise (3-predicate union).
    try:
        if phi_guard.is_any_phi_request(f"{title} {summary}".strip()):
            # NAMES ONLY, never the text (D-082). A refusal that leaves no
            # trace of WHICH detector fired cannot be tuned -- both 8/24 false
            # positives were diagnosed only by reconstructing candidate wordings
            # after the fact.
            try:
                fired = ",".join(phi_guard.which_predicates(f"{title} {summary}")) or "none"
            except Exception:  # noqa: BLE001
                fired = "unknown"
            log.info("code_queue.seed_item: refused PHI-flagged seed "
                     "(signal=%s entity=%s predicates=%s)", signal, entity, fired)
            return None
    except Exception:  # noqa: BLE001 -- fail closed
        log.info("code_queue.seed_item: PHI check errored -- refusing seed (fail-closed)")
        return None
    # D-051 2026-07-31 follow-through: screen subsystem_guess here too -- the seed
    # lane is caller-supplied (incl. the MCP cora_code_queue_seed tool) and bypasses
    # _capture's screen, yet the value egresses via mixed-bundle prompt-path slugs
    # (_affinity_key -> _bundle_theme -> _slug). Fail-closed blank (falls back to
    # the entity code); the seed itself survives.
    sub = str(subsystem_guess or "")
    if sub:
        try:
            if phi_guard.is_any_phi(sub):
                sub = ""
        except Exception:  # noqa: BLE001 -- fail closed
            sub = ""
    rec["subsystem_guess"] = sub or entity
    class_key = _class_key(title, sub or entity)
    existing = find_fingerprint(signal, title, class_key=class_key)
    if existing:
        return existing
    # PHI-safe persistence (mirror _capture FULLY, D-051 defect A): NEVER persist a raw
    # LEX or PHI-tripping representative -- else it becomes an embedding candidate whose
    # raw text egresses to OpenAI. Exact-hash dedup still works (fingerprint is over
    # `title`). The evidence note is scrubbed on the SAME rule as _capture (LEX -> pointer
    # only; any note that itself trips is_any_phi -> dropped) -- the seed path previously
    # persisted summary[:200] raw, an at-rest LEX/PHI leak for a LEX seed (1i).
    is_lex = str(entity or "").strip().upper().startswith("LEX")
    rec["evidence"] = _scrub_evidence(rec.get("evidence"), is_lex=is_lex)
    try:
        rep_phi = phi_guard.is_any_phi(title)
    except Exception:  # noqa: BLE001 -- fail closed
        rep_phi = True
    store_rep = "" if (is_lex or rep_phi) else title
    rec["representative"] = store_rep
    if is_lex:
        # D-051 PHI parity-raise (2026-07-30): same title/summary redaction as
        # _capture -- the seed path is a capture-equivalent write.
        _redact_lex_build_fields(rec)
    cq_id = "cq-" + uuid.uuid4().hex[:12]
    rec["id"] = cq_id
    rec["ts"] = _now_iso()
    rec["status"] = status
    rec["count"] = 1
    rec["fingerprint"] = _fingerprint(signal, title)
    _append_event({"event": "captured", **rec})
    _append_fingerprint(rec["fingerprint"], signal, store_rep, cq_id, class_key=class_key)
    # Slice 4: a seed-at-APPROVED priority item bypassed process_queue_action's
    # kickoff rule entirely and did so SILENTLY. Generate on request; otherwise WARN
    # loudly and let the nightly monitor catch it.
    if str(status).strip().upper() == "APPROVED" and is_priority_severity(severity):
        if stage_now:
            outcome, detail = ensure_kickoff_staged(cq_id)
            if outcome == "error":
                log.error("code_queue.seed_item: %s seeded APPROVED (%s) but the "
                          "kickoff did NOT generate: %s", cq_id, severity, detail)
            else:
                log.info("code_queue.seed_item: %s seeded APPROVED (%s), kickoff %s: %s",
                         cq_id, severity, outcome, detail)
        else:
            log.warning("code_queue.seed_item: %s seeded APPROVED at %s (P0/P1-class) "
                        "with NO kickoff prompt -- stage it, or the nightly health "
                        "check will WARN after %dh (pass stage_now=True to generate "
                        "here)", cq_id, severity, PRIORITY_KICKOFF_GRACE_HOURS)
    return cq_id


# ─────────────────────────────────────────────────────────────────────────────
# R14-9(a) (cq-323c8974fa02): the founder-DM queue/card-STATUS read
# ─────────────────────────────────────────────────────────────────────────────
# 2026-09-21 08:44-08:47 AZ: Harrison asked three times whether his Monday-menu
# card presses had registered and got three zero-tool answers ("I don't have
# direct read access to the card ledger", "outside my current context scope",
# "that needs a direct check of the ledger"). VERIFY-FIRST overturned the
# premise that the read existed: the bot had NO tool that reads this ledger (only
# the MCP cora_code_queue surface, which the bot model is never offered), so the
# denials were TRUE for the bot as built. The truthful answer was on disk: 20 of
# 21 carded rows carried a decision event after the menu ts; the 21st
# (cq-f880ce946bb6) was an APPROVED row whose Stage press the evidence floor had
# refused (outcome=no_evidence, which writes no event).
#
# This block is (1) a deterministic intent predicate the DM seam uses to FORCE the
# new read tool before the model speaks, and (2) the read-only renderer the tool
# returns. Pure reads: no _append_event, no file writes (test-pinned by bytes).
_AZ = timezone(timedelta(hours=-7))          # Arizona: no DST
_QS_MAX_CHARS = 500
# A command, never a status question: the capture / verb / write paths own these.
# `show me` / `give me` are READ requests, not commands (D-051 forcing-seams-6: the
# first cut listed show/give here, so "show me which cards are still unresponded?"
# was rejected while "tell me which ..." forced, and _QS_REQUEST_RE's own `show me`
# alternative was dead). A bare "show the cards again" / "give Tommy the ..." stays
# imperative. The lookahead is fixed-position under the \A anchor (D-171).
_QS_IMPERATIVE_RE = re.compile(
    r"\A[^\w\n]{0,8}(?:(?:hey|hi|ok|okay)[,!]?[ \t]{1,3})?(?:@?cora[,:]?[ \t]{1,3})?"
    r"(?:please[ \t]{1,3})?"
    r"(?:stage|approve|dismiss|ship|queue|park|keep|mark|close|delegate|surface|resend|"
    r"re-?post|send|file|log|create|press|tap|click|re-?stage|re-?queue|draft|dm|remember|"
    r"do|go|make|add|fix|build|update|delete|remove|move|flag|"
    r"give(?![ \t]{1,3}me\b)|show(?![ \t]{1,3}me\b))\b",
    re.IGNORECASE)
_QS_REQUEST_RE = re.compile(
    r"\?|\b(?:confirm|check|verify|tell[ \t]+me|let[ \t]+me[ \t]+know|show[ \t]+me|list)\b"
    r"[^.\n]{0,20}?\b(?:if|whether|which|what|how[ \t]+many|that|any|all)\b",
    re.IGNORECASE)
# Tier A names the CODE-QUEUE object specifically (D-051 forcing-seams-2). A bare
# `backlog` forced the read for "is the AP backlog still stuck?", and `decision` /
# `menu` were allowed card prefixes, so the knowledge-review decision cards and
# OSN's restaurant menu cards read as queue cards. `backlog` now needs a code /
# queue / build qualifier; a generic `cards` still counts (Q1 is "Have all cards
# been responded to") unless _QS_CARD_BEFORE_RE names another card surface;
# `menu cards` counts only as `monday menu cards`.
# D-051 F2-R4: a bare `button` is NOT a press prefix -- "did the Buy Now button
# presses register in Shopify?" / "is the checkout button click count still stuck?"
# forced the card read. A button press counts only with a card / queue-verb / Monday
# menu qualifier ("card button presses", "stage button", "monday menu button ...").
_QS_OBJECT_RE = re.compile(
    r"\b(?:(?:code[\s-]?(?:session[ \t]+)?|build[ \t]+|queue[ \t]+|capture[ \t]+|"
    r"monday[ \t]+(?:menu[ \t]+)?)?cards?\b"
    r"|(?:card|stage|keep|park|dismiss|queue|approve)[ \t]+"
    r"(?:press(?:es)?|taps?|clicks?|buttons?)\b"
    r"|my[ \t]+presses\b"
    r"|monday[ \t]+menu\b"
    r"|(?:my|the|your)[ \t]+(?:code[\s-]?(?:session[ \t]+)?)?queue\b"
    r"|code[\s-]?(?:session[ \t]+)?queue\b"
    r"|(?:code[\s-]?(?:session[ \t]+)?|queue[ \t]+|build[ \t]+)backlog\b"
    r"|staged[ \t]+(?:items?|prompts?|kickoffs?|sessions?)\b"
    r"|(?:kickoff|code[\s-]?session)[ \t]+prompts?\b"
    r"|cq-[0-9a-f]{12}\b)",
    re.IGNORECASE)
# A 'card(s)' hit that is part of an everyday compound -- or of ANOTHER Cora card
# surface (decision-inbox / knowledge-review / confirm / blog-publish / catch-up /
# meeting-ask cards: other ledgers, not covered by this read) or a retail / menu
# card (OSN is a restaurant group; F3E ships rack and shelf cards) -- is NOT a
# queue card. `menu` here is safe for the Monday menu: "monday menu cards" is one
# _QS_OBJECT_RE hit that starts at "monday", so its before-text never ends in menu.
_QS_CARD_BEFORE_RE = re.compile(
    r"\b(?:credit|debit|gift|business|amex|visa|mastercard|corporate|company|bank|sim|report|"
    r"rate|playing|trading|greeting|thank[\s-]you|birthday|loyalty|punch|fighter|id|key|wild|"
    r"score|index|recipe|preview|rewards?|membership|insurance|health|sd|memory|graphics|"
    r"tarot|flash|post|christmas|holiday|wedding|"
    r"decisions?|knowledge|review|inbox|confirm(?:ation)?|blog|publish(?:ing)?|catch[\s-]?up|"
    r"meeting|ask|menu|rack|promo(?:tional)?|shelf|price|pricing|sample|table|tasting)"
    r"[ \t]+\Z", re.IGNORECASE)
_QS_CARD_AFTER_RE = re.compile(
    r"\A[ \t]*(?:payments?|statements?|balances?|charges?|numbers?|readers?|terminals?|fees?|"
    r"limits?|transactions?|holders?|swipes?|processing|program|reader|slot)\b", re.IGNORECASE)
_QS_STATUS_RE = re.compile(
    r"\b(?:(?:been[ \t]+)?(?:responded|replied)[ \t]+to|unresponded|un-?answered|registered|"
    r"register|recorded|landed|land|(?:go|went|gone)[ \t]+through|t(?:ake|ook)[ \t]+effect|"
    r"stuck|stick|(?:un)?decided|actioned|acknowledged|missed|"
    r"still[ \t]+(?:show(?:s|ing)?|pending|waiting|open|there|awaiting|unanswered|up)|"
    r"show(?:s|ing)?[ \t]+as|awaiting(?:[ \t]+a)?[ \t]+decision|"
    r"waiting[ \t]+on[ \t]+(?:me|a[ \t]+decision|my)|outstanding|"
    r"left[ \t]+to[ \t]+(?:decide|review|respond|answer)|pending[ \t]+(?:a[ \t]+)?(?:decision|my))\b",
    re.IGNORECASE)
# The queue's OWN outcome words -- status terms only when paired with a cq- id
# (D-051 forcing-seams-6: "has cq-X been staged yet?", "where does cq-X stand?"
# never forced although the tool advertises per-id lookups). Past participles and
# nouns only: a base-form verb next to an id is a command (_QS_VERB_ID_RE).
_QS_ID_STATUS_RE = re.compile(
    r"\b(?:staged|approved|dismissed|shipped|parked|kept|snoozed|superseded|blocked|"
    r"status|stand(?:s|ing)?)\b", re.IGNORECASE)
# A queue verb NEXT TO an id, anywhere in the message, is a command or a compound
# command + question ("can you stage cq-X? and did cq-Y land?") -- never force the
# read on it (D-051 forcing-seams-3). The forced read would make the turn's
# tool_use_count 1, which switches off S2's zero-tool lexicon screen -- the net the
# R14-2 start-anchor residual relies on for exactly this shape. Adjacent only
# (bounded decoration: space, backtick, quote), so "did my stage press on cq-X
# land?" still forces.
_QS_VERB_ID_RE = re.compile(
    r"\b(?:re-?stage|stage|approve|dismiss|ship|un-?park|park|keep|snooze|re-?queue|queue)\b"
    r"[^\w\n]{0,8}cq-", re.IGNORECASE)
# Follow-up turns ("did those go through?") refer back; only valid when a recent
# prior user turn was itself a card-status question (Tier A). The pronoun must be
# one that REFERS TO PRESSES / CARDS (D-051 forcing-seams-1): the first cut took
# any / all / it / they / ones / each, so "any outstanding invoices for F3E?", "were
# all the payroll runs recorded?" and "yes go ahead, did it land?" forced the card
# read for up to ten DM messages after one card question. Now: `them`; `those` /
# `these` / `any` only in PRONOUN use (followed by a verb, `of them`, or the end of
# the clause -- never a determiner in front of a noun: "those invoices", "any open
# decisions"); `the rest` / `the others`; the press nouns and verbs. Q2 ("confirm
# if any have not been responded to") and Q3 ("I have pressed them all") keep
# forcing; "did they go through?" no longer does (accepted recall cost). Every
# lookahead is bounded and fixed-position (D-171).
#
# D-051 F2-R1: in an INVERTED follow-up ("did those land?", "have those
# registered?", "did any land?") the auxiliary sits BEFORE the pronoun and the
# status MAIN verb after it, so the lookahead also takes the status main verbs --
# but only when the verb closes its clause (end, punctuation, or a closing adverb:
# "did those register yet?"). That keeps a determiner in front of a participle
# adjective or a homograph noun out ("those landed costs", "any registered
# users", "those land parcels"). A bare BASE form after `any` also needs an
# auxiliary right before `any`: "did any land?" is a follow-up, "did we buy any
# land?" ends the same way and is not. Each lookbehind is fixed-width.
_QS_PRON_END = (r"(?=[ \t]{0,3}(?:[?.!,;:)\-–—]|\Z|(?:yet|ok|okay|properly|"
                r"correctly|already|fine|now|too|then|in|on|at|through|successfully)\b))")
_QS_PRON_ADV = r"(?:(?:actually|really|ever|finally|both|all|two|three)[ \t]{1,3})?"
_QS_PRON_PART = (r"(?:" + _QS_PRON_ADV + r"(?:landed|registered|recorded|stuck)\b" + _QS_PRON_END
                 + r"|" + _QS_PRON_ADV
                 + r"(?:t(?:ake|ook)[ \t]{1,3}effect|show(?:s|ing|ed)?[ \t]{1,3}as)\b)")
_QS_PRON_BASE = r"(?:" + _QS_PRON_ADV + r"(?:land|register|stick)\b" + _QS_PRON_END + r")"
_QS_PRONOUN_RE = re.compile(
    r"\b(?:them"
    r"|(?:those|these)(?=[ \t]{0,3}(?:[?.!,;:)\-–—]|\Z|(?:have|has|had|were|was|are|is|"
    r"did|do|got|get|went|go|all|still|been|of|ones|not|that|which|i|you)\b|"
    + _QS_PRON_PART + r"|" + _QS_PRON_BASE + r"))"
    r"|any(?=[ \t]{0,3}(?:[?.!,;:)\-–—]|\Z|(?:have|has|had|were|was|are|is|not|still|"
    r"been|got|get|did|go|went|left|that|which|of[ \t]{1,3}(?:them|those|these))\b|"
    + _QS_PRON_PART + r"))"
    r"|(?:(?<=\bdid[ \t])|(?<=\bdo[ \t])|(?<=\bdoes[ \t])|(?<=\bcan[ \t])|(?<=\bwill[ \t])|"
    r"(?<=\bcould[ \t])|(?<=\bwould[ \t]))any(?=[ \t]{1,3}" + _QS_PRON_BASE + r")"
    r"|the[ \t]+(?:rest|others)"
    r"|(?:my|the|those|these)[ \t]+"
    r"(?:press(?:es)?|taps?|clicks?)(?![ \t]+(?:release|releases|pipeline|coverage|hits?|kit|"
    r"mentions?|pieces?|tour|list|conference|team|room))|pressed|tapped|clicked)\b",
    re.IGNORECASE)
# A follow-up that names its OWN non-queue object is not a follow-up about cards:
# "which of them are still pending my approval in Asana?" (D-051 forcing-seams-1).
# Tier B only -- a Tier-A message names a queue object explicitly.
_QS_FOREIGN_RE = re.compile(
    r"\b(?:asana|tasks?|subtasks?|invoices?|bills?|payments?|payroll|bank|wires?|deposits?|"
    r"transfers?|emails?|inbox|drafts?|deals?|orders?|pos|shipments?|pallets?|hubspot|qbo|"
    r"quickbooks|shopify|deposco|calendar|meetings?|invites?|transcripts?|receipts?|"
    r"statements?|sources|knowledge[ \t]+base|kb|mailboxes?|folders?|files?|docs?|documents?)\b",
    re.IGNORECASE)
# D-051 F2-R4 / forcing-seams-1 residual: a press / click / tap that happened on a
# STOREFRONT, web, ad or MEDIA surface is not a card press. The press nouns are
# shared with ordinary marketing and press-coverage talk, so a Tier-B follow-up
# ("did the clicks land on the landing page?", "did the press land in the Phoenix
# paper?") and a Tier-A hit whose only object is the weak "my presses" ("did my
# presses land in the Phoenix paper?") never force when the text names one.
# Bare `page` / `site` are deliberately absent (a card lives on a Slack page too).
_QS_SURFACE_RE = re.compile(
    r"\b(?:shopify|klaviyo|amazon|landing[ \t]+pages?|web[ \t]?pages?|web[ \t]?sites?|"
    r"storefronts?|checkout|pop-?ups?|listings?|analytics|kiosks?|ads?|campaigns?|"
    r"newspapers?|paper|articles?|journal|magazines?|coverage|instagram|tiktok|facebook|"
    r"linkedin|youtube|google)\b", re.IGNORECASE)
# The Tier-A objects that name only a press, not a card or the queue: vetoed by a
# foreign or surface object in the same message (strong objects never are).
_QS_WEAK_OBJECT_RE = re.compile(r"my[ \t]+presses", re.IGNORECASE)
_QS_CQ_ID_RE = re.compile(r"\bcq-[0-9a-f]{12}\b", re.IGNORECASE)
_QS_CLAUSE_BREAK = re.compile(r"[.!?\n;]")
_QS_PAIR_GAP = 60


def _qs_card_hit_is_compound(text: str, m: re.Match) -> bool:
    if not m.group(0).lower().rstrip("s").endswith("card"):
        return False
    before = text[max(0, m.start() - 25):m.start()]
    after = text[m.end():m.end() + 20]
    return bool(_QS_CARD_BEFORE_RE.search(before) or _QS_CARD_AFTER_RE.match(after))


def _qs_paired(text: str, left: list[tuple[int, int]], right: list[tuple[int, int]]) -> bool:
    """Any (left, right) span pair in the SAME clause, <= _QS_PAIR_GAP chars apart,
    either order. Bounded: both lists are capped by the 500-char gate."""
    for ls, le in left:
        for rs, re_ in right:
            lo, hi = (le, rs) if le <= rs else (re_, ls)
            if hi - lo > _QS_PAIR_GAP:
                continue
            if lo < hi and _QS_CLAUSE_BREAK.search(text, lo, hi):
                continue
            return True
    return False


def _qs_off_surface(text: str) -> bool:
    """The message names a non-queue object or a storefront / web / media surface."""
    return bool(_QS_FOREIGN_RE.search(text) or _QS_SURFACE_RE.search(text))


def _qs_tier_a(text: str) -> bool:
    hits = [m for m in _QS_OBJECT_RE.finditer(text) if not _qs_card_hit_is_compound(text, m)]
    if hits and all(_QS_WEAK_OBJECT_RE.fullmatch(m.group(0)) for m in hits) \
            and _qs_off_surface(text):
        return False
    objs = [(m.start(), m.end()) for m in hits]
    if not objs:
        return False
    stats = [(m.start(), m.end()) for m in _QS_STATUS_RE.finditer(text)]
    if stats and _qs_paired(text, objs, stats):
        return True
    ids = [(m.start(), m.end()) for m in _QS_CQ_ID_RE.finditer(text)]
    if not ids:
        return False
    id_stats = [(m.start(), m.end()) for m in _QS_ID_STATUS_RE.finditer(text)]
    return bool(id_stats) and _qs_paired(text, ids, id_stats)


def _qs_gates(text: str) -> bool:
    return (bool(text) and len(text) <= _QS_MAX_CHARS
            and not _QS_IMPERATIVE_RE.match(text) and bool(_QS_REQUEST_RE.search(text))
            and not _QS_VERB_ID_RE.search(text))


def is_queue_status_question(text: str, *, prior_user_texts: Any = ()) -> bool:
    """True when *text* asks about the STATE of Harrison's code-queue cards / presses
    (Tier A: a code-queue object and a status term in one clause, or a cq- id and
    one of the queue's own outcome words), or is a follow-up ("did those go
    through?") to such a question in the last three user turns (Tier B: a
    press-referring pronoun, no object of its own). Fail-closed: > 500 chars, an
    imperative / command opening, a queue verb next to a cq- id anywhere, or no
    question / request form -> False. The caller gates on founder + DM, on no
    staged write pending and on no verb attempt; this predicate reads text only.
    Regex over bounded input plus plain span logic (D-171: re-timed, tests pin the
    degenerate shapes)."""
    t = html.unescape(str(text or "")).strip()
    if not _qs_gates(t):
        return False
    if _qs_tier_a(t):
        return True
    pron = [(m.start(), m.end()) for m in _QS_PRONOUN_RE.finditer(t)]
    stats = [(m.start(), m.end()) for m in _QS_STATUS_RE.finditer(t)]
    if not (pron and stats and _qs_paired(t, pron, stats)):
        return False
    if _qs_off_surface(t):
        return False
    priors = [html.unescape(str(p or "")).strip() for p in list(prior_user_texts or ())[-3:]]
    return any(p and len(p) <= _QS_MAX_CHARS and _qs_tier_a(p) for p in priors)


_QS_DECISION_EVENTS = frozenset({
    "approved", "dismissed", "snoozed", "staged", "shipped", "kept", "parked",
    "superseded", "blocked",
})
# The Monday-menu rows that carry decision BUTTONS (the rest are listed as text).
_QS_CARDED_KEYS = ("approved", "stale_actionable", "proposed_actionable", "parked_due",
                   "expired_snoozed")
_QS_CAPTURE_CARD_DAYS = 7
_QS_TITLE_CHARS = 80
QUEUE_STATUS_FOOTER = (
    "The card's own text does NOT refresh after a press (known: cq-2d26f131091e), so a "
    "card can read as unanswered after the press registered -- this ledger is the "
    "source of truth. A repeat press is safe: Approve / Stage / Dismiss / Ship are "
    "no-ops once recorded; a repeat Keep or Later records one more keep / snooze.")
# D-051 forcing-seams-2: the read states its own scope, so a turn forced onto it by
# a question about some OTHER card surface cannot relay the Monday-menu tally as
# that surface's truth.
QUEUE_STATUS_SCOPE = (
    "Scope: code-queue cards only (the Monday menu and code-queue capture cards). "
    "Decision-inbox, knowledge-review, confirm and blog-publish cards are separate "
    "ledgers this read does not cover.")


def _qs_az(ts: Any) -> str:
    dt = _parse_ts(ts)
    return dt.astimezone(_AZ).strftime("%a %m/%d %H:%M AZ") if dt else "time unknown"


def _qs_title(it: dict[str, Any] | None) -> str:
    """LEX-safe (the view is load_items'), PHI-screened, capped."""
    if not it:
        return ""
    title = " ".join(str(it.get("title") or "").split())
    if not title or phi_guard.is_any_phi(title):
        return ""
    return title if len(title) <= _QS_TITLE_CHARS else title[:_QS_TITLE_CHARS - 3] + "..."


def latest_menu_run() -> dict[str, Any] | None:
    """The most recent Monday-menu row that was actually SENT (read-only)."""
    rows = [r for r in _read_jsonl(_MENU_RUNS_LEDGER) if r.get("sent")]
    return rows[-1] if rows else None


def _events_by_id() -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for ev in _read_jsonl(_EVENT_LEDGER):
        cid = str(ev.get("id") or "").lower()
        if cid:
            out.setdefault(cid, []).append(ev)
    return out


def _first_decision_after(events: list[dict[str, Any]], after: datetime | None
                          ) -> dict[str, Any] | None:
    for ev in events:
        if ev.get("event") not in _QS_DECISION_EVENTS:
            continue
        ts = _parse_ts(ev.get("ts"))
        if after is None or (ts is not None and ts >= after):
            return ev
    return None


def _qs_line(cid: str, safe: dict[str, Any] | None, raw: dict[str, Any] | None,
             decision: dict[str, Any] | None) -> str:
    title = _qs_title(safe)
    head = f"`{cid}`" + (f" {title}" if title else "")
    if decision is not None:
        via = f", via {decision['via']}" if decision.get("via") else ""
        return (f"- {head} -- {str(decision.get('event')).upper()} "
                f"{_qs_az(decision.get('ts'))}{via}")
    status = str((safe or {}).get("status") or "?")
    line = f"- {head} -- no decision recorded (status {status})"
    if raw is not None and status == "APPROVED" and not has_evidence(raw):
        line += (" -- a Stage press on this card is refused by the evidence floor and "
                 f"writes nothing; type `stage {cid}` to override")
    return line


def render_card_status(cq_ids: Any = None, *, now: datetime | None = None) -> str:
    """Deterministic, read-only answer to "have my cards / presses registered?".

    With *cq_ids*: one status line per id (status + its latest decision event).
    Without: the latest SENT Monday menu's carded rows (which have a decision
    event after the menu went out, which do not), plus capture cards DM'd in the
    last 7 days still PROPOSED with no decision. Titles are LEX-safe (load_items),
    PHI-screened and capped. Writes nothing."""
    now = now or _now()
    safe_by_id = {str(r.get("id") or "").lower(): r for r in load_items()}
    raw_by_id = {str(k).lower(): v for k, v in _fold_items().items()}
    events = _events_by_id()
    lines: list[str] = []
    ids = [str(x).strip().lower() for x in (cq_ids or []) if str(x).strip()]
    if ids:
        lines.append("*Code-queue status (from the ledger):*")
        for cid in ids[:20]:
            if cid not in raw_by_id:
                # Code #14 D-051 round 2 (forcing-seams-5): membership is the RAW ledger
                # (known_ids(), the fabricated-id rail's reference set), not the fold -- an
                # ORPHAN id (events, no `captured`) is in the ledger and must not be
                # reported absent.
                if events.get(cid):
                    lines.append(f"- `{cid}` -- in the queue ledger with events only (no captured "
                                 "record), so it has no status to report")
                else:
                    lines.append(f"- `{cid}` -- not in the queue ledger")
                continue
            decisions = [e for e in events.get(cid, []) if e.get("event") in _QS_DECISION_EVENTS]
            lines.append(_qs_line(cid, safe_by_id.get(cid), raw_by_id.get(cid),
                                  decisions[-1] if decisions else None))
        lines.append("")
        lines.append(QUEUE_STATUS_SCOPE)
        lines.append(QUEUE_STATUS_FOOTER)
        return "\n".join(lines)

    menu = latest_menu_run()
    if menu is None:
        lines.append("No Monday menu has been sent yet (the menu-run ledger has no sent row).")
    else:
        menu_ts = _parse_ts(menu.get("ts"))
        carded: list[str] = []
        for key in _QS_CARDED_KEYS:
            for cid in menu.get(key) or []:
                cid = str(cid).lower()
                if cid and cid not in carded:
                    carded.append(cid)
        decided, waiting = [], []
        for cid in carded:
            dec = _first_decision_after(events.get(cid, []), menu_ts)
            (decided if dec is not None else waiting).append((cid, dec))
        lines.append(f"*Monday menu sent {_qs_az(menu.get('ts'))}:* {len(carded)} card(s) with "
                     f"decision buttons -- {len(decided)} decided since it went out, "
                     f"{len(waiting)} with no decision recorded.")
        if waiting:
            lines.append("*No decision recorded:*")
            for cid, _ in waiting:
                lines.append(_qs_line(cid, safe_by_id.get(cid), raw_by_id.get(cid), None))
        if decided:
            lines.append("*Decided:*")
            for cid, dec in decided:
                lines.append(_qs_line(cid, safe_by_id.get(cid), raw_by_id.get(cid), dec))
    cutoff = now - timedelta(days=_QS_CAPTURE_CARD_DAYS)
    pending_capture: list[str] = []
    for cid, evs in events.items():
        sent = [e for e in evs if e.get("event") == "dm_sent"]
        if not sent:
            continue
        sent_ts = _parse_ts(sent[-1].get("ts"))
        if sent_ts is None or sent_ts < cutoff:
            continue
        safe = safe_by_id.get(cid)
        if not safe or safe.get("status") != "PROPOSED":
            continue
        if _first_decision_after(evs, sent_ts) is None:
            pending_capture.append(_qs_line(cid, safe, raw_by_id.get(cid), None))
    if pending_capture:
        lines.append(f"*Capture cards DM'd in the last {_QS_CAPTURE_CARD_DAYS} days, still "
                     "undecided:*")
        lines.extend(pending_capture[:20])
    lines.append("")
    lines.append(QUEUE_STATUS_SCOPE)
    lines.append(QUEUE_STATUS_FOOTER)
    return "\n".join(lines)
