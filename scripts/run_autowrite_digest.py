#!/usr/bin/env python3
r"""Weekly "Cora auto-learned this week" digest (§7B oversight-after-the-fact).

Since the graduated-trust flip (CORA_AUTOWRITE_LIVE) lets Cora auto-write Tier-0/1
knowledge without a per-item Harrison gate, THIS is the oversight surface:
a weekly DM to Harrison of every auto-write, with a one-tap Revert per item, plus
week-over-week counts so drift is visible. Reversibility + audit replace the gate.

For the first ~4 weeks after the flip, this digest IS the validation (the shadow
produced zero Tier-0/1 track record, so the flip rests on the conservative tier
scoping + reversibility, not a shadow verdict -- watch these counts).

Runs weekly (Mon). Reads logs/cora-autowrite-audit.jsonl (written by
knowledge_review.apply_autowrite). DMs Harrison ONLY. Fail-soft.

Usage:
    .venv\Scripts\python.exe scripts\run_autowrite_digest.py            # DM if any activity
    .venv\Scripts\python.exe scripts\run_autowrite_digest.py --dry-run  # print, no DM
    .venv\Scripts\python.exe scripts\run_autowrite_digest.py --force    # DM even if quiet
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import os  # noqa: E402

from cora import knowledge_review as kr  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("autowrite-digest")

_DAY = 86400.0


def _ts(rec: dict) -> float:
    try:
        return datetime.fromisoformat(str(rec.get("ts", ""))).timestamp()
    except ValueError:
        return 0.0


def _is_autowrite(rec: dict) -> bool:
    return str(rec.get("decision_reason", "")).startswith("auto_")


def build_digest(now_ts: float, days: int = 7) -> tuple[dict, list[dict]]:
    """Return (stats, this_week_autowrites). this_week = non-reverted auto-writes
    in the last `days`."""
    records = kr.read_autowrite_audit()
    since_1 = now_ts - days * _DAY
    since_2 = now_ts - 2 * days * _DAY
    # index reverts by update_id so we can flag/skip already-reverted items
    reverted_ids = {r.get("update_id") for r in records
                    if r.get("decision_reason") == "revert"}
    this_week, prev_week, reverts_this_week = [], 0, 0
    for r in records:
        t = _ts(r)
        if r.get("decision_reason") == "revert":
            if t >= since_1:
                reverts_this_week += 1
            continue
        if not _is_autowrite(r):
            continue
        if t >= since_1:
            if r.get("update_id") not in reverted_ids:
                this_week.append(r)
        elif since_2 <= t < since_1:
            prev_week += 1
    stats = {
        "this_week": len(this_week),
        "prev_week": prev_week,
        "reverts_this_week": reverts_this_week,
        "level": kr.autowrite_level(),
    }
    return stats, this_week


def _why_zero_line(stats: dict, days: int = 7) -> str:
    """Explain a zero instead of printing it bare (C3 / cq-a46ebe458d92).

    The digest has read 0/0/0 for three straight Mondays at level=all, and the
    reader cannot tell an accurate zero from a broken pipe. It is accurate: the
    ledger `logs/cora-autowrite-audit.jsonl` is 0 bytes because
    `_autowrite_eligible` has never returned True in production -- 56 of 59
    graduated-trust shadow records classify Tier-2, machine-mined items carry no
    contributor id (so `contributor_recognized` / `authorized_owner` both
    fail-safe False), and the only two items ever to reach Tier-0 were
    #info-for-cora generics refused by a deliberate source exclusion.

    So this line reports the SCAN, not the writes: how many knowledge items were
    classified in the window and how they tiered. A zero next to "12 scanned,
    all Tier-2" is a working lane with nothing eligible. A zero next to
    "0 scanned" is a broken pipe. Those must not look the same.

    Fail-soft: "" on any error. The digest must never fail because of a
    diagnostic.
    """
    if stats.get("this_week"):
        return ""            # non-zero week: the numbers speak for themselves
    level = str(stats.get("level") or "off")
    if level == "off":
        return ("\n:pause_button: _Auto-write lane is OFF (CORA_AUTOWRITE_LIVE "
                "unset or `off`) -- every item routed to you for review. This "
                "zero is the setting, not a fault._")
    try:
        from cora import graduated_trust_shadow as gts
        scans = gts.read_autowrite_scans(days=days)
    except Exception:  # noqa: BLE001 -- diagnostics are never load-bearing
        return ""

    if scans:
        # AUTHORITATIVE: the lane's own per-run record of what it declined and
        # why. Preferred over inferring from shadow tiers, which cannot see a
        # downstream refusal at all.
        scanned = sum(int(r.get("scanned") or 0) for r in scans)
        agg: dict[str, int] = {}
        for r in scans:
            for reason, n in (r.get("refusals") or {}).items():
                agg[str(reason)] = agg.get(str(reason), 0) + int(n)
        if not scanned:
            return (f"\n:warning: _The auto-write lane ran {len(scans)} time(s) in "
                    f"{days}d at level=`{level}` and saw 0 knowledge items. "
                    f"Nothing is reaching it -- a starved pipe, not a quiet week._")
        top = ", ".join(f"`{r}` x{n}" for r, n in
                        sorted(agg.items(), key=lambda kv: -kv[1])[:3]) or "none recorded"
        return (f"\n:mag: _0 auto-writes from {scanned} item(s) scanned over "
                f"{len(scans)} run(s) at level=`{level}`. Declined by: {top}. "
                f"Every one routed to you instead -- the lane ran, nothing "
                f"qualified._")

    # FALLBACK: no scan rows yet (they only start accruing at the next review
    # run). Infer from the graduated-trust shadow log, which records how each
    # knowledge item TIERED but not what happened to it afterwards -- so this
    # branch deliberately does not claim to know why.
    try:
        rep = gts.build_report(days=days)
        scanned = int(rep.get("total_decisions") or 0)
    except Exception:  # noqa: BLE001
        return ""
    if not scanned:
        return (f"\n:warning: _0 auto-writes AND 0 knowledge items classified in "
                f"{days}d at level=`{level}` -- nothing reached the lane at all. "
                f"That is a starved or broken pipe, not a quiet week._")
    by_tier = rep.get("by_tier") or {}
    eligible = int(by_tier.get("0", 0)) + int(by_tier.get("1", 0))
    tiers = ", ".join(f"T{k}={v}" for k, v in sorted(by_tier.items())) or "none"
    if eligible:
        return (f"\n:mag: _0 auto-writes from {scanned} knowledge item(s) "
                f"classified in {days}d ({tiers}) at level=`{level}`. "
                f"{eligible} tiered to 0/1, so a downstream rule declined them "
                f"(the #info-for-cora source exclusion is the usual one). Exact "
                f"reasons start being recorded at the next review run._")
    return (f"\n:mag: _0 auto-writes because nothing was ELIGIBLE: {scanned} "
            f"knowledge item(s) classified in {days}d ({tiers}) at "
            f"level=`{level}`. Machine-mined items carry no contributor, so "
            f"they tier to 2 and route to you by design -- the lane is working, "
            f"the supply is Tier-2._")


def _decisions_inbox_line(days: int = 7) -> str:
    """One fail-soft context line about the Fork-4 decisions inbox. "" when
    empty or on any error -- the digest must never fail because of the inbox.

    Wording is LIFETIME counts (D-051 digest-awaiting-count-monotonic): the
    ledger is append-only and the Cowork cascade has no hook to mark rows
    promoted, so claiming "N awaiting promotion" would overstate forever once
    anything is promoted. The .md itself is the authoritative to-promote view."""
    try:
        from cora.decision_inbox import inbox_stats, _inbox_path
        s = inbox_stats(days=days)
        if not s.get("total"):
            return ""
        # NAME THE ACTOR AND THE LANE. "accepted" read as "auto-writes that
        # stood" inside a message headlined "Cora auto-learned this week" -- it
        # is neither: every one of these rows is Harrison's own tap on a
        # DECISION card (all 70 live rows are via=one_tap_button), a different
        # lane, a different ledger, a different actor. It was also the only
        # non-zero number in the DM, so it visually masked the 0/0/0 and made a
        # structurally-dead lane look like a quiet one.
        return (f"\n:inbox_tray: _Decision cards YOU filed (a separate lane, not "
                f"auto-writes): {s['total']} all-time ({s['recent']} in the last "
                f"{days}d) -- review/promote from {_inbox_path().name}_")
    except Exception:  # noqa: BLE001
        return ""


def deliver(stats: dict, items: list[dict], days: int = 7) -> bool:
    from slack_sdk import WebClient

    from cora import slack_egress
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("autowrite-digest: SLACK_BOT_TOKEN not set -- cannot DM")
        return False
    fallback, blocks = kr.build_autowrite_digest_blocks(items)
    summary = (f"{fallback}\n_This week {stats['this_week']} · last week "
               f"{stats['prev_week']} · reverts this week {stats['reverts_this_week']} · "
               f"level={stats['level']}_")
    summary += _why_zero_line(stats, days=days)
    summary += _decisions_inbox_line(days=days)
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": summary}}] + blocks[1:]
    try:
        safe_fallback = slack_egress.sanitize_text(summary)
    except Exception:  # noqa: BLE001
        safe_fallback = summary
    try:
        client = WebClient(token=token)
        resp = client.conversations_open(users=[kr.HARRISON_SLACK_USER_ID])
        client.chat_postMessage(channel=resp["channel"]["id"], text=safe_fallback[:3000], blocks=blocks)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("autowrite-digest: DM failed: %s", exc)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly Cora auto-write digest (DM to Harrison).")
    ap.add_argument("--dry-run", action="store_true", help="Print, do not DM.")
    ap.add_argument("--force", action="store_true", help="DM even if there was no activity.")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    now_ts = datetime.now(timezone.utc).timestamp()
    stats, items = build_digest(now_ts, days=args.days)
    log.info("autowrite-digest: this_week=%d prev_week=%d reverts=%d level=%s",
             stats["this_week"], stats["prev_week"], stats["reverts_this_week"], stats["level"])

    # Fork 4: a week with newly-accepted decisions is oversight-relevant even if
    # no auto-write happened -- the inbox line must not wait for a busier week.
    inbox_recent = 0
    try:
        from cora.decision_inbox import inbox_stats
        inbox_recent = int(inbox_stats(days=args.days).get("recent", 0))
    except Exception:  # noqa: BLE001 -- inbox is fail-soft for this digest
        inbox_recent = 0

    if (not items and stats["reverts_this_week"] == 0 and inbox_recent == 0
            and not args.force):
        log.info("No auto-write or decisions-inbox activity this week -- no DM "
                 "(use --force to send anyway).")
        return 0
    if args.dry_run:
        for it in items:
            log.info("[DRY RUN] %s tier=%s %s", it.get("update_type"), it.get("tier"),
                     str(it.get("summary", ""))[:120])
        why = _why_zero_line(stats, days=args.days)
        if why:
            log.info("[DRY RUN]%s", why.replace("\n", " "))
        line = _decisions_inbox_line(days=args.days)
        if line:
            log.info("[DRY RUN]%s", line.strip())
        return 0
    ok = deliver(stats, items, days=args.days)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
