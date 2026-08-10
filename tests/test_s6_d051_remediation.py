"""S6 D-051 remediation pins (2026-08-09).

Six adversarial lenses reviewed d12ea0d..1410bdd. These pin the confirmed
findings so each fix shows up as a deliberate change if it is ever undone.

Numbering follows the cascade report's finding list.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from cora import app as capp
from cora import confirm_cards as cc
from cora import gap_autofill as ga
from cora.connectors import hubspot_email_sync as hes
from cora.tools import osn_shift_db as odb
from cora.tools import osn_shift_handler as osh
from cora.tools import tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"
_CH = "cora-build"
_FOOTER = "\n\n*Sent using* <@U0B2RM2JYJ1>"


class TestF1FooterStripReDoS:
    """HIGH: strip_connector_footer reused a documented cubic regex without the
    length guard its only other consumer carries. Measured 25.6s at 2,400
    spaces, on the bot's shared GIL-holding thread, reachable from any DM."""

    def test_linear_on_a_40k_body(self):
        for probe in ("note" + " " * 40000 + "x",
                      "note" + " " * 40000 + "x" + _FOOTER,
                      "\n" * 40000 + "body" + _FOOTER,
                      " " * 20000 + "*" * 20000 + _FOOTER):
            start = time.perf_counter()
            td.strip_connector_footer(probe)
            elapsed = time.perf_counter() - start
            assert elapsed < 1.0, f"footer strip took {elapsed:.2f}s"

    def test_the_shared_regex_is_no_longer_ambiguous(self):
        """The de-ambiguation also bounds the PRE-EXISTING exposure in
        _strip_connector_noise, whose 2,000-char bail still allowed ~15s."""
        start = time.perf_counter()
        td._strip_connector_noise("x" + " " * 1990 + "y")
        assert time.perf_counter() - start < 1.0

    def test_still_strips_and_still_preserves_mid_text(self):
        assert td.strip_connector_footer("Revised volumes." + _FOOTER) == "Revised volumes."
        keep = "invoices sent using the old template"
        assert td.strip_connector_footer(keep) == keep


class TestF2ForcedToolDisplacement:
    """HIGH: the DM detector's object test was a bare \\S, so any message
    opening with the NOUN slack/dm had its turn stolen by tool_choice."""

    @pytest.mark.parametrize("message", [
        "slack is down again",
        "Slack is down for me right now",
        "slack notifications stopped working last night",
        "slack export for the audit is ready",
        "slack integration with Asana broke",
        "slack thread with Tommy has the pricing -- pull the numbers",
        "slack Q3 revenue numbers are wrong in the sheet",
        "dm history with Justin is missing",
        "dm notifications are off for me",
        "dm thread from Tommy says the order shipped",
        "Slack channel health monitor keeps firing",
        "slack messages are not syncing to the KB",
        "DM notifications are broken",
    ])
    def test_noun_sense_never_forces_a_dm(self, message):
        assert capp._staged_write_force_tool(message) is None

    @pytest.mark.parametrize("message", [
        "write an email signature block for Justin",
        "prepare an email summary of the board deck",
        "write a quick email blurb for the newsletter",
        "compose an email subject line for the campaign",
    ])
    def test_copywriting_never_forces_a_draft(self, message):
        assert capp._staged_write_force_tool(message) is None

    @pytest.mark.parametrize("message", [
        "note that the numbers below exclude OSNVV, give me the WoW delta",
        "note that Jerry is out -- who covers AP this week",
        "remember the cash pulse said OSN was down, pull the last 4 weeks",
    ])
    def test_discourse_marker_never_forces_a_note(self, message):
        assert capp._staged_write_force_tool(message) is None

    def test_mention_recipient_still_matches(self):
        """The \\b after a '>' never matched, so every "DM <@U...>" was missed."""
        assert capp._staged_write_force_tool(
            "DM <@U0B3VGWJTMJ> the updated fighter list") == "slack_send_dm"

    @pytest.mark.parametrize("message,expected", [
        ("can you dm Tommy the Q3 numbers?", "slack_send_dm"),
        ("could you draft an email to legal about the lease?", "gmail_create_draft"),
        ("can you remember that the gate code is 4412?", "cora_remember"),
    ])
    def test_polite_modal_is_an_imperative_not_a_question(self, message, expected):
        """A blanket '?' bail left the headline fix dark for the most natural
        phrasing of the very intent it exists to catch."""
        assert capp._staged_write_force_tool(message) == expected

    @pytest.mark.parametrize("message", [
        "can I dm Tommy directly?",
        "do you remember what Larry said?",
        "should I draft an email to the buyer?",
    ])
    def test_real_questions_still_never_force(self, message):
        assert capp._staged_write_force_tool(message) is None

    def test_lexicon_object_noun_does_not_steal_an_asana_comment(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        for msg in ("add a comment to the glossary task",
                    "add a note to the vocabulary task"):
            assert capp._staged_write_force_tool(msg) is None
            assert capp._asana_destructive_intent(msg) == "asana_add_comment"
        for msg in ("add the definitions to the glossary doc in Drive",
                    "add these to the glossary section of the deck",
                    "the term sheet means we are past LOI"):
            assert capp._staged_write_force_tool(msg) is None

    def test_new_guards_stay_linear_on_40k(self):
        for probe in ("cora" + " " * 40000 + "x",
                      "please" + " " * 40000 + "x",
                      "can you dm" + " " * 40000 + "x?",
                      "remember it" + " " * 40000 + "-- show me"):
            start = time.perf_counter()
            capp._staged_write_force_tool(probe)
            elapsed = time.perf_counter() - start
            assert elapsed < 1.0, f"detector took {elapsed:.2f}s"


class TestF3CardTruncation:
    """HIGH: adding blocks demotes text= to a notification fallback, so a body
    that used to render in full was silently sliced. The OSN card lost Sunday's
    shifts AND the scheduling warnings while the Approve button still rendered."""

    def test_osn_card_chunks_instead_of_truncating(self):
        body = "\n".join(f"line {i} " + "x" * 90 for i in range(60))
        blocks = osh.build_approval_blocks(body, "sched-1")
        sections = [b for b in blocks if b["type"] == "section"]
        assert len(sections) > 1
        rendered = "\n".join(s["text"]["text"] for s in sections)
        assert "line 59" in rendered
        assert all(len(s["text"]["text"]) <= cc.SECTION_CHARS for s in sections)
        assert blocks[-1]["type"] == "actions"

    def test_every_card_builder_uses_the_shared_chunker(self):
        long_body = "\n".join(f"row {i} " + "y" * 90 for i in range(60))
        for blocks in (osh.build_approval_blocks(long_body, "s1"),
                       ga.build_ask_blocks(long_body, "gapask-1"),
                       hes.build_match_blocks(long_body, "hsmatch-1")):
            sections = [b for b in blocks if b["type"] == "section"]
            assert len(sections) > 1
            assert "row 59" in "\n".join(s["text"]["text"] for s in sections)

    def test_overflow_past_the_block_cap_is_marked_not_silent(self):
        blocks = cc.chunk_mrkdwn_sections("z" * (cc.SECTION_CHARS * 50))
        assert len(blocks) == cc.MAX_BODY_BLOCKS
        assert "truncated" in blocks[-1]["text"]["text"]


class TestF4HubspotLockCoversBothPaths:
    """MED: the lock lived only in the button path, but the reaction handler
    calls resolve_pending_reaction directly in the SAME process -- so a tap and
    a 👍 could both file the engagements."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hes, "_PENDING_PATH", tmp_path / "pending.json")
        yield

    def _seed(self):
        hes._save_pending({"1.1": {
            "thread_id": "t1", "owner_email": "o@x.com", "owner_id": "1",
            "contact_id": "c1", "contact_name": "Acme", "deal_ids": ["d1"],
            "messages": [{"sender": "a@x.com", "recipients": "b@x.com",
                          "subject": "s", "body_text": "b", "date_ts": 1}],
            "slack_user_id": "U_OWNER", "pending_id": "hsmatch-1",
        }})

    def test_reaction_then_button_files_once(self):
        self._seed()
        with patch("cora.tools.hubspot_client.log_email_engagement") as mock:
            assert hes.resolve_pending_reaction("1.1", approved=True) is True
            outcome, _ = hes.process_match_tap("hsmatch-1", "U_OWNER", attach=True)
        assert outcome == "orphaned"
        assert mock.call_count == 1

    def test_the_claim_is_inside_the_lock(self):
        import inspect
        src = inspect.getsource(hes.resolve_pending_reaction)
        assert "_PENDING_LOCK" in src, "the reaction path must share the lock"


class TestF5OsnReactionUsesTheCas:
    """LOW: the reaction path's unconditional UPDATE let a ✅ landing after a
    button tap overwrite approved_by with the loser and announce twice."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(odb, "_DB_PATH", tmp_path / "osn.db")
        monkeypatch.setattr(osh, "_ADMIN_USER_IDS", {"U_A", "U_B"})
        odb.init_db()
        yield

    def _seed(self, ts="9.9"):
        conn = odb._connect()
        conn.execute(
            "INSERT INTO osn_schedules (schedule_id, week_start, shifts_json, "
            "status, created_at, approved_by, approved_at, notes) VALUES "
            "(?,?,?,?,?,?,?,?)",
            ("sched-x", "2026-08-10", json.dumps([]), "draft", 1, None, None,
             json.dumps({"approval_card_ts": ts})))
        conn.commit()
        conn.close()

    def test_reaction_after_a_button_tap_is_a_no_op(self):
        self._seed()
        assert osh.process_schedule_approval_tap("sched-x", "U_A")[0] == "approved"
        reply = osh.handle_schedule_approval_reaction(
            reaction="white_check_mark", message_ts="9.9",
            reactor_user_id="U_B", client=None)
        assert reply is None, "a second announcement would double-post"
        conn = odb._connect()
        row = conn.execute("SELECT approved_by FROM osn_schedules "
                           "WHERE schedule_id = ?", ("sched-x",)).fetchone()
        conn.close()
        assert row["approved_by"] == "U_A", "the loser must not overwrite"


class TestF6ClassBBodyFields:
    """LOW: gmail_draft.subject and influencer_deliverable.notes are FILED
    verbatim and were carrying the connector footer."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        yield

    @pytest.mark.parametrize("kind,field", [
        ("gmail_draft", "subject"),
        ("influencer_deliverable", "notes"),
        ("hubspot_note", "note_body"),
        ("slack_dm", "message"),
    ])
    def test_composed_field_is_stripped(self, kind, field):
        td._classb_stash(kind, HARRISON, _CH, {field: "real content" + _FOOTER})
        stored = td._CLASSB[kind]["peek"](HARRISON, _CH)
        assert stored[field] == "real content"


class TestF7ForcedPreviewIsNeverCached:
    """MED: a forced staged-write preview stored in the shared semantic cache is
    replayed with no stash and no buttons -- the exact phantom state the rider
    exists to remove -- and is entity-keyed, so another user can be served the
    first user's recipient and message body."""

    def test_dispatch_marks_a_forced_turn_uncacheable(self):
        import inspect
        src = inspect.getsource(capp._dispatch_qa)
        i_force = src.index("force_tool = _staged_write_force_tool")
        marker = "if force_tool is not None:\n        cache_storable = False"
        assert marker in src
        assert src.index(marker) > i_force


class TestF8GapTypedPathLocks:
    """LOW: the button path locked and the typed path did not, so a concurrent
    typed answer reverted a decline whose card was already closed."""

    def test_typed_decline_takes_the_same_lock(self):
        import inspect
        src = inspect.getsource(ga.record_ask_answer)
        assert "_ASKS_LOCK" in src
