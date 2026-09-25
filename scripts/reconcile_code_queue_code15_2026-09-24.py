"""Step 7.5 queue reconciliation for Code #15 -- the integrity + queue-UX bundle
(branch ``claude/code-15-integrity-queue-ux-2026-09-21``, bundle_id ``code-15``).

Transitions ONLY the seeds this branch closed (kickoff section 7). Dry-run by default;
``--apply`` writes through the module's own writer -- ``process_queue_action``
carrying bundle_id + branch + commit on the shipped event (the C7 HARD GATE) -- never
by hand-editing the jsonl or the backlog (loop step 7.5; the 2026-07-31 stale-positive
incident).

    SHIPPED (bundle_id code-15; commit = THIS ITEM's own commits, never HEAD -- the
    #13/#14 provenance nit: their scripts stamped `git rev-parse HEAD` on every row)
      cq-d9d0c92cc797  S1: API-token-shape redactor at every banking-belt renderer +
                       the session-capture harvest belts + the combined KB purge (lane
                       s1_tokens; the APPLY is Harrison's)
      cq-22b84598aee8  S2: gap-executor creates into a real project + assignee or
                       refuses visibly; two-tier dedup (normalized title + entity)
      cq-90568f0b1222  S3: DW 9/10 diagnosis (FAILED = content_guard/non_lex_phi on
                       ask #1; ask #2 never reached the tool -- a zero-tool "Queued"
                       ack) + the confirm-shape pins
      cq-74e6b20d5d3d  S3: requester (id + roster name) + per-requester counts on the
                       founder-local cora_delegated_jobs surface
      cq-5f44ce934aeb  S4: carve-out breach = a NO-RECORD carve-out only; breach event
                       ids (+ transcript ids) on the audit row, titles never
      cq-2d26f131091e  S5: a Monday-menu press re-renders the pressed (and every
                       decided) row; Keep idempotent per card
      cq-3a29e7dc3953  S6: kickoff files -- code-owned STATUS line + H1, wrapper
                       stripped, truncation marked, shape gate + read-back
      cq-59c5048d0891  RIDER B (KB): ships WITH its per-item record (never bare):
                       items 1,2,4 + C13-14 built; 3 = rows 2-4 PURGE / row 1 HELD;
                       5 A8 = NOT path-keyed; 6+7 = a PROPOSED manifest only; 8 + every
                       purge lane = the stop-window APPLY is Harrison's
      cq-592baba613f1  RIDER B (R8): weekly NO-HASH hygiene instrument + report; the
                       task REGISTRATION and the Cowork Notion rewrite are Harrison's
    NOT SHIPPED / NOT TOUCHED (printed only)
      cq-3b3130dd3a4c  S6b: PROPOSED at fire (no approval) -- did NOT ride
      cq-11b694215709  the 8/18 orphan fixture -- UNTOUCHED by design (reducer pinned)
      cq-24b88a65a5ae  rider (a) fixed its item (2) (the 4 worktree-red tests); items
                       (1),(3),(4) stay open -> left PROPOSED
      cq-17fe76f5ab91  Code #14's, not this bundle's
    SEEDED (PROPOSED; only under --apply; idempotent on fingerprint)
      9 rider (b) seeds: the #13 Appendix-A LOW text VERBATIM, tagged [code-15-rider]
      the bundle's follow-up seeds, tagged [code-15]

Every transition is GUARDED on the fix being present on the checked-out tree
(behavioural / reachability checks that RUN the shipped function; wiring checks parse
the source with `ast` and need a real node -- D-051 integration-tests-4). A DISMISSED or
SUPERSEDED row is never flipped.

    --probe-gate   proves the C7 gate on THIS tree against a throwaway ledger: a
                   MARK_SHIPPED with no bundle reference must be refused. Never touches
                   the real ledger.

Run (from the repo root, AFTER the FF-merge, with main's tree checked out):
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code15_2026-09-24.py
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code15_2026-09-24.py --probe-gate
    .venv\\Scripts\\python.exe scripts\\reconcile_code_queue_code15_2026-09-24.py --apply
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import inspect
import shutil
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import code_queue  # noqa: E402

HARRISON_ID = "U0B2RM2JYJ1"
BUNDLE_ID = "code-15"
BRANCH = "claude/code-15-integrity-queue-ux-2026-09-21"

# The commits that closed each item (build + D-051 fixes), on the branch as merged.
COMMITS: dict[str, str] = {
    "cq-d9d0c92cc797": "dc261783,eaf3707c,37b390e5,e3b83459,6016bfa1",
    "cq-22b84598aee8": "0ee863cc,a2aac6ee,6016bfa1,6e0a33df,c61d3dc7,5b1dee25",
    "cq-90568f0b1222": "3da6cced,5410cd5f",
    "cq-74e6b20d5d3d": "3da6cced,5410cd5f",
    "cq-5f44ce934aeb": "6c9b6c97,5410cd5f,1503cfb2,6e0a33df",
    "cq-2d26f131091e": "b3ed2621,23b6a0b3,ea366070",
    "cq-3a29e7dc3953": "b5ba666a,23b6a0b3,ea366070",
    "cq-59c5048d0891": "a6bded4f,2849c6d0,4e28d5a9,9408e135,a1bc558d,ce6625d5,6613107d,6e0a33df",
    "cq-592baba613f1": "6fad929e,804a6034,ed133cd5,51787fb3,6e0a33df,5b1dee25",
}
# RIDER B ships with its per-item record in the bundle reference (kickoff section 7).
BUNDLE_REFS: dict[str, str] = {
    "cq-59c5048d0891": ("code-15 [rider-b: 1,2,4 built; 3 rows2-4 PURGE row1 HELD; "
                        "5 A8 not-path-keyed; 6+7 proposal only; 8+purge APPLY=Harrison stop window]"),
    "cq-592baba613f1": "code-15 [rider-b R8: built; task REGISTRATION + Cowork Notion rewrite=Harrison]",
}


def _fn_ast(fn: Callable) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


def _call_name(node: ast.Call) -> str:
    f = node.func
    return f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")


def _calls(tree: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _call_name(n) == name]


def _load_script(name: str, rel: str):
    """Load a script module ONCE. An already-loaded module is reused, never replaced:
    replacing the sys.modules entry would detach whoever holds the first copy from
    anything keyed on that name (tests/conftest.py neutralises the purge script's
    real Drive factory BY this name -- a replaced entry left the purge tests' copy
    live-Drive-capable)."""
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, _REPO_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Token fixtures are ASSEMBLED (the pre-commit gate greps literal shapes).
def _asana_v2() -> str:
    return "2/" + "1234567890123456" + "/" + "6543210987654321" + ":" + "0123456789abcdef" * 2


def _slack() -> str:
    return "xo" + "xb-" + "123456789012-" + "1234567890123-" + "A1b2C3d4E5f6G7h8I9j0K1l2"


# -- code-presence preconditions (each returns a blocker string, or None) ----------
def _s1_present() -> str | None:
    from cora import secret_tokens as st, session_capture as sc, context_loader as cl
    from cora.knowledge_base.store import SearchResult
    tok_text = f"key {_asana_v2()} and {_slack()} end"
    out, n = st.redact_secret_tokens(tok_text)
    if n < 2 or _asana_v2() in out or _slack() in out or st.MARKER not in out:
        return "S1 redact_secret_tokens does not redact an Asana v2 PAT + a Slack token"
    sha = "0123456789abcdef0123456789abcdef01234567"
    if st.redact_secret_tokens(f"commit {sha}")[1] != 0:
        return "S1 a 40-hex git SHA is redacted (false positive)"
    prompt = sc._build_distill_prompt(tok_text, "FNDR", phi=False)
    if _asana_v2() in prompt or _slack() in prompt:
        return "S1 the session-capture distill prompt still carries a token"
    r = SearchResult(chunk_id="c-probe", source="gmail", source_id="s", entity="F3E",
                     title=f"t {_slack()}", content=tok_text, deep_link="", date_modified=None,
                     distance=0.1, author="", metadata=None)
    rendered = cl._format_kb_chunks([r]) or ""
    if _slack() in rendered or _asana_v2() in rendered:
        return "S1 the KB chunk renderer still serves a token (title or body)"
    lex = SearchResult(chunk_id="c-lex", source="drive_sweep", source_id="s", entity="LEX-LLC",
                       title="t", content=f"billing units {_slack()}", deep_link="",
                       date_modified=None, distance=0.1, author="", metadata=None)
    scrubbed = cl._apply_lex_phi_scrub([lex])
    if any(_slack()[-12:] in (x.content or "") for x in scrubbed):
        return "S1 the LEX non-custodian scrub runs before the token belt (tail survives)"
    pk = _load_script("purge_kb_code15_2026_09", "scripts/purge_kb_code15_2026-09.py")
    if "s1_tokens" not in pk.LANES or pk.LANES["s1_tokens"].redact is not st.redact_secret_tokens_strict:
        return "S1 the combined purge script has no s1_tokens lane on the STRICT redactor"
    return None


def _s2_present() -> str | None:
    from cora import gap_task_dedup as gd
    plan = gd.plan_create({"update_type": "asana_task", "payload": {},
                           "description": "[BDM] Drive doc suggests missing task: probe item"})
    if not str(plan.refusal).startswith("no_project:BDM"):
        return f"S2 a BDM gap is not refused for want of a project (refusal={plan.refusal!r})"
    a = "Monitor cash position and assess inflow stabilization"
    if not gd.tier_a(a, a + " ($33,487 as of 2026-08-27)"):
        return "S2 the figure/date drift family is not a tier-A duplicate"
    if gd.tier_a("Collect payment on SAS invoice #2077", "Collect payment on SAS invoice #2101"):
        return "S2 two distinct invoice ids collapse to one gap (false suppression)"
    import run_knowledge_review as rkr
    main_tree = _fn_ast(rkr.main)
    deferred = [n for n in ast.walk(main_tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "defer" for t in n.targets)
                and any(isinstance(c, ast.Constant) and c.value == "asana_task" for c in ast.walk(n.value))]
    if not deferred:
        return "S2 Step 1 does not defer an APPROVED asana_task until after the apply"
    return None


def _s3_present() -> str | None:
    from cora import delegated_work as dw
    default = dw.jobs_summary(limit=3)
    if any("requester" in row for row in default.get("recent", [])) or "counts_by_requester" in default:
        return "S3 the DEFAULT jobs_summary (the org-readable snapshot's view) carries requester data"
    full = dw.jobs_summary(limit=3, include_requester=True)
    if "counts_by_requester" not in full:
        return "S3 include_requester=True does not add the all-jobs per-requester counts"
    if any("requester" not in row for row in full.get("recent", [])):
        return "S3 include_requester=True rows lack the requester field"
    return None


def _s4_present() -> str | None:
    from cora import meeting_capture as mc
    cfg = mc.load_config()
    ev = {"id": "probe_20260918T170000Z", "summary": "weekly sync"}
    t = {"id": "t-probe", "title": "weekly sync"}
    is_breach, _, why = mc.classify_carved_recording((ev, "roster-user-declined"), None, t, cfg)
    if is_breach:
        return "S4 a roster-user-declined recording still alarms as a carve-out breach"
    is_breach, _, why = mc.classify_carved_recording((ev, "title-marker:[no-bot]"),
                                                     (ev, "title-marker:[no-bot]"), t, cfg)
    if not is_breach:
        return "S4 a [no-bot] recording is no longer a breach"
    is_breach, _, _ = mc.classify_carved_recording((ev, "future-reason:x"), None, t, cfg)
    if not is_breach:
        return "S4 an unknown veto reason does not fail closed to a breach"
    import run_meeting_capture_audit as rma
    if "carve_out_breach_event_ids" not in inspect.getsource(rma):
        return "S4 the audit ledger row does not carry carve_out_breach_event_ids"
    return None


def _module_fn_kwargs(module, fn_name: str) -> set[str]:
    """Keyword params of a function as DEFINED in the module source (never the bound
    attribute, which a caller or a test may have wrapped)."""
    tree = ast.parse(inspect.getsource(module))
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == fn_name:
            return {a.arg for a in n.args.args + n.args.kwonlyargs}
    return set()


def _s5_present() -> str | None:
    if "card_ts" not in _module_fn_kwargs(code_queue, "process_queue_action"):
        return "S5 process_queue_action takes no card_ts (per-card Keep idempotency missing)"
    from cora import app
    tree = _fn_ast(app._cq_ack_in_message)
    if not (_calls(tree, "_cq_rerender_rows") and _calls(tree, "is_menu_card")):
        return "S5 _cq_ack_in_message does not re-render menu rows (still thread-only)"
    saved = (code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER,
             code_queue._SYNC)
    tmpdir = Path(tempfile.mkdtemp(prefix="cq15-s5-"))
    try:
        code_queue._EVENT_LEDGER = tmpdir / "ledger.jsonl"
        code_queue._FINGERPRINT_LEDGER = tmpdir / "fp.jsonl"
        code_queue._SIGNALS_LEDGER = tmpdir / "sig.jsonl"
        code_queue._SYNC = True
        card_ts = f"{datetime.now(timezone.utc).timestamp() - 60:.6f}"
        cid = code_queue.seed_item(kind="bug", severity="P3", title="probe s5 rerender code-15",
                                   summary="throwaway", entity="F3E", signal="explicit",
                                   status="PROPOSED")
        if not cid:
            return "S5 probe could not seed a throwaway item"
        code_queue._append_event({"event": "dismissed", "ts": code_queue._now_iso(), "id": cid})
        blocks = [{"type": "section", "block_id": "sec_probe", "text": {"type": "mrkdwn", "text": "x"}},
                  {"type": "actions", "block_id": f"cq_proposed_{cid}",
                   "elements": [{"type": "button", "action_id": "probe", "value": cid,
                                 "text": {"type": "plain_text", "text": "Dismiss"}}]}]
        new, summary = code_queue.rerender_card_blocks(blocks, card_ts)
        if not any(b.get("type") == "context" and str(b.get("block_id", "")).startswith("cq_done_")
                   for b in new):
            return "S5 a decided row is not re-rendered to a cq_done_ context block"
    finally:
        (code_queue._EVENT_LEDGER, code_queue._FINGERPRINT_LEDGER, code_queue._SIGNALS_LEDGER,
         code_queue._SYNC) = saved
        shutil.rmtree(tmpdir, ignore_errors=True)
    return None


def _s6_present() -> str | None:
    body, unclosed = code_queue._strip_model_fences("```markdown\n## 1. Deliverables\nx\n```")
    if body.strip().startswith("```") or unclosed:
        return "S6 a closed markdown wrapper is not stripped"
    _, unclosed = code_queue._strip_model_fences("```markdown\n## 1. Deliverables\nx cut mid-")
    if not unclosed:
        return "S6 an unclosed wrapper is not flagged (truncation would hide)"
    line = code_queue._kickoff_status_line("button", datetime(2026, 9, 21, 15, 20, tzinfo=timezone.utc))
    if not (line.startswith("STATUS: STAGED ") and "via button" in line and "fire-owner: Harrison" in line
            and "fire-or-park: 2026-09-25T16:00:00-07:00" in line):
        return "S6 the STATUS line is not the deterministic shape"
    if "via" not in inspect.signature(code_queue.generate_kickoff_prompt).parameters:
        return "S6 generate_kickoff_prompt does not take via"
    return None


def _rider_b_present() -> str | None:
    from cora import kb_exclusions as kx
    for fid in ("16q7RfzibKms2rLvBKGIfaTSPBPUGYPaP", "1l7Hms6KwISUelnB-ItLAF9vms6K_Wd9s"):
        if fid not in kx.KB_EXCLUDED_FOLDER_IDS:
            return f"RIDER B folder {fid} is not KB-excluded"
    if fid in getattr(kx, "KB_EXCLUDED_WALK_ONLY_IDS", ()) or fid in getattr(kx, "KB_DASHBOARD_FOLDER_IDS", ()):
        return "RIDER B a new pin leaked into the walk-only / dashboard sets"
    if not kx.is_os_junk_filename("Desktop.ini") or kx.is_os_junk_filename("desktop.ini.bak"):
        return "RIDER B the desktop.ini belt is missing or over-matches"
    pk = _load_script("purge_kb_code15_2026_09", "scripts/purge_kb_code15_2026-09.py")
    want = {"rb2_personal_finances", "rb3_static_old_paths", "rb3_archived_nonmd",
            "rb4_desktop_ini", "rb8_ufl_equity"}
    if not want <= set(pk.LANES):
        return f"RIDER B purge lanes missing: {sorted(want - set(pk.LANES))}"
    if not (_REPO_ROOT / "scripts" / "propose_rider_b_holds_manifest.py").exists():
        return "RIDER B item 6 proposer is missing"
    return None


def _r8_present() -> str | None:
    import run_hygiene_drive_weekly as rh
    ps1 = _REPO_ROOT / "deployment" / "hygiene" / "folder-audit-inventory.ps1"
    if not ps1.exists():
        return "R8 the vendored inventory PS1 is missing"
    if hashlib.sha256(ps1.read_bytes()).hexdigest() != rh.INVENTORY_PS1_SHA256:
        return ("R8 the checked-out inventory PS1 does not match its pinned sha256 "
                "(a CRLF checkout? see .gitattributes -text) -- every Saturday would refuse")
    if not (_REPO_ROOT / "deployment" / "setup-hygiene-drive-weekly-task.ps1").exists():
        return "R8 the task setup script is missing"
    return None


def _gate(fn: Callable[[], str | None]) -> str | None:
    """Run one precondition; a RAISE is a blocker, never an abort."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return f"precondition raised {type(exc).__name__}: {_ascii(exc)}"


PRECONDITIONS: dict[str, Callable[[], str | None]] = {
    "cq-d9d0c92cc797": _s1_present,
    "cq-22b84598aee8": _s2_present,
    "cq-90568f0b1222": _s3_present,
    "cq-74e6b20d5d3d": _s3_present,
    "cq-5f44ce934aeb": _s4_present,
    "cq-2d26f131091e": _s5_present,
    "cq-3a29e7dc3953": _s6_present,
    "cq-59c5048d0891": _rider_b_present,
    "cq-592baba613f1": _r8_present,
}

SHIPPED: dict[str, str] = {
    "cq-d9d0c92cc797": "S1: token-shape redactor + harvest belts + combined purge (lane s1_tokens)",
    "cq-22b84598aee8": "S2: gap-executor real project + assignee or visible refusal; two-tier dedup",
    "cq-90568f0b1222": "S3: DW 9/10 diagnosis + confirm-shape pins",
    "cq-74e6b20d5d3d": "S3: requester on the founder-local cora_delegated_jobs surface",
    "cq-5f44ce934aeb": "S4: carve-out breach = no-record carve-out only; breach ids, titles never",
    "cq-2d26f131091e": "S5: Monday-menu rows re-render after a press; Keep idempotent per card",
    "cq-3a29e7dc3953": "S6: kickoff files carry a STATUS line + title H1; wrapper stripped; truncation marked",
    "cq-59c5048d0891": "RIDER B (KB): pins, belts, combined purge lanes, holds proposal (per-item record)",
    "cq-592baba613f1": "RIDER B (R8): weekly NO-HASH hygiene instrument + report",
}
NOT_TOUCHED: dict[str, str] = {
    "cq-3b3130dd3a4c": "S6b -- PROPOSED at fire (no approval), did NOT ride",
    "cq-11b694215709": "the 8/18 orphan fixture -- untouched by design (reducer behaviour pinned)",
    "cq-24b88a65a5ae": "rider (a) fixed item (2); items (1),(3),(4) open -- left PROPOSED",
    "cq-17fe76f5ab91": "Code #14's item, not this bundle's",
}

# (kind, severity, title, summary). Rider (b): the #13 Appendix-A LOW text VERBATIM.
_RB = "[code-15-rider] #13 D-051 Appendix-A LOW, seeded by the Code #15 consolidation rider (b). "
_FU = "[code-15] Code #15 follow-up. "
SEEDS: list[tuple[str, str, str, str]] = [
    ("bug", "LOW", "C13-04: gate-escalation tier 2 never notifies the owner",
     _RB + "[E-escalation-gates] scripts/nightly_health_check.py:970 -- The gate consumer's tier 2 adds no owner surface: repeat_signal's contract says the 2nd fire goes to 'the owner + ONE line in Harrison's daily briefing', but _gate_escalate only records owner_slack_id in the ledger and never notifies the owner, so for a non-Harrison owner tier 2 is the same #cora-health CRITICAL as tier 1 plus a Harrison-only line."),
    ("bug", "LOW", "C13-07: 'shopify' in _GENERIC_TOKENS kills the capability alias rows",
     _RB + "[A-egress-honesty] src/cora/capability_set.py:72 -- 'shopify' sits in _GENERIC_TOKENS, so the _TOKEN_ALIASES['shopify'] and _FAMILY_HINTS['shopify'] rows are dead and a Shopify denial never trips even where four f3e_shopify_* tools are offered -- contradicting the module docstring's 'a new klaviyo_* tool makes klaviyo a capability the day it ships' derivation claim."),
    ("bug", "LOW", "C13-11: toolname rail counts the founder DM as a non-developer surface",
     _RB + "[A-egress-honesty] src/cora/slack_egress.py:464 -- The toolname half treats the founder's own DM (channel_name='dm') as a non-developer surface, so a legitimate founder conversation about a tool by name ('what does cora_self_inventory do?') is a counted kind=toolname firing that keeps the third rail non-zero and, under enforce, redacts the symbol in Harrison's DM."),
    ("config", "LOW", "C13-12: catch-up replays qbo-token-refresh at a different RunLevel",
     _RB + "[B-catchup-runmarkers] data/maps/nightly-catchup-set.yaml:41 -- cowork-cora-qbo-token-refresh is registered RunLevel Highest in Task Scheduler while the catch-up lane runs RunLevel Limited, so enabling that candidate row replays the token refresh un-elevated -- a different security context from every scheduled run."),
    ("bug", "LOW", "C13-17: recap outside-attendee count assumes the addressee attended",
     _RB + "[C-calendar-meeting] src/cora/meeting_recap.py:642 -- `outside = max(attendees - len(recipients) - 1, 0)` assumes the card's addressee is one of the attendees, so when an external organizer is routed to Harrison (not an attendee) the 'N other attendee(s) are outside the workspace ... will receive nothing' disclosure is suppressed although an external IS on the list."),
    ("bug", "LOW", "C13-19: recap recipients include invitees who declined or never joined",
     _RB + "[C-calendar-meeting] src/cora/meeting_recap.py:246 -- Recipients are derived from `meeting_attendees` (calendar INVITEES) as well as `participants` (joiners), so an invitee who declined or never joined receives the recap DM reading 'because you were an attendee', and the card counts them as 'internal attendee(s)'."),
    ("bug", "LOW", "C13-20: an expired recap card keeps its live buttons",
     _RB + "[C-calendar-meeting] src/cora/app.py:4875 -- On the EXPIRED path `claim_for_tap` writes the terminal state and ledgers it but the handler only posts the refusal in-thread and never edits the card, so an expired card keeps its live buttons and the registered affordance line (the C4 contract) indefinitely."),
    ("bug", "LOW", "R1-04: mailbox sweep automated-subject rule drops roster humans",
     _RB + "[A-intake-roster] scripts/run_mailbox_intake_sweep.py:299 -- The automated-subject rule runs BEFORE roster resolution and applies to roster humans: a resolved roster sender's note whose subject starts with 'Reminder'/'Accepted'/'Declined'/'Invitation' or contains the word 'fireflies' anywhere is silently dropped (counted automated_subject), i.e. the path skips a roster human."),
    ("bug", "LOW", "R1-05: mailbox sweep 'NO size floor' docstring not honoured end to end",
     _RB + "[A-intake-roster] scripts/run_mailbox_intake_sweep.py:37 -- Docstring 'NO size floor: a one-line human note is the primary use case' is not honoured end to end: ingest applies info_intake._MIN_FACT_WORDS=6 and the '?'-anywhere interrogative screen to the WHOLE body[:4000] (quoted replies + signatures included; the gmail sweep's quote-strip is not reused), so short one-liners and any reply carrying a quoted question are dropped as not_a_contribution."),
    # -- bundle follow-ups (HIGH/MEDIUM only; LOW residuals are listed in the cascade report)
    ("feature", "HIGH", "Token redactor: add xapp / xoxe / Google OAuth shapes",
     _FU + "S1 shipped exactly four token shapes. The one live at-rest token chunk (LEX partition, drive_sweep) also carries a Slack app-level xapp token served unredacted at every renderer. Add xapp-, xoxe(.xoxp)-, ya29. and 1//0 shapes, bounded + validated on the S1 FP set and the live KB; extend the drift positives; re-run the combined purge dry-run; Harrison rotates the old credentials."),
    ("feature", "HIGH", "Live-.env credential values at rest in the KB (no token shape)",
     _FU + "The S1 recon found CURRENT credential values at rest that no shape catches (an API key in 2 chunks, two service passwords in 4 gmail chunks), by secrets_scan.live_secret_values substring equality. Add a REDACT lane to scripts/purge_kb_code15_2026-09.py reusing live_secret_values (never writing a value to the INTENT) + a matching egress belt; stage rotation with Harrison."),
    ("feature", "MEDIUM", "Wire the token+banking belt into the 7 chunk consumers it misses",
     _FU + "user_notes overlay, gap_autofill evidence (its answers become durable known-answers), coras_read, reconciliation_engine source_evidence, friction_mining, completion_detector digests and drive_extractor hand raw chunk text to an LLM or a person; neither belt covers them and most slice before any redaction. Apply secret_tokens.redact_chunk_egress before every slice; decide the token leg over deep_link."),
    ("feature", "MEDIUM", "Ingest-time token redaction at store.upsert_documents Step 0",
     _FU + "The s1_tokens REDACT is not durable: drive_sweep rewrites a row from its source when the file's modifiedTime changes. Redacting token shapes at the store's upsert Step 0 closes the re-ingest door for every connector; measure over the live corpus first."),
    ("config", "MEDIUM", "Regenerate the DR probe baseline after f816e27 (elevated)",
     _FU + "tests/test_dr_manifest_probes.py TestCommittedBaseline is red on main since f816e27: task-estate.json counts 99 (drive-extractor is ACL-hidden from non-elevated sessions -> a non-elevated probe sees 98) while dr-manifest.json D13 reads 96/96; RIDER B also moved D12 to 12 pins. Run scripts/dr_manifest_probes.py --update-docs from an ELEVATED shell after the hygiene task is registered, and commit the json + block."),
    ("feature", "MEDIUM", "Ruling + lane: whole-_archive KB purge scope, drive_asset cards, LEX rows",
     _FU + "RIDER B purged only _archive/dedup-2026-09 drive_sweep rows. Open: (a) the whole _archive subtree (~7.9k chunks incl. .md twins in _archive/_shared and a 389-chunk file directly under _archive); (b) the ~2k stale drive_asset cards under dedup-2026-09; (c) the LEX-partition rows held in both sets. Each becomes a lane in scripts/purge_kb_code15_2026-09.py once ruled."),
    ("feature", "MEDIUM", "Tombstone for purged Drive files (tree-walk re-ingest door)",
     _FU + "sweep_founders_os writes source=drive_sweep and has no UFL/FNDR watermark, so when the budget-interrupted walk first reaches 04-UFL it re-ingests the three purged UFL equity files (and any purged file under an un-watermarked entity). No per-file tombstone exists; options: a purge tombstone table consulted by both Drive doors, or a reviewed per-file exclusion."),
    ("config", "MEDIUM", "Dispose of the 32 open projectless gap-executor orphans",
     _FU + "As of 2026-09-24 the gap executor had created 48 Asana tasks with projects=[] and assignee=null; 32 still open (list in the Code #15 cascade report). S2 stops new orphans but builds no delete/complete verb. Harrison decides per family: move into the entity catch-all with the domain owner, or complete the tier-A duplicates -- a staged, dry-run-default script he runs."),
    ("bug", "MEDIUM", "Delegated worker ships a max_tokens-cut draft as complete",
     _FU + "delegated_worker._run_phase_b treats any non-tool_use stop as the final text, so a max_tokens cut at MAX_TOKENS_PER_TURN ships with partial=False. Both of Justin's doc_drafts (8/18, 9/10) ended on a 4096-token turn. Continue on max_tokens within the cost cap, or set partial=True with a disclosed reason; log stop_reason per turn (enum only)."),
    ("bug", "MEDIUM", "_delegate_work_intent does not force the tool on 'Cora, draft ...' asks",
     _FU + "A leading 'Cora' vocative + an imperative 'Draft ...' matches none of the forcing shapes, so both 9/10 asks reached the model unforced; ask #2 ended in a fabricated zero-tool 'Queued (dw-...)' ack. Strip a leading vocative before the intent regexes; test the gmail-draft and Asana-create collision shapes."),
    ("feature", "MEDIUM", "DW phantom-ack correction (ruling: now, or rely on the enforce flip)",
     _FU + "On 9/10 a zero-tool Haiku reply 'Queued (dw-...)' named an id in neither DW ledger; the phantom rail flagged it in OBSERVE mode and it went out unchanged, and the F-23 bare-affirmative guard has no 'Queued' shape. Option A: a DW claim regex with its own correction copy; option B: rely on the CORA_SENTINEL_ENFORCE flip. Needs Harrison's ruling."),
    ("bug", "MEDIUM", "Cross-surface card staleness (menu press / typed verb vs capture card)",
     _FU + "S5 re-renders the card a press was made on. A decision made elsewhere (a Monday-menu press, a typed founder-DM verb) never re-renders the item's own capture card, which keeps reading its old state. Keep the capture card ts on the fold and re-render through the shared locked renderer after any ledger decision (staged first; a Slack write outside an interaction)."),
    ("config", "MEDIUM", "Globally redirect FOUNDER_OS_ROOT and code_queue._NOTES_DIR in tests",
     _FU + "The kickoff generator writes to the real Founder-OS _notes (or the repo _notes fallback); the autouse conftest redirects neither, so only per-test qenv fixtures keep tests off Drive. One labelled conftest block pointing both at tmp closes the class."),
    ("bug", "MEDIUM", "Channel-health monitor writes the dated report even under --dry-run",
     _FU + "scripts/run_channel_health_monitor.py writes logs/channel-health-<date>.md unconditionally from run(); the tests' chm.run(dry_run=True) calls overwrote real reports in the primary (58 of 70 dated reports carry the fixture). Dry-run must not write (or take an out_dir); stage a content-keyed cleanup of the polluted live files for Harrison."),
    ("bug", "MEDIUM", "Folder-audit apply.ps1 has no pre-move keep/size check",
     _FU + "The folder-audit apply script checks only that the source exists and the dest does not; a stale ARCHIVE manifest could archive the last live copy (recoverable only via -Revert). The RIDER B proposer re-verifies size+mtime at proposal time only. A Founder-OS Drive edit (D-051 re-review): require the keep to exist and match bytes before Move-Item."),
]

_TERMINAL_NOT_SHIPPABLE = frozenset({"DISMISSED", "SUPERSEDED"})


def _ascii(s: object) -> str:
    return str(s).encode("ascii", "replace").decode("ascii")


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
        cid = code_queue.seed_item(kind="bug", severity="P2", title="probe-gate fixture code-15",
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
    ap.add_argument("--apply", action="store_true", help="Perform the transitions + seeds. Omitted = report only.")
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

    print(f"Step 7.5 -- Code #15 ({'APPLY' if args.apply else 'DRY-RUN'}) bundle_id={BUNDLE_ID} "
          f"branch={BRANCH} (commit = each item's own commits)\n")
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
        bid = BUNDLE_REFS.get(cq_id, BUNDLE_ID)
        commit = COMMITS[cq_id]
        if not args.apply:
            print(f"  [dry-run] would mark SHIPPED  {cq_id}  {label}  (status {status}; bundle={bid}; commit={commit})")
            continue
        try:
            outcome, msg = code_queue.process_queue_action(
                code_queue.ACTION_MARK_SHIPPED, cq_id, HARRISON_ID,
                bundle_id=bid, branch=BRANCH, commit=commit)
            if outcome == "shipped":
                print(f"  SHIPPED  {cq_id}  {label}  -> {_ascii(msg)}")
            else:
                print(f"  NOT SHIPPED  {cq_id}  {label}  -> {outcome}: {_ascii(msg)}")
                rc = 1
        except Exception as exc:  # noqa: BLE001 -- one bad id must not abort the rest
            print(f"  FAILED   {cq_id}  {label}  -> {type(exc).__name__}: {_ascii(exc)}")
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
                                       subsystem_guess="code-15")
            print(f"  SEEDED  {cid or 'REFUSED (PHI screen or invalid)'}  {sev:6}  {title}")
            if not cid:
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"  SEED FAILED  {title}  -> {type(exc).__name__}: {_ascii(exc)}")
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
