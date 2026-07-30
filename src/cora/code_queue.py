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

# Statuses at which an item is still "live" -- a new similar signal should dedup INTO
# it (used by the embedding paraphrase layer). Terminal statuses are excluded so a
# dismissed/shipped item never absorbs a genuinely fresh ask.
_OPEN_STATUSES = frozenset({"PROPOSED", "APPROVED", "STAGED", "SNOOZED", "BLOCKED"})

# Block Kit action ids (own namespace; handled by app.py wrappers)
ACTION_APPROVE = "code_queue_approve"
ACTION_EDIT = "code_queue_edit"
ACTION_DISMISS = "code_queue_dismiss"
ACTION_LATER = "code_queue_later"
ACTION_STAGE = "code_queue_stage"           # stage a prompt (single item or bundle)
ACTION_MARK_SHIPPED = "code_queue_shipped"
ACTION_KEEP = "code_queue_keep"
VIEW_EDIT_SUBMIT = "code_queue_edit_submit"

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATE_DIR = _REPO_ROOT / "data" / "state"
_EVENT_LEDGER = _STATE_DIR / "code-session-queue.jsonl"
_FINGERPRINT_LEDGER = _STATE_DIR / "code-queue-fingerprints.jsonl"
_SIGNALS_LEDGER = _STATE_DIR / "code-queue-signals.jsonl"
_NOTES_DIR = _REPO_ROOT / "_notes"

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
    """True if free text must NOT be persisted raw at rest: LEX-sourced OR
    is_phi_risk (fail-closed on error). D-082 extension."""
    if str(entity or "").strip().upper().startswith("LEX"):
        return True
    try:
        return bool(phi_guard.is_phi_risk(text))
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


def _fold_items() -> dict[str, dict[str, Any]]:
    """Fold the append-only event ledger into {id: record}. Last-write-wins per
    field; process_queue_action enforces which transitions are legal to write."""
    items: dict[str, dict[str, Any]] = {}
    for ev in _read_jsonl(_EVENT_LEDGER):
        et = ev.get("event")
        if et == "captured":
            rec = {k: v for k, v in ev.items() if k != "event"}
            rec.setdefault("count", 1)
            rec.setdefault("status", "PROPOSED")
            items[rec.get("id", "")] = rec
            continue
        rec = items.get(ev.get("id", ""))
        if not rec:
            continue
        if et == "recurrence":
            rec["count"] = int(rec.get("count", 1)) + 1
            rec["last_seen"] = ev.get("ts")
            ev_ev = ev.get("evidence")
            if ev_ev:
                ev_list = rec.get("evidence") or []
                if len(ev_list) < 10:
                    rec["evidence"] = ev_list + [ev_ev]
        elif et == "approved":
            rec["status"] = "APPROVED"
        elif et == "dismissed":
            rec["status"] = "DISMISSED"
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
        elif et == "kept":
            rec["last_touch"] = ev.get("ts")
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


def load_items() -> list[dict[str, Any]]:
    """All queue records (folded), newest-captured first."""
    items = list(_fold_items().values())
    items.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    return items


def get_item(cq_id: str) -> dict[str, Any] | None:
    return _fold_items().get(cq_id)


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
    dropped to a pointer if it itself trips is_phi_risk (belt-and-braces)."""
    out: list[dict[str, Any]] = []
    for e in (evidence or [])[:5]:
        ptr = {"channel_id": str(e.get("channel_id", "") or ""), "ts": str(e.get("ts", "") or "")}
        note = str(e.get("note", "") or "")
        if note and not is_lex and not phi_guard.is_phi_risk(note):
            ptr["note"] = note[:200]
        out.append(ptr)
    return out


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

    # Summary PHI gate -- FAIL-CLOSED. is_phi_risk error is treated as PHI (drop).
    # Covers every model-authored field that could carry PHI (title/summary/fix).
    summary_text = (f"{rec.get('title', '')} {rec.get('summary', '')} "
                    f"{rec.get('fix_sketch', '')}").strip()
    try:
        phi = phi_guard.is_phi_risk(summary_text)
    except Exception:  # noqa: BLE001 -- fail closed
        phi = True
    if phi:
        log.info("code_queue: dropped PHI-flagged candidate (signal=%s entity=%s)",
                 rec.get("signal"), entity)
        return None

    rec["evidence"] = _scrub_evidence(rec.get("evidence"), is_lex=is_lex)

    signal = str(rec.get("signal") or "unknown")
    representative = _representative(rec)
    rec["fingerprint"] = _fingerprint(signal, representative)
    class_key = _class_key(str(rec.get("title", "")), str(rec.get("subsystem_guess", "")))

    # PHI-safe persistence of the dedup basis: NEVER store raw LEX or PHI-tripping
    # text in the fingerprint ledger OR the event record. The hash (already
    # computed from the full text) still dedups exact repeats; only fuzzy dedup is
    # forgone for these. D-082 extension.
    try:
        rep_phi = phi_guard.is_phi_risk(representative)
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
            if not (cand and cand.get("status") in _OPEN_STATUSES):
                existing_id = None
        if existing_id is None and emb_id:
            cand = get_item(emb_id)
            if cand and cand.get("status") in _OPEN_STATUSES:
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
                         *, client_factory: Callable | None = None) -> None:
    """Hot-path entry (called from dispatch's timeout + crash arms). Fail-soft:
    NEVER raises, NEVER blocks the dispatch return. No message text is captured
    (evidence is a channel pointer only), so S1 is inherently PHI-safe."""
    try:
        if code_queue_level() == "off":
            return
        _submit(_process_tool_failure, tool_name, entity, error_class,
                channel_id, slack_user_id, is_timeout, client_factory)
    except Exception:  # noqa: BLE001 -- belt-and-braces; capture may never affect the reply
        log.debug("code_queue.capture_tool_failure swallowed", exc_info=True)


def _process_tool_failure(tool_name: str, entity: str, error_class: str,
                          channel_id: str, slack_user_id: str, is_timeout: bool,
                          client_factory: Callable | None) -> None:
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
        "evidence": [{"channel_id": channel_id, "ts": "", "note": "tool failure (no message text)"}],
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
                           *, client_factory: Callable | None = None) -> None:
    """Hot-path entry (called post-reply from _extract_and_log_gap). Detects an
    S2 phrase in the user's message OR an S4 capability deflection in Cora's reply;
    a hit becomes a classifier candidate. Fail-soft, off-thread, dedup-before-model."""
    try:
        if code_queue_level() == "off":
            return
        clean = _strip_quoted(text or "")
        phrase_hit = bool(_PHRASE_RE.search(clean))
        deflect_hit = bool(_DEFLECTION_RE.search(response_text or ""))
        if not (phrase_hit or deflect_hit):
            return
        signal = "phrase" if phrase_hit else "deflection"
        _submit(_process_message_signal, clean, entity, channel_id, channel_name,
                slack_user_id, signal, client_factory)
    except Exception:  # noqa: BLE001
        log.debug("code_queue.capture_message_signal swallowed", exc_info=True)


def _process_message_signal(text: str, entity: str, channel_id: str, channel_name: str,
                            slack_user_id: str, signal: str,
                            client_factory: Callable | None) -> None:
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
    # still pass; PHI-tripping ones are dropped before any model call.
    try:
        if phi_guard.is_phi_risk(question):
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
        "evidence": [{"channel_id": channel_id, "ts": "", "note": question[:400]}],
        "reporter": slack_user_id,
        "fix_sketch": verdict.get("fix_sketch", ""),
    }
    _capture(rec, client_factory=client_factory)


def _route_to_flywheel(question: str, entity: str, channel_name: str, user: str) -> None:
    """Knowledge-shaped finding -> the existing knowledge flywheel, never the queue."""
    try:
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
    if summary and phi_guard.is_phi_risk(summary):
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
_STATUS_ORDER = ["PROPOSED", "APPROVED", "STAGED", "BLOCKED", "SNOOZED",
                 "SHIPPED", "DISMISSED", "SUPERSEDED"]


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
            age = _age_days(it.get("ts"))
            cnt = int(it.get("count", 1))
            cnt_s = f" x{cnt}" if cnt > 1 else ""
            entity = it.get("entity", "?")
            lines.append(
                f"- `{it.get('severity', '?')}` **{it.get('kind', '?')}** "
                f"[{entity}] {it.get('title', '(untitled)')}{cnt_s} "
                f"-- {age}d old (`{it.get('id', '?')}`)"
            )
            if it.get("prompt_path"):
                lines.append(f"    - prompt: `{it['prompt_path']}`")
        lines.append("")
    return "\n".join(lines)


def render_backlog(items: list[dict[str, Any]] | None = None) -> bool:
    """Write the backlog view to the Founder-OS Drive path. Fail-soft: a G: outage
    (DriveUnavailable) or any error returns False and NEVER raises (a button ack
    must not break on a Drive blip -- Rider B / drive_io doctrine)."""
    try:
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


_PROMPT_SYS = """\
You write a paste-ready Code-session kickoff prompt for "Cora" (an internal
Slack AI-assistant codebase). Match this house skeleton EXACTLY:

- A one-line byline pinning Opus-tier + the STANDING OPERATING LOOP, and the
  literal banner "AUTO-GENERATED DRAFT -- VERIFY-FIRST everything", plus a
  suggested branch name `claude/<slug>`.
- Section 0: evidence (the signals below, with any Slack pointers).
- Section 1: deliverable slices (ONE per queued item when bundled).
- Section 2: guardrails to respect (reference Cora doctrine IDs where relevant:
  D-011 no-canon-write, staged-write gate, D-051 adversarial review, PHI D-082).
- Section 3: tests.
- Section 4: live acceptance (Harrison, after merge + restart).
- Section 5: notes incl. restart implications.

Be concise. Do NOT invent facts beyond the evidence. Output MARKDOWN only.
"""


def _deterministic_prompt(items: list[dict[str, Any]], slug: str) -> str:
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
    for it in items:
        lines.append(f"- `{it.get('severity', '?')}` **{it.get('kind', '?')}** "
                     f"[{it.get('entity', '?')}] {it.get('title', '')} (`{it.get('id', '?')}`)")
        if it.get("summary"):
            lines.append(f"    - {it['summary']}")
        for ev in (it.get("evidence") or [])[:3]:
            if ev.get("channel_id") or ev.get("ts"):
                lines.append(f"    - evidence: channel `{ev.get('channel_id', '')}` ts `{ev.get('ts', '')}`")
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
                            meta_out: dict[str, Any] | None = None) -> str | None:
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
        evidence = "\n".join(
            f"- [{it.get('severity')}] {it.get('kind')} [{it.get('entity')}] "
            f"{it.get('title')} :: {it.get('summary', '')} :: fix: {it.get('fix_sketch', '')} "
            f":: id={it.get('id')} :: evidence="
            + "; ".join(f"ch {e.get('channel_id', '')}/ts {e.get('ts', '')}"
                        for e in (it.get('evidence') or [])[:3])
            for it in items
        )
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model=_SONNET_MODEL, max_tokens=2000, system=_PROMPT_SYS,
                messages=[{"role": "user", "content":
                           f"Slug: {slug}\nItems to cover:\n{evidence}"}],
            )
            body = resp.content[0].text.strip()
        except Exception as exc:  # noqa: BLE001 -- fail-soft to the skeleton
            log.warning("code_queue: prompt generation failed, using skeleton: %s", exc)
            body = None
    if not body:
        body = _deterministic_prompt(items, slug)

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


def build_item_card(rec: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """(fallback_text, Block Kit blocks) for one item card. A PROPOSED item gets
    the four Queue / Edit / Dismiss / Later buttons; an already-APPROVED item
    (founder fast-path / friction spillover) gets Stage-prompt / Dismiss. value =
    cq-id."""
    cq_id = str(rec.get("id", ""))
    status = str(rec.get("status", "PROPOSED"))
    ev_lines = []
    for e in (rec.get("evidence") or [])[:3]:
        if e.get("channel_id") or e.get("ts"):
            ev_lines.append(f"<slack://channel?id={e.get('channel_id', '')}> ts `{e.get('ts', '')}`")
    ev_txt = ("\n" + "\n".join(f"> {x}" for x in ev_lines)) if ev_lines else ""
    lead = "queued" if status == "APPROVED" else "new"
    text = (
        f"*Code-session queue* -- {lead} {rec.get('kind', '?')} `{rec.get('severity', '?')}` "
        f"[{rec.get('entity', '?')}]\n"
        f"*{rec.get('title', '(untitled)')}*\n"
        f"{rec.get('summary', '')}"
    )
    if rec.get("fix_sketch"):
        text += f"\n_Fix sketch:_ {rec['fix_sketch']}"
    text += ev_txt
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
def process_queue_action(action_id: str, cq_id: str, actor_id: str) -> tuple[str, str]:
    """Apply a card button action. Returns (outcome, message). Harrison-only
    (org-wide intake, founder-only approval per the locked decision). Idempotent.
    All correctness lives here; the app.py wrapper is only Slack I/O."""
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
        # P0/P1 get a full kickoff prompt immediately -- reservation-guarded so a
        # concurrent approve can't double-generate (defect #3 TOCTOU class).
        if str(rec.get("severity", "")).upper() in ("P0", "P1") and _begin_staging([cq_id]):
            try:
                fresh = get_item(cq_id) or rec
                if not (fresh.get("status") == "STAGED" and fresh.get("prompt_path")):
                    meta: dict[str, Any] = {}
                    path = generate_kickoff_prompt([fresh], meta_out=meta)
                    if path:
                        ev = {"event": "staged", "ts": _now_iso(), "id": cq_id,
                              "prompt_path": path}
                        if meta.get("mis_homed"):
                            ev["mis_homed"] = True
                        _append_event(ev)
                        _render_backlog_safe()
                        _dm_prompt_path(path)
                        msg = f"✅ Queued + prompt staged: `{path}`"
            finally:
                _end_staging([cq_id])
        return "approved", msg

    if action_id == ACTION_DISMISS:
        if status == "DISMISSED":
            return "noop", "Already dismissed."
        _append_event({"event": "dismissed", "ts": _now_iso(), "id": cq_id})
        _render_backlog_safe()
        return "dismissed", "🗑️ Dismissed -- this fingerprint won't resurface."

    if action_id == ACTION_LATER:
        snooze_until = (_now() + timedelta(days=SNOOZE_DAYS)).isoformat()
        _append_event({"event": "snoozed", "ts": _now_iso(), "id": cq_id,
                       "snooze_until": snooze_until})
        _render_backlog_safe()
        return "snoozed", f"⏸ Snoozed {SNOOZE_DAYS}d -- resurfaces in the Monday menu."

    if action_id == ACTION_STAGE:
        if status == "STAGED" and rec.get("prompt_path"):
            return "noop", f"Already staged: `{rec['prompt_path']}`"
        # Reservation guard (defect #3): a re-tap while the first tap is still
        # generating finds the id reserved and returns WITHOUT a second Sonnet call
        # or a second `staged` event.
        if not _begin_staging([cq_id]):
            cur = get_item(cq_id) or rec
            return "noop", (f"Already staged: `{cur['prompt_path']}`" if cur.get("prompt_path")
                            else "Already staging -- I'll post the prompt path when it's ready.")
        try:
            fresh = get_item(cq_id) or rec
            if fresh.get("status") == "STAGED" and fresh.get("prompt_path"):
                return "noop", f"Already staged: `{fresh['prompt_path']}`"
            meta: dict[str, Any] = {}
            path = generate_kickoff_prompt([rec], meta_out=meta)
            if not path:
                return "error", "Prompt generation failed -- nothing staged."
            ev = {"event": "staged", "ts": _now_iso(), "id": cq_id, "prompt_path": path}
            if meta.get("mis_homed"):
                ev["mis_homed"] = True
            _append_event(ev)
            _render_backlog_safe()
            _dm_prompt_path(path)
            return "staged", f"📝 Prompt staged: `{path}`"
        finally:
            _end_staging([cq_id])

    if action_id == ACTION_MARK_SHIPPED:
        _append_event({"event": "shipped", "ts": _now_iso(), "id": cq_id})
        _render_backlog_safe()
        return "shipped", "🚢 Marked shipped."

    if action_id == ACTION_KEEP:
        _append_event({"event": "kept", "ts": _now_iso(), "id": cq_id})
        return "kept", "Kept -- staleness clock reset."

    return "error", f"Unknown action: {action_id}"


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
    # PHI belt-and-braces on the edited text (fail-closed).
    try:
        if phi_guard.is_phi_risk(f"{title} {summary}"):
            return "error", "Edit rejected -- text tripped the PHI guard."
    except Exception:  # noqa: BLE001
        return "error", "Edit rejected -- PHI check failed (fail-closed)."
    _append_event({"event": "edited", "ts": _now_iso(), "id": cq_id,
                   "title": title, "summary": summary})
    _render_backlog_safe()
    return "edited", "✏️ Updated."


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


def queue_explicit(user: str, entity: str, channel_id: str, request: str,
                   is_founder: bool) -> tuple[str | None, str]:
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
    held = (not is_founder) and _explicit_count_today(user) >= EXPLICIT_THROTTLE_PER_DAY
    rec = {
        "kind": "feature", "severity": "P2", "title": request[:120],
        "summary": request[:200], "subsystem_guess": "", "entity": entity,
        "signal": "explicit", "representative": request,
        "evidence": [{"channel_id": channel_id, "ts": "", "note": request[:400]}],
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


def build_weekly_menu() -> tuple[str, list[dict[str, Any]]] | None:
    """(text, blocks) for the Monday menu, or None if there is nothing to show.
    APPROVED items are grouped by AFFINITY (subsystem_guess -> entity) into bundles of
    at most _MAX_BUNDLE_ITEMS; a group of one is listed singly. There is NO kitchen-sink
    "other" bundle (defect #5). Plus config items + a staleness sweep (STAGED >14d and
    expired SNOOZEs)."""
    items = load_items()
    approved = [it for it in items if it.get("status") == "APPROVED" and it.get("kind") != "config"]
    config_items = [it for it in items if it.get("status") == "APPROVED" and it.get("kind") == "config"]

    now = _now()
    # last_touch (a "Keep" tap) takes precedence so Keeping resets the staleness clock.
    stale_staged = [it for it in items if it.get("status") == "STAGED"
                    and _age_days(it.get("last_touch") or it.get("staged_at") or it.get("ts")) >= STALE_STAGED_DAYS]
    expired_snoozed = [it for it in items if it.get("status") == "SNOOZED"
                       and (_parse_ts(it.get("snooze_until")) or now) <= now]

    if not (approved or config_items or stale_staged or expired_snoozed):
        return None

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn",
         "text": "*Cora code-session queue -- Monday menu*"}},
    ]
    text_lines = ["*Cora code-session queue -- Monday menu*"]

    # APPROVED -> affinity bundles (<= _MAX_BUNDLE_ITEMS each), singletons listed singly.
    if approved:
        groups: dict[str, list[dict[str, Any]]] = {}
        for it in approved:
            groups.setdefault(_affinity_key(it), []).append(it)
        # Deterministic order: largest group first, then key. Split each group into
        # chunks of <= _MAX_BUNDLE_ITEMS -- NO cross-affinity merge.
        rows: list[list[dict[str, Any]]] = []
        for _key, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            for start in range(0, len(group), _MAX_BUNDLE_ITEMS):
                rows.append(group[start:start + _MAX_BUNDLE_ITEMS])
        shown, overflow_n = rows[:_MENU_MAX_ROWS], max(0, len(rows) - _MENU_MAX_ROWS)
        for chunk in shown:
            ids_csv = ",".join(str(g.get("id", "")) for g in chunk)
            if len(chunk) == 1:
                it = chunk[0]
                sline = f"• [{it.get('entity', '?')}] {it.get('title', '')} (`{it.get('id', '?')}`)"
                text_lines.append("- " + sline)
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": sline[:2900]}})
                blocks.append({
                    "type": "actions", "block_id": f"cq_single_{it.get('id', '')}"[:255],
                    "elements": [
                        {"type": "button", "action_id": ACTION_STAGE, "style": "primary",
                         "text": {"type": "plain_text", "text": "📝 Stage prompt"},
                         "value": str(it.get("id", ""))},
                    ],
                })
            else:
                theme = _bundle_theme(chunk)
                eff = _effort(len(chunk))
                titles = "; ".join(str(g.get("title", "")) for g in chunk[:6])
                bline = f"*{theme}* ({len(chunk)} items, ~{eff}): {titles}"
                text_lines.append("- " + bline)
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": bline[:2900]}})
                blocks.append({
                    "type": "actions", "block_id": f"cq_bundle_{_slug(theme)}_{_id_suffix(chunk)}"[:255],
                    "elements": [
                        {"type": "button", "action_id": ACTION_STAGE, "style": "primary",
                         "text": {"type": "plain_text", "text": "📝 Stage bundle"},
                         "value": "bundle:" + ids_csv},
                    ],
                })
        if overflow_n:
            oline = (f"_+{overflow_n} more bundle(s) not shown -- review the generated "
                     f"backlog; nothing was dropped._")
            text_lines.append(oline)
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": oline}})

    if config_items:
        cline = "*No Code session needed (config):* " + "; ".join(
            str(c.get("title", "")) for c in config_items[:8])
        text_lines.append(cline)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": cline[:2900]}})

    for it in (stale_staged + expired_snoozed):
        why = "STAGED >14d" if it.get("status") == "STAGED" else "snooze expired"
        sline = f"⏳ {why}: {it.get('title', '')} (`{it.get('id')}`)"
        text_lines.append("- " + sline)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": sline[:2900]}})
        blocks.append({
            "type": "actions", "block_id": f"cq_stale_{it.get('id')}"[:255],
            "elements": [
                {"type": "button", "action_id": ACTION_MARK_SHIPPED,
                 "text": {"type": "plain_text", "text": "🚢 Mark shipped"}, "value": str(it.get("id", ""))},
                {"type": "button", "action_id": ACTION_KEEP,
                 "text": {"type": "plain_text", "text": "Keep"}, "value": str(it.get("id", ""))},
                {"type": "button", "action_id": ACTION_DISMISS,
                 "text": {"type": "plain_text", "text": "🗑️ Dismiss"}, "value": str(it.get("id", ""))},
            ],
        })

    return "\n".join(text_lines), blocks


def maybe_send_weekly_menu(*, client_factory: Callable | None = None) -> bool:
    """Send the Monday menu DM. No-op unless live. Returns True if a DM was sent.
    Gating on the weekday is the CALLER's job (run_knowledge_review._is_digest_day)."""
    if code_queue_level() != "live":
        return False
    try:
        built = build_weekly_menu()
        if built is None:
            return False
        text, blocks = built
        client = (client_factory or _default_client_factory)()
        if client is None:
            return False
        open_resp = client.conversations_open(users=[HARRISON_ID])
        client.chat_postMessage(
            channel=open_resp["channel"]["id"], text=text[:2900], blocks=blocks,
            unfurl_links=False, unfurl_media=False,
        )
        return True
    except Exception:  # noqa: BLE001
        log.warning("code_queue: weekly menu send failed (non-fatal)", exc_info=True)
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
        return "staged", f"📝 Bundle prompt staged ({len(still)} items): `{path}`"
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
    still exists. Never globs or deletes blindly."""
    repo_notes = _NOTES_DIR.resolve()
    plan: list[dict[str, str]] = []
    for cq_id, p in _latest_staged_prompt_paths().items():
        src = Path(p)
        try:
            parents = list(src.resolve().parents)
        except OSError:
            continue
        if repo_notes not in parents:
            continue  # already Founder-OS-homed (or elsewhere) -- leave it
        if "cora-code-prompt" not in src.name:
            continue
        if not src.exists():
            continue
        plan.append({"id": cq_id, "src": str(src),
                     "dst": str(founder_os_notes_dir() / src.name)})
    return plan


def apply_prompt_rehome(plan: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Execute a ``plan_prompt_rehome`` plan: copy src -> Founder-OS ``_notes`` (via
    drive_io), backfill the ledger prompt_path (a ``staged`` event, ``rehomed=True``),
    then delete the repo copy. Best-effort per item -- one failure never aborts the
    rest. Returns the per-item outcomes."""
    done: list[dict[str, Any]] = []
    for a in plan:
        src, dst, cq_id = Path(a["src"]), Path(a["dst"]), a["id"]
        try:
            body = src.read_text(encoding="utf-8", errors="replace")
            drive_io.write_text_atomic(dst, body)
            _append_event({"event": "staged", "ts": _now_iso(), "id": cq_id,
                           "prompt_path": str(dst), "rehomed": True})
            src.unlink()
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
              signal: str, status: str, subsystem_guess: str = "") -> str | None:
    """Directly seed a queue item (no DM, no classifier). Idempotent on fingerprint.
    Used only by the one-shot seed script. Returns the cq-id."""
    rec = {
        "kind": kind, "severity": severity, "title": title, "summary": summary,
        "subsystem_guess": subsystem_guess or entity, "entity": entity, "signal": signal,
        "representative": title,
        "evidence": [{"channel_id": "", "ts": "", "note": summary[:200]}],
        "reporter": HARRISON_ID,
    }
    # Summary PHI gate -- FAIL-CLOSED, mirroring _capture (D-051): the seed persists
    # title/summary RAW (title is the dedup basis; it also renders into the KB-ingested
    # code-session-backlog.md and would egress). A PHI-tripping seed is refused outright,
    # never persisted -- same contract as the capture path (fixes the seed/ _capture
    # asymmetry). The 1h LEX-DDD seed is generic build text and passes cleanly.
    try:
        if phi_guard.is_phi_risk(f"{title} {summary}".strip()):
            log.info("code_queue.seed_item: refused PHI-flagged seed (signal=%s entity=%s)",
                     signal, entity)
            return None
    except Exception:  # noqa: BLE001 -- fail closed
        log.info("code_queue.seed_item: PHI check errored -- refusing seed (fail-closed)")
        return None
    class_key = _class_key(title, subsystem_guess or entity)
    existing = find_fingerprint(signal, title, class_key=class_key)
    if existing:
        return existing
    # PHI-safe persistence (mirror _capture FULLY, D-051 defect A): NEVER persist a raw
    # LEX or PHI-tripping representative -- else it becomes an embedding candidate whose
    # raw text egresses to OpenAI. Exact-hash dedup still works (fingerprint is over
    # `title`). The evidence note is scrubbed on the SAME rule as _capture (LEX -> pointer
    # only; any note that itself trips is_phi_risk -> dropped) -- the seed path previously
    # persisted summary[:200] raw, an at-rest LEX/PHI leak for a LEX seed (1i).
    is_lex = str(entity or "").strip().upper().startswith("LEX")
    rec["evidence"] = _scrub_evidence(rec.get("evidence"), is_lex=is_lex)
    try:
        rep_phi = phi_guard.is_phi_risk(title)
    except Exception:  # noqa: BLE001 -- fail closed
        rep_phi = True
    store_rep = "" if (is_lex or rep_phi) else title
    rec["representative"] = store_rep
    cq_id = "cq-" + uuid.uuid4().hex[:12]
    rec["id"] = cq_id
    rec["ts"] = _now_iso()
    rec["status"] = status
    rec["count"] = 1
    rec["fingerprint"] = _fingerprint(signal, title)
    _append_event({"event": "captured", **rec})
    _append_fingerprint(rec["fingerprint"], signal, store_rep, cq_id, class_key=class_key)
    return cq_id
