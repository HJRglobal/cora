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


def test_auto_approve_eligible_matrix():
    def u(update_type, confidence, state="PENDING"):
        return {"update_type": update_type, "confidence": confidence, "state": state}
    # HIGH non-canonical known_answer -> eligible
    assert rkr._auto_approve_eligible(u("known_answer", "HIGH")) is True
    # Lower confidence -> not eligible
    assert rkr._auto_approve_eligible(u("known_answer", "MED")) is False
    assert rkr._auto_approve_eligible(u("known_answer", "LOW")) is False
    # Canonical / external types never auto-approve, even at HIGH
    assert rkr._auto_approve_eligible(u("decision_capture", "HIGH")) is False
    assert rkr._auto_approve_eligible(u("asana_task", "HIGH")) is False
    assert rkr._auto_approve_eligible(u("hubspot_note", "HIGH")) is False
    assert rkr._auto_approve_eligible(u("efficiency", "HIGH")) is False
    # Already-resolved never auto-approves
    assert rkr._auto_approve_eligible(u("known_answer", "HIGH", state="APPROVED")) is False


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


def test_autoapprove_floor_inits_and_excludes_old_backlog(tmp_path, monkeypatch):
    floor_file = tmp_path / "floor.txt"
    monkeypatch.setattr(rkr, "_AUTOAPPROVE_FLOOR_PATH", floor_file)
    floor = rkr._autoapprove_floor()            # first call inits to "now"
    assert floor and floor_file.exists()
    assert rkr._autoapprove_floor() == floor    # stable on subsequent calls
    # An old backlog item is type/confidence-eligible but excluded by the floor.
    old = {"state": "PENDING", "update_type": "known_answer", "confidence": "HIGH",
           "proposed_at": "2020-01-01T00:00:00+00:00", "payload": {}}
    assert rkr._auto_approve_eligible(old) is True
    assert old["proposed_at"] < floor           # the caller's floor filter drops it


def test_autoapprove_excludes_teammate_dm():
    base = {"state": "PENDING", "update_type": "known_answer", "confidence": "HIGH"}
    assert rkr._auto_approve_eligible({**base, "payload": {"answer_source": "teammate_dm"}}) is False
    assert rkr._auto_approve_eligible({**base, "payload": {"answer_source": "slack_kb"}}) is True


def test_high_known_answer_roundtrip_persists(tmp_path, monkeypatch):
    """Confirm -> save -> retrieve (Harrison #9): a HIGH known_answer auto-approves,
    writes to known-answers, and is APPROVED with the auto-approve reason."""
    import importlib
    kr = importlib.import_module("cora.knowledge_review")

    proposed = tmp_path / "proposed.jsonl"
    reply_log = tmp_path / "reply.jsonl"
    ka_dir = tmp_path / "known-answers"
    resolved = tmp_path / "resolved.jsonl"

    floor_file = tmp_path / "floor.txt"
    floor_file.write_text("2000-01-01T00:00:00+00:00", encoding="utf-8")  # old floor
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", proposed)
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", reply_log)
    monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
    monkeypatch.setattr(rkr, "_AUTOAPPROVE_FLOOR_PATH", floor_file)
    monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(rkr, "_is_digest_day", lambda: False)  # isolate Step 1.5
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(ka_dir))
    monkeypatch.setenv("RESOLVED_GAPS_PATH", str(resolved))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")  # _post_to_slack no-ops; no network

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

    # Saved: the answer is now in the known-answers file.
    written = (ka_dir / "fndr.md").read_text(encoding="utf-8")
    assert "1234 Example St, Anaheim CA." in written
    # Persisted state: APPROVED with the auto-approve audit reason.
    entries = [e for e in kr.load_proposed_updates() if e["update_id"] == "ka-1"]
    assert entries and entries[0]["state"] == "APPROVED"
    assert entries[0]["resolved_reason"] == "auto_approved_high_generic"
    # Gap marked resolved (the retrieve-side ledger).
    assert resolved.exists() and "g-1" in resolved.read_text(encoding="utf-8")


def test_med_known_answer_not_auto_approved(tmp_path, monkeypatch):
    """A MED-confidence known_answer stays PENDING (only HIGH auto-approves)."""
    import importlib
    kr = importlib.import_module("cora.knowledge_review")

    proposed = tmp_path / "proposed.jsonl"
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", proposed)
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply.jsonl")
    monkeypatch.setattr(rkr, "_LOCK_PATH", tmp_path / "kr.lock")
    monkeypatch.setattr(rkr, "_AUTOAPPROVE_FLOOR_PATH", tmp_path / "floor.txt")
    monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(rkr, "_is_digest_day", lambda: False)
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path / "known-answers"))
    monkeypatch.setenv("RESOLVED_GAPS_PATH", str(tmp_path / "resolved.jsonl"))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")

    kr.propose_update(
        update_id="ka-med",
        update_type="known_answer",
        description="lower-confidence fact",
        payload={"entity": "FNDR", "question": "q", "answer": "a", "gap_ts": "g"},
        confidence="MED",
    )
    monkeypatch.setattr("sys.argv", ["run_knowledge_review.py"])
    rkr.main()

    entries = [e for e in kr.load_proposed_updates() if e["update_id"] == "ka-med"]
    assert entries and entries[0]["state"] == "PENDING"  # not auto-approved


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
        _op("op2", "decision_capture", "FNDR"),  # -> Harrison
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
    monkeypatch.setattr(rkr, "_AUTOAPPROVE_FLOOR_PATH", tmp_path / "floor.txt")
    monkeypatch.setattr(rkr, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(rkr, "_is_digest_day", lambda: False)  # NOT Monday
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
    monkeypatch.setattr(rkr, "_AUTOAPPROVE_FLOOR_PATH", tmp_path / "floor.txt")
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
