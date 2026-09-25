"""Code #16 C1 -- the bot wiring of the dead-channel lane, through the REAL handlers.

  * the five @app.action listeners are on Bolt's live table and no helper is (an
    orphaned decorator is the lesson-25 class); one real ``bolt.dispatch`` press;
  * the tap wrapper: EVAL_MODE, buttons off, race losers ephemeral-only, the outcome
    threaded BEFORE the re-render, T1 taps on the act pool (never Bolt's listeners);
  * the founder DM intercept runs through the real ``handle_message_event`` AHEAD of
    a live knowledge-check cycle and a pending gap ask, and never reaches the model or
    code_queue's signal capture; the founder @mention path likewise;
  * the missed-message catch-up drafts the fixed line (A23).
"""
from __future__ import annotations

import ast
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cora.app as app_module
from _chanarch_fakes import DAY, HARRISON, NOW, PERSON, resp
from cora.channel_archive import cards, deliver, intents
from cora.channel_archive import registry as reg
from cora.channel_archive import store as st

REPO = Path(__file__).resolve().parents[1]
PID = "chanarch-ddddeeeeffff"
BOT = "U0B44MDGC5R"
A1 = "C0DEADAAA1"


def _registered() -> set[str]:
    names = set()
    for li in app_module.app._listeners:
        fn = getattr(li, "ack_function", None)
        if fn is None:
            lazy = getattr(li, "lazy_functions", None) or []
            fn = lazy[0] if lazy else None
        if fn is not None:
            names.add(getattr(fn, "__name__", ""))
    return names


class TestListenerTable:
    def test_the_five_actions_are_registered_and_no_helper_is(self):
        reg_names = _registered()
        for name in ("handle_channel_archive_row", "handle_channel_archive_all",
                     "handle_channel_archive_keep", "handle_channel_archive_override",
                     "handle_channel_archive_card_agreed", "handle_message_event", "handle_mention"):
            assert name in reg_names, name
        for helper in ("_ca_post", "_ca_run_scan", "_ca_start_scan", "_channel_archive_dm_intercept",
                       "_channel_archive_mention_intercept", "_ca_rerender", "_ca_run_tap",
                       "_handle_channel_archive_tap"):
            assert helper not in reg_names, f"{helper} is a listener -- an orphaned decorator"

    def test_every_lane_decorator_sits_on_its_own_handler(self):
        tree = ast.parse((REPO / "src" / "cora" / "app.py").read_text(encoding="utf-8-sig"))
        seen = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Attribute)
                        and isinstance(dec.args[0].value, ast.Name)
                        and dec.args[0].value.id == "channel_archive_cards"):
                    seen[dec.args[0].attr] = node.name
        assert seen == {"ACTION_ROW": "handle_channel_archive_row",
                        "ACTION_ALL": "handle_channel_archive_all",
                        "ACTION_KEEP": "handle_channel_archive_keep",
                        "ACTION_OVERRIDE": "handle_channel_archive_override",
                        "ACTION_AGREED": "handle_channel_archive_card_agreed"}

    def test_the_action_ids_are_unique_on_the_app(self):
        tree = ast.parse((REPO / "src" / "cora" / "app.py").read_text(encoding="utf-8-sig"))
        assert len(set(cards.ACTIONS)) == 5
        for a in cards.ACTIONS:
            assert a.startswith("cora_channel_archive_")


def _stage(tier="T0"):
    rows = [{"cid": A1, "section": "A", "tier": tier, "name": "fx-dead-one",
             "name_fp": reg.name_fp("fx-dead-one"), "is_private": False, "last_person_days": 200,
             "history_complete": True, "keep_count": 0, "lex": False},
            {"cid": "C0DEADAAA2", "section": "A", "tier": tier, "name": "fx-dead-two",
             "name_fp": reg.name_fp("fx-dead-two"), "is_private": False, "last_person_days": 210,
             "history_complete": True, "keep_count": 0, "lex": False}]
    now = time.time()
    st.append_event("staged", proposal_id=PID, ts=now, expires_ts=now + 14 * DAY, rows=rows,
                    counts={}, scanned=2)
    st.append_event("delivered", proposal_id=PID, page=1, dm_channel="DHARRISON1",
                    message_ts="1790000000.000100", rendered_cids=[A1, "C0DEADAAA2"], buttons=True, ts=now)


def _body(action, value, user=HARRISON):
    return {"actions": [{"action_id": action, "value": value}], "user": {"id": user},
            "channel": {"id": "DHARRISON1"}, "message": {"ts": "1790000000.000100", "blocks": []}}


@pytest.fixture
def buttons_on(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)


class TestTapWrapper:
    def test_eval_mode_returns_before_anything(self, monkeypatch, buttons_on):
        _stage()
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        client = MagicMock()
        app_module._handle_channel_archive_tap(_body(cards.ACTION_ROW, f"{PID}:{A1}"), client,
                                               cards.ACTION_ROW)
        assert client.method_calls == [] and st.fold().proposals[PID].state_of(A1) == st.OPEN

    def test_buttons_off_is_ephemeral_and_records_nothing(self, monkeypatch):
        _stage()
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        client = MagicMock()
        app_module._handle_channel_archive_tap(_body(cards.ACTION_ROW, f"{PID}:{A1}"), client,
                                               cards.ACTION_ROW)
        assert client.chat_postEphemeral.called and not client.chat_update.called
        assert st.fold().proposals[PID].state_of(A1) == st.OPEN

    def test_a_non_harrison_tap_never_edits_the_card(self, buttons_on):
        _stage()
        client = MagicMock()
        app_module._handle_channel_archive_tap(_body(cards.ACTION_ROW, f"{PID}:{A1}", user=PERSON),
                                               client, cards.ACTION_ROW)
        assert client.chat_postEphemeral.call_args.kwargs["text"] == "Only Harrison can act on this card."
        assert not client.chat_update.called and not client.chat_postMessage.called

    def test_a_t0_mark_threads_the_outcome_first_then_rerenders_from_the_store(self, buttons_on):
        _stage()
        order: list[str] = []
        client = MagicMock()
        client.chat_postMessage.side_effect = lambda **kw: order.append("thread")
        client.chat_update.side_effect = lambda **kw: order.append("update")
        app_module._handle_channel_archive_tap(_body(cards.ACTION_ROW, f"{PID}:{A1}"), client,
                                               cards.ACTION_ROW)
        assert order == ["thread", "update"]
        post = client.chat_postMessage.call_args.kwargs
        assert post["thread_ts"] == "1790000000.000100" and "nothing was archived" in post["text"].lower()
        upd = client.chat_update.call_args.kwargs
        acted = [e["value"] for b in upd["blocks"] if b.get("type") == "actions" for e in b["elements"]
                 if e["action_id"] == cards.ACTION_ROW]
        assert acted == [f"{PID}:C0DEADAAA2:T0"]                 # A1 decided: no button left
        assert upd["text"].startswith("Dead-channel proposal (T0 — nothing archived)")
        assert st.fold().proposals[PID].state_of(A1) == st.AGREED

    def test_a_t1_tap_runs_on_the_act_pool_never_inline(self, buttons_on, monkeypatch):
        _stage(tier="T1")
        seen: list[str] = []
        done = threading.Event()

        def _run(body, client, action):
            seen.append(threading.current_thread().name)
            done.set()
        monkeypatch.setattr(app_module, "_ca_run_tap", _run)
        client = MagicMock()
        app_module._handle_channel_archive_tap(_body(cards.ACTION_ROW, f"{PID}:{A1}:T1"), client,
                                               cards.ACTION_ROW)
        assert done.wait(5) and seen[0].startswith("chanarch-act")
        assert client.chat_postEphemeral.call_args.kwargs["text"] == app_module._CHANNEL_ARCHIVE_WORKING

    def test_a_busy_render_lock_retries_once_on_the_act_pool(self, buttons_on, monkeypatch):
        _stage()
        monkeypatch.setattr(app_module, "_CHANNEL_ARCHIVE_RENDER_WAIT_S", 0.01)
        client = MagicMock()
        with app_module._CHANNEL_ARCHIVE_RENDER_LOCK:
            assert app_module._ca_rerender(client, "DHARRISON1", "1790000000.000100") is False
        app_module._CHANNEL_ARCHIVE_ACT_POOL.shutdown(wait=True)
        assert client.chat_update.called                      # the one retry landed


class TestBoltDispatch:
    def test_a_real_press_through_bolt_records_the_mark(self, monkeypatch, buttons_on):
        from slack_bolt import BoltRequest
        from slack_bolt.middleware.authorization.single_team_authorization import SingleTeamAuthorization
        from slack_sdk.web.client import WebClient
        from slack_sdk.web.slack_response import SlackResponse
        _stage()
        data = {"ok": True, "url": "https://test.slack.com/", "user_id": "U_CORA_TEST",
                "team": "T", "user": "bot", "team_id": "T_TEST", "bot_id": "B_TEST"}

        def _auth(self, **kw):
            return SlackResponse(client=self, http_verb="POST", api_url="auth.test", req_args={},
                                 data=data, headers={"x-oauth-scopes": "chat:write"}, status_code=200)
        monkeypatch.setattr(WebClient, "auth_test", _auth)
        bolt = app_module.app
        for m in bolt._middleware_list:
            if isinstance(m, SingleTeamAuthorization):
                monkeypatch.setattr(m, "auth_test_result", _auth(bolt.client))
        posted: list = []
        monkeypatch.setattr(app_module, "_ca_post", lambda client, ch, text, thread_ts=None: posted.append(text))
        monkeypatch.setattr(app_module, "_ca_rerender", lambda *a, **k: True)
        payload = {"type": "block_actions", "user": {"id": HARRISON}, "team": {"id": "T_TEST"},
                   "api_app_id": "A1", "token": "t", "trigger_id": "trig1",
                   "container": {"type": "message", "message_ts": "1790000000.000100", "channel_id": "DHARRISON1"},
                   "channel": {"id": "DHARRISON1"}, "message": {"ts": "1790000000.000100", "blocks": []},
                   "actions": [{"type": "button", "action_id": cards.ACTION_ROW, "block_id": "b1",
                                "value": f"{PID}:{A1}", "action_ts": "1"}]}
        resp = bolt.dispatch(BoltRequest(mode="socket_mode", body=payload))
        assert resp.status == 200
        deadline = time.monotonic() + 5
        while not posted and time.monotonic() < deadline:
            time.sleep(0.02)
        assert st.fold().proposals[PID].state_of(A1) == st.AGREED
        assert posted and "recorded as T1 promotion evidence" in posted[0]


def _event(text, user=HARRISON, channel="DHARRISON1", ts="1790000100.000100", thread_ts=None):
    ev = {"user": user, "text": text, "channel": channel, "ts": ts, "channel_type": "im"}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    return ev


@pytest.fixture
def dm(monkeypatch):
    """The real handle_message_event with a LIVE knowledge-check cycle AND a pending gap
    ask in place, the model and code_queue's capture recorded."""
    kc_match, gap_match, qa, capture, scans = MagicMock(return_value=None), MagicMock(return_value=None), \
        MagicMock(), MagicMock(), []
    monkeypatch.setattr(app_module.knowledge_check, "enabled", lambda: True)
    monkeypatch.setattr(app_module.knowledge_check, "has_live_cycle", lambda uid: True)
    monkeypatch.setattr(app_module.knowledge_check, "has_cycle_asked_today", lambda uid: True)
    monkeypatch.setattr(app_module.knowledge_check, "match_live_cycle", kc_match)
    monkeypatch.setattr(app_module.gap_autofill, "has_live_ask", lambda uid: True)
    monkeypatch.setattr(app_module.gap_autofill, "match_pending_ask", gap_match)
    monkeypatch.setattr(app_module.historical_access, "detect_retrieval_intent", lambda t: False)
    monkeypatch.setattr(app_module.decision_alerts, "match_alert_reply", lambda *a: None)
    monkeypatch.setattr(app_module, "_dm_is_shift_message", lambda *a: False)
    monkeypatch.setattr(app_module, "_handle_dm_qa", qa)
    monkeypatch.setattr(app_module, "_dispatch_qa", qa)
    monkeypatch.setattr(app_module.code_queue, "capture_message_signal", capture)
    monkeypatch.setattr(app_module, "_resolve_bot_user_id", lambda c: BOT)
    done = threading.Event()

    def _deliver(**kw):
        scans.append((threading.current_thread().name, kw))
        done.set()
        return {"delivered": True, "reason": "delivered", "proposal_id": PID}
    monkeypatch.setattr(deliver, "deliver_proposal", _deliver)
    return SimpleNamespace(kc=kc_match, gap=gap_match, qa=qa, capture=capture, scans=scans, done=done)


def _texts(client):
    return [c.kwargs.get("text") for c in client.chat_postMessage.call_args_list]


class TestFounderDM:
    def test_the_ask_starts_the_scan_ahead_of_every_capture_and_the_model(self, dm):
        client = MagicMock()
        app_module.handle_message_event(_event("can you archive the dead channels?"), client)
        assert dm.done.wait(5)
        assert dm.scans[0][0].startswith("chanarch-scan") and dm.scans[0][1]["trigger"] == "ask"
        assert _texts(client) == [intents.ACK_REPLY]
        assert not dm.kc.called and not dm.gap.called and not dm.qa.called and not dm.capture.called

    def test_a_member_dm_is_not_intercepted(self, dm):
        client = MagicMock()
        app_module.handle_message_event(_event("archive the dead channels", user=PERSON), client)
        assert not dm.scans and intents.ACK_REPLY not in _texts(client)
        assert dm.kc.called or dm.gap.called or dm.qa.called     # it went down the normal path

    def test_status_and_attempt_get_code_replies(self, dm):
        client = MagicMock()
        app_module.handle_message_event(_event("did the dead channels get archived?"), client)
        app_module.handle_message_event(_event("archive #old-promo channel"), client)
        texts = _texts(client)
        assert texts[0].startswith("Dead-channel lane: T0") and texts[1] == intents.ATTEMPT_REPLY
        assert not dm.qa.called and not dm.scans

    def test_a_followup_to_a_live_card_is_refused_but_a_yes_for_a_staged_write_is_not(self, dm, monkeypatch):
        _stage()
        st.append_event("delivered", proposal_id=PID, page=1, dm_channel="DHARRISON1",
                        message_ts=f"{time.time() - 5:.6f}", rendered_cids=[A1], buttons=True)
        client = MagicMock()
        app_module.handle_message_event(_event("archive them"), client)
        assert _texts(client)[-1].startswith(intents.FOLLOWUP_REPLY_LEAD)
        monkeypatch.setattr(app_module._tool_dispatch, "snapshot_stash_ids", lambda u, c: {"x": ["id1"]})
        client2 = MagicMock()
        app_module.handle_message_event(_event("yes"), client2)
        assert intents.FOLLOWUP_REPLY_LEAD not in " ".join(t or "" for t in _texts(client2))

    def test_a_bare_yes_never_steals_a_live_gap_or_kc_answer(self, dm, monkeypatch):
        _stage()
        st.append_event("delivered", proposal_id=PID, page=1, dm_channel="DHARRISON1",
                        message_ts=f"{time.time() - 5:.6f}", rendered_cids=[A1], buttons=True)
        client = MagicMock()
        app_module.handle_message_event(_event("yes"), client)          # gap + KC live (fixture)
        assert intents.FOLLOWUP_REPLY_LEAD not in " ".join(t or "" for t in _texts(client))
        assert dm.gap.called or dm.kc.called
        monkeypatch.setattr(app_module.gap_autofill, "has_live_ask", lambda uid: False)
        monkeypatch.setattr(app_module.knowledge_check, "has_live_cycle", lambda uid: False)
        client2 = MagicMock()
        before = dm.qa.call_count
        app_module.handle_message_event(_event("yes"), client2)
        assert _texts(client2)[-1].startswith(intents.FOLLOWUP_REPLY_LEAD) and dm.qa.call_count == before

    def test_a_card_stamped_a_hair_ahead_of_the_host_clock_still_refuses_a_bare_yes(self, dm, monkeypatch):
        """harness-isolation#0: a card ts is Slack's clock and ``now`` is the host's, and
        a .6f stamp rounds UP about half the time -- a card a moment in the FUTURE is
        clock skew, not a stale card, so the A21(d) rail must still answer."""
        _stage()
        st.append_event("delivered", proposal_id=PID, page=1, dm_channel="DHARRISON1",
                        message_ts=f"{time.time() + 1.0:.6f}", rendered_cids=[A1], buttons=True)
        monkeypatch.setattr(app_module.gap_autofill, "has_live_ask", lambda uid: False)
        monkeypatch.setattr(app_module.knowledge_check, "has_live_cycle", lambda uid: False)
        client = MagicMock()
        before = dm.qa.call_count
        app_module.handle_message_event(_event("yes"), client)
        assert _texts(client) and _texts(client)[-1] == intents.followup_reply()
        assert dm.qa.call_count == before

    def test_eval_mode_is_a_silent_no_op(self, dm, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        client = MagicMock()
        app_module.handle_message_event(_event("archive the dead channels"), client)
        assert not client.chat_postMessage.called and not dm.scans and not dm.qa.called

    def test_off_says_so_and_scans_nothing(self, dm, monkeypatch):
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "off")
        client = MagicMock()
        app_module.handle_message_event(_event("archive the dead channels"), client)
        assert _texts(client) == [intents.OFF_REPLY] and not dm.scans

    def test_a_crashed_scan_says_so_and_the_next_ask_is_accepted(self, dm, monkeypatch):
        calls = {"n": 0}

        def _boom(**kw):
            calls["n"] += 1
            raise RuntimeError("scan blew up")
        monkeypatch.setattr(deliver, "deliver_proposal", _boom)
        client = MagicMock()
        app_module.handle_message_event(_event("archive the dead channels"), client)
        app_module._CHANNEL_ARCHIVE_SCAN_POOL.shutdown(wait=True)
        assert _texts(client) == [intents.ACK_REPLY, intents.SCAN_FAILED_REPLY]
        monkeypatch.setattr(app_module, "_CHANNEL_ARCHIVE_SCAN_POOL",
                            __import__("concurrent.futures").futures.ThreadPoolExecutor(1))
        app_module.handle_message_event(_event("archive the dead channels"), client)
        app_module._CHANNEL_ARCHIVE_SCAN_POOL.shutdown(wait=True)
        assert calls["n"] == 2                                   # the guard was released

    def test_a_scan_already_running_in_this_process(self, dm):
        client = MagicMock()
        assert app_module._CHANNEL_ARCHIVE_SCAN_GUARD.acquire(blocking=False)
        try:
            app_module.handle_message_event(_event("archive the dead channels"), client)
        finally:
            app_module._CHANNEL_ARCHIVE_SCAN_GUARD.release()
        assert _texts(client) == [intents.SCAN_RUNNING_REPLY] and not dm.scans

    def test_the_default_client_belt_turns_a_real_scan_into_the_failure_line(self, dm, monkeypatch):
        """No fake injected: under pytest the lane refuses to build a live client (A26)."""
        monkeypatch.setattr(deliver, "deliver_proposal", _REAL_DELIVER)
        client = MagicMock()
        app_module.handle_message_event(_event("archive the dead channels"), client)
        app_module._CHANNEL_ARCHIVE_SCAN_POOL.shutdown(wait=True)
        assert _texts(client)[-1] == intents.SCAN_FAILED_REPLY
        assert st.read_events() == []                # the refusal came before any write


_REAL_DELIVER = deliver.deliver_proposal


def _live_card():
    _stage()
    st.append_event("delivered", proposal_id=PID, page=1, dm_channel="DHARRISON1",
                    message_ts=f"{time.time() - 5:.6f}", rendered_cids=[A1], buttons=True)


ASK_TS = "1790000050.000100"


class TestThreadOwnershipR1:
    """integration#1: a reply typed in a gap-ask / knowledge-check / decision-alert
    thread is THAT capture's answer ('threaded replies always win'), even when it
    reads like an archive request -- driven through the REAL handle_message_event."""

    ANSWER = "archive channels after 90 days with no human posts, except leadership"

    def test_a_gap_ask_thread_answer_is_recorded_not_refused(self, dm, monkeypatch):
        dm.gap.side_effect = lambda uid, tts, allow_toplevel=True: (
            {"ask_id": "ask-1", "ask_message_ts": ASK_TS} if tts == ASK_TS else None)
        recorded = []
        monkeypatch.setattr(app_module.gap_autofill, "record_ask_answer",
                            lambda ask, text: recorded.append((ask["ask_id"], text)) or "Got it -- thanks.")
        client = MagicMock()
        app_module.handle_message_event(_event(self.ANSWER, thread_ts=ASK_TS), client)
        assert recorded == [("ask-1", self.ANSWER)]
        assert intents.ATTEMPT_REPLY not in _texts(client) and "Got it -- thanks." in _texts(client)

    @pytest.mark.parametrize("text", ["archive the channels on the list", "did you archive the channels?",
                                      "archive the dead channels"])
    def test_a_knowledge_check_thread_answer_goes_to_the_check(self, dm, monkeypatch, text):
        dm.kc.side_effect = lambda uid, tts, allow_toplevel=True: (
            {"cycle_id": "kc-1"} if tts == ASK_TS else None)
        handled = MagicMock(return_value=True)
        monkeypatch.setattr(app_module, "_handle_knowledge_check_reply", handled)
        client = MagicMock()
        app_module.handle_message_event(_event(text, thread_ts=ASK_TS), client)
        assert handled.called and _texts(client) == [] and not dm.scans, text

    def test_a_decision_alert_thread_answer_goes_to_the_decision(self, dm, monkeypatch):
        monkeypatch.setattr(app_module.decision_alerts, "match_alert_reply",
                            lambda uid, tts, *a: {"alert_message_ts": ASK_TS} if tts == ASK_TS else None)
        monkeypatch.setattr(app_module.decision_alerts, "is_decline", lambda t: True)
        marked = MagicMock()
        monkeypatch.setattr(app_module.decision_alerts, "mark_state", marked)
        monkeypatch.setattr(app_module.gap_autofill, "has_live_ask", lambda uid: False)
        monkeypatch.setattr(app_module.knowledge_check, "has_live_cycle", lambda uid: False)
        client = MagicMock()
        app_module.handle_message_event(_event("archive the channels on the list", thread_ts=ASK_TS), client)
        assert marked.called and intents.ATTEMPT_REPLY not in _texts(client)

    def test_an_unowned_thread_still_gets_the_attempt_reply(self, dm):
        """Only a CAPTURE-owned thread yields: an ordinary Q&A thread keeps the rail (the
        model has no archive tool, and its 'Archived ...' trips no rail)."""
        client = MagicMock()
        app_module.handle_message_event(_event(self.ANSWER, thread_ts="1790000070.000100"), client)
        assert _texts(client) == [intents.ATTEMPT_REPLY] and not dm.qa.called

    def test_a_card_thread_follow_up_is_still_the_cards(self, dm):
        _live_card()
        card_ts = next(iter(intents.live_card_message_ts("DHARRISON1")))
        dm.gap.side_effect = lambda uid, tts, allow_toplevel=True: {"ask_id": "x"}   # would claim anything
        client = MagicMock()
        app_module.handle_message_event(_event("archive them", thread_ts=card_ts), client)
        assert _texts(client) == [intents.followup_reply()]


def _history(*msgs):
    return resp({"ok": True, "messages": list(msgs), "has_more": False})


class TestNewerBotMessageR1:
    """c1-intents-copy#5: a bare 'yes' is the card's only while the card is the newest
    thing Cora said in the DM; a newer answer of Cora's owns it."""

    @pytest.fixture
    def quiet(self, dm, monkeypatch):
        monkeypatch.setattr(app_module.gap_autofill, "has_live_ask", lambda uid: False)
        monkeypatch.setattr(app_module.knowledge_check, "has_live_cycle", lambda uid: False)
        _live_card()
        return dm

    def test_a_newer_bot_question_owns_the_yes(self, quiet):
        client = MagicMock()
        client.conversations_history.return_value = _history(
            {"ts": f"{time.time() - 1:.6f}", "user": HARRISON, "text": "yes"},
            {"ts": f"{time.time() - 2:.6f}", "bot_id": "BCORA", "user": BOT,
             "text": "Cash is $1.2M across the operating accounts. Want the 13-week view too?"})
        before = quiet.qa.call_count
        app_module.handle_message_event(_event("yes"), client)
        assert quiet.qa.call_count == before + 1
        assert intents.FOLLOWUP_REPLY_LEAD not in " ".join(t or "" for t in _texts(client))
        kw = client.conversations_history.call_args.kwargs
        assert kw["channel"] == "DHARRISON1" and "oldest" in kw

    def test_the_lanes_own_newer_line_does_not(self, quiet):
        client = MagicMock()
        client.conversations_history.return_value = _history(
            {"ts": f"{time.time() - 2:.6f}", "bot_id": "BCORA", "user": BOT, "text": intents.followup_reply()})
        app_module.handle_message_event(_event("yes"), client)
        assert _texts(client) == [intents.followup_reply()] and not quiet.qa.called

    def test_a_newer_founder_message_does_not(self, quiet):
        client = MagicMock()
        client.conversations_history.return_value = _history(
            {"ts": f"{time.time() - 2:.6f}", "user": HARRISON, "text": "hmm let me look"})
        app_module.handle_message_event(_event("yes"), client)
        assert _texts(client) == [intents.followup_reply()] and not quiet.qa.called

    def test_a_history_read_error_keeps_the_rail(self, quiet):
        client = MagicMock()
        client.conversations_history.side_effect = RuntimeError("slack down")
        app_module.handle_message_event(_event("yes"), client)
        assert _texts(client) == [intents.followup_reply()] and not quiet.qa.called

    def test_a_yes_in_the_cards_own_thread_is_the_cards(self, quiet):
        client = MagicMock()
        client.conversations_history.return_value = _history(
            {"ts": f"{time.time() - 2:.6f}", "bot_id": "BCORA", "text": "Want the 13-week view too?"})
        card_ts = next(iter(intents.live_card_message_ts("DHARRISON1")))
        app_module.handle_message_event(_event("yes", thread_ts=card_ts), client)
        assert _texts(client) == [intents.followup_reply()] and not quiet.qa.called

    def test_eval_mode_reads_no_slack_history(self, quiet, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        client = MagicMock()
        app_module.handle_message_event(_event("yes"), client)
        assert not client.conversations_history.called and not client.chat_postMessage.called
        assert not quiet.qa.called

    def test_an_imperative_follow_up_ignores_newer_messages(self, quiet):
        """Only the bare affirmative is ambiguous; 'archive them' always means the card."""
        client = MagicMock()
        client.conversations_history.return_value = _history(
            {"ts": f"{time.time() - 2:.6f}", "bot_id": "BCORA", "text": "Want the 13-week view too?"})
        app_module.handle_message_event(_event("archive them"), client)
        assert _texts(client) == [intents.followup_reply()] and not quiet.qa.called


class TestFounderDMGrammarR1:
    """D-051 round 1 (c1-intents-copy#0/#1/#2/#6, integration#5): every grammar change
    driven through the REAL handle_message_event."""

    @pytest.mark.parametrize("text", [
        "how do I archive a channel in slack?", "which archived reports cover the retail channel?",
        "what did we decide in <#C0B2T18R3FG|hjr-archive-2024>?", "did the hubspot proposal get archived?",
        "which channels should I archive?", "what is the policy for archiving channels?",
        "did we ever decide which channels to archive?", "do you have access to the archive channel?",
        "archive the retail channel deals that closed lost", "archive the email from the channel",
        "archive the amazon channel report in drive", "archive them",
    ])
    def test_must_not_fire_reaches_the_model(self, dm, text):
        client = MagicMock()
        app_module.handle_message_event(_event(text), client)
        assert dm.qa.called, text
        assert _texts(client) == [] and not dm.scans, text

    @pytest.mark.parametrize("text", [
        "can we archive the dead channels?", "archive the dead channels, please", "cora! archive the dead channels",
        "would you mind archiving the dead channels?", "archive the dead channels :pray:",
        "Cora \u2014 archive the dead channels", "_archive the dead channels_", "yes, archive the dead channels",
    ])
    def test_natural_asks_start_the_scan(self, dm, text):
        client = MagicMock()
        app_module.handle_message_event(_event(text), client)
        assert dm.done.wait(5), text
        assert _texts(client) == [intents.ACK_REPLY] and not dm.qa.called and not dm.capture.called

    @pytest.mark.parametrize("text", ["yes, archive them", "archive them all", "archive the rest",
                                      "sounds good, archive them", "just archive them", "archive everything",
                                      "archive all 12", "archive the ones I marked"])
    def test_typed_followups_to_a_live_card_get_the_code_reply(self, dm, text):
        _live_card()
        client = MagicMock()
        app_module.handle_message_event(_event(text), client)
        assert _texts(client) == [intents.followup_reply()], text
        assert not dm.qa.called and not dm.capture.called and not dm.scans

    @pytest.mark.parametrize("text", ["archive the retail channel deals", "archive my emails from tommy",
                                      "archive the hubspot deal", "archive everything in the promo folder"])
    def test_an_archive_of_some_other_object_is_not_a_card_followup(self, dm, text):
        _live_card()
        client = MagicMock()
        app_module.handle_message_event(_event(text), client)
        assert dm.qa.called and _texts(client) == [], text

    def test_an_attempt_with_no_live_card_still_gets_the_attempt_reply(self, dm):
        """The wider follow-up shape must not swallow the attempt rail when no card is live."""
        client = MagicMock()
        app_module.handle_message_event(_event("archive this channel"), client)
        assert _texts(client) == [intents.ATTEMPT_REPLY] and not dm.qa.called


class TestFounderMention:
    def _run(self, text, user=HARRISON):
        say, client = MagicMock(), MagicMock()
        event = {"channel": "C0B3K67J10T", "user": user, "ts": "1790000200.000100",
                 "text": f"<@{BOT}> {text}"}
        with patch.object(app_module.rate_limiter, "check", return_value=(True, None)), \
             patch.object(app_module, "_resolve_channel_name", return_value="hjrg-leadership"), \
             patch.object(app_module, "_resolve_bot_user_id"), \
             patch.object(app_module.code_queue, "capture_message_signal") as capture, \
             patch.object(app_module, "_dispatch_qa") as dispatch, \
             patch.object(app_module, "_ca_start_scan") as start:
            app_module.handle_mention(event, say, client)
        return client, dispatch, capture, start

    def test_the_founder_ask_in_a_channel_scans_to_his_dm(self):
        client, dispatch, capture, start = self._run("archive the dead channels")
        assert start.called and start.call_args.kwargs["ack_text"] == intents.CHANNEL_ACK_REPLY
        assert start.call_args.args[1] == "C0B3K67J10T" and start.call_args.args[2] == "1790000200.000100"
        assert not dispatch.called and not capture.called

    def test_a_status_question_in_a_channel_gets_the_ledger_line(self):
        client, dispatch, _, start = self._run("did you archive the dead channels?")
        assert not dispatch.called and not start.called
        assert client.chat_postMessage.call_args.kwargs["text"].startswith("Dead-channel lane:")

    def test_a_member_mention_is_not_intercepted(self):
        _client, dispatch, _capture, start = self._run("archive the dead channels", user=PERSON)
        assert not start.called

    def _run_real_scan_path(self, text):
        """handle_mention with the REAL _ca_start_scan / _ca_run_scan (the scan itself faked)."""
        say, client = MagicMock(), MagicMock()
        event = {"channel": "C0B3K67J10T", "user": HARRISON, "ts": "1790000200.000100",
                 "text": f"<@{BOT}> {text}"}
        with patch.object(app_module.rate_limiter, "check", return_value=(True, None)), \
             patch.object(app_module, "_resolve_channel_name", return_value="hjrg-leadership"), \
             patch.object(app_module, "_resolve_bot_user_id"), \
             patch.object(app_module.code_queue, "capture_message_signal"), \
             patch.object(app_module, "_dispatch_qa") as dispatch:
            app_module.handle_mention(event, say, client)
        app_module._CHANNEL_ARCHIVE_SCAN_POOL.shutdown(wait=True)
        return client, dispatch

    def test_c1_intents_copy_4_a_running_scan_in_this_process_points_at_the_dm(self, monkeypatch):
        """c1-intents-copy#4: the card always lands in Harrison's DM, never in the channel."""
        monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
        assert app_module._CHANNEL_ARCHIVE_SCAN_GUARD.acquire(blocking=False)
        try:
            client, dispatch = self._run_real_scan_path("archive the dead channels")
        finally:
            app_module._CHANNEL_ARCHIVE_SCAN_GUARD.release()
        posts = client.chat_postMessage.call_args_list
        assert [c.kwargs["text"] for c in posts] == [intents.SCAN_RUNNING_CHANNEL_REPLY]
        assert posts[0].kwargs["thread_ts"] == "1790000200.000100" and not dispatch.called
        assert "your DM" in intents.SCAN_RUNNING_CHANNEL_REPLY

    def test_c1_intents_copy_4_the_cross_process_lock_loser_points_at_the_dm(self, monkeypatch):
        monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
        monkeypatch.setattr(deliver, "deliver_proposal",
                            lambda **kw: {"delivered": False, "reason": "scan_running"})
        client, _ = self._run_real_scan_path("archive the dead channels")
        assert [c.kwargs["text"] for c in client.chat_postMessage.call_args_list] == [
            intents.CHANNEL_ACK_REPLY, intents.SCAN_RUNNING_CHANNEL_REPLY]

    @pytest.mark.parametrize("text", [
        "how do I archive a channel in slack?", "which archived reports cover the retail channel?",
        "what did we decide in <#C0B2T18R3FG|hjr-archive-2024>?", "archive the amazon channel report in drive",
        "which channels are safe to archive?", "is the proposal for walmart archived?",
    ])
    def test_r1_must_not_fire_reaches_the_model(self, text):
        client, dispatch, capture, start = self._run(text)
        assert dispatch.called and not start.called, text
        assert not client.chat_postMessage.called, text

    @pytest.mark.parametrize("text", ["archive the dead channels, please", "can we archive the dead channels?",
                                      "archive the dead channels \U0001f64f"])
    def test_r1_natural_asks_scan(self, text):
        client, dispatch, capture, start = self._run(text)
        assert start.called and not dispatch.called and not capture.called, text

    def test_r1_a_lane_status_question_still_gets_the_ledger_line(self):
        client, dispatch, _, start = self._run("how many channels have you archived?")
        assert not dispatch.called and not start.called
        assert client.chat_postMessage.call_args.kwargs["text"].startswith("Dead-channel lane:")

    def test_r1_a_sales_channel_attempt_is_not_refused(self):
        client, dispatch, _, start = self._run("archive the dtc channel tasks")
        assert dispatch.called and not client.chat_postMessage.called


class TestFounderSlashAskR1:
    """integration#4 (SPLIT -> fix): /cora-ask is a third founder entry point -- the same
    intents as a channel @mention, answered by code top-level, never the model."""

    def _run(self, text, user=HARRISON):
        client = MagicMock()
        body = {"channel_id": "C0B3K67J10T", "user_id": user, "text": text}
        with patch.object(app_module.rate_limiter, "check", return_value=(True, None)), \
             patch.object(app_module, "_resolve_channel_name", return_value="hjrg-leadership"), \
             patch.object(app_module, "_resolve_bot_user_id"), \
             patch.object(app_module.user_access, "check_access", return_value=None), \
             patch.object(app_module.sibling_guard, "check_redirect", return_value=None), \
             patch.object(app_module.cross_entity_guard, "check_cross_entity", return_value=None), \
             patch.object(app_module.code_queue, "capture_message_signal") as capture, \
             patch.object(app_module, "_dispatch_qa") as dispatch, \
             patch.object(app_module, "_ca_start_scan") as start:
            app_module.handle_cora_ask(MagicMock(), body, client)
        return client, dispatch, capture, start

    def test_the_founder_ask_scans_to_his_dm(self):
        client, dispatch, capture, start = self._run("archive the dead channels")
        assert start.called and not dispatch.called and not capture.called
        assert start.call_args.args[1] == "C0B3K67J10T" and start.call_args.args[2] is None
        assert start.call_args.kwargs["ack_text"] == intents.CHANNEL_ACK_REPLY
        assert start.call_args.kwargs["running_text"] == intents.SCAN_RUNNING_CHANNEL_REPLY

    def test_a_status_question_gets_the_ledger_line_top_level(self):
        client, dispatch, _, start = self._run("did you archive the dead channels?")
        assert not dispatch.called and not start.called
        kw = client.chat_postMessage.call_args.kwargs
        assert kw["text"].startswith("Dead-channel lane:") and kw["thread_ts"] is None

    def test_an_attempt_gets_the_attempt_reply(self):
        client, dispatch, _, _ = self._run("archive <#C0B2T18R3FG|social>")
        assert not dispatch.called
        assert client.chat_postMessage.call_args.kwargs["text"] == intents.ATTEMPT_REPLY

    @pytest.mark.parametrize("text,user", [("archive the dead channels", PERSON),
                                           ("how do I archive a channel in slack?", HARRISON)])
    def test_a_member_or_a_non_lane_question_reaches_the_model(self, text, user):
        client, dispatch, _, start = self._run(text, user=user)
        assert dispatch.called and not start.called and not client.chat_postMessage.called


def _cand(mmc, text, user):
    return mmc.Candidate(channel_id="DHARRISON1" if user == HARRISON else "DP", channel_name="dm",
                         is_dm=True, user_id=user, text=text, event_ts="1790000300.000100",
                         reply_thread_ts=None, root_thread_ts=None, detection_tier="dm")


class TestCatchup:
    @pytest.mark.parametrize("text", ["archive the dead channels", "archive #old-promo channel",
                                      "did the dead channels get archived?",
                                      "<@U0B44MDGC5R>, archive the dead channels",
                                      "<@U0B44MDGC5R>: can you archive the dead channels?"])
    def test_a_missed_founder_archive_request_drafts_the_fixed_line(self, monkeypatch, text):
        from cora import missed_message_catchup as mmc
        monkeypatch.setattr(mmc, "HARRISON_ID", HARRISON)
        monkeypatch.setattr(mmc, "_run_dispatch_capture",
                            lambda *a, **k: pytest.fail("the model must not draft this"))
        out = mmc.generate_draft(MagicMock(), _cand(mmc, text, HARRISON))
        assert out.status == "draft" and out.draft_text == intents.CATCHUP_DRAFT

    @pytest.mark.parametrize("text", ["how do I archive a channel in slack?",
                                      "archive the retail channel deals that closed lost",
                                      "what did we decide in <#C0B2T18R3FG|hjr-archive-2024>?"])
    def test_r1_a_founder_question_that_is_not_a_lane_request_drafts_normally(self, monkeypatch, text):
        from cora import missed_message_catchup as mmc
        monkeypatch.setattr(mmc, "HARRISON_ID", HARRISON)
        monkeypatch.setattr(mmc, "_run_dispatch_capture", lambda *a, **k: "a model draft")
        monkeypatch.setattr(mmc.user_access, "check_access", lambda *a, **k: None)
        out = mmc.generate_draft(MagicMock(), _cand(mmc, text, HARRISON))
        assert out.draft_text == "a model draft", text

    def test_a_member_archive_text_still_drafts_normally(self, monkeypatch):
        from cora import missed_message_catchup as mmc
        monkeypatch.setattr(mmc, "_run_dispatch_capture", lambda *a, **k: "a model draft")
        monkeypatch.setattr(mmc.user_access, "check_access", lambda *a, **k: None)
        out = mmc.generate_draft(MagicMock(), _cand(mmc, "archive the dead channels", PERSON))
        assert out.draft_text == "a model draft"


# ── D-051 round 2 (Code #16) ────────────────────────────────────────────────
PID2 = "chanarch-111122223333"


def _rows(*cids):
    return [{"cid": c, "section": "A", "tier": "T0", "name": f"fx-{c.lower()}",
             "name_fp": reg.name_fp(f"fx-{c.lower()}"), "is_private": False, "last_person_days": 200,
             "history_complete": True, "keep_count": 0, "lex": False} for c in cids]


def _partial_card(pid, *, created, page1_ts, n_pages=2):
    """A card whose page 1 posted and whose later pages did not (c1-state-machine#6)."""
    st.append_event("staged", proposal_id=pid, ts=created, expires_ts=created + 14 * DAY,
                    rows=_rows("C0PARTAAA1", "C0PARTAAA2"), counts={}, n_pages=n_pages)
    st.append_event("delivered", proposal_id=pid, page=1, dm_channel="DHARRISON1",
                    message_ts=page1_ts, rendered_cids=["C0PARTAAA1"], buttons=True, ts=created)
    st.append_event("delivery_failed", proposal_id=pid, page=2, error="post_failed:ratelimited", ts=created)


def _real_missing_parts_line():
    seen: list[dict] = []
    writer = MagicMock()
    writer.chat_postMessage.side_effect = lambda **kw: seen.append(kw) or {"ok": True}
    deliver._say_missing_parts(writer, "DHARRISON1", 1, 2, "post_failed:ratelimited")
    return seen[0]["text"]


@pytest.fixture
def quiet_dm(dm, monkeypatch):
    """No live gap ask / knowledge-check cycle: a bare 'yes' can only be the card's."""
    monkeypatch.setattr(app_module.gap_autofill, "has_live_ask", lambda uid: False)
    monkeypatch.setattr(app_module.knowledge_check, "has_live_cycle", lambda uid: False)
    return dm


class TestPartialCardR2:
    """r2:c1-intents-copy#2 / c1-state-machine#1 / harness-isolation#1 / integration#1:
    after a partly delivered card, deliver's own 'card is incomplete' line is the
    newest bot message in the DM -- it is a LANE line, so a bare 'yes' is still the
    card's and never reaches the model."""

    def test_a_bare_yes_after_the_missing_parts_line_gets_the_code_reply(self, quiet_dm):
        now = time.time()
        _partial_card(PID2, created=now - 10, page1_ts=f"{now - 5:.6f}")
        client = MagicMock()
        client.conversations_history.return_value = _history(
            {"ts": f"{now - 4:.6f}", "bot_id": "BCORA", "user": BOT, "text": _real_missing_parts_line()})
        before = quiet_dm.qa.call_count
        app_module.handle_message_event(_event("yes"), client)
        assert _texts(client) == [intents.followup_reply()]
        assert quiet_dm.qa.call_count == before and not quiet_dm.capture.called


class TestEveryLiveCardR2:
    """r2:integration#2: a newer partly delivered (or blind) card supersedes nothing, so
    the older complete card stays live and its buttons still act -- a bare 'yes' typed
    in ITS thread is still a card follow-up, never a model turn."""

    def _older_complete_card(self):
        now = time.time()
        a_ts = f"{now - 20:.6f}"
        st.append_event("staged", proposal_id=PID, ts=now - 30, expires_ts=now + 14 * DAY,
                        rows=_rows(A1, "C0DEADAAA2"), counts={}, n_pages=1)
        st.append_event("delivered", proposal_id=PID, page=1, dm_channel="DHARRISON1",
                        message_ts=a_ts, rendered_cids=[A1, "C0DEADAAA2"], buttons=True, ts=now - 30)
        return now, a_ts

    def test_a_yes_in_the_older_live_cards_thread_after_a_partial_newer_card(self, quiet_dm):
        now, a_ts = self._older_complete_card()
        _partial_card(PID2, created=now - 10, page1_ts=f"{now - 5:.6f}")
        f = st.fold()
        assert f.is_live(PID, time.time()) and f.live_proposal(time.time()).proposal_id == PID2
        client = MagicMock()
        app_module.handle_message_event(_event("yes", thread_ts=a_ts), client)
        assert _texts(client) == [intents.followup_reply()] and not quiet_dm.qa.called

    def test_a_yes_in_the_older_live_cards_thread_after_a_blind_newer_card(self, quiet_dm):
        now, a_ts = self._older_complete_card()
        st.append_event("staged", proposal_id=PID2, ts=now - 10, expires_ts=now + 14 * DAY, rows=[],
                        counts={}, n_pages=1, blind="list_incomplete")
        st.append_event("delivered", proposal_id=PID2, page=1, dm_channel="DHARRISON1",
                        message_ts=f"{now - 5:.6f}", rendered_cids=[], buttons=True, ts=now - 10)
        client = MagicMock()
        app_module.handle_message_event(_event("ok", thread_ts=a_ts), client)
        assert _texts(client) == [intents.followup_reply()] and not quiet_dm.qa.called


class TestAffirmativesR2:
    """r2:c1-intents-copy#0: the natural card confirmations get the A21(d) code reply
    through the REAL handle_message_event; replies with content still reach the model."""

    @pytest.mark.parametrize("text", [
        "yes, go ahead", "Yes, go ahead.", "yes go ahead", "ok do it", "ok, do it", "go for it", "yes do it",
        "sounds good", "sounds good, go ahead", "yes please do", "ok go", "sure, go ahead", "yup",
        "yep go ahead", "\U0001f44d", ":+1:", ":thumbsup:", "go ahead and do it", "yes confirm",
        "yes — do it", "cora, do it", "cora go ahead", "go ahead, cora", "<@U0B44MDGC5R> yes, go ahead",
    ])
    def test_a_natural_confirmation_of_a_live_card_gets_the_code_reply(self, quiet_dm, text):
        _live_card()
        client = MagicMock()
        app_module.handle_message_event(_event(text), client)
        assert _texts(client) == [intents.followup_reply()], text
        assert not quiet_dm.qa.called and not quiet_dm.capture.called, text

    @pytest.mark.parametrize("text", [
        "ok thanks for that", "yes, and also send the report", "ok thanks", "yes but hold off",
        "go ahead and send it to tommy", "sure, what's the 13-week view?", "ok let me check",
        "confirm the order", "do it later", "yes cancel it", "sounds good, but keep #social",
        "go ahead and book the flight", "yes please send the report",
    ])
    def test_a_reply_with_content_is_not_the_cards(self, quiet_dm, text):
        _live_card()
        client = MagicMock()
        app_module.handle_message_event(_event(text), client)
        assert intents.FOLLOWUP_REPLY_LEAD not in " ".join(t or "" for t in _texts(client)), text
        assert quiet_dm.qa.called, text

    def test_a_widened_yes_still_yields_to_cora_s_newer_question(self, quiet_dm):
        _live_card()
        client = MagicMock()
        client.conversations_history.return_value = _history(
            {"ts": f"{time.time() - 1:.6f}", "bot_id": "BCORA", "user": BOT,
             "text": "Cash is $1.2M. Want the 13-week view too?"})
        app_module.handle_message_event(_event("yes, go ahead"), client)
        assert quiet_dm.qa.called
        assert intents.FOLLOWUP_REPLY_LEAD not in " ".join(t or "" for t in _texts(client))


ASK_FIRE_R2 = [
    "archive the dead channels, cora", "archive the dead channels please cora", "archive dead channels for me cora",
    "archive the dead channels again", "archive the dead channels. thanks!", "archive the dead channels — thanks",
    "archive the dead channels when you can", "archive the old dead channels",
    "can you archive all of our dead channels?", "archive every dead channel",
    "archive the dead and inactive channels", "archive dead/inactive channels", "archive the dead, inactive channels",
]


class TestAskGrammarR2:
    """r2:c1-intents-copy#1: the full ask with a trailing vocative / 'thanks' sentence /
    'again' / 'every' / two deadness adjectives starts the scan on every founder
    surface -- never the refusal telling him to type what he typed."""

    @pytest.mark.parametrize("text", ASK_FIRE_R2)
    def test_the_dm_ask_starts_the_scan(self, dm, text):
        client = MagicMock()
        app_module.handle_message_event(_event(text), client)
        assert dm.done.wait(5), text
        assert _texts(client) == [intents.ACK_REPLY] and not dm.qa.called and not dm.capture.called, text

    @pytest.mark.parametrize("text", ["archive the dead channels, cora, except #social",
                                      "archive the dead channels for osn", "archive the dead or live channels"])
    def test_an_exception_or_scope_in_a_dm_is_not_the_scan(self, dm, text):
        client = MagicMock()
        app_module.handle_message_event(_event(text), client)
        assert not dm.scans and intents.ACK_REPLY not in _texts(client), text

    @pytest.mark.parametrize("text", ASK_FIRE_R2)
    def test_the_mention_ask_starts_the_scan(self, text):
        client, dispatch, capture, start = TestFounderMention()._run(text)
        assert start.called and start.call_args.kwargs["ack_text"] == intents.CHANNEL_ACK_REPLY, text
        assert not dispatch.called and not capture.called and not client.chat_postMessage.called, text

    @pytest.mark.parametrize("text", ["archive the dead channels. thanks!", "archive every dead channel",
                                      "archive dead/inactive channels"])
    def test_the_slash_ask_starts_the_scan(self, text):
        client, dispatch, capture, start = TestFounderSlashAskR1()._run(text)
        assert start.called and not dispatch.called and not capture.called, text

    @pytest.mark.parametrize("text", ["archive the promo and event channels", "archive promo/event channels",
                                      "archive the promo, event and launch channels"])
    def test_a_coordinated_channel_object_gets_the_attempt_reply_not_the_model(self, dm, text):
        """r2:c1-intents-copy#1 (a): A21(b) -- an archive request whose object is channels."""
        client = MagicMock()
        app_module.handle_message_event(_event(text), client)
        assert _texts(client) == [intents.ATTEMPT_REPLY] and not dm.qa.called and not dm.scans, text
        client, dispatch, _, start = TestFounderMention()._run(text)
        assert client.chat_postMessage.call_args.kwargs["text"] == intents.ATTEMPT_REPLY
        assert not dispatch.called and not start.called, text

    @pytest.mark.parametrize("text", ["archive it and tell the channel", "archive my inbox and the channel",
                                      "archive the emails and notify the channel"])
    def test_two_objects_or_a_second_clause_reach_the_model(self, dm, text):
        client = MagicMock()
        app_module.handle_message_event(_event(text), client)
        assert dm.qa.called and _texts(client) == [], text
