"""Cora AI-Visibility scan runner.

Fans the frozen prompt basket across models x runs_per_prompt, classifies each
answer (Haiku judge), verifies citations, merges the Google-AI-Overviews slice
(Otterly), scores each brand 0-100, and writes everything to data/ai_visibility.db.

Flags:
  --dry-run              plan calls + print a cost estimate; ZERO API calls
  --brand energy,pure    limit to specific brand(s)
  --models perplexity_sonar,claude_web   limit to specific model(s)
  --runs N               override runs_per_prompt
  --max-cost-usd X       HARD stop once grounded-search spend would exceed X
  --time-budget-min M    wall-clock self-budget (script-side; the real control)
  --country us           Otterly AIO country (default us)
  --no-aio               skip the Otterly AIO pull
  --no-verify-citations  skip HEAD-checking cited URLs (faster; keeps all)
  --fresh                start a new scan instead of resuming a running one
  --db-path PATH         override the SQLite path (tests)
  --channel NAME         (slice 8) Slack channel for the score card
  --no-post              (slice 8) do not post the score card

Exit codes: 0 = completed, 1 = fatal, 2 = partial (budget/cost cut it short).

PHI guard OFF: F3 marketing/visibility data only, no health data.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
load_dotenv(_REPO_ROOT / ".env", override=True)  # D-021

from cora.ai_visibility import citations as cites  # noqa: E402
from cora.ai_visibility import classifier as clf  # noqa: E402
from cora.ai_visibility import prompts as pb  # noqa: E402
from cora.ai_visibility import scorer as scoring  # noqa: E402
from cora.ai_visibility import store  # noqa: E402
from cora.connectors import ai_search  # noqa: E402
from cora.connectors import otterly_client  # noqa: E402

log = logging.getLogger("ai_visibility_scan")

_STATE_PATH = _REPO_ROOT / "data" / "state" / "ai-visibility-scan-state.json"
_RESUME_STALE_HOURS = 24


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def _selected_brands(basket: pb.Basket, brand_filter: str) -> list[str]:
    if not brand_filter:
        return list(basket.brand_keys())
    wanted = [b.strip() for b in brand_filter.split(",") if b.strip()]
    unknown = [b for b in wanted if b not in basket.brands]
    if unknown:
        raise SystemExit(f"unknown brand(s) {unknown}; known: {list(basket.brand_keys())}")
    return wanted


def _selected_models(basket: pb.Basket, model_filter: str) -> list[str]:
    models = list(basket.models)
    if not model_filter:
        return models
    wanted = [m.strip() for m in model_filter.split(",") if m.strip()]
    unknown = [m for m in wanted if m not in ai_search.SUPPORTED_MODELS]
    if unknown:
        raise SystemExit(f"unknown model(s) {unknown}; supported: {sorted(ai_search.SUPPORTED_MODELS)}")
    return wanted


def build_work_items(basket: pb.Basket, brands: list[str], models: list[str]) -> list[tuple]:
    """Return (brand_key, Prompt, model) triples (runs handled by the loop)."""
    items: list[tuple] = []
    for bkey in brands:
        for p in basket.brand(bkey).prompts:
            for m in models:
                items.append((bkey, p, m))
    return items


def estimate_total_cost(items: list[tuple], runs: int) -> tuple[float, dict[str, float]]:
    per_model: dict[str, float] = {}
    for _bkey, _p, model in items:
        per_model[model] = per_model.get(model, 0.0) + ai_search.estimate_call_cost(model) * runs
    return round(sum(per_model.values()), 4), {k: round(v, 4) for k, v in per_model.items()}


# ---------------------------------------------------------------------------
# Resume state (atomic write)
# ---------------------------------------------------------------------------
def _load_state() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(_STATE_PATH)


def _clear_state() -> None:
    try:
        _STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _work_key(brand: str, prompt_id: str, model: str, run_index: int) -> str:
    return f"{brand}|{prompt_id}|{model}|{run_index}"


def _resume_target(fresh: bool) -> tuple[int | None, set[str]]:
    """Return (scan_id_to_resume, done_keys) or (None, set()) for a fresh scan."""
    if fresh:
        return None, set()
    state = _load_state()
    sid = state.get("scan_id")
    if not sid:
        return None, set()
    scan = store.get_scan(int(sid))
    if not scan or scan.get("status") != "running":
        return None, set()
    try:
        started = datetime.fromisoformat(scan["started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - started).total_seconds() / 3600.0
        if age_h > _RESUME_STALE_HOURS:
            return None, set()
    except Exception:  # noqa: BLE001
        return None, set()
    log.info("resuming scan %d (%d work items already done)", sid, len(state.get("done_keys", [])))
    return int(sid), set(state.get("done_keys", []))


# ---------------------------------------------------------------------------
# Concurrent fan-out (bounded in-flight window)
# ---------------------------------------------------------------------------
def _run_units(units, args, basket, scan_id, done, state_started, *,
               query_fn, classify_fn, resolve_fn):
    """Run query->classify->verify for each unit concurrently, storing results
    in the main thread. Bounded window of `workers` in-flight calls; per-provider
    caps are enforced by ai_search's semaphores.

    Cost cap: exact hard stop at --workers 1; with concurrency it may overshoot
    by up to ~(workers x per-call cost) -- negligible since real cost ~= estimate.
    Time budget self-bounds mid-run (the real control; task limit is the backstop).
    """
    deadline = time.monotonic() + args.time_budget_min * 60.0
    workers = max(1, getattr(args, "workers", 16))
    total_cost = 0.0
    total_calls = 0
    partial = False
    skipped_models: set[str] = set()
    units_iter = iter(units)

    def work(unit):
        bkey, prompt, model, _ri = unit
        res = query_fn(model, prompt.text)
        if res.skipped or res.error:
            return unit, res, None, None
        brand = basket.brand(bkey)
        cls = classify_fn(brand, prompt, res.text, res.citations)
        verified = resolve_fn(cls.cited_sources, competitor_names=brand.competitor_set,
                              verify=not args.no_verify_citations)
        return unit, res, cls, verified

    with ThreadPoolExecutor(max_workers=workers) as pool:
        inflight: dict = {}
        stop = False

        def try_submit() -> bool:
            nonlocal stop, partial
            while not stop:
                if time.monotonic() > deadline:
                    log.warning("time budget %.0fmin exhausted; stopping (partial)",
                                args.time_budget_min)
                    partial = True
                    stop = True
                    return False
                unit = next(units_iter, None)
                if unit is None:
                    return False
                if unit[2] in skipped_models:
                    continue  # model has no key / errored-out; skip its remaining units
                if args.max_cost_usd and total_cost + ai_search.estimate_call_cost(unit[2]) > args.max_cost_usd:
                    log.warning("cost cap $%.2f reached (spent $%.4f); stopping (partial)",
                                args.max_cost_usd, total_cost)
                    partial = True
                    stop = True
                    return False
                inflight[pool.submit(work, unit)] = unit
                return True
            return False

        for _ in range(workers):
            if not try_submit():
                break

        while inflight:
            completed, _pending = wait(list(inflight), return_when=FIRST_COMPLETED)
            for fut in completed:
                unit = inflight.pop(fut)
                bkey, prompt, model, ri = unit
                key = _work_key(bkey, prompt.id, model, ri)
                total_calls += 1
                try:
                    _u, res, cls, verified = fut.result()
                except Exception as exc:  # noqa: BLE001 -- a crashed unit must not abort the scan
                    log.warning("work unit %s crashed: %s", key, exc)
                    continue
                if res.skipped:
                    log.warning("model %s skipped (%s); dropping its remaining units",
                                model, res.error)
                    skipped_models.add(model)
                    continue
                total_cost += res.cost_usd
                if res.error:
                    store.insert_answer(scan_id=scan_id, brand=bkey, prompt_id=prompt.id,
                                        intent=prompt.intent, aided=prompt.aided, model=model,
                                        run_index=ri, raw_text="",
                                        classification=clf.Classification(),
                                        cost_usd=res.cost_usd, error=res.error)
                else:
                    answer_id = store.insert_answer(
                        scan_id=scan_id, brand=bkey, prompt_id=prompt.id, intent=prompt.intent,
                        aided=prompt.aided, model=model, run_index=ri, raw_text=res.text,
                        classification=cls, cost_usd=res.cost_usd, error=cls.error)
                    store.record_answer_mentions(scan_id=scan_id, answer_id=answer_id, brand=bkey,
                                                 brand_name=basket.brand(bkey).brand_name,
                                                 model=model, classification=cls)
                    store.insert_citations(scan_id=scan_id, answer_id=answer_id, brand=bkey,
                                           model=model, citations=verified)
                done.add(key)
                _save_state({"scan_id": scan_id, "started_at": state_started,
                             "done_keys": sorted(done)})
                if args.max_cost_usd and total_cost >= args.max_cost_usd:
                    partial = True
                    stop = True
                if time.monotonic() > deadline:
                    partial = True
                    stop = True
            for _ in range(len(completed)):
                if not try_submit():
                    break
    return total_calls, total_cost, partial


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------
def execute_scan(args, *, query_fn=None, classify_fn=None, resolve_fn=None, aio_fn=None) -> int:
    query_fn = query_fn or ai_search.run_query
    classify_fn = classify_fn or clf.classify_answer
    resolve_fn = resolve_fn or cites.resolve_citations
    aio_fn = aio_fn or otterly_client.fetch_aio_slice

    if args.db_path:
        store.set_db_path(args.db_path)

    basket = pb.load_basket()
    brands = _selected_brands(basket, args.brand)
    models = _selected_models(basket, args.models)
    runs = args.runs if args.runs and args.runs > 0 else basket.runs_per_prompt
    items = build_work_items(basket, brands, models)

    # ---- dry run ----
    if args.dry_run:
        total_cost, per_model = estimate_total_cost(items, runs)
        planned_calls = len(items) * runs
        print("=== AI-Visibility scan DRY RUN ===")
        print(f"basket v{basket.version}: {basket.total_prompts()} prompts")
        print(f"brands: {brands}")
        print(f"models: {models}  x  runs_per_prompt: {runs}")
        print(f"planned grounded calls: {planned_calls}")
        for m in models:
            key_present = _model_key_present(m)
            print(f"  {m}: ~${per_model.get(m, 0.0):.2f}  "
                  f"({'key present' if key_present else 'NO KEY -> will skip'})")
        aio_on = (not args.no_aio) and bool(otterly_client._key())
        print(f"AIO (Otterly): {'enabled' if aio_on else 'disabled/no key'} "
              f"(a few report calls, not per-prompt)")
        print(f"estimated grounded-search cost: ~${total_cost:.2f} "
              f"(cap --max-cost-usd={args.max_cost_usd})")
        print("ZERO API calls made.")
        return 0

    # ---- real run ----
    resume_sid, done = _resume_target(args.fresh)
    if resume_sid is not None:
        scan_id = resume_sid
    else:
        scan_id = store.create_scan(basket_version=basket.version, models=models,
                                    runs_per_prompt=runs, brands=brands)
    state_started = datetime.now(timezone.utc).isoformat()
    _save_state({"scan_id": scan_id, "started_at": state_started, "done_keys": sorted(done)})

    # Work units = every (brand, prompt, model, run) not already done. The
    # per-provider concurrency caps live in ai_search's semaphores; the runner
    # fans these out concurrently (bounded window) so a full weekly scan fits
    # its time budget instead of running ~1780 calls serially.
    units = [(bkey, prompt, model, ri)
             for (bkey, prompt, model) in items
             for ri in range(runs)
             if _work_key(bkey, prompt.id, model, ri) not in done]

    total_calls, total_cost, partial = _run_units(
        units, args, basket, scan_id, done, state_started,
        query_fn=query_fn, classify_fn=classify_fn, resolve_fn=resolve_fn)

    # ---- AIO pull (Otterly) + scoring ----
    aio_by_brand = _pull_aio(brands, basket, args, aio_fn, scan_id) if not partial else {}
    for bkey in brands:
        _score_and_save(scan_id, bkey, basket, aio_by_brand.get(bkey))

    status = "partial" if partial else "completed"
    store.finish_scan(scan_id, status=status, total_calls=total_calls,
                      total_cost_usd=total_cost, aio_included=bool(aio_by_brand))
    if not partial:
        _clear_state()
    log.info("scan %d %s: %d calls, $%.4f grounded spend, aio=%s",
             scan_id, status, total_calls, total_cost, bool(aio_by_brand))
    print(f"scan {scan_id} {status}: {total_calls} calls, ${total_cost:.4f} grounded spend")
    # slice 8 wires the Slack card post here.
    _maybe_post_card(scan_id, args)
    return 2 if partial else 0


def _model_key_present(model: str) -> bool:
    import os
    return {
        "perplexity_sonar": bool(os.environ.get("PERPLEXITY_API_KEY")),
        "openai_web_search": bool(os.environ.get("OPENAI_API_KEY")),
        "gemini_grounding": bool(os.environ.get("GEMINI_API_KEY")),
        "claude_web": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }.get(model, False)


def _pull_aio(brands, basket, args, aio_fn, scan_id) -> dict:
    if args.no_aio or not otterly_client._key():
        return {}
    end = date.today()
    start = end - timedelta(days=7)
    out: dict = {}
    for bkey in brands:
        brand = basket.brand(bkey)
        slc = aio_fn(bkey, brand.aliases, start_date=start.isoformat(),
                     end_date=end.isoformat(), country=args.country)
        if not getattr(slc, "available", False):
            log.warning("AIO unavailable for %s: %s", bkey, getattr(slc, "error", "?"))
            continue
        store.record_aio_slice(scan_id=scan_id, brand=bkey, brand_name=brand.brand_name,
                               model=otterly_client.ENGINE_TAG,
                               competitor_mentions=slc.competitor_mentions,
                               citations=_tag_aio_citations(slc, brand))
        out[bkey] = scoring.aio_metrics(presence=slc.presence,
                                        share_of_voice=slc.share_of_voice,
                                        average_rank=slc.average_rank,
                                        sentiment=slc.sentiment)
    return out


def _tag_aio_citations(slc, brand) -> list:
    """Re-tag Otterly citation URLs with our canonical source_type (uniform)."""
    urls = [c.url for c in slc.citations]
    return cites.resolve_citations(urls, competitor_names=brand.competitor_set, verify=False)


def _score_and_save(scan_id: int, bkey: str, basket: pb.Basket, aio) -> None:
    rows = store.answers_for_scan(scan_id, bkey)
    comp_counts = store.competitor_counts_by_answer(scan_id, bkey)
    verdicts = [scoring.RunVerdict(
        model=r["model"], intent=r["intent"], aided=bool(r["aided"]),
        is_hit=bool(r["mentioned"] and r["is_correct_brand"]),
        position=r["position"], sentiment=r["sentiment"] or "neutral",
        competitor_count=comp_counts.get(r["id"], 0),
    ) for r in rows]
    score = scoring.score_brand(verdicts, aio=aio)
    score.brand = bkey
    store.save_score(scan_id, score)


def _maybe_post_card(scan_id: int, args) -> None:
    """Slice 8 fills this in (Slack score card). No-op until then / when --no-post."""
    if getattr(args, "no_post", False):
        return
    try:
        from cora.ai_visibility import report  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- report/card lands in slice 8
        return
    poster = getattr(report, "post_scorecards", None)
    if poster is None:
        return
    try:
        channel = getattr(args, "channel", "") or None
        poster(scan_id, channel=channel)
    except Exception as exc:  # noqa: BLE001 -- posting must never fail the scan
        log.warning("score-card post failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--brand", default="", help="comma-separated brand keys (energy,pure,mood)")
    ap.add_argument("--models", default="", help="comma-separated model ids")
    ap.add_argument("--runs", type=int, default=0, help="override runs_per_prompt")
    ap.add_argument("--max-cost-usd", type=float, default=200.0, help="hard grounded-search spend cap")
    ap.add_argument("--time-budget-min", type=float, default=100.0,
                    help="script-side wall-clock budget (real control; task limit is the backstop)")
    ap.add_argument("--workers", type=int, default=16,
                    help="concurrent in-flight calls (per-provider caps still apply); 1 = serial")
    ap.add_argument("--country", default="us", help="Otterly AIO country code (default us)")
    ap.add_argument("--no-aio", action="store_true", help="skip the Otterly AIO pull")
    ap.add_argument("--no-verify-citations", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="ignore any resumable running scan")
    ap.add_argument("--db-path", default="", help="override SQLite path (tests)")
    ap.add_argument("--channel", default="", help="Slack channel for the score card (slice 8)")
    ap.add_argument("--no-post", action="store_true", help="do not post the score card")
    ap.add_argument("--log-level", default="INFO")
    return ap.parse_args(argv)


def _setup_logging(level: str) -> None:
    (_REPO_ROOT / "logs").mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(_REPO_ROOT / "logs" / f"ai-visibility-scan-{today}.log",
                                encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    _setup_logging(args.log_level)
    try:
        return execute_scan(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("ai-visibility scan fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
