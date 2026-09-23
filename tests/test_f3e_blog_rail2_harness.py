"""Rail-2 attribution rail + differential harness (Code #13 slice 6; wired by R14-3,
cq-85b35413b020).

Contract under test:
  * run_preflight SHIPS the attribution rail (source pin, R14-3) and keeps its four
    parameters (the fail-closed signature pin stands); rail2_legacy_hit stays
    exported as the frozen measurement baseline;
  * the shipping rail catches every clean/natural-on-Energy/Mood probe incl. the
    attributed quote, ALL-CAPS, the Title-Case evasion, the ambiguous Pure+Energy
    clause, the no-widening probes for the two ruled phrases and the R14-3 new-hole
    shapes, and PASSES every measured false-positive shape;
  * the five pinned D-051 holes trip under BOTH preflights;
  * THE GATE (ruling ESC-1 (A), D-329): ship is True; the sugar-free-on-Pure and
    comparative-category classes still pass BOTH preflights (no rail exists) and
    are RULED OUT of the condition, still probed and named in the summary;
  * differential() releases exactly the legacy-tripping FP shapes and catches only
    the R14-3 hole closures legacy misses;
  * every new regex has a growth-shape test on the input that reaches it (D-239);
  * the script's offline mode prints the verdict and exits 0 (gate open); the live
    mode parses index pages and never touches the network in tests.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import time
from pathlib import Path

import pytest

from cora.f3e_blog import preflight as pf
from cora.f3e_blog import rail2_harness as rh

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "run_f3e_blog_rail2_differential.py"

#: D-051 r143 remediation: the gate probes the FROZEN baseline misses (single-sentence
#: legacy PASS) that the shipping rail now closes. Named one by one -- the differential
#: pin below stays an EXACT set, it only gains the closures listed here.
R143_LEGACY_MISSES = frozenset({
    "F3 Energy is the super-cleaner fuel.",                          # r143-claims-2
})


def _best_of_3(fn) -> float:
    """D-171: a single timing run flakes under host load; the minimum of three is the
    measurement."""
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


class TestShippingRail:
    def test_run_preflight_ships_the_attribution_rail_and_never_the_legacy_one(self):
        """DELIBERATE FLIP (R14-3): was the inverse pin while the gate was closed."""
        # comment lines stripped first: a source pin must never match its own comment
        src = "\n".join(ln for ln in inspect.getsource(pf.run_preflight).splitlines()
                        if not ln.strip().startswith("#"))
        assert "rail2_attribution_hit(sent)" in src
        assert "rail2_legacy_hit" not in src

    def test_the_legacy_rail_stays_exported_as_the_frozen_baseline(self):
        assert callable(pf.rail2_legacy_hit)
        assert "never called by run_preflight" in (pf.rail2_legacy_hit.__doc__ or "")

    def test_signature_pin_stands(self):
        assert set(inspect.signature(pf.run_preflight).parameters) == {"title", "summary", "body_html", "lane"}

    def test_the_pipeline_dirty_draft_still_trips(self):
        sentence = "F3 Energy is clean and simple."
        r = pf.run_preflight(title="t", summary="", body_html="<p>%s</p>" % sentence)
        assert "R2" in r.tripped_rail_ids
        assert pf.rail2_legacy_hit(sentence) is not None

    def test_the_eight_twenty_six_rejection_now_passes_the_shipping_preflight(self):
        """DELIBERATE FLIP (R14-3): this was pinned as a run_preflight TRIP. It is the
        measured false positive the ruling releases; the frozen baseline still trips."""
        sentence = "Explore the full stack in F3 Energy or the clean-sweetened version in F3 Pure."
        r = pf.run_preflight(title="t", summary="", body_html="<p>%s</p>" % sentence)
        assert r.passed, r.render()
        assert pf.rail2_legacy_hit(sentence) is not None


class TestAttributionSibling:
    @pytest.mark.parametrize("sentence", rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"])
    def test_catches_every_clean_on_energy_mood_probe(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is not None, sentence

    @pytest.mark.parametrize("sentence", rh.FALSE_POSITIVE_SET)
    def test_passes_every_measured_false_positive(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is None, sentence

    @pytest.mark.parametrize("sentence", [
        "Explore the full stack in F3 Energy or the clean-sweetened version in F3 Pure.",
        "F3 Energy partners with CleanHub to fund a cleaner planet with every case sold.",
        "Every F3 Energy purchase supports a cleaner future for the oceans.",
        "F3 Energy carries 120 mg of natural caffeine from green tea plus a nootropic-leaning stack "
        "for training and competition.",
        "Every can carries the same formula philosophy: the functional stack you know from F3 Energy, "
        "with a cleaner fuel source.",
    ])
    def test_legacy_trips_the_shapes_attribution_releases(self, sentence):
        """The measured FP rate: these are the sentences the ruling wants released."""
        assert pf.rail2_legacy_hit(sentence) is not None
        assert pf.rail2_attribution_hit(sentence) is None

    def test_who_said_it_is_never_an_exemption(self):
        quote = ("Their CEO said F3 Energy delivers exactly what our customers are seeking, "
                 "clean energy with a clean label.")
        assert pf.rail2_attribution_hit(quote) is not None

    def test_and_does_not_split_clauses(self):
        """'F3 Pure is clean-sweetened and F3 Energy is too' attaches clean to Energy
        across 'and' -- splitting there would open a hole."""
        assert pf.rail2_attribution_hit("F3 Pure is clean-sweetened and F3 Energy is too.") is not None

    def test_pure_only_sentences_are_untouched(self):
        assert pf.rail2_attribution_hit("F3 Pure is clean-sweetened with organic cane sugar.") is None
        assert pf.rail2_legacy_hit("F3 Pure is clean-sweetened with organic cane sugar.") is None

    def test_environmental_redaction_is_narrow(self):
        assert pf.rail2_attribution_hit("F3 Energy is the clean choice for the planet.") is not None   # 'clean choice' is a product claim
        assert pf.rail2_attribution_hit("F3 Energy helps clean up the beaches every spring.") is None   # 'clean up' is an activity


class TestGate:
    @pytest.mark.parametrize("sentence", rh.PINNED_D051)
    def test_pinned_d051_holes_trip_under_both_preflights(self, sentence):
        assert not rh.legacy_preflight(sentence).passed
        assert not rh.new_preflight(sentence).passed

    def test_the_gate_is_open_under_ruling_a(self):
        """DELIBERATE FLIP (R14-3): was test_the_finding_two_classes_have_no_rail_so_
        the_gate_is_closed (ship False). The two rail-less classes are unchanged --
        still uncaught by BOTH preflights -- but ruled out of the condition."""
        v = rh.evaluate()
        assert v.ship is True
        assert v.pinned_missed == []
        assert v.fp_still_tripping == []                 # the shipping rail releases every measured FP
        missed = {cls for cls, s in v.uncaught_by_class.items() if s}
        assert missed == rh.RULED_OUT_CLASSES == {"sugar_free_on_pure", "comparative_category"}, missed
        # ...and those two ALSO pass the LEGACY preflight: the hole pre-exists rail 2
        for cls in missed:
            assert set(v.uncaught_legacy_by_class[cls]) == set(v.uncaught_by_class[cls])
            assert v.uncaught_by_class[cls] == list(rh.CLAIMS_HOLE_PROBES[cls])   # still PROBED
        assert set(rh.CLAIMS_HOLE_PROBES) - missed == {"clean_natural_on_energy_mood", "nsf_on_pure_mood", "sleep_on_mood"}
        lines = "\n".join(v.summary_lines())
        assert lines.startswith("SHIP: YES")
        for cls in rh.RULED_OUT_CLASSES:
            assert any(cls in ln and "no rail exists -- ruled out of rail-2's condition 2026-09-19" in ln
                       and "seeded" in ln for ln in v.summary_lines()), cls
        # PIN CHANGED with R14-3: +2 ruled sentences (green tea, cleaner fuel source)
        assert "false positives: legacy trips 5/6, attribution trips 0/6" in lines

    def test_ruled_out_classes_are_pinned_exactly(self):
        """Adding a class here is a RULING -- this pin makes it a deliberate edit."""
        assert rh.RULED_OUT_CLASSES == frozenset({"sugar_free_on_pure", "comparative_category"})

    def test_a_gated_class_regression_closes_the_gate(self, monkeypatch):
        """The exclusion is scoped: an uncaught probe in any OTHER class still blocks."""
        probes = dict(rh.CLAIMS_HOLE_PROBES)
        probes["sleep_on_mood"] = probes["sleep_on_mood"] + ("F3 Mood is a lovely drink.",)
        monkeypatch.setattr(rh, "CLAIMS_HOLE_PROBES", probes)
        assert rh.evaluate().ship is False

    def test_a_ruled_out_class_regression_does_not_close_the_gate_but_is_reported(self, monkeypatch):
        probes = dict(rh.CLAIMS_HOLE_PROBES)
        probes["comparative_category"] = probes["comparative_category"] + ("F3 Energy tops them all.",)
        monkeypatch.setattr(rh, "CLAIMS_HOLE_PROBES", probes)
        v = rh.evaluate()
        assert v.ship is True and "F3 Energy tops them all." in "\n".join(v.summary_lines())

    def test_undecided_sentences_are_reported_not_gated(self):
        """PIN CHANGED with R14-3: 2 -> 1 (the 9/14 green-tea sentence was ruled
        cleared and moved to FALSE_POSITIVE_SET)."""
        v = rh.evaluate()
        assert len(rh.UNDECIDED) == 1
        for s in rh.UNDECIDED:
            assert v.undecided[s] == {"legacy": True, "attribution": True}, s
        assert sum("UNDECIDED" in ln for ln in v.summary_lines()) == 1
        assert not any("green tea" in s for s in rh.UNDECIDED)
        assert any("natural caffeine from green tea" in s for s in rh.FALSE_POSITIVE_SET)
        assert any("cleaner fuel source" in s for s in rh.FALSE_POSITIVE_SET)
        # the 9/1 shape is the one that moved (see TestFailClosedInheritance)
        assert any("organic cane sugar, monk fruit and stevia" in s for s in rh.UNDECIDED)
        assert not any("organic cane sugar" in s for s in rh.FALSE_POSITIVE_SET)

    def test_new_preflight_keeps_every_other_rail(self):
        r = rh.new_preflight("F3 Pure is NSF Certified for Sport and costs $39.99.")
        assert {"R4", "R5"} <= set(r.tripped_rail_ids)
        assert "R2" not in r.tripped_rail_ids

    def test_new_preflight_is_the_shipping_function(self):
        for s in ("F3 Energy is clean.", "F3 Pure is clean-sweetened.", rh.FALSE_POSITIVE_SET[0]):
            a, b = rh.new_preflight(s), pf.run_preflight(title="Post", summary="", body_html="<p>%s</p>" % s)
            assert (a.passed, a.trips) == (b.passed, b.trips)

    def test_legacy_preflight_keeps_every_other_rail_and_the_legacy_rail_two(self):
        r = rh.legacy_preflight("Explore the full stack in F3 Energy or the clean-sweetened version in F3 Pure. $39.99.")
        assert {"R2", "R5"} <= set(r.tripped_rail_ids)
        assert rh.legacy_run(title="F3 Energy is clean", summary="", body_html="<p>x</p>").tripped_rail_ids == ["R2"]

    def test_differential_releases_only_the_fp_shapes(self):
        """PIN CHANGED with R14-3: only_legacy 3 -> 5 (the two ruled sentences) and
        only_attribution is no longer empty -- it is exactly the hole closures the
        frozen baseline misses (verb forms, hyphen compounds)."""
        corpus = list(rh.FALSE_POSITIVE_SET) + list(rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"]) + [
            "A sentence about nothing in particular.", "F3 Pure is the clean-sweetened version.",
        ]
        d = rh.differential(corpus)
        assert d.sentences == len(corpus)
        assert set(d.only_legacy) == {s for s in rh.FALSE_POSITIVE_SET if pf.rail2_legacy_hit(s)}
        assert len(d.only_legacy) == 5
        assert set(d.only_attribution) == {
            "F3 Energy runs on cleaner-fuel.",
            "F3 Energy cleans up your afternoon.",
            "F3 Energy is the clean-up crew for your afternoon slump.",
            "F3 Energy is clean-energy in a can.",
            "F3 Mood is a clean-energy calm.",
            "F3 Energy is naturally-caffeinated.",
            "F3 Energy is the natural-energy pick.",
        } | R143_LEGACY_MISSES
        assert len(d.attribution_trips) == len(rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"])


class TestFailClosedInheritance:
    """D-051 EF-7 regression. Nearest-clause inheritance cleared a clean word
    predicated OF Energy/Mood whenever the nearest preceding segment named Pure
    alone ('F3 Energy, like F3 Pure, is clean.' -> legacy TRIP, attribution
    pass, new_preflight PASSED) and the probe set was blind to the shape, so
    evaluate() reported the class fully caught. A brand-less clean segment now
    inherits the UNION of every brand named earlier; a bare coordinated brand
    after 'or' folds into the clause it coordinates with."""

    HOLES = (
        "F3 Energy, like F3 Pure, is clean.",
        "F3 Energy, similar to F3 Pure, is all-natural.",
        "F3 Mood, our companion to F3 Pure, is the clean way to wind down.",
        "F3 Energy: think F3 Pure, then clean caffeine on top.",
        "Clean energy from F3 Pure or F3 Energy.",
        "F3 Energy or F3 Pure: clean, natural energy.",
    )

    @pytest.mark.parametrize("sentence", HOLES)
    def test_the_six_review_shapes_trip_the_attribution_rail_and_the_new_preflight(self, sentence):
        assert pf.rail2_legacy_hit(sentence) is not None
        assert pf.rail2_attribution_hit(sentence) is not None, sentence
        assert rh.new_preflight(sentence).passed is False, sentence

    def test_the_shapes_are_in_the_ruled_probe_set_so_the_harness_sees_them(self):
        probes = rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"]
        for s in self.HOLES:
            assert s in probes, s
        assert rh.evaluate().uncaught_by_class["clean_natural_on_energy_mood"] == []

    def test_the_nine_one_shape_trips_again_and_is_undecided_not_a_false_positive(self):
        s = next(x for x in rh.UNDECIDED if "organic cane sugar" in x)
        assert pf.rail2_attribution_hit(s) is not None
        assert s not in rh.FALSE_POSITIVE_SET

    def test_own_brand_attribution_still_clears_the_live_rejection(self):
        """The 8/26 shape keeps clearing: the clean word sits in a clause that
        names Pure ITSELF (own attribution wins over the union)."""
        s = "Explore the full stack in F3 Energy or the clean-sweetened version in F3 Pure."
        assert pf.rail2_attribution_hit(s) is None
        assert pf.rail2_attribution_hit("F3 Pure is clean-sweetened; F3 Energy is the full stack.") is None

    def test_bare_brand_segment_detector(self):
        assert pf._bare_brand_segment(" F3 Energy.") and pf._bare_brand_segment(" and F3's Pure")
        assert not pf._bare_brand_segment(" the clean-sweetened version in F3 Pure.")
        assert not pf._bare_brand_segment(" energy drinks") and not pf._bare_brand_segment("")

    def test_run_preflight_trips_every_review_shape(self):
        """RENAMED with R14-3 (was ..._is_untouched_by_the_rail_change): run_preflight
        now ships the attribution rail, and every EF-7 review shape still trips it."""
        for s in self.HOLES:
            assert "R2" in pf.run_preflight(title="t", summary="", body_html="<p>%s</p>" % s).tripped_rail_ids


class TestReDoS:
    """D-239: a growth-shape test on the input that REACHES each new construct."""

    def test_coordinated_brand_runs_scale_linearly(self):
        """EF-7's bare-brand fold: a run of ', F3 Pure' coordinations must not
        grow a string per fold (parts are joined once per segment)."""
        def elapsed(n):
            sent = "F3 Energy" + (", F3 Pure" * n) + ", clean."
            t0 = time.perf_counter()
            assert pf.rail2_attribution_hit(sent) is not None
            return time.perf_counter() - t0
        base, dbl = elapsed(5000), elapsed(10000)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)

    @staticmethod
    def _elapsed(fn, reps: int) -> float:
        sent = "F3 Energy, " + ("clean planet, or cleaner future: " * reps) + "clean energy."
        t0 = time.perf_counter()
        fn(sent)
        return time.perf_counter() - t0

    def test_attribution_hit_scales_linearly(self):
        base = self._elapsed(pf.rail2_attribution_hit, 2000)
        dbl = self._elapsed(pf.rail2_attribution_hit, 4000)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)

    def test_pathological_clause_runs(self):
        sent = "F3 Energy " + ("," * 20000) + " clean"
        t0 = time.perf_counter()
        pf.rail2_attribution_hit(sent)
        assert time.perf_counter() - t0 < 1.0


class TestScript:
    def _load(self):
        spec = importlib.util.spec_from_file_location("rail2_differential_under_test", _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_offline_mode_prints_the_open_gate_and_exits_0(self, capsys):
        """DELIBERATE FLIP (R14-3): rc 3 / SHIP: NO -> rc 0 / SHIP: YES; the ruled-out
        classes are still printed."""
        mod = self._load()
        rc = mod.main([])
        out = capsys.readouterr().out
        assert rc == 0 and "SHIP: YES" in out and "== probe table" in out
        assert "sugar_free_on_pure" in out and "comparative_category" in out and "ruled out" in out

    def test_live_mode_uses_the_injected_fetch_and_reports_counts(self, capsys, tmp_path):
        mod = self._load()
        index = ('<a href="/blogs/news/first-post">x</a> <a href="https://f3energy.com/blogs/news/second-post">y</a>'
                 ' <a href="/blogs/news">index</a>')
        pages = {
            "https://f3energy.com/blogs/news": (200, index),
            "https://f3energy.com/blogs/learn": (404, ""),
            "https://f3energy.com/blogs/news/first-post": (200, "<p>Explore the full stack in F3 Energy or the clean-sweetened version in F3 Pure. F3 Energy is Clean Energy.</p>"),
            "https://f3energy.com/blogs/news/second-post": (200, "<p>F3 Pure is clean-sweetened with organic cane sugar.</p>"),
        }
        calls = []

        def fetch(url):
            calls.append(url)
            return pages.get(url, (404, ""))
        out_path = tmp_path / "report.txt"
        rc = mod.main(["--live", "--out", str(out_path)], fetch=fetch)
        out = capsys.readouterr().out
        assert rc == 0   # DELIBERATE FLIP (R14-3): the gate is open
        assert "articles linked 2 | fetched 2" in out
        assert "rail-2 trips: legacy 2 | attribution 1" in out
        assert "released by attribution scope" in out and out_path.exists()
        assert all(u.startswith("https://f3energy.com/blogs/") for u in calls)

    def test_article_url_parser(self):
        mod = self._load()
        urls = mod.article_urls('<a href="/blogs/learn/a-b">1</a><a href="/blogs/learn/a-b">dup</a><a href="/blogs/news/">idx</a>', "x")
        assert urls == ["https://f3energy.com/blogs/learn/a-b"]

    def test_script_is_read_only_and_bootstrapped(self):
        src = _SCRIPT.read_text(encoding="utf-8")
        assert src.index("from cora.f3e_blog") > src.index('sys.path.insert(0, str(_REPO_ROOT / "src"))')
        assert "create_article" not in src and "publish" not in src.lower().replace("public", "")
        assert "anthropic" not in src.lower()


# ---------------------------------------------------------------------------
# R14-3 (Code #14, cq-85b35413b020): the ruled exemptions, the closed new holes,
# ReDoS shapes for every new pattern, and the differential regression pin.
# ---------------------------------------------------------------------------


def _pf(sentence: str) -> pf.PreflightResult:
    return pf.run_preflight(title="Post", summary="", body_html="<p>%s</p>" % sentence)


class TestExemptions:
    """ESC 3(i)/(ii), D-329: two EXACT phrases, brand-scoped, fail-closed on Mood."""

    PASS = (
        "F3 Energy carries 120 mg of natural caffeine from green tea plus a nootropic-leaning stack "
        "for training and competition.",
        "Every can carries the same formula philosophy: the functional stack you know from F3 Energy, "
        "with a cleaner fuel source.",
        "Same flavor, cleaner fuel: F3 Pure is F3 Energy with a cleaner fuel source.",
        "F3 Energy gets its lift from natural caffeine from green tea.",
        "NATURAL CAFFEINE FROM GREEN TEA powers every can of F3 Energy.",   # IGNORECASE: a headline capital
        "F3 Pure and F3 Energy share a cleaner fuel source.",               # the ruled Pure AND Energy scope
    )
    MUST_TRIP = (
        "F3 Energy has natural caffeine.",
        "F3 Energy is naturally caffeinated.",
        "F3 Energy runs on clean fuel.",
        "F3 Energy is the cleanest fuel source.",
        "F3 Energy: natural caffeine from green tea and clean energy all day.",
        "F3 Energy is a cleaner fuel for a natural high.",
        "F3 Mood is a cleaner fuel source for your evening.",
        "F3 Mood has natural caffeine from green tea.",
        "F3 Energy and F3 Mood both run on natural caffeine from green tea.",
        "F3 Energy runs on cleaner-fuel.",
        "F3 Energy is Clean Energy.",
    )

    @pytest.mark.parametrize("sentence", PASS)
    def test_the_ruled_phrases_pass_the_shipping_preflight(self, sentence):
        r = _pf(sentence)
        assert r.passed, r.render()

    @pytest.mark.parametrize("sentence", MUST_TRIP)
    def test_no_widening_every_variant_still_trips(self, sentence):
        r = _pf(sentence)
        assert "R2" in r.tripped_rail_ids, sentence

    def test_the_no_widening_probes_are_in_the_gate(self):
        """The gate enforces no-widening, not just this test class."""
        probes = rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"]
        for s in self.MUST_TRIP:
            assert s in probes, s

    def test_whitespace_between_the_phrase_words_is_bounded(self):
        assert pf.rail2_attribution_hit("F3 Energy has natural   caffeine from green tea.") is None
        assert pf.rail2_attribution_hit("F3 Energy has natural    caffeine from green tea.") is not None

    def test_mood_named_anywhere_in_the_sentence_disables_both_exemptions(self):
        assert pf.rail2_attribution_hit(
            "F3 Energy runs on natural caffeine from green tea; F3 Mood does not.") is not None
        assert pf.rail2_attribution_hit(
            "F3 Energy runs on a cleaner fuel source, unlike F3 Mood.") is not None

    def test_the_frozen_baseline_still_trips_the_ruled_phrases(self):
        for s in rh.FALSE_POSITIVE_SET[-2:]:
            assert pf.rail2_legacy_hit(s) is not None, s

    def test_the_exemption_patterns_are_the_two_ruled_phrases_only(self):
        pats = [p.pattern for p, _ in pf._RAIL2_PHRASE_EXEMPTIONS]
        assert len(pats) == 2
        assert all("\\s{1,3}" in p for p in pats)
        assert {frozenset(a) for _, a in pf._RAIL2_PHRASE_EXEMPTIONS} == {
            frozenset({"ENERGY"}), frozenset({"ENERGY", "PURE"})}


class TestEnvironmentalRedactionIsPositionChecked:
    """The Code #13 environmental redaction cleared a PREDICATE of the brand; R14-3
    redacts only the object of an environmental action (fail-closed)."""

    HOLES = (
        "F3 Energy is clean air in a can.",
        "F3 Energy is a cleaner future.",
        "F3 Energy is a clean world of flavor.",
        "F3 Energy delivers clean water-like hydration.",
        "F3 Energy cleans up your afternoon.",
        "F3 Energy is the clean-up crew for your afternoon slump.",
        "F3 Mood is a cleaner planet in a can.",
        # a metaphorical environmental object after an action verb is a product claim
        "F3 Energy builds a cleaner world of flavor.",
        "F3 Energy protects clean water in every can.",
        "F3 Energy supports clean air inside your lungs.",
        "F3 Energy funds a cleaner planet in every sip.",
        "F3 Energy supports a cleaner future for the oceans of flavor.",
        "F3 Energy cleans up the streets of your mind.",
    )
    RELEASED = (
        "F3 Energy partners with CleanHub to fund a cleaner planet with every case sold.",
        "Every F3 Energy purchase supports a cleaner future for the oceans.",
        "F3 Energy helps clean up the beaches every spring.",
        "F3 Energy sponsors a beach clean-up every spring.",
    )

    @pytest.mark.parametrize("sentence", HOLES)
    def test_a_clean_predicate_of_the_brand_trips(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is not None, sentence
        assert "R2" in _pf(sentence).tripped_rail_ids, sentence

    @pytest.mark.parametrize("sentence", RELEASED)
    def test_an_environmental_object_of_an_environmental_action_is_released(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is None, sentence

    def test_the_redaction_only_removes_the_phrase(self):
        """Anything clean OUTSIDE the redacted object still trips."""
        assert pf.rail2_attribution_hit(
            "F3 Energy funds a cleaner planet and is the cleanest can in the cooler.") is not None


class TestHyphenAndVerbForms:
    @pytest.mark.parametrize("sentence", [
        "F3 Energy is clean-energy in a can.",
        "F3 Mood is a clean-energy calm.",
        "F3 Energy is naturally-caffeinated.",
        "F3 Energy is the natural-energy pick.",
        "F3 Energy is a super-clean formula.",
        "F3 Energy is clean/natural energy.",
        "F3 Energy cleanses your afternoon.",
    ])
    def test_compounds_and_verb_forms_trip(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is not None, sentence

    def test_hyphenated_chemistry_is_still_chemistry(self):
        assert pf.rail2_attribution_hit(
            "L-theanine is a naturally-occurring amino acid, which F3 Energy pairs with caffeine.") is None

    def test_pure_compounds_still_clear_in_a_pure_clause(self):
        assert pf.rail2_attribution_hit("F3 Pure is super-clean.") is None
        assert pf.rail2_attribution_hit(
            "F3 Energy carries the full stack; F3 Pure is the clean-sweetened version.") is None

    def test_the_legacy_token_set_is_untouched(self):
        assert "cleans" not in pf._CLEAN_TOKENS and "cleans" in pf._ATTRIBUTION_CLEAN_TOKENS


class TestNewPatternReDoS:
    """D-171 / D-239: every new or edited regex, timed on the degenerate input that
    REACHES it (40k of the character its quantifier eats), plus a growth shape
    through the full shipping hit."""

    DEGENERATE = (
        " " * 40000,
        "-" * 40000,
        "natural " * 5000,
        "natural caffeine from green " * 1500,
        "cleaner " * 5000,
        "cleaner fuel " * 3000,
        "fund " * 8000,
        "fund a clean " * 3000,
        "supports a cleaner future for " * 1300,
        "clean up " * 4400,
        "clean-" * 6600,
        "beach " * 6600,
        "naturally-" * 4000,
        "clean " + " " * 40000 + "planet",
        "fund clean water " * 2500 + "in",
        "water " + " " * 40000 + "in",
    )

    def _patterns(self):
        pats = [p for p, _ in pf._RAIL2_PHRASE_EXEMPTIONS]
        pats += list(pf._CLEAN_ENVIRONMENT_RES)
        pats.append(pf._NATURAL_OCCURRENCE_HYPHEN_RE)
        return pats

    def test_each_new_pattern_is_fast_on_degenerate_input(self):
        for pat in self._patterns():
            for text in self.DEGENERATE:
                t0 = time.perf_counter()
                pat.sub(" ", text)
                dt = time.perf_counter() - t0
                assert dt < 0.2, "%s on %r...: %.3fs" % (pat.pattern[:40], text[:20], dt)

    @pytest.mark.parametrize("unit,tail", [
        ("natural caffeine from green ", "tea clean."),
        ("cleaner fuel ", "clean."),
        ("fund a cleaner ", "planet clean."),
        ("clean up ", "the beaches clean."),
        ("clean-", "x clean."),
        ("beach ", "clean-up clean."),
        ("naturally-", "occurring clean."),
    ])
    def test_the_shipping_hit_scales_linearly(self, unit, tail):
        def elapsed(n):
            sent = "F3 Energy " + unit * n + tail
            t0 = time.perf_counter()
            pf.rail2_attribution_hit(sent)
            return time.perf_counter() - t0
        base, dbl = elapsed(3000), elapsed(6000)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)


def _load_preflight_corpora():
    path = _REPO / "tests" / "test_f3e_blog_preflight.py"
    spec = importlib.util.spec_from_file_location("preflight_corpora_for_r14_3", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.EVASIONS, mod.CLEARED


class TestDifferentialRegressionPin:
    """The D-051 S7-revert class, pinned as a PROPERTY: over every pinned corpus, the
    set of inputs that move legacy TRIP -> shipping PASS is EXACTLY the measured,
    legacy-tripping false-positive set. Any other movement is a loosening nobody
    ruled on, and fails here."""

    @staticmethod
    def _corpus():
        evasions, cleared = _load_preflight_corpora()
        sentences = list(rh.PINNED_D051) + list(rh.FALSE_POSITIVE_SET) + list(rh.UNDECIDED)
        for probes in rh.CLAIMS_HOLE_PROBES.values():
            sentences += list(probes)
        sentences += list(TestExemptions.PASS) + list(TestExemptions.MUST_TRIP)
        sentences += list(TestEnvironmentalRedactionIsPositionChecked.HOLES)
        sentences += list(TestEnvironmentalRedactionIsPositionChecked.RELEASED)
        items = [("s:" + s, {"title": "Post", "summary": "", "body_html": "<p>%s</p>" % s}) for s in sentences]
        for label, kw in [(e[0], e[2]) for e in evasions] + [(c[0], c[1]) for c in cleared]:
            items.append(("kw:" + label, {"title": kw.get("title", "Test Title"),
                                          "summary": kw.get("summary", ""),
                                          "body_html": kw.get("body", "<p>Body copy.</p>")}))
        return items

    def test_only_the_ruled_false_positives_move_trip_to_pass(self):
        moved = set()
        for key, kw in self._corpus():
            if not rh.legacy_run(**kw).passed and pf.run_preflight(**kw).passed:
                moved.add(key)
        expected = {"s:" + s for s in rh.FALSE_POSITIVE_SET if not rh.legacy_preflight(s).passed}
        extra = {"s:" + s for s in TestExemptions.PASS} | {
            "s:" + s for s in TestEnvironmentalRedactionIsPositionChecked.RELEASED}
        # the exemption/environmental PASS tables are the same ruled shapes, re-worded
        assert expected <= moved <= expected | extra, sorted(moved - expected)
        assert len(expected) == 5

    def test_no_cleared_copy_and_no_evasion_moves_at_all(self):
        evasions, cleared = _load_preflight_corpora()
        for label, rail, kw in evasions:
            k = {"title": kw.get("title", "Test Title"), "summary": kw.get("summary", ""),
                 "body_html": kw.get("body", "<p>Body copy.</p>")}
            assert not pf.run_preflight(**k).passed and rail in pf.run_preflight(**k).tripped_rail_ids, label
        for label, kw in cleared:
            k = {"title": kw.get("title", "Test Title"), "summary": kw.get("summary", ""),
                 "body_html": kw.get("body", "<p>Body copy.</p>")}
            assert pf.run_preflight(**k).passed and rh.legacy_run(**k).passed, label

    def test_the_only_pass_to_trip_movement_is_the_named_hole_closures(self):
        closed = set()
        for key, kw in self._corpus():
            if rh.legacy_run(**kw).passed and not pf.run_preflight(**kw).passed:
                closed.add(key[2:])
        allowed = set(rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"]) | set(
            TestEnvironmentalRedactionIsPositionChecked.HOLES)
        assert closed and closed <= allowed, sorted(closed - allowed)


class TestMirrorVersion:
    def test_the_rail_scope_change_bumped_the_mirror_version(self):
        assert pf.CHECKLIST_MIRROR_VERSION == "1.1"


# ---------------------------------------------------------------------------
# D-051 r143 remediation (Code #14 review of R14-3, 29c9735). Every hole below
# was reproduced as legacy TRIP / shipping PASS (or PASS on both rails where
# noted); each is also a must-trip probe in rh.CLAIMS_HOLE_PROBES so the ship
# gate enforces it, not only this module.
# ---------------------------------------------------------------------------


class TestExactPhraseEdges:
    """r143-claims-2: the ruled exemptions are EXACT phrases. The first cut matched
    the phrase TAIL, so a modifier or a hyphen-fused prefix before "natural" /
    "cleaner" was left behind as a non-clean token and the sentence passed."""

    MUST_TRIP = (
        "F3 Energy is all-natural caffeine from green tea.",
        "F3 Energy is all natural caffeine from green tea.",
        "F3 Energy: 100% natural caffeine from green tea.",
        "F3 Energy has 100 percent natural caffeine from green tea.",
        "F3 Energy uses only the most natural caffeine from green tea.",
        "F3 Energy: purely natural caffeine from green tea.",
        "F3 Energy has totally natural caffeine from green tea.",
        "F3 Energy is pure and natural caffeine from green tea.",
        "F3 Energy is the super-cleaner fuel.",
        "F3 Energy is the super cleaner fuel.",
        "F3 Energy runs on a much cleaner fuel source.",
        "F3 Energy runs on cleaner fuel-like energy.",
    )
    STILL_PASS = (
        "F3 Energy has L-theanine plus natural caffeine from green tea.",   # coordinator, no modifier
        "F3 Energy: natural caffeine from green tea.",                      # punctuation, not a modifier
        "F3 Energy runs on a cleaner fuel source.",
        "Same flavor, cleaner fuel: F3 Pure is F3 Energy with a cleaner fuel source.",
    )

    @pytest.mark.parametrize("sentence", MUST_TRIP)
    def test_a_modified_or_fused_phrase_is_not_the_ruled_phrase(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is not None, sentence
        assert "R2" in _pf(sentence).tripped_rail_ids, sentence

    @pytest.mark.parametrize("sentence", MUST_TRIP)
    def test_every_edge_probe_is_in_the_gate(self, sentence):
        assert sentence in rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"]

    def test_title_and_tag_split_forms_trip_too(self):
        assert "R2" in pf.run_preflight(title="F3 Energy: All-Natural Caffeine From Green Tea",
                                        summary="", body_html="<p>x</p>").tripped_rail_ids
        body = "<p>F3 Energy is <em>all</em>-natural caffeine from green tea.</p>"
        assert "R2" in pf.run_preflight(title="t", summary="", body_html=body).tripped_rail_ids

    @pytest.mark.parametrize("sentence", STILL_PASS + TestExemptions.PASS + rh.FALSE_POSITIVE_SET[-2:])
    def test_the_exact_phrase_still_clears(self, sentence):
        r = _pf(sentence)
        assert r.passed, r.render()

    def test_modifier_classification(self):
        for w in ("all", "most", "super", "purely", "100", "100%", "much", "clean", "all-natural"):
            assert pf._is_phrase_modifier(w), w
        for w in ("of", "from", "has", "the", "a", "uses", "energy", "l-theanine", "stack"):
            assert not pf._is_phrase_modifier(w), w

    DEGENERATE = (
        " " * 40000,
        "\t" * 40000,
        "-" * 40000 + "natural caffeine from green tea",
        "%" * 40000 + " natural caffeine from green tea",
        "all natural caffeine from green tea " * 1100,
        "and " * 10000 + "natural caffeine from green tea",
        "super cleaner fuel " * 2100,
        "cleaner fuel-" * 3000,
        "natural caffeine from green te" * 1300,      # near miss: never a full match
    )

    @pytest.mark.parametrize("text", DEGENERATE, ids=range(len(DEGENERATE)))
    def test_d171_exact_phrase_redaction_is_fast_at_40k(self, text):
        for pat, _ in pf._RAIL2_PHRASE_EXEMPTIONS:
            dt = _best_of_3(lambda: pf._redact_exact_phrase(pat, text))
            assert dt < 0.2, "%s on %r...: %.3fs" % (pat.pattern[:30], text[:20], dt)

    @pytest.mark.parametrize("unit", ["all natural caffeine from green tea ", "a much cleaner fuel source ",
                                      "natural caffeine from green tea ", "cleaner fuel "])
    def test_d171_exact_phrase_redaction_scales_linearly(self, unit):
        def run(n):
            text = "F3 Energy " + unit * n
            return _best_of_3(lambda: pf.rail2_attribution_hit(text))
        base, dbl = run(1500), run(3000)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)
