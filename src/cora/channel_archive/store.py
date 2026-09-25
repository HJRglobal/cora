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
SCAN_LOCK_STALE_S = 1800
DAY_S = 86400.0

# Row states. TERMINAL rows never move again (a reconcile may resolve `unknown`).
OPEN, CLAIMED, AGREED, KEPT, ARCHIVED, ALREADY_ARCHIVED, FAILED, UNKNOWN, STALE = (
    "open", "claimed", "agreed", "kept", "archived", "already_archived", "failed",
    "unknown", "stale_refused")
TERMINAL: frozenset = frozenset({AGREED, KEPT, ARCHIVED, ALREADY_ARCHIVED, STALE, UNKNOWN})
_ROW_EVENTS: frozenset = frozenset({CLAIMED, AGREED, KEPT, ARCHIVED, ALREADY_ARCHIVED,
                                    FAILED, UNKNOWN, STALE, "released", "reconciled"})

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
    pages: dict = field(default_factory=dict)      # page -> {dm_channel, message_ts, rendered_cids, buttons}
    row_state: dict = field(default_factory=dict)  # cid -> {"state", "by", "ts", "override", "code", ...}
    card_agreed_by: str = ""
    card_agreed_ts: float | None = None

    @property
    def rows_by_cid(self) -> dict:
        return {r.get("cid"): r for r in self.rows if isinstance(r, dict)}

    @property
    def delivered(self) -> bool:
        return any(p.get("message_ts") for p in self.pages.values())

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
        """The newer DELIVERED proposal with rows that supersedes *pid* (A14), or None.
        A blind, empty or undelivered scan retires nothing: it has nothing to act on."""
        p = self.proposals.get(pid)
        if p is None:
            return None
        for other_id in reversed(self.order):
            o = self.proposals[other_id]
            if o.proposal_id == pid:
                return None
            if o.created > p.created and o.rows and o.delivered:
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
    cannot be read returns ``Fold(ok=False)`` -- callers refuse to act on it."""
    now = time.time() if now is None else float(now)
    if events is None:
        events = read_events()
    if ledger is None:
        ledger = read_ledger()
    f = Fold()
    if events is None:
        f.ok = False
        return f
    intents: dict[tuple, list[float]] = {}
    outcomes: dict[tuple, list[tuple[float, str]]] = {}
    for r in ledger or []:
        key = (str(r.get("proposal_id") or ""), str(r.get("channel_id") or ""))
        if r.get("event") == "intent":
            intents.setdefault(key, []).append(float(r.get("ts") or 0))
        elif r.get("event") == "outcome":
            outcomes.setdefault(key, []).append((float(r.get("ts") or 0), str(r.get("outcome") or "")))
    for e in events:
        ev = e.get("event")
        pid = str(e.get("proposal_id") or "")
        ts = float(e.get("ts") or 0)
        if ev == "scan_started":
            f.scans_started.append(e)
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
            if cur.get("state") == UNKNOWN:
                new = ARCHIVED if e.get("outcome") == ARCHIVED else FAILED
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
    # reader-side claim expiry (A13)
    for p in f.proposals.values():
        for cid, st in list(p.row_state.items()):
            if st.get("state") != CLAIMED:
                continue
            key = (p.proposal_id, cid)
            cts = float(st.get("ts") or 0)
            its = [t for t in intents.get(key, []) if t >= cts - 1]
            if not its:
                if now - cts > CLAIM_TTL_S:
                    p.row_state[cid] = {"state": OPEN, "ts": now, "code": "claim_expired"}
                continue
            outs = [o for t, o in outcomes.get(key, []) if t >= min(its)]
            if not outs:
                p.row_state[cid] = {**st, "state": UNKNOWN, "code": "no_outcome"}
            else:
                p.row_state[cid] = {**st, "state": _state_from_outcome(outs[-1]),
                                    "code": outs[-1]}
    return f


def _state_from_outcome(outcome: str) -> str:
    o = str(outcome or "")
    if o.startswith("archived"):
        return ARCHIVED
    if o == "already_archived":
        return ALREADY_ARCHIVED
    if o.startswith("stale_refused"):
        return STALE
    if o.startswith("unknown"):
        return UNKNOWN
    return FAILED


def unarchive_state(ledger: list[dict] | None) -> dict:
    """cid -> {"at": epoch, "by": uid} for channels the ledger shows archived by this
    lane and then unarchived (the monitor's ``unarchived_seen`` rows, A10)."""
    out: dict = {}
    archived: dict[str, float] = {}
    for r in ledger or []:
        cid = str(r.get("channel_id") or "")
        if not cid:
            continue
        if r.get("event") == "outcome" and str(r.get("outcome") or "").startswith("archived"):
            archived[cid] = float(r.get("ts") or 0)
        elif r.get("event") == "unarchived_seen":
            at = float(r.get("unarchive_ts") or r.get("ts") or 0)
            if at >= archived.get(cid, 0):
                out[cid] = {"at": at, "by": str(r.get("by") or "")}
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


# ── the cross-process scan lock (A18) ────────────────────────────────────────
def acquire_scan_lock(*, now: float | None = None) -> str | None:
    """A token when this process now holds the scan lock; None when another live
    scan holds it. A lock older than 30 minutes (or unreadable) is stale."""
    now = time.time() if now is None else float(now)
    p = scan_lock_path()
    token = secrets.token_hex(8)
    body = json.dumps({"pid": os.getpid(), "ts": now, "token": token})
    for _attempt in range(2):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                held = json.loads(p.read_text(encoding="utf-8"))
                stale = now - float(held.get("ts") or 0) > SCAN_LOCK_STALE_S
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


def clear_demotion(*, actor: str, dry_run: bool) -> dict:
    """Harrison's clear. Appends an ``acknowledged`` ledger row for the demotion's
    exact (channel_id, archive_ts) so that archive event can never re-demote the
    lane, then deletes the file. Returns what was (or would be) cleared."""
    state = policy.demotion_state()
    if state is None:
        return {"cleared": False, "reason": "not demoted"}
    out = {"cleared": False, "channel_id": state.get("channel_id"),
           "archive_ts": state.get("archive_ts"), "since": state.get("since"),
           "reason": state.get("reason")}
    if dry_run:
        out["would_clear"] = True
        return out
    if state.get("channel_id") and state.get("archive_ts"):
        if not append_ledger("acknowledged", channel_id=state.get("channel_id"),
                             archive_ts=state.get("archive_ts"), by=actor,
                             demoted_since=state.get("since")):
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
