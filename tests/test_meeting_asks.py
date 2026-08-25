"""S3 (cq-f52c6b691127): explicit in-meeting Cora asks become propose-only cards.

The properties under test, in the order they matter:

  1. NOTHING EXECUTES WITHOUT A TAP. Detection is pure; the store records a
     proposal; only the tap handler acts. (D-136, D-054.)
  2. THE DETECTOR IS NARROW IN BOTH DIRECTIONS. It catches a spoken request and
     refuses Cora-as-subject prose. This is the D-217/D-218 discipline: every
     filter here is pinned against the exact artifact it exists to stop AND
     against the shortest legitimate input, because the failure #6 shipped nine
     times was a fix that broke the thing next to it.
  3. ATTRIBUTION IS NOT TRUSTED. A diarization-flagged transcript addresses the
     meeting owner, never a named speaker.
  4. THE C4 CONTRACT HOLDS STRUCTURALLY. The card's footer is registered in
     knowledge_review._CARD_AFFORDANCE_LINES, so a resolved card cannot keep
     advertising a dead button -- the exact defect C4 shipped to fix.
  5. THE ADDRESSEE IS THE AUTHORITY, and D-011 is untouched: review_lanes gains
     no new lane and can_approve is not widened.
"""

from __future__ import annotations

import json

import pytest

from cora import knowledge_review as kr
from cora import meeting_asks as ma
from cora import review_lanes

HARRISON = "U0B2RM2JYJ1"
OTHER = "U0B3AEJCYGP"


def _sent(text: str, speaker: str = "Harrison Rogers", start: float = 12.0,
          index: int = 0) -> dict:
    return {"text": text, "speaker_name": speaker, "start_time": start, "index": index}


def _transcript(**over) -> dict:
    base = {
        "id": "T1",
        "title": "F3 Weekly",
        "date": 1787097600,
        "organizer_email": "harrison@hjrglobal.com",
        "host_email": "harrison@hjrglobal.com",
        # displayName is None on LIVE data for every human attendee -- only the
        # Fireflies bot carries one. The fixture says so, because a fixture that
        # populated it is what hid a matcher that could never work in production.
        "meeting_attendees": [
            {"displayName": None, "email": "harrison@hjrglobal.com"},
            {"displayName": None, "email": "justin@hjrglobal.com"},
        ],
        "sentences": [],
    }
    base.update(over)
    return base


# ── 2. the detector, both directions ─────────────────────────────────────────

@pytest.mark.parametrize("text,kind", [
    ("Cora, make a task to send Larry the deck.", ma.KIND_TASK),
    ("Hey Cora, can you create a task for me to follow up with Tommy?", ma.KIND_TASK),
    ("Cora, add a follow-up for the Costco meeting.", ma.KIND_TASK),
    ("Cora, remind me to call the broker on Friday.", ma.KIND_TASK),
    ("Cora, log a ticket for the Ellsworth repair.", ma.KIND_TASK),
    ("Cora, make a note that the Tucson vendor is Apex Appliance.", ma.KIND_NOTE),
    ("Cora, note this: we owe Justin the Q3 numbers.", ma.KIND_NOTE),
    ("Cora, log that the price moved to $25.15.", ma.KIND_NOTE),
    ("Cora, please draft an email to Larry about the invoice.", ma.KIND_OTHER),
])
def test_a_spoken_request_is_detected_and_classified(text, kind):
    got = ma.detect_asks([_sent(text)])
    assert len(got) == 1, f"not detected: {text}"
    assert got[0]["kind"] == kind


@pytest.mark.parametrize("text", [
    # Cora as SUBJECT -- someone talking ABOUT her, incl. D-136's laundering mode
    # where a human reads her output aloud in the first person.
    "Cora said she would make a task for that.",
    "Cora already created the task yesterday.",
    "I asked Cora to create a task yesterday.",
    "We should get Cora to make a task.",
    "Cora will draft the email later.",
    "Cora is going to note that.",
    # A question ABOUT Cora is not an instruction TO Cora.
    "Did Cora note that already?",
    "Can Cora make a task for this?",
    # Phrasal false positive: "make sure" creates nothing.
    "Cora, make sure that we send it today.",
    # Cora's ROLE being read off the participant list.
    "Cora, note taker.",
    "Cora note taking.",
    # A body whose only content word is Cora herself.
    "Cora, note for Cora.",
    # Substring: "cora" inside another word must not fire.
    "The corale winery is booked for Friday.",
    "Please check the corporate card statement.",
])
def test_prose_that_is_not_a_request_is_refused(text):
    assert ma.detect_asks([_sent(text)]) == [], f"false positive: {text}"


def test_the_shortest_legitimate_asks_still_survive_the_junk_floor():
    """The floor exists to stop "note taker" / "note for Cora". It must not stop a
    real short request -- the failure mode #6 shipped when a quality floor
    rejected "$25.15" and "Net 30" as insubstantial."""
    for text in ("Cora, make a task to call Justin.",
                 "Cora, note the payoff includes legal fees.",
                 "Cora, log a ticket for the sauna."):
        assert ma.detect_asks([_sent(text)]), f"floor rejected a real ask: {text}"


def test_an_ask_with_no_timestamp_is_not_proposable():
    """D-136 asks for a quoted line WITH a timestamp. An ungroundable proposal is
    dropped rather than carded without provenance."""
    s = _sent("Cora, make a task to send the deck.")
    del s["start_time"]
    assert ma.detect_asks([s]) == []


def test_a_zero_second_offset_is_a_real_timestamp():
    """0.0 is a legitimate offset (an ask in the first second), so the presence
    check must not be a truthiness check."""
    assert ma.detect_asks([_sent("Cora, make a task to call Justin.", start=0.0)])


def test_detection_is_pure_and_carries_the_verbatim_line():
    text = "Cora, make a task to send Larry the deck."
    got = ma.detect_asks([_sent(text, start=93.0)])[0]
    assert got["quoted_line"] == text
    assert got["start_time"] == 93.0
    assert ma.format_offset(93.0) == "1:33"


def test_malformed_sentence_rows_never_raise():
    assert ma.detect_asks(None) == []
    assert ma.detect_asks([None, 42, "string", {}, {"text": None}]) == []


# ── the per-meeting cap is reported, never silent ────────────────────────────

def test_the_per_meeting_cap_reports_what_it_held_back():
    asks = [{"n": i} for i in range(ma.MAX_ASKS_PER_MEETING + 4)]
    kept, overflow = ma.cap_overflow(asks)
    assert len(kept) == ma.MAX_ASKS_PER_MEETING
    assert overflow == 4
    assert ma.cap_overflow([{"n": 1}]) == ([{"n": 1}], 0)


# ── 3. attribution ───────────────────────────────────────────────────────────

def test_a_diarization_flagged_transcript_addresses_the_owner_not_the_speaker():
    ask = {"speaker": "Justin Moran"}
    sid, email, why = ma.resolve_addressee(
        ask, _transcript(), attribution_unreliable=True,
        email_to_slack={"harrison@hjrglobal.com": HARRISON,
                        "justin@hjrglobal.com": OTHER},
    )
    assert sid == HARRISON, "a flagged transcript must not address a named speaker"
    assert email == "harrison@hjrglobal.com"
    assert "attribution unreliable" in why


def test_a_clean_transcript_addresses_the_speaker_who_spoke():
    """Resolved through the roster and CONFIRMED against the attendee list."""
    sid, email, why = ma.resolve_addressee(
        {"speaker": "Justin Moran"}, _transcript(), attribution_unreliable=False,
        email_to_slack={"harrison@hjrglobal.com": HARRISON,
                        "justin@hjrglobal.com": OTHER},
    )
    assert sid == OTHER and email == "justin@hjrglobal.com"
    assert "the speaker who made the ask" in why


def test_the_matcher_resolves_a_speaker_through_the_roster_not_display_name():
    """THE defect this shape exists for. `meeting_attendees[].displayName` is None
    for every human on live data -- only the Fireflies notetaker carries one -- so
    matching speaker_name against displayName could never succeed in production and
    every card silently fell back to the meeting owner. Found by running the
    capture against 25 real meetings; the fixtures had displayName populated
    because I wrote them."""
    atts = [{"displayName": None, "email": "hannah@hjrglobal.com"},
            {"displayName": None, "email": "harrison@hjrglobal.com"}]
    assert ma.match_speaker_to_attendee("Hannah Grant", atts) == "hannah@hjrglobal.com"
    assert ma.match_speaker_to_attendee("Harrison Rogers", atts) == "harrison@hjrglobal.com"
    # An unambiguous first name resolves too (the roster matcher's step 2).
    assert ma.match_speaker_to_attendee("Hannah", atts) == "hannah@hjrglobal.com"


def test_a_resolved_speaker_who_was_not_in_the_meeting_is_refused():
    """The roster is global; the attendee list is what proves presence. Without
    this check a name resolved through the roster would address someone who was
    never in the room. Measured live: a Finance meeting whose speaker labels
    include Harrison while its attendee list does not."""
    atts = [{"displayName": None, "email": "justin@hjrglobal.com"}]
    assert ma.match_speaker_to_attendee("Harrison Rogers", atts) == ""


def test_speaker_matching_is_not_a_substring_test():
    """The 2026-06-13 sweep shipped a substring matcher that assigned "Lex" to
    "Alex". Here the same bug DMs the wrong person. The roster matcher this
    delegates to carries the anti-substring fix."""
    atts = [{"displayName": None, "email": "alex@f3energy.com"},
            {"displayName": None, "email": "hannah@hjrglobal.com"}]
    assert ma.match_speaker_to_attendee("Lex", atts) == ""
    assert ma.match_speaker_to_attendee("Ann", atts) == ""


def test_an_empty_attendee_list_matches_nobody():
    assert ma.match_speaker_to_attendee("Hannah Grant", []) == ""
    assert ma.match_speaker_to_attendee("Hannah Grant", None) == ""


def test_a_recording_with_no_attendee_list_says_so_rather_than_blaming_the_label():
    """Two fallbacks that look identical to a reader mean different things: a
    personal recording carries NO attendee list, while a calendar meeting can have
    a speaker who is not on its invite."""
    t = _transcript(meeting_attendees=[])
    _, _, why = ma.resolve_addressee(
        {"speaker": "Hannah Grant"}, t, attribution_unreliable=False,
        email_to_slack={"harrison@hjrglobal.com": HARRISON})
    assert "no attendee list" in why

    _, _, why2 = ma.resolve_addressee(
        {"speaker": "Larry Stone"}, _transcript(), attribution_unreliable=False,
        email_to_slack={"harrison@hjrglobal.com": HARRISON})
    assert "isn't on this meeting's attendee list" in why2


def test_no_addressable_recipient_means_no_proposal():
    sid, _, _ = ma.resolve_addressee(
        {"speaker": "Nobody"}, _transcript(organizer_email="", host_email=""),
        attribution_unreliable=False, email_to_slack={},
    )
    assert sid == "", "an unaddressable ask must not be carded"


# ── the durable store ────────────────────────────────────────────────────────

def _record(**over):
    base = dict(
        ask_id="a1", transcript_id="T1", meeting_title="F3 Weekly",
        meeting_date="2026-08-19", entity="F3E", kind=ma.KIND_TASK,
        body="send Larry the deck", quoted_line="Cora, make a task to send Larry the deck.",
        start_time=93.0, speaker="Harrison Rogers", addressee_id=HARRISON,
        addressee_email="harrison@hjrglobal.com", routing_reason="addressed to the speaker who made the ask",
        dm_channel_id="D1", card_message_ts="1787097600.001",
    )
    base.update(over)
    return base


def test_the_ask_key_is_stable_across_processes():
    """hashlib, never the builtin hash(): siphash is randomised per interpreter,
    which is what made every downstream dedup a silent no-op in C6 and filed one
    vendor quote six times."""
    a = ma.ask_key("T1", "Cora, make a task.", 93.0)
    b = ma.ask_key("T1", "Cora,  make   a task.", 93)
    assert a == b, "normalisation should make these the same ask"
    assert a != ma.ask_key("T2", "Cora, make a task.", 93.0)


def test_a_resolved_ask_never_comes_back(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(tmp_path / "s.json"))
    assert ma.already_carded("a1") is False
    ma.record_card(**_record())
    assert ma.already_carded("a1") is True
    ma.mark_state("a1", ma.STATE_DISMISSED)
    # Any state, not just PENDING -- otherwise the push becomes the nag D-054
    # retired.
    assert ma.already_carded("a1") is True


def test_the_store_survives_a_malformed_file(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(p))
    assert ma.pending_records() == []
    assert ma.already_carded("a1") is False


def test_the_state_path_is_read_per_call(tmp_path, monkeypatch):
    """A module-level constant reading os.environ is the cq-06f4797db4f1 class:
    frozen at bot start, and it defeats test isolation."""
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(tmp_path / "one.json"))
    ma.record_card(**_record())
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(tmp_path / "two.json"))
    assert ma.already_carded("a1") is False


# ── 5. the addressee is the authority ────────────────────────────────────────

def test_only_the_addressee_can_tap(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(tmp_path / "s.json"))
    ma.record_card(**_record())
    rec, refusal = ma.claim_for_tap("a1", OTHER)
    assert rec is None and "addressed to someone else" in refusal
    rec, refusal = ma.claim_for_tap("a1", HARRISON)
    assert rec is not None and refusal == ""


def test_the_claim_is_atomic_so_two_taps_cannot_both_execute(tmp_path, monkeypatch):
    """THE button-tap race. Without an atomic claim both taps read PENDING and
    both execute -- and for a task ask that is TWO Asana tasks from one card.
    This repo has shipped this class twice (cq-883878e81274, cq-056a3a4de2f7)."""
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(tmp_path / "s.json"))
    ma.record_card(**_record())
    first, _ = ma.claim_for_tap("a1", HARRISON)
    second, refusal = ma.claim_for_tap("a1", HARRISON)
    assert first is not None, "the first tap must win"
    assert second is None, "the second tap must NOT also get the record"
    assert "already" in refusal.lower()


def test_a_failed_execution_releases_the_claim_for_a_retry(tmp_path, monkeypatch):
    """CLAIMED must not be terminal, or a transient Asana failure wedges the card
    forever behind "I'm working on that one already"."""
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(tmp_path / "s.json"))
    ma.record_card(**_record())
    assert ma.claim_for_tap("a1", HARRISON)[0] is not None
    ma.mark_state("a1", ma.STATE_PENDING)          # what the handler does on failure
    assert ma.claim_for_tap("a1", HARRISON)[0] is not None, "retry must be possible"
    assert ma.STATE_CLAIMED not in ma._TERMINAL


def test_the_ts_fallback_still_finds_a_card_posted_before_the_id_rode_along(
        tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(tmp_path / "s.json"))
    ma.record_card(**_record())
    rec, refusal = ma.claim_for_tap("", HARRISON, message_ts="1787097600.001")
    assert rec is not None and refusal == ""


def test_an_empty_stored_addressee_is_a_refusal_not_a_wildcard(tmp_path, monkeypatch):
    """Fail CLOSED on the only authority check there is. review_lanes settled the
    same argument for the entity field: an unknown on an authority boundary is a
    no."""
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(tmp_path / "s.json"))
    ma.record_card(**_record(addressee_id=""))
    rec, refusal = ma.claim_for_tap("a1", HARRISON)
    assert rec is None and "someone else" in refusal


def test_an_unidentifiable_tapper_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(tmp_path / "s.json"))
    ma.record_card(**_record())
    rec, refusal = ma.claim_for_tap("a1", "")
    assert rec is None and refusal


def test_a_second_tap_finds_the_card_already_handled(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(tmp_path / "s.json"))
    ma.record_card(**_record())
    ma.mark_state("a1", ma.STATE_ACCEPTED)
    rec, refusal = ma.claim_for_tap("a1", HARRISON)
    assert rec is None and "Already handled" in refusal


def test_an_expired_card_refuses_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(tmp_path / "s.json"))
    p = tmp_path / "s.json"
    ma.record_card(**_record())
    data = json.loads(p.read_text(encoding="utf-8"))
    data["a1"]["carded_at"] = "2020-01-01T00:00:00+00:00"
    p.write_text(json.dumps(data), encoding="utf-8")
    rec, refusal = ma.claim_for_tap("a1", HARRISON)
    assert rec is None
    assert "aged out" in refusal, "expiry must be reported, not silent"
    assert json.loads(p.read_text(encoding="utf-8"))["a1"]["state"] == ma.STATE_EXPIRED


def test_an_unknown_card_refuses_honestly(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_ASK_STATE_PATH", str(tmp_path / "s.json"))
    rec, refusal = ma.claim_for_tap("nope", HARRISON, message_ts="nope")
    assert rec is None and "can't find this card" in refusal


def test_review_lanes_gains_no_lane_and_can_approve_is_not_widened():
    """D-011 is structural in review_lanes. S3 must not have touched it: a
    meeting_ask type still lands in the Harrison-only operational lane, and a
    non-founder still cannot approve it there."""
    assert review_lanes.lane_for("meeting_ask", {}) == review_lanes.LANE_OPERATIONAL
    row = {"update_type": "meeting_ask", "payload": {"entity": "F3E"}}
    assert review_lanes.can_approve(row, HARRISON) is True
    assert review_lanes.can_approve(row, OTHER) is False
    assert review_lanes.MECHANICAL_TYPES == frozenset(
        {"asana_task", "task_close", "hubspot_note"})


# ── 4. the C4 contract ───────────────────────────────────────────────────────

def test_the_card_footer_is_registered_so_the_strip_is_not_a_no_op():
    """strip_card_affordance is driven by a CLOSED tuple of literals and returns
    its input unchanged for an unknown footer. An unregistered footer therefore
    means a resolved card silently keeps advertising a dead button."""
    assert ma.AFFORDANCE_LINE in kr._CARD_AFFORDANCE_LINES
    text = "card body\n" + ma.AFFORDANCE_LINE
    assert kr.strip_card_affordance(text) != text


def test_the_footer_is_stripped_in_slack_read_back_form_too():
    """Slack normalises emoji-presentation Unicode to its shortcode on read-back,
    and both card consumers read the card back FROM Slack. Matching only the
    literal emoji is how the first cut of this strip shipped dead."""
    shortcoded = ("card body\n" + ma.AFFORDANCE_LINE
                  .replace("\U0001F44D", ":+1:").replace("\U0001F44E", ":-1:"))
    assert kr.strip_card_affordance(shortcoded) != shortcoded


def test_a_resolved_card_drops_its_buttons_and_names_the_outcome():
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": "body\n" + ma.AFFORDANCE_LINE}},
        {"type": "actions", "elements": [{"type": "button"}]},
    ]
    outcome = ma.outcome_text("ACCEPTED", ma.KIND_TASK)
    out = kr.terminal_card_blocks(blocks, outcome)
    assert not [b for b in out if b.get("type") == "actions"]
    assert ma.AFFORDANCE_LINE not in json.dumps(out)
    assert outcome in json.dumps(out)


@pytest.mark.parametrize("kind,expect", [
    (ma.KIND_TASK, "Asana"),
    (ma.KIND_NOTE, "personal notes"),
])
def test_the_outcome_names_the_store_the_item_landed_in(kind, expect):
    """The C4 rule, and the reason outcome_text exists at all: one string for
    every type had rendered "Saved to Cora's known-answers" over an item that
    went to the efficiency backlog."""
    assert expect in ma.outcome_text("ACCEPTED", kind)


def test_a_failed_execution_is_never_rendered_as_success():
    text = ma.outcome_text("ACCEPTED", ma.KIND_TASK, success=False, detail="Asana said no.")
    assert "couldn't" in text and "nothing was created" in text.lower()
    assert "white_check_mark" not in text


def test_a_dismissal_says_nothing_was_created():
    assert "didn't create" in ma.outcome_text("DISMISSED", ma.KIND_TASK)


# ── 1. the card promises only what it can do ─────────────────────────────────

def test_the_card_quotes_the_line_with_its_timestamp():
    text = ma.build_card_text(_record())
    assert "Cora, make a task to send Larry the deck." in text
    assert "[1:33]" in text, "D-136 requires the timestamp on the quoted line"
    assert "F3 Weekly" in text


def test_the_card_says_nothing_has_happened_yet():
    text = ma.build_card_text(_record())
    assert "have not done anything yet" in text


def test_an_unactionable_kind_gets_no_affordance_and_promises_nothing():
    """A draft/send ask is real but is an egress class this slice does not open.
    The card hands it back instead of implying a capability -- the defect C4
    found was a card claiming Cora carried out a HubSpot note she never writes."""
    text = ma.build_card_text(_record(kind=ma.KIND_OTHER, body="draft an email to Larry"))
    assert ma.AFFORDANCE_LINE not in text
    assert "can't act on this one from a card" in text


def test_an_owner_routed_card_explains_why_it_came_to_them():
    text = ma.build_card_text(_record(
        routing_reason="attribution unreliable on this transcript -- addressed to "
                       "the meeting owner rather than a named speaker"))
    assert "Routing:" in text and "attribution unreliable" in text


def test_a_speaker_routed_card_does_not_add_routing_noise():
    assert "Routing:" not in ma.build_card_text(_record())


# ── the poll window (S3 trigger) ─────────────────────────────────────────────
#
# These live here rather than in a script test file because the window rule is
# the correctness core of the trigger, and getting it wrong is SILENT.

def _window_mod():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "scripts" / "run_meeting_ask_capture.py"
    spec = importlib.util.spec_from_file_location("_mac", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_fresh_watermark_does_not_narrow_the_poll_window():
    """THE defect this rule exists for. Fireflies `transcripts(fromDate, toDate)`
    filters on MEETING DATE, not on when the transcript became available -- a
    transcript appears minutes to hours after the meeting ends. So narrowing the
    window to the last run time (`max(watermark, floor)`) would make a 10:00
    meeting whose transcript lands at 10:45 permanently invisible: every 15-minute
    poll would ask for meetings dated after the previous poll, and that meeting is
    always older than that. The watermark may only WIDEN."""
    import time
    m = _window_mod()
    now = int(time.time())
    start = m._window_start(now - 900, 24)   # last run 15 minutes ago
    assert now - start >= 23 * 3600, (
        "a 15-minute-old watermark must not shrink the window to 15 minutes")


def test_no_watermark_uses_the_fixed_lookback():
    import time
    m = _window_mod()
    now = int(time.time())
    assert 23 * 3600 <= now - m._window_start(0, 24) <= 25 * 3600


def test_a_stale_watermark_widens_the_window_to_recover():
    """An outage must not create a permanent hole."""
    import time
    m = _window_mod()
    now = int(time.time())
    start = m._window_start(now - 5 * 86400, 24)
    assert now - start >= 4 * 86400


def test_an_ancient_watermark_is_capped():
    """Otherwise one call asks Fireflies for months of transcripts WITH FULL
    SENTENCES."""
    import time
    m = _window_mod()
    now = int(time.time())
    start = m._window_start(now - 365 * 86400, 24)
    assert now - start <= m._MAX_LOOKBACK_HOURS * 3600 + 60


def test_a_zero_or_negative_lookback_never_produces_a_future_window():
    import time
    m = _window_mod()
    now = int(time.time())
    for hours in (0, -5):
        assert m._window_start(0, hours) <= now


# ── D-051 review fixes (2026-08-25) ─────────────────────────────────────────
#
# Every test below pins a defect the adversarial review found in this session's
# own code. They are grouped rather than scattered so the next reader can see
# what the review actually bought.

@pytest.mark.parametrize("text", [
    # A REPORTING FRAME quoting an address to Cora is not an address to Cora.
    # This was the leak in the slice's central safety claim, and in a company
    # that talks about Cora constantly it is a high-volume shape -- including
    # D-136's laundering mode, where somebody reads her output aloud.
    "Harrison said, Cora, make a task for that, but it never showed up.",
    "So the way it works is you just say, Cora, make a task for the deck.",
    "I told him, Cora, note this down.",
    "She asked, Cora, create a ticket.",
    "He types, Cora, make a note of the price.",
])
def test_a_quoted_address_is_not_an_address(text):
    assert ma.detect_asks([_sent(text)]) == [], f"reported speech carded: {text}"


def test_a_real_address_survives_the_reported_speech_veto():
    """The veto must not eat the thing it sits next to."""
    for text in ("Cora, make a task for that.",
                 "Hey Cora, make a task for the deck.",
                 "So, Cora, make a note about the payoff."):
        assert ma.detect_asks([_sent(text)]), f"veto ate a real ask: {text}"


@pytest.mark.parametrize("text,expect_in_body", [
    ("Cora, log that the price moved to $25.15.", "$25.15"),
    ("Cora, note the payoff is $1,234.56 plus fees.", "$1,234.56"),
    ("Cora, make a task to reconcile 1.5% of the OSN spend.", "1.5%"),
])
def test_the_body_is_not_cut_at_the_first_period(text, expect_in_body):
    """`[^.?!]{0,300}` stopped at the FIRST period, so every decimal figure was
    sliced in half -- and the BODY is what gets persisted as the Asana task or
    the note. A decimal point has no space after it, which is exactly what
    distinguishes it from a sentence end."""
    got = ma.detect_asks([_sent(text)])
    assert got, text
    assert expect_in_body in got[0]["body"], got[0]["body"]


def test_a_mid_request_abbreviation_is_a_known_limitation_not_a_silent_one():
    """"e.g. " is a period followed by a space, which is indistinguishable from a
    sentence end without an abbreviation dictionary. So the body stops there.
    Pinned deliberately: this is the residual after the decimal fix, it is small
    (the request's opening survives, only a trailing clause is lost), and pinning
    it stops someone "fixing" it by removing the sentence-end rule and
    reintroducing the decimal bug."""
    got = ma.detect_asks([_sent("Cora, make a note that Net 30 applies to e.g. Costco.")])
    assert got
    assert "Net 30" in got[0]["body"]
    assert "Costco" not in got[0]["body"]


def test_the_body_still_stops_at_the_end_of_the_request():
    """The window is now unrestricted, so something has to stop it -- a real
    sentence end (punctuation followed by whitespace)."""
    got = ma.detect_asks([_sent(
        "Cora, make a task to call Justin. Then we talked about something else "
        "entirely for a while.")])
    assert got
    assert "something else" not in got[0]["body"]
    assert "call Justin" in got[0]["body"]


@pytest.mark.parametrize("seconds,expect", [
    (93, "1:33"),
    (0, "0:00"),
    (59, "0:59"),
    (3599, "59:59"),
    (3600, "1:00:00"),
    (4523, "1:15:23"),      # 75 minutes in -- rendered "75:23" before the fix
    (10559, "2:55:59"),     # the live corpus holds a 176-minute meeting
])
def test_the_timestamp_rolls_over_past_the_hour(seconds, expect):
    """D-136's grounding stamp has to be a stamp somebody can scrub to."""
    assert ma.format_offset(seconds) == expect


@pytest.mark.parametrize("bad", [None, -1, "x", float("inf"), float("nan"), object()])
def test_a_junk_offset_degrades_instead_of_raising(bad):
    """`int(float('inf'))` raises OverflowError, and this is called from
    build_card_text -- letting it escape would take out the whole card."""
    assert ma.format_offset(bad) == "?"


def test_the_affordance_advertises_only_what_the_card_can_do():
    """The first cut led with a 👍/👎 emoji pair, copying the review cards --
    but those have a REACTION handler and this surface has none, so the card's
    most prominent affordance did nothing. That is the C4 defect exactly."""
    assert "\U0001F44D" not in ma.AFFORDANCE_LINE
    assert "\U0001F44E" not in ma.AFFORDANCE_LINE
    assert "Yes, do it" in ma.AFFORDANCE_LINE
    # ...and it must still be registered, or the terminal strip is a no-op.
    assert ma.AFFORDANCE_LINE in kr._CARD_AFFORDANCE_LINES
    text = "body\n" + ma.AFFORDANCE_LINE
    assert kr.strip_card_affordance(text) != text


def test_the_cap_bounds_the_total_not_the_rate():
    """The cap has to bound how many cards ONE MEETING can ever produce. Deduping
    before capping would free the slots on the next poll and deliver every
    detection inside an hour at a 15-minute interval -- the exact flood that
    retired the last push (D-054, "Demi's 14 unwanted tasks"). I made that change
    on a review finding and the adversarial verifier was right to refuse it; this
    pins the semantics so it does not get "fixed" again.

    Pinned on the docstring because the ordering itself lives in the runner: the
    contract is that over-cap asks are DROPPED, never queued.
    """
    doc = ma.cap_overflow.__doc__ or ""
    assert "DROPPED, NOT QUEUED" in doc
    kept, overflow = ma.cap_overflow([{"n": i} for i in range(9)])
    assert len(kept) == ma.MAX_ASKS_PER_MEETING and overflow == 6


def test_every_keyword_in_the_project_map_is_a_string():
    """A DRIFT GUARD for a live crash this session surfaced.

    `asana-project-map.yaml` carried UNQUOTED integers in two keyword lists
    ("1337", "750", "1555" -- addresses and a member target), and
    `project_resolver._norm` calls `.lower()` on each entry. So `resolve_project`
    raised TypeError for HJRP and bare-LEX on EVERY call, which broke the
    conversational `asana_create_task` path for those entities long before this
    slice existed. YAML turns a bare `1337` into an int silently, so nothing but a
    guard catches it.
    """
    import yaml
    from pathlib import Path
    raw = yaml.safe_load(
        (Path(__file__).resolve().parent.parent / "data" / "maps" /
         "asana-project-map.yaml").read_text(encoding="utf-8"))
    offenders = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, (dict, list)):
                    walk(v, f"{path}[{i}]")
                elif not isinstance(v, str):
                    offenders.append(f"{path}[{i}] = {v!r} ({type(v).__name__})")

    walk(raw, "")
    assert not offenders, (
        "quote these -- a non-string keyword makes resolve_project raise:\n  "
        + "\n  ".join(offenders))


def test_the_project_resolver_no_longer_raises_for_any_live_entity():
    """The entities that appear in real meeting titles must all route or return
    None -- never raise."""
    from cora.tools.project_resolver import resolve_project
    for entity in ("FNDR", "HJRG", "F3E", "OSN", "BDM", "HJRP", "UFL", "F3C",
                   "HJRPROD", "LEX", "LEX-LLC"):
        resolve_project(entity=entity, task_text="send Larry the deck",
                        assignee_gid="1204525779609669", meeting_title="F3 Weekly")
