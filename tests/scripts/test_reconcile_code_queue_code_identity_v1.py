"""Step 7.5 reconcile for the Code #13 RIDER 1 (bundle code-identity-v1): the one
SHIPPED transition is gated on the cora@ system-mailbox row + its consumer being
present; the two 9/11 follow-on rows are never touched; dry-run writes nothing;
--apply carries the C7 reference; the probe never touches the real ledger."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location(
            "reconcile_code_identity_v1",
            _REPO_ROOT / "scripts" / "reconcile_code_queue_code_identity_v1_2026-09-19.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


def _rec(status):
    return lambda cq_id: {"id": cq_id, "status": status, "severity": "MEDIUM"}


def _boom(*a, **k):
    raise AssertionError("dry-run wrote!")


def test_ids_match_the_identity_kickoff_and_the_precondition_passes_on_this_tree():
    mod = _load()
    assert set(mod.SHIPPED) == {"cq-8d16f1a557e5"}
    assert set(mod.NOT_TOUCHED) == {"cq-40baab26d7f3", "cq-c624f28c772a"}
    assert mod.BUNDLE_ID == "code-identity-v1" and mod.BRANCH == "claude/cora-identity-least-privilege"
    for cq_id, fn in mod.PRECONDITIONS.items():
        assert fn() is None, (cq_id, fn())


def test_dry_run_reports_and_writes_nothing(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("PROPOSED"))
    for name in ("process_queue_action", "dismiss_with_evidence", "supersede_item", "_append_event"):
        monkeypatch.setattr(mod.code_queue, name, _boom)
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "would mark SHIPPED  cq-8d16f1a557e5" in out
    assert "NOT TOUCHED  cq-40baab26d7f3" in out and "NOT TOUCHED  cq-c624f28c772a" in out
    assert "BLOCKED" not in out


def test_apply_carries_the_bundle_reference_and_never_names_the_rider_rows(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("PROPOSED"))
    monkeypatch.setattr(mod, "_head_commit", lambda: "abc1234")
    calls = []

    def fake(action_id, cq_id, actor_id, **kw):
        calls.append((cq_id, kw))
        return "shipped", "\U0001F6A2 Marked shipped."
    monkeypatch.setattr(mod.code_queue, "process_queue_action", fake)
    assert mod.main(["--apply"]) == 0
    assert calls == [("cq-8d16f1a557e5", {"bundle_id": "code-identity-v1", "branch": mod.BRANCH, "commit": "abc1234"})]
    assert "SHIPPED  cq-8d16f1a557e5" in capsys.readouterr().out


def test_terminal_row_is_never_flipped(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("DISMISSED"))
    monkeypatch.setattr(mod.code_queue, "process_queue_action", _boom)
    assert mod.main(["--apply"]) == 1
    assert "refusing to flip a terminal row" in capsys.readouterr().out


def test_a_missing_row_blocks_the_ship(capsys, monkeypatch):
    """The precondition is behavioural: a roster without the cora@ row BLOCKS."""
    mod = _load()
    monkeypatch.setitem(mod.PRECONDITIONS, "cq-8d16f1a557e5", lambda: "S-A: expected exactly one row, found 0")
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("PROPOSED"))
    monkeypatch.setattr(mod.code_queue, "process_queue_action", _boom)
    assert mod.main(["--apply"]) == 1
    assert "BLOCKED  cq-8d16f1a557e5" in capsys.readouterr().out


def test_probe_gate_never_touches_the_real_ledger(capsys):
    mod = _load()
    real = mod.code_queue._DEFAULT_EVENT_LEDGER
    before = real.read_text(encoding="utf-8") if real.exists() else None
    assert mod.main(["--probe-gate"]) == 0
    assert "PROBE OK" in capsys.readouterr().out
    after = real.read_text(encoding="utf-8") if real.exists() else None
    assert before == after


def test_env_pins_survive_loading_the_script(monkeypatch):
    monkeypatch.setenv("CORA_CODE_QUEUE", "off")
    _load()
    assert os.environ["CORA_CODE_QUEUE"] == "off"
