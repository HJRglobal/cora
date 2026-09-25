"""Step 7.5 queue reconciliation for the 2026-09-08 INGEST-INTEGRITY bundle
(branch ``claude/ingest-integrity-2026-09-08``, bundle_id ``ingest-integrity-2026-09``).

Transitions ONLY the seeds this branch closed. Dry-run by default; ``--apply``
writes through ``code_queue.process_queue_action`` / ``code_queue.supersede_item``
-- never by hand-editing the jsonl or the backlog (loop step 7.5; the 2026-07-31
stale-positive incident is why this step exists).

    SHIPPED
      cq-c89cfab00b1f  I1 banking identifiers redacted at every chunk renderer + the
                       MCP structured results (src/cora/banking_identifiers.py).
      cq-bc5e5b7512bd  I2 Leak #2: a non-LEX distill is never re-homed to LEX; a
                       value-shaped PHI hit QUARANTINES (phi_guard.is_prose_phi_risk,
                       session_capture.route_capture) + the re-file/purge script
                       (scripts/refile_misrouted_session_captures.py, Harrison-run).
      cq-e4b0d20a313f  folded into I2: the mirror ZONE-K screen takes the same prose
                       screen, so approved/pending/claims no longer quarantine skills.
      cq-a0da505f8e5f  I3 the two Drive "Computers" backup roots pinned WALK-ONLY +
                       the ancestry-walk exclusion + purge --impersonate/--computers-root.
      cq-bd6eab1fcb44  folded into I3: purge_dashboard_kb walks KB_DASHBOARD_FOLDER_IDS only.
      cq-3542e1b095b2  I4 cora_self_inventory tool + the "do you have X" force route.
      cq-12fd5d76fd04  I5 the flat sweep leaves MARKDOWN inside the Founder-OS tree to
                       the static_md door (one entity tag per capture file) + the
                       two-door parity probe.
    SUPERSEDED
      cq-b80c5bc5be7a  by cq-a0da505f8e5f (LOCATED: the door is the Computers backup,
                       not a stray copy of the Scheduled store).

Every transition is GUARDED on the fix being present on the checked-out tree
(the inverse of the 7/31 incident: marking "shipped" from a tree that lacks the
code). Every applied transition also appends ONE ``reconciled`` ledger event
carrying ``bundle_id`` + ``branch`` + the merge commit -- the discipline Code #12's
C7 gate will read; the fold ignores the kind today (status untouched), so this is
a record, not a state change.

Run (from the repo root, AFTER the FF-merge):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_ingest_integrity_2026-09-08.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_ingest_integrity_2026-09-08.py --apply
"""

from __future__ import annotations

import argparse
import inspect
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import code_queue  # noqa: E402

HARRISON_ID = "U0B2RM2JYJ1"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # D-266: every spawn windowless (rail-pinned shape)
BUNDLE_ID = "ingest-integrity-2026-09"
BRANCH = "claude/ingest-integrity-2026-09-08"

COMPUTERS_ROOTS = frozenset({"1cdDb9jvDhoOz1vliE-tmVaks01ey1AJj", "1xmXreU4eKvcpAsj7Ic3fiwJO_ySidZ0F"})


# ── code-presence preconditions (each returns a blocker string, or None) ──────
def _i1_present() -> str | None:
    try:
        from cora import banking_identifiers, context_loader, mcp_server  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return f"I1 module missing on this tree ({exc})"
    src = (_REPO_ROOT / "src" / "cora" / "context_loader.py").read_text(encoding="utf-8")
    # Code #15 S1 (cq-d9d0c92cc797): the renderer now reaches banking_identifiers through
    # the composed chunk-egress helper (API-token shapes first, then banking) -- either
    # wiring is the I1 belt.
    if ("banking_identifiers.redact_banking_identifiers" not in src
            and "secret_tokens.redact_chunk_egress" not in src):
        return "I1 renderer wiring missing in context_loader.py"
    return None


def _i2_present() -> str | None:
    from cora import phi_guard, session_capture
    if not hasattr(phi_guard, "is_prose_phi_risk") or not hasattr(session_capture, "route_capture"):
        return "I2 prose screen / route_capture missing on this tree"
    # BEHAVIOURAL, not a grep for the old override string (a rephrased re-home would
    # pass a grep; D-051 lens F #7): a non-LEX distill with a strict-screen trip must
    # stay non-LEX and un-quarantined; a value-shaped hit must quarantine.
    try:
        if session_capture.route_capture("F3E", "review the care plan and the diagnosis notes") != ("F3E", False, False):
            return "I2 route_capture re-homes / quarantines a topic-word non-LEX distill"
        if session_capture.route_capture("F3E", "client Marcus Johnson was diagnosed with autism")[2] is not True:
            return "I2 route_capture does not quarantine a value-shaped PHI hit"
    except Exception as exc:  # noqa: BLE001
        return f"I2 route_capture raised ({exc})"
    if not (_REPO_ROOT / "scripts" / "refile_misrouted_session_captures.py").exists():
        return "I2 re-file script missing"
    return None


def _mirror_fold_present() -> str | None:
    src = (_REPO_ROOT / "scripts" / "mirror_claude_workspace.py").read_text(encoding="utf-8")
    if "phi_guard.prose_phi_legs(text)" not in src:
        return "cq-e4b0d20a313f fold missing: the mirror screen does not use the prose screen"
    return None


def _i3_present() -> str | None:
    from cora.kb_exclusions import KB_EXCLUDED_FOLDER_IDS
    walk_only = getattr(__import__("cora.kb_exclusions", fromlist=["x"]), "KB_EXCLUDED_WALK_ONLY_IDS", frozenset())
    if not COMPUTERS_ROOTS <= KB_EXCLUDED_FOLDER_IDS or not COMPUTERS_ROOTS <= walk_only:
        return "I3 the two Computers roots are NOT pinned walk-only on this tree"
    src = (_REPO_ROOT / "scripts" / "purge_cora_internal_kb.py").read_text(encoding="utf-8")
    if "--computers-root" not in src or "--impersonate" not in src:
        return "I3 purge flags missing"
    return None


_DASHBOARD_FOLDERS_LINE = re.compile(r"^\s*folders(?:\s*:\s*[^=]+?)?\s*=\s*(.+)$", re.MULTILINE)


def _dashboard_purge_scoped() -> str | None:
    src = (_REPO_ROOT / "scripts" / "purge_dashboard_kb.py").read_text(encoding="utf-8")
    m = _DASHBOARD_FOLDERS_LINE.search(src)
    if not m:
        return "cq-bd6eab1fcb44 fold: purge_dashboard_kb has no `folders = ...` assignment"
    rhs = m.group(1)
    # the ASSIGNMENT itself must draw from the dashboard set and not the exclusion set
    if "KB_DASHBOARD_FOLDER_IDS" not in rhs or "KB_EXCLUDED_FOLDER_IDS" in rhs:
        return f"cq-bd6eab1fcb44 fold missing: purge_dashboard_kb walks `{rhs.strip()}`"
    return None


def _i4_present() -> str | None:
    try:
        from cora.tools import tool_dispatch as td
    except Exception as exc:  # noqa: BLE001
        return f"tool_dispatch not importable ({exc})"
    if "cora_self_inventory" not in td._GLOBAL_CORE_TOOLS or "cora_self_inventory" not in td._TOOL_FUNCTIONS:
        return "I4 cora_self_inventory is not wired on this tree"
    src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
    # REACHABLE shape, not the bare assignment string: the branch condition followed
    # (comments allowed) by the assignment, inside _dispatch_qa (lens F #7: an
    # `elif False and ...` or a dead copy would pass a substring check)
    body = src[src.find("def _dispatch_qa("):]
    if not re.search(
        r"if force_tool is None and inventory_turn:\n(?:\s*#[^\n]*\n)*\s*force_tool = \"cora_self_inventory\"",
        body,
    ):
        return "I4 force route missing or unreachable in app._dispatch_qa"
    if "inventory_turn = bool(user_id) and retrieval_grant is None and _self_inventory_force(user_message) is not None" not in body:
        return "I4 the inventory predicate is not computed in _dispatch_qa"
    return None


def _i5_present() -> str | None:
    from cora.connectors import drive_sweep
    # the skip must be WIRED into sweep_user, not merely defined (lens F #7: a helper
    # left behind by a merge conflict passed a hasattr check)
    try:
        body = inspect.getsource(drive_sweep.sweep_user)
    except (OSError, TypeError) as exc:
        return f"I5 cannot read sweep_user source ({exc})"
    if "_file_disposition(" not in body or '"static_md_owned"' not in body:
        return "I5 the static_md-owned skip is not wired into drive_sweep.sweep_user"
    if not (_REPO_ROOT / "scripts" / "probe_two_door_entity_parity.py").exists():
        return "I5 parity probe missing"
    return None


PRECONDITIONS: dict[str, Callable[[], str | None]] = {
    "cq-c89cfab00b1f": _i1_present,
    "cq-bc5e5b7512bd": _i2_present,
    "cq-e4b0d20a313f": _mirror_fold_present,
    "cq-a0da505f8e5f": _i3_present,
    "cq-bd6eab1fcb44": _dashboard_purge_scoped,
    "cq-3542e1b095b2": _i4_present,
    "cq-12fd5d76fd04": _i5_present,
    "cq-b80c5bc5be7a": _i3_present,   # superseded only once the pin that supersedes it is real
}

SHIPPED: dict[str, str] = {
    "cq-c89cfab00b1f": "I1 banking identifiers redacted at chunk egress (renderer + MCP results)",
    "cq-bc5e5b7512bd": "I2 Leak #2: never re-home to LEX; prose screen -> quarantine; re-file script",
    "cq-e4b0d20a313f": "folded into I2: mirror ZONE-K screen uses the prose screen",
    "cq-a0da505f8e5f": "I3 Computers backup roots pinned walk-only + ancestry walk + purge flags",
    "cq-bd6eab1fcb44": "folded into I3: purge_dashboard_kb walks the dashboard set only",
    "cq-3542e1b095b2": "I4 cora_self_inventory tool + 'do you have X' force route",
    "cq-12fd5d76fd04": "I5 flat sweep leaves Founder-OS markdown to static_md + parity probe",
}
SUPERSEDED: dict[str, tuple[str, str]] = {
    "cq-b80c5bc5be7a": ("cq-a0da505f8e5f", "LOCATED: the door is the Drive Computers backup, closed by the I3 pin"),
}

_TERMINAL_NOT_SHIPPABLE = frozenset({"DISMISSED", "SUPERSEDED"})


def _ascii(s: object) -> str:
    """process_queue_action's messages carry emoji; a redirected stdout on this host
    is cp1252 and would raise AFTER the ledger write (D-051 pin lens MED-1)."""
    return str(s).encode("ascii", "replace").decode("ascii")


def _head_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
            cwd=str(_REPO_ROOT), timeout=10, creationflags=_NO_WINDOW,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _record(cq_id: str, transition: str, commit: str) -> None:
    """The bundle record the C7 gate (Code #12) will read: one ``reconciled`` event
    per transition with bundle_id + branch + commit. The fold ignores the kind
    (status untouched); this is provenance, written through the module's own
    ledger writer -- never a hand-edit of the jsonl."""
    code_queue._append_event({
        "event": "reconciled", "ts": code_queue._now_iso(), "id": cq_id,
        "transition": transition, "bundle_id": BUNDLE_ID, "branch": BRANCH, "commit": commit,
    })


def _record_safe(cq_id: str, transition: str, commit: str) -> str | None:
    """_record, reporting a failure INSTEAD of letting it read as a failed transition:
    the queue write already succeeded; only the provenance row is missing."""
    try:
        _record(cq_id, transition, commit)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {_ascii(exc)}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Perform the transitions. Omitted = report only.")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

    commit = _head_commit()
    print(f"Step 7.5 -- ingest-integrity bundle ({'APPLY' if args.apply else 'DRY-RUN'}) "
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
            print(f"  [dry-run] would mark SHIPPED  {cq_id}  {label}  (status {status})")
            continue
        try:
            outcome, msg = code_queue.process_queue_action(code_queue.ACTION_MARK_SHIPPED, cq_id, HARRISON_ID)
            if outcome == "shipped":
                rec_err = _record_safe(cq_id, "SHIPPED", commit)
                print(f"  SHIPPED  {cq_id}  {label}  -> {_ascii(msg)}"
                      + (f"  [provenance record FAILED: {rec_err}]" if rec_err else ""))
                if rec_err:
                    rc = 1
            else:
                print(f"  NOT SHIPPED  {cq_id}  {label}  -> {outcome}: {_ascii(msg)}")
                rc = 1
        except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
            print(f"  FAILED   {cq_id}  {label}  -> {type(exc).__name__}: {_ascii(exc)}")
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
                rec_err = _record_safe(loser, f"SUPERSEDED by {winner}", commit)
                print(f"  SUPERSEDED  {loser}  by {winner}  ({label})"
                      + (f"  [provenance record FAILED: {rec_err}]" if rec_err else ""))
                if rec_err:
                    rc = 1
            else:
                print(f"  NOT SUPERSEDED  {loser}  -> supersede_item returned False (ids missing or already superseded)")
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED   {loser}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
