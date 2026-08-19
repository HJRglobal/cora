"""Approval recon: what Harrison's approval queue actually costs, per lane.

Ordered by the 2026-08-18 Ops session (O5, cq-e6ab72d91735) as the REQUIRED
INPUT to the multi-approver decision. Multi-approver stays Harrison-only until
he rules on this output; nothing here changes an approval path.

READ-ONLY. Reads the append-only ledgers each lane already writes and prints a
report. It approves nothing, writes to no ledger, and takes no argument that
could make it do either.

WHAT IT MEASURES, AND WHY THESE THREE THINGS
--------------------------------------------
1. THROUGHPUT -- proposed / approved / denied / still-open, per lane. The base
   rate. Without it, "the queue is backing up" is a feeling.
2. LATENCY -- how long a resolved item WAITED, and how long open items have
   been waiting. This is the number the second-approver question actually turns
   on: a lane with 40 items and a 2-hour median does not need another approver;
   a lane with 6 items and a 9-day median does.
3. THE COUNTERFATUAL, stated as a BOUND rather than a benefit. A second approver
   can only help with items they are ALLOWED to approve. So the report splits
   the backlog into "a second approver could have taken this" and "only Harrison
   could", and reports the wait time in each half separately. If the wait is
   concentrated in the half a second approver cannot touch, adding one buys
   nothing -- and that is the finding most worth surfacing, because it is the
   one an enthusiastic reading of the throughput numbers would miss.

HANNAH'S BOUND IS A HARD INPUT, NOT A TUNABLE
----------------------------------------------
She stated it herself: DW actions and knowledge items, NEVER code. It is encoded
as `SECOND_APPROVER_ELIGIBLE_LANES` and the code lane is deliberately absent, so
the counterfactual can never quietly credit a second approver with clearing work
she has said she will not do. Widening it is a decision for the two of them, not
a constant to nudge.

COST
----
Reported where it is RECORDED, which today is delegated work only (per-job
`cost.est_usd`). No approval record in any other lane carries a token or dollar
figure, so the report says so rather than estimating one -- an invented cost in a
document about whether to delegate authority is worse than an absent one.

USAGE
-----
    .venv\\Scripts\\python.exe scripts\\run_approval_recon.py
    .venv\\Scripts\\python.exe scripts\\run_approval_recon.py --days 30 --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

HARRISON_ID = "U0B2RM2JYJ1"
_AZ = timezone(timedelta(hours=-7))

# Lanes a SECOND approver could take, per Hannah's own stated boundary
# (2026-08-18 Ops session): delegated-work actions and knowledge items only.
# `code` is absent BY DECISION, not by omission -- see the module docstring.
SECOND_APPROVER_ELIGIBLE_LANES: frozenset[str] = frozenset({
    "knowledge", "delegated_work",
})

LEDGERS = {
    "knowledge": _REPO_ROOT / "data" / "cora-proposed-memory-updates.jsonl",
    "reactions": _REPO_ROOT / "data" / "cora-reply-log.jsonl",
    "autowrite": _REPO_ROOT / "logs" / "cora-autowrite-audit.jsonl",
    "delegated_work": _REPO_ROOT / "data" / "state" / "delegated-work.jsonl",
    "delegated_work_runner": _REPO_ROOT / "data" / "state" / "delegated-work-runner.jsonl",
    "code": _REPO_ROOT / "data" / "state" / "code-session-queue.jsonl",
}


@dataclass
class Lane:
    name: str
    proposed: int = 0
    approved: int = 0
    denied: int = 0        # a human said no
    expired: int = 0       # NOBODY decided -- aged out
    routed: int = 0        # handed to an owner; not an approval decision
    withdrawn: int = 0     # terminal, but nobody was waiting on an approver
    open_items: int = 0
    resolved_wait_hours: list[float] = field(default_factory=list)
    open_wait_hours: list[float] = field(default_factory=list)
    cost_usd: float = 0.0
    cost_available: bool = False
    note: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "lane": self.name,
            "second_approver_eligible": self.name in SECOND_APPROVER_ELIGIBLE_LANES,
            "proposed": self.proposed,
            "approved": self.approved,
            "denied": self.denied,
            "expired": self.expired,
            "routed": self.routed,
            "withdrawn": self.withdrawn,
            "open": self.open_items,
            "decided_by_a_human": self.approved + self.denied,
            "median_wait_h": _median(self.resolved_wait_hours),
            "p90_wait_h": _pct(self.resolved_wait_hours, 0.9),
            "oldest_open_h": max(self.open_wait_hours) if self.open_wait_hours else None,
            "open_wait_total_h": round(sum(self.open_wait_hours), 1),
            "cost_usd": round(self.cost_usd, 2) if self.cost_available else None,
            "note": self.note,
        }


def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 1) if xs else None


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ordered = sorted(xs)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return round(ordered[idx], 1)


def _parse_ts(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue      # a torn tail line must not abort the recon
    return rows


def _hours(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 3600.0)


def analyze(now: datetime | None = None, days: int = 30) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    lanes: dict[str, Lane] = {}

    # ── knowledge review (the proposals Harrison thumbs in the 7am DM) ──────
    lane = lanes["knowledge"] = Lane("knowledge")
    for row in _read(LEDGERS["knowledge"]):
        proposed = _parse_ts(row.get("proposed_at"))
        if proposed is None or proposed < cutoff:
            continue
        lane.proposed += 1
        state = str(row.get("state") or "").lower()
        reason = str(row.get("resolved_reason") or "").lower()
        resolved = _parse_ts(row.get("resolved_at"))

        # A DISMISSED row is NOT necessarily a decision. Live 30-day read
        # 2026-08-19: of 101 dismissals only 12 came from a human tap --
        # 76 EXPIRED with nobody deciding and 24 were routed to an owner.
        # Counting all three as "denied" would report a decisive, well-served
        # queue and hide the actual failure, which is that most items age out
        # unanswered. On a document about delegating approval authority that
        # single mislabel would invert the conclusion.
        if state in ("approved", "applied", "accepted"):
            lane.approved += 1
        elif "expired" in reason or state == "expired":
            lane.expired += 1
        elif reason.startswith("routed_to_owner"):
            lane.routed += 1
        elif state in ("dismissed", "rejected", "declined"):
            lane.denied += 1
        else:
            lane.open_items += 1
            lane.open_wait_hours.append(_hours(proposed, now))
            continue
        if resolved:
            lane.resolved_wait_hours.append(_hours(proposed, resolved))
    lane.note = "7am review DM; one tap per item."

    # ── delegated work (HELD jobs awaiting release/dismiss) ─────────────────
    lane = lanes["delegated_work"] = Lane("delegated_work", cost_available=True)
    bot = _read(LEDGERS["delegated_work"])
    runner = _read(LEDGERS["delegated_work_runner"])
    held_at: dict[str, datetime] = {}
    resolved_at: dict[str, tuple[str, datetime]] = {}
    for ev in bot:
        ts = _parse_ts(ev.get("ts"))
        jid = str(ev.get("job_id") or "")
        if ts is None or not jid:
            continue
        if ev.get("event") == "held":
            held_at[jid] = ts
        elif ev.get("event") == "released":
            resolved_at[jid] = ("approved", ts)
        elif ev.get("event") == "cancelled":
            # `harrison_dismiss` is the approver saying no. Every OTHER cancel --
            # requester_cancel, harrison_cancel -- is terminal but is NOT an
            # approval decision, and crucially is NOT still waiting on one.
            # Counting those as `open` accrued unbounded "wait" on jobs nobody
            # was waiting for, and that number is exactly what the
            # second-approver recommendation turns on. Same mislabel class as
            # counting an expiry as a denial, one lane over.
            reason = str(ev.get("reason") or "")
            resolved_at[jid] = (
                "denied" if reason == "harrison_dismiss" else "withdrawn", ts)
    for jid, ts in held_at.items():
        if ts < cutoff:
            continue
        lane.proposed += 1
        outcome = resolved_at.get(jid)
        if outcome is None:
            lane.open_items += 1
            lane.open_wait_hours.append(_hours(ts, now))
            continue
        kind, when = outcome
        setattr(lane, kind, getattr(lane, kind) + 1)
        if kind != "withdrawn":
            # A withdrawal's elapsed time is not approver latency.
            lane.resolved_wait_hours.append(_hours(ts, when))
    for ev in runner:
        cost = (ev.get("cost") or {}).get("est_usd")
        ts = _parse_ts(ev.get("ts"))
        if cost and ev.get("event") in ("delivered", "failed") and ts and ts >= cutoff:
            try:
                lane.cost_usd += float(cost)
            except (TypeError, ValueError):
                pass
    lane.note = "HELD jobs only (quota/envelope holds); cost is measured, not estimated."

    # ── code queue (build work; NOT second-approver eligible) ───────────────
    lane = lanes["code"] = Lane("code")
    # KNOWN TRANSITIONS only, read from the field that actually carries them.
    # Verified against the live ledger (308 rows): `status` appears ONLY on the
    # `captured` seed row; every transition rides the EVENT name (shipped /
    # staged / approved / dismissed / superseded). The ledger ALSO carries
    # non-transition events -- dm_sent (19), recurrence (8), evidence (2),
    # edited (1), dm_held (1) -- and the first cut read
    # `status or event`, which treated each of those as a status: a teammate
    # re-mentioning an already-SHIPPED item appends `recurrence`, and the item
    # silently reverted to "open" with its full age added to the accrued wait.
    # An allowlist keyed to the real vocabulary is the only version of this that
    # cannot be broken by adding a new bookkeeping event.
    _EVENT_OUTCOME = {
        "shipped": "approved", "staged": "approved", "approved": "approved",
        "dismissed": "denied", "superseded": "denied", "expired": "expired",
    }
    _SEED_OUTCOME = {
        "SHIPPED": "approved", "STAGED": "approved", "APPROVED": "approved",
        "DISMISSED": "denied", "SUPERSEDED": "denied", "EXPIRED": "expired",
    }

    first_seen: dict[str, datetime] = {}
    latest: dict[str, tuple[str, datetime]] = {}
    for ev in _read(LEDGERS["code"]):
        ts = _parse_ts(ev.get("ts"))
        cq = str(ev.get("id") or "")
        if ts is None or not cq:
            continue
        first_seen.setdefault(cq, ts)
        outcome = _EVENT_OUTCOME.get(str(ev.get("event") or "").strip().lower())
        if outcome is None:
            outcome = _SEED_OUTCOME.get(str(ev.get("status") or "").strip().upper())
        if outcome:
            latest[cq] = (outcome, ts)

    for cq, ts in first_seen.items():
        if ts < cutoff:
            continue
        lane.proposed += 1
        outcome, when = latest.get(cq, ("", ts))
        if outcome in ("approved", "denied"):
            setattr(lane, outcome, getattr(lane, outcome) + 1)
            lane.resolved_wait_hours.append(_hours(ts, when))
        elif outcome == "expired":
            lane.expired += 1
        else:
            lane.open_items += 1
            lane.open_wait_hours.append(_hours(ts, now))
    lane.note = ("Harrison-only by Hannah's own stated boundary -- excluded from "
                 "the second-approver counterfactual.")

    # ── the counterfactual, stated as a bound ──────────────────────────────
    eligible = [l for l in lanes.values() if l.name in SECOND_APPROVER_ELIGIBLE_LANES]
    harrison_only = [l for l in lanes.values()
                     if l.name not in SECOND_APPROVER_ELIGIBLE_LANES]

    def _agg(ls: list[Lane]) -> dict[str, Any]:
        waits = [h for l in ls for h in l.open_wait_hours]
        return {
            "lanes": [l.name for l in ls],
            "open_items": sum(l.open_items for l in ls),
            "open_wait_total_h": round(sum(waits), 1),
            "oldest_open_h": round(max(waits), 1) if waits else None,
            "decided_by_a_human": sum(l.approved + l.denied for l in ls),
            "expired_undecided": sum(l.expired for l in ls),
            "median_wait_h": _median([h for l in ls for h in l.resolved_wait_hours]),
        }

    return {
        "window_days": days,
        "generated_at_utc": now.isoformat(),
        "lanes": [l.summary() for l in lanes.values()],
        "counterfactual": {
            "a_second_approver_could_take": _agg(eligible),
            "harrison_only": _agg(harrison_only),
            "eligible_lanes": sorted(SECOND_APPROVER_ELIGIBLE_LANES),
            "bound": ("Hannah's stated boundary: delegated-work actions and "
                      "knowledge items only, never code."),
        },
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        f"APPROVAL RECON -- trailing {result['window_days']} days",
        f"generated {result['generated_at_utc']} (read-only)",
        "",
        f"{'lane':<18}{'prop':>6}{'appr':>6}{'deny':>6}{'exp':>6}{'rout':>6}"
        f"{'wdrn':>6}{'open':>6}{'med h':>8}{'p90 h':>8}{'oldest h':>10}{'cost':>9}",
    ]
    for lane in result["lanes"]:
        cost = "n/a" if lane["cost_usd"] is None else f"${lane['cost_usd']:.2f}"
        lines.append(
            f"{lane['lane']:<18}{lane['proposed']:>6}{lane['approved']:>6}"
            f"{lane['denied']:>6}{lane['expired']:>6}{lane['routed']:>6}"
            f"{lane['withdrawn']:>6}{lane['open']:>6}"
            f"{_fmt(lane['median_wait_h']):>8}{_fmt(lane['p90_wait_h']):>8}"
            f"{_fmt(lane['oldest_open_h']):>10}{cost:>9}")
    cf = result["counterfactual"]
    lines += [
        "",
        "COUNTERFACTUAL -- what a second approver could actually take",
        f"  bound: {cf['bound']}",
        f"  eligible lanes : {', '.join(cf['eligible_lanes'])}",
        f"  decided by a human, eligible half : "
        f"{cf['a_second_approver_could_take']['decided_by_a_human']}"
        f"   EXPIRED undecided: "
        f"{cf['a_second_approver_could_take']['expired_undecided']}",
        f"  eligible open  : {cf['a_second_approver_could_take']['open_items']} item(s), "
        f"{cf['a_second_approver_could_take']['open_wait_total_h']}h of accrued wait",
        f"  Harrison-only  : {cf['harrison_only']['open_items']} item(s), "
        f"{cf['harrison_only']['open_wait_total_h']}h of accrued wait",
        "",
        "  Read this as a BOUND, not a benefit: a second approver can only remove",
        "  wait from the eligible half. If the wait is concentrated in the",
        "  Harrison-only half, adding an approver buys nothing.",
        "",
        "  Cost is reported only where it is RECORDED (delegated work). No other",
        "  lane's approval record carries a token or dollar figure, and an",
        "  invented one would be worse than an absent one here.",
    ]
    return "\n".join(lines)


def _fmt(v: Any) -> str:
    return "-" if v is None else f"{v:g}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="Trailing window (default 30).")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = ap.parse_args(argv)

    result = analyze(days=max(1, args.days))
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
