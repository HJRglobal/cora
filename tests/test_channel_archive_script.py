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
        assert SCRIPT.main(["--apply", "--monthly"], now=az(2026, 10, 12)) == 0   # retried next week
        assert markers()[-1]["outcome"] == "delivered"

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
