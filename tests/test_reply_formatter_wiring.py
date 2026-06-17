"""Wiring tests for the reply-formatter integration (D-032 + Phase 2.1 opt-out).

Proves the two halves of the doctrine end-to-end:
  1. claude_client.generate_response / generate_response_streaming report tool
     usage via the caller-owned `meta` dict: meta["used_tools"] (any tool),
     meta["tool_names"] (cumulative), and meta["used_verbatim_tool"] (True only
     when a VERBATIM_TABLE_TOOLS member fires).
  2. app._dispatch_qa formats CONVERSATIONAL replies through
     reply_formatter.format_reply and BYPASSES formatting ONLY when the reply
     incorporated a genuine verbatim-table tool (financial/data dashboards are
     presented as-is). A reply that merely used a LOOKUP tool is still formatted
     -- this is the Phase 2.1 fix to the old too-broad bool(used_tools) bypass.

Pure-formatting behavior is covered separately in test_reply_formatter.py;
the egress boundary is covered in test_slack_egress.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import cora.claude_client as cl  # noqa: E402
import cora.app as app_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — mock Anthropic responses
# ---------------------------------------------------------------------------

def _text_response(text="plain answer"):
    response = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    response.content = [block]
    response.stop_reason = "end_turn"
    return response


def _tool_use_response(tool_name="some_tool"):
    response = MagicMock()
    block = MagicMock()
    block.type = "tool_use"
    block.id = "toolu_test"
    block.name = tool_name
    block.input = {}
    response.content = [block]
    response.stop_reason = "tool_use"
    return response


def _mock_client(side_effect):
    client = MagicMock()
    client.messages.create.side_effect = side_effect
    return client


# ---------------------------------------------------------------------------
# 1. claude_client meta signal
# ---------------------------------------------------------------------------

def test_generate_response_meta_no_tools():
    meta: dict = {}
    mock = _mock_client([_text_response("done")])
    with patch.object(cl, "_get_client", return_value=mock):
        result = cl.generate_response("sys", "ctx", "hi", meta=meta)
    assert result == "done"
    assert meta["used_tools"] is False
    assert meta["used_verbatim_tool"] is False
    assert meta["tool_names"] == []


def test_generate_response_meta_with_lookup_tool():
    """A non-verbatim (lookup) tool: used_tools True but used_verbatim_tool False."""
    meta: dict = {}
    mock = _mock_client([_tool_use_response("asana_get_my_tasks"), _text_response("answer")])
    tool_result = [{"type": "tool_result", "tool_use_id": "toolu_test", "content": "data"}]
    with patch.object(cl, "_get_client", return_value=mock), \
         patch.object(cl, "_dispatch_tools_parallel", return_value=tool_result):
        result = cl.generate_response("sys", "ctx", "hi", meta=meta)
    assert result == "answer"
    assert meta["used_tools"] is True
    assert meta["used_verbatim_tool"] is False
    assert meta["tool_names"] == ["asana_get_my_tasks"]


def test_generate_response_meta_with_verbatim_table_tool():
    """A VERBATIM_TABLE_TOOLS member sets used_verbatim_tool True."""
    meta: dict = {}
    mock = _mock_client(
        [_tool_use_response("financial_get_cashflow"), _text_response("| table |")]
    )
    tool_result = [{"type": "tool_result", "tool_use_id": "toolu_test", "content": "data"}]
    with patch.object(cl, "_get_client", return_value=mock), \
         patch.object(cl, "_dispatch_tools_parallel", return_value=tool_result):
        cl.generate_response("sys", "ctx", "hi", meta=meta)
    assert meta["used_tools"] is True
    assert meta["used_verbatim_tool"] is True
    assert "financial_get_cashflow" in meta["tool_names"]


def test_generate_response_meta_omitted_is_safe():
    """No meta kwarg -> identical behavior to before (backward compat)."""
    mock = _mock_client([_text_response("ok")])
    with patch.object(cl, "_get_client", return_value=mock):
        assert cl.generate_response("sys", "ctx", "hi") == "ok"


def test_verbatim_table_tools_are_real_tools():
    """Drift guard: every verbatim-table tool name is a defined tool."""
    from cora.tools.tool_dispatch import TOOL_DEFINITIONS, VERBATIM_TABLE_TOOLS
    defined = {t["name"] for t in TOOL_DEFINITIONS}
    missing = VERBATIM_TABLE_TOOLS - defined
    assert not missing, f"VERBATIM_TABLE_TOOLS references unknown tools: {missing}"


# ---------------------------------------------------------------------------
# 2. app._dispatch_qa wiring
# ---------------------------------------------------------------------------

_RAW_REPLY = "Heads up — the **deck** is ready 🚀"
_FORMATTED_REPLY = "Heads up - the deck is ready"


def _routing_hints():
    # bypass_cache=True keeps the test off the embedding + semantic-cache path.
    return SimpleNamespace(
        bypass_cache=True, skip_kb=True, kb_k_override=None, cache_ttl=0,
    )


def _run_dispatch_qa(verbatim: bool):
    """Drive _dispatch_qa down the non-streaming path; return the final say kwargs."""

    def fake_generate(*args, meta=None, **kwargs):
        if meta is not None:
            meta["used_tools"] = True
            meta["used_verbatim_tool"] = verbatim
        return _RAW_REPLY

    # First say() call (placeholder) raises -> placeholder_ts None -> the
    # non-streaming fallback path posts the final reply via the second say().
    say = MagicMock(side_effect=[Exception("no placeholder"), {"ok": True}])

    with patch.object(app_mod, "generate_response", side_effect=fake_generate), \
         patch.object(app_mod.ic, "classify", return_value="qa"), \
         patch.object(app_mod.ic, "routing_hints", return_value=_routing_hints()), \
         patch.object(app_mod, "load_context_parts", return_value=("static", "kb")), \
         patch.object(app_mod, "load_prompt", return_value="sys"), \
         patch.object(app_mod.model_router, "choose_model", return_value="model-x"), \
         patch.object(app_mod.model_router, "short_label", return_value="x"), \
         patch.object(app_mod.user_identity, "display_name", return_value="Tester"), \
         patch.object(app_mod.user_identity, "get_user", return_value=None), \
         patch.object(app_mod.active_thread_store, "register"):
        app_mod._dispatch_qa(
            channel_id="C0TEST",
            channel_name="f3e-leadership",
            user_id="U0TEST",
            user_message="how is the deck coming?",
            reply_thread_ts="123.456",
            entity="F3E",
            client=MagicMock(),
            say=say,
        )

    assert say.call_count == 2, "expected placeholder attempt + final post"
    return say.call_args_list[1].kwargs


def test_dispatch_qa_formats_lookup_grounded_reply():
    """A reply that used a non-verbatim (lookup) tool is STILL voice-formatted
    inline (the 2.1b fix to the old too-broad bool(used_tools) bypass)."""
    kwargs = _run_dispatch_qa(verbatim=False)
    assert kwargs["text"] == _FORMATTED_REPLY


def test_dispatch_qa_skips_inline_format_for_verbatim_table():
    """A reply that used a verbatim-table tool skips the inline voice formatter
    (the table is preserved). The egress boundary's universal safety layer still
    runs at send time (not exercised here -- say is a mock)."""
    kwargs = _run_dispatch_qa(verbatim=True)
    assert kwargs["text"] == _RAW_REPLY
    # The send no longer carries a cora_verbatim kwarg (the boundary is SAFETY-only
    # and does not voice-flatten, so no opt-out signal is needed).
    assert "cora_verbatim" not in kwargs
