"""Fireflies `duration` is MINUTES, not seconds (S3, cq-f52c6b691127).

WHY THIS FILE EXISTS. `grep -rn 'Duration:' tests/` returned NOTHING before this
session: the rendered duration line had no assertion anywhere, and the two
fixtures that supplied a duration actively encoded the wrong unit (1800 and 3600,
i.e. 30 and 60 minutes expressed as seconds). So a green 12,676-test suite sat on
top of a defect that had corrupted every Fireflies chunk ever written.

MEASURED ON THE LIVE KB, three independent ways, before the fix:
  * "HJR 13 wcf Meeting" carries duration=64 while its own inline action-item
    timestamps reach 54m30s -- impossible if 64 were seconds.
  * "Harrison x Alex" carries duration=31, and a speaker says "it's a 30 minute
    meeting" inside that very transcript.
  * Across the whole corpus no value exceeds 240, which no seconds reading could
    produce for a 2h56m call.
And the consequence, also measured: the only duration values ever rendered in the
KB were "1 min" (112 chunks) and "2 min" (6). Every sub-hour meeting -- the
majority -- rendered no Duration line at all, because `int(31/60)` is 0 and the
line was truthiness-gated.
"""

from __future__ import annotations

from cora.connectors import fireflies_connector as ffc


def _transcript(duration):
    return {
        "id": "T1",
        "title": "HJR 13 wcf Meeting",
        "date": 1787097600,
        "duration": duration,
        "organizer_email": "harrison@hjrglobal.com",
        "meeting_attendees": [{"displayName": "Harrison Rogers",
                               "email": "harrison@hjrglobal.com"}],
        "sentences": [{"index": 0, "speaker_name": "Harrison Rogers",
                       "text": "Opening remarks."}],
        "summary": {},
    }


def test_a_31_minute_meeting_renders_31_minutes():
    """The regression that mattered most: before the fix this rendered NO
    duration line at all, because int(31/60) == 0."""
    out = ffc._format_transcript_content(_transcript(31))
    assert "Duration: 31 min" in out


def test_a_64_minute_meeting_does_not_render_as_one_minute():
    out = ffc._format_transcript_content(_transcript(64))
    assert "Duration: 64 min" in out
    assert "Duration: 1 min" not in out


def test_a_long_meeting_renders_its_real_length():
    """175.68 is a real live value (2h56m). It used to render "Duration: 2 min"."""
    out = ffc._format_transcript_content(_transcript(175.68))
    assert "Duration: 176 min" in out


def test_a_fractional_duration_is_rounded_not_truncated():
    """23.030000686645508 is a real live value -- Fireflies returns a Float."""
    assert "Duration: 23 min" in ffc._format_transcript_content(_transcript(23.030000686645508))


def test_a_missing_or_zero_duration_renders_no_duration_line():
    for value in (None, 0, 0.0):
        assert "Duration:" not in ffc._format_transcript_content(_transcript(value))


def test_a_junk_duration_never_raises():
    """A connector must degrade, not crash, on a schema surprise."""
    for value in ("not a number", {}, []):
        out = ffc._format_transcript_content(_transcript(value))
        assert "Duration:" not in out
        assert "[Fireflies Meeting]" in out


def test_the_metadata_key_no_longer_asserts_the_wrong_unit():
    """`duration_sec` was renamed to `duration_min`. Verified safe before the
    rename: the key had ZERO readers anywhere in src/, scripts/ or tests/ -- and
    a key whose NAME states the wrong unit is how the next reader repeats the
    bug."""
    import inspect
    source = inspect.getsource(ffc)
    assert '"duration_min": t.get("duration")' in source
    assert '"duration_sec": t.get("duration")' not in source
