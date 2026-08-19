"""Long outbound messages never end mid-word (cq-64a8f5e3e654).

Reproduces each cited case: the briefing mid-word cut-offs ("How can I",
"...priorit"), the strategy memo splitting "for" across two Slack messages, and
the family's shared root -- a max_tokens stop that raises nothing and a naive
character slice at the Slack boundary.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cora import long_message as lm

_REPO_ROOT = Path(__file__).resolve().parents[1]


class _Block:
    def __init__(self, text):
        self.text = text


def _resp(text, stop_reason="end_turn"):
    return SimpleNamespace(content=[_Block(text)], stop_reason=stop_reason)


class FakeAnthropic:
    """Returns the queued responses in order."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._queue:
            return _resp("")
        return self._queue.pop(0)


class FakeSlack:
    def __init__(self, fail_on=None):
        self.posted = []
        self.fail_on = fail_on

    def chat_postMessage(self, **kwargs):
        if self.fail_on is not None and len(self.posted) == self.fail_on:
            self.posted.append(None)
            raise RuntimeError("slack down")
        self.posted.append(kwargs)
        return {"ok": True}


# ── splitting ───────────────────────────────────────────────────────────────
class TestSplitting:
    def test_short_text_is_one_chunk(self):
        assert lm.split_for_slack("hello") == ["hello"]

    def test_empty_yields_nothing_to_post(self):
        assert lm.split_for_slack("") == []
        assert lm.split_for_slack("   \n  ") == []

    def test_never_splits_mid_word(self):
        """The memo defect: `text[:39000]` split the word "for"."""
        words = ["reconciliation"] * 400
        text = " ".join(words)
        chunks = lm.split_for_slack(text, limit=200)
        assert len(chunks) > 1
        for c in chunks:
            for token in c.split():
                assert token == "reconciliation", f"broken token: {token!r}"

    def test_never_splits_mid_link(self):
        """The plate defect: a reply ending in a malformed half-link."""
        link = "<https://example.test/a/very/long/path/that/goes/on|the filed report>"
        text = ("filler word " * 40) + link + (" trailing word" * 40)
        chunks = lm.split_for_slack(text, limit=120)
        assert any(link in c for c in chunks), "the link was split across chunks"

    def test_prefers_line_boundaries(self):
        text = "\n".join(f"line {i} with some content" for i in range(40))
        for c in lm.split_for_slack(text, limit=200):
            assert not c.startswith(" ")
            for line in c.split("\n"):
                assert line.startswith("line ")

    def test_falls_back_to_sentence_then_word_within_a_long_line(self):
        line = "Alpha beta gamma. Delta epsilon zeta. Eta theta iota. " * 12
        chunks = lm.split_for_slack(line, limit=120)
        assert len(chunks) > 1
        assert all(len(c) <= 120 for c in chunks)
        assert " ".join(chunks).split() == line.split()

    def test_one_token_longer_than_the_limit_still_terminates(self):
        """A pathological input must not loop forever; a hard cut is reachable
        only for a token longer than the entire limit, which no real link is."""
        chunks = lm.split_for_slack("x" * 500, limit=100)
        assert len(chunks) == 5
        assert "".join(chunks) == "x" * 500

    def test_no_content_is_lost(self):
        text = "\n".join(f"row {i}: value {i}" for i in range(300))
        assert " ".join(lm.split_for_slack(text, limit=250)).split() == text.split()

    def test_every_chunk_is_within_the_limit(self):
        text = "\n".join("a fairly long line of report content here" for _ in range(200))
        assert all(len(c) <= 300 for c in lm.split_for_slack(text, limit=300))


# ── posting ─────────────────────────────────────────────────────────────────
class TestPostLong:
    def test_single_message_carries_no_continuation_label(self):
        client = FakeSlack()
        assert lm.post_long(client, "C1", "short") == 1
        assert "continued" not in client.posted[0]["text"]

    def test_multi_part_labels_each_part(self):
        """Without the label, part 2 opening mid-sentence reads as a glitch
        rather than as the rest of one message."""
        client = FakeSlack()
        text = "\n".join(f"line {i}" for i in range(200))
        n = lm.post_long(client, "C1", text, limit=200)
        assert n > 1
        assert all("continued" in p["text"] for p in client.posted)
        assert f"continued 1/{n}" in client.posted[0]["text"]

    def test_nothing_posted_for_empty_text(self):
        client = FakeSlack()
        assert lm.post_long(client, "C1", "") == 0
        assert client.posted == []

    def test_thread_ts_is_threaded_through(self):
        client = FakeSlack()
        lm.post_long(client, "C1", "hi", thread_ts="123.456")
        assert client.posted[0]["thread_ts"] == "123.456"

    def test_a_failed_part_does_not_abort_the_rest(self):
        """Delivering 3 of 4 parts beats delivering none."""
        client = FakeSlack(fail_on=0)
        text = "\n".join(f"line {i}" for i in range(200))
        assert lm.post_long(client, "C1", text, limit=200) > 0


# ── truncation detection + continuation ─────────────────────────────────────
class TestTruncation:
    def test_detects_a_max_tokens_stop(self):
        assert lm.was_truncated(_resp("x", "max_tokens")) is True
        assert lm.was_truncated(_resp("x", "end_turn")) is False
        assert lm.was_truncated(SimpleNamespace()) is False

    def test_continues_a_cut_off_reply(self):
        """The briefing case: "...How can I" -> continued to a whole sentence."""
        client = FakeAnthropic(_resp(" help you today?", "end_turn"))
        text, complete = lm.complete_truncated(
            client, model="m", system=None, messages=[{"role": "user", "content": "q"}],
            first_text="Here is your day. How can I",
            first_response=_resp("...", "max_tokens"), max_tokens=100)
        assert complete is True
        assert text == "Here is your day. How can I help you today?"

    def test_a_complete_reply_makes_no_extra_call(self):
        client = FakeAnthropic()
        text, complete = lm.complete_truncated(
            client, model="m", system=None, messages=[], first_text="done",
            first_response=_resp("done", "end_turn"), max_tokens=100)
        assert (text, complete) == ("done", True)
        assert client.calls == []

    def test_continuation_is_bounded(self):
        """An unbounded continue loop turns one over-long day into an unbounded
        bill, on surfaces where nobody is watching the spend."""
        client = FakeAnthropic(*[_resp("more ", "max_tokens") for _ in range(10)])
        text, complete = lm.complete_truncated(
            client, model="m", system=None, messages=[], first_text="start ",
            first_response=_resp("start ", "max_tokens"), max_tokens=100,
            max_continuations=2)
        assert len(client.calls) == 2
        assert complete is False

    def test_incomplete_output_is_labelled_not_delivered_silently(self):
        assert "cut short" in lm.TRUNCATION_NOTICE

    def test_a_failing_continuation_returns_what_it_has(self):
        """Never an exception into a scheduled job."""
        class Boom:
            messages = property(lambda self: self)

            def create(self, **kw):
                raise RuntimeError("api down")

        text, complete = lm.complete_truncated(
            Boom(), model="m", system=None, messages=[], first_text="partial",
            first_response=_resp("partial", "max_tokens"), max_tokens=100)
        assert text == "partial" and complete is False

    def test_continuation_prompt_forbids_repeating(self):
        assert "do not repeat" in lm.CONTINUE_PROMPT.lower()
        assert "mid-word" in lm.CONTINUE_PROMPT.lower()


# ── the family is actually wired ────────────────────────────────────────────
def _code_only(path: Path) -> str:
    """Executable lines only.

    A naive substring pin over the whole file matches the COMMENT that explains
    why the bad pattern was removed, so it passes on a file that still does it
    (and fails on one that documents the fix). Third time this class has bitten
    this session -- read code, not prose.
    """
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


class TestFamilyWiring:
    def test_hot_path_detects_truncation_at_the_shared_chokepoint(self):
        """Two create-loops exist; wiring one and missing the other is how this
        class survives. _log_usage is the call both already funnel through."""
        src = _code_only(_REPO_ROOT / "src" / "cora" / "claude_client.py")
        assert 'stop_reason", None) == "max_tokens"' in src
        assert src.count("_log_usage(response, iteration)") \
            + src.count("_log_usage(final, iteration)") == 2

    def test_strategy_memo_continues_and_splits(self):
        src = _code_only(_REPO_ROOT / "src" / "cora" / "strategy_memo.py")
        assert "complete_truncated(" in src
        assert "split_for_slack(" in src
        assert "text[:39000]" not in src

    def test_channel_synthesis_splits_on_boundaries(self):
        src = _code_only(_REPO_ROOT / "src" / "cora" / "channel_synthesis.py")
        assert "split_for_slack(" in src
        assert "[:_MAX_SLACK_CHARS]" not in src

    def test_briefing_cap_is_sized_and_continued(self):
        src = _code_only(_REPO_ROOT / "scripts" / "run_daily_briefing.py")
        assert "_BRIEFING_MAX_TOKENS" in src
        assert "max_tokens=600" not in src
        assert "complete_truncated(" in src

    def test_positive_control_short_content_is_unchanged(self):
        """7/28 and 7/30 completed fine; the fix must not alter that path."""
        client = FakeSlack()
        lm.post_long(client, "C1", "A short complete briefing.")
        assert client.posted[0]["text"] == "A short complete briefing."
