"""Tests for the disk-backed pending-order store + push ledger."""

from __future__ import annotations

import pytest

from cora.deposco_orders import pending
from cora.deposco_orders.preflight import PreflightCheck, PreflightResult

PASSED_PREFLIGHT = PreflightResult(
    passed=True, checked_at="2026-09-23T10:00:00-07:00",
    checks=[PreflightCheck("number_miss", True)],
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_DEPOSCO_PENDING_DIR", str(tmp_path / "pending"))
    monkeypatch.setenv("CORA_DEPOSCO_PUSH_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("CORA_DEPOSCO_DEMOTION_STATE_PATH", str(tmp_path / "demotion.json"))
    monkeypatch.delenv("CORA_DEPOSCO_STANDING_CHANNELS", raising=False)


def _stage(channel="wholesale", number="F3E-W-GOTHAM-4471"):
    return pending.stage_entry(
        channel=channel, number=number,
        payload={"order": [{"number": number}]},
        preflight_result=PASSED_PREFLIGHT, authored_by="Harrison",
    )


class TestStagingAndRetrieval:
    def test_staged_entry_is_retrievable_by_id(self):
        entry = _stage()
        fetched = pending.get_entry(entry["id"])
        assert fetched["state"] == pending.STATE_STAGED
        assert fetched["number"] == "F3E-W-GOTHAM-4471"

    def test_unknown_id_returns_none(self):
        assert pending.get_entry("deposco-doesnotexist") is None

    def test_a_corrupted_entry_file_returns_none_rather_than_raising(self):
        """Atomic writes (drive_io.write_text_atomic) make a torn file from a
        crash mid-write unreachable in normal operation, but a corrupted file
        can still land by other means (disk fault, manual edit) -- this must
        fail safe (treated like a miss) rather than crash the tap handler."""
        entry = _stage()
        pending._entry_path(entry["id"]).write_text("{not valid json", encoding="utf-8")
        assert pending.get_entry(entry["id"]) is None

    def test_payload_hash_is_recorded(self):
        entry = _stage()
        assert entry["payload_hash"] == pending.hash_payload(entry["payload"])

    def test_two_stages_mint_distinct_ids(self):
        a, b = _stage(number="A"), _stage(number="B")
        assert a["id"] != b["id"]


class TestClaimExactlyOnce:
    def test_a_staged_entry_can_be_claimed(self):
        entry = _stage()
        claimed = pending.claim_for_push(entry["id"], "U_HARRISON")
        assert claimed is not None
        assert claimed["state"] == pending.STATE_CLAIMED
        assert claimed["claimed_by"] == "U_HARRISON"

    def test_a_second_claim_on_the_same_entry_loses_the_race(self):
        entry = _stage()
        first = pending.claim_for_push(entry["id"], "U_HARRISON")
        second = pending.claim_for_push(entry["id"], "U_HARRISON")
        assert first is not None
        assert second is None

    def test_claiming_a_missing_entry_returns_none(self):
        assert pending.claim_for_push("deposco-nope", "U_HARRISON") is None

    def test_claiming_an_already_terminal_entry_returns_none(self):
        entry = _stage()
        pending.claim_for_push(entry["id"], "U_HARRISON")
        pending.resolve(entry["id"], pending.STATE_CONFIRMED)
        assert pending.claim_for_push(entry["id"], "U_HARRISON") is None


class TestClaimUnderRealConcurrency:
    def test_twenty_threads_racing_for_one_claim_exactly_one_wins(self):
        """The sequential double-claim test above proves the STATE MACHINE is
        correct; it does not prove the LOCK actually serializes real
        concurrent threads. This does."""
        import threading

        entry = _stage()
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()
            claimed = pending.claim_for_push(entry["id"], "U_HARRISON")
            with lock:
                results.append(claimed is not None)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 20
        assert sum(results) == 1, "more than one thread claimed the same pending entry"


class TestReleaseClaim:
    def test_release_returns_a_claimed_entry_to_staged(self):
        entry = _stage()
        pending.claim_for_push(entry["id"], "U_HARRISON")
        released = pending.release_claim(entry["id"], "preflight drift detected")
        assert released["state"] == pending.STATE_STAGED
        assert released["claimed_by"] == ""
        # re-claimable after release
        assert pending.claim_for_push(entry["id"], "U_HARRISON") is not None

    def test_release_never_moves_a_terminal_entry(self):
        entry = _stage()
        pending.claim_for_push(entry["id"], "U_HARRISON")
        pending.resolve(entry["id"], pending.STATE_CONFIRMED)
        released = pending.release_claim(entry["id"], "should be a no-op")
        assert released["state"] == pending.STATE_CONFIRMED


class TestResolveAndUnknownLock:
    def test_resolve_moves_to_a_terminal_state(self):
        entry = _stage()
        pending.claim_for_push(entry["id"], "U_HARRISON")
        resolved = pending.resolve(entry["id"], pending.STATE_CONFIRMED, order_number="X")
        assert resolved["state"] == pending.STATE_CONFIRMED
        assert resolved["order_number"] == "X"

    def test_resolve_refuses_a_non_terminal_target_state(self):
        entry = _stage()
        with pytest.raises(ValueError):
            pending.resolve(entry["id"], pending.STATE_CLAIMED)

    def test_unknown_locks_the_entry_forever(self):
        """UNKNOWN never auto-retries -- once written, NOTHING may move this
        entry again, including a later resolve() call."""
        entry = _stage()
        pending.claim_for_push(entry["id"], "U_HARRISON")
        pending.resolve(entry["id"], pending.STATE_UNKNOWN)
        second = pending.resolve(entry["id"], pending.STATE_CONFIRMED)
        assert second["state"] == pending.STATE_UNKNOWN, "a terminal state must never be overwritten"

    def test_resolve_on_a_missing_entry_returns_none(self):
        assert pending.resolve("deposco-nope", pending.STATE_CONFIRMED) is None


class TestDismiss:
    def test_dismiss_a_staged_entry(self):
        entry = _stage()
        dismissed = pending.dismiss(entry["id"], "U_HARRISON", reason="wrong PO number")
        assert dismissed["state"] == pending.STATE_DISMISSED
        assert dismissed["dismiss_reason"] == "wrong PO number"

    def test_dismiss_a_claimed_entry_is_refused(self):
        """A Dismiss tap arriving mid-publish must not steal a claimed row --
        same race publish_cards.py's docstring names."""
        entry = _stage()
        pending.claim_for_push(entry["id"], "U_HARRISON")
        assert pending.dismiss(entry["id"], "U_HARRISON") is None


class TestLedgerAndConsecutiveClean:
    def test_confirmed_rows_count_as_clean(self):
        pending.append_ledger_row(channel="wholesale", number="A",
                                  event=pending.STATE_CONFIRMED, clean=True)
        pending.append_ledger_row(channel="wholesale", number="B",
                                  event=pending.STATE_CONFIRMED, clean=True)
        assert pending.consecutive_clean_count("wholesale") == 2

    def test_a_not_clean_row_resets_the_count_going_forward(self):
        pending.append_ledger_row(channel="wholesale", number="A",
                                  event=pending.STATE_CONFIRMED, clean=True)
        pending.append_ledger_row(channel="wholesale", number="B",
                                  event=pending.STATE_MISMATCH, clean=False)
        pending.append_ledger_row(channel="wholesale", number="C",
                                  event=pending.STATE_CONFIRMED, clean=True)
        assert pending.consecutive_clean_count("wholesale") == 1

    def test_channels_are_independent(self):
        pending.append_ledger_row(channel="wholesale", number="A",
                                  event=pending.STATE_CONFIRMED, clean=True)
        pending.append_ledger_row(channel="fba", number="B",
                                  event=pending.STATE_MISMATCH, clean=False)
        assert pending.consecutive_clean_count("wholesale") == 1
        assert pending.consecutive_clean_count("fba") == 0

    def test_a_root_cause_note_row_does_not_itself_count_as_a_push(self):
        pending.append_ledger_row(channel="wholesale", number="A",
                                  event=pending.STATE_CONFIRMED, clean=True)
        pending.add_root_cause_note("wholesale", "A", "unrelated note")
        assert pending.consecutive_clean_count("wholesale") == 1


class TestStageBlockedGate:
    def test_no_history_is_never_blocked(self):
        blocked, _ = pending.channel_stage_blocked("wholesale")
        assert blocked is False

    def test_a_clean_last_push_is_not_blocked(self):
        pending.append_ledger_row(channel="wholesale", number="A",
                                  event=pending.STATE_CONFIRMED, clean=True)
        blocked, _ = pending.channel_stage_blocked("wholesale")
        assert blocked is False

    def test_a_not_clean_last_push_with_no_note_blocks(self):
        pending.append_ledger_row(channel="wholesale", number="A",
                                  event=pending.STATE_MISMATCH, clean=False)
        blocked, reason = pending.channel_stage_blocked("wholesale")
        assert blocked is True
        assert "root-cause note" in reason

    def test_adding_the_note_unblocks(self):
        pending.append_ledger_row(channel="wholesale", number="A",
                                  event=pending.STATE_MISMATCH, clean=False)
        pending.add_root_cause_note("wholesale", "A", "Nimbl corrected the ship-to server-side")
        blocked, _ = pending.channel_stage_blocked("wholesale")
        assert blocked is False

    def test_a_note_for_a_different_number_does_not_unblock(self):
        pending.append_ledger_row(channel="wholesale", number="A",
                                  event=pending.STATE_MISMATCH, clean=False)
        pending.add_root_cause_note("wholesale", "B", "wrong order entirely")
        blocked, _ = pending.channel_stage_blocked("wholesale")
        assert blocked is True

    def test_other_channels_are_unaffected(self):
        pending.append_ledger_row(channel="wholesale", number="A",
                                  event=pending.STATE_MISMATCH, clean=False)
        blocked, _ = pending.channel_stage_blocked("fba")
        assert blocked is False


class TestStandingApproverTier:
    def test_default_is_supervised(self):
        assert pending.approver_tier("wholesale") == "supervised"

    def test_env_flag_alone_is_not_enough_without_the_other_condition(self, monkeypatch):
        """This test's name describes the INVARIANT; the env flag IS one of
        the two required conditions, so setting it alone (no demotion) DOES
        grant standing -- see the paired demotion test below for the other half."""
        monkeypatch.setenv("CORA_DEPOSCO_STANDING_CHANNELS", "wholesale")
        assert pending.approver_tier("wholesale") == "standing"

    def test_demotion_overrides_the_env_flag(self, monkeypatch):
        monkeypatch.setenv("CORA_DEPOSCO_STANDING_CHANNELS", "wholesale")
        pending.demote_channel("wholesale", reason="MISMATCH on F3E-W-GOTHAM-9999")
        assert pending.approver_tier("wholesale") == "supervised"

    def test_a_channel_not_in_the_env_flag_stays_supervised_even_with_no_demotion(self):
        assert pending.approver_tier("fba") == "supervised"

    def test_clear_demotion_restores_standing_when_the_flag_is_still_set(self, monkeypatch):
        monkeypatch.setenv("CORA_DEPOSCO_STANDING_CHANNELS", "wholesale")
        pending.demote_channel("wholesale", reason="test")
        pending.clear_demotion("wholesale")
        assert pending.approver_tier("wholesale") == "standing"

    def test_demotion_on_one_channel_does_not_affect_another(self, monkeypatch):
        monkeypatch.setenv("CORA_DEPOSCO_STANDING_CHANNELS", "wholesale,fba")
        pending.demote_channel("wholesale", reason="test")
        assert pending.approver_tier("wholesale") == "supervised"
        assert pending.approver_tier("fba") == "standing"
