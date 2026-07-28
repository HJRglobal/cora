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

log = logging.getLogger("cora.code_queue")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
HARRISON_ID = os.environ.get("HARRISON_SLACK_USER_ID", "U0B2RM2JYJ1")

_HAIKU_MODEL = "claude-haiku-4-5"
_SONNET_MODEL = "claude-sonnet-4-6"

FUZZY_DEDUP_RATIO = 0.85          # same-signal paraphrase-dedup threshold (friction pattern)
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


# ─────────────────────────────────────────────────────────────────────────────
# Rollout flag
# ─────────────────────────────────────────────────────────────────────────────
def code_queue_level() -> str:
    """CORA_CODE_QUEUE: 'off' (default, fully inert), 'log' (capture + ledger +
    backlog, NO DMs), or 'live' (+ immediate DM cards). Unrecognized -> 'off'.
    Read per-call so a flip needs no restart (mirrors autowrite_level())."""
    v = (os.environ.get("CORA_CODE_QUEUE", "off") or "off").strip().lower()
    return v if v in ("off", "log", "live") else "off"


# ─────────────────────────────────────────────────────────────────────────────
# Small utilities
# ─────────────────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _fingerprint(signal: str, representative: str) -> str:
    basis = f"{signal}:{_normalize(representative)}"
    return hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()  # noqa: S324 -- dedup, not security


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
def _append_fingerprint(fp: str, signal: str, representative: str, cq_id: str) -> None:
    _append_jsonl(_FINGERPRINT_LEDGER, {
        "fingerprint": fp,
        "signal": signal,
        "representative": (representative or "")[:300],
        "id": cq_id,
        "ts": _now_iso(),
    })


def find_fingerprint(signal: str, representative: str) -> str | None:
    """Return the cq-id of a prior candidate matching this signal+text (exact
    fingerprint OR same-signal paraphrase >= FUZZY_DEDUP_RATIO), else None."""
    fp = _fingerprint(signal, representative)
    rep = _normalize(representative)
    for entry in _read_jsonl(_FINGERPRINT_LEDGER):
        if entry.get("fingerprint") == fp:
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
             client_factory: Callable | None = None) -> str | None:
    """Deduplicate, PHI-gate, persist, and (if live) DM a card. Returns the cq-id
    (existing on recurrence, new on first sighting) or None if dropped.

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

    existing_id = find_fingerprint(signal, representative)
    if existing_id:
        _append_event({
            "event": "recurrence", "ts": _now_iso(), "id": existing_id,
            "evidence": (rec.get("evidence") or [None])[0],
        })
        _render_backlog_safe()
        if code_queue_level() == "live":
            _thread_count_update(existing_id, client_factory)
        return existing_id

    cq_id = "cq-" + uuid.uuid4().hex[:12]
    rec["id"] = cq_id
    rec["ts"] = _now_iso()
    rec["status"] = initial_status
    rec.setdefault("count", 1)
    _append_event({"event": "captured", **rec})
    _append_fingerprint(rec["fingerprint"], signal, store_rep, cq_id)
    _render_backlog_safe()

    if code_queue_level() == "live":
        _send_new_item_card(rec, client_factory)
    return cq_id


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
        # Remember the fingerprint so identical noise never re-classifies.
        _append_fingerprint(_fingerprint(signal, question), signal, question, "noise")
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
    _record_signal("thumbsdown", basis[:300])
    n = _count_signals("thumbsdown", basis[:300], days=THUMBSDOWN_WINDOW_DAYS, fuzzy=True)
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


def generate_kickoff_prompt(items: list[dict[str, Any]], *, slug: str | None = None) -> str | None:
    """Render a kickoff prompt for one item or a bundle and write it to _notes/.
    Returns the written path (str) or None on write failure. Model call is fail-soft:
    on any Sonnet error a deterministic skeleton is written instead of nothing."""
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

    fname = f"{today}_fndr_cora-code-prompt-{slug}.md"
    try:
        _NOTES_DIR.mkdir(parents=True, exist_ok=True)
        path = _NOTES_DIR / fname
        path.write_text(body, encoding="utf-8")
        return str(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("code_queue: prompt file write failed: %s", exc)
        return None


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


def _send_new_item_card(rec: dict[str, Any], client_factory: Callable | None) -> None:
    """Send one new-item DM card to Harrison, respecting the 5/day storm cap.
    Over cap -> mark dm_held (the overflow flush delivers a '+N more' card)."""
    try:
        if _dm_sent_today() >= MAX_DM_PER_DAY:
            _append_event({"event": "dm_held", "ts": _now_iso(), "id": rec["id"]})
            log.info("code_queue: DM cap hit -- holding item %s for overflow flush", rec["id"])
            return
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
        if status == "APPROVED":
            return "noop", "Already queued."
        if status in ("DISMISSED", "SHIPPED", "SUPERSEDED"):
            return "noop", f"Item is {status} -- not re-queuing."
        _append_event({"event": "approved", "ts": _now_iso(), "id": cq_id})
        _render_backlog_safe()
        msg = "✅ Queued (APPROVED)."
        # P0/P1 get a full kickoff prompt immediately.
        if str(rec.get("severity", "")).upper() in ("P0", "P1"):
            fresh = get_item(cq_id) or rec
            path = generate_kickoff_prompt([fresh])
            if path:
                _append_event({"event": "staged", "ts": _now_iso(), "id": cq_id,
                               "prompt_path": path})
                _render_backlog_safe()
                _dm_prompt_path(path)
                msg = f"✅ Queued + prompt staged: `{path}`"
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
        path = generate_kickoff_prompt([rec])
        if not path:
            return "error", "Prompt generation failed -- nothing staged."
        _append_event({"event": "staged", "ts": _now_iso(), "id": cq_id, "prompt_path": path})
        _render_backlog_safe()
        _dm_prompt_path(path)
        return "staged", f"📝 Prompt staged: `{path}`"

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
                   is_founder: bool) -> str | None:
    """Backend for the explicit tool's confirmed call. Founder -> APPROVED
    fast-path; teammate -> PROPOSED. Returns cq-id (or existing on dedup)."""
    request = (request or "").strip()
    if not request:
        return None
    if _explicit_count_today(user) >= EXPLICIT_THROTTLE_PER_DAY:
        return None
    rec = {
        "kind": "feature", "severity": "P2", "title": request[:120],
        "summary": request[:200], "subsystem_guess": "", "entity": entity,
        "signal": "explicit", "representative": request,
        "evidence": [{"channel_id": channel_id, "ts": "", "note": request[:400]}],
        "reporter": user,
    }
    return _capture(rec, initial_status="APPROVED" if is_founder else "PROPOSED")


# ─────────────────────────────────────────────────────────────────────────────
# Monday menu (rides run_knowledge_review; behind _is_digest_day; zero new tasks)
# ─────────────────────────────────────────────────────────────────────────────
def _effort(n: int) -> str:
    return "S" if n <= 2 else ("M" if n <= 4 else "L")


def build_weekly_menu() -> tuple[str, list[dict[str, Any]]] | None:
    """(text, blocks) for the Monday menu, or None if there is nothing to show.
    APPROVED items grouped into <=3 subsystem bundles + config items + a staleness
    sweep (STAGED >14d and expired SNOOZEs)."""
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

    # APPROVED -> <=3 subsystem bundles.
    if approved:
        bundles: dict[str, list[dict[str, Any]]] = {}
        for it in approved:
            key = str(it.get("subsystem_guess") or it.get("entity") or "general")
            bundles.setdefault(key, []).append(it)
        ordered = sorted(bundles.items(), key=lambda kv: -len(kv[1]))
        top = ordered[:3]
        overflow = ordered[3:]
        if overflow:
            merged: list[dict[str, Any]] = []
            for _, v in overflow:
                merged.extend(v)
            top.append(("other", merged))
        for key, group in top:
            eff = _effort(len(group))
            titles = "; ".join(str(g.get("title", "")) for g in group[:6])
            bline = f"*{key}* ({len(group)} item(s), ~{eff}): {titles}"
            text_lines.append("- " + bline)
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": bline[:2900]}})
            blocks.append({
                "type": "actions", "block_id": f"cq_bundle_{_slug(key)}"[:255],
                "elements": [
                    {"type": "button", "action_id": ACTION_STAGE, "style": "primary",
                     "text": {"type": "plain_text", "text": "📝 Stage bundle"},
                     "value": "bundle:" + ",".join(str(g.get("id", "")) for g in group)},
                ],
            })

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
    recs = [get_item(i) for i in ids]
    recs = [r for r in recs if r]
    if not recs:
        return "error", "No items to stage."
    slug = _slug(str(recs[0].get("subsystem_guess") or recs[0].get("entity") or "bundle"))
    path = generate_kickoff_prompt(recs, slug=f"{slug}-bundle")
    if not path:
        return "error", "Prompt generation failed -- nothing staged."
    bundle_id = "bnd-" + uuid.uuid4().hex[:8]
    for r in recs:
        _append_event({"event": "staged", "ts": _now_iso(), "id": r["id"],
                       "prompt_path": path, "bundle_id": bundle_id})
    _render_backlog_safe()
    _dm_prompt_path(path)
    return "staged", f"📝 Bundle prompt staged ({len(recs)} items): `{path}`"


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
    existing = find_fingerprint(signal, title)
    if existing:
        return existing
    cq_id = "cq-" + uuid.uuid4().hex[:12]
    rec["id"] = cq_id
    rec["ts"] = _now_iso()
    rec["status"] = status
    rec["count"] = 1
    rec["fingerprint"] = _fingerprint(signal, title)
    _append_event({"event": "captured", **rec})
    _append_fingerprint(rec["fingerprint"], signal, title, cq_id)
    return cq_id
