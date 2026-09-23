"""Step 7.5 queue reconciliation for the Deposco order-push write path
(branch ``claude/deposco-push-write-path-2026-09-22``, bundle_id ``deposco-push``).

Transitions ONLY the seed this branch closed. Dry-run by default; ``--apply``
writes through the module's own writer -- ``code_queue.process_queue_action``
(carrying bundle_id + branch + commit on the shipped event) -- never by
hand-editing the jsonl or the backlog (loop step 7.5).

    SHIPPED (bundle_id deposco-push)
      cq-81b5d10feef3  Deposco inventory sync: eager module-scope FileHandler +
                       test import create 0-byte dated logs (misread as a dead
                       lane 8/21) -- fixed in this build's step 2

Run (from the repo root, AFTER the FF-merge):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_deposco_push_2026-09-23.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_deposco_push_2026-09-23.py --apply
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import code_queue  # noqa: E402

HARRISON_ID = "U0B2RM2JYJ1"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # D-266: every spawn windowless
BUNDLE_ID = "deposco-push"
BRANCH = "claude/deposco-push-write-path-2026-09-22"
_PY = str(_REPO_ROOT / ".venv" / "Scripts" / "python.exe")


def _hygiene_fix_present() -> str | None:
    """BEHAVIOURAL, not a grep: a fresh subprocess imports the module and
    reports how many handlers landed on the root logger. Before this fix,
    module-scope `logging.basicConfig(handlers=[FileHandler(...)])` added one
    on every import (including every pytest collection). After the fix,
    `_configure_runtime` defers that to the first call from `main()`, so a
    bare import must add zero."""
    probe = (
        "import sys; "
        f"sys.path.insert(0, r'{_REPO_ROOT / 'scripts'}'); "
        "import logging; "
        "import run_deposco_inventory_sync; "
        "print(len(logging.getLogger().handlers))"
    )
    try:
        result = subprocess.run(
            [_PY, "-c", probe], capture_output=True, text=True,
            cwd=str(_REPO_ROOT), timeout=30, creationflags=_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return f"hygiene-fix probe could not run: {type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return f"hygiene-fix probe subprocess failed: {result.stderr[:300]!r}"
    count = result.stdout.strip()
    if count != "0":
        return (
            f"importing run_deposco_inventory_sync added {count} root logging "
            f"handler(s) at import time -- the fix is not present on this tree"
        )
    if not hasattr(__import__("run_deposco_inventory_sync"), "_configure_runtime"):
        pass  # already proven above by the subprocess probe; this is belt-only
    return None


def _ascii(s: object) -> str:
    return str(s).encode("ascii", "replace").decode("ascii")


def _head_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
            cwd=str(_REPO_ROOT), timeout=10, creationflags=_NO_WINDOW,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Perform the transition. Omitted = report only.")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

    commit = _head_commit()
    print(f"Step 7.5 -- Deposco push ({'APPLY' if args.apply else 'DRY-RUN'}) "
          f"bundle_id={BUNDLE_ID} branch={BRANCH} commit={commit or '?'}\n")

    cq_id = "cq-81b5d10feef3"
    label = "Deposco inventory sync: eager module-scope FileHandler + test import 0-byte logs"

    blocker = _hygiene_fix_present()
    if blocker:
        print(f"  BLOCKED  {cq_id}  {label}  -> {blocker}")
        return 1

    rec = code_queue.get_item(cq_id)
    if rec is None:
        print(f"  NOT SHIPPED  {cq_id}  {label}  -> missing id (no queue item)")
        return 1
    status = str(rec.get("status", ""))
    if status == "SHIPPED":
        print(f"  SKIP (already shipped)  {cq_id}  {label}")
        return 0
    if status in ("DISMISSED", "SUPERSEDED"):
        print(f"  NOT SHIPPED  {cq_id}  {label}  -> status is {status}; refusing to flip a terminal row")
        return 1

    if not args.apply:
        print(f"  [dry-run] would mark SHIPPED  {cq_id}  {label}  (status {status}; bundle_id={BUNDLE_ID})")
        return 0

    try:
        outcome, msg = code_queue.process_queue_action(
            code_queue.ACTION_MARK_SHIPPED, cq_id, HARRISON_ID,
            bundle_id=BUNDLE_ID, branch=BRANCH, commit=commit,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED   {cq_id}  {label}  -> {type(exc).__name__}: {_ascii(exc)}")
        return 1

    if outcome == "shipped":
        print(f"  SHIPPED  {cq_id}  {label}  -> {_ascii(msg)}")
        return 0
    print(f"  NOT SHIPPED  {cq_id}  {label}  -> {outcome}: {_ascii(msg)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
