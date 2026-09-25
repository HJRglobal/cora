"""Code #16 C1 -- the ONE classifier and the scan (kickoff section 3 C1 tests).

The failure that matters is a false "inactive" on an active channel, so every
ambiguity resolves to ACTIVE or UNKNOWN, never to a candidate: the 90-day boundary,
people posting through apps, users.info failures, thread replies under old parents,
pagination shapes, read errors. The kickoff's D-051 probes -- bot-only recent traffic,
a pinned channel, a -finance channel silent for 200 days -- each come out exempt or
informational (section B), never an Archive-all row.
"""
from __future__ import annotations

import logging

import pytest

from _chanarch_fakes import (APPUSER, BOT_UID, BOTUSER, DAY, HARRISON, LEXSTAFF, NOW, PERSON,
                             FakeSlack, api_error, chan, context, msg, no_sleep, resp)
from cora.channel_archive import classify as cl
from cora.channel_archive import scan as sc

CID = "C0TESTCH01"


def run(history, *, meta=None, ctx=None, **fake_kw):
    meta = meta or chan(CID, "fx-quiet-room")
    fake = FakeSlack(channels=[meta], history={meta["id"]: history}, **fake_kw)
    v = cl.classify_channel(fake, meta, ctx or context(), now=NOW, sleep=no_sleep)
    return v, fake


class TestThresholdBoundary:
    @pytest.mark.parametrize("days,kind", [(89.0, cl.ACTIVE), (89.99, cl.ACTIVE),
                                           (90.0, cl.SECTION_A), (91.0, cl.SECTION_A)])
    def test_person_message_age_t_minus_1_t_t_plus_1(self, days, kind):
        v, _ = run([msg(days), msg(400)])
        assert v.kind == kind, (days, v)

    def test_the_last_person_age_is_on_an_inactive_row(self):
        v, _ = run([msg(143), msg(400)])
        assert v.kind == cl.SECTION_A and v.last_person_days == 143 and v.history_complete

    def test_no_person_ever_is_inactive_and_says_so(self):
        v, _ = run([msg(200, user=None, subtype="channel_join")])
        assert v.kind == cl.SECTION_A and v.last_person_days is None and v.history_complete


class TestWhoIsAPerson:
    @pytest.mark.parametrize("bot_msg", [
        {"user": BOT_UID},                                   # Cora herself
        {"user": None, "bot_id": "BWEBHOOK", "subtype": "bot_message"},   # webhook
        {"user": BOTUSER, "bot_id": "BOTHER"},               # users.info is_bot
        {"user": APPUSER},                                   # users.info is_app_user
        {"user": "USLACKBOT"},
        {"user": None, "bot_id": "BINTEG"},
    ])
    def test_bot_only_recent_traffic_is_informational_section_b(self, bot_msg):
        """THE kickoff D-051 probe: bot-only recent traffic is never an Archive-all row."""
        m = msg(10, **{k: v for k, v in bot_msg.items() if k != "user"},
                user=bot_msg.get("user"))
        v, _ = run([m, msg(300)])
        assert v.kind == cl.SECTION_B and v.reason == cl.B_BOT_TRAFFIC
        assert v.bot_posts == 1 and v.bot_latest_days == 10

    def test_a_person_posting_through_an_app_is_a_person(self):
        """Live census 2026-09-25: 106 `user+bot_id` messages were by people."""
        v, _ = run([msg(5, user=PERSON, bot_id="BAPP", app_id="AAPP"), msg(300)])
        assert v.kind == cl.ACTIVE

    def test_a_users_info_failure_counts_as_a_person(self):
        v, _ = run([msg(5, user="UUNKNOWN9"), msg(300)])
        assert v.kind == cl.ACTIVE

    def test_system_events_are_not_activity(self):
        hist = [msg(3, subtype="channel_join"), msg(4, subtype="channel_topic"),
                msg(5, subtype="bot_add"), msg(6, subtype="tabbed_canvas_updated"),
                msg(300)]
        v, _ = run(hist)
        assert v.kind == cl.SECTION_A and v.bot_posts == 0

    @pytest.mark.parametrize("subtype", ["thread_broadcast", "file_share", "me_message",
                                         "sh_room_created", "huddle_thread"])
    def test_person_subtypes_count(self, subtype):
        v, _ = run([msg(3, subtype=subtype), msg(300)])
        assert v.kind == cl.ACTIVE

    def test_an_unarchive_by_a_person_is_activity_but_not_by_cora(self):
        v, _ = run([msg(20, subtype="channel_unarchive"), msg(300)])
        assert v.kind == cl.ACTIVE
        v2, _ = run([msg(20, user=BOT_UID, subtype="channel_unarchive"), msg(300)])
        assert v2.kind == cl.SECTION_A


class TestThreadGuard:
    def test_an_old_parent_with_a_recent_reply_is_active(self):
        parent = msg(150, reply_count=4, latest_reply=f"{NOW - 1 * DAY:.6f}")
        v, _ = run([msg(120), parent])
        assert v.kind == cl.ACTIVE

    def test_a_parent_with_replies_but_no_latest_reply_fails_closed(self):
        v, _ = run([msg(120), msg(150, reply_count=2)])
        assert v.kind == cl.ACTIVE

    def test_an_unparseable_latest_reply_fails_closed(self):
        v, _ = run([msg(200, reply_count=2, latest_reply="soon")])
        assert v.kind == cl.ACTIVE

    def test_old_replies_do_not_revive(self):
        v, _ = run([msg(150, reply_count=2, latest_reply=f"{NOW - 140 * DAY:.6f}")])
        assert v.kind == cl.SECTION_A

    def test_phase_two_reads_past_the_first_person_message(self):
        """A4: the 'last person' field must not stop the thread guard."""
        hist = [msg(100), msg(101), msg(160, reply_count=1, latest_reply=f"{NOW - 2 * DAY:.6f}")]
        v, _ = run(hist)
        assert v.kind == cl.ACTIVE

    def test_history_longer_than_the_phase_two_cap_is_threads_unchecked(self):
        hist = [msg(100 + i * 0.01, user=None, subtype="channel_join") for i in range(12)]
        v, _ = run(hist, page_size=1)
        assert v.kind == cl.SECTION_B and v.reason == cl.B_THREADS_UNCHECKED


class TestPaginationFailsSafe:
    def _override(self, pages):
        it = iter(pages)

        def _h(channel, oldest, latest, cursor):
            return next(it)
        return _h

    def test_missing_has_more_is_unknown(self):
        v, _ = run([], history_override=self._override([resp({"ok": True, "messages": []})]))
        assert v.kind == cl.UNKNOWN

    def test_has_more_without_a_cursor_is_unknown(self):
        page = resp({"ok": True, "messages": [], "has_more": True,
                     "response_metadata": {"next_cursor": ""}})
        v, _ = run([], history_override=self._override([page]))
        assert v.kind == cl.UNKNOWN and v.reason == "pagination"

    def test_a_non_bool_has_more_is_unknown(self):
        page = resp({"ok": True, "messages": [], "has_more": "false",
                     "response_metadata": {"next_cursor": ""}})
        v, _ = run([], history_override=self._override([page]))
        assert v.kind == cl.UNKNOWN

    def test_an_empty_page_with_more_follows_the_cursor(self):
        """The 6/17 empty-page incident: an empty page is not 'dead'."""
        pages = [resp({"ok": True, "messages": [], "has_more": True,
                       "response_metadata": {"next_cursor": "c2"}}),
                 resp({"ok": True, "messages": [msg(3)], "has_more": False,
                       "response_metadata": {"next_cursor": ""}})]
        v, _ = run([], history_override=self._override(pages))
        assert v.kind == cl.ACTIVE

    def test_a_read_error_is_unknown_never_a_candidate(self):
        v, _ = run(api_error("not_in_channel"))
        assert v.kind == cl.UNKNOWN and v.reason == "not_in_channel"

    def test_a_connection_error_is_unknown(self):
        v, _ = run(TimeoutError("read timed out"))
        assert v.kind == cl.UNKNOWN

    def test_the_phase_one_page_cap_is_unknown(self):
        hist = [msg(1 + i * 0.001, user=BOT_UID) for i in range(12)]
        v, _ = run(hist, page_size=1)
        assert v.kind == cl.UNKNOWN and v.reason == "page_cap"

    def test_latest_carries_clock_skew_headroom(self):
        _, fake = run([msg(300)])
        first = [c for c in fake.calls if c[0] == "conversations_history"][0][1]
        assert float(first["latest"]) == pytest.approx(NOW + cl.LATEST_SKEW_S)
        assert float(first["oldest"]) == pytest.approx(NOW - 90 * DAY)


class TestExemptionClasses:
    def _meta(self, **kw):
        return chan(CID, kw.pop("name", "fx-quiet-room"), **kw)

    @pytest.mark.parametrize("kw,cls", [
        ({"name": "personal-tasks"}, cl.X_DENIED),
        ({"is_general": True}, cl.X_GENERAL),
        ({"is_ext_shared": True}, cl.X_SHARED), ({"is_shared": True}, cl.X_SHARED),
        ({"is_org_shared": True}, cl.X_SHARED), ({"is_pending_ext_shared": True}, cl.X_SHARED),
        ({"name": "ops-leadership"}, cl.X_LEAD_FIN),
        ({"created_days": 29}, cl.X_YOUNG), ({"created": None}, cl.X_YOUNG),
        ({"is_archived": True}, cl.X_ARCHIVED),
    ])
    def test_each_non_overridable_class_is_exempt_without_a_history_read(self, kw, cls):
        v, fake = run([msg(300)], meta=self._meta(**kw))
        assert v.kind == cl.EXEMPT and v.reason == cls
        assert "conversations_history" not in fake.method_names()

    def test_the_blocked_general_id_and_info_for_cora_are_exempt(self):
        from cora.channel_archive import registry as reg
        # the live deny-list ALSO denies C0B2NMLK7CK (denied wins the count); with a
        # policy that lacks it, the general belt still holds on its own
        thin = reg.DenyPolicy(ids=frozenset({f"C0DENY{i:04d}" for i in range(12)}),
                              names=frozenset({"x"}))
        for cid, cls in (("C0B2NMLK7CK", cl.X_GENERAL), ("C0B5BNP6YKY", cl.X_INFO)):
            v, fake = run([msg(300)], meta=chan(cid, "fx-any"), ctx=context(deny=thin))
            assert v.kind == cl.EXEMPT and v.reason == cls
            assert "conversations_history" not in fake.method_names()
        v, _ = run([msg(300)], meta=chan("C0B2NMLK7CK", "fx-any"))
        assert v.kind == cl.EXEMPT and v.reason == cl.X_DENIED

    def test_a_denied_id_is_never_read(self):
        meta = chan("C0B7PMLQ26B", "renamed-family-channel")
        v, fake = run([msg(300)], meta=meta)
        assert v.reason == cl.X_DENIED and fake.calls == []

    def test_a_finance_channel_silent_200_days_is_exempt(self):
        """Kickoff D-051 probe #3."""
        v, _ = run([msg(200)], meta=chan(CID, "hjrp-finance"))
        assert v.kind == cl.EXEMPT and v.reason == cl.X_LEAD_FIN
        v2, _ = run([msg(200)], meta=chan(CID, "brand-new-finance"))
        assert v2.kind == cl.EXEMPT

    def test_a_pinned_channel_is_exempt(self):
        """Kickoff D-051 probe #2."""
        v, _ = run([msg(300)], pins={CID: 1})
        assert v.kind == cl.EXEMPT and v.reason == cl.X_PINNED

    def test_pins_unreadable_is_unknown_not_a_candidate(self):
        v, _ = run([msg(300)], pins={CID: api_error("missing_scope")})
        assert v.kind == cl.UNKNOWN and v.reason == "pins_unknown"

    def test_a_keep_window_suppresses(self):
        ctx = context(keep_state={CID: {"count": 1, "until": NOW + 5 * DAY}})
        v, fake = run([msg(300)], ctx=ctx)
        assert v.kind == cl.EXEMPT and v.reason == cl.X_KEPT and fake.calls == []
        ctx2 = context(keep_state={CID: {"count": 1, "until": NOW - 5 * DAY}})
        v2, _ = run([msg(300)], ctx=ctx2)
        assert v2.kind == cl.SECTION_A

    def test_an_unarchive_suppresses_for_90_days_then_shows_in_section_b(self):
        ctx = context(unarchive_state={CID: {"at": NOW - 10 * DAY, "by": PERSON}})
        v, _ = run([msg(300)], ctx=ctx)
        assert v.kind == cl.EXEMPT and v.reason == cl.X_UNARCHIVE_SUPPRESSED
        ctx2 = context(unarchive_state={CID: {"at": NOW - 95 * DAY, "by": PERSON}})
        v2, _ = run([msg(300)], ctx=ctx2)
        assert v2.kind == cl.SECTION_B and v2.reason == cl.B_UNARCHIVED_BEFORE
        assert v2.unarchived_by == PERSON


class TestOverridableAndLookCloser:
    def test_registry_channel_is_section_b_registry(self):
        v, _ = run([msg(300)], meta=chan("C0FXREGQ0001", "fx-registry-quiet"))
        assert v.kind == cl.SECTION_B and v.reason == cl.B_REGISTRY

    def test_a_registry_channel_that_is_active_is_just_active(self):
        v, _ = run([msg(3)], meta=chan("C0FXREGQ0001", "fx-registry-quiet"))
        assert v.kind == cl.ACTIVE

    def test_a_pinned_registry_channel_is_non_overridable(self):
        v, _ = run([msg(300)], meta=chan("C0FXREGQ0001", "fx-registry-quiet"),
                   pins={"C0FXREGQ0001": 2})
        assert v.kind == cl.EXEMPT and v.reason == cl.X_PINNED

    @pytest.mark.parametrize("cid,name", [("C0TESTLX01", "open-tucson-dta-location"),
                                          ("C0FXLXPLAIN1", "fx-community-help"),
                                          ("C0TESTLX02", "lex-old-project")])
    def test_lex_channels_are_section_b_lex(self, cid, name):
        v, _ = run([msg(300)], meta=chan(cid, name))
        assert v.kind == cl.SECTION_B and v.reason == cl.B_LEX and v.lex

    def test_a_lex_staff_member_makes_it_lex(self):
        v, _ = run([msg(300)], members={CID: [HARRISON, LEXSTAFF]})
        assert v.reason == cl.B_LEX

    def test_members_unreadable_makes_it_lex(self):
        v, _ = run([msg(300)], members={CID: api_error("missing_scope")})
        assert v.reason == cl.B_LEX

    def test_a_fail_safe_substitution_is_reported_on_the_verdict(self):
        """c1-authority-tier#1: the scan keeps its fail-safe classes, but the verdict SAYS
        a read fell back, so the T1 re-verify can refuse retryably instead of 'stale'."""
        v, _ = run([msg(300)], members={CID: api_error("ratelimited")})
        assert v.reason == cl.B_LEX and "members" in v.extra.get("unreadable", [])
        v2, _ = run([msg(5, user="UUNKNOWN9"), msg(300)])
        assert v2.kind == cl.ACTIVE and "users_info" in v2.extra.get("unreadable", [])
        v3, _ = run([msg(5), msg(300)])                     # a real, readable person
        assert v3.kind == cl.ACTIVE and not v3.extra.get("unreadable")
        v4, _ = run([msg(300)])
        assert v4.kind == cl.SECTION_A and not v4.extra.get("unreadable")

    def test_keep_list_channel_is_section_b(self):
        v, _ = run([msg(300)], meta=chan(CID, "cowork-daily-briefs"))
        assert v.reason == cl.B_KEEP_LIST

    def test_a_private_channel_harrison_is_not_in_is_section_b(self):
        v, _ = run([msg(300)], meta=chan(CID, "fx-secret", private=True),
                   members={CID: [PERSON]})
        assert v.kind == cl.SECTION_B and v.reason == cl.B_PRIVATE_NOT_MEMBER
        v2, _ = run([msg(300)], meta=chan(CID, "fx-secret", private=True),
                    members={CID: [PERSON, HARRISON]})
        assert v2.kind == cl.SECTION_A

    def test_a_clean_candidate_carries_its_card_fields(self):
        meta = chan(CID, "fx-quiet-room", num_members=7,
                    properties={"canvas": {"file_id": "F1", "is_empty": False}, "tabs": [{}, {}]})
        v, _ = run([msg(143), msg(400)], meta=meta, bookmarks={CID: 1})
        assert v.kind == cl.SECTION_A
        assert (v.member_count, v.pins, v.bookmarks, v.canvas, v.tabs) == (7, 0, 1, True, 2)
        assert v.harrison_member is True and v.created_days == 400


class TestMetadataOnly:
    def test_the_projection_drops_message_bodies(self):
        m = msg(3, text="the body", blocks=[{"x": 1}], files=[{"id": "F"}])
        p = cl._project(m)
        assert set(p) == {"ts", "user", "bot_id", "app_id", "subtype", "reply_count", "latest_reply"}

    def test_the_scan_logs_ids_and_counts_never_names(self, caplog):
        caplog.set_level(logging.DEBUG)
        meta = chan(CID, "fx-very-distinctive-name")
        fake = FakeSlack(channels=[meta], history={CID: [msg(300)]})
        sc.scan(fake, context(), now=NOW, sleep=no_sleep)
        assert not [r for r in caplog.records if "fx-very-distinctive-name" in r.getMessage()]
        assert not [r for r in caplog.records if "SECRET" in r.getMessage()]

    def test_project_channel_drops_purpose_and_topic(self):
        p = sc.project_channel(chan(CID, "fx-x"))
        assert "purpose" not in p and "topic" not in p

    def test_every_history_call_is_paced_two_seconds(self):
        naps: list[float] = []
        meta = chan(CID, "fx-quiet-room")
        fake = FakeSlack(channels=[meta], history={CID: [msg(300)]})
        cl.classify_channel(fake, meta, context(), now=NOW, sleep=naps.append)
        n_hist = fake.method_names().count("conversations_history")
        assert cl.PACE_S >= 2.0 and naps.count(cl.PACE_S) >= n_hist >= 2


class TestScan:
    def _world(self):
        chans = [
            chan("C0CANDID01", "fx-dead-one"),
            chan("C0ACTIVE01", "fx-alive"),
            chan("C0BOTONL01", "fx-bot-feed"),
            chan("C0FXREGQ0001", "fx-registry-quiet"),
            chan("C0TESTLX09", "lex-archive-me"),
            chan("C0FINANCE1", "fx-finance"),
            chan("C0PINNED01", "fx-pinned"),
            chan("C0UNREAD01", "fx-unreadable"),
            chan("D0DMDMDM01", "dm-row", is_im=True),
            chan("G0MPIMMP01", "mpdm-row", is_mpim=True),
            chan("C0NOTMEMB1", "fx-not-member", member=False),
        ]
        hist = {"C0CANDID01": [msg(200)], "C0ACTIVE01": [msg(2)],
                "C0BOTONL01": [msg(1, user=BOT_UID), msg(250)],
                "C0FXREGQ0001": [msg(300)], "C0TESTLX09": [msg(300)],
                "C0FINANCE1": [msg(200)], "C0PINNED01": [msg(300)],
                "C0UNREAD01": api_error("not_in_channel")}
        return FakeSlack(channels=chans, history=hist, pins={"C0PINNED01": 1})

    def test_the_scan_sorts_counts_and_never_touches_dms(self):
        fake = self._world()
        res = sc.scan(fake, context(), now=NOW, sleep=no_sleep)
        assert res["blind"] is None and res["scanned"] == 8     # im, mpim, non-member dropped
        assert res["candidate_ids"] == ["C0CANDID01"]
        by = {r["cid"]: r for r in res["rows"]}
        assert by["C0BOTONL01"]["reason"] == cl.B_BOT_TRAFFIC
        assert by["C0FXREGQ0001"]["reason"] == cl.B_REGISTRY
        assert by["C0TESTLX09"]["reason"] == cl.B_LEX and "name" not in by["C0TESTLX09"]
        assert res["counts"]["active"] == 1
        assert res["counts"]["exempt"] == {cl.X_LEAD_FIN: 1, cl.X_PINNED: 1}
        assert res["counts"]["unreadable"] == 1
        assert res["counts"]["unreadable_codes"] == {"not_in_channel": 1}
        assert res["rows"][0]["cid"] == "C0CANDID01"                # A first
        assert not {"D0DMDMDM01", "G0MPIMMP01", "C0NOTMEMB1"} & fake.history_channels()
        assert "types" in fake.calls[0][1] and fake.calls[0][1]["types"] == "public_channel,private_channel"

    def test_a_lex_row_persists_no_name(self):
        fake = self._world()
        res = sc.scan(fake, context(), now=NOW, sleep=no_sleep)
        lex = next(r for r in res["rows"] if r["cid"] == "C0TESTLX09")
        assert "lex-archive-me" not in repr(lex) and lex["lex"] and lex["name_fp"]

    @pytest.mark.parametrize("ctx_kw,cause", [
        ({"registry": None, "_bad_registry": True}, "registry_unreadable"),
        ({"deny": None}, "policy_unreadable"),
        ({"bot_uid": ""}, "bot_id_unknown"),
        ({"store_ok": False}, "store_unreadable"),
    ])
    def test_blind_causes_propose_nothing(self, ctx_kw, cause):
        from cora.channel_archive import registry as reg
        if ctx_kw.pop("_bad_registry", False):
            ctx_kw["registry"] = reg.Registry(ok=False, reason="registry_unreadable: x")
        fake = self._world()
        res = sc.scan(fake, context(**ctx_kw), now=NOW, sleep=no_sleep)
        assert res["blind"] == cause and res["rows"] == [] and res["scanned"] == 0
        assert "conversations_history" not in fake.method_names()

    def test_an_incomplete_channel_list_is_blind(self):
        fake = FakeSlack(list_error=api_error("ratelimited"))
        res = sc.scan(fake, context(), now=NOW, sleep=no_sleep)
        assert res["blind"] == "list_incomplete" and res["blind_detail"] == "ratelimited"

    def test_a_list_page_without_the_cursor_field_is_blind(self):
        class NoCursor(FakeSlack):
            def conversations_list(self, **kw):
                return resp({"ok": True, "channels": [chan("C0CANDID01", "fx-dead")]})
        res = sc.scan(NoCursor(), context(), now=NOW, sleep=no_sleep)
        assert res["blind"] == "list_incomplete"
