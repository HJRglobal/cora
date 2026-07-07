"""Tests for the AI-visibility SQLite store (tmp DB roundtrip)."""

from __future__ import annotations

import pytest

from cora.ai_visibility import store as st
from cora.ai_visibility.citations import Citation
from cora.ai_visibility.classifier import Classification
from cora.ai_visibility.scorer import BrandScore


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    st.set_db_path(tmp_path / "ai_visibility.db")
    yield
    st.set_db_path(None)


def _hit_classification():
    return Classification(mentioned=True, is_correct_brand=True, position=1,
                          sentiment="positive", competitors_mentioned=["Celsius", "Ghost"],
                          cited_sources=["https://f3energy.com"])


def _miss_classification(competitors):
    return Classification(mentioned=False, is_correct_brand=False, position=None,
                          sentiment="neutral", competitors_mentioned=competitors,
                          cited_sources=[])


def test_create_and_finish_scan():
    sid = st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=5,
                         brands=["energy"])
    scan = st.get_scan(sid)
    assert scan["status"] == "running"
    st.finish_scan(sid, status="completed", total_calls=10, total_cost_usd=1.23,
                   aio_included=True)
    scan = st.get_scan(sid)
    assert scan["status"] == "completed"
    assert scan["total_calls"] == 10
    assert scan["aio_included"] == 1
    assert scan["finished_at"] is not None


def test_insert_answer_and_mentions_roundtrip():
    sid = st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=1,
                         brands=["energy"])
    aid = st.insert_answer(scan_id=sid, brand="energy", prompt_id="ENG-D01",
                           intent="discovery", aided=False, model="perplexity_sonar",
                           run_index=0, raw_text="F3 Energy tops the list.",
                           classification=_hit_classification(), cost_usd=0.01)
    st.record_answer_mentions(scan_id=sid, answer_id=aid, brand="energy",
                              brand_name="F3 Energy", model="perplexity_sonar",
                              classification=_hit_classification())
    rows = st.answers_for_scan(sid, "energy")
    assert len(rows) == 1
    assert rows[0]["mentioned"] == 1 and rows[0]["is_correct_brand"] == 1
    assert rows[0]["num_competitors"] == 2
    counts = st.competitor_counts_by_answer(sid, "energy")
    assert counts[aid] == 2  # Celsius + Ghost


def test_insert_citations_and_aio_slice():
    sid = st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=1,
                         brands=["energy"])
    aid = st.insert_answer(scan_id=sid, brand="energy", prompt_id="ENG-D01",
                           intent="discovery", aided=False, model="perplexity_sonar",
                           run_index=0, raw_text="x", classification=_hit_classification(),
                           cost_usd=0.0)
    n = st.insert_citations(scan_id=sid, answer_id=aid, brand="energy", model="perplexity_sonar",
                            citations=[Citation("https://f3energy.com", "f3energy.com", True, "own_site")])
    assert n == 1
    st.record_aio_slice(scan_id=sid, brand="energy", brand_name="F3 Energy", model="aio_otterly",
                        competitor_mentions={"Celsius": 15},
                        citations=[Citation("https://reddit.com/x", "reddit.com", True, "reddit")])
    # AIO citation is stored with model aio_otterly and no answer_id
    import sqlite3
    conn = sqlite3.connect(str(st._db_path()))
    try:
        aio_cites = conn.execute(
            "SELECT * FROM citations WHERE model='aio_otterly'").fetchall()
        aio_ment = conn.execute(
            "SELECT * FROM mentions WHERE model='aio_otterly'").fetchall()
    finally:
        conn.close()
    assert len(aio_cites) == 1 and aio_cites[0][2] is None  # answer_id NULL
    assert len(aio_ment) == 1


def test_save_score_and_wow_delta():
    # scan 1 (completed) with composite 50
    s1 = st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=1,
                        brands=["energy"])
    st.save_score(s1, BrandScore(brand="energy", composite=50.0, composite_direct_only=50.0,
                                 presence=50, share_of_voice=40, position=33, sentiment=60))
    st.finish_scan(s1, status="completed", total_calls=1, total_cost_usd=0.0, aio_included=False)

    # scan 2 with composite 62 -> WoW +12 vs scan 1
    s2 = st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=1,
                        brands=["energy"])
    st.save_score(s2, BrandScore(brand="energy", composite=62.0, composite_direct_only=62.0,
                                 presence=62, share_of_voice=45, position=40, sentiment=70))
    st.finish_scan(s2, status="completed", total_calls=1, total_cost_usd=0.0, aio_included=False)

    latest = st.latest_scores()
    assert "energy" in latest
    assert latest["energy"]["composite"] == 62.0
    assert latest["energy"]["prev_composite"] == 50.0
    assert latest["energy"]["wow_delta"] == 12.0


def test_first_scan_has_no_wow_baseline():
    sid = st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=1,
                         brands=["energy"])
    st.save_score(sid, BrandScore(brand="energy", composite=40.0, composite_direct_only=40.0))
    st.finish_scan(sid, status="completed", total_calls=1, total_cost_usd=0.0, aio_included=False)
    latest = st.latest_scores()
    assert latest["energy"]["wow_delta"] is None
    assert latest["energy"]["prev_composite"] is None


def test_top_competitor_gaps():
    sid = st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=1,
                         brands=["energy"])
    # prompt where brand missed but 3 competitors named
    a1 = st.insert_answer(scan_id=sid, brand="energy", prompt_id="ENG-D02", intent="discovery",
                          aided=False, model="perplexity_sonar", run_index=0, raw_text="x",
                          classification=_miss_classification(["Celsius", "Ghost", "Monster"]),
                          cost_usd=0.0)
    st.record_answer_mentions(scan_id=sid, answer_id=a1, brand="energy", brand_name="F3 Energy",
                              model="perplexity_sonar",
                              classification=_miss_classification(["Celsius", "Ghost", "Monster"]))
    # prompt where brand hit -> not a gap
    a2 = st.insert_answer(scan_id=sid, brand="energy", prompt_id="ENG-D01", intent="discovery",
                          aided=False, model="perplexity_sonar", run_index=0, raw_text="x",
                          classification=_hit_classification(), cost_usd=0.0)
    st.record_answer_mentions(scan_id=sid, answer_id=a2, brand="energy", brand_name="F3 Energy",
                              model="perplexity_sonar", classification=_hit_classification())
    gaps = st.top_competitor_gaps(sid, "energy", limit=3)
    assert gaps and gaps[0]["prompt_id"] == "ENG-D02"
    assert gaps[0]["competitor_pressure"] == 3
    assert all(g["prompt_id"] != "ENG-D01" for g in gaps)  # hit prompt excluded


def test_latest_scores_empty_when_no_completed_scan():
    st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=1,
                   brands=["energy"])  # running, not completed
    assert st.latest_scores() == {}
