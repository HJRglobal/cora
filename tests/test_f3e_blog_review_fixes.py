"""Regression pins for the D-051 review of the F3E blog publish lane.

Each test names a defect the review found in the first cut. The two that matter
most are reproductions rather than assertions about code shape: the publish/dismiss
interleaving that left an article publicly live while the card said "still a
draft", and the cross-process write that erased a staged card outright.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cora.f3e_blog import news_lane, operating_files, pipeline, publish_cards as pc

REPO_ROOT = Path(__file__).resolve().parents[1]


def block_text(blocks) -> str:
    """All human-visible text in a Block Kit list, sections and contexts and
    button labels alike."""
    out: list[str] = []
    for b in blocks or []:
        text = (b.get("text") or {})
        if isinstance(text, dict) and text.get("text"):
            out.append(text["text"])
        for el in b.get("elements") or []:
            val = el.get("text")
            if isinstance(val, dict):
                val = val.get("text")
            if val:
                out.append(val)
    return "\n".join(out)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_F3E_BLOG_CARDS_PATH", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("CORA_F3E_BLOG_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("CORA_F3E_BLOG_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3energy.myshopify.com")
    monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "shpat_test")
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    yield


def _article(published=False):
    return {
        "id": "gid://shopify/Article/777", "title": "A Test Post",
        "handle": "a-test-post", "summary": "An excerpt.",
        "isPublished": published, "publishedAt": None, "tags": [],
        "blog": {"id": "gid://shopify/Blog/1", "handle": "learn", "title": "Learn"},
        "author": {"name": "F3 Energy Team"},
    }


def _staged(**kw):
    rec = pc.record_for_article(article=_article(), lane="learn",
                               excerpt="An excerpt.", rails_passed=11, **kw)
    return pc.stage_card(rec, client_factory=lambda: None)


# ---------------------------------------------------------------------------
# Lens 1 HIGH -- publish and dismiss interleaving
# ---------------------------------------------------------------------------


def test_a_dismiss_cannot_steal_a_row_a_publish_is_working_on():
    """THE defect this review existed to find.

    The publish branch released the lock and then spent up to ~40s in Shopify
    calls. A Harrison who saw nothing happen and tapped Dismiss won the row; the
    publish then SUCCEEDED; and the card told him the article was "still a
    draft" while it was publicly live, with no ledger row and no channel note.

    Reproduced by tapping Dismiss from inside the publish's own network call.
    """
    rec = _staged()
    handle = rec["handle"]
    dismiss_result = {}

    def publish_and_race(_gid):
        # Harrison taps Dismiss while the publish is in flight.
        dismiss_result["outcome"] = pc.process_tap(
            handle, pc.HARRISON_ID, action="dismiss")
        return _article(True)

    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article",
                      side_effect=publish_and_race), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")):
        outcome, msg = pc.process_tap(handle, pc.HARRISON_ID, action="publish")

    # The dismiss must have been refused, not silently won.
    assert dismiss_result["outcome"][0] == "already_handled"
    # The publish reports the truth about an article that is now live.
    assert outcome == "published", msg
    assert "Published" in msg
    stored = pc.get_record(handle)
    assert stored["state"] == pc.STATE_PUBLISHED
    # ...and it is not recorded as dismissed anywhere.
    assert stored.get("resolved_via") == "button"


def test_a_claimed_row_is_reported_honestly_to_a_second_tap():
    rec = _staged()
    pc._append_event(rec["handle"], "publishing", {"state": pc.STATE_PUBLISHING})
    outcome, msg = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert outcome == "already_handled"
    assert "middle of publishing" in msg


def test_a_failed_publish_releases_its_claim_so_the_retry_works():
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article",
                      side_effect=RuntimeError("boom")):
        outcome, _ = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert outcome == "failed"
    # Back to PENDING, or the retryable card would keep a button it cannot honour.
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PENDING
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")):
        outcome2, _ = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert outcome2 == "published"


def test_only_an_explicit_publish_publishes():
    """The default direction on an irreversible outward action must be safe: the
    first cut treated anything that was not the literal string "dismiss" as a
    publish, so a future typo would have published."""
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article") as pub:
        outcome, _ = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publsh")
    assert not pub.called
    assert outcome == "dismissed"


# ---------------------------------------------------------------------------
# Lens 1 MED -- dismiss must re-read live state
# ---------------------------------------------------------------------------


def test_dismiss_on_an_article_that_is_actually_live_says_so():
    """Harrison publishes from admin, then clears the stale card. Saying "it
    stays as a draft" would assert a false fact about the public site AND drop a
    live post out of the later-day read-back."""
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(True)):
        outcome, msg = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="dismiss")
    assert outcome == "already_live"
    assert "live already" in msg
    stored = pc.get_record(rec["handle"])
    assert stored["state"] == pc.STATE_PUBLISHED
    assert stored["public_url"], "must be re-checkable later"


def test_already_live_records_the_public_url_so_the_recheck_covers_it():
    """The first cut recorded PUBLISHED with no public_url, and
    recheck_published skips a record with no URL -- permanently exempting exactly
    the population most likely to have been published outside the lane."""
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(True)):
        pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    stored = pc.get_record(rec["handle"])
    assert stored["public_url"] == "https://f3energy.com/blogs/learn/a-test-post"

    report = pipeline.RunReport()
    with patch.object(pipeline.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")):
        pipeline.recheck_published(report)
    assert "all still serving" in report.render()


# ---------------------------------------------------------------------------
# Lens 5 HIGH -- the cross-process write
# ---------------------------------------------------------------------------


def test_a_concurrent_process_cannot_erase_a_staged_card(tmp_path):
    """Reproduces the reviewed failure in a real second interpreter.

    With the old JSON read-modify-write, the bot's write (built from a snapshot
    taken before the script staged card B) erased B entirely: the article staged,
    the backlog row consumed, the card in Harrison's DM, and every tap answering
    "I don't have a record of that draft anymore", forever.
    """
    events = tmp_path / "events.jsonl"
    a = _staged()  # card A exists

    # A separate process stages card B while this one resolves card A.
    script = textwrap.dedent(
        """
        import os, sys
        sys.path.insert(0, %r)
        sys.path.insert(0, %r)
        os.environ["CORA_F3E_BLOG_CARDS_PATH"] = %r
        os.environ["CORA_F3E_BLOG_LEDGER_PATH"] = %r
        os.environ["SHOPIFY_F3E_STORE"] = "f3energy.myshopify.com"
        os.environ["SHOPIFY_F3E_ACCESS_TOKEN"] = "shpat_test"
        from cora.f3e_blog import publish_cards as pc
        art = {"id": "gid://shopify/Article/888", "title": "Second Post",
               "handle": "second-post", "summary": "s", "isPublished": False,
               "publishedAt": None, "tags": [],
               "blog": {"id": "b", "handle": "learn", "title": "Learn"},
               "author": {"name": "F3 Energy Team"}}
        rec = pc.record_for_article(article=art, lane="learn", excerpt="s")
        pc.stage_card(rec, client_factory=lambda: None)
        print(rec["handle"])
        """
    ) % (str(REPO_ROOT), str(REPO_ROOT / "src"), str(events),
         str(tmp_path / "ledger.jsonl"))

    proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, cwd=str(REPO_ROOT), timeout=120)
    assert proc.returncode == 0, proc.stderr
    b_handle = proc.stdout.strip().splitlines()[-1]

    # This process now resolves card A, from a view taken before B existed.
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")):
        pc.process_tap(a["handle"], pc.HARRISON_ID, action="publish")

    store = pc._read_all()
    assert b_handle in store, "the other process's card was ERASED"
    assert store[b_handle]["state"] == pc.STATE_PENDING
    assert store[a["handle"]]["state"] == pc.STATE_PUBLISHED


def test_a_terminal_row_cannot_be_resurrected_by_a_later_event():
    """The fold is otherwise last-write-wins, and appending a non-terminal event
    to a resolved row would put it back on the board."""
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")):
        pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    pc._append_event(rec["handle"], "staged", {"state": pc.STATE_PENDING})
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PUBLISHED


def test_a_corrupt_event_line_does_not_empty_the_store():
    """`except Exception: return {}` could not tell "no file" from "torn file", so
    one bad byte wiped every pending card and the next write persisted the loss."""
    rec = _staged()
    with pc.pending_path().open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    store = pc._read_all()
    assert rec["handle"] in store
    with pytest.raises(pc.CardStoreCorrupt):
        pc._fold(strict=True)


# ---------------------------------------------------------------------------
# Lens 3 HIGH -- honest reporting
# ---------------------------------------------------------------------------


def test_an_undelivered_card_is_never_reported_as_sent():
    """stage_card is fail-soft, and four callers asserted "Publish card sent to
    Harrison" from the mere absence of an exception -- including the permanent
    Drive pipeline log."""
    client = MagicMock()
    client.conversations_open.side_effect = RuntimeError("slack down")
    rec = pc.record_for_article(article=_article(), lane="learn", excerpt="e")
    stored = pc.stage_card(rec, client_factory=lambda: client)
    assert pc.card_was_delivered(stored) is False


def test_an_undelivered_card_stays_re_offerable():
    """It was `already_carded_gids` that made an undelivered card unrecoverable,
    and turned "Every staged draft already has a card in your DMs" into a false
    statement about Harrison's DMs with no resend path."""
    client = MagicMock()
    client.conversations_open.side_effect = RuntimeError("slack down")
    rec = pc.record_for_article(article=_article(), lane="learn", excerpt="e")
    pc.stage_card(rec, client_factory=lambda: client)
    assert rec["article_gid"] not in pc.already_carded_gids()
    assert [r["handle"] for r in pc.undelivered_records()] == [rec["handle"]]


def test_a_delivered_card_is_not_re_offered():
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D1"}}
    client.chat_postMessage.return_value = {"ts": "1.1"}
    rec = pc.record_for_article(article=_article(), lane="learn", excerpt="e")
    pc.stage_card(rec, client_factory=lambda: client)
    assert rec["article_gid"] in pc.already_carded_gids()


def test_the_report_scrub_hides_drive_paths_hosts_and_api_bodies():
    """The weekly report is posted to #f3-marketing, where the Founder-OS Drive
    path, the store host, the admin API path and raw Shopify bodies do not
    belong -- and the class-level Slack sanitiser covers none of them."""
    cases = [
        r"[Errno 2] No such file: 'G:\My Drive\HJR-Founder-OS\02-F3-Energy\x.md'",
        "GraphQL network error: HTTPSConnectionPool(host='f3energy.myshopify.com', "
        "port=443): url /admin/api/2024-10/graphql.json",
        "See https://admin.shopify.com/store/f3energy/content/articles/61844 detail",
    ]
    for raw in cases:
        out = pc.scrub_for_report(raw)
        for leak in ("My Drive", "HJR-Founder-OS", "myshopify.com",
                     "admin.shopify.com", "/admin/api/"):
            assert leak not in out, "%s leaked from %r" % (leak, raw)


def test_an_unreadable_checklist_closes_the_gate_for_the_news_lane_too():
    """run_weekly gated on drift_blocked, which the checklist READ-FAILURE branch
    never set -- so a G: blip made run_learn say "I will not stage without it"
    and the News half then staged and carded an article in the same run."""
    with patch.object(operating_files, "read_checklist",
                      side_effect=OSError("mount gone")), \
         patch.object(news_lane, "sweep",
                      side_effect=AssertionError("must not sweep")) as swept:
        report = pipeline.run_weekly(client_factory=lambda: None)
    assert not swept.called
    assert report.gate_closed and report.failed
    assert "will not stage without it" in report.render()


def test_a_corrupt_state_file_does_not_disarm_the_drift_gate():
    """The gate is fail-closed by design; a torn state file made it report "first
    run, nothing to compare against" and re-adopt un-reviewed rails."""
    pipeline.state_path().parent.mkdir(parents=True, exist_ok=True)
    pipeline.state_path().write_text("{ this is not json", encoding="utf-8")
    report = pipeline.RunReport()
    with patch.object(operating_files, "read_checklist", return_value="1. No prices."):
        ok, _ = pipeline.check_checklist_drift(report)
    assert ok is False
    assert report.gate_closed and report.failed
    assert "unreadable" in report.render()


def test_a_press_tracker_outage_is_not_reported_as_a_quiet_week():
    report = pipeline.RunReport()
    with patch.object(news_lane, "published_f3_rows", return_value=([], False, 0)):
        news_lane.sweep(report, {"news_seen_page_ids": []})
    txt = report.render()
    assert "could not read it" in txt
    assert "not the same as a quiet week" in txt
    assert report.failed


def test_a_baseline_is_never_taken_from_an_incomplete_read():
    """A baseline from a partial read would later treat the missing rows as NEW
    flips and re-amplify coverage already published by hand."""
    flip = news_lane.PressFlip(page_id="p1", reporter="R", outlet="O",
                               date="2026-08-21", link="")
    report = pipeline.RunReport()
    with patch.object(news_lane, "published_f3_rows", return_value=([flip], True, 3)):
        state = news_lane.sweep(report, {})
    assert "news_seen_page_ids" not in state
    assert "not recording a baseline from an incomplete read" in report.render()


def test_dry_run_does_not_write_the_state_file():
    with patch.object(operating_files, "read_checklist", return_value="1. No prices."):
        report = pipeline.RunReport()
        ok, _ = pipeline.check_checklist_drift(report, dry_run=True)
    assert ok
    assert not pipeline.state_path().exists()
    assert "a real run would adopt" in report.render()


# ---------------------------------------------------------------------------
# Lens 1 / 3 -- the backlog row is advanced on a terminal outcome
# ---------------------------------------------------------------------------

BACKLOG = """# Backlog

| # | Status | Working title | Lane/pillar | Target prompt class | Notes |
|---|--------|---------------|-------------|---------------------|-------|
| 4 | DRAFTED 2026-08-26 (gid 777) | A Test Post | Ingredient explainer | x | y |
"""


@pytest.mark.parametrize("action,expect", [("publish", "PUBLISHED"),
                                           ("dismiss", "DISMISSED")])
def test_a_terminal_tap_advances_the_human_backlog_row(action, expect):
    """The two status-cell helpers had NO production caller: a published post
    read DRAFTED in Harrison's own table forever, and a DISMISSED topic kept the
    DRAFTED cell, so next_queued never returned it -- the topic was silently
    consumed with nothing published."""
    written = {}

    def _read_text(path, **kw):
        return BACKLOG

    def _write(path, text, **kw):
        written["text"] = text

    rec = _staged(backlog_row="4")
    with patch.object(operating_files.drive_io, "read_text", _read_text), \
         patch.object(operating_files.drive_io, "write_text_atomic", _write), \
         patch.object(pc.shopify_client, "get_article",
                      return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")):
        pc.process_tap(rec["handle"], pc.HARRISON_ID, action=action)

    assert written, "the backlog was never written"
    row = operating_files.parse_backlog(written["text"])[0]
    assert row.status == expect
    assert row.article_gid == "777"


def test_a_drive_failure_while_advancing_the_row_does_not_lose_the_publish():
    rec = _staged(backlog_row="4")
    with patch.object(operating_files.drive_io, "read_text",
                      side_effect=OSError("mount gone")), \
         patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")):
        outcome, _ = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert outcome == "published"
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PUBLISHED


def test_a_news_card_has_no_backlog_row_to_advance():
    rec = _staged(backlog_row=None)
    with patch.object(operating_files.drive_io, "read_text",
                      side_effect=AssertionError("must not touch the backlog")), \
         patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")):
        outcome, _ = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert outcome == "published"


# ---------------------------------------------------------------------------
# Lens 4 -- card copy after the fix
# ---------------------------------------------------------------------------


def test_a_card_for_a_draft_this_lane_did_not_write_says_no_preflight_ran():
    """The footer names the rails NOT machine checked, whose only reading is that
    the others WERE. On a tool-minted card nothing was checked at all."""
    rec = pc.record_for_article(article=_article(), lane="learn", excerpt="e")
    _, blocks = pc.build_publish_blocks(rec)
    body = "\n".join(b["text"]["text"] for b in blocks if b["type"] == "section")
    assert "my claims preflight never ran" in body


def test_a_card_without_an_admin_link_does_not_tell_harrison_to_use_one(monkeypatch):
    monkeypatch.delenv("SHOPIFY_F3E_STORE", raising=False)
    rec = pc.record_for_article(article=_article(), lane="learn", excerpt="e")
    assert rec["admin_url"] == ""
    for builder in (pc.build_publish_blocks, pc.build_buttons_off_blocks):
        _, blocks = builder(rec)
        text = block_text(blocks)
        assert "admin link above" not in text
        assert "Shopify admin" in text


def test_a_retryable_card_keeps_the_not_machine_checked_disclosure():
    """It still asks for a publish decision, so the disclosure has to survive."""
    rec = _staged()
    _, blocks = pc.build_publish_blocks(pc.get_record(rec["handle"]))
    kept = pc.terminal_card_blocks(blocks, "It failed.", keep_buttons=True)
    text = block_text(kept)
    assert "Still not machine checked" in text
    assert any(b.get("type") == "actions" for b in kept)


def test_a_terminal_card_drops_both_the_buttons_and_the_state_claims():
    rec = _staged()
    _, blocks = pc.build_publish_blocks(pc.get_record(rec["handle"]))
    kept = pc.terminal_card_blocks(blocks, "Published it.", keep_buttons=False)
    assert not any(b.get("type") == "actions" for b in kept)
    body = "\n".join((b.get("text") or {}).get("text", "") for b in kept)
    assert "ready to publish" not in body
    assert "Staged unpublished" not in body
    assert "Published it." in body


# ---------------------------------------------------------------------------
# Lens 5 MED -- the fetch ceiling
# ---------------------------------------------------------------------------


def test_fetch_public_page_caps_an_enormous_response():
    """This function is pointed at ARBITRARY third-party pages via a press-tracker
    URL field, through unvalidated redirects. The first cut buffered and decoded
    the whole body."""
    from cora.connectors import shopify_client as sc

    class _Resp:
        status_code = 200
        encoding = "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=65536):
            while True:  # an endless response
                yield b"x" * chunk_size

    with patch.object(sc.requests, "get", return_value=_Resp()):
        code, text = sc.fetch_public_page("https://an-outlet.example/story")
    assert code == 200
    assert len(text) <= sc.MAX_PAGE_BYTES + 65536


# ---------------------------------------------------------------------------
# Test-hygiene guard: the suite must not be able to reach the real Drive
# ---------------------------------------------------------------------------


def test_the_suite_cannot_reach_the_real_founder_os_drive():
    """This guard exists because it already bit.

    A publish tap advances the human editorial backlog row on Drive. A card test
    that set `backlog_row` without stubbing `drive_io` wrote a fixture's gid
    ("PUBLISHED (gid 777)") into Harrison's REAL backlog file, which had to be
    restored by hand. The suite-wide conftest fixture now redirects
    CORA_DRIVE_ROOT, so a future test that forgets to patch anything writes to
    tmp_path instead of to the Founder OS.
    """
    root = str(operating_files.drive_root())
    assert "HJR-Founder-OS" not in root, root
    assert not root.upper().startswith("G:"), root
    for path in (operating_files.backlog_path(), operating_files.checklist_path(),
                 operating_files.pipeline_log_path(), operating_files.lineup_path()):
        assert "HJR-Founder-OS" not in str(path), str(path)


def test_an_unstubbed_terminal_tap_writes_only_inside_the_sandbox(tmp_path):
    """Drive the exact path that caused the incident, with NOTHING patched on the
    IO layer, and prove the write lands in the sandbox."""
    real_backlog = Path(
        r"G:\My Drive\HJR-Founder-OS\02-F3-Energy\projects"
        r"\build-f3e-news-and-blog-strategy"
        r"\2026-08-26_f3e_learn-editorial-backlog-v1.md")
    before = real_backlog.read_text(encoding="utf-8") if real_backlog.exists() else None

    rec = _staged(backlog_row="4")
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")):
        outcome, _ = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")

    assert outcome == "published"
    if before is not None:
        assert real_backlog.read_text(encoding="utf-8") == before, \
            "the suite wrote to the REAL Founder-OS backlog file"
