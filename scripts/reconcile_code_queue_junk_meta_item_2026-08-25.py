"""Step 7.5 ADDENDUM -- junk meta-item cleanup (authored by Cowork 2026-08-25).

WHY THIS EXISTS
    `cq-983b14c16ecb` ("stage a code-session prompt for cq-abcdef123456") is the
    SECOND junk meta-item minted by the verb-less typed stage request -- the same
    class as `cq-a43ef0ccb644`, which session #6's step 7.5 dismissed. The
    mechanism itself was closed by `cq-5414c154b213`, which SHIPPED at
    2026-08-25 07:49 AZ.

    This instance was minted 2026-08-25 01:48 UTC -- BEFORE that fix merged
    (`main` fast-forwarded to 8134020 at 2026-08-25 07:48 AZ). So the fix is not
    implicated; this row is residue from the window before it landed. It is pure
    junk: a P2 feature row whose stated fix target is the literal placeholder
    `cq-abcdef123456`, auto-APPROVED at mint, with an auto-generated kickoff
    prompt staged on Drive that any session could have picked up and run.

    The staged Drive kickoff was already removed by the 2026-08-25 Cowork
    cascade session:
        2026-08-25_fndr_cora-code-prompt-stage-a-code-session-prompt-for-cq-abcdef123456-ba7b39.md
        Drive file id 1Rq4RggvGSwwLglHczoMyCv4Y1ek6Usoy -> moved to Drive TRASH
    Trash, deliberately, not permanent deletion -- it is recoverable for 30 days
    if it turns out to have carried anything worth keeping.

    So the file half is done and this script closes the ledger half. Note the
    ordering rule from #6's reconcile is satisfied in the other direction: a
    dismissal here cannot leave a live `prompt_path` pointing at a pasteable
    file, because the file is already gone. The check below re-asserts that
    rather than assuming it.

    Nothing else is touched. No build shipped in this pass, so nothing is marked
    SHIPPED, and no APPROVE flips are made.

USAGE (from the repo root)
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_junk_meta_item_2026-08-25.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_junk_meta_item_2026-08-25.py --apply

    Safe to run before OR after the #7 FF-merge -- it touches only the queue
    event ledger, which is not changed by that branch.
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

# Already trashed by Cowork 2026-08-25. Checked, never written to -- if this path
# is somehow live again, that is a finding, not something to silently delete.
TRASHED_KICKOFF = Path(
    r"G:\My Drive\HJR-Founder-OS\_shared\projects\cora\_notes"
    r"\2026-08-25_fndr_cora-code-prompt-stage-a-code-session-prompt-for-cq-abcdef123456-ba7b39.md"
)

DISMISS: dict[str, str] = {
    "cq-983b14c16ecb": (
        "junk meta-item minted by the verb-less typed stage request (2026-08-25 "
        "01:48Z, before cq-5414c154b213 merged); its auto-staged kickoff was "
        "trashed on Drive 2026-08-25"
    ),
}


def _act(action: str, cq_id: str, label: str, apply: bool, verb: str) -> int:
    if not apply:
        print(f"  [dry-run] would {verb:<10} {cq_id}  {label}")
        return 0
    try:
        result = code_queue.process_queue_action(action, cq_id, HARRISON_ID)
        print(f"  {verb.upper():<10} {cq_id}  {label}  -> {result}")
        return 0
    except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
        print(f"  FAILED     {cq_id}  {label}  -> {type(exc).__name__}: {exc}")
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Perform the transition. Omitted = report only.")
    args = ap.parse_args(argv)

    print(f"Step 7.5 addendum -- junk meta-item cleanup "
          f"({'APPLY' if args.apply else 'DRY-RUN'})\n")

    rc = 0
    for cq_id, label in DISMISS.items():
        rc |= _act(code_queue.ACTION_DISMISS, cq_id, label, args.apply, "dismiss")

    # Assert, do not assume: the staged kickoff must not be live.
    if TRASHED_KICKOFF.exists():
        print(f"\n  WARNING: the kickoff is present again at\n    {TRASHED_KICKOFF}"
              f"\n  It was trashed on 2026-08-25. Someone restored it or a new one "
              f"was minted -- investigate before pasting anything from it.")
        rc = 1
    else:
        print(f"\n  Confirmed absent (trashed 2026-08-25): {TRASHED_KICKOFF.name}")

    print("\n  Deliberately NOT touched:")
    print("    cq-5414c154b213  the fix for this class -- already SHIPPED 2026-08-25 07:49 AZ")
    print("    cq-288edaba659d  the 2 unaudited Class-B kinds -- still awaiting Harrison's ruling")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
