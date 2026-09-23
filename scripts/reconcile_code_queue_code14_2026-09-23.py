"""Step 7.5 queue reconciliation for Code #14 -- the bug bundle
(branch ``claude/code-14-bug-bundle``, bundle_id ``code-14``).

Transitions ONLY the seeds this branch closed (kickoff section 1 + the 1b RIDER). Dry-run
by default; ``--apply`` writes through the module's own writer -- ``process_queue_action``
carrying bundle_id + branch + commit on the shipped event (the C7 HARD GATE) -- never by
hand-editing the jsonl or the backlog (loop step 7.5; the 2026-07-31 stale-positive
incident).

    SHIPPED (bundle_id code-14)
      cq-0f04ad6543a8  S2: health check full-detail log line + durable reports/health artifact
      cq-439d89a84de4  S3: phantom-rail hits adjudicable from disk (ledger + scrubbed snippet)
      cq-c7dbaef87633  S5: cashflow as_of via a metadata-only direct-SA credential; one WARN
      cq-a5b3e6a2e844  S6: fail-soft daily noise bundle (drive-sweep notify / revops 404 /
                       filer classifier / LEX-sync unsupported-mime count)
      cq-17fe76f5ab91  S7: mechanical card entity badge from resolve_entity (never '?')
      cq-2f6a6cee0169  S8: decision cards resolve raw Slack ids (Cora -> @Cora, unmapped ->
                       @unknown user)
      cq-323c8974fa02  R14-9: founder-DM honesty precision (forced ledger read on queue-status
                       intent; completion-grammar write-claim rail; capability-denial recall)
      cq-85b35413b020  R14-3: attribution-scoped rail 2 wired into run_preflight (ESC-1 (A) /
                       D-329; the Code #13 harness's SHIP: NO is resolved by the ruling that
                       took the two rail-less classes OUT of the condition)
    PRINTED ONLY (nothing written)
      cq-70d7b203f7ad  S1: already SHIPPED by Code #13 (RIDER 2, a5fafce) -- read back only
      cq-1a8611487e26  S4: DISMISSED 2026-09-21 -- never transitioned
      cq-a24f9d2210fc  read-back: STAGED with its kickoff on disk (LEX row: the path is
                       WITHHELD from the output). If it is still APPROVED the bare line
                       `stage cq-a24f9d2210fc` is printed for Harrison -- sessions never
                       stage on his behalf.
    NOT SEEDED (so nothing to transition): R14-1, R14-2, R14-4, R14-5, R14-6, R14-7, R14-8.

Every transition is GUARDED on the fix being present on the checked-out tree
(behavioural / reachability checks, never a grep for a string a stub could carry):
behaviour runs the shipped function; wiring parses the source with `ast` and needs a
real Call / keyword / assignment node, so a comment naming the wiring is blocked
(D-051 integration-tests-4). A DISMISSED or SUPERSEDED row is never flipped
(MARK_SHIPPED itself has no terminal guard).

    --probe-gate   proves the C7 gate on THIS tree against a throwaway ledger: a
                   MARK_SHIPPED with no bundle reference must be refused. Never touches
                   the real ledger.

Run (from the repo root, AFTER the FF-merge, with main's tree checked out):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code14_2026-09-23.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code14_2026-09-23.py --probe-gate
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code14_2026-09-23.py --apply
"""

from __future__ import annotations

import argparse
import ast
import inspect
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import code_queue  # noqa: E402

HARRISON_ID = "U0B2RM2JYJ1"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # D-266: every spawn windowless
BUNDLE_ID = "code-14"
BRANCH = "claude/code-14-bug-bundle"

READBACK_ID = "cq-a24f9d2210fc"


# -- code-presence preconditions (each returns a blocker string, or None) ----------
# D-051 integration-tests-4: every WIRING check parses the function's source with
# `ast` and looks for a real Call / keyword / assignment node -- a comment or a
# string literal carrying the same text is NOT a node, so a stub that only
# mentions the wiring is blocked. Every BEHAVIOUR check runs the shipped function.
def _fn_ast(fn: Callable) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


def _call_name(node: ast.Call) -> str:
    f = node.func
    return f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _call_name(n) == name]


def _capture_log(logger_name: str, fn: Callable[[], object]) -> tuple[object, list[str]]:
    hits: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):  # noqa: D401
            hits.append(record.getMessage())
    h = _H()
    lg = logging.getLogger(logger_name)
    lg.addHandler(h)
    prev = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        out = fn()
    finally:
        lg.removeHandler(h)
        lg.setLevel(prev)
    return out, hits


def _s2_present() -> str | None:
    import nightly_health_check as nhc
    for name in ("_build_artifact", "_render_artifact_md", "_artifact_paths", "_is_own_log"):
        if not hasattr(nhc, name):
            return f"S2 {name} missing from nightly_health_check"
    art = nhc._build_artifact([], 0.0, exit_code=0, dry_run=True, report_text="probe")
    if not isinstance(art, dict) or art.get("schema") != nhc.ARTIFACT_SCHEMA:
        return "S2 _build_artifact did not return a schema-stamped record"
    if art.get("report_text") != "probe" or "exit code: 0" not in nhc._render_artifact_md(art):
        return "S2 artifact does not carry the report text"
    if nhc._is_own_log(Path("bot-2026-09-23.log")):
        return "S2 own-log detector claims a bot log"
    # BEHAVIOUR (integration-tests-4): the check's OWN log re-read -- a quoted
    # critical in a report line must NOT re-raise, while a real writer failure the
    # check's process logged to the same file MUST stay critical. Throwaway dir;
    # the module's _LOG_DIR is restored.
    saved_dir = nhc._LOG_DIR
    tmpdir = Path(tempfile.mkdtemp(prefix="cq14-s2-"))
    try:
        now = datetime.now()
        ts = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S,000")
        own = tmpdir / f"health-check-{now:%Y-%m-%d}.log"
        own.write_text(f"{ts} INFO health-check: [CRITICAL] Critical log patterns: 1 critical "
                       f"pattern(s) found: |   * [cora-probe.log] {ts} ERROR cora.probe: "
                       f"REPEAT_SIGNAL_WRITE_FAILING quoted\n", encoding="utf-8")
        nhc._LOG_DIR = tmpdir
        got = {r.name: r.status for r in nhc.check_logs_24h()}
        if got.get("Critical log patterns") != "ok":
            return "S2 the check re-raises its own quoted report line from its own log"
        with own.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} ERROR cora.probe: REPEAT_SIGNAL_WRITE_FAILING real\n")
        got = {r.name: r.status for r in nhc.check_logs_24h()}
        if got.get("Critical log patterns") != "critical":
            return "S2 a real writer failure in the check's own log no longer reads critical"
    finally:
        nhc._LOG_DIR = saved_dir
        shutil.rmtree(tmpdir, ignore_errors=True)
    # BEHAVIOUR: the full-detail line folds newlines and never truncates.
    long_detail = "head\n" + ("x" * 150) + "\nTAIL-OF-DETAIL"
    flat = nhc._flatten_detail(long_detail)
    if "\n" in flat or not flat.endswith("TAIL-OF-DETAIL") or len(flat) < 150:
        return "S2 the health-check log line truncates or keeps newlines in the detail"
    # WIRING (AST): main() logs every result through _flatten_detail(r.detail).
    if not any(c.args and isinstance(c.args[0], ast.Attribute) and c.args[0].attr == "detail"
               for c in _calls(_fn_ast(nhc.main), "_flatten_detail")):
        return "S2 main() does not log the result detail through _flatten_detail"
    return None


def _s3_present() -> str | None:
    from cora import slack_egress as se, app
    for name in ("arm_rail_ledger", "PHANTOM_CLAIMS_LEDGER", "_rail_snippet", "_record_rail_hit"):
        if not hasattr(se, name):
            return f"S3 slack_egress.{name} missing"
    import importlib
    # WIRING (AST, integration-tests-4): a real call, never a comment that names it.
    main_tree = ast.parse(inspect.getsource(importlib.import_module("cora.main")))
    if not _calls(main_tree, "arm_rail_ledger"):
        return "S3 the bot entry point never arms the rail ledger"
    qa_tree = _fn_ast(app._dispatch_qa)
    passes = [kw for c in ast.walk(qa_tree) if isinstance(c, ast.Call) for kw in c.keywords
              if kw.arg == "rail_context" and isinstance(kw.value, ast.Name)
              and kw.value.id == "rail_ctx"]
    if len(passes) != 6:
        return "S3 rail_context is not passed at all six screen sites in _dispatch_qa"
    built = [n for n in ast.walk(qa_tree) if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "rail_ctx" for t in n.targets)
             and isinstance(n.value, ast.Call) and _call_name(n.value) == "_rail_context"]
    if len(built) != 1:
        return "S3 _dispatch_qa does not build rail_ctx from _rail_context"
    # BEHAVIOUR: the per-turn scope the bot actually computes. LEX scope and a Tier-2
    # grant turn withhold the snippet; an ordinary non-LEX turn keeps one.
    lex = app._rail_context("C0PROBE", None, "LEX-LLC", None, False, False)
    if (lex or {}).get("snippet_withheld") != se.RAIL_SNIPPET_WITHHELD_LEX:
        return "S3 app._rail_context does not withhold the snippet in LEX scope"
    grant = app._rail_context("C0PROBE", "U0PROBE", "F3E", object(), False, False)
    if (grant or {}).get("snippet_withheld") != se.RAIL_SNIPPET_WITHHELD_GRANT:
        return "S3 app._rail_context does not withhold the snippet on a Tier-2 grant turn"
    plain = app._rail_context("C0PROBE", None, "F3E", None, False, False)
    if (plain or {}).get("snippet_withheld") is not None:
        return "S3 app._rail_context withholds the snippet on an ordinary non-LEX turn"
    loc = lambda t: (0, min(len(t), 10))  # noqa: E731
    if se._rail_snippet("a LEX client note", loc, {"snippet_withheld": se.RAIL_SNIPPET_WITHHELD_LEX}) \
            != se.RAIL_SNIPPET_WITHHELD_LEX:
        return "S3 a LEX-scoped hit does not withhold its snippet"
    if se._rail_snippet("any text", loc, None) != se.RAIL_SNIPPET_NO_CONTEXT:
        return "S3 a hit with no scope context does not fail closed"
    # BEHAVIOUR: an armed ledger (redirected to a throwaway file) gets ONE row for a
    # zero-tool completion claim; the text is byte-identical (observe mode is never
    # flipped here). Every module global is restored.
    saved = (se._RAIL_LEDGER_ARMED, se.PHANTOM_CLAIMS_LEDGER, os.environ.get("CORA_EVAL_MODE"))
    tmpdir = Path(tempfile.mkdtemp(prefix="cq14-s3-"))
    try:
        se._RAIL_LEDGER_ARMED = True
        se.PHANTOM_CLAIMS_LEDGER = tmpdir / "phantom.jsonl"
        os.environ.pop("CORA_EVAL_MODE", None)
        text = "Done -- I staged the kickoff prompt for you."
        ctx = {"channel_id": "C0PROBE", "entity": "F3E", "snippet_withheld": None, "founder_belt": False}
        out = se.screen_phantom_write_claims(text, tool_use_count=0, channel_name="f3e-sales",
                                             user_id="U0PROBE", rail_context=ctx)
        if out != text:
            return "S3 the observe-mode screen mutated the reply"
        rows = se.PHANTOM_CLAIMS_LEDGER.read_text(encoding="utf-8").splitlines() \
            if se.PHANTOM_CLAIMS_LEDGER.exists() else []
        if len(rows) != 1:
            return f"S3 an armed ledger recorded {len(rows)} row(s) for one zero-tool claim (want 1)"
    finally:
        se._RAIL_LEDGER_ARMED, se.PHANTOM_CLAIMS_LEDGER, ev = saved
        if ev is None:
            os.environ.pop("CORA_EVAL_MODE", None)
        else:
            os.environ["CORA_EVAL_MODE"] = ev
        shutil.rmtree(tmpdir, ignore_errors=True)
    return None


def _s5_present() -> str | None:
    from cora.connectors import gsheets_financials as gf
    if getattr(gf, "_DRIVE_META_SCOPES", None) != ["https://www.googleapis.com/auth/drive.metadata.readonly"]:
        return "S5 the as_of read is not on a metadata-only scope"
    if gf.as_of_label(SimpleNamespace(as_of_date=gf.AS_OF_UNKNOWN)) != "as of: unknown":
        return "S5 an unknown as_of does not render the explicit unknown label"
    if gf.as_of_label(SimpleNamespace(as_of_date="2026-09-21")) != "as of 2026-09-21":
        return "S5 a real as_of date does not render"
    if not hasattr(gf, "_warn_as_of_unknown_once"):
        return "S5 the one-per-day as_of-unknown WARN is missing"
    return None


def _s6_present() -> str | None:
    import run_drive_sweep as rds
    saved = os.environ.pop("DRIVE_SWEEP_NOTIFY_CHANNEL", None)
    try:
        if rds._resolve_notify_channel() != "C0B7CADQ98S":
            return "S6a the drive-sweep summary is not pinned to #cora-health by id"
    finally:
        if saved is not None:
            os.environ["DRIVE_SWEEP_NOTIFY_CHANNEL"] = saved
    from cora.revops import sweep as rsw
    import httplib2
    from googleapiclient.errors import HttpError
    if rsw.GONE_HOLD_REASON != "gmail_thread_not_found":
        return "S6b the gone-thread hold reason is missing"
    if not rsw._is_gmail_not_found(HttpError(httplib2.Response({"status": 404}), b"")):
        return "S6b a Gmail 404 is not recognised"
    if rsw._is_gmail_not_found(HttpError(httplib2.Response({"status": 500}), b"")):
        return "S6b a Gmail 500 is treated as gone"
    from cora.connectors import attachment_filer as af
    lo, hi = af._first_attempt_max_tokens(1), af._first_attempt_max_tokens(40)
    if not (lo < hi <= af._FIRST_ATTEMPT_MAX_TOKENS):
        return "S6c the classifier max_tokens does not scale (bounded) with the attachment count"
    if not getattr(af, "QUARANTINE_REASON_UNPARSEABLE", ""):
        return "S6c the unparseable-classification quarantine reason is missing"
    import run_lex_dump_folder_sync as lex
    if "skipped_unsupported" not in inspect.getsource(lex.run):
        return "S6d the LEX sync run summary does not count unsupported-mime skips"
    return None


def _s7_present() -> str | None:
    from cora import knowledge_review as kr
    body = kr.format_mechanical_dm({"update_type": "task_close", "description": "Close a probe task",
                                    "payload": {}})
    first = body.splitlines()[0] if body else ""
    if "`?`" in first or first.rstrip().endswith("?"):
        return "S7 an unresolved mechanical card still renders a '?' badge"
    return None


def _s8_present() -> str | None:
    from cora import knowledge_review as kr
    out = kr._card_resolve_slack_ids("<@U0B44MDGC5R> said the vendor call moved")
    if "<@U" in out or "@Cora" not in out:
        return "S8 Cora's own raw id is not resolved on a review card"
    out2 = kr._card_resolve_slack_ids("<@UZZZZZZZZ9> asked for it")
    if "<@U" in out2 or "@unknown user" not in out2:
        return "S8 an unmapped raw id does not render @unknown user"
    return None


def _r149_present() -> str | None:
    from cora import slack_egress as se, app
    from cora.tools import tool_dispatch as td
    if not code_queue.is_queue_status_question("have my cards been responded to?"):
        return "R14-9(a) a card-status question is not recognised"
    if code_queue.is_queue_status_question("stage cq-0123456789ab"):
        return "R14-9(a) a typed verb is misread as a status question"
    if "cora_queue_status" not in getattr(td, "_TOOL_FUNCTIONS", {}):
        return "R14-9(a) the cora_queue_status ledger-read tool is not registered"
    if "cora_queue_status" in set(getattr(td, "_GLOBAL_CORE_TOOLS", ())):
        return "R14-9(a) cora_queue_status leaked into the global core tool set"
    seam = getattr(app, "_queue_status_turn", None)
    if not callable(seam):
        return "R14-9(a) the forcing seam _queue_status_turn is missing"
    # BEHAVIOUR (integration-tests-4): the seam forces for Harrison in his DM only.
    ask = "have my cards been responded to?"
    if seam(code_queue.HARRISON_ID, "dm", None, ask, []) is not True:
        return "R14-9(a) the forcing seam does not fire for Harrison's card-status DM"
    if seam("U0PROBEMEMBER", "dm", None, ask, []) or \
            seam(code_queue.HARRISON_ID, "f3e-sales", None, ask, []) or \
            seam(code_queue.HARRISON_ID, "dm", object(), ask, []):
        return "R14-9(a) the forcing seam fires outside Harrison's DM (member / channel / grant)"
    # WIRING (AST): _dispatch_qa consults the seam and forces the ledger-read tool.
    qa_tree = _fn_ast(app._dispatch_qa)
    forced = [n for n in ast.walk(qa_tree) if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "force_tool" for t in n.targets)
              and isinstance(n.value, ast.Constant) and n.value.value == "cora_queue_status"]
    if not _calls(qa_tree, "_queue_status_turn") or not forced:
        return "R14-9(a) _dispatch_qa never consults the seam or never forces cora_queue_status"
    if se._find_write_claim("the nine staged prompts are waiting for you") is not None:
        return "R14-9(b) a descriptive 'staged' still counts as a write claim"
    if se._find_write_claim("I staged the kickoff prompt for you.") is None:
        return "R14-9(b) a first-person completion claim no longer fires"
    text = "I don't have direct read access to the card ledger, so I can't say which cards are done."
    out, hits = _capture_log(se.__name__, lambda: se.screen_capability_claims(
        text, tool_use_count=0, channel_name="dm", user_id=HARRISON_ID, entity="FNDR",
        cross_entity=True, founder=True))
    if out != text:
        return "R14-9(c) the capability screen mutated text outside enforce mode"
    if not any("phantom-capability-claim" in m for m in hits):
        return "R14-9(c) a founder-DM ledger-access denial at tool_use=0 did not WARN"
    return None


def _r143_present() -> str | None:
    from cora.f3e_blog import preflight as pf
    if not hasattr(pf, "rail2_attribution_hit"):
        return "R14-3 rail2_attribution_hit missing"
    # WIRING (AST, integration-tests-4): a real call to the attribution rail and no
    # call to the legacy one -- a comment naming either is not a call.
    rp_tree = _fn_ast(pf.run_preflight)
    if not _calls(rp_tree, "rail2_attribution_hit") or _calls(rp_tree, "rail2_legacy_hit"):
        return "R14-3 run_preflight is not wired to the attribution rail"
    # BEHAVIOUR: the SHIPPING preflight trips R2 on a clean claim of Energy and
    # passes the ruled phrase, which the frozen legacy rail still trips -- so a
    # run_preflight that fell back to the legacy scan (or dropped rail 2) is blocked.
    ruled = "F3 Energy carries 120 mg of natural caffeine from green tea."
    if "R2" not in pf.run_preflight(title="t", summary="",
                                    body_html="<p>F3 Energy is a clean energy drink.</p>"
                                    ).tripped_rail_ids:
        return "R14-3 run_preflight does not trip rail 2 on a clean claim of Energy"
    if "R2" in pf.run_preflight(title="t", summary="", body_html="<p>%s</p>" % ruled).tripped_rail_ids:
        return "R14-3 run_preflight still trips rail 2 on the ruled green-tea phrase"
    if hasattr(pf, "rail2_legacy_hit") and pf.rail2_legacy_hit(ruled) is None:
        return "R14-3 the frozen legacy baseline no longer trips the ruled phrase (probe is blind)"
    if pf.rail2_attribution_hit("F3 Energy carries 120 mg of natural caffeine from green tea.") is not None:
        return "R14-3 the ruled exact phrase 'natural caffeine from green tea' still trips"
    if pf.rail2_attribution_hit("F3 Energy is a clean energy drink.") is None:
        return "R14-3 a clean claim predicated of Energy no longer trips"
    return None


def _gate(fn: Callable[[], str | None]) -> str | None:
    """Run one precondition; a RAISE is a blocker, never an abort (Code #13 lens E F10)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return f"precondition raised {type(exc).__name__}: {_ascii(exc)}"


PRECONDITIONS: dict[str, Callable[[], str | None]] = {
    "cq-0f04ad6543a8": _s2_present,
    "cq-439d89a84de4": _s3_present,
    "cq-c7dbaef87633": _s5_present,
    "cq-a5b3e6a2e844": _s6_present,
    "cq-17fe76f5ab91": _s7_present,
    "cq-2f6a6cee0169": _s8_present,
    "cq-323c8974fa02": _r149_present,
    "cq-85b35413b020": _r143_present,
}

SHIPPED: dict[str, str] = {
    "cq-0f04ad6543a8": "S2: health check full-detail log line + durable run artifact",
    "cq-439d89a84de4": "S3: phantom-rail hits adjudicable from disk (ledger + scrubbed snippet)",
    "cq-c7dbaef87633": "S5: cashflow as_of via a metadata-only direct-SA credential",
    "cq-a5b3e6a2e844": "S6: fail-soft daily noise bundle (S6a-S6d)",
    "cq-17fe76f5ab91": "S7: mechanical card entity badge (never '?')",
    "cq-2f6a6cee0169": "S8: decision cards resolve raw Slack ids",
    "cq-323c8974fa02": "R14-9: founder-DM honesty precision (a/b/c)",
    "cq-85b35413b020": "R14-3: attribution-scoped rail 2 wired into run_preflight",
}
ALREADY_SHIPPED: dict[str, str] = {
    "cq-70d7b203f7ad": "S1 rode Code #13 (RIDER 2, a5fafce) -- read back only",
}
DISMISSED_NOT_TOUCHED: dict[str, str] = {
    "cq-1a8611487e26": "S4 DISMISSED 2026-09-21 -- dropped from this bundle, never transitioned",
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


def _readback_line() -> tuple[str, bool]:
    """(line, ok) for the cq-a24f9d2210fc read-back. Reads the RAW fold (the LEX-safe
    view replaces prompt_path with a marker) but never prints the path: this is a LEX
    row, so only its status and whether its kickoff file exists leave this function."""
    rec = code_queue._fold_items().get(READBACK_ID)
    if rec is None:
        return f"  READ-BACK  {READBACK_ID}  -> MISSING (no queue item)", False
    status = str(rec.get("status", ""))
    if status == "APPROVED":
        return (f"  READ-BACK  {READBACK_ID}  (APPROVED, no kickoff yet) -- Harrison, send this "
                f"bare line in your Cora DM to stage it:\n\nstage {READBACK_ID}\n"), True
    if status != "STAGED":
        return f"  READ-BACK  {READBACK_ID}  ({status}) -> not STAGED; nothing to do here", True
    path = str(rec.get("prompt_path") or "")
    if not path:
        return f"  READ-BACK  {READBACK_ID}  (STAGED) -> NO prompt_path on the row", False
    try:
        from cora import drive_io
        present = drive_io.exists(path)
    except Exception as exc:  # noqa: BLE001 -- a gone mount cannot answer "absent"
        return (f"  READ-BACK  {READBACK_ID}  (STAGED) -> kickoff existence UNKNOWN "
                f"({type(exc).__name__}; path withheld, LEX row)"), False
    if present:
        return f"  READ-BACK  {READBACK_ID}  (STAGED) -> kickoff on disk (path withheld, LEX row)", True
    return f"  READ-BACK  {READBACK_ID}  (STAGED) -> kickoff file MISSING (path withheld, LEX row)", False


def probe_gate() -> int:
    """Prove the C7 gate on THIS tree against a THROWAWAY ledger -- a MARK_SHIPPED with
    no bundle reference must be refused. Never touches the real ledger (the module
    constants are swapped for temp paths and restored)."""
    saved = (code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER)
    saved_sync = code_queue._SYNC
    tmpdir = Path(tempfile.mkdtemp(prefix="cq-probe-"))
    try:
        code_queue._EVENT_LEDGER = tmpdir / "ledger.jsonl"
        code_queue._FINGERPRINT_LEDGER = tmpdir / "fp.jsonl"
        code_queue._SIGNALS_LEDGER = tmpdir / "sig.jsonl"
        code_queue._SYNC = True
        cid = code_queue.seed_item(kind="bug", severity="P2", title="probe-gate fixture code-14",
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
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Perform the transitions. Omitted = report only.")
    ap.add_argument("--probe-gate", action="store_true",
                    help="Prove the C7 gate against a throwaway ledger; writes nothing real.")
    args = ap.parse_args(argv)
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

    commit = _head_commit()
    print(f"Step 7.5 -- Code #14 ({'APPLY' if args.apply else 'DRY-RUN'}) "
          f"bundle_id={BUNDLE_ID} branch={BRANCH} commit={commit or '?'}\n")

    rc = 0
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

    for cq_id, note in ALREADY_SHIPPED.items():
        rec = code_queue.get_item(cq_id)
        status = rec.get("status") if rec else "MISSING"
        print(f"  ALREADY SHIPPED  {cq_id}  ({status}; bundle {rec.get('bundle_id') if rec else '?'})  -> {note}")
        if status != "SHIPPED":
            print(f"    !! expected SHIPPED -- Code #13's 7.5 has not landed {cq_id}; S1 comes back FIRST")
            rc = 1
    for cq_id, note in DISMISSED_NOT_TOUCHED.items():
        rec = code_queue.get_item(cq_id)
        print(f"  NOT TOUCHED  {cq_id}  ({rec.get('status') if rec else 'MISSING'})  -> {note}")

    line, ok = _readback_line()
    print(line)
    if not ok:
        rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
