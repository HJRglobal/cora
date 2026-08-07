"""[QA] smoke-traffic quarantine (D-104) -- completeness across ALL intake surfaces.

Before 2026-08-06 the marker was honoured in exactly one place: gap_autofill's
known-answer MINING eligibility screen, which runs long AFTER a gap is already
logged. A [QA] smoke message therefore still minted knowledge gaps, code-queue
capture cards, and decision-inbox items. These tests pin the quarantine at every
intake chokepoint so the D-104 rider cannot silently regress on one surface.
"""

from unittest.mock import patch

import pytest

from cora import qa_scaffolding as qa


class TestPredicates:
    @pytest.mark.parametrize("text", [
        "[QA] smoke", "  [qa] lower", "*[QA]* bold", "> [QA] quoted",
        "<@U0B44MDGC5R> [QA] mention-prefixed",
    ])
    def test_prefix_forms(self, text):
        assert qa.is_qa_message(text) is True

    @pytest.mark.parametrize("text", [
        "Our [QA] process changed in July",
        "the qa team signed off",
        "",
    ])
    def test_non_prefix_is_not_a_qa_message(self, text):
        assert qa.is_qa_message(text) is False

    def test_contains_marker_is_position_independent(self):
        assert qa.contains_qa_marker("summary of [QA] smoke run") is True
        assert qa.contains_qa_marker("nothing here") is False


class TestGapIntakeQuarantine:
    def test_qa_question_never_logs_a_gap(self, tmp_path, monkeypatch):
        from cora import knowledge_gaps
        monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "g.jsonl"))
        with patch.object(knowledge_gaps, "is_capability_ask") as cap:
            knowledge_gaps.log_gap(
                entity="FNDR", channel="c", user="U1",
                question="[QA] what is our cash position?", response_chars=0,
                gap="unknown", latency_ms=1)
        # Quarantined BEFORE the capability-ask reroute, so a capability-shaped
        # smoke ask cannot become a code-queue feature candidate either.
        assert not cap.called
        assert not (tmp_path / "g.jsonl").exists()

    def test_normal_question_still_logs(self, tmp_path, monkeypatch):
        from cora import knowledge_gaps
        monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "g.jsonl"))
        with patch.object(knowledge_gaps, "is_capability_ask", return_value=False):
            knowledge_gaps.log_gap(
                entity="FNDR", channel="c", user="U1",
                question="what is our cash position?", response_chars=0,
                gap="unknown", latency_ms=1)
        assert (tmp_path / "g.jsonl").exists()


class TestCodeQueueQuarantine:
    """R7(b): the original version of this test was VACUOUS. Its text tripped
    neither _PHRASE_RE nor the deflection regex, so the assertion passed even with
    the quarantine deleted. The text below deliberately trips _PHRASE_RE ("cora
    should"), and the companion test proves that -- so this pair fails if the
    quarantine is removed."""

    def test_qa_message_never_captures_a_signal(self):
        from cora import code_queue
        with patch.object(code_queue, "code_queue_level", return_value="shadow"), \
             patch.object(code_queue, "_submit") as submit:
            code_queue.capture_message_signal(
                text="[QA] cora should track RepRally listings",
                entity="FNDR", channel_id="C1", channel_name="c",
                slack_user_id="U1")
        assert not submit.called

    def test_the_same_text_without_the_marker_DOES_capture(self):
        """Non-vacuity guard: proves the quarantine is what suppressed the capture
        above, not an inert phrase."""
        from cora import code_queue
        with patch.object(code_queue, "code_queue_level", return_value="shadow"), \
             patch.object(code_queue, "_submit") as submit:
            code_queue.capture_message_signal(
                text="cora should track RepRally listings",
                entity="FNDR", channel_id="C1", channel_name="c",
                slack_user_id="U1")
        assert submit.called


class TestDecisionInboxQuarantine:
    def test_qa_decision_is_screened_out(self):
        from cora import decision_inbox
        excluded, reason = decision_inbox.screen_decision({
            "description": "Decision: adopt the [QA] smoke workflow",
            "payload": {"entity": "FNDR"},
        })
        assert excluded is True and reason == "qa"

    def test_ordinary_decision_survives(self):
        from cora import decision_inbox
        excluded, reason = decision_inbox.screen_decision({
            "description": "Decision: F3 Pure retail price locked at $36.99",
            "payload": {"entity": "F3E"},
        })
        assert excluded is False and reason == ""


class TestNotePathQuarantine:
    """The @Cora note: path is a fourth intake surface -- and the only one that
    spends a Haiku paraphrase call BEFORE anything is queued."""

    def test_qa_note_never_paraphrased_or_staged(self):
        from unittest.mock import MagicMock
        import cora.app as app_module
        say, client = MagicMock(), MagicMock()
        with patch.object(app_module.team_learning, "is_authorized_contributor") as auth, \
             patch.object(app_module.team_learning, "paraphrase_note") as para, \
             patch.object(app_module.team_learning, "store_pending_confirm") as pend:
            app_module._handle_note(
                client=client, say=say, entity="F3E", channel_id="C1",
                channel_name="f3e-sales", user_id="U1",
                content="[QA] smoke -- the vendor is Apex Appliance",
                original_ts="1.1")
        # Screened before the authorization check, so no model call and no stash.
        assert not auth.called
        assert not para.called   # no Haiku call
        assert not pend.called
        assert "[QA]" in say.call_args.kwargs["text"]
