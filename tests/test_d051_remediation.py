"""Regression pins for the D-051 review remediation (session #11).

The review confirmed 27 defects, 26 of them SELF-INFLICTED by this branch. These
pin the fixes for the ones that were about a rail being DEAD, DISHONEST, or
DESTRUCTIVE -- the three shapes the session exists to retire, which the session
then committed itself.
"""
from __future__ import annotations

import importlib.util
import io
import sys
from datetime import date
from pathlib import Path

import pytest


class TestClaimsRailWasReverted:
    """FIVE of the six HIGH findings were the S7 preflight change, which loosened
    a REGULATORY control three separate ways with differential proof
    (main=TRIP -> branch=PASS). The slice was reverted rather than patched: the
    false-positive cost is a productivity nuisance, a claims hole is not."""

    @pytest.mark.parametrize("copy_text", [
        "F3 Energy is Clean Energy.",                        # R2 canonical violation
        "F3 Energy delivers Clean Energy All Day.",          # Title-Case evasion
        "No other drink prevents fatigue like F3 Energy.",   # comparative claim
        "There is no better way to manage stress than F3 Energy.",
        "No one denies that F3 Energy cures fatigue.",
    ])
    def test_the_five_loosened_cases_all_trip_again(self, copy_text):
        from src.cora.f3e_blog.preflight import run_preflight

        result = run_preflight(title="Post", summary="", body_html="<p>%s</p>" % copy_text)
        assert result.trips, "claims rail no longer catches: %s" % copy_text

    def test_the_reverted_helpers_are_gone(self):
        src = io.open("src/cora/f3e_blog/preflight.py", encoding="utf-8").read()
        assert "_CLEAN_PROPER_NOUN_RES" not in src


class TestStalenessRoundTrips:
    """The store lives on a WINDOWS DRIVE MOUNT and the bot writes it CRLF.
    splitlines() silently ate \\r, so every read would have rewritten the whole
    document to LF -- a transform running over always-injected context."""

    def test_crlf_content_is_returned_unchanged(self):
        from src.cora.known_answer_staleness import apply_staleness

        crlf = "## Known facts\r\n\r\n### Ladder\r\nWholesale $24.\r\n"
        assert apply_staleness(crlf, date(2026, 8, 30)) is crlf

    def test_lf_content_is_returned_unchanged(self):
        from src.cora.known_answer_staleness import apply_staleness

        lf = "## Known facts\n\n### Ladder\nWholesale $24.\n"
        assert apply_staleness(lf, date(2026, 8, 30)) is lf

    def test_it_still_transforms_when_it_should(self):
        from src.cora.known_answer_staleness import apply_staleness

        doc = "## Known facts\n\n**[2026-07-13] cash**\nA: Cash balance is $1,347,657.\n"
        out = apply_staleness(doc, date(2026, 8, 30))
        assert "1,347,657" not in out and "WITHHELD" in out

    def test_docstring_no_longer_claims_an_end_to_end_withhold(self):
        """The withhold is real where the helper is CALLED and false of the reply:
        KB retrieval still returns the same figure from static_md chunks."""
        src = io.open("src/cora/known_answer_staleness.py", encoding="utf-8").read()
        assert "withheld everywhere the\nhelper is called" not in src
        assert "NAMED FOLLOW-ON" in src


class TestSpeakerNeutralisation:
    """The first cut rewrote the FIRST bracketed token only, and rewrote tokens
    that are not speaker labels at all."""

    def test_every_speaker_token_is_replaced(self):
        from src.cora.reconciliation_engine import _neutralize_speaker

        out = _neutralize_speaker("[Harrison] We ship. [Tommy] Agreed. [Alina] Yes.", True)
        for name in ("Harrison", "Tommy", "Alina"):
            assert name not in out, name

    def test_structural_header_survives(self):
        from src.cora.reconciliation_engine import _neutralize_speaker

        out = _neutralize_speaker("[Fireflies Meeting] [Harrison] We ship.", True)
        assert "[Fireflies Meeting]" in out
        assert "Harrison" not in out

    def test_markdown_link_survives(self):
        from src.cora.reconciliation_engine import _neutralize_speaker

        out = _neutralize_speaker("See [the doc](https://x.co) and [Harrison] said yes.", True)
        assert "[the doc](https://x.co)" in out
        assert "Harrison" not in out

    def test_numeric_citation_survives(self):
        from src.cora.reconciliation_engine import _neutralize_speaker

        out = _neutralize_speaker("As noted [1], [Harrison] agreed.", True)
        assert "[1]" in out

    def test_untouched_when_flag_absent(self):
        from src.cora.reconciliation_engine import _neutralize_speaker

        text = "[Harrison] We ship."
        assert _neutralize_speaker(text, False) == text


class TestPass4FilterAndRenderAgree:
    def test_prefilter_uses_the_downweighted_confidence(self):
        """The LOW-drop filter read the UN-downweighted value while the rendered
        value was downweighted, so a flagged chunk could pass the filter and then
        emit as LOW -- a confidence this pass otherwise never produces."""
        src = io.open("src/cora/reconciliation_engine.py", encoding="utf-8").read()
        assert src.count('bool(chunk.get("attribution_unreliable"))') >= 2


class TestRunMarkerCountsAreReal:
    """`outputs` is the ONE number the contract exists to provide. A constant
    makes the FIRED-BUT-WROTE-NOTHING alarm structurally unfirable -- the
    'shipped dead in prod' shape, committed inside the fix for it."""

    def test_capture_audit_count_is_not_derived_from_argv(self):
        src = io.open("scripts/run_meeting_capture_audit.py", encoding="utf-8").read()
        assert "outputs=1 + (1 if args.post else 0)" not in src
        assert "findings_count" in src

    def test_blog_pipeline_count_uses_real_report_fields(self):
        from src.cora.f3e_blog.pipeline import RunReport

        src = io.open("scripts/run_f3e_blog_pipeline.py", encoding="utf-8").read()
        # Strip comments first: the fix's own explanatory comment quotes the old
        # expression, and a bare substring pin would match the COMMENT rather
        # than the code -- a pin that cannot fail (my own recorded lesson).
        code = chr(10).join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        assert 'getattr(report, "staged"' not in code
        # the fields it now reads must actually exist
        report = RunReport()
        assert hasattr(report, "staged_gid") and hasattr(report, "proposed")
        assert not hasattr(report, "staged")

    def test_close_pack_marks_its_failure_paths(self):
        src = io.open("scripts/run_finance_close_pack.py", encoding="utf-8").read()
        assert 'outcome="build_failed"' in src
        assert 'outcome="already_sent_this_week"' in src

    def test_close_pack_does_not_credit_a_previous_run(self):
        src = io.open("scripts/run_finance_close_pack.py", encoding="utf-8").read()
        assert "sent_now = len(set(delivered) - set(already))" in src

    @pytest.mark.parametrize("script", [
        "scripts/run_meeting_capture_audit.py",
        "scripts/run_f3e_blog_pipeline.py",
        "scripts/run_finance_close_pack.py",
    ])
    def test_run_marker_import_is_below_the_syspath_bootstrap(self, script):
        """Above it, the import resolves through the editable install and pins to
        the MAIN checkout -- so running from a worktree silently imports the wrong
        module."""
        src = io.open(script, encoding="utf-8").read()
        imp = src.index("from cora import run_marker")
        boot = src.index('sys.path.insert(0, str(_REPO_ROOT / "src"))')
        assert imp > boot, "%s imports run_marker before its sys.path bootstrap" % script


class TestHealthCheckHonesty:
    @pytest.fixture(scope="class")
    def nhc(self):
        spec = importlib.util.spec_from_file_location(
            "nhc_rem", "scripts/nightly_health_check.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["nhc_rem"] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_bracketed_error_level_is_counted(self, nhc):
        """A THIRD live log format uses [%(levelname)s]; its ERROR lines were
        invisible, so the volume metric silently under-counted."""
        assert nhc._ERROR_LINE_RE.search("2026-08-28 03:30:15,081 [ERROR] cora.x: boom")
        assert nhc._ERROR_LINE_RE.search("2026-08-28T03:25:37 ERROR [Thread-2] cora.x: boom")

    def test_prose_still_not_counted(self, nhc):
        assert not nhc._ERROR_LINE_RE.search("2026-08-28T03:25:37 INFO an error occurred")

    def test_no_findings_message_does_not_claim_output_it_never_saw(self, nhc):
        """It reported 'all fired in-window with output' when ZERO markers
        existed -- a false OK, the exact class this check exists to surface."""
        src = io.open("scripts/nightly_health_check.py", encoding="utf-8").read()
        assert "all fired in-window with output." not in src
        assert "awaiting a first marker" in src


class TestPurgeIsSafeOnLiveCanon:
    def test_writes_are_atomic_with_a_backup(self):
        src = io.open("scripts/purge_fixture_pollution_2026-08-30.py", encoding="utf-8").read()
        assert "os.replace(tmp, path)" in src
        assert ".bak-2026-08-30" in src
        assert 'newline="\\n").write' not in src   # no bare truncating LF writes left

    def test_preserves_the_files_own_line_terminator(self):
        src = io.open("scripts/purge_fixture_pollution_2026-08-30.py", encoding="utf-8").read()
        assert '_NL = "\\r\\n" if "\\r\\n" in raw else "\\n"' in src
