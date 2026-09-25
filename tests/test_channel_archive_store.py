"""Code #16 C1 -- the lane's state: the append-only proposals store, the fsync'd archive
ledger, per-channel claims (A13), keep windows (A15), supersede/expiry (A14), the
cross-process scan lock (A18) and the demotion write/clear helpers (A25).

Every test drives the REAL fold over tmp files (conftest redirects the paths).
"""
from __future__ import annotations

import json
import os
import threading

import pytest

from _chanarch_fakes import DAY, HARRISON, NOW
from cora.channel_archive import policy
from cora.channel_archive import store as st


def _row(cid, section="A", **kw):
    return {"cid": cid, "section": section, "reason": kw.pop("reason", ""), "tier": "T0",
            "name": f"fx-{cid.lower()}", "name_fp": "x", "is_private": False, **kw}


def stage(pid="chanarch-aaaaaaaaaaaa", rows=None, ts=NOW, delivered=True, **kw):
    rows = rows if rows is not None else [_row("C0AAAAAAA1"), _row("C0AAAAAAA2")]
    assert st.append_event("staged", proposal_id=pid, ts=ts, expires_ts=ts + 14 * DAY,
                           rows=rows, scanned=len(rows), counts={}, **kw)
    if delivered:
        assert st.append_event("delivered", proposal_id=pid, page=1, dm_channel="DH",
                               message_ts=f"{ts:.6f}", rendered_cids=[r["cid"] for r in rows],
                               buttons=True, ts=ts)
    return pid


class TestAppendAndFold:
    def test_round_trip_and_a_torn_line_is_skipped_not_fatal(self):
        pid = stage()
        with st.store_path().open("a", encoding="utf-8") as fh:
            fh.write("{torn\n")
        f = st.fold(now=NOW)
        assert f.ok and pid in f.proposals and f.proposals[pid].delivered

    def test_an_unreadable_store_folds_not_ok(self, monkeypatch, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_STORE_PATH", str(d))
        assert st.fold(now=NOW).ok is False

    def test_terminal_is_sticky(self):
        pid = stage()
        st.append_event(st.AGREED, proposal_id=pid, cid="C0AAAAAAA1", by=HARRISON, ts=NOW)
        st.append_event("released", proposal_id=pid, cid="C0AAAAAAA1", ts=NOW + 1)
        st.append_event(st.KEPT, proposal_id=pid, cid="C0AAAAAAA1", ts=NOW + 2)
        assert st.fold(now=NOW).proposals[pid].state_of("C0AAAAAAA1") == st.AGREED

    def test_failed_is_not_terminal(self):
        pid = stage()
        st.append_event(st.FAILED, proposal_id=pid, cid="C0AAAAAAA1", code="x", ts=NOW)
        ok, why, _ = st.decide_row(pid, "C0AAAAAAA1", st.AGREED, actor=HARRISON, now=NOW + 1)
        assert ok, why

    def test_a_reconcile_resolves_unknown(self):
        pid = stage()
        st.append_event(st.UNKNOWN, proposal_id=pid, cid="C0AAAAAAA1", ts=NOW)
        st.append_event("reconciled", proposal_id=pid, cid="C0AAAAAAA1", outcome="archived", ts=NOW + 5)
        assert st.fold(now=NOW + 6).proposals[pid].state_of("C0AAAAAAA1") == st.ARCHIVED
        pid2 = stage("chanarch-bbbbbbbbbbbb", ts=NOW + 10)
        st.append_event(st.UNKNOWN, proposal_id=pid2, cid="C0AAAAAAA2", ts=NOW + 11)
        st.append_event("reconciled", proposal_id=pid2, cid="C0AAAAAAA2", outcome="failed", ts=NOW + 12)
        assert st.fold(now=NOW + 13).proposals[pid2].state_of("C0AAAAAAA2") == st.FAILED

    def test_a_duplicate_staged_line_never_resets_a_card(self):
        pid = stage()
        st.append_event(st.KEPT, proposal_id=pid, cid="C0AAAAAAA1", ts=NOW + 1)
        st.append_event("staged", proposal_id=pid, ts=NOW + 2, rows=[_row("C0ZZZZZZZ9")])
        p = st.fold(now=NOW).proposals[pid]
        assert "C0ZZZZZZZ9" not in p.rows_by_cid and p.state_of("C0AAAAAAA1") == st.KEPT

    def test_row_events_for_unknown_rows_are_ignored(self):
        pid = stage()
        st.append_event(st.AGREED, proposal_id=pid, cid="C0NOTINCARD", ts=NOW)
        assert "C0NOTINCARD" not in st.fold(now=NOW).proposals[pid].row_state

    def test_last_registry_count_follows_the_latest_staged(self):
        stage(registry_count=140)
        stage("chanarch-bbbbbbbbbbbb", ts=NOW + 5, registry_count=128)
        assert st.fold(now=NOW).last_registry_count == 128


class TestLedger:
    def test_the_ledger_append_fsyncs(self, monkeypatch):
        seen: list[int] = []
        real = os.fsync
        monkeypatch.setattr(os, "fsync", lambda fd: (seen.append(fd), real(fd))[1])
        assert st.append_ledger("intent", proposal_id="p", channel_id="C1")
        assert seen, "the intent row must be fsynced before any Slack write"
        row = json.loads(st.ledger_path().read_text(encoding="utf-8").splitlines()[-1])
        assert row["event"] == "intent" and row["channel_id"] == "C1" and row["ts"]

    def test_a_failed_append_returns_false(self, monkeypatch, tmp_path):
        d = tmp_path / "isadir"
        d.mkdir()
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_LEDGER_PATH", str(d))
        assert st.append_ledger("intent", proposal_id="p", channel_id="C1") is False
        assert st.read_ledger() is None                   # unreadable, not "empty"

    def test_unarchive_state_needs_an_archive_first(self):
        st.append_ledger("outcome", proposal_id="p", channel_id="C1", outcome="archived", ts=NOW - 50 * DAY)
        st.append_ledger("unarchived_seen", channel_id="C1", unarchive_ts=NOW - 40 * DAY, by="UP1",
                         ts=NOW - 39 * DAY)
        st.append_ledger("unarchived_seen", channel_id="C2", unarchive_ts=NOW, by="UP2", ts=NOW)
        ua = st.unarchive_state(st.read_ledger())
        assert ua["C1"] == {"at": NOW - 40 * DAY, "by": "UP1"}
        assert "C2" in ua    # a later unarchive of an earlier archive is still an unarchive


def test_a_reconciled_already_archived_outcome_folds_terminal_even_without_its_store_event():
    """D-051 r1 c1-monitor#1: the monitor's `already_archived (reconciled)` ledger outcome
    must read ALREADY_ARCHIVED on the claim-expiry path too (a lost store append)."""
    pid = stage()
    st.append_event(st.CLAIMED, proposal_id=pid, cid="C0AAAAAAA1", ts=NOW)
    st.append_ledger("intent", proposal_id=pid, channel_id="C0AAAAAAA1", tapped_by=HARRISON, ts=NOW + 1)
    st.append_ledger("outcome", proposal_id=pid, channel_id="C0AAAAAAA1",
                     outcome="already_archived (reconciled)", ts=NOW + 5000)
    assert st.fold(now=NOW + 6000).proposals[pid].state_of("C0AAAAAAA1") == st.ALREADY_ARCHIVED


class TestClaims:
    def test_claim_expiry_releases_without_an_intent(self):
        pid = stage()
        st.append_event(st.CLAIMED, proposal_id=pid, cid="C0AAAAAAA1", kind="archive", ts=NOW)
        assert st.fold(now=NOW + 60).proposals[pid].state_of("C0AAAAAAA1") == st.CLAIMED
        assert st.fold(now=NOW + st.CLAIM_TTL_S + 1).proposals[pid].state_of("C0AAAAAAA1") == st.OPEN

    def test_claim_plus_intent_without_outcome_is_unknown_locked(self):
        pid = stage()
        st.append_event(st.CLAIMED, proposal_id=pid, cid="C0AAAAAAA1", kind="archive", ts=NOW)
        st.append_ledger("intent", proposal_id=pid, channel_id="C0AAAAAAA1", ts=NOW + 1)
        p = st.fold(now=NOW + 3600).proposals[pid]
        assert p.state_of("C0AAAAAAA1") == st.UNKNOWN
        ok, why, _ = st.decide_row(pid, "C0AAAAAAA1", st.AGREED, actor=HARRISON, now=NOW + 3600)
        assert not ok and why == "already_handled"

    def test_an_in_flight_archive_is_in_progress_until_the_bound(self):
        """c1-state-machine#2 (A13): claimed + a FRESH intent with no outcome yet is an
        archive in flight -- CLAIMED ('In progress…', taps refused in_progress), not a
        locked UNKNOWN; UNKNOWN only once the intent is older than the bound."""
        pid = stage()
        st.append_event(st.CLAIMED, proposal_id=pid, cid="C0AAAAAAA1", kind="archive", ts=NOW)
        st.append_ledger("intent", proposal_id=pid, channel_id="C0AAAAAAA1", ts=NOW + 20)
        f = st.fold(now=NOW + 21)
        assert f.proposals[pid].state_of("C0AAAAAAA1") == st.CLAIMED
        assert f.channel_claim("C0AAAAAAA1") == (pid, "archive")
        ok, why, _ = st.decide_row(pid, "C0AAAAAAA1", st.KEPT, actor=HARRISON, now=NOW + 21)
        assert not ok and why == "in_progress"
        late = NOW + 20 + st.INFLIGHT_S + 1
        assert st.fold(now=late).proposals[pid].state_of("C0AAAAAAA1") == st.UNKNOWN

    def test_a_retry_claim_right_after_a_failed_attempt_is_its_own_live_claim(self):
        """c1-state-machine#3: the intent tolerance is anchored to the claim's own ts --
        a retry claimed within 1 s of the previous attempt's intent is NOT shadowed by
        that attempt's FAILED outcome (so a Keep cannot slip in under it)."""
        pid = stage()
        st.append_event(st.CLAIMED, proposal_id=pid, cid="C0AAAAAAA1", kind="archive", ts=NOW)
        st.append_ledger("intent", proposal_id=pid, channel_id="C0AAAAAAA1", ts=NOW + 1.0)
        st.append_ledger("outcome", proposal_id=pid, channel_id="C0AAAAAAA1",
                         outcome="notice_failed:not_in_channel", ts=NOW + 1.2)
        st.append_event(st.FAILED, proposal_id=pid, cid="C0AAAAAAA1", code="x", ts=NOW + 1.2)
        st.append_event(st.CLAIMED, proposal_id=pid, cid="C0AAAAAAA1", kind="archive", ts=NOW + 1.5)
        f = st.fold(now=NOW + 5)
        assert f.proposals[pid].state_of("C0AAAAAAA1") == st.CLAIMED
        ok, why, _ = st.decide_row(pid, "C0AAAAAAA1", st.KEPT, actor=HARRISON, now=NOW + 5)
        assert not ok and why == "in_progress"

    def test_an_unreadable_ledger_folds_not_ok_and_never_reopens_a_claim(self, monkeypatch, tmp_path):
        """c1-state-machine#4: an unreadable archive ledger is not 'empty' -- the fold is
        not ok (decide_row refuses) and an unresolved claim is never expired open."""
        pid = stage()
        st.append_event(st.CLAIMED, proposal_id=pid, cid="C0AAAAAAA1", kind="archive", ts=NOW)
        st.append_ledger("intent", proposal_id=pid, channel_id="C0AAAAAAA1", ts=NOW + 1)
        d = tmp_path / "isadir"
        d.mkdir()
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_LEDGER_PATH", str(d))
        f = st.fold(now=NOW + st.CLAIM_TTL_S + 100)
        assert f.ok is False and pid in f.proposals          # renderable, never actionable
        assert f.proposals[pid].state_of("C0AAAAAAA1") == st.CLAIMED
        ok, why, _ = st.decide_row(pid, "C0AAAAAAA1", st.KEPT, actor=HARRISON,
                                   now=NOW + st.CLAIM_TTL_S + 100)
        assert not ok and why == "store_unreadable"

    def test_claim_plus_ledger_outcome_folds_as_that_outcome(self):
        """The store append failed after a successful archive: the ledger still tells."""
        pid = stage()
        st.append_event(st.CLAIMED, proposal_id=pid, cid="C0AAAAAAA1", kind="archive", ts=NOW)
        st.append_ledger("intent", proposal_id=pid, channel_id="C0AAAAAAA1", ts=NOW + 1)
        st.append_ledger("outcome", proposal_id=pid, channel_id="C0AAAAAAA1", outcome="archived", ts=NOW + 3)
        assert st.fold(now=NOW + 5).proposals[pid].state_of("C0AAAAAAA1") == st.ARCHIVED

    def test_a_claim_holds_the_channel_across_proposals(self):
        p1 = stage()
        p2 = stage("chanarch-bbbbbbbbbbbb", ts=NOW + 1, delivered=False)
        ok, _, _ = st.claim_row(p1, "C0AAAAAAA1", "archive", actor=HARRISON, now=NOW + 2)
        assert ok
        ok2, why, _ = st.decide_row(p2, "C0AAAAAAA1", st.KEPT, actor=HARRISON, now=NOW + 3)
        assert not ok2 and why == "in_progress"

    def test_exactly_one_of_twenty_concurrent_decisions_wins(self):
        pid = stage()
        barrier = threading.Barrier(20)
        wins: list[bool] = []
        lock = threading.Lock()

        def _go(i):
            barrier.wait()
            ok, _, _ = st.decide_row(pid, "C0AAAAAAA1", st.AGREED if i % 2 else st.KEPT,
                                     actor=HARRISON, now=NOW)
            with lock:
                wins.append(ok)
        threads = [threading.Thread(target=_go, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert wins.count(True) == 1


class TestSupersedeExpiryKeeps:
    def test_a_newer_delivered_card_with_rows_supersedes(self):
        p1 = stage()
        assert st.fold(now=NOW).is_live(p1, NOW + 1)
        p2 = stage("chanarch-bbbbbbbbbbbb", ts=NOW + 10)
        f = st.fold(now=NOW + 11)
        assert not f.is_live(p1, NOW + 11) and f.superseded_by(p1).proposal_id == p2
        assert f.live_proposal(NOW + 11).proposal_id == p2

    @pytest.mark.parametrize("kw", [{"rows": []}, {"delivered": False}, {"blind": "list_incomplete", "rows": []}])
    def test_blind_empty_or_undelivered_scans_retire_nothing(self, kw):
        p1 = stage()
        stage("chanarch-bbbbbbbbbbbb", ts=NOW + 10, **kw)
        assert st.fold(now=NOW + 11).is_live(p1, NOW + 11)

    def test_expiry_is_reader_enforced(self):
        p1 = stage()
        f = st.fold(now=NOW)
        assert f.is_live(p1, NOW + 13 * DAY) and not f.is_live(p1, NOW + 14 * DAY + 1)

    def test_keep_state_counts_and_windows(self):
        p1 = stage()
        st.append_event(st.KEPT, proposal_id=p1, cid="C0AAAAAAA1", ts=NOW)
        ks = st.fold(now=NOW).keep_state(NOW)
        assert ks["C0AAAAAAA1"] == {"count": 1, "until": NOW + 90 * DAY}


class TestScanLock:
    def test_one_holder_at_a_time_and_release_by_token(self):
        t1 = st.acquire_scan_lock(now=NOW)
        assert t1 and st.acquire_scan_lock(now=NOW + 5) is None
        st.release_scan_lock("not-mine")
        assert st.acquire_scan_lock(now=NOW + 6) is None
        st.release_scan_lock(t1)
        t2 = st.acquire_scan_lock(now=NOW + 7)
        assert t2 and t2 != t1

    def test_a_stale_or_unreadable_lock_is_taken_over(self):
        t1 = st.acquire_scan_lock(now=NOW)
        assert st.acquire_scan_lock(now=NOW + st.SCAN_LOCK_STALE_S + 1)
        st.scan_lock_path().write_text("garbage", encoding="utf-8")
        assert st.acquire_scan_lock(now=NOW)
        assert t1

    def _hold(self, **body):
        st.scan_lock_path().parent.mkdir(parents=True, exist_ok=True)
        st.scan_lock_path().write_text(json.dumps({"ts": NOW, "token": "theirs", **body}),
                                       encoding="utf-8")

    def test_a_lock_left_by_a_dead_process_is_taken_over_at_once(self):
        """c1-state-machine#7: a hard kill (the doctrine-5 restart's Stop-Process -Force)
        skips the finally that releases the lock; the next ask must not answer 'A scan
        is already running; its card will arrive here' for 30 minutes."""
        import subprocess
        import sys
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=60)
        self._hold(pid=proc.pid, nonce="n-dead")
        assert st.acquire_scan_lock(now=NOW + 60)                  # well inside 30 min

    def test_our_own_pid_from_a_previous_instance_is_taken_over(self):
        self._hold(pid=os.getpid(), nonce="a-previous-instance")
        assert st.acquire_scan_lock(now=NOW + 60)
        self._hold(pid=os.getpid())                                 # a pre-nonce body
        assert st.acquire_scan_lock(now=NOW + 60)

    def test_a_live_holder_still_holds(self):
        self._hold(pid=os.getppid(), nonce="n-parent")              # a live process
        assert st.acquire_scan_lock(now=NOW + 60) is None
        tok = None
        st.scan_lock_path().unlink()
        tok = st.acquire_scan_lock(now=NOW)                         # THIS process, this run
        assert tok and st.acquire_scan_lock(now=NOW + 60) is None


class TestDemotionHelpers:
    def test_dry_run_writes_nothing(self):
        assert st.write_demotion({"channel_id": "C1"}, dry_run=True) is False
        assert not policy.demotion_path().exists()

    def test_write_then_clear_appends_an_acknowledged_row(self):
        assert st.write_demotion({"channel_id": "C1", "archive_ts": "1790.1", "since": "x",
                                  "reason": "unattributed"}, dry_run=False)
        assert policy.is_demoted()
        dry = st.clear_demotion(actor=HARRISON, dry_run=True)
        assert dry["would_clear"] and policy.is_demoted() and st.read_ledger() == []
        out = st.clear_demotion(actor=HARRISON, dry_run=False)
        assert out["cleared"] and not policy.is_demoted()
        ack = st.read_ledger()[-1]
        assert ack["event"] == "acknowledged" and ack["channel_id"] == "C1" and ack["archive_ts"] == "1790.1"

    def test_clear_refuses_when_the_ack_row_cannot_land(self, monkeypatch, tmp_path):
        st.write_demotion({"channel_id": "C1", "archive_ts": "1790.1"}, dry_run=False)
        d = tmp_path / "isadir"
        d.mkdir()
        monkeypatch.setenv("CORA_CHANNEL_ARCHIVE_LEDGER_PATH", str(d))
        out = st.clear_demotion(actor=HARRISON, dry_run=False)
        assert not out["cleared"] and policy.is_demoted()
