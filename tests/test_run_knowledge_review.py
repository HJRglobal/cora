"""Tests for run_knowledge_review._auto_dismiss_stale_pending (D1 fix, 2026-06-13).

A PENDING entry is auto-dismissed ONLY once it has been DM'd to Harrison
(dm_message_ts set) and left unreacted past 48h. A never-DM'd entry must survive
(Step 2 DMs it this run) so an #info-for-cora note posted right before a >48h gap
(Friday evening -> Monday 7am review) is not silently dropped before he sees it.
"""

from datetime import datetime, timedelta, timezone

import scripts.run_knowledge_review as rkr


def _now():
    return datetime.now(timezone.utc)


def _entry(dm_ts="", age_hours=100, state="PENDING"):
    return {
        "update_id": "x",
        "state": state,
        "dm_message_ts": dm_ts,
        "proposed_at": (_now() - timedelta(hours=age_hours)).isoformat(),
        "resolved_at": None,
    }


def test_dmd_and_stale_is_dismissed():
    now = _now()
    e = _entry(dm_ts="1700.1", age_hours=100)
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(hours=48), now) == 1
    assert e["state"] == "DISMISSED" and e["resolved_at"]


def test_never_dmd_is_not_dismissed():
    now = _now()
    e = _entry(dm_ts="", age_hours=100)  # never shown to Harrison
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(hours=48), now) == 0
    assert e["state"] == "PENDING"


def test_dmd_but_recent_is_not_dismissed():
    now = _now()
    e = _entry(dm_ts="1700.1", age_hours=10)
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(hours=48), now) == 0
    assert e["state"] == "PENDING"


def test_non_pending_untouched():
    now = _now()
    e = _entry(dm_ts="1700.1", age_hours=100, state="APPROVED")
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(hours=48), now) == 0
    assert e["state"] == "APPROVED"


def test_bad_proposed_at_ignored():
    now = _now()
    e = {"state": "PENDING", "dm_message_ts": "1.1", "proposed_at": "not-a-date", "resolved_at": None}
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(hours=48), now) == 0
    assert e["state"] == "PENDING"


# ── Single-instance run lock (audit N2: triple-post race guard) ──────────────

def test_run_lock_acquire_then_block(tmp_path, monkeypatch):
    import logging
    monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "knowledge-review.lock")
    log = logging.getLogger("test")
    assert rkr._acquire_run_lock(log) is True          # first run takes it
    assert (tmp_path / "knowledge-review.lock").exists()
    assert rkr._acquire_run_lock(log) is False         # concurrent run is blocked
    rkr._release_run_lock()                            # release frees it
    assert not (tmp_path / "knowledge-review.lock").exists()
    assert rkr._acquire_run_lock(log) is True          # next run can take it again
    rkr._release_run_lock()


def test_run_lock_stale_is_reclaimed(tmp_path, monkeypatch):
    import logging
    import os as _os
    import time as _time
    lock = tmp_path / "knowledge-review.lock"
    monkeypatch.setattr(rkr, "_LOCK_PATH", lock)
    monkeypatch.setattr(rkr, "_LOCK_STALE_SECONDS", 1)
    log = logging.getLogger("test")
    assert rkr._acquire_run_lock(log) is True
    old = _time.time() - 10                            # age the lock past stale window
    _os.utime(lock, (old, old))
    assert rkr._acquire_run_lock(log) is True          # stale lock cleared + reacquired
    rkr._release_run_lock()


# ── Phase 2.4 rebuild: auto-expire reason, auto-approve gate, weekly digest ──

def test_dismissed_entry_records_reason():
    now = _now()
    e = _entry(dm_ts="1700.1", age_hours=400)
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(days=14), now) == 1
    assert e["resolved_reason"] == "auto_expired_dmd_unreacted"


def test_is_digest_day_deterministic(monkeypatch):
    """Fixed AZ (-7) offset, robust without tzdata. 2026-06-15 is a Monday."""
    import datetime as _dt

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            return _dt.datetime(2026, 6, 15, 12, 0, tzinfo=tz)  # Monday, AZ noon

    monkeypatch.setattr(rkr, "datetime", _FakeDatetime)
    monkeypatch.setattr(rkr, "_DIGEST_WEEKDAY", 0)  # Monday
    assert rkr._is_digest_day() is True
    monkeypatch.setattr(rkr, "_DIGEST_WEEKDAY", 2)  # Wednesday
    assert rkr._is_digest_day() is False


def test_high_known_answer_requires_thumbs_up(tmp_path, monkeypatch):
    """WS17-C: the silent auto-approve is RETIRED. A HIGH-confidence known_answer
    with NO Harrison reaction must (a) stay PENDING, (b) NOT be written to
    known-answers, (c) NOT resolve its gap, and (d) still be DM'd to Harrison so
    he can 👍 it. (Inverts the pre-WS17-C auto-approve roundtrip.)"""
    import importlib
    kr = importlib.import_module("cora.knowledge_review")

    proposed = tmp_path / "proposed.jsonl"
    reply_log = tmp_path / "reply.jsonl"
    ka_dir = tmp_path / "known-answers"
    resolved = tmp_path / "resolved.jsonl"

    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", proposed)
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", reply_log)
    monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
    monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(ka_dir))
    monkeypatch.setenv("RESOLVED_GAPS_PATH", str(resolved))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-dummy")  # enable the Step-2 DM path
    # Keep the WS17-C enrichment off the network/KB in this unit test.
    monkeypatch.setattr(rkr, "_attach_coras_read", lambda items, log: None, raising=False)

    # Capture the DM instead of hitting Slack; record which items were "sent".
    sent: dict[str, str] = {}

    def _fake_individual(updates, token, _client_factory=None):
        for u in updates:
            sent[u["update_id"]] = "111.1"
        return dict(sent)

    monkeypatch.setattr(rkr, "send_individual_dms", _fake_individual)
    monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: None)

    kr.propose_update(
        update_id="ka-1",
        update_type="known_answer",
        description="F3E Anaheim warehouse address",
        payload={
            "entity": "FNDR",
            "question": "What's the F3E Anaheim warehouse address?",
            "answer": "1234 Example St, Anaheim CA.",
            "gap_ts": "g-1",
        },
        confidence="HIGH",
    )

    monkeypatch.setattr("sys.argv", ["run_knowledge_review.py"])
    rkr.main()

    # (a) stays PENDING -- no reaction means no resolution.
    entries = [e for e in kr.load_proposed_updates() if e["update_id"] == "ka-1"]
    assert entries and entries[0]["state"] == "PENDING"
    # (b) NOT written to known-answers (no ungated write).
    assert not (ka_dir / "fndr.md").exists()
    # (c) gap NOT resolved.
    assert not resolved.exists()
    # (d) it WAS DM'd to Harrison for his 👍.
    assert sent.get("ka-1") == "111.1"


def test_no_auto_approve_symbols_remain():
    """WS17-C: the auto-approve machinery is fully removed (no dangling refs)."""
    for name in (
        "_auto_approve_eligible", "_autoapprove_floor",
        "_AUTO_APPROVE_TYPES", "_MAX_AUTO_APPROVE_PER_RUN", "_AUTOAPPROVE_FLOOR_PATH",
    ):
        assert not hasattr(rkr, name), f"{name} should be gone after WS17-C"


# == WS17-B items 3 + 4: knowledge/operational split + owner routing ==========

def test_is_knowledge_item_classification():
    assert rkr._is_knowledge_item({"update_type": "known_answer"}) is True
    assert rkr._is_knowledge_item({"update_type": "efficiency"}) is True
    assert rkr._is_knowledge_item(
        {"update_type": "generic", "payload": {"source": "info-for-cora"}}) is True
    # Operational nudges are NOT knowledge:
    assert rkr._is_knowledge_item({"update_type": "hubspot_note"}) is False
    assert rkr._is_knowledge_item({"update_type": "asana_task"}) is False
    assert rkr._is_knowledge_item({"update_type": "decision_capture"}) is False
    assert rkr._is_knowledge_item({"update_type": "task_close"}) is False
    # A drive-extractor generic (no info-for-cora source) is operational:
    assert rkr._is_knowledge_item({"update_type": "generic", "payload": {}}) is False


def test_routing_floor_inits_and_is_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(rkr, "_ROUTING_FLOOR_PATH", tmp_path / "rfloor.txt")
    f = rkr._routing_floor()
    assert f and (tmp_path / "rfloor.txt").exists()
    assert rkr._routing_floor() == f  # stable


def _op(uid, utype, entity, proposed="2026-06-01T00:00:00+00:00", confidence="MED"):
    return {"update_id": uid, "update_type": utype, "confidence": confidence,
            "state": "PENDING", "proposed_at": proposed,
            "payload": {"entity": entity}, "description": utype + " " + uid}


def test_route_operational_to_owners(tmp_path, monkeypatch):
    import logging
    from unittest.mock import MagicMock
    floor = tmp_path / "rfloor.txt"
    floor.write_text("2000-01-01T00:00:00+00:00", encoding="utf-8")  # old -> all eligible
    monkeypatch.setattr(rkr, "_ROUTING_FLOOR_PATH", floor)

    sent = MagicMock(return_value="ts-1")
    resolved = MagicMock(return_value=True)
    monkeypatch.setattr(rkr, "_send_dm_to_user", sent)
    monkeypatch.setattr(rkr, "resolve_update", resolved)

    items = [
        _op("op1", "hubspot_note", "F3E"),       # -> Tommy
        _op("op2", "task_close", "FNDR"),        # -> Harrison
        _op("lex1", "asana_task", "LEX-LLC"),    # PHI -> never routed
        _op("old", "hubspot_note", "F3E", proposed="1999-01-01T00:00:00+00:00"),  # below floor
    ]
    n = rkr._route_operational_to_owners(items, "xoxb-test", logging.getLogger("t"))
    assert n == 2
    routed_ids = {c.args[0] for c in resolved.call_args_list}
    assert routed_ids == {"op1", "op2"}
    for c in resolved.call_args_list:
        assert c.args[1] == "DISMISSED"
        assert c.kwargs["reason"].startswith("routed_to_owner:")
    assert "lex1" not in routed_ids and "old" not in routed_ids


def test_route_per_owner_cap(tmp_path, monkeypatch):
    import logging
    from unittest.mock import MagicMock
    floor = tmp_path / "rfloor.txt"
    floor.write_text("2000-01-01T00:00:00+00:00", encoding="utf-8")
    monkeypatch.setattr(rkr, "_ROUTING_FLOOR_PATH", floor)
    monkeypatch.setattr(rkr, "_MAX_OWNER_DMS_PER_OWNER", 2)
    monkeypatch.setattr(rkr, "_send_dm_to_user", MagicMock(return_value="ts"))
    monkeypatch.setattr(rkr, "resolve_update", MagicMock(return_value=True))
    items = [_op("f" + str(i), "hubspot_note", "F3E") for i in range(5)]  # all -> Tommy
    n = rkr._route_operational_to_owners(items, "xoxb-test", logging.getLogger("t"))
    assert n == 2  # per-owner cap


def test_route_failed_dm_leaves_pending(tmp_path, monkeypatch):
    import logging
    from unittest.mock import MagicMock
    floor = tmp_path / "rfloor.txt"
    floor.write_text("2000-01-01T00:00:00+00:00", encoding="utf-8")
    monkeypatch.setattr(rkr, "_ROUTING_FLOOR_PATH", floor)
    monkeypatch.setattr(rkr, "_send_dm_to_user", MagicMock(return_value=None))  # DM fails
    resolved = MagicMock(return_value=True)
    monkeypatch.setattr(rkr, "resolve_update", resolved)
    n = rkr._route_operational_to_owners([_op("op1", "hubspot_note", "F3E")],
                                         "xoxb-test", logging.getLogger("t"))
    assert n == 0
    resolved.assert_not_called()  # not marked resolved -> retried next run


def test_route_nothing_without_token():
    import logging
    assert rkr._route_operational_to_owners([_op("op1", "hubspot_note", "F3E")],
                                            "", logging.getLogger("t")) == 0


# == Rider D: D3 (no-action line on top) + D4 (batch one DM per owner) =========

def test_owner_batch_dm_leads_with_no_action_and_keeps_links():
    """D3: the FYI/no-action line LEADS the card (was previously the last line).
    D4: many items render in ONE body; per-item links preserved."""
    items = [
        {"update_id": "a", "update_type": "hubspot_note", "description": "note one",
         "payload": {"entity": "F3E", "deal_url": "https://hub/deal/1"}},
        {"update_id": "b", "update_type": "asana_task", "description": "task two",
         "payload": {"entity": "F3E", "task_url": "https://asana/task/2"}},
    ]
    body = rkr._format_owner_batch_dm(items)
    first_line = body.splitlines()[0]
    assert "no action needed" in first_line.lower()  # D3: leads, not trails
    assert "2 suggestions" in first_line               # D4: batch count
    # both items + their links are present in the single body
    assert "note one" in body and "task two" in body
    assert "https://hub/deal/1" in body and "https://asana/task/2" in body
    # numbered lines for legibility
    assert "1. " in body and "2. " in body


def test_route_batches_one_dm_per_owner(tmp_path, monkeypatch):
    """D4: three items for the SAME owner produce exactly ONE DM (not three),
    and all three are still marked routed/DISMISSED."""
    import logging
    from unittest.mock import MagicMock
    floor = tmp_path / "rfloor.txt"
    floor.write_text("2000-01-01T00:00:00+00:00", encoding="utf-8")
    monkeypatch.setattr(rkr, "_ROUTING_FLOOR_PATH", floor)
    sent = MagicMock(return_value="ts-1")
    resolved = MagicMock(return_value=True)
    monkeypatch.setattr(rkr, "_send_dm_to_user", sent)
    monkeypatch.setattr(rkr, "resolve_update", resolved)

    items = [_op("f1", "hubspot_note", "F3E"),
             _op("f2", "asana_task", "F3E"),
             _op("f3", "task_close", "F3E")]  # all -> Tommy
    n = rkr._route_operational_to_owners(items, "xoxb-test", logging.getLogger("t"))

    assert n == 3
    assert sent.call_count == 1  # ONE batched DM, not three
    body = sent.call_args.args[1]
    assert "3 suggestions" in body and body.splitlines()[0].lower().startswith(":information_source:")
    assert {c.args[0] for c in resolved.call_args_list} == {"f1", "f2", "f3"}


def test_route_two_owners_two_dms(tmp_path, monkeypatch):
    """D4: distinct owners each get their own single batched DM."""
    import logging
    from unittest.mock import MagicMock
    floor = tmp_path / "rfloor.txt"
    floor.write_text("2000-01-01T00:00:00+00:00", encoding="utf-8")
    monkeypatch.setattr(rkr, "_ROUTING_FLOOR_PATH", floor)
    sent = MagicMock(return_value="ts")
    monkeypatch.setattr(rkr, "_send_dm_to_user", sent)
    monkeypatch.setattr(rkr, "resolve_update", MagicMock(return_value=True))
    items = [_op("f1", "hubspot_note", "F3E"),  # -> Tommy
             _op("d1", "task_close", "FNDR")]   # -> Harrison
    n = rkr._route_operational_to_owners(items, "xoxb-test", logging.getLogger("t"))
    assert n == 2 and sent.call_count == 2  # one DM per distinct owner


# == Rider D: D2 (acknowledge every processed emoji reaction) ==================

def test_ack_reaction_text():
    t = rkr._ack_reaction_text
    assert "known-answers" in t("APPROVED", "known_answer").lower()
    assert "backlog" in t("APPROVED", "efficiency").lower()
    assert "recorded" in t("APPROVED", "asana_task").lower()
    assert "dismissed" in t("DISMISSED", "known_answer").lower()
    assert t("OTHER", "known_answer") == ""  # non-actionable -> no ack


def test_ack_correlated_reaction_threads_reply_and_reacts():
    from unittest.mock import MagicMock
    import logging
    client = MagicMock()
    reaction = {"action": "APPROVED", "channel_id": "D1", "message_ts": "111.2"}
    rkr._ack_correlated_reaction(
        reaction, "APPROVED", {"update_type": "known_answer"},
        "xoxb-test", logging.getLogger("t"), _client_factory=lambda: client)
    # threaded one-liner on the original card
    kw = client.chat_postMessage.call_args.kwargs
    assert kw["channel"] == "D1" and kw["thread_ts"] == "111.2"
    assert "known-answers" in kw["text"].lower()
    # glanceable check reaction for an approval
    assert client.reactions_add.call_args.kwargs["name"] == "white_check_mark"


def test_ack_correlated_reaction_dismiss_no_reaction_add():
    from unittest.mock import MagicMock
    import logging
    client = MagicMock()
    reaction = {"action": "DISMISSED", "channel_id": "D1", "message_ts": "111.2"}
    rkr._ack_correlated_reaction(
        reaction, "DISMISSED", {"update_type": "known_answer"},
        "xoxb-test", logging.getLogger("t"), _client_factory=lambda: client)
    assert client.chat_postMessage.called          # still threads the ack
    client.reactions_add.assert_not_called()        # no check-mark on a dismiss


def test_ack_correlated_reaction_noop_without_anchor():
    from unittest.mock import MagicMock
    import logging
    client = MagicMock()
    # missing channel/ts -> nothing to anchor to -> no Slack call
    rkr._ack_correlated_reaction(
        {"action": "APPROVED"}, "APPROVED", {"update_type": "known_answer"},
        "xoxb-test", logging.getLogger("t"), _client_factory=lambda: client)
    client.chat_postMessage.assert_not_called()


def test_ack_correlated_reaction_failsoft():
    import logging
    def _boom():
        raise RuntimeError("slack down")
    # a client build/post failure must not raise
    rkr._ack_correlated_reaction(
        {"action": "APPROVED", "channel_id": "D1", "message_ts": "1.2"},
        "APPROVED", {"update_type": "known_answer"},
        "xoxb-test", logging.getLogger("t"), _client_factory=_boom)


def test_correlated_reaction_is_acked_in_main(tmp_path, monkeypatch):
    """D2 wiring: a processed DISMISSED reaction triggers an ack in main()."""
    import importlib
    from unittest.mock import MagicMock
    kr = importlib.import_module("cora.knowledge_review")

    (tmp_path / "proposed.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "reply.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "proposed.jsonl")
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply.jsonl")
    kr._SEEN_IDS_CACHE = None
    kr._ARCHIVE_IDS_CACHE = None
    monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
    monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(rkr, "_attach_coras_read", lambda items, log: None)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("CORA_AUTOWRITE_LIVE", "off")

    update = {"update_id": "kx", "update_type": "known_answer", "state": "PENDING"}
    reaction = {"action": "DISMISSED", "channel_id": "D1", "message_ts": "111.222"}
    monkeypatch.setattr(rkr, "correlate_reactions_to_updates", lambda: [(update, reaction)])
    monkeypatch.setattr(rkr, "resolve_update", MagicMock())
    ack = MagicMock()
    monkeypatch.setattr(rkr, "_ack_correlated_reaction", ack)
    monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: "hdr")
    monkeypatch.setattr(rkr, "send_individual_dms", lambda *a, **k: {})
    monkeypatch.setattr(rkr, "_route_operational_to_owners", lambda *a, **k: 0)

    monkeypatch.setattr("sys.argv", ["run_knowledge_review.py"])
    rkr.main()

    ack.assert_called_once()
    assert ack.call_args.args[0] is reaction and ack.call_args.args[1] == "DISMISSED"


# == Rider D D-051 remediation: ack never shows a false "Saved" on a failed apply =

def test_ack_reaction_text_failed_apply_is_not_saved():
    """D-051 (MEDIUM): a failed durable apply must NOT be acked as Saved."""
    msg = rkr._ack_reaction_text("APPROVED", "known_answer", success=False)
    assert "saved" not in msg.lower()
    assert "didn't go through" in msg.lower() and "hjrg-leadership" in msg.lower()
    # success path unchanged
    assert "saved" in rkr._ack_reaction_text("APPROVED", "known_answer", success=True).lower()


def test_ack_correlated_reaction_failed_apply_no_checkmark():
    """D-051 (MEDIUM): on a failed apply, the threaded ack is the honest warning
    and NO white_check_mark reaction is added (that would read as success)."""
    from unittest.mock import MagicMock
    import logging
    client = MagicMock()
    reaction = {"action": "APPROVED", "channel_id": "D1", "message_ts": "1.2"}
    rkr._ack_correlated_reaction(
        reaction, "APPROVED", {"update_type": "known_answer"},
        "xoxb-test", logging.getLogger("t"), _client_factory=lambda: client, success=False)
    assert client.chat_postMessage.called
    assert "saved" not in client.chat_postMessage.call_args.kwargs["text"].lower()
    client.reactions_add.assert_not_called()


def test_correlated_approved_failed_apply_acks_failure(tmp_path, monkeypatch):
    """D-051 (MEDIUM) wiring: a correlated APPROVED whose _execute_approved_update
    returns False is acked with success=False (never a false 'Saved')."""
    import importlib
    from unittest.mock import MagicMock
    kr = importlib.import_module("cora.knowledge_review")
    (tmp_path / "proposed.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "reply.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "proposed.jsonl")
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply.jsonl")
    kr._SEEN_IDS_CACHE = None
    kr._ARCHIVE_IDS_CACHE = None
    monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
    monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(rkr, "_attach_coras_read", lambda items, log: None)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("CORA_AUTOWRITE_LIVE", "off")

    update = {"update_id": "kx", "update_type": "known_answer", "state": "PENDING",
              "description": "a fact"}
    reaction = {"action": "APPROVED", "channel_id": "D1", "message_ts": "9.9"}
    monkeypatch.setattr(rkr, "correlate_reactions_to_updates", lambda: [(update, reaction)])
    monkeypatch.setattr(rkr, "resolve_update", MagicMock())
    monkeypatch.setattr(rkr, "_execute_approved_update", MagicMock(return_value=False))
    ack = MagicMock()
    monkeypatch.setattr(rkr, "_ack_correlated_reaction", ack)
    monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: "hdr")
    monkeypatch.setattr(rkr, "send_individual_dms", lambda *a, **k: {})
    monkeypatch.setattr(rkr, "_route_operational_to_owners", lambda *a, **k: 0)

    monkeypatch.setattr("sys.argv", ["run_knowledge_review.py"])
    rkr.main()

    ack.assert_called_once()
    assert ack.call_args.args[1] == "APPROVED"
    assert ack.call_args.kwargs.get("success") is False


def test_execute_approved_update_returns_success_bool(tmp_path, monkeypatch):
    """The D2 ack depends on this: advisory post -> True; failed durable apply -> False."""
    import logging
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
    # advisory generic post (no durable write) -> True
    ok = rkr._execute_approved_update(
        {"update_id": "g1", "update_type": "generic", "description": "d", "payload": {}},
        "", logging.getLogger("t"))
    assert ok is True
    # known_answer whose apply fails (empty answer) -> False
    bad = rkr._execute_approved_update(
        {"update_id": "k1", "update_type": "known_answer", "description": "d",
         "payload": {"entity": "FNDR", "question": "q", "answer": ""}},
        "", logging.getLogger("t"))
    assert bad is False


def test_route_partial_failure_defers_conservatively(tmp_path, monkeypatch):
    """D-051 (LOW, documented): under a partial DM failure the per-run cap counts
    SELECTED items, so a failed owner's slots can defer another owner's items one
    run -- strictly conservative (nothing wrongly dismissed; deferred stays PENDING).
    Pins the accepted per-owner-batching trade-off."""
    import logging
    from unittest.mock import MagicMock
    floor = tmp_path / "rfloor.txt"
    floor.write_text("2000-01-01T00:00:00+00:00", encoding="utf-8")
    monkeypatch.setattr(rkr, "_ROUTING_FLOOR_PATH", floor)
    monkeypatch.setattr("cora.gap_autofill.resolve_owner",
                        lambda e: {"EA": "UA", "EB": "UB", "EC": "UC"}.get((e or "").strip().upper()))
    sent = MagicMock(side_effect=lambda user, text, token, cf=None: None if user == "UA" else "ts")
    monkeypatch.setattr(rkr, "_send_dm_to_user", sent)
    resolved = MagicMock(return_value=True)
    monkeypatch.setattr(rkr, "resolve_update", resolved)

    items = []
    for i in range(5):  # owner UA, earliest -> selected first, DM fails
        items.append(_op(f"a{i}", "hubspot_note", "EA", proposed=f"2026-06-01T00:0{i}:00+00:00"))
    for i in range(5):  # owner UB -> fills the per-run cap of 10, DM succeeds
        items.append(_op(f"b{i}", "hubspot_note", "EB", proposed=f"2026-06-02T00:0{i}:00+00:00"))
    for i in range(2):  # owner UC -> deferred (cap consumed by A+B selection)
        items.append(_op(f"c{i}", "hubspot_note", "EC", proposed=f"2026-06-03T00:0{i}:00+00:00"))

    n = rkr._route_operational_to_owners(items, "xoxb-test", logging.getLogger("t"))
    resolved_ids = {c.args[0] for c in resolved.call_args_list}
    assert n == 5                                    # only owner B delivered
    assert resolved_ids == {f"b{i}" for i in range(5)}
    assert not any(u.startswith("a") for u in resolved_ids)  # A: DM failed -> PENDING
    assert not any(u.startswith("c") for u in resolved_ids)  # C: deferred -> PENDING (conservative)


def test_knowledge_dmd_every_run_not_just_monday(tmp_path, monkeypatch):
    """Item 4: a MED known_answer DMs Harrison on a NON-digest day (no Monday gate)."""
    import importlib
    from unittest.mock import MagicMock
    kr = importlib.import_module("cora.knowledge_review")

    proposed = tmp_path / "proposed.jsonl"
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", proposed)
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply.jsonl")
    kr._SEEN_IDS_CACHE = None
    monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
    monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(rkr, "_is_digest_day", lambda: False)  # NOT Monday
    monkeypatch.setattr(rkr, "_attach_coras_read", lambda items, log: None)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    header = MagicMock(return_value="hdr")
    individual = MagicMock(return_value={"ka-med": "ts1"})
    route = MagicMock(return_value=0)
    monkeypatch.setattr(rkr, "send_dm_to_harrison", header)
    monkeypatch.setattr(rkr, "send_individual_dms", individual)
    monkeypatch.setattr(rkr, "_route_operational_to_owners", route)

    kr.propose_update(update_id="ka-med", update_type="known_answer",
                      description="a med fact", payload={"entity": "FNDR"}, confidence="MED")

    monkeypatch.setattr("sys.argv", ["run_knowledge_review.py"])
    rkr.main()

    individual.assert_called_once()  # DM'd despite non-Monday (item 4)
    entries = [e for e in kr.load_proposed_updates() if e["update_id"] == "ka-med"]
    assert entries and entries[0]["dm_message_ts"] == "ts1"


def test_operational_routed_not_dmd_to_harrison(tmp_path, monkeypatch):
    """Item 3: an operational nudge is routed to its owner, NOT DM'd to Harrison."""
    import importlib
    from unittest.mock import MagicMock
    kr = importlib.import_module("cora.knowledge_review")

    proposed = tmp_path / "proposed.jsonl"
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", proposed)
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply.jsonl")
    kr._SEEN_IDS_CACHE = None
    monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
    monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
    floor = tmp_path / "rfloor.txt"
    floor.write_text("2000-01-01T00:00:00+00:00", encoding="utf-8")
    monkeypatch.setattr(rkr, "_ROUTING_FLOOR_PATH", floor)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    individual = MagicMock(return_value={})
    header = MagicMock(return_value="hdr")
    sent = MagicMock(return_value="ts-owner")
    monkeypatch.setattr(rkr, "send_individual_dms", individual)
    monkeypatch.setattr(rkr, "send_dm_to_harrison", header)
    monkeypatch.setattr(rkr, "_send_dm_to_user", sent)

    kr.propose_update(update_id="hn-1", update_type="hubspot_note",
                      description="deal X no activity", payload={"entity": "F3E"},
                      confidence="MED")

    monkeypatch.setattr("sys.argv", ["run_knowledge_review.py"])
    rkr.main()

    sent.assert_called_once()       # routed to the F3E owner
    individual.assert_not_called()  # NOT in Harrison's knowledge DM batch
    entries = [e for e in kr.load_proposed_updates() if e["update_id"] == "hn-1"]
    assert entries and entries[0]["state"] == "DISMISSED"
    assert entries[0]["resolved_reason"].startswith("routed_to_owner:")


# == WS17-B item 5: _execute_approved_update routes info-for-cora -> known-answers

def test_execute_approved_info_for_cora_writes_known_answers(tmp_path, monkeypatch):
    import logging
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
    update = {
        "update_id": "infocora-1", "update_type": "generic", "description": "d",
        "payload": {"source": "info-for-cora", "entity": "FNDR",
                    "text": "A founder-level fact worth keeping.", "author_name": "Harrison"},
    }
    rkr._execute_approved_update(update, "", logging.getLogger("t"))  # empty token -> Slack no-ops
    assert "A founder-level fact worth keeping." in (tmp_path / "fndr.md").read_text(encoding="utf-8")


def test_execute_approved_drive_generic_does_not_write_known_answers(tmp_path, monkeypatch):
    import logging
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
    update = {
        "update_id": "drive_fact:1", "update_type": "generic", "description": "Person: X",
        "payload": {"entity": "FNDR", "subject": "X"},  # no info-for-cora source
    }
    rkr._execute_approved_update(update, "", logging.getLogger("t"))
    assert not (tmp_path / "fndr.md").exists()  # operational generic only posts; no KB write


# == Graduated-trust SHADOW: acts on nothing, byte-identical DM/approve path =====

def test_format_single_item_dm_ignores_shadow_fields():
    """The DM render must be byte-identical whether or not the shadow pass stashed
    its fields on the item dict (the shadow verdict/tier are never shown)."""
    import cora.knowledge_review as kr
    base = {"update_type": "known_answer", "confidence": "HIGH", "description": "A fact",
            "payload": {}, "_coras_read": "🧠 *Cora's read:* ✅ CORROBORATED: ok"}
    baseline = kr.format_single_item_dm(dict(base))
    enriched = dict(base)
    enriched.update({"_coras_read_verdict": "CORROBORATED", "shadow_tier": 0,
                     "shadow_decision": "would-auto-approve"})
    assert kr.format_single_item_dm(enriched) == baseline


def test_shadow_acts_on_nothing_byte_identical(tmp_path, monkeypatch):
    """End-to-end: run the drain with shadow ON vs OFF over identical inputs.

    Asserts (a) the rendered DM text is byte-identical, (b) the ledger states are
    identical, (c) a would-auto-approve (Tier-0) item still stays PENDING and is
    still DM'd to Harrison (shadow acted on nothing), and (d) shadow ON wrote a
    shadow log while shadow OFF wrote none.
    """
    import json as _json
    from types import SimpleNamespace
    import cora.knowledge_review as kr

    # Fake org so the proposed item classifies as a would-auto-approve (Tier 0) --
    # the strongest "acts on nothing" case: it WOULD auto-approve, yet stays gated.
    monkeypatch.setattr(
        "cora.org_roles.get_role",
        lambda sid: SimpleNamespace(external=False, all_entities=["F3E"])
        if sid == "U-TOMMY" else None)
    monkeypatch.setattr(
        "cora.gap_autofill.resolve_owner",
        lambda e: "U-TOMMY" if (e or "").strip().upper() == "F3E" else None)

    def _fake_attach(items, log):
        for it in items:
            it["_coras_read"] = "🧠 *Cora's read:* ✅ CORROBORATED: ok"
            it["_coras_read_verdict"] = "CORROBORATED"

    def run_once(tag, shadow_on):
        proposed = tmp_path / f"proposed-{tag}.jsonl"
        monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", proposed)
        monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / f"reply-{tag}.jsonl")
        kr._SEEN_IDS_CACHE = None
        kr._SEEN_IDS_KEY = None
        kr._ARCHIVE_IDS_CACHE = None
        kr._ARCHIVE_IDS_KEY = None
        logs = tmp_path / f"logs-{tag}"
        monkeypatch.setattr(rkr, "LOG_DIR", logs)
        monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / f"kr-{tag}.lock")
        monkeypatch.setattr(rkr, "_ROUTING_FLOOR_PATH", tmp_path / f"floor-{tag}.txt")
        monkeypatch.setattr(rkr, "_attach_coras_read", _fake_attach)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("CORA_GRADUATED_SHADOW", "1" if shadow_on else "0")
        # Pin the graduated-trust auto-write flip OFF: this test isolates the
        # SHADOW (observe-only) layer, which is only meaningful when nothing
        # auto-writes. Without this pin the test inherits the operator's live
        # .env CORA_AUTOWRITE_LIVE=all flip, which auto-writes this Tier-0 item
        # before the Harrison-DM path and empties `captured` (D-088/D-089 flip,
        # 2026-07-24). Test-only hermeticity fix; the flip decision is untouched.
        monkeypatch.setenv("CORA_AUTOWRITE_LIVE", "off")

        captured: dict[str, str] = {}

        def fake_individual(updates, token, _client_factory=None):
            for u in updates:
                captured[u["update_id"]] = kr.format_single_item_dm(u)
            return {u["update_id"]: f"ts-{u['update_id']}" for u in updates}

        monkeypatch.setattr(rkr, "send_individual_dms", fake_individual)
        monkeypatch.setattr(rkr, "send_dm_to_harrison", lambda *a, **k: "hdr")

        kr.propose_update(
            update_id="ka-x", update_type="known_answer",
            description="F3E ops dashboard",
            payload={"entity": "F3E", "question": "where is the dashboard",
                     "answer": "the F3E ops dashboard lives in Polar",
                     "answered_by": "U-TOMMY"},
            confidence="HIGH")
        monkeypatch.setattr("sys.argv", ["run_knowledge_review.py"])
        rkr.main()
        states = {e["update_id"]: e["state"] for e in kr.load_proposed_updates()}
        return dict(captured), states, logs

    dm_on, st_on, logs_on = run_once("on", True)
    dm_off, st_off, logs_off = run_once("off", False)

    # (a) rendered DM byte-identical regardless of shadow
    assert dm_on and dm_on == dm_off
    # (b) ledger states identical
    assert st_on == st_off
    # (c) the item still PENDING -> shadow auto-approved NOTHING
    assert st_on["ka-x"] == "PENDING"
    # (d) shadow ON wrote a log; OFF wrote none
    on_files = list(logs_on.glob("graduated-trust-shadow-*.jsonl"))
    assert on_files, "shadow ON should have written a shadow log"
    assert not (logs_off.exists() and list(logs_off.glob("graduated-trust-shadow-*.jsonl")))
    # ...and the shadow record confirms it WOULD have auto-approved (Tier 0)
    recs = [_json.loads(l) for l in on_files[0].read_text(encoding="utf-8").splitlines()]
    ka = [r for r in recs if r.get("update_id") == "ka-x" and r["type"] == "shadow_decision"]
    assert ka and ka[0]["shadow_tier"] == 0 and ka[0]["shadow_decision"] == "would-auto-approve"


def test_owner_item_line_resolves_raw_slack_id():
    """Slice 3 (2026-07-29 audit): a raw <U…> token quoted from swept content into an
    owner card must be stripped/resolved before display (belt at the render chokepoint,
    catching already-PENDING items proposed before the reconciliation-side fix)."""
    line = rkr._format_owner_item_line(
        {
            "update_type": "task_close",
            "description": (
                'Possible task completion: "X" -- slack says: "<U0B3V5RHT3P>: done"'
            ),
        },
        1,
    )
    assert "U0B3V5RHT3P" not in line
