"""Step 7.5 queue reconciliation for the 2026-09-02 windowless-tasks bundle.

Transitions ONLY the seeds this branch actually closed. Dry-run by default;
`--apply` performs the writes through
``code_queue.process_queue_action(...)`` -- never by hand-editing the jsonl or
the backlog (loop step 7.5). Without this the queue reads stale-positive and
the Monday menu re-offers shipped work (the 2026-07-31 incident: 14 D-095 seeds
still PROPOSED after merge).

    SHIPPED
      cq-4708076d7bb4  Windowless scheduled tasks: pythonw run_hidden launcher
                       + estate rewrap. Built: deployment/run_hidden.py,
                       deployment/rewrap-tasks-hidden.ps1,
                       deployment/_task-action.ps1, all 81 setup scripts routed
                       through the shared helper, CREATE_NO_WINDOW on all 13
                       helper spawns, restart-cora launcher handling, the
                       backup job's background I/O mode, and a
                       check_windowless_launcher health check.

    DISMISSED -- premise overturned, no code needed
      cq-ab3b92952077  "fix-watchdog-task-settings PS1 fails: PowerShell's
                       ScheduledTask MultipleInstances enum has no StopExisting
                       -- must set via COM or XML re-register."
                       VERIFIED 2026-09-02: the setting is ALREADY correct.
                       `Export-ScheduledTask -TaskName cora-watchdog` contains
                           <MultipleInstancesPolicy>StopExisting</MultipleInstancesPolicy>
                       The seed rests on a misread of the CIM enum:
                       `(Get-ScheduledTask cora-watchdog).Settings.MultipleInstances`
                       reads back $null even though the XML is right, so the
                       existing Set-ScheduledTask in
                       setup-cora-watchdog-task.ps1 DID take effect. There is
                       no bug to fix, and the COM/XML re-register the seed asks
                       for would be an invasive change to the most
                       recovery-critical task in the estate for no gain.
                       Deliberately NOT folded into this bundle for that reason.

NOT closed by this branch (and NOT touched here):
      cq-df6142ed8856  the scheduled-task clock-collision alarm miscount. This
                       bundle changes no trigger and no schedule, so that seed
                       is untouched and still open.

Run (from the repo root, after the FF-merge):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_windowless_tasks_2026-09-02.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_windowless_tasks_2026-09-02.py --apply
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
    "cq-4708076d7bb4": "windowless scheduled tasks -- launcher + estate rewrap + setup parity",
}

DISMISSED: dict[str, str] = {
    "cq-ab3b92952077": (
        "premise overturned -- the watchdog XML already reads "
        "MultipleInstancesPolicy=StopExisting; the blank value is a PowerShell "
        "CIM enum display artifact, so there is no bug to fix"
    ),
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Perform the transitions. Omitted = report only.")
    args = ap.parse_args(argv)

    print(f"Step 7.5 -- windowless-tasks bundle ({'APPLY' if args.apply else 'DRY-RUN'})\n")

    rc = 0
    for action, label_prefix, table in (
        (code_queue.ACTION_MARK_SHIPPED, "SHIPPED  ", SHIPPED),
        (code_queue.ACTION_DISMISS, "DISMISSED", DISMISSED),
    ):
        for cq_id, label in table.items():
            if not args.apply:
                print(f"  [dry-run] would mark {label_prefix.strip()}  {cq_id}  {label}")
                continue
            try:
                result = code_queue.process_queue_action(action, cq_id, HARRISON_ID)
                print(f"  {label_prefix}  {cq_id}  {label}  -> {result}")
            except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
                print(f"  FAILED     {cq_id}  {label}  -> {type(exc).__name__}: {exc}")
                rc = 1

    print("\n  NOT closed by this branch (leave open):")
    print("    cq-df6142ed8856  clock-collision alarm miscount -- this bundle changes no trigger")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
