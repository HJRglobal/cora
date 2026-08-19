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

    def test_scan_is_length_capped(self):
        """The cap is what bounds worst-case cost on a hostile brief."""
        assert dw._DEST_SCAN_CAP <= 4000
        # A destination beyond the cap is simply not scanned -- no crash.
        assert dw.brief_names_a_destination("x" * 5000 + " deliver to G:\\a\\b") is False

    def test_no_redos_on_pathological_input(self):
        """Four ReDoS incidents in this repo's history. These patterns run over
        delimiter-dense text, which is exactly the shape that blew up before."""
        for hostile in ("/" * 1800, "a/" * 900, "\\" * 1800, "a's " * 400):
            start = time.monotonic()
            dw.brief_names_a_destination(hostile)
            assert time.monotonic() - start < 1.0, f"slow on {hostile[:12]!r}"


class TestDestinationNotice:
    def test_notice_states_the_REAL_target_directory(self):
        """The whole point: the path in the ack comes from the same function the
        runner writes to, so the two cannot disagree."""
        job = _job()
        real_dir = str(worker.artifact_target_path(job).parent)
        assert real_dir in dw.destination_notice(job)

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

    def test_notice_is_built_in_code_not_by_the_model(self):
        """No prompt/tool-description path may own this sentence: the whole
        failure was a model-authored destination claim."""
        assert "def destination_notice" in _DW_SRC


class TestPostDeliveryVerify:
    """Asserted against runner source: the verify sits inside deliver_job, which
    needs a live Slack client and a claimed job to execute end-to-end."""

    def test_runner_stats_the_target_after_writing(self):
        assert "drive_io.stat_info(target)" in _RUNNER_SRC

    def test_verify_treats_absent_or_empty_as_mis_homed(self):
        assert "size <= 0" in _RUNNER_SRC
        # stat_info returns a (mtime, size) TUPLE or None -- indexing, not .get.
        assert "int(info[1]) if info else 0" in _RUNNER_SRC

    def test_verify_runs_before_the_requester_is_told_a_path(self):
        """Ordering is the property that matters: verify must precede both the
        artifact_meta the ledger records and the 'File: {target}' message."""
        verify = _RUNNER_SRC.index("drive_io.stat_info(target)")
        meta = _RUNNER_SRC.index('artifact_meta = {"local_path"')
        told = _RUNNER_SRC.index('lines.append(f"File: {target}")')
        assert verify < meta < told

    def test_verify_runs_before_staging_is_cleaned(self):
        """_clean_staging deletes the only other copy; it must never run on an
        unverified write."""
        verify = _RUNNER_SRC.index("drive_io.stat_info(target)")
        clean = _RUNNER_SRC.rindex("_clean_staging(job_id)")
        assert verify < clean

    def test_unverifiable_write_is_downgraded_not_raised(self):
        """A G: blip must degrade to 'staged locally', not lose the delivery."""
        seg = _RUNNER_SRC[_RUNNER_SRC.index("drive_io.stat_info(target)"):]
        seg = seg[:seg.index("artifact_meta = {")]
        assert "mis_homed = True" in seg
        assert "except Exception" in seg
