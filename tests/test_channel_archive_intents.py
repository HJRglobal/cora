"""Code #16 C1 (A21) -- the founder-only archive intents: the ask grammar, the
archive-ATTEMPT rail, archive-STATUS questions and live-card follow-ups. Must-fire /
must-not-fire tables incl. the noun senses (lesson 45), ReDoS timing on the degenerate
40k-whitespace input (lesson 30/43), and replies that are store-truthful, name-free
and rail-clean.
"""
from __future__ import annotations

import logging
import time

import pytest

from _chanarch_fakes import DAY, HARRISON, NOW
from cora import slack_egress as se
from cora.channel_archive import intents as it
from cora.channel_archive import store as st

ASK_FIRE = [
    "can you archive the dead channels?", "archive the dead channels",
    "@Cora archive inactive channels please", "Cora, archive the stale channels",
    "please archive all the dead slack channels", "archive dead channels now",
    "could you please archive our unused channels?", "go ahead and archive the old channels.",
    "<@U0B44MDGC5R> archive the dead channels", "hey cora archive idle channels thanks",
    "Archive the Dead Channels!", "  archive   the   dead\nchannels  ",
]
ASK_NOT = [
    "what's in the archive?", "archive this thread", "archive the dead deals in hubspot",
    "why did you archive #x", "did the dead channels get archived?", "run the kb archive",
    "archive old emails", "I think we should archive the dead channels eventually",
    "archive the dead channels except #social", "archive #old-promo",
    "don't archive the dead channels", "the dead channels archive is fine",
    "archive the channel", "archived the dead channels", "archive the dead channels and dismiss cq-123456789012",
]
ATTEMPT_FIRE = [
    "archive the dead channels except #social", "archive <#C0B2T18R3FG|social>",
    "archive the channel", "please archive these channels", "archive #old-promo channel",
    "can you archive the old promo channels in slack by friday?",
    "archive the dead channels, then post a summary",
]
ATTEMPT_NOT = ["archive the dead channels", "archive this thread", "archive old emails",
               "what's in the archive?", "which channels should I archive?",
               "I archived the channel myself", "the archive channel is quiet"]
STATUS_FIRE = [
    "did the dead channels get archived?", "which channels did you archive?",
    "how many channels were archived?", "what's the status of the archive proposal?",
    "any channels archived yet?", "is the archive card still live?",
    "why did you archive <#C0B2T18R3FG|social>?", "@Cora did you archive the channels?",
]
STATUS_NOT = ["what's in the archive?", "what is the kb archive status?", "archive the dead channels",
              "did you archive the email?", "status of the deal?", "I archived the channels",
              "where are the archive files?"]


@pytest.mark.parametrize("text", ASK_FIRE)
def test_the_ask_grammar_fires(text):
    assert it.looks_like_archive_ask(text), text


@pytest.mark.parametrize("text", ASK_NOT)
def test_the_ask_grammar_does_not_fire(text):
    assert not it.looks_like_archive_ask(text), text


@pytest.mark.parametrize("text", ATTEMPT_FIRE)
def test_the_attempt_rail_fires(text):
    assert it.looks_like_archive_attempt(text), text


@pytest.mark.parametrize("text", ATTEMPT_NOT)
def test_the_attempt_rail_does_not_fire(text):
    assert not it.looks_like_archive_attempt(text), text


@pytest.mark.parametrize("text", STATUS_FIRE)
def test_status_questions_fire(text):
    assert it.looks_like_archive_status(text), text


@pytest.mark.parametrize("text", STATUS_NOT)
def test_status_does_not_fire(text):
    assert not it.looks_like_archive_status(text), text


class TestFollowup:
    def test_imperatives_fire_any_time_the_card_is_live(self):
        for t in ("archive them", "archive all", "archive those please", "can you archive the list?",
                  "archive all of them"):
            assert it.looks_like_live_followup(t, card_ts=NOW - 5 * DAY, now=NOW), t

    def test_a_bare_yes_only_within_thirty_minutes_of_the_card(self):
        for t in ("yes", "ok", "go ahead", "do it", "Yes please!"):
            assert it.looks_like_live_followup(t, card_ts=NOW - 60, now=NOW), t
            assert not it.looks_like_live_followup(t, card_ts=NOW - 31 * 60, now=NOW), t

    def test_nothing_fires_without_a_live_card(self):
        assert not it.looks_like_live_followup("archive them", card_ts=None, now=NOW)

    @pytest.mark.parametrize("t", ["yes, and also send the report", "archive it", "ok thanks for that",
                                   "archive them after the meeting next week and tell tommy"])
    def test_longer_or_other_replies_do_not(self, t):
        assert not it.looks_like_live_followup(t, card_ts=NOW - 60, now=NOW)


@pytest.mark.parametrize("fn", [it.looks_like_archive_ask, it.looks_like_archive_attempt,
                                it.looks_like_archive_status,
                                lambda s: it.looks_like_live_followup(s, card_ts=NOW, now=NOW)],
                         ids=["ask", "attempt", "status", "followup"])
@pytest.mark.parametrize("word", ["archive", "cora", "did", "@cora", "<@U0B44MDGC5R>", "<#C0B2T18R3FG|x>"],
                         ids=["archive", "cora", "did", "at-cora", "mention", "chan-token"])
def test_every_predicate_is_linear_on_the_degenerate_input(fn, word):
    shape = word + " " * 40000 + "x"
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        fn(shape)
        fn(" " * 40000)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.05


class TestReplies:
    def _seed(self):
        rows = [{"cid": "C0AAAAAAA1", "section": "A", "name": "fx-very-secret-name", "tier": "T0"},
                {"cid": "C0AAAAAAA2", "section": "A", "name": "fx-other", "tier": "T0"},
                {"cid": "C0AAAAAAA3", "section": "B", "lex": True, "tier": "T0"}]
        st.append_event("staged", proposal_id="chanarch-aaaaaaaaaaaa", ts=NOW, expires_ts=NOW + 14 * DAY,
                        rows=rows, counts={})
        st.append_event("delivered", proposal_id="chanarch-aaaaaaaaaaaa", page=1, dm_channel="DH",
                        message_ts=f"{NOW:.6f}", rendered_cids=[r["cid"] for r in rows], buttons=True, ts=NOW)
        st.append_event(st.AGREED, proposal_id="chanarch-aaaaaaaaaaaa", cid="C0AAAAAAA1", by=HARRISON, ts=NOW)
        st.append_event(st.KEPT, proposal_id="chanarch-aaaaaaaaaaaa", cid="C0AAAAAAA2", by=HARRISON, ts=NOW)

    def test_status_is_store_and_ledger_backed_and_name_free(self):
        self._seed()
        r = it.status_reply(now=NOW + 60)
        assert "T0 (proposal cards only — nothing is archived at T0)" in r
        assert "Marked to archive 1 · kept 1 · archived 0 (ledger-backed) · outcome unknown 0" in r
        assert "(3 listed)" in r and "fx-" not in r and "reversible" in r

    def test_status_with_no_card_yet(self):
        assert "No proposal card yet" in it.status_reply(now=NOW)

    def test_status_says_demoted(self):
        from cora.channel_archive import policy
        policy.demotion_path().write_text("{}", encoding="utf-8")
        assert "DEMOTED" in it.status_reply(now=NOW)

    def test_status_admits_an_unreadable_store(self, monkeypatch, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_LEDGER_PATH", str(d))
        assert "can't give counts" in it.status_reply(now=NOW)

    def test_followup_reply_is_tier_truthful(self):
        assert "Lane at T0: nothing has been archived" in it.followup_reply()

    def test_live_card_ts_needs_a_live_card_in_this_dm(self):
        self._seed()
        assert it.live_card_ts("DH", now=NOW + 60) == pytest.approx(NOW)
        assert it.live_card_ts("DOTHER", now=NOW + 60) is None
        assert it.live_card_ts("DH", now=NOW + 15 * DAY) is None

    def test_every_reply_passes_both_rails(self, monkeypatch, caplog):
        self._seed()
        caplog.set_level(logging.WARNING, logger=se.__name__)
        replies = [it.ACK_REPLY, it.CHANNEL_ACK_REPLY, it.SCAN_RUNNING_REPLY, it.SCAN_FAILED_REPLY,
                   it.OFF_REPLY, it.ATTEMPT_REPLY, it.CATCHUP_DRAFT, it.followup_reply(),
                   it.status_reply(now=NOW + 60)]
        for r in replies:
            assert se.screen_phantom_write_claims(r, tool_use_count=0) == r, r
            assert se.sanitize_text(r) == r, r
        assert not [x for x in caplog.records if se.PHANTOM_LOG_KEY in x.getMessage()]
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        for r in replies:
            assert se.screen_phantom_write_claims(r, tool_use_count=0) == r, r
