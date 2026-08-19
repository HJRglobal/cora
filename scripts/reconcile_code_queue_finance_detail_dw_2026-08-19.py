"""Step 7.5 queue reconciliation for session #3 (finance detail + DW integrity).

Transitions ONLY the seeds this branch actually closed, plus the two dispositions
the 2026-08-18 feature-priority review ordered. Dry-run by default; `--apply`
performs the writes through
``code_queue.process_queue_action(...)`` -- never by hand-editing the jsonl or
the backlog (loop step 7.5).

Without this the queue reads stale-positive and the Monday menu re-offers shipped
work (the 2026-07-31 incident: 14 D-095 seeds still PROPOSED after merge). The
inverse failure is quieter and just as bad: marking a seed SHIPPED because SOME
of it landed. One item below is deliberately left OPEN for exactly that reason.

Evidence of record:
  _shared/projects/cora/_notes/2026-08-19_fndr_cora-code-prompt-finance-detail-dw-integrity.md
  _shared/projects/cora/2026-08-19_fndr_cora-feature-priorities-decision-brief.md
  _shared/projects/cora/2026-08-19_fndr_approval-recon-and-tiered-approver-recommendation.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import code_queue  # noqa: E402

HARRISON_ID = "U0B2RM2JYJ1"

SHIPPED: dict[str, str] = {
    "cq-42787b27d4cb": "QBO detail-level gap -- branch (A): the renderer, not access (slice 1)",
    "cq-e3f057668e1f": "QBO vendor spend + category expense detail tools (slice 1)",
    "cq-157a961853c4": "BalanceSheet as_of_date ignored by QBO -> end_date + macro refusal (slice 1b)",
    "cq-b0a847ef0c8e": "Slack file-upload lane: byte-capable, scope-probed, honest failure (slice 2)",
    "cq-64a8f5e3e654": "Long-DM mid-word truncation family -- detect + continue + boundary split (slice 3)",
    "cq-c51123b0ad07": "xlsx export of displayed finance data (slice 4)",
    "cq-345df41234fc": "content_guard refusals refund the DW quota slot (slice 5)",
    "cq-443695ccaa60": "Conversational DW job listing -- discoverability + status detail (slice 6)",
    "cq-8f462b5701c8": "DW concurrent-resubmission dedup -- VERIFIED present, race exercised + pinned (slice 6)",
    "cq-e6ab72d91735": "Approval recon + tiered-approver recommendation doc (slice 7)",
    "cq-795b9caa0b3a": "Sara Fonseca Asana GID roster mapping (slice 11)",
    # Ordered by the 8/18 review; verified 2026-08-18 -- the weekly-check SKILL.md
    # already carries the full D-184 read-back section. No repo change needed.
    "cq-54295b43e12d": "weekly Slack-clarity check already carries D-184 read-back semantics",
}

DISMISS: dict[str, str] = {
    # Harrison 2026-08-18: the AZ DDD revalidation is resolved (TOM 1zzzzz /
    # decisions.md 2026-08-18 [FNDR/CORA]).
    "cq-40cb213854a7": "AZ DDD revalidation resolved -- seed no longer needed",
}

LEFT_OPEN: dict[str, str] = {
    "cq-f330d402e5cd": (
        "PART (a) SHIPPED -- Justin's DM is now necessity-gated with plain-language "
        "purpose lines. PART (b) NOT built and should not be built as specified: a "
        "Confirm/Cancel card captures one bit, while a confirmed intercompany "
        "pairing needs three values (left, right, opposite_signs) and the map's own "
        "header says the sign convention cannot be inferred. 32 candidates would "
        "also be 5x MAX_ITEM_CARDS and 32 separate DMs -- worse than the list it "
        "replaces. The right shape is a pairing worksheet; that is a design call "
        "for Harrison. Forecast-delta cards: F9 verified STILL PARKED (pairs: [])."
    ),
    # Stretch items (kickoff slice 8) -- capacity went to slices 0-7 and 9-11.
    "cq-6290cf5c1a4d": "stretch -- not started (8/7 BDM+HJRP tie-out investigation)",
    "cq-2ff81156f53a": "stretch -- not started (gsheets SA Drive-metadata scope 403)",
    "cq-ad74f3908e8d": "stretch -- not started (spreadsheet_build tab-name fallback)",
    "cq-144af8eaaf54": "stretch -- not started (Asana My-Tasks visibility answer)",
}

# Parked to the 9/1 review per TOM 1zzzzz. Listed so a future reader can see they
# were considered and deliberately untouched -- NOT transitioned here.
LEFT_STAGED_DELIBERATELY: tuple[str, ...] = (
    "cq-4c915df3e79a", "cq-9b368e8d43a7", "cq-90e69cba59b3", "cq-3e7a465040f1",
    "cq-ac9bb3868f02", "cq-5f48f328687b", "cq-8b51427896b8", "cq-4d5c88973e7f",
    "cq-ad0e4becafce", "cq-03ad3441d9f7",
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Perform the transitions. Omitted = report only.")
    args = ap.parse_args(argv)

    print(f"Step 7.5 -- session #3 finance detail + DW integrity "
          f"({'APPLY' if args.apply else 'DRY-RUN'})\n")

    rc = 0
    for action, label, table in (
        (code_queue.ACTION_MARK_SHIPPED, "SHIPPED", SHIPPED),
        (code_queue.ACTION_DISMISS, "DISMISS", DISMISS),
    ):
        for cq_id, why in table.items():
            if not args.apply:
                print(f"  [dry-run] would mark {label:<8} {cq_id}  {why}")
                continue
            try:
                result = code_queue.process_queue_action(action, cq_id, HARRISON_ID)
                print(f"  {label:<8} {cq_id}  {why}  -> {result}")
            except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
                print(f"  FAILED   {cq_id}  {why}  -> {type(exc).__name__}: {exc}")
                rc = 1

    print("\n  Left OPEN on purpose (do NOT mark these shipped):")
    for cq_id, why in LEFT_OPEN.items():
        print(f"    {cq_id}  {why}")

    print("\n  Left STAGED deliberately (parked to the 9/1 review; not re-offered):")
    print("    " + " | ".join(LEFT_STAGED_DELIBERATELY))

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
