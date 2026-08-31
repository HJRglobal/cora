"""Health-check reporting defects (session #11 S6, cq-f7ec95e2d313 + cq-b2dee156caee).

WHAT THE SEED ASKED FOR vs WHAT WAS ACTUALLY WRONG. The seeds read as three
separate incidents -- a health-ping DNS failure, 57 ERROR lines across 40 files in
24h, and a kb-sync-fireflies non-zero exit. Verification showed they are ONE
~8-minute network blip on 2026-08-28 (03:22-03:30 AZ) that the bot self-healed
without a restart (same pid, heartbeat never missed a 60s beat). There is no
ongoing outage to engineer against.

What IS broken is the REPORTING -- three defects that turned one blip into a
CRITICAL alarm, a fabricated 24h statistic, and an invented recurrence claim that
became a queue seed. Those are fixed; DNS resilience for a self-resolving
8-minute event was deliberately NOT built.
"""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

_SCRIPT = Path("scripts/nightly_health_check.py")


@pytest.fixture(scope="module")
def nhc():
    spec = importlib.util.spec_from_file_location("nhc_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nhc_under_test"] = mod  # @dataclass needs the module registered
    spec.loader.exec_module(mod)
    return mod


class TestFatalWordBoundary:
    """r"\\bFATAL\\b" matches the word "non-fatal" -- the hyphen IS a word
    boundary. health_endpoint logs "ping failed (non-fatal)" on every ping
    failure, so a benign self-healing blip was escalated to a CRITICAL alarm by
    the reassurance word in its own message."""

    LIVE_LINE = (
        "health-ping: ping failed (non-fatal): "
        "<urlopen error [Errno 11001] getaddrinfo failed>"
    )

    def test_the_exact_live_line_is_not_critical(self, nhc):
        assert not nhc._CRITICAL_RE.search(self.LIVE_LINE)

    @pytest.mark.parametrize("text", [
        "non-fatal condition",
        "this failure is non-fatal and self-healing",
    ])
    def test_non_fatal_never_matches(self, nhc, text):
        assert not nhc._CRITICAL_RE.search(text)

    @pytest.mark.parametrize("text", [
        "FATAL: cannot open database",
        "fatal error during startup",
        "2026-08-28T03:25:37 CRITICAL cora: FATAL shutdown",
    ])
    def test_genuine_fatal_still_matches(self, nhc, text):
        """The fix must not blunt the alarm it narrows."""
        assert nhc._CRITICAL_RE.search(text)

    def test_lookbehind_is_fixed_width_and_works_at_position_zero(self, nhc):
        """The patterns are joined into one alternation; a variable-width
        lookbehind would raise at compile time. Width 1 must also succeed at the
        very start of a string."""
        assert nhc._CRITICAL_RE.search("FATAL at position zero")


class TestErrorVolumeIsALineCountInWindow:
    """The tally was text.count(" ERROR ") computed OUTSIDE the per-line loop:
    neither a line count nor last-24h. It summed the whole life of a
    start-date-pinned log file and matched any JSON payload containing the
    substring -- including the health check's own "N ERROR lines" report."""

    def test_matches_both_live_log_formats(self, nhc):
        assert nhc._ERROR_LINE_RE.search(
            "2026-08-28T03:25:37 ERROR [Thread-2 (_run)] cora.x: boom")   # bot
        assert nhc._ERROR_LINE_RE.search(
            "2026-08-28 03:30:15,081 ERROR cora.y: boom")                 # scripts

    def test_level_field_only_not_substring(self, nhc):
        """The self-count that inflated the metric: an INFO line whose JSON
        payload contains the words 'ERROR lines'."""
        info_with_payload = (
            "2026-08-29T09:49:17 INFO [ThreadPoolExecutor-0_1] cora.claude_client: "
            "tool_use input={'request': \"Health Check: 57 ERROR lines across 40 files\"}"
        )
        assert " ERROR " in info_with_payload          # the old counter fired here
        assert not nhc._ERROR_LINE_RE.search(info_with_payload)

    def test_unstamped_lines_are_not_counted(self, nhc):
        """Deliberate asymmetry with the critical scan, which KEEPS unstamped
        lines because tracebacks are what a critical needs. For a volume metric
        that rule would admit an unbounded backlog."""
        assert not nhc._ERROR_LINE_RE.search("    File \"x.py\", line 1, in <module>")
        assert not nhc._ERROR_LINE_RE.search("ERROR without a timestamp")

    def test_not_case_insensitive(self, nhc):
        """Ordinary prose containing 'error' must not count."""
        assert not nhc._ERROR_LINE_RE.search("2026-08-28T03:25:37 INFO an error occurred")

    @pytest.mark.skipif(
        not Path("logs/cora-2026-08-26.log.2026-08-28").exists(),
        reason="known 8/28 outage corpus not present on this host",
    )
    def test_known_corpus_counts_exactly_56(self, nhc):
        """Ground truth: the 8/28 blip produced exactly 56 real ERROR lines.
        The count must be 56 -- not 57 (self-count) and not 63 (payloads)."""
        text = io.open("logs/cora-2026-08-26.log.2026-08-28",
                       encoding="utf-8", errors="replace").read()
        n = sum(1 for ln in text.splitlines() if nhc._ERROR_LINE_RE.search(ln))
        assert n == 56


class TestNoFabricatedRecurrenceClaim:
    """The warn line appended "- this repeats silently every run" to EVERY
    nonzero task result with no recurrence check. The check reads a single Last
    Task Result, so it cannot know that. The invented claim is the direct source
    of a false premise that became a queue seed."""

    def test_recurrence_boilerplate_is_gone(self):
        src = io.open(_SCRIPT, encoding="utf-8").read()
        # Only the explanatory comment may mention the retired wording.
        code = "\n".join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
        )
        assert "this repeats silently every run" not in code

    def test_replacement_states_its_own_limit(self):
        src = io.open(_SCRIPT, encoding="utf-8").read()
        assert "most recent run only" in src
        assert "does not see run history" in src
