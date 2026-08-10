"""Daily-briefing enrollment buttons (S6 migration 1).

Enable/Skip on the "WOULD-BE BRIEFING" review DMs. The reaction path
(:+1:/:-1:, resolved at the next scheduled fire) is ADDITIVE-preserved, so
these tests pin BOTH affordances and the idempotency between them.

D-167: the handler entry point is driven directly with a realistic Slack action
body -- a green module-level suite does not prove the @app.action wrapper routes
the tap, checks the flag, authorizes, and edits the card.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from cora import app as capp
from cora import briefing_enrollment as be
from cora import confirm_cards as cc

HARRISON = "U0B2RM2JYJ1"
ATTACKER = "U0BATTACKER1"
_DM = "D_HARRISON"
_TS = "1780000000.0001"


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
def _isolated_state(tmp_path, monkeypatch):
    """Point the module at a temp state file -- these tests must never touch the
    live data/state/briefing-delivery.json (the test/prod isolation gap that bit
    the known-answers write path, cq-d9432f552a33)."""
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    monkeypatch.setattr(be, "STATE_PATH", tmp_path / "briefing-delivery.json")
    yield


def _seed_review(review_id="brev-abc123", sid="U_TEAMMATE", name="Tommy Tucson"):
    state = be._empty_state()
    state["pending_reviews"].append({
        "sid": sid, "name": name, "channel": _DM, "ts": _TS,
        "review_id": review_id, "sent_at": 1780000000.0,
    })
    be.save_state(state)
    return review_id


def _tap_body(user_id, review_id, action_id):
    return {
        "user": {"id": user_id},
        "channel": {"id": _DM},
        "message": {"ts": _TS, "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "WOULD-BE BRIEFING -- Tommy"}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": action_id, "value": review_id}]},
        ]},
        "actions": [{"action_id": action_id, "value": review_id}],
    }


class TestEnrollmentCore:
    def test_harrison_enable_enrolls_and_consumes_pending(self):
        rid = _seed_review()
        outcome, msg = be.process_enrollment_tap(rid, HARRISON, enable=True)
        assert outcome == "enabled"
        state = be.load_state()
        assert "U_TEAMMATE" in state["enabled"]
        assert state["enabled"]["U_TEAMMATE"]["via"] == "digest_button"
        # Consumed: the reaction resolver must not re-apply it later.
        assert state["pending_reviews"] == []
        assert "Tommy Tucson" in msg

    def test_skip_declines_and_clears_any_prior_enable(self):
        rid = _seed_review()
        be.process_enrollment_tap(rid, HARRISON, enable=True)
        rid2 = _seed_review(review_id="brev-second")
        outcome, _ = be.process_enrollment_tap(rid2, HARRISON, enable=False)
        assert outcome == "declined"
        state = be.load_state()
        assert "U_TEAMMATE" in state["declined"]
        assert "U_TEAMMATE" not in state["enabled"]

    def test_non_harrison_refused_and_state_untouched(self):
        rid = _seed_review()
        outcome, msg = be.process_enrollment_tap(rid, ATTACKER, enable=True)
        assert outcome == "not_authorized"
        assert "Harrison" in msg
        state = be.load_state()
        assert state["enabled"] == {}
        # The pending review SURVIVES a refused tap -- a stranger must not be
        # able to consume the entry and silently block Harrison's own enroll.
        assert len(state["pending_reviews"]) == 1

    def test_second_tap_is_already_handled_not_a_re_enroll(self):
        rid = _seed_review()
        be.process_enrollment_tap(rid, HARRISON, enable=True)
        outcome, _ = be.process_enrollment_tap(rid, HARRISON, enable=True)
        assert outcome == "already_handled"

    def test_unknown_review_id_is_already_handled(self):
        _seed_review()
        outcome, _ = be.process_enrollment_tap("brev-forged", HARRISON, enable=True)
        assert outcome == "already_handled"
        assert be.load_state()["enabled"] == {}

    def test_button_then_reaction_is_idempotent(self):
        """The script's resolver iterates pending_reviews; a tapped entry is gone,
        so a later :+1: on the same message cannot double-apply."""
        rid = _seed_review()
        be.process_enrollment_tap(rid, HARRISON, enable=False)
        state = be.load_state()
        assert be.find_pending_review(state, rid) is None
        assert "U_TEAMMATE" in state["declined"]


class TestStatePersistence:
    def test_save_never_leaves_a_partial_file_on_a_mid_write_crash(self, tmp_path):
        """The REAL atomicity pin. The first version of this test only asserted
        that no .tmp file lingered, which is equally true of the plain
        write_text it replaced -- it passed against the un-fixed code, so it
        proved nothing (D-051 lens-6 LOW: a vacuous test)."""
        good = be._empty_state()
        good["enabled"]["U1"] = {"name": "Keep me", "enabled_at": 1.0, "via": "t"}
        be.save_state(good)

        # Crash *after* the temp file is written, before the atomic swap.
        with patch("cora.briefing_enrollment.os.replace",
                   side_effect=OSError("boom")):
            assert be.save_state({"enabled": {"U2": {}}, "declined": {},
                                  "pending_reviews": []}) is False

        # The previously-good file is untouched, not truncated to empty.
        assert be.load_state()["enabled"]["U1"]["name"] == "Keep me"

    def test_save_reports_failure_rather_than_returning_none(self):
        assert be.save_state(be._empty_state()) is True

    def test_tmp_file_is_process_unique(self, tmp_path):
        """A fixed shared tmp name lets the script os.replace() the bot's
        half-written file into place (D-051 lens-5)."""
        import os as _os
        captured = {}
        real_replace = be.os.replace

        def _spy(src, dst):
            captured["src"] = str(src)
            return real_replace(src, dst)

        with patch("cora.briefing_enrollment.os.replace", side_effect=_spy):
            be.save_state(be._empty_state())
        assert str(_os.getpid()) in captured["src"]

    def test_no_tmp_file_lingers_after_a_successful_save(self, tmp_path):
        be.save_state(be._empty_state())
        assert not list(tmp_path.glob("*.tmp"))

    def test_corrupt_file_reads_as_empty_not_crash(self):
        be.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        be.STATE_PATH.write_text("{not json", encoding="utf-8")
        assert be.load_state() == be._empty_state()


class TestCrossProcessDelta:
    """apply_enrollment_delta re-reads immediately before writing, so a verdict
    cannot be reverted by a whole-run-old copy held by the briefing script."""

    def test_delta_preserves_concurrent_changes_made_since_load(self):
        rid = _seed_review()
        # Simulate the script having written OTHER state while the tap was in
        # flight (a second user enabled by a reaction earlier in the same run).
        other = be.load_state()
        other["enabled"]["U_OTHER"] = {"name": "Other", "enabled_at": 1.0,
                                       "via": "digest_reaction"}
        be.save_state(other)

        be.process_enrollment_tap(rid, HARRISON, enable=True)
        state = be.load_state()
        assert "U_TEAMMATE" in state["enabled"]     # the tap landed
        assert "U_OTHER" in state["enabled"]        # and did not clobber

    def test_write_failure_is_reported_and_does_not_claim_success(self):
        rid = _seed_review()
        with patch("cora.briefing_enrollment.save_state", return_value=False):
            outcome, msg = be.process_enrollment_tap(rid, HARRISON, enable=True)
        assert outcome == "write_failed"
        assert "nothing changed" in msg.lower()


class TestReviewBlocks:
    def test_button_value_is_the_opaque_review_id_never_the_user_id(self):
        blocks = be.build_review_blocks("body", "brev-xyz")
        actions = [b for b in blocks if b["type"] == "actions"][0]
        values = {e["value"] for e in actions["elements"]}
        assert values == {"brev-xyz"}
        # Design invariant #1: no payload, and specifically not the target user.
        assert "U_TEAMMATE" not in json.dumps(actions)

    def test_long_body_is_chunked_not_truncated(self):
        body = "\n".join(f"line {i} " + "x" * 100 for i in range(200))
        blocks = be.build_review_blocks(body, "brev-1")
        sections = [b for b in blocks if b["type"] == "section"]
        assert len(sections) > 1
        rendered = "\n".join(s["text"]["text"] for s in sections)
        assert "line 199" in rendered           # the tail survived
        assert all(len(s["text"]["text"]) <= 2900 for s in sections)

    def test_single_overlong_line_is_hard_split(self):
        blocks = be.build_review_blocks("y" * 7000, "brev-1")
        sections = [b for b in blocks if b["type"] == "section"]
        assert len(sections) == 3
        assert all(len(s["text"]["text"]) <= 2900 for s in sections)

    def test_blocks_text_is_egress_sanitized_at_construction(self):
        """D-168: Block Kit bodies bypass the class-level WebClient text= patch."""
        with patch("cora.slack_egress.sanitize_text", return_value="SCRUBBED") as m:
            blocks = be.build_review_blocks("raw <!channel> body", "brev-1")
        m.assert_called_once()
        assert blocks[0]["text"]["text"] == "SCRUBBED"

    def test_actions_block_is_last_so_body_renders_above_buttons(self):
        blocks = be.build_review_blocks("body", "brev-1")
        assert blocks[-1]["type"] == "actions"


class TestHandlerEntryPoint:
    """D-167: start where production starts -- the @app.action wrapper."""

    def test_enable_tap_enrolls_and_drops_buttons_in_place(self):
        rid = _seed_review()
        fake = _FakeClient()
        capp._handle_briefing_enrollment_tap(
            _tap_body(HARRISON, rid, be.ACTION_ENABLE), fake, enable=True)
        assert "U_TEAMMATE" in be.load_state()["enabled"]
        assert len(fake.updated) == 1
        assert all(b.get("type") != "actions" for b in fake.updated[0]["blocks"])
        # The briefing body Harrison reviewed is preserved on the terminal card.
        assert any("WOULD-BE BRIEFING" in json.dumps(b) for b in fake.updated[0]["blocks"])
        assert not fake.ephemeral

    def test_skip_tap_declines(self):
        rid = _seed_review()
        fake = _FakeClient()
        capp._handle_briefing_enrollment_tap(
            _tap_body(HARRISON, rid, be.ACTION_SKIP), fake, enable=False)
        assert "U_TEAMMATE" in be.load_state()["declined"]
        assert len(fake.updated) == 1

    def test_stranger_tap_refused_ephemeral_and_card_untouched(self):
        rid = _seed_review()
        fake = _FakeClient()
        capp._handle_briefing_enrollment_tap(
            _tap_body(ATTACKER, rid, be.ACTION_ENABLE), fake, enable=True)
        assert be.load_state()["enabled"] == {}
        assert not fake.updated                      # Harrison's DM not rewritten
        assert len(fake.ephemeral) == 1
        assert fake.ephemeral[0]["user"] == ATTACKER

    def test_second_tap_does_not_edit_the_shared_card(self):
        """already_handled is the fast RACE LOSER; the winner owns the card text."""
        rid = _seed_review()
        fake = _FakeClient()
        body = _tap_body(HARRISON, rid, be.ACTION_ENABLE)
        capp._handle_briefing_enrollment_tap(body, fake, enable=True)
        capp._handle_briefing_enrollment_tap(body, fake, enable=True)
        assert len(fake.updated) == 1                # not 2
        assert len(fake.ephemeral) == 1

    def test_eval_mode_mutates_nothing(self, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        rid = _seed_review()
        fake = _FakeClient()
        capp._handle_briefing_enrollment_tap(
            _tap_body(HARRISON, rid, be.ACTION_ENABLE), fake, enable=True)
        assert be.load_state()["enabled"] == {}
        assert not fake.updated and not fake.ephemeral

    def test_write_failure_leaves_the_buttons_live_for_a_retry(self):
        rid = _seed_review()
        fake = _FakeClient()
        with patch("cora.briefing_enrollment.save_state", return_value=False):
            capp._handle_briefing_enrollment_tap(
                _tap_body(HARRISON, rid, be.ACTION_ENABLE), fake, enable=True)
        assert not fake.updated                  # card untouched, buttons live
        assert len(fake.ephemeral) == 1

    def test_terminal_card_stops_advertising_the_reaction_fallback(self):
        """A tap consumes the pending entry, so reactions on that message become
        permanent no-ops -- the card must not keep promising them (lens-5 MED)."""
        rid = _seed_review()
        fake = _FakeClient()
        body = _tap_body(HARRISON, rid, be.ACTION_ENABLE)
        body["message"]["blocks"][0]["text"]["text"] = (
            "WOULD-BE BRIEFING -- Tommy\nTap *Enable delivery* ... Reacting "
            ":+1: / :-1: still works too (picked up at the next run).\n\nbody")
        capp._handle_briefing_enrollment_tap(body, fake, enable=True)
        rendered = json.dumps(fake.updated[0]["blocks"])
        assert "still works too" not in rendered
        assert "Reactions no longer apply" in rendered

    def test_buttons_off_refuses_and_names_the_reaction_fallback(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        rid = _seed_review()
        fake = _FakeClient()
        capp._handle_briefing_enrollment_tap(
            _tap_body(HARRISON, rid, be.ACTION_ENABLE), fake, enable=True)
        assert be.load_state()["enabled"] == {}
        assert not fake.updated
        assert ":+1:" in fake.ephemeral[0]["text"]

    def test_handler_never_raises_on_a_malformed_body(self):
        fake = _FakeClient()
        capp._handle_briefing_enrollment_tap({}, fake, enable=True)   # no raise

    def test_action_ids_are_distinct_from_the_confirm_card_ids(self):
        """A briefing tap is Harrison-only; a confirm tap is requester-scoped.
        Sharing an id would apply one authorization model to the other's payload."""
        assert be.ACTION_ENABLE not in (cc.ACTION_CONFIRM, cc.ACTION_CANCEL,
                                        cc.ACTION_CONFIRM_ITEM, cc.ACTION_PICK)
        assert be.ACTION_SKIP not in (cc.ACTION_CONFIRM, cc.ACTION_CANCEL,
                                      cc.ACTION_CANCEL_ITEM, cc.ACTION_PICK)
