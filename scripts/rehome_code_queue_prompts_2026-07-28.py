#!/usr/bin/env python3
"""One-shot migration: re-home mis-homed code-session prompt files (v1.1 hardening,
Slice 1d).

Field defect: the pre-hardening generator wrote kickoff prompts to the REPO ``_notes``
folder instead of the spec'd Founder-OS ``_shared/projects/cora/_notes`` folder. This
migration moves each mis-homed prompt to the Founder-OS ``_notes``, backfills the ledger
prompt_path (a ``staged`` event with ``rehomed=true``), and deletes the repo copy.

CONSERVATIVE (D-051 over-deletion guard): only files that are (a) referenced by a
``staged`` event in the queue ledger, (b) currently under the repo ``_notes`` dir, (c)
have a ``cora-code-prompt`` basename, and (d) still exist are touched. This NEVER globs
or deletes the repo ``_notes`` folder blindly.

DRY-RUN BY DEFAULT -- pass --apply to move+delete. Standalone script -- does NOT import
the bot process; no restart needed. Harrison runs --apply (needs the G: mount).

Usage:
    python scripts/rehome_code_queue_prompts_2026-07-28.py            # dry-run: print plan
    python scripts/rehome_code_queue_prompts_2026-07-28.py --apply    # move + backfill + delete
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora import code_queue  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Actually move+backfill+delete (default: dry-run print only).")
    args = ap.parse_args()

    plan = code_queue.plan_prompt_rehome()
    if not plan:
        print("Nothing to re-home: no staged prompt under the repo _notes folder.")
        return 0

    print(f"{len(plan)} prompt file(s) to re-home from the repo _notes -> Founder-OS _notes:\n")
    for a in plan:
        print(f"  [{a['id']}]")
        print(f"    from: {a['src']}")
        print(f"    to:   {a['dst']}")

    print("")
    if not args.apply:
        print(f"Dry-run: {len(plan)} file(s) would move. Re-run with --apply.")
        return 0

    done = code_queue.apply_prompt_rehome(plan)
    ok = sum(1 for d in done if d.get("ok"))
    fail = [d for d in done if not d.get("ok")]
    print(f"Done. Re-homed {ok}/{len(done)}.")
    for d in fail:
        print(f"  FAILED [{d['id']}]: {d.get('error')}")
    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
