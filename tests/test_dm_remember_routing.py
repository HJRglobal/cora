"""v2 S3 (cq-67490abe2d86): a DM "remember that X" must mint a remember stash,
not get swallowed by the pending gap-ask capture.

Live 8/3: "Cora, remember the cobalt falcon is the staging box" was captured as
the ANSWER to an open knowledge-gap ask and proposed to Harrison as a bogus
known-answer. Nothing was ever saved to the user's notes -- a pure mis-route,
no data loss.

Two causes, both fixed here:
  1. The top-level gap-ask match is greedy. It already declines shift keywords
     and interrogative text; a staged-write COMMAND is the same class of "this
     plainly is not an answer" and now declines too.
  2. _REMEMBER_INTENT_RE was anchored at ^remember, but a DM carries no <@Uxxx>
     token to strip, so the real phrasing starts "Cora, remember ..." and never
     matched at all -- which also meant the Sonnet-force escalation missed it.

The THREADED gap-ask path is deliberately untouched: an answer typed in the
ask's own thread still always matches, even if it contains the word "remember".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402

import cora.app as app_mod  # noqa: E402

USER_ID = "U0DM"


class TestRememberIntentDetection:
    @pytest.mark.parametrize("text", [
        "Cora, remember the cobalt falcon is the staging box",  # the live phrasing
        "cora remember the door code is 4412",
        "Hey Cora, remember that Q3 kicks off Monday",
        "@Cora remember the vendor is Apex Appliance",
        "Cora: note that Tommy is out Friday",
        "remember the Tucson stove vendor is Apex",
        "please remember the gate code",
        "note that the rent is due on the 3rd",
        "make a note the shipment slipped a week",
    ])
    def test_recognised_as_a_staged_write_command(self, text):
        assert app_mod._remember_or_forget_intent(text) is True

    @pytest.mark.parametrize("text", [
        "Cora, forget that note about the vendor",
        "forget the note on parking",
        "please delete my note about the gate code",
        "remove that note",
    ])
    def test_forget_variants_recognised(self, text):
        assert app_mod._remember_or_forget_intent(text) is True

    @pytest.mark.parametrize("text", [
        "Do you remember the vendor?",          # a question, not a command
        "Cora, do you remember the vendor?",
        "I will remember to send it",           # not anchored
        "what did Cora remember about this",
        "the cobalt falcon is the staging box",  # a bare fact = a gap answer
        "",
    ])
    def test_not_a_staged_write_command(self, text):
        assert app_mod._remember_or_forget_intent(text) is False


def _dm_event(text: str, thread_ts: str | None = None) -> dict:
    ev = {"channel_type": "im", "user": USER_ID, "text": text,
          "channel": "D0TEST", "ts": "100.1"}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    return ev


def _run_dm(text: str, thread_ts: str | None = None):
    """Drive handle_message_event's DM branch; return the match_pending_ask mock
    and the _handle_dm_qa mock so a test can assert which path won."""
    match = MagicMock(return_value=None)
    dm_qa = MagicMock()
    with patch.object(app_mod.gap_autofill, "match_pending_ask", match), \
         patch.object(app_mod.gap_autofill, "is_shift_keyword", return_value=False), \
         patch.object(app_mod, "_handle_dm_qa", dm_qa), \
         patch.object(app_mod.historical_access, "detect_retrieval_intent",
                      return_value=False), \
         patch.object(app_mod, "_dm_is_shift_message", return_value=False):
        app_mod.handle_message_event(_dm_event(text, thread_ts), MagicMock())
    return match, dm_qa


class TestDmRoutingOrder:
    def test_remember_command_does_not_allow_toplevel_gap_capture(self):
        match, dm_qa = _run_dm("Cora, remember the cobalt falcon is the staging box")
        match.assert_called_once()
        assert match.call_args.kwargs["allow_toplevel"] is False, \
            "a remember command must not be eligible for top-level gap capture"
        dm_qa.assert_called_once()

    def test_forget_command_does_not_allow_toplevel_gap_capture(self):
        match, dm_qa = _run_dm("Cora, forget that note about parking")
        assert match.call_args.kwargs["allow_toplevel"] is False
        dm_qa.assert_called_once()

    def test_a_plain_statement_is_still_gap_capturable(self):
        """The gap-ask flow must keep working: a bare fact IS a plausible answer."""
        match, _ = _run_dm("the cobalt falcon is the staging box")
        assert match.call_args.kwargs["allow_toplevel"] is True

    def test_a_question_is_still_not_gap_capturable(self):
        match, _ = _run_dm("what is our cash position?")
        assert match.call_args.kwargs["allow_toplevel"] is False

    def test_threaded_reply_still_matches_regardless_of_wording(self):
        """allow_toplevel only gates the TOP-LEVEL branch; a threaded answer
        containing 'remember' must still reach match_pending_ask, which ignores
        the flag when thread_ts is present."""
        match, _ = _run_dm("Cora, remember it is the staging box", thread_ts="99.9")
        match.assert_called_once()
        assert match.call_args.args[1] == "99.9", "the thread ts is still passed through"

    def test_a_captured_gap_answer_short_circuits_before_dm_qa(self):
        """Control: when an ask genuinely matches, the DM Q&A path is skipped."""
        match = MagicMock(return_value={"ask_id": "a1"})
        dm_qa = MagicMock()
        with patch.object(app_mod.gap_autofill, "match_pending_ask", match), \
             patch.object(app_mod.gap_autofill, "is_shift_keyword", return_value=False), \
             patch.object(app_mod.gap_autofill, "record_ask_answer", return_value="Got it."), \
             patch.object(app_mod, "_handle_dm_qa", dm_qa), \
             patch.object(app_mod.historical_access, "detect_retrieval_intent",
                          return_value=False):
            app_mod.handle_message_event(
                _dm_event("the staging box is the cobalt falcon"), MagicMock())
        dm_qa.assert_not_called()
