"""Tests for the daily channel synthesis (channel_synthesis.py).

Slice 0: shared primitives -- entity-prefix task filter, TIER_1 allowlist +
deliver_to_channel (egress + fail-soft + fail-closed tier gate), and the
standalone-script (D-047) + source-post-site guards.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import asana_filters as af
from cora import channel_synthesis as cs
from cora import strategy_memo as sm
from cora.reply_formatter import normalize_slack_bold
from cora.slack_egress import sanitize_text


# ---------------------------------------------------------------------------
# Entity-prefix task filtering (asana_filters.task_belongs_to_entity)
# ---------------------------------------------------------------------------

class TestEntityPrefixFilter:
    def _task(self, *project_names, memberships=None):
        t = {"projects": [{"name": n} for n in project_names]}
        if memberships is not None:
            t["memberships"] = [{"project": {"name": n}} for n in memberships]
        return t

    def test_f3e_matches_bracket_f3e(self):
        assert af.task_belongs_to_entity(self._task("[F3E] Sales Pipeline"), "F3E")

    def test_f3e_matches_brand_line(self):
        assert af.task_belongs_to_entity(self._task("[F3 Pure] Launch"), "F3E")

    def test_f3c_separated_from_f3e(self):
        """F3C is a SEPARATE entity here; an [F3C] task must NOT match F3E and an
        [F3E] task must NOT match F3C (no cross-entity bleed between the two)."""
        f3c_task = self._task("[F3C] Nonprofit gala")
        assert af.task_belongs_to_entity(f3c_task, "F3C")
        assert not af.task_belongs_to_entity(f3c_task, "F3E")
        f3e_task = self._task("[F3E] Retail")
        assert not af.task_belongs_to_entity(f3e_task, "F3C")

    def test_f3_community_prefix_is_f3c_not_f3e(self):
        t = self._task("[F3 Community] Education foundation")
        assert af.task_belongs_to_entity(t, "F3C")
        assert not af.task_belongs_to_entity(t, "F3E")

    def test_lex_union_prefixes(self):
        for name in ("[LEX] Ops", "[LEX-LLC] DDD", "[LTS] Thing",
                     "[LBHS] COPA", "[LLA] X", "[LLC] Admin"):
            assert af.task_belongs_to_entity(self._task(name), "LEX"), name

    def test_case_insensitive(self):
        assert af.task_belongs_to_entity(self._task("[osn] gilbert"), "OSN")
        assert af.task_belongs_to_entity(self._task("[OsN] Gilbert"), "OSN")

    def test_reads_memberships_project_names(self):
        t = {"projects": [], "memberships": [{"project": {"name": "[HJRP] Leases"}}]}
        assert af.task_belongs_to_entity(t, "HJRP")

    def test_hjrprod_subcodes(self):
        for name in ("[HJRPROD] X", "[POD] Episode", "[FF] Falling Forward"):
            assert af.task_belongs_to_entity(self._task(name), "HJRPROD"), name

    def test_unknown_entity_is_false(self):
        assert not af.task_belongs_to_entity(self._task("[F3E] X"), "NOPE")

    def test_no_projects_is_false(self):
        assert not af.task_belongs_to_entity({"projects": []}, "F3E")


# ---------------------------------------------------------------------------
# TIER_1 allowlist + deliver_to_channel
# ---------------------------------------------------------------------------

class _FakeClient:
    """Records chat_postMessage calls; never opens a DM."""
    last: dict = {}

    def __init__(self, token):
        _FakeClient.last = {"token": token}

    def chat_postMessage(self, channel, text):
        _FakeClient.last["channel"] = channel
        _FakeClient.last["text"] = text
        return {"ok": True}


class _BoomClient:
    def __init__(self, token):
        pass

    def chat_postMessage(self, channel, text):
        raise RuntimeError("slack down")


class TestTierAllowlist:
    def test_all_scope_channels_are_tier1(self):
        for scope, cid in cs.SCOPE_CHANNELS.items():
            assert cs._assert_tier1(cid), scope

    def test_smoke_channel_is_tier1(self):
        assert cs._assert_tier1(cs.SMOKE_CHANNEL)

    def test_founder_operations_is_allowlisted(self):
        # D1: the name classifier mis-classifies #founder-operations as TIER_3;
        # the id allowlist must still accept it (the portfolio post's target).
        assert cs._assert_tier1("C0BCUBUDHAR")

    def test_random_channel_refused(self):
        assert not cs._assert_tier1("C0DEADBEEF")
        assert not cs._assert_tier1("")

    def test_channel_ids_pinned_to_reference(self):
        """D-051 #4/#12: pin the 9 ids to literal build-spec values (NOT derived
        from SCOPE_CHANNELS) so a future typo/swap to a public channel fails here."""
        expected = {
            "portfolio": "C0BCUBUDHAR", "f3e": "C0B4KRQT3LY",
            "hjrp": "C0B3A3W2A3H", "osn": "C0B3TCEF4KT", "lex": "C0B3A3U7WS3",
            "bdm": "C0B3PF5QK9C", "ufl": "C0B3N5YG1SR", "hjrprod": "C0BFCM2TV55",
            "f3c": "C0BFCMB2JFR",
        }
        assert cs.SCOPE_CHANNELS == expected
        assert cs.SMOKE_CHANNEL == "C0B4B0URRQS"


class TestDeliverToChannel:
    def test_refuses_non_tier1_and_posts_nothing(self, monkeypatch):
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        _FakeClient.last = {}
        ok = cs.deliver_to_channel("C0NOTALLOWED", "portfolio cash $1,000,000")
        assert ok is False
        assert "channel" not in _FakeClient.last  # never attempted a post

    def test_posts_to_allowlisted_channel(self, monkeypatch):
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        _FakeClient.last = {}
        ok = cs.deliver_to_channel(cs.SCOPE_CHANNELS["portfolio"], "hello team")
        assert ok is True
        assert _FakeClient.last["channel"] == cs.SCOPE_CHANNELS["portfolio"]
        assert "hello team" in _FakeClient.last["text"]

    def test_normalizes_bold_and_sanitizes(self, monkeypatch):
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        _FakeClient.last = {}
        body = ("**Cash** update: see "
                "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view")
        ok = cs.deliver_to_channel(cs.SCOPE_CHANNELS["f3e"], body)
        assert ok is True
        expected = sanitize_text(normalize_slack_bold(
            cs._scrub_visibility_cpa(body)))[:cs._MAX_SLACK_CHARS]
        assert _FakeClient.last["text"] == expected
        # bold was normalized (** -> *) and the raw drive URL did not survive verbatim
        assert "**Cash**" not in _FakeClient.last["text"]
        assert "*Cash*" in _FakeClient.last["text"]

    def test_no_token_fails_soft(self, monkeypatch):
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        assert cs.deliver_to_channel(cs.SCOPE_CHANNELS["osn"], "x") is False

    def test_empty_body_fails_soft(self, monkeypatch):
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        assert cs.deliver_to_channel(cs.SCOPE_CHANNELS["osn"], "") is False

    def test_post_exception_fails_soft(self, monkeypatch):
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _BoomClient)
        assert cs.deliver_to_channel(cs.SCOPE_CHANNELS["bdm"], "body") is False

    def test_visibility_cpa_name_neutralized_at_delivery(self, monkeypatch):
        # Covers synthesis AND fallback for every scope: a Visibility-CPA name in
        # a decision owner must not reach a team-facing channel post.
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        _FakeClient.last = {}
        cs.deliver_to_channel(cs.SCOPE_CHANNELS["lex"],
                              "Needs you: Andrew Stubbs or Justin to follow up.")
        assert "Andrew Stubbs" not in _FakeClient.last["text"]
        assert "external accounting" in _FakeClient.last["text"]
        assert "Justin" in _FakeClient.last["text"]     # the non-CPA owner survives


# ---------------------------------------------------------------------------
# Standalone-script (D-047) + source-post-site guards
# ---------------------------------------------------------------------------

class TestNoBotProcessImport:
    def test_import_does_not_pull_bot_modules(self):
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "import cora.channel_synthesis; "
            "bad = [m for m in ('cora.app', 'cora.tool_dispatch', 'cora.claude_client')"
            " if m in sys.modules]; "
            "assert not bad, f'bot-process modules imported: {bad}'"
        ) % str(_REPO_ROOT / "src")
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr


def _pgathered(date_str="2026-07-07", cash=None):
    cash = cash or {"F3E": 100_000.0, "OSN": 50_000.0, "LEX": 25_000.0}
    entities = {}
    for code, label in sm.CASH_ENTITIES:
        if code in cash:
            entities[code] = {"label": label, "closing_balance": cash[code],
                              "actual": -1.0, "forecast": -1.0}
        else:
            entities[code] = {"label": label, "error": True}
    return {
        "date": date_str,
        "cash": {"ok": True, "week_label": "Week of 7/6/2026", "entities": entities},
        "pipeline": {"ok": True, "pipelines": {
            "f3e_retail": {"label": "F3E Retail", "open_count": 3,
                           "open_amount": 30_000.0,
                           "stages": {"Proposal": {"count": 3, "amount": 30_000.0}},
                           "aging": []},
            "default": {"label": "UFL/OSN/BDM (default)", "open_count": 1,
                        "open_amount": 10_000.0,
                        "stages": {"Outreach": {"count": 1, "amount": 10_000.0}},
                        "aging": []}}},
        "decisions": {"ok": True, "decisions": [
            {"topic": "OIC pre-qualifier", "entity": "FNDR", "severity": "P0",
             "age_days": 20, "owner": "Harrison"}]},
        "deadlines": {"ok": True, "due_14d": 4, "overdue": 1,
                      "overdue_by_owner": {"Hannah Grant": 1},
                      "items": [{"name": "Send deck", "owner": "Hannah Grant",
                                 "due_on": "2026-07-09", "overdue": False}],
                      "aggregate_only": 2, "users_failed": 0},
        "efficiency": {"ok": True, "approved_recent": [], "approved_total": 3,
                       "pending": []},
        "kb_activity": {"ok": True, "by_entity": {"F3E": 100, "LEX": 40}},
        "health": {"ok": True, "line": "Cora healthy (heartbeat 30s ago)",
                   "age_seconds": 30},
    }


class TestPortfolioSynthesis:
    def test_no_api_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert cs.synthesize_channel_portfolio("facts") is None

    def test_api_error_returns_none(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        import anthropic

        class _Boom:
            def __init__(self, api_key):
                pass

            class messages:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    raise RuntimeError("api down")
        monkeypatch.setattr(anthropic, "Anthropic", _Boom)
        assert cs.synthesize_channel_portfolio("facts") is None

    def _fake_anthropic(self, monkeypatch, reply_text):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        import anthropic

        class _Resp:
            content = [type("T", (), {"text": reply_text})()]

        class _Client:
            def __init__(self, api_key):
                pass

            class messages:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    return _Resp()
        monkeypatch.setattr(anthropic, "Anthropic", _Client)

    def test_clinical_phi_output_dropped(self, monkeypatch):
        self._fake_anthropic(monkeypatch,
                             "Portfolio pulse. A member was diagnosed with autism.")
        assert cs.synthesize_channel_portfolio("facts") is None

    def test_aggregate_lex_vocab_not_false_blocked(self, monkeypatch):
        """The backstop is is_clinical_phi, NOT the broad is_phi_risk: a legit
        holdco line naming AHCCCS / Medicaid / assessments must survive."""
        text = ("Portfolio pulse: cash steady. Lexington aggregate: AHCCCS "
                "revalidation on track, Medicaid billing current, 12 assessments "
                "completed this week.")
        self._fake_anthropic(monkeypatch, text)
        assert cs.synthesize_channel_portfolio("facts") == text

    def test_prompt_carries_operational_rules(self):
        p = cs._PORTFOLIO_PROMPT
        assert "OPERATIONAL" in p
        assert "restructuring" in p
        assert "Never invent numbers" in p
        assert "aggregate" in p
        assert "weekly memo" in p


class TestRunSynthesis:
    def _env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYNTHESIS_SNAPSHOT_DIR", str(tmp_path / "syn"))

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        self._env(tmp_path, monkeypatch)
        spy = {"called": False}

        def deliver(body):
            spy["called"] = True
            return True
        out = cs.run_synthesis(
            "portfolio", gather_fn=_pgathered,
            synth_fn=lambda f: "BODY", deliver_fn=deliver,
            dry_run=True, today=date(2026, 7, 7))
        # Body carries the deterministic dated header (2026-07-07 is a Tuesday).
        assert out["body"] == "*Tuesday, 2026-07-07*\n\nBODY"
        assert out["delivered"] is False
        assert spy["called"] is False
        assert not (tmp_path / "syn").exists()

    def test_real_run_writes_scope_snapshot_and_delivers(self, tmp_path, monkeypatch):
        self._env(tmp_path, monkeypatch)
        out = cs.run_synthesis(
            "portfolio", gather_fn=_pgathered,
            synth_fn=lambda f: "BODY", deliver_fn=lambda b: True,
            dry_run=False, today=date(2026, 7, 7))
        assert out["delivered"] is True
        assert (tmp_path / "syn" / "portfolio" / "2026-07-07.json").exists()

    def test_fallback_when_synth_none(self, tmp_path, monkeypatch):
        self._env(tmp_path, monkeypatch)
        out = cs.run_synthesis(
            "portfolio", gather_fn=_pgathered,
            synth_fn=lambda f: None, deliver_fn=lambda b: True,
            dry_run=True, today=date(2026, 7, 7))
        assert out["synthesized"] is False
        assert "SYNTHESIS UNAVAILABLE" in out["body"]

    def test_first_run_flag(self, tmp_path, monkeypatch):
        self._env(tmp_path, monkeypatch)
        out = cs.run_synthesis(
            "portfolio", gather_fn=_pgathered,
            synth_fn=lambda f: "BODY", deliver_fn=lambda b: True,
            dry_run=True, today=date(2026, 7, 7))
        assert out["first_run"] is True

    def test_scope_snapshots_are_isolated(self, tmp_path, monkeypatch):
        """Two scopes writing the same date must not collide."""
        self._env(tmp_path, monkeypatch)
        cs.run_synthesis("portfolio", gather_fn=_pgathered,
                         synth_fn=lambda f: "B", deliver_fn=lambda b: True,
                         dry_run=False, today=date(2026, 7, 7))
        cs.run_synthesis("f3e", gather_fn=_pgathered,
                         synth_fn=lambda f: "B", deliver_fn=lambda b: True,
                         dry_run=False, today=date(2026, 7, 7))
        assert (tmp_path / "syn" / "portfolio" / "2026-07-07.json").exists()
        assert (tmp_path / "syn" / "f3e" / "2026-07-07.json").exists()

    def test_date_header_format(self):
        # Deterministic: weekday + ISO, bold. 2026-07-08 is a Wednesday.
        assert cs._date_header(date(2026, 7, 8)) == "*Wednesday, 2026-07-08*"
        assert cs._date_header(date(2026, 7, 7)) == "*Tuesday, 2026-07-07*"

    def test_prepends_deterministic_date_header(self, tmp_path, monkeypatch):
        """The post date is code-generated (weekday + ISO), never LLM-written, so it
        cannot drift (prior bug: a LEX post read 'Wed Jul 9' on 2026-07-08)."""
        self._env(tmp_path, monkeypatch)
        out = cs.run_synthesis(
            "portfolio", gather_fn=_pgathered,
            synth_fn=lambda f: "MODEL BODY, NO DATE OF ITS OWN",
            deliver_fn=lambda b: True,
            dry_run=True, today=date(2026, 7, 8))  # a Wednesday
        assert out["body"].startswith("*Wednesday, 2026-07-08*\n\n")
        assert out["body"].endswith("MODEL BODY, NO DATE OF ITS OWN")
        # Exactly one date line, at the very top -- no drift, no duplicate.
        assert out["body"].count("2026-07-08") == 1

    def test_date_header_applies_to_fallback(self, tmp_path, monkeypatch):
        """The header is prepended to the deterministic fallback too, not only synth."""
        self._env(tmp_path, monkeypatch)
        out = cs.run_synthesis(
            "portfolio", gather_fn=_pgathered,
            synth_fn=lambda f: None, deliver_fn=lambda b: True,
            dry_run=True, today=date(2026, 7, 8))
        assert out["body"].startswith("*Wednesday, 2026-07-08*\n\n")
        assert "SYNTHESIS UNAVAILABLE" in out["body"]

    def test_prompts_forbid_model_date_line(self):
        """All three synthesis prompts forbid the model from writing its own date
        line -- the authoritative date is code-prepended."""
        for p in (cs._PORTFOLIO_PROMPT, cs._ENTITY_PROMPT, cs._LEX_PROMPT):
            assert "Do NOT write your own date" in p


class TestRunPortfolioWiring:
    def test_defaults_to_founder_operations(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYNTHESIS_SNAPSHOT_DIR", str(tmp_path / "syn"))
        monkeypatch.setattr(sm, "gather_all", lambda today=None: _pgathered())
        monkeypatch.setattr(cs, "synthesize_channel_portfolio", lambda f: "PORTFOLIO BODY")
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        _FakeClient.last = {}
        out = cs.run_portfolio(dry_run=False, today=date(2026, 7, 7))
        assert out["delivered"] is True
        assert _FakeClient.last["channel"] == cs.SCOPE_CHANNELS["portfolio"]
        assert "PORTFOLIO BODY" in _FakeClient.last["text"]

    def test_channel_override_smoke(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYNTHESIS_SNAPSHOT_DIR", str(tmp_path / "syn"))
        monkeypatch.setattr(sm, "gather_all", lambda today=None: _pgathered())
        monkeypatch.setattr(cs, "synthesize_channel_portfolio", lambda f: "BODY")
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        _FakeClient.last = {}
        cs.run_portfolio(dry_run=False, today=date(2026, 7, 7), channel=cs.SMOKE_CHANNEL)
        assert _FakeClient.last["channel"] == cs.SMOKE_CHANNEL


class TestRunnerScript:
    def test_portfolio_runner_exists_and_wired(self):
        src = (_REPO_ROOT / "scripts" / "run_portfolio_synthesis.py").read_text(
            encoding="utf-8")
        assert "--dry-run" in src
        assert "run_portfolio" in src
        assert "override=True" in src            # D-021/Doctrine-2 load_dotenv
        assert 'reconfigure(encoding="utf-8"' in src
        assert "cora.tool_dispatch" not in src   # D-047
        assert "cora.app" not in src

    def test_entity_runner_exists_and_wired(self):
        src = (_REPO_ROOT / "scripts" / "run_entity_synthesis.py").read_text(
            encoding="utf-8")
        assert "--entity" in src
        assert "run_entity" in src
        assert "override=True" in src
        assert "cora.tool_dispatch" not in src   # D-047
        assert "cora.app" not in src
        assert '"lex"' in src                    # LEX now a supported entity


def _install_fake_anthropic(monkeypatch, reply_text):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    import anthropic

    class _Resp:
        content = [type("T", (), {"text": reply_text})()]

    class _Client:
        def __init__(self, api_key):
            pass

        class messages:  # noqa: N801
            @staticmethod
            def create(**kwargs):
                return _Resp()
    monkeypatch.setattr(anthropic, "Anthropic", _Client)


def _hs_deal(name, stage_id, amount, idle_days, now):
    import datetime as _dt
    modified = now - idle_days * 86400
    iso = _dt.datetime.fromtimestamp(modified, _dt.timezone.utc).isoformat()
    return {"properties": {"dealname": name, "dealstage": stage_id,
                           "amount": str(amount), "hs_lastmodifieddate": iso}}


class TestEntityPipeline:
    def test_omitted_for_non_pipeline_entities(self):
        for e in ("HJRP", "HJRPROD", "F3C"):
            out = cs.gather_pipeline_for_entity(e)
            assert out == {"ok": False, "omitted": True}, e

    def test_f3e_summary(self):
        import time as _t
        now = _t.time()
        deals = [_hs_deal("Sprouts", "s1", 5000, 30, now),
                 _hs_deal("GNC", "s1", 2000, 2, now)]
        out = cs.gather_pipeline_for_entity(
            "F3E", fetch_fn=lambda: deals,
            stage_names={"s1": "Proposal"}, now=now)
        assert out["ok"] is True
        assert out["open_count"] == 2
        assert out["open_amount"] == 7000.0
        assert out["stages"]["Proposal"]["count"] == 2
        assert len(out["aging"]) == 1
        assert out["aging"][0]["name"] == "Sprouts"

    def test_fetch_failure_degrades(self):
        def boom():
            raise RuntimeError("hubspot down")
        out = cs.gather_pipeline_for_entity("OSN", fetch_fn=boom, stage_names={})
        assert out["ok"] is False
        assert out.get("error") is True


class TestEntityCash:
    def test_f3c_cash_omitted_no_fetch(self):
        out = cs.gather_cash_for_entity("F3C")
        assert out["ok"] is False and out["omitted"] is True
        assert "F3 Energy" in out["note"]

    def test_entity_cash_fetched(self, monkeypatch):
        import cora.connectors.gsheets_financials as gf

        class _S:
            week_label = "Week of 7/6"
            closing_balance = 62_427.0
        monkeypatch.setattr(gf, "get_cashflow", lambda tab_name=None: _S())
        out = cs.gather_cash_for_entity("OSN")
        assert out["ok"] is True
        assert out["closing_balance"] == 62_427.0

    def test_entity_cash_failure_degrades(self, monkeypatch):
        import cora.connectors.gsheets_financials as gf

        def boom(tab_name=None):
            raise gf.GsheetsConnectorError("missing tab")
        monkeypatch.setattr(gf, "get_cashflow", boom)
        out = cs.gather_cash_for_entity("BDM")
        assert out["ok"] is False and out.get("error") is True


class TestEntityDeadlineRadar:
    def _map(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STRATEGY_ASANA_MAP_PATH", str(tmp_path / "map.yaml"))
        (tmp_path / "map.yaml").write_text(
            "users:\n"
            "  - slack_user_id: U1\n    asana_user_gid: '111'\n"
            "    display_name: Alex Cordova\n"
            "  - slack_user_id: U2\n    asana_user_gid: '222'\n"
            "    display_name: Matt Petrovich\n", encoding="utf-8")

    def test_filters_to_entity_only(self, tmp_path, monkeypatch):
        self._map(tmp_path, monkeypatch)
        tasks = {
            "111": [
                {"name": "F3E deck", "due_on": "2026-07-09", "completed": False,
                 "projects": [{"name": "[F3E] Sales"}]},
                {"name": "OSN thing", "due_on": "2026-07-09", "completed": False,
                 "projects": [{"name": "[OSN] Ops"}]},  # foreign entity -> excluded
            ],
            "222": [
                {"name": "F3E overdue", "due_on": "2026-07-01", "completed": False,
                 "projects": [{"name": "[F3E] Retail"}]},
            ],
        }
        out = cs.gather_deadline_radar_for_entity(
            "F3E", today=date(2026, 7, 7),
            get_tasks_fn=lambda gid: tasks[gid])
        names = [i["name"] for i in out["items"]]
        assert "F3E deck" in names
        assert "F3E overdue" in names
        assert "OSN thing" not in names       # cross-entity excluded
        assert out["due_14d"] == 1
        assert out["overdue"] == 1

    def test_phi_name_counted_not_itemized(self, tmp_path, monkeypatch):
        self._map(tmp_path, monkeypatch)
        tasks = {"111": [
            {"name": "Client patient intake form", "due_on": "2026-07-09",
             "completed": False, "projects": [{"name": "[F3E] X"}]},
        ], "222": []}
        out = cs.gather_deadline_radar_for_entity(
            "F3E", today=date(2026, 7, 7), get_tasks_fn=lambda gid: tasks[gid])
        assert out["items"] == []            # PHI name never itemized
        assert out["due_14d"] == 1           # still counted
        assert out["redacted"] == 1

    def test_itemize_false_counts_only(self, tmp_path, monkeypatch):
        self._map(tmp_path, monkeypatch)
        tasks = {"111": [
            {"name": "[LEX] task name", "due_on": "2026-07-09", "completed": False,
             "projects": [{"name": "[LEX-LLC] Ops"}]},
        ], "222": []}
        out = cs.gather_deadline_radar_for_entity(
            "LEX", today=date(2026, 7, 7), itemize=False,
            get_tasks_fn=lambda gid: tasks[gid])
        assert out["items"] == []
        assert out["due_14d"] == 1
        assert out["redacted"] == 1


class TestEntityDecisions:
    def test_token_substring_filter(self, monkeypatch):
        monkeypatch.setattr(sm, "gather_stalled_decisions", lambda today=None: {
            "ok": True, "decisions": [
                {"topic": "F3E expansion", "entity": "F3E, HJRPROD",
                 "severity": "P0", "age_days": 5, "owner": "H"},
                {"topic": "OSN cost", "entity": "OSN", "severity": "P0",
                 "age_days": 5, "owner": "H"},
                {"topic": "Podcast slot", "entity": "F3E / POD", "severity": "P1",
                 "age_days": 5, "owner": "H"},
            ]})
        f3e = cs.gather_decisions_for_entity("F3E")
        assert {d["topic"] for d in f3e["decisions"]} == {"F3E expansion", "Podcast slot"}
        hjrprod = cs.gather_decisions_for_entity("HJRPROD")
        # matches via alias token "POD" and the "HJRPROD" tag
        assert {d["topic"] for d in hjrprod["decisions"]} == {"F3E expansion", "Podcast slot"}
        osn = cs.gather_decisions_for_entity("OSN")
        assert {d["topic"] for d in osn["decisions"]} == {"OSN cost"}


class TestEntityKb:
    def test_sums_entity_and_subentities_not_siblings(self, monkeypatch):
        monkeypatch.setattr(sm, "gather_kb_activity", lambda: {
            "ok": True, "by_entity": {"LEX": 100, "LEX-LLC": 40, "HJRP": 20,
                                      "HJRPROD": 999, "F3E": 5}})
        assert cs.gather_kb_for_entity("LEX")["count"] == 140      # LEX + LEX-LLC
        assert cs.gather_kb_for_entity("HJRP")["count"] == 20      # NOT HJRPROD
        assert cs.gather_kb_for_entity("HJRPROD")["count"] == 999


class TestEntityDeltas:
    def test_first_run(self):
        assert cs.compute_entity_deltas("OSN", {"cash": {"ok": True,
                "closing_balance": 1.0}}, []) == {"first_run": True}

    def test_cash_delta_and_streak(self):
        def snap(bal):
            return {"cash": {"ok": True, "closing_balance": bal},
                    "decisions": {"decisions": []}}
        priors = [snap(60_000.0), snap(70_000.0)]  # newest first
        cur = snap(50_000.0)
        out = cs.compute_entity_deltas("OSN", cur, priors)
        assert out["cash"]["delta"] == -10_000.0
        assert out["cash"]["decline_streak"] == 2

    def test_pipeline_delta(self):
        cur = {"cash": {"ok": False}, "decisions": {"decisions": []},
               "pipeline": {"ok": True, "open_count": 5, "open_amount": 20_000.0,
                            "stages": {"Proposal": {"count": 3}}}}
        prev = {"cash": {"ok": False}, "decisions": {"decisions": []},
                "pipeline": {"ok": True, "open_count": 3, "open_amount": 12_000.0,
                             "stages": {"Proposal": {"count": 1}}}}
        out = cs.compute_entity_deltas("F3E", cur, [prev])
        assert out["pipeline"]["open_count_delta"] == 2
        assert out["pipeline"]["open_amount_delta"] == 8_000.0
        assert out["pipeline"]["stage_moves"] == {"Proposal": 2}


class TestEntityFacts:
    def _g(self, entity, **over):
        base = {"entity": entity, "date": "2026-07-07",
                "cash": {"ok": True, "label": cs._ENTITY_LABELS[entity],
                         "closing_balance": 62_427.0},
                "pipeline": {"ok": False, "omitted": True},
                "decisions": {"ok": True, "decisions": []},
                "deadlines": {"ok": True, "due_14d": 2, "overdue": 1,
                              "overdue_by_owner": {"Matt": 1},
                              "items": [{"name": "X", "owner": "Matt",
                                         "due_on": "2026-07-08", "overdue": False}]},
                "kb_activity": {"ok": True, "count": 42},
                "health": {"ok": True, "line": "Cora healthy (heartbeat 5s ago)"}}
        base.update(over)
        return base

    def test_f3c_cash_omit_note(self):
        g = self._g("F3C", cash={"ok": False, "omitted": True,
                                 "note": "Cash is tracked under F3 Energy (shared entity ledger)."})
        facts = cs.build_entity_facts_text("F3C", g, {"first_run": True})
        assert "tracked under F3 Energy" in facts
        assert "== PIPELINE" not in facts     # F3C omits pipeline

    def test_hjrp_pipeline_section_absent(self):
        facts = cs.build_entity_facts_text("HJRP", self._g("HJRP"), {"first_run": True})
        assert "== PIPELINE" not in facts

    def test_f3e_includes_ecom_and_pipeline(self):
        g = self._g("F3E",
                    pipeline={"ok": True, "open_count": 3, "open_amount": 30_000.0,
                              "stages": {"Proposal": {"count": 3, "amount": 30_000.0}},
                              "aging": []},
                    ecom={"ok": True, "lines": ["- DTC: $10,000 net (7d)",
                                                "- Subscriptions: 500 active"]})
        facts = cs.build_entity_facts_text("F3E", g, {"first_run": True})
        assert "== PIPELINE" in facts
        assert "== ECOM" in facts
        assert "DTC" in facts


class TestEntitySynth:
    def test_no_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert cs.synthesize_channel_entity("OSN", "facts") is None

    def test_cross_entity_mention_logged_not_dropped(self, monkeypatch, caplog):
        # A foreign-entity keyword is a legitimate-collaborator signal, not a data
        # leak (the entity-scoped gather is the firewall). Keep the post, log it --
        # dropping to fallback would keep the same mention and only lose quality.
        import logging
        text = "*Moved* Coordinating with Big D Media on the can graphic."
        _install_fake_anthropic(monkeypatch, text)
        with caplog.at_level(logging.INFO, logger="cora.channel_synthesis"):
            out = cs.synthesize_channel_entity("F3E", "facts")
        assert out == text
        assert any("references another entity" in r.message for r in caplog.records)

    def test_entity_scoped_gather_is_the_firewall(self, tmp_path, monkeypatch):
        """The structural firewall: a FOREIGN entity's task never enters the
        entity facts, so a foreign figure can never be synthesized."""
        monkeypatch.setenv("STRATEGY_ASANA_MAP_PATH", str(tmp_path / "m.yaml"))
        (tmp_path / "m.yaml").write_text(
            "users:\n  - slack_user_id: U1\n    asana_user_gid: '111'\n"
            "    display_name: X\n", encoding="utf-8")
        tasks = {"111": [
            {"name": "OSN secret deal $9M", "due_on": "2026-07-09",
             "completed": False, "projects": [{"name": "[OSN] Ops"}]}]}
        out = cs.gather_deadline_radar_for_entity(
            "F3E", today=date(2026, 7, 7), get_tasks_fn=lambda gid: tasks["111"])
        assert out["items"] == []            # OSN task never reaches F3E facts
        assert out["due_14d"] == 0

    def test_clean_entity_output_survives(self, monkeypatch):
        text = "*Moved* DTC net up. *Watch* aging deal in retail pipeline."
        _install_fake_anthropic(monkeypatch, text)
        assert cs.synthesize_channel_entity("F3E", "facts") == text

    def test_prompt_source_opaque_and_scope_note(self):
        assert "SOURCE-OPAQUE" in cs._ENTITY_PROMPT
        assert "STRICTLY within" in cs._ENTITY_PROMPT
        assert "PAUSED" in cs._ENTITY_SCOPE_NOTE["UFL"]
        assert "nonprofit" in cs._ENTITY_SCOPE_NOTE["F3C"]


class TestEcomFold:
    def test_fail_soft_all_sections(self, monkeypatch):
        # Force every connector call to raise -> every line degrades, never raises.
        import cora.connectors.shopify_client as shopify
        import cora.connectors.polar_client as polar
        import cora.tools.asana_client as asana

        def boom(*a, **k):
            raise RuntimeError("down")
        monkeypatch.setattr(shopify, "get_sales_pulse", boom)
        monkeypatch.setattr(shopify, "get_inventory_status", boom)
        monkeypatch.setattr(polar, "generate_report", boom)
        monkeypatch.setattr(asana, "get_project_tasks", boom)
        out = cs.gather_f3e_ecom(today=date(2026, 7, 7))
        assert out["ok"] is True
        assert len(out["lines"]) == 5
        assert any("not available" in ln or "not connected" in ln for ln in out["lines"])


class TestRunEntityWiring:
    def test_posts_to_entity_channel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYNTHESIS_SNAPSHOT_DIR", str(tmp_path / "syn"))
        monkeypatch.setattr(cs, "gather_all_for_entity",
                            lambda entity, today=None: {"entity": entity,
                                                        "date": "2026-07-07",
                                                        "cash": {"ok": False}})
        monkeypatch.setattr(cs, "compute_entity_deltas",
                            lambda e, c, p: {"first_run": True})
        monkeypatch.setattr(cs, "build_entity_facts_text", lambda e, g, d: "FACTS")
        monkeypatch.setattr(cs, "synthesize_channel_entity",
                            lambda e, facts: "OSN BODY")
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        _FakeClient.last = {}
        out = cs.run_entity("osn", dry_run=False, today=date(2026, 7, 7))
        assert out["delivered"] is True
        assert _FakeClient.last["channel"] == cs.SCOPE_CHANNELS["osn"]
        assert "OSN BODY" in _FakeClient.last["text"]
        assert (tmp_path / "syn" / "osn" / "2026-07-07.json").exists()

    def test_unknown_entity_raises(self):
        import pytest
        with pytest.raises(ValueError):
            cs.run_entity("nope", dry_run=True)

    def test_run_entity_lex_posts_to_lex_channel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYNTHESIS_SNAPSHOT_DIR", str(tmp_path / "syn"))
        monkeypatch.setattr(cs, "gather_all_for_entity",
                            lambda entity, today=None: {"entity": "LEX",
                                                        "date": "2026-07-07",
                                                        "cash": {"ok": False}})
        monkeypatch.setattr(cs, "compute_entity_deltas",
                            lambda e, c, p: {"first_run": True})
        monkeypatch.setattr(cs, "build_entity_facts_text", lambda e, g, d: "FACTS")
        monkeypatch.setattr(cs, "synthesize_channel_entity",
                            lambda e, facts: "LEX BODY")
        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        _FakeClient.last = {}
        cs.run_entity("lex", dry_run=False, today=date(2026, 7, 7))
        assert _FakeClient.last["channel"] == cs.SCOPE_CHANNELS["lex"]


class TestLexSynthesis:
    def test_lex_gather_is_aggregate(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(cs, "gather_cash_for_entity",
                            lambda e: {"ok": True, "closing_balance": 15_000.0,
                                       "label": "Lexington Services"})

        def fake_radar(entity, *, today=None, get_tasks_fn=None, itemize=True):
            captured["itemize"] = itemize
            captured["entity"] = entity
            return {"ok": True, "due_14d": 3, "overdue": 1,
                    "overdue_by_owner": {"Shaun Hawkins": 1}, "items": [],
                    "redacted": 5}
        monkeypatch.setattr(cs, "gather_deadline_radar_for_entity", fake_radar)
        monkeypatch.setattr(cs, "gather_decisions_for_entity",
                            lambda e, today=None: {"ok": True, "decisions": []})
        monkeypatch.setattr(cs, "gather_kb_for_entity",
                            lambda e: {"ok": True, "count": 10})
        monkeypatch.setattr(sm, "gather_health",
                            lambda: {"ok": True, "line": "healthy"})
        g = cs.gather_all_for_entity("LEX", today=date(2026, 7, 7))
        assert g["pipeline"] == {"ok": False, "omitted": True}
        assert captured["itemize"] is False       # LEX deadlines never itemized
        assert captured["entity"] == "LEX"
        assert g["deadlines"]["items"] == []
        assert "ecom" not in g

    def test_clinical_dx_output_dropped(self, monkeypatch):
        _install_fake_anthropic(
            monkeypatch, "*Moved* A client was diagnosed with autism this week.")
        assert cs.synthesize_channel_lex("facts") is None

    def test_medication_output_dropped(self, monkeypatch):
        _install_fake_anthropic(
            monkeypatch, "*Watch* One client is now stable on risperidone.")
        assert cs.synthesize_channel_lex("facts") is None

    def test_governed_client_name_scrubbed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STRATEGY_ASANA_MAP_PATH", str(tmp_path / "m.yaml"))
        (tmp_path / "m.yaml").write_text(
            "users:\n  - slack_user_id: U1\n    asana_user_gid: '1'\n"
            "    display_name: Shaun Hawkins\n", encoding="utf-8")
        _install_fake_anthropic(
            monkeypatch,
            "*Needs you* client Maria Gonzalez needs a service renewal.")
        out = cs.synthesize_channel_lex("facts")
        assert out is not None
        assert "Maria Gonzalez" not in out
        assert "[name redacted]" in out

    def test_aggregate_vocab_not_false_blocked_headers_intact(self, tmp_path, monkeypatch):
        """The tuned gate must NOT false-block a legit aggregate post, and must NOT
        corrupt the *Moved*/*Watch* headers (why scrub_lex_phi, not the Title-case
        cue scrub)."""
        monkeypatch.setenv("STRATEGY_ASANA_MAP_PATH", str(tmp_path / "m.yaml"))
        (tmp_path / "m.yaml").write_text("users: []\n", encoding="utf-8")
        text = ("*Moved* 40 active members enrolled; cash steady. AHCCCS "
                "revalidation on track; 12 assessments completed. *Watch* "
                "intake volume up.")
        _install_fake_anthropic(monkeypatch, text)
        out = cs.synthesize_channel_lex("facts")
        assert out == text                        # unchanged: no false-block, no corruption
        assert "*Moved*" in out and "*Watch*" in out

    def test_lex_prompt_carries_phi_rules(self):
        p = cs._LEX_PROMPT
        assert "NEVER include" in p
        assert "diagnosis" in p
        assert "AGGREGATE" in p
        assert "highest-stakes" in p

    def test_no_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert cs.synthesize_channel_lex("facts") is None


class TestD051Remediation:
    """Regression tests for the confirmed D-051 adversarial-review findings."""

    def _staff_map(self, tmp_path, monkeypatch, names=("Shaun Hawkins",)):
        monkeypatch.setenv("STRATEGY_ASANA_MAP_PATH", str(tmp_path / "m.yaml"))
        rows = "".join(
            f"  - slack_user_id: U{i}\n    asana_user_gid: '{i}'\n    display_name: {n}\n"
            for i, n in enumerate(names, 1))
        (tmp_path / "m.yaml").write_text(f"users:\n{rows}", encoding="utf-8")

    # ---- #1/#7: LEX decision topic scrubbed (covers synth AND fallback) ----
    def test_lex_decision_topic_client_name_scrubbed(self, tmp_path, monkeypatch):
        self._staff_map(tmp_path, monkeypatch)
        monkeypatch.setattr(sm, "gather_stalled_decisions", lambda today=None: {
            "ok": True, "decisions": [
                {"topic": "Approve client Maria Gonzalez respite hours",
                 "entity": "LEX", "severity": "P1", "age_days": 5, "owner": "Shaun"}]})
        out = cs.gather_decisions_for_entity("LEX", today=date(2026, 7, 7))
        topic = out["decisions"][0]["topic"]
        assert "Maria Gonzalez" not in topic
        assert "[name redacted]" in topic

    def test_lex_fallback_body_scrubbed(self, tmp_path, monkeypatch):
        """The LEX fallback (Sonnet unavailable) must not leak a client name."""
        self._staff_map(tmp_path, monkeypatch)
        monkeypatch.setenv("SYNTHESIS_SNAPSHOT_DIR", str(tmp_path / "syn"))
        monkeypatch.setattr(sm, "gather_stalled_decisions", lambda today=None: {
            "ok": True, "decisions": [
                {"topic": "client Maria Gonzalez placement decision", "entity": "LEX",
                 "severity": "P0", "age_days": 9, "owner": "Shaun"}]})
        # No cash/deadline/kb noise; synth returns None -> deterministic fallback.
        monkeypatch.setattr(cs, "gather_cash_for_entity", lambda e: {"ok": False})
        monkeypatch.setattr(cs, "gather_deadline_radar_for_entity",
                            lambda *a, **k: {"ok": True, "due_14d": 0, "overdue": 0,
                                             "overdue_by_owner": {}, "items": []})
        monkeypatch.setattr(cs, "gather_kb_for_entity", lambda e: {"ok": False})
        monkeypatch.setattr(sm, "gather_health", lambda: {"ok": False})
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # synth -> None
        out = cs.run_entity("lex", dry_run=True, today=date(2026, 7, 7))
        assert out["synthesized"] is False
        assert "Maria Gonzalez" not in out["body"]

    # ---- #2: LEX-cross-tagged decision excluded from a non-LEX post ----
    def test_lex_cross_tagged_decision_excluded_from_nonlex(self, monkeypatch):
        monkeypatch.setattr(sm, "gather_stalled_decisions", lambda today=None: {
            "ok": True, "decisions": [
                {"topic": "Approve Maria's respite billing", "entity": "F3E, LEX",
                 "severity": "P1", "age_days": 5, "owner": "H"},
                {"topic": "F3E retail expansion", "entity": "F3E",
                 "severity": "P0", "age_days": 5, "owner": "H"}]})
        out = cs.gather_decisions_for_entity("F3E", today=date(2026, 7, 7))
        topics = {d["topic"] for d in out["decisions"]}
        assert topics == {"F3E retail expansion"}   # LEX-cross-tagged dropped

    # ---- #3: LEX-cross-listed task not itemized in a non-LEX radar ----
    def test_lex_cross_listed_task_not_itemized_in_nonlex(self, tmp_path, monkeypatch):
        self._staff_map(tmp_path, monkeypatch, names=("Alex Cordova",))
        tasks = {"1": [
            {"name": "client billing appeal for Robert", "due_on": "2026-07-09",
             "completed": False,
             "projects": [{"name": "[LEX-LLC] Ops"}, {"name": "[OSN] Vendor"}]}]}
        out = cs.gather_deadline_radar_for_entity(
            "OSN", today=date(2026, 7, 7), get_tasks_fn=lambda gid: tasks["1"])
        assert out["items"] == []           # LEX-cross-listed task never itemized
        assert out["due_14d"] == 1          # still counted
        assert out["redacted"] == 1

    # ---- #5: --channel cannot target a foreign entity's channel ----
    def test_channel_override_to_foreign_entity_refused(self):
        import pytest
        with pytest.raises(ValueError):
            cs.run_entity("f3e", dry_run=True,
                          channel=cs.SCOPE_CHANNELS["ufl"])

    def test_channel_override_to_own_or_smoke_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SYNTHESIS_SNAPSHOT_DIR", str(tmp_path / "syn"))
        monkeypatch.setattr(cs, "gather_all_for_entity",
                            lambda entity, today=None: {"entity": entity,
                                                        "date": "2026-07-07",
                                                        "cash": {"ok": False}})
        monkeypatch.setattr(cs, "compute_entity_deltas", lambda e, c, p: {"first_run": True})
        monkeypatch.setattr(cs, "build_entity_facts_text", lambda e, g, d: "F")
        monkeypatch.setattr(cs, "synthesize_channel_entity", lambda e, f: "B")
        # own channel + smoke both fine (no raise)
        cs.run_entity("f3e", dry_run=True, today=date(2026, 7, 7),
                      channel=cs.SCOPE_CHANNELS["f3e"])
        cs.run_entity("f3e", dry_run=True, today=date(2026, 7, 7),
                      channel=cs.SMOKE_CHANNEL)

    # ---- #6: source-opacity -- no "sheet" in the F3E cash line ----
    def test_entity_cash_line_has_no_sheet_word(self):
        g = {"entity": "F3E", "date": "2026-07-07",
             "cash": {"ok": True, "label": "F3 Energy", "closing_balance": 1680.0,
                      "week_label": "Week of 5/19/2026"},
             "pipeline": {"ok": False, "omitted": True},
             "decisions": {"ok": True, "decisions": []},
             "deadlines": {"ok": True, "due_14d": 0, "overdue": 0,
                           "overdue_by_owner": {}, "items": []},
             "kb_activity": {"ok": False}, "health": {"ok": False}}
        facts = cs.build_entity_facts_text("F3E", g, {"first_run": True})
        assert "sheet" not in facts.lower()
        assert "Week of 5/19/2026" in facts      # label still shown

    # ---- #8/#13: F3E pipeline uses get_deals_by_pipeline (not entity filter) ----
    def test_f3e_pipeline_uses_pipeline_fetch(self, monkeypatch):
        from cora.tools import hubspot_client
        calls = {"pipeline": 0, "filter": 0}
        monkeypatch.setattr(hubspot_client, "get_deals_by_pipeline",
                            lambda pid: (calls.__setitem__("pipeline", calls["pipeline"] + 1) or []))
        monkeypatch.setattr(hubspot_client, "get_deals_by_filter",
                            lambda **k: (calls.__setitem__("filter", calls["filter"] + 1) or []))
        cs.gather_pipeline_for_entity("F3E", now=1_000_000.0)
        assert calls["pipeline"] == 1 and calls["filter"] == 0
        calls["pipeline"] = calls["filter"] = 0
        cs.gather_pipeline_for_entity("OSN", now=1_000_000.0)
        assert calls["filter"] == 1 and calls["pipeline"] == 0

    # ---- #9: bare "LLC" no longer routes a non-LEX decision into LEX ----
    def test_generic_llc_not_pulled_into_lex(self, monkeypatch):
        monkeypatch.setattr(sm, "gather_stalled_decisions", lambda today=None: {
            "ok": True, "decisions": [
                {"topic": "HJR Global reorg", "entity": "HJR Global LLC",
                 "severity": "P1", "age_days": 5, "owner": "H"}]})
        out = cs.gather_decisions_for_entity("LEX", today=date(2026, 7, 7))
        assert out["decisions"] == []       # generic LLC suffix not LEX

    # ---- #10: LEX routes to the strict gate via the PUBLIC entry point ----
    def test_synthesize_channel_entity_routes_lex_to_strict_gate(self, tmp_path, monkeypatch):
        self._staff_map(tmp_path, monkeypatch)
        _install_fake_anthropic(
            monkeypatch, "*Needs you* client Maria Gonzalez needs a renewal.")
        out = cs.synthesize_channel_entity("LEX", "facts")   # public entry, not _lex
        assert out is not None
        assert "Maria Gonzalez" not in out
        assert "[name redacted]" in out

    def test_synthesize_channel_entity_lex_drops_clinical(self, monkeypatch):
        _install_fake_anthropic(monkeypatch, "*Watch* a client is on risperidone.")
        assert cs.synthesize_channel_entity("LEX", "facts") is None

    # ---- #14: positive source-opacity assertion on the ecom fold ----
    def test_ecom_fold_source_opaque_when_healthy(self, monkeypatch):
        import cora.connectors.shopify_client as shopify
        import cora.connectors.polar_client as polar
        import cora.tools.asana_client as asana

        class _Pulse:
            net_revenue_usd = 1000.0
            order_count = 10
            avg_order_value_usd = 100.0
            top_products = []

        class _Variant:
            product_type = "beverage"
            product_title = "Pure"
            variant_title = "12pk"
            low_stock = False
            qty_on_hand = 50

        class _Rep:
            total_data = {"total_marketing_spend": 100, "blended_net_sales": 5000,
                          "blended_roas": 3.0,
                          "recharge_sales_products.computed.net_sales": 2000,
                          "recharge_sales_products.raw.total_active_subscriptions": 300}
        monkeypatch.setattr(shopify, "get_sales_pulse", lambda w: _Pulse())
        monkeypatch.setattr(shopify, "get_inventory_status", lambda **k: [_Variant()])
        monkeypatch.setattr(shopify, "is_beverage_product", lambda pt, t: True)
        monkeypatch.setattr(polar, "generate_report", lambda **k: _Rep())
        monkeypatch.setattr(asana, "get_project_tasks", lambda gid, max_tasks=100: [])
        out = cs.gather_f3e_ecom(today=date(2026, 7, 7))
        blob = " ".join(out["lines"]).lower()
        for platform in ("shopify", "recharge", "polar", "meta", "tiktok",
                         "klaviyo", "hubspot", "sheet"):
            assert platform not in blob, f"source name leaked: {platform}"


class TestSetupScripts:
    _SCOPES = {
        "portfolio": ("run_portfolio_synthesis.py", None),
        "f3e": ("run_entity_synthesis.py", "f3e"),
        "hjrp": ("run_entity_synthesis.py", "hjrp"),
        "osn": ("run_entity_synthesis.py", "osn"),
        "lex": ("run_entity_synthesis.py", "lex"),
        "bdm": ("run_entity_synthesis.py", "bdm"),
        "ufl": ("run_entity_synthesis.py", "ufl"),
        "hjrprod": ("run_entity_synthesis.py", "hjrprod"),
        "f3c": ("run_entity_synthesis.py", "f3c"),
    }

    def _path(self, scope):
        return _REPO_ROOT / "deployment" / f"setup-daily-synthesis-{scope}-task.ps1"

    def test_all_nine_exist(self):
        for scope in self._SCOPES:
            assert self._path(scope).exists(), scope

    def test_doctrine_compliance(self):
        import re
        for scope, (script, entity) in self._SCOPES.items():
            src = self._path(scope).read_text(encoding="utf-8")
            assert all(ord(c) < 128 for c in src), f"{scope}: non-ASCII (D-016)"
            assert r".venv\Scripts\python.exe" in src, f"{scope}: venv python (D-005)"
            assert "uv run" not in src, f"{scope}: uv (D-005)"
            assert "-Daily" in src, f"{scope}: daily trigger"
            assert script in src, f"{scope}: wrong runner"
            if entity:
                assert f"--entity {entity}" in src, f"{scope}: entity arg"

    def test_times_unique_and_free(self):
        import re
        occupied = {"06:00", "06:10", "06:30", "06:40", "06:45", "06:50",
                    "07:00", "07:06", "07:10", "07:15", "07:30"}
        times = []
        for scope in self._SCOPES:
            src = self._path(scope).read_text(encoding="utf-8")
            m = re.search(r'\$HourMin\s*=\s*"([\d:]+)"', src)
            assert m, f"{scope}: no HourMin"
            times.append(m.group(1))
        assert len(times) == len(set(times)), f"duplicate times: {times}"
        assert not (set(times) & occupied), "collision with a live task minute"


class TestSourcePostSites:
    def test_channel_module_posts_to_channels_never_opens_dm(self):
        """channel_synthesis posts to channels (chat_postMessage) but NEVER opens
        a DM (conversations_open) -- that path belongs to the Harrison-only memo."""
        src = (_REPO_ROOT / "src" / "cora" / "channel_synthesis.py").read_text(
            encoding="utf-8")
        assert src.count("conversations_open") == 0
        assert src.count("chat_postMessage") == 1

    def test_strategy_memo_harrison_only_invariant_unchanged(self):
        """The weekly memo's Harrison-only guarantee must remain provably intact:
        exactly one channel-post + one DM-open site, still hard-coded to Harrison."""
        src = (_REPO_ROOT / "src" / "cora" / "strategy_memo.py").read_text(
            encoding="utf-8")
        assert src.count("chat_postMessage") == 1
        assert src.count("conversations_open") == 1
        assert 'HARRISON_SLACK_ID = "U0B2RM2JYJ1"' in src
