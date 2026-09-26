"""Code #16 C1 -- D-051 round-4 regressions for the dead-channel lane (fixer R4A).

  r4:c1-intents-copy#0 -- the round-3 "archived by <actor>" rule read an actor noun
      used as a MODIFIER ("monday's team call", "tomorrow's staff meeting") as the
      actor, and 'time' as filler ("by the time people got in"), so a deadline-framed
      lane-status question went to the zero-tool model. The actor noun must END the
      phrase (never a modifier of an event noun) and 'time' is never filler; a
      phrase-final actor ("by her team?", "by the team?", "by staff?", a Slack
      mention) still bails.
  r4:harness-isolation#0 -- every acquire_scan_lock None read as "another scan is
      running", so an UNWRITABLE lock (or a stale one that cannot be removed) turned
      the monthly lane's WARN into a green ``skipped:scan_running`` every Monday. Only
      a lock HELD by a live scan defers the month; a fault is ``scan_lock_error`` ->
      ok=False month_undelivered + exit 1 (A28), and the on-ask reply never says a
      scan is running.

Every test drives the real code path (the pure predicate, the real DM / @mention /
/cora-ask / catch-up entry points, the real store lock, the real deliver_proposal and
the real script main()); nothing here mocks the function under test.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import time
from unittest.mock import MagicMock

import pytest

import cora.app as app_module
import test_channel_archive_app_wiring as aw
import test_channel_archive_script as tsc
from _chanarch_fakes import HARRISON, NOW, FakeSlack, no_sleep
from cora import slack_egress as se
from cora.channel_archive import clients, deliver
from cora.channel_archive import intents as it
from cora.channel_archive import store as st
from test_channel_archive_app_wiring import dm  # noqa: F401 -- the real-DM fixture
from test_channel_archive_script import fake  # noqa: F401 -- the script's Slack fake

# ── r4:c1-intents-copy#0 ─────────────────────────────────────────────────────
#: the seven deadline rows that fired at 5ef8f93b and bailed after the round-3 actor rule
STATUS_FIRE_R4_REGRESS = [
    "did the dead channels get archived by monday's team call?",
    "were the dead channels archived by tomorrow's staff meeting?",
    "were the dead channels archived by friday's team meeting?",
    "were the dead channels archived by this week's team sync?",
    "were the dead channels archived by next week's staff meeting?",
    "were the dead channels archived by today's team huddle?",
    "were the dead channels archived by this team call?",
]
#: ... and the same class that bailed at base too (the adjudication's residual rows)
STATUS_FIRE_R4_RESIDUAL = [
    "were the dead channels archived by the time people got in?",
    "were the dead channels archived by the team meeting?",
    "were any channels archived by the time staff arrived?",
    "were the dead channels archived by our team sync?",
    "were the dead channels archived by the next team meeting?",
    "were the dead channels archived by the team's meeting?",
    "were the dead channels archived by the team deadline?",
    "were the dead channels archived by the staff standup?",
    "were the dead channels archived by the team offsite?",
]
STATUS_FIRE_R4_BY = STATUS_FIRE_R4_REGRESS + STATUS_FIRE_R4_RESIDUAL
#: a phrase-FINAL actor noun (or a person / mention) is still someone else's archive
STATUS_NOT_R4_BY = [
    "were the channels archived by her team?", "were the channels archived by the team?",
    "were the channels archived by staff?", "were any channels archived by <@U0B3VGWJTMJ>?",
    "were the channels archived by <@U0B3VGWJTMJ|alex>?", "were the channels archived by staff members?",
    "were the channels archived by team members?", "were the channels archived by the team yesterday?",
    "were the channels archived by the ops team?", "were the channels archived by the team's script?",
    "which channels were archived by the time-tracking script?", "were the channels archived by staff at lunch?",
    "were the channels archived by an admin after the call?", "were the channels archived by our team?",
    "were the channels archived by your team?", "were the channels archived by people in the meeting?",
    "were the channels archived by the admin team?",
]


class TestArchivedByModifierR4:
    @pytest.mark.parametrize("text", STATUS_FIRE_R4_BY)
    def test_a_deadline_named_by_a_team_or_staff_event_is_lane_status(self, text):
        assert not it._names_other_actor(text), text
        assert it.looks_like_archive_status(text, now=NOW), text

    @pytest.mark.parametrize("text", STATUS_NOT_R4_BY)
    def test_a_phrase_final_actor_still_bails(self, text):
        assert not it.looks_like_archive_status(text, now=NOW), text

    @pytest.mark.parametrize("text", STATUS_FIRE_R4_BY)
    def test_the_dm_question_gets_the_ledger_line_not_the_model(self, dm, text):  # noqa: F811
        client = MagicMock()
        app_module.handle_message_event(aw._event(text), client)
        texts = aw._texts(client)
        assert texts and texts[0].startswith("Dead-channel lane:"), (text, texts)
        assert not dm.qa.called and not dm.capture.called and not dm.scans, text

    @pytest.mark.parametrize("text", STATUS_FIRE_R4_REGRESS[:3] + STATUS_FIRE_R4_RESIDUAL[:2])
    def test_the_mention_and_slash_questions_get_the_ledger_line(self, text):
        client, dispatch, capture, start = aw.TestFounderMention()._run(text)
        assert client.chat_postMessage.call_args.kwargs["text"].startswith("Dead-channel lane:"), text
        assert not dispatch.called and not capture.called and not start.called, text
        client, dispatch, capture, start = aw.TestFounderSlashAskR1()._run(text)
        assert client.chat_postMessage.call_args.kwargs["text"].startswith("Dead-channel lane:"), text
        assert not dispatch.called and not capture.called and not start.called, text

    @pytest.mark.parametrize("text", STATUS_FIRE_R4_REGRESS[:3] + STATUS_FIRE_R4_RESIDUAL[:2])
    def test_the_catch_up_drafts_the_fixed_line(self, monkeypatch, text):
        from cora import missed_message_catchup as mmc
        monkeypatch.setattr(mmc, "HARRISON_ID", HARRISON)
        monkeypatch.setattr(mmc, "_run_dispatch_capture",
                            lambda *a, **k: pytest.fail("the model must not draft this"))
        out = mmc.generate_draft(MagicMock(), aw._cand(mmc, text, HARRISON))
        assert out.status == "draft" and out.draft_text == it.CATCHUP_DRAFT, text

    @pytest.mark.parametrize("text", ["were the channels archived by her team?",
                                      "were the channels archived by staff?"])
    def test_a_phrase_final_actor_question_still_reaches_the_model(self, dm, text):  # noqa: F811
        client = MagicMock()
        app_module.handle_message_event(aw._event(text), client)
        assert dm.qa.called and aw._texts(client) == [], text

    @pytest.mark.parametrize("raw", [
        " " * 40000,
        "archived by " + " " * 40000,
        "archived by " + "team " * 8000,
        "archived by the " + "monday's " * 4400 + "team",
        "archived by " + "time " * 8000 + "staff",
        "archived by " + "x " * 20000,
        "archived by the " + "team's " * 5700 + "meeting",
    ], ids=["spaces", "by-spaces", "team-run", "possessive-run", "time-run", "filler-run", "team's-run"])
    def test_the_changed_actor_regex_is_fast_on_40k(self, raw):
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            it._BY_ACTOR_RE.search(raw)
            best = min(best, time.perf_counter() - t0)
        assert best < 0.1, (raw[:40], best)


# ── r4:harness-isolation#0 ───────────────────────────────────────────────────
def _block_lock_parent(tmp_path, monkeypatch):
    """The lock path's parent is a regular FILE: the lock can never be created."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_SCAN_LOCK_PATH", str(blocker / "scan.lock"))


def _dir_at_lock_path(tmp_path, monkeypatch):
    """A (non-empty) DIRECTORY sits at the lock path: it can never be opened as the lock
    (Windows) or reads as a stale lock that can never be unlinked (POSIX)."""
    d = tmp_path / "lockdir"
    d.mkdir()
    (d / "keep").write_text("x", encoding="utf-8")
    monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_SCAN_LOCK_PATH", str(d))


def _stale_unremovable_lock(tmp_path, monkeypatch):
    """A STALE lock (1970 ts, a previous instance's nonce) that cannot be unlinked: a
    read-only leftover (Windows refuses to unlink it; on POSIX its directory is made
    read-only too)."""
    d = tmp_path / "ro"
    d.mkdir()
    p = d / "scan.lock"
    p.write_text(json.dumps({"pid": os.getpid(), "nonce": "a-previous-instance", "ts": 0,
                             "token": "t0"}), encoding="utf-8")
    os.chmod(p, stat.S_IREAD)
    if os.name != "nt":
        os.chmod(d, stat.S_IREAD | stat.S_IEXEC)
    monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_SCAN_LOCK_PATH", str(p))


_FAULTS = {"unwritable": _block_lock_parent, "dir-at-path": _dir_at_lock_path,
           "stale-unremovable": _stale_unremovable_lock}


class TestScanLockWhyR4:
    def test_a_live_holder_is_held(self):
        tok = st.acquire_scan_lock(now=NOW)
        assert tok
        assert st.acquire_scan_lock_why(now=NOW + 5) == (None, st.SCAN_LOCK_HELD)
        st.release_scan_lock(tok)
        tok2, why = st.acquire_scan_lock_why(now=NOW + 6)
        assert tok2 and why == ""
        st.release_scan_lock(tok2)

    def test_a_stale_lock_another_process_removed_first_is_retried_not_a_fault(self, monkeypatch):
        p = st.scan_lock_path()
        p.write_text(json.dumps({"pid": os.getpid(), "nonce": "a-previous-instance", "ts": 0,
                                 "token": "t0"}), encoding="utf-8")
        real_unlink = type(p).unlink

        def _lost_the_race(self, *a, **k):
            real_unlink(self, *a, **k)                  # the other process removed it ...
            raise FileNotFoundError(str(self))          # ... so ours finds nothing there
        with monkeypatch.context() as m:
            m.setattr(type(p), "unlink", _lost_the_race)
            tok, why = st.acquire_scan_lock_why(now=NOW)
        assert tok and why == "", why
        st.release_scan_lock(tok)
        assert not p.exists()

    @pytest.mark.parametrize("fault", sorted(_FAULTS))
    def test_a_lock_fault_is_not_a_running_scan(self, tmp_path, monkeypatch, caplog, fault):
        _FAULTS[fault](tmp_path, monkeypatch)
        caplog.set_level(logging.ERROR, logger=st.__name__)
        assert st.acquire_scan_lock_why(now=NOW) == (None, st.SCAN_LOCK_FAULT)
        assert st.acquire_scan_lock(now=NOW) is None                     # the old contract holds
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, fault
        want = {"unwritable": "scan lock unwritable",                    # the path each takes
                "stale-unremovable": "stale scan lock cannot be removed"}.get(fault)
        assert want is None or any(want in e for e in errors), (fault, errors)


class TestDeliverLockFaultR4:
    @pytest.fixture
    def slack(self, monkeypatch):
        f = FakeSlack()
        monkeypatch.setattr(clients, "read_client_factory", lambda: f)
        monkeypatch.setattr(clients, "write_client_factory", lambda: f)
        return f

    @pytest.mark.parametrize("fault", sorted(_FAULTS))
    def test_a_lock_fault_is_its_own_reason(self, tmp_path, monkeypatch, slack, fault):
        _FAULTS[fault](tmp_path, monkeypatch)
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        assert out["reason"] == "scan_lock_error" and not out["delivered"], out
        assert st.read_events() in ([], None) and not slack.posts

    def test_a_held_lock_is_still_scan_running(self, slack):
        tok = st.acquire_scan_lock(now=NOW)
        out = deliver.deliver_proposal(trigger="ask", now=NOW, sleep=no_sleep)
        assert out["reason"] == "scan_running" and not out["delivered"]
        st.release_scan_lock(tok)


class TestMonthlyLockFaultR4:
    """A28: a lock fault is an undelivered month, WARNed every Monday until fixed --
    never the green deferral a live holder earns."""

    @pytest.mark.parametrize("fault", sorted(_FAULTS))
    def test_a_lock_fault_is_a_failed_month_every_monday_until_fixed(self, tmp_path, monkeypatch,
                                                                    fake, capsys, fault):  # noqa: F811
        good = st.scan_lock_path()
        _FAULTS[fault](tmp_path, monkeypatch)
        for day in (5, 12):
            assert tsc.SCRIPT.main(["--apply", "--monthly"], now=tsc.az(2026, 10, day)) == 1, day
            m = tsc.markers()[-1]
            assert m["ok"] is False and m["outcome"] == "month_undelivered", m
            assert "scan_lock_error" in m["detail"] and "was running" not in m["detail"], m
            assert "FAILED" in capsys.readouterr().out
        assert not any(m["outcome"] == tsc.SCRIPT.DEFERRED_OUTCOME for m in tsc.markers())
        assert not fake.posts and st.read_events() in ([], None)
        assert tsc.SCRIPT.monthly_due(tsc.az(2026, 10, 19), st.fold(now=tsc.az(2026, 10, 19)),
                                      tsc.SCRIPT.run_marker.read_markers()) == (True, "due")
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_SCAN_LOCK_PATH", str(good))   # the fault fixed
        assert tsc.SCRIPT.main(["--apply", "--monthly"], now=tsc.az(2026, 10, 19)) == 0
        assert tsc.markers()[-1]["outcome"] == "delivered" and len(fake.posts) == 1

    def test_a_manual_run_with_a_lock_fault_exits_1(self, tmp_path, monkeypatch, fake, capsys):  # noqa: F811
        _block_lock_parent(tmp_path, monkeypatch)
        assert tsc.SCRIPT.main(["--apply"], now=tsc.az(2026, 10, 5)) == 1
        assert "reason=scan_lock_error" in capsys.readouterr().out and not fake.posts


def _rails_pass(r: str, monkeypatch, caplog) -> None:
    """The SCAN_RUNNING_REPLY pinning pattern: both phantom rails leave the line
    byte-identical in observe (no rail log) AND in enforce mode; the sanitizer too."""
    caplog.set_level(logging.WARNING, logger=se.__name__)
    monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
    assert se.screen_phantom_write_claims(r, tool_use_count=0) == r, r
    assert se.screen_capability_claims(r, tool_use_count=0, founder=True) == r, r
    assert se.sanitize_text(r) == r, r
    assert not [x for x in caplog.records
                if se.PHANTOM_LOG_KEY in x.getMessage() or se.CAPABILITY_LOG_KEY in x.getMessage()]
    monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
    assert se.screen_phantom_write_claims(r, tool_use_count=0) == r, r
    assert se.screen_capability_claims(r, tool_use_count=0, founder=True) == r, r
    monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)


_RUNNING = re.compile(r"\b(?:already running|is running|will arrive|will follow)\b", re.I)


class TestAskLockFaultR4:
    """The on-ask reply to a lock fault: the scan stopped, nothing was archived -- never
    'a scan is already running; its card will arrive' (no card is coming)."""

    @pytest.fixture
    def real_deliver(self, monkeypatch):
        f = FakeSlack()
        monkeypatch.setattr(clients, "read_client_factory", lambda: f)
        monkeypatch.setattr(clients, "write_client_factory", lambda: f)
        monkeypatch.setattr(deliver, "deliver_proposal", aw._REAL_DELIVER)
        return f

    @pytest.mark.parametrize("fault", sorted(_FAULTS))
    def test_the_dm_ask_says_the_scan_stopped_not_that_one_is_running(self, dm, real_deliver, tmp_path,  # noqa: F811
                                                                     monkeypatch, caplog, fault):
        _FAULTS[fault](tmp_path, monkeypatch)
        client = MagicMock()
        app_module.handle_message_event(aw._event("archive the dead channels"), client)
        app_module._CHANNEL_ARCHIVE_SCAN_POOL.shutdown(wait=True)
        texts = aw._texts(client)
        assert texts == [it.ACK_REPLY, it.SCAN_FAILED_REPLY], texts
        assert not _RUNNING.search(texts[-1]) and "nothing was archived" in texts[-1]
        assert it.is_lane_reply(texts[-1]) and not dm.qa.called and not real_deliver.posts
        _rails_pass(texts[-1], monkeypatch, caplog)

    def test_the_channel_ask_says_the_scan_stopped_not_that_one_is_running(self, real_deliver, tmp_path,
                                                                          monkeypatch, caplog):
        _block_lock_parent(tmp_path, monkeypatch)
        client = MagicMock()
        app_module._ca_start_scan(client, "C0B3K67J10T", "1790000200.000100",
                                  ack_text=it.CHANNEL_ACK_REPLY, running_text=it.SCAN_RUNNING_CHANNEL_REPLY)
        app_module._CHANNEL_ARCHIVE_SCAN_POOL.shutdown(wait=True)
        texts = aw._texts(client)
        assert texts == [it.CHANNEL_ACK_REPLY, it.SCAN_FAILED_REPLY], texts
        assert it.SCAN_RUNNING_CHANNEL_REPLY not in texts
        _rails_pass(texts[-1], monkeypatch, caplog)

    def test_a_live_holder_still_gets_the_running_line(self, dm, real_deliver):  # noqa: F811
        tok = st.acquire_scan_lock(now=time.time())
        try:
            client = MagicMock()
            app_module.handle_message_event(aw._event("archive the dead channels"), client)
            app_module._CHANNEL_ARCHIVE_SCAN_POOL.shutdown(wait=True)
        finally:
            st.release_scan_lock(tok)
        assert aw._texts(client) == [it.ACK_REPLY, it.SCAN_RUNNING_REPLY]
