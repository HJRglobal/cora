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

# ── D-051 round 1 (Code #16) ────────────────────────────────────────────────
#: c1-intents-copy#2 -- natural phrasings of the ask: polite modals, vocatives with
#: any punctuation, a comma/pls/emoji tail, emphasis wrappers, and the leading
#: punctuation a stripped mention leaves behind (the catch-up's _strip_mention).
ASK_FIRE_R1 = [
    "can we archive the dead channels?", "Cora — archive the dead channels",
    "hey Cora - archive the dead channels", "cora! archive the dead channels",
    "can you go ahead and archive the dead channels?", "would you mind archiving the dead channels?",
    "time to archive the dead channels", "_archive the dead channels_",
    "archive the dead channels, please", "archive the dead channels pls",
    "archive the dead channels \U0001f64f", "archive the dead channels :pray:",
    ", archive the dead channels", ": can you archive the dead channels?",
    "<@U0B44MDGC5R>, archive the dead channels", "<@U0B44MDGC5R>: can you archive the dead channels?",
    "yes, archive the dead channels", "ok, archive the dead channels", "~archive the dead channels~",
    "archive the _dead_ channels", "do you mind archiving the dead channels?",
]
ASK_NOT_R1 = [
    "do you archive the dead channels?", "should we archive the dead channels?",
    "archiving the dead channels now", "archive the dead channels, then post a summary",
    "archive the dead channels :pray: and the deals",
]
#: c1-intents-copy#0 + integration#5 -- how-to / policy / decision framings, the
#: noun senses of archive / channel / proposal, and 'archive' inside a channel NAME.
STATUS_NOT_R1 = [
    "how did the amazon channel do per the archived excel reports?",
    "which archived reports cover the retail channel?", "did the hubspot proposal get archived?",
    "is the proposal for walmart archived?", "how do I archive a channel in slack?",
    "which channels are safe to archive?", "do you have access to the archive channel?",
    "what did we decide in <#C0B2T18R3FG|hjr-archive-2024>?", "what is the policy for archiving channels?",
    "did we ever decide which channels to archive?", "which channels should I archive?",
    "Did that already -- archived the proposal in August", "Have archived the proposal; closing this out",
    "how about we archive that proposal until Q1", "any channel quiet for 90 days should be archived",
    "What we decided: archive channels after 90 days", "did you archive the retail channel report?",
    "what happens when a channel is archived?", "how does archiving a channel work?",
    "is <#C0B2T18R3FG|hjr-archive> quiet?", "did the retail channel get archived excel reports?",
]
STATUS_FIRE_R1 = [
    "did you archive the dead channels?", "have you archived any channels?",
    "has cora archived any channels yet?", "were the dead channels archived?",
    "how many channels have you archived?", "what channels did cora archive last week?",
    "is <#C0B2T18R3FG|social> archived?", "were you able to archive the dead channels?",
    "hey cora, did you archive the channels?", "what's the status of the dead-channel archive card?",
    "show me the channels you archived", "what's the status on the dead channels archive?",
    "which channels have you archived so far?", "list the channels cora archived",
    "any update on the archive card?", "was <#C0B2T18R3FG|hjr-archive> archived?",
]
#: c1-intents-copy#6 -- 'channel' in its sales/media sense, and archive requests
#: whose object is some other thing (channel only inside a prepositional phrase).
ATTEMPT_NOT_R1 = [
    "archive the retail channel deals that closed lost", "archive the amazon channel report in drive",
    "archive my emails from the channel partners", "archive the old channel strategy doc",
    "archive the tasks for the retail channel project in asana", "archive the channel partner deals in hubspot",
    "archive the dtc channel tasks", "archive the email about the channel launch",
    "archive the email from the channel", "Archive policy: channels with no posts for 90 days",
    "archive the channel's messages", "archive of the #general channel is in drive",
    "archive the channel history", "archive folder for the channels is full",
]
ATTEMPT_FIRE_R1 = [
    "archive channels after 90 days with no human posts, except leadership",
    "archive the OSN store channels", "archive this channel", "yes, archive <#C0B2T18R3FG|social>",
    "archive any channel with no posts in 90 days", "could we archive the old promo channels?",
    "archive the retail channel",
]
#: c1-intents-copy#1 -- a typed follow-up to a LIVE card: any text opening with the
#: archive verb (after yes / ok / sure / sounds good / just / now / please / go ahead
#: and), unless its object is plainly some other thing.
FOLLOWUP_FIRE_R1 = [
    "archive the rest", "archive the marked ones", "yes, archive them", "yes archive them",
    "archive them all", "just archive them", "now archive them", "archive all listed",
    "archive everything", "archive all 12", "archive what i marked", "sounds good, archive them",
    "archive the dead ones", "ok, archive them", "sure, archive the 3 i marked", "go ahead and archive",
    "please archive them now", "archive it", "archive them after the meeting next week and tell tommy",
    "Cora, archive the ones I marked", "yes please archive them", "archive the card",
    "archive them in slack",
]
FOLLOWUP_NOT_R1 = [
    "yes, and also send the report", "ok thanks for that", "archive this thread", "archive old emails",
    "archive the retail channel deals", "archive my emails from tommy", "archive the hubspot deal",
    "archived them myself", "don't archive them", "did you archive them?",
    "archive of the old site is in drive", "archive folder is full", "archive: q3 decks",
    "archive is in drive", "archive the channel history",
    "archive everything in the promo folder", "archive them in my gmail", "archive the old stuff from asana",
]


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


@pytest.mark.parametrize("text", ASK_FIRE_R1)
def test_r1_the_ask_grammar_takes_natural_phrasings(text):
    assert it.looks_like_archive_ask(text), text
    assert not it.looks_like_archive_attempt(text), text


@pytest.mark.parametrize("text", ASK_NOT_R1)
def test_r1_the_widened_ask_grammar_still_refuses(text):
    assert not it.looks_like_archive_ask(text), text


@pytest.mark.parametrize("text", STATUS_NOT_R1)
def test_r1_status_ignores_how_to_policy_noun_senses_and_channel_names(text):
    assert not it.looks_like_archive_status(text), text


@pytest.mark.parametrize("text", STATUS_FIRE_R1)
def test_r1_lane_status_questions_still_fire(text):
    assert it.looks_like_archive_status(text), text


@pytest.mark.parametrize("text", ATTEMPT_NOT_R1)
def test_r1_the_attempt_rail_needs_channels_as_the_object(text):
    assert not it.looks_like_archive_attempt(text), text


@pytest.mark.parametrize("text", ATTEMPT_FIRE_R1)
def test_r1_the_attempt_rail_still_fires_on_a_channel_object(text):
    assert it.looks_like_archive_attempt(text), text


def test_r1_normalize_masks_channel_names_and_strips_leading_punctuation():
    assert it.normalize("<@U0B44MDGC5R>, archive <#C0B2T18R3FG|hjr-archive-2024>") == "archive #chan"
    assert it.normalize("_archive the dead channels_") == "archive the dead channels"
    assert it.normalize("react with :thumbs_up: in snake_case") == "react with :thumbs_up: in snake_case"


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

    @pytest.mark.parametrize("t", FOLLOWUP_NOT_R1)
    def test_longer_or_other_replies_do_not(self, t):
        assert not it.looks_like_live_followup(t, card_ts=NOW - 60, now=NOW)

    @pytest.mark.parametrize("t", FOLLOWUP_FIRE_R1)
    def test_any_archive_verb_opener_is_a_followup_while_the_card_is_live(self, t):
        """c1-intents-copy#1: 'archive it' and 'archive them after the meeting ...' used
        to be pinned as must-not-fire; the round-1 adjudication widened the grammar to
        every archive-verb opener while a card is live (the reply is an honest 'that
        reply archived nothing'), so both moved here."""
        assert it.followup_shape(t) == "imperative", t
        assert it.looks_like_live_followup(t, card_ts=NOW - 5 * DAY, now=NOW), t


@pytest.mark.parametrize("fn", [it.looks_like_archive_ask, it.looks_like_archive_attempt,
                                it.looks_like_archive_status,
                                lambda s: it.looks_like_live_followup(s, card_ts=NOW, now=NOW)],
                         ids=["ask", "attempt", "status", "followup"])
@pytest.mark.parametrize("word", ["archive", "cora", "did", "@cora", "<@U0B44MDGC5R>", "<#C0B2T18R3FG|x>",
                                  "yes,", "_", "~", ",", ":pray:", "\U0001f64f", "did you archive",
                                  "archive the", "cora \u2014"],
                         ids=["archive", "cora", "did", "at-cora", "mention", "chan-token", "yes-comma",
                              "underscore", "tilde", "comma", "shortcode", "emoji", "did-you-archive",
                              "archive-the", "cora-dash"])
def test_every_predicate_is_linear_on_the_degenerate_input(fn, word):
    shape = word + " " * 40000 + "x"
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        fn(shape)
        fn(" " * 40000)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.05


@pytest.mark.parametrize("fn", [it.looks_like_archive_ask, it.looks_like_archive_attempt,
                                it.looks_like_archive_status, it.followup_shape],
                         ids=["ask", "attempt", "status", "followup"])
@pytest.mark.parametrize("shape", ["archive " + "a " * 150, "did you archive " + "the " * 70,
                                   "archive the dead channels" + "!" * 270, "cora" + "-" * 290,
                                   "archive the " + ", " * 140, "_" * 299, "archive " + ":a:" * 97,
                                   "archive the dead channels" + " \U0001f64f" * 90,
                                   "did you archive " + "old " * 60 + "channels"],
                         ids=["filler", "det-run", "bangs", "dashes", "commas", "underscores", "codes",
                              "emoji-run", "long-object"])
def test_every_predicate_is_fast_on_capped_worst_cases(fn, shape):
    """The new filler / tail / punctuation patterns, timed on the longest inputs the
    300-char cap still lets through."""
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        fn(shape)
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
        r = it.followup_reply()
        assert r.startswith(it.FOLLOWUP_REPLY_LEAD)
        assert "Lane at T0: nothing has been archived" in r          # clean ledger, not demoted

    def test_followup_reply_is_scoped_to_the_typed_turn(self):
        """c1-authority-tier#4: the reply speaks for THIS typed turn, never for the lane."""
        assert it.FOLLOWUP_REPLY_LEAD == "That reply archived nothing — only the card's buttons act."

    def test_followup_reply_never_denies_archives_the_ledger_shows(self):
        """c1-authority-tier#4: a lane back at T0 after real archives (a demotion, a flag
        roll-back) must not say 'nothing has been archived'."""
        st.append_ledger("intent", proposal_id="chanarch-aaaaaaaaaaaa", channel_id="C0AAAAAAA1",
                         tapped_by=HARRISON, ts=NOW)
        st.append_ledger("outcome", proposal_id="chanarch-aaaaaaaaaaaa", channel_id="C0AAAAAAA1",
                         outcome="archived", tapped_by=HARRISON, ts=NOW)
        r = it.followup_reply()
        assert r.startswith(it.FOLLOWUP_REPLY_LEAD) and "Lane at T0" in r
        assert "nothing has been archived" not in r and "nothing was archived" not in r

    def test_followup_reply_when_demoted_or_the_ledger_is_unreadable(self, monkeypatch, tmp_path):
        from cora.channel_archive import policy
        policy.demotion_path().write_text("{}", encoding="utf-8")
        assert "nothing has been archived" not in it.followup_reply()
        policy.demotion_path().unlink()
        d = tmp_path / "d"
        d.mkdir()
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_LEDGER_PATH", str(d))
        assert "nothing has been archived" not in it.followup_reply()

    def test_followup_reply_at_t1(self, monkeypatch):
        from cora.channel_archive import gates, policy
        monkeypatch.setattr(policy, "acting_tier", lambda: "T1")
        monkeypatch.setattr(gates, "registry_allows_t1", lambda: True)
        r = it.followup_reply()
        assert r.startswith(it.FOLLOWUP_REPLY_LEAD) and "without a tap on the card" in r

    def test_live_card_ts_needs_a_live_card_in_this_dm(self):
        self._seed()
        assert it.live_card_ts("DH", now=NOW + 60) == pytest.approx(NOW)
        assert it.live_card_ts("DOTHER", now=NOW + 60) is None
        assert it.live_card_ts("DH", now=NOW + 15 * DAY) is None

    def test_every_reply_passes_both_rails(self, monkeypatch, caplog):
        self._seed()
        caplog.set_level(logging.WARNING, logger=se.__name__)
        replies = [it.ACK_REPLY, it.CHANNEL_ACK_REPLY, it.SCAN_RUNNING_REPLY,
                   it.SCAN_RUNNING_CHANNEL_REPLY, it.SCAN_FAILED_REPLY,
                   it.OFF_REPLY, it.ATTEMPT_REPLY, it.CATCHUP_DRAFT, it.followup_reply(),
                   it.status_reply(now=NOW + 60)]
        st.append_ledger("outcome", proposal_id="chanarch-aaaaaaaaaaaa", channel_id="C0AAAAAAA1",
                         outcome="archived", tapped_by=HARRISON, ts=NOW)
        replies.append(it.followup_reply())                       # the ledger-shows-archives copy
        for r in replies:
            assert se.screen_phantom_write_claims(r, tool_use_count=0) == r, r
            assert se.sanitize_text(r) == r, r
        assert not [x for x in caplog.records if se.PHANTOM_LOG_KEY in x.getMessage()]
        monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
        for r in replies:
            assert se.screen_phantom_write_claims(r, tool_use_count=0) == r, r


class TestLaneReplies:
    def test_every_lane_line_is_recognised_and_ordinary_answers_are_not(self):
        for r in (it.ACK_REPLY, it.CHANNEL_ACK_REPLY, it.SCAN_RUNNING_REPLY, it.SCAN_RUNNING_CHANNEL_REPLY,
                  it.SCAN_FAILED_REPLY, it.OFF_REPLY, it.ATTEMPT_REPLY, it.followup_reply(),
                  it.status_reply(now=NOW), "Dead-channel proposal (T0 — nothing archived): 12 listed."):
            assert it.is_lane_reply(r), r
        for r in ("Want the 13-week view too?", "", "Done.", "Dead channels are a pain"):
            assert not it.is_lane_reply(r), r


class TestClockSkew:
    """harness-isolation#0: the card ts is Slack's clock, ``now`` is the host's."""

    def test_a_card_stamp_a_hair_in_the_future_is_skew_not_stale(self):
        assert it.looks_like_live_followup("yes", card_ts=NOW + 0.0005, now=NOW)
        assert it.looks_like_live_followup("ok", card_ts=NOW + 60, now=NOW)

    def test_a_card_far_in_the_future_is_not_live(self):
        assert not it.looks_like_live_followup("yes", card_ts=NOW + 10 * 60, now=NOW)


# ── D-051 round 2 (Code #16) ────────────────────────────────────────────────
def _missing_parts_line(posted: int, total: int, reason: str = "post_failed:ratelimited") -> str:
    """The REAL deliver._say_missing_parts text, captured from a fake writer."""
    from cora.channel_archive import deliver
    seen: list[dict] = []

    class _Writer:
        def chat_postMessage(self, **kw):
            seen.append(kw)
            return {"ok": True, "ts": "1790000001.000100"}
    deliver._say_missing_parts(_Writer(), "DH", posted, total, reason)
    assert len(seen) == 1 and not seen[0].get("thread_ts")      # a TOP-LEVEL DM line
    return seen[0]["text"]


class TestLaneRepliesR2:
    """r2:c1-intents-copy#2 / c1-state-machine#1 / harness-isolation#1 / integration#1:
    the partial-delivery line deliver posts is one of the lane's own DM lines, so a
    newer copy of it never takes a bare 'yes' from the card it reports on."""

    @pytest.mark.parametrize("posted,total", [(1, 2), (1, 4), (2, 3)])
    def test_the_missing_parts_line_is_a_lane_line(self, posted, total):
        assert it.is_lane_reply(_missing_parts_line(posted, total))

    def test_the_prefix_is_delivers_own_constant(self):
        from cora.channel_archive import deliver
        assert deliver.MISSING_PARTS_LEAD in it._LANE_REPLY_PREFIXES
        assert _missing_parts_line(1, 2).startswith(deliver.MISSING_PARTS_LEAD)


def _card(pid, *, created, pages, n_pages=None, dm="DH", rows=True):
    """Stage *pid* and post the given {page: ts} pages (n_pages defaults to them all)."""
    rr = ([{"cid": f"C0{pid[-8:].upper()}{i}", "section": "A", "tier": "T0"} for i in (1, 2)]
          if rows else [])
    st.append_event("staged", proposal_id=pid, ts=created, expires_ts=created + 14 * DAY, rows=rr,
                    counts={}, n_pages=len(pages) if n_pages is None else n_pages,
                    blind=None if rows else "list_incomplete")
    for page, ts in pages.items():
        st.append_event("delivered", proposal_id=pid, page=page, dm_channel=dm, message_ts=ts,
                        rendered_cids=[r["cid"] for r in rr], buttons=True, ts=created)


class TestEveryLiveCardR2:
    """r2:integration#2: a partly delivered (or blind / empty) newer card supersedes
    nothing, so an older complete card stays LIVE (its buttons still act) -- the DM
    rail must see every live card page, not only the newest proposal's."""

    def test_both_live_cards_pages_count_and_the_newest_page_times_the_window(self):
        a_ts, c_ts = f"{NOW - 3600:.6f}", f"{NOW - 60:.6f}"
        _card("chanarch-aaaaaaaaaaaa", created=NOW - 3700, pages={1: a_ts})
        _card("chanarch-cccccccccccc", created=NOW - 120, pages={1: c_ts}, n_pages=2)   # partial
        f = st.fold(now=NOW)
        assert f.is_live("chanarch-aaaaaaaaaaaa", NOW) and f.is_live("chanarch-cccccccccccc", NOW)
        assert it.live_card_message_ts("DH", now=NOW) == {a_ts, c_ts}
        assert it.live_card_ts("DH", now=NOW) == pytest.approx(NOW - 60)

    def test_a_newer_blind_card_leaves_the_older_card_counted(self):
        a_ts, b_ts = f"{NOW - 3600:.6f}", f"{NOW - 60:.6f}"
        _card("chanarch-aaaaaaaaaaaa", created=NOW - 3700, pages={1: a_ts})
        _card("chanarch-bbbbbbbbbbbb", created=NOW - 120, pages={1: b_ts}, rows=False)
        assert it.live_card_message_ts("DH", now=NOW) == {a_ts, b_ts}

    def test_a_superseded_card_and_another_dms_card_do_not_count(self):
        a_ts, c_ts, o_ts = f"{NOW - 3600:.6f}", f"{NOW - 60:.6f}", f"{NOW - 30:.6f}"
        _card("chanarch-aaaaaaaaaaaa", created=NOW - 3700, pages={1: a_ts})
        _card("chanarch-cccccccccccc", created=NOW - 120, pages={1: c_ts})               # complete
        _card("chanarch-oooooooooooo", created=NOW - 90, pages={1: o_ts}, n_pages=2, dm="DOTHER")
        assert it.live_card_message_ts("DH", now=NOW) == {c_ts}
        assert it.live_card_ts("DH", now=NOW) == pytest.approx(NOW - 60)
        assert it.live_card_message_ts("DOTHER", now=NOW) == {o_ts}
