"""The weekly F3E blog run: drift gate, draft/preflight loop, staging, refill,
the News sweep, and the Harrison-only card-drafts tool.

The recurring assertion across this file: when the run does not stage, it says so
and leaves the backlog row QUEUED. A pipeline that reports success for work it did
not do is the failure mode this lane cannot have.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock, patch

import pytest

from cora.f3e_blog import (drafting, news_lane, operating_files, pipeline,
                           preflight, publish_cards, refill)

BACKLOG = """# Backlog

| # | Status | Working title | Lane/pillar | Target prompt class | Notes |
|---|--------|---------------|-------------|---------------------|-------|
| 1 | PUBLISHED 2026-08-26 (gid 111) | Done one | Category education | x | y |
| 2 | QUEUED | L-Theanine and Caffeine | Ingredient explainer | jitters | no jitters promises |
| 3 | QUEUED | Sweetener Guide | Ingredient explainer (Pure-led) | sweetener | Pure-only clean |
"""

CHECKLIST = "1. No prices.\n2. No em-dashes.\n"
TEMPLATES = "## Learn template\n- Title: the question people type.\n"
LINEUP = "| PURE | Original | PURE-Original |\n"

CLEAN_DRAFT = {
    "title": "Why L-Theanine and Caffeine Show Up Together",
    "summary": "They pair because of how they interact.",
    "body_html": "<p>F3 Pure and F3 Energy both use green tea caffeine.</p>",
    "tags": ["Ingredients"],
}
DIRTY_DRAFT = {
    "title": "F3 Energy is clean",
    "summary": "Only $39.99 per pack.",
    "body_html": "<p>F3 Energy is clean and simple.</p>",
    "tags": ["x"],
}

ARTICLE = {
    "id": "gid://shopify/Article/777",
    "title": CLEAN_DRAFT["title"], "handle": "why-l-theanine",
    "summary": CLEAN_DRAFT["summary"], "isPublished": False, "publishedAt": None,
    "tags": ["Learn"], "blog": {"id": operating_files.BLOG_LEARN, "handle": "learn",
                                "title": "Learn"},
    "author": {"name": "F3 Energy Team"},
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_F3E_BLOG_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("CORA_F3E_BLOG_CARDS_PATH", str(tmp_path / "cards.json"))
    monkeypatch.setenv("CORA_F3E_BLOG_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3energy.myshopify.com")
    monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "shpat_test")
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    # Drive is never really touched.
    self_state = {"backlog": BACKLOG}

    def _read_text(path, **kw):
        name = str(path)
        if operating_files.BACKLOG_NAME in name:
            return self_state["backlog"]
        if operating_files.CHECKLIST_NAME in name:
            return CHECKLIST
        if operating_files.TEMPLATES_NAME in name:
            return TEMPLATES
        if "lineup" in name:
            return LINEUP
        if "pipeline-log" in name:
            return "# log\n\n## old\n- x\n"
        raise FileNotFoundError(name)

    def _write(path, text, **kw):
        if operating_files.BACKLOG_NAME in str(path):
            self_state["backlog"] = text

    monkeypatch.setattr(operating_files.drive_io, "read_text", _read_text)
    monkeypatch.setattr(operating_files.drive_io, "write_text_atomic", _write)
    monkeypatch.setattr(drafting, "fetch_faq_text", lambda **kw: "FAQ TEXT")
    monkeypatch.setattr(refill, "build_proposals", lambda **kw: [])
    monkeypatch.setattr(news_lane, "published_f3_rows", lambda: ([], True, 0))
    yield self_state


def _no_client():
    return None


def _delivering_client():
    """A Slack client that actually delivers, so `card_was_delivered` is true.

    Needed because the review made delivery a REPORTED fact rather than an
    assumed one: a card recorded but never DMd is no longer counted as carded,
    and the pipeline now says so instead of claiming "Publish card sent".
    """
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D1"}}
    client.chat_postMessage.return_value = {"ts": "1.1"}
    return client


# ---------------------------------------------------------------------------
# Checklist drift gate
# ---------------------------------------------------------------------------


def test_first_run_adopts_the_checklist_fingerprint_without_alarming():
    report = pipeline.RunReport()
    ok, fp = pipeline.check_checklist_drift(report)
    assert ok and fp
    assert not report.drift_blocked
    assert "first run" in report.render()


def test_a_changed_checklist_blocks_staging(monkeypatch, isolated):
    pipeline.ack_checklist()
    monkeypatch.setattr(operating_files, "read_checklist",
                        lambda: CHECKLIST + "\n15. A brand new rail.\n")
    report = pipeline.run_learn(client_factory=_no_client)
    assert report.drift_blocked
    assert not report.staged_gid
    txt = report.render()
    assert "CHANGED" in txt and "--ack-checklist" in txt


def test_whitespace_only_checklist_churn_does_not_block(monkeypatch):
    pipeline.ack_checklist()
    monkeypatch.setattr(operating_files, "read_checklist",
                        lambda: CHECKLIST.replace("\n", "\r\n") + "\r\n")
    report = pipeline.RunReport()
    ok, _ = pipeline.check_checklist_drift(report)
    assert ok, "a line-ending flip from Drive sync must not read as a rule change"


def test_ack_rearms_staging(monkeypatch):
    pipeline.ack_checklist()
    monkeypatch.setattr(operating_files, "read_checklist", lambda: CHECKLIST + "15. x\n")
    assert pipeline.check_checklist_drift(pipeline.RunReport())[0] is False
    pipeline.ack_checklist()
    assert pipeline.check_checklist_drift(pipeline.RunReport())[0] is True


def test_an_unreadable_checklist_blocks_rather_than_proceeding(monkeypatch):
    monkeypatch.setattr(operating_files, "read_checklist",
                        lambda: (_ for _ in ()).throw(OSError("mount gone")))
    report = pipeline.run_learn(client_factory=_no_client)
    assert not report.staged_gid
    assert "will not stage without it" in report.render()


# ---------------------------------------------------------------------------
# The draft / preflight loop
# ---------------------------------------------------------------------------


def test_a_clean_draft_is_staged_unpublished_and_carded(isolated):
    with patch.object(drafting, "draft_article", return_value=CLEAN_DRAFT), \
         patch.object(pipeline.shopify_client, "create_article",
                      return_value=ARTICLE) as create:
        report = pipeline.run_learn(client_factory=_delivering_client)
    assert report.staged_gid == ARTICLE["id"]
    assert "Publish card sent to Harrison." in report.render()
    kw = create.call_args[1]
    assert kw["blog_id"] == operating_files.BLOG_LEARN
    assert "Learn" in kw["tags"]
    # The row advanced only after the verified staging.
    rows = operating_files.parse_backlog(isolated["backlog"])
    assert rows[1].status == "DRAFTED"
    assert rows[1].article_gid == "777"
    # ...and a card exists for it.
    assert ARTICLE["id"] in publish_cards.already_carded_gids()


def test_a_blocked_draft_stages_nothing_and_leaves_the_row_queued(isolated):
    with patch.object(drafting, "draft_article", return_value=DIRTY_DRAFT), \
         patch.object(pipeline.shopify_client, "create_article") as create:
        report = pipeline.run_learn(client_factory=_no_client)
    assert not create.called
    assert not report.staged_gid
    assert report.blocked_rails
    assert operating_files.parse_backlog(isolated["backlog"])[1].status == "QUEUED"
    assert "stays QUEUED" in report.render()
    assert "Nothing was written to Shopify" in report.render()


def test_a_tripped_rail_gets_one_revision_then_passes():
    calls = []

    def fake(row, *, template, faq, lineup, revision_trips=""):
        calls.append(revision_trips)
        return DIRTY_DRAFT if not revision_trips else CLEAN_DRAFT

    with patch.object(drafting, "draft_article", side_effect=fake), \
         patch.object(pipeline.shopify_client, "create_article", return_value=ARTICLE):
        report = pipeline.run_learn(client_factory=_no_client)
    assert len(calls) == 2
    assert calls[0] == ""
    # The revision carries the actual tripped rail and offending sentence.
    assert "R2" in calls[1]
    assert report.staged_gid == ARTICLE["id"]
    assert "after a revision" in report.render()


def test_the_revision_is_bounded_to_one_retry():
    """A model that cannot satisfy a rail costs two calls a week, not a loop."""
    calls = []

    def fake(row, *, template, faq, lineup, revision_trips=""):
        calls.append(revision_trips)
        return DIRTY_DRAFT

    with patch.object(drafting, "draft_article", side_effect=fake), \
         patch.object(pipeline.shopify_client, "create_article") as create:
        report = pipeline.run_learn(client_factory=_no_client)
    assert len(calls) == pipeline.MAX_DRAFT_ATTEMPTS == 2
    assert not create.called
    assert report.blocked_rails


def test_an_unusable_draft_stages_nothing(isolated):
    with patch.object(drafting, "draft_article", return_value=None), \
         patch.object(pipeline.shopify_client, "create_article") as create:
        report = pipeline.run_learn(client_factory=_no_client)
    assert not create.called
    assert "did not come back usable" in report.render()
    assert operating_files.parse_backlog(isolated["backlog"])[1].status == "QUEUED"


def test_a_staging_failure_leaves_the_row_queued_and_says_nothing_is_live(isolated):
    with patch.object(drafting, "draft_article", return_value=CLEAN_DRAFT), \
         patch.object(pipeline.shopify_client, "create_article",
                      side_effect=RuntimeError("read-back FAILED")):
        report = pipeline.run_learn(client_factory=_no_client)
    assert not report.staged_gid
    assert "nothing is live" in report.render()
    assert operating_files.parse_backlog(isolated["backlog"])[1].status == "QUEUED"


def test_a_backlog_write_failure_still_reports_the_draft_as_staged(monkeypatch):
    """The article IS staged. Reporting a failure would be the opposite lie from
    the usual one, and just as wrong."""
    monkeypatch.setattr(operating_files, "write_backlog",
                        lambda text: (_ for _ in ()).throw(OSError("mount gone")))
    with patch.object(drafting, "draft_article", return_value=CLEAN_DRAFT), \
         patch.object(pipeline.shopify_client, "create_article", return_value=ARTICLE):
        report = pipeline.run_learn(client_factory=_no_client)
    assert report.staged_gid == ARTICLE["id"]
    txt = report.render()
    assert "The draft is staged" in txt
    assert "still reads QUEUED" in txt


def test_a_drained_backlog_is_reported_not_crashed(monkeypatch, isolated):
    isolated["backlog"] = BACKLOG.replace("| QUEUED |", "| DRAFTED |")
    with patch.object(pipeline.shopify_client, "create_article") as create:
        report = pipeline.run_learn(client_factory=_no_client)
    assert not create.called
    assert "No QUEUED backlog row" in report.render()


def test_dry_run_writes_nothing_anywhere(isolated):
    before = isolated["backlog"]
    with patch.object(drafting, "draft_article", return_value=CLEAN_DRAFT), \
         patch.object(pipeline.shopify_client, "create_article") as create:
        report = pipeline.run_learn(dry_run=True, client_factory=_no_client)
    assert not create.called
    assert isolated["backlog"] == before
    assert not publish_cards.already_carded_gids()
    assert "DRY RUN" in report.render()


# ---------------------------------------------------------------------------
# Refill
# ---------------------------------------------------------------------------


def test_refill_fires_only_below_the_threshold(monkeypatch, isolated):
    proposals = [{"title": "What is the healthiest energy drink?",
                  "lane_pillar": "Category education (Energy)",
                  "target_prompt": "ENG-D02 0-mention", "notes": "n"}]
    monkeypatch.setattr(refill, "build_proposals", lambda **kw: proposals)
    # Only one QUEUED row left after this run stages the other.
    isolated["backlog"] = BACKLOG.replace(
        "| 3 | QUEUED | Sweetener Guide", "| 3 | PUBLISHED 2026-01-01 (gid 9) | Sweetener Guide")
    with patch.object(drafting, "draft_article", return_value=CLEAN_DRAFT), \
         patch.object(pipeline.shopify_client, "create_article", return_value=ARTICLE):
        report = pipeline.run_learn(client_factory=_no_client)
    assert report.proposed == 1
    rows = operating_files.parse_backlog(isolated["backlog"])
    assert [r for r in rows if r.status == "PROPOSED"]


def test_no_refill_when_the_backlog_is_deep(isolated):
    with patch.object(drafting, "draft_article", return_value=CLEAN_DRAFT), \
         patch.object(pipeline.shopify_client, "create_article", return_value=ARTICLE), \
         patch.object(refill, "build_proposals",
                      side_effect=AssertionError("must not be called")):
        # 2 QUEUED rows, threshold is 4 -- but one is consumed, leaving 1, so the
        # refill DOES apply here. Use a deep backlog instead.
        pass
    deep = BACKLOG + "".join(
        "| %d | QUEUED | Topic %d | Category education | x | y |\n" % (n, n)
        for n in range(4, 9))
    isolated["backlog"] = deep
    with patch.object(drafting, "draft_article", return_value=CLEAN_DRAFT), \
         patch.object(pipeline.shopify_client, "create_article", return_value=ARTICLE):
        report = pipeline.run_learn(client_factory=_no_client)
    assert report.proposed == 0
    assert "No refill needed" in report.render()


def test_refill_proposals_are_never_draftable():
    props = [{"title": "New topic"}]
    text = operating_files.append_proposed_rows(BACKLOG, props)
    rows = operating_files.parse_backlog(text)
    assert operating_files.next_queued(rows).title == "L-Theanine and Caffeine"


def test_refill_skips_titles_already_in_the_backlog():
    with patch.object(refill, "zero_mention_prompt_ids",
                      return_value=[("ENG-D02", "energy", "discovery")]), \
         patch.object(refill, "_prompt_texts",
                      return_value={"ENG-D02": "Sweetener Guide"}):
        assert refill.build_proposals(exclude_titles={"Sweetener Guide"}) == []


def test_refill_is_empty_rather_than_raising_without_a_db(monkeypatch, tmp_path):
    monkeypatch.setenv("CORA_AI_VISIBILITY_DB", str(tmp_path / "nope.db"))
    assert refill.zero_mention_prompt_ids() == []
    assert refill.build_proposals() == []


# ---------------------------------------------------------------------------
# News lane
# ---------------------------------------------------------------------------


def _flip(pid="p1"):
    return news_lane.PressFlip(page_id=pid, reporter="J Watkins",
                               outlet="East Valley Tribune", date="2026-08-21",
                               link="https://example.com/story")


def test_first_news_run_adopts_the_baseline_and_drafts_nothing(monkeypatch):
    monkeypatch.setattr(news_lane, "published_f3_rows", lambda: ([_flip("a"), _flip("b")], True, 0))
    report = pipeline.RunReport()
    with patch.object(drafting, "draft_article") as d:
        state = news_lane.sweep(report, {}, client_factory=_no_client)
    assert not d.called
    assert set(state["news_seen_page_ids"]) == {"a", "b"}
    assert "baseline recorded" in report.render()


def test_a_new_published_flip_is_drafted_and_staged(monkeypatch):
    monkeypatch.setattr(news_lane, "published_f3_rows", lambda: ([_flip("a"), _flip("new")], True, 0))
    monkeypatch.setattr(news_lane, "fetch_headline", lambda url: "Mesa firm sees growth")
    report = pipeline.RunReport()
    news_article = dict(ARTICLE, id="gid://shopify/Article/888",
                        blog={"id": operating_files.BLOG_NEWS, "handle": "news",
                              "title": "News"})
    with patch.object(drafting, "draft_article", return_value=CLEAN_DRAFT), \
         patch.object(news_lane.shopify_client, "create_article",
                      return_value=news_article) as create:
        state = news_lane.sweep(report, {"news_seen_page_ids": ["a"]},
                                client_factory=_delivering_client)
    assert create.call_args[1]["blog_id"] == operating_files.BLOG_NEWS
    assert "Press" in create.call_args[1]["tags"]
    assert "new" in state["news_seen_page_ids"]
    assert news_article["id"] in publish_cards.already_carded_gids()


def test_a_failed_news_draft_is_retried_next_week_not_dropped(monkeypatch):
    monkeypatch.setattr(news_lane, "published_f3_rows",
                        lambda: ([_flip("new")], True, 0))
    monkeypatch.setattr(news_lane, "fetch_headline", lambda url: "H")
    report = pipeline.RunReport()
    with patch.object(drafting, "draft_article", return_value=None):
        state = news_lane.sweep(report, {"news_seen_page_ids": []},
                                client_factory=_no_client)
    assert "new" not in state.get("news_seen_page_ids", [])


def test_only_one_news_post_per_run(monkeypatch):
    """A batch of amplifications on one day reads as scaled content, and the
    cadence ruling was depth over volume."""
    monkeypatch.setattr(news_lane, "published_f3_rows",
                        lambda: ([_flip("n1"), _flip("n2"), _flip("n3")], True, 0))
    monkeypatch.setattr(news_lane, "fetch_headline", lambda url: "H")
    report = pipeline.RunReport()
    with patch.object(drafting, "draft_article", return_value=CLEAN_DRAFT), \
         patch.object(news_lane.shopify_client, "create_article",
                      return_value=ARTICLE) as create:
        news_lane.sweep(report, {"news_seen_page_ids": []}, client_factory=_no_client)
    assert create.call_count == 1
    assert "waiting and will be offered on later runs" in report.render()


def test_a_blocked_news_draft_stages_nothing(monkeypatch):
    monkeypatch.setattr(news_lane, "published_f3_rows",
                        lambda: ([_flip("new")], True, 0))
    monkeypatch.setattr(news_lane, "fetch_headline", lambda url: "H")
    report = pipeline.RunReport()
    with patch.object(drafting, "draft_article", return_value=DIRTY_DRAFT), \
         patch.object(news_lane.shopify_client, "create_article") as create:
        news_lane.sweep(report, {"news_seen_page_ids": []}, client_factory=_no_client)
    assert not create.called
    assert "preflight blocked" in report.render()


def test_a_genuinely_quiet_week_is_reported_as_quiet(monkeypatch):
    """A clean read that found nothing. Distinct from an outage, which is
    reported separately -- see test_f3e_blog_review_fixes.py."""
    monkeypatch.setattr(news_lane, "published_f3_rows", lambda: ([], True, 0))
    report = pipeline.RunReport()
    state = news_lane.sweep(report, {"news_seen_page_ids": []})
    assert "no Published F3 coverage rows" in report.render()
    assert not report.failed
    assert state == {"news_seen_page_ids": []}


def test_press_rows_are_filtered_to_f3_and_published():
    assert news_lane._PUBLISHED == "Published"
    assert news_lane._F3_ENTITIES == ("F3E", "Both")


def test_headline_is_read_from_the_page_not_recalled():
    with patch.object(news_lane.shopify_client, "fetch_public_page",
                      return_value=(200, "<html><title>Mesa firm sees growth</title>")):
        assert news_lane.fetch_headline("https://x") == "Mesa firm sees growth"
    with patch.object(news_lane.shopify_client, "fetch_public_page",
                      return_value=(404, "")):
        assert news_lane.fetch_headline("https://x") == ""
    assert news_lane.fetch_headline("") == ""


# ---------------------------------------------------------------------------
# Published recheck (D-110 rule 2)
# ---------------------------------------------------------------------------


def test_recheck_flags_a_published_post_that_stopped_serving():
    rec = publish_cards.record_for_article(article=dict(ARTICLE, isPublished=True),
                                          lane="learn", excerpt="e")
    rec.update({"state": publish_cards.STATE_PUBLISHED,
                "public_url": "https://f3energy.com/blogs/learn/why-l-theanine"})
    publish_cards.stage_card(rec, client_factory=_no_client)
    report = pipeline.RunReport()
    with patch.object(pipeline.shopify_client, "fetch_public_page", return_value=(404, "")):
        problems = pipeline.recheck_published(report)
    assert len(problems) == 1
    assert "did NOT read back clean" in report.render()


def test_recheck_is_quiet_when_everything_still_serves():
    rec = publish_cards.record_for_article(article=dict(ARTICLE, isPublished=True),
                                          lane="learn", excerpt="e")
    rec.update({"state": publish_cards.STATE_PUBLISHED,
                "public_url": "https://f3energy.com/blogs/learn/why-l-theanine"})
    publish_cards.stage_card(rec, client_factory=_no_client)
    report = pipeline.RunReport()
    with patch.object(pipeline.shopify_client, "fetch_public_page",
                      return_value=(200, ARTICLE["title"])):
        assert pipeline.recheck_published(report) == []


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------


def test_the_pipeline_cannot_publish():
    """Restated here (not only in the card tests) because this is the module a
    future change is most likely to add a convenience publish to."""
    import ast
    import textwrap
    for mod in (pipeline, drafting, news_lane, refill):
        tree = ast.parse(textwrap.dedent(inspect.getsource(mod)))
        for node in ast.walk(tree):
            if (body := getattr(node, "body", None)) and isinstance(node, (
                    ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if (isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    node.body = body[1:] or [ast.Pass()]
        code = ast.unparse(ast.fix_missing_locations(tree))
        assert "publish_article" not in code, mod.__name__
        # Not a blanket ban on the word: the run's log line legitimately reports
        # "read back OK (title match, isPublished:false)" as prose for a human.
        # What must be absent is the field being SENT as true.
        for sending in ("'isPublished': True", '"isPublished": True'):
            assert sending not in code, "%s: %s" % (mod.__name__, sending)


def test_drafting_disables_thinking():
    """Sonnet 5 thinks by default and max_tokens caps thinking+output combined, so
    an omitted `thinking` spends the article's budget on reasoning."""
    assert drafting._THINKING_DISABLED == {"type": "disabled"}
    assert "thinking=_THINKING_DISABLED" in inspect.getsource(drafting.draft_article)


def test_drafting_refuses_without_the_cleared_fact_source(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    row = operating_files.parse_backlog(BACKLOG)[1]
    assert drafting.draft_article(row, template="t", faq="", lineup="l") is None


def test_drafting_json_parser_tolerates_raw_newlines_in_html():
    """body_html is multi-line, and a raw newline inside a JSON string is invalid
    under strict JSON -- which is what made the first live revision pass fail."""
    raw = '{"title": "T", "summary": "s", "body_html": "<p>line one\nline two</p>", "tags": []}'
    out = drafting._extract_json(raw)
    assert out and "line two" in out["body_html"]


def test_drafting_returns_none_on_junk():
    assert drafting._extract_json("no json here") is None
    assert drafting._extract_json("") is None
    assert drafting._extract_json('["a list"]') is None


# ---------------------------------------------------------------------------
# The card-drafts tool
# ---------------------------------------------------------------------------


def test_card_drafts_tool_is_harrison_only():
    from cora.tools import tool_dispatch as td
    out = td._tool_f3e_blog_card_drafts("U0B3AEJCYGP", "F3E", {})
    assert "Only Harrison" in out


def test_card_drafts_tool_cards_only_uncarded_drafts():
    from cora.tools import tool_dispatch as td
    unpub = dict(ARTICLE, id="gid://shopify/Article/901")
    client = _delivering_client()
    with patch.object(publish_cards.shopify_client, "list_unpublished",
                      side_effect=[[unpub], []]), \
         patch.object(publish_cards, "_default_client_factory", lambda: client):
        out = td._tool_f3e_blog_card_drafts(publish_cards.HARRISON_ID, "F3E", {})
    assert "Sent you 1 publish card" in out
    # Asking again must not re-offer a DELIVERED card.
    with patch.object(publish_cards.shopify_client, "list_unpublished",
                      side_effect=[[unpub], []]), \
         patch.object(publish_cards, "_default_client_factory", lambda: client):
        again = td._tool_f3e_blog_card_drafts(publish_cards.HARRISON_ID, "F3E", {})
    assert "already had a card" in again


def test_card_drafts_tool_reports_an_undelivered_card_as_undelivered():
    """Delivery is a reported fact now, not an assumed one."""
    from cora.tools import tool_dispatch as td
    unpub = dict(ARTICLE, id="gid://shopify/Article/902")
    broken = MagicMock()
    broken.conversations_open.side_effect = RuntimeError("slack down")
    with patch.object(publish_cards.shopify_client, "list_unpublished",
                      side_effect=[[unpub], []]), \
         patch.object(publish_cards, "_default_client_factory", lambda: broken):
        out = td._tool_f3e_blog_card_drafts(publish_cards.HARRISON_ID, "F3E", {})
    assert "could not be carded" in out
    assert "staged and unpublished" in out


def test_card_drafts_tool_reports_a_partial_run_honestly():
    """The first cut discarded what it had already done on an exception and
    returned "no cards were sent. Nothing was changed"."""
    from cora.tools import tool_dispatch as td
    unpub = dict(ARTICLE, id="gid://shopify/Article/903")
    client = _delivering_client()
    with patch.object(publish_cards.shopify_client, "list_unpublished",
                      side_effect=[[unpub], RuntimeError("shopify down")]), \
         patch.object(publish_cards, "_default_client_factory", lambda: client):
        out = td._tool_f3e_blog_card_drafts(publish_cards.HARRISON_ID, "F3E", {})
    assert "Sent you 1 publish card" in out
    assert "stopped early" in out


def test_card_drafts_tool_is_honest_when_there_is_nothing_staged():
    from cora.tools import tool_dispatch as td
    with patch.object(publish_cards.shopify_client, "list_unpublished",
                      return_value=[]):
        out = td._tool_f3e_blog_card_drafts(publish_cards.HARRISON_ID, "F3E", {})
    assert "no unpublished drafts" in out


def test_card_drafts_tool_emits_no_contract_sentinel():
    """The narration net honours a sentinel only for tools inside
    _CONTRACT_WRITE_TOOLS, so a sentinel from this non-member tool would be inert
    there and could reach Harrison verbatim."""
    from cora.tools import tool_dispatch as td
    from cora.claude_client import _CONTRACT_WRITE_TOOLS
    assert "f3e_blog_card_drafts" not in _CONTRACT_WRITE_TOOLS
    outs = []
    with patch.object(publish_cards.shopify_client, "list_unpublished", return_value=[]):
        outs.append(td._tool_f3e_blog_card_drafts(publish_cards.HARRISON_ID, "F3E", {}))
    outs.append(td._tool_f3e_blog_card_drafts("U0OTHER", "F3E", {}))
    with patch.object(publish_cards.shopify_client, "list_unpublished",
                      side_effect=RuntimeError("boom")):
        outs.append(td._tool_f3e_blog_card_drafts(publish_cards.HARRISON_ID, "F3E", {}))
    for text in outs:
        assert text and text.strip()
        for tok in ("WRITE_CONFIRMED", "WRITE_BLOCKED", "tell the user"):
            assert tok not in text


def test_card_drafts_tool_is_exposed_where_it_belongs():
    from cora.tools import tool_dispatch as td
    for entity, expected in (("F3E", True), ("FNDR", True), ("LEX", False),
                             ("OSN", False)):
        names = [t["name"] for t in td.tools_for_entity(entity)]
        assert ("f3e_blog_card_drafts" in names) is expected, entity


# ---------------------------------------------------------------------------
# The draft envelope: markers, not JSON
# ---------------------------------------------------------------------------

MARKER_REPLY = """===TITLE===
An Honest Sweetener Guide
===SUMMARY===
Reading past the "0 sugar" claim on the front of a can.
===TAGS===
Ingredients, Learn
===BODY===
<p>Past the "0 sugar" claim, F3 Pure is clean-sweetened.</p>
<a href="/collections/pure">Shop Pure</a>
<script type="application/ld+json">{"headline":"An Honest Sweetener Guide"}</script>"""


def test_the_marker_envelope_survives_quotes_in_prose_and_html():
    """The measured failure that retired the JSON envelope: a model wrote
    `reading past the "0 sugar" claim` inside a JSON string, with
    stop_reason=end_turn. An unescaped quote is indistinguishable from the end of
    the string, so no tolerant parser can repair it -- and quotation marks in
    prose are not an edge case, they are how people write."""
    out = drafting._extract_sections(MARKER_REPLY)
    assert out is not None
    assert out["title"] == "An Honest Sweetener Guide"
    assert out["tags"] == ["Ingredients", "Learn"]
    # Every byte of the body survives: prose quotes, HTML attributes, JSON-LD.
    assert '"0 sugar"' in out["body_html"]
    assert 'href="/collections/pure"' in out["body_html"]
    assert "application/ld+json" in out["body_html"]


def test_the_marker_envelope_tolerates_a_code_fence():
    out = drafting._extract_sections("```\n" + MARKER_REPLY + "\n```")
    assert out and out["title"] == "An Honest Sweetener Guide"


@pytest.mark.parametrize("raw", [
    "no markers at all",
    "===TITLE===\nT",                      # incomplete
    "===TITLE===\nT\n===BODY===\n<p>b</p>",  # missing SUMMARY and TAGS
    "",
])
def test_an_incomplete_envelope_is_rejected_rather_than_half_parsed(raw):
    assert drafting._extract_sections(raw) is None


def test_a_json_reply_still_parses_as_a_fallback():
    """Kept so a model that answers in JSON anyway is not a hard failure."""
    raw = ('{"title":"T","summary":"s","body_html":"<p>b</p>","tags":["x"]}')
    out = drafting._extract_sections(raw) or drafting._extract_json(raw)
    assert out and out["title"] == "T"


def test_the_prompt_asks_for_markers_and_promises_no_escaping():
    row = operating_files.parse_backlog(BACKLOG)[1]
    prompt = drafting.build_prompt(row, template="t", faq="f", lineup="l")
    for marker in ("===TITLE===", "===SUMMARY===", "===TAGS===", "===BODY==="):
        assert marker in prompt
    assert "Nothing needs escaping" in prompt


def test_the_revision_note_asks_for_the_same_envelope():
    """A revision that asked for "the full JSON object" would reintroduce the
    escaping hazard on the retry path only -- the path nobody watches."""
    row = operating_files.parse_backlog(BACKLOG)[1]
    prompt = drafting.build_prompt(row, template="t", faq="f", lineup="l",
                                   revision_trips="R2 (...) in body: ...")
    assert "marker sections" in prompt
    assert "JSON object" not in prompt
