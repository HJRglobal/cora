"""Two-tier staleness labelling for known-answers (session #11 S3, cq-b0e5bc37c41b).

THE C10b RULING, implemented as ruled:
  * CASH class (cash position, balances, AR/AP, inventory counts): label the
    as-of date, WARN at 7 days, STOP SERVING at 30 days.
  * PRICE class (prices, tiers, MSRP, contract values): WARN at 90 days,
    NEVER auto-expire.

READ-PATH ONLY. Nothing here mutates the store. The transform is applied to the
text on its way into context, and the file on Drive is untouched.

SCOPE OF THE WITHHOLD, STATED HONESTLY (D-051 review). This helper withholds the
figure wherever it is CALLED -- the always-injected known-answers block and the
MCP read surface. It does NOT withhold the same figure from KB RETRIEVAL: the
static_md chunks of the very same file are still returned by _try_kb_retrieve,
carrying no as-of date and no staleness label. So a withheld cash figure can
still reach a reply by the other route. Closing that needs either a
known-answers exclusion in retrieval or the same labelling applied to static_md
chunks; it is a NAMED FOLLOW-ON, not a property of this module. Do not read the
withhold as an end-to-end guarantee. That is deliberate: the
ruling asks for withholding, not deletion, and a destructive sweep already
exists for a DIFFERENT axis (knowledge_check's D-089 7-day Tier-1 expiry, which
is marker-scoped and only ever touches blocks that module wrote).

WHY THIS IS A SHARED MODULE AND NOT A CONTEXT_LOADER PRIVATE. The store has
THREE readers: context_loader (the Slack reply path), mcp_server.known_answers
(hands raw file text to Code/Cowork sessions) and info_intake.known_answer_facts
(duplicate/supersede classification). A withhold living in only the first means a
30-day-stale cash figure still reaches a Code session verbatim and still counts
as canon when deciding whether a new contribution supersedes it.

WHAT THIS DOES NOT FIX -- STATED PLAINLY. The seed and the roadmap both name "the
superseded $36.99 note in f3e.md" as the target. Age cannot separate it: the
WRONG $36.99 note and the CORRECT $39.99 note are stamped the SAME DAY
(2026-08-07). Under the ruled price tier, warn fires 2026-11-05 and withhold
never fires, so the wrong figure keeps serving. That defect is
supersession-by-contradiction, not staleness, and its mechanism is
knowledge_check.detect_collision. Building the ruled tiers does NOT close that
target; saying so is part of the deliverable.
"""
from __future__ import annotations

import re
from datetime import date

# Section the entries live under. NOTE: the SECTION HEADER LINE itself
# (context_loader.KNOWN_ANSWERS_SECTION_HEADER) is pinned by
# claude_client._STATIC_SECTION_HEADERS, which uses that literal to define the
# never-trim protected tail. Nothing here may alter that line -- all labelling
# goes INSIDE the section body.
_KNOWN_FACTS_HEADER = "## Known facts"

# An entry begins with a machine date stamp: **[YYYY-MM-DD] ...**
_ENTRY_RE = re.compile(r"^\*\*\[(\d{4})-(\d{2})-(\d{2})\]")
# Any other section/sub-section boundary ends the current entry.
_BOUNDARY_RE = re.compile(r"^(?:#{2,3} |\*\*\[)")

WARN_DAYS_CASH = 7
WITHHOLD_DAYS_CASH = 30
WARN_DAYS_PRICE = 90

# Narrow, high-confidence CASH matching. Withholding is the only DESTRUCTIVE-to-
# the-reader action in this module -- a false positive silently deletes a fact
# from every reply in that entity -- so this class is kept deliberately tight and
# everything unmatched falls through to "no action".
_CASH_RE = re.compile(
    r"\b(?:cash\s+(?:position|balance|on\s+hand)|bank\s+balance|book\s+balance"
    r"|ending\s+cash|beginning\s+cash|liquidity"
    r"|a/?[rp]\s+aging|accounts\s+(?:receivable|payable)"
    r"|on[-\s]hand\s+(?:count|units|inventory)|inventory\s+(?:count|level|on\s+hand)"
    r"|units\s+in\s+stock|stock\s+level)\b",
    re.IGNORECASE,
)
# PRICE is warn-only, so a looser match is safe here.
_PRICE_RE = re.compile(
    r"\b(?:price|pricing|msrp|wholesale|retail\s+price|rate\s+card|tier"
    r"|contract\s+value|list\s+price)\b",
    re.IGNORECASE,
)

CASH = "cash"
PRICE = "price"


def classify(text: str) -> str | None:
    """CASH / PRICE / None. CASH wins when both match: it is the tier with the
    stricter action, and a figure that is both a price and a cash balance should
    get the stricter treatment."""
    if not text:
        return None
    if _CASH_RE.search(text):
        return CASH
    if _PRICE_RE.search(text):
        return PRICE
    return None


def _age_days(stamp: date, today: date) -> int:
    return (today - stamp).days


def _label_for(kind: str, stamp: date, age: int) -> str | None:
    iso = stamp.isoformat()
    if kind == CASH:
        if age >= WITHHOLD_DAYS_CASH:
            return (
                f"_[WITHHELD -- this figure is from {iso}, {age} days old. Cash and "
                f"balance figures stop being served after {WITHHOLD_DAYS_CASH} days. "
                f"Ask for a fresh pull rather than quoting this.]_"
            )
        if age >= WARN_DAYS_CASH:
            return (
                f"_[AS OF {iso} -- {age} days old. Verify before relying on it; "
                f"cash figures move daily.]_"
            )
        return f"_[as of {iso}]_"
    if kind == PRICE and age >= WARN_DAYS_PRICE:
        return (
            f"_[AS OF {iso} -- {age} days old. Prices do not expire automatically; "
            f"confirm it is still current before quoting.]_"
        )
    return None


def _split_entries(lines: list[str]) -> list[tuple[int, int, date | None]]:
    """(start, end, stamp) per block inside the section.

    A block with no date stamp gets stamp=None and is NEVER labelled or withheld:
    9 of 43 live items are hand-written `###` sub-sections, and they include the
    authoritative F3E price ladder, which carries no machine stamp at all.
    """
    spans: list[tuple[int, int, date | None]] = []
    i = 0
    while i < len(lines):
        m = _ENTRY_RE.match(lines[i])
        start = i
        stamp = None
        if m:
            try:
                stamp = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                stamp = None
        j = i + 1
        while j < len(lines) and not _BOUNDARY_RE.match(lines[j]):
            j += 1
        spans.append((start, j, stamp))
        i = j
    return spans


def apply_staleness(content: str, today: date | None = None) -> str:
    """Label / withhold inside the `## Known facts` section. Pure.

    Returns the input UNCHANGED when there is no such section, so a store that
    does not use the convention is never mangled.
    """
    if not content or _KNOWN_FACTS_HEADER not in content:
        return content
    today = today or date.today()
    # D-051 review: splitlines() does NOT round-trip. It drops the trailing
    # newline AND silently eats \r, so on this store -- which lives on a WINDOWS
    # DRIVE MOUNT and is written CRLF by the bot's own writer -- every read would
    # have rewritten the whole document to LF. split("\n") round-trips exactly:
    # \r stays attached to each line (harmless, all patterns anchor at ^ or match
    # before it) and the trailing empty element preserves the final newline.
    lines = content.split("\n")

    # locate the section body
    try:
        head = next(i for i, ln in enumerate(lines) if ln.strip() == _KNOWN_FACTS_HEADER)
    except StopIteration:
        return content
    tail = len(lines)
    for i in range(head + 1, len(lines)):
        if lines[i].startswith("## ") and lines[i].strip() != _KNOWN_FACTS_HEADER:
            tail = i
            break

    body = lines[head + 1:tail]
    out: list[str] = []
    for start, end, stamp in _split_entries(body):
        block = body[start:end]
        if stamp is None:
            out.extend(block)
            continue
        kind = classify("\n".join(block))
        if kind is None:
            out.extend(block)
            continue
        age = _age_days(stamp, today)
        label = _label_for(kind, stamp, age)
        if kind == CASH and age >= WITHHOLD_DAYS_CASH:
            # Keep the HEADER so the reader can see a fact exists and is being
            # withheld -- silently dropping it would look like the fact never
            # existed, which is its own kind of false state.
            out.append(block[0])
            out.append(label or "")
            out.append("")
            continue
        out.extend(block)
        if label:
            # insert after the header line, before the body
            insert_at = len(out) - len(block) + 1
            out.insert(insert_at, label)

    rebuilt = "\n".join(lines[:head + 1] + out + lines[tail:])
    # Belt: if nothing in the section actually changed, hand back the ORIGINAL
    # object so "returns the input unchanged" is literally true rather than
    # merely equal-looking.
    if rebuilt == content:
        return content
    return rebuilt
