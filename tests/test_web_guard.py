"""Web-search tools gate + server-tool plumbing (2026-07-31 kickoff + D-051 remediation).

Pins the kickoff acceptance criteria:
  - explicit live-web intent in a non-LEX channel -> tools attach
  - the same ask in a LEX channel/scope -> tools never offered
  - a query carrying a client-shaped name / PHI never reaches the search API
    (evaluate blocks BEFORE any attach; fail-closed on errors)
plus the claude_client server-tool mechanics (web-tools-only tool set, pause_turn
continuation on both loops, citations/usage meta) and the sources-line rendering
through format_reply, and the D-051 remediation (skip_kb not a miss, model-support
gate, tightened intent regexes, personal-context exclusion at the app gate).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from cora import web_guard
from cora.web_guard import WebDecision

ACCEPTANCE_QUERY = "what does a 96GB DDR5 kit cost right now?"
SUP = "claude-sonnet-5"  # a model that accepts the 20260209 web tool revisions

KB_HIT = {"kb_search_ran": True, "kb_relevant_hits": 3, "kb_best_distance": 0.92}
KB_MISS = {"kb_search_ran": True, "kb_relevant_hits": 0, "kb_best_distance": 1.42}


@pytest.fixture(autouse=True)
def _tmp_ledger(tmp_path, monkeypatch):
    # Belt on top of the conftest _LEDGER_CONSTS redirect: unique per test.
    monkeypatch.setattr(web_guard, "_USAGE_LEDGER", tmp_path / "web-usage.jsonl")
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
            "web search for the current gas price in phoenix",
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
            # D-051: Google Workspace product phrasing must NOT read as web search.
            "can you check the Google Sheet for last week's OSN cash?",
            "pull the numbers from our Google Drive",
            "what's on the Google Calendar tomorrow?",
            "share the google doc with the team",
            # D-051: internal-subject price asks are not web intent.
            "what's our current pricing on the 12-pack?",
            "how does F3E cost structure compare to last year",
            "what's the current status of the launch",
        ],
    )
    def test_internal_phrasings_not_web_intent(self, text):
        assert not web_guard.is_web_intent(text)

    def test_time_sensitive_plus_kb_miss_attaches(self):
        dec = web_guard.evaluate(
            "what is the AZ minimum wage as of now?", "F3E", kb_meta=dict(KB_MISS), model=SUP
        )
        assert dec.attach and dec.reason == "time_sensitive_kb_miss"

    def test_time_sensitive_with_kb_hit_does_not_attach(self):
        dec = web_guard.evaluate(
            "what is the AZ minimum wage as of now?", "F3E", kb_meta=dict(KB_HIT), model=SUP
        )
        assert not dec.attach and dec.reason == "no_intent"

    @pytest.mark.parametrize(
        "text",
        [
            "what's the current status of the recon",
            "any updates on the launch",
            "what's currently on my plate",
            "the latest on the OSN reconciliation",
        ],
    )
    def test_internal_status_not_time_sensitive(self, text):
        # D-051: bare "current status"/"latest on X"/"currently" must not trip the
        # fallback even on a KB miss.
        assert not web_guard.is_time_sensitive(text)
        dec = web_guard.evaluate(text, "OSN", kb_meta=dict(KB_MISS), model=SUP)
        assert not dec.attach and dec.reason == "no_intent"

    def test_skip_kb_is_not_a_miss(self):
        # A FINANCIAL/IDENTITY intent skips KB (kb_meta stays empty). A
        # time-sensitive internal question must NOT attach web tools.
        assert not web_guard._kb_missed({}, skip_kb=True)
        dec = web_guard.evaluate(
            "what's our cash position as of today?", "OSN", kb_meta={}, skip_kb=True, model=SUP
        )
        # (also blocked by internal-subject + no market anchor, but skip_kb is the
        # deterministic belt for the empty-kb_meta case)
        assert not dec.attach

    def test_kb_missing_meta_counts_as_miss_when_not_skipped(self):
        assert web_guard._kb_missed(None)
        assert web_guard._kb_missed({})
        assert web_guard._kb_missed({"kb_search_ran": True, "kb_best_distance": None})
        assert not web_guard._kb_missed(dict(KB_HIT))

    def test_kb_miss_boundary_is_strict(self):
        # distance == threshold is a HIT (aligns with context_loader's <= gate).
        assert not web_guard._kb_missed({"kb_search_ran": True, "kb_best_distance": 1.30})
        assert web_guard._kb_missed({"kb_search_ran": True, "kb_best_distance": 1.301})


# ---------------------------------------------------------------------------
# Acceptance: attach in a normal channel, never in LEX scope
# ---------------------------------------------------------------------------


class TestScope:
    def test_acceptance_attaches_in_founder_scope(self):
        dec = web_guard.evaluate(ACCEPTANCE_QUERY, "FNDR", kb_meta=dict(KB_HIT), model=SUP)
        assert dec.attach and dec.reason == "explicit_intent"

    @pytest.mark.parametrize("entity", ["LEX", "LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA", "lex"])
    def test_lex_scope_never_offered(self, entity):
        dec = web_guard.evaluate(ACCEPTANCE_QUERY, entity, kb_meta=dict(KB_MISS), model=SUP)
        assert not dec.attach and dec.reason == "lex_scope"

    def test_lex_without_intent_is_no_intent_not_lex_scope(self):
        # Intent is checked before the LEX gate, so a no-intent LEX ask is not
        # ledgered as a high-volume lex_scope block.
        dec = web_guard.evaluate("what's the revalidation status?", "LEX", kb_meta=dict(KB_HIT), model=SUP)
        assert not dec.attach and dec.reason == "no_intent"

    def test_model_unsupported_soft_degrades(self):
        dec = web_guard.evaluate(ACCEPTANCE_QUERY, "FNDR", kb_meta=dict(KB_HIT), model="claude-haiku-4-5")
        assert not dec.attach and dec.reason == "model_unsupported"


# ---------------------------------------------------------------------------
# LEX lane (CORA_WEB_TOOLS_LEX) -- Harrison decision 2026-08-06, superseding the
# D-097 v1 "LEX scope OFF entirely" line. Default OFF; stricter screen when ON.
# ---------------------------------------------------------------------------

LEX_POLICY_QUERY = "search the web for the Arizona DDD live-in caregiver respite rate policy"


@pytest.fixture
def _lex_staff(monkeypatch):
    """Pin the roster so the name screen is deterministic (no YAML dependency)."""
    monkeypatch.setattr(
        web_guard, "_staff_names_cache",
        frozenset({"Shaun Hawkins", "Jennifer Mortensen", "Harrison Rogers"}),
    )
    yield


class TestLexWebLane:
    @pytest.mark.parametrize("value", ["", "off", "0", "false", "no", "disabled", "maybe"])
    def test_flag_defaults_and_unrecognized_read_off(self, monkeypatch, value):
        # Fail-closed whitelist: only an explicitly-truthy value opens the lane.
        # NOTE the deliberate asymmetry vs CORA_WEB_TOOLS, where "" is truthy --
        # extending scope to the most regulated entity is opt-in, never inherited.
        monkeypatch.setenv("CORA_WEB_TOOLS_LEX", value)
        assert not web_guard.lex_web_enabled()
        dec = web_guard.evaluate(LEX_POLICY_QUERY, "LEX-LLC", kb_meta=dict(KB_MISS), model=SUP)
        assert not dec.attach and dec.reason == "lex_scope"

    def test_flag_unset_reads_off(self, monkeypatch):
        monkeypatch.delenv("CORA_WEB_TOOLS_LEX", raising=False)
        assert not web_guard.lex_web_enabled()

    @pytest.mark.parametrize("value", ["on", "1", "true", "yes", "ON", " True "])
    def test_flag_truthy_spellings_open_the_lane(self, monkeypatch, value):
        monkeypatch.setenv("CORA_WEB_TOOLS_LEX", value)
        assert web_guard.lex_web_enabled()

    @pytest.mark.parametrize("entity", ["LEX", "LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA", "lex"])
    def test_all_four_sub_entities_attach_on_a_clean_policy_ask(
        self, monkeypatch, _lex_staff, entity
    ):
        # Smoke (a): one screen covers LEX and all four sub-entities.
        monkeypatch.setenv("CORA_WEB_TOOLS_LEX", "on")
        dec = web_guard.evaluate(LEX_POLICY_QUERY, entity, kb_meta=dict(KB_MISS), model=SUP)
        assert dec.attach and dec.reason == "explicit_intent"

    @pytest.mark.parametrize("query", [
        # Smoke (b): a client-style name never leaves the machine. Each of these
        # is a name in care/admin context that carries NO clinical vocabulary of
        # its own -- the residual is_any_phi alone would miss.
        "search the web for DDD respite rates for client Marcus Delgado",
        "google the DDD policy on units for member Priya Raghunathan",
        # A client whose first name collides with a rostered teammate's: the
        # detector must NOT let the roster shield a care-noun-governed name.
        "search the web for respite hour limits for participant Aaron",
        # ...nor a name that is also a Phoenix-metro city (Lexington's market).
        "search the web for the appeal deadline for participant Gilbert",
    ])
    def test_client_style_name_is_blocked_even_with_the_lane_on(
        self, monkeypatch, _lex_staff, query
    ):
        monkeypatch.setenv("CORA_WEB_TOOLS_LEX", "on")
        dec = web_guard.evaluate(query, "LEX-LLC", kb_meta=dict(KB_MISS), model=SUP)
        assert not dec.attach
        # Assert the SPECIFIC reason: "blocked:" alone stays green even if the
        # whole lex_strict screen is reverted, because is_any_phi fires first on
        # some phrasings (D-051 test lens -- the assertion was vacuous).
        assert dec.reason == "blocked:lex_person_name"

    @pytest.mark.parametrize("query", [
        # Normally-capitalized policy asks: every other LEX fixture opens
        # lowercase, which hid a whole false-positive class where a
        # sentence-initial capitalized verb read as a first name (D-051).
        "Search the web for the Arizona DDD live-in caregiver respite rate policy",
        "Search the web for the Arizona DDD provider revalidation requirements",
        "Google the EVV attestation requirements for Arizona day programs",
        # A rostered teammate named mid-sentence is a colleague, not a client.
        "search the web for what DDD requires, Jennifer Mortensen asked about respite units",
    ])
    def test_capitalized_policy_asks_still_attach(self, monkeypatch, _lex_staff, query):
        monkeypatch.setenv("CORA_WEB_TOOLS_LEX", "on")
        dec = web_guard.evaluate(query, "LEX-LLC", kb_meta=dict(KB_MISS), model=SUP)
        assert dec.attach, f"false positive blocked a legitimate ask: {dec.reason}"

    def test_a_rostered_teammate_name_is_not_a_client(self, monkeypatch, _lex_staff):
        # A LEX teammate named in a web query is a colleague, not a client --
        # over-blocking here is the LEX-17 dead-end failure mode.
        monkeypatch.setenv("CORA_WEB_TOOLS_LEX", "on")
        dec = web_guard.evaluate(
            "search the web for DDD respite billing guidance Shaun Hawkins asked about",
            "LEX-LLC", kb_meta=dict(KB_MISS), model=SUP,
        )
        assert dec.attach

    def test_lex_strict_screen_is_additive_not_a_replacement(self, monkeypatch, _lex_staff):
        # is_any_phi still runs FIRST for LEX: a clinical query blocks as "phi",
        # not as the narrower name reason.
        monkeypatch.setenv("CORA_WEB_TOOLS_LEX", "on")
        dec = web_guard.evaluate(
            "search the web for risperidone dosing guidance", "LEX-LLC",
            kb_meta=dict(KB_MISS), model=SUP,
        )
        assert not dec.attach and dec.reason == "blocked:phi"

    def test_name_screen_does_not_apply_outside_lex(self, monkeypatch, _lex_staff):
        # Invariant 4: non-LEX behaviour is identical with the lane ON.
        monkeypatch.setenv("CORA_WEB_TOOLS_LEX", "on")
        dec = web_guard.evaluate(
            "search the web for DDD respite rates for client Marcus Delgado",
            "FNDR", kb_meta=dict(KB_MISS), model=SUP,
        )
        assert dec.attach  # the LEX-only belt must not leak into other scopes

    def test_name_screen_error_fails_closed(self, monkeypatch, _lex_staff):
        monkeypatch.setenv("CORA_WEB_TOOLS_LEX", "on")
        monkeypatch.setattr(
            web_guard.phi_guard, "has_care_context_person_name",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        dec = web_guard.evaluate(LEX_POLICY_QUERY, "LEX-LLC", kb_meta=dict(KB_MISS), model=SUP)
        assert not dec.attach and dec.reason == "blocked:lex_screen_error"

    def test_blocked_lex_ask_is_ledgered_without_query_text(self, monkeypatch, _lex_staff):
        # D-082: the ledger records the decision, never the query.
        monkeypatch.setenv("CORA_WEB_TOOLS_LEX", "on")
        query = "search the web for DDD respite rates for client Marcus Delgado"
        dec = web_guard.evaluate(query, "LEX-LLC", kb_meta=dict(KB_MISS), model=SUP)
        web_guard.record_decision(dec, entity="LEX-LLC", channel_name="llc-leadership")
        written = open(web_guard._USAGE_LEDGER, encoding="utf-8").read()
        assert "blocked:lex_person_name" in written
        assert "Marcus" not in written and "respite" not in written
        assert web_guard.evaluate(ACCEPTANCE_QUERY, "FNDR", kb_meta=dict(KB_HIT), model=None).reason == "model_unsupported"

    @pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-sonnet-4-6", "claude-opus-5", "claude-opus-4-8"])
    def test_supported_models(self, model):
        assert web_guard.web_model_supported(model)

    def test_kill_switch_and_fail_closed_spellings(self, monkeypatch):
        for val in ("off", "0", "false", "no", "disabled", "none", "maybe"):
            monkeypatch.setenv("CORA_WEB_TOOLS", val)
            dec = web_guard.evaluate(ACCEPTANCE_QUERY, "FNDR", kb_meta=dict(KB_HIT), model=SUP)
            assert not dec.attach and dec.reason == "disabled", val
        for val in ("on", "1", "true", "yes"):
            monkeypatch.setenv("CORA_WEB_TOOLS", val)
            assert web_guard.evaluate(ACCEPTANCE_QUERY, "FNDR", kb_meta=dict(KB_HIT), model=SUP).attach


# ---------------------------------------------------------------------------
# Egress screen — a client-shaped name / PHI never reaches the search API
# ---------------------------------------------------------------------------


class TestEgressScreen:
    @pytest.mark.parametrize(
        "query",
        [
            "search the web for Bob Smith's billing authorization requirements",
            "look up online whether client Marcus being autistic qualifies for DDD",
            "google Jalen's risperidone dosage guidelines",
            "search the web for what diagnosed with fragile x means for eligibility",
        ],
    )
    def test_client_shaped_names_blocked(self, query):
        dec = web_guard.evaluate(query, "FNDR", kb_meta=dict(KB_MISS), model=SUP)
        assert not dec.attach and dec.reason == "blocked:phi"

    def test_email_blocked(self):
        dec = web_guard.evaluate(
            "search the web for jane.doe@example.com", "F3E", kb_meta=dict(KB_MISS), model=SUP
        )
        assert not dec.attach and dec.reason == "blocked:email"

    @pytest.mark.parametrize(
        "query",
        [
            "search the web for why OSN revenue was $77,629 last week",
            "look up online how F3 Energy revenue of 1.2M compares",  # bare magnitude
            "web search for our HJRP burn of $35k",
        ],
    )
    def test_internal_figure_blocked(self, query):
        dec = web_guard.evaluate(query, "OSN", kb_meta=dict(KB_MISS), model=SUP)
        assert not dec.attach and dec.reason == "blocked:internal_figure"

    def test_shopping_figure_not_blocked(self):
        dec = web_guard.evaluate(
            "search the web for laptops under $1000", "F3E", kb_meta=dict(KB_MISS), model=SUP
        )
        assert dec.attach

    def test_fail_closed_on_screen_error(self, monkeypatch):
        monkeypatch.setattr(
            web_guard.phi_guard, "is_any_phi", MagicMock(side_effect=RuntimeError("boom"))
        )
        dec = web_guard.evaluate(ACCEPTANCE_QUERY, "FNDR", kb_meta=dict(KB_HIT), model=SUP)
        assert not dec.attach and dec.reason == "error"


# ---------------------------------------------------------------------------
# Daily cap + ledger
# ---------------------------------------------------------------------------


class TestCapAndLedger:
    def test_daily_cap_blocks(self, monkeypatch):
        monkeypatch.setenv("CORA_WEB_SEARCH_DAILY_CAP", "5")
        web_guard.record_usage(5, 0, entity="F3E")
        dec = web_guard.evaluate(ACCEPTANCE_QUERY, "F3E", kb_meta=dict(KB_HIT), model=SUP)
        assert not dec.attach and dec.reason == "daily_cap"

    def test_below_cap_attaches(self, monkeypatch):
        monkeypatch.setenv("CORA_WEB_SEARCH_DAILY_CAP", "5")
        web_guard.record_usage(4, 0, entity="F3E")
        assert web_guard.evaluate(ACCEPTANCE_QUERY, "F3E", kb_meta=dict(KB_HIT), model=SUP).attach

    def test_cap_zero_disables(self, monkeypatch):
        monkeypatch.setenv("CORA_WEB_SEARCH_DAILY_CAP", "0")
        dec = web_guard.evaluate(ACCEPTANCE_QUERY, "F3E", kb_meta=dict(KB_HIT), model=SUP)
        assert not dec.attach and dec.reason == "daily_cap"

    def test_max_uses_floored_at_one(self, monkeypatch):
        monkeypatch.setenv("CORA_WEB_SEARCH_MAX_USES", "0")
        monkeypatch.setenv("CORA_WEB_FETCH_MAX_USES", "-3")
        assert web_guard.search_max_uses() == 1
        assert web_guard.fetch_max_uses() == 1

    def test_usage_accumulates_today_only(self):
        web_guard.record_usage(2, 1, entity="F3E")
        web_guard.record_usage(3, 0, entity="OSN")
        with open(web_guard._USAGE_LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "usage", "date": "2000-01-01", "searches": 99}) + "\n")
        assert web_guard.searches_today() == 5

    def test_decision_ledger_never_stores_query_text(self):
        dec = web_guard.evaluate(
            "search the web for Bob Smith's billing authorization", "FNDR",
            kb_meta=dict(KB_MISS), model=SUP,
        )
        web_guard.record_decision(dec, entity="FNDR", channel_name="founder-operations", user_id="U1")
        raw = open(web_guard._USAGE_LEDGER, encoding="utf-8").read()
        assert "Bob Smith" not in raw
        assert "blocked:phi" in raw
        assert '"user": "U1"' in raw

    def test_no_intent_not_ledgered(self):
        web_guard.record_decision(WebDecision(False, "no_intent"), entity="F3E")
        assert not web_guard._USAGE_LEDGER.exists()

    def test_lex_scope_is_ledgered(self):
        # lex_scope only arises when web intent was present -> worth recording.
        web_guard.record_decision(WebDecision(False, "lex_scope"), entity="LEX")
        assert "lex_scope" in open(web_guard._USAGE_LEDGER, encoding="utf-8").read()

    def test_zero_usage_not_ledgered(self):
        web_guard.record_usage(0, 0, entity="F3E")
        assert not web_guard._USAGE_LEDGER.exists()

    def test_ledger_self_trims_when_large(self, monkeypatch):
        monkeypatch.setattr(web_guard, "_LEDGER_TRIM_BYTES", 200)
        # Old rows (well beyond the 7-day keep window).
        with open(web_guard._USAGE_LEDGER, "w", encoding="utf-8") as fh:
            for _ in range(50):
                fh.write(json.dumps({"event": "usage", "date": "2000-01-01", "searches": 1}) + "\n")
        # A fresh append triggers the size-gated trim, dropping the stale rows.
        web_guard.record_usage(1, 0, entity="F3E")
        raw = open(web_guard._USAGE_LEDGER, encoding="utf-8").read()
        assert "2000-01-01" not in raw
        assert web_guard.searches_today() == 1

    def test_gate_skipped_reason_is_ledgered(self):
        # cq-49a7835f081c observability: a deterministic app-gate exclusion on a
        # web-actionable turn is recorded as a block row (not silently dropped
        # like no_intent/disabled).
        web_guard.record_decision(
            WebDecision(False, "gate_skipped:phi_custodian"),
            entity="LEX-LLC", channel_name="dm", user_id="U1",
        )
        rows = [
            json.loads(line)
            for line in web_guard._USAGE_LEDGER.read_text(encoding="utf-8").splitlines()
        ]
        assert rows and rows[-1]["event"] == "block"
        assert rows[-1]["reason"] == "gate_skipped:phi_custodian"


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
            {"url": "https://app.hubspot.com/deal/1", "title": "deal"},
            {"url": "https://app.fireflies.ai/view/1", "title": "meeting"},
            {"url": "https://a.example.com/1", "title": "A"},
            {"url": "https://a.example.com/1", "title": "A dup"},
            {"url": "https://b.example.com/2", "title": "B"},
            {"url": "https://c.example.com/3", "title": "C"},
            {"url": "https://d.example.com/4", "title": "D"},
            {"url": "https://e.example.com/5", "title": "E"},
        ]
        line = web_guard.format_sources_line(cites, max_sources=4)
        for host in ("docs.google.com", "intuit.com", "hubspot.com", "fireflies.ai"):
            assert host not in line
        assert line.count("<https://") == 4
        assert line.count("a.example.com") == 1

    def test_entity_token_label_falls_back_to_host(self):
        # A page must not smuggle an internal entity token into the visible link.
        line = web_guard.format_sources_line(
            [{"url": "https://evil.example.com/x", "title": "OSN internal revenue leak"}]
        )
        assert "OSN" not in line
        assert "<https://evil.example.com/x|evil.example.com>" in line

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
    def _text_response(text, stop_reason="end_turn", citations=None, searches=0, fetches=0):
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
        stu.web_fetch_requests = fetches
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

    def test_web_turn_is_web_tools_only(self):
        # D-034 injection belt: a web-enabled turn drops the client tool set.
        from cora import claude_client

        with patch.object(claude_client, "_get_client", return_value=self._mock_client(
            [self._text_response("hi")]
        )) as _:
            claude_client.generate_response(
                "prompt", "context", "question", entity="FNDR", web_tools=True,
            )
        kwargs = _.return_value.messages.create.call_args.kwargs
        tools = kwargs["tools"]
        assert [t["type"] for t in tools] == ["web_search_20260209", "web_fetch_20260209"]
        assert all("cache_control" not in t for t in tools)
        assert kwargs["timeout"] == claude_client._WEB_TIMEOUT
        assert kwargs["max_tokens"] == claude_client._WEB_MAX_TOKENS

    def test_no_web_tools_by_default(self):
        from cora import claude_client

        with patch.object(claude_client, "_get_client", return_value=self._mock_client(
            [self._text_response("hi")]
        )) as _:
            claude_client.generate_response("prompt", "context", "question", entity="FNDR")
        kwargs = _.return_value.messages.create.call_args.kwargs
        assert all(not str(t.get("type", "")).startswith("web_") for t in kwargs["tools"])
        assert kwargs["timeout"] == claude_client._TIMEOUT
        assert kwargs["max_tokens"] == claude_client._MAX_TOKENS

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
        second_messages = mock.messages.create.call_args_list[1].kwargs["messages"]
        assert second_messages[-1]["role"] == "assistant"

    def test_pause_turn_iteration_cap_returns_partial(self):
        from cora import claude_client

        # More pauses than the iteration budget -> partial text, no raise.
        pauses = [self._text_response(f"chunk{i} ", stop_reason="pause_turn") for i in range(6)]
        mock = self._mock_client(pauses)
        with patch.object(claude_client, "_get_client", return_value=mock):
            out = claude_client.generate_response(
                "prompt", "context", "q", entity="FNDR", web_tools=True,
            )
        assert mock.messages.create.call_count == claude_client._MAX_TOOL_ITERATIONS + 1
        assert out.startswith("chunk0")

    def test_streaming_pause_turn_resumes(self):
        from cora import claude_client

        # Streaming: text arrives as deltas and accumulates ACROSS iterations; the
        # final message per iteration carries stop_reason. iter0 streams "interim "
        # then pauses; iter1 streams the answer then ends. The done branch must NOT
        # replace the fuller accumulated text with the shorter final-message tail.
        def _stream_cm(deltas, final):
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=_FakeStream(deltas, final))
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        paused_final = self._text_response("interim ", stop_reason="pause_turn")
        done_final = self._text_response("final answer $280.", stop_reason="end_turn")
        client = MagicMock()
        client.messages.stream.side_effect = [
            _stream_cm(["interim "], paused_final),
            _stream_cm(["final answer $280."], done_final),
        ]
        with patch.object(claude_client, "_get_client", return_value=client):
            out = claude_client.generate_response_streaming(
                "prompt", "context", "q", entity="FNDR", web_tools=True,
            )
        assert client.messages.stream.call_count == 2
        assert out == "interim final answer $280."
        skw = client.messages.stream.call_args_list[0].kwargs
        assert skw["max_tokens"] == claude_client._WEB_MAX_TOKENS
        assert [t["type"] for t in skw["tools"]] == ["web_search_20260209", "web_fetch_20260209"]

    def test_citations_and_usage_collected_into_meta(self):
        from cora import claude_client

        cite = MagicMock()
        cite.url = "https://www.newegg.com/p/abc"
        cite.title = "Newegg"
        resp = self._text_response("answer", citations=[cite], searches=2, fetches=1)
        meta: dict = {}
        with patch.object(claude_client, "_get_client", return_value=self._mock_client([resp])):
            claude_client.generate_response(
                "prompt", "context", "q", entity="FNDR", meta=meta, web_tools=True,
            )
        assert meta["web_search_requests"] == 2
        assert meta["web_fetch_requests"] == 1
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


class _FakeStream:
    """Minimal streaming context body: yields text-delta events, then get_final_message()."""

    def __init__(self, deltas, final):
        self._deltas = deltas
        self._final = final

    def __iter__(self):
        for d in self._deltas:
            ev = MagicMock()
            ev.type = "content_block_delta"
            ev.delta = MagicMock()
            ev.delta.type = "text_delta"
            ev.delta.text = d
            yield ev

    def get_final_message(self):
        return self._final


# ---------------------------------------------------------------------------
# app.py dispatch wiring (source-level pins — the dispatch path needs a live
# Slack client to run end-to-end)
# ---------------------------------------------------------------------------


class TestAppWiring:
    def _src(self):
        import inspect

        import cora.app as app_module

        return inspect.getsource(app_module._dispatch_qa)

    def test_dispatch_source_wires_web_gate(self):
        src = self._src()
        assert "web_guard.evaluate(" in src
        assert src.count("web_tools=web_on") == 2
        assert "web_guard.record_usage(" in src
        assert "web_guard.format_sources_line(" in src
        assert "cache_storable = False" in src
        assert "WEB_MODE_CONTEXT" in src

    def test_web_gate_excludes_confirm_grant_and_personal_context(self):
        src = self._src()
        # Forced-tool / confirm / retrieval-grant / custodian / personal
        # exclusions survive as the web_gate_skip reason chain (cq-49a7835f081c
        # made them observable instead of silent).
        assert 'web_gate_skip = "forced_tool"' in src
        assert 'web_gate_skip = "assume_confirm"' in src
        assert 'web_gate_skip = "retrieval_grant"' in src
        assert 'web_gate_skip = "phi_custodian"' in src
        assert 'web_gate_skip = "unstripped_personal"' in src
        # Attach happens ONLY when no exclusion fired — pin the NESTING, not
        # just line existence (D-051 spec lens: a hoisted attach assignment
        # would keep bare string pins green while breaking all 5 exclusions).
        gate_idx = src.index("if web_gate_skip is None:")
        assert src.index('web_gate_skip = "forced_tool"') < gate_idx
        assert src.count("web_on = web_decision.attach") == 1
        assert src.index("web_on = web_decision.attach") > gate_idx
        # The belt reads the SAME kb_meta the load populated: the dict is
        # initialized once and never rebound between load and gate.
        assert src.count("kb_meta: dict = {}") == 1
        assert " kb_meta = " not in src

    def test_web_clean_load_wired(self):
        # cq-49a7835f081c: an explicit-web-intent turn that WILL attach loads a
        # web-clean context (notes/unstripped-personal absent by construction);
        # custodian/blocked/capped/disabled turns keep their full context
        # (they never attach, so degrading them is pure loss — D-051 review).
        src = self._src()
        seg = src[src.index("web_clean = ("):src.index("static_text, kb_text = load_context_parts")]
        assert "web_intent" in seg
        # R1 (2026-08-07): `not phi_custodian` was REMOVED from this condition on
        # purpose -- it made the LEX web lane inert for the only people who use
        # it. The custodian is now handled by demoting the CONTENT (web_clean
        # forces the scrub in context_loader) rather than withholding the turn.
        # See TestR1CustodianWebLane.
        assert "not phi_custodian" not in seg
        assert ").attach" in seg  # pre-flight evaluate: degrade only on real attach
        assert "kb_meta=None," in seg  # explicit leg is kb_meta-independent
        assert "web_clean=web_clean," in src

    def test_web_clean_has_no_separate_lex_clause(self):
        # LEX lane (2026-08-06): evaluate() is the SINGLE authority on attach.
        # A separate `not is_lex_scope(entity)` clause here would mean an
        # ENABLED LEX web turn attaches while still carrying the note overlay /
        # unstripped Tier-1 posture / cross-entity fallback in the context the
        # model composes outbound queries from. Behavioural coverage that the
        # OFF default is unchanged lives in TestLexWebLane.
        src = self._src()
        seg = src[src.index("web_clean = ("):src.index("static_text, kb_text = load_context_parts")]
        assert "is_lex_scope" not in seg

    def test_gate_skips_are_observable(self):
        # A deterministic exclusion swallowing a web-actionable turn must leave
        # ledger + log evidence (the original failure was three silent asks).
        src = self._src()
        assert 'f"gate_skipped:{web_gate_skip}"' in src
        assert "web_tools gate_skipped" in src

    def test_web_gate_passes_skip_kb_and_model(self):
        src = self._src()
        assert "skip_kb=hints.skip_kb" in src
        assert "model=model_router.MODEL_SONNET" in src

    def test_web_turn_skips_gap_logging(self):
        src = self._src()
        assert "if not web_on:" in src

    def test_web_intent_bypasses_cache(self):
        src = self._src()
        assert "web_intent = web_guard.is_web_intent(user_message)" in src
        assert "and not web_intent" in src

    def test_generate_response_signature_carries_web_tools(self):
        import inspect

        from cora import claude_client

        for fn in (claude_client.generate_response, claude_client.generate_response_streaming):
            assert "web_tools" in inspect.signature(fn).parameters


# ---------------------------------------------------------------------------
# Web-clean context load (cq-49a7835f081c): a turn that may carry web tools is
# built with NO unstripped personal content, so the D-051 personal-context
# exclusion is satisfied by construction instead of silently degrading.
# ---------------------------------------------------------------------------


class TestLexScopePublic:
    def test_is_lex_scope_public_name(self):
        # app.py scopes the web-clean load on this public predicate.
        assert web_guard.is_lex_scope("LEX")
        assert web_guard.is_lex_scope("LEX-LLC")
        assert web_guard.is_lex_scope("lex-lts")
        assert not web_guard.is_lex_scope("F3E")
        assert not web_guard.is_lex_scope("")


class TestWebCleanContextLoad:
    def _mk(self, distance, source="asana", chunk_id="c1", content="benign fact",
            metadata=None, source_id="s1", title="t"):
        from cora.knowledge_base.store import SearchResult

        return SearchResult(
            chunk_id=chunk_id, source=source, source_id=source_id, entity="F3E",
            title=title, content=content, deep_link="", date_modified=None,
            distance=distance, author="", metadata=metadata,
        )

    def _wire(self, monkeypatch, results):
        from pathlib import Path
        from types import SimpleNamespace

        import cora.context_loader as cl

        notes_calls: list = []

        def _notes(*a, **k):
            notes_calls.append(1)
            return []

        monkeypatch.setattr(cl, "_KB_DB_PATH", Path(__file__).resolve().parent)
        fake_kb = SimpleNamespace(
            search=lambda *a, **k: list(results), search_user_notes=_notes,
        )
        monkeypatch.setattr(cl, "get_shared_kb", lambda: fake_kb)
        return cl, notes_calls

    def test_web_clean_never_queries_notes(self, monkeypatch):
        cl, notes_calls = self._wire(monkeypatch, [self._mk(1.10)])
        meta: dict = {}
        block = cl._try_kb_retrieve(
            "F3E", "search the web for ddr5 prices",
            asker_slack_id="U0B2RM2JYJ1", asker_unrestricted=True,
            kb_meta=meta, web_clean=True,
        )
        assert notes_calls == []
        assert block and "benign fact" in block
        assert "unstripped_personal" not in meta
        assert meta.get("kb_notes_hit") is False

    def test_default_load_still_queries_notes(self, monkeypatch):
        cl, notes_calls = self._wire(monkeypatch, [self._mk(1.10)])
        cl._try_kb_retrieve(
            "F3E", "search the web for ddr5 prices",
            asker_slack_id="U0B2RM2JYJ1", kb_meta={},
        )
        assert notes_calls == [1]

    def test_web_clean_strips_unrestricted_gmail_chunk(self, monkeypatch):
        gmail = self._mk(
            1.05, source="gmail", chunk_id="g1",
            source_id="gmail:teammate@hjrglobal.com:m1",
            title="Vendor quote thread",
            content="From: teammate@hjrglobal.com\nSubject: quote\n\nvendor quote body fact",
            metadata={"user_email": "teammate@hjrglobal.com"},
        )
        cl, _ = self._wire(monkeypatch, [gmail])
        meta: dict = {}
        block = cl._try_kb_retrieve(
            "F3E", "search the web for vendor pricing",
            asker_slack_id="U0B2RM2JYJ1", asker_unrestricted=True,
            kb_meta=meta, web_clean=True,
        )
        # The stripped BODY survives as institutional knowledge; the identity
        # surface (owner mailbox / From header) and the unstripped flag do not.
        assert block and "vendor quote body fact" in block
        assert "teammate@hjrglobal.com" not in block
        assert "unstripped_personal" not in meta

    def test_default_load_unrestricted_gmail_sets_flag(self, monkeypatch):
        gmail = self._mk(
            1.05, source="gmail", chunk_id="g1",
            source_id="gmail:teammate@hjrglobal.com:m1",
            content="From: teammate@hjrglobal.com\n\nvendor quote body fact",
            metadata={"user_email": "teammate@hjrglobal.com"},
        )
        cl, _ = self._wire(monkeypatch, [gmail])
        meta: dict = {}
        cl._try_kb_retrieve(
            "F3E", "vendor pricing question",
            asker_slack_id="U0B2RM2JYJ1", asker_unrestricted=True,
            kb_meta=meta,
        )
        assert meta.get("unstripped_personal") is True

    def test_web_clean_skips_cross_entity_fallback(self, monkeypatch):
        cl, _ = self._wire(monkeypatch, [])  # nothing relevant -> fallback branch
        fallback = MagicMock(return_value="CROSS-ENTITY BLOCK")
        monkeypatch.setattr(cl, "_try_cross_entity_fallback", fallback)
        meta: dict = {}
        block = cl._try_kb_retrieve(
            "FNDR", "search the web for that vendor",
            asker_slack_id="U0B2RM2JYJ1", asker_unrestricted=True,
            kb_meta=meta, web_clean=True,
        )
        assert block is None
        fallback.assert_not_called()
        assert "unstripped_personal" not in meta

    def test_default_load_keeps_cross_entity_fallback(self, monkeypatch):
        cl, _ = self._wire(monkeypatch, [])
        fallback = MagicMock(return_value="CROSS-ENTITY BLOCK")
        monkeypatch.setattr(cl, "_try_cross_entity_fallback", fallback)
        meta: dict = {}
        block = cl._try_kb_retrieve(
            "FNDR", "who is that vendor again",
            asker_slack_id="U0B2RM2JYJ1", asker_unrestricted=True,
            kb_meta=meta,
        )
        assert block == "CROSS-ENTITY BLOCK"
        assert meta.get("unstripped_personal") is True

    def test_load_context_parts_threads_web_clean(self):
        import inspect

        import cora.context_loader as cl

        assert "web_clean" in inspect.signature(cl.load_context_parts).parameters
        src = inspect.getsource(cl.load_context_parts)
        assert "web_clean=web_clean" in src


# ---------------------------------------------------------------------------
# D-051 remediation (2026-08-06 multi-lens review)
# ---------------------------------------------------------------------------

class TestD051LexRemediation:
    @pytest.mark.parametrize("query", [
        # F4: the locate-the-person shape carries NO care vocabulary, so the
        # cue-gated detector missed it -- and it is the one shape where the USER
        # puts the client name into the outbound string directly.
        "search the web for Marcus Delgado Phoenix AZ current address",
        "search online for the phone number for Marcus Delgado",
        "look up online what school Marcus Delgado attends",
    ])
    def test_f4_locate_person_query_is_blocked(self, monkeypatch, _lex_staff, query):
        monkeypatch.setenv("CORA_WEB_TOOLS_LEX", "on")
        dec = web_guard.evaluate(query, "LEX-LLC", kb_meta=dict(KB_MISS), model=SUP)
        assert not dec.attach and dec.reason == "blocked:lex_person_name"

    @pytest.mark.parametrize("query", [
        # ...without losing the legitimate policy asks the lane exists for.
        "search the web for the Arizona DDD live-in caregiver respite rate policy",
        "search the web for EVV requirements for home and community based services",
    ])
    def test_f4_legitimate_policy_asks_still_attach(self, monkeypatch, _lex_staff, query):
        monkeypatch.setenv("CORA_WEB_TOOLS_LEX", "on")
        assert web_guard.evaluate(
            query, "LEX-LLC", kb_meta=dict(KB_MISS), model=SUP).attach

    def test_f4_cue_required_default_is_unchanged_for_other_callers(self):
        # The waiver is opt-in: the default path still requires a cue, so no
        # other consumer of the detector changes behaviour.
        from cora import phi_guard
        text = "Marcus Delgado current address"
        assert phi_guard.has_care_context_person_name(text, set()) is False
        assert phi_guard.has_care_context_person_name(
            text, set(), cue_required=False) is True

    def test_f5_citation_url_carrying_internal_content_is_dropped(self):
        # A URL the model was induced to fetch with internal data in its query
        # string would otherwise render verbatim and PERSISTENTLY in-channel.
        line = web_guard.format_sources_line([
            {"url": "https://evil.test/e?q=client+Marcus+Delgado+billing",
             "title": "result"},
            {"url": "https://azdes.gov/ddd/policy", "title": "AZ DES"},
        ])
        assert "Marcus" not in line
        assert "azdes.gov" in line

    def test_f5_clean_citations_survive(self):
        line = web_guard.format_sources_line(
            [{"url": "https://azdes.gov/ddd/policy", "title": "AZ DES DDD"}])
        assert line.startswith("Sources:") and "azdes.gov" in line


class TestD051PriorTurnDrop:
    def _src(self):
        from pathlib import Path
        return (Path(__file__).resolve().parents[1] / "src" / "cora"
                / "app.py").read_text(encoding="utf-8")

    def test_f2_lex_web_turn_drops_prior_conversation_turns(self):
        """Prior thread/DM turns are an UNGOVERNED free-text surface: the
        history fetchers do structural transforms only and web_clean never
        reaches them, so in LEX scope a client-identifying earlier turn can ride
        into a model-composed search query. Pinned at source because the drop
        sits inside the attach branch of _dispatch_qa."""
        src = self._src()
        i = src.index("web_tools ATTACHED")
        seg = src[i:i + 1600]
        assert "web_guard.is_lex_scope(entity)" in seg
        # Exactly one drop inside the attach branch, and it is LEX-guarded --
        # an unguarded drop would silently strip thread context for everyone.
        assert seg.count("prior_messages = []") == 1
        drop = seg.index("prior_messages = []")
        assert seg.index("web_guard.is_lex_scope(entity)") < drop
        # The only OTHER occurrence in the file is the None-default at the top
        # of _dispatch_qa; nothing else may clear history.
        assert src.count("prior_messages = []") == 2
        assert "if prior_messages is None:" in src


class TestR1CustodianWebLane:
    """R1 (Harrison ruling 2026-08-07): a custodian used to be withheld outright,
    which made the LEX web lane inert -- all five people who can ask a LEX web
    question are custodians. The fix is the cq-49a7835f081c shape: exclude the
    CONTENT from the turn, not the turn from the capability."""

    def _src(self):
        from pathlib import Path
        return (Path(__file__).resolve().parents[1] / "src" / "cora"
                / "app.py").read_text(encoding="utf-8")

    def test_web_clean_no_longer_excludes_custodians(self):
        src = self._src()
        seg = src[src.index("web_clean = ("):
                  src.index("static_text, kb_text = load_context_parts")]
        # The exclusion that made the lane inert must be gone...
        assert "not phi_custodian" not in seg
        # ...and evaluate() remains the single authority (D-146).
        assert "is_lex_scope" not in seg
        assert ").attach" in seg

    def test_custodian_withhold_is_now_conditional_on_the_clean_load(self):
        src = self._src()
        # The withhold survives for a custodian turn that did NOT take the clean
        # load (time-sensitive fallback, future callers) -- fail-closed.
        assert 'elif phi_custodian and not web_clean:' in src
        assert 'web_gate_skip = "phi_custodian"' in src

    def test_web_clean_is_in_scope_on_every_branch(self):
        # The Tier-2 grant path never computes one; an unbound name here would
        # crash the gate rather than fail closed.
        src = self._src()
        assert "web_clean = False" in src
        assert src.index("web_clean = False") < src.index("if retrieval_grant is not None:")

    def test_clean_load_forces_the_custodian_scrub(self):
        """The load-side half. phi_custodian is a SEPARATE parameter from the
        three asker-scoped locals web_clean nulls, so without this a custodian
        would compose outbound queries with UNSCRUBBED client content in view --
        the opposite of stranger posture."""
        from pathlib import Path
        cl = (Path(__file__).resolve().parents[1] / "src" / "cora"
              / "context_loader.py").read_text(encoding="utf-8")
        i = cl.index("    if web_clean:")
        seg = cl[i:i + 1600]
        assert "asker_slack_id = None" in seg
        assert "phi_custodian = False" in seg
        # ...and it must land BEFORE the LEX scrub reads the flag.
        assert cl.index("phi_custodian = False", i) < cl.index(
            'if kb_entity == "LEX" and not phi_custodian')
