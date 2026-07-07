"""Tests for the AI-visibility scorer math (pure; no I/O)."""

from __future__ import annotations

import pytest

from cora.ai_visibility import scorer as sc
from cora.ai_visibility.scorer import RunVerdict


def _rv(model, *, hit, pos=None, sent="neutral", comp=0, intent="discovery", aided=False):
    return RunVerdict(model=model, intent=intent, aided=aided, is_hit=hit,
                      position=pos, sentiment=sent, competitor_count=comp)


# --- primitives ---
def test_position_value_ladder():
    assert sc.position_value(True, 1) == 100.0
    assert sc.position_value(True, 2) == 66.0
    assert sc.position_value(True, 3) == 33.0
    assert sc.position_value(True, 4) == 15.0
    assert sc.position_value(True, 9) == 15.0
    assert sc.position_value(True, None) == 33.0  # present-but-unranked
    assert sc.position_value(False, None) == 0.0
    assert sc.position_value(False, 1) == 0.0  # not a hit -> absent


def test_sentiment_value():
    assert sc.sentiment_value("positive") == 100.0
    assert sc.sentiment_value("neutral") == 60.0
    assert sc.sentiment_value("mixed") == 40.0
    assert sc.sentiment_value("negative") == 0.0
    assert sc.sentiment_value("garbage") == 60.0  # unknown -> neutral


def test_composite_weights_sum_to_one():
    assert abs(sum(sc.WEIGHTS.values()) - 1.0) < 1e-9
    assert sc.composite_of(100, 100, 100, 100) == 100.0
    assert sc.composite_of(0, 0, 0, 0) == 0.0


# --- engine components ---
def test_engine_components_known_scenario():
    runs = [
        _rv("m", hit=True, pos=1, sent="positive", comp=1),
        _rv("m", hit=True, pos=1, sent="positive", comp=0),
        _rv("m", hit=True, pos=2, sent="neutral", comp=2),
        _rv("m", hit=False, comp=1),
        _rv("m", hit=False, comp=1),
    ]
    e = sc._engine_components(runs)
    assert e.presence == 60.0               # 3/5
    assert e.share_of_voice == 37.5         # 3/(3+5)
    assert e.position == 53.2               # (100+100+66+0+0)/5
    assert e.sentiment == 86.67             # (100+100+60)/3
    assert e.composite == pytest.approx(57.01, abs=0.05)
    assert e.n_runs == 5


def test_engine_components_no_hits_zero_sentiment():
    runs = [_rv("m", hit=False, comp=2), _rv("m", hit=False, comp=1)]
    e = sc._engine_components(runs)
    assert e.presence == 0.0
    assert e.share_of_voice == 0.0
    assert e.sentiment == 0.0  # no hits -> sentiment component 0


# --- score_brand across engines ---
def test_two_equal_engines_pooling_equals_mean():
    runs_a = [_rv("a", hit=True, pos=1, sent="positive", comp=0),
              _rv("a", hit=False, comp=1)]
    runs_b = [_rv("b", hit=True, pos=1, sent="positive", comp=0),
              _rv("b", hit=False, comp=1)]
    s = sc.score_brand(runs_a + runs_b)
    assert s.engines == ["a", "b"]
    assert s.presence == 50.0               # each engine 50 -> mean 50 == pooled 2/4
    assert s.composite == s.composite_direct_only


def test_differing_n_uses_per_engine_mean_not_pooled():
    # engine a: 5 runs, 3 hits -> presence 60; engine b: 2 runs, 2 hits -> 100
    runs_a = [_rv("a", hit=True) for _ in range(3)] + [_rv("a", hit=False) for _ in range(2)]
    runs_b = [_rv("b", hit=True) for _ in range(2)]
    s = sc.score_brand(runs_a + runs_b)
    assert s.presence == 80.0               # (60+100)/2, NOT pooled 5/7=71.4


def test_unaided_presence_and_per_intent():
    runs = [
        _rv("m", hit=True, intent="discovery", aided=False),
        _rv("m", hit=False, intent="discovery", aided=False),
        _rv("m", hit=True, intent="branded", aided=True),
    ]
    s = sc.score_brand(runs)
    assert s.unaided_presence == 50.0        # 1/2 unaided runs hit
    assert s.per_intent["discovery"] == 50.0
    assert s.per_intent["branded"] == 100.0


def test_empty_verdicts_all_zero():
    s = sc.score_brand([])
    assert s.composite == 0.0 and s.presence == 0.0 and s.engines == []


# --- AIO merge ---
def test_aio_metrics_from_primitives():
    m = sc.aio_metrics(presence=25, share_of_voice=10, average_rank=3,
                       sentiment={"positive": 4, "neutral": 3, "negative": 1})
    assert m is not None
    assert m.presence == 25.0
    assert m.share_of_voice == 10.0
    # position is presence-scaled to match the direct engines' basis: ladder(3)=33 * 0.25
    assert m.position == 8.25
    assert m.sentiment == 72.5               # (4*100+3*60+1*0)/8
    # composite = .4*25 + .25*10 + .2*8.25 + .15*72.5
    assert m.composite == pytest.approx(25.03, abs=0.05)


def test_cross_engine_sentiment_excludes_no_hit_engines():
    # brand hit on engine 'a' (all positive), absent on 'b'. Sentiment must be
    # 100 (a's hits only), NOT (100 + 0)/2 = 50 (b contributes no sentiment vote).
    runs = [_rv("a", hit=True, pos=1, sent="positive"),
            _rv("b", hit=False)]
    s = sc.score_brand(runs)
    assert s.sentiment == 100.0
    # sanity: presence still reflects b's absence (per-engine mean of 100 and 0)
    assert s.presence == 50.0


def test_cross_engine_sentiment_zero_when_no_hits_anywhere():
    s = sc.score_brand([_rv("a", hit=False), _rv("b", hit=False)])
    assert s.sentiment == 0.0


def test_aio_metrics_none_when_no_presence():
    assert sc.aio_metrics(presence=None) is None


def test_aio_sentiment_from_nss_scales():
    assert sc._nss_to_100(0.5) == 75.0       # -1..1 scale
    assert sc._nss_to_100(40) == 70.0        # -100..100 scale


def test_score_brand_merges_aio_as_fifth_engine():
    runs = [_rv("m", hit=True, pos=1, sent="positive", comp=0)]  # single direct engine
    s_direct = sc.score_brand(runs)
    aio = sc.AioMetrics(presence=0, share_of_voice=0, position=0, sentiment=0, composite=0.0)
    s = sc.score_brand(runs, aio=aio)
    # merged = (1*direct + aio)/2 with aio composite 0
    assert s.composite == pytest.approx(s_direct.composite / 2, abs=0.02)
    assert s.composite_direct_only == s_direct.composite
    assert s.aio_composite == 0.0


def test_score_brand_no_aio_composite_equals_direct():
    runs = [_rv("m", hit=True, pos=2, sent="neutral", comp=1)]
    s = sc.score_brand(runs)
    assert s.composite == s.composite_direct_only
    assert s.aio_composite is None
