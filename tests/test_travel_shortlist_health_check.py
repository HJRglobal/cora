"""Code #16 C2 -- check_travel_shortlist, the travel lane's failing-capable monitor
(B6: "a monitor that cannot fail on the regression it guards is not a monitor").

It reads the lane's thread store (structured fields + events, never raw text) and
must be able to FAIL: on any belt refusal in 7 days (the lane's own egress
regression), on a search-failure share above 50% of >= 4 asks, on unreadable
lines, and on an unreadable store (blind never reads clean). It writes nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import nightly_health_check as hc
from cora import travel_shortlist as ts

NOW = datetime(2026, 9, 25, 8, 45, tzinfo=timezone(timedelta(hours=-7)))
CH = "C0C145H84JZ"


def _ev(event, days_ago=0, **kw):
    ts.append_event(event, channel=CH, root_ts=f"17900.{event}.{days_ago}", now=NOW - timedelta(days=days_ago), **kw)


class TestCheckTravelShortlist:
    def test_absent_store_is_info(self):
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "ok" and r.detail.startswith("INFO: no lodging asks yet")

    def test_a_clean_week_reads_ok_with_positive_coverage_counts(self):
        for i in range(3):
            _ev("asked", i)
            _ev("posted", i, options=4)
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "ok"
        assert r.detail == "7d: 3 ask(s), 3 card(s) posted, 0 search failure(s), 0 belt refusal(s)"

    def test_any_belt_refusal_in_seven_days_warns(self):
        _ev("asked")
        _ev("belt_refused", 2, reason="word_token")
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "warn" and "1 belt refusal(s)" in r.detail
        assert "word_token" not in r.detail          # counts only, never content

    def test_an_old_belt_refusal_ages_out(self):
        _ev("belt_refused", 8, reason="word_token")
        _ev("asked")
        assert hc.check_travel_shortlist(now=NOW).status == "ok"

    @pytest.mark.parametrize("asks,failed,status", [
        (4, 3, "warn"), (4, 2, "ok"), (3, 3, "ok"), (6, 4, "warn"), (10, 5, "ok")])
    def test_search_failure_share(self, asks, failed, status):
        for i in range(asks):
            _ev("asked", 0)
        for i in range(failed):
            _ev("search_failed", 0, error="api_error:APITimeoutError")
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == status, r.detail

    def test_unreadable_lines_warn(self):
        _ev("asked")
        with open(ts.threads_path(), "a", encoding="utf-8") as fh:
            fh.write("{torn line\n")
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "warn" and "1 unreadable store line(s)" in r.detail

    def test_a_blind_read_never_reads_clean(self, monkeypatch, tmp_path):
        d = tmp_path / "store-is-a-directory.jsonl"
        d.mkdir()
        monkeypatch.setenv("CORA_TRAVEL_SHORTLIST_THREADS_PATH", str(d))
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "warn" and "UNREADABLE" in r.detail

    def test_a_summary_crash_warns(self, monkeypatch):
        monkeypatch.setattr(ts, "threads_summary", lambda now=None: (_ for _ in ()).throw(ValueError("x")))
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "warn" and "blind" in r.detail

    def test_it_writes_nothing(self):
        _ev("asked")
        _ev("belt_refused", reason="content")
        p = ts.threads_path()
        before = (p.read_bytes(), sorted(x.name for x in p.parent.iterdir()))
        hc.check_travel_shortlist(now=NOW)
        assert (p.read_bytes(), sorted(x.name for x in p.parent.iterdir())) == before

    def test_it_is_one_check_result_registered_in_main(self):
        import ast
        import inspect
        assert isinstance(hc.check_travel_shortlist(now=NOW), hc.CheckResult)
        tree = ast.parse(inspect.getsource(hc.main))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "check_travel_shortlist"]
        assert len(calls) == 1
        parent_appends = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                          and getattr(n.func, "attr", "") == "append"
                          and n.args and n.args[0] is calls[0]]
        assert len(parent_appends) == 1           # append, never extend (one CheckResult)
