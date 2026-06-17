"""Single sanitizing egress boundary for every Slack send (Phase 2.1 / B1).

Closes the real F-5 / F-19 / N9 leaks on EVERY outbound path -- raw Drive/Asana
URLs (the recurring filing-summary source-opacity leak), naked GIDs/long IDs, and
utf-8->cp1252 mojibake (nudge DMs) -- by wrapping slack_sdk's sync WebClient
text-send methods (chat_postMessage / chat_update / chat_postEphemeral /
chat_scheduleMessage) at the CLASS level, once, idempotently. Bolt's `say` and
listener `client`, and every script's own WebClient, all funnel through these, so
the class patch covers all of them. Installed once from cora/__init__.py.

DELIBERATELY NARROW (a hard-won lesson, 2026-06-17 adversarial review): the
boundary applies ONLY redactions that are SAFE on arbitrary content and never
mangle structure -- mojibake repair + bare-URL/GID/long-ID redaction. It does
NOT voice-flatten (markdown/emoji/dashes/whitespace/code-fence/table collapse)
and does NOT redact named systems. Those are CONVERSATIONAL concerns applied to
interactive Q&A replies only, inline in app.py via reply_formatter.format_reply.
Running the conversational formatter on EVERY send would corrupt proactive
structured output -- code-fenced / fixed-width tables (cash pulse, metrics
digests, the strategy memo), intentional SIGNAL emoji on cards (confidence dots,
👍/👎 affordances), numbered rankings -- and would over-redact legitimate ops
alerts that name a system ("the QuickBooks sync failed"). The safety layer here
never breaks tables/emoji and never strips a system name from an ops alert.

NOT covered (documented residual, Phase 3): ~11 scheduled senders that POST raw
JSON to slack.com/api via httpx/requests bypass slack_sdk.WebClient and thus this
patch; the async WebClient is not patched (the bot is sync-only today). See the
forensic rebuild log.
"""

from __future__ import annotations

import functools
import logging

from .reply_formatter import redact_links_and_ids

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


# ── The single sanitizer ──────────────────────────────────────────────────────
def sanitize_text(text):
    """Universal SAFETY redaction applied to EVERY outbound Slack message body.

    Mojibake repair + bare-URL/GID/long-ID redaction (sanctioned <url|label>
    links preserved). NO voice/markdown/emoji flattening and NO named-source
    redaction -- those are conversational-only (see module docstring). Pure;
    non-string / empty input passes through untouched; never raises (the wrapper
    also guards)."""
    if not isinstance(text, str) or not text:
        return text
    text = repair_mojibake(text)
    text = redact_links_and_ids(text)
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

    _installed = True
    log.info("egress sanitizer installed on WebClient %s", ", ".join(_SEND_METHODS))
    return True
