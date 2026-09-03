"""Step 7.5 queue reconciliation for the 2026-09-03 claude-workspace-mirror bundle.

Transitions ONLY the seed this branch closed. Dry-run by default; `--apply`
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

This seed was APPROVED via the MCP seed tool (no card posted), so no Stage tap
was ever required -- exactly as cq-4708076d7bb4 shipped on 9/2. The nightly
health check's "approved, no kickoff" WARN for this id is informational until
this reconcile runs.

Run (from the repo root, after the FF-merge):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_claude_mirror_2026-09-03.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_claude_mirror_2026-09-03.py --apply
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
    "cq-621dfad586aa": "claude-workspace mirror + midday sync (knowledge parity)",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="Perform the transition. Omitted = report only.")
    args = ap.parse_args(argv)

    print(f"Step 7.5 -- claude-workspace-mirror bundle ({'APPLY' if args.apply else 'DRY-RUN'})\n")

    rc = 0
    for cq_id, label in SHIPPED.items():
        if not args.apply:
            print(f"  [dry-run] would mark SHIPPED  {cq_id}  {label}")
            continue
        try:
            result = code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cq_id, HARRISON_ID)
            print(f"  SHIPPED  {cq_id}  {label}  -> {result}")
        except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
            print(f"  FAILED   {cq_id}  {label}  -> {type(exc).__name__}: {exc}")
            rc = 1

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
