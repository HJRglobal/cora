#!/usr/bin/env python3
"""One-shot seed: the LEX-LLC DDD service-definition retrieval item (Slice 1h).

Background (finding 6, hardening spec 2026-07-28 sec.1-pre): Harrison asked in
#lex-hcbs (08:52 MST 2026-07-28) for the LEX-LLC DDD service definitions (RSP / HAH /
ATC). The explicit-queue path THROTTLE-DROPPED the confirmed ask with zero ledger
events, so the build request was lost. Slice 1g fixes the drop going forward (the
founder is throttle-exempt and a confirmed ask never vanishes); this script backfills
the specific item that was lost so it is not forgotten.

Root cause of the underlying RETRIEVAL miss is PROVEN: the same question, re-asked with
the acronyms EXPANDED, answered fully from the already-ingested DDD manuals -- so the
miss is acronym-FORM embedding distance, not missing content. The operative fix is an
alias/glossary layer, not a re-ingest.

Scope of the seeded build item (hardening-spec fold amendment F2 -- keep it to):
  (a) an alias/glossary layer mapping DDD acronyms <-> full service names
      (the f3e-sku-aliases pattern, or a known-answers entry under LEX),
  (b) a verified re-chunk ONLY where extraction is provably poor, and
  (c) Notion sync.
It explicitly does NOT include entity re-tagging -- Part-2 Slice 2-2 of the same
session ships entity-tag normalization; this item cross-references it rather than
duplicating it. The seed lands APPROVED; the fix itself is a future session.

PHI/D-082: entity=LEX, so seed_item redacts the representative and scrubs the evidence
to a pointer only (no raw text at rest). Only the generic, non-PHI title/summary above
are persisted.

Idempotent: skipped if the fingerprint is already in the ledger. DRY-RUN BY DEFAULT --
pass --apply to write. Standalone script; does NOT import the bot process; no restart.

Usage:
    python scripts/seed_lex_ddd_item_2026-07-28.py            # dry-run
    python scripts/seed_lex_ddd_item_2026-07-28.py --apply     # write the ledger
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora import code_queue  # noqa: E402

SEED = dict(
    kind="feature",
    severity="P2",
    signal="explicit",
    status="APPROVED",
    entity="LEX",
    subsystem_guess="lex_ddd_retrieval",
    title="LEX-LLC DDD service-definition retrieval (RSP/HAH/ATC) -- alias layer + re-chunk/ingest",
    summary=(
        "Acronym-form asks (RSP/HAH/ATC) miss the ingested DDD manuals -- the "
        "acronym-expanded re-ask answers fully, so the gap is acronym-form embedding "
        "distance, not missing content. Fix: (a) an acronym<->full-name alias/glossary "
        "layer (f3e-sku-aliases pattern or a LEX known-answers entry), (b) a verified "
        "re-chunk only where extraction is provably poor, (c) Notion sync. Entity "
        "re-tagging is out of scope here -- see Part-2 entity-tag normalization."
    ),
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually write the ledger (default: dry-run print only).")
    args = ap.parse_args()

    existing = code_queue.find_fingerprint(SEED["signal"], SEED["title"])
    if existing:
        print(f"SKIP (already seeded {existing}): {SEED['title']}")
        return 0

    if args.apply:
        cid = code_queue.seed_item(**SEED)
        print(f"SEEDED {cid} [{SEED['severity']}/{SEED['status']}]: {SEED['title']}")
        item = code_queue.get_item(cid)
        # Confirm the LEX at-rest redaction landed (representative + evidence pointer-only).
        print(f"  representative persisted: {item.get('representative')!r} (expect '')")
        print(f"  evidence[0]: {item.get('evidence', [{}])[0]} (expect no 'note' key)")
        try:
            code_queue.render_backlog()
            print("  backlog regenerated.")
        except Exception as exc:  # noqa: BLE001
            print(f"  (backlog render skipped: {exc})")
    else:
        print(f"WOULD SEED [{SEED['severity']} {SEED['kind']} {SEED['entity']} -> "
              f"{SEED['status']}]: {SEED['title']}")
        print("Dry-run: re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
