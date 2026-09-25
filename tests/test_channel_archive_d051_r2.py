"""Code #16 C1 -- D-051 round-2 regressions for the dead-channel lane (fixer R2B).

Every test drives the REAL tap path (process_tap -> _archive_one) or the real
classifier -> row_from_verdict -> card renderer over the shared non-dict SlackResponse
fakes; nothing here mocks the function under test.

  r2:c1-authority-tier#0 / r2:c1-state-machine#2 -- the "never post the notice twice"
      read-back: an unknown identity, a window longer than one page, and a later
      refused attempt must never read as "no notice here".
  r2:c1-authority-tier#1 -- the A12 re-check runs IMMEDIATELY before the first channel
      write, after every network read of the attempt.
  r2:c1-false-inactive#2 -- a membership READ failure never renders "you are not a member".
  r2:c1-state-machine#3 -- a "failed (reconciled: channel gone)" row is terminal, honest.
  r2:c1-false-inactive#1 -- an unarchive is disclosed on EVERY B row; the T1 re-verify
      requires the same flag.
"""
from __future__ import annotations

import pytest

from _chanarch_fakes import (BOT_ID, BOT_UID, BOTUSER, DAY, HARRISON, NOW, PERSON, FakeSlack,
                             api_error, chan, context, msg, no_sleep, resp)
from cora.channel_archive import cards, clients, gates, handler, policy
from cora.channel_archive import classify as cl
from cora.channel_archive import registry as reg
from cora.channel_archive import scan as sc
from cora.channel_archive import store as st

PID = "chanarch-abcabcabcab2"
A1 = "C0DEADAAA1"
BUSY = "C0BUSYHOOK1"


def _row(cid, name, section="A", tier="T1", **kw):
    return {"cid": cid, "section": section, "reason": kw.pop("reason", ""), "tier": tier,
            "lex": False, "name": name, "name_fp": reg.name_fp(name),
            "is_private": kw.pop("is_private", False), "last_person_days": 200,
            "history_complete": True, "keep_count": 0, **kw}


def stage(rows, ts=NOW, pid=PID):
    st.append_event("staged", proposal_id=pid, ts=ts, expires_ts=ts + 14 * DAY, rows=rows,
                    counts={}, scanned=len(rows))
    st.append_event("delivered", proposal_id=pid, page=1, dm_channel="DHARRISON1",
                    message_ts=f"{ts + 1:.6f}", rendered_cids=[r["cid"] for r in rows],
                    buttons=True, ts=ts)
    return pid


@pytest.fixture
def fake(monkeypatch):
    f = FakeSlack(channels=[chan(A1, "fx-dead-one")], history={A1: [msg(200)]},
                  scopes=["channels:manage", "groups:write", "chat:write"])
    monkeypatch.setattr(clients, "read_client_factory", lambda: f)
    monkeypatch.setattr(clients, "write_client_factory", lambda: f)
    return f


@pytest.fixture
def armed(monkeypatch, tmp_path):
    p = tmp_path / "ladder.yaml"
    p.write_text("lanes:\n  - lane: slack-channel-archive\n    tier: T1\n", encoding="utf-8")
    monkeypatch.setenv("CORA_LADDER_REGISTRY_PATH", str(p))
    monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "act")
    return p


def tap(action, value, now=NOW + 10):
    return handler.process_tap(action, value, HARRISON, now=now, sleep=no_sleep)


def state(cid, pid=PID, now=NOW + 20):
    return st.fold(now=now).proposals[pid].row_state.get(cid) or {"state": st.OPEN}


def _notice_msg(ts):
    return {"ts": f"{ts:.6f}", "type": "message", "user": BOT_UID,
            "text": cards.notice_text(200, HARRISON)}


def _prior_attempt(cid, its, outcome, pid=PID):
    """An earlier lane attempt on *cid*, on the ledger with controlled timestamps."""
    assert st.append_ledger("intent", proposal_id=pid, channel_id=cid, tapped_by=HARRISON, ts=its)
    assert st.append_ledger("outcome", proposal_id=pid, channel_id=cid, outcome=outcome,
                            tapped_by=HARRISON, ts=its + 1)


def _notices_to(fake, cid):
    return [p for p in fake.posts if p["channel"] == cid
            and p["text"].startswith(cards.NOTICE_PREFIX)]


def _is_notice_read(oldest, latest):
    """The notice read-back: an ``oldest`` floor and no ``latest`` (the classifier's
    phase 1 always passes latest; phase 2 never passes oldest)."""
    return oldest is not None and latest is None


# ── r2:c1-authority-tier#0 / r2:c1-state-machine#2 ──────────────────────────────────
class TestNoticeReadBackNeverMissesAStandingNotice:

    def test_an_auth_test_blip_after_the_intent_never_reposts_the_notice(self, fake, armed):
        """(a) the second auth.test failed -> ('','') -> 'no notice here'. The identity
        now comes from the re-verify's confirmed context: no auth.test after the intent."""
        stage([_row(A1, "fx-dead-one")])
        _prior_attempt(A1, NOW + 5, "notice_indeterminate:timeout")
        fake.history[A1].insert(0, _notice_msg(NOW + 5.5))      # landed, never corrected
        real_auth = fake.auth_test

        def _auth(**kw):
            intents = [r for r in (st.read_ledger() or []) if r.get("event") == "intent"]
            if len(intents) >= 2:                                 # after THIS attempt's intent
                raise TimeoutError("auth.test timed out")
            return real_auth(**kw)
        fake.auth_test = _auth
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1", now=NOW + 100)
        assert _notices_to(fake, A1) == [], r.msg
        assert r.outcome == "archived" and "already standing (not posted twice)" in r.msg, r.msg

    def test_an_unknown_identity_reads_as_unreadable_never_as_no_notice(self):
        f = FakeSlack(channels=[chan(A1, "fx-dead-one")], history={A1: [_notice_msg(NOW + 5.5)]})
        assert handler._notice_still_standing(f, A1, BOT_UID, BOT_ID, NOW, sleep=no_sleep) is True
        assert handler._notice_still_standing(f, A1, "", "", NOW, sleep=no_sleep) is None
        assert f.method_names().count("conversations_history") == 1   # the blind call read nothing

    @pytest.mark.parametrize("page_size", [None, 10, 7], ids=["one_page", "pages_of_10", "pages_of_7"])
    def test_a_notice_behind_more_than_twenty_newer_posts_is_still_seen(self, fake, armed, page_size):
        """(b) a webhook channel (a B bot_traffic override row) with 25 posts since the
        standing notice: the read-back pages (A8) until the window is covered."""
        fake.channels.append(chan(BUSY, "fx-webhook-room"))
        hook = [{"ts": f"{NOW + 7 + i:.6f}", "type": "message", "subtype": "bot_message",
                 "bot_id": "BHOOK01", "text": "deploy ok"} for i in range(25)]
        fake.history[BUSY] = hook + [_notice_msg(NOW + 5.5), msg(200)]
        fake.page_size = page_size
        stage([_row(BUSY, "fx-webhook-room", section="B", reason=cl.B_BOT_TRAFFIC,
                    bot_posts=25, bot_latest_days=0, harrison_member=True)])
        _prior_attempt(BUSY, NOW + 5, "failed:restricted_action")
        r = tap(cards.ACTION_OVERRIDE, f"{PID}:{BUSY}:T1", now=NOW + 100)
        assert _notices_to(fake, BUSY) == [], r.msg
        assert r.outcome == "archived" and "not posted twice" in r.msg, r.msg

    @pytest.mark.parametrize("shape", [
        {"has_more": None}, {"has_more": "false"}, {"has_more": True},
        {"has_more": True, "response_metadata": {"next_cursor": ""}},
    ], ids=["missing", "non_bool", "no_cursor_field", "empty_cursor"])
    def test_a_window_not_read_to_its_end_refuses_and_posts_nothing(self, fake, armed, shape):
        stage([_row(A1, "fx-dead-one")])
        _prior_attempt(A1, NOW + 5, "notice_indeterminate:timeout")
        real = fake.conversations_history

        def _hist(channel, oldest=None, latest=None, limit=100, cursor=None, inclusive=False, **kw):
            if _is_notice_read(oldest, latest):
                data = {"ok": True, "messages": [msg(0.001, user=PERSON)]}
                data.update({k: v for k, v in shape.items() if v is not None})
                return resp(data)
            return real(channel, oldest=oldest, latest=latest, limit=limit, cursor=cursor,
                        inclusive=inclusive)
        fake.conversations_history = _hist
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1", now=NOW + 100)
        assert r.outcome == "failed" and "posted nothing" in r.msg, r.msg
        assert _notices_to(fake, A1) == [] and "conversations_archive" not in fake.method_names()
        assert state(A1, now=NOW + 200)["state"] == st.OPEN

    def test_a_window_past_the_page_cap_refuses_and_posts_nothing(self, fake, armed):
        stage([_row(A1, "fx-dead-one")])
        _prior_attempt(A1, NOW + 5, "notice_indeterminate:timeout")
        real = fake.conversations_history
        pages = {"n": 0}

        def _hist(channel, oldest=None, latest=None, limit=100, cursor=None, inclusive=False, **kw):
            if _is_notice_read(oldest, latest):
                pages["n"] += 1
                return resp({"ok": True, "messages": [msg(0.001, user=PERSON)], "has_more": True,
                             "response_metadata": {"next_cursor": f"c{pages['n']}"}})
            return real(channel, oldest=oldest, latest=latest, limit=limit, cursor=cursor,
                        inclusive=inclusive)
        fake.conversations_history = _hist
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1", now=NOW + 100)
        assert pages["n"] == handler.NOTICE_CHECK_MAX_PAGES
        assert r.outcome == "failed" and "posted nothing" in r.msg, r.msg
        assert _notices_to(fake, A1) == []

    def test_a_later_refused_attempt_never_hides_an_earlier_standing_notice(self, fake, armed):
        """(c) attempt 1 posted the notice (archive refused, correction failed); attempt 2
        was refused before any notice; attempt 3 must still see attempt 1's notice."""
        stage([_row(A1, "fx-dead-one")])
        _prior_attempt(A1, NOW + 5, "failed:restricted_action")
        fake.history[A1].insert(0, _notice_msg(NOW + 5.5))
        _prior_attempt(A1, NOW + 3605, "not_attempted:the lane was demoted a moment ago")
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1", now=NOW + 7200)
        assert _notices_to(fake, A1) == [], r.msg
        assert r.outcome == "archived" and "not posted twice" in r.msg, r.msg

    def test_a_corrected_notice_still_gets_a_fresh_one(self, fake, armed):
        stage([_row(A1, "fx-dead-one")])
        _prior_attempt(A1, NOW + 5, "failed:restricted_action")
        fake.history[A1][:0] = [{"ts": f"{NOW + 6:.6f}", "type": "message", "user": BOT_UID,
                                 "text": cards.CORRECTION_TEXT}, _notice_msg(NOW + 5.5)]
        fake.history[A1].sort(key=lambda m: -float(m["ts"]))
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1", now=NOW + 100)
        assert r.outcome == "archived" and len(_notices_to(fake, A1)) == 1, r.msg

    def test_the_floor_starts_after_the_last_archive(self):
        cid = "C0FLOOR0001"
        assert handler._prior_attempt_ts(cid, NOW) is None
        _prior_attempt(cid, NOW - 10 * DAY, "archived")                   # archived; reopened since
        assert handler._prior_attempt_ts(cid, NOW) is None
        _prior_attempt(cid, NOW - 5 * DAY, "failed:restricted_action")
        _prior_attempt(cid, NOW - 2 * DAY, "not_attempted:no_write_client")
        assert handler._prior_attempt_ts(cid, NOW) == pytest.approx(NOW - 5 * DAY)
        _prior_attempt(cid, NOW - 1 * DAY, "archived (reconciled)")
        assert handler._prior_attempt_ts(cid, NOW) is None
        assert handler._prior_attempt_ts("C0OTHER0001", NOW) is None


# ── r2:c1-authority-tier#1 ─────────────────────────────────────────────────────────
def _mark_recheck(fake, monkeypatch):
    """Wrap (never replace) the REAL A12 re-check so its position among the Slack calls
    is visible in fake.calls."""
    real = gates.pre_notice_check

    def _marked():
        fake.calls.append(("pre_notice_check", {}))
        return real()
    monkeypatch.setattr(gates, "pre_notice_check", _marked)


def _during_notice_read(fake, side_effect):
    real = fake.conversations_history

    def _hist(channel, oldest=None, latest=None, limit=100, cursor=None, inclusive=False, **kw):
        if _is_notice_read(oldest, latest):
            side_effect()
        return real(channel, oldest=oldest, latest=latest, limit=limit, cursor=cursor,
                    inclusive=inclusive)
    fake.conversations_history = _hist


def _notice_index(fake):
    return next(n for n, c in enumerate(fake.calls)
                if c[0] == "chat_postMessage" and c[1]["text"].startswith(cards.NOTICE_PREFIX))


class TestTheA12RecheckIsImmediatelyBeforeTheWrite:

    def test_first_attempt_nothing_sits_between_the_recheck_and_the_notice(self, fake, armed,
                                                                           monkeypatch):
        stage([_row(A1, "fx-dead-one")])
        _mark_recheck(fake, monkeypatch)
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "archived", r.msg
        i = _notice_index(fake)
        assert fake.calls[i - 1][0] == "pre_notice_check", fake.method_names()[:i + 1]

    def test_a_retry_rechecks_after_the_notice_read_back(self, fake, armed, monkeypatch):
        stage([_row(A1, "fx-dead-one")])
        _prior_attempt(A1, NOW + 5, "failed:restricted_action")
        _mark_recheck(fake, monkeypatch)
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1", now=NOW + 100)
        assert r.outcome == "archived" and len(_notices_to(fake, A1)) == 1, r.msg
        i = _notice_index(fake)
        assert fake.calls[i - 1][0] == "pre_notice_check", fake.method_names()[:i + 1]

    def test_a_reused_notice_rechecks_immediately_before_the_archive(self, fake, armed, monkeypatch):
        stage([_row(A1, "fx-dead-one")])
        _prior_attempt(A1, NOW + 5, "notice_indeterminate:timeout")
        fake.history[A1].insert(0, _notice_msg(NOW + 5.5))
        _mark_recheck(fake, monkeypatch)
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1", now=NOW + 100)
        assert r.outcome == "archived" and "not posted twice" in r.msg, r.msg
        i = fake.method_names().index("conversations_archive")
        assert fake.calls[i - 1][0] == "pre_notice_check", fake.method_names()[:i + 1]

    def test_a_demotion_written_during_the_read_back_stops_the_notice(self, fake, armed):
        from datetime import datetime, timedelta, timezone
        stage([_row(A1, "fx-dead-one")])
        _prior_attempt(A1, NOW + 5, "failed:restricted_action")
        since = datetime.fromtimestamp(NOW + 50, timezone(timedelta(hours=-7))).isoformat()
        _during_notice_read(fake, lambda: st.write_demotion(
            {"since": since, "channel_id": "C0ROGUE0001", "archive_ts": "1790.1",
             "reason": "an archive by Cora with no tap-attributed ledger intent"}, dry_run=False))
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1", now=NOW + 100)
        assert r.outcome == "refused_transient" and "demoted" in r.msg, r.msg
        assert _notices_to(fake, A1) == [] and "conversations_archive" not in fake.method_names()
        assert st.read_ledger()[-1]["outcome"].startswith("not_attempted:")
        assert state(A1, now=NOW + 200)["state"] == st.OPEN

    def test_a_card_demoted_during_the_read_back_stops_the_notice(self, fake, armed):
        """A demotion seen (and perhaps already cleared) while this attempt read Slack:
        the card's own history makes it T0 for good -- no notice, no archive."""
        stage([_row(A1, "fx-dead-one")])
        _prior_attempt(A1, NOW + 5, "failed:restricted_action")
        _during_notice_read(fake, lambda: st.append_event(st.DEMOTED_SEEN, proposal_id=PID,
                                                          ts=NOW + 101))
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1", now=NOW + 100)
        assert r.outcome == "refused_transient" and "demoted after this card" in r.msg, r.msg
        assert _notices_to(fake, A1) == [] and "conversations_archive" not in fake.method_names()

    def test_a_registry_rollback_during_the_read_back_stops_a_reused_archive(self, fake, armed):
        stage([_row(A1, "fx-dead-one")])
        _prior_attempt(A1, NOW + 5, "notice_indeterminate:timeout")
        fake.history[A1].insert(0, _notice_msg(NOW + 5.5))
        _during_notice_read(fake, lambda: armed.write_text(
            "lanes:\n  - lane: slack-channel-archive\n    tier: T0\n", encoding="utf-8"))
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1", now=NOW + 100)
        assert r.outcome == "refused_transient" and "ladder registry" in r.msg, r.msg
        assert "conversations_archive" not in fake.method_names() and _notices_to(fake, A1) == []


# ── r2:c1-state-machine#3 ──────────────────────────────────────────────────────────
def _texts(blocks):
    out = []
    for b in blocks:
        if b.get("type") == "section":
            out.append(b["text"]["text"])
        elif b.get("type") == "context":
            out.extend(e["text"] for e in b["elements"])
        elif b.get("type") == "actions":
            out.extend(e["text"]["text"] for e in b["elements"])
    return out


class TestAChannelGoneSettlementIsTerminal:
    """The monitor settles an unknown / unfinished attempt whose channel left Slack's
    list as 'failed (reconciled: channel gone)'. The row must fold TERMINAL with honest
    copy -- never 'the channel stays open; tap to retry' with an Archive button."""

    MPID = "chanarch-aaaaaaaaaaaa"

    def _stage(self, mon_now, *, unknown_event: bool):
        from test_channel_archive_monitor import ARCH
        st.append_event("staged", proposal_id=self.MPID, ts=mon_now - 3 * DAY,
                        expires_ts=mon_now + 11 * DAY,
                        rows=[{"cid": ARCH, "section": "A", "tier": "T1", "name": "fx-arch",
                               "name_fp": reg.name_fp("fx-arch")}])
        st.append_event("delivered", proposal_id=self.MPID, page=1, dm_channel="DH",
                        message_ts=f"{mon_now - 3 * DAY + 1:.6f}", rendered_cids=[ARCH],
                        buttons=True, ts=mon_now - 3 * DAY)
        st.append_event(st.CLAIMED, proposal_id=self.MPID, cid=ARCH, kind="archive",
                        ts=mon_now - 2 * DAY - 10)
        st.append_ledger("intent", proposal_id=self.MPID, channel_id=ARCH, tapped_by=HARRISON,
                         ts=mon_now - 2 * DAY)
        if unknown_event:
            st.append_event(st.UNKNOWN, proposal_id=self.MPID, cid=ARCH, ts=mon_now - 2 * DAY + 5)
            st.append_ledger("outcome", proposal_id=self.MPID, channel_id=ARCH, outcome="unknown",
                             ts=mon_now - 2 * DAY + 5)
        return ARCH

    @pytest.mark.parametrize("unknown_event", [True, False], ids=["after_unknown", "claim_died"])
    def test_the_real_monitor_settlement_folds_terminal_with_honest_copy(self, unknown_event,
                                                                         monkeypatch, armed):
        from test_channel_archive_monitor import NOW as MNOW, MonSlack, run
        arch = self._stage(MNOW, unknown_event=unknown_event)
        run(MonSlack())                                   # the REAL monitor: ARCH is not listed
        assert [r["outcome"] for r in st.read_ledger() if r["event"] == "outcome"][-1] \
            == st.CHANNEL_GONE_OUTCOME
        f = st.fold(now=MNOW)
        p = f.proposals[self.MPID]
        assert p.state_of(arch) in st.TERMINAL and p.state_of(arch) == st.UNKNOWN
        assert cards.undecided_a_on_page(p, 1) == []
        blocks, text = cards.render_page(f, self.MPID, 1, now=MNOW)
        body = "\n".join(_texts(blocks))
        assert cards.CHANNEL_GONE_LINE in body, body
        assert "tap to retry" not in body and "stays open" not in body
        assert not [b for b in blocks if b.get("type") == "actions"
                    and any(e.get("action_id") in (cards.ACTION_ROW, cards.ACTION_ALL)
                            for e in b["elements"])]
        assert "outcome unknown 1" in text
        # a stale Archive button from an older render: refused as decided, no Slack write
        f2 = FakeSlack(channels=[], scopes=["channels:manage", "groups:write"])
        monkeypatch.setattr(clients, "read_client_factory", lambda: f2)
        monkeypatch.setattr(clients, "write_client_factory", lambda: f2)
        r = handler.process_tap(cards.ACTION_ROW, f"{self.MPID}:{arch}:T1", HARRISON, now=MNOW,
                                sleep=no_sleep)
        assert r.outcome == "already_handled" and r.ephemeral, r.msg
        assert not f2.posts and "conversations_archive" not in f2.method_names()

    def test_a_ledger_only_settlement_still_folds_terminal(self, armed):
        """The store event of the settlement failed to land (fail-soft): the claim-expiry
        branch reads the ledger outcome alone and must reach the same terminal row."""
        from test_channel_archive_monitor import NOW as MNOW
        arch = self._stage(MNOW, unknown_event=False)
        st.append_ledger("outcome", proposal_id=self.MPID, channel_id=arch,
                         outcome=st.CHANNEL_GONE_OUTCOME, ts=MNOW)
        p = st.fold(now=MNOW).proposals[self.MPID]
        assert p.state_of(arch) == st.UNKNOWN
        assert p.row_state[arch]["code"] == st.CHANNEL_GONE_CODE

    def test_the_monitor_writes_the_outcome_the_fold_keys_on(self):
        import ast
        from pathlib import Path
        src = (Path(handler.__file__).parent / "monitor.py").read_text(encoding="utf-8")
        lits = {n.value for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert st.CHANNEL_GONE_OUTCOME in lits

    def test_the_channel_gone_line_passes_both_rails(self, monkeypatch):
        _assert_rails([cards.CHANNEL_GONE_LINE], monkeypatch)


def _assert_rails(strings, monkeypatch):
    from cora import slack_egress as se
    for s in strings:
        assert se.screen_phantom_write_claims(s, tool_use_count=0) == s, s
        assert se.sanitize_text(s) == s, s
    monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
    for s in strings:
        assert se.screen_phantom_write_claims(s, tool_use_count=0) == s, s
    monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)


def _scan_row(fake, meta, ctx, tier="T0"):
    """The REAL classifier -> row_from_verdict, as the scan builds a card row."""
    v = cl.classify_channel(fake, meta, ctx, now=NOW, sleep=no_sleep)
    assert v.kind == cl.SECTION_B, (v.kind, v.reason)
    r = sc.row_from_verdict(meta, v)
    r["tier"] = tier
    return v, r


def _card_body(rows, pid=PID):
    stage(rows, pid=pid)
    return "\n".join(_texts(cards.render_page(st.fold(now=NOW + 5), pid, 1, now=NOW + 5)[0]))


PRIV = "C0PRIVQUIET1"


# ── r2:c1-false-inactive#2 ─────────────────────────────────────────────────────────
class TestAMembershipReadFailureIsNeverNotAMember:

    @pytest.mark.parametrize("err", [TimeoutError("read timed out"), api_error("ratelimited"),
                                     api_error("internal_error")],
                             ids=["timeout", "ratelimited", "internal_error"])
    def test_the_card_says_the_list_could_not_be_read(self, err):
        meta = chan(PRIV, "fx-quiet-private", private=True)
        f = FakeSlack(channels=[meta], history={PRIV: [msg(200)]}, members={PRIV: err})
        v, r = _scan_row(f, meta, context())
        assert v.reason == cl.B_LEX and v.harrison_member is None      # the A6 fail-safe stays
        body = _card_body([r])
        assert "you are not a member" not in body, body
        assert cards.MEMBERS_UNREAD_LINE in body
        assert "Exempt: LEX (custodian-owned)" in body                  # fail-safe section B kept

    def test_a_known_non_member_still_says_not_a_member(self):
        meta = chan(PRIV, "fx-quiet-private", private=True)
        f = FakeSlack(channels=[meta], history={PRIV: [msg(200)]}, members={PRIV: [PERSON]})
        v, r = _scan_row(f, meta, context())
        assert v.reason == cl.B_PRIVATE_NOT_MEMBER
        body = _card_body([r])
        assert "Private, and you are not a member" in body and cards.MEMBERS_UNREAD_LINE not in body

    def test_an_unreadable_primary_private_row_never_says_not_a_member(self):
        r = _row(PRIV, "fx-quiet-private", section="B", reason=cl.B_PRIVATE_NOT_MEMBER, tier="T0",
                 is_private=True, harrison_member=None, members_unreadable=True)
        body = _card_body([r])
        assert "you are not a member" not in body and "could not be read" in body, body

    def test_a_tap_after_the_read_recovers_is_stale_with_an_honest_reason(self, fake, armed):
        lexp = "C0LEXPRIV01"
        meta = chan(lexp, "lex-quiet-room", private=True)
        fake.channels.append(meta)
        fake.history[lexp] = [msg(200)]
        fake.members[lexp] = TimeoutError("read timed out")
        _v, r = _scan_row(fake, meta, context(), tier="T1")
        assert r["lex"] and "name" not in r and r["members_unreadable"]
        stage([r])
        fake.members[lexp] = [HARRISON, PERSON]                 # the read works at tap time
        res = tap(cards.ACTION_OVERRIDE, f"{PID}:{lexp}:T1")
        assert res.outcome == "stale_refused", res.msg
        assert "membership of it changed" not in res.msg and "could not be read" in res.msg
        assert "conversations_archive" not in fake.method_names() and not fake.posts

    def test_the_new_lines_pass_both_rails(self, monkeypatch):
        r = _row(PRIV, "fx-quiet-private", section="B", reason=cl.B_PRIVATE_NOT_MEMBER, tier="T0",
                 is_private=True, harrison_member=None, members_unreadable=True)
        _assert_rails([cards.MEMBERS_UNREAD_LINE, cards._b_line(r)], monkeypatch)


# ── r2:c1-false-inactive#1 ─────────────────────────────────────────────────────────
REGQ = "C0FXREGQ0001"          # in the synthetic registry fixture


def _reopened_history():
    """A person post at 300 d, Cora's archive at 200 d, a person's unarchive at 120 d."""
    return [msg(120, user=PERSON, subtype="channel_unarchive"),
            msg(200, user=BOT_UID, subtype="channel_archive"),
            msg(300)]


class TestAnUnarchiveIsDisclosedOnEveryBRow:

    @pytest.mark.parametrize("cid,name,want_reason", [
        (REGQ, "fx-registry-quiet", cl.B_REGISTRY),
        ("C0LEXQUIET1", "lex-quiet-room", cl.B_LEX),
        ("C0KEEPQUIET", "cora-old-room", cl.B_KEEP_LIST),
    ], ids=["registry", "lex", "keep_list"])
    def test_a_higher_ranked_reason_never_hides_the_unarchive(self, cid, name, want_reason):
        meta = chan(cid, name)
        f = FakeSlack(channels=[meta], history={cid: _reopened_history()})
        ctx = context(unarchive_state={cid: {"at": NOW - 120 * DAY, "by": PERSON}})
        v, r = _scan_row(f, meta, ctx)
        assert v.reason == want_reason
        body = _card_body([r])
        assert f"Unarchived by <@{PERSON}> on {cards._date(NOW - 120 * DAY)} after an earlier archive" \
            in body, body
        assert r.get("unarchived") is True and r.get("unarchived_by") == PERSON

    def test_the_fail_closed_unconfirmed_form_is_disclosed_honestly(self):
        meta = chan(REGQ, "fx-registry-quiet")
        f = FakeSlack(channels=[meta], history={REGQ: [msg(300)]})
        ctx = context(unarchive_state={REGQ: {"at": None, "by": "", "unconfirmed": True}})
        _v, r = _scan_row(f, meta, ctx)
        body = _card_body([r])
        assert "Reopened after an earlier archive — who reopened it, and when, is not known." in body
        assert "on ?" not in body

    def test_the_primary_unarchived_line_never_says_on_question_mark(self):
        meta = chan("C0REOPEN001", "fx-reopened")
        f = FakeSlack(channels=[meta], history={"C0REOPEN001": [msg(300)]})
        ctx = context(unarchive_state={"C0REOPEN001": {"at": None, "by": "", "unconfirmed": True}})
        v, r = _scan_row(f, meta, ctx)
        assert v.reason == cl.B_UNARCHIVED_BEFORE
        body = _card_body([r])
        assert "someone on ?" not in body and body.count("Reopened after an earlier archive") == 1

    def _stage_registry_t1(self, fake, unarchive_state=None):
        meta = chan(REGQ, "fx-registry-quiet")
        fake.channels.append(meta)
        fake.history[REGQ] = [msg(300)]
        _v, r = _scan_row(fake, meta, context(unarchive_state=unarchive_state or {}), tier="T1")
        stage([r])

    def test_a_t1_override_refuses_when_an_unarchive_appeared_since_the_card(self, fake, armed):
        self._stage_registry_t1(fake)                        # the card showed no unarchive
        # since the card: another card's archive of it, and it is open again (fail-closed form)
        st.append_ledger("outcome", proposal_id="chanarch-000000000077", channel_id=REGQ,
                         outcome="archived", ts=NOW - 100 * DAY)
        res = tap(cards.ACTION_OVERRIDE, f"{PID}:{REGQ}:T1")
        assert res.outcome == "stale_refused" and "archive history changed" in res.msg, res.msg
        assert "conversations_archive" not in fake.method_names() and not fake.posts

    def test_a_t1_override_with_the_same_disclosed_unarchive_proceeds(self, fake, armed):
        st.append_ledger("outcome", proposal_id="chanarch-000000000077", channel_id=REGQ,
                         outcome="archived", ts=NOW - 100 * DAY)
        self._stage_registry_t1(fake, st.unarchive_state(st.read_ledger()))
        res = tap(cards.ACTION_OVERRIDE, f"{PID}:{REGQ}:T1")
        assert res.outcome == "archived", res.msg

    def test_the_unarchive_lines_pass_both_rails(self, monkeypatch):
        rows = [_row(REGQ, "fx-registry-quiet", section="B", reason=cl.B_REGISTRY, tier="T0",
                     unarchived=True, unarchived_by=PERSON, unarchived_at=NOW - 120 * DAY),
                _row(REGQ, "fx-registry-quiet", section="B", reason=cl.B_UNARCHIVED_BEFORE,
                     tier="T0", unarchived=True, unarchived_by="", unarchived_at=None)]
        _assert_rails([cards._b_line(r) for r in rows], monkeypatch)
