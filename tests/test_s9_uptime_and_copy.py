"""S9 stretch items: the uptime parser and the expired-tombstone copy.

Both are cases where the SUITE CERTIFIED THE BUG -- the uptime fixture omitted a
field the producer emits, and nothing pinned the expiry copy at all.
"""
from __future__ import annotations

import io

import pytest

from src.cora.mcp_server import _UPTIME_RE
from src.cora.tools.tool_dispatch import _stash_expired_label


class TestUptimeParserDoesNotAbsorbThePid:
    """cq-bd286f89b357. The old parser sliced everything after "uptime_s=" and
    joined the digits, concatenating the pid onto the uptime: '345678 pid=8844'
    -> 3456788844, i.e. ~109 years, which is what status.json published.

    NOT an epoch or units error, and NOT fixable by clamping -- a clamp hides it
    and is still wrong whenever the concatenation lands in a plausible range.

    Exact regression date: the parser was correct until 2026-08-19, when commit
    35b7e7d added " pid=%d" to the heartbeat line. A log-line FIELD ADDITION broke
    a downstream parser.
    """

    PRODUCTION_LINE = (
        "2026-08-30T17:00:00 INFO [MainThread] cora.main: "
        "heartbeat alive uptime_s=345678 pid=8844"
    )

    def test_pid_is_not_absorbed(self):
        m = _UPTIME_RE.search(self.PRODUCTION_LINE)
        assert m and m.group(1) == "345678"
        assert int(m.group(1)) != 3456788844

    def test_the_old_digit_scrape_would_have_failed_this(self):
        """Documents the defect shape so a future refactor cannot reintroduce it."""
        idx = self.PRODUCTION_LINE.find("uptime_s=")
        tail = self.PRODUCTION_LINE[idx + len("uptime_s="):].strip()
        old_result = int("".join(ch for ch in tail if ch.isdigit()) or "0")
        assert old_result == 3456788844          # the bug, reproduced
        assert int(_UPTIME_RE.search(self.PRODUCTION_LINE).group(1)) == 345678

    def test_line_without_pid_still_parses(self):
        """Backwards compatible with pre-8/19 logs."""
        m = _UPTIME_RE.search("... cora heartbeat alive uptime_s=93780")
        assert m and m.group(1) == "93780"

    def test_future_trailing_fields_are_harmless(self):
        """Anchoring on the digits immediately after the key is what makes the
        next field addition a non-event."""
        m = _UPTIME_RE.search("heartbeat alive uptime_s=42 pid=1 threads=9 rss_mb=120")
        assert m and m.group(1) == "42"

    def test_fixture_matches_the_producer_format(self):
        """The suite was green over this bug because the fixture omitted ' pid=N'.
        A fixture that does not match the producer's format pins the bug."""
        src = io.open("tests/test_session_snapshots.py", encoding="utf-8").read()
        assert "heartbeat alive uptime_s=93780 pid=8844" in src, (
            "the session-snapshots fixture must carry the pid field the producer "
            "emits, or it cannot exercise the concatenation bug"
        )


def _render(label: str) -> str:
    """Mirror of the app.py consumer sentence."""
    return f"{label[:1].upper()}{label[1:]} expired before you confirmed."


class TestExpiredCopyHasNoDoubledArticle:
    """cq-38faa8bd62a1. The sentence was "That {label} expired", and 13 of the 16
    labels already begin with "that " -> "That that note expired". Nothing pinned
    this copy, so nothing caught it."""

    THAT_KINDS = [
        "code_queue", "delegated", "lexicon", "remember", "forget_note",
        "schedule_meeting", "gmail_draft", "hubspot_stage", "hubspot_note",
        "slack_dm", "influencer_handle", "influencer_deliverable", "meeting_item",
    ]

    @pytest.mark.parametrize("kind", THAT_KINDS + ["unregistered_kind"])
    def test_no_doubled_article(self, kind):
        out = _render(_stash_expired_label(kind, {}))
        assert "That that" not in out
        assert "that that" not in out.lower()

    @pytest.mark.parametrize("kind", THAT_KINDS)
    def test_sentence_starts_capitalised(self, kind):
        assert _render(_stash_expired_label(kind, {})).startswith("That ")

    def test_quoted_label_shapes_survive_capitalisation(self):
        """asana/calendar emit a quoted name; upper() on a quote is a no-op, so
        the quote must still lead."""
        for kind, entry in (
            ("asana", {"action": "create", "title": "Fix the bug"}),
            ("calendar", {"summary": "Standup"}),
        ):
            out = _render(_stash_expired_label(kind, entry))
            assert out.startswith('"'), out
            assert "That that" not in out

    def test_app_consumer_no_longer_hardcodes_the_article(self):
        src = io.open("src/cora/app.py", encoding="utf-8").read()
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        assert 'f"That {label} expired' not in code

    def test_shared_label_helper_untouched(self):
        """The fix must stay at the consumer: _expired_pending_label is shared with
        the typed-confirm path, where it reads correctly inside parentheses."""
        src = io.open("src/cora/tools/tool_dispatch.py", encoding="utf-8").read()
        assert "def _expired_pending_label(" in src
        assert 'labels.get(kind, "that request")' in src
