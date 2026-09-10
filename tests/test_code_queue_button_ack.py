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

    def test_menu_message_always_threads(self):
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
