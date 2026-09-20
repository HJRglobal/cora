"""Step 7.5 queue reconciliation for the Code #13 RIDER 1 -- Cora identity, least
privilege (branch ``claude/cora-identity-least-privilege``, bundle_id ``code-identity-v1``).

Run SEPARATELY from the code-13 script (identity kickoff section 7). Dry-run by
default; ``--apply`` writes through ``process_queue_action`` with the C7 bundle
reference -- never a hand edit of the jsonl or the backlog.

    SHIPPED (bundle_id code-identity-v1)
      cq-8d16f1a557e5  S-A: cora@hjrglobal.com in the DWD roster as a SYSTEM mailbox with
                       an intake route into the knowledge-review queue (finishes the 8/18
                       PROVISION ruling). Gated on the row + the consumer being present.
    NOT TOUCHED (already seeded 9/11; stay STAGED past this session)
      cq-40baab26d7f3  Asana identity flip + Harrison PAT retirement (closes on day 14, his hand)
      cq-c624f28c772a  Cora-voice send mailbox = cora@ (R4; the October external-WRITE seam)
    NOT SEEDED HERE: the identity kickoff's "seed at 7.5: asana identity swap -- flip +
      14d PAT retirement" IS cq-40baab26d7f3 (seeded 9/11) -- re-seeding is forbidden.

    --probe-gate   proves the C7 gate against a throwaway ledger; never touches the real one.

Run (repo root, AFTER the identity branch FF-merges -- which is after code-13's):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code_identity_v1_2026-09-19.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code_identity_v1_2026-09-19.py --probe-gate
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code_identity_v1_2026-09-19.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import code_queue  # noqa: E402

HARRISON_ID = "U0B2RM2JYJ1"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BUNDLE_ID = "code-identity-v1"
BRANCH = "claude/cora-identity-least-privilege"
CORA_MAILBOX = "cora@hjrglobal.com"


def _s_a_present() -> str | None:
    import yaml
    roster = yaml.safe_load((_REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml")
                            .read_text(encoding="utf-8")) or {}
    rows = [r for r in roster.get("accounts") or []
            if isinstance(r, dict) and str(r.get("email") or "").lower() == CORA_MAILBOX]
    if len(rows) != 1:
        return f"S-A: expected exactly one {CORA_MAILBOX} roster row, found {len(rows)}"
    row = rows[0]
    if str(row.get("intake_route") or "") != "knowledge_review":
        return "S-A: the cora@ row carries no intake_route: knowledge_review"
    for key in ("thread_sweep", "attachment_filer", "drive_sweep"):
        if row.get(key, True):
            return f"S-A: the cora@ row would be CHUNK-ingested ({key} is not explicitly false)"
    if row.get("fireflies_seat"):
        return "S-A: the cora@ row is flagged fireflies_seat (the seat lives in meeting-capture-roster.yaml)"
    if not (_REPO_ROOT / "scripts" / "run_mailbox_intake_sweep.py").exists():
        return "S-A: the intake consumer script is missing"
    # BEHAVIOUR: the Fireflies coverage monitor's ONLY roster consumer must still return
    # its flagged humans only -- a system mailbox is never a seat holder.
    from cora.connectors import fireflies_coverage as fc
    names = {h.name for h in fc.load_dwd_humans()}
    if any("cora" in n.lower() for n in names):
        return "S-A: load_dwd_humans() now returns the cora@ system mailbox as a human"
    # the gmail sweep's account filter must exclude the row (no KB chunks from cora@)
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
    import gmail_threaded_sweep as gts  # noqa: PLC0415
    swept = {str(a.get("email") or "").lower() for a in gts._load_accounts()}
    if CORA_MAILBOX in swept:
        return "S-A: gmail_threaded_sweep still selects cora@ for chunk ingest"
    return None


def _gate(fn: Callable[[], str | None]) -> str | None:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return f"precondition raised {type(exc).__name__}: {_ascii(exc)}"


PRECONDITIONS: dict[str, Callable[[], str | None]] = {"cq-8d16f1a557e5": _s_a_present}
SHIPPED: dict[str, str] = {
    "cq-8d16f1a557e5": "S-A: cora@ DWD roster row (system mailbox) + knowledge-review intake route",
}
NOT_TOUCHED: dict[str, str] = {
    "cq-40baab26d7f3": "Asana identity flip + Harrison PAT retirement -- Harrison's hands; closes on day 14",
    "cq-c624f28c772a": "Cora-voice send mailbox = cora@ (R4) -- gated on the October external-WRITE seam",
}
_TERMINAL_NOT_SHIPPABLE = frozenset({"DISMISSED", "SUPERSEDED"})


def _ascii(s: object) -> str:
    return str(s).encode("ascii", "replace").decode("ascii")


def _head_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=str(_REPO_ROOT), timeout=10, creationflags=_NO_WINDOW).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def probe_gate() -> int:
    saved = (code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER)
    saved_sync = code_queue._SYNC
    tmpdir = Path(tempfile.mkdtemp(prefix="cq-probe-"))
    try:
        code_queue._EVENT_LEDGER = tmpdir / "ledger.jsonl"
        code_queue._FINGERPRINT_LEDGER = tmpdir / "fp.jsonl"
        code_queue._SIGNALS_LEDGER = tmpdir / "sig.jsonl"
        code_queue._SYNC = True
        cid = code_queue.seed_item(kind="bug", severity="P2", title="probe-gate fixture",
                                   summary="throwaway", entity="F3E", signal="explicit", status="APPROVED")
        if not cid:
            print("PROBE: could not seed the throwaway item"); return 1
        outcome, msg = code_queue.process_queue_action(code_queue.ACTION_MARK_SHIPPED, cid, HARRISON_ID)
        if outcome != "refused":
            print(f"PROBE FAILED: no-bundle MARK_SHIPPED returned {outcome!r}: {_ascii(msg)}"); return 1
        outcome2, msg2 = code_queue.process_queue_action(
            code_queue.ACTION_MARK_SHIPPED, cid, HARRISON_ID, bundle_id=BUNDLE_ID, branch=BRANCH)
        if outcome2 != "shipped":
            print(f"PROBE FAILED: MARK_SHIPPED WITH a bundle reference returned {outcome2!r}: {_ascii(msg2)}"); return 1
        print(f"PROBE OK: no-bundle MARK_SHIPPED refused; with bundle_id={BUNDLE_ID} -> shipped")
        return 0
    finally:
        code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER = saved
        code_queue._SYNC = saved_sync
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--probe-gate", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    if args.probe_gate:
        return probe_gate()
    commit = _head_commit()
    print(f"Step 7.5 -- Code #13 RIDER 1 ({'APPLY' if args.apply else 'DRY-RUN'}) "
          f"bundle_id={BUNDLE_ID} branch={BRANCH} commit={commit or '?'}\n")
    rc = 0
    for cq_id, label in SHIPPED.items():
        blocker = _gate(PRECONDITIONS.get(cq_id, lambda: None))
        if blocker:
            print(f"  BLOCKED  {cq_id}  {label}  -> {blocker}"); rc = 1; continue
        rec = code_queue.get_item(cq_id)
        if rec is None:
            print(f"  NOT SHIPPED  {cq_id}  {label}  -> missing id"); rc = 1; continue
        status = str(rec.get("status", ""))
        if status == "SHIPPED":
            print(f"  SKIP (already shipped)  {cq_id}  {label}"); continue
        if status in _TERMINAL_NOT_SHIPPABLE:
            print(f"  NOT SHIPPED  {cq_id}  {label}  -> status is {status}; refusing to flip a terminal row"); rc = 1; continue
        if not args.apply:
            print(f"  [dry-run] would mark SHIPPED  {cq_id}  {label}  (status {status}; bundle_id={BUNDLE_ID})"); continue
        try:
            outcome, msg = code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cq_id, HARRISON_ID, bundle_id=BUNDLE_ID, branch=BRANCH, commit=commit)
            if outcome == "shipped":
                print(f"  SHIPPED  {cq_id}  {label}  -> {_ascii(msg)}")
            else:
                print(f"  NOT SHIPPED  {cq_id}  {label}  -> {outcome}: {_ascii(msg)}"); rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED   {cq_id}  {label}  -> {type(exc).__name__}: {_ascii(exc)}"); rc = 1
    for cq_id, note in NOT_TOUCHED.items():
        rec = code_queue.get_item(cq_id)
        print(f"  NOT TOUCHED  {cq_id}  ({rec.get('status') if rec else 'MISSING'})  -> {note}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
