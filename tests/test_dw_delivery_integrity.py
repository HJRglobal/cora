"""Delegated-work delivery integrity (cq-e1d091eb6007).

THE LIVE FAILURE
----------------
Hannah gave an explicit destination for a DW deliverable
(`_shared/team-knowledge/hannah/`), Cora's reply agreed, and the file never
landed. Tracing the delivery leg showed why it could not have:
`delegated_worker.artifact_target_path` is FULLY deterministic --
`{entity_folder}/_delegated-work/YYYY-MM/<generated name>` -- and the job spec
has no destination field at all. A path in the brief is prose; nothing reads it.

So the "confirmation" was the model echoing the asker's own words back. Same
class as the staged-write doctrine (identity binds SERVER-SIDE, never an LLM
echo), applied to destinations.

TWO FIXES, TWO HALVES OF THE SAME PROMISE
-----------------------------------------
1. ASK time: when a brief names a destination, the ack -- built in code, from
   `artifact_target_path` -- states where the file will actually go instead of
   letting the model agree to somewhere it won't.
2. DELIVERY time: a Drive write that returns without raising is not proof a file
   exists. `mis_homed` decides both what the requester is told AND whether
   `_clean_staging` deletes the only other copy, so an unverified write could
   name a path holding nothing while discarding the staged original. Now stat'd.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from cora import delegated_work as dw  # noqa: E402
from cora import delegated_worker as worker  # noqa: E402

_DW_SRC = (_REPO_ROOT / "src" / "cora" / "delegated_work.py").read_text(encoding="utf-8")
_RUNNER_SRC = (_REPO_ROOT / "scripts" / "run_delegated_work_runner.py").read_text(
    encoding="utf-8")


def _job(**over):
    job = {
        "job_id": "dw-abc123def456",
        "archetype": "spreadsheet_build",
        "entity": "F3E",
        "deliverable": "xlsx",
        "title": "F3 SKU reference",
        "channel_name": "f3-marketing",
        "requester": "U0B3VGWJTMJ",
    }
    job.update(over)
    return job


class TestDestinationDetection:
    """Broad by design: a false positive costs one clarifying sentence, a false
    negative reproduces the original broken promise."""

    @pytest.mark.parametrize("brief", [
        # The exact live case.
        "Build a reference sheet and deliver to _shared/team-knowledge/hannah/",
        r"save it in G:\My Drive\HJR-Founder-OS",
        "Deliver to Hannah Grant's personal Drive (hannah@hjrglobal.com).",
        "upload to the shared drive when done",
        "write it into founder-os",
        "put it in my drive",
        "drop it in the team-knowledge folder",
    ])
    def test_destination_asks_are_caught(self, brief):
        assert dw.brief_names_a_destination(brief) is True

    @pytest.mark.parametrize("brief", [
        "Build a quick reference spreadsheet of all 15 F3 Energy SKUs with UPC and MSRP.",
        "Research email marketing best practices and create a 1-page brief.",
        "List our products",
        "",
    ])
    def test_ordinary_briefs_are_not_flagged(self, brief):
        assert dw.brief_names_a_destination(brief) is False

    def test_none_brief_is_safe(self):
        assert dw.brief_names_a_destination(None) is False  # type: ignore[arg-type]

    def test_scan_cap_is_actually_a_cap(self):
        """`<= 4000` asserted nothing: BRIEF_MAX_CHARS IS 4000, so a 4000 cap
        would scan every possible brief in full (D-051)."""
        assert dw._DEST_SCAN_CAP < dw.BRIEF_MAX_CHARS

    @pytest.mark.parametrize("brief", [
        # Every one of these is real prose shape that the FIRST version flagged.
        # Measured against all 26 briefs in the live ledger, it fired on 4 of 6 --
        # 67% of notices were noise telling a requester "I can't deliver to a
        # folder you pick" when they had named no folder (D-051).
        "Compare Energy/Mood/Pure sell-through by flavor",
        "Model spend at $125/athlete/month across the roster",
        "Summarize everything that happened on 8/18/2026",
        "Map the intake/approval/shipping stages",
        "Break out US/CA/MX volume 24/7",
        "Drop in a chart of monthly revenue",   # verb+prep with no destination
    ])
    def test_prose_that_merely_contains_slashes_is_not_a_destination(self, brief):
        assert dw.brief_names_a_destination(brief) is False

    def test_no_redos_on_pathological_input(self):
        """Four ReDoS incidents in this repo's history. These patterns run over
        delimiter-dense text, which is exactly the shape that blew up before."""
        for hostile in ("/" * 1800, "a/" * 900, "\\" * 1800, "a's " * 400):
            start = time.monotonic()
            dw.brief_names_a_destination(hostile)
            assert time.monotonic() - start < 1.0, f"slow on {hostile[:12]!r}"


class TestDestinationNotice:
    def test_notice_states_the_REAL_stable_parent(self):
        """The path in the ack comes from the same function the runner writes to,
        so the two cannot disagree."""
        job = _job()
        stable = str(worker.artifact_target_path(job).parent.parent)
        assert stable in dw.destination_notice(job)

    def test_notice_does_NOT_promise_a_dated_subfolder(self):
        """The `YYYY-MM` leaf is computed from `now` at ASK time and recomputed at
        DELIVERY time. A HELD job never expires and a QUEUED job has 48h, so any
        job crossing a month boundary was promised the wrong month -- the one way
        the two could genuinely disagree (D-051)."""
        job = _job()
        dated_leaf = worker.artifact_target_path(job).parent.name   # e.g. 2026-08
        notice = dw.destination_notice(job)
        assert dated_leaf not in notice
        assert "dated subfolder" in notice

    def test_notice_says_it_cannot_use_a_chosen_folder(self):
        text = dw.destination_notice(_job()).lower()
        assert "can't deliver to a folder you pick" in text

    def test_notice_is_fail_soft_and_never_invents_a_path(self, monkeypatch):
        """If the path cannot be computed we say the generic true thing. An
        unverifiable promise is the defect being closed -- don't add a new one."""
        def boom(*a, **k):
            raise RuntimeError("no entity folder")
        monkeypatch.setattr(worker, "artifact_target_path", boom)
        text = dw.destination_notice(_job())
        assert "_delegated-work" in text
        assert "G:\\" not in text and "G:/" not in text

    def test_entity_specific(self):
        """A LEX job must not be told an F3E folder."""
        f3e = dw.destination_notice(_job(entity="F3E"))
        lex = dw.destination_notice(_job(entity="LEX-LLC"))
        assert f3e != lex


class TestAckWiring:
    """Both terminal acks of submit_job must carry the notice -- a HELD job is
    exactly the one a requester waits days on, so it needs the truth too."""

    def test_queued_ack_appends_the_notice(self):
        assert 'queued_msg += "\\n\\n" + destination_notice(job)' in _DW_SRC

    def test_held_ack_appends_the_notice(self):
        assert 'held_msg += "\\n\\n" + destination_notice(job)' in _DW_SRC

    def test_both_acks_are_gated_on_the_detector(self):
        assert _DW_SRC.count("if brief_names_a_destination(brief):") == 2

    def test_notice_is_independent_of_what_the_brief_claimed(self):
        """The failure was a model-authored destination. Two briefs naming two
        different fake paths must produce the SAME real path -- the notice cannot
        be a function of the asker's words."""
        a = dict(_job(), brief="deliver to _shared/team-knowledge/hannah/")
        b = dict(_job(), brief=r"save it in D:\somewhere\else\entirely")
        assert dw.destination_notice(a) == dw.destination_notice(b)
        assert "team-knowledge" not in dw.destination_notice(a)
        assert "somewhere" not in dw.destination_notice(b)


class TestPostDeliveryVerify:
    """Delivery-time verify. The behavioral coverage lives in
    TestTargetVerifiedHelper; what matters here is ORDERING -- that nothing tells
    the requester a path, records the artifact, or deletes staging before the
    write has been confirmed.
    """

    def test_delivery_verifies_before_the_requester_is_told_a_path(self):
        verify = _RUNNER_SRC.index("_target_verified(target, job_id=job_id)")
        meta = _RUNNER_SRC.index('artifact_meta = {"local_path"')
        told = _RUNNER_SRC.index('lines.append(f"File: {target}")')
        assert verify < meta < told

    def test_delivery_verifies_before_staging_is_cleaned(self):
        """_clean_staging deletes the only other copy; it must never run on an
        unverified write."""
        verify = _RUNNER_SRC.index("_target_verified(target, job_id=job_id)")
        clean = _RUNNER_SRC.index("if not mis_homed:\n        _clean_staging(job_id)")
        assert verify < clean

    def test_an_unverified_write_is_reported_as_staged_not_delivered(self, monkeypatch):
        """End-to-end: the write returns cleanly but lands nothing, so the
        requester must be told it is staged -- never handed a path holding
        nothing (the companion behavioral test lives in
        tests/test_delegated_worker.py)."""
        import run_delegated_work_runner as runner
        monkeypatch.setattr(runner.drive_io, "stat_info", lambda *a, **k: None)
        assert runner._target_verified("whatever", job_id="dw-x") is False


class TestTargetVerifiedHelper:
    """The shared verify. Behavioral, not a source-text pin: the earlier version
    of these assertions would have passed against broken code."""

    def _runner(self):
        import run_delegated_work_runner as runner
        return runner

    def test_missing_target_is_not_verified(self, monkeypatch):
        runner = self._runner()
        monkeypatch.setattr(runner.drive_io, "stat_info", lambda *a, **k: None)
        assert runner._target_verified("X", job_id="dw-1") is False

    def test_zero_byte_target_is_not_verified(self, monkeypatch):
        runner = self._runner()
        monkeypatch.setattr(runner.drive_io, "stat_info", lambda *a, **k: (1.0, 0))
        assert runner._target_verified("X", job_id="dw-1") is False

    def test_a_real_file_is_verified(self, monkeypatch):
        runner = self._runner()
        monkeypatch.setattr(runner.drive_io, "stat_info", lambda *a, **k: (1.0, 4096))
        assert runner._target_verified("X", job_id="dw-1") is True

    def test_drive_unavailable_is_not_verified_and_does_not_raise(self, monkeypatch):
        """A mount blip must degrade to 'not delivered', never propagate -- the
        exception branch previously had no behavioral test at all (D-051)."""
        runner = self._runner()

        def boom(*a, **k):
            raise OSError("mount gone")

        monkeypatch.setattr(runner.drive_io, "stat_info", boom)
        assert runner._target_verified("X", job_id="dw-1") is False

    def test_verify_uses_an_explicit_short_budget(self, monkeypatch):
        """stat_info DEFAULTS are timeout=10/retry_seconds=90, and a mount-gone
        classification trips drive_io's PROCESS-WIDE breaker for >=90s -- which
        fast-fails the next job's write in the same pass and cascades mis_homed
        onto jobs that were fine (D-051)."""
        runner = self._runner()
        seen = {}

        def spy(target, **kw):
            seen.update(kw)
            return (1.0, 10)

        monkeypatch.setattr(runner.drive_io, "stat_info", spy)
        runner._target_verified("X", job_id="dw-1")
        assert seen.get("retry_seconds") == 0
        assert seen.get("timeout") == 10.0


class TestRetryPassCannotClaimAnUnverifiedWrite:
    """HIGH (D-051): mis_homed_retry_pass re-wrote from staging, appended
    artifact_homed unconditionally (clearing mis_homed) and deleted staging with
    NO verify -- so a write into a vanishing handle left the target empty,
    staging gone, the ledger reading homed, and the requester never corrected.
    The delivery-time verify routes MORE traffic here.
    """

    def test_retry_pass_verifies_before_claiming(self):
        src = _RUNNER_SRC
        verify = src.index("_target_verified(target, job_id=jid)")
        homed = src.index('"event": "artifact_homed"')
        clean = src.index("_clean_staging(jid)")
        assert verify < homed < clean

    def test_retry_pass_announces_a_late_landing(self):
        """The delivery post said "staged locally and will land automatically";
        without this the landing is never announced to the person waiting."""
        seg = _RUNNER_SRC[_RUNNER_SRC.index("def mis_homed_retry_pass"):]
        seg = seg[:seg.index("def overflow_digest_pass")]
        assert "has now landed on Drive" in seg
        assert "post_threaded" in seg


class TestPreviewCarriesTheCorrection:
    """The preview is the only point at which the destination correction is
    actionable -- otherwise the requester confirms still believing the wrong
    path, on a job that is then queued and budget-reserved (D-051)."""

    def test_preview_consults_the_detector_and_the_notice(self):
        src = (_REPO_ROOT / "src" / "cora" / "tools" / "tool_dispatch.py").read_text(
            encoding="utf-8")
        seg = src[src.index("def _delegated_preview_text"):]
        seg = seg[:seg.index("def _execute_claimed_delegated")]
        assert "brief_names_a_destination" in seg
        assert "destination_notice" in seg

    def test_preview_hint_is_fail_soft(self):
        src = (_REPO_ROOT / "src" / "cora" / "tools" / "tool_dispatch.py").read_text(
            encoding="utf-8")
        seg = src[src.index("def _delegated_preview_text"):]
        seg = seg[:seg.index("def _execute_claimed_delegated")]
        assert "except Exception" in seg
