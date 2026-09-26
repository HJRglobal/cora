"""Code #16 C1 -- D-051 round-4 regressions for the dead-channel lane (fixer R4A).

  r4:c1-intents-copy#0 -- the round-3 "archived by <actor>" rule read an actor noun
      used as a MODIFIER ("monday's team call", "tomorrow's staff meeting") as the
      actor, and 'time' as filler ("by the time people got in"), so a deadline-framed
      lane-status question went to the zero-tool model. The actor noun must END the
      phrase (never a modifier of an event noun) and 'time' is never filler; a
      phrase-final actor ("by her team?", "by the team?", "by staff?", a Slack
      mention) still bails.

Every test drives the real code path (the pure predicate and the real DM / @mention /
/cora-ask / catch-up entry points); nothing here mocks the function under test.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

import cora.app as app_module
import test_channel_archive_app_wiring as aw
from _chanarch_fakes import HARRISON, NOW
from cora.channel_archive import intents as it
from test_channel_archive_app_wiring import dm  # noqa: F401 -- the real-DM fixture

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
