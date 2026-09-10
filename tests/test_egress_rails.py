"""Egress-rail observe-week counts (Code #12 S3'(a), cq-deca62a00719).

The enforce-flip reminder must read BOTH seam rails -- `sentinel-egress-leak`
(session #11 S1) and `phantom-write-claim` (Code #12 S2') -- side by side, from the
bot's own logs, over 24h and 7d. The 8/30 -> 9/6 week was called clean on the first
key alone while a phantom-write incident happened on day 3 (decisions.md 2026-09-03).

Contract under test:
  * a FIRING line counts; a bare mention does not; an unstamped line does not;
  * the 24h and 7d windows are per-line stamps, not file mtimes alone;
  * both health surfaces (nightly check + Monday digest) read the same numbers and
    say "criterion MET" only when BOTH rails read zero for 7d;
  * the nightly check WARNs (never critical) while the week is not clean, and WARNs
    when it cannot count (blind never renders as clean).
"""

from __future__ import annotations

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


@pytest.fixture
def logs(tmp_path):
    now = datetime.now()
    lines = [
        # firing lines inside 24h
        f"{_stamp(now - timedelta(hours=2))} WARNING cora.slack_egress phantom-write-claim kind=lexicon phrase='queued' mode=observe channel=#dm user=U1",
        f"{_stamp(now - timedelta(hours=3))} WARNING cora.slack_egress phantom-write-claim kind=fabricated-id id=cq-e7f2a4c91b2e ledger=cq mode=observe channel=#dm user=U1",
        f"{_stamp(now - timedelta(hours=5))} WARNING cora.slack_egress sentinel-egress-leak mode=observe removed=15 chars -- a write-tool directive reached the outbound seam",
        # firing lines inside 7d but outside 24h
        f"{_stamp(now - timedelta(days=3))} WARNING cora.slack_egress phantom-write-claim kind=lexicon phrase='is live' mode=observe channel=#f3e-sales user=U2",
        # firing line OUTSIDE 7d -- must not count even though the file is fresh
        f"{_stamp(now - timedelta(days=9))} WARNING cora.slack_egress phantom-write-claim kind=lexicon phrase='deleted' mode=observe channel=#dm user=U1",
        # a MENTION, not a firing line (a tool description echoed into a log)
        f"{_stamp(now - timedelta(hours=1))} INFO cora.tools the phantom-write-claim screen and the sentinel-egress-leak scrub share one flag",
        # an unstamped continuation line carrying the key
        "    phantom-write-claim kind=lexicon (traceback continuation)",
    ]
    (tmp_path / "cora-2026-09-08.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # a rotated sibling with one more 7d hit, script-format stamp
    (tmp_path / "cora-2026-09-01.log.2026-09-05").write_text(
        f"{(now - timedelta(days=4)).strftime('%Y-%m-%d %H:%M:%S')},120 [WARNING] sentinel-egress-leak mode=observe removed=3 chars\n",
        encoding="utf-8")
    # a non-bot log that must be ignored entirely
    (tmp_path / "health-check-2026-09-08.log").write_text(
        f"{_stamp(now - timedelta(hours=1))} INFO phantom-write-claim kind=lexicon phrase='x' mode=observe\n",
        encoding="utf-8")
    return tmp_path


class TestCount:
    def test_24h_and_7d_windows_count_firing_lines_only(self, logs):
        now = datetime.now()
        c24 = er.count_rail_hits(now - timedelta(hours=26), log_dir=logs)
        assert c24 == {er.RAIL_SENTINEL: 1, er.RAIL_PHANTOM: 2}
        c7 = er.count_rail_hits(now - timedelta(days=7), log_dir=logs)
        assert c7 == {er.RAIL_SENTINEL: 2, er.RAIL_PHANTOM: 3}

    def test_missing_log_dir_counts_zero(self, tmp_path):
        assert er.count_rail_hits(datetime.now(), log_dir=tmp_path / "nope") == {
            er.RAIL_SENTINEL: 0, er.RAIL_PHANTOM: 0}

    def test_observe_week_read_not_clean(self, logs, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        read = er.observe_week_read(log_dir=logs)
        assert read["mode"] == "observe" and read["clean_7d"] is False
        assert "NOT clean" in read["flip_criterion"]
        assert "gated" in read["flip_criterion"]
        line = er.format_line(read)
        assert "sentinel-egress-leak 1 (24h) / 2 (7d)" in line
        assert "phantom-write-claim 2 (24h) / 3 (7d)" in line

    def test_observe_week_read_clean(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        (tmp_path / "cora-2026-09-08.log").write_text("nothing here\n", encoding="utf-8")
        read = er.observe_week_read(log_dir=tmp_path)
        assert read["clean_7d"] is True
        assert "MET" in read["flip_criterion"]
        assert "restart" in read["flip_criterion"]  # a flip is .env + restart, said plainly

    def test_enforce_mode_is_named(self, logs, monkeypatch):
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        read = er.observe_week_read(log_dir=logs)
        assert read["mode"] == "enforce" and "ENFORCE" in read["flip_criterion"]

    def test_unknown_flag_value_reads_observe(self, monkeypatch):
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "true")
        assert er.sentinel_mode() == "observe"


class TestNightlyCheck:
    def test_warns_while_not_clean(self, logs, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        monkeypatch.setattr(er, "DEFAULT_LOG_DIR", logs)
        r = hc.check_egress_rails()
        assert r.status == "warn"
        assert "sentinel-egress-leak" in r.detail and "phantom-write-claim" in r.detail
        assert "NOT clean" in r.detail

    def test_ok_with_criterion_met_when_both_zero(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        (tmp_path / "cora-x.log").write_text("quiet\n", encoding="utf-8")
        monkeypatch.setattr(er, "DEFAULT_LOG_DIR", tmp_path)
        r = hc.check_egress_rails()
        assert r.status == "ok" and "MET" in r.detail

    def test_counting_failure_warns_never_ok(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("disk")
        monkeypatch.setattr(er, "observe_week_read", _boom)
        r = hc.check_egress_rails()
        assert r.status == "warn" and "Could not count" in r.detail

    def test_registered_in_main(self):
        import inspect
        src = inspect.getsource(hc)
        body = src[src.index("def main("):]
        assert "all_results.append(check_egress_rails())" in body


class TestWeeklyDigest:
    def test_alarm_and_slack_line_read_both_rails(self, logs, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        monkeypatch.setattr(er, "DEFAULT_LOG_DIR", logs)
        section = chr_.egress_rails_section()
        assert section["available"] is True and section["clean_7d"] is False
        report = {"egress_rails": section}
        alarms = chr_.threshold_alarms(report)
        assert any("EGRESS RAILS" in a and "phantom-write-claim 3" in a
                   and "sentinel-egress-leak 2" in a for a in alarms)
        msg = chr_.format_slack({**report, "alarms": alarms})
        assert "*Egress rails*" in msg and "observe week NOT clean" in msg

    def test_clean_week_has_no_alarm_and_says_met(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
        (tmp_path / "cora-x.log").write_text("quiet\n", encoding="utf-8")
        monkeypatch.setattr(er, "DEFAULT_LOG_DIR", tmp_path)
        section = chr_.egress_rails_section()
        assert not [a for a in chr_.threshold_alarms({"egress_rails": section}) if "EGRESS" in a]
        assert "flip criterion MET" in chr_.format_slack({"egress_rails": section, "alarms": []})

    def test_section_failure_is_soft(self, monkeypatch):
        monkeypatch.setattr(er, "observe_week_read", lambda *a, **k: 1 / 0)
        s = chr_.egress_rails_section()
        assert s["available"] is False and "division" in s["reason"]
