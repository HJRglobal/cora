"""Pins the arrow/entity rendering invariant -- and records why two reported
defects were NOT defects, so the class stops being re-flagged.

BACKGROUND. The `cora-slack-clarity-weekly-check` task flagged two HIGH items
(seeded as cq-082174dc05fd and cq-fc4e595b8a60) across the 8/08 and 8/15 runs:

  * every entity daily-synthesis "Needs you" line rendering an entity-pair
    decision as literal text `F3E:left_right_arrow:HJRPROD`, and
  * inventory-confirm + filing-digest lines rendering `-&gt;` instead of `->`.

BOTH ARE ARTIFACTS OF READING SLACK BACK THROUGH ITS API, not user-visible bugs.
Slack's read-back normalizes emoji-presentation Unicode to its shortcode and
HTML-escapes `&`, `<`, `>`; the CLIENT renders both correctly. The audit sessions
read via the Slack API, so they saw the transport form and reported it as the
rendered form.

PROOF (2026-08-17, byte-level, same generation of the same synthesis):
  Cora's own persisted copy, 00-Founder/_daily-synthesis/2026-08/
  2026-08-17_fndr_daily-synthesis.md line 35:
      "- F3E<U+2194>HJRPROD RP receivable treatment - open 72d, untouched 72d"
  The same synthesis as read back from #founder-operations (ts 1786973608.397629):
      "- F3E:left_right_arrow:HJRPROD RP receivable treatment \\u2014 open 72d..."
  Every other character matches, and the EM DASH survived read-back as \\u2014 --
  non-emoji Unicode is preserved, emoji-presentation Unicode is not. Corroborated
  in the same window by #cora-filing (ts 1786971690.966229), where a WORKING
  Google Drive URL comes back with `&amp;ouid=` -- read-back escaping is cosmetic.

MEASURED RESIDUAL, accepted not fixed: Cora's Slack ingest stores the transport
form, so 115 of 613,182 KB chunks (0.019%) hold `:left_right_arrow:` and 1,446
(0.24%) hold `&gt;`. Nearly all are Cora's own bot-authored posts, which the
2026-08-01 bot_authored tagging already excludes from gap_autofill /
friction_mining / reconciliation. Adding a de-shortcode pass at ingest would put
a new transform on a 613K-chunk pipeline to fix 0.02%; not worth the regression
surface.

WHAT THIS FILE ACTUALLY GUARDS. There is one way this class could become a REAL
user-visible bug: DOUBLE-escaping. Slack treats `&gt;` as the escaped form of
`>`, so a single escape is invisible -- but `&amp;gt;` renders literally as
`&gt;`. The egress boundary must therefore stay transparent to arrows and
entities, and the builders must keep emitting plain characters. If someone
"fixes" a future weekly-check report by writing a shortcode into a template or
adding html.escape() to the egress path, these tests fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import slack_egress  # noqa: E402

U_LEFT_RIGHT_ARROW = "↔"
U_EM_DASH = "—"


class TestEgressIsTransparent:
    """sanitize_text runs on EVERY outbound Slack message. It must never escape
    or shortcode-ify, or a single escape upstream becomes a visible double."""

    @pytest.mark.parametrize("text", [
        "130 -> 128 cases",
        f"F3E{U_LEFT_RIGHT_ARROW}HJRPROD RP receivable treatment",
        f"open 72d {U_EM_DASH} untouched 72d",
        "a & b",
        "x > y",
        "5 <= 6 >= 4",
        "<https://drive.google.com/file/d/1zJ/view?a=1&ouid=2|report.xlsx> -> `path`",
    ])
    def test_arrows_and_entities_pass_through_unchanged(self, text):
        assert slack_egress.sanitize_text(text) == text

    def test_does_not_convert_unicode_arrow_to_a_shortcode(self):
        out = slack_egress.sanitize_text(f"F3E{U_LEFT_RIGHT_ARROW}HJRPROD")
        assert ":left_right_arrow:" not in out
        assert U_LEFT_RIGHT_ARROW in out

    def test_does_not_html_escape(self):
        out = slack_egress.sanitize_text("130 -> 128 and a & b")
        assert "&gt;" not in out
        assert "&amp;" not in out

    def test_already_escaped_text_is_not_double_escaped(self):
        # The real failure mode: `&gt;` -> `&amp;gt;` would render literally.
        assert slack_egress.sanitize_text("130 -&gt; 128") == "130 -&gt; 128"
        assert slack_egress.sanitize_text("a &amp; b") == "a &amp; b"

    def test_idempotent(self):
        text = f"F3E{U_LEFT_RIGHT_ARROW}HJRPROD -> 128 cases & more"
        once = slack_egress.sanitize_text(text)
        assert slack_egress.sanitize_text(once) == once


class TestBuildersEmitPlainCharacters:
    """No Slack-bound builder should hand Slack a pre-escaped entity or a raw
    emoji shortcode where a plain character is meant."""

    def test_filing_digest_uses_a_plain_arrow(self):
        src = (_REPO_ROOT / "src" / "cora" / "connectors"
               / "attachment_filer.py").read_text(encoding="utf-8")
        assert " -> " in src, "the file-move line should build a plain '->'"
        assert "-&gt;" not in src, "a pre-escaped arrow would double-escape"

    def test_no_builder_hardcodes_the_arrow_shortcode(self):
        # finance_close legitimately uses :left_right_arrow: as a SECTION ICON
        # (an emoji is intended there). Everything else should not.
        offenders = []
        for path in (_REPO_ROOT / "src").rglob("*.py"):
            if path.name == "finance_close.py":
                continue
            if ":left_right_arrow:" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        assert not offenders, (
            f"{offenders} hardcode ':left_right_arrow:'. If this came from a "
            f"weekly-check report, read this module's docstring first -- the "
            f"reported artifact is Slack API read-back, not a render bug."
        )
