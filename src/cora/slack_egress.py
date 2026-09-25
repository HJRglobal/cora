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
import hashlib
import heapq
import html
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from .reply_formatter import normalize_slack_bold, redact_links_and_ids

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


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
# The lexicon VERBS are the ruled list, verbatim (kickoff S2'): staged | queued |
# locked in | canonicalized | filed | created | updated | deleted | is live | ^done.
# SUPERSEDED as a bare-word match by R14-9(b) below: _WRITE_CLAIM_RE is kept only as
# the documented pre-R14-9 lexicon (no production caller); the screen reads the
# completion grammar (_find_write_claim).
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

# R14-9(b) (cq-323c8974fa02, ruled 2026-09-21): the lexicon above fired on ANY
# occurrence of a ruled verb, so a DESCRIPTIVE use ("the nine staged prompts",
# "staged cards not visually updating") or a phrase QUOTED from the user's own
# message ("into a staged Cowork session") counted as a phantom write -- four
# 9/20-9/21 founder-DM hits, zero of them phantoms, each one resetting the D-309
# observe-week clock. The VERB SET stays exactly the ruled list; what changed is the
# GRAMMAR around it: only a COMPLETION claim counts --
#   first_person  "I staged ..." / "I've filed ..." / "I have just created ..."
#   first_obj     "I've got it staged" / "I have them queued"
#   done          a sentence that opens "Done." / "Done -- " / "All set,"
#   initial       a sentence that opens with the participle + an object or a stop
#                 ("Staged cq-...", "Updated your calendar", "Deleted.")
#   receipt       a short line-initial noun phrase + participle + "(" or ":" -- the
#                 telegraphic receipt the 9/15 21:17:52 phantom mimicked
#                 ("Code-session prompt staged (AUTO-GENERATED DRAFT ...):")
#   pronoun       "it's created" / "they're queued" / "that has been updated"
#   quantifier    "All three locked in:" / "both are queued."
#   arrow         "-> staged" / "=> created"
#   live          "is now live" / "it's live"
#   perfect       "has now been staged" / "is just created" (completion adverb req.)
#   your          "your calendar is updated" / "your note is now queued"
# with a HABITUAL / STATE bail ("is updated weekly / every Monday / by Justin / in
# QBO / with the IRS / behind the payroll run / until October").
# Measured on the five verbatim incident replies (Slack DM D0B4CTD3B09, read
# 2026-09-23): the four 9/20-9/21 replies read ZERO, the 9/15 phantom reads ONE.
#
# Code #14 D-051 remediation (honesty-rails-1/2/4/8, integration-tests-1,
# redos-slack-surfaces-1):
#   * NO ECHO MASK. The first cut blanked every reply run sharing a token trigram
#     with the user's words, which silenced the rail's own incident shapes ("Is the
#     kickoff prompt staged?" -> "Kickoff prompt staged (draft): ..."; "are all three
#     locked in?" -> "All three locked in:") and, because it blanked to WHITESPACE,
#     rebuilt the space runs format_reply had collapsed (the quantifier lookahead
#     was quadratic on them). The grammar now runs on the UNMASKED reply; a hit is
#     suppressed ONLY when its own span lies entirely inside a QUOTED span ("..." /
#     curly quotes / backticks) whose inner text the user typed and that carries >= 3
#     non-id tokens. A first-person claim is NEVER suppressed; a bare id span is
#     never an echo. Nothing is blanked, so no offset / whitespace artefacts.
#   * RECALL: a sentence may open with an emoji / :shortcode: or an interjection
#     ("Got it -- staged.", "Okay, created the task", "✅ Staged cq-..."); 'initial'
#     also stops on end-of-text / emoji / dash, takes a coordinated lead participle
#     ("Approved and staged."), and a SHORT for/with/from object that ends the
#     sentence ("Queued for your review.", "Staged for Monday's menu."); 'pronoun'
#     takes "it's been" / "they've been"; a short receipt line ends on '.' / emoji
#     ("Prompt staged.", "Task created ✅"); a sentence-initial "All staged." counts.
#   * PRECISION: object words end on a word boundary ("Staged items appear ..." no
#     longer reads 'it'); a digit object must be a COUNT ("Staged 3 prompts"), never
#     a date ("Updated 9/12:", "Filed 4/15/2025"); receipt skips metadata heads
#     (last / date / originally / recently / first / when), 'team' / 'what' and a
#     capitalized proper-name subject ("Tasks Justin created:").
#   * D-171: every whitespace run that precedes a literal is POSSESSIVE, and the
#     quantifier continuation reads `(?<=[ \t])and` after an atomic space run (the
#     old `[ \t]*` + `\s+and` pair was O(n^2) -- 7 s at 40k). Re-timed at 40k.
# Round 2 (re-review F1-R2 / honesty-rails-8): the for/with/from object bails on a
# by-agent / habitual word (_WC_OBJ_NOT_HAB) and a participle + ':' / '(' + a DATE
# or "(per ...)" is a metadata label (_WC_LABEL_COLON / _WC_LABEL_PAREN).
_WC_V = r"(?P<v>staged|queued|locked\s+in|canonicali[sz]ed|filed|created|updated|deleted)"
_WC_V2 = r"(?P<v2>staged|queued|locked\s+in|canonicali[sz]ed|filed|created|updated|deleted)"
_WC_A = r"['’]"
_WC_NOT_HAB = (r"(?!\s++(?:weekly|daily|monthly|nightly|hourly|every|each|automatically|"
               r"regularly|whenever|when|by|under|on|in|at|from|for|as|per|with|behind|until)\b)")
# A pictograph (✅ ✔ ☑ 👍 🎉 ...) with an optional variation selector, or a Slack
# :shortcode:. Bounded single-char class; never a run.
_WC_EMOJI = r"(?:[\u2600-\u27BF\u2B00-\u2BFF\U0001F000-\U0001FAFF]\uFE0F?)"
_WC_MARK = r"(?:" + _WC_EMOJI + r"|:[a-z0-9_+-]{1,30}:)"
_WC_INTERJ = (r"(?:got\s++it|okay|ok|yep|yes|yeah|yup|sure(?:\s++thing)?|alright|all\s++right|perfect|"
              r"great|roger(?:\s++that)?|on\s++it|sounds\s++good|will\s++do|no\s++problem|absolutely|"
              r"noted|understood)")
_WC_SEP = r"[ \t]*+(?:[,!.:;]|-{1,2}|[—–])[ \t]*+"
_WC_START = (r"(?:^|(?<=[.!?]\s))[ \t]*+(?:[-*•][ \t]++)?(?:" + _WC_MARK + r"[ \t]*+){0,3}\*?"
             r"(?:" + _WC_INTERJ + _WC_SEP + r"\*?)?")
# A number after a sentence-initial participle is a COUNT only when a word follows
# that is neither a month nor a time unit ("Updated 2 hours ago" is a timestamp).
_WC_COUNT = (r"\d{1,3}[ \t]++(?!(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
             r"|(?:sec|second|min|minute|hour|hr|day|week|wk|month|year|yr)s?\b)[a-z]")
_WC_OBJ = (r"(?:(?:the|a|an|it|them|this|that|these|those|all|both|each|every|your|my|its|their|to|into)\b"
           r"|cq-|dw-|[`*_]|" + _WC_COUNT + r")")
# Code #14 D-051 round 2 (F1-R2): the short for/with/from object of 'initial' bails on
# a BY-AGENT or HABITUAL word anywhere in its <= 4 tokens, the same bail _WC_NOT_HAB
# gives the pronoun / your / perfect forms -- "Queued for review by Justin.",
# "Updated from the bank feed nightly.", "Filed with the IRS every April." describe a
# teammate's or a recurring action, not Cora's. Bounded lookahead (<= 3 possessive
# tokens), so linear.
_WC_HAB_WORD = (r"(?:by|weekly|daily|monthly|nightly|hourly|annually|yearly|quarterly|every|each|"
                r"automatically|regularly|whenever)")
_WC_OBJ_NOT_HAB = r"(?!(?:[\w'’&-]++[ \t]++){0,3}" + _WC_HAB_WORD + r"\b)"
# Code #14 D-051 round 2 (honesty-rails-8): a sentence-initial participle + ':' + a
# DATE (or "(per ...)", or a dated bullet on the next line) is a METADATA LABEL --
# "Updated: 9/12 (per the doc footer)", "*Filed:* 4/15/2025", "Created: March 2025",
# "- Updated: 2026-09-12", "Filed (per the county record): 4/15/2025". A date must END
# on a non-word char, so a staged prompt's dated FILENAME ("Staged: 2026-09-16_fndr_
# cora-code-prompt.md") is still a claim. Receipts are untouched (a receipt label IS
# the 9/15 phantom shape).
#
# Code #14 D-051 round 3 (R2-A2): a ':' label bails only when the date is the WHOLE
# value -- after the date (or the "(per ...)" note) only an optional parenthetical, a
# closing bold marker and the END OF THE LINE may follow (_WC_LABEL_VALUE_END). Round 2
# bailed on any value that merely OPENED with a date, which silenced "Created: 9/24 at
# 2pm with Justin -- the invite is on both calendars.", "Filed: 9/23 Cox invoice to the
# Receipts & Invoices Inbox." and "Updated: 2026 budget tab now shows the new totals."
# (the bare-year alternative matched any year). A bare year now bails only at the end
# of the line or after a month ("March 2025", "March 1, 2025"). Fails toward FIRING: a
# label with anything after its date ("Updated: 9/12 3:14pm", "Created: 2024-03-01 by
# Justin") now counts -- accepted over-trips.
_WC_DATE = (r"(?:\d{1,4}[/.-]\d{1,2}(?:[/.-]\d{2,4})?|(?:19|20)\d{2}"
            r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]{0,6}\.?[ \t]++\d{1,4}(?:st|nd|rd|th)?"
            r"(?:,?[ \t]++(?:19|20)\d{2})?)"
            r"(?![\w/-])")
_WC_LABEL_VALUE_END = r"[ \t]*+(?:\([^()\n]{0,80}\)[ \t]*+)?\*{0,2}[ \t]*+$"
_WC_LABEL_COLON = (r":(?![ \t]*+\*{0,2}[ \t]*+(?:\n[ \t]*+(?:[-*•][ \t]++)?)?(?:" + _WC_DATE
                   + r"|\(per\b[^()\n]{0,80}\)[ \t]*+(?:" + _WC_DATE + r")?)" + _WC_LABEL_VALUE_END + r")")
_WC_LABEL_PAREN = (r"\((?!(?:(?:per|last)\b[^()\n]{0,80}\)[ \t]*+\*{0,2}:[ \t]*+\*{0,2}[ \t]*+" + _WC_DATE
                   + r"|" + _WC_DATE + r"))")
_WRITE_CLAIM_FORMS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(rx, re.IGNORECASE | re.MULTILINE)) for label, rx in (
        ("first_person",
         r"\bI(?:\s+have|\s*" + _WC_A + r"ve)?(?:\s+(?:just|now|also|already|successfully|"
         r"gone\s+ahead\s+and|went\s+ahead\s+and|went\s+and))?\s+" + _WC_V + r"\b"),
        ("first_obj",
         r"\bI(?:\s+have|\s*" + _WC_A + r"ve)\s+(?:got\s+)?(?:it|them|that|this|those|these|both|"
         r"everything|all\s+of\s+them|all\s+(?:two|three|four|five|six|seven|eight|nine|ten|\d{1,2}))"
         r"\s+" + _WC_V + r"\b"),
        ("done",
         _WC_START + r"(?P<v>done|all\s++set)\*?[ \t]*+(?:[.!,:;—–]|-{1,2}(?=\s)|\Z|" + _WC_EMOJI + r")"),
        ("initial",
         _WC_START + r"(?:[a-z]{2,20}ed[ \t]++(?:and|&)[ \t]++)?" + _WC_V + r"\*?(?:"
         r"[ \t]*+(?:[.!]|" + _WC_LABEL_COLON + r"|" + _WC_LABEL_PAREN + r"|\Z|" + _WC_EMOJI
         + r"|[—–]|-{1,2}(?=\s))"
         r"|[ \t]++" + _WC_OBJ
         + r"|[ \t]++(?:for|with|from)[ \t]++" + _WC_OBJ_NOT_HAB
         + r"(?:[\w'’&-]++[ \t]*+){1,4}?(?:[.!]|\Z|" + _WC_EMOJI + r"))"),
        ("pronoun",
         r"\b(?:it|that|this|they|those|these|everything)(?:\s*+" + _WC_A + r"(?:s|ve)\s++been|\s*+"
         + _WC_A + r"(?:s|re)|\s++(?:is|are|has\s++been|have\s++been))"
         r"(?:\s++(?:now|just|all|successfully|officially))?\s++" + _WC_V + r"\b" + _WC_NOT_HAB),
        ("quantifier",
         r"\b(?:both|each\s++one|all\s++(?:two|three|four|five|six|seven|eight|nine|ten|\d{1,2}|of\s++them))"
         r"(?:\s++(?:are|have\s++been|got))?(?:\s++(?:now|just|successfully))?\s++" + _WC_V
         + r"(?=[ \t]*+(?:[.!,:;)—–]|$|" + _WC_EMOJI + r"|(?<=[ \t])and\b|(?<=[ \t])-))"
         r"|" + _WC_START + r"all\s++" + _WC_V2 + r"(?=[ \t]*+(?:[.!,:;]|\Z|" + _WC_EMOJI + r"))"),
        ("arrow", r"(?:→|->|=>)[ \t]*(?:[\w'-]+[ \t]+){0,3}" + _WC_V + r"\b"),
        ("live",
         r"\b(?:is|are)\s+now\s+(?P<v>live)\b|\b(?:it|that|this)(?:\s*" + _WC_A + r"s|\s+is)\s+"
         r"(?:now\s+)?(?P<v2>live)\b"),
        ("perfect",
         r"\b(?:has|have)\s+(?:now|just|successfully)\s+been\s+" + _WC_V + r"\b" + _WC_NOT_HAB
         + r"|\b(?:has|have)\s+been\s+(?:now|just|successfully)\s+(?P<v2>staged|queued|filed|created|"
         r"updated|deleted|canonicali[sz]ed)\b" + _WC_NOT_HAB
         + r"|\b(?:is|are)\s+(?:now|just)\s+(?P<v3>staged|queued|filed|created|updated|deleted|"
         r"canonicali[sz]ed)\b" + _WC_NOT_HAB),
        ("your",
         r"\byour\s+(?:[\w-]+\s+){0,2}(?:is|are)\s+(?:now\s+|all\s+)?" + _WC_V + r"\b" + _WC_NOT_HAB),
    ))
#: Forms whose subject is the bot itself -- NEVER suppressed as an echo.
_WC_FIRST_PERSON_FORMS = frozenset({"first_person", "first_obj"})
# The receipt form needs a word-level check a regex cannot express cheaply: the
# token right before the participle must not be a 2nd/3rd-person subject ("the
# prompts you've staged (see above)" describes Harrison's action, not Cora's), a
# metadata head ("Last updated:", "Date created:") or a proper name ("Tasks Justin
# created:"). The `short` group is a receipt SENTENCE of <= 2 tokens at the start
# of a line that ends on '.' / '!' / an emoji ("Prompt staged.", "Task created ✅").
_WC_RECEIPT_RE = re.compile(
    r"^[ \t]*+(?:[-*•][ \t]++)?(?:" + _WC_MARK + r"[ \t]*+){0,3}\*?(?P<np>(?:[\w`'*-]++[ \t]++){1,4})"
    + _WC_V + r"\*?(?:[ \t]*+(?:[(:]|" + _WC_EMOJI + r")|[ \t]++for[ \t]++(?:cq|dw)-[0-9a-f]{12}\b"
    r"|(?P<short>[ \t]*+(?:(?:[.!]|" + _WC_MARK + r"){1,3}(?=\s)|(?:[.!]|" + _WC_MARK
    + r"){0,3}[ \t]*+$)))",
    re.IGNORECASE | re.MULTILINE)
_WC_RECEIPT_NOT_SUBJECT = frozenset({
    "you", "you've", "youve", "you'd", "you're", "they", "they've", "theyve", "we", "we've",
    "he", "she", "harrison", "has", "have", "had", "was", "were", "be", "been", "is", "are",
    "not", "never", "i", "i've", "ive",
    # Code #14 D-051 (honesty-rails-8): metadata heads and non-bot subjects.
    "last", "date", "originally", "recently", "first", "when", "team", "what", "who", "which",
    "everyone", "someone", "anyone", "nobody", "nothing", "none", "people", "staff"})
# A capitalized, non-initial last token is a proper-name SUBJECT ("Tasks Justin
# created:") unless it is one of the receipt nouns a title-cased receipt uses.
_WC_RECEIPT_NOUNS = frozenset({
    "prompt", "prompts", "task", "tasks", "draft", "drafts", "note", "notes", "card", "cards", "deal",
    "deals", "item", "items", "session", "sessions", "invoice", "invoices", "receipt", "receipts",
    "event", "events", "entry", "entries", "ticket", "tickets", "job", "jobs", "file", "files", "doc",
    "docs", "document", "documents", "row", "rows", "brief", "report", "page", "post", "email",
    "emails", "message", "messages", "reminder", "reminders", "calendar", "meeting", "meetings",
    "kickoff", "record", "records", "sheet", "list", "folder", "project", "projects", "contact",
    "contacts", "order", "orders", "comment", "comments", "subtask", "subtasks", "category",
    "categories", "template", "request", "requests"})
_WC_TOKEN_RE = re.compile(r"[A-Za-z0-9'’]+")
_WC_ID_TOKEN_RE = re.compile(r"\b(?:cq|dw)-[0-9a-f]{12}\b", re.IGNORECASE)
_WC_QUOTED_RE = re.compile(r"\"([^\"\n]{3,200})\"|“([^”\n]{3,200})”|`([^`\n]{3,200})`")
_WC_ECHO_MAX_CHARS = 8000
_WC_ECHO_MIN_TOKENS = 3


def _echo_norm(s: str) -> str:
    return " ".join(html.unescape(str(s or "")).lower().replace("’", "'").split())


def _user_quote_spans(text: str, user_texts: Any) -> list[tuple[int, int]]:
    """(start, end) of every QUOTED span in *text* ("..." / curly / backticks) whose
    inner text the user typed (this message + the last few user turns) and which
    carries >= 3 tokens once cq-/dw- ids are removed -- a bare id is never an echo.
    Offsets are the INNER text (quote marks excluded). Linear: one bounded regex
    pass plus substring checks against a capped user string."""
    joined = _echo_norm(" ".join(str(u or "") for u in (user_texts or ()) if u)[:_WC_ECHO_MAX_CHARS])
    if not joined:
        return []
    spans: list[tuple[int, int]] = []
    for m in _WC_QUOTED_RE.finditer(text):
        gi = next(i for i, g in enumerate(m.groups(), start=1) if g is not None)
        inner = m.group(gi)
        if len(_WC_TOKEN_RE.findall(_WC_ID_TOKEN_RE.sub(" ", inner))) < _WC_ECHO_MIN_TOKENS:
            continue
        norm = _echo_norm(inner)
        if norm and norm in joined:
            spans.append((m.start(gi), m.end(gi)))
    return spans


_PERSON_NAME_TTL_S = 60.0
_PERSON_NAME_CACHE: dict[str, Any] = {"at": None, "names": frozenset()}


def _person_name_tokens() -> frozenset[str]:
    """Lower-cased name tokens of every org-roles person (60s cache, the registry's own
    TTL). Fail-soft: an unreadable registry yields the empty set, so a title-case
    receipt is judged by shape alone -- the rail errs toward FIRING, never silence."""
    now = time.monotonic()
    at = _PERSON_NAME_CACHE["at"]
    if at is not None and now - at < _PERSON_NAME_TTL_S:
        return _PERSON_NAME_CACHE["names"]
    try:
        from . import org_roles  # lazy: the registry loader reads data/maps
        names = frozenset(
            tok.lower() for r in org_roles.all_roles()
            for tok in _WC_TOKEN_RE.findall(str(getattr(r, "name", "") or "")) if len(tok) > 1)
    except Exception:  # noqa: BLE001
        names = frozenset()
    _PERSON_NAME_CACHE.update({"at": now, "names": names - _WC_RECEIPT_NOUNS})
    return _PERSON_NAME_CACHE["names"]


def _is_person_subject(np_toks: list[str], raw_last: str, last: str) -> bool:
    """Code #14 D-051 round 2 (F1-R3): a receipt's last np token names a TEAMMATE only
    when it is a known person's name ("Hannah Grant updated:", "Justin created: the
    deck") or follows a lower-cased PLURAL noun ("Tasks Justin created:", "Deals Tommy
    updated (last 7 days):"). Title case alone never skips -- round 1 skipped every
    capitalized last token outside a hand-listed noun set, which silenced 8 of 11
    mimicked receipts ("**Calendar Invite Created:**", "Inventory Adjustment staged:",
    "Slack DM queued (draft):", "Kroger PO updated:")."""
    if not raw_last[:1].isupper() or raw_last.isupper() or last in _WC_RECEIPT_NOUNS:
        return False            # lower-case, an ACRONYM (DM / PO / SKU), or a receipt noun
    if last in _person_name_tokens():
        return True
    if len(np_toks) >= 2:
        prev = np_toks[-2].strip("`*'").lower()
        if len(prev) > 3 and prev.endswith("s") and not prev.endswith("ss") and prev.isalpha():
            return True
    return False


def _receipt_subject_ok(m: re.Match) -> bool:
    # A bare bullet char is not a noun phrase (round 2, honesty-rails-8): the np class
    # admits '-' / '*', so "- Created: 2024-03-01" backtracked into np='-' and read the
    # dated metadata bullet as a receipt once 'initial' stopped claiming it.
    np_toks = [t for t in m.group("np").split() if any(c.isalnum() for c in t)]
    if not np_toks:
        return False
    if m.group("short") is not None and len(np_toks) > 2:
        return False
    raw_last = np_toks[-1].strip("`*'")
    last = raw_last.lower().replace("’", "'")
    if last in _WC_RECEIPT_NOT_SUBJECT:
        return False
    if _is_person_subject(np_toks, raw_last, last):
        return False   # a teammate subject ("Tasks Justin created:", "Hannah Grant updated:")
    if (m.group("short") is not None and len(np_toks) == 1 and raw_last[:1].isupper()
            and last not in _WC_RECEIPT_NOUNS):
        return False   # "Justin created." describes a teammate, "Prompt staged." is a receipt
    return True


def _verb_of(m: re.Match) -> str:
    return next((g for g in (m.groupdict().get(k) for k in ("v", "v2", "v3")) if g), m.group(0))


def _inside(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(qs <= start and end <= qe for qs, qe in spans)


def _find_write_claim_span(text: str, user_texts: Any = (),
                           spans: list[tuple[int, int]] | None = None,
                           want: tuple[str, str] | None = None,
                           pos: int = 0) -> tuple[int, int, str, str] | None:
    """(start, end, verb, form) of the FIRST completion claim in *text* at or after
    ``pos``, else None.

    A hit whose span lies entirely inside a user-typed quoted span (``spans``, or
    computed from ``user_texts``) is skipped unless it is first-person. ``want`` =
    (verb, form): return the first unsuppressed hit of THAT form and verb (the S3
    ledger locates the claim that actually fired), falling back to the first hit.
    ``pos`` searches with ``finditer(text, pos)``, so ``^`` / look-behind anchors keep
    their whole-text meaning."""
    if spans is None:
        spans = _user_quote_spans(text, user_texts) if user_texts else []
    best: tuple[int, int, str, str] | None = None
    for label, rx in _WRITE_CLAIM_FORMS:
        if want is not None and label != want[1]:
            continue
        for m in rx.finditer(text, pos):
            if best is not None and m.start() >= best[0]:
                break
            if spans and label not in _WC_FIRST_PERSON_FORMS and _inside(spans, m.start(), m.end()):
                continue
            verb = _verb_of(m)
            if want is not None and " ".join(verb.split()).lower() != want[0]:
                continue
            best = (m.start(), m.end(), verb, label)
            break
    if want is None or want[1] == "receipt":
        for m in _WC_RECEIPT_RE.finditer(text, pos):
            if best is not None and m.start() >= best[0]:
                break
            if not _receipt_subject_ok(m):
                continue
            if spans and _inside(spans, m.start(), m.end()):
                continue
            if want is not None and " ".join(m.group("v").split()).lower() != want[0]:
                continue
            best = (m.start(), m.end(), m.group("v"), "receipt")
            break
    if best is None:
        return _find_write_claim_span(text, spans=spans, pos=pos) if want is not None else None
    return best[0], best[1], " ".join(best[2].split()).lower(), best[3]


_WC_PREFER_MAX_HOPS = 16
_WC_ECHO_TAIL_TOKENS = 3
_WC_ECHO_TAIL_BREAK_RE = re.compile(r"[.!?;\n]")


def _is_echo_hit(text: str, hit: tuple[int, int, str, str], spans: list[tuple[int, int]],
                 joined_user: str) -> bool:
    """A hit whose words the USER wrote: inside a user-typed quoted span (first-person
    included -- it still COUNTS, it is just not the likeliest phantom), or an unquoted
    run -- the hit plus up to three following tokens of its sentence, >= 2 tokens in
    all -- that appears verbatim in the user's own text. The tail is what tells the
    pasted "Task created: Pay the invoice" from a later "Task created: Reorder Kroger
    cases" (a receipt match ends at its ':')."""
    if spans and _inside(spans, hit[0], hit[1]):
        return True
    if not joined_user:
        return False
    tail = text[hit[1]:hit[1] + 80]
    cut = _WC_ECHO_TAIL_BREAK_RE.search(tail)
    if cut:
        tail = tail[:cut.start()]
    toks = list(_WC_TOKEN_RE.finditer(tail))[:_WC_ECHO_TAIL_TOKENS]
    norm = _echo_norm(text[hit[0]:hit[1]] + (tail[:toks[-1].end()] if toks else ""))
    return len(_WC_TOKEN_RE.findall(norm)) >= 2 and norm in joined_user


def _iter_write_claim_hits(text: str, spans: list[tuple[int, int]]
                           ) -> Iterator[tuple[int, int, int, str, str]]:
    """Every unsuppressed completion-claim hit of *text* as (start, form index, end,
    verb, form), in (start, form order) order -- the per-form rules of
    _find_write_claim_span, so the FIRST item is exactly its hit. Code #14 D-051 round 3
    (R2-A3): one lazy finditer per form (plus the receipt) merged by heapq.merge, so each
    form's regex scans the text AT MOST ONCE however many hits a caller walks -- round
    2's preference re-ran every form from each hop to the end of the reply (about 34
    full grammar scans on an echo prefix + a long non-matching tail: 1.6 s at 40k)."""
    def _form(idx: int, label: str, rx: re.Pattern[str]) -> Iterator[tuple[int, int, int, str, str]]:
        for m in rx.finditer(text):
            if spans and label not in _WC_FIRST_PERSON_FORMS and _inside(spans, m.start(), m.end()):
                continue
            yield m.start(), idx, m.end(), " ".join(_verb_of(m).split()).lower(), label

    def _receipt(idx: int) -> Iterator[tuple[int, int, int, str, str]]:
        for m in _WC_RECEIPT_RE.finditer(text):
            if not _receipt_subject_ok(m) or (spans and _inside(spans, m.start(), m.end())):
                continue
            yield m.start(), idx, m.end(), " ".join(m.group("v").split()).lower(), "receipt"

    gens = [_form(i, label, rx) for i, (label, rx) in enumerate(_WRITE_CLAIM_FORMS)]
    gens.append(_receipt(len(_WRITE_CLAIM_FORMS)))
    return heapq.merge(*gens)


def _walk_write_claims(text: str, users: Any, spans: list[tuple[int, int]],
                       want: tuple[str, str] | None = None
                       ) -> tuple[tuple[int, int, str, str] | None, tuple[int, int, str, str] | None,
                                  tuple[int, int, str, str] | None]:
    """ONE pass over the merged hits -> (preferred, first hit of ``want``, first hit).
    preferred = the first hit whose words the user did not write, checked over at most
    1 + _WC_PREFER_MAX_HOPS distinct hit starts, else the first hit. The walk stops as
    soon as both answers are known."""
    joined = _echo_norm(" ".join(str(u) for u in (users or ()) if u)[:_WC_ECHO_MAX_CHARS]) if users else ""
    first = pref = wanted = None
    pref_done = False
    checked = 0
    last_start = -1
    want_t = (str(want[0]), str(want[1])) if want is not None else None
    for start, _idx, end, verb, label in _iter_write_claim_hits(text, spans):
        hit = (start, end, verb, label)
        if first is None:
            first = hit
        if want_t is not None and wanted is None and (verb, label) == want_t:
            wanted = hit
        if not pref_done and start > last_start:      # one check per start (a later form at the same start is the same claim)
            last_start = start
            if not users or not _is_echo_hit(text, hit, spans, joined):
                pref, pref_done = hit, True
            else:
                checked += 1
                if checked > _WC_PREFER_MAX_HOPS:
                    pref, pref_done = first, True
        if pref_done and (want_t is None or wanted is not None):
            break
    return (pref if pref_done else first), wanted, first


def _preferred_write_claim_span(text: str, users: Any = (),
                                spans: list[tuple[int, int]] | None = None
                                ) -> tuple[int, int, str, str] | None:
    """The claim the ONE ledger row / WARN of a reply records (Code #14 D-051 round 2,
    honesty-rails-10 / redos-slack-surfaces-6): the FIRST hit whose words the user did
    not write, falling back to the first hit. The FIRE decision is unchanged -- any hit
    fires -- but a reply that opens by echoing the user ("Task created: Pay the
    invoice -- that line is the portal notice you pasted", 'You said "I filed the Cox
    invoice ..."') must not hide the real phantom later in it ("I updated the Kroger
    reorder sheet", "all three queued"). Round 3 (R2-A3): a SINGLE pass
    (_walk_write_claims) -- each form scans the reply at most once, at most
    1 + _WC_PREFER_MAX_HOPS hits are echo-checked."""
    if spans is None:
        spans = _user_quote_spans(text, users) if users else []
    return _walk_write_claims(text, users, spans)[0]


def _find_write_claim(text: str, user_texts: Any = ()) -> tuple[str, str] | None:
    """(verb, form) of the FIRST completion claim in *text* that is not a quoted
    echo of the user's own words, else None."""
    hit = _find_write_claim_span(text, user_texts)
    return (hit[2], hit[3]) if hit else None


# -- S3 (cq-439d89a84de4): the rail ledger -- a phantom hit adjudicable from disk --
# The 9/17 hits (phrase='filed' in a teammate DM, 'updated' in #lex-website,
# 'created' in #hjr-travel-booking) were UNADJUDICABLE: the WARN named only the
# phrase and the reply text was nowhere on disk, so the observe-week count could
# not be split into real phantoms and noise. Every firing line of the two seam
# screens now appends ONE row to data/state/phantom-write-claims.jsonl carrying a
# scrubbed +/-60-char snippet around the hit, response_chars and the tool count,
# and the WARN gains response_chars / tool_use / ref (the row id) -- NOT the
# snippet: bot logs are backed up to Drive and sessions quote WARN lines into
# captures the KB ingests, so reply text in the log line would be a propagation
# path (D-145). The ledger is the ADJUDICATION record; the 7d counter stays the
# log scan (a ledger that starts empty at deploy would read a false "clean").
#
# THE SNIPPET: the FULL reply is scrubbed first (mojibake repair, a SILENT sentinel
# delete -- never scrub_write_sentinels, which logs a sentinel-egress-leak line and
# would double-count that rail -- API-token-shape redaction (Code #15 S1, BEFORE the
# id pass), bare-URL/GID/long-id redaction, banking-identifier
# redaction), then the hit is re-located and windowed, because the banking redactor
# keys on a cue up to 40 chars from the value and a window cut first could strand a
# bare routing number. WITHHELD entirely ("[LEX — withheld]") in LEX scope, on a
# Tier-2 grant turn, and in a non-founder custodian DM; in the FOUNDER DM (a PHI-
# eligible context by carve-out) a snippet is kept only if a fail-closed content
# belt passes. No scope context at all = withheld. Rows are written only in the
# bot process (arm_rail_ledger, called at startup) and never under CORA_EVAL_MODE.
PHANTOM_CLAIMS_LEDGER = _REPO_ROOT / "data" / "state" / "phantom-write-claims.jsonl"
RAIL_SNIPPET_WITHHELD_LEX = "[LEX — withheld]"
RAIL_SNIPPET_WITHHELD_GRANT = "[private retrieval — withheld]"
RAIL_SNIPPET_WITHHELD_PHI = "[PHI screen — withheld]"
# Code #14 D-051 (honesty-rails-11): a turn built on OWNER-PRIVATE unstripped content
# -- the asker's own mailbox (D-043 Tier-1 unstripped), personal notes (D-049) --
# withholds exactly like the Tier-2 grant turn of the same data class.
RAIL_SNIPPET_WITHHELD_PERSONAL = "[owner-private content — withheld]"
RAIL_SNIPPET_NO_CONTEXT = "[no scope context — withheld]"
RAIL_SNIPPET_UNAVAILABLE = "[snippet unavailable]"
_SNIPPET_RADIUS = 60
_SNIPPET_CAP = 200
_RAIL_LEDGER_LOCK = threading.Lock()
_RAIL_LEDGER_ARMED = False
_RAIL_REF_COUNTER = [0]


def arm_rail_ledger() -> None:
    """Called ONCE by main at bot startup: rows are written only by the bot process
    (a script importing this module -- the evals, the missed-message replay -- never
    writes the live ledger)."""
    global _RAIL_LEDGER_ARMED
    _RAIL_LEDGER_ARMED = True


def _rail_ledger_on() -> bool:
    return _RAIL_LEDGER_ARMED and os.environ.get("CORA_EVAL_MODE", "") != "1"


def _rail_ref(*parts: object) -> str:
    with _RAIL_LEDGER_LOCK:
        _RAIL_REF_COUNTER[0] += 1
        n = _RAIL_REF_COUNTER[0]
    raw = "|".join(str(p) for p in parts) + f"|{os.getpid()}|{n}|{time.time_ns()}"
    return "pr-" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:10]


def _append_rail_row(row: dict) -> bool:
    """Fail-soft append (never raises, never blocks the reply)."""
    if not _rail_ledger_on():
        return False
    path = Path(PHANTOM_CLAIMS_LEDGER)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _RAIL_LEDGER_LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        return True
    except Exception:  # noqa: BLE001
        log.warning("rail ledger append failed (non-fatal)", exc_info=True)
        return False


def _scrub_for_snippet(text: str) -> str:
    t = repair_mojibake(text)
    t = _SENTINEL_ANY_RE.sub("", t)                 # SILENT: never log a sentinel line here
    # Code #15 S1 (cq-d9d0c92cc797): API-token shapes FIRST -- before the long-id
    # redaction below, which strips a token's digit segments and would leave a
    # shape the token regex no longer matches (the secret tail surviving), the
    # same ordering rule as tokens-before-banking.
    from .secret_tokens import redact_secret_tokens  # lazy
    t, _n = redact_secret_tokens(t)
    t = redact_links_and_ids(t)
    from .banking_identifiers import redact_banking_identifiers  # lazy
    t, _n = redact_banking_identifiers(t)
    return _LINK_TOKEN_RE.sub(" ", t)


def _founder_belt_passes(snippet: str) -> bool:
    """Fail-closed PHI belt for a snippet from the FOUNDER DM (a custodian context):
    every predicate must read clean, and the LEX scrub must change nothing."""
    try:
        from . import phi_guard  # lazy
        if (phi_guard.is_any_phi(snippet) or phi_guard.is_lex_program_context(snippet)
                or phi_guard.non_lex_phi_backstop_trips(snippet)):
            return False
        return phi_guard.scrub_lex_phi(snippet) == snippet
    except Exception:  # noqa: BLE001 -- an unevaluable belt withholds
        return False


class _SnippetMemo:
    """ONE scrub (and ONE full-text founder belt) per screen call, shared by every
    ledger row that call writes (Code #14 D-051 redos-slack-surfaces-2 /
    honesty-rails-12: the fabricated-id loop used to re-scrub the WHOLE reply once
    per unknown id -- O(ids x length) on the bolt worker)."""

    __slots__ = ("text", "_clean", "_belt")

    def __init__(self, text: str) -> None:
        self.text = text
        self._clean: str | None = None
        self._belt: bool | None = None

    def clean(self) -> str:
        if self._clean is None:
            self._clean = _scrub_for_snippet(self.text)
        return self._clean

    def full_belt_passes(self) -> bool:
        """honesty-rails-3: the founder-DM belt reads the FULL scrubbed reply BEFORE
        any window is cut -- its cues (Lexington / DDD / AHCCCS, care nouns, the
        120-char cue proximity) can sit far outside +/-60 chars of the hit, the same
        'windowing first strands the cue' class the banking scrub order closes."""
        if self._belt is None:
            try:
                self._belt = _founder_belt_passes(" ".join(self.clean().split()))
            except Exception:  # noqa: BLE001 -- an unevaluable belt withholds
                self._belt = False
        return self._belt


def _rail_snippet(text: str, locate: Callable[[str], tuple[int, int] | None],
                  rail_context: dict | None, memo: "_SnippetMemo | None" = None) -> str:
    """The scrubbed +/-60-char window around the hit, or a WITHHELD marker. Never
    raises; fails CLOSED (a marker, never the raw text). In the founder DM the
    content belt runs on the FULL scrubbed reply first (any trip anywhere withholds),
    then again on the window."""
    ctx = rail_context if isinstance(rail_context, dict) else None
    if ctx is None:
        return RAIL_SNIPPET_NO_CONTEXT
    withheld = ctx.get("snippet_withheld")
    if withheld:
        return str(withheld)
    memo = memo if memo is not None and memo.text is text else _SnippetMemo(text)
    try:
        clean = memo.clean()
        if ctx.get("founder_belt") and not memo.full_belt_passes():
            return RAIL_SNIPPET_WITHHELD_PHI
        span = locate(clean)
        if span is None:
            window = clean[:120]
        else:
            lo = max(0, span[0] - _SNIPPET_RADIUS)
            hi = min(len(clean), span[1] + _SNIPPET_RADIUS)
            window = ("…" if lo > 0 else "") + clean[lo:hi] + ("…" if hi < len(clean) else "")
        snippet = " ".join(window.split())[:_SNIPPET_CAP]
    except Exception:  # noqa: BLE001
        log.warning("rail snippet failed (non-fatal)", exc_info=True)
        return RAIL_SNIPPET_UNAVAILABLE
    if ctx.get("founder_belt") and not _founder_belt_passes(snippet):
        return RAIL_SNIPPET_WITHHELD_PHI
    return snippet


def _record_rail_hit(*, rail: str, kind: str, phrase: str, text: str,
                     locate: Callable[[str], tuple[int, int] | None], mode: str,
                     channel_name: str, user_id: str, tool_use_count: int | None,
                     rail_context: dict | None, form: str = "",
                     memo: "_SnippetMemo | None" = None) -> str:
    """Append one ledger row for one FIRING line; return its ref ('' when no row)."""
    if not _rail_ledger_on():
        return ""
    ctx = rail_context if isinstance(rail_context, dict) else {}
    ref = _rail_ref(rail, kind, phrase, channel_name, user_id)
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "rail": rail, "kind": kind, "phrase": phrase,
        "snippet": _rail_snippet(text, locate, rail_context, memo),
        "mode": mode, "channel": channel_name or "", "channel_id": str(ctx.get("channel_id") or ""),
        "user": user_id or "", "entity": str(ctx.get("entity") or ""),
        "response_chars": len(text), "tool_use_count": tool_use_count,
        "ref": ref, "pid": os.getpid(),
    }
    if form:
        row["form"] = form
    return ref if _append_rail_row(row) else ""


def _locate_text(needle: str) -> Callable[[str], tuple[int, int] | None]:
    low = str(needle or "").lower()

    def _loc(clean: str) -> tuple[int, int] | None:
        i = clean.lower().find(low) if low else -1
        return (i, i + len(low)) if i >= 0 else None
    return _loc


def _locate_regex(rx: re.Pattern[str]) -> Callable[[str], tuple[int, int] | None]:
    def _loc(clean: str) -> tuple[int, int] | None:
        m = rx.search(clean)
        return (m.start(), m.end()) if m else None
    return _loc


def _locate_write_claim(clean: str, users: Any = (),
                        want: tuple[str, str] | None = None,
                        recorded: tuple[str, tuple[int, int]] | None = None) -> tuple[int, int] | None:
    """The span of the claim the screen RECORDED, found on the scrubbed reply with
    the same echo rule and the same preference (honesty-rails-10 /
    redos-slack-surfaces-6: locating the FIRST claim of the unmasked text could
    centre the snippet on a user-echo the screen had excluded; round 2: the recorded
    claim is the first NON-echo hit, so an earlier echo with the same verb and form
    must not win the snippet either). Falls back to the first hit of the wanted
    (verb, form), then to the first hit.

    Round 3 (R2-A3): ``recorded`` = (the text the screen searched, the span it
    recorded). When the scrub changed nothing (the common case) the recorded span IS
    the answer and nothing is re-scanned; otherwise ONE walk (_walk_write_claims)
    yields the preference and the wanted fallback together."""
    if recorded is not None and recorded[0] == clean:
        return recorded[1]
    try:
        spans = _user_quote_spans(clean, users) if users else []
    except Exception:  # noqa: BLE001 -- the locator never raises
        spans = []
    pref, wanted, first = _walk_write_claims(clean, users, spans, want)
    if pref is not None and (want is None or (pref[2], pref[3]) == tuple(want)):
        return pref[0], pref[1]
    hit = wanted or first
    return (hit[0], hit[1]) if hit else None


def _write_claim_locator(users: Any, verb: str, form: str,
                         recorded: tuple[str, tuple[int, int]] | None = None
                         ) -> Callable[[str], tuple[int, int] | None]:
    frozen = tuple(users or ())

    def _loc(clean: str) -> tuple[int, int] | None:
        return _locate_write_claim(clean, frozen, (verb, form), recorded)
    return _loc


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

# Code #14 D-051 round 2 (F1-R1 / forcing-seams-5): the user-typed-id exemption.
# Round 1 exempted EVERY unknown id the user typed this turn, so a fabricated STATE
# claim about that id ("did cq-000000000002 land?" -> "Yes -- cq-000000000002 is
# staged.") wrote no counted line at any tool count -- and round 1 had accepted the
# lexicon half's noun-subject gap ("cq-X has been staged." reads zero) BECAUSE the id
# half caught it. The exemption is now a PURE ECHO only: every sentence carrying the
# id must hold no completion claim (the lexicon grammar, run at ANY tool count) and
# no noun-subject state claim ("cq-X is / has been / 's been staged"). The typed set
# is this message plus the last six user turns (a forced status follow-up names no
# id: "and is it there now?"). Anything else about a typed id is screened in full,
# redaction under enforce included.
#
# Code #14 D-051 round 3 (R2-A1): SUPERSEDES the paragraph above. The exemption is an
# ALLOWLIST, not an absence test. Round 2's "no claim detected" rule, over a typed set
# widened to six prior turns, let a made-up status for an id the user named EARLIER go
# uncounted and unredacted whenever its phrasing slipped the grammar -- "`cq-1111aaaa2222`
# (Sprouts reorder) is staged.", "- `cq-...` — staged ✅", "`cq-...`: approved ✅",
# "Yes -- the Sprouts card (cq-...) landed.", "Found it: cq-.... It's staged". An unknown
# id is now exempt from the fabricated-id half ONLY when (i) the user typed it in the
# CURRENT message (no prior-turn widening), and (ii) EVERY sentence naming it carries an
# explicit NEGATIVE relay (_ID_NEG_RELAY_RE: "not in the queue ledger", "isn't in the
# ledger", "couldn't find", "no record of", "never captured", "unknown id", "doesn't
# exist", "no such", "has not been staged") AND no positive status -- no noun-subject
# state claim, no status word (_ID_STATUS_WORD_RE) that is not itself negated within the
# three tokens before it, and no completion claim (the lexicon grammar). The id's
# sentence also takes the NEXT sentence when that one continues it (opens with a
# pronoun, a status word or a conjunction: "... not in the queue ledger. It's staged
# under a new id." counts). Everything else is counted and redacted under enforce, as
# at the base. Fails toward COUNTING: an honest relay phrased outside the allowlist, or
# the forced follow-up that relays a PRIOR turn's id ("and is it there now?"), is an
# accepted over-trip.
_ID_STATE_CLAIM_RE = re.compile(
    r"\b(?:cq|dw)-[0-9a-f]{12}\b[`*_]{0,3}[ \t]*+"
    r"(?:is|are|was|were|has|have|had|got|gets|['’]s)"
    r"(?:[ \t]++(?:now|just|already|successfully|officially|also|finally|all|been)){0,3}"
    r"[ \t]++(?:staged|queued|approved|shipped|filed|created|updated|deleted|merged|closed|dismissed|"
    r"parked|kept|live|done|completed?|locked[ \t]++in|canonicali[sz]ed)\b",
    re.IGNORECASE)
_ID_SENTENCE_BREAK_RE = re.compile(r"[.!?;\n]")
_ID_ECHO_RADIUS = 300
_ID_ECHO_MAX_OCCURRENCES = 64
# Round 3 (R2-A1): the NEGATIVE relays an honest answer about an unknown id uses -- the
# card-status read's own "not in the queue ledger" line first. Closed list; anything
# phrased outside it is counted.
_ID_LEDGER_NOUN = (r"(?:(?:code[ \t-]?)?(?:session[ \t]++)?(?:queue|ledger|backlog|menu|list)"
                   r"|records?|system)")
_ID_NEG_STATUS = (r"(?:staged|queued|approved|shipped|filed|created|captured|recorded|logged|"
                  r"registered|merged|landed)")
_ID_NEG_RELAY_RE = re.compile(
    r"\b(?:not|isn['’]t|wasn['’]t|aren['’]t|weren['’]t)[ \t]++(?:in|on)[ \t]++"
    r"(?:(?:the|our|my|your|any|that)[ \t]++)?(?:[\w-]{1,20}[ \t]++){0,2}" + _ID_LEDGER_NOUN + r"\b"
    r"|\b(?:couldn['’]t|could[ \t]++not|can['’]t|cannot|can[ \t]++not|didn['’]t|did[ \t]++not|"
    r"don['’]t|do[ \t]++not)[ \t]++(?:find|locate|see)\b"
    r"|\bno[ \t]++(?:record|trace|entry|entries|row|match)(?:e?s)?[ \t]++(?:of|for)\b"
    r"|\bnever[ \t]++(?:been[ \t]++)?captured\b"
    r"|\bunknown[ \t]++(?:id|card|item|entry)\b"
    r"|\b(?:doesn['’]t|does[ \t]++not|didn['’]t|did[ \t]++not)[ \t]++exist\b"
    r"|\bno[ \t]++such\b"
    r"|\b(?:(?:has|have|had)[ \t]++(?:not|never)|hasn['’]t|haven['’]t|hadn['’]t)[ \t]++been[ \t]++"
    + _ID_NEG_STATUS + r"\b"
    r"|\b(?:(?:is|are|was|were)[ \t]++(?:not|never)|isn['’]t|aren['’]t|wasn['’]t|weren['’]t)"
    r"[ \t]++(?:yet[ \t]++)?" + _ID_NEG_STATUS + r"\b",
    re.IGNORECASE)
# A POSITIVE status anywhere in the id's sentence voids the exemption unless that word is
# itself negated (one of the three tokens before it, the id's own tokens left out, is a
# negator: "I couldn't find cq-X in the queue."). Closed vocabulary; "live" before a
# ledger noun is an adjective ("the live ledger"), not a status.
_ID_STATUS_WORD_RE = re.compile(
    r"\b(?:staged|queued|approved|shipped|filed|created|updated|deleted|merged|closed|dismissed|"
    r"parked|kept|live(?![ \t]++(?:(?:code|decision|card|session)[ \t-]?)?"
    r"(?:queue|ledger|backlog|menu|list|records?|system|data)\b)|"
    r"done|completed?|landed|registered|recorded|captured|logged|confirmed|saved|"
    r"added|posted|scheduled|ready|canonicali[sz]ed|locked[ \t]++in|all[ \t]++set|good[ \t]++to[ \t]++go|"
    r"(?:went|gone|goes|go)[ \t]++through|in[ \t]++the[ \t]++queue|"
    r"on[ \t]++(?:the|monday['’]s|this[ \t]++week['’]s|next[ \t]++week['’]s)[ \t]++menu)\b",
    re.IGNORECASE)
_ID_NEGATORS = frozenset({"not", "never", "no", "nothing", "none", "nor", "without", "cannot"})
_ID_NEG_LOOKBACK_CHARS = 60
# The NEXT sentence continues the id's sentence when it opens with a pronoun, a status
# word (a subject-less fragment: "Staged ✅.") or a conjunction.
_ID_CONTINUATION_RE = re.compile(
    r"[ \t\n*_`>\"'“”•-]{0,12}(?:" + _WC_MARK + r"[ \t]*+){0,3}"
    r"(?:(?:it|that|this|they|those|these|which|both|all|and|but|also|now|then|so)\b"
    r"|" + _ID_STATUS_WORD_RE.pattern + r")",
    re.IGNORECASE)


def _typed_ids(rx: re.Pattern[str], user_texts: Any) -> set[str]:
    """The ids of *rx*'s family in the user's own words, capped at _WC_ECHO_MAX_CHARS
    like the lexicon echo rule. Round 3 (R2-A1): the screen passes the CURRENT message
    only."""
    joined = " ".join(str(u) for u in (user_texts or ()) if isinstance(u, str) and u)
    return {m.group(0).lower() for m in rx.finditer(joined[:_WC_ECHO_MAX_CHARS])}


def _id_sentence(text: str, start: int, end: int) -> str:
    """The sentence (bounded +/-_ID_ECHO_RADIUS chars) that carries text[start:end],
    plus the NEXT sentence when it continues this one (_ID_CONTINUATION_RE)."""
    lo = max(0, start - _ID_ECHO_RADIUS)
    before = text[lo:start]
    cut = 0
    for b in _ID_SENTENCE_BREAK_RE.finditer(before):
        cut = b.end()
    after = text[end:end + _ID_ECHO_RADIUS]
    m = _ID_SENTENCE_BREAK_RE.search(after)
    if m is None:
        return before[cut:] + text[start:end] + after
    sentence = before[cut:] + text[start:end] + after[:m.start()]
    nxt = text[end + m.end():end + m.end() + _ID_ECHO_RADIUS]
    lead = len(nxt) - len(nxt.lstrip(" \t\n"))
    if _ID_CONTINUATION_RE.match(nxt, lead):
        stop = _ID_SENTENCE_BREAK_RE.search(nxt, lead)
        sentence += " " + nxt[lead:stop.start() if stop else len(nxt)]
    return sentence


def _has_positive_status(sentence: str) -> bool:
    """A status word in *sentence* none of whose three preceding tokens is a negator.
    The id itself is not a token of that window (it would spend two of the three)."""
    for m in _ID_STATUS_WORD_RE.finditer(sentence):
        before = _WC_ID_TOKEN_RE.sub(" ", sentence[max(0, m.start() - _ID_NEG_LOOKBACK_CHARS):m.start()])
        toks = _WC_TOKEN_RE.findall(before)[-3:]
        if not any(t.lower() in _ID_NEGATORS or t.lower().endswith(("n't", "n’t")) for t in toks):
            return True
    return False


def _id_is_negative_relay(text: str, fid: str,
                          spans: list[tuple[int, int]] | None = None) -> bool:
    """True only when EVERY occurrence of *fid* in *text* (``spans``, else a literal
    search) sits in a sentence that carries an explicit negative relay and no positive
    status (no noun-subject state claim, no un-negated status word, no completion
    claim). Fails toward COUNTING: no occurrence, more than _ID_ECHO_MAX_OCCURRENCES of
    them, or an unevaluable sentence is not a relay."""
    try:
        if spans is None:
            spans = [(m.start(), m.end()) for m in re.finditer(re.escape(fid), text, re.IGNORECASE)]
        if not spans or len(spans) > _ID_ECHO_MAX_OCCURRENCES:
            return False
        for start, end in spans:
            sentence = _id_sentence(text, start, end)
            if (not _ID_NEG_RELAY_RE.search(sentence) or _ID_STATE_CLAIM_RE.search(sentence)
                    or _has_positive_status(sentence)
                    or _find_write_claim_span(sentence) is not None):
                return False
        return True
    except Exception:  # noqa: BLE001 -- an unevaluable relay is counted, never hidden
        log.warning("%s id-relay rule failed -- the id is screened in full", PHANTOM_LOG_KEY,
                    exc_info=True)
        return False


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
                                user_id: str = "", user_text: str = "",
                                prior_user_texts: Any = (), rail_context: dict | None = None):
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

    R14-9(b): the lexicon half reads COMPLETION grammar only (_find_write_claim)
    over the UNMASKED reply; a non-first-person hit lying entirely inside a quoted
    span the user typed (``user_text`` + up to six ``prior_user_texts``;
    _user_quote_spans) is an echo and is skipped. The WARN names the VERB and the
    form label, never the matched words (an arrow / possessive form can carry up to
    three words before the verb, which in a LEX channel could be a name, D-145).

    Fabricated-id half (Code #14 D-051 forcing-seams-5, round 2 F1-R1, round 3
    R2-A1): an unknown id the user typed in THIS message that the reply relays as an
    explicit NEGATIVE relay ("did cq-000000000002 land?" -> "`cq-000000000002` -- not
    in the queue ledger") is not a fabrication -- it is logged at INFO (no ``kind=``,
    never counted) and never redacted. Every sentence naming it must carry an
    allowlisted negative relay and no positive status (_id_is_negative_relay, at ANY
    tool count); "Yes -- cq-000000000002 is staged.", a status with no negative, an id
    typed only in a PRIOR turn and every id the user did not type are screened in full.

    S3: every firing line also appends ONE row to PHANTOM_CLAIMS_LEDGER (the
    adjudication record: a scrubbed snippet or a withheld marker, see
    _rail_snippet) and names it `ref=` in the WARN with response_chars /
    tool_use -- including the counted ABSENT / UNAVAILABLE cannot-check lines
    (integration-tests-8). The reply is scrubbed ONCE per call (_SnippetMemo).
    ``rail_context`` = {channel_id, entity, snippet_withheld, founder_belt} from
    app._dispatch_qa; absent = the snippet is withheld.
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
    memo = _SnippetMemo(text)
    try:
        priors = list(prior_user_texts or ())[-6:]
    except TypeError:
        priors = []
    users = [u for u in [user_text, *priors] if isinstance(u, str) and u]
    for label, rx, reader in _ID_LEDGERS:
        found = {m.group(0).lower() for m in rx.finditer(out)}
        if not found:
            continue
        first = min(found)
        try:
            known = reader()
        except Exception:  # noqa: BLE001 -- an unreadable ledger must not redact real ids
            ref = _record_rail_hit(rail=PHANTOM_LOG_KEY, kind="fabricated-id",
                                   phrase=f"ledger={label} UNAVAILABLE", text=text,
                                   locate=_locate_text(first), mode=mode,
                                   channel_name=channel_name, user_id=user_id,
                                   tool_use_count=count, rail_context=rail_context, memo=memo)
            log.warning("%s kind=fabricated-id ledger=%s UNAVAILABLE -- %d id(s) not "
                        "checked this turn ref=%s", PHANTOM_LOG_KEY, label, len(found),
                        ref or "-", exc_info=True)
            continue
        if known is None:
            # D-051 lens C F6: a ledger that does not EXIST is "cannot check", not
            # "nothing is known" -- an empty reference set would redact every real
            # id under ENFORCE. Skip the family, loudly.
            ref = _record_rail_hit(rail=PHANTOM_LOG_KEY, kind="fabricated-id",
                                   phrase=f"ledger={label} ABSENT", text=text,
                                   locate=_locate_text(first), mode=mode,
                                   channel_name=channel_name, user_id=user_id,
                                   tool_use_count=count, rail_context=rail_context, memo=memo)
            log.warning("%s kind=fabricated-id ledger=%s ABSENT -- %d id(s) not checked "
                        "this turn ref=%s", PHANTOM_LOG_KEY, label, len(found), ref or "-")
            continue
        unknown = found - set(known)
        # Round 3 (R2-A1): typed = the CURRENT message only (never the prior turns).
        typed = _typed_ids(rx, [user_text]) if unknown else set()
        echoed: set[str] = set()
        if unknown & typed:
            claim_view = _LINK_TOKEN_RE.sub(" ", text)
            occ: dict[str, list[tuple[int, int]]] = {}
            for m in rx.finditer(claim_view):
                occ.setdefault(m.group(0).lower(), []).append((m.start(), m.end()))
            echoed = {fid for fid in unknown & typed
                      if _id_is_negative_relay(claim_view, fid, occ.get(fid, []))}
        if echoed:
            log.info("%s fabricated-id echo -- %d unknown %s id(s) the user typed this turn were "
                     "relayed as a negative relay and not counted", PHANTOM_LOG_KEY, len(echoed), label)
        for fid in sorted(unknown - echoed):
            ref = _record_rail_hit(rail=PHANTOM_LOG_KEY, kind="fabricated-id", phrase=fid,
                                   text=text, locate=_locate_text(fid), mode=mode,
                                   channel_name=channel_name, user_id=user_id,
                                   tool_use_count=count, rail_context=rail_context, memo=memo)
            emit("%s kind=fabricated-id id=%s ledger=%s mode=%s channel=#%s user=%s "
                 "response_chars=%d tool_use=%s ref=%s -- "
                 "the reply names an id that exists in no ledger",
                 PHANTOM_LOG_KEY, fid, label, mode, channel_name or "?", user_id or "?",
                 len(text), "?" if count is None else count, ref or "-")
            if mode == "enforce":
                out = re.sub(re.escape(fid), "[unknown id]", out, flags=re.IGNORECASE)
    if count == 0:
        masked = _LINK_TOKEN_RE.sub(" ", out)
        spans: list[tuple[int, int]] = []
        if users:
            try:
                spans = _user_quote_spans(masked, users)
            except Exception:  # noqa: BLE001 -- an echo-rule failure never hides a claim
                log.warning("%s echo rule failed -- screening with no echo exemption",
                            PHANTOM_LOG_KEY, exc_info=True)
                spans = []
        hit = _find_write_claim_span(masked, spans=spans)
        if hit:
            # ONE line per reply (the count is per turn); it RECORDS the first hit whose
            # words the user did not write (round 2, honesty-rails-10).
            try:
                rec = _preferred_write_claim_span(masked, users, spans) or hit
            except Exception:  # noqa: BLE001 -- a preference failure records the first hit
                rec = hit
            verb, form = rec[2], rec[3]
            ref = _record_rail_hit(rail=PHANTOM_LOG_KEY, kind="lexicon", phrase=verb, text=text,
                                   locate=_write_claim_locator(users, verb, form,
                                                               recorded=(masked, (rec[0], rec[1]))),
                                   mode=mode, channel_name=channel_name, user_id=user_id,
                                   tool_use_count=count, rail_context=rail_context, form=form,
                                   memo=memo)
            emit("%s kind=lexicon phrase=%r form=%s mode=%s channel=#%s user=%s "
                 "response_chars=%d tool_use=%s ref=%s -- a write claim with zero tool_use "
                 "this turn",
                 PHANTOM_LOG_KEY, verb, form, mode, channel_name or "?", user_id or "?",
                 len(text), count, ref or "-")
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
# Code #14 D-051 (honesty-rails-7): NO 'write' / 'full' modifier -- every QBO tool
# the bot has is a READ, so "I don't have write access to QuickBooks" is TRUE; a
# write-access denial about a read-only family must never count as a phantom.
_CAP_NEG_HAVE = (
    r"\bI\s+(?:don['\u2019]?t|do\s+not|didn['\u2019]?t|did\s+not|won['\u2019]?t|will\s+not)\s+(?:currently\s+|actually\s+|really\s+)?have\s+"
    r"(?:(?:direct|read|read-only|live|real[- ]?time|any|the|a|an|current)\s+){0,3}"
    r"(?:tools?|access|visibility|way|ability|permissions?|mechanism|integration|connector|means|"
    r"capability|capabilities|hooks?|line|route|path|window)\b"
)
_CAP_NO_HAVE = (
    r"\bI\s+have\s+no\s+(?:(?:direct|read|read-only|live|real[- ]?time|current)\s+){0,2}"
    r"(?:tools?|access|visibility|way|ability|permission|means|integration|"
    r"connector|mechanism|hooks?)\b"
    r"|\bI\s+lack\s+(?:the\s+|any\s+)?(?:tools?|access|visibility|ability|permission|means)\b"
    r"|\bthere['\u2019]?s?\s+(?:is\s+)?no\s+(?:tool|way|integration|connector|hook)\s+(?:for\s+me|I\s+(?:can|have))\b"
)
_CAP_CANT_VERB = (
    r"\bI\s+(?:can['\u2019]?t|cannot|can\s+not|am\s+not\s+able\s+to|['\u2019]m\s+not\s+able\s+to|am\s+unable\s+to|"
    r"['\u2019]m\s+unable\s+to|won['\u2019]?t\s+be\s+able\s+to|don['\u2019]?t\s+have\s+the\s+ability\s+to|have\s+no\s+way\s+to)\s+"
    r"(?:directly\s+|actually\s+|currently\s+|really\s+)?"
    r"(?:access|see|reach|stage|ship|dismiss|close|approve|queue|read|pull(?:\s+up)?|check|view|open|query|"
    r"use|touch|modify|update|create|send|post|run|execute|trigger|look\s+(?:at|into|up)|get\s+(?:to|into|at)|"
    r"interact\s+with|connect\s+to|talk\s+to|search|retrieve|fetch|list|manage|edit|write\s+to|log\s+into|"
    r"delete|complete|mark|schedule|draft|dm|message)\b"
)
# The "not connected / available / set up / integrated / enabled" alternation needs
# a BOT-SUBJECT in the same clause (D-051 Code #13 review AD-5): the bare form read
# third-person business facts as capability denials whenever the sentence also
# named a family word -- "The Asana board is not set up for that project yet",
# "Klaviyo isn't integrated with the CRM yet", "The calendar invite is not available
# yet", "Our inventory isn't available at the Anaheim warehouse". Three anchors:
# the bot as subject ("I'm not connected", "my tools aren't wired to"), the bot as
# the beneficiary ("HubSpot isn't connected FOR ME / TO ME / ON MY END"), or a tool
# / connector / integration as subject ("that connector isn't enabled").
_CAP_NOT_STATE = (
    r"(?:connected|available|accessible|exposed|wired(?:\s+up)?|hooked\s+up|integrated|enabled|set\s+up|"
    r"provisioned|plugged\s+in)"
)
_CAP_NOT_CONNECTED = (
    r"\b(?:I\s+(?:am|['’]m)\s+not|I['\u2019]m\s+not|I\s+am\s+not)\s+(?:currently\s+|yet\s+)?" + _CAP_NOT_STATE + r"\b"
    r"|\bmy\s+(?:tools?|toolset|connectors?|integrations?|access)\s+(?:isn['\u2019]?t|is\s+not|aren['\u2019]?t|are\s+not)\s+"
    r"(?:currently\s+|yet\s+)?" + _CAP_NOT_STATE + r"\b"
    r"|\b(?:that|this|the)\s+(?:tool|connector|integration|hook)\s+(?:isn['\u2019]?t|is\s+not)\s+"
    r"(?:currently\s+|yet\s+)?" + _CAP_NOT_STATE + r"\b"
    r"|\b(?:isn['\u2019]?t|is\s+not|aren['\u2019]?t|are\s+not|not)\s+(?:currently\s+|yet\s+)?" + _CAP_NOT_STATE
    + r"\s+(?:(?:to|for)\s+me|on\s+my\s+(?:end|side)|in\s+my\s+(?:tools?|toolset))\b"
    r"|\bnot\s+available\s+to\s+me\b"
    r"|\b(?:outside|beyond)\s+(?:of\s+)?my\s+(?:(?:current|immediate|present|available|live|working|context)\s+){0,2}"
    r"(?:reach|access|tools|toolset|capabilities|scope|purview|context(?:\s+window)?|window|view)\b"
)
_CAP_USE_INTERFACE = (
    r"\b(?:use|check|open|go\s+to|try|log\s+into|do\s+(?:that|this|it)\s+(?:in|through|via))\s+"
    r"(?:the\s+)?(?:asana|hubspot|quickbooks|qbo|shopify|gmail|calendar|slack|deposco|notion|klaviyo|"
    r"make(?:\.com)?|google\s+calendar)\s+(?:interface|app|ui|dashboard|console|website|site|portal|web\s+app)\b"
    r"|\b(?:do|handle|check|stage|approve|dismiss|close|ship|update|create|complete|mark|queue)\s+"
    r"(?:that|this|it|those|these|them)\s+(?:manually|yourself|directly|by\s+hand)\b"
    r"|\byou['\u2019]?ll\s+(?:need|have)\s+to\s+(?:do|handle|check|stage|approve|dismiss|close|ship|update|create|queue)\s+"
    r"(?:that|this|it|those|these|them\s+)?(?:manually|yourself|directly)\b"
)
# R14-9(c): a DEFLECTION -- the reply points at a check it will not run itself
# (9/21 08:46:52 "that needs a direct check of the ledger, not another tap").
# Trips only when the deflection's OWN OBJECT (the phrase after "check of", up to
# the next clause break) or the sentence text BEFORE the deflection names a
# capability the bot HAS -- Code #14 D-051: the whole sentence was searched, so "a
# direct check of the ledger at the bank, not QBO" read the NEGATED 'QBO' in the
# TAIL as the object; round 2 (F1-R5) restores the front-named family ("For
# Asana, that would need a direct check of the task history.").
_CAP_DEFLECT = (
    r"\b(?:that|this|it|which)\s+(?:needs|requires|would\s+need|would\s+require|takes|calls\s+for)\s+"
    r"(?:a\s+)?(?:(?:direct|manual|live|separate|real)\s+){0,2}(?:check|look|query|read|lookup|pull)\s+"
    r"(?:of|at|on|in|into)\b"
)
_DEFLECT_RE = re.compile(_CAP_DEFLECT, re.IGNORECASE)
_CLAUSE_BREAK_RE = re.compile(r"[,;:.!?\n()\u2014\u2013]|\s-{1,2}\s")
_DENIAL_RE = re.compile(
    "(?:" + _CAP_NEG_HAVE + "|" + _CAP_NO_HAVE + "|" + _CAP_CANT_VERB + "|" + _CAP_NOT_CONNECTED
    + "|" + _CAP_USE_INTERFACE + "|" + _CAP_DEFLECT + ")",
    re.IGNORECASE,
)
# RULED refusals the model is REQUIRED to voice are exempt BY SHAPE (D-051 Code #13
# review AD-6): the capability set reads the tool REGISTRY, but a Tier-3 finance
# refusal ("I can't pull P&L figures in this channel -- ask in #f3e-finance"), the
# D-043 own-mailbox refusal ("only your own mailbox is in scope for me") and a
# Harrison-only / custodian-only scope line are zero-tool_use turns that name a
# family the registry offers -- the PROMPT-level tier rule forbids the pull, not the
# registry. Enforce would have PREPENDED "Try: ask me for a QuickBooks P&L" under
# the very refusal that was enforcing the guardrail. A sentence that redirects to
# another channel, scopes to "this channel", names the own-mailbox rule or a
# Harrison- / founder- / custodian-only scope is a refusal, not a denial. Bounded
# classes only (ReDoS discipline).
_RULED_REFUSAL_RE = re.compile(
    r"(?:\b(?:not\s+)?in\s+this\s+channel\b"
    r"|\bthis\s+channel\s+(?:isn['\u2019]?t|is\s+not|doesn['\u2019]?t|does\s+not|can['\u2019]?t)\b"
    r"|\bask\s+(?:me\s+)?(?:again\s+)?(?:in|over\s+in|from|via)\s+(?:the\s+)?(?:#|<#|[a-z0-9_-]{1,40}\s+channel\b)"
    r"|\b(?:in|from|via|over\s+in|to)\s+#[a-z0-9_-]{1,60}"
    r"|\b(?:finance|leadership|founder)\s+channel\b"
    r"|\b(?:your|their|his|her|the)\s+own\s+(?:mailbox|inbox|emails?|mail|drive)\b"
    r"|\bown[- ]mailbox[- ]only\b"
    r"|\b(?:harrison|founder|owner|custodian|admin)[- ]only\b"
    r"|\bonly\s+(?:harrison|the\s+founder|a\s+custodian|phi\s+custodians?)\b"
    r"|\btier[- ]?[123]\b"
    r"|\bout\s+of\s+scope\s+(?:for\s+me\s+)?(?:in|here)\b)",
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


def _sentence_head(text: str, start: int) -> str:
    """The part of the sentence BEFORE text[start] (bounded 160 chars back)."""
    before = text[max(0, start - 160):start]
    brk = list(_SENTENCE_BREAK_RE.finditer(before))
    return before[brk[-1].end():] if brk else before


def _denial_window(text: str, start: int, end: int) -> str:
    """The sentence the denial sits in (bounded 160 chars back / 220 ahead), so the
    capability term is looked for where the denial's OBJECT lives -- after it
    ("...visibility into the code queue") or before it ("HubSpot isn't connected
    for me")."""
    hi = min(len(text), end + 220)
    before = _sentence_head(text, start)
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


def _capability_terms(entity: str, cross_entity: bool, founder: bool,
                      dm: bool = False) -> dict[str, str]:
    from .capability_set import capability_terms  # lazy: pulls tool_dispatch
    return capability_terms(entity, cross_entity=cross_entity, founder=founder, dm=dm)


def _registry_symbols() -> frozenset[str]:
    from .capability_set import registry_tool_names  # lazy
    return registry_tool_names()


def screen_capability_claims(text, *, tool_use_count, channel_name: str = "", user_id: str = "",
                             entity: str = "", cross_entity: bool = False, founder: bool = False,
                             rail_context: dict | None = None):
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

    RULED tier / scope refusals are EXEMPT BY SHAPE (AD-6, ``_RULED_REFUSAL_RE``):
    a sentence that redirects to another channel, scopes to "this channel", names
    the own-mailbox rule or a Harrison- / founder- / custodian-only scope is the
    guardrail speaking, not a denial -- the capability set reads the tool registry,
    which cannot see the prompt-level tier rule that forbids the pull. Refusal
    shapes outside that list still read as denials until the set is tier-scoped.
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
    memo = _SnippetMemo(text)

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
            ref = _record_rail_hit(rail=CAPABILITY_LOG_KEY, kind="toolname", phrase=sym, text=text,
                                   locate=_locate_text(sym), mode=mode,
                                   channel_name=channel_name, user_id=user_id,
                                   tool_use_count=count, rail_context=rail_context, memo=memo)
            emit("%s kind=toolname symbol=%s mode=%s channel=#%s user=%s tool_use=%s "
                 "response_chars=%d ref=%s -- an "
                 "internal tool name reached a non-developer surface",
                 CAPABILITY_LOG_KEY, sym, mode, channel_name or "?", user_id or "?",
                 "?" if count is None else count, len(text), ref or "-")
            if mode == "enforce":
                out = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(sym)}(?![A-Za-z0-9_])",
                             INTERNAL_TOOL_REDACTION, out)

    # (1) capability denial -- zero-tool_use turns only, about something the bot HAS
    if count == 0:
        masked = _LINK_TOKEN_RE.sub(" ", out)
        try:
            # Code #14 D-051 (honesty-rails-6 / forcing-seams-4): the card-ledger read
            # answers ONLY in the founder's DM, so its family is a capability there
            # alone -- the founder's honest "I can't read the card ledger from this
            # channel" elsewhere is true (the tool refuses every non-DM surface).
            terms = _capability_terms(entity, cross_entity, founder,
                                      dm=str(channel_name or "").strip().lower() == "dm")
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
                if _RULED_REFUSAL_RE.search(window):
                    continue   # AD-6: a tier / scope refusal the model must voice, not a denial
                if _DEFLECT_RE.fullmatch(m.group(0)):
                    # The deflection's OWN object first, then the sentence text BEFORE
                    # it (round 2 F1-R5: "On the HubSpot side, that needs a live look at
                    # the deal record." names the family up front) -- never the tail
                    # after the object's clause break, where a negated "not QBO" lives.
                    obj = masked[m.end():m.end() + 160]
                    brk = _CLAUSE_BREAK_RE.search(obj)
                    hit = (find_capability_term(obj[:brk.start()] if brk else obj, terms)
                           or find_capability_term(_sentence_head(masked, m.start()), terms))
                else:
                    hit = find_capability_term(window, terms)
                if hit is None:
                    continue
                term, hint = hit
                ref = _record_rail_hit(rail=CAPABILITY_LOG_KEY, kind="denial",
                                       phrase=m.group(0).strip(), text=text,
                                       locate=_locate_text(m.group(0).strip()), mode=mode,
                                       channel_name=channel_name, user_id=user_id,
                                       tool_use_count=count, rail_context=rail_context, memo=memo)
                emit("%s kind=denial phrase=%r term=%r mode=%s channel=#%s user=%s "
                     "response_chars=%d tool_use=%s ref=%s -- a capability "
                     "denial with zero tool_use about a capability the bot has in this channel",
                     CAPABILITY_LOG_KEY, m.group(0).strip(), term, mode, channel_name or "?",
                     user_id or "?", len(text), count, ref or "-")
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
