"""Tests for scripts/audit_test_timerot.py -- the time-rot audit instrument.

Only the PURE parts are tested: failure-summary parsing and the triage rule.
Running the suite under a shifted clock is I/O and needs freezegun, which is
deliberately not a project dependency (the audit installs it into a throwaway
--target dir so the venv the bot runs from is untouched).

The triage rule is the part worth pinning. It encodes a fact that cost real
measurement to establish: a clock shift produces two classes of FALSE positive,
both from fixtures generated at runtime, and they are separated by whether the
failure survives flipping the mtime shim.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "audit_test_timerot.py"


def _load():
    spec = importlib.util.spec_from_file_location("_timerot_audit", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_timerot_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


audit = _load()


class TestParseFailures:
    def test_reads_failed_and_error_lines(self):
        out = (
            "........F.....\n"
            "FAILED tests/test_a.py::test_one - AssertionError: nope\n"
            "FAILED tests/test_b.py::TestK::test_two\n"
            "ERROR tests/test_c.py::test_three - AssertionError: boom\n"
            "3 failed, 10 passed in 1.00s\n"
        )
        assert audit.parse_failures(out) == {
            "tests/test_a.py::test_one",
            "tests/test_b.py::TestK::test_two",
            "tests/test_c.py::test_three",
        }

    def test_a_green_run_parses_to_nothing(self):
        assert audit.parse_failures("13587 passed in 400.00s\n") == set()

    def test_the_trailing_reason_is_stripped_from_the_id(self):
        out = "FAILED tests/test_a.py::test_one - AssertionError: assert 1 == 2\n"
        assert audit.parse_failures(out) == {"tests/test_a.py::test_one"}

    def test_a_mention_of_the_word_failed_in_prose_is_not_an_id(self):
        """The summary lines are anchored at the start of a line, so narrative
        output ('4 failed', 'the run FAILED because...') is not mistaken for a
        test id."""
        assert audit.parse_failures("  4 failed, 2 passed\nthe run failed\n") == set()


class TestTriageRule:
    """Genuine rot never involves a runtime timestamp, so it fails with the
    mtime shim ON *and* OFF. A failure in only one mode flips with mtime
    semantics, which means the fixture was created during the test and
    therefore cannot age out in real time."""

    def test_a_failure_in_both_modes_is_a_candidate(self):
        assert audit.genuine_candidates({"a", "b"}, {"b", "c"}) == {"b"}

    def test_a_failure_in_only_the_shim_mode_is_an_artefact(self):
        """os.utime-backdated fixtures read as FRESH once mtimes are shifted --
        finance_adherence, session_capture, the stale-lock reclaim tests."""
        assert audit.genuine_candidates({"only_with_shim"}, set()) == set()

    def test_a_failure_in_only_the_no_shim_mode_is_an_artefact(self):
        """A just-written file reads as N days old when now() is shifted but
        os.stat is not -- dynamic_answers' '720.0h old, threshold 24h'."""
        assert audit.genuine_candidates(set(), {"only_without_shim"}) == set()

    def test_no_failures_anywhere_yields_no_candidates(self):
        assert audit.genuine_candidates(set(), set()) == set()

    def test_the_rule_is_symmetric(self):
        a, b = {"x", "y"}, {"y", "z"}
        assert audit.genuine_candidates(a, b) == audit.genuine_candidates(b, a)


class TestPluginSource:
    """The plugin is emitted as source rather than shipped as a module, so the
    audit leaves nothing importable behind in the repo."""

    def test_the_emitted_plugin_compiles(self):
        compile(audit.PLUGIN_SOURCE, "timeshift_plugin.py", "exec")

    def test_it_freezes_at_the_earliest_hook(self):
        """Started later (in pytest_configure) the test's own clock and the
        product's disagree by exactly the shift, which manufactures a false
        positive that takes a direct measurement to unmask."""
        assert "def pytest_load_initial_conftests" in audit.PLUGIN_SOURCE
        src = audit.PLUGIN_SOURCE
        assert src.index("pytest_load_initial_conftests") < src.index("def pytest_configure") \
            if "def pytest_configure" in src else True

    def test_it_ticks_rather_than_hard_freezing(self):
        """A hard-frozen instant breaks anything measuring elapsed time, which
        would swamp the audit with failures caused by the instrument."""
        assert "tick=True" in audit.PLUGIN_SOURCE

    def test_the_mtime_shim_is_opt_in_and_restores_os_stat(self):
        assert 'TIMEROT_SHIFT_MTIME", "0"' in audit.PLUGIN_SOURCE, "shim must default OFF"
        assert "os.stat, os.lstat = saved" in audit.PLUGIN_SOURCE, "must restore os.stat"

    def test_the_shim_shifts_mtimes_by_the_same_delta(self):
        assert "_SECS = _SHIFT * 86400" in audit.PLUGIN_SOURCE
        assert "st_mtime" in audit.PLUGIN_SOURCE


class TestDocumentsWhatItCostToLearn:
    """This script exists so the next person does not re-derive the artefact
    classes from scratch. If the explanation goes, so does most of its value."""

    def test_both_artefact_classes_are_named(self):
        doc = audit.__doc__ or ""
        assert "os.stat" in doc, "the mtime artefact class must be documented"
        assert "ThreadPoolExecutor" in doc, "the cross-thread artefact class must be documented"

    def test_it_says_why_a_static_grep_is_not_enough(self):
        doc = audit.__doc__ or ""
        assert "CLOCK INJECTION" in doc
        assert "false positives" in doc

    def test_it_names_both_incidents(self):
        doc = audit.__doc__ or ""
        assert "2026-08-31" in doc and "2026-12-31" in doc


@pytest.mark.skipif(not _SCRIPT.exists(), reason="script missing")
def test_emit_plugin_writes_a_working_plugin(tmp_path):
    rc = audit.main(["--emit-plugin", "--tools-dir", str(tmp_path)])
    assert rc == 0
    written = tmp_path / "timeshift_plugin.py"
    assert written.exists()
    compile(written.read_text(encoding="utf-8"), str(written), "exec")


def test_it_refuses_to_run_without_the_instrument(tmp_path, capsys):
    """freezegun is not a project dependency. Missing it must produce a clear
    install line, not a confusing import error mid-run."""
    rc = audit.main(["--tools-dir", str(tmp_path), "--shifts", "30"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "uv pip install --target" in out
    assert "freezegun" in out
