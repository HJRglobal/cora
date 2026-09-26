"""Code #16 C2 -- D-051 ROUND-3 regressions for the travel lane (fixer R3B).

Every test drives the REAL code path: the pure predicates / parser / sanitizer on
Slack text, route_turn on a real (tmp-redirected) lane-thread store, or the real
handle_message_event / _dispatch_qa entry points with only infrastructure stubbed
(the wiring module's ``lane`` fixture). Guest names are SYNTHETIC ("Jordan
Riverstone", "Mike Jones"). Every table carries its precision rows next to its recall
rows, and every new regex is timed on the whitespace-only 40k input and its
neighbours. Invisible / control characters are written as ESCAPES, never literally.
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from cora import travel_shortlist as ts
from test_travel_shortlist import NOW, _fx, _msg

HARRISON = "U0B2RM2JYJ1"
TODAY = date(2026, 9, 25)


def _best_of_3(fn) -> float:
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


# ── r3:c2-injection-card#0: C1 controls never split a booking word on the card ──

class TestC1ControlsOnTheCard:
    def test_the_finding_repro_end_to_end(self):
        """validate_options + render_card: 'Boo<U+0081>ked ... Con<U+0090>firmed' used to
        reach the T0 card verbatim (a Chromium client draws C1 as nothing)."""
        urls, _e = ts.collect_record_urls([_msg(_fx())])
        raw = [{"property": "Hotel Valley Ho", "nightly_rate": "$250", "kind": "hotel",
                "url": "https://hotelvalleyho.com/",
                "fit_note": "Boo\x81ked for your team of 4 - Con\x90firmed"}]
        opts, dropped = ts.validate_options(raw, urls)
        assert dropped == 0 and len(opts) == 1
        _t, blocks = ts.render_card(ts.parse_constraints(
            "find hotels in scottsdale oct 17-21", today=TODAY).constraints, opts, now=NOW)
        body = blocks[1]["text"]["text"]
        assert not any(0x80 <= ord(ch) <= 0x9F for ch in body), repr(body)
        seen = body.lower()
        assert "booked" not in seen and "confirmed" not in seen, repr(body)

    @pytest.mark.parametrize("raw,want", [
        ("Caf\xe9 Monarch", "Caf\xe9 Monarch"),          # Latin-1 letters just above C1 stay
        ("Diner \xa9 2026", "Diner \xa9 2026"),
        ("pool\x9fside", "pool side"),                  # a C1 control is a SPACE, never deleted
    ])
    def test_precision_latin1_letters_are_kept(self, raw, want):
        import unicodedata
        assert ts.sanitize_field(raw, 160) == unicodedata.normalize("NFKC", want)

    def test_the_control_pass_is_linear(self):
        for shape in (" " * 40000, "\x81" * 40000, "a\x90" * 20000, "Boo\x81ked " * 4000):
            assert _best_of_3(lambda: ts._CTRL_RE.sub(" ", shape)) < 0.05, repr(shape[:3])
            assert _best_of_3(lambda: ts.sanitize_field(shape, 160)) < 0.05, repr(shape[:3])
