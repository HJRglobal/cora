"""Tests for the weekly Slack score card (build + post; network mocked)."""

from __future__ import annotations

import types

import pytest

from cora.ai_visibility import report as rpt
from cora.ai_visibility import store as st
from cora.ai_visibility.scorer import BrandScore


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    st.set_db_path(tmp_path / "av.db")
    yield
    st.set_db_path(None)


def _seed_scan(*, with_aio=False):
    sid = st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=1,
                         brands=["energy"])
    st.save_score(sid, BrandScore(
        brand="energy", composite=62.0 if not with_aio else 55.0,
        composite_direct_only=62.0, presence=60, share_of_voice=31, position=40,
        sentiment=70, unaided_presence=48.0,
        aio_composite=(50.0 if with_aio else None)))
    # a competitor-only answer -> a gap + a top rival
    from cora.ai_visibility.classifier import Classification
    a = st.insert_answer(scan_id=sid, brand="energy", prompt_id="ENG-D02", intent="discovery",
                         aided=False, model="perplexity_sonar", run_index=0, raw_text="x",
                         classification=Classification(mentioned=False, is_correct_brand=False,
                                                       competitors_mentioned=["Celsius", "Ghost"]),
                         cost_usd=0.0)
    st.record_answer_mentions(scan_id=sid, answer_id=a, brand="energy", brand_name="F3 Energy",
                              model="perplexity_sonar",
                              classification=Classification(mentioned=False,
                                                            competitors_mentioned=["Celsius", "Ghost"]))
    st.finish_scan(sid, status="completed", total_calls=1, total_cost_usd=0.1,
                   aio_included=with_aio)
    return sid


def test_build_scorecard_first_run_and_format():
    sid = _seed_scan()
    scores = st.scores_for_scan(sid)
    card = rpt.build_scorecard(scores)
    assert "*F3 AI Visibility - weekly scan" in card
    assert "*F3 Energy* - 62/100" in card
    assert "first run - no baseline" in card       # WoW baseline text
    assert "Unaided presence 48%" in card
    assert "top rivals:" in card and "Celsius" in card
    assert "Competitors beat us on:" in card
    # green status emoji for 62/100
    assert "\U0001F7E2" in card
    # no raw markdown table separator
    assert "|---" not in card and "---|" not in card
    assert "Google AI Overviews unavailable this week" in card


def test_build_scorecard_with_aio():
    sid = _seed_scan(with_aio=True)
    card = rpt.build_scorecard(st.scores_for_scan(sid))
    assert "Google AI Overviews: 50/100" in card
    assert "+ Google AI Overviews. Scores refresh weekly." in card
    # 55/100 -> yellow status
    assert "\U0001F7E1" in card


def test_build_scorecard_empty():
    assert "no completed scan" in rpt.build_scorecard({})


def test_resolve_channel_precedence(monkeypatch):
    monkeypatch.delenv("AI_VISIBILITY_CHANNEL", raising=False)
    assert rpt._resolve_channel(None) == rpt._DEFAULT_CHANNEL
    assert rpt._resolve_channel("cora-build") == "cora-build"
    monkeypatch.setenv("AI_VISIBILITY_CHANNEL", "f3-ai-visibility")
    assert rpt._resolve_channel(None) == "f3-ai-visibility"


def test_post_scorecards_success(monkeypatch):
    sid = _seed_scan()
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return types.SimpleNamespace(ok=True, status_code=200, json=lambda: {"ok": True})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    ok = rpt.post_scorecards(sid, channel="cora-build")
    assert ok is True
    assert captured["url"] == "https://slack.com/api/chat.postMessage"
    assert captured["json"]["channel"] == "cora-build"
    assert captured["json"]["mrkdwn"] is True
    assert captured["json"]["unfurl_links"] is False
    assert "F3 Energy" in captured["json"]["text"]


def test_post_scorecards_no_token(monkeypatch):
    sid = _seed_scan()
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert rpt.post_scorecards(sid) is False


def test_post_scorecards_slack_error_is_failsoft(monkeypatch):
    sid = _seed_scan()
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    def fake_post(url, headers=None, json=None, timeout=None):
        return types.SimpleNamespace(ok=True, status_code=200,
                                     json=lambda: {"ok": False, "error": "channel_not_found"})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    assert rpt.post_scorecards(sid, channel="nope") is False  # never raises


def test_post_scorecards_no_scores(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    # scan exists but no scores rows
    sid = st.create_scan(basket_version=1, models=["perplexity_sonar"], runs_per_prompt=1,
                         brands=["energy"])
    assert rpt.post_scorecards(sid) is False
