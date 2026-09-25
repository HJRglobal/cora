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
from _chanarch_fakes import DAY, HARRISON, NOW, PERSON
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
        assert acted == [f"{PID}:C0DEADAAA2"]                 # A1 decided: no button left
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
        app_module._handle_channel_archive_tap(_body(cards.ACTION_ROW, f"{PID}:{A1}"), client,
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
                        message_ts=f"{time.time():.6f}", rendered_cids=[A1], buttons=True)
        client = MagicMock()
        app_module.handle_message_event(_event("archive them"), client)
        assert "Typed replies don't act" in _texts(client)[-1]
        monkeypatch.setattr(app_module._tool_dispatch, "snapshot_stash_ids", lambda u, c: {"x": ["id1"]})
        client2 = MagicMock()
        app_module.handle_message_event(_event("yes"), client2)
        assert "Typed replies don't act" not in " ".join(t or "" for t in _texts(client2))

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


def _cand(mmc, text, user):
    return mmc.Candidate(channel_id="DHARRISON1" if user == HARRISON else "DP", channel_name="dm",
                         is_dm=True, user_id=user, text=text, event_ts="1790000300.000100",
                         reply_thread_ts=None, root_thread_ts=None, detection_tier="dm")


class TestCatchup:
    @pytest.mark.parametrize("text", ["archive the dead channels", "archive #old-promo channel",
                                      "did the dead channels get archived?"])
    def test_a_missed_founder_archive_request_drafts_the_fixed_line(self, monkeypatch, text):
        from cora import missed_message_catchup as mmc
        monkeypatch.setattr(mmc, "HARRISON_ID", HARRISON)
        monkeypatch.setattr(mmc, "_run_dispatch_capture",
                            lambda *a, **k: pytest.fail("the model must not draft this"))
        out = mmc.generate_draft(MagicMock(), _cand(mmc, text, HARRISON))
        assert out.status == "draft" and out.draft_text == intents.CATCHUP_DRAFT

    def test_a_member_archive_text_still_drafts_normally(self, monkeypatch):
        from cora import missed_message_catchup as mmc
        monkeypatch.setattr(mmc, "_run_dispatch_capture", lambda *a, **k: "a model draft")
        monkeypatch.setattr(mmc.user_access, "check_access", lambda *a, **k: None)
        out = mmc.generate_draft(MagicMock(), _cand(mmc, "archive the dead channels", PERSON))
        assert out.draft_text == "a model draft"
