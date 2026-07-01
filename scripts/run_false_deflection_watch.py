#!/usr/bin/env python3
"""False-deflection watch -- weekly QA on Cora's pre-LLM access blocks (R6).

Companion to the 2026-06-30 over-deflection fix (review doc:
_shared/projects/cora/2026-06-30_fndr_cora-over-deflection-review.md). The fix
narrowed the deterministic `financials` / `legal` topic blocks so a sales owner's
COMMERCIAL questions (deal value, PO amount, wholesale price, margin on an order,
invoice paid-status) no longer read as restricted finance. This watch makes the
narrowing observable: it counts `user_access` blocks per user over the last 7
days from the bot's own logs and flags a SPIKE of NON-PHI blocks on a commercial
role (Alex, Tommy, Elena). A non-PHI block on a sales role is the signature of an
over-deflection regression -- exactly the failure this fix removed.

PHI blocks are legitimate (client health -> EHR) and never flagged. The watch
NEVER changes access; it only reports.

Fires weekly (Monday 08:00 UTC / 1am AZ). Posts a compact summary to #cora-health;
the flagged section leads when there is a spike, otherwise a one-line "clean".

Usage (Windows Task Scheduler):
    python scripts/run_false_deflection_watch.py [--dry-run] [--window-days N]

Environment variables:
    SLACK_BOT_TOKEN    For posting the summary (not needed for --dry-run)

Script-side: reads on-disk logs and posts as its own process. It imports the
`cora` package only to install the egress-sanitizing WebClient patch (forensic
rebuild B1) in-process -- it does NOT import bot/app logic, so it never needs a
Cora restart.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
load_dotenv(_REPO_ROOT / ".env")

# Importing the cora package runs cora/__init__.py, which installs the class-level
# egress sanitizer on slack_sdk.WebClient.chat_* (forensic rebuild B1). Every
# Slack post from this script is then sanitized in-process, like the bot's own
# sends. Import for the side effect only -- no bot/app logic is loaded.
import cora  # noqa: E402,F401

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("false_deflection_watch")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CORA_HEALTH_CHANNEL = "C0B7CADQ98S"  # #cora-health (Cora self-monitoring)
LOG_DIR = _REPO_ROOT / "logs"
DEFAULT_WINDOW_DAYS = 7

# Commercial / revenue-facing roles: their daily work is money- and deal-adjacent,
# so a NON-PHI block on them is the over-deflection signature. Source of truth is
# data/maps/user-permissions.yaml + design/known-answers/people.md; mirrored here so
# the watch stays a standalone script that imports no bot module.
COMMERCIAL_ROLES: dict[str, str] = {
    "U0B3VGWJTMJ": "Alex Cordova",
    "U0B3RU5Q55G": "Tommy Anderson",
    "U0B8DGHR2D6": "Elena Meirndorf",
}

# A commercial role with at least this many NON-PHI blocks in the window is flagged.
SPIKE_THRESHOLD = 3

# The bot logs a block at three call sites, all in the form:
#   "user_access: blocked user=U... entity=... reason=..."   (handle_mention)
#   "cora_ask: user_access blocked user=U... entity=... reason=..."
#   "dm_qa: user_access blocked user=U... entity=... reason=..."
# reason= is optional (older log lines omitted it on the cora_ask/dm_qa paths).
_BLOCK_RE = re.compile(
    r"user_access:?\s+blocked\s+user=(?P<user>\S+)\s+entity=(?P<entity>\S+)"
    r"(?:\s+reason=(?P<reason>.*))?$"
)
# Bot log lines start with an ISO timestamp: "2026-06-30T14:41:07 INFO [..] ..."
_TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\b")

# Classify a block by its (fixed, literal) redirect copy into the TOPIC that
# fired it. This is what makes the spike meaningful: only the two topics THIS fix
# narrowed (financials, legal) are over-deflection-prone, so only they drive the
# alert. hr / cap_table / entity_auth blocks on a commercial role are CORRECT
# refusals (an equity ask, an HR ask, a wrong-channel/entity ask) — surfaced as
# context, never as an over-deflection flag. phi is legitimate and never flagged.
# Matched on distinctive substrings of the redirect strings in
# user_access.check_access; ordered most-distinctive first. Robust to R4 copy
# tweaks: PHI is a token SET (not the single 'EHR' literal), and financials keys
# on 'financ' rather than the exact sentence.
_REASON_BUCKETS: list[tuple[str, re.Pattern[str]]] = [
    ("phi", re.compile(r"\bEHR\b|electronic\s+health\s+record|clinical\s+lead|health\s+info", re.I)),
    ("legal", re.compile(r"legal\s+matter|Emily\s+Stubbs", re.I)),
    ("hr", re.compile(r"\bHR\b|Hannah\s+Grant", re.I)),
    ("cap_table", re.compile(r"ownership", re.I)),
    ("financials", re.compile(r"financ", re.I)),  # "Company financials..." redirect
    ("entity_auth", re.compile(r"outside what i can help with|team that owns it", re.I)),
]

# The topics this fix narrowed — the only buckets that count toward a spike.
OVER_DEFLECTION_BUCKETS = frozenset({"financials", "legal"})


def classify_reason(reason: str) -> str:
    """Map a block reason string to its topic bucket (financials/legal/hr/
    cap_table/phi/entity_auth), or 'other' if no known redirect matches."""
    for bucket, pat in _REASON_BUCKETS:
        if pat.search(reason):
            return bucket
    return "other"


# ---------------------------------------------------------------------------
# Pure helpers (no I/O -- unit-tested directly)
# ---------------------------------------------------------------------------

def parse_block_line(line: str) -> dict[str, Any] | None:
    """Parse one log line into a block event, or None if it is not a block line.

    Returns {"ts": datetime|None, "user": str, "entity": str,
             "reason": str, "bucket": str}.
    """
    m = _BLOCK_RE.search(line)
    if not m:
        return None
    reason = (m.group("reason") or "").strip()
    ts: datetime | None = None
    tm = _TS_RE.match(line)
    if tm:
        try:
            ts = datetime.strptime(tm.group("ts"), "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            ts = None
    return {
        "ts": ts,
        "user": m.group("user"),
        "entity": m.group("entity"),
        "reason": reason,
        "bucket": classify_reason(reason),
    }


def collect_events(lines: list[str], cutoff: datetime | None) -> list[dict[str, Any]]:
    """Parse block events from log lines, keeping only those at/after `cutoff`.

    A line without a parseable timestamp is KEPT (fail-open: better to over-count
    an ambiguous block into the window than to silently drop a real one). When
    cutoff is None, all block lines are kept.
    """
    events: list[dict[str, Any]] = []
    for line in lines:
        ev = parse_block_line(line)
        if ev is None:
            continue
        if cutoff is not None and ev["ts"] is not None and ev["ts"] < cutoff:
            continue
        events.append(ev)
    return events


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate events per-user by topic bucket. A commercial role is FLAGGED
    only on a spike of over-deflection-prone buckets (financials + legal); other
    buckets (hr / cap_table / entity_auth / phi) are correct refusals shown as
    context, never as the alert trigger."""
    per_user_total: dict[str, int] = defaultdict(int)
    per_user_bucket: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    over_reasons: dict[str, list[str]] = defaultdict(list)

    for ev in events:
        u = ev["user"]
        b = ev["bucket"]
        per_user_total[u] += 1
        per_user_bucket[u][b] += 1
        if b in OVER_DEFLECTION_BUCKETS and ev["reason"]:
            over_reasons[u].append(ev["reason"])

    def _over_count(uid: str) -> int:
        return sum(per_user_bucket[uid].get(b, 0) for b in OVER_DEFLECTION_BUCKETS)

    flagged: list[dict[str, Any]] = []
    for uid, name in COMMERCIAL_ROLES.items():
        n = _over_count(uid)
        if n >= SPIKE_THRESHOLD:
            seen: set[str] = set()
            samples: list[str] = []
            for r in over_reasons.get(uid, []):
                if r not in seen:
                    seen.add(r)
                    samples.append(r)
                if len(samples) >= 3:
                    break
            flagged.append({
                "user": uid, "name": name, "over_deflection": n,
                "total": per_user_total.get(uid, 0),
                "buckets": dict(per_user_bucket[uid]), "samples": samples,
            })

    return {
        "total_blocks": len(events),
        "distinct_users": len(per_user_total),
        "per_user_total": dict(per_user_total),
        "per_user_bucket": {u: dict(b) for u, b in per_user_bucket.items()},
        "commercial_over": {uid: _over_count(uid) for uid in COMMERCIAL_ROLES},
        "flagged": flagged,
    }


def build_report(summary: dict[str, Any], window_days: int) -> str:
    """Render the #cora-health summary. Flagged spikes lead; else a clean one-liner."""
    flagged = summary["flagged"]
    lines = [f":mag: *False-deflection watch -- last {window_days}d*", ""]

    if flagged:
        lines.append(
            f":rotating_light: *{len(flagged)} commercial role(s) with a "
            f"financials/legal deflection spike (>= {SPIKE_THRESHOLD}) -- possible "
            f"over-deflection of commercial questions:*"
        )
        for f in flagged:
            lines.append(
                f"  - *{f['name']}* -- {f['over_deflection']} financials/legal "
                f"deflections ({f['total']} total blocks)"
            )
            for s in f["samples"]:
                lines.append(f"      · {s}")
        lines.append("")
        lines.append(
            "_Check each: was it a genuine company-finance/legal deflection, or a "
            "commercial (deal/PO/price/invoice) question wrongly refused? Only the "
            "second is a bug. hr/cap_table/entity-scope blocks are correct refusals "
            "and are NOT counted here._"
        )
    else:
        # No spike: keep it to one confirming line so the channel isn't noisy.
        cnt = ", ".join(
            f"{COMMERCIAL_ROLES[uid]} {n}"
            for uid, n in summary["commercial_over"].items()
        )
        lines.append(
            f":white_check_mark: No over-deflection spike. Commercial-role "
            f"financials/legal deflections (threshold {SPIKE_THRESHOLD}): {cnt}."
        )

    lines.append("")
    lines.append(
        f"_Totals: {summary['total_blocks']} blocks across "
        f"{summary['distinct_users']} users._"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _read_log_lines() -> list[str]:
    """Read every line from all bot log files (cora-*.log + rotated variants)."""
    lines: list[str] = []
    if not LOG_DIR.exists():
        log.warning("log dir not found: %s", LOG_DIR)
        return lines
    for path in sorted(LOG_DIR.glob("cora-*.log*")):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                lines.extend(f.read().splitlines())
        except OSError as exc:  # noqa: PERF203
            log.warning("could not read %s: %s", path, exc)
    return lines


def run(dry_run: bool = False, window_days: int = DEFAULT_WINDOW_DAYS,
        now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    cutoff = now - timedelta(days=window_days)

    lines = _read_log_lines()
    events = collect_events(lines, cutoff)
    summary = summarize(events)
    report = build_report(summary, window_days)

    if dry_run:
        log.info("[DRY RUN] Would post to %s:\n%s", CORA_HEALTH_CHANNEL, report)
        return summary

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("SLACK_BOT_TOKEN not set -- cannot post; summary=%s", summary)
        return summary

    try:
        from slack_sdk import WebClient
        WebClient(token=token).chat_postMessage(
            channel=CORA_HEALTH_CHANNEL, text=report,
        )
        log.info(
            "false-deflection watch posted: %d blocks, %d flagged",
            summary["total_blocks"], len(summary["flagged"]),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("failed to post false-deflection watch: %s", exc)

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cora false-deflection watch")
    parser.add_argument("--dry-run", action="store_true", help="Log without posting")
    parser.add_argument(
        "--window-days", type=int, default=DEFAULT_WINDOW_DAYS,
        help="Lookback window in days (default 7)",
    )
    args = parser.parse_args()
    result = run(dry_run=args.dry_run, window_days=args.window_days)
    log.info("false_deflection_watch result: %s", result)
