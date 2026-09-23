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
  * RELEASE_PROBES     -- D-051 round 2: copy the round-1 remediation re-tripped
                          (F3-R2 quantities / focus particles, F3-R3 CSR tails); gated
                          must-PASS, kept apart from the measured FP set;
  * CARRY_RELEASE_PROBES / CARRY_HOLE_PROBES -- D-051 round 2 (F3-R1): ARTICLE-level
                          must-PASS / must-TRIP probes for the cross-sentence carry
                          (title / summary / body fields, paragraphs), gated;
  * UNDECIDED          -- the 9/1 Pure-attached tail (trips under fail-closed union
                          inheritance, D-051 EF-7): reported, never gated;
  * legacy_preflight() -- the FROZEN pre-R14-3 composition: run_preflight's non-R2
                          trips + rail2_legacy_hit over the same fields;
  * new_preflight()    -- run_preflight itself: the gate measures the SHIPPING
                          function, never a composition of it;
  * evaluate() / gate() -- the ship decision, and WHY;
  * differential()     -- legacy vs shipping rail 2 over any sentence corpus (the
                          live News/Learn read the differential script performs);
                          with `articles=` it also walks each article WITH the carry
                          (rail2_article_walk, run_preflight's own loop).
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
        # D-051 r143-claims-1: a clause that merely NAMES Pure (the standard of a
        # comparison, a possessor, an appositive, a relative clause) is not Pure
        # attachment -- the clean word is said of Energy/Mood
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
        # ...the same class the other way round: a later Energy/Mood clause that
        # takes the clean predicate by ellipsis or comparison
        "F3 Pure is clean-sweetened, and F3 Energy is too.",
        "F3 Pure is clean, like F3 Energy.",
        "F3 Pure is all-natural, and so is F3 Energy.",
        "F3 Pure is clean-sweetened, as is F3 Energy.",
        "Like F3 Energy, F3 Pure is clean-sweetened.",
        "F3 Energy, or the clean version of F3 Pure, hits hard.",
        "Clean energy from F3 Pure, or F3 Energy.",
        # D-051 round 2 (r143-claims-1 PARTIAL): comparison / transfer wording OUTSIDE
        # the closed relation list -- an Energy/Mood clause beside a Pure-attached
        # clean word must now stand alone (legacy TRIP / round-1 PASS)
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
        # D-051 round 2 (r143-claims-3 PARTIAL): a LEADING bare brand never folded,
        # so the Pure clause after it cleared -- the EF-7 disjunction, mirrored
        "F3 Energy or F3 Pure is clean.",
        "F3 Energy or F3 Pure is the clean pick.",
        "F3 Mood or F3 Pure is the natural pick.",
        "F3 Energy or F3 Pure is a clean way to start the day.",
        "F3 Energy, F3 Pure are clean.",
        "F3 Energy, F3 Mood, F3 Pure are all clean.",
        "F3 Energy vs F3 Pure is the clean matchup.",
        "F3 ENERGY OR F3 PURE IS CLEAN",
        "F3 Energy Or F3 Pure Is The Clean Pick",
        # ...and the holes adversarial probing found in the remediation's OWN first
        # cut: a predicate / verb-phrase disjunct (P2), a comma + "and" metaphor
        # tail, and a coordinate adjective before a ruled phrase
        "F3 Mood is calm or the natural pick of F3 Pure.",
        "F3 Energy is the stack or the clean version in F3 Pure.",
        "F3 Mood keeps you calm or delivers the natural calm of F3 Pure.",
        "F3 Mood supports a clean environment, and your mind.",
        "F3 Mood supports a clean environment, and for your mind.",
        "F3 Energy has real, natural caffeine from green tea.",
        # D-051 r143-claims-5: a pronoun across a sentence boundary laundered a clean
        # word (or a ruled phrase) onto Mood/Energy. These are TWO-sentence probes;
        # new_preflight splits them, so the gate measures the cross-sentence carry
        "F3 Mood is our evening can. Like F3 Energy, it runs on a cleaner fuel source.",
        "F3 Mood is our evening can. Like F3 Energy, it runs on natural caffeine from green tea.",
        "F3 Mood is our evening can. It is all-natural.",
        "F3 Mood is our evening can. It runs on a cleaner fuel source.",
        "F3 Energy is our training can. We love it because it is all-natural.",
        "F3 Energy is great. It is F3 Pure's clean sibling.",
        "F3 Mood is calm. F3 Pure is clean-sweetened, and it is too.",
        # D-051 round 2 (r143-claims-5 PARTIAL): the Mood guard on the ruled phrases
        # widened only when the phrase sat in the PRONOUN'S OWN clause, and a
        # pronoun coordinated with a brand ("F3 Energy and it") was never a
        # back-reference at all (legacy TRIP / round-1 PASS)
        "F3 Mood is our evening can. It, like F3 Energy, runs on a cleaner fuel source.",
        "F3 Mood is our evening can. It, like F3 Energy, runs on natural caffeine from green tea.",
        "F3 Mood is our evening can. It runs, like F3 Energy, on natural caffeine from green tea.",
        "F3 Mood is our evening can. F3 Energy runs on a cleaner fuel source, and so does it.",
        "F3 Mood is our evening can. F3 Energy runs on a cleaner fuel source, and it does too.",
        "F3 Mood is our evening can. F3 Energy and it both run on a cleaner fuel source.",
        "F3 Mood is our evening can. It and F3 Energy share natural caffeine from green tea.",
        # D-051 r143-claims-3: the bare-brand fold REPLACED the host clause's
        # inheritance instead of joining it
        "F3 Energy: all-natural, F3 Pure too.",
        "F3 Energy: clean-sweetened, F3 Pure.",
        "F3 Mood: clean, and F3 Pure too.",
        "F3 Energy: zero sugar, 200mg caffeine, all-natural flavors, and F3 Pure too.",
        "F3 Mood: caffeine-free, clean, and F3 Pure too.",
        "F3 Energy is great, clean, and F3 Pure too.",
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
        # D-051 round-2 (r143-claims-2 PARTIAL): colloquial intensifiers the degree set
        # did not list (legacy TRIP / round-1 PASS); an unlisted "-ly" intensifier
        # stays fail-closed through the structural rule
        "F3 Energy runs on way cleaner fuel.",
        "F3 Energy runs on a way cleaner fuel source.",
        "F3 Energy runs on miles cleaner fuel.",
        "F3 Energy has next-level natural caffeine from green tea.",
        "F3 Energy has straight-up natural caffeine from green tea.",
        "F3 Energy has refreshingly natural caffeine from green tea.",
        "F3 Energy has 2x cleaner fuel.",
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
        # D-051 r143-claims-4: the metaphor guard was a blacklist (of/in/inside/
        # within); every other continuation cleared a product metaphor
        "F3 Energy builds a cleaner world for your taste buds.",
        "F3 Energy restores clean earth to your routine.",
        "F3 Energy builds a cleaner world at every workout.",
        "F3 Energy protects clean air for your lungs.",
        "F3 Mood restores a clean environment for your mind.",
        "F3 Mood supports a clean environment for your mind.",
        "F3 Mood supports a clean environment, for your mind.",
        "F3 Energy builds a cleaner world with every sip.",
        # D-051 round 2 (r143-claims-4 PARTIAL): the comma branch stayed a refuse-list
        # and ";" / ":" were never checked, so a participle / "so" / adjective tail
        # still cleared a product metaphor (legacy TRIP / round-1 PASS)
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
        # D-051 r143-claims-6: the noun form after a copula, and a compound whose
        # first half is the clean-up object (these passed BOTH rails)
        "F3 Energy is the community clean-up crew for your afternoon slump.",
        "F3 Energy is a beach clean-up in a can.",
        "F3 Energy is a community clean-up for your gut.",
        "F3 Energy cleans up trash talk in the gym.",
        # R14-3 holes that passed BOTH rails (verb form + hyphenated compounds)
        "F3 Energy cleans up your afternoon.",
        "F3 Energy is the clean-up crew for your afternoon slump.",
        "F3 Energy is clean-energy in a can.",
        "F3 Mood is a clean-energy calm.",
        "F3 Energy is naturally-caffeinated.",
        "F3 Energy is the natural-energy pick.",
        # D-051 r143-claims-8: tokenizer gaps that passed BOTH rails
        "F3 Energy burns cleanly.",
        "F3 Energy delivers energy cleanly.",
        "F3 Energy is cleansed of junk.",
        "F3 Energy's cleanliness sets it apart.",
        "F3 Energy's cleanness sets it apart.",
        "F3 Energy's naturalness sets it apart.",
        "F3 Mood is one of the naturals.",
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

# ── D-051 round 2: copy the round-1 remediation RE-TRIPPED (must PASS) ─────────
#: Plain factual or CSR sentences that the pre-remediation rail cleared, that the
#: round-1 remediation re-tripped, and that are no claim: gated like
#: FALSE_POSITIVE_SET but kept apart from it, because that set is the MEASURED
#: live FP rate (its "5/6" pin) and these are review probes. Legacy trips most of
#: them; the ruling (D-329) is what releases them.
RELEASE_PROBES: tuple[str, ...] = (
    # F3-R2: a unit-fused quantity and a focus particle are not degree modifiers of
    # the ruled phrase (the house spelling is "200mg natural caffeine")
    "F3 Energy delivers 120mg natural caffeine from green tea.",
    "Each can of F3 Energy packs 120mg natural caffeine from green tea.",
    "F3 Energy has a 120-mg natural caffeine from green tea.",
    "F3 Energy uses only natural caffeine from green tea.",
    "F3 Energy uses exclusively natural caffeine from green tea.",
    "F3 Energy: your daily natural caffeine from green tea.",
    # ...and a guard on round 2's OWN new intensifier "way": after a definite
    # determiner it is a noun (round 1 passed this row; it must stay passing)
    "F3 Energy changes the way natural caffeine from green tea hits.",
    # F3-R3: the comma branch refused its own allowlisted CSR continuation
    "F3 Energy funds a cleaner planet, one case at a time.",
    "F3 Energy helps build a cleaner planet, one can at a time.",
)


def _article(body: str, title: str = "Post", summary: str = "") -> dict[str, str]:
    return {"title": title, "summary": summary, "body_html": body}


#: D-051 round 2 (F3-R1): ARTICLE-level copy the round-1 cross-sentence carry
#: re-tripped -- invisible to every single-sentence set above, which is why the
#: "FP 0/6" gate never saw it. Each passed the pre-remediation rail, and each but
#: the row-8 draft (it carries the ruled green-tea phrase) passed legacy too.
#: (label, run_preflight fields). Gated must-PASS.
CARRY_RELEASE_PROBES: tuple[tuple[str, dict[str, str]], ...] = (
    ("an ingredient paragraph after an Energy paragraph",
     _article("<p>F3 Energy pairs caffeine with L-theanine.</p><p>L-theanine is an amino acid found in "
              "tea leaves. It is a natural component of green tea.</p>", "What Is L-Theanine?")),
    ("a sweetener paragraph after a Mood paragraph",
     _article("<p>F3 Mood is our caffeine-free evening can.</p><p>Monk fruit is a small melon grown in "
              "southern China. Its sweetness is entirely natural.</p>", "Monk Fruit, Explained")),
    ("green tea's caffeine, a paragraph on",
     _article("<p>F3 Energy gets its caffeine from green tea.</p><p>Tea has been brewed for thousands of "
              "years. Its caffeine is natural and arrives with L-theanine.</p>", "Why Green Tea Caffeine")),
    ("coffee beans: they",
     _article("<p>We built F3 Energy for training days.</p><p>Coffee beans are roasted seeds. They are a "
              "natural source of caffeine.</p>", "Coffee vs Energy Drinks")),
    ("an Energy title, then a sleep paragraph's second sentence",
     _article("<p>Sleep matters more than any can. It is the most natural recovery tool you have.</p>",
              "How F3 Energy Fits Your Morning")),
    ("a Mood title, then a stretching paragraph's second sentence",
     _article("<p>Start with ten minutes of stretching. It is a natural way to wind down.</p>",
              "An Evening Routine With F3 Mood")),
    ("five brand-less sentences after the only Energy mention",
     _article("<p>F3 Energy is our training can.</p><p>Find a gym near you. Pick one with good lighting. "
              "Check the locker rooms. Ask about classes. Make sure it is clean.</p>", "Training Tips")),
    ("the object of a clean verb (gear care)",
     _article("<p>Pack a can of F3 Energy in your gym bag.</p><p>Rinse your shaker bottle after every "
              "session and clean it weekly.</p>", "Gear Care")),
    ("a pronoun two sentences into the next paragraph (the reviewer's idiom row)",
     _article("<p>F3 Energy is built for training days.</p><p>Recovery is simple. Keep it simple: water, "
              "sleep, and natural food.</p>")),
    ("expletive it: it's worth noting that",
     _article("<p>F3 Energy is our training can. It's worth noting that natural caffeine and synthetic "
              "caffeine are the same molecule.</p>")),
    ("generic they: they say",
     _article("<p>F3 Energy is our training can. They say natural caffeine hits smoother.</p>")),
    ("Harrison's ruled two-sentence remedy for the 9/1 UNDECIDED shape",
     _article("<p>If caffeine plus L-theanine is what you are after, F3 Pure and F3 Energy are both built "
              "around that pairing. F3 Pure uses organic cane sugar, monk fruit and stevia as its "
              "clean-sweetened base.</p>")),
    ("the prompt's good example, then Pure's own possessive after the sweetener list",
     _article("<p>F3 Energy carries the full stack. F3 Pure uses organic cane sugar, monk fruit and stevia "
              "as its clean-sweetened base.</p>")),
    ("Pure's own possessive under an Energy title",
     _article("<p>F3 Pure uses organic cane sugar, monk fruit and stevia as its clean-sweetened base.</p>",
              "F3 Pure vs F3 Energy: What Changed")),
    ("Pure's own possessive after a Mood sentence",
     _article("<p>F3 Mood is caffeine-free. F3 Pure keeps the stack, with monk fruit and stevia in its "
              "clean-sweetened base.</p>")),
    # the refuter's realistic drafts of the NEXT queued Learn rows (8, 13, 12): 2, 3
    # and 3 sentences tripped in one body under round 1 -- more than the one bounded
    # revision can fix, so the week's post was lost
    ("queued row 8: green tea caffeine vs synthetic caffeine",
     _article("<p>F3 Energy and F3 Pure each carry 120 mg of natural caffeine from green tea. F3 Mood is "
              "caffeine-free.</p><h2>Green tea caffeine vs synthetic caffeine</h2><p>Most energy drinks use "
              "synthetic caffeine, which is made in a lab. Natural caffeine comes from plants such as tea "
              "leaves, coffee beans and guarana. Chemically, they are the same molecule, so natural caffeine "
              "is not stronger than synthetic caffeine.</p><p>If you are comparing labels, look for the "
              "caffeine source. Some brands list it as natural caffeine, others simply as caffeine.</p>"
              "<h2>What about F3 Mood?</h2><p>F3 Mood has 0 mg of caffeine. It is built for calm and focus "
              "in the evening.</p>",
              "How Much Caffeine Is in F3? (And How That Compares)",
              "F3 Energy and F3 Pure each carry 120 mg of caffeine from green tea, and F3 Mood has none.")),
    ("queued row 13: reading labels, Pure-led",
     _article("<p>F3 Pure is our clean-sweetened can. F3 Energy is the zero-sugar training can, and F3 Mood "
              "is caffeine-free.</p><h2>What makes an energy drink healthier?</h2><p>Start with the label. "
              "If it is short and you recognize every ingredient, that is a good sign. Clean-label drinks "
              "skip artificial colors and dyes, and they list the caffeine amount per can.</p><p>Next, check "
              "the sugar. Their sweetness often comes from high-fructose corn syrup rather than natural "
              "sources.</p><p>F3 Pure uses organic cane sugar, monk fruit and stevia as its clean-sweetened "
              "base.</p>",
              "What Are the Healthiest Energy Drinks?",
              "A plain-English guide to reading energy drink labels, and where F3 Pure fits.")),
    ("queued row 12: an ingredients glossary under an Energy summary",
     _article("<p>F3 Energy carries a nootropic-leaning stack. Here is what each term on a label means.</p>"
              "<h2>L-theanine</h2><p>An amino acid found in tea leaves. It is a natural partner to caffeine."
              "</p><h2>Stevia</h2><p>A sweetener made from the leaves of a South American plant. Its "
              "sweetness is natural and calorie-free.</p>",
              "Energy Drink Ingredients Glossary: 12 Terms, Plain English",
              "Twelve ingredients you will see on energy drink labels, including the ones in F3 Energy.")),
)

#: ...and the ARTICLE-level laundering shapes the carry must still catch after it
#: decays. All but the claims-5 cross-field row pass legacy (the clean sentence
#: names no line); the last two passed round 1 as well. Gated must-TRIP.
CARRY_HOLE_PROBES: tuple[tuple[str, dict[str, str]], ...] = (
    ("a pronoun opening the next paragraph", _article("<p>F3 Mood is our evening can.</p><p>It is all-natural.</p>")),
    ("a pronoun opening the body under the title", _article("<p>It is all-natural.</p>", "F3 Mood Tonight")),
    ("the title stays in view past a brand-less summary",
     _article("<p>It is all-natural.</p>", "F3 Mood Tonight", "A calm routine for evenings.")),
    ("one brand-less sentence between, same paragraph",
     _article("<p>F3 Mood is the evening can. Our team loves the flavor. It is all-natural.</p>")),
    ("one brand-less sentence closing the paragraph, the pronoun opening the next",
     _article("<p>F3 Mood is our evening can. We love the flavor.</p><p>It is all-natural.</p>")),
    ("a pronoun chain renews the referent",
     _article("<p>F3 Mood is our evening can. It keeps you calm. It tastes like citrus. It is all-natural.</p>")),
    ("a pronoun chain across a paragraph",
     _article("<p>F3 Mood is our evening can.</p><p>It keeps you calm. It is all-natural.</p>")),
    ("an object pronoun", _article("<p>F3 Mood is our evening can. We made it all-natural.</p>")),
    ("an object pronoun opening the body under the title",
     _article("<p>We made it all-natural.</p>", "F3 Mood Tonight")),
    ("an idiom that opens an elaboration (colon)",
     _article("<p>F3 Energy is our training can. Keep it simple: clean fuel, all day.</p>")),
    ("an object idiom is referential", _article("<p>F3 Mood is our evening can. Keep it simple and natural.</p>")),
    ("that's it, then a colon", _article("<p>F3 Mood is our evening can. That's it: all-natural.</p>")),
    ("tough-movement it is the can", _article("<p>F3 Mood is our evening can. It is easy to love: all-natural.</p>")),
    ("worth + a non-information verb", _article("<p>F3 Mood is our evening can. It's worth trying, all-natural.</p>")),
    ("important TO us is referential",
     _article("<p>F3 Mood is our evening can. It is important to us and all-natural.</p>")),
    ("a to-infinitive exhortation after a product sentence",
     _article("<p>F3 Mood is our evening can. It's time to go natural.</p>")),
    ("an expletive before a colon is void",
     _article("<p>F3 Mood is our evening can. It's worth noting: all-natural and calm.</p>")),
    ("expletive exclusion is per occurrence",
     _article("<p>F3 Mood is our evening can. It's worth noting it is all-natural.</p>")),
    ("generic they, then a referential it", _article("<p>F3 Mood is our evening can. They say it is all-natural.</p>")),
    ("a clean verb with up / out still refers back", _article("<p>F3 Mood is our evening can. We cleaned it up.</p>")),
    ("a clean verb outside the household-care frame",
     _article("<p>F3 Mood is our evening can. We clean it with monk fruit.</p>")),
    ("the household-care frame beside another clean word",
     _article("<p>F3 Energy is our can. We clean it weekly, all clean.</p>")),
    ("Pure possessive resolution needs Pure as the subject",
     _article("<p>F3 Mood is our evening can. Unlike F3 Pure, its base is all-natural.</p>")),
    ("the Mood guard across a field (r143-claims-5)",
     _article("<p>It, like F3 Energy, runs on a cleaner fuel source.</p>", "F3 Mood: Our Evening Can")),
    ("a back-referring clause beside Pure must stand alone (r143-claims-1)",
     _article("<p>F3 Mood is calm. F3 Pure is clean, and that goes for it.</p>")),
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
    release_tripping: list[str] = field(default_factory=list)               # round-2 must-PASS rows
    carry_release_tripping: list[str] = field(default_factory=list)         # article-level must-PASS
    carry_holes_missed: list[str] = field(default_factory=list)             # article-level must-TRIP

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
        out.append(f"round-2 release probes (D-051): attribution trips "
                   f"{len(self.release_tripping)}/{len(RELEASE_PROBES)}")
        for s in self.release_tripping:
            out.append("  release probe still tripping: " + s)
        out.append(f"article-level carry probes (D-051 round 2): release trips "
                   f"{len(self.carry_release_tripping)}/{len(CARRY_RELEASE_PROBES)}, holes missed "
                   f"{len(self.carry_holes_missed)}/{len(CARRY_HOLE_PROBES)}")
        for s in self.carry_release_tripping:
            out.append("  carry release probe still tripping: " + s)
        for s in self.carry_holes_missed:
            out.append("  carry hole probe PASSES the shipping preflight: " + s)
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
    release = [s for s in RELEASE_PROBES if not new_preflight(s).passed]
    carry_release = [label for label, kw in CARRY_RELEASE_PROBES if not pf.run_preflight(**kw).passed]
    carry_missed = [label for label, kw in CARRY_HOLE_PROBES if pf.run_preflight(**kw).passed]
    gated_uncaught = [s for cls, missed in uncaught.items() if cls not in RULED_OUT_CLASSES for s in missed]
    ship = (not pinned_missed and not gated_uncaught and not fp_new and not release
            and not carry_release and not carry_missed)
    return Verdict(ship=ship, uncaught_by_class=uncaught, uncaught_legacy_by_class=uncaught_legacy,
                   fp_still_tripping=fp_new, fp_legacy_tripping=fp_legacy,
                   pinned_missed=pinned_missed, undecided=undecided, release_tripping=release,
                   carry_release_tripping=carry_release, carry_holes_missed=carry_missed)


@dataclass
class Differential:
    sentences: int
    legacy_trips: list[str]
    attribution_trips: list[str]
    only_legacy: list[str]        # the false-positive candidates the attribution scope releases
    only_attribution: list[str]   # new catches: the R14-3 hole closures (verb forms, hyphen compounds)
    articles: int = 0             # articles walked WITH the cross-sentence carry (D-051 round 2)
    carry_only: list[str] = field(default_factory=list)   # sentences that trip ONLY through the carry

    def summary_lines(self) -> list[str]:
        out = [
            f"sentences scanned: {self.sentences}",
            f"rail-2 trips: legacy {len(self.legacy_trips)} | attribution {len(self.attribution_trips)}",
            f"released by attribution scope (legacy-only trips = the measured FP candidates): {len(self.only_legacy)}",
            f"caught only by the shipping rail (R14-3 hole closures: verb forms, hyphen compounds): "
            f"{len(self.only_attribution)}",
        ]
        if self.articles:
            out.append(f"tripped only through the cross-sentence carry ({self.articles} article(s) walked "
                       f"in reading order): {len(self.carry_only)}")
        return out


def rail2_article_walk(fields, *, first_per_field: bool = False) -> list[tuple[str, str, tuple[str, str], bool]]:
    """run_preflight's rail-2 loop over (field_name, text) views, reporting EVERY
    tripping sentence as (field, sentence, hit, via_carry): via_carry is True when
    the plain single-sentence check passed and only the carried context tripped it.
    With first_per_field=True it stops each field at its first trip exactly as
    run_preflight does (a test pins the two loops equal on the article corpora)."""
    out: list[tuple[str, str, tuple[str, str], bool]] = []
    carry = pf.Rail2Carry()
    for name, text in fields:
        carry = carry.enter_field(name)
        for sent, opens_block in pf.rail2_sentences(text):
            carried = carry.visible()
            plain = pf.rail2_attribution_hit(sent)
            hit = plain or (pf.rail2_attribution_hit(sent, context_lines=carried) if carried else None)
            if hit:
                out.append((name, sent, hit, plain is None))
                if first_per_field:
                    break
            carry = carry.after(sent, carried, pf.rail2_context_after(sent, carried), opens_block)
        carry = carry.leave_field(name, text)
    return out


def differential(sentences_iter, *, articles=None) -> Differential:
    """Frozen legacy vs SHIPPING rail-2 over any sentence corpus (pure; no other rail).

    The sentence counts are sentence by sentence. D-051 round 2 (F3-R1): they
    cannot see the shipping rail's cross-sentence carry, so a carry false positive
    was invisible to the live read. Pass `articles` (each one article's prose, as
    html_to_text returns it) and every article is also walked in reading order with
    the carry; `carry_only` lists the sentences that trip only through it."""
    sents = [s for s in sentences_iter if s and s.strip()]
    legacy = [s for s in sents if pf.rail2_legacy_hit(s)]
    attrib = [s for s in sents if pf.rail2_attribution_hit(s)]
    la = set(attrib)
    ll = set(legacy)
    carry_only: list[str] = []
    n_articles = 0
    for prose in articles or ():
        n_articles += 1
        carry_only += [s for _, s, _, via in rail2_article_walk((("body", prose or ""),)) if via]
    return Differential(sentences=len(sents), legacy_trips=legacy, attribution_trips=attrib,
                        only_legacy=[s for s in legacy if s not in la],
                        only_attribution=[s for s in attrib if s not in ll],
                        articles=n_articles, carry_only=carry_only)
