"""Unit tests for the S2 generic confirm-button claim dispatcher in
tool_dispatch.py (_claim_stash_by_id, resolve_and_claim_stash,
snapshot_stash_ids/freshest_changed_stash) -- the shared claim-then-execute
path used by app.py's Confirm/Cancel tap handler across all 9 stash kinds.

Security invariants under test (design doc section 5 + the D-051 build brief):
requester-only authorization checked BEFORE any state mutation, atomic claim
keyed on an exact stash_id match (double-tap safety), honest exhaustive
lifecycle wording (orphaned/already_handled/superseded/expired/cancelled/
executed/indeterminate), CORA_EVAL_MODE never executes, bulk Shopify batch is
one stash/one card/one confirm.
"""

import contextvars
import time
from unittest.mock import patch

import pytest

import cora.tools.tool_dispatch as td
from cora import confirm_cards as cc

HARRISON = "U0B2RM2JYJ1"
OTHER = "U0B3RU5Q55G"
ATTACKER = "U0BATTACKER1"
_CH = "cora-build"


@pytest.fixture(autouse=True)
def _clear_all_stores():
    td._PENDING_ASANA_WRITES.clear()
    td._PENDING_SHOPIFY_WRITES.clear()
    td._PENDING_CALENDAR_WRITES.clear()
    td._PENDING_LEXICON_ADDS.clear()
    td._PENDING_CODE_QUEUE.clear()
    td._PENDING_DELEGATED_WORK.clear()
    td._PENDING_REMEMBER.clear()
    td._PENDING_FORGET_NOTE.clear()
    td._PENDING_SCHEDULE_MEETING.clear()
    td._PENDING_ASK_STASH.clear()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    with cc._ASK_INDEX_LOCK:
        cc._ASK_INDEX.clear()
    yield
    td._PENDING_ASANA_WRITES.clear()
    td._PENDING_SHOPIFY_WRITES.clear()
    td._PENDING_CALENDAR_WRITES.clear()
    td._PENDING_LEXICON_ADDS.clear()
    td._PENDING_CODE_QUEUE.clear()
    td._PENDING_DELEGATED_WORK.clear()
    td._PENDING_REMEMBER.clear()
    td._PENDING_FORGET_NOTE.clear()
    td._PENDING_SCHEDULE_MEETING.clear()
    td._PENDING_ASK_STASH.clear()


def _stash_asana_delete(user=HARRISON, channel=_CH, gid="g1", label="Test task"):
    sid = cc.mint_stash_id("asana", user, channel)
    td._store_pending_asana_write(user, channel, {
        "action": "delete", "gid": gid, "label": label, "ts": time.time(), "stash_id": sid,
    })
    return sid


def _stash_asana_create(user=HARRISON, channel=_CH):
    sid = cc.mint_stash_id("asana", user, channel)
    td._store_pending_asana_write(user, channel, {
        "action": "create", "title": "New task", "assignee_gid": "g", "assignee_display": "H",
        "project_gid": None, "notes": None, "due_on": None, "notices": [],
        "follower_gids": [], "follower_displays": [], "ts": time.time(), "stash_id": sid,
    })
    return sid


def _stash_shopify_single(user=HARRISON, channel=_CH):
    sid = cc.mint_stash_id("shopify", user, channel)
    td._store_pending_shopify_write(user, channel, {
        "inventory_item_id": "i1", "location_id": "l1", "target_qty": 10,
        "preview_qty": 8, "delta": 2, "unit": "units", "variant_label": "Pure",
        "location_label": "Office", "resolved_from": "", "lex": None,
        "ts": time.time(), "stash_id": sid,
    })
    return sid


def _stash_shopify_batch(user=HARRISON, channel=_CH, n=3):
    sid = cc.mint_stash_id("shopify", user, channel)
    rows = [{
        "inventory_item_id": f"i{i}", "location_id": "l1", "target_qty": 10 + i,
        "preview_qty": 8, "delta": None, "unit": "units", "variant_label": f"Variant {i}",
        "location_label": "Office", "resolved_from": "", "lex": None,
    } for i in range(n)]
    td._store_pending_shopify_write(user, channel, {
        "rows": rows, "skipped": [], "ts": time.time(), "stash_id": sid,
    })
    return sid


def _stash_remember(user=HARRISON, channel=_CH, note_text="the wifi password is x"):
    sid = cc.mint_stash_id("remember", user, channel)
    td._store_pending_remember(user, channel, {
        "note_text": note_text, "entity": "F3E", "sub_entity": None,
        "share_requested": False, "channel_name": channel, "ts": time.time(), "stash_id": sid,
    })
    return sid


class TestClaimStashById:
    def test_claimed_pops_the_entry(self):
        sid = _stash_asana_delete()
        status, entry = td._claim_stash_by_id("asana", HARRISON, _CH, sid)
        assert status == "claimed"
        assert entry["gid"] == "g1"
        assert td._peek_pending_asana(HARRISON, _CH) is None  # popped

    def test_superseded_when_id_does_not_match_current_entry(self):
        _stash_asana_delete()  # old preview
        stale_id = "0" * 16
        status, entry = td._claim_stash_by_id("asana", HARRISON, _CH, stale_id)
        assert status == "superseded"
        assert entry is None
        # The CURRENT entry must survive -- a superseded tap never destroys
        # the newer pending it wasn't meant for.
        assert td._peek_pending_asana(HARRISON, _CH) is not None

    def test_expired_pops_the_stale_entry(self):
        sid = cc.mint_stash_id("asana", HARRISON, _CH)
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "delete", "gid": "g1", "label": "X",
            "ts": time.time() - td._ASANA_PENDING_TTL_SECONDS - 5, "stash_id": sid,
        })
        status, entry = td._claim_stash_by_id("asana", HARRISON, _CH, sid)
        assert status == "expired"
        assert entry is not None
        assert td._peek_pending_asana(HARRISON, _CH) is None  # popped either way

    def test_not_found_when_nothing_stashed(self):
        status, entry = td._claim_stash_by_id("asana", HARRISON, _CH, "anything")
        assert status == "not_found"
        assert entry is None

    def test_not_found_for_unknown_kind(self):
        status, entry = td._claim_stash_by_id("not_a_real_kind", HARRISON, _CH, "x")
        assert status == "not_found"


class TestResolveAndClaimStashSecurity:
    def test_orphaned_for_never_minted_id(self):
        result = td.resolve_and_claim_stash("f" * 16, HARRISON, "confirm")
        assert result == {"outcome": "orphaned"}

    def test_garbage_value_refused_no_crash(self):
        # A spoofed/garbage button value must never crash the receiver.
        for junk in ["", "not-hex", "'; DROP TABLE--", "a" * 500, "🎉🎉🎉"]:
            result = td.resolve_and_claim_stash(junk, HARRISON, "confirm")
            assert result["outcome"] in ("orphaned",)

    def test_cross_user_tap_is_unauthorized_and_touches_nothing(self):
        sid = _stash_asana_delete(user=HARRISON)
        with patch.object(td.asana_client, "delete_task") as mock:
            result = td.resolve_and_claim_stash(sid, ATTACKER, "confirm")
        assert result == {"outcome": "unauthorized", "owner": HARRISON}
        mock.assert_not_called()
        # Untouched: the real owner's pending survives for their OWN tap.
        assert td._peek_pending_asana(HARRISON, _CH) is not None

    def test_unauthorized_check_runs_before_any_claim_attempt(self):
        # Authorization must be checked BEFORE the atomic claim -- an
        # unauthorized tap must never pop the victim's pending.
        sid = _stash_asana_delete(user=HARRISON)
        td.resolve_and_claim_stash(sid, ATTACKER, "confirm")
        status, entry = td._claim_stash_by_id("asana", HARRISON, _CH, sid)
        assert status == "claimed"  # still there, poppable by the real owner

    def test_double_tap_race_exactly_one_execute(self):
        sid = _stash_asana_delete()
        with patch.object(td.asana_client, "delete_task", return_value=None) as mock:
            r1 = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
            r2 = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert r1["outcome"] == "executed"
        assert r2["outcome"] == "already_handled"
        mock.assert_called_once()

    def test_tap_after_typed_confirm_reads_already_handled(self):
        # Simulates the typed path having ALREADY consumed the pending (it
        # never touches the confirm_cards index) -- a stale button tap must
        # read as an idempotent ack, not a false "orphaned".
        sid = _stash_asana_delete()
        td._claim_pending_asana(HARRISON, _CH, "delete")  # the typed path's own claim
        result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result == {"outcome": "already_handled"}

    def test_superseded_card_tap_is_honest(self):
        _stash_asana_delete()          # first preview
        stale_id = "1" * 16
        with cc._INDEX_LOCK:
            cc._INDEX[stale_id] = {"kind": "asana", "user": HARRISON, "channel": _CH,
                                   "ts": time.time()}
        _stash_asana_create()          # a DIFFERENT action overwrites... actually asana
        # Re-preview the SAME (user, channel) slot with a fresh id (simulates
        # a newer preview overwriting the old one this stale_id pointed to).
        fresh_sid = cc.mint_stash_id("asana", HARRISON, _CH)
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "delete", "gid": "g2", "label": "Newer", "ts": time.time(),
            "stash_id": fresh_sid,
        })
        result = td.resolve_and_claim_stash(stale_id, HARRISON, "confirm")
        assert result == {"outcome": "superseded"}
        # The NEWER pending must survive a stale tap.
        assert td._peek_pending_asana(HARRISON, _CH)["stash_id"] == fresh_sid

    def test_expired_tap_gets_tombstone_label(self):
        sid = cc.mint_stash_id("asana", HARRISON, _CH)
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "delete", "gid": "g1", "label": "Old Task",
            "ts": time.time() - td._ASANA_PENDING_TTL_SECONDS - 5, "stash_id": sid,
        })
        result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "expired"
        assert "Old Task" in result["label"]

    def test_expired_tap_gets_tombstone_label_create(self):
        # create's pending entry has NO "label" key at all -- the name lives
        # under "title" (D-051 8-02 review finding: this used to fall back
        # to the generic "that task").
        sid = cc.mint_stash_id("asana", HARRISON, _CH)
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "create", "title": "Brand New Task", "assignee_gid": "g",
            "assignee_display": "H", "project_gid": None, "notes": None,
            "due_on": None, "notices": [], "follower_gids": [], "follower_displays": [],
            "ts": time.time() - td._ASANA_PENDING_TTL_SECONDS - 5, "stash_id": sid,
        })
        result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "expired"
        assert "Brand New Task" in result["label"]

    def test_expired_tap_gets_tombstone_label_subtask(self):
        # subtask's own name lives under "name"; "parent_label" names the
        # PARENT task, a different field -- the tombstone must name the
        # subtask itself, not fall back to the generic "that task".
        sid = cc.mint_stash_id("asana", HARRISON, _CH)
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "subtask", "parent_gid": "g1", "parent_label": "Parent Task",
            "name": "Follow up with client", "notes": None, "due_on": None,
            "assignee_gid": "", "assignee_display": "unassigned",
            "ts": time.time() - td._ASANA_PENDING_TTL_SECONDS - 5, "stash_id": sid,
        })
        result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "expired"
        assert "Follow up with client" in result["label"]

    def test_orphaned_after_simulated_restart(self):
        # A "restart" clears BOTH the index and every pending store (both are
        # in-memory) -- a tap on a stash_id from before must read orphaned.
        sid = _stash_asana_delete()
        with cc._INDEX_LOCK:
            cc._INDEX.clear()
        td._PENDING_ASANA_WRITES.clear()
        result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result == {"outcome": "orphaned"}

    def test_drift_during_execute_reports_re_previewed_not_executed(self):
        # D-051 adversarial review finding (HIGH): Shopify's own live-inventory
        # re-check inside execute can decline to write and re-stash a FRESH
        # preview instead. A confirm tap landing here must NOT be reported as
        # "executed" (which would drop the card's buttons over a live,
        # un-carded pending) -- it must surface the fresh stash_id instead.
        sid = _stash_shopify_single()  # preview_qty=8, delta=2 -> target 10
        with patch.object(td.shopify_client, "get_inventory_level", return_value=99), \
             patch.object(td.shopify_client, "set_inventory_level") as mock_set, \
             patch.object(td, "_load_shopify_write_config", return_value=({"office"}, {})):
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "re_previewed"
        mock_set.assert_not_called()
        fresh_sid = result["stash_id"]
        assert fresh_sid and fresh_sid != sid
        # The fresh re-preview is a REAL, live, executable pending.
        fresh_pending = td._peek_pending_shopify(HARRISON, _CH)
        assert fresh_pending["stash_id"] == fresh_sid
        assert fresh_pending["preview_qty"] == 99
        # The OLD stash_id is dead (claimed) -- a stale tap on it now reads
        # already_handled, never a second execute.
        stale_result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert stale_result["outcome"] == "already_handled"
        # The FRESH stash_id is fully confirmable via the button path.
        with patch.object(td.shopify_client, "get_inventory_level", return_value=99), \
             patch.object(td.shopify_client, "set_inventory_level", return_value=101) as mock_set2, \
             patch.object(td, "_load_shopify_write_config", return_value=({"office"}, {})):
            final = td.resolve_and_claim_stash(fresh_sid, HARRISON, "confirm")
        assert final["outcome"] == "executed"
        mock_set2.assert_called_once()

    def test_floor_guard_during_execute_mints_a_fresh_reachable_id(self):
        # The floor-guard branch (delta would go below zero against the FRESH
        # live count) used to reuse the OLD, already-claimed stash_id --
        # permanently unreachable by any future button tap. Must mint fresh.
        sid = cc.mint_stash_id("shopify", HARRISON, _CH)
        td._store_pending_shopify_write(HARRISON, _CH, {
            "inventory_item_id": "i1", "location_id": "l1", "target_qty": 5,
            "preview_qty": 8, "delta": -3, "unit": "units", "variant_label": "Pure",
            "location_label": "Office", "resolved_from": "", "lex": None,
            "ts": time.time(), "stash_id": sid,
        })
        with patch.object(td.shopify_client, "get_inventory_level", return_value=1), \
             patch.object(td, "_load_shopify_write_config", return_value=({"office"}, {})):
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "re_previewed"
        fresh_sid = result["stash_id"]
        assert fresh_sid and fresh_sid != sid
        assert cc.index_lookup(fresh_sid) is not None  # reachable by a future tap

    def test_cancel_pops_without_executing(self):
        sid = _stash_asana_delete()
        with patch.object(td.asana_client, "delete_task") as mock:
            result = td.resolve_and_claim_stash(sid, HARRISON, "cancel")
        assert result == {"outcome": "cancelled"}
        mock.assert_not_called()
        assert td._peek_pending_asana(HARRISON, _CH) is None

    def test_indeterminate_on_crash_after_claim(self):
        sid = _stash_asana_delete()
        with patch.object(td, "_execute_claimed_stash", side_effect=RuntimeError("boom")):
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result == {"outcome": "indeterminate"}
        # The entry was already popped by the atomic claim (apply-first
        # ordering) -- a retry can never double-apply even after a crash.
        assert td._peek_pending_asana(HARRISON, _CH) is None

    def test_eval_mode_never_executes(self, monkeypatch):
        sid = _stash_asana_delete()
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        with patch.object(td.asana_client, "delete_task") as mock:
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result == {"outcome": "orphaned"}
        mock.assert_not_called()
        # Untouched -- eval mode must not even claim the entry.
        assert td._peek_pending_asana(HARRISON, _CH) is not None


class TestResolveAndClaimStashExecution:
    def test_asana_delete_executes_and_strips_sentinel(self):
        sid = _stash_asana_delete()
        with patch.object(td.asana_client, "delete_task", return_value=None) as mock:
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "executed"
        mock.assert_called_once_with("g1")
        assert "WRITE_CONFIRMED" not in result["message"]
        assert "deleted" in result["message"].lower()

    def test_shopify_single_executes(self):
        sid = _stash_shopify_single()
        with patch.object(td.shopify_client, "get_inventory_level", return_value=8), \
             patch.object(td.shopify_client, "set_inventory_level", return_value=10) as mock_set, \
             patch.object(td, "_load_shopify_write_config", return_value=({"office"}, {})):
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "executed"
        mock_set.assert_called_once()

    def test_shopify_bulk_batch_is_one_stash_one_confirm(self):
        # The whole N-row batch previews under ONE stash_id; a single confirm
        # tap executes ALL rows via one claim (no per-row card/confirm).
        sid = _stash_shopify_batch(n=3)
        with patch.object(td.shopify_client, "get_inventory_level", return_value=8), \
             patch.object(td.shopify_client, "set_inventory_level", return_value=10) as mock_set, \
             patch.object(td, "_load_shopify_write_config", return_value=({"office"}, {})):
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "executed"
        assert mock_set.call_count == 3          # all 3 rows written
        assert "3 item" in result["message"]      # one combined outcome message
        # A second tap on the SAME (now-consumed) stash_id must not re-fire.
        with patch.object(td.shopify_client, "set_inventory_level") as mock_set2:
            result2 = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result2["outcome"] == "already_handled"
        mock_set2.assert_not_called()

    def test_remember_executes_from_stash_not_model_echo(self):
        sid = _stash_remember(note_text="the real stashed note")

        class _FakeKB:
            def __init__(self):
                self.saved = []

            def upsert_documents(self, *a, **k):
                return None

        # save_note / conflict_excerpt are patched directly to avoid needing a
        # real KB -- the point under test is that the STASHED note_text is
        # what gets passed through, not anything from a confirm-turn echo.
        with patch.object(td, "_notes_kb", return_value=(_FakeKB(), __import__("threading").Lock())), \
             patch("cora.user_notes.save_note", return_value="note:U1:abc") as mock_save, \
             patch("cora.user_notes.conflict_excerpt", return_value=""):
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "executed"
        assert mock_save.call_args.kwargs["note_text"] == "the real stashed note"

    def test_code_queue_wiring_confirmed_executes_stash(self):
        sid = cc.mint_stash_id("code_queue", HARRISON, _CH)
        td._store_pending_code_queue(HARRISON, _CH, {
            "request": "fix the widget", "channel_id": "C1", "ts": time.time(), "stash_id": sid,
        })
        with patch("cora.code_queue.code_queue_level", return_value="live"), \
             patch("cora.code_queue.queue_explicit", return_value=("cq-1", "queued")):
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "executed"

    def test_delegated_wiring_confirmed_executes_stash(self):
        sid = cc.mint_stash_id("delegated", HARRISON, _CH)
        td._store_pending_delegated(HARRISON, _CH, {
            "archetype": "research_brief", "brief": "test brief", "deliverable": "md",
            "entity": "F3E", "channel_id": "C1", "channel_name": _CH, "thread_ts": "",
            "ts": time.time(), "stash_id": sid,
        })
        with patch("cora.delegated_work.submit_job", return_value=(object(), "queued", "Job queued.")), \
             patch("cora.delegated_work.delegated_level", return_value="live"):
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "executed"

    def test_calendar_wiring_confirmed_executes_stash(self):
        sid = cc.mint_stash_id("calendar", HARRISON, _CH)
        td._store_pending_calendar_write(HARRISON, _CH, {
            "action": "delete", "event_id": "evt1", "summary": "Sync",
            "user_email": "h@x.com", "ts": time.time(), "stash_id": sid,
        })
        with patch.object(td.calendar_client, "delete_event") as mock_del:
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "executed"
        mock_del.assert_called_once()

    def test_lexicon_wiring_confirmed_executes_stash(self):
        sid = cc.mint_stash_id("lexicon", HARRISON, _CH)
        td._store_pending_lexicon_add(HARRISON, _CH, {
            "payload": {"term": "x", "aliases": [], "type": "process", "entity": "F3E",
                       "canonical": "X", "canonical_name": "X meaning", "lane": "taught",
                       "contributor_id": HARRISON},
            "ts": time.time(), "stash_id": sid,
        })
        with patch("cora.lexicon_writer.apply_lexicon_update", return_value=(True, "saved")):
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "executed"

    def test_forget_note_wiring_confirmed_executes_stash(self):
        sid = cc.mint_stash_id("forget_note", HARRISON, _CH)
        td._store_pending_forget_note(HARRISON, _CH, {
            "note_id": "note:U1:abc", "ts": time.time(), "stash_id": sid,
        })

        class _FakeKB:
            def delete_user_note(self, note_id, owner_slack):
                return 1

        with patch.object(td, "_notes_kb", return_value=(_FakeKB(), __import__("threading").Lock())):
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "executed"
        assert "deleted" in result["message"].lower()

    def test_schedule_meeting_wiring_confirmed_books_soonest_slot(self):
        sid = cc.mint_stash_id("schedule_meeting", HARRISON, _CH)
        td._store_pending_schedule_meeting(HARRISON, _CH, {
            "requester_email": "h@x.com", "requester_name": "Harrison", "title": "Sync",
            "names": ["Harrison", "Larry"], "emails": ["h@x.com", "l@x.com"],
            "slots": [("2026-06-02T09:00:00-07:00", "2026-06-02T09:30:00-07:00")],
            "ts": time.time(), "stash_id": sid,
        })
        fake_event = {"id": "e1", "summary": "Sync", "htmlLink": "https://x", "attendees": []}
        with patch.object(td.calendar_client, "create_event", return_value=fake_event) as mock_create:
            result = td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        assert result["outcome"] == "executed"
        assert mock_create.call_args.kwargs["start"] == "2026-06-02T09:00:00-07:00"


class TestSnapshotDiff:
    def test_no_change_returns_none(self):
        before = td.snapshot_stash_ids(HARRISON, _CH)
        assert td.freshest_changed_stash(before, HARRISON, _CH) is None

    def test_fresh_asana_preview_detected(self):
        before = td.snapshot_stash_ids(HARRISON, _CH)
        cc.begin_turn()  # S1 fix: a stash only counts as "this turn's" if minted within one
        sid = _stash_asana_delete()
        changed = td.freshest_changed_stash(before, HARRISON, _CH)
        assert changed == ("asana", sid)

    def test_unrelated_users_channel_isolated(self):
        before = td.snapshot_stash_ids(OTHER, _CH)
        _stash_asana_delete(user=HARRISON)  # a DIFFERENT user's fresh preview
        assert td.freshest_changed_stash(before, OTHER, _CH) is None

    def test_claimed_stash_no_longer_reads_as_changed(self):
        before = td.snapshot_stash_ids(HARRISON, _CH)
        sid = _stash_asana_delete()
        with patch.object(td.asana_client, "delete_task", return_value=None):
            td.resolve_and_claim_stash(sid, HARRISON, "confirm")
        # After the claim (pop), a snapshot taken AFTER that pop shows no
        # stash_id for asana again -- so a stale `before` from way earlier
        # would show "changed" only if a DIFFERENT id appears later, not a
        # phantom re-detection of the already-consumed one.
        after_claim_snapshot = td.snapshot_stash_ids(HARRISON, _CH)
        assert after_claim_snapshot["asana"] is None

    def test_fresh_ask_detected_as_pseudo_kind(self):
        before = td.snapshot_stash_ids(HARRISON, _CH)
        cc.begin_turn()  # S1 fix: a stash only counts as "this turn's" if minted within one
        aid = cc.mint_ask_id(HARRISON, _CH)
        td._store_pending_ask(HARRISON, _CH, {
            "ask_id": aid, "ask_kind": "variant", "loc_id": "l1", "loc_name": "Office",
            "unit": "units", "quantity": 5, "delta": None,
            "candidates": [("0", "A", {}), ("1", "B", {})], "ts": time.time(),
        })
        changed = td.freshest_changed_stash(before, HARRISON, _CH)
        assert changed == ("ask", aid)


class TestConcurrentTurnIsolation:
    """S1 fix (cq-883878e81274, HIGH): the exact live-smoke repro -- 3
    concurrent turns in the SAME (user, channel), each triggering a DIFFERENT
    staged-write kind, with overlapping snapshot/diff windows (multiple
    @mentions landing within a couple seconds). Simulated with real
    contextvars.Context objects (one per turn) rather than sequential
    begin_turn() calls on one thread, so the isolation is faithful to what
    actually happens across concurrent Bolt worker threads / claude_client's
    per-tool-call copied contexts -- not just "whichever begin_turn() ran
    last on a shared thread"."""

    def _turn_ctx(self):
        ctx = contextvars.copy_context()
        ctx.run(cc.begin_turn)
        return ctx

    def test_three_concurrent_kinds_each_bind_their_own_turn(self):
        # All 3 turns' "before" snapshots predate ALL 3 mints (the live-smoke
        # window: 3 previews land within ~2s of each other).
        ctx_a, ctx_b, ctx_c = self._turn_ctx(), self._turn_ctx(), self._turn_ctx()
        before_a = ctx_a.run(lambda: td.snapshot_stash_ids(HARRISON, _CH))
        before_b = ctx_b.run(lambda: td.snapshot_stash_ids(HARRISON, _CH))
        before_c = ctx_c.run(lambda: td.snapshot_stash_ids(HARRISON, _CH))

        # Turn A's own tool call: an Asana delete preview.
        sid_a = ctx_a.run(lambda: cc.mint_stash_id("asana", HARRISON, _CH))
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "delete", "gid": "gA", "label": "Turn A task",
            "ts": time.time(), "stash_id": sid_a,
        })
        # Turn B's own tool call: a calendar delete preview.
        sid_b = ctx_b.run(lambda: cc.mint_stash_id("calendar", HARRISON, _CH))
        td._store_pending_calendar_write(HARRISON, _CH, {
            "action": "delete", "event_id": "evtB", "summary": "Turn B event",
            "user_email": "h@x.com", "ts": time.time(), "stash_id": sid_b,
        })
        # Turn C's own tool call: a remember (personal note) preview.
        sid_c = ctx_c.run(lambda: cc.mint_stash_id("remember", HARRISON, _CH))
        td._store_pending_remember(HARRISON, _CH, {
            "note_text": "Turn C note", "entity": "F3E", "sub_entity": None,
            "share_requested": False, "channel_name": _CH, "ts": time.time(),
            "stash_id": sid_c,
        })

        # By now all 3 kinds have changed in the SHARED (user, channel)
        # store. Each turn's OWN diff must bind to ITS OWN mint only.
        changed_a = ctx_a.run(lambda: td.freshest_changed_stash(before_a, HARRISON, _CH))
        changed_b = ctx_b.run(lambda: td.freshest_changed_stash(before_b, HARRISON, _CH))
        changed_c = ctx_c.run(lambda: td.freshest_changed_stash(before_c, HARRISON, _CH))

        assert changed_a == ("asana", sid_a)
        assert changed_b == ("calendar", sid_b)
        assert changed_c == ("remember", sid_c)

    def test_tombstone_label_provenance_never_leaks_a_sibling_turns_kind(self):
        # End-to-end: mint -> bind (via freshest_changed_stash) -> resolve.
        # Each turn's bound stash_id, once cancelled, must produce a label
        # that reflects ITS OWN kind -- never a concurrent sibling's.
        ctx_a, ctx_b, ctx_c = self._turn_ctx(), self._turn_ctx(), self._turn_ctx()
        before_a = ctx_a.run(lambda: td.snapshot_stash_ids(HARRISON, _CH))
        before_b = ctx_b.run(lambda: td.snapshot_stash_ids(HARRISON, _CH))
        before_c = ctx_c.run(lambda: td.snapshot_stash_ids(HARRISON, _CH))

        sid_a = ctx_a.run(lambda: cc.mint_stash_id("asana", HARRISON, _CH))
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "delete", "gid": "gA", "label": "Asana task A",
            "ts": time.time(), "stash_id": sid_a,
        })
        sid_b = ctx_b.run(lambda: cc.mint_stash_id("calendar", HARRISON, _CH))
        td._store_pending_calendar_write(HARRISON, _CH, {
            "action": "delete", "event_id": "evtB", "summary": "Calendar event B",
            "user_email": "h@x.com", "ts": time.time(), "stash_id": sid_b,
        })
        sid_c = ctx_c.run(lambda: cc.mint_stash_id("remember", HARRISON, _CH))
        td._store_pending_remember(HARRISON, _CH, {
            "note_text": "Note C", "entity": "F3E", "sub_entity": None,
            "share_requested": False, "channel_name": _CH, "ts": time.time(),
            "stash_id": sid_c,
        })

        bound_a = ctx_a.run(lambda: td.freshest_changed_stash(before_a, HARRISON, _CH))
        bound_b = ctx_b.run(lambda: td.freshest_changed_stash(before_b, HARRISON, _CH))
        bound_c = ctx_c.run(lambda: td.freshest_changed_stash(before_c, HARRISON, _CH))

        result_a = td.resolve_and_claim_stash(bound_a[1], HARRISON, "cancel")
        result_b = td.resolve_and_claim_stash(bound_b[1], HARRISON, "cancel")
        result_c = td.resolve_and_claim_stash(bound_c[1], HARRISON, "cancel")

        # All 3 cancel cleanly (each claim is against its OWN, correctly-
        # bound stash_id) -- no cross-kind "superseded"/"already_handled".
        assert result_a == {"outcome": "cancelled"}
        assert result_b == {"outcome": "cancelled"}
        assert result_c == {"outcome": "cancelled"}
        # And each bound id really was its own turn's own kind (the pre-fix
        # bug: a max-ts tiebreak could hand ALL THREE turns the SAME id).
        assert {bound_a[0], bound_b[0], bound_c[0]} == {"asana", "calendar", "remember"}
        assert len({bound_a[1], bound_b[1], bound_c[1]}) == 3

    def test_no_active_turn_context_fails_closed_to_no_card(self):
        # A change with NO turn context at all (turn_id=None, e.g. a stash
        # minted outside of _dispatch_qa's begin_turn scope) must never be
        # guessed as "this turn's own" -- fail closed to no card.
        before = td.snapshot_stash_ids(HARRISON, _CH)
        _stash_asana_delete()  # minted with no begin_turn() call at all
        assert td.freshest_changed_stash(before, HARRISON, _CH) is None

    def test_sibling_turns_mint_never_reattaches_to_a_turn_with_no_mint_of_its_own(self):
        # Turn A takes its snapshot, then does nothing itself; turn B (a
        # concurrent, unrelated ask in the same channel) mints a fresh
        # pending. Turn A's own reply must NOT get a card for turn B's stash.
        ctx_a, ctx_b = self._turn_ctx(), self._turn_ctx()
        before_a = ctx_a.run(lambda: td.snapshot_stash_ids(HARRISON, _CH))
        ctx_b.run(lambda: td.snapshot_stash_ids(HARRISON, _CH))
        sid_b = ctx_b.run(lambda: cc.mint_stash_id("asana", HARRISON, _CH))
        td._store_pending_asana_write(HARRISON, _CH, {
            "action": "delete", "gid": "gB", "label": "Turn B task",
            "ts": time.time(), "stash_id": sid_b,
        })
        assert ctx_a.run(lambda: td.freshest_changed_stash(before_a, HARRISON, _CH)) is None


class TestHasPendingRememberForgetNote:
    """S4 fix (cq-08166dcf283d): has_pending_remember/has_pending_forget_note
    close the CONFIRM-turn escalation gap -- these two kinds previously had no
    wrapper at all, so a pending remember/forget's bare-"yes" follow-up could
    never force Sonnet the way Asana/Shopify/calendar/delegated already do."""

    def test_has_pending_remember_false_when_nothing_stashed(self):
        assert td.has_pending_remember(HARRISON, _CH) is False

    def test_has_pending_remember_true_after_stash(self):
        _stash_remember()
        assert td.has_pending_remember(HARRISON, _CH) is True

    def test_has_pending_remember_isolated_by_user_and_channel(self):
        _stash_remember(user=HARRISON, channel=_CH)
        assert td.has_pending_remember(OTHER, _CH) is False
        assert td.has_pending_remember(HARRISON, "different-channel") is False

    def test_has_pending_forget_note_false_when_nothing_stashed(self):
        assert td.has_pending_forget_note(HARRISON, _CH) is False

    def test_has_pending_forget_note_true_after_stash(self):
        sid = cc.mint_stash_id("forget_note", HARRISON, _CH)
        td._store_pending_forget_note(HARRISON, _CH, {
            "note_id": "note:1", "ts": time.time(), "stash_id": sid,
        })
        assert td.has_pending_forget_note(HARRISON, _CH) is True


class TestDispatchPropagatesTurnContext:
    """D-051 re-review finding (CRITICAL): td.dispatch() has its OWN internal
    per-tool timeout executor (tool_dispatch.py, inside dispatch()) -- a
    SEPARATE thread-hop from claude_client._dispatch_tools_parallel's outer
    one. It was ALSO doing a bare executor.submit(fn, ...) with no context
    copy, so EVERY real tool call (not just concurrent ones -- the single-
    tool path goes through this exact executor too) minted a stash with
    turn_id=None, and freshest_changed_stash's turn-ownership filter NEVER
    matched -- NO confirm card would EVER attach, for ANY reply. Two
    independent review agents found this by static reading; this test drives
    the REAL, unmocked dispatch() end-to-end (not a patched stand-in the way
    test_parallel_tool_dispatch.py's S1 tests do) to prove the fix holds at
    the exact layer where it was broken."""

    def test_real_tool_call_through_dispatch_sees_the_turn_id(self, monkeypatch):
        def _probe_tool(slack_user_id, _entity, _input):
            return cc.mint_stash_id("asana", slack_user_id, _input.get("_channel_name", ""))

        monkeypatch.setitem(td._TOOL_FUNCTIONS, "_test_probe_tool", _probe_tool)
        cc.begin_turn()
        tid = cc.current_turn_id()
        returned_sid = td.dispatch("_test_probe_tool", {}, HARRISON, "FNDR", _CH, "C1", None)
        entry = cc.index_lookup(returned_sid)
        assert entry is not None
        assert entry["turn_id"] == tid

    def test_freshest_changed_stash_sees_a_mint_from_inside_a_real_dispatch_call(self, monkeypatch):
        # The end-to-end version of the bug: mint via the REAL dispatch()
        # path (not a direct cc.mint_stash_id call), then confirm the SAME
        # snapshot/diff mechanism app.py uses actually attaches it.
        def _probe_tool(slack_user_id, _entity, _input):
            sid = cc.mint_stash_id("asana", slack_user_id, _input.get("_channel_name", ""))
            td._store_pending_asana_write(slack_user_id, _CH, {
                "action": "delete", "gid": "gProbe", "label": "Probe task",
                "ts": time.time(), "stash_id": sid,
            })
            return "preview text"

        monkeypatch.setitem(td._TOOL_FUNCTIONS, "_test_probe_tool", _probe_tool)
        cc.begin_turn()
        before = td.snapshot_stash_ids(HARRISON, _CH)
        td.dispatch("_test_probe_tool", {}, HARRISON, "FNDR", _CH, "C1", None)
        changed = td.freshest_changed_stash(before, HARRISON, _CH)
        assert changed is not None
        assert changed[0] == "asana"
