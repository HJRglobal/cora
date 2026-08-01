"""Web-search tools gate + server-tool plumbing (2026-07-31 kickoff).

Pins the kickoff acceptance criteria:
  - explicit live-web intent in a non-LEX channel -> tools attach
  - the same ask in a LEX channel/scope -> tools never offered
  - a query carrying a client-shaped name / PHI never reaches the search API
    (evaluate blocks BEFORE any attach; fail-closed on errors)
plus the claude_client server-tool mechanics (tool defs appended after the
cache breakpoint, pause_turn continuation, citations/usage meta) and the
sources-line rendering through format_reply.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from cora import web_guard
from cora.web_guard import WebDecision

ACCEPTANCE_QUERY = "what does a 96GB DDR5 kit cost right now?"

KB_HIT = {"kb_search_ran": True, "kb_relevant_hits": 3, "kb_best_distance": 0.92}
KB_MISS = {"kb_search_ran": True, "kb_relevant_hits": 0, "kb_best_distance": 1.42}


@pytest.fixture(autouse=True)
def _tmp_ledger(tmp_path, monkeypatch):
    # Belt on top of the conftest _LEDGER_CONSTS redirect: unique per test.
    monkeypatch.setattr(web_guard, "_USAGE_LEDGER", tmp_path / "web-usage.jsonl")
    monkeypatch.delenv("CORA_WEB_TOOLS", raising=False)
    monkeypatch.delenv("CORA_WEB_SEARCH_DAILY_CAP", raising=False)
    yield


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------


class TestIntent:
    def test_acceptance_query_is_web_intent(self):
        assert web_guard.is_web_intent(ACCEPTANCE_QUERY)

    @pytest.mark.parametrize(
        "text",
        [
            "can you search the web for DDR5 prices",
            "look it up online please",
            "google the latest RTX 5090 street price",
            "what's the current price of copper per pound?",
            "latest news on the port strike",
            "how much is a pallet of 20oz cans going for these days?",
        ],
    )
    def test_explicit_intent_positive(self, text):
        assert web_guard.is_web_intent(text)

    @pytest.mark.parametrize(
        "text",
        [
            "what's on my plate today",
            "what's the latest on the OSN recon?",
            "summarize the Q1 close pack",
            "look up Tommy's open deals",  # internal look-up, no web anchor
            "yes",
            "",
        ],
    )
    def test_internal_phrasings_not_web_intent(self, text):
        assert not web_guard.is_web_intent(text)

    def test_time_sensitive_plus_kb_miss_attaches(self):
        dec = web_guard.evaluate(
            "what is the AZ minimum wage as of now?", "F3E", kb_meta=dict(KB_MISS)
        )
        assert dec.attach and dec.reason == "time_sensitive_kb_miss"

    def test_time_sensitive_with_kb_hit_does_not_attach(self):
        dec = web_guard.evaluate(
            "what is the AZ minimum wage as of now?", "F3E", kb_meta=dict(KB_HIT)
        )
        assert not dec.attach and dec.reason == "no_intent"

    def test_no_intent_no_attach(self):
        dec = web_guard.evaluate("summarize the OSN recon status", "OSN", kb_meta=dict(KB_MISS))
        assert not dec.attach and dec.reason == "no_intent"

    def test_kb_missing_meta_counts_as_miss(self):
        assert web_guard._kb_missed(None)
        assert web_guard._kb_missed({})
        assert web_guard._kb_missed({"kb_search_ran": True, "kb_best_distance": None})
        assert not web_guard._kb_missed(dict(KB_HIT))


# ---------------------------------------------------------------------------
# Acceptance: attach in a normal channel, never in LEX scope
# ---------------------------------------------------------------------------


class TestScope:
    def test_acceptance_attaches_in_founder_scope(self):
        dec = web_guard.evaluate(ACCEPTANCE_QUERY, "FNDR", kb_meta=dict(KB_HIT))
        assert dec.attach and dec.reason == "explicit_intent"

    @pytest.mark.parametrize("entity", ["LEX", "LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA", "lex"])
    def test_lex_scope_never_offered(self, entity):
        dec = web_guard.evaluate(ACCEPTANCE_QUERY, entity, kb_meta=dict(KB_MISS))
        assert not dec.attach and dec.reason == "lex_scope"

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("CORA_WEB_TOOLS", "off")
        dec = web_guard.evaluate(ACCEPTANCE_QUERY, "FNDR", kb_meta=dict(KB_HIT))
        assert not dec.attach and dec.reason == "disabled"


# ---------------------------------------------------------------------------
# Egress screen — a client-shaped name / PHI never reaches the search API
# ---------------------------------------------------------------------------


class TestEgressScreen:
    @pytest.mark.parametrize(
        "query",
        [
            # The D-050 live-bug string shape: named billing/authorization.
            "search the web for Bob Smith's billing authorization requirements",
            # Care-noun-governed client name + clinical term.
            "look up online whether client Marcus being autistic qualifies for DDD",
            # Possessive + psych med name.
            "google Jalen's risperidone dosage guidelines",
            # Clinical framing.
            "search the web for what diagnosed with fragile x means for eligibility",
        ],
    )
    def test_client_shaped_names_blocked(self, query):
        dec = web_guard.evaluate(query, "FNDR", kb_meta=dict(KB_MISS))
        assert not dec.attach
        assert dec.reason == "blocked:phi"

    def test_email_blocked(self):
        dec = web_guard.evaluate(
            "search the web for jane.doe@example.com", "F3E", kb_meta=dict(KB_MISS)
        )
        assert not dec.attach and dec.reason == "blocked:email"

    def test_internal_figure_blocked(self):
        dec = web_guard.evaluate(
            "search the web for why OSN revenue was $77,629 last week",
            "OSN",
            kb_meta=dict(KB_MISS),
        )
        assert not dec.attach and dec.reason == "blocked:internal_figure"

    def test_shopping_figure_not_blocked(self):
        dec = web_guard.evaluate(
            "search the web for laptops under $1000", "F3E", kb_meta=dict(KB_MISS)
        )
        assert dec.attach

    def test_fail_closed_on_screen_error(self, monkeypatch):
        monkeypatch.setattr(
            web_guard.phi_guard, "is_any_phi", MagicMock(side_effect=RuntimeError("boom"))
        )
        dec = web_guard.evaluate(ACCEPTANCE_QUERY, "FNDR", kb_meta=dict(KB_HIT))
        assert not dec.attach and dec.reason == "error"


# ---------------------------------------------------------------------------
# Daily cap + ledger
# ---------------------------------------------------------------------------


class TestCapAndLedger:
    def test_daily_cap_blocks(self, monkeypatch):
        monkeypatch.setenv("CORA_WEB_SEARCH_DAILY_CAP", "5")
        web_guard.record_usage(5, 0, entity="F3E")
        dec = web_guard.evaluate(ACCEPTANCE_QUERY, "F3E", kb_meta=dict(KB_HIT))
        assert not dec.attach and dec.reason == "daily_cap"

    def test_usage_accumulates_today_only(self):
        web_guard.record_usage(2, 1, entity="F3E")
        web_guard.record_usage(3, 0, entity="OSN")
        # A stale line from another day is ignored.
        with open(web_guard._USAGE_LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "usage", "date": "2000-01-01", "searches": 99}) + "\n")
        assert web_guard.searches_today() == 5

    def test_decision_ledger_never_stores_query_text(self):
        dec = web_guard.evaluate(
            "search the web for Bob Smith's billing authorization", "FNDR", kb_meta=dict(KB_MISS)
        )
        web_guard.record_decision(dec, entity="FNDR", channel_name="founder-operations")
        raw = open(web_guard._USAGE_LEDGER, encoding="utf-8").read()
        assert "Bob Smith" not in raw
        assert "blocked:phi" in raw

    def test_no_intent_not_ledgered(self):
        dec = WebDecision(False, "no_intent")
        web_guard.record_decision(dec, entity="F3E")
        assert not web_guard._USAGE_LEDGER.exists()

    def test_zero_usage_not_ledgered(self):
        web_guard.record_usage(0, 0, entity="F3E")
        assert not web_guard._USAGE_LEDGER.exists()


# ---------------------------------------------------------------------------
# Sources line rendering
# ---------------------------------------------------------------------------


class TestSourcesLine:
    def test_basic_line(self):
        line = web_guard.format_sources_line(
            [
                {"url": "https://www.newegg.com/p/abc", "title": "Newegg — DDR5 96GB"},
                {"url": "https://pcpartpicker.com/x", "title": "PCPartPicker"},
            ]
        )
        assert line.startswith("Sources: ")
        assert "<https://www.newegg.com/p/abc|Newegg" in line
        assert "<https://pcpartpicker.com/x|PCPartPicker>" in line

    def test_dedup_cap_and_internal_domains_skipped(self):
        cites = [
            {"url": "https://docs.google.com/spreadsheets/d/xyz", "title": "internal sheet"},
            {"url": "https://quickbooks.intuit.com/r/x", "title": "intuit"},
            {"url": "https://a.example.com/1", "title": "A"},
            {"url": "https://a.example.com/1", "title": "A dup"},
            {"url": "https://b.example.com/2", "title": "B"},
            {"url": "https://c.example.com/3", "title": "C"},
            {"url": "https://d.example.com/4", "title": "D"},
            {"url": "https://e.example.com/5", "title": "E"},
        ]
        line = web_guard.format_sources_line(cites, max_sources=4)
        assert "docs.google.com" not in line
        assert "intuit.com" not in line
        assert line.count("<https://") == 4
        assert line.count("a.example.com") == 1

    def test_label_escaping_and_bad_urls(self):
        line = web_guard.format_sources_line(
            [
                {"url": "https://x.example.com/1", "title": "A & B | C <weird>"},
                {"url": "javascript:alert(1)", "title": "nope"},
                {"url": "https://bad url.example.com/space", "title": "nope"},
                {"url": "https://y.example.com/<angle>", "title": "nope"},
            ]
        )
        assert "&amp;" in line and "&lt;weird&gt;" in line and "/ C" in line
        assert "javascript:" not in line
        assert "bad url" not in line
        assert line.count("<https://") == 1

    def test_empty_inputs(self):
        assert web_guard.format_sources_line(None) == ""
        assert web_guard.format_sources_line([]) == ""
        assert web_guard.format_sources_line([{"title": "no url"}]) == ""

    def test_sources_tokens_survive_format_reply(self):
        from cora.reply_formatter import format_reply

        line = web_guard.format_sources_line(
            [{"url": "https://www.newegg.com/p/abc?x=1", "title": "Newegg DDR5"}]
        )
        body = "A 96GB DDR5-6000 kit runs $260-$310 right now per Newegg.\n\n" + line
        out = format_reply(body)
        assert "<https://www.newegg.com/p/abc?x=1|Newegg DDR5>" in out

    def test_sources_tokens_survive_egress_sanitizer(self):
        from cora.slack_egress import sanitize_text

        line = web_guard.format_sources_line(
            [{"url": "https://example.com/report", "title": "Example"}]
        )
        assert "<https://example.com/report|Example>" in sanitize_text("body\n\n" + line)


# ---------------------------------------------------------------------------
# claude_client server-tool plumbing
# ---------------------------------------------------------------------------


class TestClaudeClientWebTools:
    def _mock_client(self, responses):
        client = MagicMock()
        client.messages.create.side_effect = list(responses)
        return client

    @staticmethod
    def _text_response(text, stop_reason="end_turn", citations=None, searches=0):
        block = MagicMock()
        block.type = "text"
        block.text = text
        block.citations = citations or []
        resp = MagicMock()
        resp.content = [block]
        resp.stop_reason = stop_reason
        usage = MagicMock()
        usage.input_tokens = 10
        usage.cache_creation_input_tokens = 0
        usage.cache_read_input_tokens = 0
        usage.output_tokens = 5
        stu = MagicMock()
        stu.web_search_requests = searches
        stu.web_fetch_requests = 0
        usage.server_tool_use = stu
        resp.usage = usage
        return resp

    def test_web_tool_defs_shape(self):
        from cora.claude_client import _build_web_tool_defs

        defs = _build_web_tool_defs()
        assert [d["type"] for d in defs] == ["web_search_20260209", "web_fetch_20260209"]
        assert [d["name"] for d in defs] == ["web_search", "web_fetch"]
        for d in defs:
            assert d["max_uses"] >= 1
            assert "docs.google.com" in d["blocked_domains"]
            assert "cache_control" not in d
        assert defs[0]["user_location"]["timezone"] == "America/Phoenix"

    def test_web_tools_appended_after_cache_breakpoint(self):
        from cora import claude_client

        with patch.object(claude_client, "_get_client", return_value=self._mock_client(
            [self._text_response("hi")]
        )) as _:
            claude_client.generate_response(
                "prompt", "context", "question", entity="FNDR", web_tools=True,
            )
        kwargs = _.return_value.messages.create.call_args.kwargs
        tools = kwargs["tools"]
        assert tools[-1]["type"] == "web_fetch_20260209"
        assert tools[-2]["type"] == "web_search_20260209"
        # The cache breakpoint stays on the last CLIENT tool so the client-tools
        # cache prefix is byte-identical between web and non-web requests.
        client_tools = tools[:-2]
        assert client_tools, "entity tool set unexpectedly empty"
        assert client_tools[-1].get("cache_control") == {"type": "ephemeral"}
        assert all("cache_control" not in t for t in tools[-2:])
        assert kwargs["timeout"] == claude_client._WEB_TIMEOUT

    def test_no_web_tools_by_default(self):
        from cora import claude_client

        with patch.object(claude_client, "_get_client", return_value=self._mock_client(
            [self._text_response("hi")]
        )) as _:
            claude_client.generate_response("prompt", "context", "question", entity="FNDR")
        kwargs = _.return_value.messages.create.call_args.kwargs
        assert all(not str(t.get("type", "")).startswith("web_") for t in kwargs["tools"])
        assert kwargs["timeout"] == claude_client._TIMEOUT

    def test_pause_turn_resumes_and_concatenates_text(self):
        from cora import claude_client

        paused = self._text_response("Searching for prices... ", stop_reason="pause_turn")
        done = self._text_response("A 96GB kit runs about $280.", stop_reason="end_turn")
        mock = self._mock_client([paused, done])
        with patch.object(claude_client, "_get_client", return_value=mock):
            out = claude_client.generate_response(
                "prompt", "context", "q", entity="FNDR", web_tools=True,
            )
        assert mock.messages.create.call_count == 2
        assert out == "Searching for prices... A 96GB kit runs about $280."
        assert "  " not in out
        # The paused assistant turn was appended verbatim, with NO tool_result turn.
        second_messages = mock.messages.create.call_args_list[1].kwargs["messages"]
        assert second_messages[-1]["role"] == "assistant"

    def test_pause_turn_without_web_tools_returns_text(self):
        # Defensive: a pause_turn on a non-web call (should not happen) must not
        # loop -- but with no server tools it just ends the turn via the != gate.
        from cora import claude_client

        paused = self._text_response("partial", stop_reason="pause_turn")
        done = self._text_response("finished", stop_reason="end_turn")
        mock = self._mock_client([paused, done])
        with patch.object(claude_client, "_get_client", return_value=mock):
            out = claude_client.generate_response("prompt", "context", "q", entity="FNDR")
        # pause_turn is resumed regardless of web_tools (server tools may exist
        # in future paths); two calls, concatenated text.
        assert mock.messages.create.call_count == 2
        assert out == "partial finished"

    def test_citations_and_usage_collected_into_meta(self):
        from cora import claude_client

        cite = MagicMock()
        cite.url = "https://www.newegg.com/p/abc"
        cite.title = "Newegg"
        resp = self._text_response("answer", citations=[cite], searches=2)
        meta: dict = {}
        with patch.object(claude_client, "_get_client", return_value=self._mock_client([resp])):
            claude_client.generate_response(
                "prompt", "context", "q", entity="FNDR", meta=meta, web_tools=True,
            )
        assert meta["web_search_requests"] == 2
        assert meta["web_citations"] == [
            {"url": "https://www.newegg.com/p/abc", "title": "Newegg"}
        ]

    def test_meta_untouched_when_web_off(self):
        from cora import claude_client

        meta: dict = {}
        with patch.object(claude_client, "_get_client", return_value=self._mock_client(
            [self._text_response("hi")]
        )):
            claude_client.generate_response(
                "prompt", "context", "q", entity="FNDR", meta=meta,
            )
        assert "web_citations" not in meta
        assert "web_search_requests" not in meta


# ---------------------------------------------------------------------------
# app.py dispatch wiring
# ---------------------------------------------------------------------------


class TestAppWiring:
    def test_dispatch_source_wires_web_gate(self):
        # Source-level pins (the dispatch path needs a live Slack client to run
        # end-to-end): the gate is consulted, both generate calls carry web_tools,
        # LEX-off + screen live in web_guard.evaluate which is the only entry.
        import inspect

        import cora.app as app_module

        src = inspect.getsource(app_module._dispatch_qa)
        assert "web_guard.evaluate(" in src
        assert src.count("web_tools=web_on") == 2
        assert "web_guard.record_usage(" in src
        assert "web_guard.format_sources_line(" in src
        assert "cache_storable = False" in src
        assert "WEB_MODE_CONTEXT" in src

    def test_web_gate_skipped_on_forced_tool_turns(self):
        import inspect

        import cora.app as app_module

        src = inspect.getsource(app_module._dispatch_qa)
        assert (
            "if force_tool is None and not assume_confirm and retrieval_grant is None:"
            in src
        )

    def test_generate_response_signature_carries_web_tools(self):
        import inspect

        from cora import claude_client

        for fn in (claude_client.generate_response, claude_client.generate_response_streaming):
            assert "web_tools" in inspect.signature(fn).parameters
