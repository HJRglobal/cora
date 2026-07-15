"""Tests for scripts/run_ai_visibility_scan.py (all deps injected/mocked)."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


def _load_runner():
    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    try:
        import run_ai_visibility_scan as m
        return m
    except ImportError:  # pragma: no cover
        pytest.skip("run_ai_visibility_scan not importable")


from cora.ai_visibility import store as st
from cora.ai_visibility.classifier import Classification
from cora.ai_visibility.citations import Citation
from cora.connectors.ai_search import QueryResult
from cora.connectors.otterly_client import AioBrandSlice


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    st.set_db_path(tmp_path / "ai_visibility.db")
    yield
    st.set_db_path(None)


def _args(m, **over):
    a = m.parse_args(["--brand", "energy", "--models", "perplexity_sonar", "--runs", "1",
                      "--no-aio", "--no-verify-citations", "--no-post"])
    for k, v in over.items():
        setattr(a, k, v)
    return a


def _args_serial(m, **over):
    over.setdefault("workers", 1)
    return _args(m, **over)


# --- dry run ---
def test_dry_run_makes_zero_calls(monkeypatch, capsys):
    m = _load_runner()
    monkeypatch.setattr(m.ai_search, "run_query",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no calls in dry-run")))
    rc = m.execute_scan(m.parse_args(["--dry-run"]))
    out = capsys.readouterr().out
    assert rc == 0
    assert "ZERO API calls" in out
    assert "101 prompts" in out  # 89 frozen F3 + 12 hjr (4-brand total)


# --- planning ---
def test_build_work_items_and_cost():
    m = _load_runner()
    import cora.ai_visibility.prompts as pb
    basket = pb.load_basket()
    items = m.build_work_items(basket, ["energy"], ["perplexity_sonar", "claude_web"])
    assert len(items) == 33 * 2  # energy has 33 prompts x 2 models
    total, per_model = m.estimate_total_cost(items, runs=5)
    assert total > 0
    assert set(per_model) == {"perplexity_sonar", "claude_web"}


# --- full small scan (hit/miss) ---
def test_full_scan_scores_and_completes(monkeypatch):
    m = _load_runner()

    def fake_query(model, prompt):
        # F3 Energy hits on discovery, misses elsewhere -> nonzero composite
        return QueryResult(model=model, prompt=prompt, text="F3 Energy is great.",
                           citations=["https://f3energy.com"], input_tokens=10,
                           output_tokens=40, num_searches=1, cost_usd=0.01)

    def fake_classify(brand, prompt, text, citations):
        hit = prompt.intent == "discovery"
        return Classification(mentioned=hit, is_correct_brand=hit,
                              position=1 if hit else None,
                              sentiment="positive" if hit else "neutral",
                              competitors_mentioned=["Celsius"], cited_sources=citations)

    def fake_resolve(urls, **k):
        return [Citation(u, "f3energy.com", True, "own_site") for u in urls]

    rc = m.execute_scan(_args(m), query_fn=fake_query, classify_fn=fake_classify,
                        resolve_fn=fake_resolve)
    assert rc == 0
    latest = st.latest_scores()
    assert "energy" in latest
    assert 0 < latest["energy"]["composite"] <= 100
    assert latest["energy"]["scan"]["status"] == "completed"
    # answers were stored for all 33 energy prompts
    rows = st.answers_for_scan(latest["energy"]["scan"]["id"], "energy")
    assert len(rows) == 33


# --- 4th brand (Harrison Rogers) flows through identically ---
def test_hjr_brand_scores_and_completes(monkeypatch):
    m = _load_runner()

    def fake_query(model, prompt):
        return QueryResult(model=model, prompt=prompt,
                           text="Harrison Rogers, the HJR Global CEO and F3 Energy founder.",
                           citations=["https://hjrglobal.com"], input_tokens=10,
                           output_tokens=40, num_searches=1, cost_usd=0.01)

    def fake_classify(brand, prompt, text, citations):
        # correct-brand hit on branded prompts, miss elsewhere -> nonzero composite
        hit = prompt.intent == "branded"
        return Classification(mentioned=hit, is_correct_brand=hit,
                              position=1 if hit else None,
                              sentiment="positive" if hit else "neutral",
                              competitors_mentioned=["Alex Hormozi"], cited_sources=citations)

    args = m.parse_args(["--brand", "hjr", "--models", "perplexity_sonar", "--runs", "1",
                         "--no-aio", "--no-verify-citations", "--no-post"])
    rc = m.execute_scan(args, query_fn=fake_query, classify_fn=fake_classify,
                        resolve_fn=lambda urls, **k: [])
    assert rc == 0
    latest = st.latest_scores()
    assert "hjr" in latest
    assert 0 < latest["hjr"]["composite"] <= 100
    assert latest["hjr"]["scan"]["status"] == "completed"
    # all 12 hjr prompts stored under brand='hjr'
    rows = st.answers_for_scan(latest["hjr"]["scan"]["id"], "hjr")
    assert len(rows) == 12
    assert all(r["brand"] == "hjr" for r in rows)


# --- cost cap hard stop ---
def test_cost_cap_stops_and_marks_partial(monkeypatch):
    m = _load_runner()

    def dear_query(model, prompt):
        return QueryResult(model=model, prompt=prompt, text="x", citations=[],
                           cost_usd=0.5)

    rc = m.execute_scan(_args_serial(m, max_cost_usd=1.0),  # workers=1 -> exact hard cap
                        query_fn=dear_query,
                        classify_fn=lambda *a, **k: Classification(),
                        resolve_fn=lambda urls, **k: [])
    assert rc == 2  # partial
    # 2 calls at $0.5 -> the 3rd would exceed $1.0, so it stops at 2
    conn = sqlite3.connect(str(st._db_path()))
    try:
        scan = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert scan[7] == "partial"  # status column
    assert scan[8] == 2          # total_calls column


# --- missing model key -> skipped, scan still completes ---
def test_missing_key_model_skipped(monkeypatch):
    m = _load_runner()

    def skip_query(model, prompt):
        return QueryResult(model=model, prompt=prompt, skipped=True, error="no key")

    rc = m.execute_scan(_args(m), query_fn=skip_query,
                        classify_fn=lambda *a, **k: Classification(),
                        resolve_fn=lambda urls, **k: [])
    assert rc == 0  # scan still completes
    # all models skipped -> no successful answers -> brand OMITTED (no false 0/100)
    assert st.latest_scores() == {}
    conn = sqlite3.connect(str(st._db_path()))
    try:
        scan = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        n_ans = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    finally:
        conn.close()
    assert scan[7] == "completed"
    assert n_ans == 0  # nothing stored for a fully-skipped model


# --- resume state helpers ---
def test_state_roundtrip_and_resume(monkeypatch, tmp_path):
    m = _load_runner()
    monkeypatch.setattr(m, "_STATE_PATH", tmp_path / "state.json")
    m._save_state({"scan_id": 1, "started_at": "x", "done_keys": ["a", "b"]})
    assert m._load_state()["done_keys"] == ["a", "b"]
    m._clear_state()
    assert m._load_state() == {}


def test_resume_target_running_scan(monkeypatch, tmp_path):
    m = _load_runner()
    monkeypatch.setattr(m, "_STATE_PATH", tmp_path / "state.json")
    sid = st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=1,
                         brands=["energy"])  # status running
    m._save_state({"scan_id": sid, "started_at": "x", "done_keys": ["energy|ENG-D01|perplexity_sonar|0"]})
    resume_sid, done = m._resume_target(fresh=False)
    assert resume_sid == sid
    assert "energy|ENG-D01|perplexity_sonar|0" in done
    # fresh=True ignores the resumable scan
    assert m._resume_target(fresh=True) == (None, set())


def test_resume_skips_completed_scan(monkeypatch, tmp_path):
    m = _load_runner()
    monkeypatch.setattr(m, "_STATE_PATH", tmp_path / "state.json")
    sid = st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=1,
                         brands=["energy"])
    st.finish_scan(sid, status="completed", total_calls=1, total_cost_usd=0.0, aio_included=False)
    m._save_state({"scan_id": sid, "started_at": "x", "done_keys": []})
    assert m._resume_target(fresh=False) == (None, set())


def test_unknown_brand_raises_systemexit():
    m = _load_runner()
    with pytest.raises(SystemExit):
        m.execute_scan(m.parse_args(["--brand", "nope", "--dry-run"]))


def test_aio_path_merges_fifth_engine(monkeypatch):
    """End-to-end AIO path: _pull_aio -> scoring.aio_metrics -> merged composite."""
    m = _load_runner()
    monkeypatch.setenv("OTTERLY_API_KEY", "otk-test")  # else _pull_aio short-circuits

    def fake_query(model, prompt):
        return QueryResult(model=model, prompt=prompt, text="F3 Energy.", citations=[],
                           cost_usd=0.001)

    def fake_classify(brand, prompt, text, citations):
        return Classification(mentioned=True, is_correct_brand=True, position=1,
                              sentiment="positive")

    def fake_aio(bkey, aliases, *, start_date, end_date, country="us", workspace_id=None,
                 engine=None):
        return AioBrandSlice(brand_key=bkey, report_id="r", report_title="t", available=True,
                             presence=40.0, share_of_voice=30.0, average_rank=2.0,
                             sentiment={"positive": 3, "neutral": 1, "negative": 0},
                             competitor_mentions={"Celsius": 10}, citations=[])

    args = m.parse_args(["--brand", "energy", "--models", "perplexity_sonar", "--runs", "1",
                         "--no-verify-citations", "--no-post"])  # NOTE: AIO left ON
    rc = m.execute_scan(args, query_fn=fake_query, classify_fn=fake_classify,
                        resolve_fn=lambda urls, **k: [], aio_fn=fake_aio)
    assert rc == 0
    latest = st.latest_scores()["energy"]
    assert latest["aio_composite"] is not None          # AIO merged in
    assert latest["composite"] != latest["composite_direct_only"]  # 5th engine shifted it
    assert latest["scan"]["aio_included"] == 1


def test_person_brand_excludes_aio_from_headline(monkeypatch):
    """hjr (person brand): the un-disambiguatable Otterly AIO leg must NOT be merged
    into the headline; hjr is scored on the 4 direct (Haiku-judged) engines only."""
    m = _load_runner()
    monkeypatch.setenv("OTTERLY_API_KEY", "otk-test")  # else _pull_aio short-circuits entirely
    aio_calls = []

    def fake_query(model, prompt):
        return QueryResult(model=model, prompt=prompt, text="Harrison Rogers.", citations=[],
                           cost_usd=0.001)

    def fake_classify(brand, prompt, text, citations):
        return Classification(mentioned=True, is_correct_brand=True, position=1,
                              sentiment="positive")

    def fake_aio(bkey, aliases, *, start_date, end_date, country="us", workspace_id=None,
                 engine=None):
        aio_calls.append(bkey)  # MUST NOT be called for a person brand
        return AioBrandSlice(brand_key=bkey, report_id="r", report_title="t", available=True,
                             presence=90.0, share_of_voice=90.0, average_rank=1.0,
                             sentiment={"positive": 5, "neutral": 0, "negative": 0},
                             competitor_mentions={}, citations=[])

    args = m.parse_args(["--brand", "hjr", "--models", "perplexity_sonar", "--runs", "1",
                         "--no-verify-citations", "--no-post"])  # AIO left ON
    rc = m.execute_scan(args, query_fn=fake_query, classify_fn=fake_classify,
                        resolve_fn=lambda urls, **k: [], aio_fn=fake_aio)
    assert rc == 0
    latest = st.latest_scores()["hjr"]
    assert latest["aio_composite"] is None                      # AIO NOT merged for the person
    assert latest["composite"] == latest["composite_direct_only"]  # direct engines only
    assert aio_calls == []                                       # Otterly not even fetched for hjr
    assert latest["scan"]["aio_included"] == 0


def test_disambiguation_not_counted_at_integration(monkeypatch):
    """A namesake (mentioned=True, is_correct_brand=False) reaches the DB but
    _score_and_save's is_hit derivation keeps presence at 0."""
    m = _load_runner()

    def fake_query(model, prompt):
        return QueryResult(model=model, prompt=prompt, text="F3 Nation is a workout group.",
                           citations=[], cost_usd=0.001)

    def namesake_classify(brand, prompt, text, citations):
        return Classification(mentioned=True, is_correct_brand=False, position=None,
                              sentiment="neutral")

    rc = m.execute_scan(_args(m), query_fn=fake_query, classify_fn=namesake_classify,
                        resolve_fn=lambda urls, **k: [])
    assert rc == 0
    latest = st.latest_scores()["energy"]
    assert latest["presence"] == 0.0   # namesake NOT counted as presence
    assert latest["composite"] == 0.0
    # ... but the rows were genuinely stored with mentioned=1
    rows = st.answers_for_scan(latest["scan"]["id"], "energy")
    assert all(r["mentioned"] == 1 and r["is_correct_brand"] == 0 for r in rows)


def test_cost_cap_presubmit_guard_stops(monkeypatch):
    """Exercises the pre-submit estimate guard (not the post-completion >=): a cap
    just above one call's spend stops before the 2nd call is submitted."""
    m = _load_runner()

    def q(model, prompt):
        return QueryResult(model=model, prompt=prompt, text="x", citations=[], cost_usd=0.5)

    rc = m.execute_scan(_args_serial(m, max_cost_usd=0.505), query_fn=q,
                        classify_fn=lambda *a, **k: Classification(),
                        resolve_fn=lambda urls, **k: [])
    assert rc == 2
    conn = sqlite3.connect(str(st._db_path()))
    try:
        scan = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert scan[8] == 1  # only 1 call: pre-submit guard blocked the 2nd (0.5 + est > 0.505)
    assert scan[7] == "partial"


def test_concurrent_scan_stores_all_units(monkeypatch):
    """With workers>1 (default), every unit is still processed + stored exactly once."""
    m = _load_runner()
    seen = []

    def fake_query(model, prompt):
        seen.append(prompt)
        return QueryResult(model=model, prompt=prompt, text="F3 Energy.",
                           citations=[], cost_usd=0.001)

    rc = m.execute_scan(_args(m, workers=8, runs=2),  # 33 prompts x 2 runs = 66 units
                        query_fn=fake_query,
                        classify_fn=lambda b, p, t, c: Classification(
                            mentioned=True, is_correct_brand=True, position=1,
                            sentiment="positive"),
                        resolve_fn=lambda urls, **k: [])
    assert rc == 0
    latest = st.latest_scores()
    rows = st.answers_for_scan(latest["energy"]["scan"]["id"], "energy")
    assert len(rows) == 66  # every unit stored once, no dupes despite concurrency
    assert len(seen) == 66
