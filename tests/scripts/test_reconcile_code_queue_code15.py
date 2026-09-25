"""Step 7.5 reconcile for Code #15: every transition is gated on the fix being present
on this tree (behavioural checks that RUN the shipped code); dry-run writes nothing and
seeds nothing; every SHIPPED transition carries the C7 bundle reference AND its own
commits (never a HEAD stamp); RIDER B ships with its per-item record; terminal rows are
never flipped; the rider (b) seeds carry the Appendix-A LOW text verbatim; the probe
proves the gate on a throwaway ledger."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_SHIPPED = {"cq-d9d0c92cc797", "cq-22b84598aee8", "cq-90568f0b1222", "cq-74e6b20d5d3d",
                    "cq-5f44ce934aeb", "cq-2d26f131091e", "cq-3a29e7dc3953", "cq-59c5048d0891",
                    "cq-592baba613f1"}
NEVER_SHIPPED = {"cq-3b3130dd3a4c", "cq-11b694215709", "cq-24b88a65a5ae", "cq-17fe76f5ab91"}
RIDER_B = {"cq-59c5048d0891", "cq-592baba613f1"}


def _load():
    # the script's import-time load_dotenv(override=True) writes straight into
    # os.environ (monkeypatch cannot see it) and would un-pin the flags conftest set.
    saved = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location(
            "reconcile_code15",
            _REPO_ROOT / "scripts" / "reconcile_code_queue_code15_2026-09-24.py")
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
                 "ensure_kickoff_staged"):
        monkeypatch.setattr(mod.code_queue, name, _boom)


def _staged(cq_id):
    return {"id": cq_id, "status": "STAGED", "severity": "HIGH"}


def _preconditions_pass(mod, monkeypatch):
    # The preconditions themselves are RUN in the first test (the S5 probe writes to
    # its OWN throwaway ledger, which the write traps below would trip on). Here only
    # the transition + seed logic is under test.
    monkeypatch.setattr(mod, "PRECONDITIONS", {k: (lambda: None) for k in mod.PRECONDITIONS})


def test_ids_match_the_kickoff_and_every_precondition_passes_on_this_tree():
    mod = _load()
    assert set(mod.SHIPPED) == EXPECTED_SHIPPED
    assert set(mod.PRECONDITIONS) == EXPECTED_SHIPPED
    assert set(mod.COMMITS) == EXPECTED_SHIPPED
    assert set(mod.NOT_TOUCHED) == NEVER_SHIPPED and not (NEVER_SHIPPED & set(mod.SHIPPED))
    assert mod.BUNDLE_ID == "code-15" and mod.BRANCH == "claude/code-15-integrity-queue-ux-2026-09-21"
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


def test_rider_b_ships_with_its_per_item_record_never_bare():
    mod = _load()
    for cq_id in RIDER_B:
        ref = mod.BUNDLE_REFS[cq_id]
        assert ref.startswith("code-15 [") and ref.endswith("]"), ref
    assert "row1 HELD" in mod.BUNDLE_REFS["cq-59c5048d0891"]
    assert "Harrison" in mod.BUNDLE_REFS["cq-592baba613f1"]


def test_dry_run_reports_everything_and_writes_nothing(capsys, monkeypatch):
    mod = _load()
    _preconditions_pass(mod, monkeypatch)
    monkeypatch.setattr(mod.code_queue, "get_item", _staged)
    _no_writes(mod, monkeypatch)
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    for cq in EXPECTED_SHIPPED:
        assert f"would mark SHIPPED  {cq}" in out
    assert out.count("would seed") == len(mod.SEEDS)


def test_apply_passes_the_bundle_reference_and_the_items_own_commits(monkeypatch):
    mod = _load()
    _preconditions_pass(mod, monkeypatch)
    monkeypatch.setattr(mod.code_queue, "get_item", _staged)
    calls, seeds = [], []

    def _pqa(action, cq_id, actor, **kw):
        calls.append((action, cq_id, actor, kw))
        return "shipped", "ok"
    monkeypatch.setattr(mod.code_queue, "process_queue_action", _pqa)
    monkeypatch.setattr(mod.code_queue, "seed_item", lambda **kw: seeds.append(kw) or "cq-000000000000")
    assert mod.main(["--apply"]) == 0
    assert {c[1] for c in calls} == EXPECTED_SHIPPED
    for action, cq_id, actor, kw in calls:
        assert action == mod.code_queue.ACTION_MARK_SHIPPED and actor == mod.HARRISON_ID
        assert kw["branch"] == mod.BRANCH and kw["commit"] == mod.COMMITS[cq_id]
        assert kw["bundle_id"] == mod.BUNDLE_REFS.get(cq_id, "code-15")
    assert len(seeds) == len(mod.SEEDS)
    assert all(s["status"] == "PROPOSED" for s in seeds)


def test_terminal_rows_are_never_flipped(capsys, monkeypatch):
    mod = _load()
    _preconditions_pass(mod, monkeypatch)
    monkeypatch.setattr(mod.code_queue, "get_item",
                        lambda cq: {"id": cq, "status": "DISMISSED"})
    _no_writes(mod, monkeypatch)
    monkeypatch.setattr(mod.code_queue, "seed_item", lambda **kw: "cq-000000000000")
    assert mod.main(["--apply"]) == 1
    assert "refusing to flip a terminal row" in capsys.readouterr().out


def test_rider_b_seeds_carry_the_appendix_a_text_verbatim():
    mod = _load()
    rider = [s for s in mod.SEEDS if s[3].startswith(mod._RB)]
    assert len(rider) == 9
    ids = {s[2].split(":", 1)[0] for s in rider}
    assert ids == {"C13-04", "C13-07", "C13-11", "C13-12", "C13-17", "C13-19", "C13-20", "R1-04", "R1-05"}
    for kind, sev, title, summary in rider:
        assert sev == "LOW" and kind in ("bug", "feature", "config")
        # the verbatim LOW text opens with its lens tag + file:line
        assert summary[len(mod._RB):].startswith("["), title


def test_a_missing_fix_blocks_its_transition(monkeypatch):
    mod = _load()
    from cora import secret_tokens
    monkeypatch.setattr(secret_tokens, "redact_secret_tokens", lambda t: (t or "", 0))
    assert mod._s1_present() is not None


def test_the_probe_proves_the_c7_gate_on_a_throwaway_ledger(capsys):
    mod = _load()
    before = mod.code_queue._EVENT_LEDGER
    assert mod.main(["--probe-gate"]) == 0
    assert "PROBE OK" in capsys.readouterr().out
    assert mod.code_queue._EVENT_LEDGER == before
