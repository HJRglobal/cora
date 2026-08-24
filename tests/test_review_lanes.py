"""The mechanical/judgment review split (cq-6b014816819c) and its expiry rule.

Two properties carry the weight here and neither is a formatting concern:

  D-011 IS STRUCTURAL. A non-Harrison actor can reach the MECHANICAL lane and
  nothing else, and no entry in knowledge-approvers.yaml can express otherwise.
  The tests below try to reach the judgment, decision and operational lanes
  through every route the split opened -- can_approve, the reaction correlation
  and the one-tap handler -- with a listed approver, and each must be refused.

  D-206: NO *MECHANICAL* ROW IS RESOLVED BY A TIMER. (Narrowed from a blanket
  "nothing is" -- a bare non-info-for-cora `generic` still ages out at 14 days
  and is deliberately left that way, so the broader claim was false in this
  file's own headline.) Every one of the 89 expired_unrouted dismissals on the
  live ledger was a mechanical row that nobody ever saw. Those rows now
  escalate, the two passes that could dismiss them must both leave them alone,
  and when the escalation budget runs out the ending is NAMED rather than
  disguised as a decision.
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
def granted(monkeypatch):
    """The state AFTER Harrison makes BOTH halves of the flip -- Hannah listed
    AND the surface enabled (done 2026-08-21). Patched rather than read from the
    live file/env so these tests pin CODE behaviour, not host configuration."""
    monkeypatch.setenv("CORA_MECHANICAL_REVIEW", "on")
    with patch.object(review_lanes, "_load_mechanical_approvers",
                      return_value=(HARRISON, HANNAH)):
        yield


@pytest.fixture
def listed_only(monkeypatch):
    """Half the flip: Hannah listed, surface OFF. Must confer NOTHING -- the
    YAML's own instructions promise it, and before the D-051 review the reaction
    capture in app.py ignored the flag entirely. Still worth pinning after the
    8/21 flip: the flag is the kill switch, and turning it back off must revoke
    the grant rather than leave a half-live surface."""
    monkeypatch.delenv("CORA_MECHANICAL_REVIEW", raising=False)
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


def test_a_listed_approver_confers_nothing_while_the_surface_is_off(listed_only):
    """The two halves of the flip are independent IN CODE, not just in the
    file's instructions (D-051 lens-2 LOW)."""
    assert review_lanes.can_approve(_row(update_type="asana_task"), HANNAH) is False
    assert review_lanes.is_review_approver(HANNAH) is False
    assert review_lanes.is_review_approver(HARRISON) is True


# -- the fail-closed entity rule (the review's headline finding) --------------

def test_an_item_with_NO_entity_is_harrison_only(granted):
    """116 of 124 live PENDING mechanical rows carry no payload.entity -- two of
    the three reconciliation passes never set it. `startswith("LEX")` read that
    absence as "not LEX", i.e. delegable, and two such rows target
    [LEX-LLC] Operations. An unknown entity on a PHI boundary is a no."""
    row = _row(update_type="task_close")
    row["payload"] = {}
    assert review_lanes.can_approve(row, HANNAH) is False
    assert review_lanes.can_approve(row, HARRISON) is True


def test_a_non_dict_payload_is_refused_and_never_raises(granted):
    """can_approve runs inside correlate_reactions_to_updates, the first
    un-wrapped statement of the review run: one malformed row must not take the
    whole run down."""
    row = _row(update_type="task_close")
    row["payload"] = "oops"
    assert review_lanes.item_entity(row) == ""
    assert review_lanes.can_approve(row, HANNAH) is False


def test_lex_named_only_in_the_description_is_refused(granted):
    """The exact live shape: entity absent, "(LEX)" in the prose. Caught by the
    decision lane's content screen, which payload.entity cannot see."""
    row = _row(update_type="hubspot_note", entity="F3E")
    row["description"] = ('Deal "At Your Convenience" mentioned in fireflies '
                          '(LEX) but no HubSpot activity in 7d')
    assert review_lanes.can_approve(row, HANNAH) is False
    assert review_lanes.can_approve(row, HARRISON) is True


def test_a_failing_content_screen_excludes_rather_than_admits(granted):
    with patch.object(review_lanes, "content_screen_excludes",
                      side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            review_lanes.can_approve(_row(update_type="task_close"), HANNAH)
    # ...and the real wrapper turns that into an exclusion rather than a raise
    with patch("cora.decision_inbox.screen_decision", side_effect=RuntimeError("boom")):
        assert review_lanes.content_screen_excludes(_row())[0] is True


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

def test_the_shipped_file_grants_exactly_the_ruled_roster():
    """The shipped roster is Harrison + Hannah and NOBODY else.

    History: this pin used to read `== (HARRISON,)` -- "build the surface, grant
    nothing" -- because on 2026-08-20 the grant was meant to live only in
    Harrison's own working-tree edit. He made that edit on 8/21 (both halves:
    this row and CORA_MECHANICAL_REVIEW=on), which reddened the pin against the
    live host and left the suite failing for three days unnoticed. A grant that
    exists only in an uncommitted working tree is also lost to any fresh clone,
    so the row is now committed and this pin tracks the RULED roster instead.

    It still does the job the original did: a third name landing in the repo
    without a ruling fails here. What it can no longer do is fail merely because
    Harrison exercised authority he already has -- and note that the D-011
    invariants do NOT depend on this list at all: every lane but MECHANICAL is
    Harrison-only in code, pinned above against a fixture roster."""
    assert review_lanes.mechanical_approvers() == (HARRISON, HANNAH)


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
    pairs = _correlate([_row(update_type="task_close", entity="F3E", dm_ts="100.1")],
                       [_reaction(HANNAH)])
    assert len(pairs) == 1


def test_a_reaction_from_another_channel_does_not_correlate():
    """A Slack ts is unique per CHANNEL, not globally, and the reply log now
    collects reactions from more than one conversation."""
    row = _row(dm_ts="100.1")
    row["dm_channel_id"] = "D_HARRISON"
    r = _reaction(HARRISON)
    r["channel_id"] = "C_SOMEWHERE_ELSE"
    assert _correlate([row], [r]) == []
    r["channel_id"] = "D_HARRISON"
    assert len(_correlate([row], [r])) == 1


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
        outcome, msg = kr.process_one_tap_action("u1", HARRISON, approve=True)
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
    newly, overdue, retired = rkr._escalate_stale_mechanical([e], now)
    assert (newly, overdue, retired) == (1, 1, 0)
    assert e["state"] == "PENDING", "an undecided item must never be resolved by a timer"
    assert e["escalation_count"] == 1
    assert e["escalated_at"]


def test_escalation_does_NOT_clear_the_dm_ts():
    """D-051 lens-3 HIGH. A mechanical card carries no buttons, so
    dm_message_ts is its ONLY correlation key -- clearing it on escalation
    destroyed the approver's reaction (the 👍 stays in the reply log keyed to a
    ts nothing points at) and re-armed the empty-ts bulk sweep on a row that had
    already been shown."""
    now = _now()
    e = _row(expires_in_days=-1, dm_ts="100.1")
    rkr._escalate_stale_mechanical([e], now)
    assert e["dm_message_ts"] == "100.1"


def test_an_escalated_row_still_correlates_its_reaction():
    """The end-to-end version of the above, which is what the isolated
    escalation tests could not see: escalate, then correlate."""
    now = _now()
    e = _row(expires_in_days=-1, dm_ts="100.1", uid="u-corr")
    rkr._escalate_stale_mechanical([e], now)
    pairs = _correlate([e], [_reaction(HARRISON, ts="100.1")])
    assert len(pairs) == 1, "the escalation destroyed the decision"


def test_a_row_inside_its_deadline_is_untouched():
    now = _now()
    e = _row(expires_in_days=3)
    assert rkr._escalate_stale_mechanical([e], now) == (0, 0, 0)
    assert "escalation_count" not in e


def test_re_escalation_is_rate_limited_but_still_counted_as_overdue():
    now = _now()
    e = _row(expires_in_days=-30)
    e["escalated_at"] = (now - timedelta(days=1)).isoformat()
    e["escalation_count"] = 1
    newly, overdue, _retired = rkr._escalate_stale_mechanical([e], now)
    assert newly == 0, "must not re-card daily"
    assert overdue == 1, "but the backlog count must still see it"
    assert e["escalation_count"] == 1


def test_re_escalation_fires_again_after_the_interval():
    now = _now()
    e = _row(expires_in_days=-30, dm_ts="1.1")
    e["escalated_at"] = (now - timedelta(
        days=rkr._MECHANICAL_ESCALATION_INTERVAL_DAYS + 1)).isoformat()
    e["escalation_count"] = 1
    newly, _overdue, _retired = rkr._escalate_stale_mechanical([e], now)
    assert newly == 1
    assert e["escalation_count"] == 2


def test_the_escalation_budget_is_bounded_and_the_ending_is_NAMED():
    """Nothing else bounds the pool: inflow is ~21.5 mechanical rows/day and the
    timer this replaced was 65% of all mechanical dispositions. The ending has
    to exist -- and it has to say which of D-206's two endings it reached."""
    now = _now()
    carded = _row(expires_in_days=-90, dm_ts="1.1", uid="carded")
    carded["escalation_count"] = rkr._MECHANICAL_MAX_ESCALATIONS
    carded["escalated_at"] = (now - timedelta(days=30)).isoformat()
    never = _row(expires_in_days=-90, uid="never")
    never["escalation_count"] = rkr._MECHANICAL_MAX_ESCALATIONS
    never["escalated_at"] = (now - timedelta(days=30)).isoformat()

    _newly, overdue, retired = rkr._escalate_stale_mechanical([carded, never], now)
    assert (overdue, retired) == (2, 2)
    assert carded["state"] == "DISMISSED"
    assert carded["resolved_reason"] == "escalated_unanswered"
    assert never["resolved_reason"] == "unreviewed_no_surface", (
        "a row nobody was ever asked about must not be recorded as unanswered")


def test_a_row_under_budget_is_not_retired():
    now = _now()
    e = _row(expires_in_days=-90)
    e["escalation_count"] = rkr._MECHANICAL_MAX_ESCALATIONS - 1
    e["escalated_at"] = (now - timedelta(days=30)).isoformat()
    _newly, _overdue, retired = rkr._escalate_stale_mechanical([e], now)
    assert retired == 0
    assert e["state"] == "PENDING"


def test_a_malformed_escalated_at_is_RESTAMPED_not_skipped_forever():
    """D-051 lens-3 MED. The first cut swallowed the parse error and never
    overwrote the field, so such a row could never escalate, never retire and
    never expire by any pass -- a permanent +1 in the overdue counter."""
    now = _now()
    e = _row(expires_in_days=-30)
    e["escalated_at"] = "not-a-date"
    newly, overdue, _retired = rkr._escalate_stale_mechanical([e], now)
    assert (newly, overdue) == (1, 1)
    assert e["escalated_at"] != "not-a-date"


def test_a_malformed_deadline_never_escalates_and_never_raises():
    e = _row()
    e["expires_at"] = "not-a-date"
    e["proposed_at"] = "also-not-a-date"
    assert rkr._escalate_stale_mechanical([e], _now()) == (0, 0, 0)
    assert e["state"] == "PENDING"


def test_a_row_with_no_expires_at_uses_the_legacy_window():
    now = _now()
    old = _row(age_days=rkr._OPERATIONAL_UNROUTED_EXPIRY_DAYS + 1)
    young = _row(age_days=1)
    assert rkr._escalate_stale_mechanical([old], now)[1] == 1
    assert rkr._escalate_stale_mechanical([young], now)[1] == 0


def test_a_judgment_row_is_not_escalated_by_the_mechanical_pass():
    e = _row(update_type="known_answer", expires_in_days=-5)
    assert rkr._escalate_stale_mechanical([e], _now()) == (0, 0, 0)


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


def test_the_flag_is_whitelist_validated_and_case_insensitive(monkeypatch):
    """Named for what it checks. The predicate lowercases and strips, so "ON"
    and "  on  " are on -- the earlier name promised a stricter rule than the
    code has and tested neither case."""
    for value in ("", "off", "1", "true", "yes", "ON!", "on x"):
        monkeypatch.setenv("CORA_MECHANICAL_REVIEW", value)
        assert rkr._mechanical_review_enabled() is False, value
    for value in ("on", "ON", "On", "  on  "):
        monkeypatch.setenv("CORA_MECHANICAL_REVIEW", value)
        assert rkr._mechanical_review_enabled() is True, value


def test_the_script_and_the_bot_read_ONE_flag(monkeypatch):
    """They must not drift: an ungated is_review_approver in the bot was how a
    name in the YAML started logging reactions with the surface off."""
    monkeypatch.setenv("CORA_MECHANICAL_REVIEW", "on")
    assert rkr._mechanical_review_enabled() is review_lanes.mechanical_review_enabled()
    monkeypatch.setenv("CORA_MECHANICAL_REVIEW", "off")
    assert rkr._mechanical_review_enabled() is review_lanes.mechanical_review_enabled()


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


def test_an_entity_less_item_goes_to_harrison_not_the_delegate(monkeypatch):
    """The recipient is chosen with the SAME predicate that decides whether the
    reaction counts, so a card can never be sent to someone who would then be
    refused (D-051)."""
    blank = _row(uid="blank")
    blank["payload"] = {}
    _n, sent = _send([blank], monkeypatch, approvers=(HARRISON, HANNAH))
    assert [u["update_id"] for u in sent[HARRISON]] == ["blank"]
    assert HANNAH not in sent


def test_with_no_delegate_everything_goes_to_harrison(monkeypatch):
    _n, sent = _send([_row(uid="a"), _row(uid="b", entity="LEX")], monkeypatch)
    assert set(sent) == {HARRISON}


def test_the_dry_run_report_calls_the_same_deadline_predicate(monkeypatch, capsys):
    """The preview an operator reads before flipping the flag must not be able
    to disagree with the real pass. Asserted by RUNNING the dry run with the
    predicate patched and checking the report followed it -- the earlier version
    of this test only compared two functions to each other and would have passed
    unchanged if the dry-run branch used a different predicate entirely."""
    monkeypatch.setenv("CORA_MECHANICAL_REVIEW", "off")
    seen: list = []

    def _fake(entry, now):
        seen.append(entry)
        return True  # every pending row reads as overdue

    with patch.object(rkr, "_mechanical_past_deadline", side_effect=_fake), \
         patch.object(rkr, "get_pending_updates",
                      return_value=[_row(uid="a"), _row(uid="b")]), \
         patch.object(rkr, "correlate_reactions_to_updates", return_value=[]), \
         patch.object(rkr, "_acquire_run_lock", return_value=True), \
         patch.object(rkr, "_release_run_lock"),          patch.object(rkr.sys, "argv", ["run_knowledge_review.py", "--dry-run"]):
        rkr.main()
    assert len(seen) == 2, "the dry-run report did not use the shared predicate"


def test_the_deadline_predicate_agrees_with_the_escalation_pass():
    now = _now()
    overdue = _row(expires_in_days=-1)
    assert rkr._mechanical_past_deadline(overdue, now) is True
    assert rkr._escalate_stale_mechanical([dict(overdue)], now)[1] == 1
    assert rkr._mechanical_past_deadline(_row(expires_in_days=5), now) is False
