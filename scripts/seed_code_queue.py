#!/usr/bin/env python3
"""One-shot seed for the code-session queue (Slice 1h).

Seeds the ledger so the first Monday menu is real:
  * item #0 -- the phantom-failure create bug found by the 2026-07-27 post-merge
    smoke (a real, evidence-backed defect); and
  * the 9 genuinely-open items from the 2026-07-10 wishlist reconciliation
    (_shared/projects/cora/2026-07-10_fndr_cora-wishlist-reconciliation.md).

Idempotent: an item whose fingerprint is already in the ledger is skipped, so
re-running never duplicates. DRY-RUN BY DEFAULT -- pass --apply to write.

Standalone script -- does NOT import the bot process. No restart needed.

Usage:
    python scripts/seed_code_queue.py            # dry-run: print what would seed
    python scripts/seed_code_queue.py --apply    # write the ledger
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora import code_queue  # noqa: E402

# (kind, severity, title, summary, entity, signal, status, subsystem_guess)
SEEDS: list[dict] = [
    # ── item #0: the real bug the 7/27 post-merge smoke found (evidence in hand) ──
    dict(
        kind="bug", severity="P3", signal="tool_error", status="APPROVED",
        entity="FNDR", subsystem_guess="asana_create_task",
        title="asana_create_task phantom-failure leaves an UNSTAMPED task",
        summary=(
            "When the Asana create POST lands server-side but the client read times "
            "out (observed 2026-07-27 20:58: task 1216937509601843 existed but the "
            "call reported 'read operation timed out'), no gid is returned so the "
            "Slice-5 Entity/Status/Priority stamping PUT never runs; on retry the "
            "creation-time dedup guard says 'already exists' and nobody backfills the "
            "stamps -> the task lingers with Entity/Status/Priority=null. Fix: on "
            "duplicate-found, verify the existing task's stamps and backfill-PUT if null."
        ),
    ),
    # ── the 9 genuinely-open wishlist-reconciliation items (2026-07-10) ──────────
    dict(kind="feature", severity="P3", signal="friction", status="APPROVED",
         entity="FNDR", subsystem_guess="knowledge_flywheel",
         title="Flywheel activation tail (Interactivity ON -> one-tap smoke -> kb_miss floor)",
         summary="The North Star knowledge loop is live but starving; highest-leverage, not on any wishlist."),
    # #2 shopify -- SHIPPED 7/21 (seeded as SHIPPED for the historical record).
    dict(kind="feature", severity="P3", signal="friction", status="SHIPPED",
         entity="F3E", subsystem_guess="f3e_shopify_set_inventory",
         title="f3e_shopify_set_inventory conversational DTC inventory tool",
         summary="Shipped 2026-07-21 (SKU alias map + delta + bulk). Kept as a SHIPPED record."),
    dict(kind="config", severity="P3", signal="friction", status="APPROVED",
         entity="LEX", subsystem_guess="known_answers",
         title="Curated known-answers adds (LEX cap table/audit, BDM role/brand, OSN cross-refs)",
         summary="Cheap 10-minute known-answers additions via the live write path; no code build -- config."),
    dict(kind="feature", severity="P3", signal="friction", status="APPROVED",
         entity="BDM", subsystem_guess="brand_voice_check",
         title="bdm_brand_voice_check / portfolio brand-voice extension",
         summary="f3e_brand_voice_check covers F3 brands only; no UFL/OSN/Lex/Podcast voice checking."),
    dict(kind="feature", severity="P3", signal="friction", status="APPROVED",
         entity="OSN", subsystem_guess="osn_dna_ar_status",
         title="osn_dna_ar_status (DNA-specific AR tracker view)",
         summary="qbo_get_ar_aging covers aggregate AR; the DNA-specific tracker view does not exist."),
    dict(kind="feature", severity="P3", signal="friction", status="APPROVED",
         entity="HJRP", subsystem_guess="hjrp_rogers_ranch_bookings",
         title="hjrp_rogers_ranch_bookings (blocked on a booking-data source)",
         summary="Blocked on an Airbnb/direct-booking data source."),
    dict(kind="feature", severity="P3", signal="friction", status="APPROVED",
         entity="LEX", subsystem_guess="lex_ar_audit",
         title="lex_lbhs_ar_aging / lex_audit_status (blocked on sources; P2)",
         summary="LBHS is not a QBO realm; no AR source since Rita Tracking paused. lex_audit_status also open."),
    dict(kind="feature", severity="P3", signal="friction", status="APPROVED",
         entity="HJRG", subsystem_guess="fndr_intercompany_check",
         title="fndr_intercompany_check (Justin's lane)",
         summary="Partially covered by QBO tools + cashflow; P2."),
    dict(kind="feature", severity="P3", signal="friction", status="APPROVED",
         entity="LEX", subsystem_guess="lex_revalidation_status",
         title="Revalidation-tool retirement/re-point after confirming the 6/30 outcome",
         summary="lex_revalidation_status deadline (2026-06-30) passed; confirm the outcome then retire or re-point."),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the ledger (default: dry-run print only).")
    args = ap.parse_args()

    seeded, skipped = 0, 0
    for s in SEEDS:
        existing = code_queue.find_fingerprint(s["signal"], s["title"])
        if existing:
            print(f"SKIP (already seeded {existing}): {s['title']}")
            skipped += 1
            continue
        if args.apply:
            cid = code_queue.seed_item(**s)
            print(f"SEEDED {cid} [{s['severity']}/{s['status']}]: {s['title']}")
            seeded += 1
        else:
            print(f"WOULD SEED [{s['severity']} {s['kind']} {s['entity']} -> {s['status']}]: {s['title']}")

    print("")
    if args.apply:
        print(f"Done. Seeded {seeded}, skipped {skipped} (already present).")
        # Regenerate the backlog view so it reflects the seeded items.
        try:
            code_queue.render_backlog()
        except Exception as exc:  # noqa: BLE001
            print(f"(backlog render skipped: {exc})")
    else:
        print(f"Dry-run: {len(SEEDS) - skipped} item(s) would be seeded, {skipped} already present. "
              "Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
