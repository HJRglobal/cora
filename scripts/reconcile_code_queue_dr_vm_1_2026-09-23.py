"""Step 7.5 queue reconciliation for the DR/VM step-1 session (branch
``claude/dr-manifest-vm-step1-2026-09-22``, bundle_id ``code-dr-vm-1``).

Transitions ONLY what this branch closed (kickoff section 7). Dry-run by default; ``--apply``
writes through the module's own writers -- ``process_queue_action`` (carrying bundle_id +
branch + commit on the shipped event: the C7 HARD GATE) and ``seed_item`` -- never by
hand-editing the jsonl or the backlog (loop step 7.5; the 2026-07-31 stale-positive incident).

    SHIPPED (bundle_id code-dr-vm-1)
      cq-a296aa8e0a2e  Repo docs/DR sync bundle: runbook task table + bootstrap DR doc regenerated
                       from the live registry (M1), uv.lock mcp closure (M2), rate-limiter copy
                       drift (M3). VERIFY: it read HIGH (not LOW) at fire -- Code #12's 7.5 had
                       re-graded it -- so no re-grade is done here.
    SEED (PROPOSED, provenance = the cascade report; each seeded ONLY if no open item with the
          same title exists -- checked against load_items() first)
      VM step 2 -- stand-up as sandbox + warm standby (the T0 card issued by M4; Harrison
                   provisions after Justin's checkpoint)                      -- P2 feature
      KB restore-drill RTO measurement (runs on the VM at step 2; closes DR-MANIFEST D16/D17/D24
                   UNMEASURED cells)                                          -- P2 feature
      Minimal secrets vault (WS-C dependency; charter D3)                     -- P3 feature
      Repo-TOM regeneration cadence (rides the weekly consolidation; no new task) -- P3 config
    NO transitions for Code #12/#13/#14 ids, the 9/7 DO-NOT-FIRE kickoffs, or cq-06045f418bd2
    (run-marker widening, GATED on the C13 ruling -- this session only MEASURED coverage).

Every SHIPPED transition is GUARDED on the fix being present on the checked-out tree
(behavioural / reachability checks, never a grep for a string a stub could carry).

    --probe-gate   proves the C7 gate on THIS tree against a throwaway ledger: a MARK_SHIPPED
                   with no bundle reference must be refused. Never touches the real ledger.

Run (from the MAIN checkout, AFTER the FF-merge -- the queue ledger lives in its data/state/):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_dr_vm_1_2026-09-23.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_dr_vm_1_2026-09-23.py --probe-gate
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_dr_vm_1_2026-09-23.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import code_queue  # noqa: E402

HARRISON_ID = "U0B2RM2JYJ1"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BUNDLE_ID = "code-dr-vm-1"
BRANCH = "claude/dr-manifest-vm-step1-2026-09-22"
REPORT = "_shared/projects/cora/2026-09-23_fndr_cora-dr-manifest-vm-step1-CASCADE-REPORT.md"
PACKET = "_shared/projects/cora/2026-09-23_cora_vm-step1-scoping-packet.md"


# ── code-presence preconditions (each returns a blocker string, or None) ──────
def _bundle_present() -> str | None:
    """cq-a296aa8e0a2e = runbook table + bootstrap DR doc + uv.lock mcp + rate-limiter copy.
    Behavioural: the generator diffs, the generated blocks are marker-gated and current, the
    lock carries mcp, the refusal copy reads the constant."""
    import generate_task_estate_manifest as tem
    import dr_manifest_probes as dmp
    # M1: the drift monitor is fail-capable on a fixture (the test's exact contract)
    fx = _REPO_ROOT / "tests" / "fixtures" / "task_estate_small.json"
    if not fx.exists():
        return "M1 fixture missing"
    raw = json.loads(fx.read_text(encoding="utf-8"))
    intent = {"running": set(), "disabled": set(), "enabled": set(), "run_markers": {}}
    tasks = [tem.normalize_task(r, intent=intent, scripts={}, ladder=[], markers={}, log_dir=_REPO_ROOT / "nowhere")
             for r in raw]
    cur = [t for t in tasks if t["name"] != tasks[0]["name"]]
    fake = dict(tasks[0]); fake["name"] = "Cora - Probe Fake"
    lines = tem.warn_lines(tem.diff_manifests(tasks, cur + [fake]))
    if len(lines) != 2 or not any("added Cora - Probe Fake" in ln for ln in lines):
        return f"M1 diff is not fail-capable on the fixture ({len(lines)} lines)"
    committed = tem.load_manifest(_REPO_ROOT / "deployment" / "manifest" / tem.MANIFEST_JSON)
    if not committed or committed.get("count", 0) < 90:
        return "M1 committed manifest missing or implausibly small"
    for doc, block in ((_REPO_ROOT / "deployment" / "bootstrap-new-machine.md", tem.BOOTSTRAP_BLOCK),
                       (_REPO_ROOT / "deployment" / "runbook.md", tem.RUNBOOK_BLOCK)):
        text = doc.read_text(encoding="utf-8")
        begin, end = tem._markers(block)  # noqa: SLF001
        if text.count(begin) != 1 or text.count(end) != 1:
            return f"M1 generated block markers missing in {doc.name}"
        body = text.split(begin, 1)[1].split(end, 1)[0]
        if not all(f"`{t['name']}`" in body for t in committed["tasks"]):
            return f"M1 generated block in {doc.name} does not list every manifest task"
    # M2: the DR manifest + probe runner + the lock
    if not (_REPO_ROOT / "deployment" / "DR-MANIFEST.md").exists():
        return "M2 DR-MANIFEST.md missing"
    if len({i.id for i in dmp.build_items()}) < 20:
        return "M2 probe items missing"
    if 'name = "mcp"' not in (_REPO_ROOT / "uv.lock").read_text(encoding="utf-8", errors="replace"):
        return "M2 uv.lock still omits mcp"
    # M3: the refusal copy reads the constant (both caps), the runbook agrees
    app_src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
    if "{rate_limiter._USER_LIMIT}/hour" not in app_src or "(10/hour)" in app_src:
        return "M3 rate-cap refusal copy still hardcoded"
    if "per-user (30/hr" not in (_REPO_ROOT / "deployment" / "runbook.md").read_text(encoding="utf-8"):
        return "M3 runbook rate-limit copy not reconciled"
    return None


def _gate(fn: Callable[[], str | None]) -> str | None:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return f"precondition raised {type(exc).__name__}: {_ascii(exc)}"


PRECONDITIONS: dict[str, Callable[[], str | None]] = {"cq-a296aa8e0a2e": _bundle_present}
SHIPPED: dict[str, str] = {
    "cq-a296aa8e0a2e": "Repo docs/DR sync bundle: runbook table + bootstrap DR doc (M1), uv.lock mcp (M2), rate-limiter copy (M3)",
}
NOT_TOUCHED: tuple[tuple[str, str], ...] = (
    ("cq-06045f418bd2", "run-marker widening: GATED on the C13 ruling; this session MEASURED 4/96 only"),
    ("cq-fb50c9e6c911", "9/9 missed-start catch-up: shipped by Code #13 (its own 7.5)"),
)
SEEDS: tuple[dict, ...] = (
    {"kind": "feature", "severity": "P2", "entity": "FNDR", "signal": "explicit",
     "title": "VM step 2 -- stand the Cora VM up as WS-A sandbox + warm standby (after Justin's cost checkpoint + Harrison's go)",
     "summary": ("Charter D1 phase 2. Input = the scoping packet " + PACKET + " (every 16-vCPU Windows row is OUTSIDE "
                 "the $300-600 band; in-band paths: 8 vCPU / Azure Hybrid Benefit / re-ruled band). Harrison provisions "
                 "after Justin; the T0 card (scripts/stage_vm_step2_card.py --apply) is the decision surface. Step 2 runs "
                 "deployment/DR-MANIFEST.md section 4 (the restore drill) and the parity checklist for ~2 clean weeks. "
                 "PHI-free sandbox scope by exclusion; no LEX partition, no prod Slack token, no Gmail/Fireflies/Calendar creds."),
     "subsystem_guess": "deployment/infra"},
    {"kind": "feature", "severity": "P2", "entity": "FNDR", "signal": "explicit",
     "title": "KB restore-drill RTO measurement on the VM (closes DR-MANIFEST D16/D17/D24 UNMEASURED cells)",
     "summary": ("Time both KB restore paths on the step-2 VM: (a) snapshot restore from the 9/8 --include-kb copy "
                 "(~7.9 GB) + nightly catch-up; (b) connector rebuild per deployment/kb-rebuild.md restricted to the "
                 "PHI-free doors. Record wall-clock per DR-MANIFEST section 4 step; write the numbers into the RTO "
                 "column via scripts/dr_manifest_probes.py --update-docs. Only historical bound today: ~20 calendar days "
                 "of nightly windows (2026-05-28..06-17 re-ingest)."),
     "subsystem_guess": "deployment/kb"},
    {"kind": "feature", "severity": "P3", "entity": "FNDR", "signal": "explicit",
     "title": "Minimal secrets vault for the VM (WS-C scoped-credential build; charter D3: no flat .env off-prem)",
     "summary": ("The step-2 sandbox injects sandbox-only credentials from the provider's secret store at process start "
                 "(Azure Key Vault / AWS Secrets Manager / GCP Secret Manager); the encrypted secrets-*.enc bundle stays an "
                 "office-host restore artifact. Named as the WS-C dependency in the scoping packet; not built by DR/VM step 1."),
     "subsystem_guess": "config/secrets"},
    {"kind": "config", "severity": "P3", "entity": "FNDR", "signal": "explicit",
     "title": "Repo CLAUDE.md TOM regeneration cadence -- pointer block refreshed at the weekly consolidation, never a narrative",
     "summary": ("DR/VM step-1 M3 replaced the 1,690-line narrative TOM with a 31-line pointer block (D-087). Keep it that "
                 "way: at each weekly memory consolidation, add/refresh one table row per merged bundle (hash + report) and "
                 "prune the open-items line; no new scheduled task. A Code session that lands a bundle adds its row in its "
                 "M-docs slice."),
     "subsystem_guess": "docs"},
)


def _ascii(s: object) -> str:
    return str(s).encode("ascii", "replace").decode("ascii")


def _head_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=str(_REPO_ROOT), timeout=10, creationflags=_NO_WINDOW).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _provenance(cq_id: str, transition: str, commit: str) -> str | None:
    try:
        code_queue._append_event({  # noqa: SLF001
            "event": "reconciled", "ts": code_queue._now_iso(), "id": cq_id,  # noqa: SLF001
            "transition": transition, "bundle_id": BUNDLE_ID, "branch": BRANCH, "commit": commit,
        })
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {_ascii(exc)}"


def _existing_open_by_title(title: str) -> dict | None:
    """An item with the same title that is not terminal -- the seed is skipped, not duplicated."""
    t = title.strip().lower()
    for it in code_queue.load_items():
        if str(it.get("title", "")).strip().lower() == t and str(it.get("status", "")) not in ("SHIPPED", "DISMISSED", "SUPERSEDED"):
            return it
    return None


def probe_gate() -> int:
    saved = (code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER)  # noqa: SLF001
    saved_sync = code_queue._SYNC  # noqa: SLF001
    tmpdir = Path(tempfile.mkdtemp(prefix="cq-probe-"))
    try:
        code_queue._EVENT_LEDGER = tmpdir / "ledger.jsonl"  # noqa: SLF001
        code_queue._FINGERPRINT_LEDGER = tmpdir / "fp.jsonl"  # noqa: SLF001
        code_queue._SIGNALS_LEDGER = tmpdir / "sig.jsonl"  # noqa: SLF001
        code_queue._SYNC = True  # noqa: SLF001
        cid = code_queue.seed_item(kind="bug", severity="P2", title="probe-gate fixture", summary="throwaway",
                                   entity="F3E", signal="explicit", status="APPROVED")
        if not cid:
            print("PROBE: could not seed the throwaway item"); return 1
        outcome, msg = code_queue.process_queue_action(code_queue.ACTION_MARK_SHIPPED, cid, HARRISON_ID)
        if outcome != "refused":
            print(f"PROBE FAILED: MARK_SHIPPED without a bundle reference returned {outcome!r}: {_ascii(msg)}"); return 1
        outcome2, msg2 = code_queue.process_queue_action(code_queue.ACTION_MARK_SHIPPED, cid, HARRISON_ID,
                                                          bundle_id=BUNDLE_ID, branch=BRANCH)
        if outcome2 != "shipped":
            print(f"PROBE FAILED: MARK_SHIPPED WITH a bundle reference returned {outcome2!r}: {_ascii(msg2)}"); return 1
        print(f"PROBE OK: no-bundle MARK_SHIPPED refused ({_ascii(msg)[:90]}...); with bundle_id={BUNDLE_ID} -> shipped")
        return 0
    finally:
        code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER = saved  # noqa: SLF001
        code_queue._SYNC = saved_sync  # noqa: SLF001
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Perform the transitions. Omitted = report only.")
    ap.add_argument("--probe-gate", action="store_true", help="Prove the C7 gate against a throwaway ledger; writes nothing real.")
    ap.add_argument("--ledger-root", type=Path, default=None,
                    help="DRY-RUN from a git worktree: read the queue ledgers under this data/state dir "
                         "(the live one is C:\\Users\\Harri\\code\\cora\\data\\state). Refused with --apply: apply "
                         "runs from the main checkout after the FF-merge, where the defaults are the live ledgers.")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    if args.probe_gate:
        return probe_gate()
    if args.ledger_root:
        if args.apply:
            print("REFUSED: --ledger-root is a dry-run aid; --apply must run from the main checkout (default ledgers).")
            return 2
        code_queue._EVENT_LEDGER = args.ledger_root / "code-session-queue.jsonl"  # noqa: SLF001
        code_queue._FINGERPRINT_LEDGER = args.ledger_root / "code-queue-fingerprints.jsonl"  # noqa: SLF001
        code_queue._SIGNALS_LEDGER = args.ledger_root / "code-queue-signals.jsonl"  # noqa: SLF001
    commit = _head_commit()
    print(f"Step 7.5 -- DR/VM step 1 ({'APPLY' if args.apply else 'DRY-RUN'}) bundle_id={BUNDLE_ID} branch={BRANCH} commit={commit or '?'}")
    print(f"ledger: {code_queue._EVENT_LEDGER}\n")  # noqa: SLF001
    rc = 0
    for cq_id, label in SHIPPED.items():
        blocker = _gate(PRECONDITIONS.get(cq_id, lambda: None))
        if blocker:
            print(f"  BLOCKED  {cq_id}  {label}  -> {blocker}"); rc = 1; continue
        rec = code_queue.get_item(cq_id)
        if rec is None:
            print(f"  NOT SHIPPED  {cq_id}  -> missing id"); rc = 1; continue
        status = str(rec.get("status", ""))
        sev = str(rec.get("severity", ""))
        if status == "SHIPPED":
            print(f"  SKIP (already shipped)  {cq_id}  {label}"); continue
        if status in ("DISMISSED", "SUPERSEDED"):
            print(f"  NOT SHIPPED  {cq_id}  -> status is {status}; refusing to flip a terminal row"); rc = 1; continue
        if not args.apply:
            print(f"  [dry-run] would mark SHIPPED  {cq_id}  {label}  (status {status}, severity {sev}; bundle_id={BUNDLE_ID})"); continue
        try:
            outcome, msg = code_queue.process_queue_action(code_queue.ACTION_MARK_SHIPPED, cq_id, HARRISON_ID,
                                                            bundle_id=BUNDLE_ID, branch=BRANCH, commit=commit)
            if outcome == "shipped":
                print(f"  SHIPPED  {cq_id}  {label}  -> {_ascii(msg)}")
            else:
                print(f"  NOT SHIPPED  {cq_id}  -> {outcome}: {_ascii(msg)}"); rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED   {cq_id}  -> {type(exc).__name__}: {_ascii(exc)}"); rc = 1
    for cq_id, note in NOT_TOUCHED:
        rec = code_queue.get_item(cq_id)
        print(f"  NOT TOUCHED  {cq_id}  ({rec.get('status') if rec else 'MISSING'})  -> {note}")
    for seed in SEEDS:
        existing = _existing_open_by_title(seed["title"])
        if existing:
            print(f"  SKIP SEED (exists: {existing.get('id')} {existing.get('status')})  {seed['title'][:80]}"); continue
        if not args.apply:
            print(f"  [dry-run] would SEED PROPOSED {seed['severity']} {seed['kind']}  {seed['title'][:90]}"); continue
        try:
            cid = code_queue.seed_item(kind=seed["kind"], severity=seed["severity"], title=seed["title"],
                                       summary=seed["summary"] + f" Provenance: {REPORT}.", entity=seed["entity"],
                                       signal=seed["signal"], status="PROPOSED", subsystem_guess=seed.get("subsystem_guess", ""))
            if cid:
                err = _provenance(cid, "SEEDED by the DR/VM step-1 7.5", commit)
                print(f"  SEEDED  {cid}  {seed['title'][:80]}" + (f"  [provenance record FAILED: {err}]" if err else ""))
            else:
                print(f"  NOT SEEDED (refused/PHI-gated)  {seed['title'][:80]}"); rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED SEED  {seed['title'][:60]}  -> {type(exc).__name__}: {_ascii(exc)}"); rc = 1
    print("\n" + ("DONE" if rc == 0 else "DONE WITH BLOCKERS (rc=1)"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
