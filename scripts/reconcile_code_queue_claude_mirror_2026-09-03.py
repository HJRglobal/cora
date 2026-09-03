"""Step 7.5 queue reconciliation for the 2026-09-03 claude-workspace-mirror bundle.

Transitions ONLY the seeds this branch closed. Dry-run by default; `--apply`
performs the write through ``code_queue.process_queue_action(...)`` -- never by
hand-editing the jsonl or the backlog (loop step 7.5). Without this the queue
reads stale-positive and the Monday menu re-offers shipped work (the 2026-07-31
incident: 14 D-095 seeds still PROPOSED after merge).

    SHIPPED
      cq-621dfad586aa  Claude-workspace mirror + midday sync (knowledge parity).
                       Built: scripts/mirror_claude_workspace.py +
                       data/maps/claude-workspace-mirror.yaml (the deterministic,
                       no-LLM mirror of skills / Cowork task defs / agent memory
                       into the Founder OS, D-057 two-zone split); the
                       bootstrap.txt static-walk + drive_sweep `mirror` belt; the
                       cowork-cora-claude-mirror task + midday triggers on
                       kb-sync-static / session-capture; and the health-lane
                       observability (nightly + Monday digest). Seeded APPROVED
                       2026-09-02 on Harrison's in-session FIRE ruling; this
                       moves it APPROVED -> SHIPPED at merge.
      cq-11e9abda254a  D-057 IS LEAKING: the PARENT _shared/projects/cora folder
                       id pinned into KB_EXCLUDED_FOLDER_IDS (the drive_sweep
                       DOOR; the cora-mirror- title keyword stays the belt) +
                       the purge's folder-ancestry selector
                       (purge_cora_internal_kb.py --folder-id) that makes the
                       purge half executable -- the title heuristic could not
                       reach the >=157 token-less files. Added to this branch as
                       the 2026-09-03 pin commit (Harrison "A) Option 1"). The
                       purge --apply itself is Harrison-run inside the stop
                       window (runbook step 6); this records the CODE shipped.
                       GUARDED: this id is BLOCKED (rc 1, dry-run and apply)
                       unless the pin is actually present in
                       KB_EXCLUDED_FOLDER_IDS on the checked-out tree -- marking
                       "pin + purge" SHIPPED without the pin would be the
                       inverse of the 7/31 stale-positive incident.

Both seeds were APPROVED via the MCP seed tool (no card posted), so no Stage tap
was ever required -- exactly as cq-4708076d7bb4 shipped on 9/2. The nightly
health check's "approved, no kickoff" WARN for these ids is informational until
this reconcile runs.

Run (from the repo root, after the FF-merge):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_claude_mirror_2026-09-03.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_claude_mirror_2026-09-03.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import code_queue  # noqa: E402
from cora.kb_exclusions import KB_EXCLUDED_FOLDER_IDS  # noqa: E402

HARRISON_ID = "U0B2RM2JYJ1"

# The Drive folder id of _shared/projects/cora (cora <- projects <- _shared <-
# HJR-Founder-OS; resolved + verified live 2026-09-03). Must equal the id pinned
# in src/cora/kb_exclusions.py -- the precondition below checks exactly that.
CORA_WORKSPACE_FOLDER_ID = "1YNObhKwo8RITgrRbw3MFpf-0hIiLWTx9"


def _pin_present() -> str | None:
    """None when the D-057 parent pin is in the exclusion set on THIS tree; else the
    reason the cq-11e9abda254a transition is blocked."""
    if CORA_WORKSPACE_FOLDER_ID in KB_EXCLUDED_FOLDER_IDS:
        return None
    return (f"the D-057 parent pin {CORA_WORKSPACE_FOLDER_ID} is NOT in "
            f"KB_EXCLUDED_FOLDER_IDS on this tree -- the pin commit has not landed here")


PRECONDITIONS: dict[str, Callable[[], str | None]] = {
    "cq-11e9abda254a": _pin_present,
}

_TERMINAL_NOT_SHIPPABLE = frozenset({"DISMISSED", "SUPERSEDED"})


def _ascii(s: object) -> str:
    """process_queue_action's messages carry emoji; a redirected stdout on this host
    is cp1252 and would raise AFTER the ledger write, turning a success into a
    printed FAILED (D-051 pin lens MED-1). Fold to ASCII before printing."""
    return str(s).encode("ascii", "replace").decode("ascii")

SHIPPED: dict[str, str] = {
    "cq-621dfad586aa": "claude-workspace mirror + midday sync (knowledge parity)",
    "cq-11e9abda254a": "D-057 parent folder-id pin + purge folder-ancestry selector (drive_sweep door)",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Perform the transition. Omitted = report only.")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")  # belt: never die on a print
        except (ValueError, OSError):
            pass

    print(f"Step 7.5 -- claude-workspace-mirror bundle ({'APPLY' if args.apply else 'DRY-RUN'})\n")

    rc = 0
    for cq_id, label in SHIPPED.items():
        blocker = PRECONDITIONS.get(cq_id, lambda: None)()
        if blocker:
            print(f"  BLOCKED  {cq_id}  {label}  -> {blocker}")
            rc = 1
            continue
        # ACTION_MARK_SHIPPED is status-blind (it appends unconditionally), so the
        # status check lives here: idempotent on a re-run, and a terminal row is
        # never flipped to SHIPPED.
        rec = code_queue.get_item(cq_id)
        status = str((rec or {}).get("status", "")) if rec else None
        if rec is None:
            print(f"  NOT SHIPPED  {cq_id}  {label}  -> missing id (no queue item)")
            rc = 1
            continue
        if status == "SHIPPED":
            print(f"  SKIP (already shipped)  {cq_id}  {label}")
            continue
        if status in _TERMINAL_NOT_SHIPPABLE:
            print(f"  NOT SHIPPED  {cq_id}  {label}  -> status is {status}; refusing to flip a terminal row")
            rc = 1
            continue
        if not args.apply:
            print(f"  [dry-run] would mark SHIPPED  {cq_id}  {label}  (status {status})")
            continue
        try:
            outcome, msg = code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cq_id, HARRISON_ID)
            if outcome == "shipped":
                print(f"  SHIPPED  {cq_id}  {label}  -> {_ascii(msg)}")
            else:
                # process_queue_action reports not_authorized/error WITHOUT raising;
                # the label and the exit code must follow the real outcome.
                print(f"  NOT SHIPPED  {cq_id}  {label}  -> {outcome}: {_ascii(msg)}")
                rc = 1
        except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
            print(f"  FAILED   {cq_id}  {label}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
