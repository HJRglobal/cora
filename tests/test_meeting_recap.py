"""Code #13 slice 5 (cq-7a724ee43964): a captured meeting's recap, offered to the
organizer as ONE propose-only card, DM'd to INTERNAL attendees on the tap.

The properties under test, in the order they matter:

  1. EXTERNAL ATTENDEES NEVER RECEIVE ANYTHING, under any path -- and their
     addresses appear nowhere: not the card, not the store, not the ledger.
  2. INTERNAL means roster-mapped; a domain alone admits nobody, and the
     org-roles `external` flag is a belt behind the roster.
  3. LEX -> PHI custodians only (D-145); an empty custodian set builds NO card.
  4. NOTHING IS SENT WITHOUT THE TAP; the tap is addressee-only, atomic, and a
     second tap cannot double-send (idempotency is per recipient).
  5. THE CARD IS HONEST: it offers nothing when Fireflies produced no summary,
     it expires and says so, and its resolved outcome narrates only what the
     ledger shows.
  6. WIRING: the actions are registered in app.py, the footer is registered in
     knowledge_review, both new write paths are redirected in conftest, and
     D-011 is untouched.

All fixture text is synthetic (D-145): invented people at invented domains,
random-word meeting titles, no client or health content anywhere.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cora import knowledge_review as kr
from cora import meeting_recap as mr
from cora import review_lanes

HARRISON = mr.HARRISON_ID
OTHER = "U0B3AEJCYGP"

ORGANIZER_ID = "U0ORGANIZERZ1"
INTERNAL_A = "U0INTERNALA1"
INTERNAL_B = "U0INTERNALB1"

ORGANIZER_EMAIL = "orla.penhaligon@northwind.test"
INTERNAL_A_EMAIL = "ivy.marlow@northwind.test"
INTERNAL_B_EMAIL = "tobias.renshaw@northwind.test"
EXTERNAL_EMAIL = "quill.baxter@outside-vendor.test"

EMAIL_TO_SLACK = {
    ORGANIZER_EMAIL: ORGANIZER_ID,
    INTERNAL_A_EMAIL: INTERNAL_A,
    INTERNAL_B_EMAIL: INTERNAL_B,
}


def _transcript(**over) -> dict:
    base = {
        "id": "T-quartz-lantern-01",
        "title": "Quartz Lantern Sync",
        "date": 1787097600,
        "organizer_email": ORGANIZER_EMAIL,
        "host_email": ORGANIZER_EMAIL,
        "transcript_url": "https://app.fireflies.ai/view/quartz-lantern",
        # displayName is None on LIVE data for every human attendee.
        "meeting_attendees": [
            {"displayName": None, "email": ORGANIZER_EMAIL},
            {"displayName": None, "email": INTERNAL_A_EMAIL},
            {"displayName": None, "email": EXTERNAL_EMAIL},
        ],
        "participants": [],
        "sentences": [],
        "summary": {
            "short_summary": "The team agreed the pilot launch moves to the second week.",
            "overview": "Longer overview text that must lose to short_summary.",
            "action_items": ["Draft the launch checklist", "Book the venue walkthrough"],
        },
    }
    base.update(over)
    return base


def _prepare(t: dict | None = None, *, entity: str = "F3E", **kw) -> tuple[dict | None, str]:
    return mr.prepare_card(
        t or _transcript(), entity=entity, email_to_slack=EMAIL_TO_SLACK,
        attribution_unreliable=False, meeting_date="2026-09-19", **kw,
    )


def _carded(t: dict | None = None, **kw) -> dict:
    rec, skip = _prepare(t, **kw)
    assert rec is not None, skip
    return mr.record_card(rec, dm_channel_id="D0ORG", card_message_ts="1758300000.1")


class _FakeClient:
    """Records every Slack call. `fail_for` makes chat_postMessage raise for those
    recipient channels, to drive the partial-failure path."""

    def __init__(self, fail_for: set[str] | None = None):
        self.opened: list[str] = []
        self.posted: list[dict] = []
        self.updated: list[dict] = []
        self.ephemeral: list[dict] = []
        self.fail_for = set(fail_for or ())

    def conversations_open(self, users):
        uid = users[0] if isinstance(users, (list, tuple)) else users
        self.opened.append(uid)
        return {"ok": True, "channel": {"id": f"D-{uid}"}}

    def chat_postMessage(self, **kw):
        if kw.get("channel", "").replace("D-", "") in self.fail_for:
            raise RuntimeError("simulated Slack failure")
        self.posted.append(kw)
        return {"ok": True, "ts": "999.9"}

    def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)
        return {"ok": True}


def _ledger_text() -> str:
    p = mr.ledger_path()
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _store_text() -> str:
    p = mr.pending_path()
    return p.read_text(encoding="utf-8") if p.exists() else ""


# ── 1 + 2. population: external never, internal = roster-mapped ─────────────

def test_external_attendee_is_never_a_recipient_and_is_named_nowhere():
    """THE central negative assertion. One internal + one external attendee ->
    exactly the internal id is a recipient, and the external address appears
    in neither the record nor the card text nor the store."""
    rec = _carded()
    assert rec["recipients"] == [INTERNAL_A]
    assert EXTERNAL_EMAIL not in json.dumps(rec)
    assert EXTERNAL_EMAIL not in mr.build_card_text(rec)
    assert EXTERNAL_EMAIL not in _store_text()
    assert EXTERNAL_EMAIL not in _ledger_text()


def test_a_domain_alone_never_admits_an_attendee():
    """An unmapped attendee at the SAME domain as mapped staff is not internal.
    The roster is the identity; a domain guess would admit a contractor's
    company address or a spoofed one."""
    t = _transcript(meeting_attendees=[
        {"displayName": None, "email": ORGANIZER_EMAIL},
        {"displayName": None, "email": "stranger.at.same.domain@northwind.test"},
    ])
    pop = mr.resolve_recipients(t, email_to_slack=EMAIL_TO_SLACK, exclude_ids={ORGANIZER_ID})
    assert pop["recipients"] == []
    assert pop["unmapped"] == 1


def test_org_roles_external_flag_is_a_belt_behind_the_roster(monkeypatch):
    """A roster-mapped id flagged `external: true` in org-roles (a guest
    consultant with a workspace seat) is still not a recipient."""
    from cora import org_roles

    class _Rec:
        external = True

    monkeypatch.setattr(org_roles, "get_role",
                        lambda sid: _Rec() if sid == INTERNAL_A else None)
    pop = mr.resolve_recipients(_transcript(), email_to_slack=EMAIL_TO_SLACK,
                                exclude_ids={ORGANIZER_ID})
    assert INTERNAL_A not in pop["recipients"]


def test_capture_identities_are_not_attendees():
    """Cora's own seat and the Fireflies bot sit on every invite. They are not
    people: not counted, not DM'd, even if somebody mapped them."""
    t = _transcript(meeting_attendees=[
        {"displayName": None, "email": ORGANIZER_EMAIL},
        {"displayName": "Fred", "email": "notetaker@fireflies.ai"},
        {"displayName": None, "email": "cora@hjrglobal.com"},
    ])
    lookup = dict(EMAIL_TO_SLACK, **{"cora@hjrglobal.com": "U0CORABOTX1"})
    pop = mr.resolve_recipients(t, email_to_slack=lookup, exclude_ids={ORGANIZER_ID})
    assert pop["recipients"] == []
    assert pop["attendees"] == 1


def test_the_addressee_does_not_receive_their_own_recap():
    """The organizer holds the card; DMing them the same text is noise, and the
    card names who WILL be DM'd, so the list must not include them."""
    pop = mr.resolve_recipients(_transcript(), email_to_slack=EMAIL_TO_SLACK,
                                exclude_ids={ORGANIZER_ID})
    assert ORGANIZER_ID not in pop["recipients"]


def test_participants_list_is_also_read_and_deduped():
    """Fireflies carries attendees in two places; a person listed in both must
    be one recipient, and a participant absent from meeting_attendees still
    counts."""
    t = _transcript(participants=[INTERNAL_A_EMAIL, INTERNAL_B_EMAIL])
    pop = mr.resolve_recipients(t, email_to_slack=EMAIL_TO_SLACK, exclude_ids={ORGANIZER_ID})
    assert pop["recipients"] == [INTERNAL_A, INTERNAL_B]


# ── 3. LEX -> custodians only ────────────────────────────────────────────────

def test_lex_recipients_are_custodians_only():
    """Two internal attendees, one custodian -> only the custodian; the other is
    counted as filtered, never named."""
    t = _transcript(meeting_attendees=[
        {"displayName": None, "email": ORGANIZER_EMAIL},
        {"displayName": None, "email": INTERNAL_A_EMAIL},
        {"displayName": None, "email": INTERNAL_B_EMAIL},
        {"displayName": None, "email": EXTERNAL_EMAIL},
    ])
    custodians = frozenset({ORGANIZER_ID, INTERNAL_A})
    rec, skip = _prepare(t, entity="LEX-LLC", custodian_loader=lambda: custodians)
    assert rec is not None, skip
    assert rec["recipients"] == [INTERNAL_A]
    assert rec["is_lex"] is True
    assert INTERNAL_B not in json.dumps(rec)
    assert EXTERNAL_EMAIL not in json.dumps(rec)


def test_lex_with_empty_custodian_set_builds_no_card_at_all():
    """D-145: not a card with zero recipients -- NOTHING. No record, no ledger
    row, so the next poll re-evaluates the same silence rather than a stale
    card sitting anywhere."""
    rec, skip = _prepare(entity="LEX", custodian_loader=lambda: frozenset())
    assert rec is None
    assert "custodian" in skip
    assert not mr.pending_path().exists()
    assert not mr.ledger_path().exists()
    assert mr.pending_records() == []


def test_lex_default_custodian_source_is_the_fail_closed_allowlist(monkeypatch):
    """Without an injected loader the module reads lex_phi_access's allowlist --
    the single source of truth -- and an empty read means no card."""
    from cora import lex_phi_access
    monkeypatch.setattr(lex_phi_access, "_load_custodian_ids", lambda: frozenset())
    rec, skip = _prepare(entity="LEX")
    assert rec is None and "custodian" in skip


def test_lex_non_custodian_organizer_never_holds_the_card():
    """A LEX recap must not be previewed to a non-custodian, even the organizer.
    The card routes to Harrison and says why."""
    custodians = frozenset({HARRISON, INTERNAL_A})
    rec, skip = _prepare(entity="LEX", custodian_loader=lambda: custodians)
    assert rec is not None, skip
    assert rec["addressee_id"] == HARRISON
    assert "custodian" in rec["routing_reason"]


def test_lex_body_goes_through_the_phi_scrub(monkeypatch):
    """Defence in depth: even custodian-only text runs the existing LEX scrub
    before it is persisted or rendered."""
    monkeypatch.setattr(mr, "scrub_lex", lambda s: f"[SCRUBBED]{s}" if s else s)
    custodians = frozenset({ORGANIZER_ID, INTERNAL_A})
    rec, skip = _prepare(entity="LEX-LTS", custodian_loader=lambda: custodians)
    assert rec is not None, skip
    assert rec["overview"].startswith("[SCRUBBED]")
    assert rec["action_items"].startswith("[SCRUBBED]")


def test_lex_scrub_failure_withholds_the_body_rather_than_leaking(monkeypatch):
    from cora.connectors import fireflies_action_extractor as fae

    def _boom(_):
        raise RuntimeError("scrubber down")

    monkeypatch.setattr(fae, "_scrub_lex_text", _boom)
    assert "withheld" in mr.scrub_lex("anything")


def test_non_lex_body_that_trips_the_phi_screen_is_not_carded(monkeypatch):
    """A non-LEX meeting whose AI summary reads as PHI is dropped outright --
    the same posture as the ask lane's _phi_flagged."""
    monkeypatch.setattr(mr, "phi_flagged", lambda text: True)
    rec, skip = _prepare()
    assert rec is None and "PHI" in skip
    assert not mr.pending_path().exists()


def test_phi_screen_unavailable_reads_as_flagged(monkeypatch):
    from cora import phi_guard

    def _boom(_):
        raise RuntimeError("screen down")

    monkeypatch.setattr(phi_guard, "is_any_phi", _boom)
    assert mr.phi_flagged("anything") is True


# ── organizer resolution ────────────────────────────────────────────────────

@pytest.mark.parametrize("organizer", [
    "cora@hjrglobal.com",           # the capture seat -- the COPY path's organizer
    EXTERNAL_EMAIL,                 # unmapped outsider
    "",                             # nothing at all
])
def test_external_or_capture_identity_organizer_routes_to_harrison(organizer):
    t = _transcript(organizer_email=organizer, host_email=organizer)
    sid, reason = mr.resolve_addressee(t, email_to_slack=EMAIL_TO_SLACK)
    assert sid == HARRISON
    assert "founder" in reason


def test_a_mapped_internal_organizer_holds_their_own_card():
    sid, reason = mr.resolve_addressee(_transcript(), email_to_slack=EMAIL_TO_SLACK)
    assert sid == ORGANIZER_ID and reason == ""


def test_host_is_the_fallback_when_organizer_is_blank():
    t = _transcript(organizer_email="", host_email=INTERNAL_A_EMAIL)
    sid, _ = mr.resolve_addressee(t, email_to_slack=EMAIL_TO_SLACK)
    assert sid == INTERNAL_A


# ── recap text: deterministic, honest about emptiness ───────────────────────

def test_recap_is_read_deterministically_from_summary_fields():
    """short_summary wins over overview; a list-valued action_items is joined.
    No model call, no invented 'decisions'."""
    recap = mr.extract_recap(_transcript())
    assert recap["overview"].startswith("The team agreed")
    assert "Draft the launch checklist" in recap["action_items"]
    assert "Book the venue walkthrough" in recap["action_items"]
    assert "decision" not in json.dumps(recap).lower()


def test_overview_falls_back_through_overview_then_gist():
    t = _transcript(summary={"overview": "", "gist": "Gist only."})
    assert mr.extract_recap(t)["overview"] == "Gist only."


def test_empty_summary_card_says_so_and_offers_nothing():
    """The spec's own words: if the summary is empty the card says so and offers
    nothing -- no buttons, no affordance line, and it is TERMINAL so the next
    poll cannot card the same silence again."""
    rec = _carded(_transcript(summary={}))
    assert rec["state"] == mr.STATE_NO_SUMMARY
    text = mr.build_card_text(rec)
    assert "no summary" in text.lower()
    assert mr.AFFORDANCE_LINE not in text
    _, blocks = mr.build_card_blocks(rec)
    assert not [b for b in blocks if b.get("type") == "actions"]
    assert mr.already_carded(rec["recap_id"])
    got, refusal = mr.claim_for_tap(rec["recap_id"], ORGANIZER_ID)
    assert got is None and "no recap" in refusal.lower()


def test_no_internal_recipient_with_a_real_recap_builds_no_card():
    """A card whose only button would DM nobody is noise, not a proposal."""
    t = _transcript(meeting_attendees=[
        {"displayName": None, "email": ORGANIZER_EMAIL},
        {"displayName": None, "email": EXTERNAL_EMAIL},
    ])
    rec, skip = _prepare(t)
    assert rec is None and "no internal attendee" in skip


# ── 4. store + claim: addressee-only, atomic, expiring ──────────────────────

def test_a_carded_meeting_is_not_carded_again():
    rec = _carded()
    assert mr.already_carded(rec["recap_id"])
    again, skip = _prepare()
    assert again is None and skip == "already carded"


def test_claim_is_addressee_only_and_leaves_the_row_pending():
    rec = _carded()
    got, refusal = mr.claim_for_tap(rec["recap_id"], OTHER)
    assert got is None and "someone else" in refusal
    assert mr.get_record(rec["recap_id"])["state"] == mr.STATE_PENDING


def test_an_empty_addressee_is_a_refusal_not_a_wildcard():
    rec = _carded()
    mr._append_event(rec["recap_id"], "state", {"addressee_id": ""})
    got, refusal = mr.claim_for_tap(rec["recap_id"], ORGANIZER_ID)
    assert got is None and "someone else" in refusal


def test_a_second_tap_after_a_claim_is_refused():
    """The claim moves the row to CLAIMED atomically; a second tap sees that and
    does not act -- two DMs per recipient from one card is the race this stops."""
    rec = _carded()
    first, _ = mr.claim_for_tap(rec["recap_id"], ORGANIZER_ID)
    assert first is not None and first["state"] == mr.STATE_CLAIMED
    second, refusal = mr.claim_for_tap(rec["recap_id"], ORGANIZER_ID)
    assert second is None and "already" in refusal.lower()


def test_an_expired_card_says_so_at_the_tap_and_sends_nothing():
    rec = _carded()
    late = datetime.now(timezone.utc) + timedelta(days=mr.TTL_DAYS + 1)
    got, refusal = mr.claim_for_tap(rec["recap_id"], ORGANIZER_ID, now=late)
    assert got is None and "aged out" in refusal
    assert mr.get_record(rec["recap_id"])["state"] == mr.STATE_EXPIRED
    assert '"event": "expired"' in _ledger_text()
    assert '"event": "sent"' not in _ledger_text()


def test_an_unknown_card_refuses_honestly():
    got, refusal = mr.claim_for_tap("nope", ORGANIZER_ID)
    assert got is None and "can't find this card" in refusal


def test_terminal_state_is_sticky_against_a_late_carded_event():
    """The fold refuses to move a row OUT of a terminal state, so a stale
    script-side `carded` event cannot resurrect a shared or dismissed card."""
    rec = _carded()
    mr.mark_dismissed(rec)
    mr._append_event(rec["recap_id"], mr.EVENT_CARDED, dict(rec, state=mr.STATE_PENDING))
    assert mr.get_record(rec["recap_id"])["state"] == mr.STATE_DISMISSED


def test_a_torn_store_line_is_skipped_not_read_as_empty():
    rec = _carded()
    with mr.pending_path().open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    assert mr.get_record(rec["recap_id"]) is not None


# ── 4 + 5. fan-out: exactly the internal DM, per-recipient idempotency ──────

def test_share_dms_exactly_the_internal_recipient_and_names_no_external():
    """THE ledger negative assertion. After the tap: one DM opened for the
    internal attendee, one `sent` ledger row for that id, and the external
    address appears in no post and no ledger row."""
    rec = _carded()
    client = _FakeClient()
    sent, failed, resolved = mr.process_share(rec, client)
    assert (sent, failed, resolved) == (1, 0, True)
    assert client.opened == [INTERNAL_A]
    assert len(client.posted) == 1
    assert client.posted[0]["channel"] == f"D-{INTERNAL_A}"
    ledger = _ledger_text()
    sent_rows = [json.loads(l) for l in ledger.splitlines() if '"event": "sent"' in l]
    assert [r["recipient_id"] for r in sent_rows] == [INTERNAL_A]
    assert EXTERNAL_EMAIL not in ledger
    assert EXTERNAL_EMAIL not in json.dumps(client.posted)
    assert "@" not in "".join(r.get("recipient_id", "") for r in sent_rows)
    assert mr.get_record(rec["recap_id"])["state"] == mr.STATE_SHARED


def test_the_ledger_carries_slack_ids_and_never_an_email():
    """Structural: no attendee -- internal or external -- is identified in the
    ledger by an address, so an external one cannot get there by accident."""
    rec = _carded()
    mr.process_share(rec, _FakeClient())
    for line in _ledger_text().splitlines():
        row = json.loads(line)
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", json.dumps(row)), row


def test_a_second_share_does_not_double_send():
    """Per-recipient idempotency: once a recipient is in `sent_to`, a second
    fan-out (a retry, a race loser that somehow got through) sends nothing."""
    rec = _carded()
    client = _FakeClient()
    mr.process_share(rec, client)
    again = mr.get_record(rec["recap_id"])
    sent, failed, resolved = mr.process_share(again, client)
    assert (sent, failed, resolved) == (1, 0, True)
    assert len(client.posted) == 1
    ledger = _ledger_text()
    assert ledger.count('"event": "sent"') == 1


def test_partial_failure_returns_to_pending_and_retries_only_the_remainder():
    """Two recipients; the second DM fails. The row goes back to PENDING with
    the first recorded, the outcome says '1 of 2', and the retry reaches ONLY
    the second -- the first is never DM'd twice."""
    t = _transcript(meeting_attendees=[
        {"displayName": None, "email": ORGANIZER_EMAIL},
        {"displayName": None, "email": INTERNAL_A_EMAIL},
        {"displayName": None, "email": INTERNAL_B_EMAIL},
    ])
    rec = _carded(t)
    assert rec["recipients"] == [INTERNAL_A, INTERNAL_B]
    flaky = _FakeClient(fail_for={INTERNAL_B})
    sent, failed, resolved = mr.process_share(rec, flaky)
    assert (sent, failed, resolved) == (1, 1, False)
    assert "1 of 2" in mr.outcome_text(rec, sent=sent, failed=failed)
    stored = mr.get_record(rec["recap_id"])
    assert stored["state"] == mr.STATE_PENDING
    assert stored["sent_to"] == [INTERNAL_A]
    assert '"event": "send_failed"' in _ledger_text()

    # The retry: the row is claimable again and only B is sent.
    claimed, refusal = mr.claim_for_tap(rec["recap_id"], ORGANIZER_ID)
    assert claimed is not None, refusal
    ok = _FakeClient()
    sent, failed, resolved = mr.process_share(claimed, ok)
    assert (sent, failed, resolved) == (2, 0, True)
    assert ok.opened == [INTERNAL_B]
    sent_rows = [json.loads(l) for l in _ledger_text().splitlines() if '"event": "sent"' in l]
    assert sorted(r["recipient_id"] for r in sent_rows) == [INTERNAL_A, INTERNAL_B]


def test_a_tap_cannot_widen_the_recipient_set():
    """Recipients are frozen into the record at card time; the fan-out reads
    them from the record and nothing else. Even a hand-edited later event
    adding an id after a SHARED state is ignored by the sticky fold."""
    rec = _carded()
    mr.process_share(rec, _FakeClient())
    mr._append_event(rec["recap_id"], "state",
                     {"state": mr.STATE_PENDING, "recipients": [INTERNAL_A, "U0INJECTEDX1"]})
    stored = mr.get_record(rec["recap_id"])
    assert stored["state"] == mr.STATE_SHARED


def test_dismiss_sends_nothing_and_says_so():
    rec = _carded()
    mr.mark_dismissed(rec)
    assert mr.get_record(rec["recap_id"])["state"] == mr.STATE_DISMISSED
    assert '"event": "sent"' not in _ledger_text()
    assert "sent nothing" in mr.outcome_text(rec, sent=0, failed=0, dismissed=True)


def test_outcome_narrates_only_what_the_ledger_shows():
    rec = {"recipients": [INTERNAL_A, INTERNAL_B]}
    assert "2 of 2" in mr.outcome_text(rec, sent=2, failed=0)
    partial = mr.outcome_text(rec, sent=1, failed=1)
    assert "1 of 2" in partial and "1 DM(s) failed" in partial
    assert "white_check_mark" not in partial


# ── 5. the card and the DM ──────────────────────────────────────────────────

def test_card_names_recipients_as_mentions_and_carries_no_address_or_em_dash():
    rec = _carded()
    text = mr.build_card_text(rec)
    assert f"<@{INTERNAL_A}>" in text
    assert "@northwind.test" not in text and EXTERNAL_EMAIL not in text
    assert chr(0x2014) not in text, "D-109: no em-dashes in card text"
    assert "Nothing has been sent" in text
    assert text.rstrip().endswith(mr.AFFORDANCE_LINE)
    assert "outside the workspace" in text, "the card says externals get nothing"


def test_card_blocks_carry_buttons_with_the_recap_id_as_value():
    rec = _carded()
    fallback, blocks = mr.build_card_blocks(rec)
    actions = [b for b in blocks if b.get("type") == "actions"]
    assert len(actions) == 1
    ids = {e["action_id"] for e in actions[0]["elements"]}
    assert ids == {mr.ACTION_SHARE, mr.ACTION_DISMISS}
    assert all(e["value"] == rec["recap_id"] for e in actions[0]["elements"])
    assert "Quartz Lantern Sync" in fallback


def test_lex_card_states_the_custodian_rule():
    custodians = frozenset({ORGANIZER_ID, INTERNAL_A})
    rec, _ = _prepare(entity="LEX", custodian_loader=lambda: custodians)
    assert "PHI custodians" in mr.build_card_text(rec)


def test_dm_text_names_the_sharer_and_labels_the_ai_summary():
    rec = _carded()
    text = mr.build_dm_text(rec)
    assert f"<@{ORGANIZER_ID}>" in text
    assert "Fireflies' AI summary" in text
    assert "a lead, not a record" in text
    assert "Full transcript in Fireflies" in text
    assert chr(0x2014) not in text


def test_attribution_footer_is_shape_only():
    """The diarization footer must carry no speaker names -- it says only that
    attribution looked unreliable."""
    rec, _ = mr.prepare_card(_transcript(), entity="F3E", email_to_slack=EMAIL_TO_SLACK,
                             attribution_unreliable=True, meeting_date="2026-09-19")
    body = mr._recap_body(rec)
    assert "attribution on this transcript looked unreliable" in body


def test_the_footer_is_registered_so_the_terminal_strip_is_not_a_no_op():
    """C4: strip_card_affordance is a closed tuple; an unregistered footer would
    leave a resolved card advertising a dead button."""
    assert mr.AFFORDANCE_LINE in kr._CARD_AFFORDANCE_LINES
    text = "body\n" + mr.AFFORDANCE_LINE
    assert kr.strip_card_affordance(text) != text


def test_a_resolved_card_drops_buttons_and_footer_and_names_the_outcome():
    rec = _carded()
    _, blocks = mr.build_card_blocks(rec)
    outcome = mr.outcome_text(rec, sent=1, failed=0)
    out = kr.terminal_card_blocks(blocks, outcome)
    assert not [b for b in out if b.get("type") == "actions"]
    assert mr.AFFORDANCE_LINE not in json.dumps(out)
    assert outcome in json.dumps(out)


# ── 6. wiring: app.py handler, conftest redirects, D-011 ────────────────────

def _body(recap_id: str, user: str, action_id: str, blocks: list | None = None) -> dict:
    return {
        "user": {"id": user},
        "channel": {"id": "D0ORG"},
        "message": {"ts": "1758300000.1", "blocks": blocks or []},
        "actions": [{"action_id": action_id, "value": recap_id}],
    }


def test_both_recap_actions_are_registered_not_orphaned_decorators():
    """A decorator on a function the app never registers is a silent dead
    handler -- that exact defect shipped once in this repo."""
    from cora import app as app_mod
    names = {getattr(l.ack_function, "__name__", "") for l in app_mod.app._listeners}
    assert "handle_meeting_recap_share" in names
    assert "handle_meeting_recap_dismiss" in names


def test_recap_handlers_are_registered_after_the_meeting_ask_siblings():
    """Order pin: the recap block sits directly under the S3 block it copies,
    so a reader finds the two propose-only card surfaces together."""
    src = Path(mr.__file__).with_name("app.py").read_text(encoding="utf-8")
    assert src.index("def _handle_meeting_ask_tap") < src.index("def _handle_meeting_recap_tap")
    assert src.index("@app.action(meeting_asks.ACTION_DISMISS)") \
        < src.index("@app.action(meeting_recap.ACTION_SHARE)")


def test_non_addressee_tap_posts_a_thread_refusal_and_leaves_the_card(monkeypatch):
    from cora import app as app_mod
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    rec = _carded()
    client = MagicMock()
    app_mod._handle_meeting_recap_tap(_body(rec["recap_id"], OTHER, mr.ACTION_SHARE),
                                      client, share=True)
    assert not client.chat_update.called
    client.chat_postMessage.assert_called_once()
    kw = client.chat_postMessage.call_args[1]
    assert kw["thread_ts"] == "1758300000.1" and "someone else" in kw["text"]
    assert not client.conversations_open.called
    assert mr.get_record(rec["recap_id"])["state"] == mr.STATE_PENDING


def test_share_tap_fans_out_and_resolves_the_card(monkeypatch):
    from cora import app as app_mod
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    rec = _carded()
    _, blocks = mr.build_card_blocks(rec)
    client = _FakeClient()
    app_mod._handle_meeting_recap_tap(
        _body(rec["recap_id"], ORGANIZER_ID, mr.ACTION_SHARE, blocks), client, share=True)
    assert client.opened == [INTERNAL_A]
    assert len(client.posted) == 1
    assert len(client.updated) == 1
    edited = client.updated[0]["blocks"]
    assert not [b for b in edited if b.get("type") == "actions"]
    assert "1 of 1" in client.updated[0]["text"]
    assert mr.get_record(rec["recap_id"])["state"] == mr.STATE_SHARED
    assert EXTERNAL_EMAIL not in json.dumps(client.posted) + json.dumps(client.updated)


def test_a_second_share_tap_through_the_handler_sends_nothing(monkeypatch):
    from cora import app as app_mod
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    rec = _carded()
    client = _FakeClient()
    body = _body(rec["recap_id"], ORGANIZER_ID, mr.ACTION_SHARE)
    app_mod._handle_meeting_recap_tap(body, client, share=True)
    app_mod._handle_meeting_recap_tap(body, client, share=True)
    assert client.opened == [INTERNAL_A]
    dm_posts = [p for p in client.posted if not p.get("thread_ts")]
    assert len(dm_posts) == 1
    refusals = [p for p in client.posted if p.get("thread_ts")]
    assert len(refusals) == 1 and "already" in refusals[0]["text"].lower()


def test_dismiss_tap_sends_nothing_and_resolves(monkeypatch):
    from cora import app as app_mod
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    rec = _carded()
    client = _FakeClient()
    app_mod._handle_meeting_recap_tap(
        _body(rec["recap_id"], ORGANIZER_ID, mr.ACTION_DISMISS), client, share=False)
    assert client.opened == [] and client.posted == []
    assert len(client.updated) == 1 and "sent nothing" in client.updated[0]["text"]
    assert mr.get_record(rec["recap_id"])["state"] == mr.STATE_DISMISSED


def test_partial_failure_through_the_handler_keeps_the_buttons(monkeypatch):
    from cora import app as app_mod
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    t = _transcript(meeting_attendees=[
        {"displayName": None, "email": ORGANIZER_EMAIL},
        {"displayName": None, "email": INTERNAL_A_EMAIL},
        {"displayName": None, "email": INTERNAL_B_EMAIL},
    ])
    rec = _carded(t)
    client = _FakeClient(fail_for={INTERNAL_B})
    app_mod._handle_meeting_recap_tap(
        _body(rec["recap_id"], ORGANIZER_ID, mr.ACTION_SHARE), client, share=True)
    assert client.updated == [], "a retryable outcome must not drop the buttons"
    notes = [p for p in client.posted if p.get("thread_ts")]
    assert len(notes) == 1 and "1 of 2" in notes[0]["text"]
    assert mr.get_record(rec["recap_id"])["state"] == mr.STATE_PENDING


def test_eval_mode_short_circuits_the_handler(monkeypatch):
    from cora import app as app_mod
    monkeypatch.setenv("CORA_EVAL_MODE", "1")
    rec = _carded()
    client = _FakeClient()
    app_mod._handle_meeting_recap_tap(
        _body(rec["recap_id"], ORGANIZER_ID, mr.ACTION_SHARE), client, share=True)
    assert client.opened == [] and client.posted == [] and client.updated == []


def test_conftest_redirects_both_new_write_paths_to_tmp():
    """A new write path needs its conftest redirect in the same change. Both
    env vars are set by the autouse fixture and point OUTSIDE the repo."""
    repo = Path(mr.__file__).resolve().parents[2]
    for var, fn in (("MEETING_RECAP_PENDING_PATH", mr.pending_path),
                    ("MEETING_RECAP_LEDGER_PATH", mr.ledger_path)):
        assert os.environ.get(var), f"{var} not redirected by conftest"
        assert repo not in fn().resolve().parents, f"{var} points inside the repo"


def test_the_real_paths_are_in_the_session_guard():
    conftest = Path(__file__).with_name("conftest.py").read_text(encoding="utf-8")
    assert '"data/state/meeting-recap-pending.jsonl"' in conftest
    assert '"logs/meeting-recap-ledger.jsonl"' in conftest


def test_review_lanes_gains_no_lane_and_can_approve_is_not_widened():
    """D-011 is structural in review_lanes. A meeting_recap type still lands in
    the Harrison-only operational lane and a non-founder cannot approve it;
    the recipient authority lives in claim_for_tap instead."""
    assert review_lanes.lane_for("meeting_recap", {}) == review_lanes.LANE_OPERATIONAL
    row = {"update_type": "meeting_recap", "payload": {"entity": "F3E"}}
    assert review_lanes.can_approve(row, HARRISON) is True
    assert review_lanes.can_approve(row, OTHER) is False
    assert review_lanes.MECHANICAL_TYPES == frozenset(
        {"asana_task", "task_close", "hubspot_note"})


# ── the runner hook (script-side; loaded without running main()) ─────────────

def _runner():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "scripts" / "run_meeting_ask_capture.py"
    spec = importlib.util.spec_from_file_location("_mac_recap", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runner_query_only_uses_fields_the_live_ingest_query_already_fetches():
    """One unknown GraphQL field fails the WHOLE poll -- the S3 ask lane too.
    Every recap field must therefore already exist in the ingest query that
    runs live nightly. Nothing guessed."""
    from cora.connectors import fireflies_connector as ffc
    m = _runner()
    fields = re.findall(r"\b([a-z_]+)\b", m._RECAP_FIELDS)
    assert "transcript_url" in fields and "summary" in fields
    for f in fields:
        assert f in ffc._TRANSCRIPTS_QUERY, f"{f} is not in the live ingest query"
    assert "transcript_url" in m._ASK_QUERY and "summary {" in m._ASK_QUERY
    assert "transcript_url" in m._ASK_QUERY_BY_ID and "summary {" in m._ASK_QUERY_BY_ID
    assert "start_time" in m._ASK_QUERY, "the ask lane's own field survived the edit"


def test_process_recap_dry_run_cards_nothing(monkeypatch, capsys):
    m = _runner()
    monkeypatch.setattr(m, "_excluded", lambda t: "")
    monkeypatch.setattr(m, "_entity_for", lambda t: "F3E")
    out = m.process_recap(_transcript(), dry_run=True, email_to_slack=EMAIL_TO_SLACK)
    assert out["carded"] == 1
    assert not mr.pending_path().exists()
    printed = capsys.readouterr().out
    assert "WOULD CARD" in printed
    assert EXTERNAL_EMAIL not in printed


def test_process_recap_posts_one_organizer_card_and_records_pending(monkeypatch):
    m = _runner()
    monkeypatch.setattr(m, "_excluded", lambda t: "")
    monkeypatch.setattr(m, "_entity_for", lambda t: "F3E")
    client = _FakeClient()
    out = m.process_recap(_transcript(), dry_run=False, email_to_slack=EMAIL_TO_SLACK,
                          client=client)
    assert out["carded"] == 1
    assert client.opened == [ORGANIZER_ID]
    assert len(client.posted) == 1
    rec = mr.get_record("T-quartz-lantern-01")
    assert rec["state"] == mr.STATE_PENDING
    assert rec["recipients"] == [INTERNAL_A]
    assert rec["card_message_ts"] == "999.9"
    # Second poll of the same window: nothing re-carded.
    again = m.process_recap(_transcript(), dry_run=False, email_to_slack=EMAIL_TO_SLACK,
                            client=client)
    assert again["carded"] == 0 and again["skipped"] == "already carded"
    assert len(client.posted) == 1


def test_process_recap_respects_the_ask_lanes_exclusions(monkeypatch):
    """An excluded meeting (COPA / LEX hard-exclude / PHI title) gets no recap
    card either -- the exclusion is re-run as a belt inside process_recap."""
    m = _runner()
    monkeypatch.setattr(m, "_excluded", lambda t: "LEX hard-exclude (program)")
    client = _FakeClient()
    out = m.process_recap(_transcript(), dry_run=False, email_to_slack=EMAIL_TO_SLACK,
                          client=client)
    assert out["excluded"] and out["carded"] == 0
    assert client.posted == [] and not mr.pending_path().exists()


def test_process_recap_post_failure_records_nothing_so_the_next_run_retries(monkeypatch):
    m = _runner()
    monkeypatch.setattr(m, "_excluded", lambda t: "")
    monkeypatch.setattr(m, "_entity_for", lambda t: "F3E")
    client = _FakeClient(fail_for={ORGANIZER_ID})
    out = m.process_recap(_transcript(), dry_run=False, email_to_slack=EMAIL_TO_SLACK,
                          client=client)
    assert out["carded"] == 0 and "failed" in out["skipped"]
    assert not mr.already_carded("T-quartz-lantern-01")


def test_main_loop_calls_the_recap_hook_only_for_non_excluded_meetings():
    """Source pin on the wiring: the recap rides the same poll, inside the
    not-excluded branch, so an excluded meeting gets neither product."""
    src = (Path(__file__).resolve().parent.parent / "scripts"
           / "run_meeting_ask_capture.py").read_text(encoding="utf-8")
    loop = src[src.index("for t in transcripts:"):src.index("_write_watermark(run_start)")]
    assert "process_recap(" in loop
    assert loop.index('if res["excluded"]:') < loop.index("process_recap(")
    assert "else:" in loop[loop.index('if res["excluded"]:'):loop.index("process_recap(")]
