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

import pytest

import cora.app as app_module
from cora import code_queue as cq

from test_code_queue import qenv  # noqa: F401 -- shared isolation fixture (TestApproveRouting)

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
        # s5s6#r2-0: only a GENERATING Queue press is offloaded, so the pressed row is a
        # P1 PROPOSED item here (a P2/P3 approve runs inline -- TestApproveRouting).
        # The autouse fixture has _EVENT_LEDGER on a tmp file.
        cq._append_event({"event": "captured", "id": "cq-aaaaaaaaaaaa",
                          "ts": "2026-09-20T10:00:00+00:00", "status": "PROPOSED",
                          "title": "t", "entity": "F3E", "kind": "bug", "severity": "P1"})
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


class TestApproveRouting:
    """D-051 Code #15 r2 s5s6#r2-0: s6#0 sent EVERY Queue press to the two-worker
    kickoff pool, P2/P3 rows included, whose approve never generates. With both
    workers busy (a Monday-menu Stage burst) the press sat invisible, a later inline
    Later / Park on the same row committed first, and the late approve then folded the
    SNOOZED / PARKED row back to APPROVED -- against the founder's last press. Only a
    press that will generate is offloaded now; every other approve runs inline, in
    press order. The pool is the conftest's per-test one, drained at teardown."""

    def _occupy_pool(self, gate):
        started: list[int] = []

        def _hold():
            started.append(1)
            assert gate.wait(10)
        futs = [app_module._CQ_KICKOFF_POOL.submit(_hold) for _ in range(2)]
        deadline = time.monotonic() + 5
        while len(started) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(started) == 2, "both kickoff workers should be busy"
        return futs

    @staticmethod
    def _events(cid):
        return [e.get("event") for e in cq._read_jsonl(cq._EVENT_LEDGER) if e.get("id") == cid]

    def _race(self, cid, second_press):
        """Queue on ``cid`` while both kickoff workers are busy, then the second
        press; returns (status right after the Queue press, final record)."""
        gate = threading.Event()
        futs = self._occupy_pool(gate)
        try:
            app_module.handle_cq_approve(MagicMock(), _body(value=cid), MagicMock())
            after_queue = cq.get_item(cid)["status"]
            second_press()
        finally:
            gate.set()
            for f in futs:
                f.result(10)
        app_module._CQ_KICKOFF_POOL.shutdown(wait=True)   # anything pooled has run
        return after_queue, cq.get_item(cid)

    def test_p3_queue_then_later_ends_snoozed(self, qenv):  # noqa: F811
        cid = cq.seed_item(kind="bug", severity="P3", title="Routing probe row for later",
                           summary="s", entity="F3E", signal="explicit", status="PROPOSED")
        after_queue, rec = self._race(cid, lambda: app_module.handle_cq_later(
            MagicMock(), _body(value=cid), MagicMock()))
        # the founder's LAST press governs (the defect: a late approve folded it to APPROVED)
        assert rec["status"] == "SNOOZED"
        assert self._events(cid) == ["captured", "approved", "snoozed"]
        assert after_queue == "APPROVED"      # inline: on the ledger before the next press

    def test_p3_queue_then_park_ends_parked(self, qenv):  # noqa: F811
        cid = cq.seed_item(kind="bug", severity="P3", title="Park race probe item",
                           summary="s", entity="F3E", signal="explicit", status="PROPOSED")

        def _park():
            outcome, _msg = cq.park_item(cid, HARRISON, "waiting on the vendor",
                                         trigger_event="vendor replies")
            assert outcome == "parked"
        after_queue, rec = self._race(cid, _park)
        assert rec["status"] == "PARKED"
        assert self._events(cid) == ["captured", "approved", "parked"]
        assert after_queue == "APPROVED"

    @pytest.mark.parametrize("severity", ["P0", "P1", "HIGH", "P2", "P3", "LOW", ""])
    def test_routing_matches_exactly_when_process_queue_action_generates(
            self, qenv, monkeypatch, severity):  # noqa: F811
        """The routing predicate must never drift from process_queue_action: a press
        is offloaded iff that approve reaches ensure_kickoff_staged."""
        generated: list[str] = []
        monkeypatch.setattr(cq, "ensure_kickoff_staged",
                            lambda cq_id, **kw: (generated.append(cq_id), ("staged", "p"))[1])
        statuses = ("PROPOSED", "SNOOZED", "PARKED", "BLOCKED", "APPROVED", "STAGED",
                    "DISMISSED", "SHIPPED", "SUPERSEDED")
        for i, status in enumerate(statuses):
            cid = f"cq-{i:012x}"
            cq._append_event({"event": "captured", "id": cid, "ts": "2026-09-20T10:00:00+00:00",
                              "status": status, "title": f"t{i}", "entity": "F3E",
                              "kind": "bug", "severity": severity})
            predicted = app_module._cq_approve_will_generate(_body(value=cid))
            cq.process_queue_action(cq.ACTION_APPROVE, cid, HARRISON)
            assert predicted == (cid in generated), (severity, status, predicted)
        assert app_module._cq_approve_will_generate(_body(value="cq-000000000000")) is False

    def test_a_generating_approve_is_offloaded_and_a_p3_one_is_not(self, qenv, monkeypatch):  # noqa: F811
        seen: list[tuple[str, str]] = []

        def _record(body, client, action_id):
            seen.append((body["actions"][0]["value"], threading.current_thread().name))
        monkeypatch.setattr(app_module, "_handle_code_queue_button", _record)
        p1 = cq.seed_item(kind="bug", severity="P1", title="Offload probe priority row",
                          summary="s", entity="F3E", signal="explicit", status="PROPOSED")
        p3 = cq.seed_item(kind="bug", severity="P3", title="Inline probe minor ask",
                          summary="s", entity="F3E", signal="explicit", status="PROPOSED")
        for cid in (p1, p3):
            app_module.handle_cq_approve(MagicMock(), _body(value=cid), MagicMock())
        app_module._CQ_KICKOFF_POOL.shutdown(wait=True)
        names = dict(seen)
        assert names[p1].startswith("cq-kickoff"), names
        assert names[p3] == threading.current_thread().name, names


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
