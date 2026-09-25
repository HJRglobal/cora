"""Step 7.5 reconcile for Code #16: every transition is gated on the fix being present
on this tree (behavioural checks that RUN the shipped code); dry-run (default and the
explicit --dry-run alias) writes nothing and seeds nothing; every SHIPPED transition
carries the C7 bundle reference AND its own commits; the two browser mechanisms are
superseded by the C1 API lane only AFTER it ships; the dismissed LEX row is never
touched; terminal rows are never flipped; the probe proves the gate on a throwaway
ledger."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_SHIPPED = {"cq-be90cea867c3", "cq-e9ef3f581d60"}
EXPECTED_SUPERSEDED = {"cq-4f3fcc9b7096", "cq-062291f79968"}
C1 = "cq-be90cea867c3"
NEVER_TOUCHED = {"cq-b93e2abf2922"}


def _load():
    # the script's import-time load_dotenv(override=True) writes straight into
    # os.environ (monkeypatch cannot see it) and would un-pin the flags conftest set.
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location(
            "reconcile_code16",
            _REPO_ROOT / "scripts" / "reconcile_code_queue_code16_2026-09-25.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return mod


def _boom(*a, **k):
    raise AssertionError("dry-run wrote!")


def _no_writes(mod, monkeypatch):
    for name in ("process_queue_action", "seed_item", "_append_event", "dismiss_with_evidence",
                 "ensure_kickoff_staged", "supersede_item", "park_item"):
        monkeypatch.setattr(mod.code_queue, name, _boom)


def _staged(cq_id):
    return {"id": cq_id, "status": "STAGED", "severity": "P2"}


def _preconditions_pass(mod, monkeypatch):
    monkeypatch.setattr(mod, "PRECONDITIONS", {k: (lambda: None) for k in mod.PRECONDITIONS})


def test_ids_match_the_kickoff_and_every_precondition_passes_on_this_tree():
    mod = _load()
    assert set(mod.SHIPPED) == EXPECTED_SHIPPED
    assert set(mod.SUPERSEDED) == EXPECTED_SUPERSEDED
    assert all(w == C1 for w, _label in mod.SUPERSEDED.values())
    assert set(mod.PRECONDITIONS) == EXPECTED_SHIPPED | EXPECTED_SUPERSEDED
    assert set(mod.COMMITS) == EXPECTED_SHIPPED
    assert set(mod.NOT_TOUCHED) == NEVER_TOUCHED
    assert not (NEVER_TOUCHED & (set(mod.SHIPPED) | set(mod.SUPERSEDED)))
    assert mod.BUNDLE_ID == "code-16" and mod.BRANCH == "claude/code-16-new-capability-lanes-2026-09-21"
    from cora import code_queue
    assert mod.HARRISON_ID == code_queue.HARRISON_ID
    for cq_id, fn in mod.PRECONDITIONS.items():
        assert fn() is None, (cq_id, fn())


def test_every_item_names_its_own_commits_and_they_exist_in_this_repo():
    mod = _load()
    for cq_id, commits in mod.COMMITS.items():
        shas = [c for c in commits.split(",") if c]
        assert shas, cq_id
        for sha in shas:
            rc = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=str(_REPO_ROOT),
                                capture_output=True, timeout=20).returncode
            if rc != 0:
                pytest.skip(f"{sha} not in this clone (a shallow/DR checkout)")


@pytest.mark.parametrize("argv", [[], ["--dry-run"]])
def test_dry_run_reports_everything_and_writes_nothing(capsys, monkeypatch, argv):
    mod = _load()
    _preconditions_pass(mod, monkeypatch)
    monkeypatch.setattr(mod.code_queue, "get_item", _staged)
    _no_writes(mod, monkeypatch)
    assert mod.main(argv) == 0
    out = capsys.readouterr().out
    for cq in EXPECTED_SHIPPED:
        assert f"would mark SHIPPED  {cq}" in out
    for cq in EXPECTED_SUPERSEDED:
        assert f"would mark SUPERSEDED  {cq}  by {C1}" in out
    assert "NOT TOUCHED  cq-b93e2abf2922" in out
    assert out.count("would seed") == len(mod.SEEDS)


def test_apply_and_dry_run_together_are_refused(monkeypatch):
    mod = _load()
    _no_writes(mod, monkeypatch)
    assert mod.main(["--apply", "--dry-run"]) == 2


def test_apply_ships_with_the_bundle_and_commits_then_supersedes_with_provenance(monkeypatch):
    mod = _load()
    _preconditions_pass(mod, monkeypatch)
    monkeypatch.setattr(mod.code_queue, "get_item", _staged)
    order, calls, seeds, prov = [], [], [], []

    def _pqa(action, cq_id, actor, **kw):
        order.append(("ship", cq_id))
        calls.append((action, cq_id, actor, kw))
        return "shipped", "ok"

    def _sup(loser, winner):
        order.append(("supersede", loser))
        return True
    monkeypatch.setattr(mod.code_queue, "process_queue_action", _pqa)
    monkeypatch.setattr(mod.code_queue, "supersede_item", _sup)
    monkeypatch.setattr(mod.code_queue, "_append_event", lambda ev: prov.append(ev))
    monkeypatch.setattr(mod.code_queue, "seed_item", lambda **kw: seeds.append(kw) or "cq-000000000000")
    assert mod.main(["--apply"]) == 0
    assert {c[1] for c in calls} == EXPECTED_SHIPPED
    for action, cq_id, actor, kw in calls:
        assert action == mod.code_queue.ACTION_MARK_SHIPPED and actor == mod.HARRISON_ID
        assert kw["branch"] == mod.BRANCH and kw["commit"] == mod.COMMITS[cq_id]
        assert kw["bundle_id"] == "code-16"
    # every supersede comes AFTER every ship (the winner is final first)
    last_ship = max(i for i, (k, _c) in enumerate(order) if k == "ship")
    first_sup = min(i for i, (k, _c) in enumerate(order) if k == "supersede")
    assert last_ship < first_sup
    assert {p["id"] for p in prov} == EXPECTED_SUPERSEDED
    assert all(p["event"] == "reconciled" and p["bundle_id"] == "code-16"
               and p["transition"] == f"SUPERSEDED by {C1}" for p in prov)
    assert len(seeds) == len(mod.SEEDS)
    assert all(s["status"] == "PROPOSED" and s["kind"] in mod.code_queue.VALID_KINDS for s in seeds)


def test_supersede_waits_for_the_winner_to_ship(capsys, monkeypatch):
    """If C1 does not ship this run (blocked / refused), the browser rows stay put."""
    mod = _load()
    monkeypatch.setattr(mod, "PRECONDITIONS", {
        **{k: (lambda: None) for k in mod.PRECONDITIONS}, C1: lambda: "C1 fix missing"})
    monkeypatch.setattr(mod.code_queue, "get_item", _staged)
    monkeypatch.setattr(mod.code_queue, "process_queue_action", lambda *a, **k: ("shipped", "ok"))
    monkeypatch.setattr(mod.code_queue, "supersede_item", _boom)
    monkeypatch.setattr(mod.code_queue, "_append_event", _boom)
    monkeypatch.setattr(mod.code_queue, "seed_item", lambda **kw: "cq-000000000000")
    assert mod.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert f"BLOCKED  {C1}" in out
    assert out.count("BLOCKED") + out.count("NOT SUPERSEDED") >= 3


def test_terminal_rows_are_never_flipped(capsys, monkeypatch):
    mod = _load()
    _preconditions_pass(mod, monkeypatch)
    monkeypatch.setattr(mod.code_queue, "get_item", lambda cq: {"id": cq, "status": "DISMISSED"})
    _no_writes(mod, monkeypatch)
    monkeypatch.setattr(mod.code_queue, "seed_item", lambda **kw: "cq-000000000000")
    assert mod.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert "refusing to flip a terminal row" in out


def test_the_c1_precondition_catches_an_archive_call_outside_the_tap_handler(monkeypatch, tmp_path):
    mod = _load()
    rogue = tmp_path / "cora"
    (rogue / "channel_archive").mkdir(parents=True)
    real = mod._SRC
    for p in (real / "channel_archive").glob("*.py"):
        (rogue / "channel_archive" / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    (rogue / "rogue.py").write_text("def f(c):\n    c.conversations_archive(channel='C1')\n",
                                    encoding="utf-8")
    monkeypatch.setattr(mod, "_SRC", rogue)
    blocker = mod._c1_present()
    assert blocker and "outside the tap handler" in blocker


def test_the_c2_precondition_catches_a_leaking_request_builder(monkeypatch):
    mod = _load()
    from cora import travel_shortlist as tsl
    real = tsl.build_request

    def leaky(c, **kw):
        req = real(c, **kw)
        req["messages"][0]["content"] += " Guest: Jordan Riverstone."
        return req
    monkeypatch.setattr(tsl, "build_request", leaky)
    blocker = mod._c2_present()
    assert blocker and "identity tokens" in blocker


def test_the_probe_proves_the_c7_gate_on_a_throwaway_ledger(capsys):
    mod = _load()
    before = mod.code_queue._EVENT_LEDGER
    assert mod.main(["--probe-gate"]) == 0
    assert "PROBE OK" in capsys.readouterr().out
    assert mod.code_queue._EVENT_LEDGER == before
