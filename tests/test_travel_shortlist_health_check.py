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


def _ev(event, days_ago=0, root=None, hours_ago=0, **kw):
    """One store row. An ask and its outcome share a thread root (``root``), the way
    execute_route and _search_job key them."""
    ts.append_event(event, channel=CH, root_ts=root if root is not None else f"17900.{event}.{days_ago}",
                    now=NOW - timedelta(days=days_ago, hours=hours_ago), **kw)


class TestCheckTravelShortlist:
    def test_absent_store_is_info(self):
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "ok" and r.detail.startswith("INFO: no lodging asks yet")

    def test_a_clean_week_reads_ok_with_positive_coverage_counts(self):
        for i in range(3):
            _ev("asked", i, root=f"r{i}")
            _ev("posted", i, root=f"r{i}", options=4)
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
            _ev("asked", 0, root=f"r{i}", hours_ago=2)
        for i in range(asks):
            if i < failed:
                _ev("search_failed", 0, root=f"r{i}", hours_ago=2, error="api_error:APITimeoutError")
            else:
                _ev("posted", 0, root=f"r{i}", hours_ago=2, options=3)
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

    # ── D-051 r1 c2-webcall#0: the delivery failures the monitor used to read as OK ──

    def test_any_card_post_failure_warns(self):
        for i in range(5):
            _ev("asked", 0, root=f"r{i}", hours_ago=3)
            _ev("post_failed", 0, root=f"r{i}", hours_ago=3, error="SlackApiError", searches=4)
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "warn" and "5 card post failure(s) -- a finished search's card" in r.detail

    def test_an_ask_that_never_settles_warns_after_an_hour(self):
        _ev("asked", 0, root="r-dead", hours_ago=2)           # the bot restarted mid-search
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "warn" and "1 ask(s) never settled" in r.detail
        assert "1 unsettled ask(s)" in r.detail

    def test_an_ask_still_inside_the_hour_is_not_yet_unsettled(self):
        ts.append_event("asked", channel=CH, root_ts="r-live", now=NOW - timedelta(minutes=30))
        assert hc.check_travel_shortlist(now=NOW).status == "ok"

    def test_an_ask_killed_before_its_job_ran_warns_through_the_real_execute_route(self):
        """The real path: execute_route writes the asked row, the pool loses the job (a
        restart kills the queue) -- nothing else is ever written for that ask."""
        from unittest.mock import MagicMock
        from test_travel_shortlist import _constraints, _slack_client
        ts.execute_route(ts.Route("search", constraints=_constraints(), budget=4), channel_id=CH,
                         thread_root_ts="1790000000.000990", entity="FNDR", user_id="U0B2RM2JYJ1",
                         client=_slack_client(), say=MagicMock(), submit=lambda *a, **k: True,
                         now=NOW - timedelta(hours=2))
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "warn" and "never settled" in r.detail

    def test_follow_up_asks_in_one_thread_pair_in_order(self):
        _ev("asked", 0, root="R", hours_ago=5)
        _ev("posted", 0, root="R", hours_ago=5, options=3)
        _ev("asked", 0, root="R", hours_ago=3)                # the lane-thread re-search
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "warn" and "1 ask(s) never settled" in r.detail
        _ev("posted", 0, root="R", hours_ago=3, options=2)
        assert hc.check_travel_shortlist(now=NOW).status == "ok"

    def test_a_lane_that_refuses_everything_warns_not_no_asks_yet(self):
        """Gate refusals are ledgered by the REAL execute_route -- a lane whose web tools
        are off (or whose caps are full) no longer reads 'no asks yet'."""
        from unittest.mock import MagicMock
        from test_travel_shortlist import _slack_client
        for reason, reply in (("web_off", ts.WEB_OFF_REPLY), ("daily_cap", ts.CAP_REPLY),
                              ("web_off", ts.WEB_OFF_REPLY)):
            ts.execute_route(ts.Route("reply", reply, reason), channel_id=CH,
                             thread_root_ts="1790000000.000991", entity="FNDR", user_id="U0B2RM2JYJ1",
                             client=_slack_client(), say=MagicMock(), submit=lambda *a, **k: True,
                             now=NOW - timedelta(hours=1))
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "warn"
        assert "3 refusal(s) (daily_cap 1, web_off 2)" in r.detail
        assert "the lane refused 3 ask(s) and posted no card" in r.detail
        assert "INFO" not in r.detail

    def test_refusals_beside_delivered_cards_read_ok_but_model_unsupported_warns(self):
        _ev("asked", 0, root="r1", hours_ago=2)
        _ev("posted", 0, root="r1", hours_ago=2, options=3)
        ts.record_gate_refusal(ts.Route("reply", ts.CAP_REPLY, "daily_cap"), channel_id=CH, now=NOW)
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "ok" and "1 refusal(s) (daily_cap 1)" in r.detail
        ts.record_gate_refusal(ts.Route("reply", ts.MODEL_REPLY, "model_unsupported"), channel_id=CH, now=NOW)
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "warn" and "1 model_unsupported refusal(s)" in r.detail

    def test_a_job_time_refusal_settles_its_ask_and_a_gate_refusal_settles_nothing(self):
        _ev("asked", 0, root="r-capped", hours_ago=2)
        _ev("refused", 0, root="r-capped", hours_ago=2, stage="job", reason="daily_cap", searches=0)
        _ev("asked", 0, root="", hours_ago=2)                 # an ask whose ack failed (no root)
        ts.record_gate_refusal(ts.Route("reply", ts.WEB_OFF_REPLY, "web_off"), channel_id=CH,
                               now=NOW - timedelta(minutes=90))
        s = ts.threads_summary(now=NOW)
        assert s["unsettled"] == 1                              # only the rootless ask
        assert s["refused"] == 2 and s["refused_reasons"] == {"daily_cap": 1, "web_off": 1}

    def test_a_catch_up_replay_row_never_warns(self):
        """D-051 r2 c2-webcall#0: EVAL_MODE is set only by a reconstruction (the
        missed-message catch-up), which now writes nothing; a legacy 'eval' gate row
        (the r1 fix range wrote them) is never counted as a lane refusal, so a replay
        can never make the lane read 'not serving lodging asks'."""
        for _ in range(3):
            _ev("refused", 0, root="", hours_ago=1, stage="gate", reason="eval")
        s = ts.threads_summary(now=NOW)
        assert s["refused"] == 0 and s["refused_reasons"] == {} and s["eval_replays"] == 3
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "ok", r.detail
        assert "the lane refused" not in r.detail and "refusal(s) (" not in r.detail
        assert "3 catch-up replay row(s) ignored" in r.detail
        ts.record_gate_refusal(ts.Route("reply", ts.WEB_OFF_REPLY, "web_off"), channel_id=CH, now=NOW)
        r = hc.check_travel_shortlist(now=NOW)          # a LIVE refusal still warns
        assert r.status == "warn" and "the lane refused 1 ask(s) and posted no card in 7d (web_off 1)" in r.detail

    def test_every_card_unverified_warns(self):
        for i in range(2):
            _ev("asked", 0, root=f"r{i}", hours_ago=2)
            _ev("posted", 0, root=f"r{i}", hours_ago=2, options=0, dropped=3)
        r = hc.check_travel_shortlist(now=NOW)
        assert r.status == "warn" and "every card in 7d (2) showed only 'No listing I could verify'" in r.detail
        _ev("asked", 0, root="r9", hours_ago=2)
        _ev("posted", 0, root="r9", hours_ago=2, options=4, dropped=1)
        assert hc.check_travel_shortlist(now=NOW).status == "ok"

    def test_a_gate_refusal_never_registers_a_lane_thread(self):
        ts.record_gate_refusal(ts.Route("reply", ts.CAP_REPLY, "daily_cap"), channel_id=CH, now=NOW)
        (row,) = [__import__("json").loads(l) for l in ts.threads_path().read_text(encoding="utf-8").splitlines()]
        assert row["root_ts"] == "" and row["stage"] == "gate" and row["reason"] == "daily_cap"
        assert set(row) == {"ts", "event", "channel", "root_ts", "stage", "reason"}   # shape only
        assert not ts.is_lane_thread(CH, "1790000000.000991")

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
