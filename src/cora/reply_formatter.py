"""Deterministic post-processor for Cora's conversational Slack replies.

Enforces the voice/style contract in design/system-prompts/fndr.md mechanically,
because prompt-only enforcement is unreliable (same doctrine as the cross-entity
and sibling guards). Applied in app.py immediately before posting, in the same
place WRITE_CONFIRMED and the [CORA_KNOWLEDGE_GAP: ...] marker are stripped.

Conversational replies get (Slack-native mrkdwn -- 2026-06-30 format standard):
  - markdown bold (**x** / __x__) CONVERTED to Slack bold (*x*), not stripped
  - a markdown header line (# H) converted to a Slack bold label line (*H*)
  - unordered list markers (-, *, +) normalized to Slack bullets (•); numbered
    lists (1.) kept intact; markdown tables flattened; horizontal rules removed;
    `inline code` + ``` fences flattened
  - em/en dashes replaced with a hyphen (voice contract bans em-dashes)
  - emoji + :shortcode: tokens filtered to a small FUNCTIONAL allowlist
    (check / warning / red / yellow / green / pushpin); all other emoji stripped
  - source-opacity lint: bare docs.google.com / drive.google.com / app.asana.com
    / notion.so / *.intuit.com URLs and naked Asana/Slack IDs (gid <digits>, 16+
    digit numbers) are redacted. Sanctioned Slack <url|label> links and <@mentions>
    are preserved.
  - ~900-char soft cap measured + logged (NOT hard-truncated -- truncation is worse
    than length; length is governed primarily via the prompt).

is_tool_output=True bypasses ALL of the above. Financial pulses, decision queues,
dashboard/pipeline tool outputs are presented exactly as the tool returned them
(per fndr.md "tool outputs are presented as-is").

This module never changes financial / PHI / cross-entity guard behavior. It only
shapes already-approved conversational text.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Soft cap: log-only, never truncates. Tiered standard (2026-06-30) -- a simple
# reply is 1-3 sentences; a structured multi-part reply may run longer. This
# threshold flags only genuine walls so the log signal stays meaningful.
CONVERSATIONAL_CHAR_CAP = 900

# --- Slack entity protection ---------------------------------------------
# Slack angle-bracket tokens are sanctioned: <url|label>, <url>, <@U123>,
# <#C123|name>. Protect them wholesale so bare-URL/ID redaction never touches
# their internals.
_SLACK_TOKEN_RE = re.compile(r"<[^<>\n]+>")
_PLACEHOLDER = "\x00CORATOK{}\x00"

# --- markdown ------------------------------------------------------------
_BOLD_STAR_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_BOLD_UNDER_RE = re.compile(r"__([^_\n]+)__")
_HEADER_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")

# --- code + lists --------------------------------------------------------
# 2026-06-30 format standard: KEEP list structure (Slack-native). Unordered
# markers normalize to a Slack bullet; numbered lists (1. / 1)) are left intact
# (Slack renders them). Code fences / inline code still flatten.
_CODE_FENCE_RE = re.compile(r"^[ \t]*```[^\n]*$", re.MULTILINE)   # drop ``` fence lines
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")                       # `code` -> code
# Unordered bullet markers only (-, *, +) + a trailing space. Numbered lists are
# NOT matched (kept intact). A "*Label:*" bold line is NOT matched (no space after *).
_UL_MARKER_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)

# --- dashes --------------------------------------------------------------
# figure dash, en dash, em dash, horizontal bar -> hyphen
_DASH_RE = re.compile(r"[‒–—―]")

# --- emoji + shortcodes (functional allowlist -- 2026-06-30) -------------
# Cora may use a SMALL functional set as status markers; everything else is
# stripped (deterministic "sparing + functional", not prompt-only). Allowlist
# by BASE codepoint; variation selectors / ZWJ / keycap are matched as part of
# the cluster so an allowed emoji keeps its exact presentation and a decorative
# sequence is removed whole.
_ALLOWED_EMOJI_BASES = frozenset({
    "✅",       # white check mark
    "⚠",       # warning sign (may carry a trailing VS16 -> warning emoji)
    "\U0001F534",   # red circle
    "\U0001F7E1",   # yellow circle
    "\U0001F7E2",   # green circle
    "\U0001F4CC",   # pushpin
})
_ALLOWED_SHORTCODES = frozenset({
    "white_check_mark", "warning", "red_circle",
    "large_yellow_circle", "large_green_circle", "pushpin",
})
# Shortcodes must start with a letter so timestamps like 12:30:45 are untouched.
_SHORTCODE_RE = re.compile(r":[a-z][a-z0-9_+\-]*:")
# One emoji cluster = a base pictograph (group 1) + optional VS16/keycap and
# ZWJ-joined continuations, so a multi-codepoint decorative sequence is one match.
_EMOJI_CLUSTER_RE = re.compile(
    "([\U0001F300-\U0001FAFF"   # symbols & pictographs (incl. 1F534 red, 1F6A8 siren)
    "\U0001F000-\U0001F0FF"     # mahjong / dominoes / cards
    "\U00002600-\U000027BF"     # misc symbols + dingbats (incl. 2705 check, 2728 sparkles)
    "\U00002300-\U000023FF"     # technical (incl. 23F3 hourglass, 231A watch)
    "\U00002B00-\U00002BFF"     # misc symbols & arrows (incl. 2B50 star)
    "\U0001F1E6-\U0001F1FF])"   # regional indicator (flags)
    "(?:[︀-️⃣]|‍[\U0001F300-\U0001FAFF\U00002600-\U000027BF])*",
    flags=re.UNICODE,
)


def _emoji_cluster_replace(m: "re.Match") -> str:
    """Keep an emoji cluster only if its base codepoint is allowlisted."""
    return m.group(0) if m.group(1) in _ALLOWED_EMOJI_BASES else ""


def _shortcode_replace(m: "re.Match") -> str:
    """Keep a :shortcode: only if it is an allowlisted functional marker."""
    return m.group(0) if m.group(0).strip(":") in _ALLOWED_SHORTCODES else ""


def _header_to_bold(m: "re.Match") -> str:
    """A markdown header line -> a single Slack bold label line. Strip ALL '*' from
    the header text (not just the ends) so interior emphasis -- which step 2 reliably
    produces (e.g. '## **Key** takeaways' -> '## *Key* takeaways') -- cannot yield an
    unbalanced, dangling-asterisk label ('*Key* takeaways*')."""
    txt = m.group(1).replace("*", "").strip()
    return f"*{txt}*" if txt else ""

# --- source-opacity lint -------------------------------------------------
_BARE_DOC_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?"
    # *.intuit.com (bounded {0,3} subdomain depth -> no ReDoS) added as B2
    # defense-in-depth: a bare/fabricated qbo.intuit.com report link is redacted
    # here AND on the egress boundary, covering the conversational-fabrication and
    # WebClient-bypass paths the QBO tool-description fix can't fully prevent.
    r"(?:docs\.google\.com|drive\.google\.com|app\.asana\.com|notion\.so"
    r"|(?:[a-z0-9-]+\.){0,3}intuit\.com)"
    # ')' excluded so a URL inside "(...)" or a markdown link "[label](...)"
    # leaves a balanced empty shell for the 8b cleanup (these URLs never
    # legitimately contain a close-paren).
    r"/?[^\s<>|)]*",
    re.IGNORECASE,
)
_GID_RE = re.compile(r"\bgid[:=]?\s*\d{4,}\b", re.IGNORECASE)
_NAKED_ID_RE = re.compile(r"\b\d{16,}\b")

# Sheet/tab identifiers that must never appear in a conversational reply (the
# 2026-06-08 SEV-1 "named the sheet" class). Replaced with a NEUTRAL phrase (not
# deleted) so the sentence stays grammatical. Conversational-only: this runs
# inside format_reply, which the egress boundary does NOT apply to proactive
# sends (an ops alert may legitimately reference a sheet).
_SHEET_IDENT_RE = re.compile(
    r"\b(?:the\s+)?Standing\s+ACTUALS(?:\s+(?:sheet|spreadsheet|tab))?\b"
    r"|\bCF[_\s]?SUMMARY(?:\s+(?:sheet|tab))?\b",
    re.IGNORECASE,
)
_SHEET_IDENT_REPLACEMENT = "the cash flow model"

# HJR-Founder-OS Drive file PATHS that name a document (the 2026-06-17 /cora-ask
# leak: Cora composed "02-F3-Energy/production/...xlsx" into prose -- source-opacity
# prompt drift the URL/gid/sheet-name lints above don't catch). Match a Founder-OS
# root (an NN-Entity folder, e.g. "02-F3-Energy"/"00-Founder", or "_shared") + at
# least one "/segment" + a DOCUMENT extension, replaced with a neutral phrase.
# Anchored so ordinary prose ("ramping production", "the _shared drive", "a pdf of
# the deck") cannot match -- the required slash-segments + doc extension are the
# prose guard. The per-segment class EXCLUDES '/' so each separator is consumed
# exactly once: this is a non-overlapping quantifier (no (a+)+ ambiguity), which
# avoids catastrophic backtracking / ReDoS on a long path-shaped reply that lacks
# a trailing doc extension. Roots are limited to NN-Entity + _shared (the green-lit
# scope) so generic dir names ("outputs/dist/x.csv", "memory/blob.pdf") in echoed
# build/log output are left alone. Conversational-only (runs in format_reply, NOT
# the egress boundary): a proactive ops alert may legitimately reference a path.
# Doc extensions only -- a bare .md path is NOT matched (avoids "README.md" prose).
_DRIVE_PATH_RE = re.compile(
    r"\b(?:[0-9]{2}-[A-Za-z][A-Za-z0-9-]*|_shared)"
    r"(?:/[^\s<>|/]+)+"
    r"\.(?:xlsx|gsheet|pdf|docx|pptx|csv)\b",
    re.IGNORECASE,
)
_DRIVE_PATH_REPLACEMENT = "a portfolio document"

# Redaction shells: when a redacted URL sat inside parens or a markdown link,
# the surrounding "()" / "[label]()" survives as a visible artifact (live
# 2026-06-11 follow-up replies). Clean them after the redaction pass.
_EMPTY_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(\s*\)")  # [label]() -> label
_EMPTY_PARENS_RE = re.compile(r"\(\s*\)")
_EMPTY_BRACKETS_RE = re.compile(r"\[\s*\]")

# --- whitespace cleanup --------------------------------------------------
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_MULTINEWLINE_RE = re.compile(r"\n{3,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r" +([.,;:!?])")


def _flatten_tables(text: str) -> str:
    """Drop markdown table separator rows; flatten pipe rows to ' - ' prose."""
    out_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if "|" in stripped:
            # Separator row: only pipes, dashes, colons, spaces -> drop entirely.
            if stripped and re.fullmatch(r"[\s|:\-]+", stripped) and "-" in stripped:
                continue
            # Data row with 2+ pipes -> flatten cells to prose.
            if stripped.count("|") >= 2:
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                cells = [c for c in cells if c]
                out_lines.append(" - ".join(cells))
                continue
        out_lines.append(line)
    return "\n".join(out_lines)


def format_reply(text: str, *, is_tool_output: bool = False) -> str:
    """Shape a conversational reply per the voice contract. Pure function.

    is_tool_output=True returns the text untouched (tool outputs are presented
    as-is per fndr.md).
    """
    if not text:
        return text
    if is_tool_output:
        return text

    # 1. Protect sanctioned Slack tokens (<url|label>, <@mentions>, etc.).
    tokens: list[str] = []

    def _protect(m: re.Match) -> str:
        tokens.append(m.group(0))
        return _PLACEHOLDER.format(len(tokens) - 1)

    work = _SLACK_TOKEN_RE.sub(_protect, text)

    # 2. Convert markdown bold to Slack single-asterisk bold (*x*), not strip.
    # The model emits **x**/__x__; Slack `text=` renders only *x*.
    work = _BOLD_STAR_RE.sub(r"*\1*", work)
    work = _BOLD_UNDER_RE.sub(r"*\1*", work)

    # 2b. Code: drop ``` fence lines, unwrap `inline code`, strip stray backticks.
    # Runs before the source-opacity lint so a redactable id wrapped in backticks
    # (e.g. `gid 12345...`) is unwrapped first, then still redacted.
    work = _CODE_FENCE_RE.sub("", work)
    work = _INLINE_CODE_RE.sub(r"\1", work)
    work = work.replace("`", "")

    # 3. Convert a markdown header line to a Slack bold label line (*Header*).
    work = _HEADER_RE.sub(_header_to_bold, work)

    # 4. Tables -> prose (before HR removal so separator rows are handled here).
    work = _flatten_tables(work)

    # 5. Remove horizontal rules (whole-line).
    work = "\n".join(line for line in work.split("\n") if not _HR_RE.match(line))

    # 5b. Normalize unordered list markers to Slack bullets; keep numbered lists.
    # Line-anchored, so mid-line " - " (flattened table cells) and hyphenated
    # words ("well-being") are untouched; a "*Label:*" bold line is untouched
    # (the marker needs a trailing space, which "*Label:*" lacks).
    work = _UL_MARKER_RE.sub(r"\1• ", work)

    # 6. Dashes -> hyphen (voice contract bans em-dashes).
    work = _DASH_RE.sub("-", work)

    # 7. Filter emoji + :shortcode: to the functional allowlist; strip the rest.
    work = _SHORTCODE_RE.sub(_shortcode_replace, work)
    work = _EMOJI_CLUSTER_RE.sub(_emoji_cluster_replace, work)

    # 8. Source-opacity lint (sanctioned links are placeholders, so safe).
    work = _BARE_DOC_URL_RE.sub("", work)
    work = _GID_RE.sub("", work)
    work = _NAKED_ID_RE.sub("", work)
    # 8a. Named sheet identifiers -> neutral phrase (conversational source-opacity).
    work = _SHEET_IDENT_RE.sub(_SHEET_IDENT_REPLACEMENT, work)
    # 8a'. Drive document PATHS -> neutral phrase (same conversational scoping).
    work = _DRIVE_PATH_RE.sub(_DRIVE_PATH_REPLACEMENT, work)

    # 8a''. Belt-and-suspenders for the two conversational-only identifier lints
    # (sheet names 8a, Drive paths 8a') -- unlike the URL/GID lints they have NO
    # egress-boundary backstop. Step 2 now CONVERTS **bold** -> *bold* (the
    # pre-2026-06-30 code stripped the markers), so a marker the model dropped
    # INSIDE an identifier ("Standing **ACTUALS**" -> "Standing *ACTUALS*",
    # "run.**xlsx**" -> "run.*xlsx*") splits the literal anchor and the lint above
    # misses it. Re-scan an asterisk-stripped view; if it exposes an identifier the
    # emphasized pass missed, re-run the full source-opacity pass on that view and
    # adopt it -- a reply naming a restricted sheet/path is scrubbed even at the
    # cost of its display emphasis (correctness > formatting when restricted content
    # is present). Only fires when a '*' is present AND a de-emphasized identifier
    # matches, so ordinary bolded replies keep their formatting untouched.
    if "*" in work:
        _deemph = work.replace("*", "")
        if _deemph != work and (
            _SHEET_IDENT_RE.search(_deemph) or _DRIVE_PATH_RE.search(_deemph)
        ):
            _deemph = _BARE_DOC_URL_RE.sub("", _deemph)
            _deemph = _GID_RE.sub("", _deemph)
            _deemph = _NAKED_ID_RE.sub("", _deemph)
            _deemph = _SHEET_IDENT_RE.sub(_SHEET_IDENT_REPLACEMENT, _deemph)
            _deemph = _DRIVE_PATH_RE.sub(_DRIVE_PATH_REPLACEMENT, _deemph)
            work = _deemph

    # 8b. Clean redaction shells the lint leaves behind: "[label]()" -> label,
    # then any empty "()" / "[]" pairs.
    work = _EMPTY_MD_LINK_RE.sub(r"\1", work)
    work = _EMPTY_PARENS_RE.sub("", work)
    work = _EMPTY_BRACKETS_RE.sub("", work)

    # 9. Whitespace cleanup.
    work = _MULTISPACE_RE.sub(" ", work)
    work = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", work)
    work = _TRAILING_WS_RE.sub("", work)
    work = _MULTINEWLINE_RE.sub("\n\n", work)
    work = work.strip()

    # 10. Restore sanctioned Slack tokens.
    for i, tok in enumerate(tokens):
        work = work.replace(_PLACEHOLDER.format(i), tok)

    # 11. Measure the soft char cap. Log only -- never hard-truncate.
    if len(work) > CONVERSATIONAL_CHAR_CAP:
        log.warning("reply_over_cap: %d chars (cap %d)", len(work), CONVERSATIONAL_CHAR_CAP)

    return work


def redact_links_and_ids(text: str) -> str:
    """Redact bare source URLs + naked GIDs/long IDs; preserve sanctioned
    <url|label> links and <@mentions>. The SAFETY subset of the source-opacity
    lint, usable on ANY content -- it does NOT flatten markdown, strip emoji,
    collapse whitespace, or touch table/code structure -- so the egress boundary
    can run it on EVERY outbound message (proactive tables/cards included) without
    mangling layout. Conversational voice-flattening stays in format_reply.

    Pure function; returns the input on falsy/non-str.
    """
    if not text:
        return text
    tokens: list[str] = []

    def _protect(m: re.Match) -> str:
        tokens.append(m.group(0))
        return _PLACEHOLDER.format(len(tokens) - 1)

    work = _SLACK_TOKEN_RE.sub(_protect, text)
    work = _BARE_DOC_URL_RE.sub("", work)
    work = _GID_RE.sub("", work)
    work = _NAKED_ID_RE.sub("", work)
    # Clean only the shells a redaction leaves behind (no structural reflow). The
    # whitespace/space-before-punct collapse is deliberately NOT applied here -- it
    # would break the fixed-width / decimal-aligned columns of proactive tables.
    work = _EMPTY_MD_LINK_RE.sub(r"\1", work)
    work = _EMPTY_PARENS_RE.sub("", work)
    work = _EMPTY_BRACKETS_RE.sub("", work)
    for i, tok in enumerate(tokens):
        work = work.replace(_PLACEHOLDER.format(i), tok)
    return work


# Whole fenced code blocks (```...```) protected as single units so bold
# conversion can never rewrite ** inside a fixed-width proactive table.
# Non-greedy across lines; an unterminated fence is left alone (no match).
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
# Inline `code` spans, protected for the same reason.
_INLINE_CODE_SPAN_RE = re.compile(r"`[^`\n]+`")


def normalize_slack_bold(text: str) -> str:
    """Convert markdown bold (**text** / __text__) to Slack bold (*text*) --
    and do NOTHING else (WS-5, closes the 2026-07-01 format-arc deferral).

    For LLM-composed PROACTIVE senders (daily briefing, strategy memo, finance
    recap) that egress outside format_reply's conversational path. Unlike
    format_reply this is code-fence- and table-safe: fenced blocks and inline
    code spans are protected wholesale (format_reply DELETES fences, so it
    never needed a fence guard -- proactive senders keep theirs). Sanctioned
    Slack <...> tokens are protected the same way as redact_links_and_ids.
    Idempotent (converting already-*bold* text is a no-op: ** is required to
    match). Pure function; returns the input on falsy/non-str.

    Deliberately NOT hooked into slack_egress.sanitize_text: the boundary is
    kept to universal SAFETY transforms (2026-06-17 review); bold conversion
    is per-sender opt-in.
    """
    if not text or not isinstance(text, str):
        return text
    tokens: list[str] = []

    def _protect(m: re.Match) -> str:
        tokens.append(m.group(0))
        return _PLACEHOLDER.format(len(tokens) - 1)

    work = _FENCED_BLOCK_RE.sub(_protect, text)
    work = _INLINE_CODE_SPAN_RE.sub(_protect, work)
    work = _SLACK_TOKEN_RE.sub(_protect, work)
    work = _BOLD_STAR_RE.sub(r"*\1*", work)
    work = _BOLD_UNDER_RE.sub(r"*\1*", work)
    for i, tok in enumerate(tokens):
        work = work.replace(_PLACEHOLDER.format(i), tok)
    return work
