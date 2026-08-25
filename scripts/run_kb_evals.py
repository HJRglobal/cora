#!/usr/bin/env python3
"""Golden-set eval harness v1 (WS-3, flywheel-reliability build 2026-07-01).

Two layers over data/evals/golden-set.yaml + golden-set-auto.yaml:

  L1 (default, no LLM, minutes): per CONTENT case, does expect_substring
     surface in what the production pipeline would actually assemble --
     the live KB retrieval block (same thresholds, same code path via
     context_loader.load_context_parts) OR the entity's static context?
     GUARD cases run deterministic canon checks (D-064 financials,
     cross-entity firewall, PHI detectors) with zero API calls.
  L2 (--answers, bounded LLM spend): full pipeline question -> response via
     claude_client.generate_response; assert must_contain / must_not_contain /
     must_refuse. Capped at --max-llm-cases per run.

ISOLATION (hard requirement, lens R3): eval runs are side-effect-free.
  - CORA_EVAL_MODE=1 is set before any cora import: tools_for_entity returns
    [] and tool_dispatch.dispatch refuses, so no staged write / connector call
    can execute; gap_detection never logs eval traffic.
  - This script never imports app.py, never touches the semantic cache, never
    writes any ledger. KB access is search-only.
  - The ONLY Slack egress is the optional --slack summary post (sanitized),
    used by the weekly task.

Newly-failing detection: the previous run's failing ids live in
data/state/kb-evals-last.json; the report highlights regressions.

Scheduled as: "Cora - KB Evals"  Weekly Monday 09:05 AZ (before the 09:30
weekly health metrics; unique clock minute per the stagger doctrine).
Register with: deployment\\setup-kb-evals-task.ps1 (elevated PowerShell).

Usage:
    .venv\\Scripts\\python.exe scripts\\run_kb_evals.py [--answers]
        [--max-llm-cases N] [--only-id ID] [--json]
        [--slack] [--channel cora-health]

Exit codes: 0 = ran (pass-rate reported), 1 = harness error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
# R3 isolation: set AFTER load_dotenv (override=True would clobber it if a
# stray CORA_EVAL_MODE line ever landed in .env -- adversarial review LOW) and
# BEFORE any cora import, so every gate that reads it at call time
# (tools_for_entity, dispatch, gap_detection) is armed for this process.
os.environ["CORA_EVAL_MODE"] = "1"
sys.path.insert(0, str(_REPO_ROOT / "src"))

_EVALS_DIR = _REPO_ROOT / "data" / "evals"
_STATE_PATH = _REPO_ROOT / "data" / "state" / "kb-evals-last.json"
_DEFAULT_MAX_LLM_CASES = 15

# Refusal shapes for must_refuse assertions: the prompt-enforced deflection
# inventory (shared with gap_detection) plus generic refusal phrasings.
_REFUSAL_EXTRA = (
    "i'm not able to", "i am not able to", "i can't help with",
    "i cannot help with", "stays in the ehr", "not able to discuss",
    "can't discuss", "cannot discuss", "don't have access to",
    "outside what i can help with", "needs harrison", "ask in",
)


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def load_cases(only_id: str | None = None,
               load_errors: list[str] | None = None) -> list[dict]:
    """Merge the hand + auto corpora. A parse failure of the HAND file is a
    harness ERROR (recorded in load_errors), never a silent shrink -- a broken
    golden-set.yaml must not produce a green '1/1 passed' weekly post while
    the whole guard canon is skipped (adversarial review MEDIUM)."""
    import yaml
    cases: list[dict] = []
    for name in ("golden-set.yaml", "golden-set-auto.yaml"):
        path = _EVALS_DIR / name
        if not path.exists():
            if name == "golden-set.yaml" and load_errors is not None:
                load_errors.append(f"{name} is MISSING")
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] could not parse {name}: {exc}", file=sys.stderr)
            if name == "golden-set.yaml" and load_errors is not None:
                load_errors.append(f"{name} failed to parse: {exc}")
            continue
        for case in data.get("cases") or []:
            if isinstance(case, dict) and case.get("id") and case.get("question"):
                case["_file"] = name
                cases.append(case)
    # De-dup by id (hand file wins over auto on collision).
    seen: set[str] = set()
    unique = []
    for c in cases:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        unique.append(c)
    if only_id:
        unique = [c for c in unique if c["id"] == only_id]
    return unique


# ---------------------------------------------------------------------------
# Guard layer (deterministic, no LLM, no KB)
# ---------------------------------------------------------------------------

def run_guard_case(case: dict) -> tuple[bool, str]:
    kind = case.get("guard")
    question = case.get("question") or ""
    expect = (case.get("expect") or "").lower()
    if kind == "financials":
        from cora.user_access import _financials_is_blocked
        blocked = bool(_financials_is_blocked(question.lower()))
        want = expect == "blocked"
        return blocked == want, f"financials blocked={blocked} expected={expect}"
    if kind == "cross_entity":
        from cora import cross_entity_guard
        redirect = cross_entity_guard.check_cross_entity(
            question, case.get("channel_entity") or "F3E")
        blocked = redirect is not None
        want = expect == "blocked"
        return blocked == want, f"cross_entity blocked={blocked} expected={expect}"
    if kind in ("phi", "clinical_phi", "lex_billing_status",
                "non_lex_phi_backstop", "non_lex_phi_backstop_live"):
        from cora import phi_guard
        # non_lex_phi_backstop pins the STRICT store-defense predicate (drive/dossier egress);
        # non_lex_phi_backstop_live pins the LIVE-retrieval variant (context_loader) that is
        # tuned NOT to over-refuse OSN/F3E product copy or aggregate finance. Both guard the
        # D-059-class regression the green suite has repeatedly missed.
        if kind == "clinical_phi":
            flagged = phi_guard.is_clinical_phi(question)
        elif kind == "lex_billing_status":
            flagged = phi_guard.is_lex_billing_status_phi(question)
        elif kind == "non_lex_phi_backstop":
            flagged = phi_guard.non_lex_phi_backstop_trips(question)
        elif kind == "non_lex_phi_backstop_live":
            flagged = phi_guard.non_lex_phi_backstop_trips_live(question)
        else:
            flagged = phi_guard.is_phi_risk(question)
        want = expect == "flagged"
        return flagged == want, f"{kind} flagged={flagged} expected={expect}"
    return False, f"unknown guard kind: {kind!r}"


# ---------------------------------------------------------------------------
# L1 content layer (live retrieval + static context, no LLM)
# ---------------------------------------------------------------------------

def run_l1_case(case: dict) -> tuple[bool, str]:
    from cora.context_loader import load_context_parts
    entity = (case.get("entity") or "FNDR").strip().upper()
    question = str(case["question"])
    # str() coercion: an unquoted numeric YAML scalar (expect_substring: 2880)
    # must not crash the whole run (adversarial review LOW).
    expect = _norm(str(case.get("expect_substring") or ""))
    if not expect:
        return True, "no expect_substring (L2-only case) -- skipped at L1"
    try:
        static_text, kb_text = load_context_parts(entity, query=question)
    except Exception as exc:  # noqa: BLE001
        return False, f"context assembly failed: {exc}"
    if expect in _norm(kb_text):
        return True, "satisfied_by=kb"
    if expect in _norm(static_text):
        return True, "satisfied_by=static"
    return False, "expected content absent from retrieval AND static context"


# ---------------------------------------------------------------------------
# L2 answer layer (full pipeline, tools disabled, bounded spend)
# ---------------------------------------------------------------------------

def _is_refusal(reply: str) -> bool:
    from cora.gap_detection import is_deflection
    if is_deflection(reply):
        return True
    norm = _norm(reply)
    return any(p in norm for p in _REFUSAL_EXTRA)


def run_l2_case(case: dict) -> tuple[bool, str]:
    from cora.context_loader import load_context_parts
    from cora.prompt_loader import load_prompt
    from cora.claude_client import generate_response

    entity = (case.get("entity") or "FNDR").strip().upper()
    question = case["question"]
    layer2 = case.get("layer2") or {}
    try:
        static_text, kb_text = load_context_parts(entity, query=question)
        prompt = load_prompt(entity)
        runtime = (
            "## Runtime channel context\n\n"
            f"This channel (#cora-evals) has these properties:\n"
            f"- Entity: {entity}\n- Function: build\n"
            f"- Financial-access tier: TIER_1\n\n"
            "**The person asking this question is: Eval Harness** "
            "(Slack ID: unknown).\n\n---\n\n"
        )
        reply = generate_response(
            prompt, runtime + kb_text, question,
            slack_user_id="", entity=entity,
            channel_name="cora-evals", cached_context=static_text,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"pipeline error: {exc}"

    norm_reply = _norm(reply)
    if layer2.get("must_refuse"):
        ok = _is_refusal(reply)
        return ok, ("refused as required" if ok
                    else f"expected refusal, got: {reply[:160]!r}")
    for needle in layer2.get("must_contain") or []:
        if _norm(str(needle)) not in norm_reply:
            return False, f"missing required content {needle!r}: {reply[:160]!r}"
    for needle in layer2.get("must_not_contain") or []:
        if _norm(str(needle)) in norm_reply:
            return False, f"contains forbidden content {needle!r}"
    return True, "answer assertions passed"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_all(cases: list[dict], *, answers: bool,
            max_llm_cases: int) -> list[dict]:
    results = []
    llm_budget = max_llm_cases
    for case in cases:
        cid = case["id"]
        t0 = time.monotonic()
        if case.get("guard"):
            ok, detail = run_guard_case(case)
            layer = "guard"
        else:
            ok, detail = run_l1_case(case)
            layer = "L1"
            if (answers and (case.get("layer2") or {}) and llm_budget > 0):
                llm_budget -= 1
                ok2, detail2 = run_l2_case(case)
                # An L2-only case (must_refuse, no expect_substring) is judged
                # by L2 alone; a case with both must pass both.
                if case.get("expect_substring"):
                    ok = ok and ok2
                    detail = f"{detail} | L2: {detail2}"
                else:
                    ok = ok2
                    detail = f"L2: {detail2}"
                layer = "L1+L2" if case.get("expect_substring") else "L2"
            elif (case.get("layer2") or {}) and not case.get("expect_substring"):
                # L2-only case in an L1 run: not evaluated.
                results.append({"id": cid, "layer": "L2-only", "ok": None,
                                "detail": "requires --answers", "ms": 0})
                continue
        results.append({"id": cid, "layer": layer, "ok": ok, "detail": detail,
                        "ms": int((time.monotonic() - t0) * 1000)})
    return results


# C1 (cq-da2d6772f0ec): a case failing this many consecutive runs auto-seeds a
# queue item. Two is the threshold because one red week is noise -- a canon edit
# mid-week, a transient retrieval miss -- and by the third the evidence is that
# nobody is watching. f3e-pure-wholesale-ladder went red on 8/10, 8/17 and 8/24
# with no seed and no owner, and its actual cause (a literal anchor left behind
# by the 8/04 Pure MSRP rewrite) sat in plain sight the whole time.
_AUTOSEED_AFTER_CONSECUTIVE = 2


def _load_state() -> tuple[set[str], dict[str, int], dict[str, str]]:
    """(failing_ids, streaks, seeded) -- back-compatible with the old one-key file.

    A >=2-consecutive rule needs no new state at all (`failing_ids &
    prev_failing` IS the 2-in-a-row set), but a STREAK COUNT does, and so does
    "seed once per incident". Both are additive keys; an old file simply reads
    as empty and the first run rebuilds them.
    """
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return (set(data.get("failing_ids") or []),
                dict(data.get("streaks") or {}),
                dict(data.get("seeded") or {}))
    except Exception:
        return set(), {}, {}


def _load_last_failing() -> set[str]:
    return _load_state()[0]


def _save_last_failing(failing: set[str], streaks: dict | None = None,
                       seeded: dict | None = None) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "failing_ids": sorted(failing),
            "streaks": dict(streaks or {}),
            "seeded": dict(seeded or {}),
            "ts": datetime.now(timezone.utc).isoformat(),
        }, indent=1), encoding="utf-8")
        tmp.replace(_STATE_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] could not persist eval state: {exc}", file=sys.stderr)


def _bump_streaks(failing: set[str], prev_streaks: dict) -> dict[str, int]:
    """Consecutive-run counts. A case that passes drops out entirely, so a
    later regression starts a fresh streak rather than resuming an old one."""
    return {cid: int(prev_streaks.get(cid, 0)) + 1 for cid in sorted(failing)}


def _autoseed_persistent_failures(streaks: dict, seeded: dict,
                                  cases_by_id: dict) -> dict[str, str]:
    """Seed one queue item per case that has failed >= the threshold.

    Returns the updated `seeded` map (case_id -> streak-start marker), so a case
    is seeded ONCE PER INCIDENT rather than once per run OR once ever.

    THE DEDUP TRAP this avoids: code_queue.find_fingerprint is sha1(signal +
    normalized title) with NO time component, and it never checks item status --
    so a title keyed only on the eval id would seed exactly once in the lifetime
    of the repo, and a regression six months after the first item shipped would
    be silently swallowed. The signal therefore carries the streak identity.

    Seeded at PROPOSED, never APPROVED: seed_item warns (and the nightly check
    flags) on a P0/P1 APPROVED seed with no kickoff, and an automated seeder must
    not be creating owed taps.
    """
    out = dict(seeded)
    for cid, streak in sorted(streaks.items()):
        if streak < _AUTOSEED_AFTER_CONSECUTIVE:
            continue
        marker = f"streak{streak - _AUTOSEED_AFTER_CONSECUTIVE + 1}"
        # Already seeded for THIS incident -- the marker only changes when the
        # case passes and later regresses (streaks reset on a pass).
        if out.get(cid):
            continue
        case = cases_by_id.get(cid) or {}
        entity = str(case.get("entity") or "FNDR")
        question = str(case.get("question") or "")
        try:
            from cora import code_queue
            cq_id = code_queue.seed_item(
                kind="bug",
                severity="MEDIUM",
                title=f"KB eval {cid} failing {streak} consecutive weekly runs",
                summary=(
                    f"The golden-set case `{cid}` ({entity}) has failed "
                    f"{streak} consecutive KB-eval runs. Question: "
                    f"{question[:180]}. Either the retrieval regressed or the "
                    f"case's expected text no longer matches canon -- check "
                    f"BOTH before assuming a retrieval fault, since a canon "
                    f"rewrite silently staled this exact case's literal anchor "
                    f"for three weeks in August 2026."),
                entity=entity,
                signal=f"kb_eval_persistent_failure:{cid}:{marker}",
                status="PROPOSED",
                subsystem_guess="kb-evals",
            )
        except Exception as exc:  # noqa: BLE001 -- a seed must never fail the run
            print(f"[warn] eval auto-seed failed for {cid}: {exc}", file=sys.stderr)
            continue
        if cq_id:
            out[cid] = marker
            print(f"[seed] {cid} failing {streak}x -> {cq_id}")
    # A case that is no longer failing loses its seed marker, so a future
    # regression is a NEW incident and seeds again.
    return {k: v for k, v in out.items() if k in streaks}


def summarize(results: list[dict], prev_failing: set[str],
              load_errors: list[str] | None = None) -> dict:
    evaluated = [r for r in results if r["ok"] is not None]
    failed = [r for r in evaluated if not r["ok"]]
    passed = len(evaluated) - len(failed)
    failing_ids = {r["id"] for r in failed}
    # A case SKIPPED this run (L2-only in an L1 run) keeps its previous failing
    # status -- otherwise a scheduled non---answers run would announce a
    # manually-recorded L2 failure as "fixed" (adversarial review LOW).
    skipped_ids = {r["id"] for r in results if r["ok"] is None}
    carried = prev_failing & skipped_ids
    failing_ids |= carried
    return {
        "total_cases": len(results),
        "evaluated": len(evaluated),
        "passed": passed,
        "failed": len(failed),
        "pass_rate_pct": round(100 * passed / len(evaluated), 1) if evaluated else 0.0,
        "newly_failing": sorted((failing_ids - carried) - prev_failing),
        "fixed_since_last": sorted(prev_failing - failing_ids - skipped_ids),
        "failing_ids": sorted(failing_ids),
        "still_failing_unevaluated": sorted(carried),
        "skipped_l2_only": len(skipped_ids),
        "load_errors": list(load_errors or []),
    }


def format_slack_summary(summary: dict) -> str:
    if summary.get("load_errors"):
        return (":rotating_light: *KB evals -- corpus load ERROR:* "
                + "; ".join(summary["load_errors"])
                + f"\nOnly {summary['evaluated']} case(s) ran -- treat this "
                  "run as invalid until the corpus parses.")
    icon = ":white_check_mark:" if not summary["failed"] else ":warning:"
    lines = [
        f"{icon} *KB evals* -- {summary['passed']}/{summary['evaluated']} passed "
        f"({summary['pass_rate_pct']}%)"
        + (f", {summary['skipped_l2_only']} L2-only case(s) skipped"
           if summary.get("skipped_l2_only") else ""),
    ]
    if summary["newly_failing"]:
        lines.append("*Newly failing:* " + ", ".join(summary["newly_failing"][:10]))
    if summary.get("still_failing_unevaluated"):
        lines.append("Still failing (not re-run, needs --answers): "
                     + ", ".join(summary["still_failing_unevaluated"][:10]))
    if summary["fixed_since_last"]:
        lines.append("Fixed since last run: "
                     + ", ".join(summary["fixed_since_last"][:10]))
    # Was gated `if failed and not newly_failing`, so in any week with BOTH a new
    # failure and an older one, the older ids vanished from the post entirely --
    # the digest would report the new break and silently stop mentioning the
    # standing one. List the repeats explicitly instead, and say how long.
    repeats = sorted(set(summary["failing_ids"]) - set(summary["newly_failing"])
                     - set(summary.get("still_failing_unevaluated") or []))
    if repeats:
        streaks = summary.get("streaks") or {}
        rendered = ", ".join(
            f"{cid} ({streaks[cid]}w)" if streaks.get(cid, 0) > 1 else cid
            for cid in repeats[:10])
        lines.append("Still failing: " + rendered)
    return "\n".join(lines)


def _post_slack(message: str, channel: str) -> bool:
    """The runner's ONLY Slack egress -- the weekly summary post (sanitized)."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        print("[warn] SLACK_BOT_TOKEN not set -- not posting", file=sys.stderr)
        return False
    try:
        import httpx
        from cora.slack_egress import sanitize_text
        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"channel": channel, "text": sanitize_text(message),
                  "unfurl_links": False, "unfurl_media": False},
            timeout=15,
        )
        return bool(resp.json().get("ok"))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Slack post failed: {exc}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Golden-set KB evals.")
    ap.add_argument("--answers", action="store_true",
                    help="Also run L2 full-pipeline answer assertions (LLM spend).")
    ap.add_argument("--max-llm-cases", type=int, default=_DEFAULT_MAX_LLM_CASES,
                    help=f"L2 case cap per run (default {_DEFAULT_MAX_LLM_CASES}).")
    ap.add_argument("--only-id", default=None, help="Run a single case by id.")
    ap.add_argument("--json", action="store_true", help="Emit JSON results.")
    ap.add_argument("--slack", action="store_true",
                    help="Post the summary to Slack (weekly task uses this).")
    ap.add_argument("--channel", default="cora-health")
    ap.add_argument("--no-autoseed", action="store_true",
                    help="Do not seed a queue item for a persistently-failing "
                         "case (for local/manual runs).")
    args = ap.parse_args()

    load_errors: list[str] = []
    cases = load_cases(args.only_id, load_errors=load_errors)
    if not cases and not load_errors:
        print("No eval cases found.", file=sys.stderr)
        return 1

    results = run_all(cases, answers=args.answers,
                      max_llm_cases=args.max_llm_cases)
    prev_failing, prev_streaks, prev_seeded = _load_state()
    summary = summarize(results, prev_failing, load_errors=load_errors)
    if not args.only_id and not load_errors:
        failing = set(summary["failing_ids"])
        streaks = _bump_streaks(failing, prev_streaks)
        summary["streaks"] = streaks
        # A load error invalidates the whole run, and --only-id never persists
        # (both already gate the save) -- so a broken corpus can never manufacture
        # a streak or a seed.
        seeded = (_autoseed_persistent_failures(
                      streaks, prev_seeded, {c.get("id"): c for c in cases})
                  if not args.no_autoseed else dict(prev_seeded))
        _save_last_failing(failing, streaks, seeded)
    else:
        summary["streaks"] = dict(prev_streaks)

    if args.json:
        print(json.dumps({"summary": summary, "results": results},
                         indent=1, default=str))
    else:
        for r in results:
            mark = {True: "PASS", False: "FAIL", None: "SKIP"}[r["ok"]]
            print(f"[{mark}] {r['id']:<40} ({r['layer']}, {r['ms']}ms) {r['detail']}")
        print(f"\n{summary['passed']}/{summary['evaluated']} passed "
              f"({summary['pass_rate_pct']}%)"
              f" | newly failing: {len(summary['newly_failing'])}"
              f" | L2-only skipped: {summary['skipped_l2_only']}")

    if args.slack:
        ok = _post_slack(format_slack_summary(summary), args.channel)
        print(f"[slack] posted to #{args.channel}: {ok}")

    # A corpus load error is a harness failure -- nonzero so the scheduled
    # task's Last Result flags it even if the Slack post also went out.
    return 1 if load_errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        sys.exit(1)
