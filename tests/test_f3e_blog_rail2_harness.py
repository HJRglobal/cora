"""Rail-2 attribution sibling + differential harness (Code #13 slice 6, cq-85b35413b020).

Contract under test:
  * run_preflight still ships the LEGACY same-sentence rail 2 (source pin) and keeps
    its four parameters (the fail-closed signature pin stands);
  * the attribution sibling catches every clean/natural-on-Energy/Mood probe incl.
    the attributed quote, ALL-CAPS, the Title-Case evasion and the ambiguous
    Pure+Energy clause, and PASSES every measured false-positive shape (Pure-
    attached clause, environmental object, chemistry);
  * the five pinned D-051 holes trip under BOTH preflights;
  * THE FINDING: the sugar-free-on-Pure and comparative-category classes pass BOTH
    preflights (no rail exists), so evaluate().ship is False and names exactly
    those two classes -- the escalation, pinned so a future rail-set change flips
    it deliberately;
  * differential() releases exactly the Pure-attached / environmental FPs and
    catches nothing legacy misses;
  * every new regex has a growth-shape test on the input that reaches it (D-239);
  * the script's offline mode prints the verdict and exits 3 (gate closed); the
    live mode parses index pages and never touches the network in tests.
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


class TestShippingRailUnchanged:
    def test_run_preflight_uses_the_legacy_hit_and_not_the_attribution_sibling(self):
        # comment lines stripped first: a source pin must never match its own comment
        src = "\n".join(ln for ln in inspect.getsource(pf.run_preflight).splitlines()
                        if not ln.strip().startswith("#"))
        assert "rail2_legacy_hit(sent)" in src
        assert "rail2_attribution_hit" not in src

    def test_signature_pin_stands(self):
        assert set(inspect.signature(pf.run_preflight).parameters) == {"title", "summary", "body_html", "lane"}

    @pytest.mark.parametrize("sentence", [
        "F3 Energy is clean and simple.",                      # the pipeline DIRTY_DRAFT
        "Explore the full stack in F3 Energy or the clean-sweetened version in F3 Pure.",
    ])
    def test_legacy_behaviour_is_byte_identical(self, sentence):
        r = pf.run_preflight(title="t", summary="", body_html="<p>%s</p>" % sentence)
        assert "R2" in r.tripped_rail_ids
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

    def test_the_finding_two_classes_have_no_rail_so_the_gate_is_closed(self):
        v = rh.evaluate()
        assert v.ship is False
        assert v.pinned_missed == []
        assert v.fp_still_tripping == []                 # the attribution rail releases every measured FP
        missed = {cls for cls, s in v.uncaught_by_class.items() if s}
        assert missed == {"sugar_free_on_pure", "comparative_category"}, missed
        # ...and those two ALSO pass the LEGACY preflight: the hole pre-exists rail 2
        for cls in missed:
            assert set(v.uncaught_legacy_by_class[cls]) == set(v.uncaught_by_class[cls])
        assert set(rh.CLAIMS_HOLE_PROBES) - missed == {"clean_natural_on_energy_mood", "nsf_on_pure_mood", "sleep_on_mood"}
        lines = "\n".join(v.summary_lines())
        assert lines.startswith("SHIP: NO") and "sugar_free_on_pure" in lines and "comparative_category" in lines
        # PIN CHANGED with D-051 EF-7: the 9/1 Pure-attached tail left the FP set
        # for UNDECIDED (it trips again under fail-closed union inheritance)
        assert "false positives: legacy trips 3/4, attribution trips 0/4" in lines

    def test_undecided_sentences_are_reported_not_gated(self):
        v = rh.evaluate()
        assert len(rh.UNDECIDED) == 2
        for s in rh.UNDECIDED:
            assert v.undecided[s] == {"legacy": True, "attribution": True}, s
        assert sum("UNDECIDED" in ln for ln in v.summary_lines()) == 2
        # the 9/1 shape is the one that moved (see TestFailClosedInheritance)
        assert any("organic cane sugar, monk fruit and stevia" in s for s in rh.UNDECIDED)
        assert not any("organic cane sugar" in s for s in rh.FALSE_POSITIVE_SET)

    def test_new_preflight_keeps_every_other_rail(self):
        r = rh.new_preflight("F3 Pure is NSF Certified for Sport and costs $39.99.")
        assert {"R4", "R5"} <= set(r.tripped_rail_ids)
        assert "R2" not in r.tripped_rail_ids

    def test_differential_releases_only_the_fp_shapes(self):
        corpus = list(rh.FALSE_POSITIVE_SET) + list(rh.CLAIMS_HOLE_PROBES["clean_natural_on_energy_mood"]) + [
            "A sentence about nothing in particular.", "F3 Pure is the clean-sweetened version.",
        ]
        d = rh.differential(corpus)
        assert d.sentences == len(corpus)
        assert set(d.only_legacy) == {
            "Explore the full stack in F3 Energy or the clean-sweetened version in F3 Pure.",
            "F3 Energy partners with CleanHub to fund a cleaner planet with every case sold.",
            "Every F3 Energy purchase supports a cleaner future for the oceans.",
        }
        assert d.only_attribution == []
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

    def test_run_preflight_is_untouched_by_the_rail_change(self):
        """Keep run_preflight byte-identical: still the legacy hit, so a live
        draft's verdict cannot move with this harness-only change."""
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

    def test_offline_mode_prints_the_closed_gate_and_exits_3(self, capsys):
        mod = self._load()
        rc = mod.main([])
        out = capsys.readouterr().out
        assert rc == 3 and "SHIP: NO" in out and "sugar_free_on_pure" in out and "== probe table" in out

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
        assert rc == 3
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
