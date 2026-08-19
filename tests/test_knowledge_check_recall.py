"""Slice 4 -- Cora remembers its OWN question, and --force means what it says.

Two defects with one shape: a promise the code did not keep.

  (a) cq-6fbaf37b1ee7 / the 8/14 incident. `live_cycle_for` enforces "asked
      TODAY" at read time, which is right for deciding whether a passing DM may
      be auto-captured, but it was ALSO Cora's only memory. Live event data shows
      the consequence exactly: 8/14 was a Friday, its five unanswered asks were
      not swept until Monday 8/17 08:05 AZ, and any reply in between matched
      nothing -- so the answer was dropped into plain Q&A and Cora told the
      person it had never asked. 51 asks, 9 captured.

  (b) cq-ab0a8e753f19. `--force`'s help text has always said it ignores the
      already-handled ledger AND the item cooldown. Only the ledger was wired, so
      the one thing --force exists for -- re-smoking the flow the same day --
      reported "no Tier-1 item off cooldown" and sent nothing.

The write gate is deliberately untouched: a late answer still only STAGES behind
the confirm card, and the card now names the day it is binding the answer to.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import knowledge_check as kc  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_events(tmp_path, monkeypatch):
    monkeypatch.setattr(kc, "_events_path", lambda: tmp_path / "events.jsonl")
    kc.reset_caches_for_tests()
    yield


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv("CORA_KNOWLEDGE_CHECK", raising=False)
    yield


def _pin(monkeypatch, day):
    monkeypatch.setattr(kc, "az_date", lambda dt=None: day)


def _reserve_and_ask(user, date, cycle_id, item_key="k1", entity="F3E",
                     question="q?", tier=1, message_ts="111.222"):
    kc.append_event("reserved", cycle_id=cycle_id, user=user, date=date,
                    item_key=item_key, entity=entity, question=question, tier=tier)
    kc.append_event("asked", cycle_id=cycle_id, user=user, date=date,
                    message_ts=message_ts, channel="D1")


# ── (a) recall across the day boundary ───────────────────────────────────────

def test_recent_cycle_remembers_fridays_ask_on_monday(monkeypatch):
    _reserve_and_ask("U1", "2026-08-14", "kchk-fri")
    _pin(monkeypatch, "2026-08-17")
    st = kc.fold_state()
    assert kc.live_cycle_for(st, "U1") is None          # capture gate unchanged
    assert kc.recent_cycle_for(st, "U1")["cycle_id"] == "kchk-fri"


def test_recent_cycle_expires_past_the_lookback(monkeypatch):
    _reserve_and_ask("U1", "2026-08-11", "kchk-old")
    _pin(monkeypatch, "2026-08-20")
    assert kc.recent_cycle_for(kc.fold_state(), "U1") is None


def test_recent_cycle_ignores_a_resolved_cycle(monkeypatch):
    _reserve_and_ask("U1", "2026-08-11", "kchk-done")
    kc.append_event("captured", cycle_id="kchk-done", user="U1",
                    date="2026-08-11", answer="all clear")
    kc.append_event("promoted", cycle_id="kchk-done", user="U1", date="2026-08-11")
    _pin(monkeypatch, "2026-08-12")
    assert kc.recent_cycle_for(kc.fold_state(), "U1") is None


def test_recent_cycle_does_not_reopen_a_swept_cycle(monkeypatch):
    """An EXPIRED cycle stays expired -- reopening one is a state-machine change,
    and "that question expired on Monday" is already an answer that REMEMBERS."""
    _reserve_and_ask("U1", "2026-08-11", "kchk-x")
    kc.append_event("expired", cycle_id="kchk-x", user="U1", date="2026-08-11",
                    reason="no_response")
    _pin(monkeypatch, "2026-08-12")
    assert kc.recent_cycle_for(kc.fold_state(), "U1") is None


def test_recent_cycle_is_scoped_to_the_person(monkeypatch):
    _reserve_and_ask("U1", "2026-08-14", "kchk-a")
    _pin(monkeypatch, "2026-08-17")
    assert kc.recent_cycle_for(kc.fold_state(), "U2") is None


def test_recent_cycle_prefers_the_newest_ask(monkeypatch):
    _reserve_and_ask("U1", "2026-08-14", "kchk-older", message_ts="1.1")
    _reserve_and_ask("U1", "2026-08-17", "kchk-newer", message_ts="2.2")
    _pin(monkeypatch, "2026-08-17")
    assert kc.recent_cycle_for(kc.fold_state(), "U1")["cycle_id"] == "kchk-newer"


def test_recent_cycle_ignores_a_future_dated_cycle(monkeypatch):
    """A clock skew or a hand-edited event must not make tomorrow's ask 'recent'."""
    _reserve_and_ask("U1", "2026-08-20", "kchk-future")
    _pin(monkeypatch, "2026-08-17")
    assert kc.recent_cycle_for(kc.fold_state(), "U1") is None


def test_late_threaded_reply_now_matches_its_own_ask(monkeypatch):
    _reserve_and_ask("U1", "2026-08-14", "kchk-fri")
    _pin(monkeypatch, "2026-08-17")
    assert kc.match_live_cycle("U1", "111.222")["cycle_id"] == "kchk-fri"
    assert kc.match_live_cycle("U1", "999.999") is None


def test_late_answer_still_only_stages_behind_the_confirm_card(monkeypatch):
    """The widened READ window must not widen the WRITE gate."""
    _reserve_and_ask("U1", "2026-08-14", "kchk-fri")
    _pin(monkeypatch, "2026-08-17")
    outcome, answer = kc.record_answer("kchk-fri", "U1", "two still open")
    assert outcome == "captured" and answer == "two still open"
    cyc = kc.fold_state()["cycles"]["kchk-fri"]
    assert cyc["state"] == kc.STATE_CAPTURED
    assert cyc["state"] != kc.STATE_PROMOTED


def test_has_live_cycle_agrees_with_match_live_cycle(monkeypatch):
    """If the router's gate kept the narrow window while the matcher widened, the
    router would gate out the very match it was about to make."""
    _reserve_and_ask("U1", "2026-08-14", "kchk-fri")
    _pin(monkeypatch, "2026-08-17")
    assert kc.has_live_cycle("U1") is True
    assert kc.match_live_cycle("U1", None, allow_toplevel=True) is not None


def test_is_late_ask_only_flags_an_earlier_day(monkeypatch):
    _reserve_and_ask("U1", "2026-08-14", "kchk-fri")
    cyc = kc.fold_state()["cycles"]["kchk-fri"]
    _pin(monkeypatch, "2026-08-14")
    assert kc.is_late_ask(cyc) is False
    _pin(monkeypatch, "2026-08-17")
    assert kc.is_late_ask(cyc) is True
    assert kc.is_late_ask(None) is False


def test_confirm_card_reanchors_a_late_answer():
    question = "How many PCI notices are still open?"
    same_day = kc.confirm_text("two open", question)
    late = kc.confirm_text("two open", question, "2026-08-14")
    assert "2026-08-14" not in same_day
    assert "2026-08-14" in late and question in late
    blocks = kc.build_confirm_blocks("two open", "kchk-1", question, "2026-08-14")
    assert "2026-08-14" in json.dumps(blocks)


def test_recall_note_says_cora_did_ask(monkeypatch):
    _reserve_and_ask("U1", "2026-08-14", "kchk-fri",
                     question="How many PCI notices are still open?")
    _pin(monkeypatch, "2026-08-17")
    note = kc.recall_ask_note("U1")
    assert "How many PCI notices are still open?" in note
    assert "2026-08-14" in note
    assert "never deny" in note


def test_recall_note_marks_an_answer_awaiting_confirmation(monkeypatch):
    _reserve_and_ask("U1", "2026-08-14", "kchk-fri", question="How many notices?")
    kc.append_event("captured", cycle_id="kchk-fri", user="U1",
                    date="2026-08-14", answer="two")
    _pin(monkeypatch, "2026-08-17")
    assert "awaiting their confirmation" in kc.recall_ask_note("U1")


def test_recall_note_is_empty_with_nothing_outstanding(monkeypatch):
    _pin(monkeypatch, "2026-08-17")
    assert kc.recall_ask_note("U1") == ""


def test_recall_note_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("state unreadable")
    monkeypatch.setattr(kc, "fold_state", boom)
    assert kc.recall_ask_note("U1") == ""


def test_recall_note_is_dm_only_in_the_dispatcher():
    """The question carries its own entity scope: a LEX question has no business
    in an F3E channel's context just because the same person mentioned Cora
    there. Source-pinned -- the branch is inside _dispatch_qa."""
    src = (_REPO_ROOT / "src" / "cora" / "app.py").read_text(encoding="utf-8")
    idx = src.index("knowledge_check.recall_ask_note")
    window = src[max(0, idx - 700):idx]
    assert 'channel_name == "dm"' in window


# ── (b) --force and the item cooldown ────────────────────────────────────────

def _person_with_one_item():
    return {"slack_id": "U1", "name": "Test Person", "entity": "F3E",
            "items": [{"key": "k1", "question": "q?", "kpi": "kpi"}]}


def _answered_today(user="U1", cycle_id="kchk-a", date="2026-08-11"):
    _reserve_and_ask(user, date, cycle_id, item_key="k1")
    kc.append_event("captured", cycle_id=cycle_id, user=user, date=date, answer="a")
    kc.append_event("promoted", cycle_id=cycle_id, user=user, date=date)


def test_cooldown_blocks_a_reask_by_default():
    _answered_today()
    st = kc.fold_state()
    person = _person_with_one_item()
    assert kc.select_tier1(person, st, today="2026-08-11") is None
    assert kc.select_question(person, st, today="2026-08-11") is None


def test_force_bypasses_the_item_cooldown():
    _answered_today()
    st = kc.fold_state()
    person = _person_with_one_item()
    item = kc.select_tier1(person, st, today="2026-08-11", ignore_cooldown=True)
    assert item is not None and item["key"] == "k1"
    picked = kc.select_question(person, st, today="2026-08-11", ignore_cooldown=True)
    assert picked["tier"] == 1 and picked["item_key"] == "k1"


def test_force_still_refuses_an_unparseable_ask_date():
    """Fail-closed stays fail-closed: an item whose age cannot be established is
    not re-asked even under --force."""
    kc.append_event("reserved", cycle_id="kchk-b", user="U1", date="not-a-date",
                    item_key="k1", entity="F3E", question="q?", tier=1)
    kc.append_event("asked", cycle_id="kchk-b", user="U1", date="not-a-date",
                    message_ts="1.1", channel="D1")
    person = _person_with_one_item()
    assert kc.select_tier1(person, kc.fold_state(), today="2026-08-11",
                           ignore_cooldown=True) is None


def test_force_prefers_a_never_asked_item_over_a_forced_reask():
    """Order-preserving: --force widens eligibility, it does not reshuffle
    priority. A fresh item still outranks one being re-asked off cooldown."""
    _answered_today()
    person = _person_with_one_item()
    person["items"].append({"key": "k2", "question": "q2?", "kpi": "kpi2"})
    item = kc.select_tier1(person, kc.fold_state(), today="2026-08-11",
                           ignore_cooldown=True)
    assert item["key"] == "k2"


def test_runner_passes_force_into_both_selection_sites():
    src = (_REPO_ROOT / "scripts" / "run_knowledge_check.py").read_text(encoding="utf-8")
    assert src.count("ignore_cooldown=args.force") == 2, (
        "both select_question call sites must honour --force, including the "
        "Tier-2-claimed-elsewhere re-selection")
    assert "AND the item cooldown" in src
