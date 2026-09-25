"""Code #15 rider (c): the D-243 redirects added to tests/conftest.py FIRE.

The 2026-09-24 worktree ROOT differential (touch a marker, run the writer
tests, list logs/ data/ design/ newer than it) showed three unredirected
repo-relative writers, each with measured LIVE damage in the primary:

  * scripts/run_asana_hygiene_nudges.py  DEFERRED_FILE
      -> data/state/hygiene-deferred.jsonl (22,000 of 22,117 live rows fixture)
  * src/cora/tools/financial_client.py   _audit_log_path() / _throttle_path()
      -> logs/cora-finance-queries.jsonl (2,448 of 2,505 live rows fixture)
  * scripts/run_info_for_cora_sweep.py   _RUNSTATE_PATH
      -> data/state/info-for-cora-runstate.json (a test stamp makes the health
         check read a dead sweep as alive for 48h)

A redirect that silently does not fire is worse than none (it certifies the
pollution -- the #11 S2 lesson), so each one is proven here BEHAVIOURALLY: the
real writer is called and its output must land under this test's tmp_path.
The script modules are imported at module top on purpose -- that is how every
existing importer does it, and it is what puts them in sys.modules before the
autouse fixture runs (the fixture never imports a script itself: that would run
the script's import-time load_dotenv on every test).

All fixture values are synthetic; nothing here reads or writes a live file.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO))

import run_asana_hygiene_nudges as nudges  # noqa: E402
import run_info_for_cora_sweep as sweep  # noqa: E402
import scripts.run_info_for_cora_sweep as sweep_pkg  # noqa: E402  (the second spelling)
from cora.tools import financial_client as fcl  # noqa: E402

_CONFTEST = (_REPO / "tests" / "conftest.py").read_text(encoding="utf-8")


def _under(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def test_hygiene_deferred_ledger_write_lands_in_tmp(tmp_path):
    assert nudges.DEFERRED_FILE == tmp_path / "hygiene-deferred.jsonl"
    assert nudges.THROTTLE_FILE == tmp_path / "hygiene_nudge_throttle.json"
    nudges._record_deferred("111", "synthetic task", "U_SYNTH", 1, "cap", "run-x")
    rows = (tmp_path / "hygiene-deferred.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and json.loads(rows[0])["task_gid"] == "111"


@pytest.mark.parametrize("mod", [sweep, sweep_pkg], ids=["run_info_for_cora_sweep",
                                                         "scripts.run_info_for_cora_sweep"])
def test_info_for_cora_runstate_write_lands_in_tmp_under_both_spellings(tmp_path, mod):
    assert mod._RUNSTATE_PATH == tmp_path / "info-for-cora-runstate.json"
    assert mod._WATERMARK_PATH == tmp_path / "info-for-cora-watermark.json"
    assert mod._LOCK_PATH == tmp_path / "info_for_cora_sweep.lock"
    mod._write_runstate("complete", "synthetic")
    state = json.loads((tmp_path / "info-for-cora-runstate.json").read_text(encoding="utf-8"))
    assert state["outcome"] == "complete"


def test_the_two_sweep_spellings_are_distinct_module_objects():
    # why conftest lists BOTH names: patching one leaves the other writing live
    assert sweep is not sweep_pkg


def test_finance_audit_and_throttle_writes_land_in_tmp(tmp_path):
    assert _under(fcl._audit_log_path(), tmp_path)
    assert _under(fcl._throttle_path(), tmp_path)
    fcl._audit(channel="synthetic", user="U_SYNTH", query_summary="q", result_type="r")
    audit = fcl._audit_log_path()
    assert json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])["user"] == "U_SYNTH"
    assert not _under(audit, _REPO)


def test_every_redirected_path_is_also_session_guarded():
    # the belt behind a future LAZY importer: a write the redirect misses is
    # caught by _guard_logs_untouched instead of silently landing live
    for rel in ("data/state/hygiene-deferred.jsonl", "data/state/hygiene_nudge_throttle.json",
                "logs/cora-finance-queries.jsonl", "data/cache/finance-notify-throttle.json",
                "data/state/info-for-cora-runstate.json", "data/state/info-for-cora-watermark.json"):
        assert f'"{rel}",' in _CONFTEST, rel
    assert _CONFTEST.count("# Code #15 rider (c):") == 2   # the fixture block + the guard rows
