"""Deterministic post-processor for Cora's conversational Slack replies.

Enforces the voice/style contract in design/system-prompts/fndr.md mechanically,
because prompt-only enforcement is unreliable (same doctrine as the cross-entity
and sibling guards). Applied in app.py immediately before posting, in the same
place WRITE_CONFIRMED and the [CORA_KNOWLEDGE_GAP: ...] marker are stripped.

Conversational replies get:
  - markdown bold (**x** / __x__) flattened to plain text
  - markdown tables flattened to prose; horizontal rules + headers removed
  - em/en dashes replaced with a hyphen (voice contract bans em-dashes)
  - emoji and :shortcode: tokens stripped
  - source-opacity lint: bare docs.google.com / drive.google.com / app.asana.com
    / notion.so URLs and naked Asana/Slack IDs (gid <digits>, 16+ digit numbers)
    are redacted. Sanctioned Slack <url|label> links and <@mentions> are preserved.
  - 280-char cap measured + logged (NOT hard-truncated -- truncation is worse than
    length; the cap is enforced primarily via the prompt).

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

CONVERSATIONAL_CHAR_CAP = 280

# --- Slack entity protection ---------------------------------------------
# Slack angle-bracket tokens are sanctioned: <url|label>, <url>, <@U123>,
# <#C123|name>. Protect them wholesale so bare-URL/ID redaction never touches
# their internals.
_SLACK_TOKEN_RE = re.compile(r"<[^<>\n]+>")
_PLACEHOLDER = "\x00CORATOK{}\x00"

# --- markdown ------------------------------------------------------------
_BOLD_STAR_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_BOLD_UNDER_RE = re.compile(r"__([^_\n]+)__")
_HEADER_RE = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)
_HR_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")

# --- dashes --------------------------------------------------------------
# figure dash, en dash, em dash, horizontal bar -> hyphen
_DASH_RE = re.compile(r"[‒–—―]")

# --- emoji + shortcodes --------------------------------------------------
# Shortcodes must start with a letter so timestamps like 12:30:45 are untouched.
_SHORTCODE_RE = re.compile(r":[a-z][a-z0-9_+\-]*:")
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # symbols & pictographs (incl. 1F534 red, 1F7E1 yellow, 1F6A8 siren)
    "\U0001F000-\U0001F0FF"  # mahjong / dominoes / cards
    "\U00002600-\U000027BF"  # misc symbols + dingbats (incl. 2705 check, 274C cross, 2728 sparkles)
    "\U00002300-\U000023FF"  # technical (incl. 23F3 hourglass, 231A watch)
    "\U00002B00-\U00002BFF"  # misc symbols & arrows (incl. 2B50 star)
    "\U0001F1E6-\U0001F1FF"  # regional indicator (flags)
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"             # zero-width joiner (emoji sequences)
    "\U000020E3"             # combining enclosing keycap
    "]+",
    flags=re.UNICODE,
)

# --- source-opacity lint -------------------------------------------------
_BARE_DOC_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:docs\.google\.com|drive\.google\.com|app\.asana\.com|notion\.so)"
    # ')' excluded so a URL inside "(...)" or a markdown link "[label](...)"
    # leaves a balanced empty shell for the 8b cleanup (these URLs never
    # legitimately contain a close-paren).
    r"/?[^\s<>|)]*",
    re.IGNORECASE,
)
_GID_RE = re.compile(r"\bgid[:=]?\s*\d{4,}\b", re.IGNORECASE)
_NAKED_ID_RE = re.compile(r"\b\d{16,}\b")

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

    # 2. Flatten markdown bold to plain text.
    work = _BOLD_STAR_RE.sub(r"\1", work)
    work = _BOLD_UNDER_RE.sub(r"\1", work)

    # 3. Strip leading markdown headers (keep the header text as plain prose).
    work = _HEADER_RE.sub("", work)

    # 4. Tables -> prose (before HR removal so separator rows are handled here).
    work = _flatten_tables(work)

    # 5. Remove horizontal rules (whole-line).
    work = "\n".join(line for line in work.split("\n") if not _HR_RE.match(line))

    # 6. Dashes -> hyphen (voice contract bans em-dashes).
    work = _DASH_RE.sub("-", work)

    # 7. Strip emoji + :shortcode: tokens.
    work = _SHORTCODE_RE.sub("", work)
    work = _EMOJI_RE.sub("", work)

    # 8. Source-opacity lint (sanctioned links are placeholders, so safe).
    work = _BARE_DOC_URL_RE.sub("", work)
    work = _GID_RE.sub("", work)
    work = _NAKED_ID_RE.sub("", work)

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

    # 11. Measure the 280-char cap. Log only -- never hard-truncate.
    if len(work) > CONVERSATIONAL_CHAR_CAP:
        log.warning("reply_over_cap: %d chars (cap %d)", len(work), CONVERSATIONAL_CHAR_CAP)

    return work
