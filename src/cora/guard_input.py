"""Normalize the TEXT that deterministic pre-LLM scope guards evaluate.

Why this exists (live incidents, #f3-hq-inventory-adjustments, 2026-08-03..08-13):
the office-inventory write request carries a free-text ``Reason:`` line that is
OPERATOR ANNOTATION, not a question. Scope guards read the whole message, so the
annotation drove routing:

  * "Reason: 4 OSN Stores"   -> cross_entity_guard saw the word "osn" and
    redirected an all-F3E-SKU write to #osn-leadership (8/03 13:23, 8/05 16:35,
    8/06 07:55 -- refused 3x across two users before a human pushed back).
  * "Reason: OSN Stores"     -> same, twice more in one thread until Hannah
    dropped the Reason line entirely and the write went through.

Every SKU and the location in those requests were F3 PURE at the F3 office. The
guards were right about the keyword and wrong about the field.

DELIBERATELY NARROW -- this is the whole security argument. Blanking any line
that merely starts with "Reason:" would hand every guard a trivial evasion
prefix ("Reason: what is <person>'s salary"). So the strip requires the FULL
inventory-request SHAPE to be present: an INVENTORY UPDATE header AND a Reason
line AND at least one SKU/quantity line. A bare "Reason: ..." message is left
completely untouched and every guard still sees it.

Scope of effect: guard INPUT only. The real message still reaches the LLM and the
inventory tool with the Reason intact -- the Reason is recorded on the
adjustment, so it must not be stripped from the request itself.

Residual, measured not assumed (1 occurrence in the 8/03-8/13 window): a THREAD
FOLLOW-UP that names an entity in prose -- Hannah's "OSN is just the reason"
(8/06 07:56) -- still trips the cross-entity guard, because the follow-up text
carries the keyword and none of the request shape. Fixing that needs thread-parent
context at the guard call site; seeded separately rather than half-handled here.
"""

from __future__ import annotations

import re

# An "OFFICE INVENTORY UPDATE - 1337 S Gilbert Rd" style header. Kept loose on
# the qualifier ("OFFICE"/"HQ"/none) and the trailing location.
_INVENTORY_HEADER_RE = re.compile(r"^\s*\**\s*[\w \-]*inventory\s+update\b", re.I | re.M)

# "Reason: <free text>" -- optionally Slack-bolded ("*Reason:* ...").
_REASON_LINE_RE = re.compile(r"^([ \t]*\**\s*reason\s*:\**[ \t]*)(.*)$", re.I | re.M)

# A SKU/quantity line: "• PURE-Original: 2", "- F3-PureE-V4F: 64", "PURESL: 2".
_SKU_LINE_RE = re.compile(r"^\s*(?:[•*\-–—]\s*)?[\w][\w \-/.()]*:\s*\d+\s*$", re.M)


def is_inventory_adjustment_request(text: str) -> bool:
    """True only when ALL THREE structural signals of an office-inventory write
    request are present. Any one alone is not enough (see module docstring)."""
    if not text or not isinstance(text, str):
        return False
    return bool(
        _INVENTORY_HEADER_RE.search(text)
        and _REASON_LINE_RE.search(text)
        and _SKU_LINE_RE.search(text)
    )


def scope_guard_text(text: str) -> str:
    """The text a scope/entity/sensitivity guard should evaluate.

    Blanks the VALUE of the Reason line (keeping the label, so the message shape
    is unchanged for anything that inspects structure) when -- and only when --
    the full inventory-request shape is present. Otherwise returns `text`
    unchanged. Pure; never raises."""
    if not text or not isinstance(text, str):
        return text
    try:
        if not is_inventory_adjustment_request(text):
            return text
        return _REASON_LINE_RE.sub(lambda m: m.group(1), text)
    except Exception:  # noqa: BLE001 -- a normalizer must never break a guard
        return text
