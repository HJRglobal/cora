"""Tests for the Daily Personalized Knowledge Check core (knowledge_check.py).

Layer A -- roster pins: the roster is derived from a LOCKED source doc
(daily-schedules-and-kpis-v1 Instrumentation ledger), so drift in either
direction is a correctness bug, not a preference. These pin the derivation.

Layer B -- state machine: the append-only event log's fold, terminal
stickiness (D-096), and the send-ledger idempotency that stops a restart or a
re-run from double-DMing 13 real people.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sys

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import knowledge_check as kc


@pytest.fixture(autouse=True)
def _isolated_events(tmp_path, monkeypatch):
    """Every test gets its own event log. The real one is live pilot state."""
    monkeypatch.setattr(kc, "_events_path", lambda: tmp_path / "events.jsonl")
    yield


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv("CORA_KNOWLEDGE_CHECK", raising=False)
    yield


def _reserve_and_ask(user, date, cycle_id, item_key="k1", entity="F3E",
                     question="q?", tier=1):
    kc.append_event("reserved", cycle_id=cycle_id, user=user, date=date,
                    item_key=item_key, entity=entity, question=question, tier=tier)
    kc.append_event("asked", cycle_id=cycle_id, user=user, date=date,
                    message_ts="111.222", channel="D1")


# ---------------------------------------------------------------------------
# Layer A -- roster
# ---------------------------------------------------------------------------

def test_roster_loads_the_locked_13_plus_dogfood():
    pilot = kc.pilot_roster()
    assert len(pilot) == 13, [p["name"] for p in pilot]
    names = {p["name"] for p in pilot}
    assert names == {
        "Tommy Anderson", "Alex Cordova", "Elena Meirndorf", "Eric Canku",
        "Hannah Grant", "Justin Moran", "Alina Thomas", "Jerry Reick",
        "Matt Petrovich", "Micah Kessler", "Shaun Hawkins",
        "Jennifer Mortensen", "Aaron Ferrucci",
    }
    # Harrison is present but dogfood-only -- he must never ride a roster run.
    full = kc.load_roster()
    dogfood = [p for p in full if p["dogfood_only"]]
    assert [p["name"] for p in dogfood] == ["Harrison Rogers"]
    assert "Harrison Rogers" not in names


def test_excluded_people_are_absent():
    """Jeff (oversight, no invented KPIs), Sara (freelancer), and all of BDM are
    excluded BY DESIGN (spec section 6). A roster edit that adds them back should
    be a decision, not an accident."""
    names = {p["name"] for p in kc.load_roster()}
    for excluded in ("Jeff Montgomery", "Sara Fonseca", "Larry Stone",
                     "Daniel Sion", "Brei Pebley", "Demi Bagby", "Jake Lichtman"):
        assert excluded not in names


def test_the_four_fully_system_read_people_have_empty_pools():
    """Spec section 3.4: Tommy, Justin, Hannah and Jerry are already fully
    system-read, so they have NO Tier-1 items and will mostly skip. Pinned so it
    reads as the documented consequence rather than a silent failure."""
    by_name = {p["name"]: p for p in kc.pilot_roster()}
    for name in ("Tommy Anderson", "Justin Moran", "Hannah Grant", "Jerry Reick"):
        assert by_name[name]["items"] == [], f"{name} should have an empty Tier-1 pool"


def test_everyone_else_has_a_nonempty_grounded_pool():
    by_name = {p["name"]: p for p in kc.pilot_roster()}
    for name in ("Alex Cordova", "Elena Meirndorf", "Eric Canku", "Alina Thomas",
                 "Matt Petrovich", "Micah Kessler", "Shaun Hawkins",
                 "Jennifer Mortensen", "Aaron Ferrucci"):
        items = by_name[name]["items"]
        assert items, f"{name} should have Tier-1 items"
        for it in items:
            # Every item traces to a numbered KPI row in the locked source doc.
            assert it["kpi"], f"{name}/{it['key']} has no KPI provenance"
            assert it["question"].strip()


def test_micah_is_scoped_to_his_osn_half_not_bdm():
    micah = {p["name"]: p for p in kc.pilot_roster()}["Micah Kessler"]
    assert micah["entity"] == "OSN"


def test_lex_participants_present_day_one():
    """Harrison's explicit call: LEX goes live day 1, no wave gate."""
    lex = [p for p in kc.pilot_roster() if p["entity"].startswith("LEX")]
    assert {p["name"] for p in lex} == {
        "Shaun Hawkins", "Jennifer Mortensen", "Aaron Ferrucci"}


def test_roster_validates_clean_against_the_registry():
    assert kc.validate_roster() == []


def test_roster_load_is_fail_closed_on_a_broken_file(monkeypatch, tmp_path):
    """A broken roster must never degrade to 'DM everyone'."""
    kc.load_roster(force=True)  # prime the cache with the good file
    bad = tmp_path / "bad.yaml"
    bad.write_text("roster: [ this is not: valid: yaml", encoding="utf-8")
    monkeypatch.setattr(kc, "_roster_path", lambda: bad)
    out = kc.load_roster(force=True)
    # Last-good is kept; it is never a wider set than the real roster.
    assert len(out) <= 14
    missing = tmp_path / "nope.yaml"
    monkeypatch.setattr(kc, "_roster_path", lambda: missing)
    assert kc.load_roster(force=True) == []


# ---------------------------------------------------------------------------
# Layer B -- flag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, "off"), ("", "off"), ("off", "off"), ("dry", "dry"), ("on", "on"),
    ("ON", "on"), (" on ", "on"), ("1", "off"), ("true", "off"), ("yes", "off"),
])
def test_mode_is_whitelist_validated_and_fail_closed(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CORA_KNOWLEDGE_CHECK", raising=False)
    else:
        monkeypatch.setenv("CORA_KNOWLEDGE_CHECK", value)
    assert kc.mode() == expected


def test_enabled_and_live_gates(monkeypatch):
    monkeypatch.setenv("CORA_KNOWLEDGE_CHECK", "off")
    assert not kc.enabled() and not kc.live()
    monkeypatch.setenv("CORA_KNOWLEDGE_CHECK", "dry")
    assert kc.enabled() and not kc.live()
    monkeypatch.setenv("CORA_KNOWLEDGE_CHECK", "on")
    assert kc.enabled() and kc.live()


# ---------------------------------------------------------------------------
# Layer B -- event log fold
# ---------------------------------------------------------------------------

def test_fold_tracks_a_full_cycle():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    kc.append_event("captured", cycle_id="kchk-a", user="U1", date="2026-08-11",
                    answer="three notices open")
    st = kc.fold_state()
    cyc = st["cycles"]["kchk-a"]
    assert cyc["state"] == kc.STATE_CAPTURED
    assert cyc["answer"] == "three notices open"
    assert cyc["item_key"] == "k1"
    assert st["by_day"][("U1", "2026-08-11")] == "kchk-a"
    assert st["last_asked"][("U1", "k1")] == "2026-08-11"


def test_terminal_state_is_sticky_d096():
    """D-096's lesson: fold is last-write-wins, so a late non-terminal event
    would RESURRECT a finished cycle and let it be answered/promoted twice."""
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    kc.append_event("captured", cycle_id="kchk-a", user="U1", date="2026-08-11",
                    answer="first")
    kc.append_event("promoted", cycle_id="kchk-a", user="U1", date="2026-08-11")
    # A replayed / out-of-order capture arrives after the terminal promote.
    kc.append_event("captured", cycle_id="kchk-a", user="U1", date="2026-08-11",
                    answer="SECOND ANSWER")
    kc.append_event("asked", cycle_id="kchk-a", user="U1", date="2026-08-11")
    cyc = kc.fold_state()["cycles"]["kchk-a"]
    assert cyc["state"] == kc.STATE_PROMOTED
    assert cyc["answer"] == "first"


@pytest.mark.parametrize("terminal_event,expected", [
    ("promoted", kc.STATE_PROMOTED),
    ("held_collision", kc.STATE_HELD),
    ("skipped_by_user", kc.STATE_SKIPPED),
    ("expired", kc.STATE_EXPIRED),
    ("ask_failed", kc.STATE_FAILED),
])
def test_every_terminal_event_is_sticky(terminal_event, expected):
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    kc.append_event(terminal_event, cycle_id="kchk-a", user="U1", date="2026-08-11")
    kc.append_event("captured", cycle_id="kchk-a", user="U1", date="2026-08-11",
                    answer="late")
    assert kc.fold_state()["cycles"]["kchk-a"]["state"] == expected


def test_torn_line_does_not_kill_the_fold(tmp_path):
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    with kc._events_path().open("a", encoding="utf-8") as fh:
        fh.write('{"event": "asked", "cycle_id": "kchk-b"\n')  # truncated
    _reserve_and_ask("U2", "2026-08-11", "kchk-c")
    st = kc.fold_state()
    assert "kchk-a" in st["cycles"] and "kchk-c" in st["cycles"]


# ---------------------------------------------------------------------------
# Layer B -- send-ledger idempotency (the restart guarantee)
# ---------------------------------------------------------------------------

def test_handled_today_blocks_a_resend_in_every_state():
    for i, event in enumerate(
            ["reserved", "asked", "captured", "promoted", "skipped_by_user",
             "expired", "ask_failed", "held_collision"]):
        user = f"U{i}"
        kc.append_event("reserved", cycle_id=f"c{i}", user=user, date="2026-08-11",
                        item_key="k1")
        if event != "reserved":
            kc.append_event(event, cycle_id=f"c{i}", user=user, date="2026-08-11")
        st = kc.fold_state()
        assert kc.handled_today(st, user, "2026-08-11") is True, event


def test_reservation_alone_blocks_a_resend():
    """The crash window: reserved but the Slack call never returned. A re-run
    must NOT send again -- one lost question beats a duplicate DM."""
    kc.append_event("reserved", cycle_id="kchk-a", user="U1", date="2026-08-11",
                    item_key="k1")
    st = kc.fold_state()
    assert kc.handled_today(st, "U1", "2026-08-11") is True
    assert st["cycles"]["kchk-a"]["state"] == kc.STATE_RESERVED


def test_skipped_no_gap_counts_as_handled_and_creates_no_cycle():
    kc.append_event("skipped_no_gap", user="U1", date="2026-08-11")
    st = kc.fold_state()
    assert kc.handled_today(st, "U1", "2026-08-11") is True
    assert st["cycles"] == {}


def test_handled_today_is_per_day_and_per_person():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    st = kc.fold_state()
    assert kc.handled_today(st, "U1", "2026-08-12") is False
    assert kc.handled_today(st, "U2", "2026-08-11") is False


# ---------------------------------------------------------------------------
# Layer B -- live cycle lookup (drives DM capture)
# ---------------------------------------------------------------------------

def test_live_cycle_ignores_terminal_cycles():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    kc.append_event("promoted", cycle_id="kchk-a", user="U1", date="2026-08-11")
    assert kc.live_cycle_for(kc.fold_state(), "U1") is None


def test_live_cycle_prefers_the_most_recent_day():
    _reserve_and_ask("U1", "2026-08-10", "kchk-old")
    _reserve_and_ask("U1", "2026-08-11", "kchk-new")
    assert kc.live_cycle_for(kc.fold_state(), "U1")["cycle_id"] == "kchk-new"


def test_live_cycle_is_scoped_to_the_person():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    assert kc.live_cycle_for(kc.fold_state(), "U2") is None


def test_reserved_cycle_is_not_live_for_capture():
    """A reservation whose DM never went out must not swallow an unrelated DM."""
    kc.append_event("reserved", cycle_id="kchk-a", user="U1", date="2026-08-11",
                    item_key="k1")
    assert kc.live_cycle_for(kc.fold_state(), "U1") is None


# ---------------------------------------------------------------------------
# Layer B -- Tier-1 rotation
# ---------------------------------------------------------------------------

_PERSON = {"slack_id": "U1", "name": "Test", "entity": "OSN", "dogfood_only": False,
           "items": [{"key": "a", "question": "qa", "kpi": "KPI 1"},
                     {"key": "b", "question": "qb", "kpi": "KPI 2"}]}


def test_tier1_picks_never_asked_first_in_roster_order():
    st = kc.fold_state()
    assert kc.select_tier1(_PERSON, st, today="2026-08-11")["key"] == "a"


def test_tier1_rotates_to_the_next_unasked_item():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a", item_key="a")
    st = kc.fold_state()
    assert kc.select_tier1(_PERSON, st, today="2026-08-12")["key"] == "b"


def test_tier1_respects_the_cooldown_and_returns_none_when_pool_exhausted():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a", item_key="a")
    _reserve_and_ask("U1", "2026-08-12", "kchk-b", item_key="b")
    st = kc.fold_state()
    assert kc.select_tier1(_PERSON, st, today="2026-08-13") is None


def test_tier1_reoffers_an_item_after_the_cooldown_even_if_it_was_promoted():
    """Rotation, NOT consumption (spec 3.1): a status snapshot goes stale, so a
    confirmed+promoted item is a legitimate question again a week later."""
    _reserve_and_ask("U1", "2026-08-01", "kchk-a", item_key="a")
    kc.append_event("promoted", cycle_id="kchk-a", user="U1", date="2026-08-01")
    _reserve_and_ask("U1", "2026-08-02", "kchk-b", item_key="b")
    st = kc.fold_state()
    assert kc.select_tier1(_PERSON, st, today="2026-08-08")["key"] == "a"


def test_tier1_picks_the_oldest_asked_when_all_are_off_cooldown():
    _reserve_and_ask("U1", "2026-08-01", "kchk-a", item_key="a")
    _reserve_and_ask("U1", "2026-08-02", "kchk-b", item_key="b")
    st = kc.fold_state()
    assert kc.select_tier1(_PERSON, st, today="2026-08-20")["key"] == "a"


def test_tier1_empty_pool_yields_none():
    empty = dict(_PERSON, items=[])
    assert kc.select_tier1(empty, kc.fold_state(), today="2026-08-11") is None


def test_tier1_unparseable_last_asked_fails_closed(monkeypatch):
    kc.append_event("reserved", cycle_id="kchk-a", user="U1", date="not-a-date",
                    item_key="a")
    kc.append_event("reserved", cycle_id="kchk-b", user="U1", date="2026-08-11",
                    item_key="b")
    st = kc.fold_state()
    # 'a' has an unparseable asked-date -> not re-offered; 'b' is on cooldown.
    assert kc.select_tier1(_PERSON, st, today="2026-08-12") is None


# ---------------------------------------------------------------------------
# Layer B -- Tier-2 organic gap sourcing
# ---------------------------------------------------------------------------

@pytest.fixture
def _gap_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GAPS_LOG_PATH", str(tmp_path / "gaps.jsonl"))
    monkeypatch.setenv("RESOLVED_GAPS_PATH", str(tmp_path / "resolved.jsonl"))
    monkeypatch.setenv("GAP_AUTOFILL_STATE_PATH", str(tmp_path / "gapstate.json"))
    monkeypatch.setenv("GAP_ASK_PENDING_PATH", str(tmp_path / "asks.json"))
    yield tmp_path


def _gap(ts_hours_ago=100.0, entity="OSN", question="who owns the recon workbook?",
         detector="unknown_response", **extra):
    ts = (datetime.now(timezone.utc) - timedelta(hours=ts_hours_ago)).isoformat()
    row = {"ts": ts, "entity": entity, "question": question,
           "gap": "no answer found", "detector": detector, "channel": "osn-leadership"}
    row.update(extra)
    return row


@pytest.mark.parametrize("person_entity,gap_entity,expected", [
    ("OSN", "OSN", True),
    ("F3E", "F3E", True),
    ("LEX-LLC", "LEX-LLC", True),
    ("LEX-LLC", "LEX", True),       # sub-entity holder sees GM-level LEX
    ("LEX-LLC", "LEX-LLA", False),  # sibling sub-entity firewall
    ("LEX", "LEX-LLC", False),      # parent does NOT inherit a sub-entity's gap
    ("OSN", "F3E", False),
    ("OSN", "", False),
    ("", "OSN", False),
])
def test_entity_matching_respects_the_sub_entity_firewall(person_entity, gap_entity, expected):
    assert kc._entity_matches(person_entity, gap_entity) is expected


def test_tier2_eligible_accepts_a_real_aged_unknown_response(_gap_paths):
    ok, why = kc.tier2_eligible(_gap())
    assert ok is True, why


def test_tier2_excludes_kb_miss_deliberately(_gap_paths):
    """VERIFIED AGAINST LIVE CODE, deviating from the kickoff's literal wording:
    kb_miss is retrieval-side telemetry that fires even when Cora answered
    correctly from static context, so asking a human for an answer she already
    gave is noise (gap_autofill's own reviewed finding)."""
    ok, _ = kc.tier2_eligible(_gap(detector="kb_miss"))
    assert ok is False


def test_tier2_excludes_a_dm_origin_gap(_gap_paths):
    """A question asked in a private DM must not be re-broadcast to a third
    party -- this protection is not LEX-specific and is left standing."""
    assert kc.tier2_eligible(_gap(channel="dm"))[0] is False
    assert kc.tier2_eligible(_gap(private_source=True))[0] is False


def test_tier2_excludes_a_too_young_gap(_gap_paths):
    assert kc.tier2_eligible(_gap(ts_hours_ago=1.0))[0] is False


def test_tier2_excludes_a_capability_ask(_gap_paths):
    ok, _ = kc.tier2_eligible(_gap(question="are you able to send a weekly email digest?"))
    assert ok is False


def test_tier2_screen_fails_closed_on_error(monkeypatch, _gap_paths):
    import cora.gap_autofill as ga
    monkeypatch.setattr(ga, "mine_eligibility",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert kc.tier2_eligible(_gap())[0] is False


def test_select_tier2_scopes_to_the_persons_domain(_gap_paths):
    gaps = [_gap(entity="F3E"), _gap(entity="OSN", question="osn one?")]
    person = {"slack_id": "U1", "entity": "OSN", "items": []}
    picked = kc.select_tier2(person, open_gaps=gaps)
    assert picked and picked["entity"] == "OSN"


def test_select_tier2_picks_the_oldest_eligible(_gap_paths):
    old = _gap(ts_hours_ago=500.0, question="older?")
    new = _gap(ts_hours_ago=100.0, question="newer?")
    person = {"slack_id": "U1", "entity": "OSN", "items": []}
    assert kc.select_tier2(person, open_gaps=[new, old])["question"] == "older?"


def test_select_tier2_skips_in_run_claimed_gaps(_gap_paths):
    """Matt and Micah both scope to OSN -- one run must not ask them the same
    question and collect two potentially conflicting answers."""
    g = _gap()
    person = {"slack_id": "U1", "entity": "OSN", "items": []}
    assert kc.select_tier2(person, open_gaps=[g], claimed={g["ts"]}) is None


def test_select_tier2_returns_none_when_nothing_is_eligible(_gap_paths):
    person = {"slack_id": "U1", "entity": "OSN", "items": []}
    assert kc.select_tier2(person, open_gaps=[_gap(detector="kb_miss")]) is None


def test_claim_gap_writes_into_gap_autofills_own_ledger(_gap_paths):
    """The claim must remove the gap from the Harrison digest flow too -- a
    parallel claim file would leave the two flows blind to each other."""
    import cora.gap_autofill as ga
    g = _gap()
    assert kc.claim_gap(g["ts"], "kchk-a") is True
    assert ga.load_state()[g["ts"]]["via"] == "knowledge_check"
    # ga's own open-gap loader now excludes it.
    Path(_gap_paths / "gaps.jsonl").write_text(json.dumps(g) + "\n", encoding="utf-8")
    assert [x["ts"] for x in ga.load_open_gaps()] == []


def test_claim_gap_refuses_a_double_claim(_gap_paths):
    g = _gap()
    assert kc.claim_gap(g["ts"], "kchk-a") is True
    assert kc.claim_gap(g["ts"], "kchk-b") is False


def test_claim_gap_is_safe_on_empty_ts(_gap_paths):
    assert kc.claim_gap("", "kchk-a") is False


def test_select_question_tier2_preempts_tier1(_gap_paths):
    person = dict(_PERSON, entity="OSN")
    picked = kc.select_question(person, kc.fold_state(), open_gaps=[_gap()],
                                today="2026-08-11")
    assert picked["tier"] == 2
    assert picked["item_key"].startswith("gap:")
    assert picked["gap_ts"]


def test_select_question_falls_back_to_tier1(_gap_paths):
    person = dict(_PERSON, entity="OSN")
    picked = kc.select_question(person, kc.fold_state(), open_gaps=[],
                                today="2026-08-11")
    assert picked["tier"] == 1 and picked["item_key"] == "a"


def test_select_question_returns_none_rather_than_inventing(_gap_paths):
    """The hard rule: no Tier 3, no generative fallback. A person with an
    exhausted pool and no live gap is SKIPPED."""
    person = dict(_PERSON, items=[], entity="OSN")
    assert kc.select_question(person, kc.fold_state(), open_gaps=[],
                              today="2026-08-11") is None


def test_no_generative_call_exists_anywhere_in_selection():
    """Structural pin: an LLM-invented question is the failure class this design
    rejects, so the module must have no model client at all."""
    src = (_REPO_ROOT / "src" / "cora" / "knowledge_check.py").read_text(encoding="utf-8")
    for forbidden in ("anthropic", "claude_client", "messages.create",
                      "batch_client", "openai"):
        assert forbidden not in src, f"knowledge_check must not reach a model ({forbidden})"


# ---------------------------------------------------------------------------
# Layer B -- answer hygiene / injection belt
# ---------------------------------------------------------------------------

def test_scrub_neutralizes_slack_broadcast_in_an_answer():
    """A confirmed answer lands in an ALWAYS-INJECTED file and is re-served
    through retrieval -- an embedded broadcast would fire on every render."""
    out = kc.scrub_answer("all good <!channel> ping everyone")
    assert "<!channel>" not in out
    assert "channel" in out


def test_scrub_strips_labelled_and_bare_links():
    out = kc.scrub_answer("see <https://evil.example/x|click here> for details")
    assert "evil.example" not in out
    assert "https://" not in out


def test_scrub_neutralizes_user_mentions():
    assert "<@U0B2RM2JYJ1>" not in kc.scrub_answer("ask <@U0B2RM2JYJ1> about it")


def test_scrub_collapses_whitespace_and_caps_length():
    assert kc.scrub_answer("a\n\n  b") == "a b"
    long = kc.scrub_answer("x" * (kc.MAX_ANSWER_CHARS + 500))
    assert len(long) <= kc.MAX_ANSWER_CHARS + 20
    assert long.endswith("(truncated)")


def test_scrub_preserves_an_ordinary_answer():
    assert kc.scrub_answer("  3 PCI notices still open, tracker is current ") == \
        "3 PCI notices still open, tracker is current"


@pytest.mark.parametrize("text", ["no idea", "Not my area", "don't know",
                                  "n/a", "nothing", "skip", "  DUNNO  "])
def test_non_answers_are_recognized(text):
    assert kc.is_non_answer(text) is True


@pytest.mark.parametrize("text", [
    "no idea yet but Jen is checking",     # qualified -> a real answer
    "3 open",
    "nothing is outstanding this week",    # 'nothing' as a real status answer
])
def test_real_answers_are_not_treated_as_declines(text):
    assert kc.is_non_answer(text) is False


# ---------------------------------------------------------------------------
# Layer B -- Slack surfaces
# ---------------------------------------------------------------------------

def test_ask_text_is_one_question_and_promises_the_confirm_step():
    t = kc.ask_text("How many PCI notices are open?", "Matt Petrovich")
    assert t.startswith("Matt -- ")
    assert "How many PCI notices are open?" in t
    assert "before anything is saved" in t


def test_ask_blocks_carry_only_the_cycle_id_as_the_button_value():
    blocks = kc.build_ask_blocks("q?", "kchk-abc", "Matt Petrovich")
    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    els = actions[0]["elements"]
    assert [e["action_id"] for e in els] == [kc.ACTION_SKIP_TODAY]
    # Invariant: the value is the opaque handle ONLY -- never a payload/echo.
    assert all(e["value"] == "kchk-abc" for e in els)


def test_there_is_no_answer_via_button():
    """The answer is free prose; buttons only ever ADD a route to something the
    typed path already does. An answer-via-button must never exist."""
    labels = [e["text"]["text"].lower()
              for b in kc.build_ask_blocks("q?", "kchk-a") if b["type"] == "actions"
              for e in b["elements"]]
    assert labels == ["skip today"]


def test_confirm_blocks_offer_save_reword_skip_on_the_same_cycle_id():
    blocks = kc.build_confirm_blocks("3 open", "kchk-abc", "how many?")
    els = [e for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert [e["action_id"] for e in els] == [
        kc.ACTION_CONFIRM_ANSWER, kc.ACTION_EDIT_ANSWER, kc.ACTION_SKIP_ANSWER]
    assert all(e["value"] == "kchk-abc" for e in els)


def test_confirm_text_restates_the_persons_own_words():
    t = kc.confirm_text("3 notices still open", "how many PCI notices?")
    assert "3 notices still open" in t and "how many PCI notices?" in t


def test_long_bodies_chunk_rather_than_silently_truncate():
    """Once a message carries blocks, `text=` is demoted to a notification
    fallback -- a long body must chunk, not vanish (the S6 lesson)."""
    body = "\n".join(f"line {i} " + "x" * 200 for i in range(60))
    blocks = kc.build_confirm_blocks(body, "kchk-a")
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) > 1
    assert all(len(b["text"]["text"]) <= 3000 for b in sections)


def test_open_dm_returns_empty_on_failure():
    class _Boom:
        def conversations_open(self, **_kw):
            raise RuntimeError("nope")
    assert kc.open_dm(_Boom(), "U1") == ""

    class _Ok:
        def conversations_open(self, **_kw):
            return {"channel": {"id": "D123"}}
    assert kc.open_dm(_Ok(), "U1") == "D123"


# ---------------------------------------------------------------------------
# Layer B -- end-of-day expiry
# ---------------------------------------------------------------------------

def test_expiry_closes_yesterdays_cycles_with_distinct_reasons():
    _reserve_and_ask("U1", "2026-08-10", "kchk-asked")
    _reserve_and_ask("U2", "2026-08-10", "kchk-cap")
    kc.append_event("captured", cycle_id="kchk-cap", user="U2", date="2026-08-10",
                    answer="a")
    kc.append_event("reserved", cycle_id="kchk-res", user="U3", date="2026-08-10",
                    item_key="k1")
    rows = kc.expire_stale_cycles(kc.fold_state(), today="2026-08-11")
    reasons = {r["cycle_id"]: r["reason"] for r in rows}
    assert reasons == {"kchk-asked": "no_response", "kchk-cap": "no_confirm",
                       "kchk-res": "reserved_never_sent"}
    st = kc.fold_state()
    assert all(st["cycles"][c]["state"] == kc.STATE_EXPIRED for c in reasons)


def test_expiry_leaves_todays_cycles_alone():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    assert kc.expire_stale_cycles(kc.fold_state(), today="2026-08-11") == []


def test_expiry_does_not_stack_questions_into_the_next_day():
    """An unanswered question expires rather than carrying over, so tomorrow's
    run sees the person as un-handled and asks exactly one new thing."""
    _reserve_and_ask("U1", "2026-08-10", "kchk-a")
    kc.expire_stale_cycles(kc.fold_state(), today="2026-08-11")
    st = kc.fold_state()
    assert kc.live_cycle_for(st, "U1") is None
    assert kc.handled_today(st, "U1", "2026-08-11") is False


# ---------------------------------------------------------------------------
# Layer B -- CAPTURE
# ---------------------------------------------------------------------------

def test_match_live_cycle_threaded_reply_matches_its_own_ask():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    assert kc.match_live_cycle("U1", "111.222")["cycle_id"] == "kchk-a"


def test_match_live_cycle_threaded_reply_to_another_message_does_not_match():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    assert kc.match_live_cycle("U1", "999.999") is None


def test_match_live_cycle_toplevel_respects_the_caller_gate():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    assert kc.match_live_cycle("U1", None, allow_toplevel=True)["cycle_id"] == "kchk-a"
    assert kc.match_live_cycle("U1", None, allow_toplevel=False) is None


def test_record_answer_stages_but_writes_nothing():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    outcome, answer = kc.record_answer("kchk-a", "U1", "  3 notices  open ")
    assert outcome == "captured" and answer == "3 notices open"
    cyc = kc.fold_state()["cycles"]["kchk-a"]
    assert cyc["state"] == kc.STATE_CAPTURED
    assert cyc["state"] != kc.STATE_PROMOTED  # nothing written


def test_record_answer_rejects_a_non_owner():
    """A confirm writes a fact ATTRIBUTED to that person -- somebody else must
    never be able to put words in their mouth."""
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    assert kc.record_answer("kchk-a", "U2", "made up")[0] == "not_authorized"
    assert kc.fold_state()["cycles"]["kchk-a"]["state"] == kc.STATE_ASKED


def test_record_answer_treats_a_decline_as_a_skip():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    outcome, _ = kc.record_answer("kchk-a", "U1", "no idea")
    assert outcome == "declined"
    assert kc.fold_state()["cycles"]["kchk-a"]["state"] == kc.STATE_SKIPPED


def test_record_answer_on_a_terminal_cycle_is_refused():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    kc.append_event("promoted", cycle_id="kchk-a", user="U1", date="2026-08-11")
    assert kc.record_answer("kchk-a", "U1", "late answer")[0] == "not_live"


def test_record_answer_rewords_a_staged_answer():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    kc.record_answer("kchk-a", "U1", "first try")
    outcome, answer = kc.record_answer("kchk-a", "U1", "second try")
    assert outcome == "recaptured" and answer == "second try"
    assert kc.fold_state()["cycles"]["kchk-a"]["answer"] == "second try"


def test_record_answer_scrubs_before_staging():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    _, answer = kc.record_answer("kchk-a", "U1", "done <!channel> <https://x.io|here>")
    assert "<!channel>" not in answer and "x.io" not in answer
    assert "<!channel>" not in kc.fold_state()["cycles"]["kchk-a"]["answer"]


def test_record_answer_ignores_an_empty_reply():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    assert kc.record_answer("kchk-a", "U1", "   ")[0] == "empty"
    assert kc.fold_state()["cycles"]["kchk-a"]["state"] == kc.STATE_ASKED


# ---------------------------------------------------------------------------
# Layer B -- taps
# ---------------------------------------------------------------------------

def test_skip_today_records_participation_and_writes_nothing():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    outcome, msg = kc.process_skip_today_tap("kchk-a", "U1")
    assert outcome == "skipped" and msg
    assert kc.fold_state()["cycles"]["kchk-a"]["state"] == kc.STATE_SKIPPED


def test_taps_are_addressee_only():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    for fn in (kc.process_skip_today_tap, kc.process_skip_answer_tap,
               kc.process_edit_tap):
        assert fn("kchk-a", "U2")[0] == "not_authorized"
    assert kc.fold_state()["cycles"]["kchk-a"]["state"] == kc.STATE_ASKED


def test_a_forged_or_unknown_cycle_id_is_orphaned():
    for fn in (kc.process_skip_today_tap, kc.process_skip_answer_tap,
               kc.process_edit_tap):
        assert fn("kchk-does-not-exist", "U1")[0] == "orphaned"
        assert fn("", "U1")[0] == "orphaned"


def test_a_second_tap_reads_already_handled_not_orphaned():
    """Idempotent ack -- 'orphaned' would wrongly imply nothing happened."""
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    assert kc.process_skip_today_tap("kchk-a", "U1")[0] == "skipped"
    assert kc.process_skip_today_tap("kchk-a", "U1")[0] == "already_handled"


def test_skip_at_confirm_discards_the_staged_answer_unwritten():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    kc.record_answer("kchk-a", "U1", "3 open")
    assert kc.process_skip_answer_tap("kchk-a", "U1")[0] == "skipped"
    assert kc.fold_state()["cycles"]["kchk-a"]["state"] == kc.STATE_SKIPPED


def test_skip_at_confirm_refuses_a_cycle_with_no_staged_answer():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    assert kc.process_skip_answer_tap("kchk-a", "U1")[0] == "not_live"


def test_edit_keeps_the_cycle_live_for_a_reworded_answer():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    kc.record_answer("kchk-a", "U1", "first")
    assert kc.process_edit_tap("kchk-a", "U1")[0] == "editing"
    assert kc.fold_state()["cycles"]["kchk-a"]["state"] == kc.STATE_CAPTURED
    assert kc.record_answer("kchk-a", "U1", "reworded")[0] == "recaptured"


# ---------------------------------------------------------------------------
# Layer B -- participation reporting
# ---------------------------------------------------------------------------

def test_participation_separates_pool_exhausted_from_no_response():
    """Collapsing the two would make the four fully-system-read people look like
    they were ignoring their DMs (spec 3.4 / 8)."""
    kc.append_event("skipped_no_gap", user="U1", date="2026-08-11")
    _reserve_and_ask("U2", "2026-08-11", "kchk-b")
    kc.append_event("expired", cycle_id="kchk-b", user="U2", date="2026-08-11",
                    reason="no_response")
    t = kc.participation_stats(days=30, today="2026-08-12")["totals"]
    assert t["pool_exhausted"] == 1
    assert t["no_response"] == 1
    assert t["asked"] == 1  # the skipped-no-gap day was never an ask


def test_participation_counts_a_full_confirm():
    _reserve_and_ask("U1", "2026-08-11", "kchk-a")
    kc.append_event("captured", cycle_id="kchk-a", user="U1", date="2026-08-11",
                    answer="3 open")
    kc.append_event("promoted", cycle_id="kchk-a", user="U1", date="2026-08-11")
    t = kc.participation_stats(days=30, today="2026-08-12")["totals"]
    assert (t["asked"], t["answered"], t["confirmed"]) == (1, 1, 1)


def test_participation_surfaces_delivery_anomalies_separately():
    """A reservation whose DM never went out is a silent loss unless surfaced."""
    kc.append_event("reserved", cycle_id="kchk-a", user="U1", date="2026-08-11",
                    item_key="k1")
    kc.append_event("reserved", cycle_id="kchk-b", user="U2", date="2026-08-11",
                    item_key="k1")
    kc.append_event("ask_failed", cycle_id="kchk-b", user="U2", date="2026-08-11")
    t = kc.participation_stats(days=30, today="2026-08-11")["totals"]
    assert t["reserved_never_sent"] == 1 and t["failed"] == 1
    assert t["asked"] == 0  # neither counts as a question the person ever saw
    assert any("ANOMALIES" in ln for ln in kc.participation_report(today="2026-08-11"))


def test_participation_window_excludes_old_cycles():
    _reserve_and_ask("U1", "2026-06-01", "kchk-old")
    assert kc.participation_stats(days=30, today="2026-08-11")["totals"]["asked"] == 0


# ---------------------------------------------------------------------------
# Layer B -- the LEX/PHI posture switch
# ---------------------------------------------------------------------------

def test_phi_gate_is_off_by_decision_and_is_a_noop_for_every_entity():
    """Harrison 2026-08-11: LEX is treated identically to every other entity in
    this build -- no PHI scrub or gating on questions or answers."""
    assert kc.PHI_GATE_ANSWERS is False
    for text in ("client Bob Smith has an autism diagnosis and takes risperidone",
                 "Bob Smith's billing authorization is pending",
                 "utilization is 87 percent"):
        assert kc.phi_blocked(text) == (False, "")


def test_phi_gate_when_armed_refuses_and_is_entity_agnostic(monkeypatch):
    """One flip restores the wall -- pinned so the reversal path stays real."""
    monkeypatch.setattr(kc, "PHI_GATE_ANSWERS", True)
    blocked, reason = kc.phi_blocked(
        "client Bob Smith has an autism diagnosis and takes risperidone")
    assert blocked is True and reason
    assert kc.phi_blocked("utilization is 87 percent") == (False, "")


def test_phi_gate_when_armed_fails_closed_on_screen_error(monkeypatch):
    monkeypatch.setattr(kc, "PHI_GATE_ANSWERS", True)
    import cora.phi_guard as pg
    monkeypatch.setattr(pg, "is_phi_risk", lambda _t: (_ for _ in ()).throw(RuntimeError("boom")))
    blocked, reason = kc.phi_blocked("anything")
    assert blocked is True and "errored" in reason


def test_phi_policy_lives_in_exactly_one_place():
    """The reversal must be ONE flip. If a second copy of this policy appears,
    flipping the constant would silently leave a live bypass behind."""
    import ast
    # Parse rather than grep: docstrings and comments legitimately NAME the flag,
    # and only real bindings/reads can create a second live policy site.
    tree = ast.parse(
        (_REPO_ROOT / "src" / "cora" / "knowledge_check.py").read_text(encoding="utf-8"))
    stores = loads = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "PHI_GATE_ANSWERS":
            if isinstance(node.ctx, ast.Store):
                stores += 1
            else:
                loads += 1
    assert stores == 1, f"{stores} assignments to PHI_GATE_ANSWERS"
    assert loads == 1, f"{loads} reads of PHI_GATE_ANSWERS -- the reversal must be one flip"


# ---------------------------------------------------------------------------
# Layer B -- date helpers
# ---------------------------------------------------------------------------

def test_az_date_uses_arizona_not_utc():
    """A UTC date rolls over at 5pm local and would split one working day in two."""
    late = datetime(2026, 8, 12, 4, 30, tzinfo=timezone.utc)  # 21:30 AZ on 8/11
    assert kc.az_date(late) == "2026-08-11"


def test_is_weekday():
    assert kc.is_weekday(datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc)) is True   # Tue
    assert kc.is_weekday(datetime(2026, 8, 15, 16, 0, tzinfo=timezone.utc)) is False  # Sat
    assert kc.is_weekday(datetime(2026, 8, 16, 16, 0, tzinfo=timezone.utc)) is False  # Sun
