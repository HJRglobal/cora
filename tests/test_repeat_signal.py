"""Repeat-signal escalation (Code #13 slice 9b; D-302 + D-310).

The property under test is the ladder itself: the same signal on consecutive
fires changes SURFACE (tier 1 normal -> tier 2 owner + briefing line -> tier 3
ONE propose-only card + suppression), never repeats, and resets on a human ack
or on the fact clearing. Every escalation is a ledger row; every failure mode
that would leave a signal suppressed with nothing to acknowledge is pinned.

Fixture text is synthetic (D-145) and titles carry random words.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from cora import knowledge_review as kr  # noqa: E402
from cora import repeat_signal as rs  # noqa: E402

KEY = rs.make_key("quartz-monitor", "vendor Kestrel statement", "F3E")
OWNER = "U0B3KH5UZJ7"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Own ledger + own proposed-updates file; the id caches reset like the
    decision-inbox tests do."""
    monkeypatch.setenv("REPEAT_SIGNAL_LEDGER_PATH", str(tmp_path / "repeat-signals.jsonl"))
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "proposed.jsonl")
    monkeypatch.setattr(kr, "_ARCHIVE_PATH", tmp_path / "proposed.archive.jsonl")
    kr._SEEN_IDS_CACHE = None
    kr._ARCHIVE_IDS_CACHE = None
    yield
    kr._SEEN_IDS_CACHE = None
    kr._ARCHIVE_IDS_CACHE = None


def _fire(fid, key=KEY, subject="vendor Kestrel statement not filed", entity="F3E", **kw):
    return rs.fire(key, fire_id=fid, subject=subject, entity=entity,
                   owner_slack_id=OWNER, owner_name="Tessa Miller",
                   normal_surface="dm:owner", **kw)


def _rows():
    return rs.read_rows()


def _pending_cards():
    return [u for u in kr.load_proposed_updates()
            if u.get("update_type") == kr.UPDATE_TYPE_DECISION and u.get("state") == "PENDING"]


# ── the ladder ───────────────────────────────────────────────────────────────

def test_three_consecutive_fires_climb_the_tiers_and_mint_exactly_one_card():
    """The kickoff fixture: tiers 1, 2, 3; exactly ONE proposal minted; the
    tier-3 outcome carries the card id and suppression."""
    o1, o2, o3 = _fire("2026-06"), _fire("2026-07"), _fire("2026-08")
    assert (o1.tier, o2.tier, o3.tier) == (1, 2, 3)
    assert (o1.consecutive, o2.consecutive, o3.consecutive) == (1, 2, 3)
    assert not o1.suppressed and not o2.suppressed and o3.suppressed
    cards = _pending_cards()
    assert len(cards) == 1
    assert cards[0]["update_id"] == o3.card_update_id
    assert cards[0]["payload"]["signal_key"] == KEY
    assert cards[0]["confidence"] == "MED"
    # the three options are TEXT on the existing Accept/Dismiss card
    for opt in ("keep with owner Tessa Miller", "retire the signal", "re-scope it"):
        assert opt in cards[0]["description"]
    assert cards[0]["description"].startswith("[F3E]")   # what decision_inbox.entity_of reads


def test_a_fourth_fire_is_suppressed_and_ledgered_not_re_carded():
    """Suppression at the original surface until ack -- and the suppressed fire
    is still a ledger event, so the audit trail shows it kept happening."""
    for fid in ("2026-06", "2026-07", "2026-08"):
        _fire(fid)
    o4 = _fire("2026-09")
    assert o4.suppressed is True and o4.tier == 3
    assert o4.card_update_id == rs.state(KEY)["card_update_id"]
    assert len(_pending_cards()) == 1, "a second card for the same signal before ack"
    assert [r["event"] for r in _rows()][-1] == rs.EVENT_SUPPRESSED
    assert _rows()[-1]["fire_id"] == "2026-09"


def test_ack_resets_and_the_next_fire_is_tier_one_again():
    for fid in ("2026-06", "2026-07", "2026-08"):
        _fire(fid)
    assert rs.ack(KEY, via="card-dismiss") is True
    st = rs.state(KEY)
    assert st["consecutive"] == 0 and st["suppressed"] is False
    o = _fire("2026-09")
    assert o.tier == 1 and o.consecutive == 1 and not o.suppressed
    assert [r["event"] for r in _rows()].count(rs.EVENT_ACK) == 1


def test_clear_resets_like_an_ack_and_is_its_own_event():
    _fire("2026-06"), _fire("2026-07")
    assert rs.clear(KEY, why="invoice filed") is True
    assert rs.state(KEY)["consecutive"] == 0
    assert _rows()[-1]["event"] == rs.EVENT_CLEAR and "filed" in _rows()[-1]["why"]


def test_ack_or_clear_of_a_signal_with_no_live_state_writes_nothing():
    """A reset of nothing is not an event -- the nightly reconciliation would
    otherwise append a clear row for every quiet signal, every night."""
    assert rs.ack(KEY, via="x") is False
    assert rs.clear(KEY, why="x") is False
    assert _rows() == []


def test_the_same_fire_id_is_idempotent_no_new_row_same_tier():
    """A re-run inside one fire (a retried task, a second invocation the same
    month) must not climb the ladder."""
    _fire("2026-06")
    again = _fire("2026-06")
    assert again.tier == 1 and again.consecutive == 1 and again.recorded is False
    assert len(_rows()) == 1


def test_a_changed_subject_is_a_new_signal_never_suppressed_by_the_old():
    """The D-051 lens the ladder row names: a suppression that hides a NEW
    signal. Keys are exact (task|subject|entity); a different subject starts at
    tier 1 with its own count even while the old signal is suppressed."""
    for fid in ("2026-06", "2026-07", "2026-08"):
        _fire(fid)
    assert rs.state(KEY)["suppressed"] is True
    other = rs.make_key("quartz-monitor", "vendor Heron receipt", "F3E")
    o = _fire("2026-08", key=other, subject="vendor Heron receipt not filed")
    assert o.tier == 1 and not o.suppressed
    assert rs.state(other)["consecutive"] == 1
    # and a different entity for the same subject is also a new signal
    assert rs.make_key("t", "s", "F3E") != rs.make_key("t", "s", "OSN")


def test_dry_run_computes_the_outcome_but_writes_nothing_and_mints_nothing():
    """A dry run is a claim about EVERY write site -- this module has two (the
    ledger and the proposal file)."""
    _fire("2026-06"), _fire("2026-07")
    preview = _fire("2026-08", dry_run=True)
    assert preview.tier == 3 and preview.suppressed and preview.recorded is False
    assert preview.card_update_id == "rs-" + __import__("hashlib").sha256(
        KEY.encode()).hexdigest()[:12]
    assert len(_rows()) == 2
    assert _pending_cards() == []
    assert rs.state(KEY)["consecutive"] == 2


# ── the card id and the failure modes that would leave a signal mute ─────────

def test_card_id_is_deterministic_per_spec_and_cycle_suffixed_after_an_ack():
    """First cycle: 'rs-' + sha256(signal_key)[:12] exactly (re-fires idempotent
    on update_id). A later cycle must NOT collide with the first, already
    resolved row -- propose_update would return False, no card would render,
    and the signal would sit suppressed with nothing to tap."""
    import hashlib
    spec_id = "rs-" + hashlib.sha256(KEY.encode("utf-8")).hexdigest()[:12]
    for fid in ("2026-06", "2026-07", "2026-08"):
        o = _fire(fid)
    assert o.card_update_id == spec_id
    kr.process_decision_tap(spec_id, kr.HARRISON_SLACK_USER_ID, approve=False)
    for fid in ("2026-09", "2026-10", "2026-11"):
        o = _fire(fid)
    assert o.tier == 3 and o.suppressed
    assert o.card_update_id == spec_id + "-1"
    assert len(_pending_cards()) == 1 and _pending_cards()[0]["update_id"] == spec_id + "-1"


def test_a_lex_entity_card_is_withheld_and_nothing_is_suppressed():
    """decision_inbox.screen_decision excludes LEX fail-closed, exactly as the
    gap_autofill sibling. With no card there is nothing to ack, so the signal
    must NOT be suppressed -- the normal surface keeps alarming (pass-through)."""
    key = rs.make_key("gate", "some LEX policy question", "LEX-LLC")
    for fid in ("d1", "d2", "d3"):
        o = rs.fire(key, fire_id=fid, subject="[LEX decision -- counted, not itemized here]",
                    entity="LEX-LLC", owner_slack_id=OWNER, normal_surface="#cora-health")
    assert o.tier == 3 and o.suppressed is False and o.card_update_id is None
    assert _pending_cards() == []
    o4 = rs.fire(key, fire_id="d4", subject="x", entity="LEX-LLC",
                 owner_slack_id=OWNER, normal_surface="#cora-health")
    assert o4.suppressed is False and o4.tier == 3


def test_an_already_resolved_card_row_means_no_suppression(monkeypatch):
    """propose_update returning False is not proof a card is pending: the row
    may already be DISMISSED. Suppressing on it would mute the signal forever."""
    import hashlib
    spec_id = "rs-" + hashlib.sha256(KEY.encode("utf-8")).hexdigest()[:12]
    kr._PROPOSED_UPDATES_PATH.write_text(json.dumps({
        "update_id": spec_id, "update_type": kr.UPDATE_TYPE_DECISION,
        "description": "old", "payload": {}, "state": "DISMISSED",
    }) + "\n", encoding="utf-8")
    kr._SEEN_IDS_CACHE = None
    for fid in ("2026-06", "2026-07", "2026-08"):
        o = _fire(fid)
    assert o.tier == 3 and o.suppressed is False and o.card_update_id is None


def test_a_card_mint_error_never_breaks_the_caller_and_never_suppresses(monkeypatch):
    monkeypatch.setattr(kr, "propose_update",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("disk")))
    for fid in ("2026-06", "2026-07", "2026-08"):
        o = _fire(fid)
    assert o.tier == 3 and not o.suppressed and o.card_update_id is None


def test_a_ledger_write_failure_logs_the_critical_token(tmp_path, monkeypatch, caplog):
    """Observability that fails quietly is worse than none: an unwritable
    ledger turns 'third unacked fire' into 'first fire' every night."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("REPEAT_SIGNAL_LEDGER_PATH", str(blocker / "repeat-signals.jsonl"))
    with caplog.at_level(logging.ERROR):
        o = _fire("2026-06")
    assert o.recorded is False and o.tier == 1
    assert any(rs.WRITE_FAIL_TOKEN in r.getMessage() for r in caplog.records)


def test_the_nightly_health_check_matches_the_write_fail_token():
    import nightly_health_check as hc
    assert hc._CRITICAL_RE.search(f"2026-09-19 09:38:00,001 ERROR expected-invoices: "
                                  f"{rs.WRITE_FAIL_TOKEN} key=x")


def test_malformed_ledger_lines_are_skipped_not_fatal():
    p = rs.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json\n\n" + json.dumps({"event": "fire", "signal_key": KEY,
                                                "fire_id": "a", "tier": 1,
                                                "consecutive": 1}) + "\n", encoding="utf-8")
    assert rs.state(KEY)["consecutive"] == 1


# ── the card tap IS the ack (knowledge_review.process_decision_tap) ──────────

class TestCardTapAck:
    def _card(self):
        for fid in ("2026-06", "2026-07", "2026-08"):
            o = _fire(fid)
        assert o.suppressed
        return o.card_update_id

    def test_accept_tap_acks_and_lifts_suppression(self):
        uid = self._card()
        outcome, _ = kr.process_decision_tap(uid, kr.HARRISON_SLACK_USER_ID, approve=True)
        assert outcome == "accepted"
        st = rs.state(KEY)
        assert st["suppressed"] is False and st["consecutive"] == 0
        assert _rows()[-1]["event"] == rs.EVENT_ACK and _rows()[-1]["via"] == "card-accept"

    def test_dismiss_tap_acks_too(self):
        uid = self._card()
        outcome, _ = kr.process_decision_tap(uid, kr.HARRISON_SLACK_USER_ID, approve=False)
        assert outcome == "dismissed"
        assert rs.state(KEY)["suppressed"] is False
        assert _rows()[-1]["via"] == "card-dismiss"

    def test_a_non_harrison_tap_neither_resolves_nor_acks(self):
        """Harrison-only, as the sibling processor already was: the roster owner
        cannot ack by tapping Harrison's card."""
        uid = self._card()
        outcome, _ = kr.process_decision_tap(uid, OWNER, approve=True)
        assert outcome == "not_authorized"
        assert rs.state(KEY)["suppressed"] is True
        assert _rows()[-1]["event"] != rs.EVENT_ACK

    def test_an_apply_failure_leaves_the_signal_suppressed(self, monkeypatch):
        """The card stays PENDING (retryable), so a tap can still arrive -- the
        ack must wait for a TERMINAL state."""
        uid = self._card()
        import cora.decision_inbox as dimod
        monkeypatch.setattr(dimod, "apply_decision_accept",
                            lambda u, via="": (False, "inbox write failed: disk"))
        outcome, _ = kr.process_decision_tap(uid, kr.HARRISON_SLACK_USER_ID, approve=True)
        assert outcome == "apply_failed"
        assert rs.state(KEY)["suppressed"] is True

    def test_an_ordinary_decision_card_without_a_signal_key_is_untouched(self, tmp_path):
        kr._PROPOSED_UPDATES_PATH.write_text(json.dumps({
            "update_id": "dec-plain", "update_type": kr.UPDATE_TYPE_DECISION,
            "description": "[F3E] Decision: lock the Pelican launch.", "payload": {},
            "state": "PENDING", "source_evidence": "",
        }) + "\n", encoding="utf-8")
        kr._SEEN_IDS_CACHE = None
        outcome, _ = kr.process_decision_tap("dec-plain", kr.HARRISON_SLACK_USER_ID,
                                             approve=False)
        assert outcome == "dismissed" and _rows() == []


# ── tier 2: Harrison's briefing line ─────────────────────────────────────────

def _seed_fire_row(key, tier, ts, subject="vendor Kestrel statement not filed", fid="x"):
    p = rs.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts.isoformat(), "event": "fire", "signal_key": key,
                             "fire_id": fid, "tier": tier, "consecutive": tier,
                             "suppressed": False, "subject": subject, "entity": "F3E",
                             "owner_slack_id": OWNER, "owner_name": "Tessa Miller",
                             "normal_surface": "dm:owner"}) + "\n")


def test_tier2_line_reads_yesterdays_fire_because_the_briefing_runs_first():
    """07:30 briefing, 08:45 gate check, 09:38 invoice check: a strictly
    same-day read would never see either consumer's tier-2 fire."""
    today = date(2026, 9, 19)
    yesterday_utc = datetime(2026, 9, 18, 16, 45, tzinfo=timezone.utc)   # 09:45 AZ 9/18
    _seed_fire_row(KEY, 1, yesterday_utc - timedelta(days=30), fid="a")
    _seed_fire_row(KEY, 2, yesterday_utc, fid="b")
    sigs = rs.tier2_signals(today)
    assert [s["signal_key"] for s in sigs] == [KEY]
    lines = rs.format_briefing_lines(sigs)
    assert lines == ["Repeat signal (2nd consecutive fire): vendor Kestrel statement not "
                     "filed -- owner Tessa Miller"]


def test_tier2_line_never_repeats_past_its_window_and_never_at_tier_3():
    today = date(2026, 9, 19)
    old = datetime(2026, 9, 16, 16, 45, tzinfo=timezone.utc)
    _seed_fire_row(KEY, 1, old - timedelta(days=30), fid="a")
    _seed_fire_row(KEY, 2, old, fid="b")
    assert rs.tier2_signals(today) == []           # three days old: gone, not repeated
    other = rs.make_key("t", "vendor Heron receipt", "OSN")
    now = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)
    _seed_fire_row(other, 1, now - timedelta(days=60), fid="a")
    _seed_fire_row(other, 2, now - timedelta(days=30), fid="b")
    _seed_fire_row(other, 3, now, fid="c")
    assert rs.tier2_signals(today) == []           # tier 3 is a different surface


def test_an_acked_tier2_signal_drops_out_of_the_briefing():
    today = date(2026, 9, 19)
    now = datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)
    _seed_fire_row(KEY, 1, now - timedelta(days=30), fid="a")
    _seed_fire_row(KEY, 2, now, fid="b")
    assert len(rs.tier2_signals(today)) == 1
    rs.ack(KEY, via="test")
    assert rs.tier2_signals(today) == []


def test_briefing_appends_the_lines_after_synthesis_and_only_for_harrison():
    import run_daily_briefing as rdb
    today = datetime.now(rs._AZ).date()
    _fire("2026-07"), _fire("2026-08")             # live tier 2, stamped now
    body = "SYNTHESIZED BODY\n"
    out = rdb._append_repeat_signal_lines(body, today=today)
    assert out.startswith("SYNTHESIZED BODY")
    assert "Repeat signal (2nd consecutive fire): vendor Kestrel statement not filed" in out
    assert rdb._append_repeat_signal_lines("plain", today=today - timedelta(days=10)) == "plain"
    # Harrison-only, applied AFTER build_user_briefing returns -- never inside
    # the LLM-synthesized sections (the role-header incident).
    src = (_REPO_ROOT / "scripts" / "run_daily_briefing.py").read_text(encoding="utf-8")
    call = src.index("text = _append_repeat_signal_lines(text)")
    window = src[call - 200:call]
    assert "if rec.slack_id == _HARRISON_SLACK_ID:" in window
    assert "build_user_briefing(rec" in window
    assert src.count("_append_repeat_signal_lines(text)") == 1


def test_briefing_read_is_fail_soft(monkeypatch):
    import run_daily_briefing as rdb
    monkeypatch.setattr(rs, "tier2_signals",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ledger")))
    assert rdb._append_repeat_signal_lines("body") == "body"
