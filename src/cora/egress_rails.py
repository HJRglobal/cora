"""Egress-rail observe-week counts (Code #12 S3'(a), cq-deca62a00719).

Two seam rails log a keyed line when they fire:

  * ``sentinel-egress-leak``  -- session #11 S1: the model echoed a WRITE_CONFIRMED /
    WRITE_BLOCKED contract token (slack_egress.scrub_write_sentinels);
  * ``phantom-write-claim``   -- Code #12 S2': a write claim with zero tool_use in the
    turn, or an id that exists in no ledger (slack_egress.screen_phantom_write_claims).

Both ride ONE flag, ``CORA_SENTINEL_ENFORCE`` (observe = WARN and deliver; enforce =
correct at ERROR). Doctrine (decisions.md 2026-09-03): the 8/30 -> 9/6 "clean week"
counted only the first key and was NOT clean -- a three-fold phantom-write incident
happened on day 3 in plain prose. So the exit criterion for the enforce flip reads
BOTH keys, side by side, over a 7-day window, and the nightly health check + the
Monday health digest render them together. This module is the ONE counter both
scripts call, so the two surfaces can never disagree about the numbers.

COVERAGE (D-051 lens A MED #3 / lens E F2, 2026-09-09): a count of ZERO is a clean
read only when the rails were ARMED in a running bot for the whole window and the
counter actually scanned bot log lines. The first cut equated silence with safety
one level up from the incident it cites -- the phantom rail existed on a branch, the
live bot ran `main`, no `phantom-write-claim` line had ever been written anywhere,
and the check said "criterion MET". Now the bot records the armed rails at startup
(``record_armed`` -> data/state/egress-rails-armed.json; ``first_armed_at`` survives
restarts while the rail set is unchanged) and ``observe_week_read`` refuses
``clean_7d`` unless ``first_armed_at`` is at/before the window start AND at least one
bot log file was scanned.

SHAPE (lens E F5): ~12 scheduled scripts (the nightly health check included) log into
the same ``logs/cora-<date>.log`` in a different format, and a test run can attach a
root handler to it. Only a line in the BOT's own format from the rails' own logger
counts -- ``<date>T<time> WARNING|ERROR [thread] cora.slack_egress: <key> kind=|mode=``
-- so the health check can never read its own echo, and a test's real WARNING can
never inflate the live count.

Read by nightly_health_check + cora_health_report (script-side); ``record_armed`` is
the one function the bot calls (main.py, at startup).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

RAIL_SENTINEL = "sentinel-egress-leak"
RAIL_PHANTOM = "phantom-write-claim"
# Code #13 slice 1 (cq-2a88e32a75ea): the THIRD seam rail -- a capability denial
# about something the bot has, or an internal tool name on a non-developer
# surface (slack_egress.screen_capability_claims). Same flag, same ritual.
RAIL_CAPABILITY = "phantom-capability-claim"
RAIL_KEYS: tuple[str, ...] = (RAIL_SENTINEL, RAIL_PHANTOM, RAIL_CAPABILITY)
#: The two Code #12 keys -- the S2' observe-week pair whose clock started
#: 2026-09-10 08:45:06. Rendered side by side with the full set so the 9/25
#: criterion read stays COMPARABLE after the third key joined (kickoff section 3).
S2_PAIR: tuple[str, ...] = (RAIL_SENTINEL, RAIL_PHANTOM)

ENFORCE_ENV = "CORA_SENTINEL_ENFORCE"
WINDOW_DAYS = 7

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIR = _REPO_ROOT / "logs"
ARMED_STATE_PATH = _REPO_ROOT / "data" / "state" / "egress-rails-armed.json"

log = logging.getLogger(__name__)

# A FIRING line, not a mention: the two emitters always follow the key with
# ` mode=` (sentinel) or ` kind=` (phantom). A tool description, a docstring
# echoed into a log, or this module's own summary line never has that shape.
_FIRING_RE = {
    RAIL_SENTINEL: re.compile(r"sentinel-egress-leak\s+mode="),
    RAIL_PHANTOM: re.compile(r"phantom-write-claim\s+kind="),
    RAIL_CAPABILITY: re.compile(r"phantom-capability-claim\s+kind="),
}

# The bot's log format (main._setup_logging): "%(asctime)s %(levelname)s
# [%(threadName)s] %(name)s: %(message)s" with a T-stamp. A rail line is WARNING or
# ERROR from cora.slack_egress. Scripts stamp "2026-08-19 06:33:41,929 [INFO] ..." --
# a different shape -- and never log as cora.slack_egress.
_BOT_RAIL_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2}) (?:WARNING|ERROR) (?:\[[^\]]*\] )?cora\.slack_egress:? ")


def sentinel_mode() -> str:
    """observe (default) | enforce -- the single flag both rails read."""
    v = (os.environ.get(ENFORCE_ENV) or "observe").strip().lower()
    return "enforce" if v == "enforce" else "observe"


def _bot_rail_line(line: str) -> datetime | None:
    """The line's stamp when it is a bot-format rail line; None otherwise."""
    m = _BOT_RAIL_LINE_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def scan_rail_hits(cutoff: datetime, *, log_dir: Path | None = None) -> dict:
    """{counts: {key: hits}, files_scanned: n, bot_lines_in_window: n} across the bot
    logs since ``cutoff`` (naive local time, like the log stamps). A file is read
    only if its mtime is at/after the cutoff; a line counts only if it is a
    bot-format rail line whose own stamp is at/after the cutoff (a start-date-pinned
    live log carries the whole life of an instance). Unstamped continuation lines,
    script-format lines and other loggers' lines are never counted."""
    root = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
    counts = {k: 0 for k in RAIL_KEYS}
    files_scanned = 0
    lines_in_window = 0
    if not root.exists():
        return {"counts": counts, "files_scanned": 0, "bot_lines_in_window": 0}
    files = list(root.glob("cora-*.log")) + list(root.glob("cora-*.log.*"))
    for lf in sorted(files):
        try:
            if lf.stat().st_mtime < cutoff.timestamp():
                continue
            text = lf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files_scanned += 1
        for line in text.splitlines():
            stamp = _bot_rail_line(line)
            if stamp is None or stamp < cutoff:
                continue
            lines_in_window += 1
            # S3 hardening: the key must START the message (right after the bot
            # prefix), never merely appear in it -- the S3 fields (and any future
            # field) must not be able to make one line count for a second rail.
            head = _BOT_RAIL_LINE_RE.match(line)
            body_at = head.end() if head else 0
            for key, rx in _FIRING_RE.items():
                if rx.match(line, body_at):
                    counts[key] += 1
    return {"counts": counts, "files_scanned": files_scanned, "bot_lines_in_window": lines_in_window}


def count_rail_hits(cutoff: datetime, *, log_dir: Path | None = None) -> dict[str, int]:
    """{key: hits} -- the counts half of scan_rail_hits."""
    return scan_rail_hits(cutoff, log_dir=log_dir)["counts"]


# ── armed record (written by the bot at startup) ─────────────────────────────
def armed_state(path: Path | None = None) -> dict | None:
    """The armed record, or None when the bot has never recorded one (or it is
    unreadable -- reported as not armed, never as covered)."""
    p = Path(path) if path is not None else ARMED_STATE_PATH
    try:
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) and data.get("first_armed_at") else None
    except Exception:  # noqa: BLE001 -- unreadable = not armed
        return None


def record_armed(*, path: Path | None = None, now: datetime | None = None,
                 rails: tuple[str, ...] = RAIL_KEYS, log_to=None) -> dict:
    """Record that ``rails`` are armed in THIS process.

    PER-KEY ``first_armed`` (Code #13 slice 1; kickoff section 3): every rail keeps
    ITS OWN first-armed stamp across restarts, and a NEW rail joining the set starts
    its own clock WITHOUT resetting the others' -- the Code #12 rule ("keep while the
    set is unchanged, else reset") would have wiped the S2' pair's 2026-09-10
    08:45:06 clock the first time the capability rail booted. Top-level
    ``first_armed_at`` = the earliest surviving key (so the legacy field still reads
    2026-09-10 for the pair). A key REMOVED from the set drops its stamp. A record
    written by the Code #12 shape (no ``first_armed`` map) is migrated: each prior
    key inherits the record's ``first_armed_at``. Atomic write; fail-soft (returns
    the record it tried to write)."""
    p = Path(path) if path is not None else ARMED_STATE_PATH
    now = now or datetime.now()
    now_iso = now.isoformat(timespec="seconds")
    prior = armed_state(p)
    rail_list = sorted(rails)
    first_by_key: dict[str, str] = {}
    if prior:
        prior_keys = set(prior.get("rails") or [])
        prior_map = prior.get("first_armed") if isinstance(prior.get("first_armed"), dict) else {}
        for k in rail_list:
            if k in prior_keys:
                first_by_key[k] = str(prior_map.get(k) or prior.get("first_armed_at") or now_iso)
    for k in rail_list:
        first_by_key.setdefault(k, now_iso)
    first = min(first_by_key.values()) if first_by_key else now_iso
    rec = {"rails": rail_list, "first_armed_at": first, "first_armed": first_by_key,
           "last_armed_at": now_iso, "pid": os.getpid()}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        tmp.replace(p)
    except Exception:  # noqa: BLE001 -- a breadcrumb must never block startup
        (log_to or log).warning("egress-rails: armed record write failed (non-fatal)", exc_info=True)
    later = [f"{k} since {v}" for k, v in first_by_key.items() if v != first]
    (log_to or log).info("egress-rails armed: %s (first armed %s%s)", ", ".join(rail_list), first,
                         ("; " + "; ".join(later)) if later else "")
    return rec


def first_armed_by_key(st: dict | None) -> dict[str, datetime]:
    """{rail: first-armed stamp} from an armed record -- per-key when the record has
    the map, else every listed rail inherits the record's ``first_armed_at``
    (the Code #12 shape). Unparseable stamps are dropped (never read as covered)."""
    out: dict[str, datetime] = {}
    if not st:
        return out
    m = st.get("first_armed") if isinstance(st.get("first_armed"), dict) else {}
    for k in (st.get("rails") or []):
        raw = m.get(k) or st.get("first_armed_at")
        try:
            out[str(k)] = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            continue
    return out


def _coverage(now: datetime, scan7: dict, armed_path: Path | None) -> dict:
    st = armed_state(armed_path)
    by_key = first_armed_by_key(st)
    first = min(by_key.values()) if by_key else None
    window_start = now - timedelta(days=WINDOW_DAYS)
    covers_by_key = {k: (k in by_key and by_key[k] <= window_start) for k in RAIL_KEYS}
    return {
        "armed": st is not None,
        "first_armed_at": first.isoformat(timespec="seconds") if first else "",
        "armed_days": (now - first).days if first else 0,
        "first_armed": {k: v.isoformat(timespec="seconds") for k, v in by_key.items()},
        "armed_days_by_key": {k: (now - v).days for k, v in by_key.items()},
        "covers_window": all(covers_by_key.values()),
        "covers_window_by_key": covers_by_key,
        "uncovered": [k for k in RAIL_KEYS if not covers_by_key[k]],
        "files_scanned": int(scan7.get("files_scanned") or 0),
        "bot_lines_in_window": int(scan7.get("bot_lines_in_window") or 0),
    }


def observe_week_read(now: datetime | None = None, *, log_dir: Path | None = None,
                      armed_path: Path | None = None) -> dict:
    """The one structure both health surfaces render.

    {mode, counts_24h: {key: n}, counts_7d: {key: n}, coverage: {...}, clean_7d: bool,
     flip_criterion: str} -- ``clean_7d`` is True only when BOTH rails read zero over
    the 7-day window AND the rails were armed for the whole window AND at least one
    bot log file was scanned; ``flip_criterion`` is the human sentence the reminder
    prints, and it says WHICH of those held.
    """
    now = now or datetime.now()
    s24 = scan_rail_hits(now - timedelta(hours=26), log_dir=log_dir)
    s7 = scan_rail_hits(now - timedelta(days=WINDOW_DAYS), log_dir=log_dir)
    c24, c7 = s24["counts"], s7["counts"]
    mode = sentinel_mode()
    cov = _coverage(now, s7, armed_path)
    zero = all(v == 0 for v in c7.values())
    covered = cov["covers_window"] and cov["files_scanned"] > 0
    clean = zero and covered
    # The Code #12 pair on its own (S2' clock 2026-09-10): rendered beside the full
    # set so adding the third key never makes the 9/25 read incomparable.
    pair_zero = all(c7.get(k, 0) == 0 for k in S2_PAIR)
    pair_covered = all(cov["covers_window_by_key"].get(k) for k in S2_PAIR) and cov["files_scanned"] > 0
    clean_pair = pair_zero and pair_covered
    hits_txt = " + ".join(f"{k} {c7[k]}" for k in RAIL_KEYS)
    if mode == "enforce":
        crit = ("ENFORCE is on: every hit is an ERROR-level correction; "
                f"{sum(c7.values())} correction(s) in 7d")
    elif not zero:
        crit = (f"observe week NOT clean: {hits_txt} in 7d -- the enforce flip stays "
                "gated until ALL rails read 0 for a full armed week")
    elif not covered:
        if not cov["armed"]:
            why = "the rails were never armed in a running bot (no armed record)"
        else:
            parts = []
            for k in cov["uncovered"]:
                fa = (cov["first_armed"] or {}).get(k)
                parts.append(f"{k} {'armed only since ' + fa[:10] + ' (' + str(cov['armed_days_by_key'].get(k, 0)) + 'd)' if fa else 'NOT yet armed in a running bot'}")
            why = "; ".join(parts) + f" (window is {WINDOW_DAYS}d)"
            if clean_pair:
                why += (f"; the S2' pair ({' + '.join(S2_PAIR)}) IS clean and covered since "
                        f"{cov['first_armed'].get(RAIL_PHANTOM, '')[:10]}")
        if cov["files_scanned"] == 0:
            why += " and no bot log file fell inside the window"
        crit = (f"NOT a clean read: {why} -- zero firings is silence, not safety; the "
                "CORA_SENTINEL_ENFORCE flip stays gated until every rail reads 0 across a "
                "FULL armed week")
    else:
        crit = (f"all {len(RAIL_KEYS)} rails clean over 7d with the rails armed for the whole window -- the "
                "CORA_SENTINEL_ENFORCE flip criterion is MET (Harrison's call; the bot reads the "
                "flag at startup, so a flip = .env edit + restart)")
    return {
        "mode": mode,
        "counts_24h": c24,
        "counts_7d": c7,
        "coverage": cov,
        "clean_7d": clean,
        "clean_7d_s2_pair": clean_pair,
        "flip_criterion": crit,
    }


def format_line(read: dict) -> str:
    """One ASCII line for a digest: counts for EVERY rail key, 24h and 7d, mode,
    coverage (per-key first-armed when a key's clock differs from the earliest)."""
    c24 = read.get("counts_24h") or {}
    c7 = read.get("counts_7d") or {}
    cov = read.get("coverage") or {}
    if cov.get("armed"):
        first = str(cov.get("first_armed_at", ""))
        armed = f"armed since {first[:10]} ({cov.get('armed_days', '?')}d)"
        later = [f"{k} since {str(v)[:10]}" for k, v in (cov.get("first_armed") or {}).items()
                 if str(v) != first]
        missing = [k for k in RAIL_KEYS if k not in (cov.get("first_armed") or {})]
        extra = later + [f"{k} NOT armed" for k in missing]
        if extra:
            armed += " (" + "; ".join(extra) + ")"
    else:
        armed = "rails NOT armed (no record)"
    counts = " | ".join(f"{k} {c24.get(k, '?')} (24h) / {c7.get(k, '?')} (7d)" for k in RAIL_KEYS)
    return (f"mode={read.get('mode', '?')} | {counts} | {armed}, "
            f"{cov.get('files_scanned', '?')} bot log(s) scanned")
