"""v2 S1: rendered-card lifecycle -- one stash gets exactly ONE live card, and
any terminal state takes that card's buttons down no matter which route reached
it (typed confirm, typed cancel, supersede, expiry, tap).

v1 could only close a card from a tap on that same card, so a TYPED confirm --
which consumes the per-kind store entry and never touches confirm_cards at all
-- left live-looking Confirm/Cancel buttons over a stash that no longer existed
(cq-fee6c9764950). Nothing stopped a second live card for the same stash either.

Covers: claim_card_attach one-shot, register/pop coordinate round-trip,
stash_is_live across every terminal route, the _close_stale_confirm_cards sweep
(including its flag-off no-op), and a real 3-thread concurrent-turn repro of the
asana+shopify+calendar trio (cq-db3b28dcdd42) asserting N turns -> N replies ->
N correctly-bound distinct cards.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402

import cora.app as app_mod  # noqa: E402
from cora import confirm_cards as cc  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

USER_ID = "U0CARD"
CHANNEL_NAME = "f3e-leadership"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    cc.reset_cards_for_tests()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    with cc._ASK_INDEX_LOCK:
        cc._ASK_INDEX.clear()
    td._PENDING_ASANA_WRITES.clear()
    td._PENDING_SHOPIFY_WRITES.clear()
    td._PENDING_CALENDAR_WRITES.clear()
    yield
    cc.reset_cards_for_tests()
    td._PENDING_ASANA_WRITES.clear()
    td._PENDING_SHOPIFY_WRITES.clear()
    td._PENDING_CALENDAR_WRITES.clear()


def _mint_asana(user=USER_ID, channel=CHANNEL_NAME, label="Test task"):
    sid = cc.mint_stash_id("asana", user, channel)
    td._store_pending_asana_write(user, channel, {
        "action": "delete", "gid": "g1", "label": label,
        "ts": time.time(), "stash_id": sid,
    })
    return sid


# ── claim_card_attach: one stash, one live card ────────────────────────────


class TestClaimCardAttach:
    def test_first_claim_wins_second_refused(self):
        assert cc.claim_card_attach("abc123") is True
        assert cc.claim_card_attach("abc123") is False

    def test_distinct_stashes_each_claim(self):
        assert cc.claim_card_attach("aaa") is True
        assert cc.claim_card_attach("bbb") is True

    def test_empty_stash_id_never_claims(self):
        assert cc.claim_card_attach("") is False

    def test_claim_survives_pop_so_no_second_live_card(self):
        """A closed card must not be silently replaced by a fresh live one for
        the same stash -- the attach claim is deliberately NOT released by pop."""
        assert cc.claim_card_attach("sid1") is True
        cc.register_card("sid1", "C1", "111.1", "preview")
        assert cc.pop_cards("sid1") == [("C1", "111.1", "preview")]
        assert cc.claim_card_attach("sid1") is False

    def test_concurrent_claims_exactly_one_winner(self):
        results: list[bool] = []
        lock = threading.Lock()

        def worker():
            got = cc.claim_card_attach("race-sid")
            with lock:
                results.append(got)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(results) == 1, "exactly one thread may card a given stash"


class TestCardRegistry:
    def test_register_then_pop_round_trip(self):
        cc.claim_card_attach("s")
        cc.register_card("s", "C9", "222.2", "the preview text")
        assert cc.open_card_stash_ids() == ["s"]
        assert cc.pop_cards("s") == [("C9", "222.2", "the preview text")]

    def test_pop_is_once_only(self):
        cc.claim_card_attach("s")
        cc.register_card("s", "C9", "222.2", "p")
        cc.pop_cards("s")
        assert cc.pop_cards("s") == []
        assert cc.open_card_stash_ids() == []

    def test_register_ignores_incomplete_coordinates(self):
        cc.claim_card_attach("s")
        cc.register_card("s", "", "222.2", "p")
        cc.register_card("s", "C9", "", "p")
        cc.register_card("", "C9", "222.2", "p")
        assert cc.pop_cards("s") == []

    def test_duplicate_register_is_idempotent(self):
        cc.claim_card_attach("s")
        cc.register_card("s", "C9", "222.2", "p")
        cc.register_card("s", "C9", "222.2", "p")
        assert len(cc.pop_cards("s")) == 1


# ── stash_is_live: the terminal-state predicate every route ends in ────────


class TestStashIsLive:
    def test_fresh_stash_is_live(self):
        sid = _mint_asana()
        assert td.stash_is_live(sid) is True

    def test_unknown_id_is_not_live(self):
        assert td.stash_is_live("never-existed") is False
        assert td.stash_is_live("") is False

    def test_typed_take_makes_it_dead(self):
        sid = _mint_asana()
        td._take_pending_asana_write(USER_ID, CHANNEL_NAME)
        assert td.stash_is_live(sid) is False

    def test_superseded_by_fresher_same_kind_is_dead(self):
        old = _mint_asana(label="old")
        new = _mint_asana(label="new")
        assert old != new
        assert td.stash_is_live(old) is False
        assert td.stash_is_live(new) is True

    def test_index_resolved_is_dead_even_if_store_entry_lingers(self):
        sid = _mint_asana()
        cc.index_mark_resolved(sid)
        assert td.stash_is_live(sid) is False

    def test_expired_ttl_is_dead(self):
        sid = cc.mint_stash_id("asana", USER_ID, CHANNEL_NAME)
        td._store_pending_asana_write(USER_ID, CHANNEL_NAME, {
            "action": "delete", "gid": "g1", "label": "stale",
            "ts": time.time() - (td._ASANA_PENDING_TTL_SECONDS + 5),
            "stash_id": sid,
        })
        assert td.stash_is_live(sid) is False

    def test_button_claim_makes_it_dead(self):
        sid = _mint_asana()
        with patch.object(td, "_execute_claimed_asana", return_value="Deleted."), \
             patch("cora.entity_router.route", return_value="F3E"):
            td.resolve_and_claim_stash(sid, USER_ID, "confirm")
        assert td.stash_is_live(sid) is False


# ── the interceptor must not touch a CONCURRENT turn's pending ────────────


class TestInterceptorIgnoresConcurrentTurnPendings:
    """cq-db3b28dcdd42 root cause. Case 2 of try_confirm_pending_write abandons
    a destructive Asana pending once a FRESHER write supersedes it. Under
    concurrency that "fresher write" can be a sibling turn that is still in
    flight, and the "stale" delete it pops is another in-flight turn's brand-new
    preview -- destroyed before that turn ever rendered its card. turn_started_at
    scopes the arbitration to pendings that predate THIS turn.

    Deliberately deterministic (hand-stamped ts), because the threaded
    integration test above cannot guarantee it hits this interleaving."""

    def _seed(self, asana_ts: float, shopify_ts: float) -> str:
        sid = cc.mint_stash_id("asana", USER_ID, CHANNEL_NAME)
        td._store_pending_asana_write(USER_ID, CHANNEL_NAME, {
            "action": "delete", "gid": "g1", "label": "In-flight task",
            "ts": asana_ts, "stash_id": sid,
        })
        td._store_pending_shopify_write(USER_ID, CHANNEL_NAME, {
            "sku": "SKU1", "delta": -1, "ts": shopify_ts,
            "stash_id": cc.mint_stash_id("shopify", USER_ID, CHANNEL_NAME),
        })
        return sid

    def test_concurrent_turns_pending_survives(self):
        now = time.time()
        turn_start = now          # this turn began now...
        # Comfortably past _CLOCK_SKEW_TOLERANCE_SECONDS: a sub-second margin
        # is deliberately treated as this turn's own, since wall-clock skew
        # of that size is indistinguishable from a genuine sibling mint.
        sid = self._seed(asana_ts=now + 30, shopify_ts=now + 31)

        reply = td.try_confirm_pending_write(
            slack_user_id=USER_ID, channel_name=CHANNEL_NAME, entity="F3E",
            message="stage a calendar write", turn_started_at=turn_start,
        )
        assert reply is None
        assert td.stash_is_live(sid), \
            "a sibling turn's in-flight delete preview must not be abandoned"

    def test_genuinely_stale_destructive_pending_is_still_abandoned(self):
        """The supersede-abandon must keep working for its real case: a delete
        the user left hanging BEFORE this turn, superseded by a newer write."""
        now = time.time()
        sid = self._seed(asana_ts=now - 60, shopify_ts=now - 30)

        reply = td.try_confirm_pending_write(
            slack_user_id=USER_ID, channel_name=CHANNEL_NAME, entity="F3E",
            message="stage a calendar write", turn_started_at=now,
        )
        assert reply is None
        assert not td.stash_is_live(sid), \
            "a pre-existing stale destructive pending must still be abandoned"

    def test_default_none_keeps_legacy_behaviour(self):
        """No turn_started_at (every pre-v2 caller and test) = unfiltered."""
        now = time.time()
        sid = self._seed(asana_ts=now + 30, shopify_ts=now + 31)

        td.try_confirm_pending_write(
            slack_user_id=USER_ID, channel_name=CHANNEL_NAME, entity="F3E",
            message="stage a calendar write",
        )
        assert not td.stash_is_live(sid)

    def test_concurrent_pending_cannot_be_confirmed_by_this_turn(self):
        """A bare affirmative typed in one turn must not fire a write another
        turn staged AFTER this turn started -- the user cannot have seen it."""
        now = time.time()
        sid = cc.mint_stash_id("asana", USER_ID, CHANNEL_NAME)
        td._store_pending_asana_write(USER_ID, CHANNEL_NAME, {
            "action": "delete", "gid": "g1", "label": "Sibling task",
            "ts": now + 30, "stash_id": sid,
        })
        with patch.object(td, "_run_confirm_execute") as ex:
            reply = td.try_confirm_pending_write(
                slack_user_id=USER_ID, channel_name=CHANNEL_NAME, entity="F3E",
                message="yes", turn_started_at=now,
            )
        ex.assert_not_called()
        assert reply is None
        assert td.stash_is_live(sid), "the sibling turn's pending stays intact"


# ── the sweep ──────────────────────────────────────────────────────────────


class TestCloseStaleConfirmCards:
    def test_dead_stash_card_loses_its_buttons(self):
        sid = _mint_asana()
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "333.3", "Delete *Test task*?")
        td._take_pending_asana_write(USER_ID, CHANNEL_NAME)  # typed confirm/cancel

        client = MagicMock()
        app_mod._close_stale_confirm_cards(client)

        client.chat_update.assert_called_once()
        kwargs = client.chat_update.call_args.kwargs
        assert kwargs["channel"] == "C1"
        assert kwargs["ts"] == "333.3"
        types = [b.get("type") for b in kwargs["blocks"]]
        assert "actions" not in types, "a closed card must carry no live buttons"
        assert any("Delete *Test task*?" in (b.get("text") or {}).get("text", "")
                   for b in kwargs["blocks"]), "the original preview stays visible"

    def test_live_stash_card_is_left_alone(self):
        sid = _mint_asana()
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "333.3", "Delete *Test task*?")

        client = MagicMock()
        app_mod._close_stale_confirm_cards(client)
        client.chat_update.assert_not_called()
        assert cc.open_card_stash_ids() == [sid]

    def test_sweep_is_idempotent(self):
        sid = _mint_asana()
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "333.3", "p")
        td._take_pending_asana_write(USER_ID, CHANNEL_NAME)

        client = MagicMock()
        app_mod._close_stale_confirm_cards(client)
        app_mod._close_stale_confirm_cards(client)
        assert client.chat_update.call_count == 1

    def test_flag_off_is_a_total_no_op(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        sid = _mint_asana()
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "333.3", "p")
        td._take_pending_asana_write(USER_ID, CHANNEL_NAME)

        client = MagicMock()
        app_mod._close_stale_confirm_cards(client)
        client.chat_update.assert_not_called()

    def test_failed_edit_never_raises(self):
        sid = _mint_asana()
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "333.3", "p")
        td._take_pending_asana_write(USER_ID, CHANNEL_NAME)

        client = MagicMock()
        client.chat_update.side_effect = RuntimeError("slack down")
        app_mod._close_stale_confirm_cards(client)  # must not raise

    def test_multiple_cards_for_one_stash_all_close(self):
        """Belt-and-braces: even if a duplicate card somehow got registered
        (a pre-claim path, or a future caller), EVERY rendered copy closes."""
        sid = _mint_asana()
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "1.1", "p")
        cc.register_card(sid, "C2", "2.2", "p")
        td._take_pending_asana_write(USER_ID, CHANNEL_NAME)

        client = MagicMock()
        app_mod._close_stale_confirm_cards(client)
        assert client.chat_update.call_count == 2


# ── tap path: the tap's own outcome text is never clobbered by the sweep ───


class TestTapKeepsItsOwnOutcomeText:
    def test_confirm_tap_edit_is_not_overwritten_by_sweep(self):
        sid = _mint_asana()
        cc.claim_card_attach(sid)
        cc.register_card(sid, "C1", "444.4", "Delete *Test task*?")

        client = MagicMock()
        body = {
            "user": {"id": USER_ID},
            "channel": {"id": "C1"},
            "message": {"ts": "444.4", "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "Delete *Test task*?"}},
                {"type": "actions", "elements": []},
            ]},
            "actions": [{"value": sid}],
        }
        with patch.object(td, "_execute_claimed_asana", return_value="Deleted *Test task*."), \
             patch("cora.entity_router.route", return_value="F3E"):
            app_mod._handle_confirm_tap(body, client, action="confirm")

        assert client.chat_update.call_count == 1, \
            "the sweep must not re-edit a card the tap already closed"
        assert "Deleted *Test task*." in client.chat_update.call_args.kwargs["text"]


# ── concurrent turns: N turns -> N replies -> N distinct bound cards ───────


def _routing_hints():
    return SimpleNamespace(bypass_cache=True, skip_kb=True, kb_k_override=None, cache_ttl=0)


_KIND_MINTERS = {
    "asana": lambda u, c: (
        lambda sid: (td._store_pending_asana_write(u, c, {
            "action": "delete", "gid": "ga", "label": "A",
            "ts": time.time(), "stash_id": sid}), sid)[1]
    )(cc.mint_stash_id("asana", u, c)),
    "shopify": lambda u, c: (
        lambda sid: (td._store_pending_shopify_write(u, c, {
            "sku": "SKU1", "delta": -1, "ts": time.time(), "stash_id": sid}), sid)[1]
    )(cc.mint_stash_id("shopify", u, c)),
    "calendar": lambda u, c: (
        lambda sid: (td._store_pending_calendar_write(u, c, {
            "action": "create", "summary": "Sync",
            "ts": time.time(), "stash_id": sid}), sid)[1]
    )(cc.mint_stash_id("calendar", u, c)),
}


def _run_one_turn(kind: str, replies: dict, errors: list):
    """One full _dispatch_qa turn. The module-level patches are installed ONCE
    by the caller in the main thread -- unittest.mock.patch mutates module
    globals and is NOT thread-safe, so per-thread `with patch(...)` blocks would
    have threads restoring each other's attributes mid-flight (a harness
    artifact that looks exactly like a dropped card)."""
    say = MagicMock(side_effect=[Exception("no placeholder"), {"ok": True}])
    try:
        app_mod._dispatch_qa(
            channel_id="C0TEST", channel_name=CHANNEL_NAME, user_id=USER_ID,
            user_message=f"stage a {kind} write", reply_thread_ts="123.456",
            entity="F3E", client=MagicMock(), say=say,
        )
        replies[kind] = say.call_args_list[1].kwargs
    except Exception as exc:  # noqa: BLE001
        errors.append((kind, exc))


class TestTypedConfirmClosesTheCard:
    """cq-fee6c9764950 headline: a TYPED cancel/confirm consumes the stash
    through a path that never touches confirm_cards -- the already-posted card
    must still lose its buttons, in the same turn."""

    def _turn(self, message: str, client, *, mint: bool, say_ts: str,
              via_interceptor: bool = False):
        def fake_generate(*args, meta=None, **kwargs):
            if mint:
                _KIND_MINTERS["asana"](USER_ID, CHANNEL_NAME)
            if meta is not None:
                meta["used_tools"] = True
                meta["used_verbatim_tool"] = False
            return "Delete *A*? Confirm and I'll do it."

        posted = {"ok": True, "channel": "C0TEST", "ts": say_ts}
        # The deterministic confirm interceptor replies and returns BEFORE the
        # streaming placeholder is ever attempted, so that turn makes exactly
        # one say() call.
        say = MagicMock(side_effect=[posted] if via_interceptor
                        else [Exception("no placeholder"), posted])
        with patch.object(app_mod, "generate_response", side_effect=fake_generate), \
             patch.object(app_mod.ic, "classify", return_value="qa"), \
             patch.object(app_mod.ic, "routing_hints", return_value=_routing_hints()), \
             patch.object(app_mod, "load_context_parts", return_value=("static", "kb")), \
             patch.object(app_mod, "load_prompt", return_value="sys"), \
             patch.object(app_mod.model_router, "choose_model", return_value="model-x"), \
             patch.object(app_mod.model_router, "short_label", return_value="x"), \
             patch.object(app_mod.user_identity, "display_name", return_value="Tester"), \
             patch.object(app_mod.user_identity, "get_user", return_value=None), \
             patch.object(app_mod.active_thread_store, "register"):
            app_mod._dispatch_qa(
                channel_id="C0TEST", channel_name=CHANNEL_NAME, user_id=USER_ID,
                user_message=message, reply_thread_ts="123.456",
                entity="F3E", client=client, say=say,
            )
        return say

    def test_typed_cancel_drops_the_rendered_cards_buttons(self):
        client = MagicMock()
        say1 = self._turn("delete task A", client, mint=True, say_ts="900.1")

        # Turn 1 posted a live card.
        blocks1 = say1.call_args_list[1].kwargs["blocks"]
        assert any(b.get("type") == "actions" for b in blocks1)
        client.chat_update.assert_not_called()

        # Turn 2: the user TYPES a cancel -- the deterministic interceptor pops
        # the pending, and the already-posted card must be closed.
        self._turn("no", client, mint=False, say_ts="900.2", via_interceptor=True)

        client.chat_update.assert_called_once()
        kwargs = client.chat_update.call_args.kwargs
        assert kwargs["ts"] == "900.1", "the ORIGINAL card is the one that closes"
        assert not any(b.get("type") == "actions" for b in kwargs["blocks"])

    def test_live_pending_card_is_untouched_by_an_unrelated_turn(self):
        client = MagicMock()
        self._turn("delete task A", client, mint=True, say_ts="901.1")
        self._turn("what is our revenue", client, mint=False, say_ts="901.2")
        client.chat_update.assert_not_called()


class TestConcurrentTurnsEachGetTheirOwnCard:
    """cq-db3b28dcdd42: three staged-write turns seconds apart in one channel.
    Each must produce its OWN reply carrying its OWN kind's stash id -- no
    dropped turn, no cross-bound card, no duplicate."""

    def test_three_interleaved_kinds(self):
        minted: dict[str, str] = {}
        replies: dict[str, dict] = {}
        errors: list = []

        def fake_generate(*args, meta=None, **kwargs):
            # kwargs-free dispatch on the message the turn was started with.
            msg = args[2] if len(args) > 2 else kwargs.get("user_message", "")
            kind = next(k for k in _KIND_MINTERS if k in msg)
            # Real tool latency so the three turns genuinely interleave.
            time.sleep(0.05)
            minted[kind] = _KIND_MINTERS[kind](USER_ID, CHANNEL_NAME)
            time.sleep(0.05)
            if meta is not None:
                meta["used_tools"] = True
                meta["used_verbatim_tool"] = False
            return f"Staged a {kind} write -- confirm?"

        with patch.object(app_mod, "generate_response", side_effect=fake_generate), \
             patch.object(app_mod.ic, "classify", return_value="qa"), \
             patch.object(app_mod.ic, "routing_hints", return_value=_routing_hints()), \
             patch.object(app_mod, "load_context_parts", return_value=("static", "kb")), \
             patch.object(app_mod, "load_prompt", return_value="sys"), \
             patch.object(app_mod.model_router, "choose_model", return_value="model-x"), \
             patch.object(app_mod.model_router, "short_label", return_value="x"), \
             patch.object(app_mod.user_identity, "display_name", return_value="Tester"), \
             patch.object(app_mod.user_identity, "get_user", return_value=None), \
             patch.object(app_mod.active_thread_store, "register"):
            threads = [
                threading.Thread(target=_run_one_turn, args=(k, replies, errors))
                for k in ("asana", "shopify", "calendar")
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        assert not errors, f"a turn raised: {errors}"
        assert set(replies) == {"asana", "shopify", "calendar"}, \
            f"a turn was dropped: only {sorted(replies)} replied"

        carded: dict[str, str] = {}
        for kind, kwargs in replies.items():
            blocks = kwargs.get("blocks")
            assert blocks, f"{kind} turn got no card"
            actions = [b for b in blocks if b.get("type") == "actions"]
            assert len(actions) == 1, f"{kind} turn rendered {len(actions)} action blocks"
            values = {e["value"] for e in actions[0]["elements"]}
            assert len(values) == 1, f"{kind} card mixes stash ids: {values}"
            carded[kind] = values.pop()

        # Each card is bound to ITS OWN turn's stash, and no id is reused.
        for kind, sid in carded.items():
            assert sid == minted[kind], (
                f"{kind}'s card is bound to another turn's stash "
                f"({sid} != {minted[kind]})")
        assert len(set(carded.values())) == 3, "two turns shared one card id"
