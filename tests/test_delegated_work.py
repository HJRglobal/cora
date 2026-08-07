"""Tests for delegated work Phase 1 -- job store, intake screens, quota/envelope,
fold, HELD lane, and the cora_delegate_work F-23 tool surface (S1).

Design of record: _shared/projects/cora/2026-08-01_fndr_cora-delegated-work-
phase1-design.md (LOCKED). The worker/runner (S2/S3) tests live in
tests/test_delegated_worker.py.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cora.delegated_work as dw
import cora.tools.tool_dispatch as td

REPO_ROOT = Path(__file__).resolve().parent.parent

USER = "U_TEAMMATE1"
OTHER = "U_TEAMMATE2"
CHANNEL = "f3e-leadership"
CHANNEL_ID = "C_F3ELEAD"


def _role(entity="F3E", external=False, name="Test Teammate"):
    return SimpleNamespace(entity=entity, external=external, name=name)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Fresh ledgers + log-level flag + passing guards for every test.

    Individual tests override the pieces they exercise (a refusal test patches
    the specific guard back to a blocking value).
    """
    monkeypatch.setattr(dw, "_BOT_LEDGER", tmp_path / "dw-bot.jsonl")
    monkeypatch.setattr(dw, "_RUNNER_LEDGER", tmp_path / "dw-runner.jsonl")
    monkeypatch.setattr(dw, "_CARD_RESERVE", {"date": None, "n": 0})
    monkeypatch.setenv("CORA_DELEGATED_WORK", "log")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)

    import cora.org_roles as org_roles
    import cora.user_access as user_access
    import cora.sibling_guard as sibling_guard
    import cora.cross_entity_guard as cross_entity_guard
    import cora.phi_guard as phi_guard

    monkeypatch.setattr(org_roles, "get_role", lambda uid: _role() if uid else None)
    monkeypatch.setattr(user_access, "check_access", lambda *a, **k: None)
    monkeypatch.setattr(sibling_guard, "check_redirect", lambda *a, **k: None)
    monkeypatch.setattr(cross_entity_guard, "check_cross_entity", lambda *a, **k: None)
    monkeypatch.setattr(phi_guard, "is_any_phi", lambda text: False)
    td._PENDING_DELEGATED_WORK.clear()
    yield
    td._PENDING_DELEGATED_WORK.clear()


def _submit(user=USER, entity="F3E", brief="Research the Sprouts energy-set reset timeline for F3",
            archetype="research_brief", deliverable="md", channel_id=CHANNEL_ID,
            channel_name=CHANNEL, thread_ts="123.456", client_factory=None):
    return dw.submit_job(user, entity, channel_id, channel_name, thread_ts,
                         archetype, brief, deliverable,
                         client_factory=client_factory or (lambda: None))


# ---------------------------------------------------------------------------
# Flag semantics
# ---------------------------------------------------------------------------

def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("CORA_DELEGATED_WORK", raising=False)
    assert dw.delegated_level() == "off"


def test_flag_whitelist(monkeypatch):
    for bad in ("banana", "ON", "1", "true", "enabled"):
        monkeypatch.setenv("CORA_DELEGATED_WORK", bad)
        assert dw.delegated_level() == "off"
    for good in ("off", "log", "live", " LIVE ", "Log"):
        monkeypatch.setenv("CORA_DELEGATED_WORK", good)
        assert dw.delegated_level() == good.strip().lower()


def test_screen_refuses_when_off(monkeypatch):
    monkeypatch.setenv("CORA_DELEGATED_WORK", "off")
    refusal = dw.screen_request(USER, "F3E", CHANNEL, "research_brief",
                                "long enough brief here", "md")
    assert refusal and "turned off" in refusal


# ---------------------------------------------------------------------------
# Intake screens (each deterministic + fail-closed)
# ---------------------------------------------------------------------------

def test_screen_unknown_user_refused(monkeypatch):
    import cora.org_roles as org_roles
    monkeypatch.setattr(org_roles, "get_role", lambda uid: None)
    refusal = dw.screen_request(USER, "F3E", CHANNEL, "research_brief",
                                "long enough brief here", "md")
    assert refusal and "can't verify you" in refusal


def test_screen_org_roles_error_fail_closed(monkeypatch):
    import cora.org_roles as org_roles

    def _boom(uid):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(org_roles, "get_role", _boom)
    assert dw.screen_request(USER, "F3E", CHANNEL, "research_brief",
                             "long enough brief here", "md")


def test_screen_external_refused(monkeypatch):
    import cora.org_roles as org_roles
    monkeypatch.setattr(org_roles, "get_role", lambda uid: _role(external=True))
    refusal = dw.screen_request(USER, "F3E", CHANNEL, "research_brief",
                                "long enough brief here", "md")
    assert refusal and "internal teammates" in refusal


def test_screen_lex_primary_requester_refused(monkeypatch):
    import cora.org_roles as org_roles
    monkeypatch.setattr(org_roles, "get_role", lambda uid: _role(entity="LEX-LLC"))
    refusal = dw.screen_request(USER, "F3E", CHANNEL, "research_brief",
                                "long enough brief here", "md")
    assert refusal and "Lexington" in refusal


def test_screen_lex_channel_refused():
    for lex_entity in ("LEX", "LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA"):
        refusal = dw.screen_request(USER, lex_entity, "llc-leadership",
                                    "research_brief", "long enough brief here", "md")
        assert refusal and "Lexington" in refusal


def test_screen_phi_refused(monkeypatch):
    import cora.phi_guard as phi_guard
    monkeypatch.setattr(phi_guard, "is_any_phi", lambda text: True)
    refusal = dw.screen_request(USER, "F3E", CHANNEL, "research_brief",
                                "long enough brief here", "md")
    assert refusal and "protected" in refusal


# ---------------------------------------------------------------------------
# LEX lane (CORA_DELEGATED_WORK_LEX) -- Harrison decision 2026-08-06,
# superseding the D-102 v1 "LEX excluded by construction" line.
# The two tests above pin the OFF default and must stay green unchanged.
# ---------------------------------------------------------------------------

LEX_BRIEF = "research what DDD requires for live-in caregiver respite documentation"


@pytest.fixture
def _lex_lane(monkeypatch):
    monkeypatch.setenv("CORA_DELEGATED_WORK_LEX", "on")
    monkeypatch.setattr(
        dw, "_staff_names_cache", {"Shaun Hawkins", "Jennifer Mortensen"})
    yield


class TestLexDelegatedLane:
    @pytest.mark.parametrize("value", ["", "off", "0", "false", "no", "sometimes"])
    def test_flag_defaults_and_unrecognized_read_off(self, monkeypatch, value):
        monkeypatch.setenv("CORA_DELEGATED_WORK_LEX", value)
        assert not dw.lex_delegated_enabled()

    @pytest.mark.parametrize("archetype", ["research_brief", "doc_draft"])
    def test_allowed_archetypes_pass_with_the_lane_on(self, _lex_lane, archetype):
        # Smoke (a): a LEX policy research brief / doc draft may queue.
        assert dw.screen_request(
            USER, "LEX-LLC", "llc-leadership", archetype, LEX_BRIEF, "md") is None

    @pytest.mark.parametrize("archetype", ["spreadsheet_build", "creator_shortlist"])
    def test_off_archetypes_refuse_with_route_copy(self, _lex_lane, archetype):
        # Smoke (c): archetype refusal names what IS available, never a bare no.
        refusal = dw.screen_request(
            USER, "LEX-LLC", "llc-leadership", archetype, LEX_BRIEF, "xlsx"
            if archetype == "spreadsheet_build" else "md")
        assert refusal
        assert "research brief" in refusal and "document draft" in refusal
        assert "Harrison" in refusal  # who can unlock

    def test_lane_off_refusal_carries_all_three_parts(self):
        # C2 route-don't-deflect: why + nearest path + unlock owner.
        refusal = dw.screen_request(
            USER, "LEX-LLC", "llc-leadership", "research_brief", LEX_BRIEF, "md")
        assert refusal
        assert "isn't enabled" in refusal            # why
        assert "knowledge base" in refusal           # nearest working path
        assert "Harrison" in refusal                 # who unlocks

    @pytest.mark.parametrize("brief", [
        "research whether client Marcus Delgado qualifies for respite units",
        "draft a memo about Marcus Delgado's service authorization renewal",
    ])
    def test_client_identifying_brief_refuses_fail_closed(self, _lex_lane, brief):
        # Smoke (b): nothing queued, and the refusal routes rather than dead-ends.
        refusal = dw.screen_request(
            USER, "LEX-LLC", "llc-leadership", "research_brief", brief, "md")
        assert refusal
        assert "names a specific person" in refusal
        assert "what does DDD require" in refusal    # the nearest working path
        assert "Harrison" in refusal

    def test_lex_requester_in_a_non_lex_channel_is_still_a_lex_job(
        self, monkeypatch, _lex_lane
    ):
        # Both legs of the LEX test survive the flip: a LEX person asking in a
        # shared channel gets the LEX archetype restriction, not the open set.
        import cora.org_roles as org_roles
        monkeypatch.setattr(org_roles, "get_role", lambda uid: _role(entity="LEX-LLC"))
        refusal = dw.screen_request(
            USER, "F3E", CHANNEL, "spreadsheet_build", LEX_BRIEF, "xlsx")
        assert refusal and "research brief" in refusal

    def test_non_lex_requests_are_unaffected_by_the_lane(self, _lex_lane):
        # Invariant 4: non-LEX behaviour identical with the flag ON.
        assert dw.screen_request(
            USER, "F3E", CHANNEL, "spreadsheet_build", LEX_BRIEF, "xlsx") is None
        assert dw.screen_request(
            USER, "F3E", CHANNEL, "research_brief",
            "research whether client Marcus Delgado qualifies for respite units",
            "md") is None

    def test_guard_parity_still_runs_for_lex(self, monkeypatch, _lex_lane):
        # The flip must be a SCOPED ALLOW, never a guard removal (D-102).
        import cora.user_access as user_access
        monkeypatch.setattr(
            user_access, "check_access",
            lambda *a, **k: "blocked by user_access")
        refusal = dw.screen_request(
            USER, "LEX-LLC", "llc-leadership", "research_brief", LEX_BRIEF, "md")
        assert refusal == "blocked by user_access"


def test_screen_phi_error_fail_closed(monkeypatch):
    import cora.phi_guard as phi_guard

    def _boom(text):
        raise RuntimeError("phi check exploded")

    monkeypatch.setattr(phi_guard, "is_any_phi", _boom)
    refusal = dw.screen_request(USER, "F3E", CHANNEL, "research_brief",
                                "long enough brief here", "md")
    assert refusal and "fail-closed" in refusal


def test_screen_user_access_block_propagates(monkeypatch):
    import cora.user_access as user_access
    monkeypatch.setattr(user_access, "check_access",
                        lambda *a, **k: "You don't have access to that entity.")
    refusal = dw.screen_request(USER, "F3E", CHANNEL, "research_brief",
                                "long enough brief here", "md")
    assert refusal == "You don't have access to that entity."


def test_screen_user_access_pins_non_custodian(monkeypatch):
    """The guard-parity screen must pass phi_custodian=False -- a delegated job
    never carries custodian privileges (design 8.3)."""
    import cora.user_access as user_access
    seen = {}

    def _check(user, entity, text, phi_custodian=None, tier=None):
        seen["phi_custodian"] = phi_custodian
        seen["tier"] = tier
        return None

    monkeypatch.setattr(user_access, "check_access", _check)
    dw.screen_request(USER, "F3E", CHANNEL, "research_brief",
                      "long enough brief here", "md")
    assert seen["phi_custodian"] is False
    assert seen["tier"] in ("TIER_1", "TIER_3")


def test_screen_sibling_redirect_propagates(monkeypatch):
    import cora.sibling_guard as sibling_guard
    monkeypatch.setattr(sibling_guard, "check_redirect",
                        lambda *a, **k: "Ask in the #lts channel instead.")
    refusal = dw.screen_request(USER, "F3E", CHANNEL, "research_brief",
                                "long enough brief here", "md")
    assert refusal == "Ask in the #lts channel instead."


def test_screen_cross_entity_redirect_propagates(monkeypatch):
    import cora.cross_entity_guard as cross_entity_guard
    monkeypatch.setattr(cross_entity_guard, "check_cross_entity",
                        lambda *a, **k: "That's an OSN question -- ask in #osn-*.")
    refusal = dw.screen_request(USER, "F3E", CHANNEL, "research_brief",
                                "long enough brief here", "md")
    assert refusal == "That's an OSN question -- ask in #osn-*."


def test_screen_guard_error_fail_closed(monkeypatch):
    import cora.user_access as user_access

    def _boom(*a, **k):
        raise RuntimeError("guard exploded")

    monkeypatch.setattr(user_access, "check_access", _boom)
    refusal = dw.screen_request(USER, "F3E", CHANNEL, "research_brief",
                                "long enough brief here", "md")
    assert refusal and "fail-closed" in refusal


def test_screen_shape_validation():
    assert "archetype" in dw.screen_request(USER, "F3E", CHANNEL, "make_coffee",
                                            "long enough brief here", "md")
    assert "deliverable" in dw.screen_request(USER, "F3E", CHANNEL, "doc_draft",
                                              "long enough brief here", "pdf")
    assert "too short" in dw.screen_request(USER, "F3E", CHANNEL, "doc_draft",
                                            "hi", "md")


# ---------------------------------------------------------------------------
# Submit -> queued; quota; founder exemption; cancel-does-not-refund
# ---------------------------------------------------------------------------

def test_submit_queues_and_folds():
    job, outcome, msg = _submit()
    assert outcome == "queued"
    assert job["job_id"].startswith("dw-") and len(job["job_id"]) == 15
    rec = dw.get_job(job["job_id"])
    assert rec["state"] == dw.STATE_QUEUED
    assert rec["archetype"] == "research_brief"
    assert rec["channel_id"] == CHANNEL_ID
    assert rec["thread_ts"] == "123.456"
    assert rec["requester"] == USER


def test_user_quota_third_job_ok_fourth_held():
    for i in range(dw.user_daily_quota()):
        job, outcome, _ = _submit(brief=f"distinct brief number {i} long enough")
        assert outcome == "queued"
    job, outcome, msg = _submit(brief="one more distinct brief beyond quota")
    assert outcome == "held"
    assert dw.get_job(job["job_id"])["state"] == dw.STATE_HELD
    assert dw.get_job(job["job_id"])["held_reason"] == "user_quota"


def test_founder_quota_exempt():
    for i in range(dw.user_daily_quota() + 2):
        job, outcome, _ = _submit(user=dw.HARRISON_ID,
                                  brief=f"founder brief number {i} long enough")
        assert outcome == "queued"


def test_org_quota_held(monkeypatch):
    monkeypatch.setenv("CORA_DELEGATED_ORG_DAILY", "2")
    monkeypatch.setenv("CORA_DELEGATED_USER_DAILY", "5")
    _submit(user=USER, brief="first org-wide brief long enough")
    _submit(user=OTHER, brief="second org-wide brief long enough")
    job, outcome, _ = _submit(user="U_THIRD", brief="third org-wide brief long enough")
    assert outcome == "held"
    assert dw.get_job(job["job_id"])["held_reason"] == "org_quota"


def test_cancel_does_not_refund_quota():
    job, outcome, _ = _submit()
    assert outcome == "queued"
    before = dw.requested_today(USER)
    outcome, _ = dw.cancel_job(job["job_id"], USER)
    assert outcome == "cancelled"
    assert dw.requested_today(USER) == before  # requested events are the basis


# ---------------------------------------------------------------------------
# Envelope: reservation math + month bucketing
# ---------------------------------------------------------------------------

def test_envelope_reservation_holds_burst(monkeypatch):
    # Cap $4, per-job $2 -> two open reservations exhaust the envelope even
    # with ZERO spend recorded (the review H-7 burst case).
    monkeypatch.setenv("CORA_DELEGATED_MONTHLY_USD", "4")
    monkeypatch.setenv("CORA_DELEGATED_JOB_USD", "2")
    monkeypatch.setenv("CORA_DELEGATED_USER_DAILY", "10")
    _submit(brief="reservation burst brief one long enough")
    _submit(brief="reservation burst brief two long enough")
    job, outcome, _ = _submit(brief="reservation burst brief three long enough")
    assert outcome == "held"
    assert dw.get_job(job["job_id"])["held_reason"] == "envelope"


def test_envelope_mtd_buckets_by_requested_az_month():
    # A job requested LAST month with real spend must not count toward this
    # month's MTD (design section 10: bucketed by requested ts, AZ time).
    last_month = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    dw.append_bot_event({"event": "requested", "ts": last_month, "job_id": "dw-old111111111",
                         "archetype": "doc_draft", "title": "t", "brief": "b",
                         "requester": OTHER, "requester_name": "o", "entity": "F3E",
                         "channel_id": "C1", "channel_name": "c", "thread_ts": "",
                         "deliverable": "md", "fingerprint": "fp-old"})
    dw.append_bot_event({"event": "queued", "ts": last_month, "job_id": "dw-old111111111"})
    dw.append_runner_event({"event": "started", "ts": last_month, "job_id": "dw-old111111111"})
    dw.append_runner_event({"event": "delivered", "ts": last_month, "job_id": "dw-old111111111",
                            "cost": {"est_usd": 1.75}})
    assert dw.mtd_spend() == 0.0

    now = dw._now_iso()
    dw.append_bot_event({"event": "requested", "ts": now, "job_id": "dw-new111111111",
                         "archetype": "doc_draft", "title": "t", "brief": "b2",
                         "requester": OTHER, "requester_name": "o", "entity": "F3E",
                         "channel_id": "C1", "channel_name": "c", "thread_ts": "",
                         "deliverable": "md", "fingerprint": "fp-new"})
    dw.append_bot_event({"event": "queued", "ts": now, "job_id": "dw-new111111111"})
    dw.append_runner_event({"event": "started", "ts": dw._now_iso(), "job_id": "dw-new111111111"})
    dw.append_runner_event({"event": "delivered", "ts": dw._now_iso(),
                            "job_id": "dw-new111111111", "cost": {"est_usd": 0.5}})
    assert dw.mtd_spend() == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def test_dedup_nonterminal_refuses_regardless_of_age():
    brief = "identical dedup brief long enough to pass"
    _submit(brief=brief)
    _job, outcome, msg = _submit(brief=brief)
    assert outcome == "refused" and "already have this exact job" in msg


def test_dedup_terminal_reask_allowed():
    brief = "identical dedup brief long enough to pass"
    job, _, _ = _submit(brief=brief)
    dw.append_runner_event({"event": "started", "ts": dw._now_iso(), "job_id": job["job_id"]})
    dw.append_runner_event({"event": "delivered", "ts": dw._now_iso(),
                            "job_id": job["job_id"], "cost": {"est_usd": 0.2}})
    _job2, outcome, _ = _submit(brief=brief)
    assert outcome == "queued"


def test_dedup_normalizes_mentions_and_whitespace():
    _submit(brief="research the <@U123> vendor   list for F3 retail")
    _job, outcome, _ = _submit(brief="Research the vendor list  for F3 retail")
    assert outcome == "refused"


# ---------------------------------------------------------------------------
# Fold: two-file merge, torn lines, terminal-resurrect trap, artifact_homed
# ---------------------------------------------------------------------------

def test_fold_merges_both_ledgers_by_ts():
    job, _, _ = _submit()
    jid = job["job_id"]
    dw.append_runner_event({"event": "started", "ts": dw._now_iso(), "job_id": jid})
    rec = dw.get_job(jid)
    assert rec["state"] == dw.STATE_RUNNING
    dw.append_runner_event({"event": "delivered", "ts": dw._now_iso(), "job_id": jid,
                            "cost": {"est_usd": 0.42}})
    rec = dw.get_job(jid)
    assert rec["state"] == dw.STATE_DELIVERED
    assert rec["cost"]["est_usd"] == 0.42


def test_fold_skips_torn_lines():
    job, _, _ = _submit()
    with dw._RUNNER_LEDGER.open("a", encoding="utf-8") as fh:
        fh.write('{"event": "started", "ts": "2026-08-01T10:0')  # torn mid-write
    rec = dw.get_job(job["job_id"])
    assert rec["state"] == dw.STATE_QUEUED  # torn line ignored, fold intact


def test_terminal_resurrect_trap():
    """A late state event on a terminal row is ignored (cq-dad80c0011c9 class)."""
    job, _, _ = _submit()
    jid = job["job_id"]
    dw.append_bot_event({"event": "cancelled", "ts": dw._now_iso(), "job_id": jid,
                         "reason": "requester_cancel"})
    # Late runner events arrive AFTER the cancel (crash-delayed writes).
    dw.append_runner_event({"event": "started", "ts": dw._now_iso(), "job_id": jid})
    dw.append_runner_event({"event": "delivered", "ts": dw._now_iso(), "job_id": jid,
                            "cost": {"est_usd": 1.0}})
    rec = dw.get_job(jid)
    assert rec["state"] == dw.STATE_CANCELLED  # never resurrected


def test_artifact_homed_allowed_after_delivered_but_never_state():
    job, _, _ = _submit()
    jid = job["job_id"]
    dw.append_runner_event({"event": "started", "ts": dw._now_iso(), "job_id": jid})
    dw.append_runner_event({"event": "delivering", "ts": dw._now_iso(), "job_id": jid,
                            "artifact": {"local_path": "x.md", "mis_homed": True},
                            "cost": {"est_usd": 0.3}})
    dw.append_runner_event({"event": "delivered", "ts": dw._now_iso(), "job_id": jid,
                            "cost": {"est_usd": 0.3}})
    dw.append_runner_event({"event": "artifact_homed", "ts": dw._now_iso(), "job_id": jid,
                            "target_path": "G:/somewhere/x.md"})
    rec = dw.get_job(jid)
    assert rec["state"] == dw.STATE_DELIVERED
    assert rec["artifact"]["mis_homed"] is False
    assert rec["artifact"]["target_path"] == "G:/somewhere/x.md"


def test_expiry_clock_runs_from_queued_entry_event():
    # Manual timeline: requested + held 60h ago, released 1h ago -> the 48h
    # expiry clock re-bases on the release (a late Harrison release always
    # gets its full window; design section 6).
    jid = "dw-expiry1111111"
    old = (datetime.now(timezone.utc) - timedelta(hours=60)).isoformat()
    dw.append_bot_event({"event": "requested", "ts": old, "job_id": jid,
                         "archetype": "doc_draft", "title": "t", "brief": "b",
                         "requester": USER, "requester_name": "n", "entity": "F3E",
                         "channel_id": "C1", "channel_name": "c", "thread_ts": "",
                         "deliverable": "md", "fingerprint": "fp-expiry"})
    dw.append_bot_event({"event": "held", "ts": old, "job_id": jid, "reason": "user_quota"})
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    dw.append_bot_event({"event": "released", "ts": recent, "job_id": jid})
    rec = dw.get_job(jid)
    assert rec["state"] == dw.STATE_QUEUED
    assert rec["queued_at"] == recent  # the 48h clock re-bases on release


def test_writer_validation_rejects_cross_lane_events():
    with pytest.raises(ValueError):
        dw.append_bot_event({"event": "delivered", "ts": dw._now_iso(), "job_id": "dw-x"})
    with pytest.raises(ValueError):
        dw.append_runner_event({"event": "queued", "ts": dw._now_iso(), "job_id": "dw-x"})


# ---------------------------------------------------------------------------
# HELD lane: Harrison-gated release/dismiss + card cap
# ---------------------------------------------------------------------------

def _held_job(i=0, user=USER):
    """Force a HELD job via user quota."""
    with patch.object(dw, "user_daily_quota", return_value=0):
        job, outcome, _ = _submit(user=user, brief=f"held brief number {i} long enough")
    assert outcome == "held"
    return job


def test_process_job_action_harrison_gated():
    job = _held_job()
    outcome, msg = dw.process_job_action(dw.ACTION_RELEASE, job["job_id"], USER)
    assert outcome == "not_authorized"


def test_release_and_idempotency():
    job = _held_job()
    outcome, _ = dw.process_job_action(dw.ACTION_RELEASE, job["job_id"], dw.HARRISON_ID)
    assert outcome == "released"
    assert dw.get_job(job["job_id"])["state"] == dw.STATE_QUEUED
    outcome, msg = dw.process_job_action(dw.ACTION_RELEASE, job["job_id"], dw.HARRISON_ID)
    assert outcome == "noop" and "Already released" in msg


def test_dismiss_and_idempotency():
    job = _held_job()
    outcome, _ = dw.process_job_action(dw.ACTION_DISMISS, job["job_id"], dw.HARRISON_ID)
    assert outcome == "dismissed"
    assert dw.get_job(job["job_id"])["state"] == dw.STATE_CANCELLED
    outcome, _ = dw.process_job_action(dw.ACTION_DISMISS, job["job_id"], dw.HARRISON_ID)
    assert outcome == "noop"


def test_held_card_cap_five_per_day(monkeypatch):
    monkeypatch.setattr(dw, "user_daily_quota", lambda: 0)
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D_HARRISON"}}
    client.chat_postMessage.return_value = {"ts": "1.0"}
    factory = lambda: client  # noqa: E731
    for i in range(dw.MAX_HELD_CARDS_PER_DAY + 2):
        _submit(brief=f"card cap brief number {i} long enough",
                client_factory=factory)
    assert client.chat_postMessage.call_count == dw.MAX_HELD_CARDS_PER_DAY
    overflow = dw.held_jobs_awaiting_card()
    assert len(overflow) == 2  # durable card_held markers, never lost


def test_held_never_expires_marker():
    # HELD jobs have no queued_at, so the runner's expiry query (keyed on
    # queued_at) can never select them -- pin the precondition.
    job = _held_job()
    rec = dw.get_job(job["job_id"])
    assert rec["state"] == dw.STATE_HELD
    assert not rec.get("queued_at")


# ---------------------------------------------------------------------------
# Cancel ownership
# ---------------------------------------------------------------------------

def test_cancel_own_queued_only():
    job, _, _ = _submit()
    outcome, _ = dw.cancel_job(job["job_id"], OTHER)
    assert outcome == "refused"
    outcome, _ = dw.cancel_job(job["job_id"], USER)
    assert outcome == "cancelled"


def test_cancel_harrison_any_and_running_refused():
    job, _, _ = _submit()
    outcome, _ = dw.cancel_job(job["job_id"], dw.HARRISON_ID)
    assert outcome == "cancelled"
    job2, _, _ = _submit(brief="second cancel-test brief long enough")
    dw.append_runner_event({"event": "started", "ts": dw._now_iso(),
                            "job_id": job2["job_id"]})
    outcome, msg = dw.cancel_job(job2["job_id"], dw.HARRISON_ID)
    assert outcome == "refused" and "running" in msg.lower()


# ---------------------------------------------------------------------------
# List view + observability summary
# ---------------------------------------------------------------------------

def test_render_job_list_cross_channel_title_suppression():
    _submit(brief="visible title brief long enough", channel_id="C_HERE")
    _submit(brief="hidden title brief long enough", channel_id="C_ELSEWHERE")
    out = dw.render_job_list(USER, "C_HERE")
    assert "visible title brief" in out
    assert "hidden title brief" not in out  # id/state render, title suppressed
    assert out.count("dw-") == 2


def test_render_job_list_empty_channel_suppresses_all_titles():
    _submit(brief="some brief long enough here", channel_id="C_HERE")
    out = dw.render_job_list(USER, "")
    assert "some brief" not in out


def test_jobs_summary_never_carries_titles_or_briefs():
    brief = "super secret compensation analysis brief long enough"
    _submit(brief=brief)
    summary = dw.jobs_summary()
    blob = json.dumps(summary)
    assert "super secret" not in blob
    assert "compensation" not in blob
    assert summary["counts_by_state"].get("QUEUED") == 1
    assert summary["recent"][0]["job_id"].startswith("dw-")
    assert "title" not in summary["recent"][0]
    assert "brief" not in summary["recent"][0]


# ---------------------------------------------------------------------------
# Tool surface: F-23 stash-not-echo, TTL, re-preview verbatim, trial ack
# ---------------------------------------------------------------------------

def _tool(user=USER, entity="F3E", **inp):
    inp.setdefault("_channel_name", CHANNEL)
    inp.setdefault("_channel_id", CHANNEL_ID)
    inp.setdefault("_thread_ts", "123.456")
    return td._tool_cora_delegate_work(user, entity, inp)


def test_tool_preview_stashes_and_renders_brief_verbatim():
    brief = "Research THE EXACT verbatim brief text for the preview"
    out = _tool(action="request", archetype="research_brief", brief=brief)
    assert out.startswith("WRITE_BLOCKED")
    assert brief in out
    assert "Nothing runs until you confirm" in out
    pending = td._peek_pending_delegated(USER, CHANNEL)
    assert pending and pending["brief"] == brief


def test_tool_confirm_executes_stash_not_echo():
    stashed = "the stashed brief is the one that must run long enough"
    _tool(action="request", archetype="research_brief", brief=stashed)
    out = _tool(action="request", archetype="doc_draft",
                brief="a DIFFERENT model-echoed brief that must NOT run",
                confirmed=True)
    assert out.startswith("WRITE_CONFIRMED")
    jobs = dw.load_jobs()
    assert len(jobs) == 1
    assert jobs[0]["brief"] == stashed
    assert jobs[0]["archetype"] == "research_brief"  # stash wins over echo


def test_tool_confirm_with_no_pending_repreviews():
    out = _tool(action="request", archetype="doc_draft",
                brief="fresh brief after the pending expired long enough",
                confirmed=True)
    assert out.startswith("WRITE_BLOCKED")  # re-preview, nothing executed
    assert dw.load_jobs() == []
    assert "fresh brief after the pending expired" in out  # verbatim re-preview


def test_tool_expired_pending_repreviews():
    _tool(action="request", archetype="doc_draft",
          brief="brief that will expire in the stash long enough")
    key = td._delegated_pending_key(USER, CHANNEL)
    td._PENDING_DELEGATED_WORK[key]["ts"] = time.time() - 700  # past TTL
    out = _tool(action="request", archetype="doc_draft",
                brief="brief that will expire in the stash long enough",
                confirmed=True)
    assert out.startswith("WRITE_BLOCKED")
    assert dw.load_jobs() == []


def test_tool_log_mode_ack_labels_trial():
    _tool(action="request", archetype="doc_draft",
          brief="trial mode labelling brief long enough")
    out = _tool(confirmed=True)
    assert out.startswith("WRITE_CONFIRMED")
    assert "TRIAL MODE" in out


def test_tool_live_mode_ack_has_no_trial_label(monkeypatch):
    monkeypatch.setenv("CORA_DELEGATED_WORK", "live")
    _tool(action="request", archetype="doc_draft",
          brief="live mode labelling brief long enough")
    out = _tool(confirmed=True)
    assert out.startswith("WRITE_CONFIRMED")
    assert "TRIAL MODE" not in out


def test_tool_phi_screen_runs_before_stash(monkeypatch):
    import cora.phi_guard as phi_guard
    monkeypatch.setattr(phi_guard, "is_any_phi", lambda text: True)
    out = _tool(action="request", archetype="doc_draft",
                brief="Bob Smith's billing authorization draft long enough")
    assert out.startswith("WRITE_BLOCKED")
    assert td._peek_pending_delegated(USER, CHANNEL) is None  # never stashed
    assert "Bob Smith" not in out  # never echoed


def test_tool_off_refuses():
    import os
    os.environ["CORA_DELEGATED_WORK"] = "off"
    try:
        out = _tool(action="request", archetype="doc_draft",
                    brief="flag off refusal brief long enough")
        assert "turned off" in out
    finally:
        os.environ["CORA_DELEGATED_WORK"] = "log"


def test_tool_list_and_cancel():
    _tool(action="request", archetype="doc_draft",
          brief="list and cancel test brief long enough")
    _tool(confirmed=True)
    jid = dw.load_jobs()[0]["job_id"]
    out = _tool(action="list")
    assert jid in out
    out = _tool(action="cancel", job_id=jid)
    assert out.startswith("WRITE_CONFIRMED")
    assert dw.get_job(jid)["state"] == dw.STATE_CANCELLED


# ---------------------------------------------------------------------------
# Interceptor (F-23 bare-"yes") registration
# ---------------------------------------------------------------------------

def test_interceptor_bare_yes_executes_delegated_pending():
    _tool(action="request", archetype="research_brief",
          brief="interceptor bare-yes brief long enough")
    reply = td.try_confirm_pending_write(
        slack_user_id=USER, channel_name=CHANNEL, entity="F3E", message="yes")
    assert reply is not None and "Queued (dw-" in reply
    assert len(dw.load_jobs()) == 1


def test_interceptor_negate_clears_delegated_pending():
    _tool(action="request", archetype="research_brief",
          brief="interceptor negate brief long enough")
    reply = td.try_confirm_pending_write(
        slack_user_id=USER, channel_name=CHANNEL, entity="F3E", message="no, cancel")
    assert reply == td._CONFIRM_CANCELLED_REPLY
    assert td._peek_pending_delegated(USER, CHANNEL) is None
    assert dw.load_jobs() == []


def test_interceptor_content_message_falls_through():
    _tool(action="request", archetype="research_brief",
          brief="interceptor content fall-through brief long enough")
    reply = td.try_confirm_pending_write(
        slack_user_id=USER, channel_name=CHANNEL, entity="F3E",
        message="actually what was Q2 revenue?")
    assert reply is None
    assert td._peek_pending_delegated(USER, CHANNEL) is not None  # intact


def test_interceptor_freshest_delegated_abandons_stale_destructive_asana():
    """A fresher delegated pending supersedes a staler destructive Asana pending
    -- the stale delete is ABANDONED so a later 'yes' can never fire it."""
    td._PENDING_ASANA_WRITES[td._asana_pending_key(USER, CHANNEL)] = {
        "action": "delete", "ts": time.time() - 60, "label": "old task",
    }
    _tool(action="request", archetype="research_brief",
          brief="fresher delegated pending brief long enough")
    reply = td.try_confirm_pending_write(
        slack_user_id=USER, channel_name=CHANNEL, entity="F3E", message="yes")
    assert reply is not None and "Queued (dw-" in reply
    # The stale destructive Asana pending was abandoned, not fired.
    assert td._peek_pending_asana(USER, CHANNEL) is None


def test_interceptor_action_verb_conflict_falls_through():
    _tool(action="request", archetype="research_brief",
          brief="action verb conflict brief long enough")
    reply = td.try_confirm_pending_write(
        slack_user_id=USER, channel_name=CHANNEL, entity="F3E",
        message="delete it")
    assert reply is None  # 'delete' conflicts with the delegated pending
    assert dw.load_jobs() == []


def test_has_pending_delegated_write_probe():
    assert td.has_pending_delegated_write(USER, CHANNEL) is False
    _tool(action="request", archetype="doc_draft",
          brief="sonnet force probe brief long enough")
    assert td.has_pending_delegated_write(USER, CHANNEL) is True


# ---------------------------------------------------------------------------
# Wiring + prompt coverage
# ---------------------------------------------------------------------------

def test_tool_registered_everywhere():
    assert "cora_delegate_work" in td._GLOBAL_CORE_TOOLS
    assert "cora_delegate_work" in td._TOOL_FUNCTIONS
    assert td._TOOL_TIMEOUTS.get("cora_delegate_work") == 20
    names = [t["name"] for t in td.TOOL_DEFINITIONS]
    assert "cora_delegate_work" in names


def test_tool_in_contract_write_net():
    from cora import claude_client
    assert "cora_delegate_work" in claude_client._CONTRACT_WRITE_TOOLS


def test_app_probes_include_delegated():
    src = (REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
    assert src.count("has_pending_delegated_write") >= 2  # both probe sites


def test_all_entity_prompts_carry_delegation_section():
    prompts_dir = REPO_ROOT / "design" / "system-prompts"
    lex_files = {"lex.md", "llc.md", "lts.md", "lbhs.md", "lla.md"}
    missing, wrong = [], []
    for f in sorted(prompts_dir.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        if "## Delegating work to Cora" not in text:
            missing.append(f.name)
            continue
        if f.name in lex_files:
            # LEX lane (2026-08-06): the prompts no longer say "NOT available";
            # they state the RESTRICTED shape and must not promise the lane,
            # which may be off. See test_lex_prompts_state_the_restricted_lane.
            if "research brief" not in text or "document draft" not in text:
                wrong.append(f.name)
        elif "cora_delegate_work" not in text:
            wrong.append(f.name)
    assert not missing, f"prompts missing the delegation section: {missing}"
    assert not wrong, f"prompts with the wrong delegation variant: {wrong}"


def test_lex_prompts_state_the_restricted_lane():
    """LEX prompts must name the two allowed archetypes, exclude the other two,
    forbid client detail in a brief, and NOT promise a lane that may be off."""
    prompts_dir = REPO_ROOT / "design" / "system-prompts"
    for name in ("lex.md", "llc.md", "lts.md", "lbhs.md", "lla.md"):
        text = (prompts_dir / name).read_text(encoding="utf-8")
        seg = text[text.index("## Delegating work to Cora"):]
        seg = seg[:seg.index("\n## ")] if "\n## " in seg else seg
        seg = " ".join(seg.split())  # prompts are hard-wrapped; match on prose
        assert "research brief" in seg and "document draft" in seg, name
        assert "Spreadsheet builds" in seg and "creator shortlists" in seg, name
        assert "may not be switched on yet" in seg, name   # never promises
        assert "Do not promise it" in seg, name
        assert "client names" in seg, name                 # PHI shape warning


def test_every_prompt_carries_the_route_dont_deflect_doctrine():
    """C2 (2026-08-06): every refusal must carry why + nearest path + who
    unlocks. A bare 'I can't' is what ended a real user's usage."""
    prompts_dir = REPO_ROOT / "design" / "system-prompts"
    missing = []
    for f in sorted(prompts_dir.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        if ("Refusals: route, never dead-end" not in text
                or "Who can unlock it" not in text
                or "How you actually work" not in text
                or "Never invent a mechanism" not in text):
            missing.append(f.name)
    assert not missing, f"prompts missing the refusal doctrine: {missing}"


# ---------------------------------------------------------------------------
# D-051 remediation bindings (2026-08-01 review)
# ---------------------------------------------------------------------------

def test_confirm_rescreens_phi_flip_between_preview_and_confirm(monkeypatch):
    """The stale-stash TOCTOU defense MUST bind: a guard that starts blocking
    within the pending TTL refuses the confirm (D-051 test lens, HIGH)."""
    import cora.phi_guard as phi_guard
    _tool(action="request", archetype="doc_draft",
          brief="benign at preview time, flagged at confirm long enough")
    monkeypatch.setattr(phi_guard, "is_any_phi", lambda text: True)
    out = _tool(confirmed=True)
    assert out.startswith("WRITE_BLOCKED")
    assert dw.load_jobs() == []  # the stale stash never executed


def test_confirm_rescreens_access_flip_between_preview_and_confirm(monkeypatch):
    import cora.user_access as user_access
    _tool(action="request", archetype="doc_draft",
          brief="benign access at preview time long enough")
    monkeypatch.setattr(user_access, "check_access",
                        lambda *a, **k: "Access revoked mid-flight.")
    out = _tool(confirmed=True)
    assert out.startswith("WRITE_BLOCKED")
    assert "Access revoked" in out
    assert dw.load_jobs() == []


def test_held_card_send_failure_leaves_durable_marker():
    """A HELD job whose card DM fails must still surface via the overflow
    digest -- three review lenses found the silent-orphan arm independently."""
    client = MagicMock()
    client.conversations_open.side_effect = RuntimeError("slack 500")
    with patch.object(dw, "user_daily_quota", return_value=0):
        job, outcome, _ = _submit(brief="card send failure brief long enough",
                                  client_factory=lambda: client)
    assert outcome == "held"
    overflow = dw.held_jobs_awaiting_card()
    assert [r["job_id"] for r in overflow] == [job["job_id"]]


def test_held_card_no_client_leaves_durable_marker():
    with patch.object(dw, "user_daily_quota", return_value=0):
        job, outcome, _ = _submit(brief="no client card brief long enough",
                                  client_factory=lambda: None)
    assert outcome == "held"
    assert [r["job_id"] for r in dw.held_jobs_awaiting_card()] == [job["job_id"]]


def test_bare_confirm_after_expiry_gets_honest_reply():
    """The interceptor path passes NO args on confirm; a claim miss must not
    surface a raw 'Unknown archetype' validation error."""
    out = _tool(confirmed=True)  # no pending, no archetype, no brief
    assert out.startswith("WRITE_BLOCKED")
    assert "expired" in out
    assert "Unknown job archetype" not in out


def test_interceptor_yes_delegate_it_confirms():
    """'yes delegate it' / 'queue it' are deterministic confirms for a pending
    job preview -- never a model fall-through (phantom-queue class)."""
    _tool(action="request", archetype="research_brief",
          brief="delegate-verb confirm brief long enough")
    reply = td.try_confirm_pending_write(
        slack_user_id=USER, channel_name=CHANNEL, entity="F3E",
        message="yes delegate it")
    assert reply is not None and "Queued (dw-" in reply

    td._PENDING_DELEGATED_WORK.clear()
    _tool(action="request", archetype="doc_draft",
          brief="queue-verb confirm brief long enough")
    reply = td.try_confirm_pending_write(
        slack_user_id=USER, channel_name=CHANNEL, entity="F3E",
        message="queue it")
    assert reply is not None and "Queued (dw-" in reply


def test_queue_verb_conflicts_with_other_stores():
    """'queue it' while a Shopify write is pending names a DIFFERENT action ->
    ambiguous -> model fall-through (never fires the staler write)."""
    td._PENDING_SHOPIFY_WRITES[td._shopify_pending_key(USER, CHANNEL)] = {
        "ts": time.time(), "variant_label": "x",
    }
    try:
        reply = td.try_confirm_pending_write(
            slack_user_id=USER, channel_name=CHANNEL, entity="F3E",
            message="queue it")
        assert reply is None
    finally:
        td._PENDING_SHOPIFY_WRITES.clear()


def test_confirm_executes_stashed_entity_not_live_entity():
    """Stash purity: the entity the preview showed is the entity that runs."""
    _tool(action="request", archetype="doc_draft", entity="F3E",
          brief="stashed entity purity brief long enough")
    out = td._tool_cora_delegate_work(USER, "OSN", {
        "confirmed": True, "_channel_name": CHANNEL, "_channel_id": CHANNEL_ID,
    })
    assert out.startswith("WRITE_CONFIRMED")
    assert dw.load_jobs()[0]["entity"] == "F3E"  # the previewed scope, not OSN


def test_render_job_list_filters_to_requester():
    _submit(user=USER, brief="my own listed job brief long enough")
    _submit(user=OTHER, brief="someone elses job brief long enough")
    mine = dw.render_job_list(USER, CHANNEL_ID)
    other_job = [r for r in dw.load_jobs() if r["requester"] == OTHER][0]
    assert other_job["job_id"] not in mine
    assert mine.count("dw-") == 1
