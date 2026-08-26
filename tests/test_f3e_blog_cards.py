"""One-tap publish cards for staged F3E blog articles (S4).

Two families of test here:

  * the AUTHORITY / IDEMPOTENCY / READ-BACK behaviour of `process_tap`, which is
    where every correctness promise of this lane actually lives, and
  * the CARD COPY contract, asserted against the very same token lists the
    Class-B directive-prose guard uses, imported rather than restated, so the two
    cannot drift apart.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock, patch

import pytest

from cora.f3e_blog import publish_cards as pc
from cora.f3e_blog import preflight

# Imported, NOT restated: a copy of these lists would be a second source of
# truth that silently goes stale the next time the guard's vocabulary grows.
from test_classb_directive_prose import DIRECTIVE_TOKENS, SENTINEL_TOKENS

OTHER_USER = "U0B3AEJCYGP"  # Justin -- a real non-Harrison id


def code_only(module_or_fn) -> str:
    """Source with comments and docstrings REMOVED.

    A source pin that greps raw text matches the comment explaining why a thing is
    NOT used, which is the opposite of what the pin is asserting -- and it makes
    documenting a deliberate omission fail the build. Every pin below scans code.
    """
    import ast
    import textwrap
    src = textwrap.dedent(inspect.getsource(module_or_fn))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_F3E_BLOG_CARDS_PATH", str(tmp_path / "cards.json"))
    monkeypatch.setenv("CORA_F3E_BLOG_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("SHOPIFY_F3E_STORE", "f3energy.myshopify.com")
    monkeypatch.setenv("SHOPIFY_F3E_ACCESS_TOKEN", "shpat_test")
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    yield


def _article(published=False, title="A Test Post"):
    return {
        "id": "gid://shopify/Article/777", "title": title, "handle": "a-test-post",
        "summary": "An excerpt.", "isPublished": published, "publishedAt": None,
        "tags": ["Learn"], "blog": {"id": "gid://shopify/Blog/122516767040",
                                    "handle": "learn", "title": "Learn"},
        "author": {"name": "F3 Energy Team"},
    }


def _staged(**kw):
    rec = pc.record_for_article(
        article=_article(), lane="learn", excerpt="An excerpt.",
        backlog_row="4", rails_passed=11)
    rec.update(kw)
    return pc.stage_card(rec, client_factory=lambda: None)


def _ledger_events(tmp_path):
    p = tmp_path / "ledger.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["publish", "dismiss"])
def test_only_harrison_can_act(action):
    rec = _staged()
    outcome, msg = pc.process_tap(rec["handle"], OTHER_USER, action=action)
    assert outcome == "not_authorized"
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PENDING
    assert msg.strip()


def test_refusal_does_not_leak_whether_the_draft_exists():
    """A stranger's tap must not become an existence oracle for staged content:
    the same text comes back for a real handle and a made-up one."""
    rec = _staged()
    real = pc.process_tap(rec["handle"], OTHER_USER, action="publish")
    fake = pc.process_tap("blogpub-doesnotexist", OTHER_USER, action="publish")
    assert real == fake


def test_authority_is_checked_before_any_shopify_call():
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article") as g, \
         patch.object(pc.shopify_client, "publish_article") as p:
        pc.process_tap(rec["handle"], OTHER_USER, action="publish")
    assert not g.called and not p.called


def test_authority_is_harrison_in_code_not_via_review_lanes():
    """review_lanes.can_approve guarantees structurally that a non-Harrison actor
    is only ever admitted on the MECHANICAL lane. Publishing to the public web is
    not mechanical, so this lane must not be routed through it."""
    src = code_only(pc)
    assert "review_lanes" not in src
    assert "HARRISON_ID" in src
    assert "actor_id != HARRISON_ID" in src


def test_no_yaml_or_roster_can_grant_the_publish_tap():
    src = code_only(pc)
    for granting in ("yaml", "approvers", "allowlist", "roster"):
        assert granting not in src.lower(), granting


# ---------------------------------------------------------------------------
# Publish: read-back, and what happens when it fails
# ---------------------------------------------------------------------------


def test_publish_flips_verifies_and_records():
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article",
                      return_value=_article(True)) as pub, \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "<h1>A Test Post</h1>")):
        outcome, msg = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert outcome == "published"
    assert pub.called
    stored = pc.get_record(rec["handle"])
    assert stored["state"] == pc.STATE_PUBLISHED
    assert stored["public_verified"] is True
    assert stored["public_url"] == "https://f3energy.com/blogs/learn/a-test-post"
    assert "serving" in msg


def test_a_failed_publish_leaves_the_row_pending_and_says_so():
    """Apply-first-then-record: a failed publish must never read as approved, and
    the card must stay re-tappable."""
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article",
                      side_effect=RuntimeError("userErrors: blog is locked")):
        outcome, msg = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert outcome == "failed"
    assert outcome in pc.RETRYABLE_OUTCOMES
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PENDING
    assert "did NOT go through" in msg
    assert "still a draft" in msg


def test_an_unreachable_precheck_does_not_publish():
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article",
                      side_effect=RuntimeError("network")), \
         patch.object(pc.shopify_client, "publish_article") as pub:
        outcome, msg = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert outcome == "failed"
    assert not pub.called
    assert "did NOT publish" in msg
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PENDING


def test_a_published_article_whose_public_page_fails_is_reported_honestly():
    """The write DID happen, so this is not a failure -- but it must not claim the
    page is serving either. On this store a theme template can ignore an API
    write entirely, so mutation success is not evidence of a rendered page."""
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page", return_value=(404, "")):
        outcome, msg = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert outcome == "published"
    stored = pc.get_record(rec["handle"])
    assert stored["state"] == pc.STATE_PUBLISHED
    assert stored["public_verified"] is False
    assert "could not confirm" in msg
    assert "live-but-unverified" in msg


def test_a_200_page_missing_the_title_is_not_treated_as_verified():
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page",
                      return_value=(200, "<h1>404 not found</h1>")):
        outcome, msg = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert outcome == "published"
    assert pc.get_record(rec["handle"])["public_verified"] is False


def test_an_article_already_live_is_not_claimed_as_this_taps_work():
    """Harrison publishes from admin too. The card must say what is true."""
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "publish_article") as pub:
        outcome, msg = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert outcome == "already_live"
    assert not pub.called
    assert "already live" in msg
    assert "Nothing changed just now" in msg


# ---------------------------------------------------------------------------
# Dismiss + idempotency
# ---------------------------------------------------------------------------


def test_dismiss_leaves_the_article_unpublished():
    rec = _staged()
    with patch.object(pc.shopify_client, "publish_article") as pub:
        outcome, msg = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="dismiss")
    assert outcome == "dismissed"
    assert not pub.called
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_DISMISSED
    assert "unpublished" in msg


def test_a_dismissed_article_is_never_re_carded():
    """The spec's "no re-card loop": the gid stays in the carded set forever."""
    rec = _staged()
    pc.process_tap(rec["handle"], pc.HARRISON_ID, action="dismiss")
    assert rec["article_gid"] in pc.already_carded_gids()


def test_a_second_tap_is_already_handled_not_a_second_publish():
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article",
                      return_value=_article(True)) as pub, \
         patch.object(pc.shopify_client, "fetch_public_page", return_value=(200, "A Test Post")):
        first = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
        second = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert first[0] == "published"
    assert second[0] == "already_handled"
    assert pub.call_count == 1


def test_already_handled_is_distinct_from_orphaned():
    rec = _staged()
    pc.process_tap(rec["handle"], pc.HARRISON_ID, action="dismiss")
    handled = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    orphan = pc.process_tap("blogpub-nope", pc.HARRISON_ID, action="publish")
    assert handled[0] == "already_handled"
    assert orphan[0] == "orphaned"
    assert handled[1] != orphan[1]


def test_dismiss_after_publish_does_not_unpublish_anything():
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page", return_value=(200, "A Test Post")):
        pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    outcome, _ = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="dismiss")
    assert outcome == "already_handled"
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PUBLISHED


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def test_the_ledger_records_staging_and_the_outcome(tmp_path):
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page", return_value=(200, "A Test Post")):
        pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    events = [e["event"] for e in _ledger_events(tmp_path)]
    assert "staged" in events and "published" in events
    row = [e for e in _ledger_events(tmp_path) if e["event"] == "published"][0]
    assert row["article_gid"] == "gid://shopify/Article/777"
    assert row["backlog_row"] == "4"


def test_a_failed_publish_is_also_on_the_ledger(tmp_path):
    rec = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article",
                      side_effect=RuntimeError("nope")):
        pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert "publish_failed" in [e["event"] for e in _ledger_events(tmp_path)]


def test_a_ledger_failure_does_not_break_the_outcome(monkeypatch):
    rec = _staged()
    monkeypatch.setattr(pc, "ledger_path",
                        lambda: (_ for _ in ()).throw(OSError("disk")))
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page", return_value=(200, "A Test Post")):
        outcome, _ = pc.process_tap(rec["handle"], pc.HARRISON_ID, action="publish")
    assert outcome == "published"


# ---------------------------------------------------------------------------
# Card copy contract
# ---------------------------------------------------------------------------


def _all_card_text(blocks) -> str:
    out = []
    for b in blocks:
        if b.get("type") == "section":
            out.append(b["text"]["text"])
        elif b.get("type") == "context":
            out.extend(e.get("text", "") for e in b.get("elements", []))
        elif b.get("type") == "actions":
            out.extend(e["text"]["text"] for e in b["elements"])
    return "\n".join(out)


def _every_human_string() -> list[tuple[str, str]]:
    """Every string this module can put in front of Harrison, success AND failure.

    Enumerating only success payloads is how directive prose reached humans on six
    Class-B kinds: a failure return carries no sentinel by construction, so any
    belt keyed on one fails open exactly where the user most needs the truth.
    """
    rec = _staged()
    strings: list[tuple[str, str]] = []
    for label, builder in (("card", pc.build_publish_blocks),
                           ("buttons_off_card", pc.build_buttons_off_blocks)):
        fallback, blocks = builder(pc.get_record(rec["handle"]))
        strings.append((label + ":fallback", fallback))
        strings.append((label + ":blocks", _all_card_text(blocks)))
    strings.append(("marketing_note", pc.marketing_note(pc.get_record(rec["handle"]))))

    # Every outcome branch of process_tap.
    scenarios = {
        "not_authorized": (OTHER_USER, "publish", {}),
        "orphaned": (pc.HARRISON_ID, "publish", {"handle": "blogpub-nope"}),
    }
    for label, (actor, action, kw) in scenarios.items():
        h = kw.get("handle", rec["handle"])
        strings.append(("tap:" + label, pc.process_tap(h, actor, action=action)[1]))

    fresh = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article",
                      side_effect=RuntimeError("boom")):
        strings.append(("tap:failed", pc.process_tap(
            fresh["handle"], pc.HARRISON_ID, action="publish")[1]))
    with patch.object(pc.shopify_client, "get_article",
                      side_effect=RuntimeError("net")):
        strings.append(("tap:precheck_failed", pc.process_tap(
            fresh["handle"], pc.HARRISON_ID, action="publish")[1]))
    with patch.object(pc.shopify_client, "get_article", return_value=_article(True)):
        strings.append(("tap:already_live", pc.process_tap(
            fresh["handle"], pc.HARRISON_ID, action="publish")[1]))

    d = _staged()
    strings.append(("tap:dismissed", pc.process_tap(
        d["handle"], pc.HARRISON_ID, action="dismiss")[1]))
    strings.append(("tap:already_handled", pc.process_tap(
        d["handle"], pc.HARRISON_ID, action="dismiss")[1]))

    p = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page", return_value=(200, "A Test Post")):
        strings.append(("tap:published", pc.process_tap(
            p["handle"], pc.HARRISON_ID, action="publish")[1]))
    q = _staged()
    with patch.object(pc.shopify_client, "get_article", return_value=_article(False)), \
         patch.object(pc.shopify_client, "publish_article", return_value=_article(True)), \
         patch.object(pc.shopify_client, "fetch_public_page", return_value=(500, "")):
        strings.append(("tap:published_unverified", pc.process_tap(
            q["handle"], pc.HARRISON_ID, action="publish")[1]))
    return strings


def test_no_sentinel_or_directive_prose_reaches_a_human_on_any_path():
    for label, text in _every_human_string():
        assert text is not None, "%s: None reached a human surface" % label
        for tok in SENTINEL_TOKENS:
            assert tok not in text, "%s: sentinel %r leaked: %s" % (label, tok, text)
        low = text.lower()
        for tok in DIRECTIVE_TOKENS:
            assert tok not in low, "%s: directive %r leaked: %s" % (label, tok, text)


def test_no_outcome_text_is_ever_empty():
    """An empty message renders downstream through `or "Done."` as a fabricated
    success on an action that may have failed."""
    for label, text in _every_human_string():
        assert text and text.strip(), label


def test_the_card_names_what_the_preflight_did_not_check():
    """Harrison decides at the card, so the card is where the gap belongs. A green
    preflight must never read as full clearance."""
    rec = _staged()
    _, blocks = pc.build_publish_blocks(pc.get_record(rec["handle"]))
    text = _all_card_text(blocks)
    assert "Not machine checked" in text
    for rail in preflight.UNENFORCED_RAILS:
        assert rail in text


def test_the_card_states_that_cora_never_publishes_on_her_own():
    rec = _staged()
    _, blocks = pc.build_publish_blocks(pc.get_record(rec["handle"]))
    assert "yours alone" in _all_card_text(blocks)


def test_button_values_carry_the_opaque_handle_only():
    """No payload and no echoed content in a button value -- the value is an
    authorisation token, and the server owns what it authorises."""
    rec = _staged()
    _, blocks = pc.build_publish_blocks(pc.get_record(rec["handle"]))
    actions = [b for b in blocks if b["type"] == "actions"][0]
    for el in actions["elements"]:
        assert el["value"] == rec["handle"]
        assert el["value"].startswith("blogpub-")


def test_the_buttons_off_card_promises_no_affordance_it_cannot_honour():
    """A card that keeps advertising a dead affordance gets acted on -- that
    happened 11 times on one card in this repo. With buttons off there is no
    Publish button and no typed command claim, only the admin route."""
    rec = _staged()
    _, blocks = pc.build_buttons_off_blocks(pc.get_record(rec["handle"]))
    assert not [b for b in blocks if b.get("type") == "actions"]
    text = _all_card_text(blocks)
    assert "Shopify admin" in text
    assert "reply" not in text.lower()
    assert "type" not in text.lower().split("admin")[0]


def test_the_card_body_is_chunked_so_it_cannot_silently_truncate():
    rec = pc.get_record(_staged()["handle"])
    rec["excerpt"] = "x" * 9000
    _, blocks = pc.build_publish_blocks(rec)
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) > 1
    for s in sections:
        assert len(s["text"]["text"]) <= 3000


def test_card_text_is_sanitized_at_construction():
    """Block Kit bodies bypass the class-level WebClient egress patch, which only
    covers text= -- so the sanitizer must run here or it never runs at all."""
    src = code_only(pc)
    assert "slack_egress.sanitize_text" in src


# ---------------------------------------------------------------------------
# Staging / process boundary
# ---------------------------------------------------------------------------


def test_the_pending_store_is_on_disk_not_in_process_memory():
    """The card is minted by a scheduled SCRIPT and tapped in the always-on BOT
    process. An in-memory stash index would make every tap read as orphaned."""
    src = code_only(pc)
    assert "mint_stash_id" not in src
    rec = _staged()
    assert pc.pending_path().exists()
    assert rec["handle"] in json.loads(pc.pending_path().read_text(encoding="utf-8"))


def test_the_record_is_persisted_even_when_the_dm_fails():
    """Fail toward the recoverable side: a record with no card is re-offerable, a
    card with no record is untappable."""
    client = MagicMock()
    client.conversations_open.side_effect = RuntimeError("slack down")
    rec = pc.record_for_article(article=_article(), lane="learn", excerpt="e")
    pc.stage_card(rec, client_factory=lambda: client)
    assert pc.get_record(rec["handle"])["state"] == pc.STATE_PENDING


def test_stage_card_dms_harrison_and_stores_the_message_coords():
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D123"}}
    client.chat_postMessage.return_value = {"ts": "111.222"}
    rec = pc.record_for_article(article=_article(), lane="learn", excerpt="e")
    out = pc.stage_card(rec, client_factory=lambda: client)
    assert client.conversations_open.call_args[1]["users"] == [pc.HARRISON_ID]
    assert out["dm_channel_id"] == "D123" and out["dm_message_ts"] == "111.222"


def test_stage_card_uses_the_buttons_off_variant_when_buttons_are_off(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D1"}}
    client.chat_postMessage.return_value = {"ts": "1"}
    rec = pc.record_for_article(article=_article(), lane="learn", excerpt="e")
    pc.stage_card(rec, client_factory=lambda: client)
    blocks = client.chat_postMessage.call_args[1]["blocks"]
    assert not [b for b in blocks if b.get("type") == "actions"]


def test_the_marketing_note_goes_to_f3_marketing_not_f3_hq():
    """#f3-hq does not exist in the workspace; the interim task's first fire
    proved it."""
    assert pc.MARKETING_CHANNEL == "C0B4V8BGJSJ"
    client = MagicMock()
    rec = pc.get_record(_staged()["handle"])
    assert pc.post_marketing_note(rec, client_factory=lambda: client) is True
    assert client.chat_postMessage.call_args[1]["channel"] == "C0B4V8BGJSJ"


def test_a_failed_marketing_note_does_not_raise():
    client = MagicMock()
    client.chat_postMessage.side_effect = RuntimeError("channel gone")
    rec = pc.get_record(_staged()["handle"])
    assert pc.post_marketing_note(rec, client_factory=lambda: client) is False


def test_no_expiry_clock_was_invented():
    """Harrison has set no TTL for this lane. Live state is re-read at tap time
    instead, so outcomes are decided by verified state rather than a guessed
    clock -- which is also what keeps them honest when he publishes from admin."""
    src = code_only(pc.process_tap)
    assert "expired" not in src
    assert "get_article" in src  # the live re-read that replaces the clock


def test_publish_article_is_only_reachable_from_the_tap():
    """The lane's central promise. No drafting/LLM path may call it."""
    from cora.f3e_blog import pipeline as pl  # noqa: PLC0415
    assert "publish_article" not in code_only(pl)
    assert "publish_article" in code_only(pc.process_tap)
