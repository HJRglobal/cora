"""Deterministic claims preflight for F3E blog/news copy.

This module is the CODE MIRROR of the human-readable checklist at
``02-F3-Energy/projects/build-f3e-news-and-blog-strategy/
2026-08-26_f3e_content-claims-preflight-checklist.md``. The file is the source of
truth for humans; this module is what actually blocks a staging run.

FAIL-CLOSED: `run_preflight` returning a result with ``passed is False`` means the
caller must NOT stage. There is no override parameter, no "warn only" mode, and no
severity ladder -- a tripped rail is a stop.

WHAT THIS DOES *NOT* COVER -- read this before treating a green preflight as
clearance. Four of the fourteen checklist rails are human judgment and are
deliberately NOT code-enforced, because a regex cannot decide them:

  * rail 7  -- verified quotes only (is this quote real, and cleared?)
  * rail 9  -- product facts only from cleared sources (is this mg number right?)
  * rail 12 -- counterparty-safe news (is this relationship signed and still true?)
  * rail 14 -- escalation (is this borderline?)

A green preflight therefore means "no MECHANICALLY detectable violation", never
"cleared". The human tap is what clears it, which is exactly why the tap exists.
`UNENFORCED_RAILS` is surfaced in the run report so the gap is stated, not implied.

ReDoS discipline. Every proximity rail here is implemented with TOKEN INDEX
ARITHMETIC rather than regex proximity, and the remaining regexes are literal
alternations with bounded classes.

That was also claimed by the first cut of this docstring, and it was FALSE: the
script/style stripper was a lazy `.{0,200000}?` with a backreference and it was
the seventh catastrophic-backtracking bug found in this codebase, measured at
~4x per doubling (6.4 seconds on 128 KB). It survived a shape test because the
test's fixture contained no script tag, so the one quadratic regex in the file
was the one input the ReDoS test could not reach. It is now a linear
`str.find` scan (`_split_script_blocks`).

The lesson encoded in the tests: assert the shape on the input that reaches the
suspect construct, not on a generic body -- and never trust this paragraph
without a measurement beside it.
"""

from __future__ import annotations

import hashlib
import html as _html
import re
from dataclasses import dataclass, field
from typing import Iterable

# Bumped when the rail SET or a rail's SCOPE changes (not on wording tweaks).
# Recorded in the pipeline log so a past run's report can be read against the
# rails it ran. 1.1 (R14-3, 2026-09-19 ruling ESC-1 (A) / D-329): rail 2 ships
# attribution-scoped with the two exact-phrase exemptions.
CHECKLIST_MIRROR_VERSION = "1.1"

UNENFORCED_RAILS: tuple[str, ...] = (
    "rail 7 (verified quotes)",
    "rail 9 (product facts from cleared sources)",
    "rail 12 (counterparty-safe news)",
    "rail 14 (escalation on anything borderline)",
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trip:
    """One tripped rail. `excerpt` is what a human needs to fix it."""

    rail_id: str
    rail_name: str
    field_name: str
    excerpt: str

    def render(self) -> str:
        return "%s (%s) in %s: %s" % (
            self.rail_id, self.rail_name, self.field_name, self.excerpt,
        )


@dataclass
class PreflightResult:
    passed: bool
    trips: list[Trip] = field(default_factory=list)
    rails_checked: tuple[str, ...] = ()

    @property
    def tripped_rail_ids(self) -> list[str]:
        # Stable, de-duplicated, in rail order.
        seen: list[str] = []
        for t in self.trips:
            if t.rail_id not in seen:
                seen.append(t.rail_id)
        return seen

    def render(self) -> str:
        """Reader-facing report. Never empty, on either outcome."""
        if self.passed:
            return (
                "Claims preflight PASSED: %d mechanical rails clear. Not code-checked "
                "(human judgment): %s." % (len(self.rails_checked),
                                           ", ".join(UNENFORCED_RAILS))
            )
        lines = [
            "Claims preflight BLOCKED this draft. Nothing was staged. "
            "%d rail(s) tripped:" % len(self.tripped_rail_ids)
        ]
        for t in self.trips[:12]:
            lines.append("  - " + t.render())
        if len(self.trips) > 12:
            lines.append("  - ...and %d more" % (len(self.trips) - 12))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------

# Block-level closers become newlines so two paragraphs never merge into one
# "sentence" and defeat every same-sentence proximity rail below.
# <br> is deliberately NOT here. It is a SOFT line break -- authors use it to wrap
# a line far more often than to end a paragraph -- and treating it as a sentence
# boundary split real violations in half: "F3 Energy is<br>clean, natural, honest."
# and "F3 Energy is NSF Certified for Sport.<br>So is Pure." both passed. Merging
# two sentences is the safe direction (it only widens the window and adds trips);
# splitting one is the dangerous direction, so <br> collapses to a space.
_BLOCK_BREAK_RE = re.compile(
    r"</(?:p|div|li|ul|ol|h[1-6]|blockquote|tr|td|th|section|article)>",
    re.IGNORECASE,
)
_SCRIPT_TAGS = ("script", "style")


def _split_script_blocks(html_body: str) -> tuple[str, list[str]]:
    """(prose_with_blocks_removed, [raw_block_contents]).

    A hand-written linear scan, NOT a regex. The regex this replaces --
    `<(script|style)\\b[^>]{0,400}>.{0,200000}?</\\1>` with DOTALL -- was the
    SEVENTH catastrophic-backtracking bug found in this codebase, and it sat in
    the file whose own docstring claimed ReDoS discipline. Repeated UNCLOSED
    openings ("<script>" * n) made each one start a fresh lazy expansion hunting
    a close tag that never comes, then advance one character and rescan:

        16 KB   102 ms
        32 KB   397 ms   (3.9x)
        64 KB  1665 ms   (4.2x)
       128 KB  6430 ms   (3.9x)

    The shape test missed it because its fixture contains no script tag at all,
    which is the lesson: a ReDoS test proves nothing about a regex its input
    never reaches. This scan is O(n) -- each character is visited once by
    `str.find`, which cannot backtrack -- and it also retires the two silent
    bound overruns the old pattern had (`[^>]{0,400}` was already exceeded by
    one block on the live FAQ page, and a script body over 200 KB was not
    stripped at all).

    An UNCLOSED opening drops the remainder from prose but still returns it as a
    block, so the claims rails scan it rather than losing sight of it.
    """
    src = html_body or ""
    low = src.lower()
    prose: list[str] = []
    blocks: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        nxt = -1
        tag = ""
        for cand in _SCRIPT_TAGS:
            at = low.find("<" + cand, i)
            if at != -1 and (nxt == -1 or at < nxt):
                nxt, tag = at, cand
        if nxt == -1:
            prose.append(src[i:])
            break
        prose.append(src[i:nxt])
        close = low.find("</" + tag, nxt)
        if close == -1:
            blocks.append(src[nxt:])  # unclosed: still scanned, just not prose
            break
        blocks.append(src[nxt:close])
        gt = src.find(">", close)
        i = (gt + 1) if gt != -1 else n
        prose.append(_BLOCK_MARK)
    return "".join(prose), blocks


# Bounded deliberately: `<[^>]*>` is quadratic on a run of bare "<" characters
# (each start offset rescans to end of input). 8000 covers the largest tag
# measured on the live FAQ page (4478); a tag beyond it is left in the prose,
# which only ADDS text a rail might trip on -- never hides a violation.
_TAG_RE = re.compile(r"<[^>]{0,8000}>")
_WS_RE = re.compile(r"[ \t]{2,}")


#: Placeholder for a real block boundary, held while soft newlines are collapsed.
_BLOCK_MARK = "\x00"

# Attribute values that ship as content a machine or a screen reader reads.
_ATTR_RE = re.compile(
    r"\b(?:alt|title|aria-label|content|data-[a-z-]{1,30})\s{0,3}=\s{0,3}"
    r"(?:\"([^\"]{0,2000})\"|'([^']{0,2000})')",
    re.IGNORECASE,
)


def html_to_text(html_body: str) -> str:
    """Reader-visible prose, with SOFT line wraps collapsed.

    The soft-wrap collapse is a correctness fix, not tidiness. Sentence-scoped
    rails (2, 4, 10) split on newlines so that two paragraphs never merge into
    one "sentence" -- but a model writing multi-line HTML puts newlines INSIDE
    sentences constantly, and that split every same-sentence rail wide open:

        <p>F3 Energy is
        clean and all-natural.</p>

    ...became two "sentences", neither of which had both halves of the
    violation, so rail 2 passed it. That is the normal shape of a real draft, not
    a contrived input. So only BLOCK boundaries become sentence breaks; every
    other newline, and every <br>, becomes a space.
    """
    if not html_body:
        return ""
    txt, _blocks = _split_script_blocks(html_body)
    txt = _BLOCK_BREAK_RE.sub(_BLOCK_MARK, txt)
    txt = _TAG_RE.sub(" ", txt)
    txt = _html.unescape(txt)
    # Every remaining whitespace run, newlines included, becomes ONE space; only
    # the block marks survive as line breaks.
    txt = re.sub(r"\s+", " ", txt)
    return txt.replace(_BLOCK_MARK, "\n")


def hidden_text(html_body: str) -> str:
    """Content that SHIPS but is not reader-visible prose: JSON-LD / script
    bodies and attribute values such as alt text.

    Needed because the drafting prompt explicitly asks the model for a JSON-LD
    BlogPosting block, and `html_to_text` deletes script bodies and discards
    attributes -- so a claim placed in structured data or in an image's alt text
    was invisible to every semantic rail. The author of the first cut had already
    seen this and patched exactly one rail (a JSON-LD "price" key on rail 5),
    which is how a one-rail fix reveals an all-rail hole.
    """
    if not html_body:
        return ""
    _prose, blocks = _split_script_blocks(html_body)
    parts: list[str] = list(blocks)
    for m in _ATTR_RE.finditer(html_body):
        parts.append(m.group(1) or m.group(2) or "")
    if not parts:
        return ""
    txt = _TAG_RE.sub(" ", "\n".join(parts))
    txt = _html.unescape(txt)
    return re.sub(r"[ \t]+", " ", txt)


def unescaped(text: str) -> str:
    """HTML entities resolved. What the reader or a parser actually receives.

    Rails that scan raw shipped bytes (em-dash, price, placeholder) MUST use this
    rather than the raw string: `&mdash;` and `&#8212;` render as an em-dash and
    `&#36;39.99` renders as a price, and scanning the pre-unescape form made both
    rails completely blind to the entity spelling.
    """
    return _html.unescape(text or "")


# Splits on terminal punctuation followed by whitespace, or on a block boundary.
# NOT on a bare newline: see html_to_text.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])[\"')\]]{0,3}\s+|\n+")

# Abbreviations that end in a period and must not end a sentence. Without this,
# "F3 Energy, per Dr. Ruiz, is clean-label" split before the clean token and
# rail 2 passed a real violation.
_ABBREV = (
    "dr", "mr", "mrs", "ms", "prof", "st", "vs", "etc", "e.g", "i.e", "approx",
    "inc", "ltd", "co", "no", "fig", "al", "jr", "sr", "u.s", "mg", "oz",
)
_ABBREV_TAIL_RE = re.compile(
    r"(?:^|\s)(?:%s)\.$" % "|".join(re.escape(a) for a in _ABBREV), re.IGNORECASE)


def sentences(text: str) -> list[str]:
    """Sentence-ish units, rejoining splits that landed after an abbreviation.

    Merging two real sentences is the SAFE direction here (it only pulls more
    tokens into one window, adding trips); splitting one real sentence is the
    dangerous direction, because it can put the two halves of a violation into
    different windows. So this errs toward merging.

    D-171 (found in D-051 round 2): the first cut re-ran _ABBREV_TAIL_RE over the
    whole GROWING merged sentence and re-concatenated it on every merge, so a run
    of abbreviations was quadratic -- "Dr. " * 10000 (40 KB) took 9 s per call and
    53 s through run_preflight. rail2_sentences now does the merge in one linear
    pass, testing only the last PART (equivalent: parts are whitespace-stripped, so
    the join space before a part is the regex's "^"), and this is its projection.
    """
    return [s for s, _ in rail2_sentences(text)]


def rail2_sentences(text: str) -> list[tuple[str, bool]]:
    """sentences(text), each paired with whether it OPENS a block (a paragraph,
    heading or list item, per html_to_text's block marks; the first sentence of a
    field opens one). sentences() is this list's projection, so rail 2 splits
    exactly where every other rail does. Linear: one finditer pass with the same
    splitter; a merged sentence is built from a parts list and joined once."""
    src = text or ""
    out: list[tuple[str, bool]] = []
    cur: list[str] = []          # the parts of the sentence being built
    cur_opens = False
    opens = True
    pos = 0

    def feed(part: str, newline_after: bool) -> None:
        nonlocal cur_opens, opens
        s = part.strip()
        if s:
            if cur and _ABBREV_TAIL_RE.search(cur[-1]):
                cur.append(s)            # rejoin after an abbreviation ("Dr." / "e.g.")
            else:
                if cur:
                    out.append((" ".join(cur), cur_opens))
                cur[:] = [s]
                cur_opens = opens
            opens = False
        if newline_after:
            opens = True

    for m in _SENT_SPLIT_RE.finditer(src):
        feed(src[pos:m.start()], "\n" in m.group(0))
        pos = m.end()
    feed(src[pos:], False)
    if cur:
        out.append((" ".join(cur), cur_opens))
    return out


_WORD_RE = re.compile(r"[A-Za-z0-9$][A-Za-z0-9'&/-]{0,40}")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


def _excerpt(text: str, limit: int = 160) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[: limit - 3] + "..."


def _redact(text: str, patterns: Iterable[re.Pattern[str]]) -> str:
    """Blank out cleared/negated spans BEFORE a rail scans.

    Length-preserving is not required; these are per-rail scratch copies.
    """
    out = text or ""
    for pat in patterns:
        out = pat.sub(" ", out)
    return out


# ---------------------------------------------------------------------------
# Brand-line detection
#
# The single subtlest thing in this module. "energy" is both a BRAND LINE (F3
# Energy) and the CATEGORY NOUN that appears in nearly every sentence of an
# energy-drink article. Rails 2 and 4 are scoped to the brand line, so conflating
# them would either block every draft or clear a real violation.
#
# Rule: a capitalized `Energy` is the brand UNLESS the next word is a category
# noun ("Energy drinks", "Energy Drinks" in a title-cased headline). Lowercase
# `energy` is never the brand. Validated against real cleared copy: the live title
# "Clean Energy Drinks for Yoga, Pilates, and Everyday Active Life" must NOT trip
# rail 2, and does not, because "Energy Drinks" is the category.
# ---------------------------------------------------------------------------

# ONLY words that are unambiguously the category. "brand"/"brands" and
# "product"/"products" were removed after they cleared real violations -- "The
# Energy brand is the cleanest label in the cooler" and "Energy products from us
# are clean" are both about OUR line, not about a category.
_CATEGORY_FOLLOWERS = frozenset({
    "drink", "drinks", "beverage", "beverages", "category", "categories",
    "level", "levels", "boost", "crash", "dip", "slump", "needs", "need",
    "source", "sources", "intake", "expenditure", "market", "aisle", "shelf",
    "industry", "space", "sector",
})

# `F3's Mood` and `F3 Mood` both count. Apostrophe forms were invisible before.
_BRAND_F3_RE = re.compile(r"\bF3(?:'s)?\s{0,3}(Energy|Pure|Mood)\b", re.IGNORECASE)
# Case-INSENSITIVE deliberately: an ALL-CAPS heading ("ENERGY IS OUR CLEANEST
# LINE") is a brand reference, and a case-sensitive pattern made rails 2, 3 and 4
# blind to every all-caps heading. Lowercase is then excluded below, which is
# where the real category/brand distinction is made.
_BRAND_BARE_RE = re.compile(r"\b(Energy|Pure|Mood)\b", re.IGNORECASE)
# A first-person product reference with no brand token at all: "our caffeine-free
# calm line", "our clean-sweetened blend". Rail 3 was gated on the literal token
# MOOD, so a Mood article that never named it was completely unguarded.
_OUR_PRODUCT_RE = re.compile(
    r"\bour\b[^.\n]{0,60}\b(?:line|lines|blend|blends|can|cans|drink|drinks|"
    r"formula|formulas|beverage)\b",
    re.IGNORECASE,
)


def _is_brandish(token: str) -> bool:
    """Capitalised or ALL-CAPS reads as the brand; lowercase never does."""
    return bool(token) and token[0].isupper()


def brand_lines_in(sentence: str) -> set[str]:
    """Which F3 product LINES this sentence refers to: {'ENERGY','PURE','MOOD'}."""
    text = sentence or ""
    found: set[str] = set()
    for m in _BRAND_F3_RE.finditer(text):
        found.add(m.group(1).upper())
    # An explicit "F3" anywhere in the sentence removes the category ambiguity:
    # "Energy drinks from F3 are all-natural" IS about the brand line, and the
    # category-follower exemption must not clear it.
    names_f3 = re.search(r"\bF3\b", text) is not None
    for m in _BRAND_BARE_RE.finditer(text):
        tok = m.group(1)
        if not _is_brandish(tok):
            continue  # "an energy drink" -- the category noun
        line = tok.upper()
        if line == "ENERGY" and not names_f3:
            nxt = _words(text[m.end():m.end() + 40])
            if nxt and nxt[0].lower() in _CATEGORY_FOLLOWERS:
                continue  # "Energy Drinks" in a title -- the category
        found.add(line)
    return found


def product_referenced(sentence: str) -> bool:
    """True when the sentence is about an F3 product at all, named or not."""
    return bool(brand_lines_in(sentence)) or bool(_OUR_PRODUCT_RE.search(sentence or ""))


# ---------------------------------------------------------------------------
# Rail lexicons
# ---------------------------------------------------------------------------

# rail 6 -- em-dash class. U+2014 and the other em-width dashes are always a trip.
# U+2013 (en dash) is a trip only when SPACED, i.e. used as an em dash; between
# digits it is a legitimate numeric range ("120-140 mg" typed as an en dash).
_EM_DASH_CHARS = "—―⸺⸻‒"
_EM_DASH_RE = re.compile("[" + _EM_DASH_CHARS + "]")
_EN_AS_EM_RE = re.compile(r"(?:\s–)|(?:–\s)")

# rail 5 -- prices. Three things had to be told apart here, and the separation was
# derived by running this rail over the nine LIVE News articles rather than guessed:
#
#   a price        "$39.99 per pack"                  -> rail 5, blocked
#   a count        "12 cans per pack"                 -> not money at all, allowed
#   outlet revenue "roughly $1.36 million in revenue" -> rail 8 EXPLICITLY allows a
#                                                        revenue figure already
#                                                        printed by an outlet to be
#                                                        restated with attribution,
#                                                        so rail 5 must not eat it
#
# The first cut blocked the live Tribune amplification article on its attributed
# revenue figure -- i.e. it would have blocked the exact class of News draft this
# lane exists to produce. So a bare currency amount is judged IN CONTEXT: a match
# inside a revenue/company-financials context is not a price. Nothing is lost on
# the embargo side, because raise size and valuation are rail 8's job and rail 8
# matches them directly ("raising $", "valuation", "term sheet", ...) rather than
# leaning on the currency symbol -- verified by test, not assumed.
_PRICE_UNCONDITIONAL_RES = (
    re.compile(r"\bMSRP\b", re.IGNORECASE),
    re.compile(r"\b(?:retail|sale|list|unit)\s{1,3}price\b", re.IGNORECASE),
    re.compile(r"\bcost\s{1,3}per\s{1,3}(?:can|serving|pack|bottle)\b", re.IGNORECASE),
    re.compile(r"\bprice\s{1,3}per\s{1,3}(?:can|serving|pack|bottle)\b", re.IGNORECASE),
    # Structured-data price fields carry no currency symbol at all
    # ({"price":"39.99","priceCurrency":"USD"}), so the currency scan below cannot
    # see them. A reader-invisible price in JSON-LD is still a published price,
    # and the Learn/News template's BlogPosting schema has no price field, so any
    # of these keys appearing is a genuine violation rather than template noise.
    re.compile(r"\"(?:price|lowPrice|highPrice|priceCurrency)\"\s{0,3}:"),
)
_CURRENCY_RES = (
    # Any currency symbol, not just the dollar: a price in pounds or euros is
    # still a price, and "&#163;34.99" passed the first cut outright.
    re.compile(r"[$£€¥₹]\s{0,2}\d"),
    re.compile(r"\b\d[\d,.]{0,12}\s{0,3}(?:dollars|USD|GBP|EUR)\b", re.IGNORECASE),
    # A bare two-decimal amount attached to a purchase verb or a retailer, which
    # is how a price reads when the symbol is left off ("grab a 12-pack for
    # 39.99 at any Sprouts"). Deliberately requires the buying context so an
    # ingredient figure ("39.99 mg") cannot trip it.
    re.compile(r"\b(?:for|only|just|at)\s{1,3}\d{1,4}\.\d{2}\b(?!\s{0,3}"
               r"(?:mg|g|ml|oz|kcal|%|percent))", re.IGNORECASE),
)
# Deliberately NOT a bare "sales": "our sales team" would then exempt any price
# within 200 chars of it, which is the hole this rail is supposed to be.
_REVENUE_CONTEXT_RE = re.compile(
    r"\b(?:revenues?|ARR|run[\s-]{0,3}rate|top[\s-]{0,3}line|grossed|grossing|"
    r"turnover|bookings|(?:net|gross)\s{1,3}sales|in\s{1,3}sales|sales\s{1,3}of)\b",
    re.IGNORECASE,
)
_PRICE_CONTEXT_CHARS = 200

# rail 2 -- clean/natural language is Pure-EXCLUSIVE.
_CLEAN_TOKENS = frozenset({
    "clean", "cleaner", "cleanest", "clean-label", "cleanlabel",
    "natural", "naturally", "all-natural", "clean-sweetened",
})
# ...but "naturally" has an ordinary factual sense that is not a clean-label
# claim: L-theanine IS naturally present in green tea, and saying so is
# chemistry, not marketing. Measured, not theoretical -- the first live draft of
# an ingredient explainer was rejected for "some L-theanine may be naturally
# present", and ingredient explainers are a whole pillar of the Learn lane, so
# this would have blocked that pillar indefinitely. These occurrence phrasings
# are redacted before the rail scans; "a natural energy drink" still trips.
_NATURAL_OCCURRENCE_RES = (
    re.compile(r"\bnaturally\s{1,3}(?:present|occurring|occurs|found|sourced|"
               r"contains?|derived)\b", re.IGNORECASE),
    re.compile(r"\b(?:occurs?|occurring|present|found)\s{1,3}naturally\b",
               re.IGNORECASE),
    re.compile(r"\bnaturally\s{1,3}in\b", re.IGNORECASE),
)

# rail 2, ATTRIBUTION SCOPE -- THE SHIPPING RAIL since R14-3 (Code #14, closes
# cq-85b35413b020). History: Code #13 slice 6 built it under the 2026-09-01 C6 (b)
# ruling CONDITIONED on a differential harness, and left it unwired because the
# harness gate could not pass (two ruled classes -- sugar-free on Pure, comparative
# category claims -- have no mechanical rail at all). Harrison's 2026-09-19 ruling
# ESC-1 (A) / D-329 took those two classes OUT of rail 2's ship condition (they
# are still probed and reported by rail2_harness, and seeded as rails of their
# own), ruled the two exact-phrase exemptions below (ESC 3(i)/(ii)), and the gate
# now passes: run_preflight calls rail2_attribution_hit. rail2_legacy_hit stays
# exported, byte-identical, as the FROZEN measurement baseline the harness and the
# differential script compare against -- run_preflight never calls it.
#
# WHAT "ATTRIBUTION" MEANS HERE: the clean word is predicated OF the Energy or Mood
# line. A clean token is exempt only in these positive, narrow shapes -- (1) it sits
# in a clause segment that names F3 Pure and NOT Energy/Mood AND is positively
# Pure's: Pure is that clause's subject, or the clause is the 8/26 locative disjunct
# ("... or the clean-sweetened version in F3 Pure": the live rejection), with no
# comparison / likeness / ellipsis anywhere in the sentence (D-051 r143-claims-1,
# see rail2_attribution_hit); (2) its object is the
# ENVIRONMENT as the object of an environmental ACTION ("fund a cleaner planet",
# "clean up the beaches": the CleanHub article), never a predicate of the brand;
# (3) one of the two ruled exact phrases in _RAIL2_PHRASE_EXEMPTIONS. Who said it is
# never an exemption (the Earthbar attributed quote is a TRUE positive), an ALL-CAPS
# or Title-Case clean word is not an exemption, and a clause that names Pure AND
# Energy is ambiguous -> trips (fail closed). Segments split on , ; : and "or /
# while / whereas / versus" -- deliberately NOT on "and": "F3 Pure is clean-
# sweetened and F3 Energy is too" attaches clean to Energy across "and".
#
# ENVIRONMENTAL REDACTION IS POSITION-CHECKED (R14-3). The Code #13 cut redacted
# "clean(er) <environmental noun>" wherever it sat, so the redaction could clear a
# PREDICATE: "F3 Energy is clean air in a can.", "F3 Energy is a cleaner future.",
# "F3 Energy is a clean world of flavor." all tripped the legacy rail and PASSED
# the attribution rail -- dormant holes while unwired, live ones the moment it
# shipped. Now the environmental phrase is redacted only as the OBJECT of a closed
# list of environmental-action verbs (fund / support / build / protect / restore /
# toward), "a cleaner future" only when it is FOR an environmental noun, and "clean
# up" only when its object is an environmental noun ("clean up your afternoon"
# trips). A copula ("is", "delivers") never introduces a redaction.
_ENV_NOUNS = (r"(?:planet|oceans?|waters?|beaches|coast(?:line)?s?|environment|earth|world|"
              r"air|grid|rivers?|streets?|communit(?:y|ies))")
_ENV_ACTION_VERBS = (r"(?:fund|funds|funded|funding|support|supports|supported|supporting|"
                     r"build|builds|building|protect|protects|protecting|restore|restores|"
                     r"restoring|toward|towards)")
_ENV_DETERMINER = r"(?:(?:a|an|the|our)\s{1,3})?"
_CLEANUP_OBJECTS = (r"(?:beach|beaches|oceans?|coast(?:line)?s?|parks?|rivers?|streets?|"
                    r"shorelines?|waterways?|trails?|litter|trash|plastic|neighbou?rhoods?|"
                    r"communit(?:y|ies))")
# A metaphorical environmental noun is a product claim wearing the environment's
# clothes: "builds a cleaner world of flavor", "protects clean water in every can",
# "supports a clean environment for your mind", "restores clean earth to your
# routine". The first cut refused only of / in / inside / within after the object:
# a BLACKLIST, so every other continuation ("for your taste buds", "at every
# workout", "to your routine") kept the redaction (D-051 r143-claims-4). The
# continuation is now an ALLOWLIST (fail closed). The environmental phrase is
# redacted only when it ends its sentence, or when it runs on -- directly or after
# a comma / semicolon / colon -- into a closed CSR continuation: "with every case
# sold", "every spring", "for the oceans", "for future generations", "around the
# world", "through CleanHub", "one case at a time". After the punctuation a CSR
# participle ("pulling 400 pounds of trash") or a third-party clause ("and fans
# love it") also qualifies, with no "you"/"your" to the end of the sentence.
# Anything else keeps the clean word in the scan ("supports clean communities in
# Mesa" trips too).
_ENV_PURCHASE_NOUNS = r"(?:cases?|purchases?|orders?|sales?|packs?|box(?:es)?)"
_ENV_WEEKDAYS = r"(?:mon|tues|wednes|thurs|fri|satur|sun)days?"
_ENV_TIME_NOUNS = (r"(?:years?|months?|weeks?|weekends?|seasons?|spring|summer|fall|autumn|"
                   r"winter|quarters?|" + _ENV_WEEKDAYS + r")")
#: The closed CSR continuations ("with every case sold", "one case at a time",
#: "through CleanHub", "by removing plastic", ...). Each begins at its first word;
#: the callers supply the whitespace or punctuation before it.
_ENV_CSR_TAILS = (
    r"(?:with\s{1,3}(?:every|each)\s{1,3}(?:" + _ENV_PURCHASE_NOUNS
    + r"|(?:cans?|bottles?)\s{1,3}(?:sold|purchased|bought))\b"
    r"|(?:every|each|this|next)\s{1,3}(?:" + _ENV_PURCHASE_NOUNS + r"|" + _ENV_TIME_NOUNS + r")\b"
    r"|one\s{1,3}(?:case|can|purchase|order|pack)\s{1,3}at\s{1,3}a\s{1,3}time\b"
    r"|on\s{1,3}" + _ENV_WEEKDAYS + r"\b"
    r"|(?:today|tomorrow|yesterday|together|weekly|monthly|annually|yearly)\b"
    r"|for\s{1,3}(?:(?:future|next)\s{1,3}generations?|(?:the|our)\s{1,3}" + _ENV_NOUNS + r")\b"
    r"|(?:across|around|throughout)\s{1,3}(?:the\s{1,3})?"
    r"(?:world|globe|country|nation|region|state|planet|coast)\b"
    r"|(?:through|via|with|alongside)\s{1,3}(?:F3|CleanHub|volunteers|fans"
    r"|(?:our|the|a|its)\s{1,3}(?:partners?|partnerships?|team|crew|community|volunteers|fans))\b"
    r"|by\s{1,3}(?:removing|pulling|funding|planting|recycling|collecting|cleaning|"
    r"restoring|protecting)\b"
    r")"
)
#: After a comma / semicolon / colon, two more shapes are CSR, never a metaphor: a
#: CSR participle ("..., pulling 400 pounds of trash") and a new clause with a
#: third-party subject ("..., and fans love it"). Both also need the rest of the
#: sentence (up to 300 characters, else fail closed) to hold no first or second
#: person: "..., helping you unwind" / "..., letting your mind settle" are product
#: metaphors wearing the environment's clothes.
_ENV_CSR_PARTICIPLES = (r"(?:removing|pulling|collecting|planting|recycling|restoring|protecting|"
                        r"clearing|picking|hauling|diverting|funding)\b")
_ENV_NEW_CLAUSE = (r"(?:(?:and|but|&)\s{1,3})?(?:fans|volunteers|customers|partners|CleanHub|we|F3|"
                   r"every|each|together|our\s{1,3}(?:team|fans|partners|volunteers|community|crew))\b")
_ENV_PERSON = r"(?:you|your|yours|yourself|yourselves|y'all|my|me|mine|myself)"
_ENV_NO_PERSON_TO_END = (r"(?=(?:(?!\b" + _ENV_PERSON + r"\b)[^.!?\n]){0,300}(?:[.!?\n]|$))")
_ENV_CONTINUATION_OK = (
    r"(?="
    # the phrase ends its SENTENCE (a ";" or ":" does not end it: the clause after
    # one is judged like the clause after a comma)
    r"\s{0,3}(?:[.!?)\]\"'”’]|$)"
    # ...or runs straight on into a closed CSR continuation
    r"|\s{1,3}" + _ENV_CSR_TAILS +
    # ...or a comma / semicolon / colon, then an ALLOWLISTED continuation. D-051
    # round 2 (F3-R3, r143-claims-4): round 1 made the bare continuation an
    # allowlist but left the comma branch a refuse-list, so it refused its own
    # ", one case at a time" and let ", helping you unwind", ", so you can relax",
    # ", perfect for evenings" through; and ";" / ":" were never checked at all.
    r"|\s{0,3}[,;:]\s{0,3}(?:" + _ENV_CSR_TAILS
    + r"|(?:" + _ENV_CSR_PARTICIPLES + r"|" + _ENV_NEW_CLAUSE + r")" + _ENV_NO_PERSON_TO_END + r")"
    r")"
)
_CLEAN_ENVIRONMENT_RES = (
    # "fund a cleaner planet", "supports clean water"
    re.compile(r"\b" + _ENV_ACTION_VERBS + r"\s{1,3}" + _ENV_DETERMINER
               + r"clean(?:er|est)?\s{1,3}" + _ENV_NOUNS + r"\b" + _ENV_CONTINUATION_OK,
               re.IGNORECASE),
    # "supports a cleaner future for the oceans" -- "future" alone is a predicate
    re.compile(r"\b" + _ENV_ACTION_VERBS + r"\s{1,3}" + _ENV_DETERMINER
               + r"clean(?:er|est)?\s{1,3}future\s{1,3}for\s{1,3}(?:(?:the|our)\s{1,3})?"
               + _ENV_NOUNS + r"\b" + _ENV_CONTINUATION_OK, re.IGNORECASE),
    # "helps clean up the beaches" -- the activity, with an environmental object.
    # The continuation allowlist also refuses a compound whose first half is the
    # object ("cleans up trash talk", r143-claims-6).
    re.compile(r"\bclean(?:ed|ing|s)?[\s-]{1,3}ups?\s{1,3}(?:(?:the|our|local)\s{1,3})?"
               + _CLEANUP_OBJECTS + r"\b" + _ENV_CONTINUATION_OK, re.IGNORECASE),
)

# The NOUN form ("a beach clean-up") names an EVENT, and News copy names events
# freely ("Join us at the F3 Energy beach clean-up this Saturday."), so its
# continuation is not allowlisted. Its POSITION is checked in _env_event_referenced
# instead: it is redacted only as a reference to the event (after a verb that runs
# or backs one, after a preposition that points at one, with the brand as a
# modifier, or opening the sentence). It is never redacted as a PREDICATE of the
# brand (r143-claims-6: "F3 Energy is a beach clean-up in a can." and "F3 Energy is
# the community clean-up crew for your afternoon slump." both passed BOTH rails).
# The lookahead still refuses a head noun ("clean-up crew") and a second-person
# or metaphor tail.
_ENV_EVENT_RE = re.compile(
    r"\b(?:beach|ocean|coastal|river|park|shoreline|community|neighbou?rhood|litter|trash|plastic)"
    r"\s{1,3}clean[\s-]{0,3}ups?\b"
    r"(?!\s{1,3}(?:crews?|kits?|modes?|machines?|squads?"
    r"|in\s{1,3}(?:a|every|each)\s{1,3}(?:cans?|sips?|bottles?|glass|cups?)"
    r"|inside|within|into|of\s{1,3}(?:your|you|my|flavor|taste)"
    r"|(?:for|to|on|at)\s{1,3}(?:your|you|my)"
    r"|for\s{1,3}the\s{1,3}(?:soul|mind|body|gut|palate))\b)",
    re.IGNORECASE)
#: Words skipped walking back from the event noun ("a", "our annual", "every").
_ENV_EVENT_SKIP = frozenset({
    "a", "an", "the", "our", "its", "their", "this", "next", "every", "each", "annual",
    "local", "monthly", "weekly", "yearly", "big", "huge", "first", "spring", "summer",
    "fall", "winter",
})
#: ...then the word that makes it a REFERENCE to the event, not a predicate.
_ENV_EVENT_LEADS = frozenset({
    "sponsor", "sponsors", "sponsored", "sponsoring", "host", "hosts", "hosted", "hosting",
    "join", "joins", "joined", "joining", "organize", "organizes", "organized", "organizing",
    "organise", "organises", "organised", "organising", "fund", "funds", "funded", "funding",
    "support", "supports", "supported", "supporting", "run", "runs", "running", "ran",
    "lead", "leads", "leading", "led", "fuel", "fuels", "fueled", "fueling", "power",
    "powers", "powered", "powering", "at", "for", "during", "to", "after", "before", "from",
})


def _env_event_referenced(text: str, start: int) -> bool:
    """True when the noun-form event at text[start:] is referenced (see above),
    False when it could be predicated of the brand. Walks back at most 120
    characters, so it is linear."""
    lo = max(0, start - 120)
    window = text[lo:start]
    toks = list(_WORD_RE.finditer(window))
    cut = len(window)
    while toks:
        tok = toks.pop()
        if window[tok.end():cut].strip():
            return False   # punctuation between: an apposition or a predicate, fail closed
        word = tok.group(0)
        wl = word.lower().strip("'&/-")
        if wl in _ENV_EVENT_SKIP:
            cut = tok.start()
            continue
        if wl in _ENV_EVENT_LEADS:
            return True
        # the brand as a modifier ("the F3 Energy beach clean-up", "F3 Energy's ...")
        return wl in ("energy", "pure", "mood", "energy's", "pure's", "mood's") and _is_brandish(word)
    return lo == 0 and not window[:cut].strip()   # the sentence opens with the event


def _redact_env_events(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _ENV_EVENT_RE.finditer(text):
        if not _env_event_referenced(text, m.start()):
            continue
        out.append(text[pos:m.start()])
        out.append(" ")
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)

# The two EXACT phrases Harrison cleared on 2026-09-19 (ESC 3(i)/(ii), D-329) --
# (pattern, the rail-2-scoped lines the phrase may be said of). Nothing else is
# cleared: no variant, no bare "natural"/"clean", no hyphenated form. Whitespace
# between the words is bounded (\s{1,3}); case is ignored (a sentence-initial or
# headline capital is the same phrase). FAIL-CLOSED on brand scope: a phrase is
# redacted only when every rail-2-scoped line the SENTENCE names is inside its
# allowed set -- so neither is ever redacted in a sentence that names F3 Mood.
_RAIL2_PHRASE_EXEMPTIONS: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    # the ingredient descriptor on Energy (the 9/14 lane jam)
    (re.compile(r"\bnatural\s{1,3}caffeine\s{1,3}from\s{1,3}green\s{1,3}tea\b", re.IGNORECASE),
     frozenset({"ENERGY"})),
    # the canonical lineup's Pure-vs-Energy line ("same flavor, cleaner fuel")
    (re.compile(r"\bcleaner\s{1,3}fuel(?:\s{1,3}source)?\b", re.IGNORECASE),
     frozenset({"ENERGY", "PURE"})),
)

# Shipping-rail-only additions (legacy's _CLEAN_TOKENS is frozen): the verb forms
# ("F3 Energy cleans up your afternoon" passed BOTH rails) and the cores a
# hyphenated compound is split into ("clean-energy", "cleaner-fuel",
# "naturally-caffeinated" were single tokens outside the set and passed BOTH rails).
# D-051 r143-claims-8: the adverb, the missing participle and the abstract nouns
# passed BOTH rails too ("F3 Energy burns cleanly.", "is cleansed of junk",
# "F3 Energy's cleanliness / naturalness sets it apart"). They are listed as whole
# tokens, NEVER as a prefix match: a "clean" prefix would read the CleanHub partner
# name (a measured false positive) as a claim.
#
# r143-claims-7 is DECIDED FAIL-CLOSED: the verb tokens over-trip idioms ("spring
# cleaning", "cleaned out every cooler", "clean-and-jerk"), and they are NOT
# redacted. Each redaction candidate would clear a claim shape of its own ("F3
# Energy cleaned out my system", "a spring cleaning for your body", "clean and
# jerk-free"), and the cost of an over-trip is one bounded revision, not a leak.
_ATTRIBUTION_CLEAN_TOKENS = _CLEAN_TOKENS | frozenset({
    "cleans", "cleaned", "cleaning", "cleanse", "cleanses", "cleansing",
    "cleansed", "cleanly", "cleanliness", "cleanness", "naturalness", "naturals",
})
# ...with the chemistry exemption kept for the hyphenated spelling too, so the
# compound split cannot turn "a naturally-occurring amino acid" into a trip.
_NATURAL_OCCURRENCE_HYPHEN_RE = re.compile(
    r"\bnaturally-(?:present|occurring|occurs|found|sourced|derived)\b", re.IGNORECASE)
_CLAUSE_SPLIT_RE = re.compile(r"[,;:]|\b(?:or|while|whereas|versus|vs\.?)\b", re.IGNORECASE)


def _is_clean_token(tok: str) -> bool:
    """One lower-cased, edge-stripped token: a clean token itself, or a hyphen/slash
    compound with a clean part ("clean-energy")."""
    if tok in _ATTRIBUTION_CLEAN_TOKENS:
        return True
    if "-" in tok or "/" in tok:
        return any(part in _ATTRIBUTION_CLEAN_TOKENS for part in tok.replace("/", "-").split("-"))
    return False


def _attribution_clean_hits(seg: str) -> set[str]:
    """Clean tokens in one clause segment: whole tokens, plus every part of a
    hyphen/slash compound ("clean-energy" -> 'clean'). Linear: each token is split
    once."""
    hit: set[str] = set()
    for w in _words(seg):
        tok = w.lower().strip("'&/-")
        if _is_clean_token(tok):
            hit.add(tok)
    return hit


#: Words that, directly before a ruled phrase, MODIFY its clean word -- the phrase
#: is then no longer the exact ruled phrase (D-051 r143-claims-2: "all natural
#: caffeine from green tea", "the most natural ...", "a much cleaner fuel source"
#: all cleared because the pattern matched the tail). Degree adverbs ("-ly" words,
#: see _PHRASE_NON_DEGREE_LY), bare numbers, multipliers and percentages, and
#: hyphen/slash-joined prefixes ("all-natural", "super-cleaner") are refused
#: STRUCTURALLY in _is_phrase_modifier; this set covers the degree words that are
#: none of those, including the colloquial intensifiers the first cut missed ("way
#: cleaner fuel", "miles cleaner", "next-level natural caffeine": round-2 review).
_PHRASE_MODIFIERS = frozenset({
    "all", "most", "more", "much", "very", "so", "super", "ultra", "extra", "pure", "real",
    "true", "genuine", "whole", "total", "complete", "entire", "full", "absolute",
    "authentic", "raw", "percent", "cent", "far", "even", "lot", "lots", "ever", "just",
    "100",
    "way", "miles", "tons", "loads", "crazy", "mega", "hyper", "uber", "wicked", "hella",
    "certified", "twice", "double", "triple", "next-level", "straight-up", "top-notch",
})
#: ...except "way" after a definite determiner, where it is a noun: "the way
#: natural caffeine from green tea works" names a manner, not a degree. "a way
#: cleaner fuel source" is still the degree ("a" is not in the set, fail closed).
_PHRASE_MODIFIERS_NOUN_AFTER_DET = frozenset({"way"})
_PHRASE_DETERMINERS = frozenset({"the", "this", "that", "which", "every", "any", "no", "one",
                                 "each", "whatever"})
#: "-ly" words that say nothing about the clean word's DEGREE (D-051 round-2 F3-R2):
#: focus particles ("uses only natural caffeine from green tea" -- which caffeine,
#: not how natural) and frequency words ("your daily natural caffeine ..."). Every
#: other "-ly" word still counts, so an unlisted intensifier ("refreshingly natural
#: caffeine from green tea") stays fail-closed. A CLOSED degree list would have
#: released it -- a hole the round-1 structural rule did not have.
_PHRASE_NON_DEGREE_LY = frozenset({
    "only", "solely", "exclusively", "daily", "weekly", "monthly", "yearly", "nightly", "hourly",
})
#: A number fused to its unit ("120mg", "120-mg", "12oz", "12-pack") is a
#: QUANTITY, i.e. a fact, not a degree (the house spelling "200mg natural caffeine",
#: F3-R2). A bare number, a multiplier ("2x") or a percentage still counts.
_PHRASE_QUANTITY_RE = re.compile(
    r"^\d{1,6}(?:[.,]\d{1,3})?-?(?:mg|mcg|g|kg|ml|l|oz|fl|kcal|cal|calories|calorie|milligrams?|"
    r"grams?|ounces?|cans?|packs?|servings?|count|ct)$")
#: A coordinated modifier ("pure and natural caffeine from green tea") is checked
#: through ONE coordinator.
_PHRASE_COORDINATORS = frozenset({"and", "or", "plus", "nor"})


def _is_phrase_modifier(word: str) -> bool:
    w = word.lower().strip("'&/-")
    if not w:
        return False
    if w in _PHRASE_MODIFIERS or w in _ATTRIBUTION_CLEAN_TOKENS:
        return True
    if "%" in w:
        return True
    if any(ch.isdigit() for ch in w):
        return _PHRASE_QUANTITY_RE.match(w) is None
    if len(w) > 3 and w.endswith("ly"):
        return w not in _PHRASE_NON_DEGREE_LY
    return any(p in _PHRASE_MODIFIERS or p in _ATTRIBUTION_CLEAN_TOKENS
               for p in w.replace("/", "-").split("-") if p)


def _phrase_is_modified(text: str, start: int, end: int) -> bool:
    """True when the ruled phrase at text[start:end] is not EXACT: something is
    fused to either edge, or the word before it modifies its clean word. Linear:
    it reads at most 80 characters before the match."""
    if start > 0 and (text[start - 1].isalnum() or text[start - 1] in "-/'&%_"):
        return True   # "all-natural caffeine ...", "super-cleaner fuel", "100%natural"
    if end < len(text) and text[end] in "-/":
        return True   # "cleaner fuel-like energy"
    window = text[max(0, start - 80):start]
    toks = list(_WORD_RE.finditer(window))
    cut = len(window)
    for _ in range(2):   # the word before, and one coordinated word before that
        if not toks:
            return False
        tok = toks.pop()
        gap = window[tok.end():cut].strip()
        if "%" in gap:
            return True   # "100 % natural caffeine ..."
        if gap in ("&", "+", ","):
            # "pure & natural ...", and a COORDINATE adjective, "real, natural ..."
            # ("Same flavor, cleaner fuel" still clears: "flavor" modifies nothing)
            return _is_phrase_modifier(tok.group(0))
        if gap:
            return False  # other punctuation (":" / ";" / "("): not a modifier
        word = tok.group(0).lower().strip("'&/-")
        if word in _PHRASE_COORDINATORS:
            cut = tok.start()
            continue
        if word in _PHRASE_MODIFIERS_NOUN_AFTER_DET and toks:
            before = window[toks[-1].end():tok.start()].strip()
            if not before and toks[-1].group(0).lower() in _PHRASE_DETERMINERS:
                return False   # "the way natural caffeine ...": a noun
        return _is_phrase_modifier(word)
    return False


def _redact_exact_phrase(pat: re.Pattern[str], text: str) -> str:
    """pat.sub(" ", text), skipping every match _phrase_is_modified refuses."""
    out: list[str] = []
    pos = 0
    for m in pat.finditer(text):
        if _phrase_is_modified(text, m.start(), m.end()):
            continue
        out.append(text[pos:m.start()])
        out.append(" ")
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def _redact_phrase_exemptions(sentence: str, lines: set[str]) -> str:
    """Blank the ruled exact phrases, brand-scoped and fail-closed (see above).
    EXACT means exact at both edges: a modifier directly before the phrase, or
    anything fused to it, keeps the phrase in the scan (r143-claims-2)."""
    scoped = lines & {"ENERGY", "MOOD"}
    out = sentence
    for pat, allowed in _RAIL2_PHRASE_EXEMPTIONS:
        if scoped <= allowed:
            out = _redact_exact_phrase(pat, out)
    return out


def rail2_legacy_hit(sentence: str) -> tuple[str, str] | None:
    """The pre-R14-3 shipping rail; never called by run_preflight.

    FROZEN measurement baseline (keep byte-identical): (clean_token, 'ENERGY/MOOD')
    when any clean token shares the sentence with the Energy or Mood LINE (natural-
    occurrence phrasings redacted first); None otherwise. rail2_harness and the
    differential script compare the shipping rail (rail2_attribution_hit) against
    it, so every input the R14-3 wiring releases stays measurable."""
    sent = sentence or ""
    lines = brand_lines_in(sent)
    if not (lines & {"ENERGY", "MOOD"}):
        return None
    scan_sent = _redact(sent, _NATURAL_OCCURRENCE_RES)
    toks = {w.lower().strip("'&/-") for w in _words(scan_sent)}
    hit = toks & _CLEAN_TOKENS
    if not hit:
        return None
    return sorted(hit)[0], "/".join(sorted(lines & {"ENERGY", "MOOD"}))


#: Words a clause segment may hold and still be a BARE brand mention ("or F3 Pure",
#: "and F3 Energy"): the brand tokens themselves plus these fillers. Anything else
#: makes it a clause of its own.
_BARE_BRAND_FILLER = frozenset({"f3", "f3's", "and", "the", "a", "an", "also", "too", "even"})


def _bare_brand_segment(seg: str) -> bool:
    """True when a clause segment names an F3 line and NOTHING else -- "F3 Pure or
    F3 Energy" splits on "or" into a real clause and a bare coordinated brand, and
    the bare half belongs to the clause before it, not to a clause of its own."""
    named = False
    for w in _words(seg):
        wl = w.lower().strip("'&/-")
        if wl in _BARE_BRAND_FILLER:
            continue
        if wl in ("energy", "pure", "mood") and _is_brandish(w):
            named = True
            continue
        return False
    return named


_RAIL2_EM = frozenset({"ENERGY", "MOOD"})
_RAIL2_DISJUNCTIONS = frozenset({"or", "versus", "vs"})


def _clause_split(text: str) -> list[tuple[str, str]]:
    """[(segment, the delimiter that ENDS it)], with '' after the last segment. It
    makes the same cut as _CLAUSE_SPLIT_RE.split, but keeps the delimiters, which P2
    needs (the 8/26 shape is a DISJUNCT)."""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _CLAUSE_SPLIT_RE.finditer(text):
        out.append((text[pos:m.start()], m.group(0).lower().rstrip(".")))
        pos = m.end()
    out.append((text[pos:], ""))
    return out


class _Clause:
    """One non-empty clause segment plus the bare coordinated brands folded into it.
    `prev` / `next` are the delimiters that separate it from its neighbours."""

    __slots__ = ("host", "bare", "prev", "next", "pron")

    def __init__(self, host: str, prev: frozenset[str], pron: bool = False):
        self.host = host
        self.bare: list[str] = []
        self.prev = prev
        self.next: frozenset[str] = frozenset()
        self.pron = pron


def _rail2_clauses(scan: str, pron_flags: list[bool] | None = None) -> list[_Clause]:
    """Non-empty clause segments. A bare coordinated brand ("... or F3 Energy") is
    folded into the non-empty clause before it. An empty segment (", or" leaves one)
    is skipped, so a bare brand can never land in an empty host, and its delimiters
    are carried to its neighbours. Bare parts are kept as a list and joined once per
    clause, so a long run of ", F3 Pure" stays linear."""
    groups: list[_Clause] = []
    pending: set[str] = set()
    for i, (seg, delim) in enumerate(_clause_split(scan)):
        if _words(seg):
            if groups and _bare_brand_segment(seg):
                groups[-1].bare.append(seg)
                groups[-1].pron = groups[-1].pron or bool(pron_flags and pron_flags[i])
            else:
                if groups:
                    groups[-1].next = frozenset(pending)
                groups.append(_Clause(seg, frozenset(pending), bool(pron_flags and pron_flags[i])))
            pending = set()
        if delim:
            pending.add(delim)
    if groups:
        groups[-1].next = frozenset(pending)
    return groups


#: Leading words skipped before a clause's subject ("and F3 Pure is ...").
_LEAD_FILLERS = frozenset({
    "and", "but", "yet", "so", "then", "plus", "while", "whereas", "whilst", "though",
    "although", "meanwhile", "instead",
})
_PURE_HEAD_NOUNS = frozenset({"can", "cans", "line", "formula", "recipe", "blend"})
_PURE_SUBJECT_ADVERBS = frozenset({"now", "still", "always", "simply", "instead", "itself"})
#: A CLOSED list (fail closed): a verb that is missing only costs a trip.
_PURE_PREDICATE_VERBS = frozenset({
    "is", "isn't", "was", "wasn't", "are", "aren't", "were", "weren't", "will", "stays",
    "remains", "uses", "used", "carries", "has", "gets", "keeps", "brings", "offers",
    "delivers", "runs", "relies", "takes", "leans", "swaps", "trades", "adds", "sticks",
    "pairs", "goes", "comes", "sweetens", "features", "contains", "blends", "skips", "drops",
    "replaces", "starts", "becomes", "does", "doesn't", "launches", "launched", "arrives",
    "gives", "packs", "provides", "opts", "chooses", "tastes",
})
_P2_DETERMINERS = frozenset({"the", "a", "an"})
_P2_LOCATIVES = frozenset({"in", "of", "from", "inside"})


def _lower_tokens(words: list[str]) -> list[str]:
    return [w.lower().strip("'&/-") for w in words]


def _pure_is_subject(words: list[str]) -> bool:
    """P1: Pure is the clause's SUBJECT -- "[and|while ...] [the] F3 Pure [can] [now]
    <predicate verb> ...". A possessive ("F3 Pure's clean base"), a verb-less
    appositive ("F3 Pure's all-natural sibling"), a comparison ("as clean as F3
    Pure") or anything else before the brand fails, so the clean word is not
    positively Pure's and it trips (D-051 r143-claims-1)."""
    t = _lower_tokens(words)
    n = len(t)
    i = 0
    while i < n and t[i] in _LEAD_FILLERS:
        i += 1
    if i < n and t[i] == "the":
        i += 1
    f3 = i < n and t[i] in ("f3", "f3's")
    if f3:
        i += 1
    if i >= n or t[i] != "pure" or not (f3 or _is_brandish(words[i])):
        return False
    i += 1
    if i < n and t[i] in _PURE_HEAD_NOUNS:
        i += 1
    if i < n and t[i] in _PURE_SUBJECT_ADVERBS:
        i += 1
    return i < n and t[i] in _PURE_PREDICATE_VERBS


def _brand_after_locative(words: list[str], line: str) -> bool:
    """True when `line` is named in this clause only as the object of a locative
    preposition ("... the full stack IN F3 Energy")."""
    t = _lower_tokens(words)
    target = line.lower()
    seen = False
    for k, w in enumerate(t):
        if w != target or not ((k > 0 and t[k - 1] in ("f3", "f3's")) or _is_brandish(words[k])):
            continue
        j = k - 1
        if j >= 0 and t[j] in ("f3", "f3's"):
            j -= 1
        if j < 0 or t[j] not in _P2_LOCATIVES:
            return False
        seen = True
    return seen


def _pure_locative_disjunct(words: list[str], idx: int, n_clauses: int,
                            prev: frozenset[str], nxt: frozenset[str],
                            other_words: list[str] | None = None) -> bool:
    """P2: the 8/26 live shape. "Explore the full stack in F3 Energy or THE
    CLEAN-SWEETENED VERSION IN F3 PURE." It is a disjunct noun phrase whose clean
    word modifies a head noun located in / of / from F3 Pure, PARALLEL to a disjunct
    that names Energy/Mood the same way ("the full stack in F3 Energy").
      * The clause must be one side of an "or" / "versus": the LAST clause after
        one, which must OPEN with the determiner (a noun phrase, not a verb phrase
        sharing an Energy/Mood subject: "F3 Mood keeps you calm or delivers the
        natural calm of F3 Pure." trips). Or it is the FIRST clause before one,
        with at most an imperative verb before the determiner (the mirror).
      * A middle clause ("F3 Energy, or the clean version of F3 Pure, hits hard.")
        is an apposition and fails.
      * The other disjunct (`other_words`) must name its Energy/Mood line only after
        a locative preposition. "F3 Mood is calm or the natural pick of F3 Pure."
        makes Mood the subject, so the clean noun phrase can be its predicate; it
        trips.
      * Every clean word sits between the determiner and the preposition."""
    last = idx == n_clauses - 1 and bool(prev & _RAIL2_DISJUNCTIONS)
    first = idx == 0 and bool(nxt & _RAIL2_DISJUNCTIONS)
    if not (last or first):
        return False
    other = list(other_words or ())
    em = brand_lines_in(" ".join(other)) & _RAIL2_EM
    if not em or not all(_brand_after_locative(other, line) for line in em):
        return False
    t = _lower_tokens(words)
    p = next((k for k, w in enumerate(t)
              if w == "pure" and ((k > 0 and t[k - 1] in ("f3", "f3's")) or _is_brandish(words[k]))), -1)
    if p < 0:
        return False
    k = p - 1
    if k >= 0 and t[k] in ("f3", "f3's"):
        k -= 1
    if k < 0 or t[k] not in _P2_LOCATIVES:
        return False
    d = max((j for j in range(k) if t[j] in _P2_DETERMINERS), default=-1)
    if d < 0 or k - d > 8:
        return False
    lead = 0
    while lead < len(t) and t[lead] in _LEAD_FILLERS:
        lead += 1
    if (last and d != lead) or (first and not last and d - lead > 1):
        return False
    cleans = [j for j, w in enumerate(t) if _is_clean_token(w)]
    return bool(cleans) and all(d < j < k for j in cleans)


#: A clause holding any of these RELATES two lines (comparison, likeness, a shared
#: property, ellipsis), so a clean word in the sentence can transfer to Energy/Mood.
#: "F3 Pure is clean-sweetened, and F3 Energy is too." and "F3 Pure is clean, like
#: F3 Energy." both passed. While any clause in the sentence holds one, NO clause
#: clears (r143-claims-1). The token veto only ever ADDS trips.
_RELATION_TOKENS = frozenset({
    "like", "alike", "same", "similar", "similarly", "than", "too", "also", "likewise",
    "equally", "sibling", "siblings", "twin", "twins", "companion", "companions", "cousin",
    "cousins", "counterpart", "counterparts", "share", "shares", "shared", "sharing",
    "borrow", "borrows", "borrowed", "borrowing", "both", "either", "neither", "match",
    "matches", "matched", "matching", "mirror", "mirrors", "mirrored", "mirroring",
    "identical", "equivalent", "inherit", "inherits", "inherited", "suit",
})
#: ...except that a BRAND-LESS clause's "both" is a plain plural ("and both taste
#: great"), not a transfer.
_RELATION_WEAK_EXEMPT = frozenset({"both"})
_AUX = frozenset({"is", "are", "was", "were", "does", "do", "did", "has", "have", "had",
                  "can", "will", "would", "could", "should"})
#: "like" is a VERB after these ("If you like F3 Energy, ..."), not a comparison.
_LIKE_VERB_SUBJECTS = frozenset({"you", "we", "they", "i", "who", "fans", "people", "would",
                                 "you'd", "we'd"})


def _has_relation(words: list[str], *, weak: bool = False) -> bool:
    t = _lower_tokens(words)
    n = len(t)
    for i, w in enumerate(t):
        if w in _RELATION_TOKENS:
            if weak and w in _RELATION_WEAK_EXEMPT:
                continue
            if w == "like" and i > 0 and t[i - 1] in _LIKE_VERB_SUBJECTS:
                continue
            return True
        if w == "as" and i + 1 < n:
            if t[i + 1] in _AUX or t[i + 1] in ("well", "with", "in") or "as" in t[i + 1:i + 5]:
                return True   # "as is F3 Energy", "as well", "as with", "as clean as"
        if w == "so" and i + 1 < n and t[i + 1] in _AUX:
            return True       # "and so is F3 Energy"
    return bool(t) and t[-1] in _AUX   # VP ellipsis: "..., and F3 Energy does."


#: Pronouns that point back at a line named in an EARLIER sentence ("F3 Mood is our
#: evening can. It is all-natural."). Demonstratives count only as a clause's
#: first word, which is where they are its subject ("This is all-natural.");
#: elsewhere they are usually determiners ("this season").
_BACKREF_PRONOUNS = frozenset({
    "it", "it's", "its", "itself", "they", "they're", "their", "theirs", "them", "themselves",
    "both",
})
_BACKREF_DEMONSTRATIVES = frozenset({"this", "that", "that's", "these", "those"})


#: D-051 round 2 (F3-R1): pronouns that point at NOTHING, and so can never carry a
#: line across a sentence. Matched on exact token sequences, never on a single
#: word, and each exclusion removes only that one occurrence: "It's worth noting
#: it is all-natural." still refers back through its second "it". DELIBERATELY
#: NARROW -- the first cut of this list also excluded object idioms ("keep it
#: simple", "break it down", "that's it") and every "it is <adjective> to", and
#: adversarial probing found each one laundering: "Keep it simple and natural.",
#: "That's it: all-natural.", "It is easy to love: all-natural." (tough-movement:
#: the "it" IS the can), "It's worth trying: all-natural." Those stay referential
#: (fail closed); an over-trip costs one bounded revision. What is excluded:
#:   * expletive "it" ahead of a clause, never before a COLON (whatever follows a
#:     colon is the content of "it"): "it's worth noting / mentioning / knowing
#:     ...", "it's time to", "it's no secret / wonder / surprise", "it is
#:     <important / true / clear / likely / ...> that / whether / how / why / what
#:     / if / when", "it is <important / essential / crucial / vital / necessary>
#:     to <a verb>" (not "to us" / "to you"), "it turns out", "it seems / appears
#:     that", "it depends", "it helps / pays to", "it makes sense", "it goes
#:     without saying";
#:   * generic "they": "they say / said / call" (an "it" after it still counts);
#:   * the object of a clean VERB in a household-care frame ("rinse your shaker
#:     and clean it weekly"): followed by a closed list of frequency / manner words
#:     (weekly, daily, regularly, thoroughly, after, before, every ...). "We cleaned
#:     it up.", "We cleaned it: no junk." and "We clean it with monk fruit." still
#:     refer back -- a reformulation claim.
_WORTH_INFO_VERBS = frozenset({
    "noting", "mentioning", "remembering", "knowing", "asking", "considering", "repeating",
    "saying", "stressing", "emphasizing", "emphasising", "highlighting", "pointing",
    "understanding", "recalling",
})
_EXTRAPOSITION_THAT_ADJ = frozenset({
    "important", "essential", "crucial", "vital", "true", "clear", "obvious", "possible",
    "impossible", "likely", "unlikely", "surprising", "common", "normal", "notable",
    "worth", "evident", "certain", "unclear",
})
_EXTRAPOSITION_THAT_COMP = frozenset({"that", "whether", "if", "when", "how", "why", "what"})
_EXTRAPOSITION_TO_ADJ = frozenset({"important", "essential", "crucial", "vital", "necessary"})
#: after "it is important to": a pronoun / determiner means "important TO someone"
#: (referential: the can is important to us), not an infinitive.
_NOT_AN_INFINITIVE = frozenset({
    "us", "me", "you", "them", "him", "her", "our", "your", "their", "my", "his", "its", "the",
    "a", "an", "everyone", "anyone", "everybody", "anybody", "people", "fans", "athletes",
})
_COPULA_AFTER_IT = frozenset({"is", "was", "s"})   # "it is", "it was", curly "it’s" -> it + s
_CLEAN_VERB_FORMS = frozenset({"clean", "cleans", "cleaned", "cleaning", "cleanse", "cleanses",
                               "cleansed", "cleansing"})
_HOUSEHOLD_CARE_AFTER = frozenset({
    "weekly", "daily", "nightly", "regularly", "often", "thoroughly", "properly", "well", "after",
    "before", "every", "each", "once", "twice", "between",
})
_BACKREF_COORDINATORS = frozenset({"and", "or", "nor", "plus", "&"})


def _nonreferential_positions(t: list[str], *, colon_after: bool) -> set[int]:
    """Indices of pronoun tokens in `t` (lower-cased clause tokens) that refer to
    nothing, per the list above. Linear: a constant look-around per token."""
    n = len(t)
    out: set[int] = set()
    if colon_after:
        return out   # a colon makes what follows the content of the pronoun

    def at(k: int) -> str:
        return t[k] if 0 <= k < n else ""

    for i, w in enumerate(t):
        if w in ("it", "it's"):
            k = i + 1
            if w == "it" and at(k) in _COPULA_AFTER_IT:
                k += 1
            copula = w == "it's" or k == i + 2
            nxt, nxt2, nxt3 = at(k), at(k + 1), at(k + 2)
            if copula and ((nxt == "worth" and nxt2 in _WORTH_INFO_VERBS)
                           or (nxt == "time" and nxt2 == "to")
                           or (nxt == "no" and nxt2 in ("secret", "wonder", "surprise"))
                           or (nxt in _EXTRAPOSITION_THAT_ADJ and nxt2 in _EXTRAPOSITION_THAT_COMP)
                           or (nxt in _EXTRAPOSITION_TO_ADJ and nxt2 == "to" and nxt3
                               and nxt3 not in _NOT_AN_INFINITIVE)):
                out.add(i)
                continue
            if w == "it" and ((at(i + 1) in ("turns", "turned") and at(i + 2) == "out")
                              or (at(i + 1) in ("seems", "seemed", "appears", "appeared")
                                  and at(i + 2) == "that")
                              or at(i + 1) in ("depends", "depended")
                              or (at(i + 1) in ("helps", "pays") and at(i + 2) == "to"
                                  and at(i + 3) not in _NOT_AN_INFINITIVE)
                              or (at(i + 1) in ("makes", "made") and at(i + 2) == "sense")
                              or (at(i + 1) == "goes" and at(i + 2) == "without" and at(i + 3) == "saying")):
                out.add(i)
                continue
        if w == "they" and at(i + 1) in ("say", "said", "says", "call"):
            out.add(i)
            continue
        if w == "it" and at(i - 1) in _CLEAN_VERB_FORMS and at(i + 1) in _HOUSEHOLD_CARE_AFTER:
            out.add(i)
    return out


def _back_reference_tokens(words: list[str], *, coordinated_only: bool = False,
                           colon_after: bool = False) -> list[str]:
    """The back-referring tokens of one clause (see _BACKREF_PRONOUNS and the
    non-referential list above). With `coordinated_only` -- a clause that names an
    Energy/Mood line itself -- a pronoun counts only when it is COORDINATED with a
    brand ("F3 Energy and it both run on ...", "It and F3 Energy share ..."): any
    other pronoun there resolves to the clause's own line (D-051 round 2,
    r143-claims-5: the blanket exemption let "F3 Energy and it" launder the ruled
    phrase onto Mood)."""
    t = _lower_tokens(words)
    skip = _nonreferential_positions(t, colon_after=colon_after)
    found: list[str] = []
    for i, w in enumerate(t):
        if i in skip or w not in _BACKREF_PRONOUNS:
            continue
        if coordinated_only and not ((i > 0 and t[i - 1] in _BACKREF_COORDINATORS)
                                     or (i + 1 < len(t) and t[i + 1] in _BACKREF_COORDINATORS)):
            continue
        found.append(w)
    if coordinated_only:
        return found
    i = 0
    while i < len(t) and t[i] in _LEAD_FILLERS:
        i += 1
    if i < len(t) and t[i] in _BACKREF_DEMONSTRATIVES and i not in skip:
        found.append(t[i])
    return found


def _has_back_reference(words: list[str]) -> bool:
    return bool(_back_reference_tokens(words))


#: Possessives a Pure-SUBJECT sentence resolves to Pure itself ("F3 Pure uses
#: organic cane sugar, monk fruit and stevia as ITS clean-sweetened base").
#: Singular only: a plural "their" is not a single line's.
_SELF_POSSESSIVES = frozenset({"its", "itself"})


def _rail2_backref_flags(sentence: str) -> list[bool]:
    """Per _clause_split segment: True when the segment carries a back-reference,
    i.e. may point at a line named in an EARLIER sentence. A segment that names an
    Energy/Mood line itself resolves its own pronouns ("F3 Pure and F3 Energy both
    ..."), unless a pronoun is coordinated with the brand ("F3 Energy and it").

    SAME-SENTENCE RESOLUTION (D-051 round 2, F3-R1): when the first clause that
    names a line has F3 Pure as its SUBJECT (P1), a possessive in that clause or a
    later one resolves to Pure -- unless that clause relates two lines, or holds
    any other back-reference. The canonical lineup sentence "F3 Pure uses organic
    cane sugar, monk fruit and stevia as its clean-sweetened base." tripped "near
    ENERGY" after any Energy sentence, title or field, because the sweetener-list
    comma put its "its" in a brand-less segment -- including Harrison's ruled
    two-sentence remedy for the 9/1 UNDECIDED shape."""
    segs = _clause_split(sentence or "")
    words = [_words(seg) for seg, _ in segs]
    toks: list[list[str]] = []
    for (seg, delim), w in zip(segs, words):
        em = bool(w) and bool(brand_lines_in(seg) & _RAIL2_EM)
        toks.append(_back_reference_tokens(w, coordinated_only=em, colon_after=delim == ":")
                    if w else [])
    flags = [bool(tk) for tk in toks]
    k = next((i for i, (seg, _) in enumerate(segs) if brand_lines_in(seg)), -1)
    if k >= 0 and not any(flags[:k]) and brand_lines_in(segs[k][0]) == {"PURE"} \
            and _pure_is_subject(words[k]):
        for j in range(k, len(segs)):
            if (flags[j] and set(toks[j]) <= _SELF_POSSESSIVES and not _has_relation(words[j])
                    and not (brand_lines_in(segs[j][0]) & _RAIL2_EM)):
                flags[j] = False
    return flags


def _phrase_scope_widens(segs: list[tuple[str, str]], pron) -> bool:
    """True when a back-referring clause can take a ruled phrase, so the carried
    lines must join the exemption scope (a phrase is never cleared for Mood through
    a pronoun). D-051 round 2 (r143-claims-5 PARTIAL): round 1 widened the scope
    only when the phrase sat INSIDE the pronoun's own clause, so moving the pronoun
    one clause away redacted the phrase again ("F3 Mood is our evening can. It, like
    F3 Energy, runs on a cleaner fuel source."; "..., and so does it."). Now it
    widens when a back-referring clause
      (i)   holds the phrase;
      (ii)  comes BEFORE the first clause that holds it (a sentence-initial "It"
            is the subject the phrase is predicated of);
      (iii) relates to it -- a relation / ellipsis token ("so does it", "it does
            too", "like it").
    A pronoun AFTER the phrase with no relation ("F3 Energy carries 120 mg of
    natural caffeine from green tea, and its L-theanine keeps it smooth.") does
    not: that pronoun is the sentence's own subject."""
    flagged = [i for i, p in enumerate(pron) if p]
    if not flagged:
        return False
    holds = {i for i, (seg, _) in enumerate(segs)
             if any(pat.search(seg) for pat, _ in _RAIL2_PHRASE_EXEMPTIONS)}
    if not holds:
        return False
    first = min(holds)
    return any(i in holds or i < first or _has_relation(_words(segs[i][0])) for i in flagged)


def rail2_context_after(sentence: str, context: frozenset[str]) -> frozenset[str]:
    """The lines a LATER sentence's pronoun may refer to, once this sentence is read.
    It holds the sentence's own lines, plus the carried context when this sentence
    itself refers back. A brand-less sentence keeps the carried lines here; how
    long they stay in VIEW is Rail2Carry's decay rule (D-051 round 2, F3-R1)."""
    own = brand_lines_in(sentence or "")
    if not own:
        return context
    if any(_rail2_backref_flags(sentence)):
        own = own | context
    return frozenset(own)


#: D-051 round 2 (F3-R1): how far a pronoun reaches back. Round 1 carried the lines
#: of the last line-naming sentence forever -- across any number of brand-less
#: sentences, paragraphs and fields -- so "It is a natural component of green tea."
#: in an L-theanine paragraph tripped "near ENERGY" because a paragraph earlier
#: said "F3 Energy pairs caffeine with L-theanine." THE RULE (pinned by tests):
#:   * a pronoun sentence sees the lines named by the sentence before it, or by the
#:     one before that when the sentence between them is brand-less and stays in
#:     the naming sentence's block (paragraph / heading / list item);
#:   * a block boundary that falls BEFORE an intervening sentence ends the reach: a
#:     new paragraph's first sentence may still point back ("F3 Mood is our evening
#:     can.</p><p>It is all-natural."), its second may not ("...</p><p>L-theanine is
#:     an amino acid. It is a natural component of green tea.");
#:   * a brand-less sentence that itself points back while the lines are in view
#:     RENEWS them -- a pronoun chain keeps its referent ("It keeps you calm. It
#:     tastes like citrus. It is all-natural.");
#:   * the article TITLE stays in view for the first sentence of every later field
#:     (the summary in a listing, the body on the page), whatever sits between.
RAIL2_CARRY_REACH = 2


@dataclass(frozen=True)
class Rail2Carry:
    """The cross-sentence state run_preflight threads through rail 2 (immutable)."""

    lines: frozenset[str] = frozenset()   # lines the last naming sentence left in view
    age: int = 0                          # sentences read since then
    crossed: bool = False                 # an intervening sentence opened a new block
    title: frozenset[str] = frozenset()   # the title field's lines (anchor)
    field_start: bool = False             # the next sentence opens a non-title field

    def visible(self) -> frozenset[str]:
        """The lines a back-reference in the NEXT sentence may point at."""
        seen = self.lines if (self.lines and not self.crossed
                              and self.age + 1 <= RAIL2_CARRY_REACH) else frozenset()
        if self.field_start:
            seen = seen | self.title
        return frozenset(seen)

    def enter_field(self, name: str) -> "Rail2Carry":
        return Rail2Carry(self.lines, self.age, self.crossed, self.title, name != "title")

    def leave_field(self, name: str, text: str) -> "Rail2Carry":
        title = frozenset(brand_lines_in(text or "")) if name == "title" else self.title
        return Rail2Carry(self.lines, self.age, self.crossed, title, False)

    def after(self, sentence: str, seen: frozenset[str], lines_after: frozenset[str],
              opens_block: bool) -> "Rail2Carry":
        """The state once `sentence` is read. `seen` is what visible() returned for
        it; `lines_after` is rail2_context_after(sentence, seen)."""
        if brand_lines_in(sentence or ""):
            return Rail2Carry(frozenset(lines_after), 0, False, self.title, False)
        if seen and any(_rail2_backref_flags(sentence)):
            return Rail2Carry(seen, 0, False, self.title, False)   # a pronoun chain renews
        return Rail2Carry(self.lines, self.age + 1, self.crossed or opens_block, self.title, False)


def rail2_attribution_hit(sentence: str, *, context_lines: frozenset[str] = frozenset()
                          ) -> tuple[str, str] | None:
    """The SHIPPING rail-2 test since R14-3 (see the block comment above): trips when
    a clean token is predicated of Energy/Mood -- i.e. it is neither POSITIVELY
    Pure-attached, nor an environmental object of an environmental action, nor
    inside one of the two ruled exact phrases (redacted BEFORE clause segmentation,
    never in a sentence that names Mood). Gated by rail2_harness against the frozen
    rail2_legacy_hit baseline.

    A clean token also counts when it is one part of a hyphen/slash compound
    ("clean-energy", "cleaner-fuel") or a verb form ("cleans up your afternoon"):
    both passed the legacy rail too, and a narrowing that ships must not keep a
    hole the tokenizer happened to leave open.

    PURE ATTACHMENT IS POSITIVE, NEVER INFERRED (D-051 r143-claims-1/3). A clean
    word in a sentence that names Energy/Mood clears only when its clause names
    Pure and nothing else, AND Pure is attached in one of two shapes:
      P1  Pure is the clause SUBJECT with a predicate verb ("F3 Energy carries the
          full stack; F3 Pure is the clean-sweetened version.");
      P2  the 8/26 locative disjunct ("... or the clean-sweetened version in F3
          Pure.", and its mirror).
    It also clears only while NO clause in the sentence holds a relation token
    (_has_relation). The first cut let a clause's OWN brand win outright, so any
    clause that merely named Pure cleared, even when Pure was only the standard of
    comparison or a possessor. "F3 Energy, as clean as F3 Pure, hits hard.", "F3
    Mood, the clean companion to F3 Pure, ..." and "As clean as F3 Pure, F3 Energy
    delivers all day." all tripped the legacy rail and PASSED the shipping rail.

    INHERITANCE IS FAIL-CLOSED (D-051 EF-7, tightened by r143). A BRAND-LESS clause
    with a clean token, in a sentence that names Energy/Mood, always trips. The EF-7
    union already made that true whenever Energy/Mood was named earlier. A fronted
    brand-less modifier ("Like F3 Pure: clean-sweetened, F3 Energy delivers.")
    showed that a Pure-only union is not attachment either. The 9/1 shape therefore
    still trips and sits in rail2_harness.UNDECIDED; write it as two sentences.

    A bare coordinated brand after a split word ("... from F3 Pure or F3 Energy") is
    folded into the clause it coordinates with, and its lines JOIN that clause's
    lines (the union), never replace them. The first cut let the fold REPLACE the
    host clause's inheritance, so "F3 Mood: clean, and F3 Pure too." cleared: the
    brand-less host picked up only Pure (r143-claims-3).

    CROSS-SENTENCE REFERENCE (r143-claims-5). `context_lines` are the lines an
    earlier sentence left in view (run_preflight's decaying Rail2Carry, fed by
    rail2_context_after). They apply only when a clause here carries a
    back-reference (_rail2_backref_flags: never an expletive or idiomatic pronoun,
    never a Pure-subject sentence's own possessive, and in a clause that names
    Energy/Mood only a pronoun coordinated with the brand) -- "F3 Mood is our
    evening can. It is all-natural.", "... Like F3 Energy, it runs on a cleaner
    fuel source.". Then they:
      * count for the Energy/Mood gate;
      * widen the exemption scope when a back-referring clause holds a ruled
        phrase, precedes it, or relates to it (_phrase_scope_widens), so the
        phrase is never cleared for Mood through a pronoun;
      * make that clause's relation veto strict ("..., and it is too.").
    With no back-reference, or an empty context, the result is exactly the
    single-sentence result, so the context can only ADD trips.
    """
    sent = sentence or ""
    lines = brand_lines_in(sent)
    ctx = frozenset(context_lines or ())
    pron: list[bool] | None = None
    ref: frozenset[str] = frozenset()
    if ctx:
        pron = _rail2_backref_flags(sent)
        if any(pron):
            ref = ctx
    if not ((lines | ref) & _RAIL2_EM):
        return None
    scope = set(lines)
    if ref & _RAIL2_EM and _phrase_scope_widens(_clause_split(sent), pron or ()):
        scope |= ref
    scan_sent = _redact_phrase_exemptions(sent, scope)
    scan_sent = _redact(scan_sent, _NATURAL_OCCURRENCE_RES + (_NATURAL_OCCURRENCE_HYPHEN_RE,))
    scan_sent = _redact(scan_sent, _CLEAN_ENVIRONMENT_RES)
    scan_sent = _redact_env_events(scan_sent)
    if pron is not None and len(pron) != len(_clause_split(scan_sent)):
        pron = [True] * len(_clause_split(scan_sent))   # cannot align: every clause refers back
    clauses = _rail2_clauses(scan_sent, pron if ref else None)
    n = len(clauses)
    rows = []
    related = False
    ref_em = bool(ref & _RAIL2_EM)
    for c in clauses:
        text = " ".join([c.host] + c.bare) if c.bare else c.host
        host_own = brand_lines_in(c.host)
        own = host_own | (brand_lines_in(" ".join(c.bare)) if c.bare else set())
        if _has_relation(_words(text), weak=not (own or (c.pron and ref_em))):
            related = True
        rows.append((c, text, host_own, own))
    for idx, (c, text, host_own, own) in enumerate(rows):
        hit = _attribution_clean_hits(text)
        if not hit:
            continue
        if host_own == own == {"PURE"} and not related:
            host_words = _words(c.host)
            other = rows[idx - 1][1] if idx == n - 1 and idx > 0 else (rows[1][1] if n > 1 else "")
            if (_pure_is_subject(host_words)
                    or _pure_locative_disjunct(host_words, idx, n, c.prev, c.next, _words(other))):
                continue  # the clean word is positively Pure's
        return sorted(hit)[0], "/".join(sorted((lines | ref) & _RAIL2_EM))
    return None

# rail 3 -- Mood is never a sleep aid. Cleared framing is "composure, not sedation",
# so the cleared/negated forms are redacted before the scan (otherwise the
# checklist's OWN approved phrase would trip its own rail).
_SLEEP_RES = (
    re.compile(r"\bsleep\s{1,3}aid\b", re.IGNORECASE),
    re.compile(r"\bsleeping\s{1,3}aid\b", re.IGNORECASE),
    re.compile(r"\bdrows(?:y|iness)\b", re.IGNORECASE),
    re.compile(r"\bsedat(?:e|ed|ing|ion|ive)\b", re.IGNORECASE),
    re.compile(r"\bknock\s{1,3}you\s{1,3}out\b", re.IGNORECASE),
    re.compile(r"\bhelps?\s{1,3}you\s{1,3}sleep\b", re.IGNORECASE),
    re.compile(r"\bfall\s{1,3}asleep\b", re.IGNORECASE),
)
_SLEEP_CLEARED_RES = (
    re.compile(r"\bnot\s{1,3}a\s{1,3}sleep\s{1,3}aid\b", re.IGNORECASE),
    re.compile(r"\bnever\s{1,3}a\s{1,3}sleep\s{1,3}aid\b", re.IGNORECASE),
    re.compile(r"\bis\s{0,3}n[o']t\s{1,3}a\s{1,3}sleep\s{1,3}aid\b", re.IGNORECASE),
    re.compile(r"\b(?:not|no|without)\s{1,3}sedat(?:e|ed|ing|ion|ive)\b", re.IGNORECASE),
    re.compile(r"\bcomposure,?\s{1,3}not\s{1,3}sedation\b", re.IGNORECASE),
    re.compile(r"\bdoes\s{0,3}n[o']t\s{1,3}make\s{1,3}you\s{1,3}drowsy\b", re.IGNORECASE),
    re.compile(r"\bnot\s{1,3}drows(?:y|iness)\b", re.IGNORECASE),
)

# rail 4 -- NSF Certified for Sport is Energy-only, in FAQ phrasing.
_NSF_RE = re.compile(r"\bNSF\b")

# rail 1 -- no health/medical/disease claims.
_NAMED_CONDITIONS = (
    re.compile(r"\banxiety\b", re.IGNORECASE),
    re.compile(r"\bADHD\b"),
    re.compile(r"\binsomnia\b", re.IGNORECASE),
    re.compile(r"\bdepression\b", re.IGNORECASE),
    re.compile(r"\bmental\s{1,3}illness\b", re.IGNORECASE),
    re.compile(r"\bmedical\s{1,3}condition\b", re.IGNORECASE),
)
_CLAIM_VERB_RE = re.compile(
    r"\b(?:treats?|treating|cures?|curing|prevents?|preventing|heals?|healing|"
    r"manages?|managing|diagnos(?:e|es|ing)|remed(?:y|ies)|therapeutic)\b",
    re.IGNORECASE,
)
# A claim verb is now SUFFICIENT on its own. The first cut required a health noun
# from a category list in the same sentence, which meant every claim naming a
# SPECIFIC disease passed: "F3 Energy cures diabetes", "prevents migraines",
# "treats dementia", "cures cancer" -- all executed and confirmed passing. No
# real disease claim contains the word "condition", so the gate was keyed on
# exactly the vocabulary a violation never uses.
#
# The narrow allowlist is what keeps ordinary copy usable; it covers the only
# non-medical sense of these verbs that plausibly appears in beverage marketing.
_CLAIM_VERB_CLEARED_RES = (
    re.compile(r"\btreat\s{1,3}your(?:self|selves)?\b", re.IGNORECASE),
    re.compile(r"\ba\s{1,3}treat\b", re.IGNORECASE),
)
# Physiology and disease objects that have no legitimate place in F3 blog copy.
# Presence alone trips: these need no verb to be a claim, and "boosts immunity" /
# "lowers blood pressure" use verbs no sane verb list would include.
_DISEASE_OBJECT_RE = re.compile(
    r"\b(?:diabetes|diabetic|cancer|tumou?rs?|dementia|alzheimer'?s?|migraines?|"
    r"strokes?|heart\s{1,3}attacks?|blood\s{1,3}pressure|cholesterol|"
    r"blood\s{1,3}sugar|inflammation|immunity|immune\s{1,3}system|arthritis|"
    r"hangovers?|IBS|asthma|epilep(?:sy|tic)|thyroid|adrenal\s{1,3}fatigue)\b",
    re.IGNORECASE,
)
_MEDICAL_CLEARED_RES = (
    re.compile(
        r"\b(?:not|never|no)\s{1,3}(?:intended\s{1,3}to\s{1,3})?"
        r"(?:treat|cure|prevent|heal|diagnose|manage)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdoes\s{0,3}n[o']t\s{1,3}(?:treat|cure|prevent|heal|diagnose|manage)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bis\s{0,3}n[o']t\s{1,3}(?:a\s{1,3})?(?:treatment|cure|remedy)\b",
        re.IGNORECASE,
    ),
)

# rail 8 -- press-embargo doctrine.
_EMBARGO_RES = (
    re.compile(r"\bvaluation\b", re.IGNORECASE),
    re.compile(r"\b(?:pre|post)-money\b", re.IGNORECASE),
    re.compile(r"\bcap\s{1,3}table\b", re.IGNORECASE),
    re.compile(r"\bSAFE\s{1,3}note\b"),
    re.compile(r"\bSeries\s{1,3}[A-D]\b"),
    re.compile(r"\bequity\s{1,3}(?:stake|split)\b", re.IGNORECASE),
    re.compile(r"\brais(?:e|ed|ing)\b[^.\n]{0,30}\$", re.IGNORECASE),
    re.compile(r"\bterm\s{1,3}sheet\b", re.IGNORECASE),
    # "closed a $4 million round led by two family offices" passed BOTH rails:
    # rail 8 had no word for a funding round, and rail 5's currency scan was
    # exempted by the word "revenue" one sentence earlier. Funding mechanics are
    # rail 8's job and must not depend on the currency symbol.
    re.compile(r"\b(?:funding|investment|seed|bridge|priced)\s{1,3}round\b",
               re.IGNORECASE),
    re.compile(r"\bround\s{1,3}(?:led\s{1,3}by|of\s{1,3}funding)\b", re.IGNORECASE),
    re.compile(r"\b(?:closed|raised)\b[^.\n]{0,40}\bround\b", re.IGNORECASE),
    re.compile(r"\b(?:venture|growth)\s{1,3}capital\b", re.IGNORECASE),
    re.compile(r"\b(?:investors?|family\s{1,3}offices?)\b[^.\n]{0,30}\$",
               re.IGNORECASE),
)

# rail 10 -- founded 2023, never 2022.
_FOUNDING_WORD_RE = re.compile(
    r"\b(?:founded|founding|since|established|est\.?|started|launched|began|"
    r"inception|incorporated)\b",
    re.IGNORECASE,
)
_YEAR_2022_RE = re.compile(r"\b2022\b")

# rail 11 -- no vegan / dairy-free / gluten-free / organic PRODUCT claims.
# "organic cane sugar" is cleared FAQ ingredient language, so it is redacted first.
_PRODUCT_CLAIM_RES = (
    re.compile(r"\bvegan\b", re.IGNORECASE),
    re.compile(r"\bdairy[\s-]{0,3}free\b", re.IGNORECASE),
    re.compile(r"\bgluten[\s-]{0,3}free\b", re.IGNORECASE),
    re.compile(r"\bnon[\s-]{0,3}GMO\b", re.IGNORECASE),
    re.compile(r"\borganic\b", re.IGNORECASE),
)
_PRODUCT_CLAIM_CLEARED_RES = (
    re.compile(r"\borganic\s{1,3}cane\s{1,3}sugar\b", re.IGNORECASE),
)

# rail 13 -- beverage framing, never a dietary supplement (on-site).
_SUPPLEMENT_RES = (
    re.compile(r"\bdietary\s{1,3}supplement\b", re.IGNORECASE),
    re.compile(r"\bnutritional\s{1,3}supplement\b", re.IGNORECASE),
    re.compile(r"\bsupplement\s{1,3}facts\b", re.IGNORECASE),
)
_SUPPLEMENT_CLEARED_RES = (
    re.compile(
        r"\b(?:not|never)\s{1,3}a\s{1,3}(?:dietary|nutritional)\s{1,3}supplement\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bis\s{0,3}n[o']t\s{1,3}a\s{1,3}(?:dietary|nutritional)\s{1,3}supplement\b",
        re.IGNORECASE,
    ),
)

# Drafting-quality rail (not a checklist number): unfilled placeholders. A draft
# that reaches staging with "[TBD]" in it is a broken draft, not a claims problem,
# but it must never reach a publish card either.
_PLACEHOLDER_RES = (
    re.compile(
        r"\[(?:\s{0,3})(?:TBD|TODO|FIXME|INSERT|PLACEHOLDER|X{2,6}|\.\.\.|link|url|"
        r"name|date|number|source|quote|stat)\b[^\]]{0,80}\]",
        re.IGNORECASE,
    ),
    re.compile(r"\{\{[^}]{0,120}\}\}"),
    re.compile(r"\b(?:TBD|TODO|FIXME)\b"),
    re.compile(r"\bLorem\s{1,3}ipsum\b", re.IGNORECASE),
)

_RAIL_NAMES = {
    "R1": "no health/medical/disease claims",
    "R2": "clean/natural language is Pure-only",
    "R3": "Mood is never a sleep aid",
    "R4": "NSF Certified for Sport is Energy-only",
    "R5": "no prices",
    "R6": "no em-dashes",
    "R8": "press-embargo doctrine",
    "R10": "founded 2023, never 2022",
    "R11": "no vegan/dairy-free/gluten-free/organic product claims",
    "R13": "beverage framing, never a dietary supplement",
    "PLACEHOLDER": "no unfilled placeholders",
}

RAILS_CHECKED: tuple[str, ...] = tuple(_RAIL_NAMES)


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


def _trip(rail_id: str, field_name: str, excerpt: str) -> Trip:
    return Trip(rail_id, _RAIL_NAMES[rail_id], field_name, _excerpt(excerpt))


def _scan_raw(rail_id: str, patterns, raw: str, field_name: str) -> list[Trip]:
    out: list[Trip] = []
    for pat in patterns:
        m = pat.search(raw or "")
        if m:
            lo = max(0, m.start() - 60)
            out.append(_trip(rail_id, field_name,
                             "...%s..." % (raw[lo:m.end() + 60])))
            break  # one trip per rail per field is enough to stop the run
    return out


def _currency_trips(field_name: str, text: str) -> list[Trip]:
    """Currency amounts, judged in context (see the rail-5 note above).

    The exemption is SENTENCE-scoped, not window-scoped. A +/-200 char window was
    trivially reachable in this lane's own article class: "F3 Energy grossed
    record revenue last quarter. The 12-pack is $39.99 and ships free." exempted
    a real price because a revenue word sat one sentence away. Revenue and price
    in the SAME sentence is the shape rail 8 actually permits.

    Sentence boundaries are unavailable inside JSON-LD, so the fallback there is
    the whole fragment -- structured data has no prose to contextualise anyway,
    and a price key in it is caught unconditionally by _PRICE_UNCONDITIONAL_RES.
    """
    raw = text or ""
    if not raw:
        return []
    units = sentences(raw) or [raw]
    for unit in units:
        for pat in _CURRENCY_RES:
            m = pat.search(unit)
            if not m:
                continue
            if _REVENUE_CONTEXT_RE.search(unit):
                continue  # attributed revenue in this sentence -- rail 8's lane
            return [_trip("R5", field_name, unit)]
    return []


def rail_fields(*, title: str, summary: str, body_html: str) -> tuple[tuple[str, str], ...]:
    """The (field_name, text) views every SEMANTIC rail scans -- one definition,
    shared by run_preflight and rail2_harness's frozen legacy composition so the
    baseline can never drift from the fields the shipping preflight reads."""
    body_html = body_html or ""
    return (
        ("title", unescaped(title or "")),
        ("summary", html_to_text(summary or "")),
        ("body", html_to_text(body_html)),
        ("body(structured data / alt text)", hidden_text(body_html)),
    )


def run_preflight(
    *,
    title: str,
    summary: str,
    body_html: str,
    lane: str = "learn",
) -> PreflightResult:
    """Run every mechanically checkable claims rail. FAIL-CLOSED.

    `title` and `summary` are plain text; `body_html` is the HTML that would be
    sent to Shopify. Rails that concern any published byte (em-dash, price,
    placeholder) scan the RAW html too, because JSON-LD and alt text are
    outward-facing surfaces even though a reader does not see them as prose.
    """
    trips: list[Trip] = []
    title = title or ""
    summary = summary or ""
    body_html = body_html or ""

    # THREE views of the same content, because the first cut gave different rails
    # different views and each blind spot was a live false negative:
    #
    #   prose   reader-visible text, entities resolved, soft wraps collapsed
    #   hidden  JSON-LD / script bodies + attribute values (alt text). Ships, is
    #           machine-read, and was invisible to 8 of 11 rails -- while the
    #           drafting prompt explicitly asks the model to emit JSON-LD.
    #   bytes   entities RESOLVED. "&mdash;" and "&#36;39.99" render as an
    #           em-dash and a price; scanning the pre-unescape string made rails
    #           5 and 6 completely blind to the entity spelling.
    #
    # Every rail now scans all three. There is no rail-specific view any more.
    fields = rail_fields(title=title, summary=summary, body_html=body_html)
    raw_fields = (
        ("title", unescaped(title)),
        ("summary", unescaped(summary)),
        ("body(raw)", unescaped(body_html)),
    )

    # --- rail 6: em-dashes, anywhere in anything that ships ---
    for name, text in raw_fields:
        trips += _scan_raw("R6", (_EM_DASH_RE, _EN_AS_EM_RE), text, name)

    # --- rail 5: prices ---
    for name, text in raw_fields:
        trips += _scan_raw("R5", _PRICE_UNCONDITIONAL_RES, text, name)
        trips += _currency_trips(name, text)

    # --- placeholders ---
    for name, text in raw_fields:
        trips += _scan_raw("PLACEHOLDER", _PLACEHOLDER_RES, text, name)

    # --- rail 8: embargo ---
    for name, text in fields:
        trips += _scan_raw("R8", _EMBARGO_RES, text, name)

    # --- rail 13: supplement framing (cleared negations redacted first) ---
    for name, text in fields:
        trips += _scan_raw(
            "R13", _SUPPLEMENT_RES, _redact(text, _SUPPLEMENT_CLEARED_RES), name)

    # --- rail 11: product claims ("organic cane sugar" is cleared) ---
    for name, text in fields:
        trips += _scan_raw(
            "R11", _PRODUCT_CLAIM_RES,
            _redact(text, _PRODUCT_CLAIM_CLEARED_RES), name)

    # --- rail 1: medical claims (negations redacted first) ---
    for name, text in fields:
        clean = _redact(text, _MEDICAL_CLEARED_RES)
        trips += _scan_raw("R1", _NAMED_CONDITIONS, clean, name)
        # Disease/physiology objects trip on presence alone: they need no verb to
        # be a claim, and they have no legitimate place in this copy.
        trips += _scan_raw("R1", (_DISEASE_OBJECT_RE,), clean, name)
        # A claim verb is sufficient on its own, minus the one non-medical sense
        # ("treat yourself"). Requiring a health noun alongside it let every
        # specifically-named disease claim through.
        for sent in sentences(_redact(clean, _CLAIM_VERB_CLEARED_RES)):
            if _CLAIM_VERB_RE.search(sent):
                trips.append(_trip("R1", name, sent))
                break

    # --- rail 10: 2022 as a founding year (same sentence as a founding word) ---
    for name, text in fields:
        for sent in sentences(text):
            if _YEAR_2022_RE.search(sent) and _FOUNDING_WORD_RE.search(sent):
                trips.append(_trip("R10", name, sent))
                break

    # --- rail 2: clean/natural PREDICATED OF the Energy or Mood LINE ---
    # ATTRIBUTION scope since R14-3 (ruling ESC-1 (A) / D-329, 2026-09-19; the
    # rail2_harness gate passes). The pre-R14-3 same-sentence scan survives only
    # as rail2_legacy_hit, the frozen baseline the harness measures against.
    # D-051 r143-claims-5: the lines a sentence names are carried forward, so a
    # pronoun cannot launder a clean word onto Energy/Mood across a sentence
    # boundary. The carried check only ever ADDS a trip to the plain one. D-051
    # round 2 (F3-R1): the carry DECAYS (Rail2Carry: the next sentence, or the one
    # after a single brand-less sentence in the same block; the title stays in view
    # for each later field's first sentence), and expletive / generic pronouns never
    # carry ("it's worth noting that", "they say").
    carry = Rail2Carry()
    for name, text in fields:
        carry = carry.enter_field(name)
        for sent, opens_block in rail2_sentences(text):
            carried = carry.visible()
            hit = rail2_attribution_hit(sent) or (
                rail2_attribution_hit(sent, context_lines=carried) if carried else None)
            if hit:
                trips.append(_trip("R2", name, "%r near %s: %s" % (hit[0], hit[1], sent)))
                break
            carry = carry.after(sent, carried, rail2_context_after(sent, carried), opens_block)
        carry = carry.leave_field(name, text)

    # --- rail 3: sleep-aid language in a doc about a product ---
    # Gated on a PRODUCT reference rather than on the literal token "Mood": an
    # article whose only reference was "our caffeine-free calm line" was
    # completely unguarded, which is precisely the article most likely to drift
    # toward sleep language. Still not unconditional -- a Learn post may say
    # truthfully that caffeine late in the day makes it harder to fall asleep,
    # and blocking that would be wrong.
    product_doc = False
    for _, text in fields:
        for sent in sentences(text):
            if product_referenced(sent):
                product_doc = True
                break
        if product_doc:
            break
    if product_doc:
        for name, text in fields:
            for sent in sentences(_redact(text, _SLEEP_CLEARED_RES)):
                if not product_referenced(sent):
                    continue
                hit = next((p for p in _SLEEP_RES if p.search(sent)), None)
                if hit:
                    trips.append(_trip("R3", name, sent))
                    break
            else:
                continue
            break

    # --- rail 4: NSF only in a sentence naming Energy and NOT Pure/Mood ---
    for name, text in fields:
        for sent in sentences(text):
            if not _NSF_RE.search(sent):
                continue
            lines = brand_lines_in(sent)
            if "ENERGY" not in lines or (lines & {"PURE", "MOOD"}):
                trips.append(_trip("R4", name, sent))
                break

    return PreflightResult(
        passed=not trips, trips=trips, rails_checked=RAILS_CHECKED,
    )


# ---------------------------------------------------------------------------
# Checklist drift
# ---------------------------------------------------------------------------


def fingerprint_checklist(checklist_text: str) -> str:
    """Stable sha256 of the checklist file, whitespace-normalised.

    Normalised so a CRLF flip or a trailing-newline change (which Drive sync and
    every editor do casually) does not read as a rule change and block a staging
    run for nothing. A real edit to any rule still changes the digest.
    """
    norm = "\n".join(
        line.strip() for line in (checklist_text or "").replace("\r\n", "\n").split("\n")
        if line.strip()
    )
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
