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

ReDoS discipline (this codebase has found six): every proximity rail here is
implemented with TOKEN INDEX ARITHMETIC, not regex proximity. The regexes that do
exist are literal alternations with bounded classes and no sequential unbounded
quantifiers. `tests/test_f3e_blog_preflight.py` asserts the SHAPE (work must not
grow ~4x per input doubling), not merely a wall-clock threshold.
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
_BLOCK_BREAK_RE = re.compile(
    r"</(?:p|div|li|ul|ol|h[1-6]|blockquote|tr|td|th|section|article)>|<br\s{0,4}/?>",
    re.IGNORECASE,
)
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]{0,400}>.{0,200000}?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]{0,2000}>")
_WS_RE = re.compile(r"[ \t]{2,}")


def html_to_text(html_body: str) -> str:
    """Visible prose only: script/style blocks dropped, block tags -> newlines."""
    if not html_body:
        return ""
    txt = _SCRIPT_STYLE_RE.sub("\n", html_body)
    txt = _BLOCK_BREAK_RE.sub("\n", txt)
    txt = _TAG_RE.sub(" ", txt)
    txt = _html.unescape(txt)
    txt = _WS_RE.sub(" ", txt)
    return txt


_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(text: str) -> list[str]:
    """Sentence-ish units. Splits on terminal punctuation AND newlines."""
    return [s.strip() for s in _SENT_SPLIT_RE.split(text or "") if s.strip()]


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

_CATEGORY_FOLLOWERS = frozenset({
    "drink", "drinks", "beverage", "beverages", "category", "brands", "brand",
    "level", "levels", "boost", "crash", "dip", "slump", "needs", "need",
    "source", "sources", "intake", "expenditure", "market", "aisle", "shelf",
    "products", "product", "industry", "space",
})

_BRAND_F3_RE = re.compile(r"\bF3\s{0,3}(Energy|Pure|Mood)\b", re.IGNORECASE)
_BRAND_BARE_RE = re.compile(r"\b(Energy|Pure|Mood)\b")


def brand_lines_in(sentence: str) -> set[str]:
    """Which F3 product LINES this sentence refers to: {'ENERGY','PURE','MOOD'}."""
    found: set[str] = set()
    for m in _BRAND_F3_RE.finditer(sentence or ""):
        found.add(m.group(1).upper())
    for m in _BRAND_BARE_RE.finditer(sentence or ""):
        line = m.group(1).upper()
        if line == "ENERGY":
            tail = (sentence or "")[m.end():m.end() + 40]
            nxt = _words(tail)
            if nxt and nxt[0].lower() in _CATEGORY_FOLLOWERS:
                continue  # category noun, not the brand line
        found.add(line)
    return found


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
    re.compile(r"\$\s{0,2}\d"),
    re.compile(r"\b\d[\d,.]{0,12}\s{0,3}(?:dollars|USD)\b", re.IGNORECASE),
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
_HEALTH_NOUN_RE = re.compile(
    r"\b(?:conditions?|diseases?|illness(?:es)?|symptoms?|disorders?|diagnosis|"
    r"ailments?|syndrome)\b",
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
    re.compile(r"\braising\s{1,3}\$", re.IGNORECASE),
    re.compile(r"\braise\s{1,3}of\s{1,3}\$", re.IGNORECASE),
    re.compile(r"\bterm\s{1,3}sheet\b", re.IGNORECASE),
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

    A character WINDOW is used rather than a sentence split so the rule applies
    identically to prose and to JSON-LD / alt-text bytes, where there are no
    sentences at all: an `offers.price` in structured data is still a published
    price, and a reader-invisible price is still a price.
    """
    raw = text or ""
    for pat in _CURRENCY_RES:
        for m in pat.finditer(raw):
            lo = max(0, m.start() - _PRICE_CONTEXT_CHARS)
            hi = min(len(raw), m.end() + _PRICE_CONTEXT_CHARS)
            window = raw[lo:hi]
            if _REVENUE_CONTEXT_RE.search(window):
                continue  # attributed revenue, not a price -- rail 8's lane
            return [_trip("R5", field_name, "...%s..." % window[:220])]
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
    visible = html_to_text(body_html)

    fields = (("title", title), ("summary", summary), ("body", visible))
    # Raw-byte fields: everything that ships, including structured data and alt text.
    raw_fields = (("title", title), ("summary", summary), ("body(raw)", body_html))

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
        # Generic claim verbs only count with a health noun in the same sentence,
        # so "treat yourself to a cold one" is not a disease claim.
        for sent in sentences(clean):
            if _CLAIM_VERB_RE.search(sent) and _HEALTH_NOUN_RE.search(sent):
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
            scan_sent = _redact(sent, _NATURAL_OCCURRENCE_RES)
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

    # --- rail 3: sleep-aid language anywhere in a doc that mentions Mood ---
    doc_lines: set[str] = set()
    for _, text in fields:
        for sent in sentences(text):
            doc_lines |= brand_lines_in(sent)
    if "MOOD" in doc_lines:
        for name, text in fields:
            trips += _scan_raw(
                "R3", _SLEEP_RES, _redact(text, _SLEEP_CLEARED_RES), name)

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
