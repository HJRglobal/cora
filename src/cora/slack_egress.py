"""Single sanitizing egress boundary for every Slack send (Phase 2.1 / B1).

Closes F-5, F-19, N9: the reply formatter previously sat on ONE of 85+ send
paths (the interactive Q&A reply) and was skipped for tool outputs and for ALL
proactive/deterministic sends -- so briefings, knowledge-review cards, filing
summaries, nudges, guard refusals, and every scheduled-script digest posted raw
(banned markdown, em-dashes, emoji, raw Drive/Asana URLs, named sources, mojibake).

Rather than bolt the formatter onto each call site (B2, brittle -- miss one and it
leaks), this wraps slack_sdk's text-send methods at the CLASS level, once,
idempotently. Bolt's `say` and listener `client`, and every script's own
`WebClient`, all funnel through `slack_sdk.web.client.WebClient.chat_postMessage`
/ `chat_update` / `chat_postEphemeral` / `chat_scheduleMessage`. Patching the
class covers them all -- including future call sites -- which is the D-034
doctrine done right (enforce the contract once at the boundary, in code).

Each wrapped method sanitizes the `text` kwarg via `sanitize_text()` before
delegating to the original SDK method. Genuine pre-formatted financial/data
tables opt OUT by passing `cora_verbatim=True`, which is popped before the SDK
call so it never reaches the Slack API. The bot is sync-only (socket mode +
sync WebClient), so only the sync WebClient is patched.

`install_egress_sanitizer()` is invoked once from `cora/__init__.py`, so every
process that imports any cora module (the bot and every scheduled script) gets
the boundary with zero per-call-site edits.
"""

from __future__ import annotations

import functools
import logging
import re

from .reply_formatter import format_reply

log = logging.getLogger(__name__)


# ── Mojibake repair (N9) ─────────────────────────────────────────────────────
# Some proactive text carries UTF-8 punctuation/emoji bytes mis-decoded as cp1252
# (e.g. the em-dash "-" surfaced as "a TM" sequences; the bullet, and the
# raising-hands emoji, mojibake'd in nudge DMs). Build the {corrupted: intended}
# map by REPLAYING the exact utf-8 -> cp1252 mis-decode on the intended chars, so
# this source stays clean and we never hand-type mojibake (transport layers
# silently normalize hand-typed mojibake). Same technique as hubspot_client.
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
    returned unchanged. Runs on every send (even verbatim) -- corrupted bytes are
    never intended output."""
    if not text:
        return text
    for bad, good in _MOJIBAKE_FIXES.items():
        if bad in text:
            text = text.replace(bad, good)
    return text


# ── Named-source redaction (F-19) ────────────────────────────────────────────
# `reply_formatter` redacts bare URLs/GIDs/long IDs but NOT literal source names
# ("QuickBooks", "the Standing ACTUALS sheet") -- source-opacity for names is
# prompt-only and drifts. This is a conservative CODE net for the financial /
# analytics sources that must never surface in a reply. It deliberately does NOT
# touch operational tools that are legitimately named in normal ops talk
# (Asana / Slack / Gmail / HubSpot / Shopify / Notion / Fireflies) -- redacting
# those would mangle valid replies. The prompt remains the primary rule; this is
# defense-in-depth for the highest-risk, least-ambiguous leaks.

# Tier 1 -- sheet/tab identifiers that have no legitimate place in any reply.
_SOURCE_IDENT_RE = re.compile(
    r"\b(?:the\s+)?Standing\s+ACTUALS(?:\s+(?:sheet|spreadsheet|tab))?\b"
    r"|\bCF[_\s]?SUMMARY\b",
    re.IGNORECASE,
)
# Tier 2 -- financial-system attributions: drop the whole "in/per/from QuickBooks"
# prepositional phrase so "$X per QuickBooks" reads as "$X". Bare standalone app
# names are left to the prompt (replacing them blind reads worse than the leak);
# a survivor is logged for monitoring.
_FIN_SOURCE = r"(?:QuickBooks(?:\s+Online)?|QBO|Clover|Polar\s+Analytics)"
_SOURCE_ATTRIB_RE = re.compile(
    r"\s*\b(?:in|per|from|via|on|according\s+to|based\s+on|"
    r"check|see|pulled\s+from|sourced\s+from|using|through)\s+" + _FIN_SOURCE + r"\b",
    re.IGNORECASE,
)
_FIN_SOURCE_BARE_RE = re.compile(r"\b(?:QuickBooks(?:\s+Online)?|QBO)\b", re.IGNORECASE)
_REDACT_WS_RE = re.compile(r"[ \t]{2,}")
_REDACT_SPACE_PUNCT_RE = re.compile(r"\s+([.,;:!?])")


def redact_named_sources(text: str) -> str:
    """Remove financial/analytics source NAMES from outbound text (F-19).

    Tier 1 sheet/tab identifiers are removed outright; Tier 2 source-attribution
    phrases ("... per QuickBooks") have the prepositional phrase dropped. Runs
    BEFORE format_reply so that module's whitespace pass cleans any residue.
    """
    if not text:
        return text
    work = _SOURCE_IDENT_RE.sub("", text)
    work = _SOURCE_ATTRIB_RE.sub("", work)
    if _FIN_SOURCE_BARE_RE.search(work):
        # A bare app name survived (no attributive preposition). Leave it -- the
        # prompt owns that case -- but log so drift is visible.
        log.warning("named_source_survived: bare financial-system name in outbound text")
    work = _REDACT_WS_RE.sub(" ", work)
    work = _REDACT_SPACE_PUNCT_RE.sub(r"\1", work)
    return work


# ── The single sanitizer ──────────────────────────────────────────────────────
def sanitize_text(text, *, verbatim: bool = False):
    """Sanitize one outbound message body. The single egress contract.

    verbatim=True (genuine pre-formatted financial/data tables): only mojibake
    repair runs, so the table layout survives un-mangled. Otherwise: mojibake
    repair -> named-source redaction -> full voice/style format (markdown flatten,
    dash/emoji strip, bare-URL/GID redaction). Non-string / empty input passes
    through untouched. Pure function; never raises (callers also guard)."""
    if not isinstance(text, str) or not text:
        return text
    text = repair_mojibake(text)
    if verbatim:
        return text
    text = redact_named_sources(text)
    text = format_reply(text)
    return text


# ── Class-level WebClient wrapper ──────────────────────────────────────────────
_SEND_METHODS = (
    "chat_postMessage",
    "chat_update",
    "chat_postEphemeral",
    "chat_scheduleMessage",
)
_installed = False


def _make_wrapper(original):
    @functools.wraps(original)
    def wrapper(self, *args, cora_verbatim: bool = False, **kwargs):
        text = kwargs.get("text")
        if isinstance(text, str) and text:
            try:
                kwargs["text"] = sanitize_text(text, verbatim=cora_verbatim)
            except Exception:  # noqa: BLE001 -- never let sanitization block a send
                log.exception("egress sanitize failed; sending text raw")
        return original(self, *args, **kwargs)

    wrapper._cora_egress_wrapped = True  # type: ignore[attr-defined]
    return wrapper


def install_egress_sanitizer() -> bool:
    """Patch slack_sdk's sync WebClient text-send methods to sanitize `text`.

    Idempotent (safe to call repeatedly and from multiple entry points). Returns
    True if the patch is in place after the call, False if slack_sdk is absent."""
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

    _installed = True
    log.info("egress sanitizer installed on WebClient %s", ", ".join(_SEND_METHODS))
    return True
