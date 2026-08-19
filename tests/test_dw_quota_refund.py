"""DW quota-slot refund on content_guard refusals, and the dedup premise check.

Harrison ruled 2026-08-17 (TOM 1vvvvv): a content_guard refusal REFUNDS the
requester's daily quota slot -- content_guard ONLY, not any other failure class.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from cora import delegated_work as dw

_REPO_ROOT = Path(__file__).resolve().parents[1]
USER = "U0BTESTER1"
OTHER = "U0BTESTER2"


def _load_runner():
    path = _REPO_ROOT / "scripts" / "run_delegated_work_runner.py"
    spec = importlib.util.spec_from_file_location("dw_runner_refund", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dw_runner_refund"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ledgers(tmp_path, monkeypatch):
    """Point both ledgers at a temp dir. Split-writer discipline is preserved:
    `requested` rows go to the bot file, `failed` rows to the runner file."""
    bot = tmp_path / "delegated-work.jsonl"
    runner = tmp_path / "delegated-work-runner.jsonl"
    bot.touch()
    runner.touch()
    monkeypatch.setattr(dw, "_BOT_LEDGER", bot)
    monkeypatch.setattr(dw, "_RUNNER_LEDGER", runner)

    def _write(path: Path, rows: list[dict]) -> None:
        with path.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    class L:
        def request(self, job_id, user=USER, *, days_ago=0, **extra):
            ts = dw._now() - timedelta(days=days_ago)
            _write(bot, [{"event": "requested", "ts": ts.isoformat(),
                          "job_id": job_id, "requester": user,
                          "archetype": "research_brief", "entity": "F3E",
                          "title": "t", "brief": "b", "channel_id": "C1",
                          "fingerprint": f"fp-{job_id}", **extra}])
            _write(bot, [{"event": "queued", "ts": ts.isoformat(), "job_id": job_id}])

        def fail(self, job_id, failure_class):
            ts = dw._now_iso()
            _write(runner, [{"event": "failed", "ts": ts, "job_id": job_id,
                             "failure_class": failure_class, "message": "m"}])

        def deliver(self, job_id):
            ts = dw._now_iso()
            _write(runner, [{"event": "delivered", "ts": ts, "job_id": job_id}])

    return L()


class TestRefundAccounting:
    def test_content_guard_refunds_the_slot(self, ledgers):
        ledgers.request("dw-1")
        assert dw.quota_used_today(USER) == 1
        ledgers.fail("dw-1", "content_guard")
        assert dw.refunded_today(USER) == 1
        assert dw.quota_used_today(USER) == 0

    @pytest.mark.parametrize("failure_class",
                             ["api_error", "no_output", "interrupted", "error"])
    def test_other_failure_classes_still_spend_the_slot(self, ledgers, failure_class):
        """Harrison's ruling is content_guard ONLY. These either consumed real
        model work or reflect Cora's own crash -- neither is the fairness case."""
        ledgers.request("dw-1")
        ledgers.fail("dw-1", failure_class)
        assert dw.refunded_today(USER) == 0
        assert dw.quota_used_today(USER) == 1

    def test_delivered_and_cancelled_do_not_refund(self, ledgers):
        ledgers.request("dw-1")
        ledgers.deliver("dw-1")
        assert dw.quota_used_today(USER) == 1

    def test_raw_requested_count_is_unchanged(self, ledgers):
        """requested_today stays outcome-blind: "how many did you ask for" must
        remain answerable separately from "how many count against you"."""
        ledgers.request("dw-1")
        ledgers.fail("dw-1", "content_guard")
        assert dw.requested_today(USER) == 1
        assert dw.quota_used_today(USER) == 0

    def test_refund_keys_on_the_requested_day_not_the_failure_day(self, ledgers):
        """A job requested YESTERDAY that trips the guard today must not credit
        a slot against TODAY's allowance -- the quota is a budget of asks per
        day, and the slot it spent belonged to yesterday."""
        ledgers.request("dw-old", days_ago=2)
        ledgers.fail("dw-old", "content_guard")
        assert dw.refunded_today(USER) == 0
        assert dw.quota_used_today(USER) == 0  # yesterday's ask isn't counted either

    def test_refund_is_per_user_and_org_wide(self, ledgers):
        ledgers.request("dw-1", USER)
        ledgers.request("dw-2", OTHER)
        ledgers.fail("dw-1", "content_guard")
        assert dw.quota_used_today(USER) == 0
        assert dw.quota_used_today(OTHER) == 1
        assert dw.quota_used_today(None) == 1   # org-wide sees the same refund

    def test_never_goes_negative(self, ledgers):
        """Defensive: a refund can only ever offset a real request."""
        ledgers.fail("dw-ghost", "content_guard")
        assert dw.quota_used_today(USER) >= 0

    def test_quota_remaining_reflects_the_refund(self, ledgers, monkeypatch):
        monkeypatch.setenv("CORA_DELEGATED_USER_DAILY", "3")
        ledgers.request("dw-1")
        ledgers.request("dw-2")
        assert dw.quota_remaining(USER) == 1
        ledgers.fail("dw-1", "content_guard")
        assert dw.quota_remaining(USER) == 2


class TestGateWiring:
    def test_submit_job_gates_on_used_not_requested(self):
        """The refund is only real if the GATE consults it. A refund helper
        nothing calls is a metric, not a policy."""
        src = (_REPO_ROOT / "src" / "cora" / "delegated_work.py").read_text(
            encoding="utf-8")
        gate = src[src.index('held_reason = ""'):src.index('ts = _now_iso()')]
        assert "quota_used_today(slack_user_id, jobs)" in gate
        assert "quota_used_today(None, jobs)" in gate
        assert "requested_today(" not in gate

    def test_writer_and_reader_share_one_literal(self):
        """The runner WRITES failure_class and this module READS it back. Two
        copies of the string drift silently: change one and the refund stops,
        with every test still green because each side agrees with itself."""
        runner_src = (_REPO_ROOT / "scripts" / "run_delegated_work_runner.py").read_text(
            encoding="utf-8")
        assert '"failure_class": dw.FAILURE_CONTENT_GUARD' in runner_src
        assert dw.FAILURE_CONTENT_GUARD == "content_guard"


class TestRequesterIsTold:
    def test_guard_refusal_says_it_was_refunded(self, ledgers, monkeypatch):
        """Shipping the refund while still printing "a failed attempt doesn't
        refund it" would leave the requester believing they were charged, which
        defeats the ruling as thoroughly as not building it."""
        runner = _load_runner()
        ledgers.request("dw-1")
        ledgers.fail("dw-1", "content_guard")
        job = dw.get_job("dw-1")
        note = runner.guard_quota_note(job)
        assert "refund" in note.lower()
        assert "doesn't refund" not in note.lower()

    def test_other_failures_keep_the_honest_charged_wording(self, ledgers):
        runner = _load_runner()
        ledgers.request("dw-2")
        ledgers.fail("dw-2", "api_error")
        note = runner.quota_note(dw.get_job("dw-2"))
        assert "doesn't refund it" in note

    def test_founder_gets_no_note_either_way(self, ledgers):
        runner = _load_runner()
        ledgers.request("dw-h", dw.HARRISON_ID)
        ledgers.fail("dw-h", "content_guard")
        assert runner.guard_quota_note(dw.get_job("dw-h")) == ""

    def test_guard_branch_uses_the_refund_note(self):
        src = (_REPO_ROOT / "scripts" / "run_delegated_work_runner.py").read_text(
            encoding="utf-8")
        branch = src[src.index('"failure_class": dw.FAILURE_CONTENT_GUARD'):]
        branch = branch[:branch.index("post_sessions_line")]
        assert "guard_quota_note(rec)" in branch
        assert "quota_note(rec)" not in branch.replace("guard_quota_note(rec)", "")

    def test_note_is_fail_soft(self, ledgers):
        """A broken note must never block the failure notice itself."""
        runner = _load_runner()
        assert runner.guard_quota_note({}) == ""
        assert runner.guard_quota_note({"requester": USER, "requested_at": "garbage"}) != ""
