"""Step 7.5 reconcile for the 2026-09-08 ingest-integrity bundle: every transition
is gated on the fix being present on this tree; dry-run writes nothing; the
apply label follows the real outcome; a terminal row is never flipped."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "reconcile_ingest_integrity",
        _REPO_ROOT / "scripts" / "reconcile_code_queue_ingest_integrity_2026-09-08.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


EXPECTED_SHIPPED = {"cq-c89cfab00b1f", "cq-bc5e5b7512bd", "cq-e4b0d20a313f", "cq-a0da505f8e5f",
                    "cq-bd6eab1fcb44", "cq-3542e1b095b2", "cq-12fd5d76fd04"}


def test_ids_match_the_kickoff_and_every_precondition_passes_on_this_tree():
    mod = _load()
    assert set(mod.SHIPPED) == EXPECTED_SHIPPED
    assert mod.SUPERSEDED == {"cq-b80c5bc5be7a": ("cq-a0da505f8e5f", mod.SUPERSEDED["cq-b80c5bc5be7a"][1])}
    for cq_id, fn in mod.PRECONDITIONS.items():
        assert fn() is None, (cq_id, fn())
    assert mod.BUNDLE_ID == "ingest-integrity-2026-09" and mod.BRANCH.startswith("claude/ingest-integrity")


def _rec(status):
    return lambda cq_id: {"id": cq_id, "status": status}


def test_dry_run_reports_everything_and_writes_nothing(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("APPROVED"))
    monkeypatch.setattr(mod.code_queue, "process_queue_action",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run wrote!")))
    monkeypatch.setattr(mod.code_queue, "supersede_item",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run wrote!")))
    monkeypatch.setattr(mod.code_queue, "_append_event",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run wrote!")))
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    for cq in EXPECTED_SHIPPED:
        assert f"would mark SHIPPED  {cq}" in out
    assert "would mark SUPERSEDED  cq-b80c5bc5be7a  by cq-a0da505f8e5f" in out
    assert "BLOCKED" not in out


def test_a_missing_fix_blocks_only_its_seed(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("APPROVED"))
    monkeypatch.setitem(mod.PRECONDITIONS, "cq-3542e1b095b2", lambda: "I4 not on this tree")
    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "BLOCKED  cq-3542e1b095b2" in out and "I4 not on this tree" in out
    assert "would mark SHIPPED  cq-c89cfab00b1f" in out


def test_apply_labels_follow_real_outcomes_and_record_the_bundle(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("PROPOSED"))
    events = []
    monkeypatch.setattr(mod.code_queue, "_append_event", lambda ev: events.append(ev))
    monkeypatch.setattr(mod, "_head_commit", lambda: "abc1234")

    def fake_action(action_id, cq_id, actor_id):
        assert action_id == mod.code_queue.ACTION_MARK_SHIPPED and actor_id == mod.HARRISON_ID
        return ("shipped", "\U0001F6A2 Marked shipped.") if cq_id != "cq-12fd5d76fd04" else ("error", "gone")
    monkeypatch.setattr(mod.code_queue, "process_queue_action", fake_action)
    monkeypatch.setattr(mod.code_queue, "supersede_item", lambda loser, winner: True)
    assert mod.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert "SHIPPED  cq-c89cfab00b1f" in out
    assert "NOT SHIPPED  cq-12fd5d76fd04" in out and "error: gone" in out
    assert "SUPERSEDED  cq-b80c5bc5be7a  by cq-a0da505f8e5f" in out
    recs = [e for e in events if e.get("event") == "reconciled"]
    assert len(recs) == 6 + 1                                   # 6 shipped + 1 superseded; the failed one has no record
    assert all(e["bundle_id"] == mod.BUNDLE_ID and e["branch"] == mod.BRANCH and e["commit"] == "abc1234" for e in recs)
    assert {e["id"] for e in recs} == (EXPECTED_SHIPPED - {"cq-12fd5d76fd04"}) | {"cq-b80c5bc5be7a"}


def test_preconditions_can_actually_block(monkeypatch, tmp_path):
    """Lens F #6: every precondition must be able to return a blocker -- a tree
    lacking the code (or carrying a stub / dead branch) is BLOCKED, never marked."""
    mod = _load()
    # I5: the helper defined but the skip not wired into sweep_user
    monkeypatch.setattr(mod.inspect, "getsource", lambda fn: "def sweep_user():\n    return {}\n")
    assert mod._i5_present() and "not wired" in mod._i5_present()
    monkeypatch.undo()
    # I2: a route_capture that re-homes is caught behaviourally, not by grep
    import cora.session_capture as scap
    monkeypatch.setattr(scap, "route_capture", lambda ent, text: ("LEX", True, False))
    assert mod._i2_present() and "re-homes" in mod._i2_present()
    monkeypatch.undo()
    # path-based preconditions against a tree that lacks the code
    fake_root = tmp_path
    (fake_root / "src" / "cora").mkdir(parents=True)
    (fake_root / "scripts").mkdir()
    (fake_root / "src" / "cora" / "app.py").write_text(
        "def _dispatch_qa():\n    if False:\n        force_tool = \"cora_self_inventory\"\n", encoding="utf-8")
    (fake_root / "src" / "cora" / "context_loader.py").write_text("# no wiring\n", encoding="utf-8")
    (fake_root / "scripts" / "purge_dashboard_kb.py").write_text(
        "folders = sorted(KB_EXCLUDED_FOLDER_IDS)  # KB_DASHBOARD_FOLDER_IDS imported but unused\n", encoding="utf-8")
    (fake_root / "scripts" / "purge_cora_internal_kb.py").write_text("# no flags\n", encoding="utf-8")
    (fake_root / "scripts" / "mirror_claude_workspace.py").write_text("# old screen\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_REPO_ROOT", fake_root)
    assert "unreachable" in (mod._i4_present() or "")
    assert "wiring missing" in (mod._i1_present() or "")
    assert "walks" in (mod._dashboard_purge_scoped() or "")
    assert "flags missing" in (mod._i3_present() or "")
    assert "fold missing" in (mod._mirror_fold_present() or "")


def test_a_failed_provenance_record_is_reported_not_misfiled_as_a_failed_transition(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("PROPOSED"))
    monkeypatch.setattr(mod, "_head_commit", lambda: "abc1234")
    monkeypatch.setattr(mod.code_queue, "process_queue_action", lambda a, cq, actor: ("shipped", "ok"))
    monkeypatch.setattr(mod.code_queue, "supersede_item", lambda loser, winner: True)
    monkeypatch.setattr(mod.code_queue, "_append_event", lambda ev: (_ for _ in ()).throw(OSError("disk full")))
    rc = mod.main(["--apply"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.count("SHIPPED  cq-") == len(EXPECTED_SHIPPED)                 # the transitions DID happen ...
    assert out.count("[provenance record FAILED: OSError: disk full]") == len(EXPECTED_SHIPPED) + 1
    assert "NOT SHIPPED" not in out                                          # ... and are never mislabelled


def test_terminal_rows_are_never_flipped(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("DISMISSED"))
    monkeypatch.setattr(mod.code_queue, "process_queue_action",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write")))
    monkeypatch.setattr(mod.code_queue, "supersede_item",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not write")))
    assert mod.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert out.count("refusing to flip a terminal row") == len(EXPECTED_SHIPPED) + 1
    monkeypatch.setattr(mod.code_queue, "get_item", _rec("SHIPPED"))
    rc = mod.main(["--apply"])
    out = capsys.readouterr().out
    assert out.count("SKIP (already shipped)") == len(EXPECTED_SHIPPED)
    assert "NOT SUPERSEDED  cq-b80c5bc5be7a" in out and rc == 1     # a SHIPPED loser is terminal too
