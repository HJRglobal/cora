"""Code #14 D-051 ROUND 3 (scope A): regressions the round-2 remediation introduced in
the honesty rails, found by the final reproduce-to-report check.

  R2-A2   a participle + ':' is a metadata LABEL only when the date is the WHOLE value
          (round 2 bailed on any value that merely opened with a date or a bare year).

Every sentence in the must-fire lists below is a repro that wrote NO counted line at
the round-2 tip (29d9212) and one at the pre-round-2 base (c14-backup-pre-r2).
"""

from __future__ import annotations

import logging
import re

import pytest

from _timing import best_of_3
from cora import capability_set as cs
from cora import slack_egress as se

HARRISON = "U0B2RM2JYJ1"
KNOWN_CQ = frozenset({"cq-a24f9d2210fc", "cq-0123456789ab"})


@pytest.fixture(autouse=True)
def observe(monkeypatch, tmp_path):
    monkeypatch.delenv("CORA_SENTINEL_ENFORCE", raising=False)
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    monkeypatch.setattr(cs, "LADDER_REGISTRY_PATH", tmp_path / "no-registry.yaml")
    monkeypatch.setattr(se, "_ID_LEDGERS", (
        ("cq", re.compile(r"\bcq-[0-9a-f]{12}\b", re.IGNORECASE), lambda: KNOWN_CQ),
        ("dw", re.compile(r"\bdw-[0-9a-f]{12}\b", re.IGNORECASE), lambda: frozenset()),
    ))
    se._MODE_WARNED.clear()


def _msgs(caplog, key, kind):
    return [r.getMessage() for r in caplog.records
            if r.getMessage().startswith(f"{key} kind={kind}")]


def _write(text, *, user_text="", prior=(), count=0):
    return se.screen_phantom_write_claims(text, tool_use_count=count, channel_name="dm",
                                          user_id=HARRISON, user_text=user_text,
                                          prior_user_texts=list(prior))


# ── R2-A2: a ':' label bails only when the date is the WHOLE value ────────────
R2A2_MUST_FIRE = [
    # the six repros (tool_use=0, base counted every one, round 2 counted none)
    "Created: 9/24 at 2pm with Justin -- the invite is on both calendars.",
    "*Created:* 9/24 2pm call with Justin",
    "Updated: 10/31 is the new close date on the Sprouts deal.",
    "Filed: 9/23 Cox invoice to the Receipts & Invoices Inbox.",
    "Queued: 9/29 Monday menu with the 3 Kroger cards.",
    "Updated: 2026 budget tab now shows the new totals.",
    # the same rule on the neighbouring shapes
    "Updated: (per your ask) the kickoff prompt for the Kroger build.",
    "Created: March 2025 kickoff deck for the Kroger buyer.",
    "Updated: Sept 1st pricing on the Sprouts deal.",
]
# Accepted over-trips (fail toward FIRING): a label with anything after its date.
R2A2_ACCEPTED_OVER_TRIPS = [
    "Updated: 9/12 3:14pm",
    "Created: 2024-03-01 by Justin",
]
R2A2_MUST_NOT_FIRE = [
    # a date (or "(per ...)" note) that is the whole value, then the end of the line
    "Created: March 1, 2025", "Updated: Sept 1st, 2026", "**Updated: 9/12**", "Created: 2025",
    "Updated: (per the doc footer)", "Updated: 9/12 (per the doc footer)\nThe lease is unchanged.",
    "Created: March 2025 (per the doc properties)",
]


class TestLabelIsTheWholeValueR3:
    @pytest.mark.parametrize("text", R2A2_MUST_FIRE)
    def test_a_date_led_claim_is_not_a_label(self, text):
        hit = se._find_write_claim(text)
        assert hit is not None and hit[1] == "initial", (text, hit)

    @pytest.mark.parametrize("text", R2A2_MUST_FIRE[:6])
    def test_the_repros_write_a_counted_line(self, caplog, text):
        caplog.set_level(logging.WARNING, logger=se.__name__)
        _write(text, user_text="set up the Kroger sync and fix the deal")
        hits = _msgs(caplog, se.PHANTOM_LOG_KEY, "lexicon")
        assert len(hits) == 1 and "form=initial" in hits[0], (text, hits)

    @pytest.mark.parametrize("text", R2A2_ACCEPTED_OVER_TRIPS)
    def test_a_label_with_a_tail_after_its_date_fires(self, text):
        """Documented over-trips: the rule reads the WHOLE value, never a date prefix."""
        assert se._find_write_claim(text) is not None, text

    @pytest.mark.parametrize("text", R2A2_MUST_NOT_FIRE)
    def test_a_whole_value_date_label_never_fires(self, text):
        assert se._find_write_claim(text) is None, (text, se._find_write_claim(text))

    def test_every_round_2_label_row_still_passes(self):
        from test_d051_r2_honesty_rails import MUST_FIRE_R2, MUST_NOT_FIRE_R2
        assert [t for t in MUST_NOT_FIRE_R2 if se._find_write_claim(t) is not None] == []
        assert [(t, f) for t, f in MUST_FIRE_R2
                if (se._find_write_claim(t) or ("", ""))[1] != f] == []

    @pytest.mark.parametrize("shape", [
        "Updated: 9/12" + " " * 40000 + "x", "Updated: 9/12 (" + "x" * 40000,
        "Updated: 9/12 (" + "x" * 70 + ")" + " " * 40000 + "y", "Updated: March 1," + " " * 40000,
        "Updated: (per " + "x " * 20000 + ")", "Updated: 9/12" + "*" * 40000,
        "Created: March 1, " * 2200, ("Updated: 9/12 (per x) y\n" * 1700),
        "Updated: (per x)" + " " * 40000 + "9/12 y", "Updated: " + "March 1, 2025 " * 2800,
    ], ids=["tail_sp", "open_paren", "paren_sp", "month_comma_sp", "per_run", "stars", "month_comma_rep",
            "label_tail_lines", "per_sp_date", "month_year_rep"])
    def test_the_whole_value_rule_is_linear_at_40k(self, shape):
        """D-171: _WC_LABEL_VALUE_END / the month-year _WC_DATE extension at 40k."""
        rx = dict(se._WRITE_CLAIM_FORMS)["initial"]
        assert best_of_3(lambda: list(rx.finditer(shape))) < 0.2
        assert best_of_3(lambda: se._find_write_claim(shape)) < 0.5
