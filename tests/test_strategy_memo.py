"""Tests for the founder strategy memo (strategy_memo.py, Org Synthesis Phase 4).

Layer A: source assertions (runner script, deployment PS1s, D-005, ASCII).
Layer B: unit tests with env-overridden paths and injectable fetch/synth/
deliver functions (no network, no Sheets/HubSpot/Asana/Slack/Anthropic).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import strategy_memo as sm

TODAY = date(2026, 6, 14)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gathered(date_str="2026-06-14", cash_balances=None, decisions=None):
    cash_balances = cash_balances or {"F3E": 100_000.0, "OSN": 50_000.0}
    entities = {}
    for code, label in sm.CASH_ENTITIES:
        if code in cash_balances:
            entities[code] = {"label": label,
                              "closing_balance": cash_balances[code],
                              "actual": -5000.0, "forecast": -4000.0}
        else:
            entities[code] = {"label": label, "error": True}
    return {
        "date": date_str,
        "cash": {"ok": True, "week_label": "Week of 6/8/2026",
                 "entities": entities},
        "pipeline": {"ok": True, "pipelines": {
            "f3e_retail": {"label": "F3E Retail", "open_count": 4,
                           "open_amount": 20_000.0,
                           "stages": {"Proposal": {"count": 2, "amount": 12_000.0},
                                      "Outreach": {"count": 2, "amount": 8_000.0}},
                           "aging": []},
            "default": {"label": "UFL/OSN/BDM (default)", "open_count": 1,
                        "open_amount": 100_000.0,
                        "stages": {"Negotiation": {"count": 1,
                                                   "amount": 100_000.0}},
                        "aging": []},
        }},
        "decisions": {"ok": True, "decisions": decisions if decisions is not None
                      else [{"topic": "OIC pre-qualifier", "entity": "FNDR",
                             "severity": "P0", "age_days": 30,
                             "owner": "Harrison"}]},
        "deadlines": {"ok": True, "due_14d": 3, "overdue": 2,
                      "overdue_by_owner": {"Hannah Grant": 2},
                      "items": [{"name": "Send sponsor deck",
                                 "owner": "Hannah Grant",
                                 "due_on": "2026-06-15", "overdue": False}],
                      "aggregate_only": 1, "users_failed": 0},
        "efficiency": {"ok": True,
                       "approved_recent": [{"date": "2026-06-12",
                                            "title": "Cox bill mail filter"}],
                       "approved_total": 3,
                       "pending": [{"title": "Check recon SOP",
                                    "entity": "HJRG", "route": "doc"}]},
        "kb_activity": {"ok": True, "by_entity": {"F3E": 900, "LEX": 400}},
        "health": {"ok": True, "line": "Cora healthy (heartbeat 30s ago)",
                   "age_seconds": 30},
    }


@pytest.fixture()
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATEGY_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    monkeypatch.setenv("STRATEGY_MEMO_DIR", str(tmp_path / "memos"))
    monkeypatch.setenv("STRATEGY_DECISIONS_PATH", str(tmp_path / "pending.md"))
    monkeypatch.setenv("EFFICIENCY_BACKLOG_PATH", str(tmp_path / "backlog.md"))
    monkeypatch.setenv("STRATEGY_KB_DB_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("STRATEGY_ASANA_MAP_PATH", str(tmp_path / "map.yaml"))
    monkeypatch.setenv("STRATEGY_HEARTBEAT_PATH", str(tmp_path / "hb.txt"))
    return tmp_path


def _write_snapshot(tmp_path, date_str, gathered):
    snap_dir = tmp_path / "snaps"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / f"{date_str}.json").write_text(
        json.dumps(gathered), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fail-soft gatherers
# ---------------------------------------------------------------------------

class TestFailSoft:
    def test_safe_gather_swallows_exceptions(self):
        def boom():
            raise RuntimeError("dead source")
        out = sm._safe_gather("x", boom)
        assert out == {"ok": False}

    def test_safe_gather_rejects_non_dict(self):
        assert sm._safe_gather("x", lambda: "nope") == {"ok": False}

    def test_facts_text_degrades_dead_sections_to_stub_lines(self):
        gathered = {"date": "2026-06-14",
                    "cash": {"ok": False}, "pipeline": {"ok": False},
                    "decisions": {"ok": False}, "deadlines": {"ok": False},
                    "efficiency": {"ok": False}, "kb_activity": {"ok": False},
                    "health": {"ok": False}}
        facts = sm.build_facts_text(gathered, {"first_run": True})
        assert "(cash source unavailable this week)" in facts
        assert "(pipeline source unavailable this week)" in facts
        assert "(decisions source unavailable this week)" in facts
        assert "(deadline source unavailable this week)" in facts
        assert "(health signal unavailable this week)" in facts
        assert "first run" in facts

    def test_run_memo_survives_every_source_dead(self, paths):
        result = sm.run_memo(
            dry_run=True, today=TODAY,
            gather_fn=lambda: {"date": TODAY.isoformat(),
                               "cash": {"ok": False}, "pipeline": {"ok": False},
                               "decisions": {"ok": False},
                               "deadlines": {"ok": False},
                               "efficiency": {"ok": False},
                               "kb_activity": {"ok": False},
                               "health": {"ok": False}},
            synth_fn=lambda facts: None,
            deliver_fn=lambda body: True,
        )
        assert result["memo"]
        assert result["synthesized"] is False

    def test_run_memo_delivers_even_when_memo_write_hits_outage(self, paths, monkeypatch):
        """A G: unmount during the memo-file write must NOT block Harrison's DM -- the
        file write is best-effort, so memo_path is empty but delivered stays True
        (D-051 GAP6)."""
        def _boom_write(*_a, **_k):
            raise sm.drive_io.DriveUnavailable("simulated G: unmount")

        monkeypatch.setattr(sm.drive_io, "write_text_atomic", _boom_write)
        delivered = {"n": 0}

        def _deliver(_body):
            delivered["n"] += 1
            return True

        result = sm.run_memo(
            dry_run=False, today=TODAY,
            gather_fn=lambda: {"date": TODAY.isoformat(),
                               "cash": {"ok": False}, "pipeline": {"ok": False},
                               "decisions": {"ok": False}, "deadlines": {"ok": False},
                               "efficiency": {"ok": False}, "kb_activity": {"ok": False},
                               "health": {"ok": False}},
            synth_fn=lambda facts: "MEMO BODY",
            deliver_fn=_deliver,
        )
        assert result["memo_path"] == ""     # the memo file write was skipped
        assert result["delivered"] is True   # but the DM still fired
        assert delivered["n"] == 1

    def test_gather_cash_marks_failed_entity_and_keeps_rest(self, monkeypatch):
        import cora.connectors.gsheets_financials as gf

        class _Summary:
            week_label = "Week of 6/8/2026"
            closing_balance = 42.0
            portfolio_actual = -1.0
            portfolio_forecast = -1.0

        calls = {"n": 0}

        def fake_get_cashflow(tab_name=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise gf.GsheetsConnectorError("boom")
            return _Summary()

        monkeypatch.setattr(gf, "get_cashflow", fake_get_cashflow)
        out = sm.gather_cash()
        assert out["ok"] is True
        entities = out["entities"]
        assert entities["FNDR"].get("error") is True       # first call failed
        assert entities["F3E"]["closing_balance"] == 42.0

    def test_gather_pipeline_failed_pipeline_degrades(self):
        def fetch(pid):
            raise RuntimeError("hubspot down")
        out = sm.gather_pipeline(fetch_fn=fetch, stage_names={})
        assert out["ok"] is False
        assert out["pipelines"]["f3e_retail"]["error"] is True


# ---------------------------------------------------------------------------
# Pipeline math
# ---------------------------------------------------------------------------

class TestPipeline:
    def _deal(self, name, stage, amount, idle_days, now):
        modified = time.time() - idle_days * 86400
        iso = __import__("datetime").datetime.fromtimestamp(
            modified, __import__("datetime").timezone.utc).isoformat()
        return {"properties": {"dealname": name, "dealstage": stage,
                               "amount": str(amount),
                               "hs_lastmodifieddate": iso}}

    def test_open_totals_exclude_closed_and_flag_aging(self):
        now = time.time()
        deals = [
            self._deal("Sprouts", "s1", 5000, 30, now),     # open, aging
            self._deal("GNC", "s1", 2000, 2, now),          # open, fresh
            self._deal("Won deal", "s2", 9000, 2, now),     # closed -- excluded
        ]
        out = sm.gather_pipeline(
            fetch_fn=lambda pid: deals,
            stage_names={"s1": "Proposal", "s2": "Closed Won"},
            now=now,
        )
        p = out["pipelines"]["f3e_retail"]
        assert p["open_count"] == 2
        assert p["open_amount"] == 7000.0
        assert p["stages"]["Proposal"]["count"] == 2
        assert "Closed Won" not in p["stages"]
        assert len(p["aging"]) == 1
        assert p["aging"][0]["name"] == "Sprouts"
        assert p["aging"][0]["idle_days"] >= 29


# ---------------------------------------------------------------------------
# Stalled-decisions parsing
# ---------------------------------------------------------------------------

class TestDecisions:
    def test_parses_p0_p1_skips_p2_and_resolved(self, paths):
        (paths / "pending.md").write_text(
            "# Pending\n\n"
            "### Big call\n- **Entity**: F3E\n- **Severity**: P0\n"
            "- **Last touched**: 2026-06-01\n- **Owner of next nudge**: Harrison\n\n"
            "### Medium call\n- **Entity**: OSN\n- **Severity**: P1\n"
            "- **Last touched**: 2026-05-30\n\n"
            "### Small call\n- **Entity**: BDM\n- **Severity**: P2\n\n"
            "## Recently resolved\n\n"
            "### Done call\n- **Entity**: F3E\n- **Severity**: P0\n",
            encoding="utf-8")
        out = sm.gather_stalled_decisions(today=TODAY)
        topics = [d["topic"] for d in out["decisions"]]
        assert topics == ["Big call", "Medium call"]
        assert out["decisions"][0]["age_days"] == 13
        assert out["decisions"][0]["owner"] == "Harrison"

    def test_missing_file_fails_soft(self, paths):
        out = sm.gather_stalled_decisions(today=TODAY)
        assert out["ok"] is False

    def test_template_skeleton_never_parsed_but_annotated_severity_is(self, paths):
        """The 'How to use' template block (topic '[Topic]', severity line
        'P0 / P1 / P2 / P3') must be skipped; a real entry with an annotated
        severity ('P0 (decision Monday)') must be kept. Live-dry-run finding
        2026-06-11: the skeleton leaked into the memo as a bogus P0."""
        (paths / "pending.md").write_text(
            "# Pending\n\n## How to use\n\n"
            "### [Topic]\n- **Entity**: FNDR / HJRG / F3E / OSN\n"
            "- **Severity**: P0 / P1 / P2 / P3\n"
            "- **Owner of next nudge**: who is supposed to move this forward\n\n"
            "## Active\n\n"
            "### Real call\n- **Entity**: F3E\n"
            "- **Severity**: P0 (decision moment is the Monday call)\n"
            "- **Last touched**: 2026-06-06\n"
            "- **Owner of next nudge**: Harrison\n",
            encoding="utf-8")
        out = sm.gather_stalled_decisions(today=TODAY)
        assert [d["topic"] for d in out["decisions"]] == ["Real call"]


# ---------------------------------------------------------------------------
# Deadline radar (incl. LEX aggregate-only + PHI guard)
# ---------------------------------------------------------------------------

class TestDeadlineRadar:
    def _map(self, paths):
        (paths / "map.yaml").write_text(
            "users:\n"
            "  - slack_user_id: U1\n    asana_user_gid: '111'\n"
            "    display_name: Hannah Grant\n"
            "  - slack_user_id: U2\n    asana_user_gid: '222'\n"
            "    display_name: Shaun Hawkins\n",
            encoding="utf-8")

    def test_radar_counts_and_items(self, paths):
        self._map(paths)
        tasks = {
            "111": [
                {"name": "Send sponsor deck", "due_on": "2026-06-15",
                 "completed": False, "projects": [{"name": "[F3E] Sales"}]},
                {"name": "Old thing", "due_on": "2026-06-10",
                 "completed": False, "projects": [{"name": "[F3E] Sales"}]},
                {"name": "Far future", "due_on": "2026-09-01",
                 "completed": False, "projects": []},
                {"name": "Done", "due_on": "2026-06-15",
                 "completed": True, "projects": []},
            ],
            "222": [
                {"name": "Tucson stove install", "due_on": "2026-06-16",
                 "completed": False, "projects": [{"name": "[LEX-LLC] Ops"}]},
            ],
        }
        out = sm.gather_deadline_radar(today=TODAY,
                                       get_tasks_fn=lambda gid: tasks[gid])
        assert out["due_14d"] == 2          # sponsor deck + Tucson
        assert out["overdue"] == 1          # Old thing
        assert out["overdue_by_owner"] == {"Hannah Grant": 1}
        names = [i["name"] for i in out["items"]]
        assert "Send sponsor deck" in names
        # LEX task counted but never itemized:
        assert "Tucson stove install" not in names
        assert out["aggregate_only"] == 1

    def test_radar_user_failure_fail_soft(self, paths):
        self._map(paths)

        def get_tasks(gid):
            if gid == "111":
                raise RuntimeError("asana down")
            return []
        out = sm.gather_deadline_radar(today=TODAY, get_tasks_fn=get_tasks)
        assert out["ok"] is True
        assert out["users_failed"] == 1


# ---------------------------------------------------------------------------
# Efficiency gather
# ---------------------------------------------------------------------------

class TestEfficiency:
    def test_backlog_recent_window_and_pending_filter(self, paths, monkeypatch):
        (paths / "backlog.md").write_text(
            "# Efficiency Backlog\n\n"
            "## [2026-06-12] Cox bill mail filter\n\n- Route: make_com\n\n"
            "## [2026-05-01] Ancient entry\n\n- Route: doc\n",
            encoding="utf-8")
        from cora import knowledge_review as kr
        monkeypatch.setattr(kr, "load_proposed_updates", lambda: [
            {"update_type": "efficiency", "state": "PENDING",
             "payload": {"title": "Check recon SOP", "entity": "HJRG",
                         "route": "doc"}},
            {"update_type": "efficiency", "state": "APPROVED",
             "payload": {"title": "Already approved"}},
            {"update_type": "known_answer", "state": "PENDING",
             "payload": {"title": "Not efficiency"}},
        ])
        out = sm.gather_efficiency(today=TODAY)
        assert [e["title"] for e in out["approved_recent"]] == ["Cox bill mail filter"]
        assert out["approved_total"] == 2
        assert [p["title"] for p in out["pending"]] == ["Check recon SOP"]


# ---------------------------------------------------------------------------
# KB activity + health
# ---------------------------------------------------------------------------

class TestKbAndHealth:
    def test_kb_activity_counts(self, paths):
        db = paths / "kb.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE knowledge_chunks "
                     "(entity TEXT, source TEXT, ingested_at INTEGER)")
        now = int(time.time())
        rows = [("F3E", "slack", now), ("F3E", "gmail", now),
                ("LEX", "fireflies", now),
                ("OSN", "slack", now - 30 * 86400),     # outside window
                ("F3E", "static_md", now)]              # non-swept source
        conn.executemany("INSERT INTO knowledge_chunks VALUES (?,?,?)", rows)
        conn.commit()
        conn.close()
        out = sm.gather_kb_activity()
        assert out["by_entity"] == {"F3E": 2, "LEX": 1}

    def test_health_fresh_and_stale(self, paths):
        from datetime import datetime, timezone
        hb = paths / "hb.txt"
        hb.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        fresh = sm.gather_health()
        assert "healthy" in fresh["line"]
        stale = sm.gather_health(now=time.time() + 7200)
        assert "STALE" in stale["line"]


# ---------------------------------------------------------------------------
# Snapshot + delta math
# ---------------------------------------------------------------------------

class TestSnapshots:
    def test_first_run_has_no_deltas(self, paths):
        deltas = sm.compute_deltas(_gathered(), sm.load_prior_snapshots(today=TODAY))
        assert deltas == {"first_run": True}

    def test_cash_delta_and_streak(self, paths):
        # Three weeks of OSN decline; F3E flat-then-up.
        _write_snapshot(paths, "2026-05-31",
                        _gathered("2026-05-31",
                                  {"F3E": 100_000.0, "OSN": 70_000.0}))
        _write_snapshot(paths, "2026-06-07",
                        _gathered("2026-06-07",
                                  {"F3E": 100_000.0, "OSN": 60_000.0}))
        current = _gathered("2026-06-14", {"F3E": 110_000.0, "OSN": 50_000.0})
        priors = sm.load_prior_snapshots(today=TODAY)
        assert [p["date"] for p in priors] == ["2026-06-07", "2026-05-31"]
        deltas = sm.compute_deltas(current, priors)
        assert deltas["cash"]["OSN"]["delta"] == -10_000.0
        assert deltas["cash"]["OSN"]["decline_streak"] == 2
        assert deltas["cash"]["F3E"]["delta"] == 10_000.0
        assert deltas["cash"]["F3E"]["decline_streak"] == 0
        facts = sm.build_facts_text(current, deltas)
        assert "cash down 2 weeks straight" in facts

    def test_unmoved_decision_streak(self, paths):
        d = [{"topic": "OIC pre-qualifier", "entity": "FNDR", "severity": "P0",
              "age_days": 10, "owner": "Harrison"}]
        _write_snapshot(paths, "2026-05-31", _gathered("2026-05-31", decisions=d))
        _write_snapshot(paths, "2026-06-07", _gathered("2026-06-07", decisions=d))
        current = _gathered("2026-06-14", decisions=d)
        deltas = sm.compute_deltas(current, sm.load_prior_snapshots(today=TODAY))
        assert deltas["unmoved_decisions"]["OIC pre-qualifier"] == 3
        facts = sm.build_facts_text(current, deltas)
        assert "unmoved 3 memos running" in facts

    def test_pipeline_delta(self, paths):
        prev = _gathered("2026-06-07")
        prev["pipeline"]["pipelines"]["f3e_retail"]["open_count"] = 3
        prev["pipeline"]["pipelines"]["f3e_retail"]["open_amount"] = 15_000.0
        prev["pipeline"]["pipelines"]["f3e_retail"]["stages"] = {
            "Proposal": {"count": 1, "amount": 7_000.0},
            "Outreach": {"count": 2, "amount": 8_000.0}}
        _write_snapshot(paths, "2026-06-07", prev)
        deltas = sm.compute_deltas(_gathered(), sm.load_prior_snapshots(today=TODAY))
        f3e = deltas["pipeline"]["f3e_retail"]
        assert f3e["open_count_delta"] == 1
        assert f3e["open_amount_delta"] == 5_000.0
        assert f3e["stage_moves"] == {"Proposal": 1}

    def test_snapshot_retention(self, paths):
        for i in range(sm.SNAPSHOT_KEEP + 5):
            _write_snapshot(paths, f"2025-01-{i + 1:02d}" if i < 27
                            else f"2025-02-{i - 26:02d}", _gathered())
        sm.save_snapshot(_gathered(), today=TODAY)
        snaps = list((paths / "snaps").glob("*.json"))
        assert len(snaps) == sm.SNAPSHOT_KEEP


# ---------------------------------------------------------------------------
# Synthesis fail-closed + memo assembly
# ---------------------------------------------------------------------------

class TestSynthesis:
    def test_no_api_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert sm.synthesize_memo("facts") is None

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
        assert sm.synthesize_memo("facts") is None

    def test_fallback_memo_flags_unavailable_synthesis(self):
        memo = sm.fallback_memo("THE FACTS")
        assert "SYNTHESIS UNAVAILABLE" in memo
        assert "THE FACTS" in memo

    def test_run_memo_uses_fallback_when_synth_fails(self, paths):
        result = sm.run_memo(dry_run=True, today=TODAY,
                             gather_fn=_gathered,
                             synth_fn=lambda facts: None,
                             deliver_fn=lambda body: True)
        assert result["synthesized"] is False
        assert "SYNTHESIS UNAVAILABLE" in result["memo"]

    def test_synth_prompt_carries_hard_rules(self):
        assert "Never invent numbers" in sm._SYNTH_PROMPT
        assert "ADVISORY" in sm._SYNTH_PROMPT
        assert "client-level health" in sm._SYNTH_PROMPT
        assert "trade-off" in sm._SYNTH_PROMPT
        assert "should this live at HJR Global" in sm._SYNTH_PROMPT


# ---------------------------------------------------------------------------
# Delivery + memo file
# ---------------------------------------------------------------------------

class TestDelivery:
    def test_memo_file_naming_convention(self, paths):
        path = sm.write_memo_file("body", today=TODAY)
        assert path.parent.name == "2026-06"
        assert path.name == "2026-06-14_fndr_weekly-strategy-memo.md"
        assert path.read_text(encoding="utf-8") == "body"

    def test_document_header(self):
        doc = sm.build_memo_document("BODY", today=TODAY)
        assert "Weekly Portfolio Strategy Memo -- 2026-06-14" in doc
        assert "Advisory only" in doc
        assert "BODY" in doc

    def test_delivery_is_hardcoded_to_harrison(self, monkeypatch):
        """The DM recipient is HARRISON_SLACK_ID -- no parameter exists to
        target anyone else, and the open call must use exactly that ID."""
        import inspect
        sig = inspect.signature(sm.deliver_to_harrison)
        assert "user" not in sig.parameters
        assert "channel" not in sig.parameters

        sent = {}

        class _FakeClient:
            def __init__(self, token):
                pass

            def conversations_open(self, users):
                sent["users"] = users
                return {"channel": {"id": "D123"}}

            def chat_postMessage(self, channel, text):
                sent["channel"] = channel
                sent["text"] = text

        import slack_sdk
        monkeypatch.setattr(slack_sdk, "WebClient", _FakeClient)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        ok = sm.deliver_to_harrison("memo body", today=TODAY)
        assert ok is True
        assert sent["users"] == [sm.HARRISON_SLACK_ID]
        assert sent["channel"] == "D123"
        assert "memo body" in sent["text"]

    def test_module_never_posts_to_channels(self):
        """Source-level guard: the only Slack post site is the Harrison DM."""
        src = (_REPO_ROOT / "src" / "cora" / "strategy_memo.py").read_text(
            encoding="utf-8")
        assert src.count("chat_postMessage") == 1
        assert src.count("conversations_open") == 1
        assert 'HARRISON_SLACK_ID = "U0B2RM2JYJ1"' in src

    def test_dry_run_writes_nothing(self, paths):
        delivered = {"called": False}

        def deliver(body):
            delivered["called"] = True
            return True
        result = sm.run_memo(dry_run=True, today=TODAY, gather_fn=_gathered,
                             synth_fn=lambda facts: "MEMO", deliver_fn=deliver)
        assert result["memo"] == "MEMO"
        assert delivered["called"] is False
        assert not (paths / "snaps").exists()
        assert not (paths / "memos").exists()

    def test_real_run_writes_snapshot_file_and_delivers(self, paths):
        result = sm.run_memo(dry_run=False, today=TODAY, gather_fn=_gathered,
                             synth_fn=lambda facts: "MEMO BODY",
                             deliver_fn=lambda body: True)
        assert result["delivered"] is True
        assert (paths / "snaps" / "2026-06-14.json").exists()
        memo_file = (paths / "memos" / "2026-06" /
                     "2026-06-14_fndr_weekly-strategy-memo.md")
        assert memo_file.exists()
        assert "MEMO BODY" in memo_file.read_text(encoding="utf-8")
        assert result["memo_path"].endswith("2026-06-14_fndr_weekly-strategy-memo.md")


# ---------------------------------------------------------------------------
# PHI / LEX posture
# ---------------------------------------------------------------------------

class TestPhiPosture:
    def test_lex_cash_is_aggregate_label_only(self):
        facts = sm.build_facts_text(
            _gathered(cash_balances={"LEX": 25_000.0}), {"first_run": True})
        assert "Lexington Services" in facts     # aggregate cash line is fine

    def test_phi_flagged_task_names_never_itemized(self, paths):
        (paths / "map.yaml").write_text(
            "users:\n  - slack_user_id: U1\n    asana_user_gid: '111'\n"
            "    display_name: Jen Mortensen\n", encoding="utf-8")
        from cora import phi_guard
        phi_text = "task with PHI"
        tasks = [{"name": phi_text, "due_on": "2026-06-15",
                  "completed": False, "projects": []}]
        import cora.strategy_memo as mod
        orig = mod.is_phi_risk
        try:
            mod.is_phi_risk = lambda text: phi_text in text or orig(text)
            out = sm.gather_deadline_radar(today=TODAY,
                                           get_tasks_fn=lambda gid: tasks)
        finally:
            mod.is_phi_risk = orig
        assert out["items"] == []
        assert out["aggregate_only"] == 1
        assert phi_guard is not None

    def test_synthesized_phi_output_is_dropped(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        import anthropic

        class _Resp:
            content = [type("T", (), {"text": "memo"})()]

        class _Client:
            def __init__(self, api_key):
                pass

            class messages:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    return _Resp()
        monkeypatch.setattr(anthropic, "Anthropic", _Client)
        import cora.strategy_memo as mod
        orig = mod.is_phi_risk
        try:
            mod.is_phi_risk = lambda text: True
            assert sm.synthesize_memo("facts") is None
        finally:
            mod.is_phi_risk = orig


# ---------------------------------------------------------------------------
# Standalone-script guarantee (no bot-process imports)
# ---------------------------------------------------------------------------

class TestNoBotProcessImport:
    def test_import_does_not_pull_bot_modules(self):
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "import cora.strategy_memo; "
            "bad = [m for m in ('cora.app', 'cora.tool_dispatch', 'cora.claude_client')"
            " if m in sys.modules]; "
            "assert not bad, f'bot-process modules imported: {bad}'"
        ) % str(_REPO_ROOT / "src")
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Source assertions (runner / PS1 doctrine)
# ---------------------------------------------------------------------------

class TestSourceWiring:
    def test_runner_script_exists_with_dry_run(self):
        src = (_REPO_ROOT / "scripts" / "run_strategy_memo.py").read_text(
            encoding="utf-8")
        assert "--dry-run" in src
        assert "run_memo" in src
        # cp1252 console guard: live text is non-ASCII; print must not crash.
        assert 'reconfigure(encoding="utf-8"' in src

    def test_setup_ps1_doctrine_compliance(self):
        ps1 = _REPO_ROOT / "deployment" / "setup-strategy-memo-task.ps1"
        assert ps1.exists()
        src = ps1.read_text(encoding="utf-8")
        assert r".venv\Scripts\python.exe" in src        # D-005 absolute venv python
        assert "uv run" not in src                       # never uv (D-005)
        assert "Sunday" in src                           # weekly Sunday slot
        assert "18:30" in src                            # after friction mining
        assert all(ord(ch) < 128 for ch in src)          # ASCII-only (D-016)

    def test_ship_ps1_doctrine_compliance(self):
        ps1 = _REPO_ROOT / "deployment" / "ship-strategy-memo-2026-06-11.ps1"
        assert ps1.exists()
        src = ps1.read_text(encoding="utf-8")
        assert "uv run" not in src
        assert all(ord(ch) < 128 for ch in src)

    def test_module_constants(self):
        assert sm.HARRISON_SLACK_ID == "U0B2RM2JYJ1"
        assert sm.SONNET_MODEL.startswith("claude-sonnet")
        assert sm.DEADLINE_RADAR_DAYS == 14
