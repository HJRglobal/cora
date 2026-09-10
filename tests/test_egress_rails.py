"""Egress-rail observe-week counts (Code #12 S3'(a), cq-deca62a00719).

The enforce-flip reminder must read BOTH seam rails -- `sentinel-egress-leak`
(session #11 S1) and `phantom-write-claim` (Code #12 S2') -- side by side, from the
bot's own logs, over 24h and 7d. The 8/30 -> 9/6 week was called clean on the first
key alone while a phantom-write incident happened on day 3 (decisions.md 2026-09-03).

Contract under test (D-051 lens A MED #3 / lens E F2+F5 folded in, 2026-09-09):
  * a FIRING line in the BOT's own format from cora.slack_egress counts; a bare
    mention does not; an unstamped line does not; a SCRIPT-format line does not
    (twelve scheduled scripts share the same file); another logger's line does not;
  * the 24h and 7d windows are per-line stamps, not file mtimes alone;
  * a zero count is CLEAN only with positive coverage: the rails recorded as armed
    (record_armed, written by the bot at startup) at/before the window start AND at
    least one bot log scanned -- an unarmed or unscanned window is "not a clean
    read", never "criterion MET" (silence is not safety);
  * first_armed_at survives a restart while the rail set is unchanged, and resets
    when it changes;
  * both health surfaces (nightly check + Monday digest) read the same numbers and
    say "criterion MET" only when all of the above hold;
  * the nightly check WARNs (never critical) while the week is not clean, and WARNs
    when it cannot count (blind never renders as clean).
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from cora import egress_rails as er

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nightly_health_check as hc  # noqa: E402
import cora_health_report as chr_  # noqa: E402


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _bot(dt: datetime, level: str, msg: str, logger: str = "cora.slack_egress") -> str:
    return f"{_stamp(dt)} {level} [ThreadPoolExecutor-0_0] {logger}: {msg}"


@pytest.fixture
def logs(tmp_path):
    now = datetime.now()
    lines = [
        # firing lines inside 24h (bot format, the rails' own logger)
        _bot(now - timedelta(hours=2), "WARNING", "phantom-write-claim kind=lexicon phrase='queued' mode=observe channel=#dm user=U1"),
        _bot(now - timedelta(hours=3), "WARNING", "phantom-write-claim kind=fabricated-id id=cq-e7f2a4c91b2e ledger=cq mode=observe channel=#dm user=U1"),
        _bot(now - timedelta(hours=5), "WARNING", "sentinel-egress-leak mode=observe removed=15 chars -- a write-tool directive reached the outbound seam"),
        # firing line inside 7d but outside 24h
        _bot(now - timedelta(days=3), "WARNING", "phantom-write-claim kind=lexicon phrase='is live' mode=observe channel=#f3e-sales user=U2"),
        # firing line OUTSIDE 7d -- must not count even though the file is fresh
        _bot(now - timedelta(days=9), "WARNING", "phantom-write-claim kind=lexicon phrase='deleted' mode=observe channel=#dm user=U1"),
        # a MENTION, not a firing line (a tool description echoed into a log)
        _bot(now - timedelta(hours=1), "INFO", "the phantom-write-claim screen and the sentinel-egress-leak scrub share one flag", logger="cora.tools"),
        # an unstamped continuation line carrying the key
        "    phantom-write-claim kind=lexicon (traceback continuation)",
        # SCRIPT-format lines in the same file (lens E F5): never counted
        f"{(now - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')},120 [WARNING] cora.slack_egress: sentinel-egress-leak mode=observe removed=3 chars",
        f"{(now - timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')},121 [INFO] nightly_health_check: Egress rails (observe week) sentinel-egress-leak 0 (24h) / 0 (7d) | phantom-write-claim 0 (24h) / 0 (7d)",
        # firing SHAPE from another logger (a test's real WARNING routed through a root handler)
        _bot(now - timedelta(hours=1), "WARNING", "phantom-write-claim kind=lexicon phrase='x' mode=observe", logger="tests.test_phantom_write_claim"),
    ]
    (tmp_path / "cora-2026-09-08.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # a rotated sibling with one more 7d hit (bot format)
    (tmp_path / "cora-2026-09-01.log.2026-09-05").write_text(
        _bot(now - timedelta(days=4), "WARNING", "sentinel-egress-leak mode=observe removed=3 chars") + "\n",
        encoding="utf-8")
    # a non-bot log that must be ignored entirely
    (tmp_path / "health-check-2026-09-08.log").write_text(
        _bot(now - timedelta(hours=1), "WARNING", "phantom-write-claim kind=lexicon phrase='x' mode=observe") + "\n",
        encoding="utf-8")
    return tmp_path


def _armed(tmp_path: Path, *, days_ago: float, rails=er.RAIL_KEYS) -> Path:
    p = tmp_path / "armed.json"
    first = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    p.write_text(json.dumps({"rails": sorted(rails), "first_armed_at": first,
                             "last_armed_at": first, "pid": 1}), encoding="utf-8")
    return p


def _quiet_bot_log(tmp_path: Path) -> None:
    (tmp_path / "cora-x.log").write_text(
        _bot(datetime.now() - timedelta(hours=1), "INFO", "heartbeat alive", logger="cora.main") + "\n",
        encoding="utf-8")


class TestCount:
    def test_24h_and_7d_windows_count_bot_firing_lines_only(self, logs):
        now = datetime.now()
        c24 = er.count_rail_hits(now - timedelta(hours=26), log_dir=logs)
        assert c24 == {er.RAIL_SENTINEL: 1, er.RAIL_PHANTOM: 2}
        c7 = er.count_rail_hits(now - timedelta(days=7), log_dir=logs)
        assert c7 == {er.RAIL_SENTINEL: 2, er.RAIL_PHANTOM: 3}
        scan = er.scan_rail_hits(now - timedelta(days=7), log_dir=logs)
        assert scan["files_scanned"] == 2 and scan["bot_lines_in_window"] == 5

    def test_missing_log_dir_counts_zero_and_scans_nothing(self, tmp_path):
        assert er.count_rail_hits(datetime.now(), log_dir=tmp_path / "nope") == {
            er.RAIL_SENTINEL: 0, er.RAIL_PHANTOM: 0}
        assert er.scan_rail_hits(datetime.now(), log_dir=tmp_path / "nope")["files_scanned"] == 0

    def test_observe_week_read_not_clean(self, logs, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        read = er.observe_week_read(log_dir=logs, armed_path=_armed(logs, days_ago=8))
        assert read["mode"] == "observe" and read["clean_7d"] is False
        assert "NOT clean" in read["flip_criterion"]
        assert "gated" in read["flip_criterion"]
        line = er.format_line(read)
        assert "sentinel-egress-leak 1 (24h) / 2 (7d)" in line
        assert "phantom-write-claim 2 (24h) / 3 (7d)" in line
        assert "armed since" in line and "2 bot log(s) scanned" in line

    def test_enforce_mode_is_named(self, logs, monkeypatch):
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        read = er.observe_week_read(log_dir=logs)
        assert read["mode"] == "enforce" and "ENFORCE" in read["flip_criterion"]

    def test_unknown_flag_value_reads_observe(self, monkeypatch):
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "true")
        assert er.sentinel_mode() == "observe"


class TestCoverage:
    """Zero is clean only with positive coverage (lens A MED #3 / lens E F2)."""

    def test_unarmed_quiet_window_is_not_a_clean_read(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        _quiet_bot_log(tmp_path)
        read = er.observe_week_read(log_dir=tmp_path, armed_path=tmp_path / "no-armed.json")
        assert all(v == 0 for v in read["counts_7d"].values())
        assert read["clean_7d"] is False and read["coverage"]["armed"] is False
        assert "never armed" in read["flip_criterion"] and "MET" not in read["flip_criterion"]
        assert "rails NOT armed" in er.format_line(read)

    def test_armed_only_two_days_is_not_a_clean_read(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        _quiet_bot_log(tmp_path)
        read = er.observe_week_read(log_dir=tmp_path, armed_path=_armed(tmp_path, days_ago=2))
        assert read["clean_7d"] is False and read["coverage"]["covers_window"] is False
        assert "armed only since" in read["flip_criterion"]

    def test_armed_full_window_with_no_bot_log_is_not_a_clean_read(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        read = er.observe_week_read(log_dir=tmp_path, armed_path=_armed(tmp_path, days_ago=8))
        assert read["clean_7d"] is False and read["coverage"]["files_scanned"] == 0
        assert "no bot log file" in read["flip_criterion"]

    def test_armed_full_window_quiet_bot_log_is_clean(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        _quiet_bot_log(tmp_path)
        read = er.observe_week_read(log_dir=tmp_path, armed_path=_armed(tmp_path, days_ago=8))
        assert read["clean_7d"] is True
        assert "MET" in read["flip_criterion"] and "armed for the whole window" in read["flip_criterion"]
        assert "restart" in read["flip_criterion"]  # a flip is .env + restart, said plainly

    def test_record_armed_keeps_first_armed_at_across_restarts_and_resets_on_a_new_rail(self, tmp_path):
        p = tmp_path / "armed.json"
        t0 = datetime(2026, 9, 1, 8, 0, 0)
        rec1 = er.record_armed(path=p, now=t0)
        assert rec1["first_armed_at"] == rec1["last_armed_at"] == t0.isoformat(timespec="seconds")
        rec2 = er.record_armed(path=p, now=t0 + timedelta(days=3))
        assert rec2["first_armed_at"] == t0.isoformat(timespec="seconds")          # kept
        assert rec2["last_armed_at"] == (t0 + timedelta(days=3)).isoformat(timespec="seconds")
        assert json.loads(p.read_text(encoding="utf-8"))["first_armed_at"] == rec2["first_armed_at"]
        rec3 = er.record_armed(path=p, now=t0 + timedelta(days=4), rails=er.RAIL_KEYS + ("new-rail",))
        assert rec3["first_armed_at"] == (t0 + timedelta(days=4)).isoformat(timespec="seconds")  # reset

    def test_unreadable_armed_record_reads_as_not_armed(self, tmp_path):
        p = tmp_path / "armed.json"
        p.write_text("{not json", encoding="utf-8")
        assert er.armed_state(p) is None

    def test_bot_records_armed_at_startup(self):
        from cora import main as cora_main
        src = inspect.getsource(cora_main.main)
        assert "egress_rails.record_armed(" in src
        assert src.index("instance_ledger.record_start(") < src.index("egress_rails.record_armed(")


class TestNightlyCheck:
    def test_warns_while_not_clean(self, logs, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        monkeypatch.setattr(er, "DEFAULT_LOG_DIR", logs)
        monkeypatch.setattr(er, "ARMED_STATE_PATH", _armed(logs, days_ago=8))
        r = hc.check_egress_rails()
        assert r.status == "warn"
        assert "sentinel-egress-leak" in r.detail and "phantom-write-claim" in r.detail
        assert "NOT clean" in r.detail

    def test_warns_when_quiet_but_unarmed(self, tmp_path, monkeypatch):
        """The live 9/9 reading: zero phantom lines ever written, check said ok."""
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        _quiet_bot_log(tmp_path)
        monkeypatch.setattr(er, "DEFAULT_LOG_DIR", tmp_path)
        monkeypatch.setattr(er, "ARMED_STATE_PATH", tmp_path / "no-armed.json")
        r = hc.check_egress_rails()
        assert r.status == "warn" and "never armed" in r.detail and "MET" not in r.detail

    def test_ok_with_criterion_met_when_both_zero_and_armed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        _quiet_bot_log(tmp_path)
        monkeypatch.setattr(er, "DEFAULT_LOG_DIR", tmp_path)
        monkeypatch.setattr(er, "ARMED_STATE_PATH", _armed(tmp_path, days_ago=8))
        r = hc.check_egress_rails()
        assert r.status == "ok" and "MET" in r.detail

    def test_counting_failure_warns_never_ok(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("disk")
        monkeypatch.setattr(er, "observe_week_read", _boom)
        r = hc.check_egress_rails()
        assert r.status == "warn" and "Could not count" in r.detail

    def test_registered_in_main(self):
        src = inspect.getsource(hc)
        body = src[src.index("def main("):]
        assert "all_results.append(check_egress_rails())" in body


class TestWeeklyDigest:
    def test_alarm_and_slack_line_read_both_rails(self, logs, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        monkeypatch.setattr(er, "DEFAULT_LOG_DIR", logs)
        monkeypatch.setattr(er, "ARMED_STATE_PATH", _armed(logs, days_ago=8))
        section = chr_.egress_rails_section()
        assert section["available"] is True and section["clean_7d"] is False
        report = {"egress_rails": section}
        alarms = chr_.threshold_alarms(report)
        assert any("EGRESS RAILS" in a and "phantom-write-claim 3" in a
                   and "sentinel-egress-leak 2" in a for a in alarms)
        msg = chr_.format_slack({**report, "alarms": alarms})
        assert "*Egress rails*" in msg and "observe week NOT clean" in msg

    def test_quiet_but_unarmed_week_alarms_and_never_says_met(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        _quiet_bot_log(tmp_path)
        monkeypatch.setattr(er, "DEFAULT_LOG_DIR", tmp_path)
        monkeypatch.setattr(er, "ARMED_STATE_PATH", tmp_path / "no-armed.json")
        section = chr_.egress_rails_section()
        assert section["clean_7d"] is False
        assert [a for a in chr_.threshold_alarms({"egress_rails": section}) if "EGRESS" in a]
        assert "flip criterion MET" not in chr_.format_slack({"egress_rails": section, "alarms": []})

    def test_clean_armed_week_has_no_alarm_and_says_met(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        _quiet_bot_log(tmp_path)
        monkeypatch.setattr(er, "DEFAULT_LOG_DIR", tmp_path)
        monkeypatch.setattr(er, "ARMED_STATE_PATH", _armed(tmp_path, days_ago=8))
        section = chr_.egress_rails_section()
        assert not [a for a in chr_.threshold_alarms({"egress_rails": section}) if "EGRESS" in a]
        assert "flip criterion MET" in chr_.format_slack({"egress_rails": section, "alarms": []})

    def test_section_failure_is_soft(self, monkeypatch):
        monkeypatch.setattr(er, "observe_week_read", lambda *a, **k: 1 / 0)
        s = chr_.egress_rails_section()
        assert s["available"] is False and "division" in s["reason"]


class TestWritePathIsolation:
    def test_armed_state_path_is_redirected_by_the_suite(self, tmp_path):
        assert er.ARMED_STATE_PATH == tmp_path / "egress-rails-armed.json"
