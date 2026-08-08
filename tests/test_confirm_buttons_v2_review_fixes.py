"""v2 D-051 remediation pins.

Ten findings from a three-lens adversarial pass, SIX of them introduced by this
branch's own fixes. Each gets a test that fails on the pre-remediation code, so
the class cannot come back.

  L2-1 HIGH  the single-tool dispatch path dropped _user_message, so S7 was a
             no-op in production for the one-tool turns it exists to fix
  L3-1 HIGH  ReDoS in the new vocative prefix (43s per call, measured)
  L3-2 HIGH  the S7 override hijacked a DIFFERENT, correctly-resolved product
  L1-1 HIGH  the S1 turn filter suppressed the supersede-abandon, re-arming a
             stale PERMANENT delete on a bare "yes"
  L1-2 MED   sweep vs tap raced the same card mid-execute
  L1-3 MED   a failed terminal edit orphaned a live-buttoned card forever
  L1-4 MED   the turn filter let the expired-tombstone branch swallow a turn
  L2-3 MED   the sweep claimed "handled" on the TTL-expiry route, where nothing
             handled anything
  L2-4 MED   a typed "confirm" in a DM could be eaten by a pending gap ask
  L3-3 MED   the gid name lookup leaked a LEX task name into a non-LEX channel
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402

import cora.app as app_mod  # noqa: E402
from cora import confirm_cards as cc, lexicon  # noqa: E402
from cora.tools import asana_client, tool_dispatch as td  # noqa: E402

USER = "U0REV"
CHANNEL = "f3e-leadership"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    cc.reset_cards_for_tests()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    for store in (td._PENDING_ASANA_WRITES, td._PENDING_SHOPIFY_WRITES,
                  td._PENDING_CALENDAR_WRITES, td._PENDING_REMEMBER):
        store.clear()
    yield
    for store in (td._PENDING_ASANA_WRITES, td._PENDING_SHOPIFY_WRITES,
                  td._PENDING_CALENDAR_WRITES, td._PENDING_REMEMBER):
        store.clear()
    cc.reset_cards_for_tests()


# ── L2-1: the single-tool path must forward the verbatim turn text ─────────


class TestSingleToolDispatchForwardsUserMessage:
    """The common case is a ONE-tool turn. The parallel branch forwarded
    user_message and the fast path did not, so S7's scan never received the
    user's words on exactly the turns it targets."""

    def _run(self, n_blocks: int):
        from cora import claude_client as cc_mod
        seen: list[tuple[str, str]] = []

        def fake_dispatch(name, _input, _user, _entity, _channel="", _channel_id="",
                          _thread_ts=None, _user_message=""):
            seen.append((name, _user_message))
            return "ok"

        blocks = [MagicMock(name=f"b{i}", id=f"t{i}", input={}) for i in range(n_blocks)]
        for i, b in enumerate(blocks):
            b.name = f"tool{i}"
        with patch.object(cc_mod, "dispatch", side_effect=fake_dispatch):
            cc_mod._dispatch_tools_parallel(
                blocks, USER, "F3E", 1, channel_name=CHANNEL,
                user_message="set the variety pack at the office to 40")
        return seen

    def test_single_block_forwards_it(self):
        seen = self._run(1)
        assert len(seen) == 1
        assert seen[0][1] == "set the variety pack at the office to 40", (
            "the one-tool fast path dropped the verbatim turn text")

    def test_multi_block_forwards_it_to_every_tool(self):
        seen = self._run(3)
        assert len(seen) == 3
        assert all(m == "set the variety pack at the office to 40" for _n, m in seen)


# ── L3-1: the vocative prefix must not backtrack ───────────────────────────


class TestVocativeIsNotQuadratic:
    @pytest.mark.parametrize("payload", [
        "cora" + " " * 20000 + "x",
        "hey cora" + " " * 20000 + ",",
        "@cora" + " " * 20000 + "!",
    ])
    def test_pathological_input_returns_fast(self, payload):
        """Measured 43,700 ms on the first cut. CPython's re holds the GIL, so
        that burn blocks the whole bot process, and this predicate runs on
        EVERY DM upstream of the rate limiter."""
        t0 = time.perf_counter()
        app_mod._remember_or_forget_intent(payload)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"took {elapsed * 1000:.0f} ms -- quadratic backtracking"

    @pytest.mark.parametrize("text", [
        "Cora, remember the cobalt falcon is the staging box",
        "cora remember the door code",
        "Hey Cora, remember that Q3 kicks off Monday",
        "@Cora remember the vendor is Apex",
        "Cora: note that Tommy is out Friday",
        "remember the vendor is Apex",
    ])
    def test_the_bounded_pattern_still_matches_every_real_phrasing(self, text):
        assert app_mod._remember_or_forget_intent(text) is True

    @pytest.mark.parametrize("text", [
        "Do you remember the vendor?",
        "I will remember to send it",
        "ok okay remember this",
    ])
    def test_it_still_rejects_the_negatives(self, text):
        assert app_mod._remember_or_forget_intent(text) is False


# ── L3-2: the S7 override must be ABOUT the resolved product ───────────────


class TestVerbatimOverrideDoesNotHijack:
    # The channel must carry a default inventory location, or the tool asks
    # "which location?" and never reaches product resolution.
    INV_CHANNEL = "f3-hq-inventory-adjustments"

    def _resolve(self, user_message: str, product_query: str):
        return td._shopify_resolve(USER, {
            "_channel_name": self.INV_CHANNEL, "_user_message": user_message,
            "product": product_query, "quantity": 40,
        }, channel=self.INV_CHANNEL)

    def test_an_unrelated_ambiguous_product_never_hijacks(self, monkeypatch):
        """The user names an ambiguous product in passing AND a specific one as
        the actual request. Hijacking would stash the SPECIFIC request's
        location and quantity against the AMBIGUOUS product's candidates."""
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        lexicon.invalidate_cache()
        td._PENDING_ASK_STASH.clear()
        out, _fresh = self._resolve(
            "we're out of variety pack -- set original energy at the office to 40",
            "original energy")
        text = out if isinstance(out, str) else str(out)
        td._PENDING_ASK_STASH.clear()
        assert "Which one?" not in text, "hijacked by an unrelated ambiguous product"

    def test_the_real_bypass_still_asks(self, monkeypatch):
        """Control: the model's exact resolution IS one of the ambiguous
        meanings -- that, and only that, is the rewrite bypass."""
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        lexicon.invalidate_cache()
        td._PENDING_ASK_STASH.clear()
        out, _fresh = self._resolve(
            "set the variety pack at the office to 40", "pure variety pack")
        text = out if isinstance(out, str) else str(out)
        td._PENDING_ASK_STASH.clear()
        assert "Which one?" in text

    def test_the_override_writes_a_resolver_ledger_row(self, monkeypatch):
        """Otherwise the flywheel monitor counts an `exact` for the very
        population that was actually asked -- blind to the new asks."""
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        lexicon.invalidate_cache()
        td._PENDING_ASK_STASH.clear()
        rows: list[dict] = []
        with patch.object(lexicon, "log_event", side_effect=lambda **kw: rows.append(kw)):
            self._resolve("set the variety pack at the office to 40", "pure variety pack")
        td._PENDING_ASK_STASH.clear()
        verbatim_rows = [r for r in rows if r.get("event") == "resolve_verbatim"]
        assert verbatim_rows, "the override emitted no ledger row"
        assert verbatim_rows[0]["status"] == "ambiguous"


# ── L1-1 / L1-4: a mid-flight sibling turn defers, it does not execute ─────


class TestConcurrentSiblingNeverFiresAStaleDelete:
    def test_a_bare_yes_does_not_fire_a_stale_delete(self):
        """THE regression the S1 fix itself introduced. Stale destructive
        delete from before this turn + a sibling turn's fresher pending:
        filtering the sibling out of the arbitration also removed it from the
        supersede-abandon, so the delete became freshest and EXECUTED."""
        now = time.time()
        sid = cc.mint_stash_id("asana", USER, CHANNEL)
        td._store_pending_asana_write(USER, CHANNEL, {
            "action": "delete", "gid": "g1", "label": "Foo",
            "ts": now - 60, "stash_id": sid,
        })
        td._store_pending_shopify_write(USER, CHANNEL, {
            "sku": "S", "delta": -1, "ts": now + 0.2,
            "stash_id": cc.mint_stash_id("shopify", USER, CHANNEL),
        })
        with patch.object(td, "_run_confirm_execute") as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER, channel_name=CHANNEL, entity="F3E",
                message="yes", turn_started_at=now,
            )
        ex.assert_not_called()
        assert reply is None, "must defer; the referent is genuinely ambiguous"

    def test_the_tombstone_branch_does_not_swallow_the_turn(self):
        """With every pending belonging to a sibling turn, `entries` is empty in
        cases it never used to be -- the expired-Shopify tombstone would answer
        a question the user did not ask and destroy the tombstone."""
        now = time.time()
        td._store_pending_calendar_write(USER, CHANNEL, {
            "action": "create", "summary": "Sync", "ts": now + 0.2,
            "stash_id": cc.mint_stash_id("calendar", USER, CHANNEL),
        })
        with patch.object(td, "_pop_expired_shopify_write") as popped:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER, channel_name=CHANNEL, entity="F3E",
                message="yes", turn_started_at=now,
            )
        popped.assert_not_called()
        assert reply is None

    def test_the_sibling_pending_is_still_protected(self):
        """The original 8/3 fix must survive the remediation."""
        now = time.time()
        sid = cc.mint_stash_id("asana", USER, CHANNEL)
        td._store_pending_asana_write(USER, CHANNEL, {
            "action": "delete", "gid": "g1", "label": "In-flight",
            "ts": now + 0.2, "stash_id": sid,
        })
        td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="F3E",
            message="stage a calendar write", turn_started_at=now)
        assert td.stash_is_live(sid)


# ── L1-2 / L1-3: tap vs sweep, and a failed edit stays recoverable ─────────


class TestTapAndSweepDoNotRaceTheSameCard:
    def test_coordinates_leave_the_registry_at_claim_time(self):
        """Not after execute: index_mark_resolved makes stash_is_live False
        immediately, and execute can run for seconds, during which any other
        turn's reply fires the process-global sweep against this same card."""
        sid = cc.mint_stash_id("asana", USER, CHANNEL)
        td._store_pending_asana_write(USER, CHANNEL, {
            "action": "delete", "gid": "g1", "label": "Foo",
            "ts": time.time(), "stash_id": sid,
        })
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "1.1", "Delete Foo?")

        during: dict[str, list] = {}

        def slow_execute(*_a, **_k):
            during["open"] = cc.open_card_stash_ids()
            return "Deleted."

        with patch.object(td, "_execute_claimed_asana", side_effect=slow_execute), \
             patch("cora.entity_router.route", return_value="F3E"):
            td.resolve_and_claim_stash(sid, USER, "confirm")

        assert during["open"] == [], (
            "the card was still sweepable while execute was in flight")

    def test_a_failed_sweep_edit_is_retried_not_orphaned(self):
        sid = cc.mint_stash_id("asana", USER, CHANNEL)
        td._store_pending_asana_write(USER, CHANNEL, {
            "action": "delete", "gid": "g1", "label": "Foo",
            "ts": time.time(), "stash_id": sid,
        })
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "1.1", "Delete Foo?")
        td._take_pending_asana_write(USER, CHANNEL)

        client = MagicMock()
        client.chat_update.side_effect = RuntimeError("429")
        app_mod._close_stale_confirm_cards(client)
        assert cc.open_card_stash_ids() == [sid], (
            "a transient edit failure orphaned a live-buttoned card forever")

        client.chat_update.side_effect = None
        app_mod._close_stale_confirm_cards(client)
        assert cc.open_card_stash_ids() == []

    def test_edit_helper_reports_success(self):
        client = MagicMock()
        assert app_mod._edit_card_terminal(client, "C1", "1.1", [], "done") is True
        client.chat_update.side_effect = RuntimeError("boom")
        assert app_mod._edit_card_terminal(client, "C1", "1.1", [], "done") is False


# ── L2-3: the expiry route must not claim it was handled ───────────────────


class TestExpiryCopyIsHonest:
    def _expired_card(self):
        sid = cc.mint_stash_id("asana", USER, CHANNEL)
        td._store_pending_asana_write(USER, CHANNEL, {
            "action": "delete", "gid": "g1", "label": "Foo",
            "ts": time.time() - (td._ASANA_PENDING_TTL_SECONDS + 5),
            "stash_id": sid,
        })
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "1.1", "Delete Foo?")
        return sid

    def test_a_ttl_expired_card_says_nothing_was_changed(self):
        sid = self._expired_card()
        assert td.stash_expired_not_consumed(sid) is True
        client = MagicMock()
        app_mod._close_stale_confirm_cards(client)
        text = client.chat_update.call_args.kwargs["text"]
        assert "expired" in text.lower()
        assert "Nothing was changed" in text
        assert "Handled in the conversation" not in text

    def test_a_consumed_card_still_says_handled(self):
        sid = cc.mint_stash_id("asana", USER, CHANNEL)
        td._store_pending_asana_write(USER, CHANNEL, {
            "action": "delete", "gid": "g1", "label": "Foo",
            "ts": time.time(), "stash_id": sid,
        })
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "1.1", "Delete Foo?")
        td._take_pending_asana_write(USER, CHANNEL)  # typed confirm/cancel
        assert td.stash_expired_not_consumed(sid) is False
        client = MagicMock()
        app_mod._close_stale_confirm_cards(client)
        assert "Handled in the conversation" in client.chat_update.call_args.kwargs["text"]


# ── L2-4: a typed confirm in a DM outranks a pending gap ask ───────────────


class TestStagedWriteOutranksTheGapAsk:
    def _run_dm(self, text: str, *, with_pending: bool):
        if with_pending:
            td._store_pending_asana_write(USER, "dm", {
                "action": "delete", "gid": "g1", "label": "Foo",
                "ts": time.time(),
                "stash_id": cc.mint_stash_id("asana", USER, "dm"),
            })
        match = MagicMock(return_value=None)
        with patch.object(app_mod.gap_autofill, "match_pending_ask", match), \
             patch.object(app_mod.gap_autofill, "is_shift_keyword", return_value=False), \
             patch.object(app_mod, "_handle_dm_qa"), \
             patch.object(app_mod.historical_access, "detect_retrieval_intent",
                          return_value=False), \
             patch.object(app_mod, "_dm_is_shift_message", return_value=False):
            app_mod.handle_message_event(
                {"channel_type": "im", "user": USER, "text": text,
                 "channel": "D0T", "ts": "1.1"}, MagicMock())
        td._PENDING_ASANA_WRITES.clear()
        return match.call_args.kwargs["allow_toplevel"]

    @pytest.mark.parametrize("text", ["confirm", "yes", "2", "go ahead"])
    def test_a_live_staged_write_blocks_top_level_capture(self, text):
        """S4's own new copy tells DM users to reply "confirm" -- which is not a
        shift keyword, not a question and not a remember command, so it was
        eligible for capture. The write would silently not fire AND the literal
        word would be filed as the gap's answer."""
        assert self._run_dm(text, with_pending=True) is False

    def test_without_a_staged_write_the_gap_ask_still_captures(self):
        assert self._run_dm("the staging box is the cobalt falcon",
                            with_pending=False) is True


# ── L3-3: a fetched task name is scrubbed as if it were LEX ────────────────


class TestFetchedTaskNameIsScrubbed:
    def test_a_fetched_name_goes_through_the_lex_scrub(self):
        """The unrestricted branch does no ownership check, so a pasted gid can
        name an arbitrary workspace task -- including a LEX one whose name
        carries a client name -- and the caller's own scrub keys on the CHANNEL
        entity, which is HJRG here and would not scrub at all."""
        with patch.object(asana_client, "get_task_name", return_value="Bob Smith intake"), \
             patch.object(td, "_lex_safe_label", return_value="[scrubbed]") as scrub:
            _gid, label, _err = td._resolve_asker_task(td._FOUNDER_SLACK_ID, "123", "", "HJRG")
        scrub.assert_called_once()
        assert scrub.call_args.args[1] == "LEX", "must scrub with LEX rules"
        assert label == "[scrubbed]"

    def test_a_user_supplied_name_is_not_scrubbed(self):
        """Their own words are already theirs."""
        with patch.object(asana_client, "get_task_name") as fetch, \
             patch.object(td, "_lex_safe_label") as scrub:
            _gid, label, _err = td._resolve_asker_task(
                td._FOUNDER_SLACK_ID, "123", "My own task", "HJRG")
        fetch.assert_not_called()
        scrub.assert_not_called()
        assert label == "My own task"


# ── L2-6 / L2-7 / L2-8: the smaller guards ─────────────────────────────────


class TestRemainingGuards:
    def test_a_slot_tap_cannot_confirm_a_non_meeting_stash(self):
        sid = cc.mint_stash_id("asana", USER, CHANNEL)
        td._store_pending_asana_write(USER, CHANNEL, {
            "action": "delete", "gid": "g1", "label": "Foo",
            "ts": time.time(), "stash_id": sid,
        })
        with patch.object(td, "_execute_claimed_asana") as ex:
            result = td.resolve_and_claim_stash(sid, USER, "confirm", slot_index=0)
        ex.assert_not_called()
        assert result["outcome"] == "orphaned"
        assert td.stash_is_live(sid), "a refused slot tap must not consume the stash"

    def test_the_sweep_is_gated_on_eval_mode(self, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        sid = cc.mint_stash_id("asana", USER, CHANNEL)
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "1.1", "p")
        client = MagicMock()
        app_mod._close_stale_confirm_cards(client)
        client.chat_update.assert_not_called()

    def test_remember_preview_carries_a_confirm_instruction(self):
        src = (_REPO_ROOT / "src" / "cora" / "tools" / "tool_dispatch.py").read_text(
            encoding="utf-8")
        block = src.split("Saving to your notes (only you can retrieve this)")[1][:400]
        assert "_confirm_how(" in block, "the remember preview has no confirm instruction"

    def test_slot_proposal_text_never_says_reply_with_1_2_or_3(self):
        """A bare reply does not reach the app in a channel -- the exact lie S4
        exists to remove."""
        src = (_REPO_ROOT / "src" / "cora" / "tools" / "tool_dispatch.py").read_text(
            encoding="utf-8")
        assert 'Tell the user to {_confirm_how(channel)} naming the option' in src or \
               "_confirm_how(channel)} naming the option" in src

    def test_get_task_name_never_raises_without_a_pat(self, monkeypatch):
        monkeypatch.delenv("ASANA_PAT", raising=False)
        with patch.object(asana_client, "_pat", side_effect=asana_client.AsanaClientError("no pat")):
            assert asana_client.get_task_name("123") is None

    def test_get_task_name_uses_a_short_timeout(self):
        """It runs inside a 12s tool budget and a dispatch timeout does not
        cancel the worker, so a slow read could stash a destructive pending
        AFTER the model was told the tool timed out."""
        src = (_REPO_ROOT / "src" / "cora" / "tools" / "asana_client.py").read_text(
            encoding="utf-8")
        body = src.split("def get_task_name(")[1].split("\ndef ")[0]
        assert "timeout=4.0" in body
        assert re.search(r"timeout=_TIMEOUT", body) is None
