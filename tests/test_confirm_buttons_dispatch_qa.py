"""_dispatch_qa-level integration tests for the Confirm/Cancel button
attachment (design 2026-08-02): the turn-snapshot/diff mechanism must attach
a card when (and only when) a fresh stash was minted THIS turn AND the flag
is on -- and must be a complete no-op (byte-identical text, no blocks kwarg)
when the flag is off or nothing changed. Reuses the test_reply_formatter_
wiring.py harness (_run_dispatch_qa) with the fully-mocked non-streaming path.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402

import cora.app as app_mod  # noqa: E402
from cora import confirm_cards as cc  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

USER_ID = "U0TEST"
CHANNEL_NAME = "f3e-leadership"
_RAW_REPLY = "Not deleted yet -- reply to confirm and I'll delete it."


@pytest.fixture(autouse=True)
def _clear_asana_pending():
    td._PENDING_ASANA_WRITES.clear()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    yield
    td._PENDING_ASANA_WRITES.clear()


def _routing_hints():
    return SimpleNamespace(bypass_cache=True, skip_kb=True, kb_k_override=None, cache_ttl=0)


def _run_dispatch_qa(*, mint_fresh_pending: bool):
    """Drive _dispatch_qa's non-streaming path. If mint_fresh_pending, the
    fake tool call ALSO stashes a fresh Asana pending (simulating a real
    asana_delete_task preview call happening during the model's tool loop) --
    exactly what the turn-snapshot diff is meant to detect."""

    def fake_generate(*args, meta=None, **kwargs):
        if mint_fresh_pending:
            sid = cc.mint_stash_id("asana", USER_ID, CHANNEL_NAME)
            td._store_pending_asana_write(USER_ID, CHANNEL_NAME, {
                "action": "delete", "gid": "g1", "label": "Test task",
                "ts": time.time(), "stash_id": sid,
            })
        if meta is not None:
            meta["used_tools"] = True
            meta["used_verbatim_tool"] = False
        return _RAW_REPLY

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
            channel_name=CHANNEL_NAME,
            user_id=USER_ID,
            user_message="delete the smoke test task",
            reply_thread_ts="123.456",
            entity="F3E",
            client=MagicMock(),
            say=say,
        )
    assert say.call_count == 2
    return say.call_args_list[1].kwargs


class TestConfirmCardAttachment:
    def test_flag_off_no_blocks_kwarg_byte_identical_text(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        kwargs = _run_dispatch_qa(mint_fresh_pending=True)
        assert kwargs["text"] == _RAW_REPLY
        assert kwargs.get("blocks") is None

    def test_flag_on_nothing_changed_no_blocks(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
        kwargs = _run_dispatch_qa(mint_fresh_pending=False)
        assert kwargs["text"] == _RAW_REPLY
        assert kwargs.get("blocks") is None

    def test_flag_on_fresh_pending_attaches_confirm_card(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
        kwargs = _run_dispatch_qa(mint_fresh_pending=True)
        assert kwargs["text"] == _RAW_REPLY  # text itself is UNCHANGED either way
        blocks = kwargs.get("blocks")
        assert blocks is not None
        actions_block = next(b for b in blocks if b["type"] == "actions")
        action_ids = {el["action_id"] for el in actions_block["elements"]}
        assert action_ids == {cc.ACTION_CONFIRM, cc.ACTION_CANCEL}
        # The bound stash_id resolves back to the REAL pending just stashed.
        sid = actions_block["elements"][0]["value"]
        pending = td._peek_pending_asana(USER_ID, CHANNEL_NAME)
        assert pending is not None and pending["stash_id"] == sid

    def test_flag_on_older_turn_pending_not_reattached(self, monkeypatch):
        # A pending that already existed BEFORE this turn (untouched by it)
        # must never get a card attached to an UNRELATED reply.
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
        sid = cc.mint_stash_id("asana", USER_ID, CHANNEL_NAME)
        td._store_pending_asana_write(USER_ID, CHANNEL_NAME, {
            "action": "delete", "gid": "g0", "label": "Old pending",
            "ts": time.time(), "stash_id": sid,
        })
        kwargs = _run_dispatch_qa(mint_fresh_pending=False)
        assert kwargs.get("blocks") is None
