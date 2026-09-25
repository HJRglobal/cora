"""Code #16 C1 (A28) -- scripts/run_channel_archive_proposal.py.

Dry run (the default) reads Slack and writes NOTHING: no store event, no lock, no run
marker; it prints the candidate IDS only (how the cascade report's first-scan line is
produced). --apply --monthly delivers on/after the month's first Monday until a
monthly card went out (a Tuesday catch-up still delivers) and writes a marker every
fire; --clear-demotion is Harrison's. The script runs load_dotenv(override=True) at
import, so it is imported with os.environ snapshotted and restored (lesson 63 family).
"""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from _chanarch_fakes import DAY, HARRISON, FakeSlack, chan, msg, no_sleep
from cora.channel_archive import clients, policy
from cora.channel_archive import store as st

REPO = Path(__file__).resolve().parents[1]
_AZ = timezone(timedelta(hours=-7))


def _load():
    snap = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location(
            "run_channel_archive_proposal_c16", REPO / "scripts" / "run_channel_archive_proposal.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(snap)
    return mod


SCRIPT = _load()


def az(y, m, d, hh=7, mm=7):
    return datetime(y, m, d, hh, mm, tzinfo=_AZ).timestamp()


@pytest.fixture
def fake(monkeypatch):
    chans = [chan("C0CANDID01", "fx-dead-one", now=az(2026, 10, 5)),
             chan("C0ACTIVE01", "fx-alive", now=az(2026, 10, 5))]
    f = FakeSlack(channels=chans, history={"C0CANDID01": [msg(200, now=az(2026, 10, 5))],
                                           "C0ACTIVE01": [msg(1, now=az(2026, 10, 5))]},
                  scopes=["channels:manage", "chat:write"])
    monkeypatch.setattr(clients, "read_client_factory", lambda: f)
    monkeypatch.setattr(clients, "write_client_factory", lambda: f)
    monkeypatch.setattr("cora.channel_archive.classify.PACE_S", 0.0)
    return f


def markers():
    p = Path(os.environ["TASK_RUNS_LEDGER_PATH"])
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_first_monday():
    assert SCRIPT.first_monday(2026, 10).isoformat() == "2026-10-05"
    assert SCRIPT.first_monday(2026, 11).isoformat() == "2026-11-02"
    assert SCRIPT.first_monday(2026, 6).isoformat() == "2026-06-01"


class TestDryRun:
    def test_the_default_prints_ids_and_writes_nothing(self, fake, capsys):
        rc = SCRIPT.main([], now=az(2026, 10, 5))
        out = capsys.readouterr().out
        assert rc == 0
        assert "SECTION A candidates (1): C0CANDID01" in out
        assert "fx-dead-one" not in out                      # ids only, never names
        assert "groups:write=MISSING" in out and "SCOPE NOT CONFIRMED: groups:write" in out
        assert not st.store_path().exists() and not st.scan_lock_path().exists()
        assert markers() == [] and not fake.posts
        assert "conversations_archive" not in fake.method_names()

    def test_a_blind_dry_run_says_so(self, fake, monkeypatch, capsys):
        monkeypatch.setattr("cora.channel_archive.registry.load_deny_policy", lambda *a, **k: None)
        SCRIPT.main([], now=az(2026, 10, 5))
        assert "BLIND: policy_unreadable" in capsys.readouterr().out


class TestMonthly:
    def test_before_the_first_monday_it_skips_with_an_ok_marker(self, fake):
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 1)) == 0
        m = markers()
        assert len(m) == 1 and m[0]["ok"] is True and m[0]["outcome"] == "skipped:not_due_before_first_monday"
        assert m[0]["task"] == SCRIPT.TASK_NAME and not fake.posts

    def test_the_first_monday_delivers_then_the_next_monday_skips(self, fake):
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 5)) == 0
        assert fake.posts and markers()[-1]["outcome"] == "delivered" and markers()[-1]["outputs"] == 1
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 12)) == 0
        assert markers()[-1]["outcome"] == "skipped:already_delivered_this_month"
        assert len(fake.posts) == 1

    def test_a_tuesday_catch_up_still_delivers(self, fake):
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 6, 9, 0)) == 0
        assert markers()[-1]["outcome"] == "delivered"

    def test_an_undelivered_due_month_is_a_failed_marker_and_exit_1(self, fake):
        from _chanarch_fakes import api_error
        fake.post_behaviour = lambda kw: api_error("channel_not_found")
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 5)) == 1
        m = markers()[-1]
        assert m["ok"] is False and m["outcome"] == "month_undelivered"
        fake.post_behaviour = None
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 6)) == 0   # retried inside the week
        assert markers()[-1]["outcome"] == "delivered"


class TestMonthlyGateD051:
    """D-051 r1 registry-ops#0 + #4 (orchestrator ruling): a monthly card is due ONLY in
    the month's first-Monday week (AZ) and only until a NON-blind, FULLY delivered
    monthly card went out this calendar month. A blind card is ok=False month_blind, a
    partial one ok=False month_partial (both exit 1), and neither counts as the month's
    card, so a same-week re-run retries."""

    def test_a_late_month_registration_waits_for_the_next_first_monday(self, fake):
        """The 9/28 case: registered after the restart, September's first Monday (9/7)
        long gone -> no surprise catch-up card superseding an open ask card."""
        assert SCRIPT.monthly_due(az(2026, 9, 28), st.fold(events=[], ledger=[])) == (
            False, "not_due_after_first_monday_week")
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 9, 28)) == 0
        m = markers()[-1]
        assert m["ok"] is True and m["outcome"] == "skipped:not_due_after_first_monday_week"
        assert not fake.posts
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 5)) == 0
        assert markers()[-1]["outcome"] == "delivered" and len(fake.posts) == 1

    def test_the_week_window_edges(self):
        empty = st.fold(events=[], ledger=[])
        assert SCRIPT.monthly_due(az(2026, 10, 11, 23, 59), empty) == (True, "due")        # Sunday
        assert SCRIPT.monthly_due(az(2026, 10, 12, 0, 1), empty)[0] is False                # +7 days
        assert SCRIPT.monthly_due(az(2026, 10, 4, 23, 59), empty) == (False, "not_due_before_first_monday")

    def test_a_blind_monthly_card_is_month_blind_and_is_retried_inside_the_week(self, fake, monkeypatch, capsys):
        from cora.channel_archive import registry as reg
        real_policy = reg.load_deny_policy
        monkeypatch.setattr(reg, "load_deny_policy", lambda *a, **k: None)
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 5)) == 1
        m = markers()[-1]
        assert m["ok"] is False and m["outcome"] == "month_blind" and "policy_unreadable" in m["detail"]
        assert len(fake.posts) == 1                                   # the honest blind card went out
        assert "FAILED" in capsys.readouterr().out
        assert SCRIPT.monthly_due(az(2026, 10, 6), st.fold(now=az(2026, 10, 6))) == (True, "due")
        monkeypatch.setattr(reg, "load_deny_policy", real_policy)
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 6)) == 0
        assert markers()[-1]["outcome"] == "delivered"
        assert SCRIPT.monthly_due(az(2026, 10, 7), st.fold(now=az(2026, 10, 7))) == (
            False, "already_delivered_this_month")

    def test_a_partial_delivery_is_month_partial_and_not_the_months_card(self, monkeypatch):
        from _chanarch_fakes import api_error
        t = az(2026, 10, 5)
        chans = [chan(f"C0DEAD{i:04d}", f"fx-dead-{i:02d}", now=t) for i in range(21)]
        f = FakeSlack(channels=chans, history={c["id"]: [msg(200, now=t)] for c in chans},
                      scopes=["channels:manage", "chat:write"])
        monkeypatch.setattr(clients, "read_client_factory", lambda: f)
        monkeypatch.setattr(clients, "write_client_factory", lambda: f)
        monkeypatch.setattr("cora.channel_archive.classify.PACE_S", 0.0)
        f.post_behaviour = lambda kw: api_error("ratelimited") if len(f.posts) >= 1 else None
        assert SCRIPT.main(["--apply", "--monthly"], now=t) == 1
        m = markers()[-1]
        assert m["ok"] is False and m["outcome"] == "month_partial" and "1 of 2" in m["detail"]
        assert SCRIPT.monthly_due(az(2026, 10, 6), st.fold(now=az(2026, 10, 6))) == (True, "due")

    def test_an_ask_card_does_not_count_as_the_monthly_one(self, fake):
        from cora.channel_archive import deliver
        deliver.deliver_proposal(trigger="ask", now=az(2026, 10, 5, 6, 0), sleep=no_sleep)
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 5)) == 0
        assert markers()[-1]["outcome"] == "delivered"

    def test_a_crashing_monthly_scan_writes_a_failed_marker_and_settles_its_stall(self, fake, monkeypatch):
        """D-051 r1 harness-isolation#2 + c1-monitor#0: a raise inside deliver_proposal
        is an undelivered month (ok=False month_undelivered + exit 1), and the crash
        path records scan_failed for the scan it started, so the stall settles."""
        def _boom(*a, **k):
            raise RuntimeError("scan blew up")
        monkeypatch.setattr("cora.channel_archive.scan.scan", _boom)
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 5)) == 1
        m = markers()[-1]
        assert m["ok"] is False and m["outcome"] == "month_undelivered"
        assert "crashed: RuntimeError" in m["detail"] and "scan blew up" not in m["detail"]
        evs = st.read_events()
        started = [e["scan_id"] for e in evs if e["event"] == "scan_started"]
        failed = [e["scan_id"] for e in evs if e["event"] == "scan_failed"]
        assert len(started) == 1 and failed == started
        from cora.channel_archive import monitor as mon
        from test_channel_archive_monitor import MonSlack
        out = mon.reconcile(MonSlack(), now=az(2026, 10, 5) + 2 * 3600, sleep=no_sleep)
        assert not any("never staged" in f for f in out["findings"]), out["findings"]

    def test_a_crashing_manual_scan_exits_1_and_records_scan_failed(self, fake, monkeypatch, capsys):
        def _boom(*a, **k):
            raise RuntimeError("scan blew up")
        monkeypatch.setattr("cora.channel_archive.scan.scan", _boom)
        assert SCRIPT.main(["--apply"], now=az(2026, 10, 5)) == 1
        assert "crashed" in capsys.readouterr().out and markers() == []
        assert [e["event"] for e in st.read_events()] == ["scan_started", "scan_failed"]

    def test_lane_off_is_an_ok_skip(self, fake, monkeypatch):
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "off")
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 5)) == 0
        assert markers()[-1]["outcome"] == "skipped:lane_off"


class TestManualAndClear:
    def test_manual_apply_delivers_without_a_marker(self, fake):
        assert SCRIPT.main(["--apply"], now=az(2026, 10, 5)) == 0
        assert fake.posts and markers() == []
        assert st.fold().proposals[st.fold().order[-1]].trigger == "manual"

    def test_clear_demotion_shows_then_clears_and_writes_no_marker(self, capsys):
        assert SCRIPT.main(["--clear-demotion"]) == 0
        assert "not demoted" in capsys.readouterr().out
        st.write_demotion({"channel_id": "C1", "archive_ts": "1790.5", "since": "2026-10-06",
                           "reason": "unattributed"}, dry_run=False)
        assert SCRIPT.main(["--clear-demotion"]) == 0
        assert "Re-run with --apply" in capsys.readouterr().out and policy.is_demoted()
        assert SCRIPT.main(["--clear-demotion", "--apply"]) == 0
        assert not policy.is_demoted() and st.read_ledger()[-1]["event"] == "acknowledged"
        assert st.read_ledger()[-1]["by"] == HARRISON and markers() == []


class TestRegistryRebaseline:
    """D-051 r1 registry-ops#1 + c1-false-inactive#2: one legitimate registry trim of
    more than 10% blinded the lane for good (a blind scan persists registry_count=0, so
    the floor never moved) with no way back but hand-editing the append-only store. The
    operator re-baseline (dry run prints; --apply appends a store event the fold honours)
    is that way back, and the blind copy names it."""

    def _stage_old_count(self, n=121):
        st.append_event("staged", proposal_id="chanarch-000000000001", trigger="monthly",
                        ts=az(2026, 9, 7), expires_ts=az(2026, 9, 21), rows=[], registry_count=n)

    def _fixture_count(self):
        from cora.channel_archive import registry as reg
        return reg.load_registry().count

    def test_a_trimmed_registry_is_blind_then_rebaselined_then_sighted(self, fake, capsys):
        n = self._fixture_count()
        self._stage_old_count(n + 13)                        # a > 10% shrink since the last good read
        SCRIPT.main([], now=az(2026, 10, 5))
        out = capsys.readouterr().out
        assert "BLIND: registry_unreadable" in out and "--rebaseline-registry --apply" in out
        before = len(st.read_events())
        assert SCRIPT.main(["--rebaseline-registry"], now=az(2026, 10, 5)) == 0
        out = capsys.readouterr().out
        assert f"{n} ids" in out and f"{n + 13}" in out and "--apply" in out
        assert len(st.read_events()) == before                # a dry run writes nothing
        assert SCRIPT.main(["--rebaseline-registry", "--apply"], now=az(2026, 10, 5)) == 0
        out = capsys.readouterr().out
        assert "RE-BASELINED" in out
        # D-051 r2 c1-monitor#4: the success line names the step that clears the blind card
        assert "fresh scan" in out and "archive the dead channels" in out
        ev = st.read_events()[-1]
        assert ev["event"] == "registry_rebaselined" and ev["registry_count"] == n
        assert ev["previous"] == n + 13 and ev["by"] == HARRISON
        assert st.fold(now=az(2026, 10, 5)).last_registry_count == n
        SCRIPT.main([], now=az(2026, 10, 5))
        out = capsys.readouterr().out
        assert "BLIND" not in out and "SECTION A candidates (1): C0CANDID01" in out
        assert markers() == []                                # an operator command writes no marker

    def test_a_structurally_incomplete_registry_is_refused(self, fake, monkeypatch, tmp_path, capsys):
        from _chanarch_fakes import REGISTRY_FIXTURE
        text = REGISTRY_FIXTURE.read_text(encoding="utf-8")
        cut = tmp_path / "registry.md"
        cut.write_text(text[: len(text) // 2], encoding="utf-8")
        monkeypatch.setenv("CORA_CHANNEL_REGISTRY_PATH", str(cut))
        self._stage_old_count()
        before = len(st.read_events())
        assert SCRIPT.main(["--rebaseline-registry", "--apply"], now=az(2026, 10, 5)) == 1
        assert "REFUSED" in capsys.readouterr().out and len(st.read_events()) == before

    def test_the_blind_card_names_the_shrink_and_the_command(self, fake):
        from cora.channel_archive import deliver
        self._stage_old_count(self._fixture_count() + 13)
        out = deliver.deliver_proposal(trigger="ask", now=az(2026, 10, 5), sleep=no_sleep)
        assert out["delivered"] and out["blind"] == "registry_unreadable"
        body = json.dumps(fake.posts[-1]["blocks"], ensure_ascii=False)
        assert "fewer than 90%" in body and "--rebaseline-registry --apply" in body
        assert "could not be read completely" not in body

    def test_a_later_good_scan_still_moves_the_baseline(self):
        st.append_event("registry_rebaselined", registry_count=108, by=HARRISON, previous=140, ts=1.0)
        st.append_event("staged", proposal_id="chanarch-000000000002", ts=2.0, rows=[], registry_count=112)
        assert st.fold(now=3.0).last_registry_count == 112


def test_clear_demotion_names_and_acks_every_listed_event(capsys):
    """D-051 r1 c1-monitor#2: the preview names every unattributed event and --apply
    acknowledges each one (and prints them all)."""
    st.write_demotion({"since": "2026-10-06", "channel_id": "C0ROGUE001", "archive_ts": "1790.5",
                       "reason": "unattributed",
                       "events": [{"channel_id": "C0ROGUE001", "archive_ts": "1790.5"},
                                  {"channel_id": "C0ROGUE002", "archive_ts": "1791.5"}]},
                      dry_run=False)
    assert SCRIPT.main(["--clear-demotion"]) == 0
    out = capsys.readouterr().out
    assert "C0ROGUE001" in out and "C0ROGUE002" in out and "2 unattributed" in out
    assert SCRIPT.main(["--clear-demotion", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "CLEARED" in out and "C0ROGUE001" in out and "C0ROGUE002" in out
    acks = [(r["channel_id"], r["archive_ts"]) for r in st.read_ledger() if r["event"] == "acknowledged"]
    assert acks == [("C0ROGUE001", "1790.5"), ("C0ROGUE002", "1791.5")] and not policy.is_demoted()


def test_the_script_never_archives_or_imports_the_bot():
    import ast
    tree = ast.parse((REPO / "scripts" / "run_channel_archive_proposal.py").read_text(encoding="utf-8"))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    mods = {getattr(n, "module", None) for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "conversations_archive" not in attrs and "cora.app" not in mods
    assert "run_marker.write(" in (REPO / "scripts" / "run_channel_archive_proposal.py").read_text(encoding="utf-8")
