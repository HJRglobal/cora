"""Approval recon (cq-e6ab72d91735, Ops session O5).

The report is a DECISION INPUT to the multi-approver question, so the tests
concentrate on the two ways it could mislead that decision: mislabelling what a
resolution WAS, and crediting a second approver with work they may not take.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def recon(tmp_path, monkeypatch):
    path = _REPO_ROOT / "scripts" / "run_approval_recon.py"
    spec = importlib.util.spec_from_file_location("approval_recon", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["approval_recon"] = mod
    spec.loader.exec_module(mod)
    for key in list(mod.LEDGERS):
        mod.LEDGERS[key] = tmp_path / f"{key}.jsonl"
        mod.LEDGERS[key].touch()
    return mod


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _lane(result, name):
    return next(l for l in result["lanes"] if l["lane"] == name)


class TestResolutionClassification:
    """The defect that would invert the recommendation."""

    def test_expiry_is_not_counted_as_a_denial(self, recon):
        """Live 30-day read: of 101 `DISMISSED` knowledge rows only 12 came from
        a human tap -- 76 EXPIRED undecided and 24 were routed to an owner.
        Counting all of them as denials reports a decisive, well-served queue
        and hides the actual failure, which is that most items age out
        unanswered.
        """
        _write(recon.LEDGERS["knowledge"], [
            {"proposed_at": _iso(50), "resolved_at": _iso(2), "state": "DISMISSED",
             "resolved_reason": "expired_unrouted"},
            {"proposed_at": _iso(50), "resolved_at": _iso(2), "state": "DISMISSED",
             "resolved_reason": "auto_expired_dmd_unreacted"},
            {"proposed_at": _iso(50), "resolved_at": _iso(2), "state": "DISMISSED",
             "resolved_reason": "one_tap_button"},
            {"proposed_at": _iso(50), "resolved_at": _iso(2), "state": "DISMISSED",
             "resolved_reason": "routed_to_owner:U0B2RM2JYJ1"},
            {"proposed_at": _iso(50), "resolved_at": _iso(2), "state": "APPROVED",
             "resolved_reason": "one_tap_button"},
        ])
        lane = _lane(recon.analyze(now=NOW, days=30), "knowledge")
        assert lane["expired"] == 2
        assert lane["routed"] == 1
        assert lane["denied"] == 1
        assert lane["approved"] == 1
        assert lane["decided_by_a_human"] == 2

    def test_pending_items_are_open_not_resolved(self, recon):
        _write(recon.LEDGERS["knowledge"],
               [{"proposed_at": _iso(100), "state": "PENDING"}])
        lane = _lane(recon.analyze(now=NOW, days=30), "knowledge")
        assert lane["open"] == 1
        assert lane["decided_by_a_human"] == 0
        assert lane["oldest_open_h"] == pytest.approx(100, abs=0.1)

    def test_items_outside_the_window_are_excluded(self, recon):
        _write(recon.LEDGERS["knowledge"], [
            {"proposed_at": _iso(24 * 60), "state": "APPROVED", "resolved_at": _iso(24 * 59)},
            {"proposed_at": _iso(10), "state": "APPROVED", "resolved_at": _iso(9)},
        ])
        assert _lane(recon.analyze(now=NOW, days=30), "knowledge")["proposed"] == 1

    def test_a_torn_ledger_line_does_not_abort_the_recon(self, recon):
        recon.LEDGERS["knowledge"].write_text(
            json.dumps({"proposed_at": _iso(5), "state": "PENDING"}) + "\n{bad json\n",
            encoding="utf-8")
        assert _lane(recon.analyze(now=NOW, days=30), "knowledge")["proposed"] == 1


class TestDelegatedWorkLane:
    def test_held_then_released_is_an_approval_with_a_wait(self, recon):
        _write(recon.LEDGERS["delegated_work"], [
            {"event": "held", "ts": _iso(10), "job_id": "dw-1", "reason": "user_quota"},
            {"event": "released", "ts": _iso(4), "job_id": "dw-1"},
        ])
        lane = _lane(recon.analyze(now=NOW, days=30), "delegated_work")
        assert (lane["approved"], lane["denied"]) == (1, 0)
        assert lane["median_wait_h"] == pytest.approx(6, abs=0.1)

    def test_harrison_dismiss_is_a_denial(self, recon):
        _write(recon.LEDGERS["delegated_work"], [
            {"event": "held", "ts": _iso(10), "job_id": "dw-1"},
            {"event": "cancelled", "ts": _iso(4), "job_id": "dw-1",
             "reason": "harrison_dismiss"},
        ])
        assert _lane(recon.analyze(now=NOW, days=30), "delegated_work")["denied"] == 1

    def test_a_requester_cancel_is_withdrawn_not_denied_and_not_open(self, recon):
        """The requester withdrawing is not the approver deciding -- AND it is
        not still waiting for one either.

        This test previously asserted `open == 1`, which pinned the defect: a
        withdrawn job is terminal, so counting it open accrued unbounded "wait"
        on a job nobody was waiting for, inflating the very number the
        second-approver recommendation turns on (D-051).
        """
        _write(recon.LEDGERS["delegated_work"], [
            {"event": "held", "ts": _iso(10), "job_id": "dw-1"},
            {"event": "cancelled", "ts": _iso(4), "job_id": "dw-1",
             "reason": "requester_cancel"},
        ])
        lane = _lane(recon.analyze(now=NOW, days=30), "delegated_work")
        assert lane["denied"] == 0
        assert lane["withdrawn"] == 1
        assert lane["open"] == 0
        assert lane["open_wait_total_h"] == 0

    def test_cost_comes_from_the_runner_records(self, recon):
        _write(recon.LEDGERS["delegated_work_runner"], [
            {"event": "delivered", "ts": _iso(5), "job_id": "dw-1",
             "cost": {"est_usd": 1.25}},
            {"event": "failed", "ts": _iso(5), "job_id": "dw-2",
             "cost": {"est_usd": 0.75}},
        ])
        assert _lane(recon.analyze(now=NOW, days=30),
                     "delegated_work")["cost_usd"] == 2.0

    def test_cost_is_null_where_it_is_not_recorded(self, recon):
        """An invented cost in a document about delegating authority is worse
        than an absent one."""
        assert _lane(recon.analyze(now=NOW, days=30), "knowledge")["cost_usd"] is None
        assert _lane(recon.analyze(now=NOW, days=30), "code")["cost_usd"] is None


class TestCounterfactualBound:
    def test_code_is_never_second_approver_eligible(self, recon):
        """Hannah's own stated boundary: DW actions and knowledge, NEVER code.
        Encoded so the counterfactual cannot quietly credit a second approver
        with clearing work she has said she will not do."""
        assert "code" not in recon.SECOND_APPROVER_ELIGIBLE_LANES
        assert recon.SECOND_APPROVER_ELIGIBLE_LANES == frozenset(
            {"knowledge", "delegated_work"})
        assert _lane(recon.analyze(now=NOW, days=30),
                     "code")["second_approver_eligible"] is False

    def test_wait_is_split_between_the_two_halves(self, recon):
        _write(recon.LEDGERS["knowledge"],
               [{"proposed_at": _iso(20), "state": "PENDING"}])
        _write(recon.LEDGERS["code"],
               [{"ts": _iso(30), "id": "cq-1", "status": "PROPOSED"}])
        cf = recon.analyze(now=NOW, days=30)["counterfactual"]
        assert cf["a_second_approver_could_take"]["open_items"] == 1
        assert cf["harrison_only"]["open_items"] == 1
        assert cf["a_second_approver_could_take"]["open_wait_total_h"] == \
            pytest.approx(20, abs=0.2)
        assert cf["harrison_only"]["open_wait_total_h"] == pytest.approx(30, abs=0.2)

    def test_expired_undecided_is_surfaced_in_the_counterfactual(self, recon):
        """A second approver changes WHO fails to answer, not whether items age
        out -- so the expiry count has to travel with the recommendation."""
        _write(recon.LEDGERS["knowledge"], [
            {"proposed_at": _iso(50), "resolved_at": _iso(2), "state": "DISMISSED",
             "resolved_reason": "expired_unrouted"},
        ])
        cf = recon.analyze(now=NOW, days=30)["counterfactual"]
        assert cf["a_second_approver_could_take"]["expired_undecided"] == 1

    def test_the_bound_is_stated_in_the_rendered_report(self, recon):
        out = recon.render(recon.analyze(now=NOW, days=30))
        assert "BOUND, not a benefit" in out
        assert "never code" in out


class TestReadOnly:
    def test_the_script_takes_no_write_argument(self):
        src = (_REPO_ROOT / "scripts" / "run_approval_recon.py").read_text(
            encoding="utf-8")
        code = "\n".join(l for l in src.splitlines()
                         if l.strip() and not l.lstrip().startswith("#"))
        body = code.split('"""', 2)[-1]
        assert "--apply" not in body
        for danger in ("process_queue_action", "resolve_update", "chat_postMessage",
                       "open(", "write_text"):
            assert danger not in body, danger

    def test_the_recommendation_doc_exists_and_cites_the_script(self):
        doc = Path(r"G:\My Drive\HJR-Founder-OS\_shared\projects\cora"
                   r"\2026-08-19_fndr_approval-recon-and-tiered-approver-recommendation.md")
        if not doc.exists():
            pytest.skip("Drive mount not available")
        text = doc.read_text(encoding="utf-8")
        assert "run_approval_recon.py" in text
        assert "Harrison-only until" in text
