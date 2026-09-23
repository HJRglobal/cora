"""Rail-2 differential harness (Code #13 slice 6; the ship gate for R14-3, cq-85b35413b020).

THE RULINGS. 2026-09-01 (C6): the blog-preflight false-positive fix is (b) an
ATTRIBUTION-scoped rail 2 + drafting.py in the same commit, CONDITIONED on a
purpose-built differential suite -- the #11 S7 attempt loosened the rail three ways
and the D-051 differential (main=TRIP -> branch=PASS on five sentences) reverted it.
So the harness, not anecdote, decides. 2026-09-19 (ESC-1 (A), ESC 3(i)/(ii), D-329):
the sugar-free-on-Pure and comparative-category classes -- which pass BOTH rails
because no mechanical rail exists for either -- are RULED OUT of rail 2's ship
condition (still probed, still reported, seeded as rails of their own), and two
exact phrases are cleared. Under those rulings the gate passes and R14-3 wired the
attribution rail into run_preflight.

WHAT THIS MODULE HOLDS (data + pure functions; no network, no LLM):
  * PINNED_D051        -- the five S7 hole sentences, verbatim from
                          tests/test_d051_remediation.py (a rail must replay its
                          own incident; edit that module and this one together);
  * CLAIMS_HOLE_PROBES -- the ruled classes: clean/natural on Energy/Mood (incl. the
                          R14-3 no-widening probes for the two exemptions and the
                          environmental-predicate / hyphen-compound holes), NSF on
                          Pure/Mood, sleep on Mood, sugar-free on Pure, comparative
                          category claims;
  * RULED_OUT_CLASSES  -- the two classes with no rail, excluded from the ship
                          condition by ruling and reported as such on every run;
  * FALSE_POSITIVE_SET -- the measured FP shapes (the 8/26 live rejection, the
                          CleanHub environmental sentences, the chemistry sentence,
                          the 9/14 green-tea jam and the Pure-launch "cleaner fuel
                          source" line) that the shipping rail must PASS;
  * UNDECIDED          -- the 9/1 Pure-attached tail (trips under fail-closed union
                          inheritance, D-051 EF-7): reported, never gated;
  * legacy_preflight() -- the FROZEN pre-R14-3 composition: run_preflight's non-R2
                          trips + rail2_legacy_hit over the same fields;
  * new_preflight()    -- run_preflight itself: the gate measures the SHIPPING
                          function, never a composition of it;
  * evaluate() / gate() -- the ship decision, and WHY;
  * differential()     -- legacy vs shipping rail 2 over any sentence corpus (the
                          live News/Learn read the differential script performs).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import preflight as pf

# ── the five pinned S7 holes (verbatim; tests/test_d051_remediation.py) ─────────
PINNED_D051: tuple[str, ...] = (
    "F3 Energy is Clean Energy.",
    "F3 Energy delivers Clean Energy All Day.",
    "No other drink prevents fatigue like F3 Energy.",
    "There is no better way to manage stress than F3 Energy.",
    "No one denies that F3 Energy cures fatigue.",
)

# ── the ruled claims-hole classes (every class outside RULED_OUT_CLASSES must be
#    fully caught for the rail to ship; the ruled-out two are probed + reported) ──
CLAIMS_HOLE_PROBES: dict[str, tuple[str, ...]] = {
    "clean_natural_on_energy_mood": (
        "F3 Energy is Clean Energy.",
        "F3 Energy delivers Clean Energy All Day.",
        "ENERGY IS OUR CLEANEST, MOST NATURAL LINE",
        "F3 Energy was born out of a need for cleaner, smarter energy.",
        # an ATTRIBUTED third-party quote is NOT an exemption (the Earthbar line)
        "F3 Energy delivers exactly what our customers are seeking, clean energy with clean label ingredients.",
        "F3 Mood is the clean way to settle in for the evening.",
        "Energy drinks from F3 are all-natural and clean.",
        # ambiguous attachment: Pure AND Energy in the same clause -> fail closed
        "F3 Pure and F3 Energy are both clean-sweetened for the same crowd.",
        # D-051 EF-7: a Pure parenthetical between an Energy/Mood SUBJECT and its
        # clean predicate must not clear the predicate (nearest-clause inheritance
        # did; union inheritance does not)
        "F3 Energy, like F3 Pure, is clean.",
        "F3 Energy, similar to F3 Pure, is all-natural.",
        "F3 Mood, our companion to F3 Pure, is the clean way to wind down.",
        "F3 Energy: think F3 Pure, then clean caffeine on top.",
        # D-051 EF-7: a disjunction of brands attaches the clean word to BOTH
        "Clean energy from F3 Pure or F3 Energy.",
        "F3 Energy or F3 Pure: clean, natural energy.",
        # R14-3 NO-WIDENING: the two ruled phrases are EXACT. Every variant, every
        # bare use, and either phrase in a sentence that names Mood still trips.
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
        # D-051 r143-claims-2: EXACT means exact at both edges -- a modifier or a
        # fused prefix/suffix on either ruled phrase is not the ruled phrase
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
        # R14-3 NEW HOLES the Code #13 environmental redaction opened (it cleared a
        # PREDICATE of the brand, not only an environmental object): legacy TRIP,
        # pre-R14-3 attribution PASS
        "F3 Energy is clean air in a can.",
        "F3 Energy is a cleaner future.",
        "F3 Energy is a clean world of flavor.",
        "F3 Energy delivers clean water-like hydration.",
        # ...and the metaphorical environmental object (the R14-3 position check's
        # own first-cut gap: an action verb + environmental noun used as a figure)
        "F3 Energy builds a cleaner world of flavor.",
        "F3 Energy protects clean water in every can.",
        # R14-3 holes that passed BOTH rails (verb form + hyphenated compounds)
        "F3 Energy cleans up your afternoon.",
        "F3 Energy is the clean-up crew for your afternoon slump.",
        "F3 Energy is clean-energy in a can.",
        "F3 Mood is a clean-energy calm.",
        "F3 Energy is naturally-caffeinated.",
        "F3 Energy is the natural-energy pick.",
    ),
    "nsf_on_pure_mood": (
        "F3 Pure is NSF Certified for Sport.",
        "NSF Certified for Sport runs across F3 Energy, F3 Pure and F3 Mood.",
        "F3 Mood carries the NSF mark on every can.",
    ),
    "sleep_on_mood": (
        "F3 Mood is the sleep aid you have been waiting for.",
        "F3 Mood helps you fall asleep faster after a long day.",
        "A can of F3 Mood will make you drowsy by ten.",
    ),
    "sugar_free_on_pure": (
        "F3 Pure is sugar-free.",
        "Zero sugar in every can of F3 Pure.",
        "F3 Pure has no sugar at all, just the fruit.",
    ),
    "comparative_category": (
        "F3 Energy is the best energy drink on the market.",
        "No other energy drink comes close to F3 Energy.",
        "F3 Energy beats every other functional beverage on the shelf.",
    ),
}

#: Classes with NO mechanical rail, ruled OUT of rail 2's ship condition by
#: Harrison 2026-09-19 (ESC-1 (A), D-329). Still probed and reported on every run
#: ("no rail exists -- ruled out ... seeded") so the gap is never silent. Adding a
#: class here is a RULING, never a fix: tests pin this set exactly.
RULED_OUT_CLASSES: frozenset[str] = frozenset({"sugar_free_on_pure", "comparative_category"})
RULED_OUT_NOTE = "no rail exists -- ruled out of rail-2's condition 2026-09-19 (ESC-1 (A), D-329); seeded"

# ── the measured false-positive shapes (must PASS the shipping rail) ────────────
FALSE_POSITIVE_SET: tuple[str, ...] = (
    # 8/26: the first live rejection (drafting.py quoted it as REJECTED until R14-3)
    "Explore the full stack in F3 Energy or the clean-sweetened version in F3 Pure.",
    # (the 9/1 attempt-1 shape moved to UNDECIDED -- D-051 EF-7, see below)
    # CleanHub: the object is the environment, not the product
    "F3 Energy partners with CleanHub to fund a cleaner planet with every case sold.",
    "Every F3 Energy purchase supports a cleaner future for the oceans.",
    # chemistry, already cleared by the natural-occurrence redaction
    "L-theanine occurs naturally in green tea, which is where F3 Energy gets its caffeine.",
    # 2026-09-14 attempt 1 (the lane jammed): 'natural caffeine from green tea' as an
    # ingredient descriptor on Energy -- RULED CLEARED 2026-09-19 (ESC 3(i), D-329)
    "F3 Energy carries 120 mg of natural caffeine from green tea plus a nootropic-leaning stack "
    "for training and competition.",
    # the Pure launch copy (9/19 --live read): 'cleaner fuel source' -- RULED CLEARED
    # 2026-09-19 for Pure AND Energy (ESC 3(ii), D-329); names only Energy
    "Every can carries the same formula philosophy: the functional stack you know from F3 Energy, "
    "with a cleaner fuel source.",
)

# ── reported, never gated: needs a Harrison ruling ──────────────────────────────
UNDECIDED: tuple[str, ...] = (
    # (the 9/14 green-tea sentence moved to FALSE_POSITIVE_SET: ruled 2026-09-19)
    # 9/1 attempt 1: the Pure-attached tail after a clause naming Pure AND Energy.
    # Was in FALSE_POSITIVE_SET under nearest-clause inheritance; D-051 EF-7 made
    # inheritance the UNION of every brand named earlier (fail-closed), so it trips
    # again. Harrison's ruling: write it as two sentences rather than keep an
    # attachment heuristic that cleared "F3 Energy, like F3 Pure, is clean."
    "If caffeine plus L-theanine is what you are after, F3 Pure and F3 Energy are both built "
    "around that pairing, with F3 Pure using organic cane sugar, monk fruit and stevia as its "
    "clean-sweetened base.",
)


def _body(sentence: str) -> str:
    return "<p>%s</p>" % (sentence or "")


def legacy_run(*, title: str = "Post", summary: str = "", body_html: str = "") -> pf.PreflightResult:
    """The FROZEN pre-R14-3 preflight: every non-R2 trip of the shipping
    run_preflight, plus rail 2 as the frozen rail2_legacy_hit over the same fields
    (pf.rail_fields -- the one definition run_preflight reads). Composed here, never
    a run_preflight kwarg, so the fail-closed signature pin stands and no caller can
    turn a rail off."""
    base = pf.run_preflight(title=title, summary=summary, body_html=body_html)
    trips = [t for t in base.trips if t.rail_id != "R2"]
    for name, text in pf.rail_fields(title=title, summary=summary, body_html=body_html):
        for sent in pf.sentences(text):
            hit = pf.rail2_legacy_hit(sent)
            if hit:
                trips.append(pf._trip("R2", name, "%r near %s: %s" % (hit[0], hit[1], sent)))
                break
    return pf.PreflightResult(passed=not trips, trips=trips, rails_checked=base.rails_checked)


def legacy_preflight(sentence: str) -> pf.PreflightResult:
    """The frozen pre-R14-3 preflight over one sentence (see legacy_run)."""
    return legacy_run(title="Post", summary="", body_html=_body(sentence))


def new_preflight(sentence: str) -> pf.PreflightResult:
    """The SHIPPING preflight over one sentence: run_preflight itself."""
    return pf.run_preflight(title="Post", summary="", body_html=_body(sentence))


@dataclass
class Verdict:
    ship: bool
    uncaught_by_class: dict[str, list[str]] = field(default_factory=dict)   # new rail misses
    uncaught_legacy_by_class: dict[str, list[str]] = field(default_factory=dict)
    fp_still_tripping: list[str] = field(default_factory=list)              # new rail FPs
    fp_legacy_tripping: list[str] = field(default_factory=list)             # the measured FP rate
    pinned_missed: list[str] = field(default_factory=list)
    undecided: dict[str, dict[str, bool]] = field(default_factory=dict)

    def summary_lines(self) -> list[str]:
        out = [f"SHIP: {'YES' if self.ship else 'NO'}"]
        if self.pinned_missed:
            out.append("pinned D-051 holes MISSED by the new preflight: " + " | ".join(self.pinned_missed))
        for cls, missed in sorted(self.uncaught_by_class.items()):
            if not missed:
                continue
            if cls in RULED_OUT_CLASSES:
                out.append(f"class {cls}: {len(missed)} probe(s) pass the shipping preflight -- "
                           f"{RULED_OUT_NOTE} -- " + " | ".join(missed))
            else:
                out.append(f"class {cls}: {len(missed)} probe(s) pass the SHIPPING preflight (uncaught) -- "
                           + " | ".join(missed))
        for cls, missed in sorted(self.uncaught_legacy_by_class.items()):
            if not missed:
                continue
            if cls in RULED_OUT_CLASSES:
                out.append(f"class {cls}: {len(missed)} probe(s) ALSO pass the LEGACY preflight (no rail exists)")
            else:
                closed = [s for s in missed if s not in self.uncaught_by_class.get(cls, [])]
                out.append(f"class {cls}: {len(missed)} probe(s) pass the LEGACY preflight; "
                           f"{len(closed)} of them closed by the shipping rail (R14-3)")
        out.append(f"false positives: legacy trips {len(self.fp_legacy_tripping)}/{len(FALSE_POSITIVE_SET)}, "
                   f"attribution trips {len(self.fp_still_tripping)}/{len(FALSE_POSITIVE_SET)}")
        for s in self.fp_still_tripping:
            out.append("  FP still tripping under attribution: " + s)
        for s, res in self.undecided.items():
            out.append(f"UNDECIDED (Harrison ruling): legacy={'TRIP' if res['legacy'] else 'pass'} "
                       f"attribution={'TRIP' if res['attribution'] else 'pass'} -- {s}")
        return out


def evaluate() -> Verdict:
    """Run every probe through BOTH preflights and decide the gate: ship iff every
    pinned hole and every claims-hole probe OUTSIDE RULED_OUT_CLASSES is caught by
    the SHIPPING preflight AND every measured false-positive sentence PASSES it. The
    ruled-out classes are still run and reported (uncaught_by_class), never hidden."""
    pinned_missed = [s for s in PINNED_D051 if new_preflight(s).passed]
    uncaught: dict[str, list[str]] = {}
    uncaught_legacy: dict[str, list[str]] = {}
    for cls, probes in CLAIMS_HOLE_PROBES.items():
        uncaught[cls] = [s for s in probes if new_preflight(s).passed]
        uncaught_legacy[cls] = [s for s in probes if legacy_preflight(s).passed]
    fp_new = [s for s in FALSE_POSITIVE_SET if not new_preflight(s).passed]
    fp_legacy = [s for s in FALSE_POSITIVE_SET if not legacy_preflight(s).passed]
    undecided = {s: {"legacy": not legacy_preflight(s).passed, "attribution": not new_preflight(s).passed}
                 for s in UNDECIDED}
    gated_uncaught = [s for cls, missed in uncaught.items() if cls not in RULED_OUT_CLASSES for s in missed]
    ship = not pinned_missed and not gated_uncaught and not fp_new
    return Verdict(ship=ship, uncaught_by_class=uncaught, uncaught_legacy_by_class=uncaught_legacy,
                   fp_still_tripping=fp_new, fp_legacy_tripping=fp_legacy,
                   pinned_missed=pinned_missed, undecided=undecided)


@dataclass
class Differential:
    sentences: int
    legacy_trips: list[str]
    attribution_trips: list[str]
    only_legacy: list[str]        # the false-positive candidates the attribution scope releases
    only_attribution: list[str]   # new catches: the R14-3 hole closures (verb forms, hyphen compounds)

    def summary_lines(self) -> list[str]:
        return [
            f"sentences scanned: {self.sentences}",
            f"rail-2 trips: legacy {len(self.legacy_trips)} | attribution {len(self.attribution_trips)}",
            f"released by attribution scope (legacy-only trips = the measured FP candidates): {len(self.only_legacy)}",
            f"caught only by the shipping rail (R14-3 hole closures: verb forms, hyphen compounds): "
            f"{len(self.only_attribution)}",
        ]


def differential(sentences_iter) -> Differential:
    """Frozen legacy vs SHIPPING rail-2 over any sentence corpus (pure; no other rail)."""
    sents = [s for s in sentences_iter if s and s.strip()]
    legacy = [s for s in sents if pf.rail2_legacy_hit(s)]
    attrib = [s for s in sents if pf.rail2_attribution_hit(s)]
    la = set(attrib)
    ll = set(legacy)
    return Differential(sentences=len(sents), legacy_trips=legacy, attribution_trips=attrib,
                        only_legacy=[s for s in legacy if s not in la],
                        only_attribution=[s for s in attrib if s not in ll])
