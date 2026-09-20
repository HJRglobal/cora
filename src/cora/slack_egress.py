"""Single sanitizing egress boundary for every Slack send (Phase 2.1 / B1).

Closes the real F-5 / F-19 / N9 leaks on EVERY outbound path -- raw Drive/Asana
URLs (the recurring filing-summary source-opacity leak), naked GIDs/long IDs, and
utf-8->cp1252 mojibake (nudge DMs) -- by wrapping slack_sdk's sync WebClient
text-send methods (chat_postMessage / chat_update / chat_postEphemeral /
chat_scheduleMessage) at the CLASS level, once, idempotently. Bolt's `say` and
listener `client`, and every script's own WebClient, all funnel through these, so
the class patch covers all of them. Installed once from cora/__init__.py.

DELIBERATELY NARROW (a hard-won lesson, 2026-06-17 adversarial review): the
boundary applies ONLY transforms that are SAFE on arbitrary content and never
mangle structure -- mojibake repair, bare-URL/GID/long-ID redaction, and
markdown-bold normalization (**x** -> *x*, the ONE Slack-render fix that is
code-fence-/table-/token-safe; see reply_formatter.normalize_slack_bold). It
does NOT do the broader voice-flatten (emoji/dashes/whitespace/code-fence/table
collapse, list-marker rewrite) and does NOT redact named systems. Those are
CONVERSATIONAL concerns applied to interactive Q&A replies only, inline in
app.py via reply_formatter.format_reply. Running the conversational formatter on
EVERY send would corrupt proactive structured output -- code-fenced / fixed-width
tables (cash pulse, metrics digests, the strategy memo), intentional SIGNAL emoji
on cards (confidence dots, 👍/👎 affordances), numbered rankings -- and would
over-redact legitimate ops alerts that name a system ("the QuickBooks sync
failed"). The safety layer here never breaks tables/emoji and never strips a
system name from an ops alert.

Bold normalization was added to the boundary 2026-07-12 (F-04): the earlier
"per-sender opt-in" stance left literal **bold** egressing on the two paths that
skip format_reply -- VERBATIM_TABLE_TOOLS replies (tool prose presented as-is)
and STREAMING mid-frames (chat_update posts raw cumulative text before the final
formatted update). normalize_slack_bold is fence-/table-/Slack-token-safe and
idempotent, so applying it universally fixes both without touching structure
(format_reply already converts bold on the conversational path -> a no-op there).

Write-sentinel scrub added to the boundary 2026-08-30 (session #11 S1): the
WRITE_CONFIRMED / WRITE_BLOCKED contract tokens are MODEL-FACING directives that
ride in tool_result payloads. Thirteen staged-write tools emit them while only
eight are handled by the narration net, so on the other five the model is merely
TRUSTED to obey the English directive and not echo it -- with nothing downstream
removing it. reply_formatter's docstring claimed app.py stripped them; app.py
never contained a strip (verified: zero occurrences of the token). This scrub is
deliberately weaker than tool_dispatch._strip_write_sentinel, which INVENTS
replacement prose ("Done.") on a bare sentinel -- safe on a known confirm payload,
unsafe on arbitrary content. Here the scrub only ever DELETES the token, never
substitutes, and returns input byte-identical when no token is present.

NOT covered (documented residual): `blocks=` payloads. The boundary sanitizes the
`text=`/`markdown_text=` kwargs only, so code-built Block Kit cards (e.g. the
meeting-item cards) sit outside it. Those are code-composed rather than model-
authored, so they are not a sentinel-echo surface; a card that ever interpolates
model prose must sanitize at its own build site.

NOT covered (documented residual, Phase 3): a handful of scheduled senders that
POST raw JSON to slack.com/api via httpx/requests bypass slack_sdk.WebClient and
thus this patch -- those wrap text with slack_egress.sanitize_text at the POST
site instead (see B1). The async WebClient is not wrapped (the bot is sync-only);
instead its construction is GUARDED to raise loudly (B3), so an async send can't
silently bypass the boundary. See the forensic rebuild log.
"""

from __future__ import annotations

import functools
import logging
import os
import re
from typing import Callable

from .reply_formatter import normalize_slack_bold, redact_links_and_ids

log = logging.getLogger(__name__)


# ── Mojibake repair (N9) ─────────────────────────────────────────────────────
# Some proactive text carries UTF-8 punctuation/emoji bytes mis-decoded as cp1252
# (e.g. an em-dash, a bullet, and the raising-hands emoji mojibake'd in nudge DMs).
# Build the {corrupted: intended} map by REPLAYING the exact utf-8 -> cp1252
# mis-decode on the intended chars, so this source stays clean and we never
# hand-type mojibake (transport layers silently normalize hand-typed mojibake).
_MOJIBAKE_INTENDED: tuple[str, ...] = (
    "—",  # em dash
    "–",  # en dash
    "‘", "’",  # curly single quotes
    "“", "”",  # curly double quotes
    "…",  # ellipsis
    "•",  # bullet
    "\U0001F64C",  # raising hands
    "\U0001F64F",  # folded hands
)


def _build_mojibake_map() -> dict[str, str]:
    fixes: dict[str, str] = {}
    for ch in _MOJIBAKE_INTENDED:
        try:
            bad = ch.encode("utf-8").decode("cp1252")
        except UnicodeDecodeError:
            continue
        if bad and bad != ch:
            fixes[bad] = ch
    return fixes


_MOJIBAKE_FIXES: dict[str, str] = _build_mojibake_map()


def repair_mojibake(text: str) -> str:
    """Repair known utf-8->cp1252 mojibake sequences. Idempotent; clean text is
    returned unchanged."""
    if not text:
        return text
    for bad, good in _MOJIBAKE_FIXES.items():
        if bad in text:
            text = text.replace(bad, good)
    return text


# -- Write-sentinel scrub (session #11 S1) ------------------------------------
# The contract tokens are model-facing and must never reach a human. Kept in sync
# with claude_client._SHOPIFY_SENTINELS by tests/test_write_sentinel_contract.py.
_WRITE_SENTINELS = ("WRITE_CONFIRMED", "WRITE_BLOCKED")
# A leading sentinel carries a trailing directive clause ("WRITE_CONFIRMED: post
# this verbatim -- ..."). Strip the token plus an immediately-following colon/dash
# separator, nothing more; the human-meaningful remainder is preserved verbatim.
_SENTINEL_ANY_RE = re.compile(r"\b(?:WRITE_CONFIRMED|WRITE_BLOCKED)\b")
_SENTINEL_LEAD_RE = re.compile(
    r"^\s*(?:WRITE_CONFIRMED|WRITE_BLOCKED)\b[ \t]*[:\-–—]*[ \t]*"
)

# observe (default) = a leak is a WARNING; enforce = a leak is an ERROR, which the
# nightly health check's ERROR-volume triage escalates. THE SCRUB ITSELF IS ALWAYS
# ON in both modes -- gating a leak-prevention behind a flag would ship the leak.
# The flag governs how loudly a leak is reported, so "enforce after a clean week"
# stays a real ritual (observe = count them; enforce = one is an alarm) with no
# mode in which the token is allowed through to a human.
_ENFORCE_ENV = "CORA_SENTINEL_ENFORCE"


_SENTINEL_MODES = ("observe", "enforce")
_MODE_WARNED: set[str] = set()


def _sentinel_mode() -> str:
    """observe (default) | enforce. Any other value reads as observe and is
    WARNED once per process (D-051 lens A MED #4): the raw value used to leak
    into every rail log line (`mode=true`) and both rails compare `== "enforce"`,
    so a typo silently kept observe while the log claimed a third mode."""
    raw = (os.environ.get(_ENFORCE_ENV) or "observe").strip().lower()
    if raw in _SENTINEL_MODES:
        return raw
    if raw not in _MODE_WARNED:
        _MODE_WARNED.add(raw)
        log.warning("%s=%r is not a mode (observe|enforce) -- treating it as observe",
                    _ENFORCE_ENV, raw)
    return "observe"


def scrub_write_sentinels(text):
    """Delete WRITE_CONFIRMED / WRITE_BLOCKED tokens from outbound prose.

    DELETES ONLY -- never substitutes replacement prose (that is
    tool_dispatch._strip_write_sentinel's job on a known confirm payload, and it
    is unsafe on arbitrary content). Returns the input unchanged, byte for byte,
    when no sentinel is present, so this is a no-op on every ordinary send.

    Pathological case -- the body is NOTHING but a sentinel: scrubbing would yield
    an empty body and Slack rejects an empty send. Rather than fabricate filler,
    the original is returned and the event is logged at ERROR regardless of mode.
    """
    if not isinstance(text, str) or not text:
        return text
    if _SENTINEL_ANY_RE.search(text) is None:
        return text  # byte-identical fast path -- the overwhelmingly common case
    scrubbed = _SENTINEL_ANY_RE.sub("", _SENTINEL_LEAD_RE.sub("", text))
    scrubbed = re.sub(r"[ \t]{2,}", " ", scrubbed).strip()
    if not scrubbed:
        log.error(
            "sentinel-egress-leak mode=%s UNSCRUBBABLE (body was sentinel-only, "
            "%d chars) -- sending original rather than fabricating a body",
            _sentinel_mode(), len(text),
        )
        return text
    emit = log.error if _sentinel_mode() == "enforce" else log.warning
    emit(
        "sentinel-egress-leak mode=%s removed=%d chars -- a write-tool directive "
        "reached the outbound seam (the model echoed a contract token)",
        _sentinel_mode(), len(text) - len(scrubbed),
    )
    return scrubbed


# -- Phantom-write-claim + fabricated-id screen (Code #12 S2', cq-60024f032136) --
# The 2026-09-03 06:53 / 06:54 founder-DM replies asserted "Done. Staging all
# three", "All three locked in", "canonicalized as resolved", "Knowledge entry is
# live" -- with ZERO tool_use in the turn -- and named two well-formed cq- ids that
# exist in no ledger. The S1 sentinel scrub above saw nothing: it guards the model
# ECHOING a contract token, not the model ASSERTING a write in prose. Doctrine
# (decisions.md 2026-09-03, Harrison-ratified): a write confirmation emitted with
# zero tool_use is a phantom by definition, and a seam detector measures OUTPUT
# claims against the turn's tool ledger (D-257 -- measured, never argv).
#
# Same observe -> enforce ritual and the SAME flag as the sentinel scrub
# (CORA_SENTINEL_ENFORCE): observe = a WARNING keyed `phantom-write-claim` naming
# the matched phrase / the fabricated id, reply delivered byte-identical; enforce =
# the honest template is PREPENDED as the reply's first line (the body is kept
# byte-identical) and the fabricated id is redacted, at ERROR. The first cut
# DELETED every claiming sentence; the D-051 review (lens A HIGH #1, 2026-09-09)
# refused that: 'filed' / 'updated' / 'created' are ordinary English, so a
# legitimate zero-tool KB answer ("the invoice was filed on Tuesday") would have
# lost its answer sentence -- the 'a strip can remove the outcome' class. A
# correction line the reader sees FIRST is honest and reversible; a deletion is
# neither. The nightly health check counts the WARN key beside the sentinel
# leaks; the flip is Harrison's, after a clean week counted WITH this screen (S3').
#
# CALLED EXPLICITLY by app._dispatch_qa on the model's FINAL reply text, NOT from
# the class-level WebClient wrapper below: (a) the turn's tool_use count exists
# only there; (b) Slack renders `blocks` and ignores `text` when both are present,
# so a rewrite must land BEFORE the confirm-card blocks are built from the same
# string; (c) code-authored posts (cards, digests, interceptor replies, scripts)
# are not model claims and must not inflate the count the enforce flip reads.
#
# The lexicon is the ruled list, verbatim (kickoff S2'): staged | queued | locked
# in | canonicalized | filed | created | updated | deleted | is live | ^done.
# "Referring to a Cora action" is not decidable by regex; the observe week
# measures the false-positive rate (a KB answer about "the invoice filed on
# Tuesday" with no tool call will count) and the phrase is logged so the noise can
# be characterized before any tightening is ruled.
_WRITE_CLAIM_RE = re.compile(
    r"\b(?:staged|queued|locked\s+in|canonicali[sz]ed|filed|created|updated|deleted|is\s+live)\b"
    r"|^\s*done\.",
    re.IGNORECASE | re.MULTILINE,
)
PHANTOM_HONEST_TEMPLATE = "I did not perform any action this turn."
PHANTOM_LOG_KEY = "phantom-write-claim"


def _known_cq_ids() -> frozenset[str]:
    from .code_queue import known_ids  # lazy: code_queue imports drive_io / phi_guard
    return known_ids()


def _known_dw_ids() -> frozenset[str]:
    from .delegated_work import known_job_ids  # lazy: same reason
    return known_job_ids()


# (label, id pattern, ledger reader). Generalized to every id family that HAS a
# ledger: cq- (the code-session queue) and dw- (delegated work). No `r-` entry:
# nothing in this repo mints an "r-" id (verified 2026-09-09), and a check with no
# ledger to read would redact every legitimate use of the pattern.
_ID_LEDGERS: tuple[tuple[str, re.Pattern[str], Callable[[], frozenset[str]]], ...] = (
    ("cq", re.compile(r"\bcq-[0-9a-f]{12}\b", re.IGNORECASE), _known_cq_ids),
    ("dw", re.compile(r"\bdw-[0-9a-f]{12}\b", re.IGNORECASE), _known_dw_ids),
)

# Slack link / mention tokens (`<https://x|label>`, `<@U..>`, `<#C..|name>`) are
# masked before the lexicon runs (D-051 lens A MED #5): a label like "Updated
# pricing page" is not a write claim, and under enforce nothing inside the token
# is ever touched (the body is kept byte-identical anyway).
_LINK_TOKEN_RE = re.compile(r"<[^<>\n]{1,400}>")


def _prepend_honest_line(text: str) -> str:
    """ENFORCE-mode correction: the honest template becomes the reply's FIRST line;
    the body is kept byte-identical (D-051 lens A HIGH #1 -- never a strip: a
    heuristic deletion over model prose removes outcomes and mangles quoted
    text). Idempotent: a body already led by the template is returned as-is."""
    body = text.lstrip()
    if body.startswith(PHANTOM_HONEST_TEMPLATE):
        return text
    return f"{PHANTOM_HONEST_TEMPLATE}\n\n{body}"


def screen_phantom_write_claims(text, *, tool_use_count, channel_name: str = "",
                                user_id: str = ""):
    """Screen ONE model reply for (1) ids that exist in no ledger and (2) a write
    claim made in a turn with zero tool_use.

    Observe mode (default, CORA_SENTINEL_ENFORCE unset): every hit is a WARNING
    keyed ``phantom-write-claim`` and the text is returned BYTE-IDENTICAL.
    Enforce mode: a fabricated id is redacted to ``[unknown id]`` and the honest
    template is PREPENDED as the first line (the body is kept byte-identical), at
    ERROR. Slack link/mention tokens are masked before the lexicon runs.

    ``tool_use_count`` is the turn's tool_use ledger (claude_client meta). None =
    unknown -> the lexicon half is skipped (never assume zero); the id half runs
    regardless of the count, because an invented id is invented even in a turn
    that called a read tool. Non-string / empty input passes through untouched;
    a ledger that cannot be read skips ITS id family with a WARNING rather than
    redacting real ids (fail-open on the reference set, never on the claim).
    """
    if not isinstance(text, str) or not text:
        return text
    try:
        count = None if tool_use_count is None else int(tool_use_count)
    except (TypeError, ValueError):
        count = None
    mode = _sentinel_mode()
    emit = log.error if mode == "enforce" else log.warning
    out = text
    for label, rx, reader in _ID_LEDGERS:
        found = {m.group(0).lower() for m in rx.finditer(out)}
        if not found:
            continue
        try:
            known = reader()
        except Exception:  # noqa: BLE001 -- an unreadable ledger must not redact real ids
            log.warning("%s kind=fabricated-id ledger=%s UNAVAILABLE -- %d id(s) not "
                        "checked this turn", PHANTOM_LOG_KEY, label, len(found), exc_info=True)
            continue
        if known is None:
            # D-051 lens C F6: a ledger that does not EXIST is "cannot check", not
            # "nothing is known" -- an empty reference set would redact every real
            # id under ENFORCE. Skip the family, loudly.
            log.warning("%s kind=fabricated-id ledger=%s ABSENT -- %d id(s) not checked "
                        "this turn", PHANTOM_LOG_KEY, label, len(found))
            continue
        for fid in sorted(found - set(known)):
            emit("%s kind=fabricated-id id=%s ledger=%s mode=%s channel=#%s user=%s -- "
                 "the reply names an id that exists in no ledger",
                 PHANTOM_LOG_KEY, fid, label, mode, channel_name or "?", user_id or "?")
            if mode == "enforce":
                out = re.sub(re.escape(fid), "[unknown id]", out, flags=re.IGNORECASE)
    if count == 0:
        m = _WRITE_CLAIM_RE.search(_LINK_TOKEN_RE.sub(" ", out))
        if m:
            emit("%s kind=lexicon phrase=%r mode=%s channel=#%s user=%s -- a write "
                 "claim with zero tool_use this turn",
                 PHANTOM_LOG_KEY, m.group(0).strip(), mode, channel_name or "?",
                 user_id or "?")
            if mode == "enforce":
                out = _prepend_honest_line(out)
    return out


# -- Capability-denial + internal-tool-name screen (Code #13 slice 1, cq-2a88e32a75ea) --
# The 2026-09-10 09:16-10:30 AZ founder-DM replies to five unmatched verb-shaped
# messages said "I don't have visibility into the code queue or staged items right
# now ... use the Asana interface directly", "I don't have a way to ship code
# sessions directly", "I don't have a tool to close code-queue items", "I don't
# have tools to approve or stage code-queue items" -- six minutes after the same
# bot STAGED a row on a typed verb -- and named `cora_queue_code_session` to the
# founder. S2' above is blind to these BY DESIGN (they are not write claims), so
# this is its SIBLING at the same seam: the model's FINAL reply text, the same
# tool ledger, the same flag, the same observe -> enforce ritual.
#
# THE DISCRIMINATOR IS THE CAPABILITY SET, NOT THE PHRASE (charter section 1, the
# honesty rail): "I don't have that document" and "I can't tell from here" are
# honest sentences about things the bot lacks. A denial trips ONLY when the
# sentence it sits in names something the bot HAS in that channel -- derived at
# call time by cora.capability_set from the offered tool registry, the queue-verb
# table and the ladder registry, never hand-listed here.
#
# Two kinds, one key (`phantom-capability-claim`, counted by cora.egress_rails as
# the THIRD rail beside sentinel-egress-leak and phantom-write-claim):
#   kind=denial   -- a capability-denial phrase in a zero-tool_use turn about a
#                    capability the bot has. Enforce = PREPEND the honest template
#                    with the exact verb / ask to try (D-282: never a sentence strip).
#   kind=toolname -- an internal symbol (`cora_*`, `_dispatch_*`, any registry tool
#                    name) spoken on a non-developer surface, regardless of tool
#                    count (a leak is a leak). Enforce = the SYMBOL is replaced by
#                    `[internal tool]`, the S2' fabricated-id precedent (a token
#                    substitution, never a sentence strip); the prepend template
#                    would be false on a turn that DID use the tool.
# Developer surfaces (#cora-build / #cora-health / #cora-security / #cora-dev) are
# exempt from the toolname half only -- a false denial is false anywhere.
CAPABILITY_LOG_KEY = "phantom-capability-claim"
CAPABILITY_HONEST_TEMPLATE = "I have tools for that; I did not use them this turn."
INTERNAL_TOOL_REDACTION = "[internal tool]"

# The ruled denial lexicon + synonyms (kickoff section 1 slice 1; named in the
# Code #13 report). Bounded classes only; no nested quantifiers (ReDoS discipline).
_CAP_NEG_HAVE = (
    r"\bI\s+(?:don'?t|do\s+not|didn'?t|did\s+not|won'?t|will\s+not)\s+(?:currently\s+|actually\s+|really\s+)?have\s+"
    r"(?:direct\s+|any\s+|the\s+|a\s+|an\s+)?(?:direct\s+)?"
    r"(?:tools?|access|visibility|way|ability|permissions?|mechanism|integration|connector|means|"
    r"capability|capabilities|hooks?|line|route|path|window)\b"
)
_CAP_NO_HAVE = (
    r"\bI\s+have\s+no\s+(?:direct\s+)?(?:tools?|access|visibility|way|ability|permission|means|integration|"
    r"connector|mechanism|hooks?)\b"
    r"|\bI\s+lack\s+(?:the\s+|any\s+)?(?:tools?|access|visibility|ability|permission|means)\b"
    r"|\bthere'?s?\s+(?:is\s+)?no\s+(?:tool|way|integration|connector|hook)\s+(?:for\s+me|I\s+(?:can|have))\b"
)
_CAP_CANT_VERB = (
    r"\bI\s+(?:can'?t|cannot|can\s+not|am\s+not\s+able\s+to|'m\s+not\s+able\s+to|am\s+unable\s+to|"
    r"'m\s+unable\s+to|won'?t\s+be\s+able\s+to|don'?t\s+have\s+the\s+ability\s+to|have\s+no\s+way\s+to)\s+"
    r"(?:directly\s+|actually\s+|currently\s+|really\s+)?"
    r"(?:access|see|reach|stage|ship|dismiss|close|approve|queue|read|pull(?:\s+up)?|check|view|open|query|"
    r"use|touch|modify|update|create|send|post|run|execute|trigger|look\s+(?:at|into|up)|get\s+(?:to|into|at)|"
    r"interact\s+with|connect\s+to|talk\s+to|search|retrieve|fetch|list|manage|edit|write\s+to|log\s+into|"
    r"delete|complete|mark|schedule|draft|dm|message)\b"
)
_CAP_NOT_CONNECTED = (
    r"\b(?:isn'?t|is\s+not|aren'?t|are\s+not|not)\s+(?:currently\s+|yet\s+)?"
    r"(?:connected|available|accessible|exposed|wired(?:\s+up)?|hooked\s+up|integrated|enabled|set\s+up|"
    r"provisioned|plugged\s+in)\b"
    r"|\bnot\s+available\s+to\s+me\b"
    r"|\b(?:outside|beyond)\s+(?:of\s+)?my\s+(?:reach|access|tools|toolset|capabilities|scope|purview)\b"
    r"|\bI\s+(?:am|'m)\s+not\s+(?:connected|wired|hooked\s+up|integrated|plugged\s+in)\b"
)
_CAP_USE_INTERFACE = (
    r"\b(?:use|check|open|go\s+to|try|log\s+into|do\s+(?:that|this|it)\s+(?:in|through|via))\s+"
    r"(?:the\s+)?(?:asana|hubspot|quickbooks|qbo|shopify|gmail|calendar|slack|deposco|notion|klaviyo|"
    r"make(?:\.com)?|google\s+calendar)\s+(?:interface|app|ui|dashboard|console|website|site|portal|web\s+app)\b"
    r"|\b(?:do|handle|check|stage|approve|dismiss|close|ship|update|create|complete|mark|queue)\s+"
    r"(?:that|this|it|those|these|them)\s+(?:manually|yourself|directly|by\s+hand)\b"
    r"|\byou'?ll\s+(?:need|have)\s+to\s+(?:do|handle|check|stage|approve|dismiss|close|ship|update|create|queue)\s+"
    r"(?:that|this|it|those|these|them\s+)?(?:manually|yourself|directly)\b"
)
_DENIAL_RE = re.compile(
    "(?:" + _CAP_NEG_HAVE + "|" + _CAP_NO_HAVE + "|" + _CAP_CANT_VERB + "|" + _CAP_NOT_CONNECTED
    + "|" + _CAP_USE_INTERFACE + ")",
    re.IGNORECASE,
)
_DEVELOPER_SURFACE_RE = re.compile(r"(?:^|-)cora-(?:build|health|security|dev)\b", re.IGNORECASE)
_INTERNAL_SYMBOL_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:cora_[a-z0-9]+(?:_[a-z0-9]+)+|_dispatch_[a-z0-9]+(?:_[a-z0-9]+)*)(?![A-Za-z0-9])")
_SENTENCE_BREAK_RE = re.compile(r"[.!?;\n]")


def is_developer_surface(channel_name: str) -> bool:
    """#cora-build / #cora-health / #cora-security / #cora-dev (and their entity-prefixed
    siblings such as #lex-cora-build): internal tool names are legitimate there."""
    return bool(_DEVELOPER_SURFACE_RE.search(str(channel_name or "")))


def _denial_window(text: str, start: int, end: int) -> str:
    """The sentence the denial sits in (bounded 160 chars back / 220 ahead), so the
    capability term is looked for where the denial's OBJECT lives -- after it
    ("...visibility into the code queue") or before it ("HubSpot isn't connected")."""
    lo = max(0, start - 160)
    hi = min(len(text), end + 220)
    before = text[lo:start]
    brk = list(_SENTENCE_BREAK_RE.finditer(before))
    if brk:
        before = before[brk[-1].end():]
    after = text[end:hi]
    m = _SENTENCE_BREAK_RE.search(after)
    if m:
        after = after[:m.start()]
    return before + text[start:end] + after


def _prepend_capability_line(text: str, hint: str) -> str:
    """ENFORCE for kind=denial: the honest template + the exact verb / ask to try
    becomes the FIRST line; the body is kept byte-identical (D-282). Idempotent."""
    body = text.lstrip()
    if body.startswith(CAPABILITY_HONEST_TEMPLATE):
        return text
    return f"{CAPABILITY_HONEST_TEMPLATE} Try: {hint}\n\n{body}"


def _capability_terms(entity: str, cross_entity: bool, founder: bool) -> dict[str, str]:
    from .capability_set import capability_terms  # lazy: pulls tool_dispatch
    return capability_terms(entity, cross_entity=cross_entity, founder=founder)


def _registry_symbols() -> frozenset[str]:
    from .capability_set import registry_tool_names  # lazy
    return registry_tool_names()


def screen_capability_claims(text, *, tool_use_count, channel_name: str = "", user_id: str = "",
                             entity: str = "", cross_entity: bool = False, founder: bool = False):
    """Screen ONE model reply for (1) a capability denial about a capability the bot
    HAS in this channel, in a zero-tool_use turn, and (2) an internal tool symbol
    spoken on a non-developer surface.

    Observe mode (default): every hit is a WARNING keyed ``phantom-capability-claim``
    (``kind=denial`` names the phrase and the matched term; ``kind=toolname`` names
    the symbol) and the text is returned BYTE-IDENTICAL. Enforce mode: a denial
    gets the honest template + `Try:` hint PREPENDED (body byte-identical); a symbol
    is replaced by ``[internal tool]``; both at ERROR.

    ``tool_use_count`` None = unknown -> the denial half is skipped (never assume
    zero); the toolname half runs regardless of the count. A capability set that
    cannot be derived (registry import failure) is EMPTY -> no denial can trip
    (fail-open on the reference set, never on the claim), logged once per turn.
    """
    if not isinstance(text, str) or not text:
        return text
    try:
        count = None if tool_use_count is None else int(tool_use_count)
    except (TypeError, ValueError):
        count = None
    mode = _sentinel_mode()
    emit = log.error if mode == "enforce" else log.warning
    out = text

    # (2) internal tool symbols -- any count, non-developer surfaces only
    if not is_developer_surface(channel_name):
        symbols = {m.group(0) for m in _INTERNAL_SYMBOL_RE.finditer(out)}
        try:
            registry = _registry_symbols()
        except Exception:  # noqa: BLE001 -- an unreadable registry never redacts prose
            registry = frozenset()
        for name in registry:
            if name and re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", out):
                symbols.add(name)
        for sym in sorted(symbols):
            emit("%s kind=toolname symbol=%s mode=%s channel=#%s user=%s tool_use=%s -- an "
                 "internal tool name reached a non-developer surface",
                 CAPABILITY_LOG_KEY, sym, mode, channel_name or "?", user_id or "?",
                 "?" if count is None else count)
            if mode == "enforce":
                out = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(sym)}(?![A-Za-z0-9_])",
                             INTERNAL_TOOL_REDACTION, out)

    # (1) capability denial -- zero-tool_use turns only, about something the bot HAS
    if count == 0:
        masked = _LINK_TOKEN_RE.sub(" ", out)
        try:
            terms = _capability_terms(entity, cross_entity, founder)
        except Exception:  # noqa: BLE001 -- no capability set = no denial can be judged false
            # NOT a firing line (no ` kind=` after the key): the health count must
            # never read a counting failure as a phantom claim.
            log.warning("%s capability set UNAVAILABLE -- denial half skipped this turn",
                        CAPABILITY_LOG_KEY, exc_info=True)
            terms = {}
        if terms:
            from .capability_set import find_capability_term  # lazy
            for m in _DENIAL_RE.finditer(masked):
                window = _denial_window(masked, m.start(), m.end())
                hit = find_capability_term(window, terms)
                if hit is None:
                    continue
                term, hint = hit
                emit("%s kind=denial phrase=%r term=%r mode=%s channel=#%s user=%s -- a capability "
                     "denial with zero tool_use about a capability the bot has in this channel",
                     CAPABILITY_LOG_KEY, m.group(0).strip(), term, mode, channel_name or "?",
                     user_id or "?")
                if mode == "enforce":
                    out = _prepend_capability_line(out, hint)
                break  # one WARN per reply, like the S2' lexicon half (keeps the count per turn)
    return out


# ── The single sanitizer ──────────────────────────────────────────────────────
def sanitize_text(text):
    """Universal SAFETY transforms applied to EVERY outbound Slack message body.

    Mojibake repair + bare-URL/GID/long-ID redaction (sanctioned <url|label>
    links preserved) + markdown-bold normalization (**x** -> *x*, fence-/table-/
    token-safe, idempotent -- F-04). NO broader voice/emoji/whitespace flattening
    and NO named-source redaction -- those are conversational-only (see module
    docstring). Non-string / empty input passes through untouched; never raises
    (the wrapper also guards).

    NOT PURE since 2026-08-30 (D-051 review): scrub_write_sentinels reads
    CORA_SENTINEL_ENFORCE and emits a log record on any send carrying a contract
    token. The old docstring said "Pure", which stopped being true the moment
    the scrub was inserted -- exactly the docstring-claims-behaviour-that-is-not-
    there class S1 was fixing."""
    if not isinstance(text, str) or not text:
        return text
    text = repair_mojibake(text)
    text = scrub_write_sentinels(text)
    text = redact_links_and_ids(text)
    text = normalize_slack_bold(text)
    return text


# ── Class-level WebClient wrapper ──────────────────────────────────────────────
_SEND_METHODS = (
    "chat_postMessage",
    "chat_update",
    "chat_postEphemeral",
    "chat_scheduleMessage",
)
# Both classic `text=` and the newer `markdown_text=` body kwargs are sanitized.
_TEXT_KWARGS = ("text", "markdown_text")
_installed = False


def _make_wrapper(original):
    @functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        for key in _TEXT_KWARGS:
            val = kwargs.get(key)
            if isinstance(val, str) and val:
                try:
                    kwargs[key] = sanitize_text(val)
                except Exception:  # noqa: BLE001 -- never let sanitization block a send
                    log.exception("egress sanitize failed; sending %s raw", key)
        return original(self, *args, **kwargs)

    wrapper._cora_egress_wrapped = True  # type: ignore[attr-defined]
    return wrapper


def _guard_async_webclient() -> None:
    """Forbid AsyncWebClient instantiation (B3): an async Slack send would bypass
    the sync-WebClient patch and egress UNSANITIZED. Cora is sync-only, so the
    correct posture is to FORBID the bypass, not silently tolerate it -- fail LOUD
    at construction so the single-boundary invariant can't regress unnoticed (a
    future maintainer who deliberately adds async Slack hits this, sees the
    message, and routes through the sync client or extends the boundary). Fully
    no-op when slack_sdk's async client / aiohttp is absent (the current env --
    aiohttp isn't installed, so AsyncWebClient isn't even importable). Idempotent."""
    try:
        from slack_sdk.web.async_client import AsyncWebClient
    except Exception:  # noqa: BLE001 -- async client / aiohttp not importable: nothing to guard
        return
    if getattr(AsyncWebClient, "_cora_async_guarded", False):
        return
    _orig_init = AsyncWebClient.__init__

    @functools.wraps(_orig_init)
    def _guarded_init(self, *args, **kwargs):
        raise RuntimeError(
            "Cora is sync-only: AsyncWebClient bypasses the egress sanitizer. "
            "Route Slack sends through the sync slack_sdk.WebClient (which is "
            "patched), or extend slack_egress to wrap AsyncWebClient.chat_*."
        )

    AsyncWebClient.__init__ = _guarded_init  # type: ignore[assignment]
    AsyncWebClient._cora_async_guarded = True  # type: ignore[attr-defined]
    log.info("egress: AsyncWebClient construction guarded (sync-only invariant)")


def install_egress_sanitizer() -> bool:
    """Patch slack_sdk's sync WebClient text-send methods to sanitize the message
    body. Idempotent (safe to call repeatedly / from multiple entry points).
    Returns True if the patch is in place after the call, False if slack_sdk is
    absent."""
    global _installed
    if _installed:
        return True
    try:
        from slack_sdk.web.client import WebClient
    except Exception:  # noqa: BLE001 -- slack_sdk not importable (e.g. minimal env)
        log.debug("slack_sdk WebClient not importable; egress sanitizer not installed")
        return False

    for name in _SEND_METHODS:
        original = getattr(WebClient, name, None)
        if original is None:
            continue
        if getattr(original, "_cora_egress_wrapped", False):
            continue
        setattr(WebClient, name, _make_wrapper(original))

    try:
        _guard_async_webclient()
    except Exception:  # noqa: BLE001 -- the async guard must NEVER break the sync install
        log.debug("async-webclient guard skipped", exc_info=True)

    _installed = True
    log.info("egress sanitizer installed on WebClient %s", ", ".join(_SEND_METHODS))
    return True
