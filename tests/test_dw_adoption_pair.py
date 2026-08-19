"""DW adoption pair: requester-facing job listing + concurrent-resubmission dedup.

cq-443695ccaa60 ("no conversational way for a requester to list or check their
own delegated jobs") and cq-8f462b5701c8 ("no dedup check against a genuinely
concurrent identical resubmission").

VERIFY-FIRST outcome, recorded here so it is not re-derived next session:

  * The LIST CAPABILITY already existed (`action='list'` -> render_job_list).
    What did not exist was DISCOVERABILITY and enough detail to answer the
    question people actually ask -- "what happened to my job?" -- so this file
    pins the routing triggers and the new state/age/quota detail rather than a
    new code path.
  * The DEDUP CHECK already existed and is genuinely concurrency-safe. The tests
    below EXERCISE the race rather than reading the code, so the claim is
    measured. Kept as regression pins on a guarantee that was reported missing.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from cora import delegated_work as dw

_REPO_ROOT = Path(__file__).resolve().parents[1]
USER = "U0BADOPT01"
OTHER = "U0BADOPT02"


@pytest.fixture
def ledgers(tmp_path, monkeypatch):
    bot = tmp_path / "delegated-work.jsonl"
    runner = tmp_path / "delegated-work-runner.jsonl"
    bot.touch()
    runner.touch()
    monkeypatch.setattr(dw, "_BOT_LEDGER", bot)
    monkeypatch.setattr(dw, "_RUNNER_LEDGER", runner)
    return bot, runner


def _append(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _seed(bot: Path, job_id, *, user=USER, channel="C1", state_evt="queued",
          minutes_ago=0, title="Sprouts reset brief", entity="F3E"):
    ts = (dw._now() - timedelta(minutes=minutes_ago)).isoformat()
    _append(bot, {"event": "requested", "ts": ts, "job_id": job_id,
                  "requester": user, "archetype": "research_brief",
                  "entity": entity, "title": title, "brief": "b",
                  "channel_id": channel, "fingerprint": f"fp-{job_id}"})
    _append(bot, {"event": state_evt, "ts": ts, "job_id": job_id})
    return ts


class TestJobListing:
    def test_empty_list_points_at_the_next_step(self, ledgers):
        out = dw.render_job_list(USER, "C1")
        assert "no delegated jobs" in out
        assert "research" in out.lower()

    def test_list_shows_state_age_and_quota(self, ledgers):
        bot, _ = ledgers
        _seed(bot, "dw-1", minutes_ago=90)
        out = dw.render_job_list(USER, "C1")
        assert "dw-1" in out
        assert "QUEUED" in out
        assert "1h ago" in out
        assert "left today" in out

    def test_failed_job_says_why(self, ledgers):
        bot, runner = ledgers
        _seed(bot, "dw-2")
        _append(runner, {"event": "failed", "ts": dw._now_iso(),
                         "job_id": "dw-2", "failure_class": "api_error",
                         "message": "m"})
        out = dw.render_job_list(USER, "C1")
        assert "api error" in out

    def test_content_guard_failure_tells_them_the_slot_came_back(self, ledgers):
        """Otherwise the refund is invisible: the requester sees FAILED and
        assumes they were charged."""
        bot, runner = ledgers
        _seed(bot, "dw-3")
        _append(runner, {"event": "failed", "ts": dw._now_iso(),
                         "job_id": "dw-3",
                         "failure_class": dw.FAILURE_CONTENT_GUARD, "message": "m"})
        out = dw.render_job_list(USER, "C1")
        assert "slot refunded" in out

    def test_cross_channel_title_suppression_is_unchanged(self, ledgers):
        """The added detail clause must not become a title-leak channel: it is
        derived from state and failure class only, never from the brief."""
        bot, _ = ledgers
        _seed(bot, "dw-4", channel="C_PRIVATE", title="Acme acquisition diligence")
        out = dw.render_job_list(USER, "C_OTHER")
        assert "Acme acquisition" not in out
        assert "dw-4" in out

    def test_empty_channel_id_suppresses_every_title(self, ledgers):
        bot, _ = ledgers
        _seed(bot, "dw-5", channel="C1", title="Acme acquisition diligence")
        assert "Acme acquisition" not in dw.render_job_list(USER, "")

    def test_only_the_askers_own_jobs(self, ledgers):
        bot, _ = ledgers
        _seed(bot, "dw-mine", user=USER)
        _seed(bot, "dw-theirs", user=OTHER)
        out = dw.render_job_list(USER, "C1")
        assert "dw-mine" in out and "dw-theirs" not in out

    @pytest.mark.parametrize("minutes,expected", [
        (0, "just now"), (5, "5m ago"), (120, "2h ago"), (60 * 26, "1d ago"),
    ])
    def test_age_phrases(self, minutes, expected):
        ts = (dw._now() - timedelta(minutes=minutes)).isoformat()
        assert dw._age_phrase(ts) == expected

    def test_age_phrase_is_safe_on_garbage(self):
        assert dw._age_phrase("") == ""
        assert dw._age_phrase("not-a-timestamp") == ""


class TestListDiscoverability:
    def test_tool_description_carries_status_triggers(self):
        """The capability existed; the model had no reason to reach for it. The
        tool is framed as "delegate a job that produces a file", so a status ask
        did not look like a match."""
        from cora.tools import tool_dispatch
        spec = next(t for t in tool_dispatch.TOOL_DEFINITIONS
                    if t["name"] == "cora_delegate_work")
        desc = spec["description"].lower()
        for phrase in ("list my delegated jobs", "status of my", "did that research",
                       "job slots", "action='list'"):
            assert phrase in desc, phrase
        assert "do not answer these from memory" in desc

    def test_list_action_is_still_a_read_not_a_staged_write(self):
        """A status check must never require a confirmation round-trip."""
        src = (_REPO_ROOT / "src" / "cora" / "tools" / "tool_dispatch.py").read_text(
            encoding="utf-8")
        block = src[src.index('def _tool_cora_delegate_work'):]
        block = block[:block.index('if action == "cancel"')]
        assert '_write_blocked_contract(delegated_work.render_job_list' in block


@pytest.fixture
def screens_pass(monkeypatch):
    """Neutralize the intake SCREEN chain only.

    The subject under test here is the dedup critical section, not authorization:
    a synthetic test id is (correctly) refused by the fail-closed unknown-user
    screen long before dedup is reached, which would make these tests pass for
    the wrong reason. Screens have their own coverage in test_delegated_work.py.
    """
    monkeypatch.setattr(dw, "screen_request", lambda *a, **k: None)


class TestConcurrentResubmissionDedup:
    """cq-8f462b5701c8. The guarantee was reported missing; these EXERCISE it."""

    def test_two_simultaneous_confirms_of_one_brief_yield_one_job(self, ledgers, screens_pass, monkeypatch):
        monkeypatch.setenv("CORA_DELEGATED_WORK", "live")
        monkeypatch.setenv("CORA_DELEGATED_USER_DAILY", "9")
        monkeypatch.setenv("CORA_DELEGATED_ORG_DAILY", "9")
        spec = dict(archetype="research_brief",
                    brief="Put together a brief on Sprouts energy-drink resets",
                    slack_user_id=USER, entity="F3E",
                    channel_id="C1", channel_name="f3e-sales", thread_ts="",
                    deliverable="md")

        results: list[tuple] = []
        barrier = threading.Barrier(2)

        def _go():
            barrier.wait()          # maximize overlap on the critical section
            results.append(dw.submit_job(**spec))

        threads = [threading.Thread(target=_go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        outcomes = sorted(r[1] for r in results)
        assert outcomes == ["queued", "refused"], outcomes
        jobs = [r for r in dw.load_jobs()
                if str(r.get("requester")) == USER
                and r.get("state") in dw.NON_TERMINAL_STATES]
        assert len(jobs) == 1

    def test_the_refusal_names_the_open_job(self, ledgers, screens_pass, monkeypatch):
        """A bare "already open" is untriageable -- the requester needs the id
        to check or cancel it."""
        monkeypatch.setenv("CORA_DELEGATED_WORK", "live")
        spec = dict(archetype="research_brief",
                    brief="brief on Sprouts energy-drink resets",
                    slack_user_id=USER, entity="F3E",
                    channel_id="C1", channel_name="f3e-sales", thread_ts="",
                    deliverable="md")
        job, outcome, _ = dw.submit_job(**spec)
        _, outcome2, msg2 = dw.submit_job(**spec)
        assert outcome == "queued" and outcome2 == "refused"
        assert job["job_id"] in msg2

    def test_a_terminal_job_does_not_block_a_re_ask(self, ledgers, screens_pass, monkeypatch):
        """Dedup keys on NON-terminal state on purpose: re-running a brief that
        already finished (or was refused by the content guard) is legitimate."""
        monkeypatch.setenv("CORA_DELEGATED_WORK", "live")
        _, runner = ledgers
        spec = dict(archetype="research_brief",
                    brief="brief on Sprouts energy-drink resets",
                    slack_user_id=USER, entity="F3E",
                    channel_id="C1", channel_name="f3e-sales", thread_ts="",
                    deliverable="md")
        job, outcome, _ = dw.submit_job(**spec)
        assert outcome == "queued"
        _append(runner, {"event": "failed", "ts": dw._now_iso(),
                         "job_id": job["job_id"],
                         "failure_class": dw.FAILURE_CONTENT_GUARD, "message": "m"})
        _, outcome2, _ = dw.submit_job(**spec)
        assert outcome2 == "queued"

    def test_fingerprint_normalizes_mentions_and_whitespace(self):
        """Two "identical" resubmissions rarely arrive byte-identical."""
        a = dw.brief_fingerprint(USER, "Brief on   Sprouts resets")
        b = dw.brief_fingerprint(USER, "<@U123> brief on Sprouts   resets\n")
        assert a == b

    def test_fingerprint_is_per_requester(self):
        assert (dw.brief_fingerprint(USER, "same brief")
                != dw.brief_fingerprint(OTHER, "same brief"))


class TestFoldOrderingInvariant:
    """Latent fragility found while writing the tests above, pinned rather than
    left implicit.

    `_fold_jobs` orders events by RAW ISO STRING (`events.sort(key=str(ts))`),
    not by parsed instant. That is fine only while every writer stamps the same
    UTC offset -- and both do, via `_now_iso()`. A writer that stamped AZ local
    time instead would produce strings that sort EARLIER than contemporaneous
    UTC ones, so a `failed` row could fold BEFORE the `queued` row that preceded
    it and the terminal state would be silently overwritten by a live one. That
    is the resurrect-trap class the split-ledger design exists to prevent, so
    the property both writers rely on gets a pin.

    (This is exactly how it showed up: a fixture stamping AZ time made a
    content_guard-failed job read as still QUEUED.)
    """

    def test_timestamps_are_utc(self):
        assert dw._now().utcoffset().total_seconds() == 0
        assert dw._now_iso().endswith(("+00:00", "Z"))

    def test_a_terminal_event_is_not_overwritten_by_an_earlier_live_one(self, ledgers):
        """Order-independence at the fold: whatever the file order, DELIVERED
        (later ts) must win over QUEUED (earlier ts)."""
        bot, runner = ledgers
        early = dw._now_iso()
        _append(runner, {"event": "delivered", "ts": dw._now_iso(),
                         "job_id": "dw-ord"})
        _append(bot, {"event": "requested", "ts": early, "job_id": "dw-ord",
                      "requester": USER, "archetype": "research_brief",
                      "entity": "F3E", "title": "t", "brief": "b",
                      "channel_id": "C1", "fingerprint": "fp-ord"})
        _append(bot, {"event": "queued", "ts": early, "job_id": "dw-ord"})
        assert dw.get_job("dw-ord")["state"] == dw.STATE_DELIVERED
