"""Founder-DM queue-verb interceptor (Code #12 S1', cq-554184feb53b).

2026-09-03 06:49 AZ: Harrison DM'd "@Cora Stage cq-621dfad586aa" and got the
morning briefing back -- zero tool_use -- because the typed verb was honored only
inside the cora_queue_code_session backend (queue_explicit), i.e. only when the
MODEL chose to call that tool. Four minutes later the model narrated "Done.
Staging all three" with two invented ids.

Contract under test:
  * an EXACT "stage|approve|dismiss cq-<12hex>" in a DM is applied deterministically
    -- stage_by_id / process_queue_action -- and the reply is the queue's OWN
    outcome string; the model is never consulted (the interceptor returns before
    _handle_dm_qa and before every greedy capture predicate);
  * a sentence that merely CITES an id still goes to the model;
  * a non-founder gets the tool's not_authorized text; an unknown id gets the
    tool's not-found text; a terminal row is refused by the inherited guard;
  * the interceptor sits ABOVE the gap-ask / knowledge-check captures so a live
    pending ask can never swallow the verb;
  * the 06:49 incident message replays through the DM mention strip and is
    intercepted (D-256: a rail must replay its incident).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cora.app as app_module
from cora import code_queue as cq

HARRISON = "U0B2RM2JYJ1"
TOMMY = "U_TOMMY_TEST"
BOT = "U0B44MDGC5R"


@pytest.fixture
def qenv(tmp_path, monkeypatch):
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "code-session-queue.jsonl")
    monkeypatch.setattr(cq, "_FINGERPRINT_LEDGER", tmp_path / "code-queue-fingerprints.jsonl")
    monkeypatch.setattr(cq, "_SIGNALS_LEDGER", tmp_path / "code-queue-signals.jsonl")
    monkeypatch.setattr(cq, "_NOTES_DIR", tmp_path / "_notes")
    monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path / "founder-os"))
    monkeypatch.setenv("CORA_CODE_QUEUE", "log")
    monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
    monkeypatch.setattr(cq, "_SYNC", True)

    def _plain_write(path, text, **kw):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")

    monkeypatch.setattr(cq.drive_io, "write_text_atomic", _plain_write)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(cq, "_default_embed", lambda texts: [])
    cq._STAGING_INFLIGHT.clear()
    yield tmp_path


def _seed(status="APPROVED", severity="P2", title="Founder verb fixture"):
    return cq.seed_item(kind="feature", severity=severity, title=title, summary="s",
                        entity="F3E", signal="explicit", status=status)


# ── match_queue_verb: the exact-match matrix ──────────────────────────────────

class TestMatchQueueVerb:
    @pytest.mark.parametrize("text", [
        "stage cq-621dfad586aa",
        "Stage cq-621dfad586aa",          # the 06:49 incident casing
        "APPROVE cq-621dfad586aa",
        "dismiss cq-621dfad586aa",
        "  stage   cq-621dfad586aa  ",
        "stage cq-621DFAD586AA",           # id case-insensitive, normalized below
    ])
    def test_exact_forms_match(self, text):
        m = cq.match_queue_verb(text)
        assert m is not None
        assert m[0] in ("stage", "approve", "dismiss")
        assert m[1] == "cq-621dfad586aa"

    @pytest.mark.parametrize("text", [
        "please stage cq-621dfad586aa",                       # leading words
        "stage cq-621dfad586aa please",                       # trailing words
        "Cora should retry uploads the way cq-621dfad586aa describes",  # a citation
        "stage all of them",                                  # the 06:53 message
        "stage cq-621dfad586a",                               # 11 hex
        "stage cq-621dfad586aab",                             # 13 hex
        "stage cq-621dfad586aa.",                             # trailing punctuation: NOT tolerated (spec regex verbatim)
        "stage cq-621dfad586aa and cq-9c4e2d8f5f1a",          # two ids
        "restage cq-621dfad586aa",                            # not a listed verb
        "",
        None,
    ])
    def test_non_exact_forms_do_not_match(self, text):
        assert cq.match_queue_verb(text) is None


# ── apply_queue_verb: inherits every guard from the tool it calls ─────────────

class TestApplyQueueVerb:
    def test_stage_known_item_as_founder_stages(self, qenv):
        cid = _seed(status="APPROVED")
        outcome, msg = cq.apply_queue_verb("stage", cid, HARRISON)
        assert outcome == "staged"
        assert cq.get_item(cid)["status"] == "STAGED"
        assert cq.get_item(cid)["prompt_path"] in msg

    def test_approve_proposed_item_as_founder(self, qenv):
        cid = _seed(status="PROPOSED", severity="P3")
        outcome, msg = cq.apply_queue_verb("approve", cid, HARRISON)
        assert outcome == "approved"
        assert cq.get_item(cid)["status"] == "APPROVED"
        assert "Queued" in msg

    def test_dismiss_as_founder(self, qenv):
        cid = _seed(status="PROPOSED", severity="P3")
        outcome, _msg = cq.apply_queue_verb("dismiss", cid, HARRISON)
        assert outcome == "dismissed"
        assert cq.get_item(cid)["status"] == "DISMISSED"

    @pytest.mark.parametrize("verb", ["stage", "approve", "dismiss"])
    def test_non_founder_gets_the_tools_not_authorized_text(self, qenv, verb):
        cid = _seed(status="APPROVED")
        outcome, msg = cq.apply_queue_verb(verb, cid, TOMMY)
        assert outcome == "not_authorized"
        assert "Harrison" in msg
        # and nothing moved
        assert cq.get_item(cid)["status"] == "APPROVED"

    def test_unknown_id_gets_the_tools_not_found_text(self, qenv):
        outcome, msg = cq.apply_queue_verb("stage", "cq-e7f2a4c91b2e", HARRISON)
        assert outcome == "not_found"
        assert "cq-e7f2a4c91b2e" in msg and "can't find" in msg
        outcome2, msg2 = cq.apply_queue_verb("approve", "cq-e7f2a4c91b2e", HARRISON)
        assert outcome2 == "error" and "no longer exists" in msg2

    def test_terminal_row_refused_by_the_inherited_guard(self, qenv):
        cid = _seed(status="APPROVED")
        cq._append_event({"event": "shipped", "ts": cq._now_iso(), "id": cid,
                          "bundle_id": "test-bundle"})
        outcome, msg = cq.apply_queue_verb("stage", cid, HARRISON)
        assert outcome == "noop" and "SHIPPED" in msg
        assert cq.get_item(cid)["status"] == "SHIPPED"

    def test_stage_is_idempotent_on_prompt_path(self, qenv):
        cid = _seed(status="APPROVED")
        o1, p1 = cq.apply_queue_verb("stage", cid, HARRISON)
        o2, p2 = cq.apply_queue_verb("stage", cid, HARRISON)
        assert o1 == "staged" and o2 == "noop" and p1 == p2

    def test_unknown_verb_is_an_error(self, qenv):
        outcome, _ = cq.apply_queue_verb("restage", "cq-621dfad586aa", HARRISON)
        assert outcome == "error"


# ── handle_message_event DM branch: the interceptor sits first ────────────────

def _event(user=HARRISON, text="stage cq-621dfad586aa", channel="D0B4CTD3B09", ts="100.1",
           thread_ts=None):
    ev = {"user": user, "text": text, "channel": channel, "ts": ts, "channel_type": "im"}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    return ev


@pytest.fixture
def branch_mocks():
    with patch.object(app_module.gap_autofill, "match_pending_ask", return_value=None) as gap, \
         patch.object(app_module.gap_autofill, "has_live_ask", return_value=False), \
         patch.object(app_module.knowledge_check, "enabled", return_value=False), \
         patch.object(app_module.historical_access, "detect_retrieval_intent",
                      return_value=False), \
         patch.object(app_module.decision_alerts, "match_alert_reply", return_value=None), \
         patch.object(app_module.osn_shift_handler, "handle_dm") as shift, \
         patch.object(app_module, "_dm_is_shift_message", return_value=False), \
         patch.object(app_module, "_handle_dm_qa") as qa, \
         patch.object(app_module, "_resolve_bot_user_id", return_value=BOT):
        yield SimpleNamespace(gap=gap, shift=shift, qa=qa)


class TestDmInterceptor:
    def test_exact_verb_is_applied_and_never_reaches_the_model(self, branch_mocks, qenv):
        client = MagicMock()
        with patch.object(app_module.code_queue, "apply_queue_verb",
                          return_value=("staged", "📝 Prompt staged: `G:/x.md`")) as apply:
            app_module.handle_message_event(_event(text="stage cq-621dfad586aa"), client)
        apply.assert_called_once_with("stage", "cq-621dfad586aa", HARRISON)
        branch_mocks.qa.assert_not_called()
        branch_mocks.shift.assert_not_called()
        # the reply is the tool's own outcome string, posted in the DM
        kw = client.chat_postMessage.call_args.kwargs
        assert kw["channel"] == "D0B4CTD3B09" and kw["text"] == "📝 Prompt staged: `G:/x.md`"

    def test_incident_message_replays_through_the_mention_strip(self, branch_mocks, qenv):
        """D-256: the exact 06:49 message, mention token and casing included."""
        client = MagicMock()
        with patch.object(app_module.code_queue, "apply_queue_verb",
                          return_value=("not_found", "I can't find `cq-621dfad586aa`")) as apply:
            app_module.handle_message_event(
                _event(text=f"<@{BOT}> Stage cq-621dfad586aa"), client)
        apply.assert_called_once_with("stage", "cq-621dfad586aa", HARRISON)
        branch_mocks.qa.assert_not_called()

    def test_citation_sentence_still_routes_to_qa(self, branch_mocks, qenv):
        client = MagicMock()
        with patch.object(app_module.code_queue, "apply_queue_verb") as apply:
            app_module.handle_message_event(
                _event(text="Cora should retry uploads the way cq-621dfad586aa describes"),
                client)
        apply.assert_not_called()
        branch_mocks.qa.assert_called_once()

    def test_the_0653_message_stage_all_of_them_routes_to_qa(self, branch_mocks, qenv):
        """'Stage all of them' names no id -- it is NOT a verb; the model owns it
        (and S2' owns the phantom claim that followed)."""
        client = MagicMock()
        with patch.object(app_module.code_queue, "apply_queue_verb") as apply:
            app_module.handle_message_event(_event(text="Stage all of them"), client)
        apply.assert_not_called()
        branch_mocks.qa.assert_called_once()

    def test_non_founder_gets_not_authorized_never_the_model(self, branch_mocks, qenv):
        cid = _seed(status="APPROVED")
        client = MagicMock()
        app_module.handle_message_event(_event(user=TOMMY, text=f"stage {cid}"), client)
        branch_mocks.qa.assert_not_called()
        assert "Harrison" in client.chat_postMessage.call_args.kwargs["text"]
        assert cq.get_item(cid)["status"] == "APPROVED"

    def test_verb_outranks_a_live_gap_ask(self, branch_mocks, qenv):
        """A pending gap ask must never swallow the verb (the cq-236fd0310eb8 class)."""
        client = MagicMock()
        branch_mocks.gap.return_value = {"ask_id": "ask-1"}
        with patch.object(app_module.gap_autofill, "record_ask_answer") as record, \
             patch.object(app_module.code_queue, "apply_queue_verb",
                          return_value=("staged", "ok")) as apply:
            app_module.handle_message_event(_event(text="stage cq-621dfad586aa"), client)
        apply.assert_called_once()
        record.assert_not_called()

    def test_threaded_verb_replies_in_thread(self, branch_mocks, qenv):
        client = MagicMock()
        with patch.object(app_module.code_queue, "apply_queue_verb",
                          return_value=("staged", "ok")):
            app_module.handle_message_event(
                _event(text="approve cq-621dfad586aa", thread_ts="99.5"), client)
        assert client.chat_postMessage.call_args.kwargs["thread_ts"] == "99.5"

    def test_verb_crash_fails_honestly_never_falls_to_the_model(self, branch_mocks, qenv):
        client = MagicMock()
        with patch.object(app_module.code_queue, "apply_queue_verb",
                          side_effect=RuntimeError("ledger gone")):
            app_module.handle_message_event(_event(text="stage cq-621dfad586aa"), client)
        branch_mocks.qa.assert_not_called()
        text = client.chat_postMessage.call_args.kwargs["text"]
        assert "went wrong" in text and "check the backlog" in text

    def test_real_ledger_path_end_to_end(self, branch_mocks, qenv):
        """No mocks on the queue: seed -> typed stage -> STAGED, reply carries the path."""
        cid = _seed(status="APPROVED")
        client = MagicMock()
        app_module.handle_message_event(_event(text=f"Stage {cid}"), client)
        assert cq.get_item(cid)["status"] == "STAGED"
        assert cq.get_item(cid)["prompt_path"] in client.chat_postMessage.call_args.kwargs["text"]
        branch_mocks.qa.assert_not_called()


class TestSourcePins:
    def test_interceptor_sits_above_every_dm_capture(self):
        """The verb check must precede the gap-ask / knowledge-check / decision-alert
        / retrieval / shift / Q&A routing in the DM branch."""
        import inspect
        src = inspect.getsource(app_module.handle_message_event)
        i_verb = src.index("code_queue.match_queue_verb(text)")
        for later in ("knowledge_check.match_live_cycle", "gap_autofill.match_pending_ask",
                      "decision_alerts.match_alert_reply",
                      "historical_access.detect_retrieval_intent",
                      "_dm_is_shift_message(user_id, text):", "_handle_dm_qa(event, client"):
            assert src.index(later) > i_verb, later
        # and it runs AFTER the mention strip (the 06:49 message carried the token)
        assert src.index("_strip_dm_bot_mention(text, client)") < i_verb

    def test_spec_regex_is_verbatim(self):
        assert cq._QUEUE_VERB_RE.pattern == r"^\s*(stage|approve|dismiss)\s+(cq-[0-9a-f]{12})\s*$"
