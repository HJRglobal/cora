"""Egress-rail observe-week counts (Code #12 S3'(a), cq-deca62a00719).

Two seam rails log a keyed line when they fire:

  * ``sentinel-egress-leak``  -- session #11 S1: the model echoed a WRITE_CONFIRMED /
    WRITE_BLOCKED contract token (slack_egress.scrub_write_sentinels);
  * ``phantom-write-claim``   -- Code #12 S2': a write claim with zero tool_use in the
    turn, or an id that exists in no ledger (slack_egress.screen_phantom_write_claims).

Both ride ONE flag, ``CORA_SENTINEL_ENFORCE`` (observe = WARN and deliver; enforce =
rewrite at ERROR). Doctrine (decisions.md 2026-09-03): the 8/30 -> 9/6 "clean week"
counted only the first key and was NOT clean -- a three-fold phantom-write incident
happened on day 3 in plain prose. So the exit criterion for the enforce flip reads
BOTH keys, side by side, over a 7-day window, and the nightly health check +
the Monday health digest render them together. This module is the ONE counter both
scripts call, so the two surfaces can never disagree about the numbers.

Counted from the BOT's own log files only (``logs/cora-*.log`` and their rotated
``cora-*.log.*`` siblings) -- the health check's report lines and any test output
live elsewhere, so the count never reads its own echo. A hit is a line carrying the
key in its FIRING shape (``<key> kind=`` / ``<key> mode=``), never a bare mention.

Script-side (nightly_health_check, cora_health_report); not imported by the bot.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

RAIL_SENTINEL = "sentinel-egress-leak"
RAIL_PHANTOM = "phantom-write-claim"
RAIL_KEYS: tuple[str, ...] = (RAIL_SENTINEL, RAIL_PHANTOM)

ENFORCE_ENV = "CORA_SENTINEL_ENFORCE"

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_DIR = _REPO_ROOT / "logs"

# A FIRING line, not a mention: the two emitters always follow the key with
# ` mode=` (sentinel) or ` kind=` (phantom). A tool description, a docstring
# echoed into a log, or this module's own summary line never has that shape.
_FIRING_RE = {
    RAIL_SENTINEL: re.compile(r"sentinel-egress-leak\s+mode="),
    RAIL_PHANTOM: re.compile(r"phantom-write-claim\s+kind="),
}

# Both live log-line formats -- "2026-08-19T15:28:25 INFO ..." (the bot) and
# "2026-08-19 06:33:41,929 [INFO] ..." (scripts). Same shape the health check uses.
_LINE_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")


def sentinel_mode() -> str:
    """observe (default) | enforce -- the single flag both rails read."""
    v = (os.environ.get(ENFORCE_ENV) or "observe").strip().lower()
    return "enforce" if v == "enforce" else "observe"


def _stamped_within(line: str, cutoff: datetime) -> bool | None:
    """True/False when the line carries a parseable stamp; None when it does not."""
    m = _LINE_TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S") >= cutoff
    except ValueError:
        return None


def count_rail_hits(cutoff: datetime, *, log_dir: Path | None = None) -> dict[str, int]:
    """{key: hits} across the bot logs since ``cutoff`` (naive local time, like the
    log stamps). A file is read only if its mtime is at/after the cutoff; a line is
    counted only if its own stamp is at/after the cutoff (a start-date-pinned live
    log carries the whole life of an instance). Unstamped lines are NOT counted
    (they are traceback continuations, never a firing line)."""
    root = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
    counts = {k: 0 for k in RAIL_KEYS}
    if not root.exists():
        return counts
    files = list(root.glob("cora-*.log")) + list(root.glob("cora-*.log.*"))
    for lf in sorted(files):
        try:
            if lf.stat().st_mtime < cutoff.timestamp():
                continue
            text = lf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if _stamped_within(line, cutoff) is not True:
                continue
            for key, rx in _FIRING_RE.items():
                if rx.search(line):
                    counts[key] += 1
    return counts


def observe_week_read(now: datetime | None = None, *, log_dir: Path | None = None) -> dict:
    """The one structure both health surfaces render.

    {mode, counts_24h: {key: n}, counts_7d: {key: n}, clean_7d: bool,
     flip_criterion: str} -- ``clean_7d`` is True only when BOTH rails read zero over
    the 7-day window; ``flip_criterion`` is the human sentence the reminder prints.
    """
    now = now or datetime.now()
    c24 = count_rail_hits(now - timedelta(hours=26), log_dir=log_dir)
    c7 = count_rail_hits(now - timedelta(days=7), log_dir=log_dir)
    mode = sentinel_mode()
    clean = all(v == 0 for v in c7.values())
    if mode == "enforce":
        crit = ("ENFORCE is on: every hit is an ERROR-level rewrite; "
                f"{sum(c7.values())} rewrite(s) in 7d")
    elif clean:
        crit = ("both rails clean over 7d -- the CORA_SENTINEL_ENFORCE flip criterion is "
                "MET (Harrison's call; the bot reads the flag at startup, so a flip = "
                ".env edit + restart)")
    else:
        crit = (f"observe week NOT clean: {RAIL_SENTINEL} {c7[RAIL_SENTINEL]} + "
                f"{RAIL_PHANTOM} {c7[RAIL_PHANTOM]} in 7d -- the enforce flip stays "
                "gated until BOTH read 0 for a full week")
    return {
        "mode": mode,
        "counts_24h": c24,
        "counts_7d": c7,
        "clean_7d": clean,
        "flip_criterion": crit,
    }


def format_line(read: dict) -> str:
    """One ASCII line for a digest: counts for both rails, 24h and 7d, plus the mode."""
    c24 = read.get("counts_24h") or {}
    c7 = read.get("counts_7d") or {}
    return (f"mode={read.get('mode', '?')} | {RAIL_SENTINEL} {c24.get(RAIL_SENTINEL, '?')} (24h) / "
            f"{c7.get(RAIL_SENTINEL, '?')} (7d) | {RAIL_PHANTOM} {c24.get(RAIL_PHANTOM, '?')} (24h) / "
            f"{c7.get(RAIL_PHANTOM, '?')} (7d)")
