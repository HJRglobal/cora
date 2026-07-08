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
        expected = sanitize_text(normalize_slack_bold(body))[:cs._MAX_SLACK_CHARS]
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
        assert out["body"] == "BODY"
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
