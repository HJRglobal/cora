"""Step 7.5 queue reconciliation for Code #16 -- the new-capability lanes bundle
(branch ``claude/code-16-new-capability-lanes-2026-09-21``, bundle_id ``code-16``).

Transitions ONLY the seeds this branch closed (kickoff section 7). Dry-run by default
(``--dry-run`` is accepted as an explicit no-op alias for the kickoff's C4 wording);
``--apply`` writes through the module's own writers -- ``process_queue_action``
carrying bundle_id + branch + commit on the shipped event (the C7 HARD GATE) and
``supersede_item`` + a ``reconciled`` provenance event -- never by hand-editing the
jsonl or the backlog (loop step 7.5).

    SHIPPED (bundle_id code-16; commit = THIS ITEM's own commits, never HEAD)
      cq-be90cea867c3  C1: dead-channel archive lane, born T0 (metadata-only scan ->
                       proposal card in Harrison's DM on ask + monthly; T1 executor
                       built DARK behind the live registry tier + CORA_CHANNEL_ARCHIVE=act)
      cq-e9ef3f581d60  C2: travel shortlist lane, T0 rung (web-search-only turn built
                       from parsed fields; <= 5 options in-thread; no booking / loyalty /
                       guest identity in any web request -- structurally)
    SUPERSEDED (ruled 2026-09-21 kickoff section 9.3: "C1 ships via the API" -> both
    browser mechanisms are superseded; the live scopes channels:manage + groups:write
    were probed PRESENT 2026-09-25, so the API path is not blocked)
      cq-4f3fcc9b7096  Cora drives browser tabs + a capability-grant framework
      cq-062291f79968  Cowork Chrome-lane bulk-archive executor
    NOT TOUCHED (printed only)
      cq-b93e2abf2922  [LEX] build ask, details withheld -- Harrison DISMISSED it himself
                       2026-09-25T14:35:47Z (kickoff section 9.4 resolved by his tap)
    SEEDED (PROPOSED; only under --apply; idempotent on fingerprint), tagged [code-16]

Every transition is GUARDED on the fix being present on the checked-out tree
(behavioural checks that RUN the shipped function; wiring checks parse the source
with `ast` and need a real node -- D-051 integration-tests-4). A DISMISSED or
SUPERSEDED row is never flipped; a SHIPPED or DISMISSED loser is never superseded.

    --probe-gate   proves the C7 gate on THIS tree against a throwaway ledger: a
                   MARK_SHIPPED with no bundle reference must be refused. Never touches
                   the real ledger.

Run (from the repo root, AFTER the FF-merge, with main's tree checked out -- the queue
ledger lives in the primary checkout's data/state/, which a worktree does not have):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code16_2026-09-25.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code16_2026-09-25.py --probe-gate
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code16_2026-09-25.py --apply
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import code_queue  # noqa: E402

HARRISON_ID = "U0B2RM2JYJ1"
BUNDLE_ID = "code-16"
BRANCH = "claude/code-16-new-capability-lanes-2026-09-21"

# The commits that closed each item (build + D-051 fixes), on the branch as merged.
COMMITS: dict[str, str] = {
    "cq-be90cea867c3": "bd63aa27,def660ef,40d560e4,3bf2dbc5,327dab51,0ab4804e,38a79c98",
    "cq-e9ef3f581d60": "bd63aa27,2e31eff4,f9007a24,a16224f9,11478209,1d2961d7",
}
SUPERSEDE_COMMIT = COMMITS["cq-be90cea867c3"]

_APP = _REPO_ROOT / "src" / "cora" / "app.py"
_SRC = _REPO_ROOT / "src" / "cora"
# A synthetic ask: a fake guest name, email, phone and loyalty number that must NEVER
# reach the web request (never the real 9/15 ask text).
_SYNTH_ASK = ("can you find hotels and airbnbs in the scottsdale area for october 17th-october 21 "
              "for Jordan Riverstone, about 4 people, $300-$400/night, a king bed. Our Hilton "
              "Honors account is 123456789, email jordan.r@example.com, cell 480-555-0199")
_SYNTH_TOKENS = ("jordan", "riverstone", "hilton", "honors", "123456789", "example.com", "480-555")


def _ascii(s: object) -> str:
    return str(s).encode("ascii", "replace").decode("ascii")


def _decorated_actions(tree: ast.AST) -> set[str]:
    """`X.ACTION_*` attribute names used as @app.action(...) decorator arguments."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "action" and dec.args
                    and isinstance(dec.args[0], ast.Attribute)
                    and isinstance(dec.args[0].value, ast.Name)
                    and dec.args[0].value.id == "channel_archive_cards"):
                out.add(dec.args[0].attr)
    return out


def _calls_named(tree: ast.AST, name: str) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            nm = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if nm == name:
                n += 1
    return n


def _c1_present() -> str | None:
    """C1: the API archive path exists ONLY in the tap handler, the lane is wired into
    the bot (every card action decorated), the founder ask grammar fires, and the
    registry row is T0 with the acting probe."""
    from cora import ladder_registry as lr
    from cora.channel_archive import intents, policy
    from cora.channel_archive import cards as ca_cards
    handler_src = (_SRC / "channel_archive" / "handler.py").read_text(encoding="utf-8-sig")
    if _calls_named(ast.parse(handler_src), "conversations_archive") < 1:
        return "channel_archive/handler.py has no conversations_archive call (the API path is absent)"
    for p in sorted(_SRC.rglob("*.py")):
        if p.name == "handler.py" and p.parent.name == "channel_archive":
            continue
        if _calls_named(ast.parse(p.read_text(encoding="utf-8-sig")), "conversations_archive"):
            return f"conversations_archive is called outside the tap handler: {p.name}"
    wired = _decorated_actions(ast.parse(_APP.read_text(encoding="utf-8-sig")))
    want = {"ACTION_ROW", "ACTION_ALL", "ACTION_KEEP", "ACTION_OVERRIDE", "ACTION_AGREED"}
    if not want <= wired:
        return f"app.py does not decorate every card action: missing {sorted(want - wired)}"
    for a in want:
        if not str(getattr(ca_cards, a, "")).startswith("cora_channel_archive_"):
            return f"cards.{a} is not a lane action id"
    if not intents.looks_like_archive_ask("can you archive the dead channels?"):
        return "the founder ask grammar does not fire on the 9/20 ask"
    if intents.looks_like_archive_ask("what's in the kb archive?"):
        return "the founder ask grammar fires on a noun-sense 'archive'"
    reg = lr.load()
    row = lr.row_for("slack-channel-archive", reg)
    if not row or row.get("tier") != "T0" or row.get("acting_probe") != "channel_archive_mode":
        return "ladder row slack-channel-archive is missing, not T0, or lacks its acting probe"
    if lr.validate(reg):
        return f"ladder registry does not validate: {lr.validate(reg)[:2]}"
    if policy.mode.__module__ != "cora.channel_archive.policy":
        return "policy.mode is not the lane's own reader"
    return None


def _c2_present() -> str | None:
    """C2: a named, loyalty-bearing synthetic ask parses to fields only; the built
    request carries none of the identity tokens, passes the allowlist belt, and uses
    ONLY web_search_20250305; the normal Q&A path carries the travel_lane withhold."""
    from cora import ladder_registry as lr
    from cora import travel_shortlist as tsl
    pr = tsl.parse_constraints(_SYNTH_ASK, today=date(2026, 9, 25))
    if pr.constraints is None:
        return f"the synthetic ask did not parse (missing {pr.missing})"
    req = tsl.build_request(pr.constraints)
    blob = json.dumps(req).lower()
    leaked = [t for t in _SYNTH_TOKENS if t in blob]
    if leaked:
        return f"the built request carries identity tokens {leaked}"
    if tsl.assert_request_clean(req, pr.constraints) is not None:
        return "assert_request_clean refuses the clean request"
    types = [t.get("type") for t in req.get("tools") or []]
    if types != ["web_search_20250305"]:
        return f"the request tools are {types}, not exactly [web_search_20250305]"
    tree = ast.parse(_APP.read_text(encoding="utf-8-sig"))
    consts = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    if "travel_lane" not in consts:
        return "app.py has no travel_lane web_gate_skip leg (the normal-path withhold)"
    row = lr.row_for("travel-shortlist", lr.load())
    if not row or row.get("tier") != "T0" or row.get("cap") != "none":
        return "ladder row travel-shortlist is missing, not T0, or not cap: none"
    return None


def _gate(fn: Callable[[], str | None]) -> str | None:
    """Run one precondition; a RAISE is a blocker, never an abort."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return f"precondition raised {type(exc).__name__}: {_ascii(exc)}"


PRECONDITIONS: dict[str, Callable[[], str | None]] = {
    "cq-be90cea867c3": _c1_present,
    "cq-e9ef3f581d60": _c2_present,
    # superseded only once the fix that supersedes them is real (the API path)
    "cq-4f3fcc9b7096": _c1_present,
    "cq-062291f79968": _c1_present,
}

SHIPPED: dict[str, str] = {
    "cq-be90cea867c3": "C1: dead-channel archive lane, born T0 (T1 executor dark)",
    "cq-e9ef3f581d60": "C2: travel shortlist lane, T0 rung (fields-only web-search turn)",
}
SUPERSEDED: dict[str, tuple[str, str]] = {
    "cq-4f3fcc9b7096": ("cq-be90cea867c3", "ruled 9.3: the API lane supersedes Cora driving browser tabs"),
    "cq-062291f79968": ("cq-be90cea867c3", "ruled 9.3: the API lane supersedes the Chrome-lane executor"),
}
NOT_TOUCHED: dict[str, str] = {
    "cq-b93e2abf2922": "Harrison DISMISSED it 2026-09-25T14:35:47Z (kickoff 9.4 resolved by his tap)",
}

SEEDS: list[tuple[str, str, str, str]] = [
    ("bug", "P2", "[code-16] Deposco push card loses its buttons on a retryable outcome",
     "app._handle_deposco_push_tap applies terminal_card_blocks on the `error` and `drifted_restaged` "
     "outcomes although the handler released the claim back to STAGED, so the card headlines "
     "'Resolved: STAGED' with no buttons over a still-tappable entry (lesson 3). The f3e sibling keeps "
     "buttons via RETRYABLE_OUTCOMES/keep_buttons -- copy it. Found by the Code #16 cardtap map."),
    ("bug", "P3", "[code-16] test_channel_health_monitor run() writes the real logs/channel-health-<date>.md",
     "tests/test_channel_health_monitor.py drives chm.run() without redirecting the script's _REPO_ROOT, "
     "so _write_full_list writes a real logs/channel-health-<date>.md with fixture ids (C_DUP / C_SPR) on "
     "every suite run. Redirect the root in the test (or conftest) and add a byte-unchanged guard."),
    ("bug", "P3", "[code-16] Q&A web turns use web_search_20260209 with a 120 s timeout",
     "Code #16 measured ONE synthetic lodging query on claude-sonnet-5: web_search_20260209 (dynamic "
     "filtering = server-side code execution) ran > 470 s without finishing (non-streaming timeouts at "
     "120 s and 400 s; a streamed run killed at 560 s), while the basic web_search_20250305 finished in "
     "22 s. The live Q&A web path (claude_client._build_web_tool_defs, _WEB_TIMEOUT 120 s) may time out "
     "on heavier asks. Measure the live web-turn latency distribution before choosing a fix."),
    ("config", "P3", "[code-16] slack-app-config/manifest.json lists 15 bot scopes; the live token has 74",
     "auth.test x-oauth-scopes (2026-09-25) shows 74 scopes incl. channels:manage, groups:write, pins:read, "
     "bookmarks:read, channels:join; the checked-in manifest and runbook.md:812 still say 15. Refresh the "
     "manifest from the live app config (read-only export) so a rebuild does not silently drop scopes."),
    ("config", "P3", "[code-16] channel_content_guard parity on the travel shortlist card",
     "The travel card posts web-derived fit notes in blocks without channel_content_guard.guard_outbound "
     "(the lane bypasses _dispatch_qa's reply gates by design; slack_egress sanitize_text runs at build). "
     "Decide whether the guard should run over the card for parity (builder open risk, Code #16 C2)."),
]

_TERMINAL_NOT_SHIPPABLE = frozenset({"DISMISSED", "SUPERSEDED"})


def _provenance(cq_id: str, transition: str, commit: str) -> str | None:
    """A `reconciled` record for the non-shipped transitions, so those rows answer
    "which bundle did this" too (supersede_item carries no bundle provenance)."""
    try:
        code_queue._append_event({
            "event": "reconciled", "ts": code_queue._now_iso(), "id": cq_id,
            "transition": transition, "bundle_id": BUNDLE_ID, "branch": BRANCH, "commit": commit,
        })
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {_ascii(exc)}"


def probe_gate() -> int:
    """Prove the C7 gate on THIS tree against a THROWAWAY ledger."""
    saved = (code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER)
    saved_sync = code_queue._SYNC
    tmpdir = Path(tempfile.mkdtemp(prefix="cq-probe-"))
    try:
        code_queue._EVENT_LEDGER = tmpdir / "ledger.jsonl"
        code_queue._FINGERPRINT_LEDGER = tmpdir / "fp.jsonl"
        code_queue._SIGNALS_LEDGER = tmpdir / "sig.jsonl"
        code_queue._SYNC = True
        cid = code_queue.seed_item(kind="bug", severity="P2", title="probe-gate fixture code-16",
                                   summary="throwaway", entity="F3E", signal="explicit",
                                   status="APPROVED")
        if not cid:
            print("PROBE: could not seed the throwaway item")
            return 1
        outcome, msg = code_queue.process_queue_action(code_queue.ACTION_MARK_SHIPPED, cid, HARRISON_ID)
        if outcome != "refused":
            print(f"PROBE FAILED: MARK_SHIPPED without a bundle reference returned {outcome!r}: {_ascii(msg)}")
            return 1
        outcome2, msg2 = code_queue.process_queue_action(
            code_queue.ACTION_MARK_SHIPPED, cid, HARRISON_ID, bundle_id=BUNDLE_ID, branch=BRANCH)
        if outcome2 != "shipped":
            print(f"PROBE FAILED: MARK_SHIPPED WITH a bundle reference returned {outcome2!r}: {_ascii(msg2)}")
            return 1
        print(f"PROBE OK: no-bundle MARK_SHIPPED refused ({_ascii(msg)[:90]}...); "
              f"with bundle_id={BUNDLE_ID} -> shipped")
        return 0
    finally:
        code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER = saved
        code_queue._SYNC = saved_sync
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Perform the transitions + seeds. Omitted = report only.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Explicit no-op alias for the default report-only run (kickoff C4 wording).")
    ap.add_argument("--probe-gate", action="store_true",
                    help="Prove the C7 gate against a throwaway ledger; writes nothing real.")
    args = ap.parse_args(argv)
    if args.apply and args.dry_run:
        print("REFUSED: --apply and --dry-run together -- pick one")
        return 2
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    if code_queue.HARRISON_ID != HARRISON_ID:
        print(f"REFUSED: code_queue.HARRISON_ID ({code_queue.HARRISON_ID!r}) != {HARRISON_ID!r} -- "
              "every transition would be refused as not_authorized; fix the env first")
        return 1
    if args.probe_gate:
        return probe_gate()

    print(f"Step 7.5 -- Code #16 ({'APPLY' if args.apply else 'DRY-RUN'}) bundle_id={BUNDLE_ID} "
          f"branch={BRANCH} (commit = each item's own commits)\n")
    rc = 0
    shipped_ok: set[str] = set()
    for cq_id, label in SHIPPED.items():
        blocker = _gate(PRECONDITIONS.get(cq_id, lambda: "no precondition registered"))
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
            shipped_ok.add(cq_id)
            continue
        if status in _TERMINAL_NOT_SHIPPABLE:
            print(f"  NOT SHIPPED  {cq_id}  {label}  -> status is {status}; refusing to flip a terminal row")
            rc = 1
            continue
        commit = COMMITS[cq_id]
        if not args.apply:
            print(f"  [dry-run] would mark SHIPPED  {cq_id}  {label}  (status {status}; bundle={BUNDLE_ID}; "
                  f"commit={commit})")
            shipped_ok.add(cq_id)
            continue
        try:
            outcome, msg = code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cq_id, HARRISON_ID,
                bundle_id=BUNDLE_ID, branch=BRANCH, commit=commit)
            if outcome == "shipped":
                print(f"  SHIPPED  {cq_id}  {label}  -> {_ascii(msg)}")
                shipped_ok.add(cq_id)
            else:
                print(f"  NOT SHIPPED  {cq_id}  {label}  -> {outcome}: {_ascii(msg)}")
                rc = 1
        except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
            print(f"  FAILED   {cq_id}  {label}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1

    # Supersede AFTER the ships: supersede_item bumps the winner's recurrence count, so
    # the winner must already be in its final state.
    for loser, (winner, label) in SUPERSEDED.items():
        blocker = _gate(PRECONDITIONS.get(loser, lambda: "no precondition registered"))
        if blocker:
            print(f"  BLOCKED  {loser}  supersede by {winner}  -> {blocker}")
            rc = 1
            continue
        if winner not in shipped_ok:
            print(f"  NOT SUPERSEDED  {loser}  -> the winner {winner} did not ship in this run")
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
                err = _provenance(loser, f"SUPERSEDED by {winner}", SUPERSEDE_COMMIT)
                print(f"  SUPERSEDED  {loser}  by {winner}  ({label})"
                      + (f"  [provenance record FAILED: {err}]" if err else ""))
                if err:
                    rc = 1
            else:
                print(f"  NOT SUPERSEDED  {loser}  -> supersede_item returned False")
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED   {loser}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1

    for cq_id, note in NOT_TOUCHED.items():
        rec = code_queue.get_item(cq_id)
        print(f"  NOT TOUCHED  {cq_id}  ({rec.get('status') if rec else 'MISSING/UNREADABLE'})  -> {note}")

    print(f"\n  SEEDS ({len(SEEDS)}; PROPOSED, idempotent on fingerprint)")
    for kind, sev, title, summary in SEEDS:
        if not args.apply:
            print(f"  [dry-run] would seed  {sev:6}  {title}")
            continue
        try:
            cid = code_queue.seed_item(kind=kind, severity=sev, title=title, summary=summary,
                                       entity="FNDR", signal="explicit", status="PROPOSED",
                                       subsystem_guess="code-16")
            print(f"  SEEDED  {cid or 'REFUSED (PHI screen or invalid)'}  {sev:6}  {title}")
            if not cid:
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  SEED FAILED  {title}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
