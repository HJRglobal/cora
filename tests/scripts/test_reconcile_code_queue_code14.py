"""Step 7.5 reconcile for Code #14: every transition is gated on the fix being
present on this tree (behavioural checks); dry-run writes nothing; every SHIPPED
transition passes the C7 bundle reference; terminal rows are never flipped; the
probe proves the gate on a throwaway ledger; the LEX read-back never prints the
kickoff path; an APPROVED read-back prints the bare verb line for Harrison and never
stages it."""
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
            "reconcile_code14",
            _REPO_ROOT / "scripts" / "reconcile_code_queue_code14_2026-09-23.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


EXPECTED_SHIPPED = {"cq-0f04ad6543a8", "cq-439d89a84de4", "cq-c7dbaef87633", "cq-a5b3e6a2e844",
                    "cq-17fe76f5ab91", "cq-2f6a6cee0169", "cq-323c8974fa02", "cq-85b35413b020"}
READBACK = "cq-a24f9d2210fc"
FAKE_PROMPT_PATH = r"G:\My Drive\SECRET-LEX-FOLDER\kickoff-probe.md"


def _rec(status, severity="MEDIUM"):
    return lambda cq_id: {"id": cq_id, "status": status, "severity": severity}


def _fold(status, prompt_path=FAKE_PROMPT_PATH):
    return lambda: {READBACK: {"id": READBACK, "status": status, "entity": "LEX",
                               "prompt_path": prompt_path}}


def _boom(*a, **k):
    raise AssertionError("dry-run wrote!")


def _no_writes(mod, monkeypatch):
    for name in ("process_queue_action", "dismiss_with_evidence", "supersede_item",
                 "set_severity", "_append_event", "ensure_kickoff_staged"):
        monkeypatch.setattr(mod.code_queue, name, _boom)


def _exists(value):
    from cora import drive_io
    return lambda path, **kw: value


def test_ids_match_the_kickoff_and_every_precondition_passes_on_this_tree():
    mod = _load()
    assert set(mod.SHIPPED) == EXPECTED_SHIPPED
    assert set(mod.PRECONDITIONS) == EXPECTED_SHIPPED
    # S1 rode Code #13; S4 was DISMISSED -- neither is ever transitioned here
    assert "cq-70d7b203f7ad" not in mod.SHIPPED and "cq-70d7b203f7ad" in mod.ALREADY_SHIPPED
    assert "cq-1a8611487e26" not in mod.SHIPPED and "cq-1a8611487e26" in mod.DISMISSED_NOT_TOUCHED
    assert READBACK not in mod.SHIPPED and mod.READBACK_ID == READBACK
    for cq_id, fn in mod.PRECONDITIONS.items():
        assert fn() is None, (cq_id, fn())
    assert mod.BUNDLE_ID == "code-14" and mod.BRANCH == "claude/code-14-bug-bundle"
    from cora import code_queue
    assert mod.HARRISON_ID == code_queue.HARRISON_ID


def test_dry_run_reports_everything_and_writes_nothing(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item",
                        lambda cq: {"id": cq, "status": "SHIPPED" if cq == "cq-70d7b203f7ad" else "APPROVED",
                                    "bundle_id": "code-13"})
    monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("STAGED"))
    from cora import drive_io
    monkeypatch.setattr(drive_io, "exists", _exists(True))
    _no_writes(mod, monkeypatch)
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    for cq in EXPECTED_SHIPPED:
        assert f"would mark SHIPPED  {cq}" in out
    assert "ALREADY SHIPPED  cq-70d7b203f7ad" in out
    assert "NOT TOUCHED  cq-1a8611487e26" in out
    assert f"READ-BACK  {READBACK}  (STAGED) -> kickoff on disk (path withheld, LEX row)" in out
    assert "SECRET-LEX-FOLDER" not in out and "kickoff-probe" not in out
    assert "BLOCKED" not in out


def test_apply_passes_the_bundle_reference_and_labels_follow_outcomes(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item",
                        lambda cq: {"id": cq, "status": "SHIPPED" if cq == "cq-70d7b203f7ad" else "PROPOSED"})
    monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("STAGED"))
    from cora import drive_io
    monkeypatch.setattr(drive_io, "exists", _exists(True))
    monkeypatch.setattr(mod, "_head_commit", lambda: "abc1234")
    ship_calls = []

    def fake_action(action_id, cq_id, actor_id, **kw):
        ship_calls.append((cq_id, kw))
        assert action_id == mod.code_queue.ACTION_MARK_SHIPPED and actor_id == mod.HARRISON_ID
        return ("shipped", "\U0001F6A2 Marked shipped.") if cq_id != "cq-2f6a6cee0169" else ("refused", "no bundle")
    monkeypatch.setattr(mod.code_queue, "process_queue_action", fake_action)
    monkeypatch.setattr(mod.code_queue, "_append_event", _boom)
    monkeypatch.setattr(mod.code_queue, "ensure_kickoff_staged", _boom)
    assert mod.main(["--apply"]) == 1  # the one refused ship makes it non-zero
    out = capsys.readouterr().out
    assert "SHIPPED  cq-0f04ad6543a8" in out and "SHIPPED  cq-85b35413b020" in out
    assert "NOT SHIPPED  cq-2f6a6cee0169" in out and "refused: no bundle" in out
    assert ship_calls and all(kw == {"bundle_id": "code-14", "branch": mod.BRANCH, "commit": "abc1234"}
                              for _cq, kw in ship_calls)
    assert {cq for cq, _ in ship_calls} == EXPECTED_SHIPPED
    # neither printed-only row was ever passed to the writer
    assert not {"cq-70d7b203f7ad", "cq-1a8611487e26", READBACK} & {cq for cq, _ in ship_calls}


def test_terminal_rows_are_never_flipped(capsys, monkeypatch):
    mod = _load()
    for terminal in ("DISMISSED", "SUPERSEDED"):
        monkeypatch.setattr(mod.code_queue, "get_item", _rec(terminal))
        monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("STAGED"))
        from cora import drive_io
        monkeypatch.setattr(drive_io, "exists", _exists(True))
        _no_writes(mod, monkeypatch)
        assert mod.main(["--apply"]) == 1
        out = capsys.readouterr().out
        assert out.count("refusing to flip a terminal row") == len(EXPECTED_SHIPPED)


def test_an_approved_readback_prints_the_bare_verb_and_never_stages(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item",
                        lambda cq: {"id": cq, "status": "SHIPPED"})
    monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("APPROVED", prompt_path=""))
    _no_writes(mod, monkeypatch)
    assert mod.main(["--apply"]) == 0
    out = capsys.readouterr().out
    # the bare line, on its own line, for Harrison to paste
    assert f"\nstage {READBACK}\n" in out


def test_a_missing_or_unknowable_kickoff_is_a_nonzero_readback(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", lambda cq: {"id": cq, "status": "SHIPPED"})
    monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("STAGED"))
    from cora import drive_io
    monkeypatch.setattr(drive_io, "exists", _exists(False))
    _no_writes(mod, monkeypatch)
    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "kickoff file MISSING (path withheld, LEX row)" in out and "SECRET-LEX-FOLDER" not in out

    def _gone(path, **kw):
        raise drive_io.DriveUnavailable("mount gone")
    monkeypatch.setattr(drive_io, "exists", _gone)
    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "kickoff existence UNKNOWN" in out and "SECRET-LEX-FOLDER" not in out


def test_s1_not_yet_shipped_by_code13_is_loud(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("PROPOSED"))
    monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("STAGED"))
    from cora import drive_io
    monkeypatch.setattr(drive_io, "exists", _exists(True))
    _no_writes(mod, monkeypatch)
    assert mod.main([]) == 1
    assert "S1 comes back FIRST" in capsys.readouterr().out


def test_a_mismatched_harrison_id_refuses_before_any_transition(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "HARRISON_ID", "U0SOMEONEELSE")
    _no_writes(mod, monkeypatch)
    assert mod.main(["--apply"]) == 1
    assert "REFUSED" in capsys.readouterr().out


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
    monkeypatch.setattr(mod.code_queue, "_fold_items", _fold("STAGED"))
    from cora import drive_io
    monkeypatch.setattr(drive_io, "exists", _exists(True))
    monkeypatch.setitem(mod.PRECONDITIONS, "cq-0f04ad6543a8", lambda: 1 / 0)
    _no_writes(mod, monkeypatch)
    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "BLOCKED  cq-0f04ad6543a8" in out and "precondition raised ZeroDivisionError" in out
    assert "would mark SHIPPED  cq-439d89a84de4" in out  # the loop continued


def test_the_s3_precondition_never_writes_the_real_rail_ledger_and_restores_state():
    mod = _load()
    from cora import slack_egress as se
    armed, path = se._RAIL_LEDGER_ARMED, se.PHANTOM_CLAIMS_LEDGER
    before = Path(path).read_bytes() if Path(path).exists() else None
    assert mod._s3_present() is None
    assert (se._RAIL_LEDGER_ARMED, se.PHANTOM_CLAIMS_LEDGER) == (armed, path)
    assert (Path(path).read_bytes() if Path(path).exists() else None) == before


def test_the_honesty_rail_precondition_is_behavioural_not_a_grep(monkeypatch):
    """A tree where the capability screen exists but never WARNs must be BLOCKED."""
    mod = _load()
    from cora import slack_egress
    monkeypatch.setattr(slack_egress, "screen_capability_claims", lambda text, **kw: text)
    blocker = mod._r149_present()
    assert blocker and "did not WARN" in blocker


def test_the_s7_precondition_blocks_a_question_mark_badge(monkeypatch):
    mod = _load()
    from cora import knowledge_review as kr
    monkeypatch.setattr(kr, "format_mechanical_dm", lambda u: "*[Asana task]* `?`\nClose a probe task")
    blocker = mod._s7_present()
    assert blocker and "'?'" in blocker
