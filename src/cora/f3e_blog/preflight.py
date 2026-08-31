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

# Bumped when the rail SET changes (not on wording tweaks). Recorded in the
# pipeline log so a past run's report can be read against the rails it ran.
CHECKLIST_MIRROR_VERSION = "1.0"

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
    """
    raw = [s.strip() for s in _SENT_SPLIT_RE.split(text or "") if s.strip()]
    out: list[str] = []
    for part in raw:
        if out and _ABBREV_TAIL_RE.search(out[-1]):
            out[-1] = out[-1] + " " + part
        else:
            out.append(part)
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
# cq-85b35413b020 (session #11 S7): "F3 Energy works with the Clean Label Project."
# tripped R2 -- the rail lowercases every token and intersects with _CLEAN_TOKENS,
# so a proper NOUN containing "Clean" is indistinguishable from the adjective. A
# capitalised Clean/Cleaner heading a Title-Case phrase is a NAME, not a claim
# about the product.
#
# Implemented as a REDACTION so it composes with the rail's existing exemption
# mechanism (_NATURAL_OCCURRENCE_RES) instead of adding a second code path, and so
# the rest of the sentence is still scanned: "F3 Energy works with the Clean Label
# Project and is all-natural" still trips on "all-natural".
#
# Bounded repetition throughout -- no nested unbounded quantifier.
_CLEAN_PROPER_NOUN_RES = (
    re.compile(r"\bClean(?:er)?\b(?:\s{1,3}[A-Z][A-Za-z]{1,20}){1,3}"),
)


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
    # cq-85b35413b020 (session #11 S7): the patterns below require the claim verb
    # within 1-3 SPACES of the negation, so a perfectly ordinary disclaimer --
    # "F3 Energy is not about eliminating jitters or preventing a crash" -- left
    # "preventing" uncleared and tripped R1's claim-verb-sufficiency rule (D-238).
    # This widens the negation's SCOPE to the rest of its CLAUSE rather than
    # adding another literal.
    #
    # Bounded on BOTH ends deliberately. The negated class excludes , ; : and
    # dashes as well as sentence terminators, so the scope cannot bridge a clause
    # boundary and clear a REAL claim ("We are not shy -- F3 prevents migraines").
    # No nested quantifier over a delimiter-rich string (the repo has found seven
    # ReDoS bugs); this is one bounded lazy negated class.
    re.compile(
        r"\b(?:not|never|no)\b[^.!?\n,;:\-–—]{0,60}?"
        r"\b(?:treat|cure|prevent|heal|diagnose|manage)\w*",
        re.IGNORECASE,
    ),
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
    prose = html_to_text(body_html)
    hidden = hidden_text(body_html)

    fields = (
        ("title", unescaped(title)),
        ("summary", html_to_text(summary)),
        ("body", prose),
        ("body(structured data / alt text)", hidden),
    )
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

    # --- rail 2: clean/natural in the same sentence as the Energy or Mood LINE ---
    for name, text in fields:
        for sent in sentences(text):
            lines = brand_lines_in(sent)
            if not (lines & {"ENERGY", "MOOD"}):
                continue
            scan_sent = _redact(sent, _NATURAL_OCCURRENCE_RES + _CLEAN_PROPER_NOUN_RES)
            toks = {w.lower().strip("'&/-") for w in _words(scan_sent)}
            hit = toks & _CLEAN_TOKENS
            if hit:
                trips.append(_trip(
                    "R2", name,
                    "%r near %s: %s" % (sorted(hit)[0],
                                        "/".join(sorted(lines & {"ENERGY", "MOOD"})),
                                        sent),
                ))
                break

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
