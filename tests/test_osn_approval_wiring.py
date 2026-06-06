"""Regression tests for the OSN schedule approval (white_check_mark) wiring in app.py.

Guards against the bug introduced in commit ecdf2b2 where:

  1. app.py called ``handle_schedule_approval_reaction(message_ts=, channel_id=, client=)``
     but the handler signature is ``(reaction, message_ts, reactor_user_id, client)``.
     That raised a TypeError on every white_check_mark reaction placed on a
     Cora-posted message, which ALSO blocked the downstream team-learning and
     knowledge-review approval handlers that run later in ``_handle_reaction``.

  2. The approval block was duplicated -- one copy sat before the bot-owner guard
     (firing twice / on unrelated messages).

These tests are signature/wiring contracts only: fast, no Slack mocking. They fail
loudly if either defect is reintroduced by a future edit to app.py or the handler.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from cora.tools import osn_shift_handler

_APP_PY = Path(__file__).resolve().parents[1] / "src" / "cora" / "app.py"


def _approval_call_nodes() -> list[ast.Call]:
    """Return every ast.Call to ``*.handle_schedule_approval_reaction`` in app.py."""
    tree = ast.parse(_APP_PY.read_text(encoding="utf-8"))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "handle_schedule_approval_reaction":
                calls.append(node)
    return calls


def test_single_approval_call_site() -> None:
    """The approval handler must be wired exactly once (no duplicate block)."""
    calls = _approval_call_nodes()
    assert len(calls) == 1, f"expected exactly 1 call site in app.py, found {len(calls)}"


def test_call_kwargs_match_handler_signature() -> None:
    """app.py's call kwargs must exactly match the handler's parameters."""
    calls = _approval_call_nodes()
    assert calls, "no call to handle_schedule_approval_reaction found in app.py"
    callsite_kwargs = {kw.arg for kw in calls[0].keywords if kw.arg is not None}
    handler_params = set(
        inspect.signature(osn_shift_handler.handle_schedule_approval_reaction).parameters
    )
    assert callsite_kwargs == handler_params, (
        f"app.py calls handle_schedule_approval_reaction with {sorted(callsite_kwargs)} "
        f"but the handler accepts {sorted(handler_params)}"
    )


def test_handler_contract_is_stable() -> None:
    """Lock the handler contract so neither side can silently drift back to the bug."""
    params = set(
        inspect.signature(osn_shift_handler.handle_schedule_approval_reaction).parameters
    )
    assert {"reaction", "message_ts", "reactor_user_id", "client"} <= params
    # ``channel_id`` was the bogus kwarg the buggy call passed -- it must not be a param.
    assert "channel_id" not in params
