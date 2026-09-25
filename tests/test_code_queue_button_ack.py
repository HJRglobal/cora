"""Code-queue button ack + streaming fallback (Code #12 D-051 remediation).

  * B MED #6: an outcome that changed NOTHING (no_evidence / refused / error / inflight)
    never consumes a single-item card -- the reply threads and the buttons stay;
    a state-changing outcome still replaces the buttons with the outcome (Rider D);
  * F MED #3: a modal ack with no card pointer is logged, never swallowed;
  * A MED #8: when the final streaming chat_update fails, the placeholder is retried
    TEXT-ONLY with the screened text before a fresh reply is posted.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from unittest.mock import MagicMock, patch

import cora.app as app_module
from cora import code_queue as cq

HARRISON = "U0B2RM2JYJ1"


def _body(value="cq-aaaaaaaaaaaa", n_actions=1):
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "card"}}]
    for i in range(n_actions):
        blocks.append({"type": "actions", "block_id": f"a{i}", "elements": []})
    return {"actions": [{"value": value}], "user": {"id": HARRISON},
            "channel": {"id": "D1"}, "message": {"ts": "9.9", "blocks": blocks}}


class TestCardKeptOnNoChange:
    def test_no_evidence_threads_and_keeps_the_buttons(self):
        client = MagicMock()
        with patch.object(cq, "process_queue_action", return_value=("no_evidence", "NOT staged -- no evidence")):
            app_module._handle_code_queue_button(_body(), client, cq.ACTION_STAGE)
        client.chat_update.assert_not_called()
        kw = client.chat_postMessage.call_args.kwargs
        assert kw["channel"] == "D1" and kw["thread_ts"] == "9.9" and "NOT staged" in kw["text"]

    def test_refused_error_inflight_thread_too(self):
        for outcome in ("refused", "error", "inflight"):
            client = MagicMock()
            with patch.object(cq, "process_queue_action", return_value=(outcome, "nothing changed")):
                app_module._handle_code_queue_button(_body(), client, cq.ACTION_MARK_SHIPPED)
            client.chat_update.assert_not_called(), outcome
            assert client.chat_postMessage.call_args.kwargs["thread_ts"] == "9.9"

    def test_state_change_still_consumes_a_single_card(self):
        client = MagicMock()
        with patch.object(cq, "process_queue_action", return_value=("approved", "Queued (APPROVED).")):
            app_module._handle_code_queue_button(_body(), client, cq.ACTION_APPROVE)
        client.chat_postMessage.assert_not_called()
        kw = client.chat_update.call_args.kwargs
        assert kw["ts"] == "9.9" and not [b for b in kw["blocks"] if b.get("type") == "actions"]

    def test_menu_press_rerenders_only_the_pressed_row(self):
        """Code #15 S5 (cq-2d26f131091e) INVERTS the old pin (was
        test_menu_message_always_threads, asserting chat_update.assert_not_called()):
        that branch is what left all 21 rows of the 9/21 menu buttoned after 29
        presses. A press now re-renders ONLY the row the ledger shows decided since
        the card went out, keeps the menu's fallback text, and still threads."""
        ids = ["cq-aaaaaaaaaaa1", "cq-aaaaaaaaaaa2", "cq-aaaaaaaaaaa3"]
        card_ts = "1789999254.143349"
        for cid in ids:
            cq._append_event({"event": "captured", "id": cid, "ts": "2026-09-20T10:00:00+00:00",
                              "status": "PROPOSED", "title": "t", "entity": "F3E", "kind": "bug"})
        cq._append_event({"event": "approved", "id": ids[1], "ts": "2026-09-21T15:00:00+00:00"})
        blocks = []
        for cid in ids:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"row {cid}"}})
            blocks.append({"type": "actions", "block_id": f"cq_proposed_{cid}", "elements": [
                {"type": "button", "action_id": aid, "value": cid,
                 "text": {"type": "plain_text", "text": aid}}
                for aid in (cq.ACTION_APPROVE, cq.ACTION_PARK, cq.ACTION_DISMISS_NOTE)]})
        body = {"actions": [{"value": ids[1]}], "user": {"id": HARRISON}, "channel": {"id": "D1"},
                "message": {"ts": card_ts, "text": "menu fallback", "blocks": blocks}}
        client = MagicMock()
        with patch.object(cq, "process_queue_action", return_value=("approved", "ok")):
            app_module._handle_code_queue_button(body, client, cq.ACTION_APPROVE)
        client.chat_update.assert_called_once()
        kw = client.chat_update.call_args.kwargs
        assert kw["ts"] == card_ts and kw["text"] == "menu fallback"
        new = kw["blocks"]
        assert len(new) == len(blocks)
        assert new[3]["type"] == "context" and new[3]["block_id"] == f"cq_done_proposed_{ids[1]}"
        assert new[1] == blocks[1] and new[5] == blocks[5]       # the other rows keep their buttons
        assert [new[0], new[2], new[4]] == [blocks[0], blocks[2], blocks[4]]
        assert client.chat_postMessage.call_args.kwargs["thread_ts"] == card_ts

    def test_a_non_queue_multi_action_message_is_threaded_not_edited(self):
        client = MagicMock()
        with patch.object(cq, "process_queue_action", return_value=("approved", "ok")):
            app_module._handle_code_queue_button(_body(n_actions=3), client, cq.ACTION_APPROVE)
        client.chat_update.assert_not_called()
        assert client.chat_postMessage.call_args.kwargs["thread_ts"] == "9.9"

    def test_bundle_floor_refusal_keeps_the_menu_row(self):
        client = MagicMock()
        with patch.object(cq, "stage_bundle", return_value=("no_evidence", "NOT staged -- no item carries evidence")):
            app_module._handle_code_queue_button(_body(value="bundle:cq-a,cq-b", n_actions=2), client, cq.ACTION_STAGE)
        client.chat_update.assert_not_called()
        assert "NOT staged" in client.chat_postMessage.call_args.kwargs["text"]


class TestKickoffOffTheListenerPool:
    """D-051 Code #15 s6#0: a press that can GENERATE a kickoff (Stage, a Stage-bundle
    row, Queue on a P0/P1 row) used to run the whole Sonnet call on Bolt's shared
    5-worker listener pool after ack(); a burst of presses left the next press's ack
    queued past Slack's 3s window (live 8/17 07:29 x5, 8/24 07:02). The listener now
    acks, hands the body to the cq-kickoff pool and returns; the outcome still posts."""

    def _blocking_pqa(self, gate, seen):
        def _pqa(action_id, value, actor_id, **kw):
            seen.append(threading.current_thread().name)
            assert gate.wait(10)
            return "approved", "Queued (APPROVED)."
        return _pqa

    def test_stage_and_queue_listeners_return_before_generation_finishes(self):
        for handler, action in ((app_module.handle_cq_stage, cq.ACTION_STAGE),
                                (app_module.handle_cq_approve, cq.ACTION_APPROVE)):
            gate, seen = threading.Event(), []
            client, ack = MagicMock(), MagicMock()
            with patch.object(cq, "process_queue_action", self._blocking_pqa(gate, seen)):
                t = threading.Thread(target=handler, args=(ack, _body(), client))
                t.start()
                t.join(2)
                try:
                    assert not t.is_alive(), f"{action}: the listener waited for the generation"
                    ack.assert_called_once_with()
                    client.chat_update.assert_not_called()      # the outcome is not in yet
                finally:
                    gate.set()
                    t.join(5)
                deadline = time.monotonic() + 5
                while not client.chat_update.called and time.monotonic() < deadline:
                    time.sleep(0.01)
            # the outcome still lands (a single card: consumed), from the cq-kickoff pool
            kw = client.chat_update.call_args.kwargs
            assert kw["ts"] == "9.9" and "Queued" in kw["text"], action
            assert seen and seen[0].startswith("cq-kickoff"), seen

    def test_quick_presses_stay_inline(self):
        """Keep / Dismiss / Later / Mark shipped generate nothing and must not queue
        behind a generation on the two-worker pool."""
        for handler, action in ((app_module.handle_cq_keep, cq.ACTION_KEEP),
                                (app_module.handle_cq_dismiss, cq.ACTION_DISMISS),
                                (app_module.handle_cq_later, cq.ACTION_LATER),
                                (app_module.handle_cq_shipped, cq.ACTION_MARK_SHIPPED)):
            seen: list[str] = []
            with patch.object(cq, "process_queue_action",
                              lambda *a, **k: (seen.append(threading.current_thread().name),
                                               ("noop", "ok"))[1]):
                handler(MagicMock(), _body(), MagicMock())
            assert seen == [threading.current_thread().name], action

    def test_a_burst_of_presses_through_bolt_acks_every_one_in_time(self, monkeypatch):
        """The incident path, through the REAL Bolt dispatch on Cora's own App (default
        5-worker listener pool): six Stage presses 50ms apart while each generation
        holds for longer than the ack window. Before: presses 0-4 took every worker and
        press 5 was never acked (404 -> Slack's 'app did not respond'). ack_timeout is
        lowered from 3s (and the stand-in generation to 1s) so the test runs in about
        three seconds."""
        from slack_bolt import BoltRequest
        from slack_bolt.middleware.authorization.single_team_authorization import (
            SingleTeamAuthorization)
        from slack_sdk.web.client import WebClient
        from slack_sdk.web.slack_response import SlackResponse

        data = {"ok": True, "url": "https://test.slack.com/", "user_id": "U_CORA_TEST",
                "team": "T", "user": "bot", "team_id": "T_TEST", "bot_id": "B_TEST"}

        def _auth(self, **kw):
            return SlackResponse(client=self, http_verb="POST", api_url="auth.test", req_args={},
                                 data=data, headers={"x-oauth-scopes": "chat:write"},
                                 status_code=200)
        monkeypatch.setattr(WebClient, "auth_test", _auth)
        bolt = app_module.app
        for m in bolt._middleware_list:
            if isinstance(m, SingleTeamAuthorization):
                monkeypatch.setattr(m, "auth_test_result", _auth(bolt.client))
        listener = next(li for li in bolt._listeners
                        if getattr(li, "ack_function", None) is app_module.handle_cq_stage)
        monkeypatch.setattr(listener, "ack_timeout", 0.5)
        assert bolt._listener_runner.listener_executor._max_workers == 5   # the shared pool

        done: list[str] = []
        done_lock = threading.Lock()

        def _pqa(action_id, value, actor_id, **kw):
            time.sleep(1.0)              # a generation: twice the ack window
            return "staged", "Prompt staged"

        def _ack_in_message(client, body, msg, **kw):
            with done_lock:
                done.append(body["actions"][0]["value"])
        monkeypatch.setattr(cq, "process_queue_action", _pqa)
        monkeypatch.setattr(app_module, "_cq_ack_in_message", _ack_in_message)

        def _payload(i):
            return {"type": "block_actions", "user": {"id": HARRISON}, "team": {"id": "T_TEST"},
                    "api_app_id": "A1", "token": "t", "trigger_id": f"trig{i}",
                    "container": {"type": "message", "message_ts": f"1.{i}", "channel_id": "D1"},
                    "channel": {"id": "D1"}, "message": {"ts": f"1.{i}", "blocks": []},
                    "actions": [{"type": "button", "action_id": cq.ACTION_STAGE,
                                 "block_id": f"b{i}", "value": f"cq-00000000000{i}",
                                 "action_ts": "1"}]}
        results: list = [None] * 6

        def _one(i):
            t0 = time.monotonic()
            r = bolt.dispatch(BoltRequest(mode="socket_mode", body=_payload(i)))
            results[i] = (r.status, time.monotonic() - t0)
        threads = []
        for i in range(6):
            th = threading.Thread(target=_one, args=(i,))
            th.start()
            threads.append(th)
            time.sleep(0.05)
        for th in threads:
            th.join(10)
        # drain every press BEFORE asserting, so no body outlives this test's patches
        deadline = time.monotonic() + 10
        while len(done) < 6 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert [s for s, _t in results] == [200] * 6, results
        assert sorted(done) == sorted(f"cq-00000000000{i}" for i in range(6))   # every outcome posts


class TestAckPointers:
    def test_missing_card_pointer_is_logged_not_swallowed(self, caplog):
        caplog.set_level(logging.WARNING, logger=app_module.log.name)
        client = MagicMock()
        app_module._cq_ack_view_submit(client, {}, "Parked -- reason")
        client.chat_postMessage.assert_not_called()
        assert any("no card pointer" in r.getMessage() for r in caplog.records)
        app_module._cq_ack_in_message(client, {"message": {}, "channel": {}}, "outcome text")
        assert any("no channel/ts pointer" in r.getMessage() for r in caplog.records)

    def test_ack_view_once_passes_kwargs_and_never_raises(self):
        ack = MagicMock()
        app_module._ack_view_once(ack, response_action="errors", errors={"x": "y"})
        ack.assert_called_once_with(response_action="errors", errors={"x": "y"})
        app_module._ack_view_once(MagicMock(side_effect=RuntimeError("late")))  # swallowed, logged


class TestStreamingFallback:
    def test_final_update_failure_retries_text_only_before_a_fresh_post(self):
        src = inspect.getsource(app_module._dispatch_qa)
        i_fail = src.index("Final chat_update failed for ts=%s")
        i_retry = src.index("client.chat_update(channel=placeholder_channel, ts=placeholder_ts, text=response_text)")
        i_fresh = src.index("text-only chat_update also failed", i_retry)
        assert i_fail < i_retry < i_fresh
        # the retry carries the SCREENED text (response_text is screened above the card build)
        screen = src.index("slack_egress.screen_phantom_write_claims(", src.index("# ── Streaming path ──"))
        assert screen < i_retry
