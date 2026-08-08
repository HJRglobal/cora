"""Unit tests for _dispatch_tools_parallel in claude_client — parallel tool
execution preserves result order and handles per-tool failures cleanly."""

import contextvars
import time
from unittest.mock import MagicMock, patch

import cora.claude_client as cc
from cora import confirm_cards


def _make_block(tool_id: str, name: str, input_dict: dict | None = None):
    """Build a mock tool_use block matching Anthropic's content-block shape."""
    block = MagicMock()
    block.id = tool_id
    block.name = name
    block.input = input_dict or {}
    return block


# ---- Single-tool path: no executor, straight dispatch ----


def test_single_tool_block_dispatches_sequentially():
    block = _make_block("toolu_1", "asana_get_my_tasks")
    with patch.object(cc, "dispatch", return_value="task list here") as mock_dispatch:
        results = cc._dispatch_tools_parallel(
            [block], slack_user_id="U123", entity="FNDR", iteration=0,
        )

    mock_dispatch.assert_called_once_with(
        "asana_get_my_tasks", {}, "U123", "FNDR", "", "", None,
    )
    assert results == [{
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "task list here",
    }]


# ---- Multi-tool path: parallel + order preserved ----


def test_multi_tool_blocks_dispatched_in_parallel():
    blocks = [
        _make_block("toolu_a", "asana_get_my_tasks"),
        _make_block("toolu_b", "calendar_get_my_events"),
        _make_block("toolu_c", "hubspot_get_my_deals"),
    ]

    def fake_dispatch(name, _input, _user, _entity, _channel="", _channel_id="",
                      _thread_ts=None, _user_message=""):
        # Each tool "takes" some work — verify they actually run concurrently
        return f"result for {name}"

    with patch.object(cc, "dispatch", side_effect=fake_dispatch):
        results = cc._dispatch_tools_parallel(
            blocks, slack_user_id="U123", entity="FNDR", iteration=0,
        )

    # Order must match input
    assert [r["tool_use_id"] for r in results] == ["toolu_a", "toolu_b", "toolu_c"]
    assert results[0]["content"] == "result for asana_get_my_tasks"
    assert results[1]["content"] == "result for calendar_get_my_events"
    assert results[2]["content"] == "result for hubspot_get_my_deals"


def test_parallel_dispatch_actually_runs_concurrently():
    """Two slow tools should complete in ~one tool's worth of wall-time, not two."""
    blocks = [
        _make_block("toolu_a", "slow_tool_1"),
        _make_block("toolu_b", "slow_tool_2"),
    ]

    def slow_dispatch(name, _input, _user, _entity, _channel="", _channel_id="",
                      _thread_ts=None, _user_message=""):
        time.sleep(0.3)
        return f"result for {name}"

    t0 = time.monotonic()
    with patch.object(cc, "dispatch", side_effect=slow_dispatch):
        results = cc._dispatch_tools_parallel(
            blocks, slack_user_id="U", entity="FNDR", iteration=0,
        )
    elapsed = time.monotonic() - t0

    # Sequential would be ~0.6s. Parallel should be ~0.3s + small overhead.
    assert elapsed < 0.5, f"Parallel dispatch took {elapsed:.2f}s — expected <0.5s"
    assert len(results) == 2


def test_results_preserve_order_when_tools_finish_out_of_order():
    """First tool sleeps longer, second returns instantly — result order still matches input."""
    blocks = [
        _make_block("first_slow", "slow"),
        _make_block("second_fast", "fast"),
    ]

    def staggered(name, _input, _user, _entity, _channel="", _channel_id="",
                  _thread_ts=None, _user_message=""):
        if name == "slow":
            time.sleep(0.2)
            return "slow result"
        return "fast result"

    with patch.object(cc, "dispatch", side_effect=staggered):
        results = cc._dispatch_tools_parallel(
            blocks, slack_user_id="U", entity="FNDR", iteration=0,
        )

    assert results[0]["tool_use_id"] == "first_slow"
    assert results[0]["content"] == "slow result"
    assert results[1]["tool_use_id"] == "second_fast"
    assert results[1]["content"] == "fast result"


# ---- Empty list edge case ----


def test_empty_tool_list_returns_empty_results():
    results = cc._dispatch_tools_parallel(
        [], slack_user_id="U", entity="FNDR", iteration=0,
    )
    assert results == []


# ---- Worker cap ----


def test_worker_count_capped_at_max():
    """Even with 10 tool blocks, executor uses at most _TOOL_DISPATCH_MAX_WORKERS."""
    blocks = [_make_block(f"toolu_{i}", "noop") for i in range(10)]

    with patch.object(cc, "dispatch", return_value="ok"), \
         patch("cora.claude_client.ThreadPoolExecutor") as mock_executor:
        # Make ThreadPoolExecutor returnable as a context manager
        mock_executor.return_value.__enter__.return_value.submit.return_value.result.return_value = "ok"

        cc._dispatch_tools_parallel(
            blocks, slack_user_id="U", entity="FNDR", iteration=0,
        )

    # ThreadPoolExecutor should be called with max_workers <= _TOOL_DISPATCH_MAX_WORKERS
    call_kwargs = mock_executor.call_args.kwargs
    assert call_kwargs["max_workers"] == cc._TOOL_DISPATCH_MAX_WORKERS


# ---- Contextvar propagation (S1 fix, live-smoke 2026-08-02) ----
# confirm_cards.mint_stash_id reads confirm_cards._TURN_ID (a contextvars.
# ContextVar) to tag a fresh stash with the calling turn's id. A bare
# executor.submit(dispatch, ...) does NOT propagate the submitting thread's
# contextvars into the worker thread (verified empirically: a plain
# ThreadPoolExecutor worker sees the ContextVar's default, not the caller's
# value) -- these tests pin that _dispatch_tools_parallel's per-call
# contextvars.copy_context().run(...) wrapper fixes this for BOTH the single-
# tool direct-dispatch path and the 2+-tool parallel path, and that reusing
# ONE copied context across concurrent submissions does not crash.


def test_single_tool_dispatch_sees_the_calling_threads_turn_id():
    confirm_cards.begin_turn()
    tid = confirm_cards.current_turn_id()
    seen = {}

    def fake_dispatch(name, _input, _user, _entity, _channel="", _channel_id="", _thread_ts=None, _user_message=""):
        seen["turn_id"] = confirm_cards.current_turn_id()
        return "ok"

    with patch.object(cc, "dispatch", side_effect=fake_dispatch):
        cc._dispatch_tools_parallel(
            [_make_block("toolu_1", "cora_remember")], slack_user_id="U", entity="FNDR", iteration=0,
        )
    assert seen["turn_id"] == tid


def test_parallel_tool_dispatch_each_worker_sees_the_calling_threads_turn_id():
    confirm_cards.begin_turn()
    tid = confirm_cards.current_turn_id()
    seen = {}

    def fake_dispatch(name, _input, _user, _entity, _channel="", _channel_id="", _thread_ts=None, _user_message=""):
        seen[name] = confirm_cards.current_turn_id()
        return f"result for {name}"

    blocks = [
        _make_block("toolu_a", "asana_create_task"),
        _make_block("toolu_b", "calendar_schedule_meeting"),
        _make_block("toolu_c", "cora_remember"),
    ]
    with patch.object(cc, "dispatch", side_effect=fake_dispatch):
        cc._dispatch_tools_parallel(blocks, slack_user_id="U", entity="FNDR", iteration=0)

    assert seen == {"asana_create_task": tid, "calendar_schedule_meeting": tid, "cora_remember": tid}


def test_a_workers_own_turn_id_mutation_never_leaks_to_siblings_or_caller():
    confirm_cards.begin_turn()
    caller_tid = confirm_cards.current_turn_id()
    seen = {}

    def fake_dispatch(name, _input, _user, _entity, _channel="", _channel_id="", _thread_ts=None, _user_message=""):
        # Simulate a worker minting its OWN fresh turn (should never happen in
        # production -- begin_turn only runs once per _dispatch_qa turn -- but
        # proves copy_context() isolation holds even if it did).
        confirm_cards.begin_turn()
        seen[name] = confirm_cards.current_turn_id()
        return "ok"

    blocks = [_make_block("toolu_a", "tool_a"), _make_block("toolu_b", "tool_b")]
    with patch.object(cc, "dispatch", side_effect=fake_dispatch):
        cc._dispatch_tools_parallel(blocks, slack_user_id="U", entity="FNDR", iteration=0)

    assert seen["tool_a"] != caller_tid
    assert seen["tool_b"] != caller_tid
    assert seen["tool_a"] != seen["tool_b"]
    assert confirm_cards.current_turn_id() == caller_tid  # the calling thread is untouched


def test_reusing_one_copied_context_across_concurrent_submissions_would_crash():
    # Documents WHY the fix copies context per-submission rather than once for
    # the whole batch: contextvars.Context.run() raises if the SAME Context
    # object is entered from more than one OS thread concurrently.
    var = contextvars.ContextVar("probe", default=None)
    var.set("SET")
    ctx = contextvars.copy_context()

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(ctx.run, time.sleep, 0.05) for _ in range(4)]
        results = [
            "ok" if _safe_result(f) else "error"
            for f in futures
        ]
    assert results.count("error") >= 1


def _safe_result(future):
    try:
        future.result()
        return True
    except RuntimeError:
        return False
