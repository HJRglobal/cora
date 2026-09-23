"""R14-1 (Code #14): nightly_health_check --dry-run holds at EVERY write site (D-290).

THE FINDING (Code #13 review, ruling R-1). check_decision_gates had no dry_run
parameter and main() called it bare, so `nightly_health_check.py --dry-run`
still wrote repeat-signal rows, `ping:` delivery rows and, on the third day,
minted a tier-3 Harrison card. VERIFY-FIRST found five write sites reachable from
the check (not the three the kickoff named) and two SIBLING checks that also
wrote under --dry-run: check_kb_health rewrote the KB baseline ("yesterday"),
which would hide a >20% drop from the next real run, and check_flywheel appended
today's pending-size baseline.

The pins below drive the REAL state machine over tmp ledgers (lesson 69: a mocked
function under test proves nothing about its guard) and compare BYTES, and one
test drives main(--dry-run) end to end (lesson 64: a dry run is a claim about
every write site -- prove it at the entry point, not per helper).
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
sys.path.insert(0, str(_REPO_ROOT / "tests"))

from cora import decision_alerts as da  # noqa: E402
from cora import knowledge_review as kr  # noqa: E402
from cora import repeat_signal as rs  # noqa: E402
from test_decision_gate_escalation import (  # noqa: E402
    TODAY, TOPIC, _cards, _entry, _file, _gate_key, _hc, _pin_resolved_at,
    _isolated, ledger,  # noqa: F401 -- fixtures re-exported for this module
)


def _snapshot(tmp_path: Path, ledger_path: Path) -> dict[str, bytes | None]:
    """Every file a gate check can write, absent vs exact bytes."""
    paths = {
        "repeat-signals": tmp_path / "repeat-signals.jsonl",
        "deliveries": ledger_path,
        "proposed": tmp_path / "proposed.jsonl",
        "proposed-archive": tmp_path / "proposed.archive.jsonl",
        "alert-state": tmp_path / "decision-alert-pending.json",
    }
    return {k: (p.read_bytes() if p.exists() else None) for k, p in paths.items()}


def test_dry_run_over_a_blown_gate_on_day_three_leaves_every_ledger_byte_identical(
        monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    assert hc.check_decision_gates(today=TODAY).status == "critical"
    assert hc.check_decision_gates(today=TODAY + timedelta(days=1)).status == "critical"
    before = _snapshot(tmp_path, ledger)

    preview = hc.check_decision_gates(today=TODAY + timedelta(days=2), dry_run=True)

    assert _snapshot(tmp_path, ledger) == before
    assert _cards() == []
    # it still PREDICTS the real run: day three would mint + suppress
    card_id = "rs-" + hashlib.sha256(_gate_key(TOPIC).encode("utf-8")).hexdigest()[:12]
    assert preview.status == "warn" and card_id in preview.detail
    # ...and the real day three mints exactly ONE card: the dry run pre-minted nothing
    real = hc.check_decision_gates(today=TODAY + timedelta(days=2))
    assert real.status == "warn" and len(_cards()) == 1
    assert _cards()[0]["update_id"] == card_id


def test_dry_run_on_day_one_writes_no_fire_row_and_no_ping_row(monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    r = hc.check_decision_gates(today=TODAY, dry_run=True)
    assert r.status == "critical" and TOPIC in r.detail
    assert rs.read_rows() == []
    assert not ledger.exists()
    assert not (tmp_path / "repeat-signals.jsonl").exists()


def test_dry_run_never_clears(monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC), _entry("Heron budget", gate=None)))
    hc.check_decision_gates(today=TODAY)
    hc.check_decision_gates(today=TODAY + timedelta(days=1))
    closed = _file(_entry(f"{TOPIC} -- CLOSED 2026-08-20"), _entry("Heron budget", gate=None))
    (tmp_path / "decisions-pending.md").write_text(closed, encoding="utf-8")
    before = _snapshot(tmp_path, ledger)
    hc.check_decision_gates(today=TODAY + timedelta(days=2), dry_run=True)
    assert _snapshot(tmp_path, ledger) == before
    assert rs.state(_gate_key(TOPIC))["consecutive"] == 2
    # the reconcile helper REPORTS what it would clear
    assert hc._gate_reconcile_clears([], dry_run=True) == [_gate_key(TOPIC)]
    assert rs.state(_gate_key(TOPIC))["consecutive"] == 2


def test_dry_run_never_acks_but_classifies_the_row_as_acked(monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    hc.check_decision_gates(today=TODAY)
    da.record_alert(topic=TOPIC, severity="P2", entity="OSN", owner="Harrison",
                    surfaced="2026-08-10", dm_channel_id="D1",
                    alert_message_ts="1.1", target_user_id=kr.HARRISON_SLACK_USER_ID)
    da.mark_state("1.1", da.STATE_ANSWERED, answer="cut over on the 1st")
    # R14-5: the ack is bounded to one window after resolved_at, which mark_state
    # stamps from the WALL clock -- pin it to the pinned TODAY (clock-collision rule).
    _pin_resolved_at("1.1", datetime(2026, 8, 19, 18, tzinfo=timezone.utc))
    before = _snapshot(tmp_path, ledger)
    # the preview reads the answer (same verdict as a real run) ...
    r = hc.check_decision_gates(today=TODAY + timedelta(days=1), dry_run=True)
    assert r.status == "warn" and "acknowledged by a human reply" in r.detail
    # ... and writes no ACK row
    assert _snapshot(tmp_path, ledger) == before
    assert rs.read_rows()[-1]["event"] == rs.EVENT_FIRE


def test_dry_run_suppressed_branch_writes_no_suppressed_row_and_no_implicit_ack(
        monkeypatch, tmp_path, ledger):
    """Day 4 with the card still PENDING = the EVENT_SUPPRESSED row; day 4 with the
    card resolved out of band = the implicit 'card-resolved-out-of-band' ACK. Both
    are write sites inside fire(); both must hold under dry_run."""
    hc = _hc(monkeypatch, tmp_path, _file(_entry(TOPIC)))
    for d in range(3):
        hc.check_decision_gates(today=TODAY + timedelta(days=d))
    before = _snapshot(tmp_path, ledger)
    r = hc.check_decision_gates(today=TODAY + timedelta(days=3), dry_run=True)
    assert r.status == "warn" and _snapshot(tmp_path, ledger) == before

    # resolve the card OUT OF BAND (a hand edit, not a tap): the ledger still says
    # suppressed, so the next real fire would write the implicit ack
    uid = _cards()[0]["update_id"]
    rows = [json.loads(l) for l in (tmp_path / "proposed.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    for row in rows:
        if row.get("update_id") == uid:
            row["state"] = "DISMISSED"
    (tmp_path / "proposed.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    kr._SEEN_IDS_CACHE = None
    before = _snapshot(tmp_path, ledger)
    r = hc.check_decision_gates(today=TODAY + timedelta(days=3), dry_run=True)
    assert r.status == "critical"            # implicit ack -> a fresh tier-1 alarm
    assert _snapshot(tmp_path, ledger) == before
    assert not any(row.get("via") == "card-resolved-out-of-band" for row in rs.read_rows())


def test_dry_run_preview_of_a_lex_gate_on_day_three_matches_the_real_run(
        monkeypatch, tmp_path, ledger):
    """The fidelity gap: fire(dry_run=True) used to return suppressed=True at tier
    3 unconditionally, so a dry run over a LEX gate read 'suppressed pending ack'
    while the real run stayed CRITICAL (the decision screen withholds LEX cards)."""
    topic = "Lex program question about Part 2 scope"
    hc = _hc(monkeypatch, tmp_path, _file(_entry(topic, entity="LEX-LBHS")))
    hc.check_decision_gates(today=TODAY)
    hc.check_decision_gates(today=TODAY + timedelta(days=1))
    preview = hc.check_decision_gates(today=TODAY + timedelta(days=2), dry_run=True)
    real = hc.check_decision_gates(today=TODAY + timedelta(days=2))
    assert preview.status == real.status == "critical"
    assert "Part 2" not in preview.detail
    assert _cards() == []


def test_repeat_signal_preview_matches_the_mint_for_a_resolved_cycle_card(tmp_path, monkeypatch):
    """_preview_card applies the mint's own "this cycle's card is already resolved
    -> no suppression" rule."""
    key = rs.make_key("quartz-monitor", "vendor Kestrel statement", "F3E")
    kw = dict(subject="vendor Kestrel statement not filed", entity="F3E",
              owner_slack_id="U0B3KH5UZJ7", owner_name="Tessa Miller", normal_surface="dm:owner")
    rs.fire(key, fire_id="2026-06", **kw)
    rs.fire(key, fire_id="2026-07", **kw)
    st = rs._state(key)
    uid = rs._card_update_id(key, st.cycle)
    assert rs._preview_card(key, st, subject=kw["subject"], entity="F3E",
                            owner_slack_id=kw["owner_slack_id"], owner_name=kw["owner_name"]) == uid
    monkeypatch.setattr("cora.knowledge_review._find_update",
                        lambda _uid: {"update_id": _uid, "state": "DISMISSED"})
    assert rs.fire(key, fire_id="2026-08", dry_run=True, **kw).suppressed is False
    monkeypatch.setattr("cora.knowledge_review._find_update",
                        lambda _uid: (_ for _ in ()).throw(OSError("unreadable")))
    assert rs.fire(key, fire_id="2026-08", dry_run=True, **kw).suppressed is False
    assert len(rs.read_rows()) == 2


# ── the sibling write sites ──────────────────────────────────────────────────

def test_kb_health_dry_run_does_not_rewrite_the_baseline(tmp_path, monkeypatch):
    import sqlite3
    import nightly_health_check as hc
    db = tmp_path / "kb.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE knowledge_chunks (source TEXT)")
    conn.executemany("INSERT INTO knowledge_chunks VALUES (?)", [("gmail",)] * 60)
    conn.commit()
    conn.close()
    baseline = tmp_path / "health-kb-baseline.json"
    baseline.write_text(json.dumps({"gmail": 100}), encoding="utf-8")
    monkeypatch.setattr(hc, "_KB_DB", db)
    monkeypatch.setattr(hc, "_BASELINE", baseline)
    before = baseline.read_bytes()
    dry = hc.check_kb_health(dry_run=True)
    assert baseline.read_bytes() == before
    assert any(r.status == "warn" and "gmail" in r.detail for r in dry)  # the drop IS seen
    hc.check_kb_health()                                                 # the real run writes
    assert json.loads(baseline.read_text(encoding="utf-8")) == {"gmail": 60}


def test_flywheel_dry_run_passes_update_baseline_false(monkeypatch):
    import nightly_health_check as hc
    from cora import flywheel_metrics as fm
    seen: list[bool] = []

    def _collect(*, update_baseline=False, **_kw):
        seen.append(update_baseline)
        return {}

    monkeypatch.setattr(fm, "collect", _collect)
    monkeypatch.setattr(fm, "evaluate", lambda _m: [])
    hc.check_flywheel(dry_run=True)
    hc.check_flywheel()
    assert seen == [False, True]


# ── the entry point ──────────────────────────────────────────────────────────

_WRITE_SITE_CHECKS = ("check_heartbeat", "check_decision_gates", "check_kb_health",
                      "check_flywheel")


def _stub_every_check(hc, monkeypatch, calls: dict[str, dict]):
    """Replace EVERY module-level check_* with a recorder. A check added later
    that main() calls is stubbed automatically; the write-site ones record the
    dry_run they were handed."""
    for name in dir(hc):
        if not name.startswith("check_") or not callable(getattr(hc, name)):
            continue

        def _rec(*args, _name=name, **kwargs):
            calls[_name] = {"args": args, "kwargs": kwargs}
            if _name in ("check_scheduled_tasks", "check_task_last_results", "check_logs_24h",
                         "check_kb_health", "check_api_connectivity", "check_flywheel"):
                return []
            return hc.CheckResult(_name, "ok", "stub")

        monkeypatch.setattr(hc, name, _rec)


@pytest.mark.parametrize("argv,expect", [(["--dry-run"], True), ([], False)])
def test_main_threads_dry_run_into_every_write_site_check(monkeypatch, tmp_path, capfd,
                                                          argv, expect):
    import nightly_health_check as hc
    calls: dict[str, dict] = {}
    _stub_every_check(hc, monkeypatch, calls)
    monkeypatch.setattr(hc, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(hc, "_post_to_slack", lambda *a, **k: None)
    monkeypatch.setattr(hc.logging, "basicConfig", lambda **_k: None)
    monkeypatch.setattr(sys, "argv", ["nightly_health_check.py", *argv])
    hc.main()
    assert calls["check_heartbeat"]["args"] == (expect,)
    for name in _WRITE_SITE_CHECKS[1:]:
        assert calls[name]["kwargs"].get("dry_run") is expect, name
