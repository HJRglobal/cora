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


def test_is_digest_day_returns_bool():
    assert isinstance(rkr._is_digest_day(), bool)


def test_high_known_answer_roundtrip_persists(tmp_path, monkeypatch):
    """Confirm -> save -> retrieve (Harrison #9): a HIGH known_answer auto-approves,
    writes to known-answers, and is APPROVED with the auto-approve reason."""
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
