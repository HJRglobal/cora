"""The app.py Slack wrapper for the F3E blog publish card.

Driven with realistic `block_actions` payloads, because the wrapper is the half
the module tests cannot reach: whether the card is EDITED, whether the reply is
ephemeral, and whether the buttons survive are all decided here, and getting them
wrong is what leaves a card advertising a dead affordance.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cora import app as app_mod
from cora.f3e_blog import publish_cards as pc

OTHER_USER = "U0B3AEJCYGP"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_F3E_BLOG_CARDS_PATH", str(tmp_path / "cards.json"))
    monkeypatch.setenv("CORA_F3E_BLOG_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3energy.myshopify.com")
    monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "shpat_test")
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    yield


def _article(published=False):
    return {
        "id": "gid://shopify/Article/777", "title": "A Test Post",
        "handle": "a-test-post", "summary": "An excerpt.",
        "isPublished": published, "publishedAt": None, "tags": [],
        "blog": {"id": "gid://shopify/Blog/1", "handle": "learn", "title": "Learn"},
        "author": {"name": "F3 Energy Team"},
    }


def _staged():
    rec = pc.record_for_article(article=_article(), lane="learn",
                               excerpt="An excerpt.", rails_passed=11)
    return pc.stage_card(rec, client_factory=lambda: None)


def _body(handle, user=None):
    """A realistic Slack block_actions payload for this card."""
    _, blocks = pc.build_publish_blocks(pc.get_record(handle))
    return {
        "user": {"id": user or pc.HARRISON_ID},
        "channel": {"id": "D0HARRISON"},
        "message": {"ts": "1756200000.123456", "blocks": blocks},
        "actions": [{"action_id": pc.ACTION_PUBLISH, "value": handle}],
    }


def _kinds(blocks):
    return [b.get("type") for b in blocks]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_both_actions_are_actually_registered_not_orphaned_decorators():
    """A decorator on a function the app never registers is a silent dead
    handler -- that exact defect shipped once in this repo and the whole feature
    was dark in production while its tests passed."""
    names = {getattr(l.ack_function, "__name__", "") for l in app_mod.app._listeners}
    assert "handle_f3e_blog_publish" in names
    assert "handle_f3e_blog_dismiss" in names


# ---------------------------------------------------------------------------
# Unauthorized / race outcomes: ephemeral only, card untouched
# ---------------------------------------------------------------------------


def test_a_non_harrison_tap_never_edits_the_shared_card():
    rec = _staged()
    client = MagicMock()
    app_mod._handle_f3e_blog_tap(_body(rec["handle"], user=OTHER_USER), client,
                                action="publish")
    assert not client.chat_update.called
    client.chat_postEphemeral.assert_called_once()
    assert "Only Harrison" in client.chat_postEphemeral.call_args[1]["text"]
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PENDING


def test_an_orphaned_tap_is_ephemeral_only():
    client = MagicMock()
    body = {"user": {"id": pc.HARRISON_ID}, "channel": {"id": "D1"},
            "message": {"ts": "1.1", "blocks": []},
            "actions": [{"action_id": pc.ACTION_PUBLISH, "value": "blogpub-gone"}]}
    app_mod._handle_f3e_blog_tap(body, client, action="publish")
    assert not client.chat_update.called
    assert client.chat_postEphemeral.called


def test_the_race_loser_gets_an_ephemeral_and_the_winners_card_stands():
    rec = _staged()
    client = MagicMock()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")):
        app_mod._handle_f3e_blog_tap(_body(rec["handle"]), client, action="publish")
        first_updates = client.chat_update.call_count
        app_mod._handle_f3e_blog_tap(_body(rec["handle"]), client, action="publish")
    assert client.chat_update.call_count == first_updates, \
        "the second tap must not re-edit the resolved card"
    assert client.chat_postEphemeral.called


# ---------------------------------------------------------------------------
# Terminal outcomes: card is closed, buttons dropped
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_marketing_post(monkeypatch):
    """The wrapper posts to #f3-marketing on a real publish. Tests must never
    attempt that: without this the suite made a live chat.postMessage attempt
    (it failed on invalid_auth, which is luck, not a control)."""
    monkeypatch.setattr(pc, "post_marketing_note", lambda *a, **k: True)


def test_a_successful_publish_closes_the_card_and_drops_the_buttons():
    rec = _staged()
    client = MagicMock()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")):
        app_mod._handle_f3e_blog_tap(_body(rec["handle"]), client, action="publish")
    kw = client.chat_update.call_args[1]
    kinds = _kinds(kw["blocks"])
    assert "actions" not in kinds, "a published card must not stay tappable"
    assert kinds[0] == "section", "the outcome must lead, not trail in grey"
    body = "\n".join(b["text"]["text"] for b in kw["blocks"]
                     if b.get("type") == "section")
    assert "Published" in body
    # The card must no longer assert the state this tap just falsified.
    assert "ready to publish" not in body
    assert "Staged unpublished" not in body
    # ...but the content Harrison still needs survives.
    assert "A Test Post" in body
    assert kw["text"].strip()


def test_a_dismiss_closes_the_card_and_drops_the_buttons():
    rec = _staged()
    client = MagicMock()
    app_mod._handle_f3e_blog_tap(_body(rec["handle"]), client, action="dismiss")
    kinds = _kinds(client.chat_update.call_args[1]["blocks"])
    assert "actions" not in kinds
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_DISMISSED


def test_a_retryable_failure_keeps_the_buttons_so_it_can_be_retried():
    rec = _staged()
    client = MagicMock()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article",
                      side_effect=RuntimeError("blog locked")):
        app_mod._handle_f3e_blog_tap(_body(rec["handle"]), client, action="publish")
    kinds = _kinds(client.chat_update.call_args[1]["blocks"])
    assert "actions" in kinds, "nothing was published, so the tap must stay available"
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PENDING


def test_a_card_with_no_section_blocks_still_renders_something():
    """Never leave an empty message: downstream an empty body renders through
    `or "Done."` as a fabricated success."""
    rec = _staged()
    client = MagicMock()
    body = _body(rec["handle"])
    body["message"]["blocks"] = [{"type": "divider"}]
    app_mod._handle_f3e_blog_tap(body, client, action="dismiss")
    blocks = client.chat_update.call_args[1]["blocks"]
    assert blocks and blocks[0]["type"] == "section"
    assert blocks[0]["text"]["text"].strip()


# ---------------------------------------------------------------------------
# Side effects outside the DM
# ---------------------------------------------------------------------------


def test_only_a_real_publish_announces_to_the_channel():
    # patch.object below shadows the autouse stub and restores it afterwards, so
    # there is nothing to undo -- and undoing the fixtures would also have thrown
    # away the env isolation the other autouse fixture set up.
    rec = _staged()
    client = MagicMock()
    with patch.object(pc, "post_marketing_note") as note:
        app_mod._handle_f3e_blog_tap(_body(rec["handle"]), client, action="dismiss")
    assert not note.called

    rec2 = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")), \
         patch.object(pc, "post_marketing_note") as note2:
        app_mod._handle_f3e_blog_tap(_body(rec2["handle"]), client, action="publish")
    assert note2.called


def test_an_already_live_article_does_not_announce_a_publish_that_did_not_happen():
    rec = _staged()
    client = MagicMock()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(True)), \
         patch.object(pc, "post_marketing_note") as note:
        app_mod._handle_f3e_blog_tap(_body(rec["handle"]), client, action="publish")
    assert not note.called, "nothing was published just now, so nothing to announce"


# ---------------------------------------------------------------------------
# Kill switches
# ---------------------------------------------------------------------------


def test_buttons_off_does_not_mutate_the_card_and_points_at_a_real_route(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
    rec = _staged()
    client = MagicMock()
    with patch.object(pc, "process_tap",
                      side_effect=AssertionError("must not run")):
        app_mod._handle_f3e_blog_tap(_body(rec["handle"]), client, action="publish")
    assert not client.chat_update.called
    txt = client.chat_postEphemeral.call_args[1]["text"]
    assert "Shopify admin" in txt
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PENDING


def test_eval_mode_is_inert(monkeypatch):
    monkeypatch.setenv("CORA_EVAL_MODE", "1")
    rec = _staged()
    client = MagicMock()
    app_mod._handle_f3e_blog_tap(_body(rec["handle"]), client, action="publish")
    assert not client.chat_update.called and not client.chat_postEphemeral.called
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PENDING


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_a_slack_api_failure_does_not_crash_the_handler():
    """A handler exception must never take the bot down."""
    rec = _staged()
    client = MagicMock()
    client.chat_update.side_effect = RuntimeError("slack 500")
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "A Test Post")):
        app_mod._handle_f3e_blog_tap(_body(rec["handle"]), client, action="publish")
    # The publish still stands -- the card edit failing must not undo it.
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PUBLISHED


def test_a_malformed_payload_does_not_crash_the_handler():
    client = MagicMock()
    for body in ({}, {"actions": []}, {"actions": [{}], "user": {}},
                 {"user": {"id": pc.HARRISON_ID}, "actions": [{"value": None}]}):
        app_mod._handle_f3e_blog_tap(body, client, action="publish")
