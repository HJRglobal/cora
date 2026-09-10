"""Step 7.5 reconcile for Code #12: every transition is gated on the fix being
present on this tree; dry-run writes nothing; the apply labels follow the real
outcomes; every SHIPPED transition passes the C7 bundle reference (this script is
the first run under the gate); the probe proves the gate on a throwaway ledger."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "reconcile_code12",
        _REPO_ROOT / "scripts" / "reconcile_code_queue_code12_2026-09-09.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


EXPECTED_SHIPPED = {"cq-554184feb53b", "cq-60024f032136", "cq-deca62a00719", "cq-b6f2f4825ffb",
                    "cq-8b51427896b8", "cq-f23d6885dd1c", "cq-28de84159c3f", "cq-a70bbbfd2d13",
                    "cq-a1aaee9f46e0", "cq-6fef5505fb3e"}
EXPECTED_DISMISSED = {"cq-651e6783994f", "cq-fea9b2676e2f", "cq-dbcca949bb92", "cq-3e7a465040f1"}


def test_ids_match_the_kickoff_and_every_precondition_passes_on_this_tree():
    mod = _load()
    assert set(mod.SHIPPED) == EXPECTED_SHIPPED
    assert set(mod.DISMISSED) == EXPECTED_DISMISSED
    assert mod.SUPERSEDED == {"cq-0f8abd3c1981": ("cq-deca62a00719", mod.SUPERSEDED["cq-0f8abd3c1981"][1])}
    assert mod.REGRADED == {"cq-a296aa8e0a2e": "HIGH"}
    assert set(mod.LEFT_OPEN) == {"cq-41540856f95b"}
    for cq_id, fn in mod.PRECONDITIONS.items():
        assert fn() is None, (cq_id, fn())
    assert mod.BUNDLE_ID == "code-12" and mod.BRANCH == "claude/code-12-queue-metabolism-2026-09-09"


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
    for cq in EXPECTED_DISMISSED:
        assert f"would DISMISS with evidence  {cq}" in out
    assert "would mark SUPERSEDED  cq-0f8abd3c1981  by cq-deca62a00719" in out
    assert "would re-grade  cq-a296aa8e0a2e  MEDIUM -> HIGH" in out
    assert "LEFT OPEN  cq-41540856f95b" in out
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
        return ("shipped", "\U0001F6A2 Marked shipped.") if cq_id != "cq-6fef5505fb3e" else ("refused", "no bundle")
    monkeypatch.setattr(mod.code_queue, "process_queue_action", fake_action)
    monkeypatch.setattr(mod.code_queue, "dismiss_with_evidence",
                        lambda cq, actor, note, **kw: ("dismissed", f"dismissed: {note[:20]}"))
    monkeypatch.setattr(mod.code_queue, "supersede_item", lambda loser, winner: True)
    monkeypatch.setattr(mod.code_queue, "set_severity", lambda cq, actor, sev: ("edited", f"Severity set to {sev}."))
    events = []
    monkeypatch.setattr(mod.code_queue, "_append_event", lambda ev: events.append(ev))
    assert mod.main(["--apply"]) == 1  # the one refused ship makes it non-zero
    out = capsys.readouterr().out
    assert "SHIPPED  cq-554184feb53b" in out
    assert "NOT SHIPPED  cq-6fef5505fb3e" in out and "refused: no bundle" in out
    # every ship call carried the C7 reference
    assert ship_calls and all(kw == {"bundle_id": "code-12", "branch": mod.BRANCH, "commit": "abc1234"}
                              for _cq, kw in ship_calls)
    for cq in EXPECTED_DISMISSED:
        assert f"DISMISSED  {cq}" in out
    assert "SUPERSEDED  cq-0f8abd3c1981  by cq-deca62a00719" in out
    assert "RE-GRADED  cq-a296aa8e0a2e  LOW -> HIGH" in out
    recs = [e for e in events if e.get("event") == "reconciled"]
    assert {e["id"] for e in recs} == {"cq-0f8abd3c1981", "cq-a296aa8e0a2e"}
    assert all(e["bundle_id"] == "code-12" and e["commit"] == "abc1234" for e in recs)


def test_terminal_rows_are_never_flipped(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("SUPERSEDED"))
    monkeypatch.setattr(mod.code_queue, "load_items", lambda: [])
    for name in ("process_queue_action", "dismiss_with_evidence", "supersede_item", "set_severity"):
        monkeypatch.setattr(mod.code_queue, name, _boom)
    assert mod.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert "refusing to flip a terminal row" in out


def test_shared_prompt_sharers_are_reported_not_struck(capsys, monkeypatch):
    mod = _load()
    shared = "G:/notes/2026-07-28_fndr_cora-code-prompt-lex-ar-audit-bundle.md"
    rows = {"cq-3e7a465040f1": {"id": "cq-3e7a465040f1", "status": "STAGED", "prompt_path": shared},
            "cq-9b368e8d43a7": {"id": "cq-9b368e8d43a7", "status": "STAGED", "prompt_path": shared},
            "cq-ac9bb3868f02": {"id": "cq-ac9bb3868f02", "status": "STAGED", "prompt_path": shared}}
    monkeypatch.setattr(mod.code_queue, "get_item",
                        lambda cq: rows.get(cq, {"id": cq, "status": "PROPOSED", "severity": "LOW"}))
    monkeypatch.setattr(mod.code_queue, "load_items", lambda: list(rows.values()))
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "prompt file shared with cq-9b368e8d43a7, cq-ac9bb3868f02 -- NOT struck" in out


def test_probe_gate_refuses_without_bundle_and_never_touches_the_real_ledger(capsys, monkeypatch):
    mod = _load()
    real = mod.code_queue._EVENT_LEDGER
    before = real.read_text(encoding="utf-8") if real.exists() else None
    assert mod.main(["--probe-gate"]) == 0
    out = capsys.readouterr().out
    assert "PROBE OK" in out and "refused" in out
    after = real.read_text(encoding="utf-8") if real.exists() else None
    assert before == after
    assert mod.code_queue._EVENT_LEDGER == real  # restored
