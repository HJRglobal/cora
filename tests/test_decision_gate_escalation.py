"""Decision gates alarm DAILY until a human ack (D-310; Code #13 slice 9b).

THE AUDIT FINDING (2026-09-19 section 3). check_decision_gates recorded its own
#cora-health post as surface "health_check", which undelivered_overdue then
counted as a delivery for DELIVERY_WINDOW_DAYS=8. A blown gate was CRITICAL on
day N, silent N+1..N+8, CRITICAL again on N+9 -- self-clearing with no human in
the loop. The pin that matters here: TWO CONSECUTIVE DAYS on the same blown gate
are BOTH critical with no ack. The health-check emission is an escalation PING
(recorded under the "ping:" surface class, auditable, ignored by the control),
routed through repeat_signal so the third day mints ONE card and suppresses.

Helpers mirror tests/test_decision_lane.py so the two files read the same file
format; that file's own tests stay green untouched.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from cora import decision_alerts as da  # noqa: E402
from cora import decision_lane as dl  # noqa: E402
from cora import knowledge_review as kr  # noqa: E402
from cora import repeat_signal as rs  # noqa: E402

TODAY = date(2026, 8, 19)
NOW = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)
TOPIC = "Osprey ledger cutover"


def _entry(topic, severity="P2", gate="2026-08-13", surfaced="2026-08-10",
           touched="2026-08-13", entity="OSN", owner="Harrison", extra=""):
    return "\n".join([
        f"### {topic}",
        f"- **Entity**: {entity}",
        "- **Question**: what to do",
        f"- **Decision-maker**: {owner}",
        "- **Blockers**: bandwidth",
        f"- **Severity**: {severity}",
        f"- **Surfaced**: {surfaced}",
        f"- **Last touched**: {touched}",
        *([f"- **Gate**: {gate}"] if gate else []),
        f"- **Owner of next nudge**: {owner}",
        extra,
        "",
    ])


def _file(*entries, header="# Decisions pending\n\n"):
    return header + "\n".join(entries)


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "decision-deliveries.jsonl"
    monkeypatch.setattr(dl, "DELIVERY_LEDGER", path)
    return path


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("REPEAT_SIGNAL_LEDGER_PATH", str(tmp_path / "repeat-signals.jsonl"))
    monkeypatch.setenv("DECISION_ALERT_STATE_PATH", str(tmp_path / "decision-alert-pending.json"))
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "proposed.jsonl")
    monkeypatch.setattr(kr, "_ARCHIVE_PATH", tmp_path / "proposed.archive.jsonl")
    kr._SEEN_IDS_CACHE = None
    kr._ARCHIVE_IDS_CACHE = None
    yield
    kr._SEEN_IDS_CACHE = None
    kr._ARCHIVE_IDS_CACHE = None


def _hc(monkeypatch, tmp_path, content):
    path = tmp_path / "decisions-pending.md"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("STRATEGY_DECISIONS_PATH", str(path))
    import nightly_health_check as hc
    return hc


def _gate_key(topic, entity="OSN"):
    return rs.make_key("decision-gate", dl._topic_key(topic), entity)


def _cards():
    return [u for u in kr.load_proposed_updates()
            if u.get("update_type") == kr.UPDATE_TYPE_DECISION]


# ── the D-310 pin ────────────────────────────────────────────────────────────

def test_two_consecutive_days_on_a_blown_gate_are_both_critical_with_no_ack(
        monkeypatch, tmp_path, ledger):
    """THE pin. Before: day 2 was silent because day 1's own post counted as a
    delivery for 8 days."""
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    day1 = hc.check_decision_gates(today=TODAY)
    day2 = hc.check_decision_gates(today=TODAY + timedelta(days=1))
    assert day1.status == "critical" and TOPIC in day1.detail
    assert day2.status == "critical" and TOPIC in day2.detail
    st = rs.state(_gate_key(TOPIC))
    assert st["consecutive"] == 2 and st["suppressed"] is False


def test_the_ping_is_recorded_for_the_audit_trail_but_never_suppresses(
        monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    hc.check_decision_gates(today=TODAY)
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["surface"] == "ping:health_check"
    assert rows[0]["key"] == dl._topic_key(TOPIC)
    # ignored by the control: the index is empty, the row still overdue
    assert dl.delivery_index(now=datetime.now(timezone.utc)) == {}
    entries = dl.parse_entries(_file(_entry(TOPIC)), today=TODAY)
    overdue = dl.undelivered_overdue(entries, today=TODAY, now=datetime.now(timezone.utc))
    assert len(overdue) == 1 and overdue[0]["never_delivered"] is True


def test_a_real_human_facing_delivery_still_silences_the_control(monkeypatch, tmp_path, ledger):
    """The change is narrow: only PING rows are ignored. strategy_memo /
    channel_synthesis deliveries keep their meaning."""
    dl.record_delivery(TOPIC, "strategy_memo")
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    assert hc.check_decision_gates(today=TODAY).status == "ok"


def test_a_legacy_health_check_row_is_a_ping_not_a_delivery(monkeypatch, tmp_path, ledger):
    """D-051 EF-3 regression. The live ledger holds 16 rows written BEFORE the
    'ping:' prefix existed, surface 'health_check' (latest 2026-09-18). Skipping
    only the prefix left them counting as deliveries, so the four blown gates
    stayed silent for up to 8 more days after the merge -- the exact self-
    clearing silence D-310 exists to end. The legacy spelling is a ping too."""
    stamp = (NOW - timedelta(hours=20)).isoformat()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"ts": stamp, "surface": "health_check",
                                  "topic": TOPIC, "key": dl._topic_key(TOPIC)}) + "\n",
                      encoding="utf-8")
    assert "health_check" in dl.LEGACY_PING_SURFACES
    assert dl.delivery_index(now=NOW) == {}
    entries = dl.parse_entries(_file(_entry(TOPIC)), today=TODAY)
    overdue = dl.undelivered_overdue(entries, today=TODAY, now=NOW)
    assert len(overdue) == 1 and overdue[0]["never_delivered"] is True
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    assert hc.check_decision_gates(today=TODAY).status == "critical"
    # a real human-facing surface written the same day still counts
    dl.record_delivery(TOPIC, "strategy_memo")
    assert dl._topic_key(TOPIC) in dl.delivery_index(now=datetime.now(timezone.utc))


def test_the_old_delivery_surface_name_is_gone_from_the_check():
    src = (_REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(encoding="utf-8")
    assert 'PING_SURFACE_PREFIX + "health_check"' in src
    assert '"health_check")' not in src.replace('PING_SURFACE_PREFIX + "health_check")', "")
    assert "THE HEALTH DIGEST IS ITSELF A DELIVERY" not in src
    assert "ESCALATION PING, NOT A DELIVERY" in src


# ── the ladder on the gate consumer ──────────────────────────────────────────

def test_third_day_mints_one_card_and_the_check_warns_suppressed_instead_of_critical(
        monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    for d in range(2):
        assert hc.check_decision_gates(today=TODAY + timedelta(days=d)).status == "critical"
    day3 = hc.check_decision_gates(today=TODAY + timedelta(days=2))
    assert day3.status == "warn"
    cards = _cards()
    assert len(cards) == 1 and cards[0]["state"] == "PENDING"
    assert cards[0]["payload"]["signal_key"] == _gate_key(TOPIC)
    assert f"gate alarm suppressed pending ack (card {cards[0]['update_id']})" in day3.detail
    assert TOPIC in day3.detail
    # day 4: still suppressed, still ONE card, still a warn -- not a repeat critical
    day4 = hc.check_decision_gates(today=TODAY + timedelta(days=3))
    assert day4.status == "warn" and len(_cards()) == 1
    assert rs.read_rows()[-1]["event"] == rs.EVENT_SUPPRESSED


def test_harrisons_card_tap_acks_and_the_next_day_starts_again_at_tier_one(
        monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    for d in range(3):
        hc.check_decision_gates(today=TODAY + timedelta(days=d))
    uid = _cards()[0]["update_id"]
    outcome, _ = kr.process_decision_tap(uid, kr.HARRISON_SLACK_USER_ID, approve=False)
    assert outcome == "dismissed"
    day4 = hc.check_decision_gates(today=TODAY + timedelta(days=3))
    assert day4.status == "critical"                 # the gate is still blown: loud again
    assert rs.state(_gate_key(TOPIC))["consecutive"] == 1


def test_a_threaded_reply_on_the_decision_alert_is_an_ack(monkeypatch, tmp_path, ledger):
    """decision_alerts ANSWERED = a human answered; the gate check must not
    re-alarm a decision Harrison already replied to."""
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    assert hc.check_decision_gates(today=TODAY).status == "critical"
    rec = da.record_alert(topic=TOPIC, severity="P2", entity="OSN", owner="Harrison",
                          surfaced="2026-08-10", dm_channel_id="D1",
                          alert_message_ts="1.1", target_user_id=kr.HARRISON_SLACK_USER_ID)
    da.mark_state("1.1", da.STATE_ANSWERED, answer="cut over on the 1st")
    assert rec["topic_key"] in da.answered_topic_keys()
    day2 = hc.check_decision_gates(today=TODAY + timedelta(days=1))
    assert day2.status == "warn" and "acknowledged by a human reply" in day2.detail
    assert rs.state(_gate_key(TOPIC))["consecutive"] == 0
    assert rs.read_rows()[-1]["event"] == rs.EVENT_ACK
    assert rs.read_rows()[-1]["via"] == "decision-alert-reply"


def test_a_closed_heading_clears_the_signal(monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC), _entry("Heron budget", gate=None)))
    hc.check_decision_gates(today=TODAY)
    hc.check_decision_gates(today=TODAY + timedelta(days=1))
    assert rs.state(_gate_key(TOPIC))["consecutive"] == 2
    closed = _file(_entry(f"{TOPIC} -- CLOSED 2026-08-20"), _entry("Heron budget", gate=None))
    (tmp_path / "decisions-pending.md").write_text(closed, encoding="utf-8")
    result = hc.check_decision_gates(today=TODAY + timedelta(days=2))
    assert result.status == "warn"                   # no gates left to enforce
    assert rs.state(_gate_key(TOPIC))["consecutive"] == 0
    assert rs.read_rows()[-1]["event"] == rs.EVENT_CLEAR


def test_the_recently_resolved_tail_clears_the_signal(monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC), _entry("Heron budget", gate="2026-12-01")))
    hc.check_decision_gates(today=TODAY)
    resolved = _file(_entry("Heron budget", gate="2026-12-01")) + \
        "\n## Recently resolved\n\n" + _entry(TOPIC)
    (tmp_path / "decisions-pending.md").write_text(resolved, encoding="utf-8")
    assert hc.check_decision_gates(today=TODAY + timedelta(days=1)).status == "ok"
    assert rs.state(_gate_key(TOPIC))["consecutive"] == 0


def test_an_unreadable_file_is_an_outage_not_a_clear(monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    hc.check_decision_gates(today=TODAY)
    monkeypatch.setenv("STRATEGY_DECISIONS_PATH", str(tmp_path / "missing.md"))
    assert hc.check_decision_gates(today=TODAY + timedelta(days=1)).status == "warn"
    assert rs.state(_gate_key(TOPIC))["consecutive"] == 1


def test_a_lex_gate_keeps_alarming_daily_with_no_card_and_no_suppression(
        monkeypatch, tmp_path, ledger):
    """LEX is aggregate-only on every new surface (decision_lane ~364) and the
    decision screen excludes LEX cards fail-closed -- so nothing can be
    suppressed, and the redacted alarm stands every day."""
    hc = _hc(monkeypatch, tmp_path,
             _file(_entry("Lex program question about Part 2 scope", entity="LEX-LBHS")))
    for d in range(4):
        r = hc.check_decision_gates(today=TODAY + timedelta(days=d))
        assert r.status == "critical"
        assert "Part 2" not in r.detail and "LEX decision" in r.detail
    assert _cards() == []
    key = rs.make_key("decision-gate", dl._topic_key("Lex program question about Part 2 scope"),
                      "LEX-LBHS")
    st = rs.state(key)
    assert st["consecutive"] == 4 and st["suppressed"] is False
    # the ledger carries the REDACTED subject, never the heading
    for row in rs.read_rows():
        assert "Part 2" not in json.dumps(row)


def test_owner_of_next_nudge_resolves_via_the_roster_else_harrison(monkeypatch, tmp_path):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    assert hc._gate_owner({"owner": "Tessa"}) == ("U0B3KH5UZJ7", "Tessa Miller")
    assert hc._gate_owner({"owner": "Harrison"})[0] == "U0B2RM2JYJ1"
    assert hc._gate_owner({"owner": "unassigned"}) == ("U0B2RM2JYJ1", "Harrison")
    assert hc._gate_owner({"owner": ""}) == ("U0B2RM2JYJ1", "Harrison")


def test_the_signal_key_shape_is_task_topic_key_entity(monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    hc.check_decision_gates(today=TODAY)
    rows = rs.read_rows()
    assert rows[0]["signal_key"] == f"decision-gate|{dl._topic_key(TOPIC)}|OSN"
    assert rows[0]["fire_id"] == TODAY.isoformat()
    assert rows[0]["normal_surface"] == "#cora-health"
    assert rows[0]["owner_slack_id"] == "U0B2RM2JYJ1"


def test_repeat_signal_unavailable_falls_back_to_pass_through(monkeypatch, tmp_path, ledger):
    """The ladder row's demotion posture: if escalation cannot run, every row
    alarms -- the module can never make the control QUIETER by failing."""
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    monkeypatch.setattr(rs, "fire", lambda *a, **k: (_ for _ in ()).throw(OSError("ledger")))
    assert hc.check_decision_gates(today=TODAY).status == "critical"


def test_two_blown_gates_escalate_independently(monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC), _entry("Heron budget lock", entity="F3E")))
    for d in range(3):
        hc.check_decision_gates(today=TODAY + timedelta(days=d))
    assert len(_cards()) == 2
    keys = {c["payload"]["signal_key"] for c in _cards()}
    assert keys == {_gate_key(TOPIC), _gate_key("Heron budget lock", "F3E")}
