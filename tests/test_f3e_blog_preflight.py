"""Deterministic claims preflight for F3E blog copy (S3 of the publish lane).

Every rail is tested in BOTH directions -- a violation is blocked AND the cleared
phrasing the checklist itself approves is not. A one-directional test on a
fail-closed guard proves nothing useful: a rail that blocks everything passes it.
"""

from __future__ import annotations

import time

import pytest

from cora.f3e_blog import preflight as pf


def _run(title="Test Title", summary="", body="<p>Body copy.</p>"):
    return pf.run_preflight(title=title, summary=summary, body_html=body)


# ---------------------------------------------------------------------------
# Real cleared copy must pass (the regression that matters most)
# ---------------------------------------------------------------------------

# Verbatim from the article published 2026-08-26 and cleared by Harrison. It puts
# "Clean" and "Energy" three words apart, so any naive proximity rule blocks it.
LIVE_CLEARED_TITLE = "Clean Energy Drinks for Yoga, Pilates, and Everyday Active Life"


def test_live_cleared_title_passes():
    r = _run(title=LIVE_CLEARED_TITLE)
    assert r.passed, r.render()


def test_category_noun_is_not_the_brand_line():
    # "Energy drinks" (category) vs "F3 Energy" (brand line) is the whole rail-2
    # distinction. Getting it wrong blocks every energy-drink article ever written.
    assert pf.brand_lines_in("Energy drinks are popular.") == set()
    assert pf.brand_lines_in("energy is what you need.") == set()
    assert pf.brand_lines_in("Clean Energy Drinks for Yoga") == set()
    assert "ENERGY" in pf.brand_lines_in("F3 Energy is zero sugar.")
    assert "ENERGY" in pf.brand_lines_in("Energy is our zero-sugar line.")
    assert "MOOD" in pf.brand_lines_in("F3 Mood has no caffeine.")
    assert "PURE" in pf.brand_lines_in("Pure uses cane sugar.")


# ---------------------------------------------------------------------------
# rail 2 -- clean/natural is Pure-only
# ---------------------------------------------------------------------------


def test_rail2_blocks_clean_in_same_sentence_as_energy():
    r = _run(body="<p>F3 Energy is clean and simple.</p>")
    assert not r.passed
    assert "R2" in r.tripped_rail_ids


def test_rail2_blocks_clean_near_mood():
    r = _run(body="<p>F3 Mood is a natural way to unwind.</p>")
    assert not r.passed
    assert "R2" in r.tripped_rail_ids


def test_rail2_allows_clean_for_pure():
    r = _run(body="<p>F3 Pure is clean-sweetened with organic cane sugar.</p>")
    assert r.passed, r.render()


def test_rail2_allows_honest_cross_line_comparison_in_separate_sentences():
    # The sweetener-guide shape (backlog row 5): Pure gets the clean language, the
    # other lines are described factually, in their OWN sentences.
    r = _run(body="<p>F3 Pure is clean-sweetened. F3 Energy uses sucralose.</p>")
    assert r.passed, r.render()


def test_rail2_survives_paragraph_boundaries():
    # Two <p> blocks must not merge into one "sentence" -- if they did, every
    # article mentioning Pure and Energy anywhere would trip.
    r = _run(body="<p>F3 Pure is the clean-sweetened line.</p><p>F3 Energy is zero sugar.</p>")
    assert r.passed, r.render()


# ---------------------------------------------------------------------------
# rail 3 -- Mood is never a sleep aid
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    "<p>F3 Mood helps you sleep.</p>",
    "<p>F3 Mood is a great sleep aid.</p>",
    "<p>Reach for F3 Mood when you feel drowsy at night.</p>",
    "<p>F3 Mood has a sedative effect.</p>",
])
def test_rail3_blocks_sleep_framing_when_mood_present(body):
    r = _run(body=body)
    assert not r.passed
    assert "R3" in r.tripped_rail_ids


def test_rail3_allows_the_checklists_own_cleared_phrase():
    # The checklist's approved framing is literally "composure, not sedation".
    # A rail that blocks its own cleared language is a broken rail.
    r = _run(body="<p>F3 Mood is not a sleep aid. It is composure, not sedation.</p>")
    assert r.passed, r.render()


def test_rail3_does_not_fire_without_mood():
    # Rail 3 is Mood-scoped; a Learn post about sleep hygiene that never mentions
    # Mood is not making a Mood claim.
    r = _run(body="<p>Caffeine late in the day can make it harder to fall asleep.</p>")
    assert r.passed, r.render()


# ---------------------------------------------------------------------------
# rail 4 -- NSF is Energy-only
# ---------------------------------------------------------------------------


def test_rail4_allows_nsf_for_energy_only():
    assert _run(body="<p>F3 Energy is NSF Certified for Sport.</p>").passed


@pytest.mark.parametrize("body", [
    "<p>F3 Pure is NSF Certified for Sport.</p>",
    "<p>F3 Mood is NSF Certified for Sport.</p>",
    "<p>F3 Energy and F3 Pure are both NSF Certified for Sport.</p>",
    "<p>Our energy drink is NSF Certified for Sport.</p>",
])
def test_rail4_blocks_nsf_outside_an_energy_only_sentence(body):
    r = _run(body=body)
    assert not r.passed
    assert "R4" in r.tripped_rail_ids


# ---------------------------------------------------------------------------
# rail 5 -- prices, and the revenue figure that is NOT a price
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kw", [
    {"summary": "Just $39.99 per pack."},
    {"summary": "MSRP applies."},
    {"body": "<p>The retail price is set by each store.</p>"},
    {"body": "<p>Cost per can drops on a 12-pack.</p>"},
    {"body": "<p>About 40 dollars a case.</p>"},
])
def test_rail5_blocks_prices(kw):
    r = _run(**kw)
    assert not r.passed
    assert "R5" in r.tripped_rail_ids


def test_rail5_allows_attributed_outlet_revenue():
    # Rail 8 explicitly permits restating a revenue figure an outlet already
    # printed. The first cut of rail 5 blocked the LIVE Tribune amplification
    # article on exactly this sentence -- i.e. it blocked the News drafts this
    # lane exists to produce.
    r = _run(body="<p>The Tribune reports roughly $1.36 million in revenue in 2025.</p>")
    assert r.passed, r.render()


def test_rail5_allows_a_pack_count():
    assert _run(summary="12 cans per pack.").passed


def test_rail5_blocks_a_structured_data_price():
    # JSON-LD prices carry no currency symbol, so the currency scan cannot see
    # them. A reader-invisible price is still a published price.
    body = ('<p>hi</p><script type="application/ld+json">'
            '{"offers":{"price":"39.99","priceCurrency":"USD"}}</script>')
    r = _run(body=body)
    assert not r.passed
    assert "R5" in r.tripped_rail_ids


def test_rail5_revenue_exemption_is_not_a_hole_for_the_word_sales():
    # A bare "sales" must not exempt a nearby price -- "our sales team" would
    # otherwise clear any price within the context window.
    r = _run(body="<p>Our sales team says a can is $3.99.</p>")
    assert not r.passed
    assert "R5" in r.tripped_rail_ids


def test_embargo_rail_catches_raise_and_valuation_independently_of_rail5():
    # The rail-5 revenue exemption is only safe because rail 8 matches embargo
    # terms directly rather than leaning on the currency symbol. Pin that.
    r = pf.run_preflight(
        title="t", summary="",
        body_html="<p>Revenue aside, we are raising $5 million at a $40 million valuation.</p>",
    )
    assert not r.passed
    assert "R8" in r.tripped_rail_ids


# ---------------------------------------------------------------------------
# rail 6 -- em-dashes
# ---------------------------------------------------------------------------


def test_rail6_blocks_em_dash_in_every_field():
    for kw in ({"title": "F3 — the story"},
               {"summary": "Clean — simple."},
               {"body": "<p>Real energy — real life.</p>"}):
        r = _run(**kw)
        assert not r.passed and "R6" in r.tripped_rail_ids, kw


def test_rail6_blocks_spaced_en_dash_used_as_an_em_dash():
    r = _run(body="<p>Real energy – real life.</p>")
    assert not r.passed
    assert "R6" in r.tripped_rail_ids


def test_rail6_allows_an_en_dash_numeric_range_and_plain_hyphens():
    r = _run(body="<p>Roughly 120–140 mg of caffeine, zero-sugar, clean-sweetened Pure.</p>")
    assert r.passed, r.render()


def test_rail6_scans_bytes_a_reader_never_sees():
    # An em-dash inside JSON-LD or alt text still ships.
    body = ('<p>ok</p><script type="application/ld+json">'
            '{"headline":"F3 — the story"}</script>')
    r = _run(body=body)
    assert not r.passed
    assert "R6" in r.tripped_rail_ids


# ---------------------------------------------------------------------------
# rails 1 / 10 / 11 / 13 + placeholders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    "<p>It helps with anxiety.</p>",
    "<p>Great for ADHD focus.</p>",
    "<p>A remedy for insomnia.</p>",
    "<p>It treats the underlying condition.</p>",
])
def test_rail1_blocks_medical_claims(body):
    r = _run(body=body)
    assert not r.passed
    assert "R1" in r.tripped_rail_ids


def test_rail1_allows_a_claim_verb_with_no_health_noun():
    # "treat yourself" is not a disease claim. A bare verb lexicon would block it.
    r = _run(body="<p>Treat yourself to a cold one after training.</p>")
    assert r.passed, r.render()


def test_rail1_allows_an_explicit_disclaimer():
    r = _run(body="<p>F3 is a beverage and does not treat any condition.</p>")
    assert r.passed, r.render()


def test_rail10_blocks_2022_as_a_founding_year_but_allows_a_2022_citation():
    assert not _run(body="<p>Founded in 2022 in Mesa, Arizona.</p>").passed
    r = _run(body="<p>Founded in 2023 in Mesa. A 2022 study looked at L-theanine.</p>")
    assert r.passed, r.render()


@pytest.mark.parametrize("body", [
    "<p>F3 is vegan.</p>",
    "<p>Gluten-free and dairy-free.</p>",
    "<p>A non-GMO formula.</p>",
    "<p>An organic beverage.</p>",
])
def test_rail11_blocks_product_claims(body):
    r = _run(body=body)
    assert not r.passed
    assert "R11" in r.tripped_rail_ids


def test_rail11_allows_organic_cane_sugar_as_cleared_ingredient_language():
    r = _run(body="<p>F3 Pure is sweetened with organic cane sugar.</p>")
    assert r.passed, r.render()


def test_rail13_blocks_supplement_framing_but_allows_the_denial():
    assert not _run(body="<p>F3 is a dietary supplement.</p>").passed
    assert _run(body="<p>F3 is a beverage, not a dietary supplement.</p>").passed


@pytest.mark.parametrize("body", [
    "<p>Caffeine is [TBD] mg.</p>",
    "<p>See {{collection_link}} for more.</p>",
    "<p>TODO: add the quote.</p>",
    "<p>Lorem ipsum dolor sit amet.</p>",
])
def test_placeholder_rail_blocks_unfilled_drafts(body):
    r = _run(body=body)
    assert not r.passed
    assert "PLACEHOLDER" in r.tripped_rail_ids


def test_placeholder_rail_allows_a_bracketed_citation_number():
    r = _run(body="<p>Caffeine peaks in about 45 minutes [1].</p>")
    assert r.passed, r.render()


# ---------------------------------------------------------------------------
# Result contract + honesty about what is NOT enforced
# ---------------------------------------------------------------------------


def test_a_clean_draft_passes_with_every_rail_run():
    r = _run(
        title="How Much Caffeine Is in F3?",
        summary="F3 Pure and F3 Energy each carry 120 mg of caffeine from green tea.",
        body="<p>F3 Pure and F3 Energy each carry 120 mg of green tea caffeine. "
             "F3 Mood has none. The FDA cites 400 mg a day as a reference point.</p>",
    )
    assert r.passed, r.render()
    assert set(r.rails_checked) == set(pf.RAILS_CHECKED)


def test_render_is_never_empty_on_either_outcome():
    # D-234's corollary: an empty outcome string renders downstream as a
    # fabricated success.
    assert _run().render().strip()
    assert _run(summary="$9.99").render().strip()


def test_report_states_which_rails_are_not_code_enforced():
    # A green preflight must never read as "cleared". The four human-judgment
    # rails are named in the passing report on purpose.
    txt = _run().render()
    assert "rail 7" in txt and "rail 12" in txt
    assert pf.UNENFORCED_RAILS


def test_blocked_report_names_the_tripped_rails():
    r = _run(title="F3 Energy is clean", summary="Only $39.99 per pack.",
             body="<p>F3 Energy is clean — really.</p>")
    txt = r.render()
    assert not r.passed
    assert "Nothing was staged" in txt
    for rail in ("R2", "R5", "R6"):
        assert rail in txt


def test_no_override_parameter_exists():
    # Fail-closed means fail-closed: there is deliberately no way for a caller to
    # ask the preflight to pass anyway.
    import inspect
    params = set(inspect.signature(pf.run_preflight).parameters)
    assert params == {"title", "summary", "body_html", "lane"}


# ---------------------------------------------------------------------------
# Checklist drift fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_ignores_whitespace_and_line_endings():
    a = pf.fingerprint_checklist("1. No prices.\n2. No em-dashes.\n")
    b = pf.fingerprint_checklist("1. No prices.\r\n2. No em-dashes.\r\n\r\n")
    c = pf.fingerprint_checklist("  1. No prices.  \n\n  2. No em-dashes.\n")
    assert a == b == c


def test_fingerprint_changes_when_a_rule_changes():
    a = pf.fingerprint_checklist("1. No prices.")
    b = pf.fingerprint_checklist("1. Prices are fine.")
    assert a != b


# ---------------------------------------------------------------------------
# ReDoS: assert the SHAPE, not a wall-clock threshold (D-236)
# ---------------------------------------------------------------------------


def _elapsed(reps: int) -> float:
    body = "<p>" + ("Energy drinks are clean and natural living is nice. " * reps) + "</p>"
    start = time.perf_counter()
    pf.run_preflight(title="t", summary="", body_html=body)
    return time.perf_counter() - start


def test_preflight_scales_linearly_not_quadratically():
    # A 2-second bar would have let the sixth ReDoS in this codebase ship. The
    # invariant is the growth RATE: doubling the input must not ~4x the work.
    base = _elapsed(2000)
    dbl = _elapsed(4000)
    assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)


def test_preflight_handles_a_pathological_single_line():
    # Slack's own cap is 40k chars; a 39k-char unbroken run is reachable.
    body = "<p>" + ("clean " * 6000) + "—" + "</p>"
    start = time.perf_counter()
    r = pf.run_preflight(title="t", summary="", body_html=body)
    assert (time.perf_counter() - start) < 2.0
    assert not r.passed
