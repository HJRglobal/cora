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
    "F3 Energy burns cleanly.",                                      # r143-claims-8
    "F3 Energy delivers energy cleanly.",
    "F3 Energy is cleansed of junk.",
    "F3 Energy's cleanliness sets it apart.",
    "F3 Energy's cleanness sets it apart.",
    "F3 Energy's naturalness sets it apart.",
    "F3 Mood is one of the naturals.",
    "F3 Energy is the community clean-up crew for your afternoon slump.",   # r143-claims-6
    "F3 Energy is a beach clean-up in a can.",
    "F3 Energy is a community clean-up for your gut.",
    "F3 Energy cleans up trash talk in the gym.",
})


def _best_of_3(fn, *args) -> float:
    """D-171 / D-051 integration-tests-6: a single timing run flakes under host load
    (the concurrent-suite state of every review and slice gate); the MINIMUM of three
    calls is what the code costs. Bounds are never loosened."""
    best = float("inf")
    for _ in range(3):
        t0 = time.perf_counter()
        fn(*args)
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
        grow a string per fold (parts are joined once per segment). Best of 3 (D-171:
        the r143 remediation touched this function and a single run flaked)."""
        def elapsed(n):
            sent = "F3 Energy" + (", F3 Pure" * n) + ", clean."
            assert pf.rail2_attribution_hit(sent) is not None
            return _best_of_3(lambda: pf.rail2_attribution_hit(sent))
        base, dbl = elapsed(5000), elapsed(10000)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)

    @staticmethod
    def _elapsed(fn, reps: int) -> float:
        sent = "F3 Energy, " + ("clean planet, or cleaner future: " * reps) + "clean energy."
        return _best_of_3(lambda: fn(sent))

    def test_attribution_hit_scales_linearly(self):
        base = self._elapsed(pf.rail2_attribution_hit, 2000)
        dbl = self._elapsed(pf.rail2_attribution_hit, 4000)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)

    def test_pathological_clause_runs(self):
        sent = "F3 Energy " + ("," * 20000) + " clean"
        assert _best_of_3(pf.rail2_attribution_hit, sent) < 1.0


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
                dt = _best_of_3(pat.sub, " ", text)
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
            return _best_of_3(lambda: pf.rail2_attribution_hit(sent))
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


class TestTokenizerGaps:
    """r143-claims-8: whole-token additions (never a prefix match)."""

    MUST_TRIP = (
        "F3 Energy burns cleanly.",
        "F3 Energy delivers energy cleanly.",
        "F3 Energy is cleansed of junk.",
        "F3 Energy's cleanliness sets it apart.",
        "F3 Energy's cleanness sets it apart.",
        "F3 Energy's naturalness sets it apart.",
        "F3 Mood is one of the naturals.",
        "F3 Energy is CLEANLY made.",                 # case never matters
        "F3 Energy is cleanly-made.",                 # the hyphen split reaches the new tokens
    )

    @pytest.mark.parametrize("sentence", MUST_TRIP)
    def test_the_gap_tokens_trip(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is not None, sentence
        assert "R2" in _pf(sentence).tripped_rail_ids, sentence

    def test_the_gap_shapes_are_in_the_gate(self):
        for s in self.MUST_TRIP[:7]:
            assert s in rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"], s

    def test_no_prefix_matching_the_partner_name_is_not_a_claim(self):
        assert "cleanhub" not in pf._ATTRIBUTION_CLEAN_TOKENS
        assert pf.rail2_attribution_hit(rh.FALSE_POSITIVE_SET[1]) is None      # the CleanHub sentence
        assert pf.rail2_attribution_hit("F3 Energy partners with CleanHub.") is None

    def test_the_new_tokens_still_clear_on_pure(self):
        assert pf.rail2_attribution_hit("F3 Pure is cleanly sweetened.") is None
        assert _pf("F3 Pure's cleanliness sets it apart.").passed

    def test_the_legacy_token_set_is_still_frozen(self):
        for tok in ("cleanly", "cleansed", "cleanliness", "cleanness", "naturalness", "naturals"):
            assert tok not in pf._CLEAN_TOKENS and tok in pf._ATTRIBUTION_CLEAN_TOKENS, tok


class TestEnvironmentalContinuationIsAllowlisted:
    """r143-claims-4 / r143-claims-6: the environmental redaction's metaphor guard was
    a BLACKLIST (of / in / inside / within), so every other continuation kept the
    redaction and cleared a product metaphor. The noun form had no position check at
    all, and a copula before it cleared a predicate of the brand."""

    MUST_TRIP = (
        # r143-claims-4
        "F3 Energy builds a cleaner world for your taste buds.",
        "F3 Energy restores clean earth to your routine.",
        "F3 Energy builds a cleaner world at every workout.",
        "F3 Energy protects clean air for your lungs.",
        "F3 Mood restores a clean environment for your mind.",
        "F3 Mood supports a clean environment for your mind.",
        "F3 Mood supports a clean environment, for your mind.",
        "F3 Energy builds a cleaner world with every sip.",
        "F3 Energy builds a cleaner world that tastes like citrus.",
        "F3 Energy supports clean water and air for your lungs.",
        # r143-claims-6
        "F3 Energy is the community clean-up crew for your afternoon slump.",
        "F3 Energy is a beach clean-up in a can.",
        "F3 Energy is a community clean-up for your gut.",
        "F3 Energy cleans up trash talk in the gym.",
        "F3 Energy: a beach clean-up for the soul.",
        "F3 Energy delivers a park clean-up for your mind.",
    )
    RELEASED = (
        "F3 Energy supports clean water for the community.",
        "F3 Energy funds a cleaner planet through CleanHub.",
        "F3 Energy helps build a cleaner planet one case at a time.",
        "F3 Energy funds cleaner oceans by removing plastic.",
        "Join us at the F3 Energy beach clean-up this Saturday.",
        "F3 Energy's annual beach clean-up pulled 400 pounds of trash off the shoreline.",
        "Beach clean-ups are better with F3 Energy.",
        "F3 Energy volunteers helped clean up the beach, pulling 400 pounds of trash.",
        "Our team cleaned up the park with F3 Energy.",
        "F3 Energy fuels the community clean-up every spring.",
    )

    @pytest.mark.parametrize("sentence", MUST_TRIP)
    def test_a_metaphorical_or_predicated_environment_trips(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is not None, sentence
        assert "R2" in _pf(sentence).tripped_rail_ids, sentence

    def test_the_review_shapes_are_in_the_gate(self):
        probes = rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"]
        for s in self.MUST_TRIP[:8] + self.MUST_TRIP[10:14]:
            assert s in probes, s

    @pytest.mark.parametrize("sentence", RELEASED
                             + TestEnvironmentalRedactionIsPositionChecked.RELEASED
                             + rh.FALSE_POSITIVE_SET[1:3])
    def test_an_environmental_object_of_an_environmental_action_is_still_released(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is None, sentence
        assert _pf(sentence).passed, _pf(sentence).render()

    def test_the_event_noun_needs_a_reference_not_a_copula(self):
        def ref(s):
            m = pf._ENV_EVENT_RE.search(s)
            assert m, s
            return pf._env_event_referenced(s, m.start())
        assert ref("F3 Energy sponsors a beach clean-up.")
        assert ref("Join us at the F3 Energy beach clean-up.")
        assert ref("Beach clean-ups are better with F3 Energy.")
        assert not ref("F3 Energy is a beach clean-up.")
        assert not ref("F3 Energy: a beach clean-up.")
        assert not ref("F3 Energy delivers a beach clean-up.")

    DEGENERATE = (
        " " * 40000,
        "\t" * 40000,
        "fund a clean planet " * 2000,
        "fund a clean planet ," * 2000,
        "fund a clean planet with every " * 1300,
        "fund a clean planet for the " * 1400,
        "fund a cleaner future for the " * 1300,
        "support clean water" + " " * 40000 + "for",
        "clean up the beach " * 2100,
        "clean up the beach with the " * 1400,
        "beach clean-up " * 2600,
        "beach clean-up in a " * 2000,
        "a " * 20000 + "beach clean-up",
        "the " * 10000 + "beach clean-up",
        "sponsors a beach clean-up for the " * 1100,
    )

    @pytest.mark.parametrize("text", DEGENERATE, ids=range(len(DEGENERATE)))
    def test_d171_environmental_patterns_are_fast_at_40k(self, text):
        for pat in pf._CLEAN_ENVIRONMENT_RES + (pf._ENV_EVENT_RE,):
            dt = _best_of_3(lambda: pat.sub(" ", text))
            assert dt < 0.2, "%s on %r...: %.3fs" % (pat.pattern[:30], text[:20], dt)
        dt = _best_of_3(lambda: pf._redact_env_events(text))
        assert dt < 0.2, "_redact_env_events on %r...: %.3fs" % (text[:20], dt)

    @pytest.mark.parametrize("unit", ["fund a clean planet with every ", "clean up the beach ",
                                      "sponsors a beach clean-up ", "a ", "fund a clean planet , "])
    def test_d171_environmental_redaction_scales_linearly(self, unit):
        def run(n):
            text = "F3 Energy " + unit * n + "clean."
            return _best_of_3(lambda: pf.rail2_attribution_hit(text))
        base, dbl = run(1500), run(3000)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)


class TestPositivePureAttachment:
    """r143-claims-1 (HIGH) / r143-claims-3 (HIGH): the first cut let a clause's OWN
    brand win outright, so any clause that merely NAMED Pure cleared its clean word,
    and a bare-brand fold REPLACED the host clause's lines. Each shape below was
    legacy TRIP / shipping PASS."""

    CLAIMS_1 = (
        "F3 Energy, clean like F3 Pure, hits hard.",
        "F3 Energy, now made with the same clean-sweetened base as F3 Pure, hits hard.",
        "F3 Energy, which shares F3 Pure's clean base, hits hard.",
        "F3 Energy, built on F3 Pure's clean-label formula, is here.",
        "As clean as F3 Pure, F3 Energy delivers all day.",
        "Clean-sweetened like F3 Pure, F3 Energy is here.",
        "F3 Energy: as clean as F3 Pure.",
        "F3 Energy, F3 Pure's all-natural sibling, is here.",
        "F3 Mood, as natural as F3 Pure, keeps you calm.",
        "F3 Mood, the clean companion to F3 Pure, winds you down.",
        "F3 Energy keeps the full stack while staying as clean as F3 Pure.",
        "Like F3 Pure's clean formula, F3 Energy's is too.",
        "F3 Energy delivers the stack, as clean as F3 Pure.",
        "F3 Energy, with the same clean-label promise as F3 Pure.",
        "F3 Pure is clean-sweetened, and F3 Energy is too.",
        "F3 Pure is clean, like F3 Energy.",
        "F3 Pure is all-natural, and so is F3 Energy.",
        "F3 Pure is clean-sweetened, as is F3 Energy.",
        "Like F3 Energy, F3 Pure is clean-sweetened.",
        "F3 Energy, or the clean version of F3 Pure, hits hard.",
        "Clean energy from F3 Pure, or F3 Energy.",
    )
    CLAIMS_3 = (
        "F3 Energy: all-natural, F3 Pure too.",
        "F3 Energy: clean-sweetened, F3 Pure.",
        "F3 Mood: clean, and F3 Pure too.",
        "F3 Energy: zero sugar, 200mg caffeine, all-natural flavors, and F3 Pure too.",
        "F3 Mood: caffeine-free, clean, and F3 Pure too.",
        "F3 Energy is great, clean, and F3 Pure too.",
    )
    STILL_PASS = (
        "Explore the full stack in F3 Energy or the clean-sweetened version in F3 Pure.",
        "Explore the full stack in F3 Energy, or the clean-sweetened version in F3 Pure.",
        "Explore the clean-sweetened version in F3 Pure or the full stack in F3 Energy.",
        "F3 Pure is clean-sweetened; F3 Energy is the full stack.",
        "F3 Energy carries the full stack; F3 Pure is the clean-sweetened version.",
        "Unlike F3 Energy, F3 Pure is clean-sweetened.",
        "F3 Energy carries the stack, whereas F3 Pure is clean-sweetened.",
        "F3 Energy carries the stack while F3 Pure is clean-sweetened.",
        "F3 Energy is the full stack, and F3 Pure is clean-sweetened.",
        "F3 Pure is clean-sweetened, and F3 Energy carries the stack.",
        "If you like F3 Energy, F3 Pure is the clean-sweetened pick.",
        "In the cooler, F3 Pure is the clean-sweetened pick; F3 Energy is the stack.",
        "The F3 Pure line is clean-sweetened, while F3 Energy carries the stack.",
        "F3 Energy has the full stack; F3 Pure is clean-sweetened, and both taste great.",
    )
    #: Accepted fail-closed consequences. Each LEGACY-trips and each passed the
    #: pre-remediation shipping rail. They trip now because the attachment is not
    #: positive: gapping (no verb in the Pure clause), a brand-less appositive, and
    #: an "also" in a later clause. The ruling does not require releasing them, and
    #: the drafting prompt's "two sentences, one line each" style avoids them.
    #: Releasing one needs a ruling, not a heuristic.
    ACCEPTED_TRIPS = (
        "F3 Energy carries the full stack, F3 Pure the clean-sweetened base.",
        "F3 Pure, clean-sweetened, is the new can next to F3 Energy.",
        "Explore the full stack in F3 Energy or the clean-sweetened version in F3 Pure, also in 12-packs.",
    )

    @pytest.mark.parametrize("sentence", CLAIMS_1 + CLAIMS_3)
    def test_a_clean_word_that_is_not_positively_pures_trips(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is not None, sentence
        assert "R2" in _pf(sentence).tripped_rail_ids, sentence
        assert pf.rail2_legacy_hit(sentence) is not None   # a loosening R14-3 must not keep

    def test_every_shape_is_in_the_gate(self):
        probes = rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"]
        for s in self.CLAIMS_1 + self.CLAIMS_3:
            assert s in probes, s

    @pytest.mark.parametrize("field", ["title", "summary", "alt", "jsonld"])
    def test_every_shipped_field_is_covered(self, field):
        s = "F3 Energy: As Clean As F3 Pure"
        kw = {"title": "Post", "summary": "", "body_html": "<p>x</p>"}
        if field == "title":
            kw["title"] = s
        elif field == "summary":
            kw["summary"] = s
        elif field == "alt":
            kw["body_html"] = '<p><img src="a.png" alt="%s"></p>' % s
        else:
            kw["body_html"] = '<script type="application/ld+json">{"description":"%s"}</script><p>x</p>' % s
        assert "R2" in pf.run_preflight(**kw).tripped_rail_ids
        assert not rh.legacy_run(**kw).passed

    @pytest.mark.parametrize("sentence", STILL_PASS)
    def test_positively_pure_attached_clean_words_still_clear(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is None, sentence
        assert _pf(sentence).passed, _pf(sentence).render()

    @pytest.mark.parametrize("sentence", ACCEPTED_TRIPS)
    def test_accepted_fail_closed_consequences_trip(self, sentence):
        assert pf.rail2_legacy_hit(sentence) is not None
        assert "R2" in _pf(sentence).tripped_rail_ids, sentence

    def test_pure_subject_detector(self):
        yes = ("F3 Pure is clean.", "and F3 Pure uses cane sugar", "The F3 Pure line is clean",
               "Pure is the clean line", "while F3 Pure now carries it", "F3's Pure is clean")
        no = ("F3 Pure's clean base", "as clean as F3 Pure", "F3 Pure without the stack",
              "the clean version in F3 Pure", "F3 Pure the clean-sweetened base", "pure is clean")
        for s in yes:
            assert pf._pure_is_subject(pf._words(s)), s
        for s in no:
            assert not pf._pure_is_subject(pf._words(s)), s

    def test_locative_disjunct_detector_needs_an_edge_of_an_or(self):
        w = pf._words(" the clean-sweetened version in F3 Pure.")
        other = pf._words("Explore the full stack in F3 Energy")
        assert pf._pure_locative_disjunct(w, 1, 2, frozenset({"or"}), frozenset(), other)
        assert not pf._pure_locative_disjunct(w, 1, 3, frozenset({"or"}), frozenset({","}), other)   # an apposition
        assert not pf._pure_locative_disjunct(w, 1, 2, frozenset({","}), frozenset(), other)          # no disjunction
        assert not pf._pure_locative_disjunct(pf._words(" the version in F3 Pure, all clean"),
                                              1, 2, frozenset({"or"}), frozenset(), other)           # clean outside the NP

    def test_relation_detector(self):
        for s in ("as clean as F3 Pure", "and F3 Energy is too", "and so is F3 Energy", "as is F3 Energy",
                  "like F3 Energy", "the same base", "F3 Energy does", "shares its base", "F3 Energy also"):
            assert pf._has_relation(pf._words(s)), s
        for s in ("if you like F3 Energy", "F3 Energy carries the stack", "unlike F3 Energy",
                  "F3 Energy does not"):
            assert not pf._has_relation(pf._words(s)), s
        assert pf._has_relation(pf._words("and both are clean"))
        assert not pf._has_relation(pf._words("and both taste great"), weak=True)

    def test_a_bare_brand_never_folds_into_an_empty_segment(self):
        clauses = pf._rail2_clauses("Clean energy from F3 Pure, or F3 Energy.")
        assert len(clauses) == 1 and clauses[0].bare == [" F3 Energy."]
        assert pf.brand_lines_in(" ".join(clauses[0].bare)) == {"ENERGY"}

    DEGENERATE = (
        ", " * 20000,
        "or " * 13000,
        " " * 40000,
        "F3 Pure is clean, " * 2200,
        "as clean as F3 Pure, " * 1900,
        "like " * 8000,
        "the clean version in F3 Pure or " * 1200,
        "F3 Energy, F3 Pure" * 2200,
        "as " * 13000,
        "so is " * 6600,
    )

    @pytest.mark.parametrize("text", DEGENERATE, ids=range(len(DEGENERATE)))
    def test_d171_the_shipping_hit_is_fast_at_40k(self, text):
        sent = "F3 Energy " + text + " clean."
        dt = _best_of_3(lambda: pf.rail2_attribution_hit(sent))
        assert dt < 1.0, "%r...: %.3fs" % (text[:20], dt)

    @pytest.mark.parametrize("unit", [", F3 Pure is clean", " as clean as F3 Pure,", " like", " or the clean version in F3 Pure",
                                      " and so is it,"])
    def test_d171_the_shipping_hit_scales_linearly(self, unit):
        def run(n):
            sent = "F3 Energy" + unit * n + " clean."
            return _best_of_3(lambda: pf.rail2_attribution_hit(sent))
        base, dbl = run(1500), run(3000)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)


class TestCrossSentenceReference:
    """r143-claims-5: every rail-2 check was sentence-scoped, so a pronoun laundered a
    clean word across a sentence boundary. The Mood fail-closed guard on the two
    ruled phrases was defeated the same way ("F3 Mood is our evening can. Like F3
    Energy, it runs on a cleaner fuel source." cleared the phrase for Mood).
    run_preflight now carries each sentence's lines to the next (rail2_context_after)."""

    MUST_TRIP = (
        "F3 Mood is our evening can. Like F3 Energy, it runs on a cleaner fuel source.",
        "F3 Mood is our evening can. Like F3 Energy, it runs on natural caffeine from green tea.",
        "F3 Mood is our evening can. It is all-natural.",
        "F3 Mood is our evening can. It runs on a cleaner fuel source.",
        "F3 Energy is our training can. We love it because it is all-natural.",
        "F3 Energy is great. It is F3 Pure's clean sibling.",
        "F3 Mood is calm. F3 Pure is clean-sweetened, and it is too.",
        "F3 Mood is our evening can. This is the clean way to wind down.",
        "F3 Mood is the evening can. Our team loves the flavor. It is all-natural.",   # carried past a brand-less sentence
    )
    STILL_PASS = (
        "F3 Mood is caffeine-free. F3 Energy carries 120 mg of natural caffeine from green tea, "
        "and its L-theanine keeps it smooth.",                       # the pronoun is not in the phrase's clause
        "F3 Mood is caffeine-free. F3 Pure and F3 Energy both carry natural caffeine from green tea.",
        "F3 Mood is calm. F3 Pure is clean-sweetened, and it tastes great.",
        "F3 Pure is our clean-sweetened can. It is all-natural.",   # "It" = Pure
        "F3 Mood keeps you calm. F3 Pure is clean-sweetened. It is all-natural.",
        "F3 Energy is the stack. F3 Pure is clean-sweetened, unlike it.",
        "F3 Energy carries the full stack. F3 Pure is the clean-sweetened version.",
    )

    @pytest.mark.parametrize("text", MUST_TRIP)
    def test_a_pronoun_cannot_launder_a_clean_word_across_sentences(self, text):
        assert "R2" in _pf(text).tripped_rail_ids, text
        assert not rh.new_preflight(text).passed

    def test_the_carry_is_what_catches_it(self):
        """Proof that these are CROSS-sentence closures: the plain single-sentence
        check passes the second sentence, and the carried check trips it."""
        for text in self.MUST_TRIP[:4]:
            first, second = pf.sentences(text)
            carried = pf.rail2_context_after(first, frozenset())
            assert pf.rail2_attribution_hit(second) is None, second
            assert pf.rail2_attribution_hit(second, context_lines=carried) is not None, second

    def test_the_review_shapes_are_in_the_gate(self):
        for s in self.MUST_TRIP[:7]:
            assert s in rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"], s

    @pytest.mark.parametrize("text", STILL_PASS)
    def test_a_resolvable_or_harmless_pronoun_still_passes(self, text):
        r = _pf(text)
        assert r.passed, r.render()

    def test_the_carry_crosses_paragraphs_and_fields(self):
        para = pf.run_preflight(title="t", summary="",
                                body_html="<p>F3 Mood is our evening can.</p><p>It is all-natural.</p>")
        assert "R2" in para.tripped_rail_ids
        field = pf.run_preflight(title="F3 Mood Tonight", summary="", body_html="<p>It is all-natural.</p>")
        assert "R2" in field.tripped_rail_ids

    def test_context_is_additive_only(self):
        """With no back-reference, or an empty context, the carried check IS the plain one."""
        for s in ("F3 Energy is clean.", "F3 Pure is clean-sweetened; F3 Energy is the full stack.",
                  "Clean living matters.", rh.FALSE_POSITIVE_SET[0]):
            for ctx in (frozenset(), frozenset({"MOOD"}), frozenset({"ENERGY", "MOOD"})):
                plain = pf.rail2_attribution_hit(s)
                carried = pf.rail2_attribution_hit(s, context_lines=ctx)
                assert (plain is None) == (carried is None), (s, ctx)

    def test_context_after(self):
        e = frozenset({"ENERGY"})
        assert pf.rail2_context_after("F3 Mood is calm.", e) == {"MOOD"}
        assert pf.rail2_context_after("Our team loves it.", e) == e              # brand-less: keep
        assert pf.rail2_context_after("It pairs with F3 Pure.", e) == {"PURE", "ENERGY"}
        assert pf.rail2_context_after("F3 Pure is great.", e) == {"PURE"}

    def test_the_source_pin_still_ships_the_attribution_rail(self):
        src = "\n".join(ln for ln in inspect.getsource(pf.run_preflight).splitlines()
                        if not ln.strip().startswith("#"))
        assert "rail2_attribution_hit(sent, context_lines=carried)" in src
        assert "rail2_context_after(sent, carried)" in src

    def test_d171_the_carry_is_linear(self):
        def run(n):
            body = "<p>" + ("F3 Mood is calm. It is great, and this is it. " * n) + "</p>"
            return _best_of_3(lambda: pf.run_preflight(title="t", summary="", body_html=body))
        base, dbl = run(400), run(800)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)

    @pytest.mark.parametrize("text", ["it " * 13000, "this " * 8000, "it, " * 10000, " " * 40000],
                             ids=range(4))   # a 40k id overflows PYTEST_CURRENT_TEST on Windows
    def test_d171_back_reference_scan_is_fast_at_40k(self, text):
        dt = _best_of_3(lambda: pf.rail2_attribution_hit("F3 Energy " + text + " clean.",
                                                         context_lines=frozenset({"MOOD"})))
        assert dt < 1.0, "%r...: %.3fs" % (text[:12], dt)


class TestRemediationSelfReview:
    """Holes found by adversarially probing THIS remediation's own new code (D-051
    doctrine: a fix is a new surface). Each was legacy TRIP / shipping PASS on the
    remediation's first cut."""

    MUST_TRIP = (
        # P2 accepted a PREDICATE disjunct (Energy/Mood the subject of the other side)
        # and a verb-phrase disjunct sharing an Energy/Mood subject
        "F3 Mood is calm or the natural pick of F3 Pure.",
        "F3 Energy is the stack or the clean version in F3 Pure.",
        "F3 Mood keeps you calm or delivers the natural calm of F3 Pure.",
        # the environmental comma allowance let ", and <metaphor tail>" through
        "F3 Mood supports a clean environment, and your mind.",
        "F3 Mood supports a clean environment, and for your mind.",
        # a COORDINATE adjective before a ruled phrase is a modifier, not a list item
        "F3 Energy has real, natural caffeine from green tea.",
    )
    STILL_PASS = (
        "Explore the full stack in F3 Energy or the clean-sweetened version in F3 Pure.",
        "Explore the clean-sweetened version in F3 Pure or the full stack in F3 Energy.",
        "Try the clean-sweetened version in F3 Pure or the full stack in F3 Energy.",
        "Explore the full stack in F3 Energy, or the clean-sweetened version in F3 Pure.",
        "F3 Energy funds a cleaner planet, and fans love it.",
        "Same flavor, cleaner fuel: F3 Pure is F3 Energy with a cleaner fuel source.",
        "F3 Energy has L-theanine, natural caffeine from green tea and a nootropic stack.",
    )

    @pytest.mark.parametrize("sentence", MUST_TRIP)
    def test_the_self_review_holes_trip(self, sentence):
        assert pf.rail2_legacy_hit(sentence) is not None
        assert "R2" in _pf(sentence).tripped_rail_ids, sentence

    def test_the_self_review_holes_are_in_the_gate(self):
        for s in self.MUST_TRIP:
            assert s in rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"], s

    @pytest.mark.parametrize("sentence", STILL_PASS)
    def test_the_parallel_disjunct_and_plain_lists_still_clear(self, sentence):
        r = _pf(sentence)
        assert r.passed, r.render()

    def test_the_other_disjunct_must_name_its_line_after_a_locative(self):
        assert pf._brand_after_locative(pf._words("Explore the full stack in F3 Energy"), "ENERGY")
        assert not pf._brand_after_locative(pf._words("F3 Energy is the stack"), "ENERGY")
        assert not pf._brand_after_locative(pf._words("the stack"), "ENERGY")

    DEGENERATE = (
        "fund a clean planet , and " * 1500,
        "fund a clean planet, and your " * 1300,
        "fund a clean planet," + " " * 40000 + "and your",
        "fund a clean planet,&" * 2000,
        ", and " * 6600,
    )

    @pytest.mark.parametrize("text", DEGENERATE, ids=range(len(DEGENERATE)))
    def test_d171_the_edited_comma_allowance_is_fast_at_40k(self, text):
        for pat in pf._CLEAN_ENVIRONMENT_RES:
            dt = _best_of_3(lambda: pat.sub(" ", text))
            assert dt < 0.2, "%s on %r...: %.3fs" % (pat.pattern[:30], text[:20], dt)

    @pytest.mark.parametrize("unit", [" or the clean version in F3 Pure", "real, natural caffeine from green tea ",
                                      "fund a clean planet, and your "])
    def test_d171_the_follow_up_scales_linearly(self, unit):
        def run(n):
            sent = "Explore the full stack in F3 Energy" + unit * n + " clean."
            return _best_of_3(lambda: pf.rail2_attribution_hit(sent))
        base, dbl = run(1500), run(3000)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)


class TestIdiomOverTripsAreFailClosed:
    """r143-claims-7, DECIDED FAIL-CLOSED: the verb tokens over-trip non-claim idioms
    and they are NOT redacted. Every redaction candidate clears a claim shape of its
    own, and an over-trip costs one bounded revision, not a leak. This pins the
    decision, so reversing it has to be a deliberate edit."""

    OVER_TRIPS = (
        "Power through spring cleaning with F3 Energy.",
        "Fans cleaned out every cooler of F3 Energy at the event.",
        "F3 Energy fuels your clean-and-jerk.",
    )
    # what the obvious redactions ("cleaned out", "spring cleaning", "clean and jerk")
    # would also have cleared
    CLAIMS_A_REDACTION_WOULD_CLEAR = (
        "F3 Energy cleaned out my system.",
        "F3 Energy is a spring cleaning for your body.",
        "F3 Energy is clean and jerk-free.",
    )

    @pytest.mark.parametrize("sentence", OVER_TRIPS + CLAIMS_A_REDACTION_WOULD_CLEAR)
    def test_both_the_idiom_and_its_claim_twin_trip(self, sentence):
        assert "R2" in _pf(sentence).tripped_rail_ids, sentence


# ---------------------------------------------------------------------------
# D-051 ROUND 2 (Code #14 re-review of the r143 remediation, bca189c). Each class
# names the regression (F3-Rn) or the PARTIAL closure (r143-claims-n) it pins. A
# must-PASS row was cleared by the pre-remediation rail and re-tripped by round 1;
# a must-TRIP row trips the frozen legacy rail and passed round 1.
# ---------------------------------------------------------------------------


class TestRound2PhraseQuantities:
    """F3-R2: the exact-phrase edge check read every digit-bearing token and every
    "-ly" word as a degree modifier, so "120mg natural caffeine from green tea" and
    "uses only natural caffeine ..." re-tripped the phrase ruled cleared on Energy
    (D-329). r143-claims-2 PARTIAL: unlisted colloquial intensifiers still cleared."""

    RELEASED = rh.RELEASE_PROBES[:7]
    MUST_TRIP = (
        "F3 Energy runs on way cleaner fuel.",
        "F3 Energy runs on a way cleaner fuel source.",
        "F3 Energy runs on miles cleaner fuel.",
        "F3 Energy has next-level natural caffeine from green tea.",
        "F3 Energy has straight-up natural caffeine from green tea.",
        "F3 Energy has refreshingly natural caffeine from green tea.",   # unlisted -ly: fail closed
        "F3 Energy has 2x cleaner fuel.",                                 # a multiplier is a degree
        "F3 Energy delivers 120 natural caffeine from green tea.",        # a bare number still counts
    )

    @pytest.mark.parametrize("sentence", RELEASED)
    def test_a_quantity_or_focus_particle_keeps_the_ruled_phrase_exact(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is None, sentence
        r = _pf(sentence)
        assert r.passed, r.render()
        assert pf.rail2_legacy_hit(sentence) is not None   # the ruling, not legacy, releases it

    @pytest.mark.parametrize("sentence", MUST_TRIP)
    def test_an_intensifier_is_still_a_modifier(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is not None, sentence
        assert "R2" in _pf(sentence).tripped_rail_ids, sentence

    def test_the_intensifier_holes_are_in_the_gate(self):
        probes = rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"]
        for s in self.MUST_TRIP[:7]:
            assert s in probes, s

    def test_the_release_probes_are_gated(self):
        v = rh.evaluate()
        assert v.release_tripping == [] and v.ship is True
        assert "round-2 release probes (D-051): attribution trips 0/%d" % len(rh.RELEASE_PROBES) \
            in v.summary_lines()

    def test_a_release_probe_regression_closes_the_gate(self, monkeypatch):
        monkeypatch.setattr(rh, "RELEASE_PROBES", rh.RELEASE_PROBES + ("F3 Energy is clean.",))
        v = rh.evaluate()
        assert v.ship is False and v.release_tripping == ["F3 Energy is clean."]

    def test_modifier_classification(self):
        for w in ("only", "solely", "exclusively", "daily", "120mg", "120-mg", "12oz", "12-pack",
                  "71mg", "200mg"):
            assert not pf._is_phrase_modifier(w), w
        for w in ("refreshingly", "insanely", "2x", "100%", "100", "120", "way", "miles",
                  "next-level", "straight-up", "100-percent", "purely", "totally"):
            assert pf._is_phrase_modifier(w), w

    def test_way_is_a_noun_only_after_a_definite_determiner(self):
        assert pf.rail2_attribution_hit("F3 Energy changes the way natural caffeine from green tea hits.") is None
        assert pf.rail2_attribution_hit("F3 Energy is one way natural caffeine from green tea fits a day.") is None
        assert pf.rail2_attribution_hit("F3 Energy runs on a way cleaner fuel source.") is not None
        assert pf.rail2_attribution_hit("F3 Energy runs on way cleaner fuel.") is not None

    def test_the_pinned_edge_probes_still_trip(self):
        for s in TestExactPhraseEdges.MUST_TRIP:
            assert pf.rail2_attribution_hit(s) is not None, s

    DEGENERATE = (
        "9" * 40000,
        "1" * 20000 + "mg",
        "120mg " * 6600,
        "the way " * 5000 + "natural caffeine from green tea",
        "only " * 8000 + "natural caffeine from green tea",
        "120mg natural caffeine from green tea " * 1050,
    )

    @pytest.mark.parametrize("text", DEGENERATE, ids=range(len(DEGENERATE)))
    def test_d171_the_quantity_rule_is_fast_at_40k(self, text):
        dt = _best_of_3(lambda: pf._PHRASE_QUANTITY_RE.match(text))
        assert dt < 0.2, "%r...: %.3fs" % (text[:12], dt)
        for pat, _ in pf._RAIL2_PHRASE_EXEMPTIONS:
            dt = _best_of_3(lambda: pf._redact_exact_phrase(pat, text))
            assert dt < 0.2, "%s on %r...: %.3fs" % (pat.pattern[:30], text[:12], dt)

    @pytest.mark.parametrize("unit", ["120mg natural caffeine from green tea ",
                                      "the way natural caffeine from green tea ",
                                      "only natural caffeine from green tea "])
    def test_d171_the_quantity_rule_scales_linearly(self, unit):
        def run(n):
            text = "F3 Energy " + unit * n
            return _best_of_3(lambda: pf.rail2_attribution_hit(text))
        base, dbl = run(1500), run(3000)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)


class TestRound2EnvironmentalPunctuation:
    """F3-R3 + r143-claims-4 PARTIAL: round 1 made the bare continuation an allowlist
    but kept the COMMA branch a refuse-list, so it refused its own ", one case at a
    time" (natural CleanHub copy re-tripped) and let every participle / "so" /
    adjective metaphor tail through; ";" and ":" ended the clause unchecked. Now the
    punctuation branch takes only the CSR allowlist, a CSR participle, or a
    third-party clause -- the last two with no "you"/"your" to the sentence end."""

    RELEASED = (
        "F3 Energy funds a cleaner planet, one case at a time.",
        "F3 Energy helps build a cleaner planet, one can at a time.",
        "F3 Energy funds a cleaner planet, with every case sold.",
        "F3 Energy funds a cleaner planet, through CleanHub.",
        "F3 Energy funds cleaner oceans, by removing plastic.",
        "F3 Energy helps clean up the beaches, every spring.",
        "F3 Energy funds a cleaner planet; fans love it.",
        "F3 Energy funds a cleaner planet: every case sold plants a tree.",
        "F3 Energy funds a cleaner planet, and our volunteers log every pound.",
        "F3 Energy volunteers helped clean up the beach, pulling 400 pounds of trash.",   # pinned
        "F3 Energy funds a cleaner planet, and fans love it.",                            # pinned
    )
    MUST_TRIP = (
        "F3 Mood supports a clean environment, helping you unwind.",
        "F3 Mood restores a clean environment, so you can relax.",
        "F3 Mood builds a clean environment, letting your mind settle.",
        "F3 Energy builds a cleaner world, so your workout hits harder.",
        "F3 Mood supports a clean environment, perfect for evenings.",
        "F3 Mood supports a clean environment, keeping your mind calm.",
        "F3 Mood restores a clean environment, sip after sip.",
        "F3 Energy builds a cleaner world, bottled.",
        "F3 Mood supports a clean environment: helping you unwind.",
        "F3 Mood supports a clean environment; your mind will thank you.",
        "F3 Energy builds a cleaner world, removing your stress.",
        "F3 Mood supports a clean environment, and we keep your mind calm.",
        "F3 Energy builds a cleaner world, one sip at a time.",          # the reason "one" was refused
        "F3 Mood supports a clean environment, for your mind.",          # pinned
        "F3 Mood supports a clean environment, and your mind.",          # pinned
    )

    @pytest.mark.parametrize("sentence", RELEASED)
    def test_a_csr_continuation_after_punctuation_is_released(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is None, sentence
        r = _pf(sentence)
        assert r.passed, r.render()

    @pytest.mark.parametrize("sentence", MUST_TRIP)
    def test_a_metaphor_tail_after_punctuation_trips(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is not None, sentence
        assert "R2" in _pf(sentence).tripped_rail_ids, sentence
        assert pf.rail2_legacy_hit(sentence) is not None

    def test_the_metaphor_tails_are_in_the_gate(self):
        probes = rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"]
        for s in self.MUST_TRIP[:12]:
            assert s in probes, s

    def test_the_f3_r3_rows_are_release_probes(self):
        for s in self.RELEASED[:2]:
            assert s in rh.RELEASE_PROBES, s

    def test_the_title_field_is_covered(self):
        r = pf.run_preflight(title="F3 Mood Supports A Clean Environment, Helping You Unwind",
                             summary="", body_html="<p>x</p>")
        assert "R2" in r.tripped_rail_ids

    def test_the_person_guard_is_bounded_and_fails_closed(self):
        """A continuation whose sentence runs past the 300-character window is not
        cleared (fail closed), and a second person anywhere inside it blocks it."""
        long_tail = "F3 Energy funds a cleaner planet, and fans " + "cheer " * 60 + "on."
        assert pf.rail2_attribution_hit(long_tail) is not None
        near = "F3 Energy funds a cleaner planet, and fans cheer as you arrive."
        assert pf.rail2_attribution_hit(near) is not None
        assert pf.rail2_attribution_hit("F3 Energy funds a cleaner planet, and fans cheer on.") is None

    DEGENERATE = (
        "fund a clean planet, and fans " * 1400,
        "fund a clean planet; " * 1900,
        "fund a clean planet, pulling " + "x" * 40000,
        "fund a clean planet, and fans " + " " * 40000,
        "fund a clean planet," + "y" * 40000,
        "clean up the beach, pulling " * 1400,
        "fund a clean planet, one case at a time, " * 950,
        "fund a clean planet: " + "you " * 10000,
    )

    @pytest.mark.parametrize("text", DEGENERATE, ids=range(len(DEGENERATE)))
    def test_d171_the_punctuation_branch_is_fast_at_40k(self, text):
        for pat in pf._CLEAN_ENVIRONMENT_RES:
            dt = _best_of_3(lambda: pat.sub(" ", text))
            assert dt < 0.2, "%s on %r...: %.3fs" % (pat.pattern[:30], text[:20], dt)

    @pytest.mark.parametrize("unit", ["fund a clean planet, and fans ", "fund a clean planet; ",
                                      "clean up the beach, pulling ", "fund a clean planet, one case at a time, "])
    def test_d171_the_punctuation_branch_scales_linearly(self, unit):
        def run(n):
            text = "F3 Energy " + unit * n + "clean."
            return _best_of_3(lambda: pf.rail2_attribution_hit(text))
        base, dbl = run(1200), run(2400)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)


def _article_kw(title="Post", summary="", body_html="<p>x</p>"):
    return {"title": title, "summary": summary, "body_html": body_html}


def _r2_sentence_trips(**kw):
    return [t for t in pf.run_preflight(**kw).trips if t.rail_id == "R2"]


class TestRound2CarryDecay:
    """F3-R1 (MEDIUM): the round-1 carry never decayed and read every it/its/they,
    so ingredient paragraphs, idioms and a Pure sentence's own "its" tripped "near
    ENERGY" long after the only Energy mention -- invisible to the single-sentence
    gate. THE RULE (Rail2Carry): the next sentence, or the one after a single
    brand-less sentence in the naming sentence's block; a new block's first
    sentence may point back, its second may not; a pronoun chain renews; the title
    stays in view for each later field's first sentence."""

    @pytest.mark.parametrize("label,kw", rh.CARRY_RELEASE_PROBES, ids=range(len(rh.CARRY_RELEASE_PROBES)))
    def test_article_copy_the_carry_re_tripped_now_passes(self, label, kw):
        r = pf.run_preflight(**kw)
        assert r.passed, (label, r.render())
        assert rh.rail2_article_walk(pf.rail_fields(**kw)) == [], label   # not one sentence, not only the first
        if not label.startswith("queued row"):
            # the frozen baseline never tripped the carry shapes (the queued-row drafts
            # also carry the ruled green-tea phrase, which legacy trips by design)
            assert rh.legacy_run(**kw).passed, label

    @pytest.mark.parametrize("label,kw", rh.CARRY_HOLE_PROBES, ids=range(len(rh.CARRY_HOLE_PROBES)))
    def test_the_carry_still_catches_a_pronoun_in_reach(self, label, kw):
        assert _r2_sentence_trips(**kw), label

    @pytest.mark.parametrize("text", TestCrossSentenceReference.MUST_TRIP)
    def test_every_round_one_must_trip_probe_still_trips(self, text):
        assert "R2" in _pf(text).tripped_rail_ids, text

    def test_both_article_sets_are_gated(self):
        v = rh.evaluate()
        assert v.ship is True and v.carry_release_tripping == [] and v.carry_holes_missed == []
        assert ("article-level carry probes (D-051 round 2): release trips 0/%d, holes missed 0/%d"
                % (len(rh.CARRY_RELEASE_PROBES), len(rh.CARRY_HOLE_PROBES))) in v.summary_lines()

    def test_a_carry_regression_in_either_direction_closes_the_gate(self, monkeypatch):
        bad_release = rh.CARRY_RELEASE_PROBES + (("x", _article_kw(body_html="<p>F3 Energy is clean.</p>")),)
        monkeypatch.setattr(rh, "CARRY_RELEASE_PROBES", bad_release)
        assert rh.evaluate().ship is False
        monkeypatch.setattr(rh, "CARRY_RELEASE_PROBES", rh.CARRY_RELEASE_PROBES[:-1])
        bad_hole = rh.CARRY_HOLE_PROBES + (("y", _article_kw(body_html="<p>A plain sentence.</p>")),)
        monkeypatch.setattr(rh, "CARRY_HOLE_PROBES", bad_hole)
        v = rh.evaluate()
        assert v.ship is False and v.carry_holes_missed == ["y"]

    def test_the_decay_rule_state_by_state(self):
        mood = frozenset({"MOOD"})
        c = pf.Rail2Carry().enter_field("body")
        c = c.after("F3 Mood is our evening can.", c.visible(),
                    pf.rail2_context_after("F3 Mood is our evening can.", frozenset()), True)
        assert c.visible() == mood                                   # the next sentence
        same = c.after("We love the flavor.", c.visible(), mood, False)
        assert same.visible() == mood                                # one brand-less, same block
        assert same.after("Pick one.", same.visible(), mood, False).visible() == frozenset()   # two: gone
        opened = c.after("L-theanine is an amino acid.", c.visible(), mood, True)
        assert opened.visible() == frozenset()                       # the intervening one opened a block
        chain = same.after("It keeps you calm.", same.visible(), mood, False)
        assert chain.visible() == mood and chain.age == 0            # a pronoun chain renews
        assert pf.RAIL2_CARRY_REACH == 2

    def test_the_title_anchor_covers_the_first_sentence_of_each_later_field(self):
        body = "<p>A plain opener. It is all-natural.</p>"
        assert pf.run_preflight(title="F3 Mood Tonight", summary="", body_html=body).passed
        assert "R2" in pf.run_preflight(title="F3 Mood Tonight", summary="",
                                         body_html="<p>It is all-natural.</p>").tripped_rail_ids
        # the summary's first sentence too (a listing shows it under the title)
        assert "R2" in pf.run_preflight(title="F3 Mood Tonight", summary="It is all-natural.",
                                         body_html="<p>x</p>").tripped_rail_ids

    def test_rail2_sentences_split_exactly_like_sentences(self):
        evasions, cleared = _load_preflight_corpora()
        texts = []
        for kw in [e[2] for e in evasions] + [c[1] for c in cleared]:
            k = {"title": kw.get("title", "Test Title"), "summary": kw.get("summary", ""),
                 "body_html": kw.get("body", "<p>Body copy.</p>")}
            texts += [t for _, t in pf.rail_fields(**k)]
        for _, kw in rh.CARRY_RELEASE_PROBES + rh.CARRY_HOLE_PROBES:
            texts += [t for _, t in pf.rail_fields(**kw)]
        texts += ["Dr.\nRuiz says hi.", "etc.\nNext. And more.", "a etc.\n" * 50, "", "\n\n", "One.\n\nTwo. Three.",
                  'He said "clean." Next one.', "Mg.\nx", "  spaced  .  out  "]
        for t in texts:
            assert [s for s, _ in pf.rail2_sentences(t)] == pf.sentences(t), t[:80]
        assert pf.rail2_sentences("A. B.\nC. D.") == [("A.", True), ("B.", False), ("C.", True), ("D.", False)]

    def test_the_article_walk_is_run_preflights_loop(self):
        """rail2_article_walk(first_per_field=True) must equal run_preflight's R2 trips
        on every article corpus -- the harness can never measure a different loop."""
        evasions, cleared = _load_preflight_corpora()
        articles = [kw for _, kw in rh.CARRY_RELEASE_PROBES + rh.CARRY_HOLE_PROBES]
        for kw in [e[2] for e in evasions] + [c[1] for c in cleared]:
            articles.append({"title": kw.get("title", "Test Title"), "summary": kw.get("summary", ""),
                             "body_html": kw.get("body", "<p>Body copy.</p>")})
        for s in TestCrossSentenceReference.MUST_TRIP + TestCrossSentenceReference.STILL_PASS:
            articles.append(_article_kw(body_html="<p>%s</p>" % s))
        for kw in articles:
            walk = rh.rail2_article_walk(pf.rail_fields(**kw), first_per_field=True)
            got = [(name, "%r near %s: %s" % (hit[0], hit[1], sent)) for name, sent, hit, _ in walk]
            want = [(t.field_name, t.excerpt) for t in _r2_sentence_trips(**kw)]
            assert [(n, pf._excerpt(e)) for n, e in got] == want, kw

    def test_the_differential_now_measures_the_carry(self):
        prose = [pf.html_to_text("<p>F3 Mood is our evening can.</p><p>It is all-natural.</p>"),
                 pf.html_to_text(rh.CARRY_RELEASE_PROBES[0][1]["body_html"])]
        sents = [s for p in prose for s in pf.sentences(p)]
        d = rh.differential(sents, articles=prose)
        assert d.articles == 2 and d.carry_only == ["It is all-natural."]
        assert "tripped only through the cross-sentence carry (2 article(s) walked in reading order): 1" \
            in d.summary_lines()
        plain = rh.differential(sents)           # sentence-by-sentence callers are unchanged
        assert plain.articles == 0 and plain.carry_only == [] and len(plain.summary_lines()) == 4

    def test_the_live_script_walks_each_article_with_the_carry(self, capsys):
        mod = TestScript()._load()
        pages = {
            "https://f3energy.com/blogs/news": (200, '<a href="/blogs/news/a-post">a</a>'),
            "https://f3energy.com/blogs/learn": (404, ""),
            "https://f3energy.com/blogs/news/a-post": (200, "<p>F3 Mood is our evening can.</p><p>It is all-natural.</p>"),
        }
        rc = mod.main(["--live"], fetch=lambda u: pages.get(u, (404, "")))
        out = capsys.readouterr().out
        assert rc == 0
        assert "tripped only through the cross-sentence carry (1 article(s) walked in reading order): 1" in out
        assert "trips only through the cross-sentence carry: It is all-natural." in out

    def test_d171_the_carry_walk_is_linear(self):
        def run(n, unit):
            body = "<p>" + unit * n + "</p>"
            return _best_of_3(lambda: pf.run_preflight(title="F3 Mood", summary="", body_html=body))
        for unit in ("F3 Mood is calm. It keeps you calm. It is great. ",   # a renewing pronoun chain
                     "A plain sentence. Another one. ",
                     "Keep it simple. It's worth noting this. They say so. "):
            base, dbl = run(300, unit), run(600, unit)
            assert dbl < base * 2.6 + 0.05, "%r superlinear: %.4fs -> %.4fs" % (unit, base, dbl)

    @pytest.mark.parametrize("text", ["x. " * 13000, "x.\n" * 13000, " " * 40000, "\n" * 40000,
                                      "Dr. " * 10000, "a etc.\n" * 5700, "e.g. " * 8000], ids=range(7))
    def test_d171_rail2_sentences_is_fast_at_40k(self, text):
        """D-171 found by this remediation: the pre-existing sentences() re-searched
        and re-joined the GROWING merged sentence on every abbreviation merge --
        9 s per call on "Dr. " * 10000, 53 s through run_preflight."""
        for fn in (pf.rail2_sentences, pf.sentences):
            dt = _best_of_3(lambda: fn(text))
            assert dt < 0.2, "%s %r...: %.3fs" % (fn.__name__, text[:10], dt)

    def test_d171_an_abbreviation_run_through_run_preflight(self):
        body = "<p>" + "Dr. " * 10000 + "</p>"
        assert _best_of_3(lambda: pf.run_preflight(title="t", summary="", body_html=body)) < 1.0

    @pytest.mark.parametrize("unit", ["Dr. ", "a etc.\n", "x. "])
    def test_d171_sentences_scales_linearly(self, unit):
        base, dbl = (_best_of_3(lambda: pf.sentences(unit * n)) for n in (5000, 10000))
        assert dbl < base * 2.6 + 0.02, "superlinear: %.4fs -> %.4fs" % (base, dbl)

    @staticmethod
    def _frozen_sentences(text):
        """The pre-round-2 sentences() verbatim (quadratic on abbreviation runs):
        the linear rewrite must return exactly this."""
        raw = [s.strip() for s in pf._SENT_SPLIT_RE.split(text or "") if s.strip()]
        out = []
        for part in raw:
            if out and pf._ABBREV_TAIL_RE.search(out[-1]):
                out[-1] = out[-1] + " " + part
            else:
                out.append(part)
        return out

    def test_the_linear_splitter_is_the_old_splitter(self):
        import random
        rng = random.Random(20260923)
        alpha = ["Dr.", "dr.", "etc.", "e.g.", "i.e.", "x.", "Hello", "world.", "\n", "  ", ".", "!", "?",
                 '"', "mg.", "U.S.", "no.", "a", "St.", "vs.", "Mr. ", "(", ")", " ", "Approx.", "oz."]
        for _ in range(4000):
            s = "".join(rng.choice(alpha) + rng.choice([" ", "", "\n", " \n"])
                        for _ in range(rng.randint(0, 25)))
            assert pf.sentences(s) == self._frozen_sentences(s), repr(s)
        evasions, cleared = _load_preflight_corpora()
        for kw in [e[2] for e in evasions] + [c[1] for c in cleared]:
            k = {"title": kw.get("title", "Test Title"), "summary": kw.get("summary", ""),
                 "body_html": kw.get("body", "<p>Body copy.</p>")}
            for _, t in pf.rail_fields(**k):
                assert pf.sentences(t) == self._frozen_sentences(t), t[:80]


class TestRound2PronounResolution:
    """F3-R1 (idioms, same-sentence resolution) + r143-claims-5 PARTIAL (the Mood guard
    on the ruled phrases): what counts as a back-reference, one occurrence at a time."""

    def _refs(self, clause, **kw):
        return pf._back_reference_tokens(pf._words(clause), **kw)

    NONREFERENTIAL = (
        "It's worth noting that natural caffeine is the same molecule",
        "it is worth knowing",
        "It's no secret that caffeine works",
        "It is important that you hydrate",
        "It is true that caffeine is caffeine",
        "It turns out caffeine is caffeine",
        "it seems that",
        "It depends on your goals",
        "It makes sense that tea is smoother",
        "It goes without saying",
        "They say green tea is smoother",
        "It’s worth noting that",                         # a curly apostrophe splits it + s
    )
    REFERENTIAL = (
        "It is all-natural",
        "It's worth a try",
        "It's worth trying",                              # worth + a non-information verb: the can is
        "It is natural to want more",                     # a clean word is never an extraposition adjective
        "It is easy to love",                             # tough-movement: the "it" IS the can
        "It is important to us",
        "It's time to go clean",                          # to-infinitive exhortations stay referential
        "It is important to go natural",
        "It helps to hydrate",
        "It makes sense to go clean",
        "It depends",
        "It helps to know it",
        "It's time-tested",
        "It helps you unwind",
        "It seems natural",
        "They are all-natural",
        "Keep it natural",
        "Keep it simple",                                 # object idioms stay referential (fail closed)
        "Take it easy",
        "That's it",
        "This is it",
        "We break it down",
        "We cleaned it up",
        "We clean it out",
        "We clean it with monk fruit",
        "We cleaned it",
        "We made it all-natural",
        "Fans call it the cleanest can",
        "Its formula is clean",
        "This is the clean way",
        "Both are clean",
    )

    @pytest.mark.parametrize("clause", NONREFERENTIAL)
    def test_expletive_and_idiomatic_pronouns_refer_to_nothing(self, clause):
        assert self._refs(clause) == [], clause

    @pytest.mark.parametrize("clause", REFERENTIAL)
    def test_their_referential_twins_still_refer_back(self, clause):
        assert self._refs(clause), clause

    def test_the_household_care_frame_needs_a_claim_free_sentence(self):
        """The clean VERB's own object ("clean it weekly") is dropped only when the
        sentence holds no other clean word: "We clean it weekly, all clean." is a
        claim about the referent (found by the round-2 self-differential)."""
        assert not any(pf._rail2_backref_flags("Rinse your shaker and clean it weekly."))
        assert not any(pf._rail2_backref_flags("Clean it after every session."))
        assert any(pf._rail2_backref_flags("We clean it weekly, all clean."))
        assert any(pf._rail2_backref_flags("Clean it weekly and keep it natural."))
        assert any(pf._rail2_backref_flags("We cleaned it up."))
        assert self._refs("clean it weekly") == ["it"]                    # a bare clause is never enough
        assert self._refs("clean it weekly", care_ok=True) == []
        assert "R2" in _pf("F3 Energy is our can. We clean it weekly, all clean.").tripped_rail_ids

    def test_exclusion_is_per_occurrence(self):
        assert self._refs("It's worth noting it is all-natural") == ["it"]
        assert self._refs("They say it is clean") == ["it"]

    def test_an_expletive_before_a_colon_is_void(self):
        """Whatever follows a colon is the content of the pronoun, so nothing is
        excluded in a clause that ends with one."""
        assert self._refs("It's worth noting", colon_after=True) == ["it's"]
        assert self._refs("They say", colon_after=True) == ["they"]
        assert pf._rail2_backref_flags("It's worth noting: clean fuel, all day.") == [True, False, False]
        assert pf._rail2_backref_flags("It's worth noting that clean fuel matters, and water too.") == [False, False]
        assert pf._rail2_backref_flags("Keep it simple: clean fuel, all day.") == [True, False, False]

    def test_an_energy_mood_clause_resolves_its_own_pronouns_unless_coordinated(self):
        assert self._refs("F3 Energy keeps its edge", coordinated_only=True) == []
        assert self._refs("F3 Pure and F3 Energy both carry it", coordinated_only=True) == []
        assert self._refs("F3 Energy and it both run", coordinated_only=True) == ["it"]
        assert self._refs("It and F3 Energy share", coordinated_only=True) == ["it"]

    RESOLVED = (   # a Pure-SUBJECT sentence's own possessive
        "F3 Pure uses organic cane sugar, monk fruit and stevia as its clean-sweetened base.",
        "F3 Pure keeps the stack, with monk fruit and stevia in its clean-sweetened base.",
        "In the cooler, F3 Pure is the pick, and its base is clean-sweetened.",
        "F3 Pure keeps its clean-sweetened base.",
    )
    UNRESOLVED = (
        "Unlike F3 Pure, its base is all-natural.",                  # Pure is not the subject
        "F3 Pure is clean-sweetened, and its sibling is too.",       # a relation in the clause
        "F3 Pure is clean-sweetened, and it is all-natural too.",
        "Its base, like F3 Pure, is clean.",                         # the possessive precedes the antecedent
        "F3 Pure is clean, and we made it that way.",                # not a possessive
        "F3 Pure is clean, and that goes for it.",
    )

    @pytest.mark.parametrize("sentence", RESOLVED)
    def test_a_pure_subject_sentence_resolves_its_own_possessive(self, sentence):
        assert not any(pf._rail2_backref_flags(sentence)), sentence
        for ctx in (frozenset({"ENERGY"}), frozenset({"MOOD"}), frozenset({"ENERGY", "MOOD"})):
            assert pf.rail2_attribution_hit(sentence, context_lines=ctx) is None, (sentence, ctx)

    @pytest.mark.parametrize("sentence", UNRESOLVED)
    def test_anything_else_still_refers_back(self, sentence):
        assert any(pf._rail2_backref_flags(sentence)), sentence

    def test_unresolved_clean_copy_still_trips_through_the_carry(self):
        for s in self.UNRESOLVED[:4]:
            assert pf.rail2_attribution_hit(s, context_lines=frozenset({"MOOD"})) is not None, s

    CLAIMS_5 = (
        "F3 Mood is our evening can. It, like F3 Energy, runs on a cleaner fuel source.",
        "F3 Mood is our evening can. It, like F3 Energy, runs on natural caffeine from green tea.",
        "F3 Mood is our evening can. It runs, like F3 Energy, on natural caffeine from green tea.",
        "F3 Mood is our evening can. F3 Energy runs on a cleaner fuel source, and so does it.",
        "F3 Mood is our evening can. F3 Energy runs on a cleaner fuel source, and it does too.",
        "F3 Mood is our evening can. F3 Energy and it both run on a cleaner fuel source.",
        "F3 Mood is our evening can. It and F3 Energy share natural caffeine from green tea.",
    )

    @pytest.mark.parametrize("text", CLAIMS_5)
    def test_a_pronoun_one_clause_away_cannot_launder_a_ruled_phrase_onto_mood(self, text):
        assert "R2" in _pf(text).tripped_rail_ids, text
        first, second = pf.sentences(text)
        assert pf.rail2_attribution_hit(second) is None                       # plain: cleared for Energy
        assert pf.rail2_attribution_hit(second, context_lines=frozenset({"MOOD"})) is not None
        assert text in rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"]

    def test_the_cross_field_version_trips(self):
        assert "R2" in pf.run_preflight(title="F3 Mood: Our Evening Can", summary="",
                                        body_html="<p>It, like F3 Energy, runs on a cleaner fuel source.</p>"
                                        ).tripped_rail_ids

    def test_scope_widening_rule(self):
        segs = pf._clause_split("F3 Energy carries natural caffeine from green tea, and its L-theanine keeps it smooth.")
        assert not pf._phrase_scope_widens(segs, [False, True])       # after the phrase, no relation
        segs = pf._clause_split("It runs, like F3 Energy, on natural caffeine from green tea.")
        assert pf._phrase_scope_widens(segs, [True, False, False])    # (ii) precedes the phrase
        segs = pf._clause_split("F3 Energy runs on a cleaner fuel source, and so does it.")
        assert pf._phrase_scope_widens(segs, [False, True])           # (iii) relates to it
        segs = pf._clause_split("It runs on a cleaner fuel source.")
        assert pf._phrase_scope_widens(segs, [True])                  # (i) holds it
        assert not pf._phrase_scope_widens(pf._clause_split("It is great."), [True])   # no phrase at all

    def test_the_round_one_still_pass_rows_still_pass(self):
        for text in TestCrossSentenceReference.STILL_PASS:
            assert _pf(text).passed, text

    @pytest.mark.parametrize("text", ["it's worth noting " * 2300, "keep it simple " * 2600,
                                      "they say " * 4400, "it " * 13000, "that's it " * 4000,
                                      "clean it " * 4400, "It, " * 10000 + "cleaner fuel source"],
                             ids=range(7))
    def test_d171_the_pronoun_scan_is_fast_at_40k(self, text):
        dt = _best_of_3(lambda: pf._rail2_backref_flags(text))
        assert dt < 0.5, "%r...: %.3fs" % (text[:12], dt)
        dt = _best_of_3(lambda: pf.rail2_attribution_hit("F3 Energy " + text + " clean.",
                                                         context_lines=frozenset({"MOOD"})))
        assert dt < 1.0, "%r...: %.3fs" % (text[:12], dt)

    @pytest.mark.parametrize("unit", ["it's worth noting ", "F3 Pure keeps its base, ", "It, like it, "])
    def test_d171_the_pronoun_scan_scales_linearly(self, unit):
        def run(n):
            sent = unit * n + "cleaner fuel source."
            return _best_of_3(lambda: pf.rail2_attribution_hit(sent, context_lines=frozenset({"MOOD"})))
        base, dbl = run(1500), run(3000)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)


class TestRound2PureAttachmentStandsAlone:
    """r143-claims-1 PARTIAL: the relation veto is a closed blacklist, so comparison
    wording outside it transferred a Pure-attached clean word ("..., and F3 Energy
    is no different."). An Energy/Mood clause -- or a clause pointing back at one
    -- beside the clean clause must now STAND ALONE (its own subject + predicate,
    a contrast adverbial, or a reader conditional). r143-claims-3 PARTIAL: a
    LEADING bare brand folds forward ("F3 Energy or F3 Pure is clean.")."""

    CLAIMS_1 = (
        "F3 Pure is clean-sweetened, and F3 Energy is no different.",
        "F3 Pure is clean-sweetened, and F3 Mood is no exception.",
        "F3 Pure is all-natural, and that goes for F3 Energy.",
        "F3 Pure is all-natural, and that is true of F3 Energy.",
        "In line with F3 Energy, F3 Pure is clean.",
        "Following F3 Energy's lead, F3 Pure is all-natural.",
        "F3 Pure is clean; ditto F3 Energy.",
        "F3 Pure is clean, and F3 Energy is no less so.",
        "F3 Pure is clean, just as F3 Energy is bold.",
        "Besides F3 Energy, F3 Pure is clean.",
        "F3 Pure is clean, and F3 Energy carries it forward.",
        "F3 Pure is clean-sweetened, and F3 Energy has it.",
        "F3 Pure is clean-sweetened, and F3 Energy is one.",
    )
    CLAIMS_3 = (
        "F3 Energy or F3 Pure is clean.",
        "F3 Energy or F3 Pure is the clean pick.",
        "F3 Mood or F3 Pure is the natural pick.",
        "F3 Energy or F3 Pure is a clean way to start the day.",
        "F3 Energy, F3 Pure are clean.",
        "F3 Energy, F3 Mood, F3 Pure are all clean.",
        "F3 Energy vs F3 Pure is the clean matchup.",
        "F3 ENERGY OR F3 PURE IS CLEAN",
        "F3 Energy Or F3 Pure Is The Clean Pick",
    )
    #: benign Energy/Mood clauses beside a Pure clean clause that must keep clearing
    #: (beyond the pinned TestPositivePureAttachment.STILL_PASS rows)
    STILL_PASS = (
        "F3 Pure is clean-sweetened, and F3 Energy is great.",
        "F3 Pure is clean-sweetened, and F3 Energy carries the stack that athletes want.",
        "F3 Pure is clean-sweetened, and F3 Energy has one goal.",
        "F3 Pure is clean-sweetened, and F3 Energy is different.",
        "F3 Pure is clean-sweetened, but F3 Energy is not.",
        "F3 Pure is clean-sweetened, and F3 Energy carries that stack.",
        "F3 Energy hits hard, while F3 Pure is the clean-sweetened pick.",
        "F3 Pure is clean-sweetened; F3 Mood keeps you calm.",
        "F3 Mood is calm. F3 Pure is clean-sweetened, and we love it.",
        "F3 Mood is calm. F3 Pure is clean-sweetened, and its flavor is bright.",
        "F3 Mood is calm. F3 Pure is clean-sweetened, and it tastes great.",   # pinned (round 1)
        "F3 Energy is the stack. F3 Pure is clean-sweetened, unlike it.",      # pinned (round 1)
    )
    #: accepted fail-closed consequences (each legacy-trips, or carries the round-1
    #: back-reference): a comparison preposition, an object pronoun in the
    #: Energy/Mood clause, a back-referring clause with its own subject
    ACCEPTED_TRIPS = (
        "Compared with F3 Energy, F3 Pure is clean.",
        "F3 Pure is clean-sweetened; F3 Mood keeps it calm.",
        "F3 Mood is calm. F3 Pure is clean-sweetened, and we made it that way.",
    )

    @pytest.mark.parametrize("sentence", CLAIMS_1 + CLAIMS_3)
    def test_a_transfer_the_relation_list_missed_trips(self, sentence):
        assert pf.rail2_attribution_hit(sentence) is not None, sentence
        assert "R2" in _pf(sentence).tripped_rail_ids, sentence
        assert pf.rail2_legacy_hit(sentence) is not None

    def test_every_shape_is_in_the_gate(self):
        probes = rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"]
        for s in self.CLAIMS_1 + self.CLAIMS_3:
            assert s in probes, s

    @pytest.mark.parametrize("sentence", STILL_PASS + TestPositivePureAttachment.STILL_PASS)
    def test_a_self_contained_clause_beside_pure_still_clears(self, sentence):
        r = _pf(sentence)
        assert r.passed, r.render()

    @pytest.mark.parametrize("sentence", ACCEPTED_TRIPS)
    def test_accepted_fail_closed_consequences_trip(self, sentence):
        assert "R2" in _pf(sentence).tripped_rail_ids, sentence

    def test_the_carried_back_reference_shape_is_a_gated_hole(self):
        label = "a back-referring clause beside Pure must stand alone (r143-claims-1)"
        assert label in [lbl for lbl, _ in rh.CARRY_HOLE_PROBES]
        assert "R2" in _pf("F3 Mood is calm. F3 Pure is clean, and that goes for it.").tripped_rail_ids

    @pytest.mark.parametrize("field", ["title", "summary", "alt", "jsonld"])
    def test_every_shipped_field_is_covered(self, field):
        s = "F3 Energy Or F3 Pure Is The Clean Pick"
        kw = {"title": "Post", "summary": "", "body_html": "<p>x</p>"}
        if field == "title":
            kw["title"] = s
        elif field == "summary":
            kw["summary"] = s
        elif field == "alt":
            kw["body_html"] = '<p><img src="a.png" alt="%s"></p>' % s
        else:
            kw["body_html"] = '<script type="application/ld+json">{"description":"%s"}</script><p>x</p>' % s
        assert "R2" in pf.run_preflight(**kw).tripped_rail_ids

    def test_the_stand_alone_detector(self):
        yes = [(" and F3 Energy carries the stack.", False), ("F3 Energy is the full stack", False),
               ("Unlike F3 Energy", False), ("If you like F3 Energy", False), (" F3 Mood keeps you calm.", False),
               (" and it tastes great.", True), (" and its flavor is bright.", True), (" and we love it.", True),
               (" unlike it.", True), (" F3 Energy has one goal.", False)]
        no = [(" and F3 Energy is no different.", False), (" and that goes for F3 Energy.", False),
              ("In line with F3 Energy", False), (" ditto F3 Energy.", False), (" and F3 Energy is no less so.", False),
              (" just as F3 Energy is bold.", False), (" and F3 Energy carries it forward.", False),
              (" and F3 Energy is one.", False), (" and F3 Energy has that.", False),
              (" and that goes for it.", True), (" and we made it that way.", True),
              (" and we love it because it is great.", True), ("Following F3 Energy's lead", False)]
        for s, br in yes:
            assert pf._clause_stands_alone(pf._words(s), back_referring=br), s
        for s, br in no:
            assert not pf._clause_stands_alone(pf._words(s), back_referring=br), s

    def test_a_leading_bare_brand_folds_forward(self):
        c = pf._rail2_clauses("F3 Energy or F3 Pure is clean.")
        assert len(c) == 1 and c[0].host == " F3 Pure is clean." and c[0].bare == ["F3 Energy "]
        c = pf._rail2_clauses("F3 Energy, F3 Mood, F3 Pure are all clean.")
        assert len(c) == 1 and pf.brand_lines_in(" ".join([c[0].host] + c[0].bare)) == {"ENERGY", "MOOD", "PURE"}
        c = pf._rail2_clauses("F3 Energy or F3 Pure.")               # nothing but bare brands
        assert len(c) == 1 and c[0].host == "F3 Energy " and c[0].bare == [" F3 Pure."]
        # a trailing fold is unchanged (round 1)
        c = pf._rail2_clauses("Clean energy from F3 Pure, or F3 Energy.")
        assert len(c) == 1 and c[0].bare == [" F3 Energy."]

    DEGENERATE = (
        "F3 Energy, " * 3700,
        "F3 Energy or " * 3100,
        "F3 Pure is clean, and F3 Energy is no " * 1000,
        "and we love it " * 2600,
        "F3 Pure is clean, and it is " * 1500,
        "Unlike F3 Energy, " * 2300,
    )

    @pytest.mark.parametrize("text", DEGENERATE, ids=range(len(DEGENERATE)))
    def test_d171_the_stand_alone_check_is_fast_at_40k(self, text):
        for s in (text + "F3 Pure is clean.", "F3 Pure is clean, " + text):
            dt = _best_of_3(lambda: pf.rail2_attribution_hit(s))
            assert dt < 1.0, "%r...: %.3fs" % (s[:20], dt)
            dt = _best_of_3(lambda: pf.rail2_attribution_hit(s, context_lines=frozenset({"MOOD"})))
            assert dt < 1.0, "%r...: %.3fs" % (s[:20], dt)

    @pytest.mark.parametrize("unit", ["F3 Energy, ", ", and F3 Energy carries the stack", ", and it tastes great",
                                      "F3 Energy or "])
    def test_d171_the_stand_alone_check_scales_linearly(self, unit):
        def run(n):
            sent = "F3 Pure is clean" + unit * n + "."
            lead = unit * n + "F3 Pure is clean."
            return _best_of_3(lambda: (pf.rail2_attribution_hit(sent, context_lines=frozenset({"MOOD"})),
                                       pf.rail2_attribution_hit(lead)))
        base, dbl = run(1200), run(2400)
        assert dbl < base * 2.6 + 0.05, "superlinear: %.4fs -> %.4fs" % (base, dbl)
