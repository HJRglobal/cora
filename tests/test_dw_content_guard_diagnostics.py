"""Requester-facing diagnostics on a delegated-work content_guard refusal.

cq-233ca1a22976. Before this: a content_guard failure told the requester only
that "its output tripped the channel content guard", and the ledger row recorded
the same flat string with no class, archetype, entity or channel. Nine live
failures (2026-08-02..08-13 -- 50% of post-GO jobs, concentrated in F3E-topic
research briefs) were therefore un-triageable after the fact, and the requester
had no idea what to change. guard_outbound ALREADY computed the specific class;
it was being discarded at both boundaries.

The drift test in this file is TestLockstepWithGuardClasses: a new content class
cannot ship without a diagnostic entry.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from cora import channel_content_guard, delegated_worker as worker  # noqa: E402


def _job(**over):
    job = {
        "job_id": "dw-abc123def456",
        "archetype": "research_brief",
        "entity": "F3E",
        "channel_name": "f3-marketing",
        "channel_id": "C0B7X2E4VNG",
        "requester": "U0B3VGWJTMJ",
    }
    job.update(over)
    return job


class TestLockstepWithGuardClasses:
    def test_every_guard_class_has_a_diagnostic(self):
        guard_classes = {c[0] for c in channel_content_guard._CLASSES}
        missing = guard_classes - set(worker._GUARD_DIAGNOSTICS)
        assert not missing, (
            f"channel_content_guard._CLASSES gained {sorted(missing)} with no "
            f"entry in delegated_worker._GUARD_DIAGNOSTICS -- a requester would "
            f"get the generic fallback instead of a real remedy."
        )

    def test_no_stale_diagnostics(self):
        guard_classes = {c[0] for c in channel_content_guard._CLASSES}
        stale = set(worker._GUARD_DIAGNOSTICS) - guard_classes
        assert not stale, f"diagnostics for classes that no longer exist: {sorted(stale)}"

    def test_sentinels_are_not_guard_classes(self):
        guard_classes = {c[0] for c in channel_content_guard._CLASSES}
        assert worker.GUARD_CLASS_PHI not in guard_classes
        assert worker.GUARD_CLASS_SCREEN_ERROR not in guard_classes


class TestDiagnosticContent:
    @pytest.mark.parametrize("guard_class,label", [
        ("company_financials", "company financial figures"),
        ("capital_program", "capital-raise terms"),
        ("personal_insurance", "personal insurance/policy figures"),
        ("travel_points", "personal travel-points detail"),
        ("creator_crm", "creator/sponsorship CRM detail"),
        ("content_pipeline", "content-pipeline detail"),
    ])
    def test_names_what_tripped_and_a_remedy(self, guard_class, label):
        msg = worker.guard_failure_message(_job(), guard_class)
        assert label in msg
        assert "re-ask in" in msg
        # Must name the channel it was evaluated against -- the F3E research
        # briefs failed because of the CHANNEL, not the brief.
        assert "#f3-marketing" in msg

    def test_dm_job_does_not_say_hash_dm(self):
        msg = worker.guard_failure_message(
            _job(channel_name="dm", channel_id="D0B4CTD3B09"), "company_financials")
        assert "#dm" not in msg
        assert "this conversation" in msg

    @pytest.mark.parametrize("guard_class", [
        "personal_insurance", "capital_program", "travel_points",
        "creator_crm", "content_pipeline",
    ])
    def test_dm_trip_never_tells_you_to_re_ask_in_a_dm(self, guard_class):
        """A DM trip is always dashboard-gated (company_financials is PERMITTED
        in a DM), so the DM already refused. Pointing the requester back at it is
        unactionable and invites a re-ask that burns another quota slot
        (D-051 MED, this branch)."""
        msg = worker.guard_failure_message(
            _job(channel_name="dm", channel_id="D0B4CTD3B09"), guard_class)
        assert "re-ask in a DM with me" not in msg
        assert "even in a DM" in msg
        assert "Narrow the brief" in msg

    def test_channel_trip_still_offers_the_dm_remedy(self):
        msg = worker.guard_failure_message(_job(), "capital_program")
        assert "re-ask in a DM with me" in msg

    def test_phi_refusal_offers_no_channel_remedy(self):
        # PHI is never deliverable to Slack -- promising a remedy would be a lie.
        msg = worker.guard_failure_message(_job(), worker.GUARD_CLASS_PHI)
        assert "protected" in msg
        assert "re-ask in" not in msg

    def test_screen_error_says_it_is_not_the_brief(self):
        msg = worker.guard_failure_message(_job(), worker.GUARD_CLASS_SCREEN_ERROR)
        assert "re-ask" in msg
        assert "Nothing is wrong with your brief" in msg

    def test_unknown_class_still_produces_a_usable_message(self):
        msg = worker.guard_failure_message(_job(), "some_future_class")
        assert "confidential content" in msg
        assert "re-ask in" in msg


class TestClassPropagation:
    def test_trip_carries_the_class_out(self, monkeypatch):
        monkeypatch.setattr(channel_content_guard, "guard_outbound",
                            lambda text, **k: ("refusal", "capital_program"))
        fclass, msg, gclass = worker.guard_artifact_text(_job(), "the $25M raise")
        assert (fclass, gclass) == ("content_guard", "capital_program")
        assert "capital-raise terms" in msg

    def test_pass_path_returns_empty_class_and_original_text(self, monkeypatch):
        import cora.phi_guard as pg
        monkeypatch.setattr(channel_content_guard, "guard_outbound",
                            lambda text, **k: (text, None))
        monkeypatch.setattr(pg, "non_lex_phi_backstop_trips_live",
                            lambda text, allowed_names=None: False)
        assert worker.guard_artifact_text(_job(), "clean") == (None, "clean", "")

    def test_empty_text_is_a_pass(self):
        assert worker.guard_artifact_text(_job(), "") == (None, "", "")


class TestQuotaDisclosure:
    @pytest.fixture
    def hermetic_quota(self, monkeypatch):
        """Pin quota + clock so these never read the live host ledger."""
        import run_delegated_work_runner as runner
        monkeypatch.setattr(runner.dw, "quota_remaining", lambda _u: 2)
        return runner

    def test_non_founder_is_told_the_slot_was_spent(self, hermetic_quota):
        runner = hermetic_quota
        note = runner.quota_note(_job(requested_at=runner.dw._now_iso()))
        assert "daily job slot" in note
        assert "2 left today" in note
        assert "doesn't refund it" in note

    def test_wording_is_outcome_neutral(self, hermetic_quota):
        """quota_note is reached from notify_failure for interrupted/api_error/
        no_output/error -- none of which is a guard refusal, so it must not blame
        one (D-051 MED, this branch)."""
        note = hermetic_quota.quota_note(
            _job(requested_at=hermetic_quota.dw._now_iso()))
        assert "guard refusal" not in note
        assert "failed attempt doesn't refund it" in note

    def test_job_from_a_prior_az_day_does_not_claim_todays_allowance(
            self, hermetic_quota):
        """requested_today() counts by AZ date, so for a job that crossed
        midnight "used a slot" + "N left today" is self-contradictory."""
        note = hermetic_quota.quota_note(_job(requested_at="2026-08-01T10:00:00+00:00"))
        assert "left today" not in note
        assert "requested 2026-08-01" in note

    def test_unparseable_requested_at_is_not_read_as_today(self, hermetic_quota):
        # _az_date(None) silently means "today" -- guard against that.
        note = hermetic_quota.quota_note(_job(requested_at="not-a-timestamp"))
        assert "daily job slot" in note

    def test_founder_gets_no_quota_note(self):
        import run_delegated_work_runner as runner
        from cora import delegated_work as dw
        assert runner.quota_note(_job(requester=dw.HARRISON_ID)) == ""

    def test_missing_requester_gets_no_quota_note(self):
        import run_delegated_work_runner as runner
        assert runner.quota_note(_job(requester="")) == ""

    def test_quota_lookup_failure_is_fail_soft(self, monkeypatch):
        import run_delegated_work_runner as runner

        def _boom(_user):
            raise RuntimeError("ledger unreadable")

        monkeypatch.setattr(runner.dw, "quota_remaining", _boom)
        # A failure notice must still go out even if quota can't be read.
        assert runner.quota_note(_job()) == ""
