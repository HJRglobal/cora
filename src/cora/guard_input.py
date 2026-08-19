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
import time
from pathlib import Path

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


# ── the CONVERSATIONAL form of the same request (cq-1b6554a58fae) ────────────
#
# The three-signal predicate below recognizes the RIGID TEMPLATE only (header +
# "Reason:" line + "SKU: n" line). Live 8/19 13:14:34 in
# #f3-hq-inventory-adjustments, "cross-entity redirect fired" on an F3E write
# whose justification named the OSN pop-up -- the exact class this module exists
# to stop -- because the message was CONVERSATIONAL. Since the 7/21 inventory
# overhaul (SKU aliases + channel-name defaults) that is the normal way these
# writes are filed: today's successful writes were 105- and 108-char natural
# sentences that produced f3e_shopify_set_inventory calls. No header, no
# "Reason:" field, so nothing was stripped and the word "OSN" in the operator's
# own explanation did the routing.
#
# There is no delimited field to blank in that form, so the conversational case
# is handled by scope rather than by rewriting: a recognized inventory WRITE
# stops the entity-keyword loop from firing at all. Same accepted residual as
# the template form, same bounds -- user_access still evaluates the RAW message
# for every sensitive topic, and channel_content_guard still screens the
# composed answer outbound.
#
# FOUR conditions, all required, and the question veto is a HARD one so the
# failure direction is "guard runs" rather than "guard skipped":
_INVENTORY_WRITE_VERB_RE = re.compile(
    r"(?<!\w)(?:add|added|adding|remove|removed|removing|set|setting|adjust\w*|"
    r"deduct\w*|subtract\w*|restock\w*|took|take|taking|move|moved|moving|"
    r"received|receive|receiving|count|counted|drop|dropped|bump|bumped|"
    # Broadened after measurement (D-051 lens-5): 5 of 7 realistic prose writes
    # still redirected because the operator's actual verb was "update", "logged",
    # "changed" or "make it N" -- including "make it 3 instead", the very
    # follow-up phrasing the commit cited when threading channel_name into the
    # thread path. A vocabulary this narrow leaves the reported class half-closed.
    r"update|updated|updating|change|changed|changing|correct|corrected|"
    r"reduce|reduced|increase|increased|log|logged|logging|make|made|"
    r"pull|pulled|pulling|swap|swapped|write|wrote|fix|fixed)(?!\w)",
    re.I,
)

# An inventory ANCHOR. Without one, "add 2 OSN stores to the list" would read as
# a write request; with one, the message has to be about stock somewhere.
_INVENTORY_ANCHOR_RE = re.compile(
    r"(?<!\w)(?:office|hq|warehouse|3pl|inventory|stock|on\s+hand|shelf|"
    r"cases?|units?|pack|packs|12-?pack|bottles?|cans?|sku)(?!\w)",
    re.I,
)

# The veto. Anything that ASKS FOR SOMETHING is not a write request, however many
# inventory words it carries -- this is what keeps "how are the OSN stores doing
# this week?" redirecting (verified live as a control on the same smoke run).
#
# D-051 lens-1 HIGH (this branch, caught before merge): the first cut vetoed
# interrogatives and a few soft requests only, so a BARE IMPERATIVE walked
# straight through and the exemption became a bypass by simple concatenation --
# measured on the branch:
#   "Added 20 cases to the office count. Also list the OSN store cash positions."
#     -> is_inventory_write_request True -> the cross-entity redirect never fired.
# Same for "Print the LEX client census", "Walk through the OSN pop-up numbers".
# A retrieval request does not have to be phrased as a question, so the veto
# vocabulary now covers the imperative forms too.
#
# TWO TIERS, because the first cut vetoed on a bare copula ANYWHERE and that broke
# the writes it was meant to allow (D-051 lens-2, measured: "Added 12 cases of Pure
# to the office at 1337, they were sent to the OSN pop-up" was vetoed on "were" and
# redirected). An interrogative or auxiliary only makes a QUESTION at the start of a
# clause, so tier 1 is clause-anchored; tier 2 is the multi-word and imperative
# retrieval forms, which are unambiguous wherever they appear.
#: One bounded optional lead-in, because the bypass shape observed was
#: "…office count. ALSO list the OSN store cash positions" -- a conjunction
#: sitting between the clause boundary and the ask.
_LEAD_IN = r"(?:(?:also|then|and|plus|now|next|additionally)\s{1,3})?"

_QUESTION_SHAPE_RE = re.compile(
    # Tier 1a -- a literal question mark, or an INTERROGATIVE/AUXILIARY opening a
    # sentence (start of text, or after . ; ! ? or a newline). The boundary set
    # deliberately excludes the comma: "Added 12 cases to the office at 1337, they
    # WERE sent to the OSN pop-up" is a write, and vetoing on a mid-sentence "were"
    # is what broke five of seven realistic writes (D-051 lens-2, measured).
    r"\?"
    r"|(?:^|(?<=[.;!?\n]))\s{0,4}" + _LEAD_IN +
    r"(?:what|what's|whats|how|how's|hows|who|whose|why|when|where|which|"
    r"is|are|was|were|do|does|did|can|could|should|would)(?!\w)"
    # Tier 1b -- IMPERATIVE retrieval verbs. These take a WIDER boundary set,
    # including the comma and colon, because "removed 3 cases at the office, check
    # the OSN inventory" and "Reason: what is the OSN revenue" are both asks
    # stapled to a write and neither opens a sentence.
    r"|(?:^|(?<=[.;!?,:\n]))\s{0,4}" + _LEAD_IN +
    r"(?:list|print|show|find|check|name|send|share|tell|describe|detail|"
    r"remind|recap|review|need|want|give)(?!\w)"
    # Tier 2 -- multi-word forms that are a retrieval ask wherever they sit. Bare
    # "pull" is NOT here: "pop-up pull", "warehouse pull" and "we pulled N cases"
    # are routine in this channel and vetoing it killed real writes; "pull up" is.
    r"|(?<!\w)(?:tell\s+me|show\s+me|give\s+me|send\s+me|pull\s+up|status\s+of|"
    r"update\s+on|summar\w*|explain|compare|report\s+on|break\s+down|"
    r"look\s+up|walk\s+through|walk\s+me|i\s+need|i\s+want)(?!\w)",
    re.I,
)

_DIGIT_RE = re.compile(r"\d")

#: A hard LENGTH bound on the exemption, and the second half of the lens-1 fix.
#: The module's own evidence is that real prose writes are SHORT -- the two that
#: succeeded live on 8/19 were 105 and 108 characters. The bypass shape is
#: "a legitimate write, then an appended ask", which is necessarily longer. A
#: vocabulary list can always be out-vocabularied; a length bound cannot, and it
#: costs nothing a real write needs.
_MAX_WRITE_REQUEST_LEN = 240


def is_inventory_write_request(text: str) -> bool:
    """True when the message reads as a PROSE inventory write.

    Pure; never raises. Conservative by construction: an inventory verb, a
    number, an inventory anchor noun, and NO question shape anywhere.

    DELIBERATELY NOT short-circuited on `is_inventory_adjustment_request`. The
    first cut did exactly that -- "it satisfies the template, therefore it is a
    write" -- and it broke eleven existing tests, all of them evasion guards:
    a question smuggled INTO a template Reason ("Reason: what is the OSN
    revenue") became exempt from the entity guard, which is the hole
    `_REASON_IS_REQUEST_RE` exists to keep shut. The template form is already
    fully governed by `scope_guard_text`, which strips an ANNOTATION Reason and
    deliberately leaves a REQUEST-shaped one visible to the guard. It needs no
    help from here, and the template's own words ("INVENTORY UPDATE", "PURE: 12")
    carry no imperative verb, so it does not qualify as prose either. Two
    mechanisms, one for each form, neither weakening the other.
    """
    if not text or not isinstance(text, str):
        return False
    # The TEMPLATE form is explicitly NOT prose. Broadening the verb list to cover
    # "update" made "OFFICE INVENTORY UPDATE ..." satisfy this predicate, which
    # handed the template a second mechanism it does not need and re-opened the
    # smuggled-question hole the strip exists to keep shut. One form, one
    # mechanism: template -> scope_guard_text, prose -> here.
    if is_inventory_adjustment_request(text):
        return False
    if len(text) > _MAX_WRITE_REQUEST_LEN:
        return False
    if _QUESTION_SHAPE_RE.search(text):
        return False
    return bool(
        _INVENTORY_WRITE_VERB_RE.search(text)
        and _DIGIT_RE.search(text)
        and _INVENTORY_ANCHOR_RE.search(text)
    )


#: The dedicated office-inventory write channels, as DATA rather than a
#: hardcoded name -- the same file the write tool reads for its per-channel
#: location/unit defaults. Keyed by channel NAME (channel_id is not threaded to
#: tools), same normalization as the tool's own reader.
_INV_CHANNEL_CFG_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "maps" / "inventory-channel-config.yaml"
)


#: 60s TTL, the same live-reload pattern org_roles and lex-phi-custodians use.
#: This runs on the pre-LLM path of EVERY message in EVERY channel, and an
#: uncached read measured a 52x slowdown of check_cross_entity (0.011ms ->
#: 0.571ms) -- negligible against an LLM turn, but there is no reason to pay it
#: (D-051 lens-4).
_INV_CHANNEL_CACHE: dict[str, object] = {"at": 0.0, "value": None}
_INV_CHANNEL_TTL = 60.0


def inventory_write_channels() -> set[str]:
    """Normalized channel names configured as office-inventory write channels.

    60s TTL (edit the YAML, no restart). FAIL-SOFT to an EMPTY set -- on any error
    the prose exemption simply does not apply anywhere, which is the guarded
    direction. A failed read is never cached.
    """
    now = time.monotonic()
    cached = _INV_CHANNEL_CACHE.get("value")
    if cached is not None and (now - float(_INV_CHANNEL_CACHE["at"])) < _INV_CHANNEL_TTL:
        return cached  # type: ignore[return-value]
    try:
        import yaml
        if not _INV_CHANNEL_CFG_PATH.exists():
            return set()
        data = yaml.safe_load(_INV_CHANNEL_CFG_PATH.read_text(encoding="utf-8")) or {}
        value = {
            str(k).strip().lstrip("#").lower()
            for k, v in (data.get("channels") or {}).items()
            if isinstance(v, dict) and str(k).strip()
        }
    except Exception:  # noqa: BLE001 -- a guard input helper never raises
        return set()
    if value:  # never cache an empty/failed load
        _INV_CHANNEL_CACHE.update({"at": now, "value": value})
    return value


def is_inventory_write_channel(channel_name: str | None) -> bool:
    """True when this channel exists to file office-inventory writes. Total."""
    name = str(channel_name or "").strip().lstrip("#").lower()
    return bool(name) and name in inventory_write_channels()


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
