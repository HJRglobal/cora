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
    def test_qa_message_never_captures_a_signal(self):
        from cora import code_queue
        with patch.object(code_queue, "code_queue_level", return_value="shadow"), \
             patch.object(code_queue, "_submit") as submit:
            code_queue.capture_message_signal(
                text="[QA] can you build a tool that checks RepRally listings?",
                entity="FNDR", channel_id="C1", channel_name="c",
                slack_user_id="U1")
        assert not submit.called


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
