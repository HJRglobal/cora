"""The mechanical/judgment review split (cq-6b014816819c) and its expiry rule.

Two properties carry the weight here and neither is a formatting concern:

  D-011 IS STRUCTURAL. A non-Harrison actor can reach the MECHANICAL lane and
  nothing else, and no entry in knowledge-approvers.yaml can express otherwise.
  The tests below try to reach the judgment, decision and operational lanes
  through every route the split opened -- can_approve, the reaction correlation
  and the one-tap handler -- with a listed approver, and each must be refused.

  D-206: NOTHING IS RESOLVED BY A TIMER. Every one of the 89 expired_unrouted
  dismissals on the live ledger was a mechanical row that nobody ever saw.
  Those rows now escalate, and the two passes that used to be able to dismiss
  them (the 48h DM'd-unreacted pass and the 14d unrouted pass) must both leave
  them alone -- otherwise the escalation is undone from the other side.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from cora import knowledge_review as kr
from cora import review_lanes
import scripts.run_knowledge_review as rkr


HARRISON = "U0B2RM2JYJ1"
HANNAH = "U0B3AEQS0NB"
STRANGER = "U_NOBODY"


def _now():
    return datetime.now(timezone.utc)


def _row(update_type="task_close", entity="F3E", state="PENDING", dm_ts="",
         age_days=0.0, expires_in_days=None, uid="u1", **extra):
    row = {
        "update_id": uid,
        "update_type": update_type,
        "description": "d",
        "payload": {"entity": entity},
        "state": state,
        "dm_message_ts": dm_ts,
        "proposed_at": (_now() - timedelta(days=age_days)).isoformat(),
        "resolved_at": None,
        "expires_at": (None if expires_in_days is None else
                       (_now() + timedelta(days=expires_in_days)).isoformat()),
    }
    row.update(extra)
    return row


@pytest.fixture(autouse=True)
def _fresh_approver_cache():
    review_lanes.reset_cache()
    yield
    review_lanes.reset_cache()


@pytest.fixture
def granted():
    """The state AFTER Harrison makes the grant -- Hannah on the mechanical
    lane. Nothing in the repo ships this; it is the flip under test."""
    with patch.object(review_lanes, "_load_mechanical_approvers",
                      return_value=(HARRISON, HANNAH)):
        yield


# ── lane classification ──────────────────────────────────────────────────────

@pytest.mark.parametrize("utype,expected", [
    ("task_close", review_lanes.LANE_MECHANICAL),
    ("asana_task", review_lanes.LANE_MECHANICAL),
    ("hubspot_note", review_lanes.LANE_MECHANICAL),
    ("known_answer", review_lanes.LANE_JUDGMENT),
    ("efficiency", review_lanes.LANE_JUDGMENT),
    ("lexicon", review_lanes.LANE_JUDGMENT),
    ("decision_capture", review_lanes.LANE_DECISION),
    ("generic", review_lanes.LANE_OPERATIONAL),
])
def test_lane_classification(utype, expected):
    assert review_lanes.lane_for(utype, {}) == expected


def test_an_info_for_cora_generic_is_judgment_not_operational():
    """A human-fed fact rides the knowledge stream. Shares its definition with
    knowledge_review.is_knowledge_update so the two cannot drift."""
    assert review_lanes.lane_for("generic", {"source": "info-for-cora"}) == \
        review_lanes.LANE_JUDGMENT


def test_an_unknown_type_is_operational_not_mechanical():
    """A new update_type must be classified on purpose. Defaulting it into a
    delegable surface is the failure this shape prevents."""
    assert review_lanes.lane_for("some_future_type", {}) == review_lanes.LANE_OPERATIONAL
    assert review_lanes.can_approve(_row(update_type="some_future_type"), HANNAH) is False


# ── D-011: the judgment/decision lanes are Harrison-only in CODE ─────────────

@pytest.mark.parametrize("utype", ["known_answer", "efficiency", "lexicon",
                                   "decision_capture", "generic"])
def test_a_listed_approver_cannot_approve_a_non_mechanical_item(granted, utype):
    assert review_lanes.can_approve(_row(update_type=utype), HANNAH) is False
    assert review_lanes.can_approve(_row(update_type=utype), HARRISON) is True


def test_a_listed_approver_can_approve_a_mechanical_item(granted):
    assert review_lanes.can_approve(_row(update_type="asana_task"), HANNAH) is True


def test_a_stranger_can_approve_nothing(granted):
    for utype in ("task_close", "known_answer", "decision_capture"):
        assert review_lanes.can_approve(_row(update_type=utype), STRANGER) is False


def test_an_empty_actor_is_refused():
    assert review_lanes.can_approve(_row(), "") is False


def test_lex_mechanical_items_are_harrison_only_whoever_is_listed(granted):
    """PHI. Owner-routing has never sent a LEX item to a teammate; delegating
    this surface must not become the way one gets there."""
    for entity in ("LEX", "LEX-LLC", "LEX-LBHS"):
        row = _row(update_type="task_close", entity=entity)
        assert review_lanes.can_approve(row, HANNAH) is False
        assert review_lanes.can_approve(row, HARRISON) is True


# ── the approver file ────────────────────────────────────────────────────────

def test_the_shipped_file_grants_nobody_but_harrison():
    """The whole point of 'build the surface, grant nothing'. If this fails, a
    grant landed in the repo instead of in Harrison's own edit."""
    assert review_lanes.mechanical_approvers() == (HARRISON,)


def test_an_unreadable_file_fails_closed_to_harrison(tmp_path):
    bad = tmp_path / "nope.yaml"
    bad.write_text("mechanical: [oh: no: :\n", encoding="utf-8")
    with patch.object(review_lanes, "_APPROVERS_PATH", bad):
        review_lanes.reset_cache()
        assert review_lanes.mechanical_approvers() == (HARRISON,)


def test_a_missing_file_fails_closed_to_harrison(tmp_path):
    with patch.object(review_lanes, "_APPROVERS_PATH", tmp_path / "absent.yaml"):
        review_lanes.reset_cache()
        assert review_lanes.mechanical_approvers() == (HARRISON,)


def test_harrison_is_added_even_if_the_file_omits_him(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text("mechanical:\n  - slack_id: %s\n" % HANNAH, encoding="utf-8")
    with patch.object(review_lanes, "_APPROVERS_PATH", f):
        review_lanes.reset_cache()
        assert review_lanes.mechanical_approvers() == (HARRISON, HANNAH)


def test_bare_ids_and_mapping_rows_both_parse(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text("mechanical:\n  - %s\n  - slack_id: U_X\n" % HANNAH, encoding="utf-8")
    with patch.object(review_lanes, "_APPROVERS_PATH", f):
        review_lanes.reset_cache()
        assert review_lanes.mechanical_approvers() == (HARRISON, HANNAH, "U_X")


# ── reaction correlation authorizes per ITEM, not per reactor ────────────────

def _correlate(updates, reactions):
    with patch.object(kr, "load_proposed_updates", return_value=updates), \
         patch.object(kr, "load_reply_log", return_value=reactions):
        return kr.correlate_reactions_to_updates()


def _reaction(reactor, ts="100.1", action="APPROVED"):
    return {"reactor_id": reactor, "event_type": "reaction_added",
            "action": action, "message_ts": ts}


def test_harrison_still_correlates_on_every_lane():
    for utype in ("known_answer", "task_close", "decision_capture"):
        pairs = _correlate([_row(update_type=utype, dm_ts="100.1")],
                           [_reaction(HARRISON)])
        assert len(pairs) == 1, utype


def test_a_listed_approver_correlates_on_a_mechanical_card(granted):
    pairs = _correlate([_row(update_type="task_close", dm_ts="100.1")],
                       [_reaction(HANNAH)])
    assert len(pairs) == 1


def test_a_listed_approver_does_not_correlate_on_a_judgment_card(granted):
    pairs = _correlate([_row(update_type="known_answer", dm_ts="100.1")],
                       [_reaction(HANNAH)])
    assert pairs == []


def test_a_listed_approver_does_not_correlate_on_a_lex_mechanical_card(granted):
    pairs = _correlate([_row(update_type="task_close", entity="LEX-LLC", dm_ts="100.1")],
                       [_reaction(HANNAH)])
    assert pairs == []


def test_a_strangers_reaction_is_ignored(granted):
    pairs = _correlate([_row(update_type="task_close", dm_ts="100.1")],
                       [_reaction(STRANGER)])
    assert pairs == []


def test_an_unauthorized_reaction_does_not_block_an_authorized_one(granted):
    """Several people can react to the same card. The FIRST AUTHORIZED one
    wins -- an earlier unauthorized reaction must not consume the slot."""
    pairs = _correlate([_row(update_type="known_answer", dm_ts="100.1")],
                       [_reaction(HANNAH), _reaction(HARRISON)])
    assert len(pairs) == 1
    assert pairs[0][1]["reactor_id"] == HARRISON


def test_a_resolved_row_never_correlates():
    pairs = _correlate([_row(state="APPROVED", dm_ts="100.1")], [_reaction(HARRISON)])
    assert pairs == []


# ── the one-tap button handler ───────────────────────────────────────────────

def test_a_non_approver_tap_cannot_probe_for_existence():
    """not_found vs not_authorized would otherwise tell a stranger whether an
    update_id is real. The pre-check runs before the lookup."""
    outcome, _msg = kr.process_one_tap_action("anything", STRANGER, approve=True)
    assert outcome == "not_authorized"


def test_a_mechanical_tap_is_refused_rather_than_misapplied(granted):
    """apply_knowledge_update has no branch for these types, so a button here
    would report a save that no writer performed. Nothing renders one; a stale
    or forged tap gets an honest answer."""
    with patch.object(kr, "_find_update", return_value=_row(update_type="task_close")), \
         patch.object(kr, "apply_knowledge_update") as apply_fn:
        outcome, msg = kr.process_one_tap_action("u1", HANNAH, approve=True)
    apply_fn.assert_not_called()
    assert outcome == "not_authorized"
    assert "mechanical review surface" in msg


def test_a_listed_approver_cannot_tap_a_judgment_card(granted):
    with patch.object(kr, "_find_update", return_value=_row(update_type="known_answer")), \
         patch.object(kr, "apply_knowledge_update") as apply_fn:
        outcome, _msg = kr.process_one_tap_action("u1", HANNAH, approve=True)
    apply_fn.assert_not_called()
    assert outcome == "not_authorized"


# ── D-206: escalation replaces the silent age-out ───────────────────────────

def test_a_past_deadline_mechanical_row_escalates_instead_of_dismissing():
    now = _now()
    e = _row(expires_in_days=-1, age_days=20)
    newly, overdue = rkr._escalate_stale_mechanical([e], now)
    assert (newly, overdue) == (1, 1)
    assert e["state"] == "PENDING", "an undecided item must never be resolved by a timer"
    assert e["escalation_count"] == 1
    assert e["escalated_at"]


def test_escalation_clears_the_dm_ts_so_the_item_re_cards():
    now = _now()
    e = _row(expires_in_days=-1, dm_ts="100.1")
    rkr._escalate_stale_mechanical([e], now)
    assert e["dm_message_ts"] == ""


def test_a_row_inside_its_deadline_is_untouched():
    now = _now()
    e = _row(expires_in_days=3)
    assert rkr._escalate_stale_mechanical([e], now) == (0, 0)
    assert "escalation_count" not in e


def test_re_escalation_is_rate_limited_but_still_counted_as_overdue():
    now = _now()
    e = _row(expires_in_days=-30)
    e["escalated_at"] = (now - timedelta(days=1)).isoformat()
    e["escalation_count"] = 1
    newly, overdue = rkr._escalate_stale_mechanical([e], now)
    assert newly == 0, "must not re-card daily"
    assert overdue == 1, "but the backlog count must still see it"
    assert e["escalation_count"] == 1


def test_re_escalation_fires_again_after_the_interval():
    now = _now()
    e = _row(expires_in_days=-30, dm_ts="1.1")
    e["escalated_at"] = (now - timedelta(
        days=rkr._MECHANICAL_ESCALATION_INTERVAL_DAYS + 1)).isoformat()
    e["escalation_count"] = 1
    newly, _overdue = rkr._escalate_stale_mechanical([e], now)
    assert newly == 1
    assert e["escalation_count"] == 2


def test_a_row_with_no_expires_at_uses_the_legacy_window():
    now = _now()
    old = _row(age_days=rkr._OPERATIONAL_UNROUTED_EXPIRY_DAYS + 1)
    young = _row(age_days=1)
    assert rkr._escalate_stale_mechanical([old], now)[1] == 1
    assert rkr._escalate_stale_mechanical([young], now)[1] == 0


def test_a_malformed_timestamp_never_escalates_and_never_raises():
    e = _row()
    e["expires_at"] = "not-a-date"
    e["proposed_at"] = "also-not-a-date"
    assert rkr._escalate_stale_mechanical([e], _now()) == (0, 0)
    assert e["state"] == "PENDING"


def test_a_judgment_row_is_not_escalated_by_the_mechanical_pass():
    e = _row(update_type="known_answer", expires_in_days=-5)
    assert rkr._escalate_stale_mechanical([e], _now()) == (0, 0)


# ── ...and both dismissal passes must leave mechanical rows alone ───────────

def test_the_unrouted_expiry_pass_no_longer_dismisses_mechanical_rows():
    now = _now()
    e = _row(expires_in_days=-1, age_days=30)
    assert rkr._auto_expire_unrouted_operational(
        [e], now - timedelta(days=14), now) == 0
    assert e["state"] == "PENDING"


def test_the_unrouted_expiry_pass_still_dismisses_a_bare_generic():
    """The change is scoped to the measured population. A non-info-for-cora
    generic keeps today's behaviour -- it has never produced one of these rows,
    and narrowing further would be a change nothing asked for."""
    now = _now()
    e = _row(update_type="generic", age_days=30)
    assert rkr._auto_expire_unrouted_operational(
        [e], now - timedelta(days=14), now) == 1
    assert e["state"] == "DISMISSED"


def test_the_48h_dmd_unreacted_pass_no_longer_dismisses_mechanical_rows():
    """The other side of the same age-out. Mechanical rows only acquire a
    dm_message_ts now that they have a surface, and this pass would then
    dismiss them 48h later -- silently undoing the escalation."""
    now = _now()
    e = _row(dm_ts="100.1", age_days=10)
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(days=2), now) == 0
    assert e["state"] == "PENDING"


def test_the_48h_pass_still_dismisses_a_judgment_row():
    now = _now()
    e = _row(update_type="known_answer", dm_ts="100.1", age_days=10)
    assert rkr._auto_dismiss_stale_pending([e], now - timedelta(days=2), now) == 1


# ── the surface is off until Harrison turns it on ───────────────────────────

def test_the_sender_is_a_no_op_while_the_flag_is_off(monkeypatch):
    monkeypatch.delenv("CORA_MECHANICAL_REVIEW", raising=False)
    with patch.object(rkr, "send_individual_dms") as send:
        assert rkr._send_mechanical_review_dms(
            [_row()], "xoxb-token", rkr.logging.getLogger("t")) == 0
    send.assert_not_called()


def test_the_flag_only_accepts_on(monkeypatch):
    for value in ("", "off", "1", "true", "yes", "ON!"):
        monkeypatch.setenv("CORA_MECHANICAL_REVIEW", value)
        assert rkr._mechanical_review_enabled() is False
    monkeypatch.setenv("CORA_MECHANICAL_REVIEW", "on")
    assert rkr._mechanical_review_enabled() is True


def _send(items, monkeypatch, approvers=(HARRISON,)):
    monkeypatch.setenv("CORA_MECHANICAL_REVIEW", "on")
    sent: dict = {}

    def _fake_send(batch, _token, _factory=None, block_builder=None, recipient_id=None):
        sent.setdefault(recipient_id, []).extend(batch)
        return {u["update_id"]: "ts-%s" % u["update_id"] for u in batch}

    with patch.object(review_lanes, "_load_mechanical_approvers", return_value=approvers), \
         patch.object(rkr, "send_individual_dms", side_effect=_fake_send), \
         patch.object(rkr, "send_dm_to_harrison"), \
         patch.object(rkr, "_send_dm_to_user"), \
         patch.object(rkr, "_patch_dm_ts"):
        n = rkr._send_mechanical_review_dms(items, "xoxb-token", rkr.logging.getLogger("t"))
    return n, sent


def test_the_send_is_capped_per_run(monkeypatch):
    items = [_row(uid="u%d" % i) for i in range(20)]
    n, sent = _send(items, monkeypatch)
    assert n == rkr._MAX_MECHANICAL_DMS_PER_RUN
    assert len(sent[HARRISON]) == rkr._MAX_MECHANICAL_DMS_PER_RUN


def test_escalated_items_are_sent_first(monkeypatch):
    fresh = _row(uid="fresh", age_days=0)
    stale = _row(uid="stale", age_days=1, escalation_count=2)
    n, sent = _send([fresh, stale], monkeypatch)
    assert n == 2
    assert [u["update_id"] for u in sent[HARRISON]][0] == "stale"


def test_lex_items_go_to_harrison_even_when_a_delegate_exists(monkeypatch):
    lex = _row(uid="lex", entity="LEX-LLC")
    f3e = _row(uid="f3e", entity="F3E")
    _n, sent = _send([lex, f3e], monkeypatch, approvers=(HARRISON, HANNAH))
    assert [u["update_id"] for u in sent[HARRISON]] == ["lex"]
    assert [u["update_id"] for u in sent[HANNAH]] == ["f3e"]


def test_with_no_delegate_everything_goes_to_harrison(monkeypatch):
    _n, sent = _send([_row(uid="a"), _row(uid="b", entity="LEX")], monkeypatch)
    assert set(sent) == {HARRISON}


def test_the_deadline_predicate_is_shared_with_the_dry_run_report():
    """One definition, so the preview an operator reads before flipping the
    flag can never disagree with what the real escalation would do."""
    now = _now()
    overdue = _row(expires_in_days=-1)
    assert rkr._mechanical_past_deadline(overdue, now) is True
    assert rkr._escalate_stale_mechanical([dict(overdue)], now)[1] == 1
    assert rkr._mechanical_past_deadline(_row(expires_in_days=5), now) is False
