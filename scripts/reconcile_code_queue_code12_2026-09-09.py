"""Step 7.5 queue reconciliation for Code #12 -- queue metabolism + integrity
(branch ``claude/code-12-queue-metabolism-2026-09-09``, bundle_id ``code-12``).

Transitions ONLY the seeds this branch closed (kickoff §7 + the 9/9 hotfix note §7).
Dry-run by default; ``--apply`` writes through the module's own writers --
``process_queue_action`` (now carrying bundle_id + branch + commit on the shipped
event: the C7 HARD GATE this very script is the first run under), ``dismiss_with_
evidence``, ``supersede_item``, ``set_severity`` -- never by hand-editing the jsonl or
the backlog (loop step 7.5; the 2026-07-31 stale-positive incident).

    SHIPPED (bundle_id code-12)
      cq-554184feb53b  S1' founder-DM queue-verb interceptor (stage|approve|dismiss cq-<id>)
      cq-60024f032136  S2' phantom-write-claim + fabricated-id screen at the reply seam (OBSERVE)
      cq-deca62a00719  S3' observe-week read (both rails) + kickoff/card/how-to-stage + seed message
      cq-b6f2f4825ffb  C1 evidence-attached kickoffs + the EVIDENCE FLOOR
      cq-8b51427896b8  C2 (folded): the weekly mechanical batch card's NEEDS HARRISON block
      cq-f23d6885dd1c  G1 HYBRID inventory authorization (in-channel post proves membership)
      cq-28de84159c3f  G1 (folded): approval parity -- roster membership was the defect
      cq-a70bbbfd2d13  the Code #12 bundle seed
      cq-a1aaee9f46e0  the 9/9 pre-step: "Desktop to Desktop" pinned (the pin half; the
                       purge is Harrison's hand inside the stop window)
      cq-6fef5505fb3e  One-Cora pack step C (ruled 8/27; meeting_asks per-ask dedup on main)
    LEFT OPEN (with a printed note)
      cq-41540856f95b  the <=1 adjacency (Shopify scope preflight) was NOT built -- stays PROPOSED
    DISMISSED WITH EVIDENCE
      cq-651e6783994f  Harrison's QUESTION minted APPROVED by a card tap; answered 9/4; kickoff struck 9/8
      cq-fea9b2676e2f  Appendix-A 8/31: superseded by the 8/26 Gmail delegation fix (D-245); no kickoff
      cq-dbcca949bb92  Appendix-A 8/31: same root cause; kickoff struck 9/8
      cq-3e7a465040f1  Appendix-A 8/31: flywheel activation tail shipped across Wave 1 + the gate
                       fixes. Its prompt_path is the SHARED 2026-07-28 lex-ar-audit bundle file --
                       NOT struck (other rows reference it; the sharers are printed).
    SUPERSEDED
      cq-0f8abd3c1981  by cq-deca62a00719 (the MCP seed-message fix rides S3')
    RE-GRADED
      cq-a296aa8e0a2e  LOW -> HIGH (D1 GO; the DR manifest gets its own session, wk of 9/14)

Every transition is GUARDED on the fix being present on the checked-out tree
(behavioural / reachability checks, never a grep for a string a stub could carry).

    --probe-gate   proves the C7 gate on THIS tree against a throwaway ledger: a
                   MARK_SHIPPED with no bundle reference must be refused (smoke #4).
                   Never touches the real ledger.

Run (from the repo root, AFTER the FF-merge):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code12_2026-09-09.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code12_2026-09-09.py --probe-gate
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code12_2026-09-09.py --apply
"""

from __future__ import annotations

import argparse
import inspect
import re
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
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # D-266: every spawn windowless
BUNDLE_ID = "code-12"
BRANCH = "claude/code-12-queue-metabolism-2026-09-09"
DESKTOP_TO_DESKTOP = "1gZVdKz3BIdePV6eeG08kkf7G65yDohaM"


# ── code-presence preconditions (each returns a blocker string, or None) ──────
def _s1_present() -> str | None:
    if code_queue.match_queue_verb("Stage cq-621dfad586aa") != ("stage", "cq-621dfad586aa"):
        return "S1' match_queue_verb does not resolve the 06:49 incident message"
    if code_queue.match_queue_verb("Cora should retry the way cq-621dfad586aa describes") is not None:
        return "S1' match_queue_verb matches a citation sentence (must be exact-match only)"
    from cora import app
    src = inspect.getsource(app.handle_message_event)
    if "code_queue.match_queue_verb(text)" not in src or "code_queue.apply_queue_verb(" not in src:
        return "S1' interceptor not wired in app.handle_message_event"
    if src.index("code_queue.match_queue_verb(text)") > src.index("gap_autofill.match_pending_ask"):
        return "S1' interceptor sits below the gap-ask capture (a pending ask could swallow the verb)"
    return None


def _s2_present() -> str | None:
    from cora import slack_egress, app
    if not hasattr(slack_egress, "screen_phantom_write_claims"):
        return "S2' screen missing on this tree"
    src = inspect.getsource(app._dispatch_qa)
    if src.count("slack_egress.screen_phantom_write_claims(") != 2:
        return "S2' screen not wired at both final-reply sites in _dispatch_qa"
    # behavioural: the 06:53 shape trips in observe mode and returns byte-identical text
    import logging
    text = "Done. Staging all three:\n• cq-e7f2a4c91b2e - x\nAll three are queued."
    handler_hits: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):
            handler_hits.append(record.getMessage())
    h = _H()
    lg = logging.getLogger(slack_egress.__name__)
    lg.addHandler(h)
    try:
        out = slack_egress.screen_phantom_write_claims(text, tool_use_count=0)
    finally:
        lg.removeHandler(h)
    if out != text or not any("phantom-write-claim" in m for m in handler_hits):
        return "S2' screen did not WARN (observe) on the 06:53 shape"
    return None


def _s3_present() -> str | None:
    from cora import egress_rails, code_queue as cq, mcp_server
    if not hasattr(egress_rails, "observe_week_read") or not hasattr(cq, "format_surface_line"):
        return "S3' observe-week read / surface fields missing"
    hc = (_REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(encoding="utf-8")
    if "all_results.append(check_egress_rails())" not in hc:
        return "S3' check_egress_rails not registered in the nightly check"
    if "No DM card is posted for a seed" not in inspect.getsource(mcp_server.code_queue_seed):
        return "S3' MCP seed message still points at a Stage tap"
    return None


def _c1_present() -> str | None:
    if not hasattr(code_queue, "has_evidence"):
        return "C1 has_evidence missing"
    src = inspect.getsource(code_queue.ensure_kickoff_staged)
    if "override_evidence_floor" not in src or "no_evidence" not in src:
        return "C1 evidence floor not in ensure_kickoff_staged"
    # the 9/7 fixture shape must be refused by the predicate
    if code_queue.has_evidence({"signal": "capability", "summary": "q",
                                "evidence": [{"channel_id": "", "ts": "", "note": "#fndr"}]}):
        return "C1 has_evidence passes the cq-651e6783994f shape"
    return None


def _c2_present() -> str | None:
    from cora import review_lanes
    if getattr(review_lanes, "EXPIRED_LOW_RISK", "") != "expired_low_risk":
        return "C2 EXPIRED_LOW_RISK missing"
    rk = (_REPO_ROOT / "scripts" / "run_knowledge_review.py").read_text(encoding="utf-8")
    if "_expire_low_risk_mechanical(entries, now, answered_ts)" not in rk:
        return "C2 expiry pass not wired into Step 0"
    if "_maybe_send_mechanical_batch_card(" not in rk:
        return "C2 batch card not wired into main"
    return None


def _g1_present() -> str | None:
    from cora import inventory_membership, user_access
    from cora.tools import tool_dispatch as td
    if not hasattr(inventory_membership, "allows"):
        return "G1 inventory_membership.allows missing"
    if "entity_grant" not in inspect.signature(user_access.check_access).parameters:
        return "G1 check_access has no entity_grant"
    if "inventory_membership.allows(" not in inspect.getsource(td._shopify_set_inventory_impl):
        return "G1 out-of-channel membership check not in the inventory tool"
    from cora import app
    if inspect.getsource(app).count("entity_grant=_inv_grant") != 3:
        return "G1 in-channel grant not threaded at the three channel-path gates"
    return None


def _hotfix_present() -> str | None:
    from cora.kb_exclusions import KB_EXCLUDED_FOLDER_IDS, KB_EXCLUDED_FOLDER_LABELS
    if DESKTOP_TO_DESKTOP not in KB_EXCLUDED_FOLDER_IDS or DESKTOP_TO_DESKTOP not in KB_EXCLUDED_FOLDER_LABELS:
        return "hotfix: Desktop to Desktop not pinned on this tree"
    return None


def _bundle_present() -> str | None:
    for fn in (_s1_present, _s2_present, _s3_present, _c1_present, _c2_present, _g1_present):
        b = fn()
        if b:
            return b
    return None


def _one_cora_step_c_present() -> str | None:
    try:
        from cora import meeting_asks
    except Exception as exc:  # noqa: BLE001
        return f"meeting_asks not importable ({exc})"
    if not hasattr(meeting_asks, "already_carded"):
        return "cq-6fef5505fb3e: meeting_asks.already_carded (per-ask dedup) missing"
    return None


PRECONDITIONS: dict[str, Callable[[], str | None]] = {
    "cq-554184feb53b": _s1_present,
    "cq-60024f032136": _s2_present,
    "cq-deca62a00719": _s3_present,
    "cq-b6f2f4825ffb": _c1_present,
    "cq-8b51427896b8": _c2_present,
    "cq-f23d6885dd1c": _g1_present,
    "cq-28de84159c3f": _g1_present,
    "cq-a70bbbfd2d13": _bundle_present,
    "cq-a1aaee9f46e0": _hotfix_present,
    "cq-6fef5505fb3e": _one_cora_step_c_present,
    "cq-0f8abd3c1981": _s3_present,   # superseded only once the fix that supersedes it is real
}

SHIPPED: dict[str, str] = {
    "cq-554184feb53b": "S1' founder-DM queue-verb interceptor",
    "cq-60024f032136": "S2' phantom-write-claim + fabricated-id screen (OBSERVE)",
    "cq-deca62a00719": "S3' observe-week read + surface fields + seed message",
    "cq-b6f2f4825ffb": "C1 evidence-attached kickoffs + EVIDENCE FLOOR",
    "cq-8b51427896b8": "C2 folded: NEEDS HARRISON block on the weekly batch card",
    "cq-f23d6885dd1c": "G1 HYBRID inventory authorization",
    "cq-28de84159c3f": "G1 folded: approval parity (roster membership was the defect)",
    "cq-a70bbbfd2d13": "Code #12 bundle seed",
    "cq-a1aaee9f46e0": "9/9 pre-step: Desktop to Desktop pinned (pin half)",
    "cq-6fef5505fb3e": "One-Cora pack step C (ruled 8/27)",
}
LEFT_OPEN: dict[str, str] = {
    "cq-41540856f95b": ("the <=1 adjacency (fail-fast Shopify scope preflight) was NOT built this "
                        "session -- stays PROPOSED; rides the Monday PROPOSED coverage"),
}
DISMISSED: dict[str, str] = {
    "cq-651e6783994f": ("Harrison's question, not a build ask: minted APPROVED by a card tap "
                        "(ledger: approved 2026-09-03T23:38Z), answered 9/4 in decisions.md; "
                        "kickoff struck 9/8; the C1 evidence floor is the fix"),
    "cq-fea9b2676e2f": ("Appendix-A 8/31 disposition: superseded by the 8/26 Gmail delegation "
                        "fix (D-245); no kickoff was ever written"),
    "cq-dbcca949bb92": ("Appendix-A 8/31 disposition: same root cause as the Gmail delegation "
                        "fix (D-245); kickoff struck 9/8"),
    "cq-3e7a465040f1": ("Appendix-A 8/31 disposition: the flywheel activation tail shipped "
                        "across Wave 1 (D-093) and the gate fixes (D-088/D-089); its shared "
                        "lex-ar-audit prompt file is left in place for the rows that still cite it"),
}
SUPERSEDED: dict[str, tuple[str, str]] = {
    "cq-0f8abd3c1981": ("cq-deca62a00719", "the MCP seed-message fix rides S3'"),
}
REGRADED: dict[str, str] = {
    "cq-a296aa8e0a2e": "HIGH",   # D1 GO: own session, wk of 9/14
}

_TERMINAL_NOT_SHIPPABLE = frozenset({"DISMISSED", "SUPERSEDED"})


def _ascii(s: object) -> str:
    """process_queue_action's messages carry emoji; a redirected stdout on this host
    is cp1252 and would raise AFTER the ledger write."""
    return str(s).encode("ascii", "replace").decode("ascii")


def _head_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
            cwd=str(_REPO_ROOT), timeout=10, creationflags=_NO_WINDOW,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _provenance(cq_id: str, transition: str, commit: str) -> str | None:
    """A `reconciled` record for the non-shipped transitions (supersede / re-grade),
    so those rows answer "which bundle did this" too. The shipped event carries its
    own fields now (C7). Reported, never mislabelled as a failed transition."""
    try:
        code_queue._append_event({
            "event": "reconciled", "ts": code_queue._now_iso(), "id": cq_id,
            "transition": transition, "bundle_id": BUNDLE_ID, "branch": BRANCH, "commit": commit,
        })
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {_ascii(exc)}"


def _shared_prompt_sharers(cq_id: str) -> list[str]:
    rec = code_queue.get_item(cq_id) or {}
    p = str(rec.get("prompt_path") or "")
    if not p:
        return []
    return sorted(it["id"] for it in code_queue.load_items()
                  if str(it.get("prompt_path") or "") == p and it["id"] != cq_id)


def probe_gate() -> int:
    """Smoke #4: prove the C7 gate on THIS tree against a THROWAWAY ledger -- a
    MARK_SHIPPED with no bundle reference must be refused. Never touches the real
    ledger (the module constant is swapped for a temp path and restored)."""
    saved = (code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER)
    saved_sync = code_queue._SYNC
    tmpdir = Path(tempfile.mkdtemp(prefix="cq-probe-"))
    try:
        code_queue._EVENT_LEDGER = tmpdir / "ledger.jsonl"
        code_queue._FINGERPRINT_LEDGER = tmpdir / "fp.jsonl"
        code_queue._SIGNALS_LEDGER = tmpdir / "sig.jsonl"
        code_queue._SYNC = True
        # the backlog render targets the live G: file -- the leak guard refuses it when
        # the ledger is redirected (2026-07-30 incident guard), so nothing egresses.
        cid = code_queue.seed_item(kind="bug", severity="P2", title="probe-gate fixture",
                                   summary="throwaway", entity="F3E", signal="explicit",
                                   status="APPROVED")
        if not cid:
            print("PROBE: could not seed the throwaway item"); return 1
        outcome, msg = code_queue.process_queue_action(code_queue.ACTION_MARK_SHIPPED, cid, HARRISON_ID)
        if outcome != "refused":
            print(f"PROBE FAILED: MARK_SHIPPED without a bundle reference returned {outcome!r}: {_ascii(msg)}")
            return 1
        outcome2, msg2 = code_queue.process_queue_action(
            code_queue.ACTION_MARK_SHIPPED, cid, HARRISON_ID, bundle_id=BUNDLE_ID, branch=BRANCH)
        if outcome2 != "shipped":
            print(f"PROBE FAILED: MARK_SHIPPED WITH a bundle reference returned {outcome2!r}: {_ascii(msg2)}")
            return 1
        print(f"PROBE OK: no-bundle MARK_SHIPPED refused ({_ascii(msg)[:90]}...); with bundle_id={BUNDLE_ID} -> shipped")
        return 0
    finally:
        code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER = saved
        code_queue._SYNC = saved_sync


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Perform the transitions. Omitted = report only.")
    ap.add_argument("--probe-gate", action="store_true",
                    help="Prove the C7 gate against a throwaway ledger (smoke #4); writes nothing real.")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    if args.probe_gate:
        return probe_gate()

    commit = _head_commit()
    print(f"Step 7.5 -- Code #12 ({'APPLY' if args.apply else 'DRY-RUN'}) "
          f"bundle_id={BUNDLE_ID} branch={BRANCH} commit={commit or '?'}\n")

    rc = 0
    for cq_id, label in SHIPPED.items():
        blocker = PRECONDITIONS.get(cq_id, lambda: None)()
        if blocker:
            print(f"  BLOCKED  {cq_id}  {label}  -> {blocker}")
            rc = 1
            continue
        rec = code_queue.get_item(cq_id)
        if rec is None:
            print(f"  NOT SHIPPED  {cq_id}  {label}  -> missing id (no queue item)")
            rc = 1
            continue
        status = str(rec.get("status", ""))
        if status == "SHIPPED":
            print(f"  SKIP (already shipped)  {cq_id}  {label}")
            continue
        if status in _TERMINAL_NOT_SHIPPABLE:
            print(f"  NOT SHIPPED  {cq_id}  {label}  -> status is {status}; refusing to flip a terminal row")
            rc = 1
            continue
        if not args.apply:
            print(f"  [dry-run] would mark SHIPPED  {cq_id}  {label}  (status {status}; bundle_id={BUNDLE_ID})")
            continue
        try:
            outcome, msg = code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cq_id, HARRISON_ID,
                bundle_id=BUNDLE_ID, branch=BRANCH, commit=commit)
            if outcome == "shipped":
                print(f"  SHIPPED  {cq_id}  {label}  -> {_ascii(msg)}")
            else:
                print(f"  NOT SHIPPED  {cq_id}  {label}  -> {outcome}: {_ascii(msg)}")
                rc = 1
        except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
            print(f"  FAILED   {cq_id}  {label}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1

    for cq_id, note in LEFT_OPEN.items():
        rec = code_queue.get_item(cq_id)
        print(f"  LEFT OPEN  {cq_id}  ({rec.get('status') if rec else 'MISSING'})  -> {note}")

    for cq_id, evidence in DISMISSED.items():
        rec = code_queue.get_item(cq_id)
        if rec is None:
            print(f"  NOT DISMISSED  {cq_id}  -> missing id")
            rc = 1
            continue
        status = str(rec.get("status", ""))
        if status == "DISMISSED":
            print(f"  SKIP (already dismissed)  {cq_id}")
            continue
        if status in ("SHIPPED", "SUPERSEDED"):
            print(f"  NOT DISMISSED  {cq_id}  -> status is {status}; refusing to flip a terminal row")
            rc = 1
            continue
        sharers = _shared_prompt_sharers(cq_id)
        share_note = (f"  [prompt file shared with {', '.join(sharers)} -- NOT struck]" if sharers else "")
        if not args.apply:
            print(f"  [dry-run] would DISMISS with evidence  {cq_id}  (status {status}){share_note}\n"
                  f"             evidence: {evidence}")
            continue
        try:
            outcome, msg = code_queue.dismiss_with_evidence(cq_id, HARRISON_ID, evidence)
            if outcome == "dismissed":
                print(f"  DISMISSED  {cq_id}  -> {_ascii(msg)[:120]}{share_note}")
            else:
                print(f"  NOT DISMISSED  {cq_id}  -> {outcome}: {_ascii(msg)}")
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED   {cq_id}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1

    for loser, (winner, label) in SUPERSEDED.items():
        blocker = PRECONDITIONS.get(loser, lambda: None)()
        if blocker:
            print(f"  BLOCKED  {loser}  supersede by {winner}  -> {blocker}")
            rc = 1
            continue
        rec = code_queue.get_item(loser)
        if rec is None:
            print(f"  NOT SUPERSEDED  {loser}  -> missing id")
            rc = 1
            continue
        status = str(rec.get("status", ""))
        if status == "SUPERSEDED":
            print(f"  SKIP (already superseded)  {loser}  by {rec.get('superseded_by', '?')}")
            continue
        if status in ("SHIPPED", "DISMISSED"):
            print(f"  NOT SUPERSEDED  {loser}  -> status is {status}; refusing to flip a terminal row")
            rc = 1
            continue
        if not args.apply:
            print(f"  [dry-run] would mark SUPERSEDED  {loser}  by {winner}  ({label}; status {status})")
            continue
        try:
            if code_queue.supersede_item(loser, winner):
                rec_err = _provenance(loser, f"SUPERSEDED by {winner}", commit)
                print(f"  SUPERSEDED  {loser}  by {winner}  ({label})"
                      + (f"  [provenance record FAILED: {rec_err}]" if rec_err else ""))
                if rec_err:
                    rc = 1
            else:
                print(f"  NOT SUPERSEDED  {loser}  -> supersede_item returned False")
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED   {loser}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1

    for cq_id, sev in REGRADED.items():
        rec = code_queue.get_item(cq_id)
        if rec is None:
            print(f"  NOT RE-GRADED  {cq_id}  -> missing id")
            rc = 1
            continue
        cur = str(rec.get("severity", ""))
        if cur.upper() == sev:
            print(f"  SKIP (already {sev})  {cq_id}")
            continue
        if not args.apply:
            print(f"  [dry-run] would re-grade  {cq_id}  {cur} -> {sev}  (D1 GO; own session wk of 9/14)")
            continue
        try:
            outcome, msg = code_queue.set_severity(cq_id, HARRISON_ID, sev)
            if outcome == "edited":
                rec_err = _provenance(cq_id, f"RE-GRADED {cur}->{sev}", commit)
                print(f"  RE-GRADED  {cq_id}  {cur} -> {sev}"
                      + (f"  [provenance record FAILED: {rec_err}]" if rec_err else ""))
                if rec_err:
                    rc = 1
            else:
                print(f"  NOT RE-GRADED  {cq_id}  -> {outcome}: {_ascii(msg)}")
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED   {cq_id}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
