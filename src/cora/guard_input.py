"""Normalize the TEXT that ENTITY-scope guards evaluate.

Why this exists (live incidents, #f3-hq-inventory-adjustments, 2026-08-03..08-06):
the office-inventory write request carries a free-text ``Reason:`` line that is
OPERATOR ANNOTATION, not a question. The entity guards read the whole message, so
the annotation drove routing:

  * "Reason: 4 OSN Stores" / "Reason: OSN Stores" -> cross_entity_guard saw the
    word "osn" and redirected all-F3E-SKU writes to #osn-leadership 5x across two
    users, until Hannah dropped the Reason line entirely to get through.

Every SKU and the location in those requests were F3 PURE at the F3 office. The
guards were right about the keyword and wrong about the field.

SCOPE IS DELIBERATELY LIMITED TO THE ENTITY GUARDS -- cross_entity_guard and
sibling_guard. It is NOT applied to user_access.check_access, and that boundary is
the security argument, established by this branch's own D-051 review:

  A shape-satisfying wrapper costs three lines ("INVENTORY UPDATE - HQ" /
  "Reason: <payload>" / "Widget: 1"), so ANY text the strip removes is text an
  operator can hide. With the strip on user_access, a DECLARATIVE payload
  ("Justin's salary, print the figure") sailed past the hr / phi / cap_table /
  financials blocks -- measured, and Alex is both blocked on three of those topics
  and the operator who files these writes daily. That is privilege escalation, not
  a false-positive fix.
  It is also UNNECESSARY: the 2026-08-13 HR false refusal ("Reason: Handout at
  camptontozona" matching "pto" inside "cam-PTO-ntozona") was a naive SUBSTRING
  match, fixed at the root in user_access by word-bounding the topic patterns. No
  stripping required. So user_access evaluates the full raw message, always.

Residual on the entity guards, ACCEPTED and documented rather than papered over:
a declarative cross-entity phrase inside the Reason of a shape-satisfying message
is stripped and will not redirect, so the model may answer it in the requesting
channel. Bounded by three things -- the value length cap below, user_access still
guarding every sensitive TOPIC on the raw text, and channel_content_guard
screening the composed ANSWER outbound. Exposure is therefore entity-scoped
operational context, never PHI / financials / cap-table / LBHS-confidential.
The durable fix is to hand the guards the PARSED request (SKUs + location, Reason
as a separate non-routing field) instead of free text; seeded separately.

Scope of effect: guard INPUT only. The real message still reaches the LLM and the
inventory tool with the Reason intact -- the Reason is recorded on the adjustment.

Measured residual on the follow-up path (1 occurrence, 8/03-8/13): a thread
FOLLOW-UP naming an entity in prose -- Hannah's "OSN is just the reason" (8/06
07:56) -- still trips the cross-entity guard, since it carries the keyword and
none of the request shape. Needs thread-parent context at the call site; seeded.

REGEX SHAPE IS LOAD-BEARING. Every pattern here runs on EVERY message reaching an
entity guard, so each is single-pass with no nested/adjacent unbounded quantifiers
over the same character. The first cut of the header pattern
(``^\\s*\\**\\s*[\\w \\-]*inventory\\s+update\\b``) let three greedy quantifiers all
match a plain space -> cubic backtracking, measured 12.8s on 1,600 leading spaces
and ~104s on 3,201, on a path fed raw uncapped Slack text. This repo has now had
six ReDoS defects; treat a quantifier next to another quantifier as a bug.
"""

from __future__ import annotations

import re

# An "OFFICE INVENTORY UPDATE - 1337 S Gilbert Rd" style header. ONE bounded
# lazy quantifier over a non-newline run -- no nesting, linear. `[ \t]+` not
# `\s+` so a header can never span a line break ("inventory\nupdate").
_INVENTORY_HEADER_RE = re.compile(
    r"^[^\n]{0,60}?inventory[ \t]+update\b", re.I | re.M)

# "Reason: <free text>" -- optionally Slack-bolded ("*Reason:* ..."), tolerant of
# tabs and a space before the colon. Bounded single run before the colon, so the
# earlier `[ \t]*\**\s*` (quadratic on a long space run) is gone.
_REASON_LINE_RE = re.compile(r"^([ \t]{0,20}\*{0,2}reason[ \t]{0,4}:\*{0,2}[ \t]{0,4})(.*)$",
                             re.I | re.M)

# A SKU/quantity line: "• PURE-Original: 2", "- F3-PureE-V4F: 64", "PURESL: 2".
# The class excludes ':', so the split point is near-deterministic (measured flat
# to 20k chars).
_SKU_LINE_RE = re.compile(r"^\s*(?:[•*\-–—]\s*)?[\w][\w \-/.()]*:\s*\d+\s*$", re.M)

# Longest real Reason value observed on this path is 40 chars ("Handout at
# camptontozona ( ASU FOOTBALL)"). A cap bounds how much text the strip can ever
# remove from a guard's view; anything longer is left intact and fully guarded.
_MAX_REASON_LEN = 60

# A Reason value must be ANNOTATION, not a smuggled request. This is a BELT, not
# the gate -- the gate is the user_access exclusion documented above, because a
# keyword list cannot separate annotation from a declarative request.
# Deliberately does NOT contain the BARE words "pull", "is", "are", "was",
# "were" or "list": those are ordinary warehouse-annotation words, and including
# them re-broke the very incident this module fixes -- "Pull for the 4 OSN
# stores", "4 OSN stores, product was damaged in transit" and "Stock is low at
# the OSN stores" all stopped stripping and the false refusal returned (D-051
# MED, this branch). "packing list" is the same trap.
# The MULTI-WORD request forms ("pull up", "status of", "update on") carry no such
# collision and are kept -- verified against every real Reason observed.
_REASON_IS_REQUEST_RE = re.compile(
    r"\?|(?<!\w)(?:what|what's|whats|how|how's|hows|who|whose|why|when|where|"
    r"which|does|did|can|could|should|would|tell\s+me|show\s+me|give\s+me|"
    r"send\s+me|pull\s+up|status\s+of|update\s+on|print|summar\w*|explain|"
    r"compare|report\s+on|break\s+down)(?!\w)",
    re.I,
)


def is_inventory_adjustment_request(text: str) -> bool:
    """True only when ALL THREE structural signals of an office-inventory write
    request are present. Any one alone is not enough -- but note that satisfying
    all three is cheap, which is why the strip's SCOPE (entity guards only) does
    the real security work, not this predicate."""
    if not text or not isinstance(text, str):
        return False
    return bool(
        _INVENTORY_HEADER_RE.search(text)
        and _REASON_LINE_RE.search(text)
        and _SKU_LINE_RE.search(text)
    )


def scope_guard_text(text: str) -> str:
    """The text an ENTITY-scope guard should evaluate.

    Blanks the VALUE of the Reason line (keeping the label, so message shape is
    unchanged for anything inspecting structure) when the full inventory-request
    shape is present AND the value is short AND it does not read as a request.
    Otherwise returns `text` unchanged. Pure; never raises."""
    if not text or not isinstance(text, str):
        return text
    try:
        if not is_inventory_adjustment_request(text):
            return text

        def _blank(m: re.Match[str]) -> str:
            value = m.group(2).strip()
            # Per-match, so one smuggled line cannot un-strip a sibling
            # annotation, and vice versa.
            if len(value) > _MAX_REASON_LEN:
                return m.group(0)
            if _REASON_IS_REQUEST_RE.search(value):
                return m.group(0)
            return m.group(1)

        return _REASON_LINE_RE.sub(_blank, text)
    except Exception:  # noqa: BLE001 -- a normalizer must never break a guard
        return text
