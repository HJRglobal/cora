"""D-043 / Code #14 D-051 re-review: a reply whose turn ran ANY tool is never stored
in the ENTITY-keyed semantic cache. gmail_inbox reads the ASKER's mailbox and the
my-tasks / my-calendar / my-deals / notes tools are per-user, so a stored reply would
be served to the next asker in the same entity with a >= 0.95-similar question
("what are my tasks?" -> another teammate's tasks for 5 minutes). Zero-tool replies
still cache. Driven through the real _dispatch_qa with the model, context load and
cache stubbed (no network, no ledger writes)."""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cora.app as app_mod

MEMBER = "U0TESTMEMBR"


class TestReplyCacheable:
    def test_a_zero_tool_turn_is_cacheable(self):
        assert app_mod._reply_cacheable({}) is True
        assert app_mod._reply_cacheable(None) is True
        assert app_mod._reply_cacheable({"tool_use_count": 0, "tool_names": []}) is True

    @pytest.mark.parametrize("meta", [
        {"tool_use_count": 1, "tool_names": ["gmail_inbox"]},
        {"tool_use_count": 2, "tool_names": ["asana_get_my_tasks", "calendar_get_my_events"]},
        {"tool_names": ["cora_my_notes"]},                 # names without a count
        {"tool_use_count": 1},                             # a count without names
        {"web_search_requests": 1},                        # a server web tool
        {"web_fetch_requests": 2},
    ])
    def test_any_tool_in_the_turn_is_never_cacheable(self, meta):
        assert app_mod._reply_cacheable(meta) is False

    def test_both_store_sites_gate_on_it(self):
        body = inspect.getsource(app_mod._dispatch_qa)
        assert body.count("and _reply_cacheable(gen_meta):") == 2
        # and every store site in _dispatch_qa is one of those two
        assert body.count("_try_cache_store(") == 2


def _drive(monkeypatch, text, *, tools):
    seen: dict = {}

    def fake_generate(*_a, meta=None, **kw):
        seen.update(kw)
        if meta is not None:
            meta["used_verbatim_tool"] = False
            if tools:
                meta["used_tools"] = True
                meta["tool_use_count"] = len(tools)
                meta["tool_names"] = list(tools)
        return "Here is what I found."

    posts = {"n": 0}

    def fake_say(**_kw):
        posts["n"] += 1
        if posts["n"] == 1:
            raise RuntimeError("no placeholder")   # -> the non-streaming path
        return {"ok": True}

    cache = MagicMock()
    cache.lookup.return_value = None
    hints = SimpleNamespace(bypass_cache=False, skip_kb=True, kb_k_override=None, cache_ttl=300)
    with patch.object(app_mod, "generate_response", side_effect=fake_generate), \
         patch.object(app_mod.ic, "classify", return_value="qa"), \
         patch.object(app_mod.ic, "routing_hints", return_value=hints), \
         patch.object(app_mod.sc, "get_cache", return_value=cache), \
         patch.object(app_mod.kb_embeddings, "embed_query", return_value=[0.0] * 8), \
         patch.object(app_mod, "load_context_parts", return_value=("static", "kb")), \
         patch.object(app_mod, "load_prompt", return_value="sys"), \
         patch.object(app_mod.model_router, "choose_model", return_value="model-x"), \
         patch.object(app_mod.model_router, "short_label", return_value="x"), \
         patch.object(app_mod.user_identity, "display_name", return_value="Member"), \
         patch.object(app_mod.user_identity, "get_user", return_value=None), \
         patch.object(app_mod.lex_phi_access, "phi_allowed", return_value=False), \
         patch.object(app_mod.knowledge_check, "recall_ask_note", return_value=""), \
         patch.object(app_mod._tool_dispatch, "describe_live_pendings", return_value=""), \
         patch.object(app_mod.active_thread_store, "register"):
        app_mod._dispatch_qa(
            channel_id="D0TESTDM2", channel_name="dm", user_id=MEMBER,
            user_message=text, reply_thread_ts="1789999254.000200", entity="HJRG",
            client=MagicMock(), say=fake_say, prior_messages=[],
        )
    return seen, cache


class TestDispatchQaCacheStore:
    @pytest.mark.parametrize("text,tools", [
        ("what are my tasks?", ["asana_get_my_tasks"]),
        ("anything new in my inbox?", ["gmail_inbox"]),
        ("what's on my calendar tomorrow?", ["calendar_get_my_events"]),
    ])
    def test_a_personal_tool_reply_is_never_stored(self, monkeypatch, text, tools):
        _seen, cache = _drive(monkeypatch, text, tools=tools)
        cache.store.assert_not_called()

    def test_control_a_zero_tool_reply_is_still_stored(self, monkeypatch):
        _seen, cache = _drive(monkeypatch, "what does OSN stand for?", tools=[])
        cache.store.assert_called_once()
