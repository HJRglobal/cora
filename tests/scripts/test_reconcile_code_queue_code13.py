"""Step 7.5 reconcile for Code #13: every transition is gated on the fix being
present on this tree (behavioural checks); dry-run writes nothing; the apply labels
follow the real outcomes; every SHIPPED transition passes the C7 bundle reference;
the probe proves the gate on a throwaway ledger; the RIDER bundle rows are never
touched; the rail-2 row gets a provenance NOTE, never a status flip."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    # the script's import-time load_dotenv(override=True) writes straight into
    # os.environ (monkeypatch cannot see it) and un-pins the live flags conftest set.
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location(
            "reconcile_code13",
            _REPO_ROOT / "scripts" / "reconcile_code_queue_code13_2026-09-19.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


EXPECTED_SHIPPED = {"cq-2a88e32a75ea", "cq-70d7b203f7ad", "cq-2e02f1fd0f65", "cq-fb50c9e6c911",
                    "cq-19b0298cf5be", "cq-8c4f2f4e73fc", "cq-7a724ee43964", "cq-6afa86210ba0",
                    "cq-da5abb36df07", "cq-eebf2408f252", "cq-c5ed06e1f0bf", "cq-21207e34a954",
                    "cq-06045f418bd2"}
EXPECTED_LEFT = {"cq-85b35413b020", "cq-7f3febc35710", "cq-9f686d94365e", "cq-bbdc206a097c",
                 "cq-9a409c0612dc", "cq-41540856f95b", "cq-9f1f6e9b7121", "cq-1de87e0f2e08",
                 "cq-6ffdd65034fa", "cq-8324caf8fa08", "cq-cdb2510b682c", "cq-5f8dc77ff9fb"}
RIDER_BUNDLE = {"cq-8d16f1a557e5", "cq-40baab26d7f3", "cq-c624f28c772a"}


def test_ids_match_the_kickoff_and_every_precondition_passes_on_this_tree():
    mod = _load()
    assert EXPECTED_SHIPPED <= set(mod.SHIPPED)
    assert set(mod.DISMISSED) == {"cq-64272c86f5e1"}
    assert mod.SUPERSEDED == {"cq-8618a256a8ef": ("cq-19b0298cf5be", mod.SUPERSEDED["cq-8618a256a8ef"][1])}
    assert EXPECTED_LEFT <= set(mod.LEFT_OPEN)
    assert set(mod.NOT_TOUCHED_RIDER_BUNDLE) == RIDER_BUNDLE
    assert set(mod.NOTED) == {"cq-85b35413b020"}
    # the RIDER 2 seed ships WITH slice 1 (kickoff section 10) -- never separately
    assert "cq-70d7b203f7ad" in mod.SHIPPED
    # the rail-2 row is LEFT (harness SHIP: NO), never in SHIPPED
    assert "cq-85b35413b020" not in mod.SHIPPED and "SHIP: NO" in mod.HARNESS_NOTE
    # nothing in this script touches the identity rider's rows
    assert not (RIDER_BUNDLE & (set(mod.SHIPPED) | set(mod.DISMISSED) | set(mod.SUPERSEDED) | set(mod.NOTED)))
    for cq_id, fn in mod.PRECONDITIONS.items():
        assert fn() is None, (cq_id, fn())
    assert mod.BUNDLE_ID == "code-13" and mod.BRANCH == "claude/code-13-honesty-rail-2026-09-19"


def _rec(status, severity="MEDIUM"):
    return lambda cq_id: {"id": cq_id, "status": status, "severity": severity}


def _boom(*a, **k):
    raise AssertionError("dry-run wrote!")


def test_dry_run_reports_everything_and_writes_nothing(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("PROPOSED"))
    monkeypatch.setattr(mod.code_queue, "load_items", lambda: [])
    for name in ("process_queue_action", "dismiss_with_evidence", "supersede_item",
                 "set_severity", "_append_event"):
        monkeypatch.setattr(mod.code_queue, name, _boom)
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    for cq in EXPECTED_SHIPPED:
        assert f"would mark SHIPPED  {cq}" in out
    assert "would DISMISS with evidence  cq-64272c86f5e1" in out and "D-300" in out
    assert "would mark SUPERSEDED  cq-8618a256a8ef  by cq-19b0298cf5be" in out
    assert "would record a provenance note on  cq-85b35413b020" in out
    for cq in EXPECTED_LEFT:
        assert f"LEFT OPEN  {cq}" in out
    for cq in RIDER_BUNDLE:
        assert f"NOT TOUCHED  {cq}" in out
    assert "BLOCKED" not in out


def test_apply_passes_the_bundle_reference_and_labels_follow_outcomes(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("PROPOSED", severity="LOW"))
    monkeypatch.setattr(mod.code_queue, "load_items", lambda: [])
    monkeypatch.setattr(mod, "_head_commit", lambda: "abc1234")
    ship_calls = []

    def fake_action(action_id, cq_id, actor_id, **kw):
        ship_calls.append((cq_id, kw))
        assert action_id == mod.code_queue.ACTION_MARK_SHIPPED and actor_id == mod.HARRISON_ID
        return ("shipped", "\U0001F6A2 Marked shipped.") if cq_id != "cq-eebf2408f252" else ("refused", "no bundle")
    monkeypatch.setattr(mod.code_queue, "process_queue_action", fake_action)
    monkeypatch.setattr(mod.code_queue, "dismiss_with_evidence",
                        lambda cq, actor, note, **kw: ("dismissed", f"dismissed: {note[:20]}"))
    monkeypatch.setattr(mod.code_queue, "supersede_item", lambda loser, winner: True)
    events = []
    monkeypatch.setattr(mod.code_queue, "_append_event", lambda ev: events.append(ev))
    assert mod.main(["--apply"]) == 1  # the one refused ship makes it non-zero
    out = capsys.readouterr().out
    assert "SHIPPED  cq-2a88e32a75ea" in out and "SHIPPED  cq-70d7b203f7ad" in out
    assert "NOT SHIPPED  cq-eebf2408f252" in out and "refused: no bundle" in out
    # every ship call carried the C7 reference
    assert ship_calls and all(kw == {"bundle_id": "code-13", "branch": mod.BRANCH, "commit": "abc1234"}
                              for _cq, kw in ship_calls)
    assert {cq for cq, _ in ship_calls} == set(mod.SHIPPED)
    assert "DISMISSED  cq-64272c86f5e1" in out
    assert "SUPERSEDED  cq-8618a256a8ef  by cq-19b0298cf5be" in out
    assert "NOTED  cq-85b35413b020" in out
    recs = [e for e in events if e.get("event") == "reconciled"]
    assert {e["id"] for e in recs} == {"cq-8618a256a8ef", "cq-64272c86f5e1", "cq-85b35413b020"}
    assert all(e["bundle_id"] == "code-13" and e["commit"] == "abc1234" for e in recs)
    note = next(e for e in recs if e["id"] == "cq-85b35413b020")
    assert note["transition"].startswith("LEFT STAGED") and "SHIP: NO" in note["transition"]
    # the noted row's STATUS was never touched: no ship / dismiss / supersede call named it
    assert "cq-85b35413b020" not in {cq for cq, _ in ship_calls}


def test_terminal_rows_are_never_flipped(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("SUPERSEDED"))
    monkeypatch.setattr(mod.code_queue, "load_items", lambda: [])
    for name in ("process_queue_action", "dismiss_with_evidence", "supersede_item"):
        monkeypatch.setattr(mod.code_queue, name, _boom)
    monkeypatch.setattr(mod.code_queue, "_append_event", lambda ev: None)
    assert mod.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert "refusing to flip a terminal row" in out


def test_probe_gate_refuses_without_bundle_and_never_touches_the_real_ledger(capsys):
    mod = _load()
    real = mod.code_queue._DEFAULT_EVENT_LEDGER
    redirected = mod.code_queue._EVENT_LEDGER
    before = real.read_text(encoding="utf-8") if real.exists() else None
    assert mod.main(["--probe-gate"]) == 0
    out = capsys.readouterr().out
    assert "PROBE OK" in out and "refused" in out
    after = real.read_text(encoding="utf-8") if real.exists() else None
    assert before == after
    assert mod.code_queue._EVENT_LEDGER == redirected  # restored


def test_env_pins_survive_loading_the_script(monkeypatch):
    monkeypatch.setenv("CORA_CODE_QUEUE", "off")
    monkeypatch.setenv("CORA_SENTINEL_ENFORCE", "observe")
    _load()
    assert os.environ["CORA_CODE_QUEUE"] == "off" and os.environ["CORA_SENTINEL_ENFORCE"] == "observe"


def test_a_raising_precondition_blocks_instead_of_aborting(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("PROPOSED"))
    monkeypatch.setattr(mod.code_queue, "load_items", lambda: [])
    monkeypatch.setitem(mod.PRECONDITIONS, "cq-2a88e32a75ea", lambda: 1 / 0)
    for name in ("process_queue_action", "dismiss_with_evidence", "supersede_item"):
        monkeypatch.setattr(mod.code_queue, name, _boom)
    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "BLOCKED  cq-2a88e32a75ea" in out and "precondition raised ZeroDivisionError" in out
    assert "would mark SHIPPED  cq-70d7b203f7ad" in out  # the loop continued


def test_the_honesty_rail_precondition_is_behavioural_not_a_grep(monkeypatch):
    """A tree where the screen exists but never WARNs must be BLOCKED."""
    mod = _load()
    from cora import slack_egress
    monkeypatch.setattr(slack_egress, "screen_capability_claims",
                        lambda text, **kw: text)   # silent stub: byte-identical, no log line
    blocker = mod._s1_present()
    assert blocker and "did not WARN" in blocker
