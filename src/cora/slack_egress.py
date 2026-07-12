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


# ── The single sanitizer ──────────────────────────────────────────────────────
def sanitize_text(text):
    """Universal SAFETY transforms applied to EVERY outbound Slack message body.

    Mojibake repair + bare-URL/GID/long-ID redaction (sanctioned <url|label>
    links preserved) + markdown-bold normalization (**x** -> *x*, fence-/table-/
    token-safe, idempotent -- F-04). NO broader voice/emoji/whitespace flattening
    and NO named-source redaction -- those are conversational-only (see module
    docstring). Pure; non-string / empty input passes through untouched; never
    raises (the wrapper also guards)."""
    if not isinstance(text, str) or not text:
        return text
    text = repair_mojibake(text)
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
