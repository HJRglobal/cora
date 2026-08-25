"""C4 (cq-e33ce0545e85) + C16: a review card must not lie about its own state.

Measured on the live stores during recon, 2026-08-24. The 07:00 run posted three
batch headers and 17 cards. Harrison tapped 12 buttons at 07:00 AZ, then reacted
👍 to 19 messages at ~16:00 AZ. Of those 19 reactions, **14 (74%) were silent
no-ops**: 11 landed on cards he had already executed nine hours earlier, and 3 on
batch headers, which have no ledger row at all.

That is not a reaction-scanner fault -- the scanner is correct, and the five
👍s on genuinely PENDING mechanical cards WILL execute at the next run (traced
end to end). It is a rendering fault with three distinct causes, each pinned
below:

  1. THE CARD KEPT ADVERTISING A DEAD AFFORDANCE. Every card ends with
     "👍 Approve · 👎 Dismiss  (or tap a button below)". The button handler
     dropped the buttons but carried that sentence through verbatim, so a
     resolved card still instructed him to do something that now does nothing.
     The identical class was fixed once before for the briefing card
     (app._strip_reaction_affordance: "the card never advertises an affordance it
     has just disabled") and never generalised.

  2. THE EMOJI PATH NEVER EDITED THE CARD AT ALL. There was no chat_update
     anywhere in run_knowledge_review, so an emoji-resolved card kept LIVE
     buttons indefinitely, under a header still reading "N item(s) below for
     your approval".

  3. TWO STEP-0 PASSES COULD RETIRE AN ANSWERED ROW. Both run before Step 1
     reads the reply log. `_auto_dismiss_stale_pending` resolved rows with the
     reason "auto_expired_dmd_unreacted" without ever checking for a reaction,
     and `_escalate_stale_mechanical` retires a row "escalated_unanswered" when
     its budget runs out. A 👍 arriving on such a row was discarded silently and
     the row filed as unanswered -- the founder's approval being dropped.

Plus the store mislabel (C4) and the HubSpot promise (C16 bonus): the mechanical
card said "I carry it out" for hubspot_note, which writes nothing to HubSpot.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from cora import knowledge_review as kr

HARRISON = "U0B2RM2JYJ1"


# ── 1. the affordance line ──────────────────────────────────────────────────

@pytest.mark.parametrize("line", kr._CARD_AFFORDANCE_LINES)
def test_every_advertised_affordance_is_strippable(line):
    """The strip list and the card builders must not drift. If a builder's
    wording changes, this fails and the author updates both."""
    body = f"*[Known answer]* `F3E`\nthe fact\n\n{line}"
    out = kr.strip_card_affordance(body)
    assert line not in out
    assert "the fact" in out
    assert "no longer apply" in out


def test_the_strip_lines_are_the_ones_the_builders_actually_emit():
    """A literal strip list is only correct while it matches production output.
    Build one card of each kind and assert its footer is in the list."""
    known = kr.format_single_item_dm(
        {"update_id": "k1", "update_type": "known_answer",
         "description": "a fact", "payload": {"entity": "F3E"},
         "confidence": "HIGH", "proposed_at": "2026-08-24T00:00:00+00:00"})
    assert any(line in known for line in kr._CARD_AFFORDANCE_LINES)


def test_strip_is_a_noop_on_text_that_has_no_affordance():
    assert kr.strip_card_affordance("just a card body") == "just a card body"
    assert kr.strip_card_affordance("") == ""


def test_strip_never_raises_on_junk():
    for junk in (None, 123, {"not": "a string"}):
        assert kr.strip_card_affordance(junk) == junk


# ── 2. terminal card blocks (shared by both resolution paths) ───────────────

def _card_blocks(footer):
    return [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": f"*[Known answer]* `F3E`\nfact\n\n{footer}"}},
        {"type": "actions", "elements": [{"type": "button", "action_id": "approve"}]},
    ]


def test_terminal_blocks_drop_the_buttons_and_the_affordance():
    out = kr.terminal_card_blocks(_card_blocks(kr._CARD_AFFORDANCE_LINES[0]),
                                  ":white_check_mark: Saved.")
    assert not [b for b in out if b.get("type") == "actions"], "buttons survived"
    body = out[0]["text"]["text"]
    assert kr._CARD_AFFORDANCE_LINES[0] not in body
    assert "fact" in body, "the card's own content must survive"
    assert out[-1]["type"] == "context"
    assert "Saved" in out[-1]["elements"][0]["text"]


def test_terminal_blocks_degrade_to_the_outcome_when_there_are_no_sections():
    out = kr.terminal_card_blocks([], ":x: Dismissed.")
    assert out == [{"type": "section",
                    "text": {"type": "mrkdwn", "text": ":x: Dismissed."}}]
    assert kr.terminal_card_blocks(None, ":x: Dismissed.") == out


def test_terminal_blocks_tolerate_junk_blocks():
    out = kr.terminal_card_blocks(
        ["not a dict", None, {"type": "divider"},
         {"type": "section", "text": {"type": "mrkdwn", "text": "kept"}}],
        "done")
    assert len(out) == 2 and out[0]["text"]["text"] == "kept"


# ── 3. the store label (the two paths had forked) ───────────────────────────

@pytest.mark.parametrize("utype,expect", [
    ("known_answer", "known-answers"),
    ("efficiency", "efficiency backlog"),
    ("lexicon", "company lexicon"),
    ("decision_capture", "decisions inbox"),
])
def test_outcome_text_names_the_store_the_item_actually_lands_in(utype, expect):
    assert expect in kr.outcome_text("APPROVED", utype)


def test_the_button_path_and_the_emoji_path_share_one_definition():
    """They forked once: the emoji path branched per type and was test-pinned,
    the button path hard-coded "Saved to Cora's known-answers" for everything --
    so an efficiency tap rendered "Saved to Cora's known-answers. (appended to
    efficiency-backlog.md)". Harrison saw that five times on 8/24."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_knowledge_review as rkr
    for utype in ("known_answer", "efficiency", "lexicon", "decision_capture",
                  "hubspot_note", "asana_task"):
        assert (rkr._ack_reaction_text("APPROVED", utype, True)
                == kr.outcome_text("APPROVED", utype, True))


def test_a_failed_apply_is_never_acked_as_saved():
    txt = kr.outcome_text("APPROVED", "known_answer", success=False)
    assert "didn't go through" in txt
    assert "Saved" not in txt


def test_one_tap_approve_labels_an_efficiency_item_correctly(tmp_path, monkeypatch):
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "p.jsonl")
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "r.jsonl")
    kr._SEEN_IDS_CACHE = None
    kr.propose_update(update_id="eff-1", update_type="efficiency",
                      description="a finding", payload={"entity": "FNDR"},
                      confidence="HIGH")
    with patch.object(kr, "apply_knowledge_update",
                      return_value=(True, "appended to efficiency-backlog.md")):
        outcome, msg = kr.process_one_tap_action("eff-1", HARRISON, approve=True)
    assert outcome == "approved"
    assert "efficiency backlog" in msg
    assert "known-answers" not in msg, "the self-contradicting line is back"


# ── 4. the HubSpot promise ──────────────────────────────────────────────────

def test_the_hubspot_card_does_not_claim_cora_carries_it_out():
    """_execute_approved_update's hubspot_note branch performs NO HubSpot write
    -- it posts a reminder to #hjrg-leadership. asana_task and task_close DO
    write, so the promise was true for two of three mechanical types."""
    line = kr.mechanical_affordance_line("hubspot_note")
    assert "hand it off" in line
    assert "I carry it out" not in line
    for utype in ("asana_task", "task_close"):
        assert kr.mechanical_affordance_line(utype) == kr._MECH_AFFORDANCE_DOES


def test_the_hubspot_outcome_says_what_actually_happened():
    txt = kr.outcome_text("APPROVED", "hubspot_note")
    assert "#hjrg-leadership" in txt
    assert "don't write HubSpot notes" in txt


# ── 5. an answered row is never retired as unanswered ───────────────────────

def _mech_row(uid, ts, count):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    return {"update_id": uid, "update_type": "task_close", "state": "PENDING",
            "proposed_at": old, "expires_at": old, "dm_message_ts": ts,
            "escalation_count": count, "payload": {"entity": "F3E"}}


def test_escalation_does_not_retire_a_row_whose_card_carries_a_reaction():
    import sys
    from datetime import datetime, timezone
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_knowledge_review as rkr
    now = datetime.now(timezone.utc)
    answered = _mech_row("answered", "111.1", rkr._MECHANICAL_MAX_ESCALATIONS)
    silent = _mech_row("silent", "222.2", rkr._MECHANICAL_MAX_ESCALATIONS)

    rkr._escalate_stale_mechanical([answered, silent], now,
                                   {"111.1": {HARRISON}})
    assert answered["state"] == "PENDING", \
        "a row Harrison answered was retired as unanswered"
    assert silent["state"] == "DISMISSED"
    assert silent["resolved_reason"] == "escalated_unanswered"


def test_auto_dismiss_does_not_expire_a_row_whose_card_carries_a_reaction():
    """The reason string is literally "auto_expired_dmd_unreacted" and the pass
    never checked for a reaction."""
    import sys
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_knowledge_review as rkr
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=rkr._PENDING_EXPIRY_DAYS)
    old = (now - timedelta(days=60)).isoformat()

    def _row(uid, ts):
        return {"update_id": uid, "update_type": "known_answer", "state": "PENDING",
                "proposed_at": old, "dm_message_ts": ts, "payload": {"entity": "FNDR"}}

    answered, silent = _row("a", "111.1"), _row("b", "222.2")
    n = rkr._auto_dismiss_stale_pending([answered, silent], cutoff, now,
                                        {"111.1": {HARRISON}})
    assert n == 1
    assert answered["state"] == "PENDING"
    assert silent["state"] == "DISMISSED"


def test_a_reaction_from_someone_who_cannot_act_does_not_freeze_the_row():
    """D-051: the skip is AUTHORITY-AWARE. Asking only "is there a reaction on
    this ts" meant a reaction from someone with no authority over the row -- one
    the correlator will never process -- suppressed its escalation on EVERY
    subsequent run, pinning it PENDING forever while the comment promised
    "exactly one more run"."""
    import sys
    from datetime import datetime, timezone
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_knowledge_review as rkr
    now = datetime.now(timezone.utc)
    row = _mech_row("x", "111.1", rkr._MECHANICAL_MAX_ESCALATIONS)
    rkr._escalate_stale_mechanical([row], now, {"111.1": {"U_A_STRANGER"}})
    assert row["state"] == "DISMISSED"


def test_an_empty_answered_set_restores_the_previous_behaviour():
    """Fail-soft: if the reply log cannot be read, expiry must behave exactly as
    it did before -- not stop working."""
    import sys
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_knowledge_review as rkr
    now = datetime.now(timezone.utc)
    row = _mech_row("x", "111.1", rkr._MECHANICAL_MAX_ESCALATIONS)
    rkr._escalate_stale_mechanical([row], now, {})
    assert row["state"] == "DISMISSED"


def test_actionable_reaction_ts_reads_only_actionable_added_reactions(
        tmp_path, monkeypatch):
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "r.jsonl")
    rows = [
        {"event_type": "reaction_added", "action": "APPROVED", "message_ts": "1.1"},
        {"event_type": "reaction_added", "action": "DISMISSED", "message_ts": "2.2"},
        {"event_type": "reaction_added", "action": "OTHER", "message_ts": "3.3"},
        {"event_type": "reaction_removed", "action": "APPROVED", "message_ts": "4.4"},
        {"event_type": "block_action", "action": "APPROVED", "message_ts": "5.5"},
        {"event_type": "reaction_added", "action": "APPROVED", "message_ts": ""},
    ]
    import json
    (tmp_path / "r.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert kr.actionable_reaction_ts() == {"1.1", "2.2"}


def test_actionable_reaction_ts_fails_soft_to_empty(monkeypatch):
    with patch.object(kr, "load_reply_log", side_effect=RuntimeError("boom")):
        assert kr.actionable_reaction_ts() == set()


# ── 6. unmatched reactions stop being invisible ─────────────────────────────

def test_unmatched_reactions_are_classified_into_their_two_populations(
        tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "p.jsonl")
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "r.jsonl")
    (tmp_path / "p.jsonl").write_text("\n".join(json.dumps(u) for u in [
        {"update_id": "done", "update_type": "known_answer", "state": "APPROVED",
         "dm_message_ts": "10.1", "payload": {}},
        {"update_id": "live", "update_type": "task_close", "state": "PENDING",
         "dm_message_ts": "20.2", "payload": {"entity": "F3E"}},
    ]), encoding="utf-8")
    (tmp_path / "r.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"event_type": "reaction_added", "action": "APPROVED",
         "message_ts": "10.1", "reactor_id": HARRISON, "reaction": "+1"},
        {"event_type": "reaction_added", "action": "APPROVED",
         "message_ts": "20.2", "reactor_id": HARRISON, "reaction": "+1"},
        {"event_type": "reaction_added", "action": "APPROVED",
         "message_ts": "99.9", "reactor_id": HARRISON, "reaction": "+1"},
    ]), encoding="utf-8")

    out = kr.classify_unmatched_reactions()
    assert [r["message_ts"] for r in out["already_resolved"]] == ["10.1"]
    assert [r["message_ts"] for r in out["no_ledger_row"]] == ["99.9"]
    # the PENDING one is NOT unmatched -- correlate will act on it
    assert all(r["message_ts"] != "20.2"
               for r in out["already_resolved"] + out["no_ledger_row"])


def test_unmatched_classification_dedupes_repeat_rows(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "p.jsonl")
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "r.jsonl")
    (tmp_path / "p.jsonl").write_text("", encoding="utf-8")
    row = {"event_type": "reaction_added", "action": "APPROVED",
           "message_ts": "9.9", "reactor_id": HARRISON, "reaction": "+1"}
    (tmp_path / "r.jsonl").write_text(
        "\n".join(json.dumps(row) for _ in range(4)), encoding="utf-8")
    assert len(kr.classify_unmatched_reactions()["no_ledger_row"]) == 1


def test_unmatched_classification_fails_soft(monkeypatch):
    with patch.object(kr, "load_proposed_updates", side_effect=RuntimeError("x")):
        out = kr.classify_unmatched_reactions()
    assert out == {"already_resolved": [], "no_ledger_row": []}


# ── 7. the emoji path now retires the card ──────────────────────────────────

def test_the_emoji_path_edits_the_card_it_resolved():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_knowledge_review as rkr
    client = MagicMock()
    client.conversations_history.return_value = {
        "messages": [{"blocks": _card_blocks(kr._CARD_AFFORDANCE_LINES[0])}]}
    ok = rkr._terminal_edit_card(client, "D123", "111.1",
                                 ":white_check_mark: Saved.",
                                 logging.getLogger("t"))
    assert ok is True
    kwargs = client.chat_update.call_args.kwargs
    assert kwargs["channel"] == "D123" and kwargs["ts"] == "111.1"
    assert not [b for b in kwargs["blocks"] if b.get("type") == "actions"]


@pytest.mark.parametrize("failure", ["history", "update"])
def test_the_card_edit_is_fail_soft(failure):
    """The resolve and the threaded ack have already happened. A cosmetic edit
    must never be able to raise into them."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_knowledge_review as rkr
    client = MagicMock()
    if failure == "history":
        client.conversations_history.side_effect = RuntimeError("nope")
    else:
        client.conversations_history.return_value = {"messages": [{"blocks": []}]}
        client.chat_update.side_effect = RuntimeError("nope")
    assert rkr._terminal_edit_card(client, "D1", "1.1", "x",
                                   logging.getLogger("t")) is False


def test_the_card_edit_refuses_without_an_anchor():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import run_knowledge_review as rkr
    client = MagicMock()
    assert rkr._terminal_edit_card(client, "", "1.1", "x",
                                   logging.getLogger("t")) is False
    assert rkr._terminal_edit_card(client, "D1", "", "x",
                                   logging.getLogger("t")) is False
    client.conversations_history.assert_not_called()
