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

# The autouse _isolated fixture stubs user_access.check_access to always pass.
# Captured here at import so a test that needs the REAL topic block can restore
# it (D-051 2026-08-07: a "regression pin" that runs against the stub is pinning
# nothing -- it stayed green with the fix reverted).
import cora.user_access as _ua
_REAL_CHECK_ACCESS = _ua.check_access
# Same reason for the PHI predicates: the fixture stubs them to a benign False,
# so a test asserting REAL detection behaviour must restore them explicitly.
import cora.phi_guard as _pg
_REAL_PERSON_LINKED = _pg.is_phi_risk_person_linked
_REAL_PHI_RISK = _pg.is_phi_risk

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
    monkeypatch.setattr(phi_guard, "is_phi_risk_person_linked", lambda text: False)
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
    # cq-a24f9d2210fc (2026-08-07): a BRIEF is request-shaped text, so this
    # screen now uses is_phi_risk_person_linked -- is_phi_risk itself carries
    # bare payer/programme names ("AHCCCS") for filename/subject triage, and
    # that single token refused three live person-free policy briefs. Stub the
    # predicate the path actually calls, or the pin tests nothing.
    import cora.phi_guard as phi_guard
    monkeypatch.setattr(phi_guard, "is_phi_risk_person_linked", lambda text: True)
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

    monkeypatch.setattr(phi_guard, "is_phi_risk_person_linked", _boom)
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
    monkeypatch.setattr(phi_guard, "is_phi_risk_person_linked", lambda text: True)
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
    monkeypatch.setattr(phi_guard, "is_phi_risk_person_linked", lambda text: True)
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


class TestR2IntakeAuthorization:
    """R2 (Harrison ruling 2026-08-07): intake asks 'is this REQUESTER authorized
    for this TOPIC' -- a question about the human. It was passing the WORKER's
    phi_custodian=False retrieval pin, which conflated two things and made the
    lane refuse its own use case."""

    LEX_POLICY_BRIEF = ("research the DDD provider revalidation requirements and "
                        "summarize what our agency must submit")

    def test_custodian_lex_policy_brief_passes_intake(self, monkeypatch, _lex_lane):
        """THE R2 REGRESSION, against the REAL topic block.

        Shaun is a LEX PHI custodian; his DDD-topic policy brief must queue.
        Before R2 this drew "Client-specific health info stays in the EHR."
        The autouse fixture stubs check_access to always pass, which would make
        this pin nothing -- so the real implementation is restored for this one
        test (verified: with R2 reverted, this FAILS)."""
        import cora.org_roles as org_roles
        import cora.user_access as user_access
        monkeypatch.setattr(user_access, "check_access", _REAL_CHECK_ACCESS)
        monkeypatch.setattr(org_roles, "get_role",
                            lambda uid: _role(entity="LEX-LLC"))
        assert dw.screen_request(
            "U0B3PS82G30", "LEX-LLC", "llc-leadership", "research_brief",
            self.LEX_POLICY_BRIEF, "md") is None

    def test_intake_passes_the_requesters_REAL_custodian_status(
        self, monkeypatch, _lex_lane
    ):
        """The load-bearing assertion. The autouse fixture stubs check_access to
        always pass, so assert on the ARGUMENT rather than the outcome: intake
        must hand user_access the requester's true custodian status, not the
        worker's pin. Reverting to phi_custodian=False fails this."""
        import cora.org_roles as org_roles
        import cora.user_access as user_access
        monkeypatch.setattr(org_roles, "get_role",
                            lambda uid: _role(entity="LEX-LLC"))
        seen = {}
        monkeypatch.setattr(
            user_access, "check_access",
            lambda uid, ent, txt, phi_custodian=None, tier=None: seen.update(
                phi_custodian=phi_custodian) or None)
        # Shaun IS a custodian in the real lex-phi-custodians.yaml.
        dw.screen_request("U0B3PS82G30", "LEX-LLC", "llc-leadership",
                          "research_brief", self.LEX_POLICY_BRIEF, "md")
        assert seen["phi_custodian"] is True

    def test_a_non_custodian_requester_is_passed_through_as_false(
        self, monkeypatch, _lex_lane
    ):
        # The flip is a SCOPED ALLOW, not a guard removal: someone not on
        # lex-phi-custodians.yaml still reaches user_access as a non-custodian,
        # so their topic block stands.
        import cora.org_roles as org_roles
        import cora.user_access as user_access
        monkeypatch.setattr(org_roles, "get_role",
                            lambda uid: _role(entity="LEX-LLC"))
        seen = {}
        monkeypatch.setattr(
            user_access, "check_access",
            lambda uid, ent, txt, phi_custodian=None, tier=None: seen.update(
                phi_custodian=phi_custodian) or None)
        dw.screen_request("U_NOT_A_CUSTODIAN", "LEX-LLC", "llc-leadership",
                          "research_brief", self.LEX_POLICY_BRIEF, "md")
        assert seen["phi_custodian"] is False

    def test_client_identifying_brief_refuses_for_a_custodian_too(
        self, monkeypatch, _lex_lane
    ):
        # Content containment is unchanged: authorization went up, the CONTENT
        # gate did not move. A custodian still cannot queue a client brief.
        import cora.org_roles as org_roles
        monkeypatch.setattr(org_roles, "get_role",
                            lambda uid: _role(entity="LEX-LLC"))
        refusal = dw.screen_request(
            "U0B3PS82G30", "LEX-LLC", "llc-leadership", "research_brief",
            "research whether client Marcus Delgado qualifies for respite units",
            "md")
        assert refusal and "names a specific person" in refusal
        # ...and the copy now states the TRUE reason (the worker cannot read
        # those records), not an implied slight on the requester's access.
        assert "non-custodian by design" in refusal

    def test_intake_resolution_is_fail_closed(self, monkeypatch, _lex_lane):
        # A custodian-check error must read as NON-custodian, never as a pass.
        import cora.lex_phi_access as lex_phi_access
        import cora.org_roles as org_roles
        import cora.user_access as user_access
        monkeypatch.setattr(org_roles, "get_role",
                            lambda uid: _role(entity="LEX-LLC"))
        monkeypatch.setattr(lex_phi_access, "phi_allowed",
                            MagicMock(side_effect=RuntimeError("boom")))
        seen = {}
        monkeypatch.setattr(
            user_access, "check_access",
            lambda uid, ent, txt, phi_custodian=None, tier=None: seen.update(
                phi_custodian=phi_custodian) or None)
        dw.screen_request("U0B3PS82G30", "LEX-LLC", "llc-leadership",
                          "research_brief", self.LEX_POLICY_BRIEF, "md")
        assert seen["phi_custodian"] is False

    # NOTE: the behavioural revert-and-fail pin for the worker retrieval pin
    # lives in tests/test_delegated_worker.py
    # (test_kb_search_lex_job_still_retrieves_as_non_custodian). It was moved
    # there from a source-grep version here that was VACUOUS: the only
    # `phi_custodian` tokens inside make_kb_search are a docstring line and a
    # comment, so the grep passed with the pin genuinely removed -- verified
    # against the full suite (D-051, 2026-08-07).


# ---------------------------------------------------------------------------
# cq-a24f9d2210fc -- DW intake refused person-free DDD policy briefs (HIGH).
# Live evidence 2026-08-07, #llc-leadership / #lts-leadership: three briefs the
# MODEL composed (captured verbatim from cora-2026-08-07.log) were refused with
# a template asserting they "name a specific person". They name nobody.
# ---------------------------------------------------------------------------

# Verbatim from the live tool_use log lines -- not paraphrased. The defect
# shipped "verified" because the previous session tested a hand-fed string
# through the function instead of the text the model actually sends (D-154).
LIVE_REFUSED_BRIEFS = [
    ("09:53", "Research brief on current AZ DDD/AHCCCS provider revalidation "
              "requirements for Provider Type 15: what Lexington LLC must submit, "
              "the APEP (AHCCCS Provider Enrollment Portal) process steps, and "
              "the 2026 revalidation fee."),
    ("09:54", "Research Arizona DDD Provider (Qualified Vendor) revalidation "
              "requirements. Summarize what Lexington LLC must submit to AHCCCS "
              "and DDD to maintain provider credentials and billing privileges. "
              "Include deadline, submission process, required documentation, and "
              "any recent changes. Focus on Provider Type 14 (Therapy/HCBS) "
              "requirements specifically."),
    ("10:11", "Research DDD provider revalidation requirements for Lexington LLC "
              "(Qualified Vendor). Focus on: what documents and attestations must "
              "be submitted, timeline/deadlines, the revalidation process via "
              "AHCCCS APEP, required staff credentials and continuing education, "
              "any compliance cross-validation requirements from DDD, and what "
              "happens if revalidation is not completed. Summarize in plain "
              "language what our agency must do to maintain Provider Type 14 and "
              "15 IDs."),
]


class TestDwIntakePrecision:
    def _screen(self, monkeypatch, brief, user="U0B2RM2JYJ1"):
        import cora.org_roles as org_roles
        import cora.user_access as user_access
        monkeypatch.setenv("CORA_DELEGATED_WORK_LEX", "on")
        monkeypatch.setattr(dw, "_staff_names_cache",
                            {"Shaun Hawkins", "Jennifer Mortensen"})
        monkeypatch.setattr(org_roles, "get_role",
                            lambda uid: _role(entity="LEX-LLC"))
        # Real topic block, not the autouse stub -- this path must be exercised
        # end to end or the pin is decorative (D-151).
        monkeypatch.setattr(user_access, "check_access", _REAL_CHECK_ACCESS)
        import cora.phi_guard as phi_guard
        monkeypatch.setattr(phi_guard, "is_phi_risk_person_linked",
                            _REAL_PERSON_LINKED)
        return dw.screen_request(user, "LEX-LLC", "llc-leadership",
                                 "research_brief", brief, "md")

    @pytest.mark.parametrize("when,brief", LIVE_REFUSED_BRIEFS)
    def test_live_person_free_policy_briefs_now_queue(self, monkeypatch, when, brief):
        assert self._screen(monkeypatch, brief) is None, (
            f"the {when} live brief is still refused")

    @pytest.mark.parametrize("brief", [
        "research whether participant Marcus Delgado qualifies for respite units",
        "summarize participant Aaron's authorization history",
        "draft a memo about Marcus Delgado's service authorization renewal",
        "brief on client Gilbert current placement status",
    ])
    def test_person_named_briefs_still_refuse(self, monkeypatch, brief):
        out = self._screen(monkeypatch, brief)
        assert out and "names a specific person" in out

    @pytest.mark.parametrize("brief", [
        "research the diagnosis and medication documentation standards we keep",
        "pull the member id and npi needed for the revalidation packet",
    ])
    def test_clinical_or_identifier_briefs_refuse_with_TRUE_copy(
        self, monkeypatch, brief
    ):
        """The copy must name what actually fired. One template for every hit is
        how a false claim shipped: all three live refusals asserted a person was
        named in briefs naming nobody."""
        out = self._screen(monkeypatch, brief)
        assert out
        assert "clinical or identifier" in out
        assert "names a specific person" not in out, (
            "asserted a person-detection that did not fire")

    @pytest.mark.parametrize("brief", [
        # Case must NEVER gate a care-noun-governed name. An earlier cut of this
        # fix required the governed name to be capitalised, which was a
        # self-inflicted egress miss -- people type lowercase in Slack all day,
        # and "client marcus delgado" then sailed through to the search API.
        "research the respite rules for client marcus delgado",
        "research the placement options for client madison",
        "research eligibility for participant aaron",
    ])
    def test_lowercase_governed_names_still_refuse(self, monkeypatch, brief):
        out = self._screen(monkeypatch, brief)
        assert out and "names a specific person" in out

    @pytest.mark.parametrize("phrase", [
        "member id", "client record", "participant roster", "member number",
    ])
    def test_record_nouns_after_a_care_noun_are_not_people(self, phrase):
        """The narrow precision case the record-noun set exists for: these
        follow a care-recipient noun but name nobody."""
        from cora import phi_guard
        assert phi_guard.has_care_context_person_name(
            f"what does the DDD manual say about the {phrase} field",
            {"Shaun Hawkins"}) is False

    def test_ingestion_screen_is_deliberately_unchanged(self, monkeypatch):
        """is_phi_risk still carries the payer/programme names. It guards email
        SUBJECTS and Drive FILENAMES before KB ingestion, where recall beats
        precision -- only the request-shaped screen dropped them."""
        from cora import phi_guard
        monkeypatch.setattr(phi_guard, "is_phi_risk", _REAL_PHI_RISK)
        monkeypatch.setattr(phi_guard, "is_phi_risk_person_linked",
                            _REAL_PERSON_LINKED)
        assert phi_guard.is_phi_risk("AHCCCS eligibility letter.pdf") is True
        assert phi_guard.is_phi_risk("Medicaid renewal packet") is True
        # ...and the request-shaped screen does NOT fire on a bare programme name.
        assert phi_guard.is_phi_risk_person_linked(
            "what are the AHCCCS revalidation steps") is False
        # ...while still firing on anything person-linked.
        assert phi_guard.is_phi_risk_person_linked("send me the member id") is True

    def test_non_lex_intake_uses_the_same_request_shaped_screen(self, monkeypatch):
        """NOT 'byte-identical' -- an earlier docstring claimed that and it was
        false: the non-LEX union changed too (is_any_phi -> person_linked u
        clinical u billing), so a non-LEX brief mentioning Medicaid POLICY now
        queues where it used to refuse. That is the intended behaviour; pin it
        honestly, against the REAL predicates rather than the fixture stubs."""
        import cora.org_roles as org_roles
        import cora.phi_guard as phi_guard
        monkeypatch.setattr(org_roles, "get_role", lambda uid: _role(entity="F3E"))
        monkeypatch.setattr(phi_guard, "is_phi_risk_person_linked",
                            _REAL_PERSON_LINKED)
        # A programme name is a topic in F3E scope too.
        assert dw.screen_request(
            USER, "F3E", CHANNEL, "research_brief",
            "research how Medicaid policy shifts could change our retail base",
            "md") is None
        # ...and person-linked content still refuses outside LEX.
        refusal = dw.screen_request(
            USER, "F3E", CHANNEL, "research_brief",
            "research the diagnosis documentation our clinic must retain", "md")
        assert refusal and "protected client/health info" in refusal

    @pytest.mark.parametrize("brief,must_say,must_not_say", [
        # Each branch gets its OWN message. Collapsing them is how a false claim
        # shipped twice (D-051 MED-4).
        ("research the client eligibility backlog reporting we owe DDD quarterly",
         "billing, authorization or eligibility", "clinical or identifier"),
        ("research the diagnosis documentation standards we must retain",
         "clinical or identifier", "billing, authorization or eligibility"),
        ("research whether participant Marcus Delgado qualifies for respite",
         "names a specific person", "clinical or identifier"),
    ])
    def test_each_refusal_names_only_what_fired(self, monkeypatch, brief,
                                                must_say, must_not_say):
        out = self._screen(monkeypatch, brief)
        assert out, brief
        assert must_say in out
        assert must_not_say not in out


class TestDwIntakeRecallClasses:
    """Recall classes a D-051 review measured as LOST by the first cut of the
    precision fix. Every one is live-reachable (all three lane flags are on),
    on BOTH the DW intake path and the web egress path -- so each is pinned
    against the predicates directly rather than through one caller."""

    STAFF = {"Shaun Hawkins", "Jennifer Mortensen"}

    @pytest.fixture(autouse=True)
    def _real_predicates(self, monkeypatch):
        # The module-level _isolated fixture stubs these to a benign False.
        # A recall pin that runs against the stub proves nothing.
        import cora.phi_guard as phi_guard
        monkeypatch.setattr(phi_guard, "is_phi_risk_person_linked",
                            _REAL_PERSON_LINKED)
        monkeypatch.setattr(phi_guard, "is_any_phi", _pg.is_any_phi)
        yield

    def _caught(self, text):
        from cora import phi_guard
        return (phi_guard.is_phi_risk_person_linked(text)
                or phi_guard.is_clinical_phi(text)
                or phi_guard.is_lex_billing_status_phi(text)
                or phi_guard.has_care_context_person_name(
                    text, self.STAFF, cue_required=False))

    @pytest.mark.parametrize("text", [
        # str.rstrip takes a CHARACTER SET: rstrip("'’s") ate the name's own
        # trailing s, so the possessive branch was dead for every -s name --
        # Marcus, James, Williams, Davis, Harris, Jones, Rogers...
        "Marcus's home address",
        "James's respite schedule",
        "Williams's authorization renewal",
        "Delgado's home address",          # control: non-s name always worked
    ])
    def test_possessive_names_are_caught(self, text):
        assert self._caught(text), text

    @pytest.mark.parametrize("text", [
        # A Medicaid/AHCCCS beneficiary number IS a HIPAA identifier, and the
        # programme name is its only marker -- _PHI_PATTERNS carries the literal
        # "member id"/"provider id" but nothing for these.
        "the client's Medicaid ID is 1234567 -- research what we file with it",
        "research the appeal path for AHCCCS ID 84213365",
        "look up Medicaid number 900123 for the packet",
    ])
    def test_programme_own_identifiers_are_caught(self, text):
        assert self._caught(text), text

    @pytest.mark.parametrize("text", [
        # A lone name tight against a care cue is person-evidence; a lone
        # capitalised word in long policy prose is not.
        "research respite units available for Madison",
        "pull the respite auth file for Delgado",
        # ...and a known non-person token must not SPLIT a real name or
        # suppress the one beside it (adding stopwords was converting 2-token
        # hits into 1-token misses).
        "Provider Madison Delgado respite units",
        "Lexington Madison respite hours",
    ])
    def test_lone_and_adjacent_names_are_caught(self, text):
        assert self._caught(text), text

    @pytest.mark.parametrize("text", [
        "Research brief on current AZ DDD/AHCCCS provider revalidation "
        "requirements for Provider Type 15: what Lexington LLC must submit, the "
        "APEP (AHCCCS Provider Enrollment Portal) process steps, and the 2026 "
        "revalidation fee.",
        "Research Arizona DDD Provider (Qualified Vendor) revalidation "
        "requirements. Summarize what Lexington LLC must submit to AHCCCS and "
        "DDD to maintain provider credentials and billing privileges. Include "
        "deadline, submission process, required documentation, and any recent "
        "changes. Focus on Provider Type 14 (Therapy/HCBS) requirements "
        "specifically.",
    ])
    def test_the_live_briefs_stay_clean_under_every_widening(self, text):
        """The precision side of the same frontier: each recall fix above was
        re-checked against the real briefs. 'Include' sat 20 chars from the cue
        'billing' and re-broke brief 2 when the tight window was added."""
        assert not self._caught(text)


# ---------------------------------------------------------------------------
# cq-d30815ee6993 -- two live residuals, 2026-08-07 evening.
# ---------------------------------------------------------------------------

class TestRaForcedDelegateTool:
    """R-A: an explicit 'delegate a job: <person>'s eligibility status' ask in
    #lts-leadership PREVIEWED a job. VERIFY-FIRST OVERTURNED THE PREMISE -- the
    screen did not fail, it never RAN: the log holds six cora_delegate_work
    calls that day and NONE from LEX-TS, while the asks 3s before and 2min after
    it both produced one. A narrated preview with no tool call bypasses every
    deterministic guard behind the tool, so the fix is to force the tool."""

    def test_the_screen_would_have_refused_all_three_phrasings(self):
        """Proves the defect is upstream of the screen, not in it -- including
        the model's own 'for the named individual' paraphrase."""
        from cora import phi_guard
        for brief in (
            "research brief on Maria Gonzalez's AHCCCS eligibility renewal status",
            "Research Maria Gonzalez's AHCCCS eligibility renewal status.",
            "Research the current AHCCCS eligibility renewal status for the "
            "named individual.",
        ):
            assert phi_guard.is_lex_billing_status_phi(brief), brief

    @pytest.mark.parametrize("text", [
        "delegate a job: research brief on Maria Gonzalez's eligibility status",
        "delegate a job: research the DDD provider revalidation requirements",
        "Delegate: put together a brief on Sprouts",
        "can you run a background job for this",
        "queue a research brief on the AZ DDD respite rates",
    ])
    def test_explicit_delegation_forces_the_tool(self, text):
        from cora import app
        assert app._delegate_work_intent(text) is True

    @pytest.mark.parametrize("text", [
        # A hand-off to a HUMAN is not a worker job.
        "delegate that to Shaun", "delegate it to Jen please",
        "we should delegate more work", "what jobs are running?",
        "cancel my job", "yes", "", "add a task to research the rates",
    ])
    def test_ordinary_phrasings_do_not_force(self, text):
        from cora import app
        assert app._delegate_work_intent(text) is False

    def test_forcing_is_wired_above_the_asana_force(self):
        """'delegate a job: ...' is a worker hand-off, not a task op -- and the
        force must reach force_tool, which is what makes the intake screens run."""
        src = (REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
        seg = src[src.index("if _code_queue_capture_intent(user_message):"):
                  src.index("# Staged-WRITE escalation")]
        seg = "\n".join(l for l in seg.splitlines() if not l.lstrip().startswith("#"))
        assert 'elif _delegate_work_intent(user_message):' in seg
        assert 'force_tool = "cora_delegate_work"' in seg
        assert seg.index("_delegate_work_intent") < seg.index("_asana_destructive_intent")

    def test_the_forced_tool_is_exposed_everywhere(self):
        """tool_choice must never name an unexposed tool -- and action=request
        FILES NOTHING, which is what makes forcing it safe."""
        from cora.tools import tool_dispatch as td
        assert "cora_delegate_work" in td._GLOBAL_CORE_TOOLS
        for entity in ("LEX-LTS", "LEX-LLC", "F3E", "FNDR"):
            names = [t["name"] for t in td.tools_for_entity(entity, cross_entity=False)]
            assert "cora_delegate_work" in names, entity


class TestRbCompoundAdjective:
    """R-B: the SAME person-free topic refused in one phrasing and previewed in
    another. The trigger was the model's own disclaimer -- 'No client-specific
    or PHI content needed' -- read as client PHI by BOTH predicates. A
    hyphenated compound adjective is one word and names no individual."""

    STAFF = {"Shaun Hawkins", "Jennifer Mortensen"}
    REFUSED = ("Research brief on current AZ DDD/AHCCCS provider revalidation "
               "requirements for Provider Type 15: what Lexington LLC must submit, "
               "the APEP (AHCCCS Provider Enrollment Portal) process steps "
               "end-to-end, and any 2026 revalidation fee. Context: LLC has active "
               "revalidation deadlines under AHCCCS for its Provider Type 15 "
               "service-site IDs (hard portfolio deadline 2026-06-30, with related "
               "Provider Type 13/14 notices also circulating through 2026-08-31). "
               "No client-specific or PHI content needed -- this is a "
               "policy/process brief.")
    PREVIEWED = ("Research DDD provider revalidation requirements for Lexington LLC "
                 "and summarize what our agency must submit to maintain AHCCCS "
                 "Provider Type 14 enrollment. Include deadlines, required "
                 "documentation, submission process, and any recent changes or "
                 "updates from AZ DDD/AHCCCS.")

    def _any(self, text):
        from cora import phi_guard
        return (phi_guard.is_lex_billing_status_phi(text)
                or phi_guard.is_phi_risk_person_linked(text)
                or phi_guard.is_clinical_phi(text)
                or phi_guard.has_care_context_person_name(text, self.STAFF))

    def test_both_phrasings_of_the_same_topic_now_agree(self):
        assert not self._any(self.REFUSED), "the was-refusing phrasing"
        assert not self._any(self.PREVIEWED), "the was-previewing phrasing"

    @pytest.mark.parametrize("text", [
        "no client-specific enrollment data needed",
        "member-facing enrollment portal documentation",
        "patient-level billing rollups, de-identified",
    ])
    def test_compound_adjectives_name_no_individual(self, text):
        assert not self._any(text)

    @pytest.mark.parametrize("text", [
        # D-050 recall, including the exact live miss the doctrine was created
        # for. The compound exclusion must not touch any of these.
        "Bob Smith's billing authorization is pending",
        "research whether client Marcus qualifies for respite units",
        "summarize the client's eligibility status",
        "Maria Gonzalez's AHCCCS eligibility renewal status",
        "research units of service for member Delgado",
        "the client is pending discharge",
    ])
    def test_d050_recall_is_untouched(self, text):
        assert self._any(text), text
