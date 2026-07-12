"""Unit tests for claude_client.generate_response()."""

from unittest.mock import MagicMock, patch

import anthropic
import pytest

import cora.claude_client as cl


def _mock_success(text="Hello from Claude"):
    response = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    response.content = [block]
    response.stop_reason = "end_turn"  # not "tool_use" — signals model is done
    return response


def _conn_error():
    return anthropic.APIConnectionError(request=MagicMock())


def _auth_error():
    resp = MagicMock()
    resp.status_code = 401
    resp.request = MagicMock()
    return anthropic.AuthenticationError("bad key", response=resp, body={})


def _mock_client(create_return=None, create_side_effect=None):
    """Build a mock Anthropic client whose messages.create is pre-wired."""
    client = MagicMock()
    if create_side_effect is not None:
        client.messages.create.side_effect = create_side_effect
    elif create_return is not None:
        client.messages.create.return_value = create_return
    return client


def test_successful_response_returns_text():
    mock = _mock_client(create_return=_mock_success("test reply"))
    with patch("cora.claude_client._get_client", return_value=mock):
        result = cl.generate_response("sys", "ctx", "hello")
    assert result == "test reply"


def test_persistent_failure_raises_ClaudeClientError():
    mock = _mock_client(create_side_effect=_conn_error())
    with patch("cora.claude_client._get_client", return_value=mock), \
         patch("cora.claude_client.time.sleep"):
        with pytest.raises(cl.ClaudeClientError):
            cl.generate_response("sys", "ctx", "hello")


def test_retries_on_transient_error():
    mock = _mock_client(create_side_effect=_conn_error())
    with patch("cora.claude_client._get_client", return_value=mock), \
         patch("cora.claude_client.time.sleep"):
        with pytest.raises(cl.ClaudeClientError):
            cl.generate_response("sys", "ctx", "hello")

    assert mock.messages.create.call_count == 3


def test_no_retry_on_auth_error():
    mock = _mock_client(create_side_effect=_auth_error())
    with patch("cora.claude_client._get_client", return_value=mock), \
         patch("cora.claude_client.time.sleep") as mock_sleep:
        with pytest.raises(cl.ClaudeClientError):
            cl.generate_response("sys", "ctx", "hello")

    assert mock.messages.create.call_count == 1
    assert mock_sleep.call_count == 0


# ── Shopify write-tool narration net (2026-07-10 HIGH-2) ─────────────────────

def _tool_use_response(tool_name, tool_id="tid1", tool_input=None):
    resp = MagicMock()
    resp.stop_reason = "tool_use"
    block = MagicMock()
    block.type = "tool_use"
    block.name = tool_name
    block.id = tool_id
    block.input = tool_input or {}
    resp.content = [block]
    return resp


class TestShopifyNarrationNet:
    def test_directed_text_extracts_confirmed_payload(self):
        raw = "WRITE_CONFIRMED -- post this:\n\nDTC inventory updated -- Pure: 202 -> 203 units."
        assert cl._shopify_directed_text(raw) == "DTC inventory updated -- Pure: 202 -> 203 units."

    def test_directed_text_extracts_blocked_payload(self):
        raw = "WRITE_BLOCKED -- show verbatim:\n\n⚠️ NOT WRITTEN -- no change.\nPure: 202 -> 203. Reply confirm."
        out = cl._shopify_directed_text(raw)
        assert out.startswith("⚠️ NOT WRITTEN")
        assert "WRITE_BLOCKED" not in out

    def test_directed_text_passthrough_without_sentinel(self):
        assert cl._shopify_directed_text("just text") == "just text"
        assert cl._shopify_directed_text("") == ""

    def test_last_shopify_result_matched_by_name(self):
        b1 = MagicMock(); b1.name = "asana_get_my_tasks"
        b2 = MagicMock(); b2.name = "f3e_shopify_set_inventory"
        results = [
            {"tool_use_id": "1", "content": "tasks..."},
            {"tool_use_id": "2", "content": "WRITE_BLOCKED -- s\n\nNOT WRITTEN"},
        ]
        assert cl._last_shopify_write_result([b1, b2], results) == "WRITE_BLOCKED -- s\n\nNOT WRITTEN"

    def test_last_shopify_result_empty_when_tool_absent(self):
        b1 = MagicMock(); b1.name = "asana_get_my_tasks"
        assert cl._last_shopify_write_result([b1], [{"tool_use_id": "1", "content": "x"}]) == ""

    def test_generate_response_overrides_false_success_narration(self):
        """The core HIGH-2 guarantee: if the write tool's last result is a non-write
        (WRITE_BLOCKED), the model's success-claim narration is REPLACED by the
        tool's NOT-WRITTEN text."""
        blocked = ("WRITE_BLOCKED -- show the user verbatim:\n\n"
                   "⚠️ NOT WRITTEN -- no inventory change was made.\n"
                   "Pure at the office: 202 -> 203 units. Reply \"confirm\" and I'll set it.")
        tu = _tool_use_response("f3e_shopify_set_inventory", tool_input={"confirmed": True})
        done = _mock_success("Done — 203 units set at the office.")  # FALSE narration
        with patch.object(cl, "_log_usage"), \
             patch.object(cl, "_create_with_retry", side_effect=[tu, done]), \
             patch.object(cl, "_dispatch_tools_parallel",
                          return_value=[{"type": "tool_result", "tool_use_id": "tid1", "content": blocked}]):
            out = cl.generate_response("sys", "ctx", "set pure to 203")
        assert "203 units set" not in out
        assert out.startswith("⚠️ NOT WRITTEN")

    def test_generate_response_posts_confirmed_line(self):
        confirmed = ("WRITE_CONFIRMED -- post the line after the blank:\n\n"
                     "DTC inventory updated -- Pure at the office: 202 -> 203 units.")
        tu = _tool_use_response("f3e_shopify_set_inventory", tool_input={"confirmed": True})
        done = _mock_success("ok")
        with patch.object(cl, "_log_usage"), \
             patch.object(cl, "_create_with_retry", side_effect=[tu, done]), \
             patch.object(cl, "_dispatch_tools_parallel",
                          return_value=[{"type": "tool_result", "tool_use_id": "tid1", "content": confirmed}]):
            out = cl.generate_response("sys", "ctx", "confirm")
        assert out == "DTC inventory updated -- Pure at the office: 202 -> 203 units."

    def test_directed_text_not_triggered_for_crash_string(self):
        crash = "Tool f3e_shopify_set_inventory crashed: KeyError. Apologize to the user and continue."
        assert not cl._is_shopify_directive(crash)
        assert cl._is_shopify_directive("WRITE_BLOCKED -- x\n\nNOT WRITTEN")
        assert cl._is_shopify_directive("WRITE_CONFIRMED -- x\n\ndone")

    def test_generate_response_does_not_override_crash_string(self):
        """Review #1: a raw crash string (no WRITE_ sentinel) must NOT be posted
        verbatim -- fall through to the model's source-opaque apology."""
        crash = "Tool f3e_shopify_set_inventory crashed: KeyError('x'). Apologize to the user and continue."
        tu = _tool_use_response("f3e_shopify_set_inventory", tool_input={"confirmed": True})
        done = _mock_success("Sorry, I hit a snag and couldn't update that. Try again shortly.")
        with patch.object(cl, "_log_usage"), \
             patch.object(cl, "_create_with_retry", side_effect=[tu, done]), \
             patch.object(cl, "_dispatch_tools_parallel",
                          return_value=[{"type": "tool_result", "tool_use_id": "tid1", "content": crash}]):
            out = cl.generate_response("sys", "ctx", "set pure to 203")
        assert out == "Sorry, I hit a snag and couldn't update that. Try again shortly."
        assert "shopify" not in out.lower() and "crashed" not in out.lower()

    def test_last_result_prefers_confirmed_in_batch(self):
        b1 = MagicMock(); b1.name = "f3e_shopify_set_inventory"
        b2 = MagicMock(); b2.name = "f3e_shopify_set_inventory"
        results = [
            {"tool_use_id": "1", "content": "WRITE_CONFIRMED -- p\n\nDTC inventory updated -- x: 202 -> 203 units."},
            {"tool_use_id": "2", "content": "WRITE_BLOCKED -- p\n\nNOT WRITTEN"},
        ]
        assert cl._last_shopify_write_result([b1, b2], results).startswith("WRITE_CONFIRMED")

    def test_merge_confirmed_is_sticky(self):
        conf = "WRITE_CONFIRMED -- p\n\ndone"
        blk = "WRITE_BLOCKED -- p\n\nNOT WRITTEN"
        assert cl._merge_shopify_result(blk, conf) == conf   # a write in this turn wins
        assert cl._merge_shopify_result(conf, blk) == conf   # ...and sticks over a later re-preview
        assert cl._merge_shopify_result(blk, "WRITE_BLOCKED -- q\n\nb").endswith("b")  # else last wins
        assert cl._merge_shopify_result(conf, "") == conf    # empty batch keeps prev


class TestContractWriteNetGeneralized:
    """F-23: the narration net covers the destructive Asana tools too."""

    def test_net_matches_asana_delete_tool(self):
        b1 = MagicMock(); b1.name = "asana_delete_task"
        results = [{"tool_use_id": "1", "content": 'WRITE_CONFIRMED\n\nDeleted "Jerry task" from Asana.'}]
        assert cl._last_shopify_write_result([b1], results).startswith("WRITE_CONFIRMED")

    def test_net_matches_asana_complete_tool(self):
        b1 = MagicMock(); b1.name = "asana_complete_task"
        results = [{"tool_use_id": "1", "content": "WRITE_BLOCKED -- p\n\nNot done yet -- reply to confirm."}]
        assert cl._last_shopify_write_result([b1], results).startswith("WRITE_BLOCKED")

    def test_asana_delete_confirmed_posts_verbatim(self):
        confirmed = 'WRITE_CONFIRMED\n\nDeleted "Jerry task" from Asana.'
        tu = _tool_use_response("asana_delete_task", tool_input={"confirmed": True})
        done = _mock_success("some model narration")
        with patch.object(cl, "_log_usage"), \
             patch.object(cl, "_create_with_retry", side_effect=[tu, done]), \
             patch.object(cl, "_dispatch_tools_parallel",
                          return_value=[{"type": "tool_result", "tool_use_id": "tid1", "content": confirmed}]):
            out = cl.generate_response("sys", "ctx", "yes")
        assert out == 'Deleted "Jerry task" from Asana.'


class TestPhantomDestructiveGuard:
    """F-23: a fabricated destructive-Asana success with NO tool call is overridden."""

    def test_guard_fires_on_fabricated_delete_claim(self):
        for claim in [
            "Task deleted permanently.",
            'Done -- I deleted the "Jerry" task from Asana.',
            "I permanently deleted that task for you.",
            'Done -- I marked "Send the deck" complete in Asana.',
        ]:
            assert cl._guard_phantom_destructive(claim) == cl._PHANTOM_DESTRUCTIVE_CORRECTION, claim

    def test_guard_ignores_factual_status_answer(self):
        # A factual THIRD-PERSON status answer (not Cora claiming she just acted) must
        # pass -- the correction would be an affirmatively FALSE statement (D-051 #4).
        for ok in [
            "That task was completed on 6/3.",
            "That onboarding task was deleted last week by Hannah, so you're all set.",
            "The Jimmy Bar task was deleted a year ago when it closed.",
            "Yes, it's marked done in the tracker.",
            "I've completed the analysis of your pipeline.",   # first-person, not an Asana action
            "I marked the onboarding notes as reviewed.",       # 'marked' but not 'complete'
            "I deleted the extra whitespace from the draft.",   # not task/from-asana
            "Here are your 5 open tasks.",
        ]:
            assert cl._guard_phantom_destructive(ok) == ok, ok

    def test_generate_response_overrides_phantom_delete_no_tool_call(self):
        # THE F-23 repro: the model narrates a delete success with ZERO tool_use.
        done = _mock_success("Task deleted permanently. It's gone from Asana.")
        with patch.object(cl, "_log_usage"), \
             patch.object(cl, "_create_with_retry", side_effect=[done]):
            out = cl.generate_response("sys", "ctx", "yes, delete the Jerry task")
        assert out == cl._PHANTOM_DESTRUCTIVE_CORRECTION

    def test_generate_response_keeps_real_delete_confirmed(self):
        # When the tool actually fired + CONFIRMED, the net posts its text (no phantom).
        confirmed = 'WRITE_CONFIRMED\n\nDeleted "Jerry task" from Asana.'
        tu = _tool_use_response("asana_delete_task", tool_input={"confirmed": True})
        done = _mock_success("Task deleted permanently.")  # model also claims it
        with patch.object(cl, "_log_usage"), \
             patch.object(cl, "_create_with_retry", side_effect=[tu, done]), \
             patch.object(cl, "_dispatch_tools_parallel",
                          return_value=[{"type": "tool_result", "tool_use_id": "tid1", "content": confirmed}]):
            out = cl.generate_response("sys", "ctx", "yes")
        assert out == 'Deleted "Jerry task" from Asana.'


class TestForcedTool:
    """F-23 Slice 2: a destructive/create intent forces the tool via tool_choice."""

    def test_apply_sets_tool_choice_iter0(self):
        kw = {}
        cl._apply_forced_tool(kw, "asana_delete_task", 0,
                              [{"name": "asana_delete_task"}, {"name": "x"}])
        assert kw["tool_choice"] == {"type": "tool", "name": "asana_delete_task"}

    def test_apply_noop_when_tool_not_offered(self):
        kw = {}
        cl._apply_forced_tool(kw, "asana_delete_task", 0, [{"name": "other"}])
        assert "tool_choice" not in kw

    def test_apply_noop_after_first_iteration(self):
        kw = {}
        cl._apply_forced_tool(kw, "asana_delete_task", 1, [{"name": "asana_delete_task"}])
        assert "tool_choice" not in kw

    def test_apply_noop_no_tools(self):
        kw = {}
        cl._apply_forced_tool(kw, "asana_delete_task", 0, [])
        assert "tool_choice" not in kw

    def test_apply_noop_no_force(self):
        kw = {}
        cl._apply_forced_tool(kw, None, 0, [{"name": "asana_delete_task"}])
        assert "tool_choice" not in kw

    def test_generate_response_forces_only_first_call(self):
        captured = []
        tu = _tool_use_response("asana_delete_task", tool_input={})
        done = _mock_success("model narration")

        def _rec(**kwargs):
            captured.append(kwargs.get("tool_choice"))
            return tu if len(captured) == 1 else done

        with patch.object(cl, "_log_usage"), \
             patch.object(cl, "_create_with_retry", side_effect=_rec), \
             patch.object(cl, "_build_cached_tools", return_value=[{"name": "asana_delete_task"}]), \
             patch.object(cl, "_dispatch_tools_parallel",
                          return_value=[{"type": "tool_result", "tool_use_id": "tid1",
                                         "content": "WRITE_BLOCKED -- x\n\nNot deleted yet -- confirm."}]):
            out = cl.generate_response("sys", "ctx", "delete the Jerry task",
                                       force_tool="asana_delete_task")
        assert captured[0] == {"type": "tool", "name": "asana_delete_task"}  # forced turn 1
        assert captured[1] is None                                           # not forced after
        assert out == "Not deleted yet -- confirm."   # net posts the tool's WRITE_BLOCKED text


class TestPhantomBroaden:
    """F-23 Slice 3: on a bare-affirmative user turn, terse fabricated completion
    claims (the live 'Confirmed -- task deleted' residual) are corrected."""

    @pytest.mark.parametrize("claim", [
        "Confirmed -- task deleted",
        "Task deleted.",
        "Done, deleted the task.",
        "All set, it's deleted.",
        "Created it in Asana.",
        "Done -- completed.",
    ])
    def test_broaden_catches_terse_claims(self, claim):
        assert cl._guard_phantom_destructive(claim, broaden=True) == cl._PHANTOM_DESTRUCTIVE_CORRECTION

    def test_broaden_off_leaves_terse_residual(self):
        # Without confirm context the terse third-person residual passes (accepted) --
        # the tool-sentinel + interceptor paths are the primary controls.
        assert cl._guard_phantom_destructive("Confirmed -- task deleted") == "Confirmed -- task deleted"

    def test_broaden_ignores_long_read_result(self):
        long_list = "Here are your open tasks: " + "; ".join(
            f"Task {i} (completed)" for i in range(30))
        assert len(long_list) > cl._PHANTOM_CONFIRM_MAX_LEN
        assert cl._guard_phantom_destructive(long_list, broaden=True) == long_list

    def test_generate_response_broaden_overrides_phantom_confirm(self):
        # THE residual repro: bare "yes, delete it permanently", model fabricates a
        # terse success with ZERO tool_use.
        done = _mock_success("Confirmed -- task deleted.")
        with patch.object(cl, "_log_usage"), \
             patch.object(cl, "_create_with_retry", side_effect=[done]):
            out = cl.generate_response("sys", "ctx", "yes, delete it permanently",
                                       assume_confirm=True)
        assert out == cl._PHANTOM_DESTRUCTIVE_CORRECTION

    def test_generate_response_no_broaden_lets_terse_residual_through(self):
        done = _mock_success("Confirmed -- task deleted.")
        with patch.object(cl, "_log_usage"), \
             patch.object(cl, "_create_with_retry", side_effect=[done]):
            out = cl.generate_response("sys", "ctx", "yes, delete it permanently",
                                       assume_confirm=False)
        assert out == "Confirmed -- task deleted."
