"""Code #16 C1 (A25) -- the evidence monitor must FAIL on the regression it guards: an
archive by Cora that the ledger cannot attribute to a tap demotes the lane (never from
a blind read, never under --dry-run); a ledger archive Slack does not show WARNs; an
unresolved intent WARNs until Slack decides; a blind read never reads clean.
"""
from __future__ import annotations

import pytest

from _chanarch_fakes import (BOT_ID, BOT_UID, DAY, HARRISON, NOW as _NOW0, PERSON, FakeSlack,
                             api_error, chan, resp)
from cora.channel_archive import monitor as mon
from cora.channel_archive import policy
from cora.channel_archive import store as st

ARCH = "C0ARCHIVE01"
#: 20 days after the lane's epoch, so "a day ago" is inside the lane's lifetime.
NOW = _NOW0 + 20 * DAY


def test_the_lane_epoch_is_the_merge_day():
    assert mon.LANE_EPOCH < _NOW0 < mon.LANE_EPOCH + DAY


class MonSlack(FakeSlack):
    """conversations.list includes archived rows; history(limit=20) returns the newest
    archive-type events the test sets; history(oldest=...) serves the unarchive search."""

    def __init__(self, archived: dict | None = None, events: dict | None = None,
                 unarchives: dict | None = None, **kw):
        chans = kw.pop("channels", None)
        if chans is None:
            chans = [chan("C0PERSONA1", "fx-person-archived", is_archived=True,
                          updated=int((NOW - 3 * DAY) * 1000))]
        for cid, spec in (archived or {}).items():
            chans.append(chan(cid, f"fx-{cid.lower()}", is_archived=spec.get("is_archived", True),
                              **({"updated": spec["updated"]} if "updated" in spec else {})))
        super().__init__(channels=chans, **kw)
        self.events = {"C0PERSONA1": [{"ts": f"{NOW - 3 * DAY:.6f}", "subtype": "channel_archive",
                                       "user": PERSON}]}
        self.events.update(events or {})
        self.unarchives = unarchives or {}

    def conversations_history(self, channel, oldest=None, latest=None, limit=100, cursor=None,
                              inclusive=False, **kw):
        self.calls.append(("conversations_history", {"channel": channel, "oldest": oldest, "limit": limit}))
        if oldest is not None:
            return resp({"ok": True, "messages": self.unarchives.get(channel, []), "has_more": False,
                         "response_metadata": {"next_cursor": ""}})
        ev = self.events.get(channel, [])
        if isinstance(ev, Exception):
            raise ev
        return resp({"ok": True, "messages": ev, "has_more": False})


def arch_event(ts, **who):
    return {"ts": f"{ts:.6f}", "subtype": "channel_archive", **who}


def intent(cid, ts, pid="chanarch-aaaaaaaaaaaa", by=HARRISON):
    st.append_ledger("intent", proposal_id=pid, channel_id=cid, tapped_by=by, ts=ts)


def outcome(cid, ts, what, pid="chanarch-aaaaaaaaaaaa"):
    st.append_ledger("outcome", proposal_id=pid, channel_id=cid, outcome=what, ts=ts)


def run(fake, **kw):
    return mon.reconcile(fake, now=NOW, sleep=lambda s: None, **kw)


class TestClean:
    def test_a_person_archive_and_an_empty_ledger_read_ok_with_coverage(self):
        out = run(MonSlack())
        assert out["status"] == "ok" and out["findings"] == []
        cov = out["coverage"]
        assert cov["list_rows"] == 1 and cov["archived_in_list"] == 1 and cov["histories"] == 1
        assert "1 archived, 1 examined, 1 histories read" in mon.coverage_line(cov)


class TestBlind:
    @pytest.mark.parametrize("fake_kw,why", [
        ({"list_error": api_error("ratelimited")}, "channel list incomplete"),
        ({"bot_uid": ""}, "bot identity unknown"),
        ({"channels": [chan("C0OPEN0001", "fx-open")]}, "zero archived channels"),
    ])
    def test_a_blind_read_is_never_ok_and_never_demotes(self, fake_kw, why):
        fake = MonSlack(**fake_kw, events={ARCH: [arch_event(NOW - DAY, user=BOT_UID)]})
        out = run(fake)
        assert out["status"] == "warn" and why in " ".join(out["findings"])
        assert not policy.is_demoted()

    def test_an_unreadable_ledger_is_blind(self, monkeypatch, tmp_path):
        d = tmp_path / "d"
        d.mkdir()
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_LEDGER_PATH", str(d))
        out = run(MonSlack(archived={ARCH: {}}, events={ARCH: [arch_event(NOW - DAY, user=BOT_UID)]}))
        assert out["status"] == "warn" and out["blind"] and not policy.is_demoted()


class TestUnattributed:
    def _cora_archive(self, ts=NOW - DAY, **who):
        who = who or {"user": BOT_UID}
        return MonSlack(archived={ARCH: {}}, events={ARCH: [arch_event(ts, **who)]})

    def test_an_archive_by_cora_with_no_intent_demotes(self):
        out = run(self._cora_archive())
        assert out["status"] == "warn" and out["demotion_written"]
        assert any("UNATTRIBUTED" in f and ARCH in f for f in out["findings"])
        dem = policy.demotion_state()
        assert dem["channel_id"] == ARCH and dem["archive_ts"] == f"{NOW - DAY:.6f}"

    def test_dry_run_demotes_nothing(self):
        out = run(self._cora_archive(), dry_run=True)
        assert out["status"] == "warn" and not out["demotion_written"] and not policy.is_demoted()

    def test_the_bot_id_alone_identifies_cora(self):
        out = run(self._cora_archive(user=None, bot_id=BOT_ID))
        assert out["demotion_written"]

    @pytest.mark.parametrize("offset,by,attributed", [
        (-5 * 60, HARRISON, True), (-14 * 60, HARRISON, True), (+60, HARRISON, True),
        (-20 * 60, HARRISON, False), (+5 * 60, HARRISON, False), (-5 * 60, PERSON, False)])
    def test_the_attribution_window_and_tapper(self, offset, by, attributed):
        ats = NOW - DAY
        intent(ARCH, ats + offset, by=by)
        out = run(self._cora_archive(ts=ats))
        assert out["demotion_written"] is (not attributed), out["findings"]

    def test_an_old_failed_intent_cannot_cover_a_new_rogue_archive(self):
        ats = NOW - DAY
        intent(ARCH, ats - 60 * DAY)
        outcome(ARCH, ats - 60 * DAY + 5, "failed:restricted_action")
        assert run(self._cora_archive(ts=ats))["demotion_written"]

    def test_a_failed_outcome_between_intent_and_archive_does_not_attribute(self):
        ats = NOW - DAY
        intent(ARCH, ats - 120)
        outcome(ARCH, ats - 60, "notice_failed:not_in_channel")
        assert run(self._cora_archive(ts=ats))["demotion_written"]

    def test_archive_unarchive_rearchive_with_one_intent_demotes_on_the_second(self):
        first = NOW - 10 * DAY
        intent(ARCH, first - 60)
        out = run(self._cora_archive(ts=NOW - DAY))       # the latest archive message
        assert out["demotion_written"]

    def test_legacy_pre_lane_archives_never_demote(self):
        out = run(self._cora_archive(ts=mon.LANE_EPOCH - 3600))
        assert not out["demotion_written"] and out["status"] == "ok"

    def test_a_legacy_sprawl_id_archived_by_cora_after_the_epoch_still_demotes(self, monkeypatch, tmp_path):
        """D-051 r1 c1-monitor#4: the per-id legacy-sprawl clause exempted ~115 channels
        forever. LANE_EPOCH already excludes every pre-lane archive (the 6/24 + 7/12
        sprawl runs predate it), so a POST-epoch Cora archive of a sprawl id with no
        intent is exactly the regression the monitor guards -> it demotes."""
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "archive-sprawl-2026-06-24.jsonl").write_text(
            '{"id": "%s", "status": "archived", "run_ts": "2026-06-24T21:00:00-07:00"}\n' % ARCH,
            encoding="utf-8")
        monkeypatch.setattr(mon, "_REPO_ROOT", tmp_path, raising=False)
        out = run(self._cora_archive())
        assert out["demotion_written"] and any("UNATTRIBUTED" in f and ARCH in f for f in out["findings"])

    def test_the_monitor_reads_no_sprawl_log(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(mon))
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        consts = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                  and isinstance(n.value, str) and id(n) not in docs]
        assert not any("archive-sprawl" in c for c in consts)

    def test_an_acknowledged_event_never_re_demotes(self):
        st.append_ledger("acknowledged", channel_id=ARCH, archive_ts=f"{NOW - DAY:.6f}", by=HARRISON)
        out = run(self._cora_archive())
        assert not out["demotion_written"] and out["status"] == "ok"

    def test_clear_demotion_then_the_next_run_stays_clear(self):
        run(self._cora_archive())
        assert policy.is_demoted()
        assert st.clear_demotion(actor=HARRISON, dry_run=False)["cleared"]
        out = run(self._cora_archive())
        assert not policy.is_demoted() and out["status"] == "ok"

    def test_an_archive_message_with_no_archiver_warns(self):
        out = run(MonSlack(archived={ARCH: {}}, events={ARCH: [arch_event(NOW - DAY)]}))
        assert any("archiver unknown" in f for f in out["findings"]) and not out["demotion_written"]

    def test_a_row_with_no_updated_field_is_examined(self):
        fake = MonSlack(archived={ARCH: {}}, events={ARCH: [arch_event(NOW - DAY, user=BOT_UID)]})
        assert "updated" not in next(c for c in fake.channels if c["id"] == ARCH)
        assert run(fake)["demotion_written"]

    def test_an_archived_channel_with_no_archive_message_cannot_be_attributed(self):
        out = run(MonSlack(archived={ARCH: {}}, events={ARCH: []}))
        assert any("cannot attribute" in f for f in out["findings"])

    def test_the_demoted_state_warns_daily(self):
        policy.demotion_path().write_text('{"since": "2026-09-26", "channel_id": "C1"}', encoding="utf-8")
        out = run(MonSlack())
        assert out["status"] == "warn" and any("DEMOTED since 2026-09-26" in f for f in out["findings"])


ARCH2 = "C0ARCHIVE02"


def _two_rogue(second=True):
    archived = {ARCH: {}}
    events = {ARCH: [arch_event(NOW - DAY, user=BOT_UID)]}
    if second:
        archived[ARCH2] = {}
        events[ARCH2] = [arch_event(NOW - 2 * DAY, user=BOT_UID)]
    return MonSlack(archived=archived, events=events)


class TestDemotionEvents:
    """D-051 r1 c1-monitor#2: the demotion file lists EVERY unattributed archive event
    (appended even while already demoted), and --clear-demotion acknowledges each one,
    so a clear never re-arms T1 while the monitor knows of another unacked event."""

    def _pairs(self):
        dem = policy.demotion_state()
        return sorted((e["channel_id"], e["archive_ts"]) for e in dem["events"])

    def test_two_unattributed_archives_in_one_run_are_both_listed_and_both_acked(self):
        out = run(_two_rogue())
        assert out["demotion_written"]
        assert self._pairs() == sorted([(ARCH, f"{NOW - DAY:.6f}"), (ARCH2, f"{NOW - 2 * DAY:.6f}")])
        cleared = st.clear_demotion(actor=HARRISON, dry_run=False)
        assert cleared["cleared"] and len(cleared["events"]) == 2
        acks = sorted((r["channel_id"], r["archive_ts"]) for r in st.read_ledger() if r["event"] == "acknowledged")
        assert acks == sorted([(ARCH, f"{NOW - DAY:.6f}"), (ARCH2, f"{NOW - 2 * DAY:.6f}")])
        again = run(_two_rogue())
        assert not policy.is_demoted() and again["status"] == "ok", again["findings"]

    def test_an_event_found_while_already_demoted_is_appended(self):
        run(_two_rogue(second=False))
        assert self._pairs() == [(ARCH, f"{NOW - DAY:.6f}")]
        since = policy.demotion_state()["since"]
        out = run(_two_rogue())
        assert self._pairs() == sorted([(ARCH, f"{NOW - DAY:.6f}"), (ARCH2, f"{NOW - 2 * DAY:.6f}")])
        assert policy.demotion_state()["since"] == since          # the original demotion is kept
        assert any(ARCH2 in f and "added to the existing demotion" in f for f in out["findings"])
        st.clear_demotion(actor=HARRISON, dry_run=False)
        assert run(_two_rogue())["status"] == "ok"

    def test_a_legacy_single_record_file_still_clears_its_event(self):
        st.write_demotion({"since": "2026-10-01", "channel_id": ARCH, "archive_ts": f"{NOW - DAY:.6f}",
                           "reason": "old shape"}, dry_run=False)
        out = st.clear_demotion(actor=HARRISON, dry_run=False)
        assert out["cleared"] and [e["channel_id"] for e in out["events"]] == [ARCH]


class TestDemotionCopyIsHonest:
    """D-051 r1 c1-monitor#5: the UNATTRIBUTED line says what actually happened to the
    demotion -- written / would demote (dry run) / WRITE FAILED with the real tier."""

    def test_dry_run_says_would_demote_not_demoted(self):
        out = run(_two_rogue(second=False), dry_run=True)
        line = next(f for f in out["findings"] if "UNATTRIBUTED" in f)
        assert "would demote" in line and "dry run" in line and "DEMOTED" not in line
        assert not policy.is_demoted() and out["demotion"] == "dry_run"

    def test_a_failed_demotion_write_is_loud_and_names_the_real_tier(self, monkeypatch, tmp_path):
        blocker = tmp_path / "afile"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_DEMOTION_PATH", str(blocker / "demotion.json"))
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE", "act")
        out = run(_two_rogue(second=False))
        assert not policy.is_demoted() and not out["demotion_written"] and out["demotion"] == "failed"
        line = next(f for f in out["findings"] if "UNATTRIBUTED" in f)
        assert "DEMOTION WRITE FAILED" in line and "still acting T1" in line and "is DEMOTED" not in line
        assert out["findings"][0].startswith("DEMOTION WRITE FAILED")

    def test_a_written_demotion_says_written(self):
        out = run(_two_rogue(second=False))
        line = next(f for f in out["findings"] if "UNATTRIBUTED" in f)
        assert "demotion WRITTEN" in line and out["demotion"] == "written"


class TestMismatch:
    def test_a_person_unarchive_is_recorded_and_leaves_the_set(self):
        outcome(ARCH, NOW - 5 * DAY, "archived")
        fake = MonSlack(archived={ARCH: {"is_archived": False}},
                        unarchives={ARCH: [{"ts": f"{NOW - 2 * DAY:.6f}", "subtype": "channel_unarchive",
                                            "user": PERSON}]})
        out = run(fake)
        assert out["status"] == "ok", out["findings"]
        seen = [r for r in st.read_ledger() if r["event"] == "unarchived_seen"]
        assert seen and seen[0]["by"] == PERSON
        fake2 = MonSlack(archived={ARCH: {"is_archived": False}})
        assert run(fake2)["status"] == "ok"            # resolved: no more history reads

    def test_a_bot_unarchive_warns(self):
        outcome(ARCH, NOW - 5 * DAY, "archived")
        fake = MonSlack(archived={ARCH: {"is_archived": False}},
                        unarchives={ARCH: [{"ts": f"{NOW - DAY:.6f}", "subtype": "channel_unarchive",
                                            "user": BOT_UID}]})
        out = run(fake)
        assert any("UNARCHIVED BY CORA" in f for f in out["findings"])

    def test_no_unarchive_found_warns_and_dry_run_appends_nothing(self):
        outcome(ARCH, NOW - 5 * DAY, "archived")
        out = run(MonSlack(archived={ARCH: {"is_archived": False}}), dry_run=True)
        assert any("MISMATCH" in f and "no unarchive" in f for f in out["findings"])
        assert [r["event"] for r in st.read_ledger()] == ["outcome"]


class TestUnresolved:
    def _stage(self):
        st.append_event("staged", proposal_id="chanarch-aaaaaaaaaaaa", ts=NOW - 3 * DAY,
                        expires_ts=NOW + 11 * DAY, rows=[{"cid": ARCH, "section": "A", "tier": "T1"}])
        st.append_event(st.CLAIMED, proposal_id="chanarch-aaaaaaaaaaaa", cid=ARCH, ts=NOW - 2 * DAY)

    def test_an_intent_slack_archived_is_reconciled_archived_in_ledger_and_store(self):
        self._stage()
        intent(ARCH, NOW - 2 * DAY)
        fake = MonSlack(archived={ARCH: {"updated": int((NOW - 2 * DAY) * 1000)}},
                        events={ARCH: [arch_event(NOW - 2 * DAY + 30, user=BOT_UID)]})
        out = run(fake)
        assert out["status"] == "ok", out["findings"]
        assert st.read_ledger()[-1]["outcome"] == "archived (reconciled)"
        assert st.fold(now=NOW).proposals["chanarch-aaaaaaaaaaaa"].state_of(ARCH) == st.ARCHIVED

    def test_an_intent_slack_never_archived_is_reconciled_failed_and_warns(self):
        self._stage()
        intent(ARCH, NOW - 2 * DAY)
        fake = MonSlack(archived={ARCH: {"is_archived": False}})
        out = run(fake)
        assert any("RECONCILED" in f for f in out["findings"])
        assert st.read_ledger()[-1]["outcome"] == "failed (reconciled)"
        assert st.fold(now=NOW).proposals["chanarch-aaaaaaaaaaaa"].state_of(ARCH) == st.FAILED

    def test_a_fresh_intent_is_left_alone_and_dry_run_appends_nothing(self):
        intent(ARCH, NOW - 60)
        out = run(MonSlack(archived={ARCH: {"is_archived": False}}), dry_run=True)
        assert out["status"] == "ok" and len(st.read_ledger()) == 1

    def test_an_unknown_outcome_warns_until_slack_decides(self):
        intent(ARCH, NOW - 600)
        outcome(ARCH, NOW - 590, "unknown")
        out = run(MonSlack(archived={ARCH: {"is_archived": False}}), dry_run=True)
        assert any("UNRESOLVED" in f for f in out["findings"])


def test_a_scan_that_started_and_never_staged_warns():
    st.append_event("scan_started", scan_id="abc123", trigger="ask", ts=NOW - 2 * 3600)
    out = run(MonSlack())
    assert any("never staged a card" in f for f in out["findings"])
    st.append_event("staged", proposal_id="chanarch-aaaaaaaaaaaa", scan_id="abc123", ts=NOW - 7000, rows=[])
    assert run(MonSlack())["status"] == "ok"


class TestScanStallSettles:
    """D-051 r1 c1-monitor#0: a crashed scan (already DM'd honestly) must not pin the
    lane's ONE health row at WARN forever -- lesson 52, an alarm the right action
    cannot clear gets ignored."""

    def test_a_crash_recorded_as_scan_failed_then_a_good_scan_reads_ok(self):
        st.append_event("scan_started", scan_id="crashed001", trigger="ask", ts=NOW - 3 * 3600)
        st.append_event("scan_failed", scan_id="crashed001", trigger="ask", error="RuntimeError",
                        ts=NOW - 3 * 3600 + 60)
        st.append_event("scan_started", scan_id="good000001", trigger="ask", ts=NOW - 2 * 3600)
        st.append_event("staged", proposal_id="chanarch-aaaaaaaaaaaa", scan_id="good000001",
                        ts=NOW - 2 * 3600 + 300, rows=[])
        out = run(MonSlack())
        assert out["status"] == "ok", out["findings"]

    def test_an_unsettled_stall_warns_for_seven_days_then_drops(self):
        st.append_event("scan_started", scan_id="killed0001", trigger="monthly", ts=NOW - 6 * DAY)
        out = run(MonSlack())
        assert any("never staged a card" in f for f in out["findings"])
        later = mon.reconcile(MonSlack(), now=NOW + 2 * DAY, sleep=lambda s: None)
        assert later["status"] == "ok", later["findings"]
