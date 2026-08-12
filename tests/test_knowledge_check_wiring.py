"""Wiring tests for the daily knowledge check's bot-process half.

The module's own logic is covered in test_knowledge_check.py. What is tested
here is the part that only exists once it is plugged into app.py:

  * the DM intent-collision rule -- which of several plausible intents wins when
    someone types one line into a DM;
  * that the two capture systems (gap ask + knowledge check) are mutually
    exclusive on the ambiguous top-level path, so one reply can never silently
    answer whichever question the code happened to check first;
  * that the four button handlers are registered and follow the terminal-edit
    rule (a race loser never chat_updates the shared card).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import cora.app as app_module
from cora import knowledge_check as kc

USER = "U0B3PS7RFJA"     # Matt Petrovich (real roster member)
OTHER = "U0B3RU5Q55G"    # Tommy Anderson


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(kc, "_events_path", lambda: tmp_path / "events.jsonl")
    kc.reset_caches_for_tests()
    monkeypatch.setenv("CORA_KNOWLEDGE_CHECK", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    yield


def _live_cycle(user=USER, cycle_id="kchk-a", message_ts="111.222",
                question="How many PCI notices are open?"):
    kc.append_event("reserved", cycle_id=cycle_id, user=user, date=kc.az_date(),
                    entity="OSN", tier=1, item_key="matt-pci-closure",
                    question=question)
    kc.append_event("asked", cycle_id=cycle_id, user=user, date=kc.az_date(),
                    channel="D1", message_ts=message_ts)
    return kc.fold_state()["cycles"][cycle_id]


# ---------------------------------------------------------------------------
# Layer A -- source pins
# ---------------------------------------------------------------------------

_APP_SRC = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")


def _registered_listener_names() -> set[str]:
    """Every function Bolt will actually dispatch to, from the LIVE app object."""
    names = set()
    for listener in app_module.app._listeners:
        fn = getattr(listener, "ack_function", None)
        if fn is None:
            lazy = getattr(listener, "lazy_functions", None) or []
            fn = lazy[0] if lazy else None
        if fn is not None:
            names.add(getattr(fn, "__name__", ""))
    return names


def test_the_message_handler_is_still_the_registered_message_listener():
    """THE regression pin for this branch's worst defect.

    Inserting these new functions between `@app.event("message")` and
    `handle_message_event` orphaned the decorator onto the first new function.
    Bolt then dispatched EVERY message event to a helper that takes non-
    injectable kwargs, and `handle_message_event` -- which owns plain-DM Q&A, the
    gap-ask capture, the OSN shift scheduler, Tier-2 historical retrieval, and
    this build's own CAPTURE stage -- became unreachable. Silent: the helper's
    own except swallowed the resulting failure.

    A source grep for `@app.action(...)` cannot catch decorator drift, which is
    exactly why the original wiring test missed it. This walks the real
    registration table instead.
    """
    registered = _registered_listener_names()
    assert "handle_message_event" in registered
    for helper in ("_kc_post", "_handle_knowledge_check_reply", "_handle_kc_tap"):
        assert helper not in registered, (
            f"{helper} is registered as a Slack listener -- a decorator has been "
            f"orphaned onto it")


def test_knowledge_check_is_imported_and_handlers_are_registered():
    assert "from . import knowledge_check" in _APP_SRC
    for action in ("ACTION_CONFIRM_ANSWER", "ACTION_EDIT_ANSWER",
                   "ACTION_SKIP_ANSWER", "ACTION_SKIP_TODAY"):
        assert f"@app.action(knowledge_check.{action})" in _APP_SRC
    # And the handlers themselves are really on the app, not just in the source.
    registered = _registered_listener_names()
    for fn in ("handle_kc_confirm", "handle_kc_edit", "handle_kc_skip_answer",
               "handle_kc_skip_today"):
        assert fn in registered


def test_capture_is_gated_on_the_feature_flag():
    """A capability that is off must not touch DM routing at all."""
    assert "knowledge_check.enabled() and knowledge_check.has_live_cycle" in _APP_SRC


def test_the_two_capture_systems_are_mutually_exclusive_toplevel():
    assert "allow_toplevel=_generic_intent_ok and not _gap_ask_live" in _APP_SRC
    assert "allow_toplevel=_generic_intent_ok and not _kc_live" in _APP_SRC


def test_capture_shares_the_gap_asks_whole_guard_set():
    """Each guard marks a DIFFERENT thing the person plainly meant to do; the
    knowledge check must not be laxer than the branch beside it."""
    block = _APP_SRC.split("_generic_intent_ok = (", 1)[1].split("\n            )", 1)[0]
    for guard in ("_dm_is_shift_message", "looks_like_question",
                  "_remember_or_forget_intent", "_has_staged_write"):
        assert guard in block


def test_a_race_loser_never_edits_the_shared_card():
    """Two independent chat_update round-trips cannot be ordered after the fact,
    so a non-owning outcome replies ephemerally only (the S2 terminal-edit rule)."""
    handler = _APP_SRC.split("def _handle_kc_tap(", 1)[1].split("\n@app.action", 1)[0]
    guard = handler.split('if outcome in ("not_authorized"', 1)[1].split("return", 1)[0]
    assert "chat_postEphemeral" in guard
    assert "chat_update" not in guard


# ---------------------------------------------------------------------------
# Layer B -- the DM reply router
# ---------------------------------------------------------------------------

class TestTypedPath:
    """Buttons are additive and can be switched off, so the typed path has to
    complete the loop on its own."""

    def test_a_first_reply_stages_and_asks_for_a_card(self):
        _live_cycle()
        outcome, payload, post_card = kc.handle_dm_reply("kchk-a", USER, "3 still open")
        assert (outcome, payload, post_card) == ("captured", "3 still open", True)

    def test_a_whole_message_yes_confirms(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
        _live_cycle()
        kc.handle_dm_reply("kchk-a", USER, "3 still open")
        outcome, msg, post_card = kc.handle_dm_reply("kchk-a", USER, "yes")
        assert outcome == "promoted" and post_card is False and msg
        assert "3 still open" in (tmp_path / "osn.md").read_text(encoding="utf-8")

    def test_a_whole_message_no_skips_without_writing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
        _live_cycle()
        kc.handle_dm_reply("kchk-a", USER, "3 still open")
        outcome, _, _ = kc.handle_dm_reply("kchk-a", USER, "no")
        assert outcome == "skipped"
        assert not (tmp_path / "osn.md").exists()

    def test_a_qualified_yes_is_a_reword_not_a_confirm(self, tmp_path, monkeypatch):
        """'yes, but actually it's 4' must NOT save the original 3 -- anchoring
        the affirmative is what stops the wrong number being written."""
        monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
        _live_cycle()
        kc.handle_dm_reply("kchk-a", USER, "3 still open")
        outcome, payload, post_card = kc.handle_dm_reply(
            "kchk-a", USER, "yes, but actually it's 4 open")
        assert outcome == "recaptured" and post_card is True
        assert "4 open" in payload
        assert not (tmp_path / "osn.md").exists()

    @pytest.mark.parametrize("text", ["yes", "Yep", "confirm", "looks good", "LGTM",
                                      "that's right", "ok.", "Perfect!"])
    def test_affirmative_forms(self, text):
        assert kc.is_affirmative(text) is True

    @pytest.mark.parametrize("text", ["yes we closed 3 of them", "confirm with Jen first",
                                      "ok so the number is 4", "correct answer is 2"])
    def test_answers_that_merely_start_with_an_affirmative_are_not_confirms(self, text):
        assert kc.is_affirmative(text) is False

    def test_an_affirmative_before_any_answer_is_captured_not_confirmed(self):
        """A bare 'yes' as the FIRST reply has nothing staged to confirm."""
        _live_cycle()
        outcome, _, _ = kc.handle_dm_reply("kchk-a", USER, "yes")
        assert outcome == "captured"


class TestReplyOwnership:
    """_handle_knowledge_check_reply must DECLINE anything that was never an
    answer -- swallowing it would leave a real question unanswered."""

    def _run(self, text, cycle):
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "999.1"}
        owned = app_module._handle_knowledge_check_reply(
            {"channel": "D1"}, client, USER, text, cycle)
        return owned, client

    def test_a_captured_reply_is_owned_and_posts_the_card(self):
        cyc = _live_cycle()
        owned, client = self._run("3 still open", cyc)
        assert owned is True
        assert client.chat_postMessage.called
        blocks = client.chat_postMessage.call_args.kwargs.get("blocks") or []
        ids = [e["action_id"] for b in blocks if b.get("type") == "actions"
               for e in b["elements"]]
        assert ids == [kc.ACTION_CONFIRM_ANSWER, kc.ACTION_EDIT_ANSWER,
                       kc.ACTION_SKIP_ANSWER]

    def test_a_reply_from_the_wrong_person_is_declined(self):
        cyc = _live_cycle()
        owned, client = self._run_other("3 still open", cyc)
        assert owned is False
        assert not client.chat_postMessage.called

    def _run_other(self, text, cycle):
        client = MagicMock()
        owned = app_module._handle_knowledge_check_reply(
            {"channel": "D1"}, client, OTHER, text, cycle)
        return owned, client

    def test_a_terminal_cycle_declines_so_the_dm_falls_through(self):
        cyc = _live_cycle()
        kc.append_event("promoted", cycle_id="kchk-a", user=USER, date=kc.az_date())
        owned, client = self._run("anything", cyc)
        assert owned is False
        assert not client.chat_postMessage.called

    def test_a_handler_error_declines_rather_than_breaking_the_dm(self, monkeypatch):
        cyc = _live_cycle()
        monkeypatch.setattr(kc, "handle_dm_reply",
                            lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")))
        owned, _ = self._run("3 still open", cyc)
        assert owned is False

    def test_buttons_off_still_ships_a_completable_card(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        cyc = _live_cycle()
        _owned, client = self._run("3 still open", cyc)
        kwargs = client.chat_postMessage.call_args.kwargs
        assert not kwargs.get("blocks")
        # "save"/"discard", NOT "yes"/"no": the F-23 interceptor's affirm list
        # contains "yes"/"ok" and its STOP list contains "no"/"skip", so the
        # buttons-off copy must not steer a user into firing or cancelling an
        # unrelated staged write.
        assert "save" in kwargs["text"] and "discard" in kwargs["text"]
        import cora.tools.tool_dispatch as td
        assert "save" not in td._CONFIRM_AFFIRM_WORDS
        assert "discard" not in td._CONFIRM_STOP_WORDS

    def test_a_threaded_ask_gets_a_threaded_reply(self):
        cyc = _live_cycle()
        client = MagicMock()
        client.chat_postMessage.return_value = {"ts": "999.1"}
        app_module._handle_knowledge_check_reply(
            {"channel": "D1", "thread_ts": "111.222"}, client, USER, "3 open", cyc)
        assert client.chat_postMessage.call_args.kwargs.get("thread_ts") == "111.222"


class TestMatching:
    def test_a_threaded_reply_matches_its_own_ask_even_when_question_shaped(self):
        """A reply typed in the ask's own thread is unambiguous intent -- the
        top-level guards deliberately do not apply to it."""
        _live_cycle()
        assert kc.match_live_cycle(USER, "111.222")["cycle_id"] == "kchk-a"

    def test_a_toplevel_reply_is_refused_when_a_gap_ask_is_also_live(self):
        _live_cycle()
        assert kc.match_live_cycle(USER, None, allow_toplevel=False) is None

    def test_no_live_cycle_means_no_capture(self):
        assert kc.match_live_cycle(USER, None) is None
        assert kc.has_live_cycle(USER) is False


# ---------------------------------------------------------------------------
# Layer B -- tap receiver
# ---------------------------------------------------------------------------

class TestTapReceiver:
    def _body(self, cycle_id="kchk-a", user=USER):
        return {"actions": [{"value": cycle_id}], "user": {"id": user},
                "channel": {"id": "D1"}, "message": {"ts": "222.1", "blocks": []}}

    def test_skip_today_closes_its_own_card(self):
        _live_cycle()
        client = MagicMock()
        app_module._handle_kc_tap(self._body(), client, "skip_today")
        assert client.chat_update.called
        assert kc.fold_state()["cycles"]["kchk-a"]["state"] == kc.STATE_SKIPPED

    def test_a_stranger_tap_is_ephemeral_and_changes_nothing(self):
        _live_cycle()
        client = MagicMock()
        app_module._handle_kc_tap(self._body(user=OTHER), client, "skip_today")
        assert client.chat_postEphemeral.called
        assert not client.chat_update.called
        assert kc.fold_state()["cycles"]["kchk-a"]["state"] == kc.STATE_ASKED

    def test_a_second_tap_does_not_clobber_the_winners_card(self):
        _live_cycle()
        client = MagicMock()
        app_module._handle_kc_tap(self._body(), client, "skip_today")
        client.reset_mock()
        app_module._handle_kc_tap(self._body(), client, "skip_today")
        assert client.chat_postEphemeral.called
        assert not client.chat_update.called

    def test_edit_keeps_the_card_live(self):
        _live_cycle()
        kc.record_answer("kchk-a", USER, "3 open")
        client = MagicMock()
        app_module._handle_kc_tap(self._body(), client, "edit")
        assert client.chat_postEphemeral.called
        assert not client.chat_update.called
        assert kc.fold_state()["cycles"]["kchk-a"]["state"] == kc.STATE_CAPTURED

    def test_eval_mode_refuses_every_tap(self, monkeypatch):
        """Catch-up reconstruction must never fire a write."""
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        _live_cycle()
        client = MagicMock()
        app_module._handle_kc_tap(self._body(), client, "skip_today")
        assert not client.chat_update.called
        assert kc.fold_state()["cycles"]["kchk-a"]["state"] == kc.STATE_ASKED

    def test_flag_off_refuses_every_tap(self, monkeypatch):
        _live_cycle()
        monkeypatch.setenv("CORA_KNOWLEDGE_CHECK", "off")
        client = MagicMock()
        app_module._handle_kc_tap(self._body(), client, "skip_today")
        assert kc.fold_state()["cycles"]["kchk-a"]["state"] == kc.STATE_ASKED

    def test_a_forged_cycle_id_is_ephemeral_only(self):
        client = MagicMock()
        app_module._handle_kc_tap(self._body(cycle_id="kchk-forged"), client, "confirm")
        assert client.chat_postEphemeral.called
        assert not client.chat_update.called

    def test_a_tap_never_crashes_the_bot(self):
        client = MagicMock()
        client.chat_update.side_effect = RuntimeError("slack down")
        _live_cycle()
        app_module._handle_kc_tap(self._body(), client, "skip_today")  # must not raise

    def test_a_stale_card_cannot_promote_an_answer_it_is_not_showing(self, tmp_path,
                                                                     monkeypatch):
        """"Let me reword" leaves the old card live, so a second card exists for
        the same cycle. Tapping the OLDER one used to write the NEWER answer
        while still displaying the old text."""
        monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
        _live_cycle()
        kc.record_answer("kchk-a", USER, "3 still open")
        stale_value = f"kchk-a:{kc.answer_fingerprint('3 still open')}"
        kc.record_answer("kchk-a", USER, "actually 4 still open")

        client = MagicMock()
        body = {"actions": [{"value": stale_value}], "user": {"id": USER},
                "channel": {"id": "D1"}, "message": {"ts": "222.1", "blocks": []}}
        app_module._handle_kc_tap(body, client, "confirm")
        assert client.chat_postEphemeral.called      # refused, ephemerally
        assert not client.chat_update.called
        assert not (tmp_path / "osn.md").exists()    # nothing written

        # The CURRENT card still works.
        fresh = f"kchk-a:{kc.answer_fingerprint('actually 4 still open')}"
        body["actions"] = [{"value": fresh}]
        app_module._handle_kc_tap(body, MagicMock(), "confirm")
        assert "actually 4 still open" in (tmp_path / "osn.md").read_text(encoding="utf-8")

    def test_an_ok_after_reword_does_not_promote_the_rejected_answer(self, tmp_path,
                                                                     monkeypatch):
        """The edit tap's own instruction invites an acknowledgement; "ok" must
        not save the very answer the person just rejected."""
        monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
        _live_cycle()
        kc.record_answer("kchk-a", USER, "3 still open")
        assert kc.process_edit_tap("kchk-a", USER)[0] == "editing"
        outcome, _payload, post_card = kc.handle_dm_reply("kchk-a", USER, "ok")
        assert outcome == "recaptured" and post_card is True
        assert not (tmp_path / "osn.md").exists()

    def test_a_tap_writes_nothing_in_dry_mode(self, tmp_path, monkeypatch):
        """`dry` is documented as no sends and NO WRITES. Cards outstanding from
        an earlier `on` run must not still write after the flag is turned down."""
        monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
        _live_cycle()
        kc.record_answer("kchk-a", USER, "3 still open")
        monkeypatch.setenv("CORA_KNOWLEDGE_CHECK", "dry")
        outcome, _ = kc.process_confirm_tap("kchk-a", USER)
        assert outcome == "refused"
        assert not (tmp_path / "osn.md").exists()
