"""OSN shift-schedule approve button (S6 migration 4).

VERIFY-FIRST correction pinned here: the ✅ reaction APPROVES ONLY -- publishing
is a separate admin command that DMs every active employee. The button mirrors
the reaction exactly (approve, same _is_admin authority) and must never publish.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from cora import app as capp
from cora.tools import osn_shift_db as db
from cora.tools import osn_shift_handler as osh

ADMIN = "U_ADMIN"
NOT_ADMIN = "U_EMPLOYEE"
_CH = "C_OSN"
_TS = "1780000000.0004"
_SID = "sched-abcdef123456"


class _FakeClient:
    def __init__(self):
        self.updated: list[dict] = []
        self.ephemeral: list[dict] = []
        self.posted: list[dict] = []

    def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)
        return {"ok": True}

    def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {"ok": True, "ts": _TS, "channel": _CH}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "osn.db")
    monkeypatch.setattr(osh, "_ADMIN_USER_IDS", {ADMIN})
    db.init_db()
    yield


def _seed_schedule(schedule_id=_SID, status="draft"):
    conn = db._connect()
    conn.execute(
        "INSERT INTO osn_schedules (schedule_id, week_start, shifts_json, status, "
        "created_at, approved_by, approved_at, notes) VALUES (?,?,?,?,?,?,?,?)",
        (schedule_id, "2026-08-10", json.dumps([]), status, 1780000000, None, None, ""),
    )
    conn.commit()
    conn.close()
    return schedule_id


def _status(schedule_id=_SID) -> str:
    conn = db._connect()
    row = conn.execute("SELECT status, approved_by FROM osn_schedules WHERE schedule_id = ?",
                       (schedule_id,)).fetchone()
    conn.close()
    return row["status"] if row else ""


def _tap_body(user_id, schedule_id):
    return {
        "user": {"id": user_id},
        "channel": {"id": _CH},
        "message": {"ts": _TS, "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Draft schedule"}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": osh.ACTION_APPROVE, "value": schedule_id}]},
        ]},
        "actions": [{"action_id": osh.ACTION_APPROVE, "value": schedule_id}],
    }


class TestApprovalTapCore:
    def test_admin_tap_approves(self):
        _seed_schedule()
        outcome, msg = osh.process_schedule_approval_tap(_SID, ADMIN)
        assert outcome == "approved"
        assert _status() == "approved"
        assert "publish schedule" in msg   # the two-step flow is still named

    def test_non_admin_refused_and_schedule_untouched(self):
        _seed_schedule()
        outcome, msg = osh.process_schedule_approval_tap(_SID, NOT_ADMIN)
        assert outcome == "not_authorized"
        assert "admins" in msg.lower()
        assert _status() == "draft"

    def test_same_authority_as_the_reaction_path(self):
        """The tap must use _is_admin, exactly as handle_schedule_approval_reaction
        does -- not a looser or separate rule."""
        _seed_schedule()
        assert osh._is_admin(ADMIN) is True
        assert osh._is_admin(NOT_ADMIN) is False
        assert osh.process_schedule_approval_tap(_SID, NOT_ADMIN)[0] == "not_authorized"

    def test_second_tap_is_already_handled_not_a_reapprove(self):
        _seed_schedule()
        osh.process_schedule_approval_tap(_SID, ADMIN)
        outcome, _ = osh.process_schedule_approval_tap(_SID, ADMIN)
        assert outcome == "already_handled"

    def test_cas_gives_exactly_one_winner(self):
        _seed_schedule()
        first = db.approve_schedule_if_pending(_SID, ADMIN)
        second = db.approve_schedule_if_pending(_SID, "U_OTHER_ADMIN")
        assert first is True and second is False
        conn = db._connect()
        row = conn.execute("SELECT approved_by FROM osn_schedules WHERE schedule_id = ?",
                           (_SID,)).fetchone()
        conn.close()
        assert row["approved_by"] == ADMIN   # the loser did not overwrite

    def test_unknown_schedule_is_orphaned(self):
        outcome, _ = osh.process_schedule_approval_tap("sched-nope", ADMIN)
        assert outcome == "orphaned"

    def test_published_schedule_is_not_reapproved(self):
        _seed_schedule(status="published")
        outcome, _ = osh.process_schedule_approval_tap(_SID, ADMIN)
        assert outcome == "already_handled"
        assert _status() == "published"


class TestNeverPublishes:
    """The load-bearing scope pin: publishing DMs every active employee."""

    def test_tap_does_not_publish(self):
        _seed_schedule()
        with patch.object(db, "publish_schedule") as pub:
            osh.process_schedule_approval_tap(_SID, ADMIN)
        pub.assert_not_called()
        assert _status() == "approved"      # approved, NOT published

    def test_button_label_does_not_promise_publishing(self):
        blocks = osh.build_approval_blocks("body", _SID)
        actions = [b for b in blocks if b["type"] == "actions"][0]
        label = actions["elements"][0]["text"]["text"]
        assert "publish" not in label.lower()
        assert label == "Approve schedule"

    def test_handler_entry_point_sends_no_employee_dms(self):
        _seed_schedule()
        fake = _FakeClient()
        capp._handle_osn_approve_tap(_tap_body(ADMIN, _SID), fake)
        assert _status() == "approved"
        assert not fake.posted              # no employee DM fan-out


class TestApprovalBlocks:
    def test_single_approve_button_carrying_the_schedule_id(self):
        blocks = osh.build_approval_blocks("body", _SID)
        actions = [b for b in blocks if b["type"] == "actions"][0]
        assert len(actions["elements"]) == 1
        assert actions["elements"][0]["value"] == _SID
        assert actions["elements"][0]["action_id"] == osh.ACTION_APPROVE

    def test_body_sanitized_at_construction(self):
        with patch("cora.slack_egress.sanitize_text", return_value="SCRUBBED") as m:
            blocks = osh.build_approval_blocks("<!channel>", _SID)
        m.assert_called_once()
        assert blocks[0]["text"]["text"] == "SCRUBBED"


class TestHandlerEntryPoint:
    """D-167: drive the @app.action wrapper the way production does."""

    def test_admin_tap_approves_and_drops_the_button(self):
        _seed_schedule()
        fake = _FakeClient()
        capp._handle_osn_approve_tap(_tap_body(ADMIN, _SID), fake)
        assert _status() == "approved"
        assert len(fake.updated) == 1
        assert all(b.get("type") != "actions" for b in fake.updated[0]["blocks"])

    def test_non_admin_tap_ephemeral_only_card_untouched(self):
        _seed_schedule()
        fake = _FakeClient()
        capp._handle_osn_approve_tap(_tap_body(NOT_ADMIN, _SID), fake)
        assert _status() == "draft"
        assert not fake.updated
        assert len(fake.ephemeral) == 1
        assert fake.ephemeral[0]["user"] == NOT_ADMIN

    def test_second_tap_does_not_edit_the_shared_card(self):
        _seed_schedule()
        fake = _FakeClient()
        body = _tap_body(ADMIN, _SID)
        capp._handle_osn_approve_tap(body, fake)
        capp._handle_osn_approve_tap(body, fake)
        assert len(fake.updated) == 1
        assert len(fake.ephemeral) == 1

    def test_eval_mode_approves_nothing(self, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        _seed_schedule()
        fake = _FakeClient()
        capp._handle_osn_approve_tap(_tap_body(ADMIN, _SID), fake)
        assert _status() == "draft"
        assert not fake.updated and not fake.ephemeral

    def test_buttons_off_names_the_reaction_fallback(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        _seed_schedule()
        fake = _FakeClient()
        capp._handle_osn_approve_tap(_tap_body(ADMIN, _SID), fake)
        assert _status() == "draft"
        assert "white_check_mark" in fake.ephemeral[0]["text"]

    def test_handler_never_raises_on_malformed_body(self):
        capp._handle_osn_approve_tap({}, _FakeClient())

    def test_action_id_unique_across_surfaces(self):
        from cora import briefing_enrollment as be
        from cora import confirm_cards as cc
        from cora import gap_autofill as ga
        from cora.connectors import hubspot_email_sync as hes
        others = {cc.ACTION_CONFIRM, cc.ACTION_CANCEL, cc.ACTION_PICK,
                  cc.ACTION_CONFIRM_ITEM, cc.ACTION_CANCEL_ITEM,
                  be.ACTION_ENABLE, be.ACTION_SKIP,
                  ga.ACTION_DECLINE_NOT_MINE, ga.ACTION_DECLINE_UNKNOWN,
                  hes.ACTION_ATTACH, hes.ACTION_SKIP}
        assert osh.ACTION_APPROVE not in others


class TestReactionPathStillWorks:
    def test_reaction_approval_unchanged(self):
        """Fallback parity: ✅ still approves via the original unconditional path."""
        _seed_schedule()
        conn = db._connect()
        conn.execute("UPDATE osn_schedules SET notes = ? WHERE schedule_id = ?",
                     (json.dumps({"approval_card_ts": _TS}), _SID))
        conn.commit()
        conn.close()
        reply = osh.handle_schedule_approval_reaction(
            reaction="white_check_mark", message_ts=_TS,
            reactor_user_id=ADMIN, client=_FakeClient())
        assert reply and "approved" in reply.lower()
        assert _status() == "approved"

    def test_reaction_still_ignores_a_non_admin(self):
        _seed_schedule()
        reply = osh.handle_schedule_approval_reaction(
            reaction="white_check_mark", message_ts=_TS,
            reactor_user_id=NOT_ADMIN, client=_FakeClient())
        assert reply is None
        assert _status() == "draft"
