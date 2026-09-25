"""State of the dead-channel lane (Code #16 C1): the proposals event log, the
archive ledger, per-channel claims and keep windows, the cross-process scan lock
and the demotion write/clear helpers.

THE PROPOSALS STORE (``data/state/channel-archive-proposals.jsonl``) is an
APPEND-ONLY event log with a terminal-sticky fold, copied from
f3e_blog/publish_cards.py: the monthly SCRIPT stages cards and the always-on BOT
resolves taps, two processes, so there is no read-modify-write anywhere -- a writer
only adds a line, and the fold refuses to move a row out of a terminal state.

THE ARCHIVE LEDGER (``logs/channel-archive-ledger.jsonl``) holds ONLY archive
attempts: an ``intent`` row written, flushed and fsynced BEFORE any Slack write --
``append_ledger`` returns False when it did not land and the tap then REFUSES to act
-- and an ``outcome`` row after (fail-soft). The monitor adds ``unarchived_seen`` and
reconciled outcomes; Harrison's ``--clear-demotion`` adds ``acknowledged``.

CLAIMS are keyed by CHANNEL across proposals (A13): a ``claimed`` event carries the
channel id, and the fold expires it (reader-side, lesson 26): claimed for more than
10 minutes with no ledger intent -> released; claimed + an intent with no outcome ->
``unknown`` (locked until the monitor reconciles against Slack).

Every path resolves PER CALL from an env var (tests/conftest.py redirects each one).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import policy

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AZ = timezone(timedelta(hours=-7))

EXPIRY_DAYS = 14
KEEP_WINDOW_DAYS = 90
KEEP_CAP = 2
CLAIM_TTL_S = 600
#: A13: a claim with a ledger intent but no outcome YET is an archive in flight for this
#: long (the no-retry write client's 30 s timeout on the notice and on the archive, plus
#: the read-back) -- CLAIMED, "In progress…" -- and only then a locked UNKNOWN.
INFLIGHT_S = 120
SCAN_LOCK_STALE_S = 1800
DAY_S = 86400.0

# Row states. TERMINAL rows never move again (a reconcile may resolve `unknown`).
OPEN, CLAIMED, AGREED, KEPT, ARCHIVED, ALREADY_ARCHIVED, FAILED, UNKNOWN, STALE = (
    "open", "claimed", "agreed", "kept", "archived", "already_archived", "failed",
    "unknown", "stale_refused")
TERMINAL: frozenset = frozenset({AGREED, KEPT, ARCHIVED, ALREADY_ARCHIVED, STALE, UNKNOWN})
_ROW_EVENTS: frozenset = frozenset({CLAIMED, AGREED, KEPT, ARCHIVED, ALREADY_ARCHIVED,
                                    FAILED, UNKNOWN, STALE, "released", "reconciled"})
#: A proposal-level event: a tap / re-render saw the lane demoted after this card (A12).
DEMOTED_SEEN = "demoted_seen"

#: Harrison's registry re-baseline (run_channel_archive_proposal.py --rebaseline-registry).
REBASELINE_EVENT = "registry_rebaselined"

_APPEND_LOCK = threading.Lock()
#: THE claim lock: every Mark / Keep / Archive decision is check-then-write inside
#: ONE acquisition of this lock (lesson 12). Lock order: the app's render lock is
#: never held while this one is taken.
CLAIM_LOCK = threading.Lock()


def _now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, _AZ).isoformat(
        timespec="seconds")


def store_path() -> Path:
    raw = str(os.environ.get("CORA_CHANNEL_ARCHIVE_STORE_PATH", "") or "").strip()
    return Path(raw) if raw else _REPO_ROOT / "data" / "state" / "channel-archive-proposals.jsonl"


def ledger_path() -> Path:
    raw = str(os.environ.get("CORA_CHANNEL_ARCHIVE_LEDGER_PATH", "") or "").strip()
    return Path(raw) if raw else _REPO_ROOT / "logs" / "channel-archive-ledger.jsonl"


def scan_lock_path() -> Path:
    raw = str(os.environ.get("CORA_CHANNEL_ARCHIVE_SCAN_LOCK_PATH", "") or "").strip()
    return Path(raw) if raw else _REPO_ROOT / "data" / "state" / "channel-archive-scan.lock"


def mint_proposal_id() -> str:
    return "chanarch-" + secrets.token_hex(6)


# ── appends ──────────────────────────────────────────────────────────────────
def _append_line(path: Path, row: dict, *, fsync: bool) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _APPEND_LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                if fsync:
                    os.fsync(fh.fileno())
    except Exception:  # noqa: BLE001 -- the caller decides what a failed append means
        log.error("channel_archive: append to %s failed", path.name, exc_info=True)
        return False
    return True


def append_event(event: str, **fields: Any) -> bool:
    """One proposals-store event. Returns False when it did not land."""
    ts = fields.pop("ts", None)
    ts = time.time() if ts is None else float(ts)
    row = {"event": event, "ts": ts, "at": _now_iso(ts)}
    row.update({k: v for k, v in fields.items() if v is not None})
    return _append_line(store_path(), row, fsync=False)


def append_ledger(event: str, **fields: Any) -> bool:
    """One archive-ledger row, flushed AND fsynced. False = it did not land, and a
    caller whose next step is irreversible must refuse to take it."""
    ts = fields.pop("ts", None)
    ts = time.time() if ts is None else float(ts)
    row = {"event": event, "ts": ts, "at": _now_iso(ts)}
    row.update({k: v for k, v in fields.items() if v is not None})
    return _append_line(ledger_path(), row, fsync=True)


def _read_jsonl(path: Path) -> tuple[list[dict], int] | None:
    """(rows, bad_line_count); [] for a missing file; None when unreadable."""
    try:
        if not path.exists():
            return [], 0
        raw = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.error("channel_archive: %s unreadable", path.name, exc_info=True)
        return None
    rows: list[dict] = []
    bad = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    if bad:
        log.error("channel_archive: %s has %d unreadable line(s)", path.name, bad)
    return rows, bad


def read_events() -> list[dict] | None:
    got = _read_jsonl(store_path())
    return None if got is None else got[0]


def read_ledger() -> list[dict] | None:
    got = _read_jsonl(ledger_path())
    return None if got is None else got[0]


# ── the fold ─────────────────────────────────────────────────────────────────
@dataclass
class Proposal:
    proposal_id: str
    created: float
    expires: float
    trigger: str = ""
    tier_at_stage: str = "T0"
    blind: str | None = None
    blind_detail: str = ""
    scanned: int = 0
    counts: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)
    registry_count: int = 0
    n_pages: int = 0                                # pages the card was split into (0 = legacy/unknown)
    pages: dict = field(default_factory=dict)      # page -> {dm_channel, message_ts, rendered_cids, buttons}
    row_state: dict = field(default_factory=dict)  # cid -> {"state", "by", "ts", "override", "code", ...}
    card_agreed_by: str = ""
    card_agreed_ts: float | None = None
    #: A12: a demotion NEWER THAN THE CARD was seen -- a ``demoted_seen`` store event
    #: (a tap or re-render observed the demotion file) or an ``acknowledged`` ledger
    #: row whose ``demoted_since`` is at/after the card's creation. From then on every
    #: T1 row of this card acts T0-equivalent FOR GOOD, even after Harrison clears.
    demoted: bool = False

    @property
    def has_t1_rows(self) -> bool:
        return any(isinstance(r, dict) and r.get("tier") == "T1" for r in self.rows)

    @property
    def rows_by_cid(self) -> dict:
        return {r.get("cid"): r for r in self.rows if isinstance(r, dict)}

    @property
    def delivered(self) -> bool:
        return any(p.get("message_ts") for p in self.pages.values())

    @property
    def fully_delivered(self) -> bool:
        """Every page of the card posted (c1-state-machine#6). A legacy staged event
        without ``n_pages`` falls back to "any page posted"."""
        if self.n_pages <= 0:
            return self.delivered
        return all((self.pages.get(n) or {}).get("message_ts") for n in range(1, self.n_pages + 1))

    def state_of(self, cid: str) -> str:
        return str((self.row_state.get(cid) or {}).get("state") or OPEN)

    def dm_channel(self) -> str:
        for p in self.pages.values():
            if p.get("dm_channel"):
                return str(p["dm_channel"])
        return ""


@dataclass
class Fold:
    proposals: dict = field(default_factory=dict)   # pid -> Proposal
    order: list = field(default_factory=list)       # pids by created
    keeps: dict = field(default_factory=dict)       # cid -> [kept ts...]
    last_registry_count: int = 0
    scans_started: list = field(default_factory=list)
    staged_scan_ids: set = field(default_factory=set)
    ok: bool = True

    def latest(self) -> Proposal | None:
        return self.proposals[self.order[-1]] if self.order else None

    def superseded_by(self, pid: str) -> Proposal | None:
        """The newer FULLY DELIVERED proposal with rows that supersedes *pid* (A14), or
        None. A blind, empty, undelivered or only partly delivered scan retires nothing:
        rows on its missing pages would be actionable from neither card."""
        p = self.proposals.get(pid)
        if p is None:
            return None
        for other_id in reversed(self.order):
            o = self.proposals[other_id]
            if o.proposal_id == pid:
                return None
            if o.created > p.created and o.rows and o.fully_delivered:
                return o
        return None

    def is_live(self, pid: str, now: float) -> bool:
        p = self.proposals.get(pid)
        return bool(p) and now < p.expires and self.superseded_by(pid) is None

    def live_proposal(self, now: float) -> Proposal | None:
        for pid in reversed(self.order):
            if self.is_live(pid, now) and self.proposals[pid].delivered:
                return self.proposals[pid]
        return None

    def channel_claim(self, cid: str) -> tuple[str, str] | None:
        """(proposal_id, kind) of a LIVE claim on *cid* in any proposal, else None."""
        for pid in self.order:
            st = self.proposals[pid].row_state.get(cid) or {}
            if st.get("state") == CLAIMED:
                return pid, str(st.get("kind") or "")
        return None

    def keep_state(self, now: float) -> dict:
        out: dict = {}
        for cid, tss in self.keeps.items():
            if tss:
                out[cid] = {"count": len(tss), "until": max(tss) + KEEP_WINDOW_DAYS * DAY_S}
        return out


def fold(events: list[dict] | None = None, ledger: list[dict] | None = None, *,
         now: float | None = None) -> Fold:
    """Replay the store (+ the ledger, for claim expiry) into state. A store that
    cannot be read returns ``Fold(ok=False)`` -- callers refuse to act on it. An archive
    ledger that cannot be read is NOT an empty one: the fold still builds the proposals
    (a card can render) but is ``ok=False`` (nothing acts) and no claim is expired."""
    now = time.time() if now is None else float(now)
    if events is None:
        events = read_events()
    if ledger is None:
        ledger = read_ledger()
    f = Fold()
    if events is None:
        f.ok = False
        return f
    ledger_ok = ledger is not None
    if not ledger_ok:
        f.ok = False
    intents: dict[tuple, list[float]] = {}
    outcomes: dict[tuple, list[tuple[float, str]]] = {}
    demotion_marks: list[float] = []     # A12: every card created at/before one is T0 for good
    for r in ledger or []:
        key = (str(r.get("proposal_id") or ""), str(r.get("channel_id") or ""))
        if r.get("event") == "intent":
            intents.setdefault(key, []).append(float(r.get("ts") or 0))
        elif r.get("event") == "outcome":
            outcomes.setdefault(key, []).append((float(r.get("ts") or 0), str(r.get("outcome") or "")))
        elif r.get("event") == "acknowledged":
            demotion_marks.append(_demotion_mark(r))
    demoted_seen: set[str] = set()
    for e in events:
        ev = e.get("event")
        pid = str(e.get("proposal_id") or "")
        ts = float(e.get("ts") or 0)
        if ev == "scan_started":
            f.scans_started.append(e)
            continue
        if ev == DEMOTED_SEEN:
            demoted_seen.add(pid)
            continue
        if ev == REBASELINE_EVENT:
            # Harrison's re-baseline after a legitimate registry trim (D-051 r1
            # registry-ops#1): the registry floor follows it until the next good scan
            try:
                n = int(e.get("registry_count") or 0)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                f.last_registry_count = n
            continue
        if ev == "staged":
            if not pid or pid in f.proposals:
                continue          # a duplicate staged line never resets a card
            p = Proposal(
                proposal_id=pid, created=ts,
                expires=float(e.get("expires_ts") or ts + EXPIRY_DAYS * DAY_S),
                trigger=str(e.get("trigger") or ""),
                tier_at_stage="T1" if e.get("tier_at_stage") == "T1" else "T0",
                blind=e.get("blind") or None, blind_detail=str(e.get("blind_detail") or ""),
                scanned=int(e.get("scanned") or 0), counts=dict(e.get("counts") or {}),
                rows=[r for r in (e.get("rows") or []) if isinstance(r, dict)],
                registry_count=int(e.get("registry_count") or 0),
                n_pages=int(e.get("n_pages") or 0),
            )
            f.proposals[pid] = p
            f.order.append(pid)
            if p.registry_count:
                f.last_registry_count = p.registry_count
            if e.get("scan_id"):
                f.staged_scan_ids.add(str(e["scan_id"]))
            continue
        p = f.proposals.get(pid)
        if ev == "kept" and e.get("cid"):
            f.keeps.setdefault(str(e["cid"]), []).append(ts)
        if p is None:
            continue
        if ev == "delivered":
            page = int(e.get("page") or 1)
            p.pages[page] = {"dm_channel": e.get("dm_channel") or "",
                             "message_ts": e.get("message_ts") or "",
                             "rendered_cids": list(e.get("rendered_cids") or []),
                             "buttons": bool(e.get("buttons"))}
            continue
        if ev == "card_agreed":
            if not p.card_agreed_by:
                p.card_agreed_by = str(e.get("by") or "")
                p.card_agreed_ts = ts
            continue
        if ev not in _ROW_EVENTS:
            continue
        cid = str(e.get("cid") or "")
        if not cid or cid not in p.rows_by_cid:
            continue
        cur = p.row_state.get(cid) or {"state": OPEN}
        if ev == "reconciled":
            # the monitor settled an attempt Slack has now decided: a locked UNKNOWN, a
            # claim whose process died between the intent and the outcome, or a FAILED
            # row Slack shows Cora archived after all (D-051 r1 c1-monitor#1/#3)
            rec = e.get("outcome")
            if cur.get("state") in (UNKNOWN, CLAIMED) or (cur.get("state") == FAILED
                                                           and rec == ARCHIVED):
                new = rec if rec in (ARCHIVED, ALREADY_ARCHIVED) else FAILED
                p.row_state[cid] = {**cur, "state": new, "reconciled": True, "ts": ts,
                                    "code": e.get("code") or cur.get("code")}
            continue
        if cur.get("state") in TERMINAL:
            continue          # terminal is sticky
        if ev == "released":
            p.row_state[cid] = {"state": OPEN, "ts": ts, "code": e.get("code")}
            continue
        p.row_state[cid] = {"state": ev, "by": e.get("by") or cur.get("by"), "ts": ts,
                            "override": bool(e.get("override")), "code": e.get("code"),
                            "kind": e.get("kind"), "age_days": e.get("age_days")}
    # A12 demotion history: a card that outlived a demotion stays T0-equivalent for good
    for p in f.proposals.values():
        if p.proposal_id in demoted_seen or any(m >= p.created for m in demotion_marks):
            p.demoted = True
    # reader-side claim expiry (A13) -- never from a ledger we could not read
    for p in (f.proposals.values() if ledger_ok else ()):
        for cid, st in list(p.row_state.items()):
            if st.get("state") != CLAIMED:
                continue
            key = (p.proposal_id, cid)
            cts = float(st.get("ts") or 0)
            # only THIS claim's intents: an earlier attempt's intent (even 0.5 s before a
            # retry's claim) must never shadow the retry as that attempt's outcome
            its = [t for t in intents.get(key, []) if t >= cts]
            if not its:
                if now - cts > CLAIM_TTL_S:
                    p.row_state[cid] = {"state": OPEN, "ts": now, "code": "claim_expired"}
                continue
            outs = [o for t, o in outcomes.get(key, []) if t >= min(its)]
            if not outs:
                if now - max(its) >= INFLIGHT_S:
                    p.row_state[cid] = {**st, "state": UNKNOWN, "code": "no_outcome"}
                # else: an archive in flight -- stays CLAIMED ("In progress…")
            else:
                p.row_state[cid] = {**st, "state": _state_from_outcome(outs[-1]),
                                    "code": outs[-1]}
    return f


def _demotion_mark(row: dict) -> float:
    """The epoch a cleared demotion began, from its ``acknowledged`` ledger row: the
    demotion record's ``demoted_since`` (ISO) when it parses, else the clear's own ts --
    always at/after the real start, so the fallback only ever makes MORE cards T0."""
    raw = row.get("demoted_since")
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_AZ)
            return dt.timestamp()
        except ValueError:
            pass
    try:
        return float(row.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def note_demoted(p: Proposal, *, now: float | None = None) -> bool:
    """A12: a tap or a re-render just observed the demotion file while *p* has T1
    rows -- persist that ONCE as a ``demoted_seen`` event, so the card stays
    T0-equivalent after Harrison clears the demotion. Returns True when *p* is (now)
    marked. The in-memory proposal is marked even if the append fails (this render /
    tap still sees it; the demotion file itself keeps it T0 until the next try)."""
    if p.demoted or not p.has_t1_rows:
        return p.demoted
    p.demoted = True
    if not append_event(DEMOTED_SEEN, proposal_id=p.proposal_id, ts=now):
        log.error("channel_archive: demoted_seen append failed proposal=%s", p.proposal_id)
    return True


def _state_from_outcome(outcome: str) -> str:
    o = str(outcome or "")
    if o.startswith("archived"):
        return ARCHIVED
    if o.startswith("already_archived"):     # incl. the monitor's "(reconciled)" form
        return ALREADY_ARCHIVED
    if o.startswith("stale_refused"):
        return STALE
    if o.startswith("unknown"):
        return UNKNOWN
    return FAILED


def unarchive_state(ledger: list[dict] | None) -> dict:
    """cid -> {"at": epoch, "by": uid} for channels the ledger shows archived by this
    lane and then unarchived (the monitor's ``unarchived_seen`` rows, A10).

    FAIL-CLOSED WITHOUT THE MONITOR (D-051 r1 c1-false-inactive#1): a lane archive with
    no ``unarchived_seen`` at/after it reads ``{"at": None, "by": "", "unconfirmed":
    True}``. The scan lists only OPEN channels, so a channel it meets here was reopened
    since the lane archived it; the classifier then routes it to section B
    ``unarchived_before`` with the date and actor unknown -- never a clean section-A row
    inside Archive-all (and an unknown date suppresses nothing). An archive is keyed on
    its archive MESSAGE's ts when the row carries ``archive_ts`` (a reconciled outcome
    is stamped at the later nightly run)."""
    archived: dict[str, float] = {}
    seen: dict[str, dict] = {}
    for r in ledger or []:
        cid = str(r.get("channel_id") or "")
        if not cid:
            continue
        if r.get("event") == "outcome" and str(r.get("outcome") or "").startswith("archived"):
            at = float(r.get("archive_ts") or r.get("ts") or 0)
            archived[cid] = max(archived.get(cid, 0.0), at)
        elif r.get("event") == "unarchived_seen":
            at = float(r.get("unarchive_ts") or r.get("ts") or 0)
            if cid not in seen or at >= seen[cid]["at"]:
                seen[cid] = {"at": at, "by": str(r.get("by") or "")}
    out: dict = {}
    for cid in set(archived) | set(seen):
        s = seen.get(cid)
        if s is not None and s["at"] >= archived.get(cid, 0.0):
            out[cid] = s
        elif cid in archived:
            out[cid] = {"at": None, "by": "", "unconfirmed": True}
    return out


# ── claims ───────────────────────────────────────────────────────────────────
def decide_row(pid: str, cid: str, event: str, *, actor: str, now: float | None = None,
               check: Any = None, **fields: Any) -> tuple[bool, str, Fold | None]:
    """Check-then-write in ONE acquisition of CLAIM_LOCK (lesson 12): the row exists,
    no live claim holds its CHANNEL in any proposal, the row is not terminal, and the
    optional ``check(fold, proposal)`` returns None -- then ``event`` is appended.
    Returns (ok, why, fold). Mark / Keep write their decision here directly; the T1
    archive path writes ``claimed`` (``claim_row``) because it then spends seconds on
    the network."""
    now = time.time() if now is None else float(now)
    with CLAIM_LOCK:
        f = fold(now=now)
        if not f.ok:
            return False, "store_unreadable", None
        p = f.proposals.get(pid)
        if p is None or cid not in p.rows_by_cid:
            return False, "orphaned", f
        if f.channel_claim(cid) is not None:
            return False, "in_progress", f
        if p.state_of(cid) in TERMINAL:
            return False, "already_handled", f
        if check is not None:
            why = check(f, p)
            if why:
                return False, str(why), f
        if not append_event(event, proposal_id=pid, cid=cid, by=actor, ts=now, **fields):
            return False, "store_write_failed", f
    return True, "", f


def claim_row(pid: str, cid: str, kind: str, *, actor: str, now: float | None = None,
              check: Any = None) -> tuple[bool, str, Fold | None]:
    """The durable T1 claim: a ``claimed`` event keyed by the channel (A13)."""
    return decide_row(pid, cid, CLAIMED, actor=actor, now=now, check=check, kind=kind)


def append_intent_if_claimed(pid: str, cid: str, claim_ts: float, *, now: float,
                             **intent: Any) -> tuple[str, Fold | None]:
    """The archive path's last check-then-write before any Slack write (c1-state-
    machine#3), in ONE acquisition of CLAIM_LOCK: re-fold; the row must still be
    CLAIMED by THIS attempt (its own claim ts, kind ``archive``) and the card must not
    have outlived a demotion (A12); only then is the ledger ``intent`` appended. A Keep
    or Mark that landed during the re-verify (an expired or shadowed claim) therefore
    stops the archive here. Returns ("ok" | "store_unreadable" | "claim_lost" |
    "demoted" | "ledger_write_failed", fold)."""
    with CLAIM_LOCK:
        f = fold(now=now)
        if not f.ok:
            return "store_unreadable", f
        p = f.proposals.get(pid)
        cur = (p.row_state.get(cid) if p is not None else None) or {}
        if (cur.get("state") != CLAIMED or cur.get("kind") != "archive"
                or abs(float(cur.get("ts") or 0) - float(claim_ts)) > 1e-3):
            return "claim_lost", f
        if p.demoted:
            return "demoted", f
        if not append_ledger("intent", proposal_id=pid, channel_id=cid, **intent):
            return "ledger_write_failed", f
    return "ok", f


# ── the cross-process scan lock (A18) ────────────────────────────────────────
#: This process's identity in the lock body, beside its pid: a lock carrying OUR pid
#: but another nonce was left by a previous instance that happened to get the same pid.
_PROCESS_NONCE = secrets.token_hex(8)


def _pid_alive(pid: Any) -> bool:
    """REAL liveness probe (copied from scripts/run_delegated_work_runner.py). On
    Windows ``os.kill(pid, 0)`` is NOT one -- signal 0 maps to GenerateConsoleCtrlEvent
    -- so OpenProcess + GetExitCodeProcess; access denied counts as ALIVE (fail closed:
    never take over a lock we cannot inspect)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes  # noqa: PLC0415
        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True          # can't tell -- treat as alive (fail closed)
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _holder_gone(held: dict) -> bool:
    """c1-state-machine#7: True when the lock's holder is certainly not running -- its
    pid is dead, or it is OUR pid from a previous instance (another nonce). A hard kill
    skips deliver's ``finally``; without this, every ask for 30 minutes was told 'A scan
    is already running; its card will arrive here' and no card came. A lock with no
    readable pid keeps the age rule; a reused pid keeps it too (fail closed)."""
    pid = held.get("pid")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid == os.getpid():
        return held.get("nonce") != _PROCESS_NONCE
    return not _pid_alive(pid)


def acquire_scan_lock(*, now: float | None = None) -> str | None:
    """A token when this process now holds the scan lock; None when another live
    scan holds it. A lock older than 30 minutes, unreadable, or left by a process
    that is no longer running is stale."""
    now = time.time() if now is None else float(now)
    p = scan_lock_path()
    token = secrets.token_hex(8)
    body = json.dumps({"pid": os.getpid(), "nonce": _PROCESS_NONCE, "ts": now, "token": token})
    for _attempt in range(2):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                held = json.loads(p.read_text(encoding="utf-8"))
                stale = (now - float(held.get("ts") or 0) > SCAN_LOCK_STALE_S
                         or _holder_gone(held))
            except Exception:  # noqa: BLE001 -- unreadable lock = stale
                stale = True
            if not stale:
                return None
            try:
                p.unlink()
            except OSError:
                return None
            continue
        except OSError:
            log.error("channel_archive: scan lock unwritable", exc_info=True)
            return None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        return token
    return None


def release_scan_lock(token: str | None) -> None:
    if not token:
        return
    p = scan_lock_path()
    try:
        held = json.loads(p.read_text(encoding="utf-8"))
        if held.get("token") == token:
            p.unlink()
    except Exception:  # noqa: BLE001
        pass


# ── demotion write / clear (the monitor writes; only Harrison clears) ────────
def write_demotion(record: dict, *, dry_run: bool) -> bool:
    """Write the demotion file (atomic replace). Under dry_run nothing is written."""
    if dry_run:
        return False
    p = policy.demotion_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp-" + secrets.token_hex(4))
        tmp.write_text(json.dumps({"demoted": True, **record}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        log.error("channel_archive: demotion write FAILED", exc_info=True)
        return False
    return True


def demotion_events(state: dict | None) -> list[dict]:
    """Every unattributed archive event a demotion record lists (D-051 r1
    c1-monitor#2): its ``events`` list plus the legacy single top-level
    (channel_id, archive_ts) pair, de-duplicated, in order."""
    out: list[dict] = []
    seen: set[tuple] = set()
    raw = (state or {}).get("events")
    pairs = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    pairs.append({"channel_id": (state or {}).get("channel_id"),
                  "archive_ts": (state or {}).get("archive_ts")})
    for e in pairs:
        cid, ats = str(e.get("channel_id") or ""), str(e.get("archive_ts") or "")
        if cid and ats and (cid, ats) not in seen:
            seen.add((cid, ats))
            out.append({"channel_id": cid, "archive_ts": ats})
    return out


def clear_demotion(*, actor: str, dry_run: bool) -> dict:
    """Harrison's clear. Appends an ``acknowledged`` ledger row for EVERY archive event
    the demotion lists (its exact (channel_id, archive_ts) pairs) so none of them can
    re-demote the lane, then deletes the file. Returns what was (or would be) cleared,
    ``events`` included."""
    state = policy.demotion_state()
    if state is None:
        return {"cleared": False, "reason": "not demoted"}
    events = demotion_events(state)
    out = {"cleared": False, "channel_id": state.get("channel_id"),
           "archive_ts": state.get("archive_ts"), "since": state.get("since"),
           "reason": state.get("reason"), "events": events}
    if dry_run:
        out["would_clear"] = True
        return out
    for e in events:
        if not append_ledger("acknowledged", channel_id=e["channel_id"],
                             archive_ts=e["archive_ts"], by=actor,
                             demoted_since=state.get("since")):
            out["reason"] = "ledger write failed -- the demotion stays"
            return out
    elif not append_ledger("acknowledged", by=actor, demoted_since=state.get("since")):
        # A12: even a demotion with no event to ack (an unreadable file) leaves its
        # history in the ledger, so every card older than this clear stays T0.
        out["reason"] = "ledger write failed -- the demotion stays"
        return out
    try:
        policy.demotion_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        out["reason"] = f"could not delete the demotion file ({type(exc).__name__})"
        return out
    out["cleared"] = True
    return out


# ── a crashed scan settles its own stall finding (D-051 r1 c1-monitor#0) ─────
def record_scan_failed(*, trigger: str, since: float, error: str = "") -> list[str]:
    """The CRASH PATH of a scan (the bot's scan pool, the monthly script) appends one
    ``scan_failed`` event per scan it started -- a ``scan_started`` of *trigger* at or
    after *since* that neither staged nor was already settled -- so the monitor's
    stall finding settles (the crash was already said where the scan was asked).
    *error* is an exception CLASS name only, never its message (lesson 7). Never
    raises; returns the scan ids it settled."""
    try:
        events = read_events() or []
        done = {str(e.get("scan_id")) for e in events
                if e.get("event") in ("staged", "scan_failed") and e.get("scan_id")}
        settled: list[str] = []
        for e in events:
            sid = str(e.get("scan_id") or "")
            if (e.get("event") != "scan_started" or not sid or sid in done
                    or str(e.get("trigger") or "") != trigger
                    or float(e.get("ts") or 0) < float(since) - 1):
                continue
            if append_event("scan_failed", scan_id=sid, trigger=trigger,
                            error=str(error or "")[:60] or None):
                settled.append(sid)
                done.add(sid)
        return settled
    except Exception:  # noqa: BLE001 -- a crash path must never raise
        log.error("channel_archive: recording scan_failed failed", exc_info=True)
        return []
