#!/usr/bin/env python3
"""One-shot seed: 5 items from the 2026-07-30 flywheel intake/delivery audit.

Background: Harrison asked whether Cora is gathering knowledge/decisions correctly
and whether his DM receives the interactive items. The 2026-07-30 Cowork audit
(capture: 00-Founder/_session-captures/2026-07/2026-07-30_fndr_morning-smokes-s2-s3-results.md,
Part 2) found the delivery/one-tap loop healthy but the knowledge-conversion layer
starved, plus one broken intake surface and a set of live rendering defects.
Harrison approved seeding these on 2026-07-30 ("proceed with all recommended next moves").

Items (all non-LEX; titles/summaries are generic build text):
  1. P1 feature -- #info-for-cora human-contribution intake never queues items
  2. P2 bug     -- missed-message catch-up card renders raw Slack user IDs
  3. P2 bug     -- outbound long-message truncation family (briefing/memo/plate)
  4. P3 bug     -- friction-mining efficiency-card rendering + near-dup findings
  5. P3 bug     -- code-queue capture-card empty provenance line

The gap_autofill throughput calibration is deliberately NOT seeded here -- that is a
design decision (Fable session), staged separately as a kickoff prompt.

Idempotent: each item skipped if its fingerprint is already in the ledger.
DRY-RUN BY DEFAULT -- pass --apply to write. Standalone; does NOT import the bot
process; no restart needed.

Usage:
    python scripts/seed_flywheel_audit_items_2026-07-30.py           # dry-run
    python scripts/seed_flywheel_audit_items_2026-07-30.py --apply   # write ledger
"""

import argparse
import sys
from pathlib import Path

try:  # dotenv optional -- seed path needs no env vars
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora import code_queue  # noqa: E402

SEEDS = [
    dict(
        kind="feature",
        severity="P1",
        signal="explicit",
        status="APPROVED",
        entity="FNDR",
        subsystem_guess="info_for_cora_intake",
        title="#info-for-cora human contributions never reach the knowledge queue -- route as knowledge items + handle connector posts",
        summary=(
            "Audit 2026-07-30: @Cora-mentioned contributions in #info-for-cora get an "
            "instant thread ack but ZERO ever became queue items (Hannah's 2026-06-17 "
            "batch incl. an explicit recorded decision: 0 hits in ledger + 19,129-row "
            "archive; the 26 channel-linked archive items are all sweep-mined, not "
            "contributions). Un-@-mentioned Cowork-connector posts (Harrison 7/10 "
            "pricing note) never reach the message path at all -- no ack, no capture. "
            "Fix: (a) route channel contributions into the KNOWLEDGE stream per WS17-B "
            "(a #info-for-cora generic rides the knowledge stream, not operational), "
            "(b) make the ack truthful about what was logged, (c) either process "
            "connector-authored posts or post pinned channel guidance that @Cora "
            "mention is required. Evidence: capture 2026-07-30 Part 2."
        ),
    ),
    dict(
        kind="bug",
        severity="P2",
        signal="explicit",
        status="APPROVED",
        entity="FNDR",
        subsystem_guess="missed_message_catchup",
        title="Missed-message catch-up cards render raw Slack user IDs -- resolve sender like sibling cards",
        summary=(
            "7/27 17:07 card: 'Missed message -- U0B7BV5688Y in "
            "#f3-hq-inventory-adjustments' while sibling cards in the same volley "
            "resolved names. The bundle-v2 S3 resolve_slack_mentions fix covered the "
            "knowledge-review owner-card renderer only; the missed_message_catchup "
            "card builder has its own render path. Apply the same defensive resolve "
            "at that chokepoint. Evidence: capture 2026-07-30 Part 2."
        ),
    ),
    dict(
        kind="bug",
        severity="P2",
        signal="explicit",
        status="APPROVED",
        entity="FNDR",
        subsystem_guess="outbound_message_segmentation",
        title="Long outbound DMs truncate mid-word -- briefing/strategy-memo/plate share one max-tokens + naive-split family",
        summary=(
            "Briefings 7/27 + 7/29 ended mid-word ('How can I' / '...priorit'; 7/28 + "
            "7/30 complete -- length-dependent max-token cutoff). Strategy memo 7/26 "
            "split the word 'for' across two Slack messages. Plate replies hit the "
            "known 1024-cap mid-link truncation (both models, pre-existing bundle "
            "candidate -- fold it here). One family: fixed output caps + naive "
            "character splitting. Fix: size caps to content or detect truncation and "
            "continue; split multi-message posts on line/sentence boundaries; never "
            "end a message mid-word/mid-link. Evidence: capture 2026-07-30 Part 2."
        ),
    ),
    dict(
        kind="bug",
        severity="P3",
        signal="explicit",
        status="APPROVED",
        entity="FNDR",
        subsystem_guess="friction_mining",
        title="Friction-mining efficiency cards: mid-word 400-char cut, broken _Source_ italic on URLs, near-dup findings, inconsistent counts",
        summary=(
            "7/27 card set: every Recommendation hard-cuts ~400 chars mid-word with no "
            "ellipsis; the _Source:_ italic wrapper breaks when the URL contains "
            "underscores (unclosed italic); 3 of the 5 cards were near-duplicate "
            "variants of the same SSR-Reports finding (finding-level dedup missing "
            "before the 5-cap, so dupes crowd out distinct findings); one card said "
            "'observed 13x' in the body and '10x' in the coras_read line. Fix render "
            "caps w/ ellipsis, escape or drop the italic wrapper around URLs, dedupe "
            "findings pre-cap, reconcile frequency sources. Evidence: capture "
            "2026-07-30 Part 2."
        ),
    ),
    dict(
        kind="bug",
        severity="P3",
        signal="explicit",
        status="APPROVED",
        entity="FNDR",
        subsystem_guess="code_queue",
        title="Code-queue capture cards emit an empty provenance line ('<slack://channel?id=...> ts ``') when ts is missing",
        summary=(
            "7/28 07:13 + 07:16 capture cards (the RepRally pair) rendered a "
            "quote-line with a bare unlabeled slack:// URI and an EMPTY ts between "
            "backticks. When provenance fields are missing, omit the line entirely "
            "(or fill both); never render empty backticks/bare URIs. Evidence: "
            "capture 2026-07-30 Part 2."
        ),
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the 2026-07-30 flywheel-audit items.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the ledger (default: dry-run print only).")
    args = ap.parse_args()

    wrote = 0
    for seed in SEEDS:
        existing = code_queue.find_fingerprint(seed["signal"], seed["title"])
        if existing:
            print(f"SKIP (already seeded {existing}): {seed['title'][:70]}")
            continue
        if args.apply:
            cid = code_queue.seed_item(**seed)
            if cid is None:
                print(f"REFUSED (PHI gate): {seed['title'][:70]}")
                continue
            wrote += 1
            print(f"SEEDED {cid} [{seed['severity']}/{seed['status']}]: {seed['title'][:70]}")
        else:
            print(f"WOULD SEED [{seed['severity']} {seed['kind']} {seed['entity']} -> "
                  f"{seed['status']}]: {seed['title'][:70]}")

    if args.apply and wrote:
        ok = code_queue.render_backlog()
        print(f"backlog regenerated: {ok} (False = G: unreachable from this host; "
              f"regenerates at the next status transition / Monday run)")
    elif not args.apply:
        print("Dry-run: re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
