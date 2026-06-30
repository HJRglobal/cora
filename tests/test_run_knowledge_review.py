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
