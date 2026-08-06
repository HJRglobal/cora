"""Cora-owned NON-canon decisions inbox (decisions lane, Fork 4, 2026-08-01).

decision_capture proposals surface to Harrison as never-expiring one-tap cards
(run_knowledge_review drain + knowledge_review.build_decision_blocks). An Accept
files the decision HERE -- the inbox .md + a jsonl ledger -- and NOWHERE else.

HARD INVARIANTS (locked design, TOM 1uuu Fork 4):
  * NON-canon: this module never writes any canon decision log (the founder
    memory log, the repo log, or the pending-decisions file strategy_memo
    reads). Promotion into canon stays the Cowork cascade on Harrison's
    explicit thumbs-up (D-011 untouched). A source-scan test pins this.
  * Never-autowrite-by-TYPE: the ONLY callers of apply_decision_accept are the
    Harrison-gated tap/reaction paths (process_decision_tap, the scheduled
    executor's decision branch). apply_knowledge_update deliberately does NOT
    know this type, so the graduated-trust autowrite path (which reuses it)
    structurally cannot file a decision.
  * LEX/PHI hard-excluded FAIL-CLOSED: screen_decision() is checked at
    card-render time (drain) AND re-checked here at the durable write. Any
    screening error counts as excluded.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Default locations. data/ is never KB-swept and data/state/ is gitignored, so
# accepted-but-not-promoted decisions can't leak into retrieval as if canon.
_DEFAULT_INBOX_PATH = _REPO_ROOT / "data" / "decisions-inbox.md"
_DEFAULT_LEDGER_PATH = _REPO_ROOT / "data" / "state" / "decisions-inbox-ledger.jsonl"

_INBOX_LOCK = Lock()

_INBOX_HEADER = """# Cora Decisions Inbox -- NON-CANON

<!-- Cora-owned. Accepted decision_capture cards land here (one-tap lane,
     Fork 4 2026-08-01). This file is NOT canon: promotion into the founder
     decisions.md happens ONLY via the Cowork cascade on Harrison's explicit
     thumbs-up (D-011). Do not treat entries here as decided doctrine, and do
     not KB-ingest this file. -->
"""

# Conservative LEX token match for content-level detection (the 65-row pass5
# payload shape carries entity only as a "[HJRP]"-style description prefix, so
# an entity-field check alone is blind). Word-bounded: "complex"/"flex" do not
# match; "LEX", "lex-llc", "LEX_LLC", "[LEX-LTS]", "Lexington",
# "LexingtonServices" do. D-051 (lex-subentity-token-blind): the distinctive
# Lexington sub-entity codes (LBHS/LTS/LLA) are matched WITHOUT a LEX prefix
# too -- a decision mined from a non-LEX-tagged chunk can reference the
# program only by sub-entity code. "LLC" alone is deliberately NOT matched
# (every HJR entity is an LLC). Erring toward exclusion is the safe direction
# for this surface.
_LEX_TOKEN_RE = re.compile(
    r"(?i)\blex(?:[-_][a-z0-9]+)*\b|\blexington[a-z]*\b|\b(?:lbhs|lts|lla)\b")

_ENTITY_PREFIX_RE = re.compile(r"^\s*\[([A-Za-z0-9-]{2,12})\]")


def _inbox_path() -> Path:
    p = os.environ.get("CORA_DECISIONS_INBOX_PATH", "")
    return Path(p) if p else _DEFAULT_INBOX_PATH


def _ledger_path() -> Path:
    p = os.environ.get("CORA_DECISIONS_INBOX_LEDGER", "")
    return Path(p) if p else _DEFAULT_LEDGER_PATH


def decision_text(update: dict[str, Any]) -> str:
    """The decision body: reconciliation pass-3 payloads carry decision_text,
    older shapes formatted_entry; the pass-5 shape has an empty payload and the
    text lives in description."""
    payload = (update or {}).get("payload") or {}
    return str(
        payload.get("decision_text")
        or payload.get("formatted_entry")
        or (update or {}).get("description")
        or ""
    ).strip()


def entity_of(update: dict[str, Any]) -> str:
    """Best-effort entity: payload.entity, else the leading "[ENTITY]" prefix
    of the description (the pass-5 shape). "" when undeterminable."""
    payload = (update or {}).get("payload") or {}
    ent = str(payload.get("entity") or "").strip()
    if ent:
        return ent.upper()
    m = _ENTITY_PREFIX_RE.match(str((update or {}).get("description") or ""))
    return m.group(1).upper() if m else ""


def _screen_text(update: dict[str, Any]) -> str:
    """Everything worth screening, concatenated: description, evidence, and the
    full payload serialization (catches LEX signals in chunk_title/source_id/
    subject/detail regardless of producer shape)."""
    payload = (update or {}).get("payload") or {}
    try:
        payload_s = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        payload_s = str(payload)
    return " ".join(
        str(x) for x in (
            (update or {}).get("description", ""),
            (update or {}).get("source_evidence", ""),
            payload_s,
        )
    )


def screen_decision(update: dict[str, Any]) -> tuple[bool, str]:
    """(excluded, reason) for a decision_capture item. FAIL-CLOSED: any error in
    screening counts as excluded. Reasons: lex_entity | lex_token | phi | qa |
    screen_error | "" (not excluded).

    Checked at BOTH chokepoints: the drain before a card renders, and
    apply_decision_accept before the durable write."""
    try:
        ent = entity_of(update)
        if ent.startswith("LEX"):
            return True, "lex_entity"
        text = _screen_text(update)
        if _LEX_TOKEN_RE.search(text):
            return True, "lex_token"
        # D-104 [QA] quarantine (2026-08-06). Uses the ANYWHERE predicate, not the
        # prefix one: by the time a decision reaches this screen the text is a
        # derived summary plus a serialized payload, so the original prefix
        # position is gone. A decision mined out of smoke traffic is never canon,
        # so over-excluding here is the cheap direction.
        from . import qa_scaffolding
        if qa_scaffolding.contains_qa_marker(text):
            return True, "qa"
        from .phi_guard import is_any_phi
        if is_any_phi(text):
            return True, "phi"
        return False, ""
    except Exception:  # noqa: BLE001 -- fail closed: never render/file on error
        log.warning("decision_inbox: screen error -- excluding fail-closed",
                    exc_info=True)
        return True, "screen_error"


def _az_date(now: datetime | None = None) -> str:
    """Arizona (no-DST, fixed UTC-7) calendar date -- matches the
    strategy_memo/_is_digest_day fixed-offset convention."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone(timedelta(hours=-7))).strftime("%Y-%m-%d")


def _ledger_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                uid = rec.get("update_id")
                if uid:
                    ids.add(str(uid))
    except FileNotFoundError:
        pass
    return ids


def _uid_marker(uid: str) -> str:
    return f"<!-- decision-inbox-id: {uid} -->"


# D-051 (cross-process-duplicate-inbox-filing): the bot tap and the scheduled
# executor run in SEPARATE processes, so _INBOX_LOCK alone cannot serialize the
# check-then-append idempotency section. A best-effort O_CREAT|O_EXCL lockfile
# (the _acquire_run_lock pattern) closes the cross-process window; failure to
# acquire returns a retryable refusal rather than risking a double-file.
_XPROC_LOCK_STALE_S = 60.0
_XPROC_LOCK_TIMEOUT_S = 3.0


def _xproc_lock_path() -> Path:
    return _ledger_path().parent / "decisions-inbox.lock"


def _acquire_xproc_lock() -> bool:
    lock = _xproc_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _XPROC_LOCK_TIMEOUT_S
    while True:
        try:
            age = time.time() - lock.stat().st_mtime
            if age > _XPROC_LOCK_STALE_S:
                log.warning("decision_inbox: clearing stale inbox lock (age %.0fs)", age)
                lock.unlink()
        except OSError:
            pass
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)
        except OSError:
            return False


def _release_xproc_lock() -> None:
    try:
        _xproc_lock_path().unlink()
    except OSError:
        pass


def apply_decision_accept(update: dict[str, Any],
                          *, via: str = "one_tap_button") -> tuple[bool, str]:
    """File an accepted decision into the NON-canon inbox. Returns (ok, summary).
    Never raises (outer belt: ANY unexpected error -> retryable (False, ...)).

    * Re-screens LEX/PHI fail-closed (ok=False, summary starts "excluded:").
    * Idempotent by update_id (crash-recovery / double-path safe): a repeat call
      returns ok=True without duplicating. Write order is inbox-md FIRST then
      ledger; a crash between the two converges on retry because the md append
      is skipped when the uid marker is already present.
    * Cross-process safe: the check-then-append runs under an O_EXCL lockfile
      in addition to the in-process _INBOX_LOCK; a busy lock returns a
      retryable (False, ...) -- callers leave the row PENDING and retry.
    """
    try:
        return _apply_decision_accept_inner(update, via=via)
    except Exception as exc:  # noqa: BLE001 -- never-raises belt (D-051)
        log.error("decision_inbox: unexpected apply error: %s", exc, exc_info=True)
        return False, f"apply failed: {exc}"


def _apply_decision_accept_inner(update: dict[str, Any],
                                 *, via: str) -> tuple[bool, str]:
    uid = str((update or {}).get("update_id") or "").strip()
    if not uid:
        return False, "missing update_id"
    excluded, reason = screen_decision(update)
    if excluded:
        return False, f"excluded: {reason} (LEX/PHI hard-exclusion, fail-closed)"

    text = decision_text(update)
    if not text:
        return False, "empty decision text"
    ent = entity_of(update)
    evidence = str((update or {}).get("source_evidence") or "").strip()
    try:  # cosmetic: resolve raw <U...> tokens for the Harrison-facing file
        from .tools.user_identity import resolve_slack_mentions
        text = resolve_slack_mentions(text)
        evidence = resolve_slack_mentions(evidence)
    except Exception:  # noqa: BLE001
        pass

    now_iso = datetime.now(timezone.utc).isoformat()
    inbox = _inbox_path()
    ledger = _ledger_path()
    got_lock = False
    try:
        with _INBOX_LOCK:
            got_lock = _acquire_xproc_lock()
            if not got_lock:
                return False, "inbox busy (another process is filing) -- retry"
            if uid in _ledger_ids(ledger):
                return True, "already filed (idempotent no-op)"

            inbox.parent.mkdir(parents=True, exist_ok=True)
            existing = ""
            if inbox.exists():
                existing = inbox.read_text(encoding="utf-8")
            if _uid_marker(uid) not in existing:
                lines = []
                if not existing.strip():
                    lines.append(_INBOX_HEADER)
                lines.append(f"\n## {_az_date()} {('[' + ent + '] ') if ent else ''}"
                             f"{text[:120]}")
                if len(text) > 120:
                    lines.append(f"\n{text}")
                if evidence:
                    lines.append(f"\n- evidence: {evidence[:400]}")
                lines.append(f"- accepted: {now_iso} via {via}")
                lines.append(f"- {_uid_marker(uid)}")
                with inbox.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")

            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "update_id": uid,
                    "ts": now_iso,
                    "entity": ent,
                    "via": via,
                    "description": str((update or {}).get("description") or "")[:300],
                }, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 -- "never raises" must hold for ANY
        # error here, not just OSError: a hand-edited non-UTF-8 inbox raises
        # UnicodeDecodeError (a ValueError), which previously escaped (D-051).
        log.error("decision_inbox: filing failed for %s: %s", uid[:12], exc,
                  exc_info=True)
        return False, f"inbox write failed: {exc}"
    finally:
        if got_lock:
            _release_xproc_lock()

    log.info("decision_inbox: FILED %s entity=%s via=%s", uid[:12], ent or "?", via)
    return True, f"filed to {inbox.name}" + (f" [{ent}]" if ent else "")


def inbox_stats(now: datetime | None = None, days: int = 7) -> dict[str, int]:
    """{total, recent} accepted-decision counts from the ledger. Never raises
    (digest consumer is fail-soft); errors read as zeros."""
    total = 0
    recent = 0
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - days * 86400
    try:
        with _ledger_path().open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                try:
                    if datetime.fromisoformat(str(rec.get("ts", ""))).timestamp() >= cutoff:
                        recent += 1
                except ValueError:
                    pass
    except Exception:  # noqa: BLE001 -- never-raises incl. decode errors (D-051)
        pass
    return {"total": total, "recent": recent}


def filed_update_ids() -> set[str]:
    """update_ids already filed to the inbox ledger. Consumed by the drain's
    cross-process self-heal (a PENDING decision row whose id is filed had its
    resolution clobbered by a concurrent ledger rewrite). Never raises."""
    try:
        return _ledger_ids(_ledger_path())
    except Exception:  # noqa: BLE001
        return set()
