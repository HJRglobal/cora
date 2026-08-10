"""HubSpot email-sync ambiguous-match buttons (S6 migration 3).

Attach/Skip on the pending-match DM. The 👍/👎 reaction path is ADDITIVE-
preserved, so the pins here cover the button's own authorization and atomic
claim, and that the stored entry the REACTION path reads is unchanged in shape.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from cora import app as capp
from cora.connectors import hubspot_email_sync as hes

OWNER = "U_OWNER"
STRANGER = "U_STRANGER"
_DM = "D_OWNER"
_TS = "1780000000.0003"
_PID = "hsmatch-abc123def456"


class _FakeClient:
    def __init__(self):
        self.updated: list[dict] = []
        self.ephemeral: list[dict] = []

    def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)
        return {"ok": True}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    monkeypatch.setattr(hes, "_PENDING_PATH", tmp_path / "pending.json")
    yield


def _seed_pending(pending_id=_PID, slack_user_id=OWNER, message_ts=_TS):
    hes._save_pending({message_ts: {
        "thread_id": "t1", "owner_email": "o@x.com", "owner_id": "123",
        "contact_id": "c1", "contact_name": "Acme", "deal_ids": ["d1"],
        "messages": [{"sender": "a@x.com", "recipients": "b@x.com",
                      "subject": "s", "body_text": "b", "date_ts": 1}],
        "slack_user_id": slack_user_id, "pending_id": pending_id,
    }})
    return pending_id


def _tap_body(user_id, pending_id, action_id):
    return {
        "user": {"id": user_id},
        "channel": {"id": _DM},
        "message": {"ts": _TS, "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Ambiguous email match"}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": action_id, "value": pending_id}]},
        ]},
        "actions": [{"action_id": action_id, "value": pending_id}],
    }


class TestMatchTapCore:
    def test_attach_calls_the_engagement_write_and_clears_the_entry(self):
        _seed_pending()
        with patch("cora.tools.hubspot_client.log_email_engagement") as mock:
            outcome, msg = hes.process_match_tap(_PID, OWNER, attach=True)
        assert outcome == "attached"
        mock.assert_called_once()
        assert hes._load_pending() == {}

    def test_skip_clears_without_writing(self):
        _seed_pending()
        with patch("cora.tools.hubspot_client.log_email_engagement") as mock:
            outcome, _ = hes.process_match_tap(_PID, OWNER, attach=False)
        assert outcome == "skipped"
        mock.assert_not_called()
        assert hes._load_pending() == {}

    def test_stranger_refused_entry_survives(self):
        _seed_pending()
        with patch("cora.tools.hubspot_client.log_email_engagement") as mock:
            outcome, _ = hes.process_match_tap(_PID, STRANGER, attach=True)
        assert outcome == "not_authorized"
        mock.assert_not_called()
        assert _TS in hes._load_pending()

    def test_second_tap_is_orphaned_not_a_double_attach(self):
        _seed_pending()
        with patch("cora.tools.hubspot_client.log_email_engagement") as mock:
            hes.process_match_tap(_PID, OWNER, attach=True)
            outcome, _ = hes.process_match_tap(_PID, OWNER, attach=True)
        assert outcome == "orphaned"
        assert mock.call_count == 1

    def test_forged_pending_id_is_orphaned(self):
        _seed_pending()
        with patch("cora.tools.hubspot_client.log_email_engagement") as mock:
            outcome, _ = hes.process_match_tap("hsmatch-forged", OWNER, attach=True)
        assert outcome == "orphaned"
        mock.assert_not_called()
        assert _TS in hes._load_pending()

    def test_legacy_entry_without_slack_user_id_still_resolvable(self):
        """Entries written before this migration carry no owner binding; they
        must not become permanently un-actionable."""
        hes._save_pending({_TS: {
            "thread_id": "t1", "owner_email": "o@x.com", "owner_id": "1",
            "contact_id": "c1", "contact_name": "Acme", "deal_ids": ["d1"],
            "messages": [], "pending_id": _PID,
        }})
        with patch("cora.tools.hubspot_client.log_email_engagement"):
            outcome, _ = hes.process_match_tap(_PID, OWNER, attach=True)
        assert outcome == "attached"

    def test_find_pending_by_id_returns_ts_and_entry(self):
        _seed_pending()
        found = hes.find_pending_by_id(_PID)
        assert found is not None
        ts, entry = found
        assert ts == _TS
        assert entry["thread_id"] == "t1"


class TestReactionPathUnchanged:
    def test_stored_entry_keeps_every_field_the_reaction_path_reads(self):
        _seed_pending()
        entry = hes.get_pending_reaction(_TS)
        for field in ("thread_id", "owner_email", "owner_id", "contact_id",
                      "contact_name", "deal_ids", "messages"):
            assert field in entry

    def test_reaction_resolve_still_works_with_the_new_fields_present(self):
        _seed_pending()
        with patch("cora.tools.hubspot_client.log_email_engagement") as mock:
            assert hes.resolve_pending_reaction(_TS, approved=True) is True
        mock.assert_called_once()


class TestMatchBlocks:
    def test_value_is_the_opaque_pending_id_not_deal_or_contact(self):
        """Design invariant #1: values land in Slack payload logs, so they carry
        a handle and never CRM identifiers."""
        blocks = hes.build_match_blocks("body", "hsmatch-zzzzzzzzzzzz")
        actions = [b for b in blocks if b["type"] == "actions"][0]
        assert {e["value"] for e in actions["elements"]} == {"hsmatch-zzzzzzzzzzzz"}
        blob = json.dumps(actions)
        for crm_id in ("DEALID987", "CONTACTID654", "THREADID321"):
            assert crm_id not in blob

    def test_body_sanitized_at_construction(self):
        with patch("cora.slack_egress.sanitize_text", return_value="SCRUBBED") as m:
            blocks = hes.build_match_blocks("<!channel>", _PID)
        m.assert_called_once()
        assert blocks[0]["text"]["text"] == "SCRUBBED"

    def test_two_buttons_attach_and_skip(self):
        blocks = hes.build_match_blocks("body", _PID)
        actions = [b for b in blocks if b["type"] == "actions"][0]
        assert {e["action_id"] for e in actions["elements"]} == {
            hes.ACTION_ATTACH, hes.ACTION_SKIP}


class TestHandlerEntryPoint:
    """D-167: drive the @app.action wrapper the way production does."""

    def test_attach_tap_writes_and_drops_buttons(self):
        _seed_pending()
        fake = _FakeClient()
        with patch("cora.tools.hubspot_client.log_email_engagement") as mock:
            capp._handle_hubspot_match_tap(
                _tap_body(OWNER, _PID, hes.ACTION_ATTACH), fake, attach=True)
        mock.assert_called_once()
        assert len(fake.updated) == 1
        assert all(b.get("type") != "actions" for b in fake.updated[0]["blocks"])

    def test_skip_tap_does_not_write(self):
        _seed_pending()
        fake = _FakeClient()
        with patch("cora.tools.hubspot_client.log_email_engagement") as mock:
            capp._handle_hubspot_match_tap(
                _tap_body(OWNER, _PID, hes.ACTION_SKIP), fake, attach=False)
        mock.assert_not_called()
        assert len(fake.updated) == 1

    def test_stranger_tap_ephemeral_only_card_untouched(self):
        _seed_pending()
        fake = _FakeClient()
        with patch("cora.tools.hubspot_client.log_email_engagement") as mock:
            capp._handle_hubspot_match_tap(
                _tap_body(STRANGER, _PID, hes.ACTION_ATTACH), fake, attach=True)
        mock.assert_not_called()
        assert not fake.updated
        assert len(fake.ephemeral) == 1

    def test_eval_mode_writes_nothing(self, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        _seed_pending()
        fake = _FakeClient()
        with patch("cora.tools.hubspot_client.log_email_engagement") as mock:
            capp._handle_hubspot_match_tap(
                _tap_body(OWNER, _PID, hes.ACTION_ATTACH), fake, attach=True)
        mock.assert_not_called()
        assert _TS in hes._load_pending()
        assert not fake.updated and not fake.ephemeral

    def test_buttons_off_names_the_reaction_fallback(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        _seed_pending()
        fake = _FakeClient()
        with patch("cora.tools.hubspot_client.log_email_engagement") as mock:
            capp._handle_hubspot_match_tap(
                _tap_body(OWNER, _PID, hes.ACTION_ATTACH), fake, attach=True)
        mock.assert_not_called()
        assert ":+1:" in fake.ephemeral[0]["text"]

    def test_handler_never_raises_on_malformed_body(self):
        capp._handle_hubspot_match_tap({}, _FakeClient(), attach=True)

    def test_action_ids_unique_across_surfaces(self):
        from cora import briefing_enrollment as be
        from cora import confirm_cards as cc
        from cora import gap_autofill as ga
        ids = {hes.ACTION_ATTACH, hes.ACTION_SKIP}
        others = {cc.ACTION_CONFIRM, cc.ACTION_CANCEL, cc.ACTION_PICK,
                  cc.ACTION_CONFIRM_ITEM, cc.ACTION_CANCEL_ITEM,
                  be.ACTION_ENABLE, be.ACTION_SKIP,
                  ga.ACTION_DECLINE_NOT_MINE, ga.ACTION_DECLINE_UNKNOWN}
        assert not (ids & others)
