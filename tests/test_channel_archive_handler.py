"""Code #16 C1 -- the tap handler (kickoff section 3 + amendments A5, A11-A17).

T0: a tap records Harrison's agreement and archives nothing. T1 (registry promoted +
flag act + scope known present): claim -> live re-verify with the ONE classifier ->
ledger intent (fsynced) -> the notice FIRST -> conversations.archive -> read-back ->
outcome. "Archived" only after ok + read-back + intent. conversations.archive has
exactly one call site in src/ (AST pin).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from _chanarch_fakes import (BOT_ID, BOT_UID, DAY, HARRISON, NOW, PERSON, FakeSlack, api_error,
                             chan, msg, no_sleep)
from cora.channel_archive import cards, clients, gates, handler, policy
from cora.channel_archive import registry as reg
from cora.channel_archive import store as st

REPO = Path(__file__).resolve().parents[1]
PID = "chanarch-abcabcabcabc"
A1, A2, A3, B1 = "C0DEADAAA1", "C0DEADAAA2", "C0DEADAAA3", "C0FXREGQ0001"


def _row(cid, name, section="A", tier="T0", **kw):
    return {"cid": cid, "section": section, "reason": kw.pop("reason", ""), "tier": tier,
            "lex": False, "name": name, "name_fp": reg.name_fp(name),
            "is_private": kw.pop("is_private", False), "last_person_days": 200,
            "history_complete": True, "keep_count": kw.pop("keep_count", 0), **kw}


def stage(tier="T0", rows=None, ts=NOW, pid=PID, pages=None):
    rows = rows or [_row(A1, "fx-dead-one", tier=tier), _row(A2, "fx-dead-two", tier=tier),
                    _row(B1, "fx-registry-quiet", section="B", reason="registry", tier=tier)]
    st.append_event("staged", proposal_id=pid, ts=ts, expires_ts=ts + 14 * DAY, rows=rows,
                    counts={}, scanned=len(rows))
    for n, cids in enumerate(pages or [[r["cid"] for r in rows]], 1):
        st.append_event("delivered", proposal_id=pid, page=n, dm_channel="DHARRISON1",
                        message_ts=f"{ts + n:.6f}", rendered_cids=cids, buttons=True, ts=ts)
    return pid


def world(**kw):
    chans = [chan(A1, "fx-dead-one"), chan(A2, "fx-dead-two"), chan(A3, "fx-dead-three"),
             chan(B1, "fx-registry-quiet")]
    hist = {A1: [msg(200)], A2: [msg(210)], A3: [msg(220)], B1: [msg(300)]}
    base = dict(channels=chans, history=hist, scopes=["channels:manage", "groups:write", "chat:write"])
    base.update(kw)
    return FakeSlack(**base)


@pytest.fixture
def fake(monkeypatch):
    f = world()
    monkeypatch.setattr(clients, "read_client_factory", lambda: f)
    monkeypatch.setattr(clients, "write_client_factory", lambda: f)
    return f


@pytest.fixture
def no_clients(monkeypatch):
    def _boom():
        raise AssertionError("a T0 tap must never build a Slack client")
    monkeypatch.setattr(clients, "read_client_factory", _boom)
    monkeypatch.setattr(clients, "write_client_factory", _boom)


@pytest.fixture
def armed(monkeypatch, tmp_path):
    """Registry T1 + CORA_CHANNEL_ARCHIVE=act (the promoted lane)."""
    p = tmp_path / "ladder.yaml"
    p.write_text("lanes:\n  - lane: slack-channel-archive\n    tier: T1\n", encoding="utf-8")
    monkeypatch.setenv("CORA_LADDER_REGISTRY_PATH", str(p))
    monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "act")


def tap(action, value, actor=HARRISON, now=NOW + 10, **kw):
    return handler.process_tap(action, value, actor, now=now, sleep=no_sleep, **kw)


def state(cid, pid=PID, now=NOW + 20):
    return st.fold(now=now).proposals[pid].state_of(cid)


class TestT0:
    def test_mark_records_agreement_and_touches_no_slack(self, no_clients):
        stage()
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}")
        assert r.outcome == "agreed" and state(A1) == st.AGREED
        assert "Nothing was archived: the lane is at T0" in r.msg and r.page == 1
        assert st.read_ledger() == []                  # the ledger holds archive attempts only

    def test_a_non_harrison_tap_is_refused_ephemerally_before_any_lookup(self, no_clients):
        stage()
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}", actor=PERSON)
        assert r.outcome == "not_authorized" and r.ephemeral and state(A1) == st.OPEN
        r2 = tap(cards.ACTION_ROW, f"chanarch-000000000000:{A1}", actor=PERSON)
        assert r2.outcome == "not_authorized"            # no probing which ids exist

    @pytest.mark.parametrize("value", ["", "garbage", f"{PID}", f"{PID}:nope", f"chanarch-zz:{A1}",
                                       f"chanarch-000000000000:{A1}"])
    def test_malformed_or_unknown_values_are_orphaned(self, no_clients, value):
        stage()
        assert tap(cards.ACTION_ROW, value).outcome == "orphaned"

    def test_buttons_are_bound_to_their_section(self, no_clients):
        stage()
        assert tap(cards.ACTION_ROW, f"{PID}:{B1}").outcome == "orphaned"
        assert tap(cards.ACTION_OVERRIDE, f"{PID}:{A1}").outcome == "orphaned"
        r = tap(cards.ACTION_OVERRIDE, f"{PID}:{B1}")
        assert r.outcome == "agreed" and "override of its registry exemption" in r.msg
        assert st.fold(now=NOW + 20).proposals[PID].row_state[B1]["override"] is True

    def test_a_double_tap_is_already_handled(self, no_clients):
        stage()
        tap(cards.ACTION_ROW, f"{PID}:{A1}")
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}")
        assert r.outcome == "already_handled" and r.ephemeral

    def test_keep_then_keep_again_and_the_cap(self, no_clients):
        stage(rows=[_row(A1, "fx-dead-one"), _row(A2, "fx-dead-two")])
        r = tap(cards.ACTION_KEEP, f"{PID}:{A1}")
        assert r.outcome == "kept" and "kept x1" in r.msg and state(A1) == st.KEPT
        assert tap(cards.ACTION_KEEP, f"{PID}:{A1}").outcome == "already_handled"
        for i in range(2):
            st.append_event(st.KEPT, proposal_id="chanarch-000000000001", cid=A2, ts=NOW - 400 * DAY - i)
        r2 = tap(cards.ACTION_KEEP, f"{PID}:{A2}")
        assert r2.outcome == "noop" and "kept 2 times" in r2.msg and state(A2) == st.OPEN

    def test_mark_all_acts_only_on_its_own_page_and_never_section_b(self, no_clients):
        rows = [_row(A1, "fx-dead-one"), _row(A2, "fx-dead-two"), _row(A3, "fx-dead-three"),
                _row(B1, "fx-registry-quiet", section="B", reason="registry")]
        stage(rows=rows, pages=[[A1, A2, B1], [A3]])
        r = tap(cards.ACTION_ALL, f"{PID}:p1")
        assert r.outcome == "archive_all" and "Marked 2 of 2 shown" in r.msg
        assert (state(A1), state(A2), state(A3), state(B1)) == (st.AGREED, st.AGREED, st.OPEN, st.OPEN)

    def test_card_agreed_is_recorded_once(self, no_clients):
        stage()
        assert tap(cards.ACTION_AGREED, PID).outcome == "card_agreed"
        assert st.fold(now=NOW + 20).proposals[PID].card_agreed_by == HARRISON
        assert tap(cards.ACTION_AGREED, PID).outcome == "noop"

    def test_superseded_and_expired_cards_do_nothing(self, no_clients):
        stage()
        stage(pid="chanarch-000000000009", ts=NOW + 50)
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}", now=NOW + 60)
        assert r.outcome == "superseded" and not r.ephemeral and state(A1, now=NOW + 60) == st.OPEN
        r2 = tap(cards.ACTION_ROW, f"chanarch-000000000009:{A1}", now=NOW + 50 + 15 * DAY)
        assert r2.outcome == "expired" and "expired" in r2.msg

    def test_a_t0_row_is_never_routed_to_the_act_pool(self):
        stage()
        assert not handler.needs_act_pool(cards.ACTION_ROW, f"{PID}:{A1}")
        assert not handler.needs_act_pool(cards.ACTION_ALL, f"{PID}:p1")
        stage(tier="T1", pid="chanarch-000000000007", ts=NOW + 5)
        assert handler.needs_act_pool(cards.ACTION_ROW, f"chanarch-000000000007:{A1}:T1")
        assert handler.needs_act_pool(cards.ACTION_ALL, "chanarch-000000000007:p1:T1")
        # a Mark-labelled (or unmarked) button on a T1 row only records: inline
        assert not handler.needs_act_pool(cards.ACTION_ROW, f"chanarch-000000000007:{A1}:T0")
        assert not handler.needs_act_pool(cards.ACTION_ROW, f"chanarch-000000000007:{A1}")
        assert not handler.needs_act_pool(cards.ACTION_ALL, "chanarch-000000000007:p1:T0")


class TestT1Gates:
    def test_registry_still_t0_records_instead_of_archiving(self, fake, monkeypatch):
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "act")        # but the real registry is T0
        stage(tier="T1")
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "agreed" and "registry says the lane is at T0" in r.msg
        assert "conversations_archive" not in fake.method_names() and not fake.posts

    def test_flag_not_act_in_this_process_is_transient(self, fake, monkeypatch, armed):
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "propose")
        stage(tier="T1")
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "refused_transient" and state(A1) == st.OPEN
        assert "archive switch is not on" in r.msg

    def test_a_demotion_after_the_card_records_t0_equivalent(self, fake, armed):
        stage(tier="T1")
        policy.demotion_path().write_text('{"since": "x"}', encoding="utf-8")
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "agreed" and "demoted" in r.msg
        assert "conversations_archive" not in fake.method_names()

    def test_scope_unknown_is_transient_and_scope_missing_records_with_the_scope_named(
            self, fake, armed):
        stage(tier="T1", rows=[_row(A1, "fx-dead-one", tier="T1"),
                               _row(A2, "fx-dead-two", tier="T1", is_private=True)])
        fake.scopes = None
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "refused_transient" and state(A1) == st.OPEN
        fake.scopes = ["channels:manage"]                   # groups:write missing
        r2 = tap(cards.ACTION_ROW, f"{PID}:{A2}:T1")
        assert r2.outcome == "agreed"
        assert "the Slack scope groups:write is missing — Harrison adds it" in r2.msg
        assert "conversations_archive" not in fake.method_names()


class TestArchivePath:
    def test_the_happy_path_orders_notice_before_archive_and_ledgers_both_rows(self, fake, armed):
        stage(tier="T1")
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "archived", r.msg
        assert r.msg.startswith(f"Archived <#{A1}>") and "ledger row written" in r.msg
        names = fake.method_names()
        notice_i = next(i for i, c in enumerate(fake.calls)
                        if c[0] == "chat_postMessage" and c[1]["channel"] == A1)
        assert names.index("conversations_archive") > notice_i
        assert names.count("conversations_archive") == 1
        assert "Archiving for inactivity — 200 days" in fake.calls[notice_i][1]["text"]
        rows = st.read_ledger()
        assert [x["event"] for x in rows] == ["intent", "outcome"]
        assert rows[0]["channel_id"] == A1 and rows[0]["tapped_by"] == HARRISON
        assert rows[0]["channel_name"] == "fx-dead-one" and rows[0]["age_days"] == 200
        assert rows[1]["outcome"] == "archived"
        assert state(A1) == st.ARCHIVED

    def test_the_intent_is_written_before_any_slack_write(self, fake, armed):
        stage(tier="T1")
        seen: list = []
        fake.post_behaviour = lambda kw: seen.append([x["event"] for x in (st.read_ledger() or [])])
        tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert seen and seen[0] == ["intent"]

    def test_a_failed_intent_write_refuses_and_keeps_the_buttons(self, fake, armed, monkeypatch):
        stage(tier="T1")
        real = st.append_ledger
        monkeypatch.setattr(st, "append_ledger",
                            lambda ev, **kw: False if ev == "intent" else real(ev, **kw))
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "failed" and "ledger write failed" in r.msg
        assert not fake.posts and "conversations_archive" not in fake.method_names()
        assert state(A1) == st.OPEN

    def test_an_unreadable_ledger_refuses_before_the_intent(self, fake, armed, monkeypatch, tmp_path):
        stage(tier="T1")
        d = tmp_path / "isadir"
        d.mkdir()
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_LEDGER_PATH", str(d))
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "failed" and "reverify_store_unreadable" in r.msg
        assert not fake.posts and "conversations_archive" not in fake.method_names()

    def test_a_lex_row_ledgers_no_name(self, fake, armed):
        row = _row("C0LEXLEX01", "lex-old", tier="T1", section="B", reason="lex")
        row["lex"] = True
        row.pop("name")
        fake.channels.append(chan("C0LEXLEX01", "lex-old"))
        fake.history["C0LEXLEX01"] = [msg(300)]
        stage(tier="T1", rows=[row])
        r = tap(cards.ACTION_OVERRIDE, f"{PID}:C0LEXLEX01:T1")
        assert r.outcome == "archived", r.msg
        intent = st.read_ledger()[0]
        assert intent["lex"] is True and "channel_name" not in intent and intent["override"] is True

    @pytest.mark.parametrize("mutate,why", [
        (lambda f: f.history[A1].insert(0, msg(1)), "a person posted since the card"),
        (lambda f: f.info_override.__setitem__(A1, {"name": "fx-renamed"}), "renamed"),
        (lambda f: f.pins.__setitem__(A1, 1), "exempt now (pinned)"),
        (lambda f: f.history[A1].append(msg(150, reply_count=1, latest_reply=f"{NOW - DAY:.6f}")),
         "a person posted since the card"),
    ])
    def test_the_live_reverify_refuses_a_changed_channel(self, fake, armed, mutate, why):
        stage(tier="T1")
        mutate(fake)
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "stale_refused" and why in r.msg, r.msg
        assert not fake.posts and "conversations_archive" not in fake.method_names()
        assert state(A1) == st.STALE and st.read_ledger() == []

    def test_a_registry_row_added_after_the_card_is_refused(self, fake, armed):
        """A channel staged as a clean A row that is in the registry at tap time."""
        stage(tier="T1", rows=[_row(B1, "fx-registry-quiet", tier="T1")])
        r = tap(cards.ACTION_ROW, f"{PID}:{B1}:T1")
        assert r.outcome == "stale_refused" and "no longer a clean candidate (registry)" in r.msg

    def test_a_keep_since_the_card_is_refused(self, fake, armed):
        stage(tier="T1")
        st.append_event(st.KEPT, proposal_id="chanarch-000000000003", cid=A1, ts=NOW + 1)
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "stale_refused" and "kept since the card" in r.msg

    def test_a_reverify_read_error_is_retryable(self, fake, armed):
        stage(tier="T1")
        fake.info_override[A1] = api_error("ratelimited")
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "failed" and "reverify_ratelimited" in r.msg
        assert state(A1) == st.OPEN and not fake.posts

    def test_an_unreadable_registry_at_tap_time_is_retryable(self, fake, armed, monkeypatch, tmp_path):
        stage(tier="T1")
        monkeypatch.setenv("CORA_CHANNEL_REGISTRY_PATH", str(tmp_path / "gone.md"))
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "failed" and "reverify_registry_unreadable" in r.msg
        assert state(A1) == st.OPEN

    def test_a_failed_notice_stops_before_the_archive(self, fake, armed):
        stage(tier="T1")
        fake.post_behaviour = lambda kw: api_error("not_in_channel") if kw["channel"] == A1 else None
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "failed" and "did not post (not_in_channel)" in r.msg
        assert "conversations_archive" not in fake.method_names()
        assert st.read_ledger()[-1]["outcome"] == "notice_failed:not_in_channel"
        assert state(A1) == st.FAILED

    def test_an_api_refusal_posts_the_correction_and_keeps_the_button(self, fake, armed):
        stage(tier="T1")
        fake.archive_behaviour = api_error("restricted_action")
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "failed" and "restricted_action" in r.msg
        texts = [p["text"] for p in fake.posts if p["channel"] == A1]
        assert texts[-1] == cards.CORRECTION_TEXT and len(texts) == 2
        assert state(A1) == st.FAILED and "Archived" not in r.msg

    @pytest.mark.parametrize("archiver,want", [({"user": BOT_UID}, "archived"),
                                               ({"user": None, "bot_id": BOT_ID}, "archived"),
                                               ({"user": PERSON}, "already_archived")])
    def test_already_archived_reads_the_archiver(self, fake, armed, archiver, want):
        stage(tier="T1")
        fake.archive_behaviour = api_error("already_archived")
        orig = fake.conversations_history

        def _hist(channel, oldest=None, latest=None, limit=100, cursor=None, inclusive=False, **kw):
            if limit == 20:
                m = {"ts": f"{NOW:.6f}", "subtype": "channel_archive", **archiver}
                return __import__("_chanarch_fakes").resp({"ok": True, "messages": [m], "has_more": False})
            return orig(channel, oldest=oldest, latest=latest, limit=limit, cursor=cursor,
                        inclusive=inclusive)
        fake.conversations_history = _hist
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == want

    @pytest.mark.parametrize("readback,want", [(True, "archived"), (False, "failed"), ("error", "unknown")])
    def test_a_timeout_gets_one_read_back_never_a_retry(self, fake, armed, readback, want):
        stage(tier="T1")

        def _timeout(channel):
            if readback is True:
                fake.archived.add(channel)
            return TimeoutError("read timed out")
        fake.archive_behaviour = _timeout
        if readback == "error":
            calls = {"n": 0}
            orig_info = fake.conversations_info

            def _info(channel, **kw):
                calls["n"] += 1
                if calls["n"] > 1:             # the re-verify read passes; the read-back fails
                    raise api_error("internal_error")
                return orig_info(channel, **kw)
            fake.conversations_info = _info
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == want, r.msg
        assert fake.method_names().count("conversations_archive") == 1
        if want == "failed":
            assert "timed out" in r.msg and fake.posts[-1]["text"] == cards.CORRECTION_TEXT
            assert state(A1) == st.FAILED
        if want == "unknown":
            assert "Outcome unknown" in r.msg and "Archived" not in r.msg
            assert state(A1) == st.UNKNOWN

    def test_ok_but_the_read_back_disagrees_is_unknown_not_archived(self, fake, armed):
        stage(tier="T1")
        fake.archive_behaviour = lambda ch: "ok_no_effect"
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "unknown" and "Archived" not in r.msg
        assert st.read_ledger()[-1]["outcome"] == "unknown"

    def test_the_pre_notice_recheck_stops_after_the_intent(self, fake, armed, monkeypatch):
        stage(tier="T1")
        monkeypatch.setattr(gates, "pre_notice_check", lambda: (False, "the lane was demoted a moment ago"))
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "refused_transient" and not fake.posts
        assert st.read_ledger()[-1]["outcome"].startswith("not_attempted:")
        assert state(A1) == st.OPEN


def _rendered_values(page=1, now=NOW + 10):
    """label -> value of every button on the page, drawn by the REAL renderer."""
    blocks, _ = cards.render_page(st.fold(now=now), PID, page, now=now)
    return {e["text"]["text"]: e["value"] for b in blocks if b.get("type") == "actions"
            for e in b["elements"]}


def _demote(since_ts=NOW + 5):
    from datetime import datetime, timedelta, timezone
    since = datetime.fromtimestamp(since_ts, timezone(timedelta(hours=-7))).isoformat(timespec="seconds")
    assert st.write_demotion({"since": since, "channel_id": "C0ROGUE0001", "archive_ts": "1790.1",
                              "reason": "an archive by Cora with no tap-attributed ledger intent"},
                             dry_run=False)


class TestDemotionHistoryA12:
    """c1-authority-tier#0: a T1 card that outlived a demotion stays T0-equivalent after
    Harrison clears it, and a button archives only when its LABEL said Archive."""

    def test_a_card_redrawn_during_a_demotion_never_archives_after_the_clear(self, fake, armed):
        stage(tier="T1")
        assert set(_rendered_values()) >= {"Archive", "Archive all 2 shown"}
        _demote()
        r0 = tap(cards.ACTION_KEEP, f"{PID}:{A2}")          # any tap re-renders the page
        assert r0.outcome == "kept"
        drawn = _rendered_values()                           # what _ca_rerender now shows
        assert "Mark to archive" in drawn and "Archive" not in drawn
        assert st.clear_demotion(actor=HARRISON, dry_run=False)["cleared"]
        assert not policy.is_demoted()
        r = tap(cards.ACTION_ROW, drawn["Mark to archive"])
        assert r.outcome == "agreed" and "demoted after this card went out" in r.msg, r.msg
        assert "conversations_archive" not in fake.method_names() and not fake.posts
        assert state(A1) == st.AGREED and st.read_ledger()[-1]["event"] == "acknowledged"

    def test_mark_all_drawn_during_a_demotion_never_archives_after_the_clear(self, fake, armed):
        rows = [_row(A1, "fx-dead-one", tier="T1"), _row(A2, "fx-dead-two", tier="T1"),
                _row(A3, "fx-dead-three", tier="T1")]
        stage(tier="T1", rows=rows)
        _demote()
        tap(cards.ACTION_KEEP, f"{PID}:{A3}")
        drawn = _rendered_values()
        st.clear_demotion(actor=HARRISON, dry_run=False)
        r = tap(cards.ACTION_ALL, drawn["Mark all 2 shown to archive"])
        assert r.outcome == "archive_all" and "Nothing was archived" in r.msg, r.msg
        assert fake.method_names().count("conversations_archive") == 0 and not fake.posts
        assert (state(A1), state(A2)) == (st.AGREED, st.AGREED)

    def test_a_card_never_redrawn_still_records_after_a_cleared_demotion(self, fake, armed):
        """No tap during the demotion: the page still shows Archive (:T1) -- the ledger's
        acknowledged row (demoted_since after the card) keeps it T0-equivalent."""
        stage(tier="T1")
        drawn = _rendered_values()
        assert drawn["Archive"].endswith(":T1")
        _demote()
        st.clear_demotion(actor=HARRISON, dry_run=False)
        r = tap(cards.ACTION_ROW, drawn["Archive"])
        assert r.outcome == "agreed" and "demoted after this card went out" in r.msg
        assert "conversations_archive" not in fake.method_names() and not fake.posts
        r2 = tap(cards.ACTION_ALL, drawn["Archive all 2 shown"])
        assert "Nothing was archived" in r2.msg and not fake.posts
        assert "Archive" not in _rendered_values()          # the page itself now says Mark

    def test_a_demotion_seen_by_a_tap_is_persisted_even_without_an_ack_row(self, fake, armed):
        stage(tier="T1")
        policy.demotion_path().write_text("{corrupt", encoding="utf-8")   # unreadable = demoted
        tap(cards.ACTION_KEEP, f"{PID}:{A2}")
        assert any(e.get("event") == st.DEMOTED_SEEN and e.get("proposal_id") == PID
                   for e in st.read_events())
        policy.demotion_path().unlink()                       # gone without any ledger row
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "agreed" and "demoted after this card went out" in r.msg
        assert "conversations_archive" not in fake.method_names()

    def test_clearing_an_unreadable_demotion_still_leaves_its_history(self, fake, armed):
        stage(tier="T1")
        policy.demotion_path().write_text("{corrupt", encoding="utf-8")
        assert st.clear_demotion(actor=HARRISON, dry_run=False)["cleared"]
        ack = st.read_ledger()[-1]
        assert ack["event"] == "acknowledged" and ack["by"] == HARRISON
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1", now=NOW + 30)
        assert r.outcome == "agreed" and not fake.posts

    def test_a_card_staged_after_the_clear_archives_normally(self, fake, armed):
        _demote(since_ts=NOW - 3 * DAY)
        st.clear_demotion(actor=HARRISON, dry_run=False)
        stage(tier="T1")                                      # created NOW, after the clear
        r = tap(cards.ACTION_ROW, _rendered_values()["Archive"])
        assert r.outcome == "archived", r.msg

    @pytest.mark.parametrize("value", [f"{PID}:{A1}", f"{PID}:{A1}:T0"],
                             ids=["unmarked", "mark-labelled"])
    def test_only_an_archive_labelled_value_archives(self, fake, armed, value):
        stage(tier="T1")
        r = tap(cards.ACTION_ROW, value)
        assert r.outcome == "agreed" and "Mark button" in r.msg, r.msg
        assert "conversations_archive" not in fake.method_names() and not fake.posts
        assert tap(cards.ACTION_ROW, f"{PID}:{A2}:T1").outcome == "archived"

    def test_every_rendered_button_carries_the_tier_its_label_shows(self):
        rows = [_row(A1, "fx-dead-one", tier="T1"),
                _row(B1, "fx-registry-quiet", section="B", reason="registry", tier="T1")]
        stage(tier="T1", rows=rows)
        v = _rendered_values()
        assert v["Archive"] == f"{PID}:{A1}:T1" and v["Archive (override)"] == f"{PID}:{B1}:T1"
        assert v["Archive all 1 shown"] == f"{PID}:p1:T1" and v["Keep"] == f"{PID}:{B1}"
        policy.demotion_path().write_text('{"since": "x"}', encoding="utf-8")
        v2 = _rendered_values()
        assert v2["Mark to archive"] == f"{PID}:{A1}:T0"
        assert v2["Mark to archive (override)"] == f"{PID}:{B1}:T0"
        assert v2["Mark all 1 shown to archive"] == f"{PID}:p1:T0"
        for value in v2.values():
            assert handler.button_tier(cards.ACTION_ROW, value) == "T0"


BUSY = "C0BUSYBOT01"


def _busy_history(older: int):
    """30 Cora posts in the last 60 days, then *older* person messages from day 91 on."""
    hist = [msg(1 + i * 2, user=BOT_UID) for i in range(30)]
    return hist + [msg(91 + i * 0.1) for i in range(older)]


class TestReverifyReadErrorsAreRetryable:
    """c1-authority-tier#1 (A5): a read error during the tap-time re-verify is a
    RETRYABLE failure (claim released, button kept) -- never a terminal stale_refused
    with a false reason ('reclassified as LEX' / 'a person posted')."""

    @pytest.mark.parametrize("err", [api_error("ratelimited"), api_error("internal_error"),
                                     TimeoutError("read timed out")],
                             ids=["ratelimited", "internal_error", "timeout"])
    def test_members_unreadable_at_tap_time_is_retryable_not_lex(self, fake, armed, err):
        stage(tier="T1")
        fake.members[A1] = err
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "failed" and "reverify_members_unknown" in r.msg, r.msg
        assert "lex" not in r.msg and state(A1) == st.OPEN
        assert not fake.posts and "conversations_archive" not in fake.method_names()
        fake.members.pop(A1)                                  # the read works again
        assert tap(cards.ACTION_ROW, f"{PID}:{A1}:T1").outcome == "archived"

    def test_a_users_info_failure_that_decides_active_is_retryable(self, fake, armed):
        stage(tier="T1")
        fake.history[A1].insert(0, msg(1, user="UNEWPOSTER"))
        fake.users["UNEWPOSTER"] = api_error("ratelimited")
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "failed" and "reverify_users_unknown" in r.msg, r.msg
        assert "a person posted" not in r.msg and state(A1) == st.OPEN and not fake.posts

    def test_a_bot_traffic_row_whose_poster_lookup_fails_is_retryable(self, fake, armed):
        from _chanarch_fakes import BOTUSER
        row = _row(BUSY, "fx-bot-room", section="B", reason="bot_traffic", tier="T1",
                   bot_posts=1, bot_latest_days=3)
        fake.channels.append(chan(BUSY, "fx-bot-room"))
        fake.history[BUSY] = [msg(3, user=BOTUSER, bot_id="BOTHER"), msg(200)]
        fake.users[BOTUSER] = api_error("internal_error")
        stage(tier="T1", rows=[row])
        r = tap(cards.ACTION_OVERRIDE, f"{PID}:{BUSY}:T1")
        assert r.outcome == "failed" and "reverify_users_unknown" in r.msg, r.msg
        assert state(BUSY) == st.OPEN and not fake.posts

    def test_a_readable_person_post_is_still_stale(self, fake, armed):
        stage(tier="T1")
        fake.history[A1].insert(0, msg(1))
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "stale_refused" and "a person posted since the card" in r.msg


class TestReverifySecondaryFlags:
    """c1-false-inactive#0: the T1 re-verify of a B row requires the SAME thread-cap and
    private-not-member flags the card disclosed -- a changed flag is stale_refused with
    an honest reason, never an archive on a card that showed something else."""

    def _stage_busy(self, *, capped: bool):
        row = _row(BUSY, "fx-busy-room", section="B", reason="bot_traffic", tier="T1",
                   bot_posts=30, bot_latest_days=1)
        row["history_complete"] = not capped
        row["last_person_days"] = 91
        stage(tier="T1", rows=[row])

    def test_matching_capped_flags_proceed(self, fake, armed):
        fake.channels.append(chan(BUSY, "fx-busy-room"))
        fake.history[BUSY] = _busy_history(2300)
        self._stage_busy(capped=True)
        r = tap(cards.ACTION_OVERRIDE, f"{PID}:{BUSY}:T1")
        assert r.outcome == "archived", r.msg

    def test_a_card_that_said_capped_but_now_reads_complete_is_stale(self, fake, armed):
        fake.channels.append(chan(BUSY, "fx-busy-room"))
        fake.history[BUSY] = _busy_history(100)
        self._stage_busy(capped=True)
        r = tap(cards.ACTION_OVERRIDE, f"{PID}:{BUSY}:T1")
        assert r.outcome == "stale_refused" and "older-thread check changed" in r.msg, r.msg
        assert "conversations_archive" not in fake.method_names() and not fake.posts

    def test_a_card_that_said_complete_but_now_reads_capped_is_stale(self, fake, armed):
        fake.channels.append(chan(BUSY, "fx-busy-room"))
        fake.history[BUSY] = _busy_history(2300)
        self._stage_busy(capped=False)
        r = tap(cards.ACTION_OVERRIDE, f"{PID}:{BUSY}:T1")
        assert r.outcome == "stale_refused" and "older-thread check changed" in r.msg, r.msg
        assert "conversations_archive" not in fake.method_names() and not fake.posts

    def test_a_membership_change_on_a_private_row_is_stale(self, fake, armed):
        row = _row(B1, "fx-registry-quiet", section="B", reason="registry", tier="T1",
                   is_private=True)
        row["harrison_member"] = False                    # the card said: not a member
        fake.channels[:] = [c for c in fake.channels if c["id"] != B1]
        fake.channels.append(chan(B1, "fx-registry-quiet", private=True))
        fake.members[B1] = [HARRISON, PERSON]             # ...he is one now
        stage(tier="T1", rows=[row])
        r = tap(cards.ACTION_OVERRIDE, f"{PID}:{B1}:T1")
        assert r.outcome == "stale_refused" and "membership" in r.msg, r.msg
        assert "conversations_archive" not in fake.method_names()


class TestFreshAge:
    def test_the_notice_and_intent_carry_the_age_at_tap_time_not_the_cards(self, fake, armed):
        row = _row(A1, "fx-dead-one", tier="T1")
        row["last_person_days"] = 150               # what the card said
        stage(tier="T1", rows=[row])
        r = tap(cards.ACTION_ROW, f"{PID}:{A1}:T1")
        assert r.outcome == "archived" and "200 days" in r.msg
        notice = next(p for p in fake.posts if p["channel"] == A1)
        assert "200 days since the last message" in notice["text"]
        assert st.read_ledger()[0]["age_days"] == 200


class TestArchiveAll:
    def test_an_unexpected_crash_mid_loop_says_how_far_it_got(self, fake, armed, monkeypatch):
        rows = [_row(A1, "fx-dead-one", tier="T1"), _row(A2, "fx-dead-two", tier="T1"),
                _row(A3, "fx-dead-three", tier="T1")]
        stage(tier="T1", rows=rows)
        real = handler._archive_one
        calls = {"n": 0}

        def _flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom")
            return real(*a, **k)
        monkeypatch.setattr(handler, "_archive_one", _flaky)
        r = tap(cards.ACTION_ALL, f"{PID}:p1:T1")
        assert r.msg.startswith("Stopped after 1 of 3 (RuntimeError)") and "archived 1" in r.msg
        assert fake.method_names().count("conversations_archive") == 1

    def test_a_systemic_error_aborts_the_loop_with_no_further_notices(self, fake, armed):
        rows = [_row(A1, "fx-dead-one", tier="T1"), _row(A2, "fx-dead-two", tier="T1"),
                _row(A3, "fx-dead-three", tier="T1")]
        stage(tier="T1", rows=rows)
        fake.archive_behaviour = api_error("restricted_action")
        r = tap(cards.ACTION_ALL, f"{PID}:p1:T1")
        assert "2 of 3 not attempted" in r.msg and "restricted_action" in r.msg
        notices = [p for p in fake.posts if p["channel"] in (A2, A3)]
        assert notices == [] and fake.method_names().count("conversations_archive") == 1
        assert (state(A2), state(A3)) == (st.OPEN, st.OPEN)

    def test_archive_all_re_renders_every_five_rows(self, monkeypatch, armed):
        cids = [f"C0DEADB{i:03d}" for i in range(12)]
        f = FakeSlack(channels=[chan(c, f"fx-d{i}") for i, c in enumerate(cids)],
                      history={c: [msg(200)] for c in cids},
                      scopes=["channels:manage", "groups:write"])
        monkeypatch.setattr(clients, "read_client_factory", lambda: f)
        monkeypatch.setattr(clients, "write_client_factory", lambda: f)
        stage(tier="T1", rows=[_row(c, f"fx-d{i}", tier="T1") for i, c in enumerate(cids)])
        ticks: list = []
        r = tap(cards.ACTION_ALL, f"{PID}:p1:T1", progress=lambda pid, page: ticks.append(page))
        assert r.counts == {"archived": 12} and ticks == [1, 1]
        assert f.method_names().count("conversations_archive") == 12


@pytest.mark.parametrize("shape", ["chanarch-" + " " * 40000 + "x", " " * 40000,
                                   "chanarch-aaaaaaaaaaaa:" + "C" * 40000,
                                   "chanarch-aaaaaaaaaaaa:p" + "9" * 40000],
                         ids=["pid-spaces", "spaces", "cid-run", "page-run"])
def test_value_parsing_is_linear_on_degenerate_input(shape):
    import time as _t
    best = float("inf")
    for _ in range(3):
        t0 = _t.perf_counter()
        for action in cards.ACTIONS:
            handler.parse_value(action, shape)
        best = min(best, _t.perf_counter() - t0)
    assert best < 0.05


def test_conversations_archive_has_exactly_one_call_site_in_src():
    """Kickoff section 3: conversations.archive is called ONLY from the tap handler,
    never from the scan. Walks the AST of every src/ module (comments cannot satisfy it)."""
    sites = []
    for path in (REPO / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        parents: dict = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "conversations_archive":
                fn = node
                while fn is not None and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = parents.get(fn)
                sites.append((path.relative_to(REPO).as_posix(), getattr(fn, "name", "<module>")))
    assert sites == [("src/cora/channel_archive/handler.py", "_archive_one")], sites


def test_the_scan_and_the_monitor_never_write_to_a_channel():
    for mod in ("scan", "classify", "monitor", "deliver"):
        tree = ast.parse((REPO / "src" / "cora" / "channel_archive" / f"{mod}.py").read_text(encoding="utf-8"))
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "conversations_archive" not in attrs, mod
        if mod != "deliver":            # deliver posts ONLY the card, to Harrison's DM
            assert "chat_postMessage" not in attrs, mod


# ── A27: the D-051 round-1 fix replies pass both honesty rails (rendered, realistic) ──
def _assert_rails(strings, monkeypatch, caplog):
    import logging
    from cora import slack_egress as se
    caplog.set_level(logging.WARNING, logger=se.__name__)
    assert strings
    for s in strings:
        assert se.screen_phantom_write_claims(s, tool_use_count=0) == s, s
        assert se.sanitize_text(s) == s, s
    assert not [r for r in caplog.records if se.PHANTOM_LOG_KEY in r.getMessage()]
    monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "enforce")
    for s in strings:
        assert se.screen_phantom_write_claims(s, tool_use_count=0) == s, s


class TestRoundOneRepliesPassTheRails:
    def test_demotion_and_mark_button_replies(self, fake, armed, monkeypatch, caplog):
        rows = [_row(A1, "fx-dead-one", tier="T1"), _row(A2, "fx-dead-two", tier="T1"),
                _row(A3, "fx-dead-three", tier="T1")]
        stage(tier="T1", rows=rows)
        out = [tap(cards.ACTION_ROW, f"{PID}:{A1}:T0").msg,                # a Mark button
               tap(cards.ACTION_ALL, f"{PID}:p1:T0").msg]                  # Mark all
        stage(tier="T1", pid="chanarch-000000000011", ts=NOW + 1)
        _demote(since_ts=NOW + 2)
        out += [tap(cards.ACTION_ROW, f"chanarch-000000000011:{A1}:T1", now=NOW + 3).msg,
                tap(cards.ACTION_ALL, "chanarch-000000000011:p1:T1", now=NOW + 3).msg]
        assert not fake.posts
        _assert_rails(out, monkeypatch, caplog)

    def test_secondary_flag_stale_replies(self, fake, armed, monkeypatch, caplog):
        out = []
        for older, capped in ((100, True), (2300, False)):
            st.store_path().unlink(missing_ok=True)
            fake.channels[:] = [c for c in fake.channels if c["id"] != BUSY]
            fake.channels.append(chan(BUSY, "fx-busy-room"))
            fake.history[BUSY] = _busy_history(older)
            TestReverifySecondaryFlags()._stage_busy(capped=capped)
            r = tap(cards.ACTION_OVERRIDE, f"{PID}:{BUSY}:T1")
            assert r.outcome == "stale_refused"
            out.append(r.msg)
            blocks, text = cards.render_page(st.fold(now=NOW + 20), PID, 1, now=NOW + 20)
            out += [e["text"] for b in blocks if b.get("type") == "section"
                    for e in [b["text"]]] + [text]
        _assert_rails(out, monkeypatch, caplog)
